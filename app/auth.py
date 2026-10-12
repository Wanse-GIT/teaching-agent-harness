"""
用户管理系统 - 认证与用户管理（v5.0 新增）

纯标准库实现（hmac/hashlib/secrets），零新增依赖：
  - 密码哈希：PBKDF2-HMAC-SHA256（120k 迭代 + 16B 随机盐）
  - 访问令牌：JWT HS256（兼容 Authorization: Bearer <token>）
  - 密钥管理：AUTH_SECRET 未配置时自动生成并持久化到 .auth_secret

UserManager：
  - 内存缓存 + Persistence（SQLAlchemy）双写；DB 不可用降级纯内存
  - 启动引导：无任何管理员时创建 bootstrap 管理员（仅一次）
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional

from app.config import settings
from app.models.user import ROLE_ADMIN, ROLE_USER, UserRecord

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 120_000
_SALT_BYTES = 16
_SECRET_FILE = ".auth_secret"


# ============================================================
# 密码哈希
# ============================================================

def hash_password(password: str) -> str:
    """PBKDF2 哈希（格式：pbkdf2_sha256$iterations$salt$hash）"""
    salt = secrets.token_hex(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较校验密码"""
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, AttributeError):
        return False


# ============================================================
# JWT（HS256，标准库实现）
# ============================================================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def get_auth_secret() -> str:
    """获取 JWT 签名密钥：配置优先 → .auth_secret 文件（自动生成持久化）"""
    if settings.auth_secret:
        return settings.auth_secret
    path = os.path.join(os.getcwd(), _SECRET_FILE)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                secret = f.read().strip()
                if secret:
                    return secret
        secret = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(secret)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info(f"已自动生成 JWT 密钥并持久化到 {_SECRET_FILE}")
        return secret
    except OSError as e:
        # 文件系统只读等极端情况：退化为进程内随机密钥（重启后令牌失效）
        logger.warning(f"JWT 密钥文件读写失败，退化为进程内密钥: {e}")
        return secrets.token_hex(32)


