"""
API 依赖：认证 + 限流

安全姿态（v5.0 全面升级）：
  - 用户级认证：Authorization: Bearer <JWT>（注册/登录获取，见 api/auth_routes.py）
  - 服务间认证：X-API-Key（配置 API_KEY 后启用，向后兼容）
  - 匿名访问：AUTH_REQUIRED=false 时映射 guest 用户空间（开发模式）
  - 进程内滑动窗口限流（无新增依赖）
"""

import logging
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.config import settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """验证服务间 API Key（未配置 API_KEY 时为开放开发模式）"""
    if not settings.api_key_enabled:
        return "anonymous"
    if not api_key or api_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail="无效的 API Key。请在请求头中设置 X-API-Key。",
        )
    return api_key


# ------------------------------------------------------------
# 用户认证（v5.0）
# ------------------------------------------------------------


def _extract_bearer(request: Request) -> Optional[str]:
    """从 Authorization 头提取 Bearer 令牌"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def get_optional_user(request: Request) -> Optional[dict]:
    """解析 JWT；无令牌返回 None（由 AUTH_REQUIRED 决定是否拒绝）"""
    token = _extract_bearer(request)
    if not token:
        return None

    from app.auth import decode_token, user_manager

    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已过期或令牌无效，请重新登录")

    record = user_manager.get(payload.get("sub", ""))
    if record is None:
        raise HTTPException(status_code=401, detail="账号不存在或已被删除")
    if not record.is_active:
        raise HTTPException(status_code=403, detail="账号已被停用，请联系管理员")
    return {"id": record.id, "username": record.username, "role": record.role}


async def resolve_user(
    request: Request,
    api_key: str = Depends(verify_api_key),
) -> dict:
    """会话端点统一用户解析：

    - 携带合法 JWT → 该用户空间（隔离）
    - 无 JWT 且 AUTH_REQUIRED=false → guest 共享空间（开发模式，兼容旧行为）
    - 无 JWT 且 AUTH_REQUIRED=true  → 401
    """
    user = await get_optional_user(request)
    if user is not None:
        return user
    if settings.auth_required:
        raise HTTPException(status_code=401, detail="请先登录后再使用")
    return {"id": "guest", "username": "guest", "role": "user"}


async def require_admin(user: dict = Depends(resolve_user)) -> dict:
    """管理员守卫：普通用户/匿名 → 403"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ------------------------------------------------------------
# 进程内滑动窗口限流
# ------------------------------------------------------------

class SlidingWindowLimiter:
    """轻量滑动窗口限流器（单进程，避免引入 slowapi 依赖）"""

    def __init__(self):
        self._windows: dict[str, deque] = defaultdict(deque)
        self._last_purge = time.time()

    def check(self, key: str, limit_per_minute: int) -> bool:
        now = time.time()
        # 定期清理过期空窗口（深度优化：防长期运行内存无限增长）
        if now - self._last_purge > 60:
            self._purge(now)
            self._last_purge = now
        window = self._windows[key]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= limit_per_minute:
            return False
        window.append(now)
        return True

    def _purge(self, now: float):
        """删除超过 5 分钟无活动的限流 key（对应窗口早已滑空）"""
        stale = [k for k, w in self._windows.items() if not w or w[0] < now - 300]
        for k in stale:
            self._windows.pop(k, None)


_limiter = SlidingWindowLimiter()
_auth_limiter = SlidingWindowLimiter()


def rate_limit_chat(client_key: str):
    """聊天接口限流（LLM 调用较慢，限制更严格）"""
    if not settings.rate_limit_enabled:
        return
    ok = _limiter.check(f"chat:{client_key}", _parse_rate(settings.rate_limit_chat_per_minute))
    if not ok:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


def rate_limit_auth(client_key: str, limit_per_minute: int = 10):
    """认证接口限流（注册/登录/改密，防暴力破解）"""
    if not settings.rate_limit_enabled:
        return
    ok = _auth_limiter.check(f"auth:{client_key}", limit_per_minute)
    if not ok:
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")


def _parse_rate(rate: str) -> int:
    try:
        return int(rate.split("/")[0])
    except (ValueError, IndexError):
        return 30
