"""
Supervisor 调度官（多 Agent 协作的中枢）

职责：基于黑板（TutorState）决定下一个执行哪个专家Agent：
  diagnostician → step_assessor → tutor → finish

实现双层决策：
  1. LLM 路由（灵活）：轻量结构化输出，读取状态摘要进行判断
  2. 规则兜底（可靠）：LLM 失效/输出非法时，按教学理念闭环的固定次序路由
     （该规则链逐条对应原项目 agent.process 的执行顺序，教学理念不变）
"""

import logging

from pydantic import ValidationError

from app.agents.base import get_services
from app.harness.context import TurnContext
from app.harness.events import TurnEventBus
from app.harness.state import TutorState
from app.models.domain import QuestionType, RouteDecision, ROUTE_TARGETS
from app.prompts.step_assessor import SUPERVISOR_ROUTING_PROMPT
from app.llm.fallback import safe_parse_json
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


# ============================================================
# 图节点
# ============================================================

async def supervisor_node(state: TutorState, config: RunnableConfig) -> dict:
    ctx, bus, gateway = get_services(state, config)
    t0 = bus.agent_enter("supervisor", "routing")

    hops = state.get("hops", 0) + 1
    decision = await _route(state, ctx, gateway, bus)

    bus.agent_exit("supervisor", t0)
    bus.emit(
        "route",
        **{
            "from": "supervisor",
            "to": decision.target,
            "reason": decision.reason,
            "source": decision.source,
            "hops": hops,
        },
    )

    return {
        "route": decision.model_dump(),
        "route_trace": list(state.get("route_trace") or []) + [decision.model_dump()],
        "hops": hops,
    }


def route_after_supervisor(state: TutorState) -> str:
    """条件边：读取调度决策映射到下一个节点"""
    route = state.get("route") or {}
    target = route.get("target", "finish")
    if target not in ROUTE_TARGETS:
        target = "finish"
    return target


# ============================================================
# 路由决策（LLM → 规则兜底）
# ============================================================

async def _route(state, ctx: TurnContext, gateway, bus) -> RouteDecision:
    from app.config import settings

    # 纯规则模式（supervisor_llm_routing=False）时跳过 LLM，延迟最低
    if not settings.supervisor_llm_routing:
        return rule_based_route(state, ctx)

    # LLM 路由（失败时规则兜底；输出过教学闭环约束）
    try:
        decision = await llm_route(state, ctx, gateway)
        if decision:
            constrained = _apply_teaching_constraints(state, ctx, decision)
            if constrained is not None:
                return constrained
            return decision
    except Exception as e:
        logger.warning(f"[Supervisor] LLM 路由失败，使用规则兜底: {e}")

    return rule_based_route(state, ctx)


def _apply_teaching_constraints(
    state: TutorState, ctx: TurnContext, decision: RouteDecision
) -> RouteDecision | None:
    """
    LLM 路由决策的教学闭环约束（深度优化新增）：

    LLM 路由灵活但不完全可靠，这里把教学理念的硬性次序作为约束收口，
    防止 LLM 跳过关键环节（与 rule_based_route 保持一致的不变量）：
      1. 引导已产出 → 必须 finish
      2. 本轮尚未诊断 → 必须 diagnostician
      3. 活跃步骤计划 + 尚未评估步骤 + 非 factual → 必须 step_assessor
    返回 None 表示 LLM 决策可接受；否则返回修正后的决策。
    """
    from app.models.domain import ClassificationResult, QuestionType

    if state.get("guidance") and decision.target != "finish":
        return RouteDecision(
            target="finish",
            reason=f"导师已产出引导（LLM建议{decision.target}，已按教学闭环约束结束）",
            source="llm+constraint",
        )

    classification_data = state.get("classification")
    if not classification_data and decision.target != "diagnostician":
        return RouteDecision(
            target="diagnostician",
            reason="本轮尚未诊断（LLM路由已约束）",
            source="llm+constraint",
        )

    if (
        classification_data
        and ctx.has_active_plan
        and state.get("step_evaluation") is None
    ):
        try:
            classification = ClassificationResult.model_validate(classification_data)
        except Exception:
            return None  # 诊断数据异常时交给规则链重新诊断
        if (
            classification.question_type != QuestionType.FACTUAL
            and decision.target != "step_assessor"
        ):
            return RouteDecision(
                target="step_assessor",
                reason="步骤计划活跃，须先评估当前步骤（LLM路由已约束）",
                source="llm+constraint",
            )

    return None


