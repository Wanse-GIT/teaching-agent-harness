"""
API v2 - 管理员路由（v5.0 新增，全部需要 admin 角色）

  GET    /api/v2/admin/users                  用户列表（含会话统计）
  POST   /api/v2/admin/users                  创建用户（可指定角色）
  PATCH  /api/v2/admin/users/{uid}            更新用户（角色/启停/重置密码）
  DELETE /api/v2/admin/users/{uid}            删除用户（级联清理其会话与画像）
  GET    /api/v2/admin/users/{uid}/conversations  某用户的会话列表（元数据）
  GET    /api/v2/admin/insights               教学经验库（统一整理视图）
  DELETE /api/v2/admin/insights/{insight_id}  删除某条经验
  GET    /api/v2/admin/stats                  全局统计

隐私边界：管理员可见各用户的会话元数据（数量/主题/进度），
但不跨用户导出对话正文与画像内容 —— 正文/画像只属于用户本人。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

import app.auth as auth                   # 动态解析 user_manager（可被测试替换）
from app.api.deps import require_admin
from app.harness.scoping import unscoped_conversation, user_space_prefix
from app.memory import memory
from app.models.user import AdminUserCreateIn, AdminUserPatchIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/admin", dependencies=[Depends(require_admin)])


def _user_out(record, conversation_count: Optional[int] = None):
    out = {
        "id": record.id,
        "username": record.username,
        "role": record.role,
        "is_active": record.is_active,
        "created_at": record.created_at,
        "last_login_at": record.last_login_at,
    }
    if conversation_count is not None:
        out["conversation_count"] = conversation_count
    return out


def _count_user_conversations(user_id: str) -> int:
    prefix = user_space_prefix(user_id)
    return sum(1 for s in memory.working.all_session_ids() if s.startswith(prefix))


def _list_user_conversations(user_id: str):
    """某用户全部会话的元数据（不含消息正文，隐私边界）"""
    prefix = user_space_prefix(user_id)
    items = []
    for sid in memory.working.all_session_ids():
        if not sid.startswith(prefix):
            continue
        session = memory.working.get_or_create(sid)
        plan_summary = memory.working.get_step_plan_summary(sid)
        first_user_msg = next(
            (m for m in session.messages if m.role == "user"), None
        )
        items.append({
            "conversation_id": unscoped_conversation(sid),
            "title": (
                (session.active_problem or "").strip()
                or (first_user_msg.content[:24] if first_user_msg else "新会话")
            ),
            "message_count": len(session.messages),
            "phase": session.phase.value,
            "has_active_plan": plan_summary is not None,
            "updated_at": session.updated_at,
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


# ============================================================
# 用户管理
# ============================================================

@router.get("/users")
async def list_users(admin: dict = Depends(require_admin)):
    """用户列表（含每用户会话数）"""
    return {
        "users": [
            _user_out(u, _count_user_conversations(u.id))
            for u in auth.user_manager.list_users()
        ]
    }


@router.post("/users")
async def create_user(body: AdminUserCreateIn, admin: dict = Depends(require_admin)):
    """管理员创建用户（可指定 admin 角色）"""
    try:
        record = auth.user_manager.register(body.username, body.password, role=body.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"[管理] {admin['username']} 创建了用户 {record.username}({record.role})")
    return {"status": "ok", "user": _user_out(record, 0)}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: AdminUserPatchIn,
    admin: dict = Depends(require_admin),
):
    """更新用户：角色 / 启停 / 重置密码（全部可选字段）"""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="没有提供需要更新的字段")
    try:
        if body.role is not None:
            auth.user_manager.set_role(user_id, body.role)
        if body.is_active is not None:
            auth.user_manager.set_active(user_id, body.is_active)
        if body.password is not None:
            auth.user_manager.admin_reset_password(user_id, body.password)
    except LookupError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    record = auth.user_manager.get(user_id)
    logger.info(f"[管理] {admin['username']} 更新了用户 {record.username}: {list(updates)}")
    return {"status": "ok", "user": _user_out(record, _count_user_conversations(user_id))}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_admin)):
    """删除用户（级联清理其全部会话与画像；禁止删除自己与最后一名管理员）"""
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员自己")
    try:
        record = auth.user_manager.get(user_id)
        if record is None:
            raise LookupError
        auth.user_manager.delete(
            user_id, memory.working, memory.profile
        )
        memory.flush()
    except LookupError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info(f"[管理] {admin['username']} 删除了用户 {record.username}（含其全部数据）")
    return {"status": "ok", "message": f"用户 {record.username} 及其数据已删除"}


@router.get("/users/{user_id}/conversations")
async def user_conversations(user_id: str, admin: dict = Depends(require_admin)):
    """查看某用户的会话列表（元数据，不含正文）"""
    if auth.user_manager.get(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"conversations": _list_user_conversations(user_id)}


# ============================================================
# 教学经验库（统一整理）
# ============================================================

@router.get("/insights")
async def list_insights(topic: Optional[str] = None, admin: dict = Depends(require_admin)):
    """教学经验库全量视图（可按主题过滤）"""
    insights = memory.experience.list_insights(topic=topic)
    return {
        "total": len(insights),
        "insights": [
            {
                "id": i.id,
                "topic": i.topic,
                "question_type": i.question_type,
                "guidance_mode": i.guidance_mode,
                "insight": i.insight,
                "effectiveness": round(i.effectiveness, 3),
                "sample_count": i.sample_count,
                "contributor_count": i.contributor_count,
                "updated_at": i.updated_at,
            }
            for i in insights
        ],
    }


@router.delete("/insights/{insight_id}")
async def delete_insight(insight_id: str, admin: dict = Depends(require_admin)):
    """删除某条教学经验"""
    if not memory.experience.delete(insight_id):
        raise HTTPException(status_code=404, detail="经验条目不存在")
    memory.flush()
    return {"status": "ok", "message": "经验条目已删除"}


# ============================================================
# 全局统计
# ============================================================

@router.get("/stats")
async def global_stats(admin: dict = Depends(require_admin)):
    """全局统计总览"""
    sessions = memory.working.export_sessions()
    guest_sessions = [s for s in sessions if s.startswith("u:guest:")]
    user_scoped = [s for s in sessions if s.startswith("u:") and not s.startswith("u:guest:")]

    return {
        "users": {
            "total": len(auth.user_manager.list_users()),
            "admins": auth.user_manager.admin_count,
            "active": sum(1 for u in auth.user_manager.list_users() if u.is_active),
        },
        "conversations": {
            "total": len(sessions),
            "by_users": len(user_scoped),
            "guest": len(guest_sessions),
            "legacy": len(sessions) - len(guest_sessions) - len(user_scoped),
        },
        "memory": {
            "profiles": len(memory.profile.export_profiles()),
            "strategy_stats": len(memory.strategy.export_stats()),
            "teaching_insights": len(memory.experience),
        },
        "db_persistent": memory.db_available,
    }
