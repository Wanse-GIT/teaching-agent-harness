"""
记忆巩固器（Memory Consolidator）

职责：把每轮交互的产出沉淀到长期记忆 —— 这是「进化」环节的具体执行者。
  - ReflectionResult → 用户画像更新（知识点/偏好/个性标签/策略标签）
  - ReflectionResult → 策略经验记忆更新（效果统计 + 调整建议）
  - ReflectionResult → 教学经验库沉淀（v5.0：跨用户统一整理）
  - 交互记录 → 工作记忆历史轨迹 + 画像动态指标
  - 进步显著且挫败低 → 自动标记问题解决（教学理念：及时正反馈）

隔离语义（v5.0）：
  - session_id（隔离后的 u:{uid}:{cid}）：工作记忆操作
  - profile_key（p:{uid}）：画像操作（未传时兼容回退为 session_id）
"""

import logging

from app.config import settings
from app.models.domain import (
    ClassificationResult,
    CognitiveState,
    ReflectionResult,
)

logger = logging.getLogger(__name__)


class MemoryConsolidator:
    """记忆巩固：情景产出 → 长期记忆（画像 + 策略经验 + 教学经验库）"""

    def __init__(self, working, profile, strategy, experience=None):
        # 传入三层记忆实例（由 memory/__init__ 组装）
        self.working = working
        self.profile = profile
        self.strategy = strategy
        self.experience = experience

    # ------------------------------------------------------------
    # 反思结果 → 用户画像
    # ------------------------------------------------------------

    def apply_reflection_to_profile(self, session_id: str, reflection: ReflectionResult):
        """将反思产出的 profile_updates 建议应用到画像（键名已归一化）"""
        prof = self.profile.get_or_create(session_id)
        updates = reflection.profile_updates

        for t in updates.get("add_known_topics", []):
            if t not in prof.known_topics:
                prof.known_topics.append(t)
            if t in prof.weak_topics:
                prof.weak_topics.remove(t)

        for t in updates.get("add_weak_topics", []):
            if t not in prof.weak_topics:
                prof.weak_topics.append(t)

        for t in updates.get("add_interest_topics", []):
            if t not in prof.interest_topics:
                prof.interest_topics.append(t)

        # 学习偏好
        if updates.get("preferred_learning_style"):
            prof.preferred_learning_style = updates["preferred_learning_style"]
        if updates.get("preferred_encouragement_style"):
            prof.preferred_encouragement_style = updates["preferred_encouragement_style"]
        if updates.get("preferred_difficulty"):
            prof.preferred_difficulty = updates["preferred_difficulty"]

        # 身份发现（v5.2：画像白手起家，身份信息由反思从对话中提取）
        if updates.get("identity"):
            prof.identity = updates["identity"]
        if updates.get("grade"):
            prof.grade = updates["grade"]
        if updates.get("major"):
            prof.major = updates["major"]
        if updates.get("location"):
            prof.location = updates["location"]
        for c in updates.get("add_current_courses", []):
            if c and c not in prof.current_courses:
                prof.current_courses.append(c)

        # 策略标签（有效/无效互斥）
        for s in updates.get("add_effective_strategies", []):
            if s not in prof.effective_strategies:
                prof.effective_strategies.append(s)
            if s in prof.ineffective_strategies:
                prof.ineffective_strategies.remove(s)
        for s in updates.get("add_ineffective_strategies", []):
            if s not in prof.ineffective_strategies:
                prof.ineffective_strategies.append(s)
            if s in prof.effective_strategies:
                prof.effective_strategies.remove(s)

        # 个性标签
        for t in updates.get("add_personality_tags", []):
            if t not in prof.personality_tags:
                prof.personality_tags.append(t)
        for t in updates.get("remove_personality_tags", []):
            if t in prof.personality_tags:
                prof.personality_tags.remove(t)

        prof.last_active_timestamp = __import__("time").time()
        # 直接写回完整画像对象并标记脏（触发持久化）
        self.profile._profiles[session_id] = prof
        self.profile._on_dirty(session_id)
        return prof

    # ------------------------------------------------------------
    # 反思结果 → 策略经验
    # ------------------------------------------------------------

    def apply_reflection_to_strategy(
        self,
        classification: ClassificationResult,
        guidance_mode: str,
        reflection: ReflectionResult,
    ):
        return self.strategy.update(
            classification.question_type,
            guidance_mode,
            reflection.strategy_effectiveness,
            reflection.student_progress,
            reflection.adjustments,
        )

    # ------------------------------------------------------------
    # 完整巩固流程（反思子图调用）
    # ------------------------------------------------------------

    def consolidate_turn(
        self,
        session_id: str,
        classification: ClassificationResult,
        cognitive_state: CognitiveState,
        guidance_mode: str,
        reflection: ReflectionResult,
        profile_key: str | None = None,
        topic: str | None = None,
        contributor: str | None = None,
    ) -> dict:
        """
        巩固一轮完整交互：
          1. 记录交互轨迹（工作记忆，按用户隔离的 session_id）
          2. 更新画像动态指标 + 反思建议（画像，按用户隔离的 profile_key）
          3. 更新策略经验统计（全局）
          4. 沉淀教学经验库（v5.0：跨用户统一整理）
          5. 判断是否自动标记问题解决（教学理念：进步显著+挫败低 → 正反馈）
        """
        profile_key = profile_key or session_id

        # 1. 交互轨迹
        self.working.record_interaction(session_id, classification, cognitive_state)

        # 2. 画像更新
        self.profile.update_from_interaction(profile_key, cognitive_state, classification)
        self.apply_reflection_to_profile(profile_key, reflection)

        # 3. 策略经验更新
        stats = self.apply_reflection_to_strategy(classification, guidance_mode, reflection)

        # 4. 教学经验沉淀（效果达标且有主题才入库；匿名贡献）
        insight = None
        if self.experience is not None:
            insight = self.experience.record(
                topic=topic,
                question_type=classification.question_type.value,
                guidance_mode=guidance_mode,
                effectiveness=reflection.strategy_effectiveness,
                adjustments=reflection.adjustments,
                contributor=contributor,
                min_effectiveness=settings.experience_min_effectiveness,
            )

        # 5. 问题解决判定（移植原项目阈值：progress>0.7 且 frustration<0.2）
        resolved = False
        if reflection.student_progress > 0.7 and cognitive_state.frustration_level < 0.2:
            self.working.mark_problem_resolved(session_id)
            resolved = True

        logger.info(
            f"[巩固] session={session_id}: effectiveness={reflection.strategy_effectiveness:.2f}, "
            f"progress={reflection.student_progress:.2f}, auto_resolved={resolved}, "
            f"strategy_uses={stats.total_uses}, insight={'+' if insight else '-'}"
        )
        return {"auto_resolved": resolved, "insight_id": insight.id if insight else None}
