"""
Harness - 事件总线

每轮对话一个 TurnEventBus 实例，收集 Agent 协作过程事件：
  - phase        处理阶段变化（前端阶段徽章）
  - agent_enter / agent_exit   专家 Agent 进入/退出（流水线动画 + 耗时）
  - route        Supervisor 调度决策（来源：llm/rules）
  - token        导师流式输出的增量文本（打字机效果）
  - final        最终响应载荷
  - error        错误信息
  - done         本轮结束标记

非流式模式下 bus 仍收集事件（用于 trace），只是不排队推送。
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


class TurnEventBus:
    """单轮事件总线"""

    def __init__(self, streaming: bool = False):
        self.streaming = streaming
        self.queue: Optional[asyncio.Queue] = asyncio.Queue() if streaming else None
        self.trace: list[dict] = []          # 本轮完整事件轨迹（写入响应 trace 字段）
        self.turn_started = time.time()

    # ------------------------------------------------------------
    # 事件发射
    # ------------------------------------------------------------

    def emit(self, event_type: str, **payload):
        """记录事件；流式模式下同时入队供 SSE 消费"""
        event = {"type": event_type, "ts": round(time.time() - self.turn_started, 3), **payload}
        self.trace.append(event)

        if self.queue is not None:
            try:
                self.queue.put_nowait(event)
            except Exception as e:
                logger.warning(f"事件入队失败: {e}")

    def phase(self, phase: str, label: str = ""):
        self.emit("phase", phase=phase, label=label)

    def agent_enter(self, agent: str, phase: str = "") -> float:
        self.emit("agent_enter", agent=agent, phase=phase)
        return time.time()

    def agent_exit(self, agent: str, start: float, note: str = ""):
        start = start or time.time()
        ms = int((time.time() - start) * 1000)
        self.emit("agent_exit", agent=agent, ms=ms, note=note)
        return ms

    def token(self, delta: str):
        self.emit("token", delta=delta)

    def error(self, message: str):
        self.emit("error", message=message)

    def done(self):
        self.emit("done", total_ms=int((time.time() - self.turn_started) * 1000))

    # ------------------------------------------------------------
    # SSE 消费
    # ------------------------------------------------------------

    async def stream_sse(self) -> AsyncIterator[str]:
        """把事件流转换为 SSE 格式文本流（在 done 事件处结束）

        深度优化：逐事件空闲超时看门狗。若后台执行链意外挂起（既不产出
        事件也不结束），客户端不会永久等待，而是在超时后收到明确的
        error + done 事件流终止。
        """
        if self.queue is None:
            return
        from app.config import settings
        idle_timeout = settings.sse_idle_timeout
        while True:
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                logger.error(
                    f"[事件总线] 事件流空闲超过 {idle_timeout}s，强制结束（后台任务疑似挂起）"
                )
                self.error(f"内部处理超时（{idle_timeout:.0f}s 无事件产出），请重试")
                yield _format_sse(self.trace[-1])
                done_event = {
                    "type": "done",
                    "ts": round(time.time() - self.turn_started, 3),
                    "total_ms": int((time.time() - self.turn_started) * 1000),
                }
                yield _format_sse(done_event)
                return
            yield _format_sse(event)
            if event.get("type") in ("done", "__abort__"):
                break

    def abort(self):
        """中止事件流（异常时调用，防止 SSE 挂起）"""
        if self.queue is not None:
            try:
                self.queue.put_nowait({"type": "__abort__"})
            except Exception:
                pass

    # ------------------------------------------------------------
    # 轨迹摘要（API trace 字段）
    # ------------------------------------------------------------

    def agent_trace(self) -> dict:
        routes = [e for e in self.trace if e["type"] == "route"]
        agents = []
        enter_map: dict[str, dict] = {}
        for e in self.trace:
            if e["type"] == "agent_enter":
                enter_map[e["agent"]] = e
            elif e["type"] == "agent_exit" and e["agent"] in enter_map:
                agents.append({
                    "agent": e["agent"],
                    "ms": e.get("ms", 0),
                })
        return {"routes": routes, "agents": agents}


def _format_sse(event: dict) -> str:
    event_type = event.get("type", "message")
    # token 事件去掉时间戳等冗余，减少传输量
    if event_type == "token":
        data = json.dumps({"delta": event.get("delta", "")}, ensure_ascii=False)
    else:
        data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"
