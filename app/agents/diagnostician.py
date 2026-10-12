"""
诊断师 Agent（感知 + 分类 + 认知评估）

教学理念不变：
  - 感知+分类合并为一次 LLM 调用（延迟优化）
  - 学习意愿三分法（eager/troubled/indifferent）为核心判据
  - factual 类型跳过 LLM 认知评估，使用轻量评估（延迟优化）
  - 延续对话必须保持与上次相同的 questionType
  - LLM 失效时使用规则降级分类器（三层情绪模型）

额外职责（移植自原 agent.process 的确定性处理）：
  - 诊断后应用教学策略（强制延续/清理过期计划/登记活跃问题）
"""

import logging

from langchain_core.runnables import RunnableConfig

from app.agents.base import get_services
from app.memory import memory
from app.models.domain import (
    ClassificationResult,
    CognitiveLevel,
    CognitiveState,
    LLMClassificationOutput,
    LLMPerceptionOutput,
    PerceptionResult,
)
from app.harness.policies import (
    DESIRE_MAP,
    LEVEL_MAP,
    TYPE_MAP,
    apply_diagnosis_policies,
    check_continuation_signals,
    heuristic_assess,
    lightweight_assess,
    log_fallback,
    rule_based_classify,
)
from app.prompts.diagnostician import (
    ASSESSMENT_SYSTEM_PROMPT,
    PERCEPTION_CLASSIFICATION_MERGED_PROMPT,
)

logger = logging.getLogger(__name__)


async def diagnostician_node(state, config: RunnableConfig) -> dict:
    ctx, bus, gateway = get_services(state, config)
    t0 = bus.agent_enter("diagnostician", "diagnosing")

    conversation_id = state.get("conversation_id", "")
    profile_key = state.get("profile_key") or conversation_id   # v5.0 画像键
    user_message = state.get("user_message", "")
    errors: list[str] = []

    try:
        # ===== 1. 感知 + 分类（合并 LLM 调用） =====
        perception, classification = await _perceive_and_classify(
            gateway, user_message, ctx
        )

        # ===== 2. 诊断后教学策略（确定性，收口在 Harness policies） =====
        active_plan = memory.working.get_active_step_plan(conversation_id)
        has_active_plan = active_plan is not None
        _, classification = apply_diagnosis_policies(
            conversation_id, user_message, perception, classification,
            has_active_plan, active_plan,
        )

        # ===== 3. 会话连续性簿记（移植） =====
        session = memory.working.get_or_create(conversation_id)
        if classification.is_continuation:
            memory.working.increment_problem_turn(conversation_id)
        else:
            # 清理残留的已完成步骤计划 / 用户切换话题时清除计划
            if session.active_step_plan and session.active_step_plan.is_complete:
                logger.info(f"清理已完成的步骤计划: {session.active_step_plan.topic}")
                memory.working.clear_step_plan(conversation_id)
            elif has_active_plan:
                logger.info(f"用户切换话题，清除步骤计划: {active_plan.topic}")
                memory.working.clear_step_plan(conversation_id)
            if perception.topic:
                memory.working.set_active_problem(
                    conversation_id, perception.topic, classification.question_type
                )

        # ===== 4. 认知评估（factual 走轻量路径——教学理念） =====
        if classification.question_type.value == "factual":
            prev_state = session.cognitive_state
            user_profile = memory.profile.get_or_create(profile_key)
            cognitive_state = lightweight_assess(
                perception, classification, prev_state, user_profile
            )
            logger.info(
                f"factual类型跳过评估链，使用轻量评估: level={cognitive_state.level.value}"
            )
        else:
            cognitive_state = await _assess(
                gateway, perception, classification, ctx,
                session.cognitive_state,
                memory.profile.get_or_create(profile_key),
            )

        # ===== 5. 更新工作记忆的会话状态 =====
        memory.working.update(
            conversation_id,
            perception=perception,
            classification=classification,
            cognitive_state=cognitive_state,
        )

        logger.info(
            f"学习意愿检测: level={classification.learning_desire_level.value}, "
            f"score={classification.learning_desire_score:.2f}, "
            f"emotions={classification.detected_emotions}, "
            f"type={classification.question_type.value}"
        )

        bus.agent_exit("diagnostician", t0)
        return {
            "perception": perception.model_dump(mode="json"),
            "classification": classification.model_dump(mode="json"),
            "cognitive_state": cognitive_state.model_dump(mode="json"),
            "is_continuation": classification.is_continuation,
            "errors": list(state.get("errors") or []) + errors,
        }

    except Exception as e:
        logger.error(f"诊断师执行失败: {e}", exc_info=True)
        errors.append(f"diagnostician: {e}")
        bus.agent_exit("diagnostician", t0, note=str(e))
        # 最终兜底：空感知 + 规则分类 + 启发式评估
        perception = PerceptionResult(raw_input=user_message, is_continuation=False)
        classification = rule_based_classify(perception)
        cognitive_state = heuristic_assess(perception, classification)
        return {
            "perception": perception.model_dump(mode="json"),
            "classification": classification.model_dump(mode="json"),
            "cognitive_state": cognitive_state.model_dump(mode="json"),
            "is_continuation": False,
            "errors": list(state.get("errors") or []) + errors,
        }


# ============================================================
# 感知 + 分类（合并调用，移植原 perception_classification_chain）
# ============================================================

