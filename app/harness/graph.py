"""
Harness - LangGraph 主图构建（Supervisor 多 Agent 协作拓扑）

        ┌──────────────────────────────────────────────┐
        │                  Supervisor                   │
        │            （LLM 路由 + 规则兜底）              │
        └───────┬──────────────┬──────────────┬────────┘
                │              │              │
        diagnostician   step_assessor        tutor
          （诊断师）      （步骤评估官）        （导师）
                │              │              │
                └──────────────┴──────┬───────┘
                                      │ 回到 Supervisor 再决策
                                    finish → END

- START → supervisor
- supervisor 经条件边路由到 diagnostician / step_assessor / tutor / END
- 每个专家执行完毕回到 supervisor，由其决定下一步（循环直到 finish）
- 反思进化为后台子图（由 Harness 在响应后触发，不占用调度环）
"""

import logging

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.diagnostician import diagnostician_node
from app.agents.step_assessor import step_assessor_node
from app.agents.supervisor import route_after_supervisor, supervisor_node
from app.agents.tutor import tutor_node
from app.harness.state import TutorState

logger = logging.getLogger(__name__)


def build_main_graph(checkpointer=None):
    """构建 Supervisor 多 Agent 协作主图"""
    g = StateGraph(TutorState)

    g.add_node("supervisor", supervisor_node)
    g.add_node("diagnostician", diagnostician_node)
    g.add_node("step_assessor", step_assessor_node)
    g.add_node("tutor", tutor_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "diagnostician": "diagnostician",
            "step_assessor": "step_assessor",
            "tutor": "tutor",
            "finish": END,
        },
    )
    # 专家执行完毕 → 回到 Supervisor 再决策
    g.add_edge("diagnostician", "supervisor")
    g.add_edge("step_assessor", "supervisor")
    g.add_edge("tutor", "supervisor")

    if checkpointer is None:
        checkpointer = InMemorySaver()

    graph = g.compile(checkpointer=checkpointer)
    logger.info(
        f"主图构建完成: supervisor + 3 专家节点, checkpointer={type(checkpointer).__name__}"
    )
    return graph
