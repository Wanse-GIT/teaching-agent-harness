"""
Harness - 用户隔离键翻译（v5.0 核心机制）

数据隔离原则（「不同用户的画像和对话记录要隔离」）：
  - 工作记忆（对话记录）：key = u:{user_id}:{conversation_id}
      同一用户的不同会话互不干扰；不同用户即使使用相同的
      conversation_id 也完全落在不同的命名空间 —— 靠猜 ID 无法越权。
  - 长期画像：key = p:{user_id}
      同一用户跨会话共享（换会话不用重新认识学生），不同用户严格隔离。
  - LangGraph 线程（checkpointer）：thread_id = 工作记忆 key，天然隔离。
  - 匿名访问（开发模式，AUTH_REQUIRED=false）：映射到共享 guest 空间，
      行为与旧版单用户完全一致（向后兼容）。

实现为「入口翻译」：只在 Runner/API 边界把外部 conversation_id
翻译成带前缀的内部 key，下游全部 Agent 与记忆层零改动。
"""

from typing import Optional

GUEST_USER = {"id": "guest", "username": "guest", "role": "user"}
GUEST_ID = "guest"

CONV_PREFIX = "u:"
PROFILE_PREFIX = "p:"


def scoped_conversation(user: Optional[dict], conversation_id: str) -> str:
    """外部会话 ID → 内部隔离 key（u:{uid}:{cid}）"""
    uid = (user or {}).get("id") or GUEST_ID
    return f"{CONV_PREFIX}{uid}:{conversation_id}"


def scoped_profile(user: Optional[dict]) -> str:
    """用户 → 画像 key（p:{uid}；同一用户跨会话共享）"""
    uid = (user or {}).get("id") or GUEST_ID
    return f"{PROFILE_PREFIX}{uid}"


def unscoped_conversation(sid: str) -> str:
    """内部 key → 外部会话 ID（无前缀时原样返回，兼容旧数据）"""
    if sid.startswith(CONV_PREFIX) and ":" in sid[len(CONV_PREFIX):]:
        return sid.split(":", 2)[2]
    return sid


def user_space_prefix(user_id: str) -> str:
    """用户全部会话的 key 前缀（列表/级联删除用）"""
    return f"{CONV_PREFIX}{user_id}:"
