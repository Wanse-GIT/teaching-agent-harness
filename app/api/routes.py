"""
API v2 - REST 路由（v5.0：多用户隔离版）

  POST   /api/v2/conversations/{cid}/messages          发送消息（隔离）
  GET    /api/v2/conversations/{cid}                   会话摘要（隔离）
  DELETE /api/v2/conversations/{cid}                   重置会话（隔离）
  GET    /api/v2/conversations/{cid}/messages          消息历史（v5.0 新增）
  GET    /api/v2/conversations/{cid}/steps             步骤进度
  POST   /api/v2/conversations/{cid}/steps/skip        跳过当前步骤
  POST   /api/v2/conversations/{cid}/resolve           标记问题已解决
  GET    /api/v2/conversations/{cid}/profile           学生画像（按用户）
  PATCH  /api/v2/conversations/{cid}/profile           更新学生画像
  POST   /api/v2/conversations/{cid}/messages/stream   SSE 流式（见 sse.py）
  GET    /api/v2/conversations                         我的会话列表（v5.0 新增）
  GET    /api/v2/profile                               我的画像（v5.0 新增，跨会话）
  GET    /api/v2/health                                健康检查

隔离规则（v5.0）：
  - 所有会话端点先经 resolve_user 解析当前用户（JWT / guest）
  - conversation_id 在服务端翻译为 u:{uid}:{cid}，用户间天然隔离
  - 画像使用 p:{uid}：同一用户跨会话共享，不同用户互不可见
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import rate_limit_chat, resolve_user, verify_api_key
from app.config import settings
from app.harness.runner import runner
from app.harness.scoping import scoped_conversation, scoped_profile, unscoped_conversation, user_space_prefix
from app.memory import memory
from app.models.api import (
    ConversationSummary,
    HealthResponse,
    MessageIn,
    MessageResponse,
    ProfileUpdateIn,
    StepPlanSummary,
)
from app.models.domain import StepEvaluationResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ============================================================
# 消息
# ============================================================

@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
async def send_message(
    conversation_id: str,
    body: MessageIn,
    request: Request,
    user: dict = Depends(resolve_user),
    api_key: str = Depends(verify_api_key),
):
    """发送学生消息，返回完整引导响应（感知→分类→评估→引导→反思 全闭环）"""
    rate_limit_chat(_client_key(request))

    try:
        return await runner.run(conversation_id, body.content, user=user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Chat API error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="处理请求时出现错误")


# ============================================================
# 会话列表 / 消息历史（v5.0 新增）
# ============================================================

@router.get("/conversations")
async def list_my_conversations(user: dict = Depends(resolve_user)):
    """当前用户的会话列表（按最近活跃排序）"""
    prefix = user_space_prefix(user["id"])
    items = []
    for sid in memory.working.all_session_ids():
        if not sid.startswith(prefix):
            continue
        session = memory.working.get_or_create(sid)
        first_user_msg = next((m for m in session.messages if m.role == "user"), None)
        items.append({
            "conversation_id": unscoped_conversation(sid),
            "title": (
                (session.active_problem or "").strip()
                or (first_user_msg.content[:24] if first_user_msg else "新会话")
            ),
            "message_count": len(session.messages),
            "phase": session.phase.value,
            "has_active_plan": memory.working.get_active_step_plan(sid) is not None,
            "updated_at": session.updated_at,
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"conversations": items[:50], "total": len(items)}


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    limit: int = 50,
    user: dict = Depends(resolve_user),
):
    """拉取会话消息历史（默认最近 50 条，用于会话切换时恢复界面）"""
    sid = scoped_conversation(user, conversation_id)
    session = memory.working.get_or_create(sid)
    messages = [
        {
            "role": m.role,
            "content": m.content,
            "created_at": getattr(m, "created_at", 0.0) or 0.0,
            "question_type": (
                m.question_type.value if hasattr(m.question_type, "value") else m.question_type
            ) if getattr(m, "question_type", None) else None,
        }
        for m in session.messages
    ][-limit:]
    return {"conversation_id": conversation_id, "messages": messages, "total": len(session.messages)}


# ============================================================
# 会话
# ============================================================

@router.get("/conversations/{conversation_id}", response_model=ConversationSummary)
async def get_conversation(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """获取会话摘要（含步骤计划、交互统计、画像）"""
    sid = scoped_conversation(user, conversation_id)
    profile_key = scoped_profile(user)
    session = memory.working.get_or_create(sid)
    step_summary = memory.working.get_step_plan_summary(sid)

    return ConversationSummary(
        conversation_id=conversation_id,
        phase=session.phase.value,
        message_count=len(session.messages),
        active_problem=session.active_problem,
        problem_turn_count=session.problem_turn_count,
        last_problem_resolved=session.last_problem_resolved,
        resolved_problems=session.resolved_problems[-5:],
        step_plan=StepPlanSummary(**step_summary) if step_summary else None,
        profile=memory.working.get_interaction_stats(sid),
        user_profile=memory.profile.get_summary_dict(profile_key),
    )


@router.delete("/conversations/{conversation_id}")
async def reset_conversation(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """重置会话（清除工作记忆；画像与策略经验保留——长期记忆语义）"""
    sid = scoped_conversation(user, conversation_id)
    memory.reset_session(sid)
    return {"status": "ok", "message": "会话已重置"}


# ============================================================
# 步骤追踪
# ============================================================

@router.get("/conversations/{conversation_id}/steps")
async def get_step_progress(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """获取当前步骤计划的进度"""
    sid = scoped_conversation(user, conversation_id)
    step_summary = memory.working.get_step_plan_summary(sid)
    if not step_summary:
        return {"has_plan": False}
    return {"has_plan": True, "step_plan": step_summary}


@router.post("/conversations/{conversation_id}/steps/skip")
async def skip_current_step(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """手动跳过当前步骤"""
    sid = scoped_conversation(user, conversation_id)
    plan = memory.working.get_active_step_plan(sid)
    if not plan:
        raise HTTPException(status_code=404, detail="没有活跃的步骤计划")

    eval_result = StepEvaluationResult(
        step_completed=True,
        student_understood=0.3,
        should_advance=True,
    )
    memory.working.advance_step_plan(sid, eval_result)
    memory.flush()
    return {"status": "ok", "message": "已跳过当前步骤"}


@router.post("/conversations/{conversation_id}/resolve")
async def mark_resolved(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """手动标记当前问题已解决"""
    sid = scoped_conversation(user, conversation_id)
    memory.working.mark_problem_resolved(sid)
    memory.flush()
    return {"status": "ok", "message": "问题已标记为解决"}


# ============================================================
# 学生画像
# ============================================================

@router.get("/profile")
async def get_my_profile(user: dict = Depends(resolve_user)):
    """我的长期画像（v5.0：跨会话共享，按用户隔离）"""
    profile_key = scoped_profile(user)
    profile = memory.profile.get_or_create(profile_key)
    return {
        "profile": profile.model_dump(),
        "summary": memory.profile.get_summary_text(profile_key),
    }


@router.get("/conversations/{conversation_id}/profile")
async def get_profile(
    conversation_id: str,
    user: dict = Depends(resolve_user),
):
    """获取学生画像详情（长期记忆；同一用户所有会话共享）"""
    profile_key = scoped_profile(user)
    profile = memory.profile.get_or_create(profile_key)
    return {
        "profile": profile.model_dump(),
        "summary": memory.profile.get_summary_text(profile_key),
    }


@router.patch("/conversations/{conversation_id}/profile")
async def update_profile(
    conversation_id: str,
    updates: ProfileUpdateIn,
    user: dict = Depends(resolve_user),
):
    """手动更新学生画像（仅允许白名单字段，防止任意篡改）"""
    update_dict = updates.model_dump(exclude_none=True)
    if not update_dict:
        raise HTTPException(status_code=400, detail="没有提供需要更新的字段")

    profile_key = scoped_profile(user)
    profile = memory.profile.update(profile_key, **update_dict)
    memory.flush()
    return {"status": "ok", "profile": profile.model_dump()}


# ============================================================
# 健康检查
# ============================================================

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查（无需认证）"""
    from app.llm.gateway import get_llm_gateway

    try:
        gateway = get_llm_gateway()
        llm_channel = gateway.status()  # 深度优化：含熔断器状态（degraded 时可见冷却剩余时间）
    except Exception:
        llm_channel = "unavailable"

    return HealthResponse(
        status="ok",
        db_persistent=memory.db_available,
        checkpointer="postgres" if memory.db_available else "memory",
        llm_channel=llm_channel,
        api_key_enabled=settings.api_key_enabled,
        rate_limit_enabled=settings.rate_limit_enabled,
        features=[
            "supervisor_multi_agent",
            "diagnostician_merged_perception",
            "step_tracker",
            "three_layer_memory",
            "dual_channel_llm",
            "reflection_background_subgraph",
            "strategy_evolution",
            "sse_streaming",
            "api_key_authentication",
            "input_guardrails",
            "rate_limiting",
            "user_management_v5",           # v5.0
            "user_isolation_v5",            # v5.0
            "teaching_experience_v5",       # v5.0
        ],
    )