async def _perceive_and_classify(gateway, user_message, ctx):
    from app.models.domain import QuestionType, LearningDesireLevel

    context_parts = []
    if ctx.history_text:
        context_parts.append(f"【对话历史】\n{ctx.history_text}")
    if ctx.unresolved_info:
        context_parts.append(
            f"【⚠️ 之前未解决的问题】{ctx.unresolved_info.get('topic', '')}\n"
            f"已交互轮数：{ctx.unresolved_info.get('turn_count', 0)}"
        )
    elif ctx.prev_problem_resolved:
        context_parts.append(
            "【✅ 之前的问题已经完全解决，学生已掌握相关知识】\n"
            "如果学生提出的是新的问题或话题，必须判定为新对话（isContinuation=false），"
            "不要因为对话历史中有相关内容就误判为延续。"
        )
    context_str = "\n\n".join(context_parts)
    full_input = (
        f"{context_str}\n\n【当前输入】\n{user_message}" if context_str else user_message
    )

    try:
        # 合并调用：一次 LLM 调用同时产出感知与分类（延迟优化，教学理念不变）
        merged = await gateway.json_dict(
            PERCEPTION_CLASSIFICATION_MERGED_PROMPT, full_input
        )
        perception_out = LLMPerceptionOutput.model_validate(merged)
        classification_out = LLMClassificationOutput.model_validate(merged)

        # 延续性增强（规则信号兜底）
        is_continuation = perception_out.is_continuation or (
            ctx.unresolved_info is not None and check_continuation_signals(user_message)
        )

        perception = PerceptionResult(
            raw_input=user_message,
            intent=perception_out.intent,
            subject=perception_out.subject,
            topic=perception_out.topic,
            prerequisites=perception_out.prerequisites,
            difficulty_indicators=perception_out.difficulty_indicators,
            keywords=perception_out.keywords,
            emotional_tone=perception_out.emotional_tone,
            is_continuation=is_continuation,
            continuation_topic=(
                perception_out.continuation_topic if is_continuation else None
            ),
        )

        desire_level = DESIRE_MAP.get(
            classification_out.learning_desire_level, LearningDesireLevel.INDIFFERENT
        )

        # 延续对话：保持上一次的问题类型（教学理念）
        if classification_out.is_continuation and ctx.unresolved_info:
            prev_type = ctx.unresolved_info.get("question_type")
            if prev_type:
                prev_type_enum = (
                    prev_type if isinstance(prev_type, QuestionType)
                    else QuestionType(prev_type)
                )
                classification = ClassificationResult(
                    question_type=prev_type_enum,
                    learning_desire_level=desire_level,
                    learning_desire_score=classification_out.learning_desire_score,
                    detected_emotions=classification_out.detected_emotions,
                    confidence=0.85,
                    reasoning=(
                        f"LLM判断为延续对话，保持上次分类：{prev_type_enum.value}。"
                        f"{classification_out.continuation_reason or ''}"
                    ),
                    is_continuation=True,
                )
                return perception, classification

        classification = ClassificationResult(
            question_type=TYPE_MAP.get(
                classification_out.question_type, QuestionType.PROBLEM_SOLVING
            ),
            learning_desire_level=desire_level,
            learning_desire_score=classification_out.learning_desire_score,
            detected_emotions=classification_out.detected_emotions,
            confidence=classification_out.confidence,
            reasoning=classification_out.reasoning,
            sub_categories=classification_out.sub_categories,
            is_continuation=False,
        )
        return perception, classification

    except Exception as e:
        log_fallback("感知+分类合并链", e)
        is_continuation = (
            ctx.unresolved_info is not None and check_continuation_signals(user_message)
        )
        perception = PerceptionResult(
            raw_input=user_message,
            intent="unknown",
            keywords=user_message.split()[:5],
            emotional_tone="neutral",
            is_continuation=is_continuation,
            continuation_topic=(
                ctx.unresolved_info.get("topic") if is_continuation else None
            ),
        )
        return perception, rule_based_classify(perception)


async def _assess(gateway, perception, classification, ctx, prev_state, user_profile):
    parts = [
        f"【学生输入】{perception.raw_input}",
        f"【问题类型】{classification.question_type.value}",
        f"【识别意图】{perception.intent}",
    ]
    if perception.emotional_tone != "neutral":
        parts.append(f"【情绪基调】{perception.emotional_tone}")
    if ctx.history_text:
        parts.append(f"【近期对话】\n{ctx.history_text}")
    if prev_state:
        parts.append(
            f"【上次认知状态】层级={prev_state.level.value}, "
            f"参与度={prev_state.engagement_level:.2f}, "
            f"挫败感={prev_state.frustration_level:.2f}"
        )
    if classification.is_continuation:
        parts.append("【⚠️ 这是延续之前未解决问题的对话】")
    if user_profile:
        parts.append(ctx.profile_section)

    from app.models.domain import LLMAssessmentOutput

    try:
        assessment_out = await gateway.json(
            ASSESSMENT_SYSTEM_PROMPT, "\n".join(parts), LLMAssessmentOutput
        )
        return CognitiveState(
            level=LEVEL_MAP.get(assessment_out.level, CognitiveLevel.UNDERSTAND),
            confidence_score=assessment_out.confidence_score,
            knowledge_gaps=assessment_out.knowledge_gaps,
            learning_style=(
                assessment_out.learning_style
                or (user_profile.preferred_learning_style if user_profile else "visual")
            ),
            engagement_level=assessment_out.engagement_level,
            frustration_level=assessment_out.frustration_level,
        )
    except Exception as e:
        log_fallback("评估链", e)
        return heuristic_assess(perception, classification, prev_state, user_profile)