def create_access_token(user: UserRecord, expires_hours: Optional[int] = None) -> str:
    """签发 JWT：payload 含 sub/username/role/exp/iat"""
    now = int(time.time())
    exp = now + int((expires_hours or settings.auth_token_expire_hours) * 3600)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"sub": user.id, "username": user.username, "role": user.role, "iat": now, "exp": exp},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url(hmac.new(get_auth_secret().encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> Optional[dict]:
    """校验并解码 JWT；任何异常返回 None（过期/篡改/格式错误）"""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(get_auth_secret().encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, TypeError):
        return None


# ============================================================
# 用户管理器
# ============================================================

class UserManager:
    """用户存储：内存缓存 + Persistence 双写（DB 不可用降级纯内存）"""

    def __init__(self):
        self._users: dict[str, UserRecord] = {}          # id -> record
        self._passwords: dict[str, str] = {}             # id -> hash
        self._by_name: dict[str, str] = {}               # username(lower) -> id
        self._persistence = None                         # 延迟注入，避免循环导入
        self._loaded = False

    def bind(self, persistence):
        """注入持久化层并由其负责 DB 读写"""
        self._persistence = persistence

    # ------------------------------------------------------------
    # 加载与引导
    # ------------------------------------------------------------

    def load(self):
        """启动时从 DB 加载用户；无管理员时创建 bootstrap 管理员"""
        if self._loaded:
            return
        self._loaded = True
        if self._persistence is not None:
            for record, pw_hash in self._persistence.load_users():
                self._index(record, pw_hash)

        if not any(u.role == ROLE_ADMIN for u in self._users.values()):
            uname = settings.admin_bootstrap_username
            if self.get_by_username(uname) is None:
                record = self.register(
                    uname, settings.admin_bootstrap_password, role=ROLE_ADMIN, bootstrap=True
                )
                logger.warning(
                    f"已创建初始管理员「{uname}」（密码为 ADMIN_BOOTSTRAP_PASSWORD 配置值，"
                    f"请登录后立即修改）"
                )
            else:
                # 同名用户存在但不是管理员 → 提升为管理员，保证系统有管理员可用
                self.set_role(self._by_name[uname.lower()], ROLE_ADMIN)

    def _index(self, record: UserRecord, pw_hash: str):
        self._users[record.id] = record
        self._passwords[record.id] = pw_hash
        self._by_name[record.username.lower()] = record.id

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------

    def get(self, user_id: str) -> Optional[UserRecord]:
        return self._users.get(user_id)

    def get_by_username(self, username: str) -> Optional[UserRecord]:
        uid = self._by_name.get(username.lower())
        return self._users.get(uid) if uid else None

    def list_users(self) -> list[UserRecord]:
        return sorted(self._users.values(), key=lambda u: u.created_at)

    @property
    def admin_count(self) -> int:
        return sum(1 for u in self._users.values() if u.role == ROLE_ADMIN and u.is_active)

    # ------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------

    def register(
        self, username: str, password: str, role: str = ROLE_USER, bootstrap: bool = False
    ) -> UserRecord:
        """创建用户；用户名冲突时抛 ValueError"""
        if self.get_by_username(username) is not None:
            raise ValueError(f"用户名「{username}」已被占用")
        record = UserRecord.new(username, role)
        pw_hash = hash_password(password)
        self._index(record, pw_hash)
        self._persist(record, pw_hash)
        return record

    def authenticate(self, username: str, password: str) -> Optional[UserRecord]:
        """校验用户名密码；成功时更新 last_login_at 并持久化"""
        record = self.get_by_username(username)
        if record is None or not record.is_active:
            return None
        if not verify_password(password, self._passwords.get(record.id, "")):
            return None
        record.last_login_at = time.time()
        self._persist(record, self._passwords[record.id])
        return record

    def change_password(self, user_id: str, old_password: str, new_password: str) -> UserRecord:
        record = self._require(user_id)
        if not verify_password(old_password, self._passwords.get(user_id, "")):
            raise ValueError("原密码不正确")
        pw_hash = hash_password(new_password)
        self._passwords[user_id] = pw_hash
        self._persist(record, pw_hash)
        return record

    def admin_reset_password(self, user_id: str, new_password: str) -> UserRecord:
        record = self._require(user_id)
        pw_hash = hash_password(new_password)
        self._passwords[user_id] = pw_hash
        self._persist(record, pw_hash)
        return record

    def set_role(self, user_id: str, role: str) -> UserRecord:
        record = self._require(user_id)
        if role not in ("user", "admin"):
            raise ValueError("角色必须是 user 或 admin")
        if record.role == ROLE_ADMIN and role != ROLE_ADMIN and self.admin_count <= 1:
            raise ValueError("系统至少需要保留一名活跃管理员")
        record.role = role
        self._persist(record, self._passwords.get(user_id, ""))
        return record

    def set_active(self, user_id: str, active: bool) -> UserRecord:
        record = self._require(user_id)
        if not active and record.role == ROLE_ADMIN and self.admin_count <= 1:
            raise ValueError("不能停用最后一名活跃管理员")
        record.is_active = active
        self._persist(record, self._passwords.get(user_id, ""))
        return record

    def delete(self, user_id: str, working, profile) -> None:
        """删除用户（含级联：其全部会话与画像）"""
        record = self._require(user_id)
        if record.role == ROLE_ADMIN and self.admin_count <= 1:
            raise ValueError("不能删除最后一名管理员")
        # 级联清理该用户的空间（工作记忆 + 画像 + DB 记录）
        prefix = f"u:{user_id}:"
        for sid in [s for s in working.all_session_ids() if s.startswith(prefix)]:
            working.clear(sid)
            if self._persistence is not None:
                self._persistence.delete_session(sid)
        profile_key = f"p:{user_id}"
        profile.clear(profile_key)
        if self._persistence is not None:
            self._persistence.delete_profile(profile_key)
            self._persistence.delete_user(user_id)
        self._users.pop(user_id, None)
        self._passwords.pop(user_id, None)
        self._by_name.pop(record.username.lower(), None)

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------

    def _require(self, user_id: str) -> UserRecord:
        record = self._users.get(user_id)
        if record is None:
            raise LookupError("用户不存在")
        return record

    def _persist(self, record: UserRecord, pw_hash: str):
        if self._persistence is not None:
            self._persistence.save_user(record, pw_hash)


# 全局单例
user_manager = UserManager()
