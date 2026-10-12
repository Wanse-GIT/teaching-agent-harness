"""
用户管理系统 - 数据模型（v5.0 新增）

  - UserStore    SQLAlchemy ORM（表 agent_users，随 Persistence 初始化建表）
  - UserRecord   Pydantic 传输模型（API 响应 / 认证上下文）

角色语义：
  - admin: 管理员（用户管理 / 经验库维护 / 全局统计）
  - user:  普通学习者（画像与会话严格隔离，只可见自己的数据）

密码哈希格式（app.auth.hash_password）：
  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""

import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.memory.persistence import Base
from sqlalchemy import Boolean, Column, Float, String

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_ADMIN)


class UserStore(Base):
    """用户表（由 Persistence 統一建表与管理）"""
    __tablename__ = "agent_users"

    id = Column(String(64), primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), default=ROLE_USER, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(Float, default=0.0)
    last_login_at = Column(Float, default=0.0)


class UserRecord(BaseModel):
    """用户传输模型（绝不携带 password_hash）"""
    id: str
    username: str
    role: Literal["user", "admin"] = ROLE_USER
    is_active: bool = True
    created_at: float = 0.0
    last_login_at: float = 0.0

    @staticmethod
    def new(username: str, role: str = ROLE_USER) -> "UserRecord":
        now = time.time()
        return UserRecord(
            id=uuid.uuid4().hex[:16],
            username=username,
            role=role if role in ROLES else ROLE_USER,
            created_at=now,
            last_login_at=0.0,
        )


class UserCredentialsIn(BaseModel):
    """注册/登录请求"""
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$")
    password: str = Field(min_length=6, max_length=128)


class PasswordChangeIn(BaseModel):
    """修改密码请求"""
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class AdminUserCreateIn(BaseModel):
    """管理员创建用户请求"""
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$")
    password: str = Field(min_length=6, max_length=128)
    role: Literal["user", "admin"] = ROLE_USER


class AdminUserPatchIn(BaseModel):
    """管理员更新用户请求（全部可选）"""
    role: Optional[Literal["user", "admin"]] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
