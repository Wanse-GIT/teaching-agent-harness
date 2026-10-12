"""
Harness - LangGraph 图状态定义

TutorState 是多 Agent 协作的共享黑板（blackboard）：
  - Harness 在图启动前写入输入与上下文，并显式清空上一轮残留
    （Checkpointer 会跨轮持久化状态，因此每轮必须显式重置所有字段）
  - 各专家 Agent 节点只读输入、写自己的产出
  - Supervisor 根据黑板状态决定下一个专家

设计约定：
  - 所有字段均为「最后写入优先」（无 reducer），跨轮重置由 runner.empty_state() 负责
  - route_trace / errors 由各节点读-改-写（串行调度图内无并发竞争）
  - 复杂对象以 JSON 安全 dict 存储（mode="json"），保证 Checkpointer 兼容
"""

from typing import Optional, TypedDict


class TutorState(TypedDict, total=False):
    # === 输入（Harness 写入） ===
    conversation_id: str              # 隔离后的内部 key：u:{uid}:{cid}（v5.0）
    profile_key: str                  # 画像专用 key：p:{uid}（v5.0）
    user_message: str
    turn_id: str

    # === Harness 装配的轮次上下文（TurnContext 序列化） ===
    context: dict

    # === Supervisor 调度 ===
    route: Optional[dict]              # RouteDecision
    route_trace: list[dict]            # 本轮路由决策轨迹（逐轮重置）

    # === 诊断师产出 ===
    perception: Optional[dict]
    classification: Optional[dict]
    cognitive_state: Optional[dict]

    # === 步骤评估官产出 ===
    step_evaluation: Optional[dict]

    # === 导师产出 ===
    guidance: Optional[dict]

    # === 派生标志（策略节点写入） ===
    is_continuation: bool
    is_step_complete: bool
    step_plan_just_completed: bool

    # === 元信息 ===
    hops: int                          # Supervisor 循环保护（逐轮重置）
    errors: list[str]                  # 本轮错误（逐轮重置，读-改-写追加）


def empty_state() -> TutorState:
    """每轮图执行前的干净黑板（显式清空所有字段，防 Checkpointer 残留泄漏）"""
    return TutorState(
        conversation_id="",
        profile_key="",
        user_message="",
        turn_id="",
        context={},
        route=None,
        route_trace=[],
        perception=None,
        classification=None,
        cognitive_state=None,
        step_evaluation=None,
        guidance=None,
        is_continuation=False,
        is_step_complete=False,
        step_plan_just_completed=False,
        hops=0,
        errors=[],
    )
