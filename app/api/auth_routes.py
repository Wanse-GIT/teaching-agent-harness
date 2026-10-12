"""
API v2 - 用户认证路由（v5.0 新增）

  POST /api/v2/auth/register         注册（AUTH_ALLOW_REGISTRATION=true 时开放）
  POST /api/v2/auth/login            登录 → {token, user}
  GET  /api/v2/auth/me               当前用户信息
  POST /api/v2/auth/change-password  修改自己的密码
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

import app.auth as auth                   # 动态解析 user_manager（可被测试替换）
from app.api.deps import get_optional_user, rate_limit_auth
from app.auth import create_access_token
from app.config import settings
from app.models.user import PasswordChangeIn, UserCredentialsIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/auth")


def _record_out(record):
    return {
        "id": record.id,
        "username": record.username,
        "role": record.role,
        "is_active": record.is_active,
        "created_at": record.created_at,
        "last_login_at": record.last_login_at,
    }


@router.post("/register")
async def register(body: UserCredentialsIn, request: Request):
    """注册新用户（角色固定为普通用户；管理员由既有管理员创建）"""
    if not settings.auth_allow_registration:
        raise HTTPException(status_code=403, detail="本服务未开放注册，请联系管理员开通账号")
    rate_limit_auth(f"register:{request.client.host if request.client else 'unknown'}")
    try:
        record = auth.user_manager.register(body.username, body.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    token = create_access_token(record)
    logger.info(f"[认证] 新用户注册: {record.username}")
    return {"token": token, "token_type": "bearer", "user": _record_out(record)}


@router.post("/login")
async def login(body: UserCredentialsIn, request: Request):
    """登录（用户名 + 密码 → JWT）"""
    rate_limit_auth(f"login:{request.client.host if request.client else 'unknown'}")
    record = auth.user_manager.authenticate(body.username, body.password)
    if record is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(record)
    return {"token": token, "token_type": "bearer", "user": _record_out(record)}


@router.get("/me")
async def me(user=Depends(get_optional_user)):
    """当前用户信息（未登录返回 guest，前端据此显示登录页）"""
    if user is None:
        return {"user": None, "auth_required": settings.auth_required}
    record = auth.user_manager.get(user["id"])
    if record is None:
        return {"user": None, "auth_required": settings.auth_required}
    return {"user": _record_out(record), "auth_required": settings.auth_required}


@router.post("/change-password")
async def change_password(body: PasswordChangeIn, user=Depends(get_optional_user)):
    """修改自己的密码（改完旧令牌仍有效直至过期，建议前端刷新令牌）"""
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        record = auth.user_manager.change_password(user["id"], body.old_password, body.new_password)
    except LookupError:
        raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_access_token(record)
    return {"status": "ok", "message": "密码已修改", "token": token, "token_type": "bearer"}
