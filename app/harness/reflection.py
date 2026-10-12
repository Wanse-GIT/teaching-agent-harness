"""
Harness - 反思后台子图（Reflection Subgraph）

反思 → 巩固 → 步骤计划懒加载 → 持久化，四节点顺序执行：
  - reflect:      LLM 反思（降级：启发式反思）
  - consolidate:  记忆巩固（画像 + 策略经验 + 问题解决判定）
  - plan:         步骤计划懒加载（teaching/problem_solving 且无活跃计划）
  - persist:      批量持久化脏数据

由 Harness 在响应送达后以 asyncio.create_task 触发（不阻塞用户回复）。
"""

import logging
from typing import Annotated, Optional, TypedDict

import operator

from app.agents.reflector import maybe_generate_step_plan, reflect
from app.llm.gateway import get_llm_gateway
from app.memory import memory
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


class ReflectionState(TypedDict, total=False):
    """反思子图状态（snapshot 快照，避免与主图状态相互干扰）"""
    conversation_id: str
    snapshot: dict                      # 诊断/引导产出的快照
    reflection: Optional[dict]
    plan_generated: bool
    errors: Annotated[list[str], operator.add]


# ------------------------------------------------------------
# 子图节点
# ------------------------------------------------------------

async def reflect_node(state: ReflectionState, config: RunnableConfig) -> dict:
    gateway = _get_gateway(config)
    snapshot = state["snapshot"]
    try:
        reflection = await reflect(gateway, snapshot)
        logger.info(
            f"[反思] effectiveness={reflection.strategy_effectiveness:.2f}, "
            f"progress={reflection.student_progress:.2f}"
        )
        return {"reflection": reflection.model_dump(mode="json")}
    except Exception as e:
        logger.error(f"[反思] 执行失败: {e}", exc_info=True)
        return {"reflection": None, "errors": [f"reflect: {e}"]}


async def consolidate_node(state: ReflectionState, config: RunnableConfig) -> dict:
    snapshot = state["snapshot"]
    reflection_data = state.get("reflection")
    if not reflection_data:
        return {}

    from app.models.domain import ReflectionResult, GuidanceAction

    try:
        reflection = ReflectionResult.model_validate(reflection_data)
        guidance = snapshot["guidance"]
        guidance_mode = (
            guidance["mode"] if isinstance(guidance, dict) else guidance.mode.value
        )
        perception = snapshot.get("perception") or {}
        topic = (
            perception.get("topic") if isinstance(perception, dict)
            else getattr(perception, "topic", None)
        )
        result = memory.consolidator.consolidate_turn(
            session_id=snapshot["conversation_id"],
            classification=snapshot["classification"],
            cognitive_state=snapshot["cognitive_state"],
            guidance_mode=guidance_mode,
            reflection=reflection,
            profile_key=snapshot.get("profile_key"),          # v5.0 画像隔离键
            topic=topic,                                        # v5.0 经验库主题
            contributor=(snapshot.get("user") or {}).get("id"),
        )
        return {"errors": []}
    except Exception as e:
        logger.error(f"[巩固] 执行失败: {e}", exc_info=True)
        return {"errors": [f"consolidate: {e}"]}


async def plan_node(state: ReflectionState, config: RunnableConfig) -> dict:
    gateway = _get_gateway(config)
    snapshot = state["snapshot"]
    try:
        plan = await maybe_generate_step_plan(gateway, snapshot)
        return {"plan_generated": plan is not None}
    except Exception as e:
        logger.error(f"[步骤计划懒加载] 执行失败: {e}", exc_info=True)
        return {"plan_generated": False, "errors": [f"plan: {e}"]}


async def persist_node(state: ReflectionState, config: RunnableConfig) -> dict:
    memory.flush()
    logger.info("[后台子图] 反思→巩固→计划→持久化 完成")
    return {}


def _get_gateway(config: dict):
    configurable = (config or {}).get("configurable", {}) or {}
    return configurable.get("gateway") or get_llm_gateway()


# ------------------------------------------------------------
# 子图构建与运行
# ------------------------------------------------------------

_graph_cache = None


def build_reflection_graph():
    """构建反思后台子图（无 checkpointer：fire-and-forget 语义）"""
    global _graph_cache
    if _graph_cache is not None:
        return _graph_cache

    from langgraph.graph import END, START, StateGraph

    g = StateGraph(ReflectionState)
    g.add_node("reflect", reflect_node)
    g.add_node("consolidate", consolidate_node)
    g.add_node("plan", plan_node)
    g.add_node("persist", persist_node)

    g.add_edge(START, "reflect")
    g.add_edge("reflect", "consolidate")
    g.add_edge("consolidate", "plan")
    g.add_edge("plan", "persist")
    g.add_edge("persist", END)

    _graph_cache = g.compile()
    return _graph_cache


async def run_reflection(snapshot: dict, gateway=None):
    """运行反思子图（供 runner 以 create_task 方式在后台调用）"""
    graph = build_reflection_graph()
    config = {}
    if gateway is not None:
        config = {"configurable": {"gateway": gateway}}
    try:
        await graph.ainvoke(
            {"conversation_id": snapshot.get("conversation_id", ""), "snapshot": snapshot},
            config=config,
        )
    except Exception as e:
        logger.error(f"[反思子图] 运行失败: {e}", exc_info=True)
        memory.flush()  # 即使失败也要尝试持久化已产生的记忆变更
