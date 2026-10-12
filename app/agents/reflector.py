"""
反思进化师 Agent（Reflector）- 后台子图

教学理念不变：
  - 每轮引导后反思策略效果（策略有效性/学生进步度/调整建议/画像更新建议）
  - 反思结果沉淀到长期记忆（画像 + 策略经验）→ 「进化」
  - 进步显著且挫败低 → 自动标记问题解决（及时正反馈）
  - teaching/problem_solving 首次提问时懒加载生成步骤计划（不阻塞回复）

Harness 设计：反思子图由 Harness 在响应送达后触发，不经过 Supervisor 调度
（后台执行保证回复延迟，教学闭环语义不变）。
"""

import logging

from pydantic import ValidationError

from app.memory import memory
from app.models.domain import (
    ClassificationResult,
    CognitiveState,
    GuidanceAction,
    LLMReflectionOutput,
    PerceptionResult,
    QuestionType,
    ReflectionResult,
    StepItem,
    StepPlan,
    normalize_profile_updates,
)
from app.harness.policies import (
    QUESTION_TYPE_TO_MODE,
    heuristic_reflect,
    log_fallback,
)
from app.prompts.reflector import REFLECTION_SYSTEM_PROMPT
from app.prompts.tutor import STEP_PLAN_PROMPTS

logger = logging.getLogger(__name__)


# ============================================================
# 反思（LLM）
# ============================================================

async def reflect(gateway, snapshot: dict) -> ReflectionResult:
    """移植原 reflection_chain"""
    classification: ClassificationResult = snapshot["classification"]
    cognitive_state: CognitiveState = snapshot["cognitive_state"]
    guidance: GuidanceAction = snapshot["guidance"]

    parts = [
        f"【问题类型】{classification.question_type.value}",
        f"【引导模式】{guidance.mode.value}",
        f"【认知层级】{cognitive_state.level.value}",
        f"【参与度】{cognitive_state.engagement_level:.2f}",
        f"【挫败感】{cognitive_state.frustration_level:.2f}",
        f"【知识缺口】{', '.join(cognitive_state.knowledge_gaps) or '无明显缺口'}",
    ]
    if classification.is_continuation:
        parts.append("【延续对话】是，已交互轮数较多")
    history_text = snapshot.get("history_text", "")
    if history_text:
        parts.append(f"【近期对话】\n{history_text}")
    profile_section = snapshot.get("profile_section", "")
    if profile_section:
        parts.append(profile_section)

    try:
        reflection_out = await gateway.json(
            REFLECTION_SYSTEM_PROMPT, "\n".join(parts), LLMReflectionOutput
        )
        profile_updates = normalize_profile_updates(reflection_out.profile_updates)
        return ReflectionResult(
            strategy_effectiveness=reflection_out.strategy_effectiveness,
            student_progress=reflection_out.student_progress,
            adjustments=reflection_out.adjustments,
            insights=reflection_out.insights,
            profile_updates=profile_updates,
        )
    except Exception as e:
        log_fallback("反思链", e)
        return heuristic_reflect(cognitive_state, classification)


# ============================================================
# 步骤计划懒加载（移植原 _async 步骤计划生成逻辑）
# ============================================================

async def maybe_generate_step_plan(gateway, snapshot: dict):
    """teaching/problem_solving 且无活跃计划时生成步骤计划（降级方案兜底）"""
    classification: ClassificationResult = snapshot["classification"]
    perception: PerceptionResult = snapshot["perception"]
    cognitive_state: CognitiveState = snapshot["cognitive_state"]
    conversation_id = snapshot["conversation_id"]

    if classification.question_type == QuestionType.FACTUAL:
        return None

    # 已有任何步骤计划（含刚完成的）都不再生成——刚完成的计划需保留给庆祝模式展示，
    # 会在学生切换新话题时清理
    if memory.working.peek_step_plan(conversation_id) is not None:
        return None

    prompt_template = STEP_PLAN_PROMPTS.get(classification.question_type.value)
    if not prompt_template:
        return None

    profile_section = snapshot.get("profile_section", "")
    prompt = (
        prompt_template
        .replace("{cognitive_level}", cognitive_state.level.value)
        .replace(
            "{knowledge_gaps}",
            ", ".join(cognitive_state.knowledge_gaps) or "暂无明显缺口",
        )
        .replace("{frustration_level}", f"{cognitive_state.frustration_level:.2f}")
        .replace("{user_profile_section}", profile_section)
    )

    try:
        from app.llm.fallback import safe_parse_json

        raw = await gateway.text(prompt, f"学生的问题：{perception.raw_input}")
        result = safe_parse_json(raw)
        if not result:
            raise ValueError("步骤计划输出不是有效 JSON")

        steps = []
        for s in result.get("steps", []):
            steps.append(StepItem(
                index=s.get("index", len(steps) + 1),
                title=s.get("title", f"步骤 {len(steps) + 1}"),
                description=s.get("description", ""),
                hint=s.get("hint", ""),
                expected_concepts=s.get("expectedConcepts", []),
            ))
        if not steps:
            raise ValueError("步骤计划为空")

        plan = memory.working.create_step_plan(
            conversation_id,
            topic=result.get("topic", perception.raw_input[:50]),
            question_type=classification.question_type,
            guidance_mode=QUESTION_TYPE_TO_MODE[classification.question_type],
            steps=steps,
            example=result.get("example"),
        )

        # 如果之前没有设置活跃问题，这里补充设置
        session = memory.working.get_or_create(conversation_id)
        if not session.active_problem:
            memory.working.set_active_problem(
                conversation_id, plan.topic, classification.question_type
            )

        logger.info(
            f"[后台] 步骤计划生成完成: topic={plan.topic}, steps={len(steps)}"
        )
        return plan

    except Exception as e:
        log_fallback("步骤计划生成链", e)
        # 降级：规则生成兜底计划（保证步骤追踪体验不中断）
        plan = _fallback_plan(snapshot)
        if plan:
            memory.working.create_step_plan(
                conversation_id,
                topic=plan.topic,
                question_type=plan.question_type,
                guidance_mode=plan.guidance_mode,
                steps=plan.steps,
                example=plan.example,
            )
            session = memory.working.get_or_create(conversation_id)
            if not session.active_problem:
                memory.working.set_active_problem(
                    conversation_id, plan.topic, classification.question_type
                )
            logger.info(f"[后台] 降级步骤计划生成: steps={len(plan.steps)}")
        return plan


def _fallback_plan(snapshot) -> StepPlan | None:
    from app.harness.policies import fallback_step_plan

    try:
        classification = snapshot["classification"]
        perception = snapshot["perception"]
        return fallback_step_plan(perception, classification)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning(f"降级步骤计划失败: {e}")
        return None
