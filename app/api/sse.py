"""
API v2 - SSE 流式端点

POST /api/v2/conversations/{cid}/messages/stream

事件流（text/event-stream）：
  event: phase         {phase, label}                  处理阶段变化
  event: agent_enter   {agent, phase}                  专家 Agent 开始
  event: agent_exit    {agent, ms, note}               专家 Agent 结束（含耗时）
  event: route         {from, to, reason, source}      Supervisor 调度决策
  event: token         {delta}                         导师流式输出增量
  event: final         {payload: MessageResponse}      最终完整响应
  event: error         {message}                       错误
  event: done          {total_ms}                      本轮结束
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import rate_limit_chat, resolve_user
from app.config import settings
from app.harness.runner import runner
from app.models.api import MessageIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2")


@router.post("/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: str,
    body: MessageIn,
    request: Request,
    user: dict = Depends(resolve_user),
):
    """SSE 流式发送消息：实时观察 Supervisor 调度与专家协作过程（v5.0 按用户隔离）"""
    if not settings.stream_enabled:
        raise HTTPException(status_code=404, detail="流式输出未启用（STREAM_ENABLED=false）")

    from app.api.routes import _client_key

    rate_limit_chat(_client_key(request))

    bus = await runner.run_stream(conversation_id, body.content, user=user)

    return StreamingResponse(
        bus.stream_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
