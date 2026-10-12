"""
导师 Agent（Tutor）- 三种差异化引导策略的执行者

教学理念不变：
  - teaching → 示例复现模式（example_reproduction）
  - problem_solving → 苏格拉底式提问（绝不直接给答案）
  - factual → 直接回答 + 知识拓展
  - 步骤计划全部完成时切换为「庆祝 + 知识拓展」模式
  - 凝练简洁 / 具体肯定不空洞表扬 / 鼓励语由系统生成
  - LLM 失效时使用降级引导

学习体验优化：
  - 引导内容支持 SSE 流式输出（token 事件 → 前端打字机效果）
"""

import logging

from app.agents.base import get_services
from app.memory import memory
from app.models.domain import (
    ClassificationResult,
    CognitiveState,
    GuidanceAction,
    GuidanceMode,
    QuestionType,
)
from app.harness.policies import (
    QUESTION_TYPE_TO_MODE,
    build_continuation_section,
    build_experience_hints_section,
    build_step_completion_section,
    build_strategy_hints_section,
    fallback_guide,
    generate_encouragement,
    log_fallback,
)
from app.prompts.tutor import GUIDE_PROMPTS
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


async def tutor_node(state, config: RunnableConfig) -> dict:
    ctx, bus, gateway = get_services(state, config)
    t0 = bus.agent_enter("tutor", "guiding")

    conversation_id = state.get("conversation_id", "")
    profile_key = state.get("profile_key") or conversation_id   # v5.0 画像键
    user_message = state.get("user_message", "")
    errors: list[str] = []

    classification = ClassificationResult.model_validate(state["classification"])
    cognitive_state = CognitiveState.model_validate(state["cognitive_state"])
    is_step_complete = bool(state.get("is_step_complete"))

    mode = QUESTION_TYPE_TO_MODE[classification.question_type]

    try:
        # ===== 构建引导 Prompt（移植原 guidance_chain 的装配逻辑） =====
        prompt = _build_guide_prompt(
            mode, classification, cognitive_state, ctx, is_step_complete
        )

        context_str = (
            f"\n\n【对话历史】\n{ctx.history_text}" if ctx.history_text else ""
        )
        user_prompt = f"学生的问题：{ctx.user_message}{context_str}"

        # ===== 流式生成引导内容（学习体验优化） =====
        content_parts: list[str] = []
        async for delta in gateway.stream(prompt, user_prompt):
            content_parts.append(delta)
            bus.token(delta)
        content = "".join(content_parts).strip()

        if not content:
            raise ValueError("导师 LLM 返回空内容")

        # ===== 组装引导动作 =====
        active_plan = memory.working.get_active_step_plan(conversation_id)
        example = (
            active_plan.example
            if (mode == GuidanceMode.EXAMPLE_REPRODUCTION and active_plan)
            else None
        )

        encouragement = generate_encouragement(
            cognitive_state, classification,
            memory.profile.get_or_create(profile_key),
        )

        guidance = GuidanceAction(
            mode=mode,
            content=content,
            example=example,
            encouragement=encouragement,
            is_continuation=classification.is_continuation,
            continuation_context=(
                ctx.unresolved_info.get("topic")
                if ctx.unresolved_info and classification.is_continuation
                else None
            ),
        )

    except Exception as e:
        log_fallback("引导链", e)
        errors.append(f"tutor: {e}")
        guidance = fallback_guide(
            _rebuild_perception(state),
            classification,
            cognitive_state,
            active_plan=memory.working.get_active_step_plan(conversation_id),
            is_step_complete=is_step_complete,
        )
        bus.token(guidance.content)  # 降级内容也推送给前端

    bus.agent_exit("tutor", t0)
    return {
        "guidance": guidance.model_dump(mode="json"),
        "errors": list(state.get("errors") or []) + errors,
    }


def _build_guide_prompt(
    mode: GuidanceMode,
    classification: ClassificationResult,
    cognitive_state: CognitiveState,
    ctx,
    is_step_complete: bool,
) -> str:
    """移植原 guidance_chain 的 Prompt 装配逻辑"""
    prompt_template = GUIDE_PROMPTS.get(
        mode.value, GUIDE_PROMPTS["socratic_questioning"]
    )

    prompt = (
        prompt_template
        .replace("{cognitive_level}", cognitive_state.level.value)
        .replace("{engagement_level}", f"{cognitive_state.engagement_level:.2f}")
        .replace("{frustration_level}", f"{cognitive_state.frustration_level:.2f}")
        .replace("{user_profile_section}", ctx.profile_section)
        .replace(
            "{continuation_section}",
            build_continuation_section(classification.is_continuation, ctx.unresolved_info),
        )
        .replace(
            "{strategy_hints_section}",
            build_strategy_hints_section(ctx.strategy_hints),
        )
        .replace(
            "{experience_hints_section}",
            build_experience_hints_section(ctx.experience_hints),
        )
        .replace(
            "{step_completion_section}",
            build_step_completion_section(is_step_complete),
        )
    )

    if ctx.step_plan_section:
        prompt = f"{ctx.step_plan_section}\n\n{prompt}"
    return prompt


def _rebuild_perception(state: dict):
    from app.models.domain import PerceptionResult

    data = state.get("perception") or {}
    try:
        return PerceptionResult.model_validate(data)
    except Exception:
        return PerceptionResult(raw_input=state.get("user_message", ""))
