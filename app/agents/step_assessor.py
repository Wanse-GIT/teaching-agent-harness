"""
步骤评估官 Agent（Step Assessor）

教学理念不变：
  - 「宁可推进也不要卡住」的推进原则
  - 学生尝试超过 2 次自动推进（避免挫败感过重）
  - 步骤推进 / 重试 / 卡住三种状态语义
  - 计划全部完成时标记问题已解决（触发导师的庆祝+知识拓展模式）
"""

import logging

from app.agents.base import get_services
from app.memory import memory
from app.models.domain import (
    LLMStepEvalOutput,
    StepEvaluationResult,
)
from app.harness.policies import heuristic_step_evaluate, log_fallback
from app.prompts.step_assessor import STEP_EVALUATION_PROMPT
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


async def step_assessor_node(state, config: RunnableConfig) -> dict:
    ctx, bus, gateway = get_services(state, config)
    t0 = bus.agent_enter("step_assessor", "assessing_step")

    conversation_id = state.get("conversation_id", "")
    user_message = state.get("user_message", "")
    errors: list[str] = []
    step_evaluation: StepEvaluationResult | None = None

    try:
        plan = memory.working.get_active_step_plan(conversation_id)
        if plan and plan.current_step:
            step_evaluation = await _evaluate_step(gateway, plan, user_message, ctx)

            # 学生尝试超过2次自动推进（教学理念：宁可推进也不要卡住）
            current = plan.current_step
            if (
                current
                and not step_evaluation.step_completed
                and current.attempts >= 2
            ):
                logger.info(
                    f"步骤 {current.index} 已尝试{current.attempts}次，自动推进"
                )
                step_evaluation.step_completed = True
                step_evaluation.should_advance = True

            updated_plan = memory.working.advance_step_plan(conversation_id, step_evaluation)

            # 步骤计划全部完成 → 标记问题解决（保留 plan 数据供庆祝模式使用）
            if updated_plan and updated_plan.is_complete:
                logger.info("步骤计划全部完成！")
                memory.working.mark_problem_resolved(conversation_id)
        else:
            # 无活跃计划（计划可能尚未生成完成）→ 跳过评估
            step_evaluation = StepEvaluationResult(step_completed=True, should_advance=True)

    except Exception as e:
        logger.error(f"步骤评估官执行失败: {e}", exc_info=True)
        errors.append(f"step_assessor: {e}")

    # 刷新步骤计划 Prompt 片段（推进后导师需要最新进度）并写回上下文
    ctx.step_plan_section = memory.working.build_step_plan_prompt_section(conversation_id)
    ctx.has_active_plan = memory.working.get_active_step_plan(conversation_id) is not None

    # 判断步骤计划是否全部完成（peek 包含已完成的计划）
    plan_any = memory.working.peek_step_plan(conversation_id)
    is_step_complete = bool(plan_any and plan_any.is_complete)

    bus.agent_exit("step_assessor", t0)
    return {
        "step_evaluation": (
            step_evaluation.model_dump(mode="json") if step_evaluation else None
        ),
        "is_step_complete": is_step_complete,
        "step_plan_just_completed": is_step_complete,
        "context": ctx.to_dict(),
        "errors": list(state.get("errors") or []) + errors,
    }


async def _evaluate_step(gateway, plan, user_message, ctx) -> StepEvaluationResult:
    """移植原 step_evaluation_chain"""
    current = plan.current_step
    if not current:
        return StepEvaluationResult(step_completed=True, should_advance=True)

    step_info_parts = [
        f"步骤 {current.index}/{len(plan.steps)}：{current.title}",
        f"步骤描述：{current.description}",
        f"预期概念：{', '.join(current.expected_concepts) or '无特定'}",
        f"学生已尝试次数：{current.attempts}",
    ]
    if current.student_response_summary:
        step_info_parts.append(f"学生上次回答摘要：{current.student_response_summary}")

    prompt = STEP_EVALUATION_PROMPT.replace(
        "{current_step_info}", "\n".join(step_info_parts)
    )

    user_parts = [f"学生的回答：{user_message}"]
    if ctx.history_text:
        user_parts.append(f"\n近期对话：\n{ctx.history_text}")

    try:
        eval_out = await gateway.json(prompt, "\n".join(user_parts), LLMStepEvalOutput)
        return StepEvaluationResult(
            step_completed=eval_out.step_completed,
            student_understood=eval_out.student_understood,
            should_advance=eval_out.should_advance,
            stay_reason=eval_out.stay_reason,
            hint_to_provide=eval_out.hint_to_provide,
            concept_mastered=eval_out.concept_mastered,
        )
    except Exception as e:
        log_fallback("步骤评估链", e)
        return heuristic_step_evaluate(user_message, current)