async def llm_route(state, ctx: TurnContext, gateway) -> RouteDecision | None:
    """LLM 路由：轻量结构化输出（失败抛异常，由上层兜底）"""
    summary = _state_summary(state, ctx)
    prompt = SUPERVISOR_ROUTING_PROMPT.replace("{state_summary}", summary)

    raw = await gateway.text(prompt, f"学生消息：{state.get('user_message', '')}")
    parsed = safe_parse_json(raw)
    if not parsed:
        raise ValueError(f"Supervisor 输出不是 JSON: {raw[:100]}")

    target = str(parsed.get("next", "")).strip()
    if target not in ROUTE_TARGETS:
        raise ValueError(f"Supervisor 给出非法路由目标: {target}")

    return RouteDecision(
        target=target,
        reason=str(parsed.get("reason", ""))[:120],
        source="llm",
    )


def rule_based_route(state: TutorState, ctx: TurnContext) -> RouteDecision:
    """
    规则路由（教学理念闭环的固定次序，逐条对应原项目流程）：
      1. 引导已产出 → finish
      2. 本轮尚未诊断 → diagnostician
      3. 已诊断 + 有活跃步骤计划（非factual）+ 尚未评估步骤 → step_assessor
      4. 否则 → tutor
    """
    from app.config import settings
    from app.models.domain import ClassificationResult

    hops = state.get("hops", 0)
    if state.get("guidance"):
        return RouteDecision(target="finish", reason="导师已产出引导，本轮结束", source="rules")

    if hops >= settings.max_supervisor_hops:
        logger.warning(f"[Supervisor] 达到最大调度次数({settings.max_supervisor_hops})，强制结束")
        return RouteDecision(target="finish", reason="达到调度上限，强制结束", source="rules")

    classification_data = state.get("classification")
    if not classification_data:
        return RouteDecision(
            target="diagnostician", reason="本轮尚未诊断，先感知分类评估", source="rules"
        )

    try:
        classification = ClassificationResult.model_validate(classification_data)
    except Exception:
        return RouteDecision(target="diagnostician", reason="诊断数据异常，重新诊断", source="rules")

    if (
        ctx.has_active_plan
        and state.get("step_evaluation") is None
        and classification.question_type != QuestionType.FACTUAL
    ):
        return RouteDecision(
            target="step_assessor",
            reason="学生在推进步骤任务，先评估当前步骤完成情况",
            source="rules",
        )

    return RouteDecision(target="tutor", reason="诊断就绪，由导师生成引导", source="rules")


def _state_summary(state: TutorState, ctx: TurnContext) -> str:
    lines = [
        f"- 本轮是否已完成诊断: {'是' if state.get('classification') else '否'}",
        f"- 本轮是否已有步骤评估结果: {'是' if state.get('step_evaluation') else '否'}",
        f"- 本轮导师是否已产出引导: {'是' if state.get('guidance') else '否'}",
        f"- 是否有活跃的步骤计划: {'是' if ctx.has_active_plan else '否'}",
        f"- 是否为延续之前问题的对话: "
        f"{'是' if state.get('is_continuation') or ctx.unresolved_info else '否'}",
    ]
    if state.get("classification"):
        qt = state["classification"].get("question_type", "")
        lines.append(f"- 已诊断的问题类型: {qt}")
    if ctx.has_active_plan and ctx.active_plan:
        cur = ctx.active_plan.current_step
        lines.append(
            f"- 步骤计划进度: {ctx.active_plan.progress_text}"
            + (f"，当前步骤「{cur.title}」" if cur else "")
        )
    return "\n".join(lines)
