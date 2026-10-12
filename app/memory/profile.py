"""
三层记忆框架 - 第二层：长期画像记忆（语义记忆）

职责：
  - 学生身份、学习偏好、认知轨迹
  - 已掌握/薄弱/感兴趣知识点
  - 有效/无效策略标签（个性化）
  - 交互动态（滑动平均参与度/挫败感）
"""

import logging
import time
from typing import Callable, Optional

from app.models.domain import ClassificationResult, CognitiveState, QuestionType, UserProfile

logger = logging.getLogger(__name__)


class ProfileMemory:
    """长期画像记忆（对应原 agent_user_profiles 语义）"""

    def __init__(self):
        self._profiles: dict[str, UserProfile] = {}
        self._on_dirty: Callable[[str], None] = lambda sid: None

    def set_dirty_hook(self, hook: Callable[[str], None]):
        self._on_dirty = hook

    def get_or_create(self, session_id: str) -> UserProfile:
        if session_id not in self._profiles:
            self._profiles[session_id] = UserProfile()
            self._on_dirty(session_id)  # 新建立即持久化
        return self._profiles[session_id]

    def update(self, session_id: str, **updates) -> UserProfile:
        profile = self.get_or_create(session_id)
        for key, value in updates.items():
            if hasattr(profile, key) and value is not None:
                setattr(profile, key, value)
        profile.last_active_timestamp = time.time()
        self._profiles[session_id] = profile
        self._on_dirty(session_id)
        return profile

    def clear(self, session_id: str):
        self._profiles.pop(session_id, None)
        self._on_dirty(session_id)

    def export_profiles(self) -> dict[str, UserProfile]:
        return dict(self._profiles)

    def import_profiles(self, profiles: dict[str, UserProfile]):
        self._profiles.update(profiles)

    # ------------------------------------------------------------
    # 画像摘要
    # ------------------------------------------------------------

    def get_summary_text(self, session_id: str) -> str:
        """画像文本摘要（注入 Prompt，让 Agent 感知学生特征）"""
        profile = self.get_or_create(session_id)

        parts: list[str] = []
        # 身份信息（v5.2 白手起家：无预设，通过对话逐步发现）
        if profile.identity:
            parts.append(f"用户身份：{profile.identity}")
            basic_bits = []
            if profile.grade:
                basic_bits.append(f"年级：{profile.grade}")
            if profile.major:
                basic_bits.append(f"专业：{profile.major}")
            if profile.location:
                basic_bits.append(f"所在地：{profile.location}")
            if basic_bits:
                parts.append("，".join(basic_bits))
        else:
            parts.append("用户身份：尚未了解（随对话自动更新）")
            parts.append("画像提示：学生身份未知，请在对话中自然地了解其背景，不要预设")
        if profile.known_topics:
            parts.append(f"已掌握知识点：{', '.join(profile.known_topics[-10:])}")
        if profile.weak_topics:
            parts.append(f"薄弱知识点：{', '.join(profile.weak_topics[-10:])}")
        if profile.interest_topics:
            parts.append(f"感兴趣领域：{', '.join(profile.interest_topics[-5:])}")
        if profile.current_courses:
            parts.append(f"正在学的课程：{', '.join(profile.current_courses)}")

        parts.append(f"学习风格偏好：{profile.preferred_learning_style}")
        parts.append(f"鼓励方式偏好：{profile.preferred_encouragement_style}")
        parts.append(f"难度偏好：{profile.preferred_difficulty}")
        parts.append(
            f"平均参与度：{profile.avg_engagement:.2f}，"
            f"平均挫败感：{profile.avg_frustration:.2f}"
        )

        if profile.personality_tags:
            parts.append(f"个性标签：{', '.join(profile.personality_tags)}")
        if profile.effective_strategies:
            parts.append(f"对该用户有效的策略：{', '.join(profile.effective_strategies[-5:])}")
        if profile.ineffective_strategies:
            parts.append(
                f"对该用户效果不好的策略：{', '.join(profile.ineffective_strategies[-5:])}"
            )
        if profile.cognitive_level_trajectory:
            recent_levels = profile.cognitive_level_trajectory[-5:]
            parts.append(f"近期认知层级变化：{' → '.join(recent_levels)}")

        return "\n".join(parts)

    def get_summary_dict(self, session_id: str) -> dict:
        """画像结构化摘要（API 响应）"""
        p = self.get_or_create(session_id)
        return {
            "identity": p.identity,
            "grade": p.grade,
            "major": p.major,
            "location": p.location,
            "known_topics": p.known_topics[-10:],
            "weak_topics": p.weak_topics[-10:],
            "interest_topics": p.interest_topics[-5:],
            "personality_tags": p.personality_tags,
            "effective_strategies": p.effective_strategies[-5:],
            "avg_engagement": round(p.avg_engagement, 3),
            "avg_frustration": round(p.avg_frustration, 3),
            "total_sessions": p.total_sessions,
        }

    # ------------------------------------------------------------
    # 交互驱动的画像更新（每次交互后）
    # ------------------------------------------------------------

    def update_from_interaction(
        self,
        session_id: str,
        cognitive_state: CognitiveState,
        classification: ClassificationResult,
    ) -> UserProfile:
        """每次交互后更新动态指标（滑动平均 + 认知轨迹 + 轮次）"""
        profile = self.get_or_create(session_id)

        profile.cognitive_level_trajectory.append(cognitive_state.level.value)
        if len(profile.cognitive_level_trajectory) > 20:
            profile.cognitive_level_trajectory = profile.cognitive_level_trajectory[-20:]

        alpha = 0.3  # 平滑系数（与原项目一致）
        profile.avg_engagement = (
            alpha * cognitive_state.engagement_level
            + (1 - alpha) * profile.avg_engagement
        )
        profile.avg_frustration = (
            alpha * cognitive_state.frustration_level
            + (1 - alpha) * profile.avg_frustration
        )
        profile.total_sessions += 1
        profile.last_active_timestamp = time.time()

        self._profiles[session_id] = profile
        self._on_dirty(session_id)
        return profile

    @staticmethod
    def weak_topics_snapshot(profile: Optional[UserProfile], n: int = 3) -> list[str]:
        return profile.weak_topics[-n:] if profile else []
