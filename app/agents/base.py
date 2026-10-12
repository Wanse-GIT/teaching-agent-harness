"""
Agent 共享服务基座

每个图节点通过 get_services(state, config) 获取：
  - TurnContext   本轮装配上下文
  - TurnEventBus  本轮事件总线
  - LLMGateway    双通道 LLM 网关（可注入）
"""

from app.harness.context import TurnContext
from app.harness.events import TurnEventBus
from app.harness.state import TutorState
from app.llm.gateway import LLMGateway, get_llm_gateway


def get_services(state: TutorState, config: dict) -> tuple[TurnContext, TurnEventBus, LLMGateway]:
    """从图状态与运行时配置中提取节点服务"""
    configurable = (config or {}).get("configurable", {}) or {}
    ctx = TurnContext.from_dict(
        state.get("context", {}),
        conversation_id=state.get("conversation_id", ""),
        user_message=state.get("user_message", ""),
    )
    bus: TurnEventBus = configurable.get("bus") or TurnEventBus(streaming=False)
    gateway: LLMGateway = configurable.get("gateway") or get_llm_gateway()
    return ctx, bus, gateway


def get_bus(config: dict) -> TurnEventBus:
    return ((config or {}).get("configurable", {}) or {}).get("bus") or TurnEventBus(streaming=False)
