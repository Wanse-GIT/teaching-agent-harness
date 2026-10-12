"""
Harness - 上下文装配器（Context Assembler）

Harness 工程的核心职责之一：在每轮图执行前，从三层记忆检索并装配
各 Agent 需要的上下文，避免每个 Agent 自行拼 Prompt 的重复劳动。

装配产物 TurnContext 包含：
  - history_text          最近对话历史（工作记忆）
  - unresolved_info       未解决问题信息（工作记忆）
  - prev_problem_resolved 上一个问题是否已解决（影响误判延续）
  - has_active_plan       是否有活跃步骤计划
  - profile_section       学生画像 Prompt 片段（长期画像记忆，按用户隔离）
  - step_plan_section     步骤计划 Prompt 片段（工作记忆）
  - strategy_hints        策略经验提示（策略经验记忆，全局）
  - experience_hints      教学经验提示（v5.0：跨用户统一整理）
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.harness.policies import build_user_profile_section
from app.memory import memory
from app.models.domain import StepPlan

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """一轮对话的装配上下文"""
    conversation_id: str
    user_message: str
    history_text: str = ""
    unresolved_info: Optional[dict] = None
    prev_problem_resolved: bool = True
    has_active_plan: bool = False
    active_plan: Optional[StepPlan] = None
    profile_section: str = ""
    profile_summary: str = ""
    step_plan_section: str = ""
    strategy_hints: list[str] = field(default_factory=list)
    experience_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "history_text": self.history_text,
            "unresolved_info": self.unresolved_info,
            "prev_problem_resolved": self.prev_problem_resolved,
            "has_active_plan": self.has_active_plan,
            "profile_section": self.profile_section,
            "step_plan_section": self.step_plan_section,
            "strategy_hints": self.strategy_hints,
            "experience_hints": self.experience_hints,
        }

    @classmethod
    def from_dict(cls, d: dict, conversation_id: str = "", user_message: str = "") -> "TurnContext":
        ctx = cls(conversation_id=conversation_id, user_message=user_message)
        for k, v in (d or {}).items():
            if hasattr(ctx, k):
                setattr(ctx, k, v)
        return ctx


class ContextAssembler:
    """上下文装配器"""

    def assemble(
        self,
        conversation_id: str,
        user_message: str,
        profile_key: Optional[str] = None,
        topic_hint: Optional[str] = None,
        question_type: Optional[str] = None,
    ) -> TurnContext:
        """装配轮次上下文

        v5.0：profile_key 独立于 conversation_id（画像按用户隔离、跨会话共享）；
        topic_hint/question_type 用于教学经验库的精准检索。
        """
        from app.config import settings

        profile_key = profile_key or conversation_id
        session = memory.working.get_or_create(conversation_id)

        unresolved_info = memory.working.check_unresolved_problem(conversation_id)
        if unresolved_info and unresolved_info.get("question_type") is not None:
            qt = unresolved_info["question_type"]
            unresolved_info["question_type"] = (
                qt.value if hasattr(qt, "value") else str(qt)
            )
        active_plan = memory.working.get_active_step_plan(conversation_id)
        prev_resolved = unresolved_info is None and session.last_problem_resolved

        # 教学经验库精准检索：优先用未解决问题的主题，其次用当前感知主题
        topic_for_hints = topic_hint
        if not topic_for_hints and unresolved_info:
            topic_for_hints = unresolved_info.get("topic")

        return TurnContext(
            conversation_id=conversation_id,
            user_message=user_message,
            history_text=memory.working.get_recent_history_text(
                conversation_id, settings.history_window
            ),
            unresolved_info=unresolved_info,
            prev_problem_resolved=prev_resolved,
            has_active_plan=active_plan is not None,
            active_plan=active_plan,
            profile_section=build_user_profile_section(
                memory.profile.get_or_create(profile_key)
            ),
            profile_summary=memory.profile.get_summary_text(profile_key),
            step_plan_section=memory.working.build_step_plan_prompt_section(conversation_id),
            strategy_hints=memory.strategy.get_strategy_hints(),
            experience_hints=memory.experience.get_hints(
                topic=topic_for_hints,
                question_type=question_type,
                limit=3,
            ),
        )


context_assembler = ContextAssembler()
