"""
三层记忆框架 - 第一层：工作记忆（短期会话记忆）

职责：
  - 当前会话的消息历史（带窗口裁剪）
  - 当前认知状态、活跃问题、活跃步骤计划
  - 会话连续性状态（问题轮数、是否已解决）
  - 步骤追踪器的全部操作（创建/推进/清除/摘要）

进程内缓存保证读取性能；持久化由 persistence 层通过脏标记批量完成。
"""

import logging
import time
from typing import Callable, Optional

from app.models.domain import (
    AgentPhase,
    ChatMessage,
    ClassificationResult,
    CognitiveLevel,
    CognitiveState,
    GuidanceMode,
    QuestionType,
    SessionState,
    StepEvaluationResult,
    StepItem,
    StepPlan,
    StepStatus,
    gen_id,
)

logger = logging.getLogger(__name__)


class WorkingMemory:
    """工作记忆：会话级状态（对应原 agent_sessions 语义）"""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        # 持久化钩子由 persistence 层注入，避免循环依赖
        self._on_dirty: Callable[[str], None] = lambda sid: None

    def set_dirty_hook(self, hook: Callable[[str], None]):
        self._on_dirty = hook

    # ------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------

    def get_or_create(self, session_id: str) -> SessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                phase=AgentPhase.IDLE,
                cognitive_state=CognitiveState(level=CognitiveLevel.UNDERSTAND),
                created_at=time.time(),
                updated_at=time.time(),
                last_problem_resolved=True,
            )
            self._on_dirty(session_id)  # 新建立即持久化
        return self._sessions[session_id]

    def update(self, session_id: str, **updates) -> SessionState:
        session = self.get_or_create(session_id)
        for key, value in updates.items():
            if hasattr(session, key):
                setattr(session, key, value)
        session.updated_at = time.time()
        self._sessions[session_id] = session
        self._on_dirty(session_id)
        return session

    def clear(self, session_id: str):
        self._sessions.pop(session_id, None)
        self._on_dirty(session_id)

    def all_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def export_sessions(self) -> dict[str, SessionState]:
        return dict(self._sessions)

    def import_sessions(self, sessions: dict[str, SessionState]):
        self._sessions.update(sessions)

    # ------------------------------------------------------------
    # 消息历史
    # ------------------------------------------------------------

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        question_type: Optional[QuestionType] = None,
        guidance_mode: Optional[str] = None,
        cognitive_level: Optional[CognitiveLevel] = None,
        problem_resolved: Optional[bool] = None,
        step_progress: Optional[str] = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            id=gen_id("m"),
            role=role,
            content=content,
            timestamp=time.time(),
            question_type=question_type,
            guidance_mode=guidance_mode,
            cognitive_level=cognitive_level,
            problem_resolved=problem_resolved,
            step_progress=step_progress,
        )
        session = self.get_or_create(session_id)
        session.messages.append(msg)
        session.updated_at = time.time()
        self._on_dirty(session_id)
        return msg

    def get_recent_history_text(self, session_id: str, max_messages: int = 6) -> str:
        """最近对话历史的文本表示（注入 Prompt 用）"""
        session = self._sessions.get(session_id)
        if not session or not session.messages:
            return ""
        recent = session.messages[-max_messages:]
        return "\n".join(
            f"{'学生' if m.role == 'user' else '导师'}: {m.content[:200]}"
            for m in recent
        )

    # ------------------------------------------------------------
    # 会话连续性（教学理念：跨轮次跟踪同一问题）
    # ------------------------------------------------------------

    def check_unresolved_problem(self, session_id: str) -> Optional[dict]:
        """检查是否存在未解决的问题；返回信息字典或 None"""
        session = self._sessions.get(session_id)
        if not session:
            return None
        if not session.active_problem or session.last_problem_resolved:
            return None

        last_interaction = ""
        if session.messages:
            recent = session.messages[-2:]
            last_interaction = "\n".join(
                f"{'学生' if m.role == 'user' else '导师'}: {m.content[:150]}"
                for m in recent
            )
        return {
            "topic": session.active_problem,
            "question_type": session.active_problem_type,
            "turn_count": session.problem_turn_count,
            "last_interaction": last_interaction,
        }

    def set_active_problem(self, session_id: str, topic: str, question_type: QuestionType):
        session = self.get_or_create(session_id)
        session.active_problem = topic
        session.active_problem_type = question_type
        session.active_problem_start = time.time()
        session.problem_turn_count = 0
        session.last_problem_resolved = False
        self._sessions[session_id] = session
        self._on_dirty(session_id)

    def mark_problem_resolved(self, session_id: str):
        """标记问题已解决（记录到最近5个已解决问题列表）"""
        session = self.get_or_create(session_id)
        if session.active_problem:
            session.resolved_problems.append(session.active_problem)
            if len(session.resolved_problems) > 5:
                session.resolved_problems = session.resolved_problems[-5:]
        session.active_problem = None
        session.active_problem_type = None
        session.active_problem_start = None
        session.problem_turn_count = 0
        session.last_problem_resolved = True
        self._sessions[session_id] = session
        self._on_dirty(session_id)

    def increment_problem_turn(self, session_id: str):
        session = self.get_or_create(session_id)
        session.problem_turn_count += 1
        self._sessions[session_id] = session
        self._on_dirty(session_id)

    def record_interaction(
        self,
        session_id: str,
        classification: ClassificationResult,
        cognitive_state: CognitiveState,
    ) -> None:
        """记录交互历史（分类轨迹 + 认知轨迹）"""
        session = self.get_or_create(session_id)
        session.history_classifications.append(classification)
        session.history_cognitive_states.append(cognitive_state.model_copy())
        self._on_dirty(session_id)

    def get_interaction_stats(self, session_id: str) -> dict:
        """交互统计（前端仪表盘）"""
        session = self._sessions.get(session_id)
        result: dict = {"profile_available": False}
        if not session or not session.history_classifications:
            return result

        type_counts: dict[str, int] = {}
        for c in session.history_classifications:
            type_counts[c.question_type.value] = type_counts.get(c.question_type.value, 0) + 1

        trajectory = session.history_cognitive_states
        engagement_trend = 0.0
        frustration_trend = 0.0
        if len(trajectory) >= 2:
            recent = trajectory[-3:]
            older = trajectory[-6:-3]
            if older:
                engagement_trend = (
                    sum(s.engagement_level for s in recent) / len(recent)
                    - sum(s.engagement_level for s in older) / len(older)
                )
                frustration_trend = (
                    sum(s.frustration_level for s in recent) / len(recent)
                    - sum(s.frustration_level for s in older) / len(older)
                )

        result.update({
            "profile_available": True,
            "type_counts": type_counts,
            "total_interactions": len(session.history_classifications),
            "engagement_trend": round(engagement_trend, 3),
            "frustration_trend": round(frustration_trend, 3),
        })
        return result

    # ------------------------------------------------------------
    # 步骤追踪器（Step Tracker，教学理念核心组件）
    # ------------------------------------------------------------

    def get_active_step_plan(self, session_id: str) -> Optional[StepPlan]:
        """获取当前活跃的步骤计划（已完成计划返回 None）"""
        session = self._sessions.get(session_id)
        if not session or not session.active_step_plan:
            return None
        if session.active_step_plan.is_complete:
            return None
        return session.active_step_plan

    def peek_step_plan(self, session_id: str) -> Optional[StepPlan]:
        """读取步骤计划（含已完成的，用于判断 is_step_complete）"""
        session = self._sessions.get(session_id)
        return session.active_step_plan if session else None

    def create_step_plan(
        self,
        session_id: str,
        topic: str,
        question_type: QuestionType,
        guidance_mode: GuidanceMode,
        steps: list[StepItem],
        example: Optional[str] = None,
    ) -> StepPlan:
        plan = StepPlan(
            plan_id=gen_id("plan"),
            topic=topic,
            question_type=question_type,
            guidance_mode=guidance_mode,
            example=example,
            steps=steps,
            current_step_index=0,
            is_complete=False,
            created_at=time.time(),
            updated_at=time.time(),
        )
        if plan.steps:
            plan.steps[0].status = StepStatus.IN_PROGRESS

        session = self.get_or_create(session_id)
        session.active_step_plan = plan
        self._on_dirty(session_id)
        logger.info(f"创建步骤计划: topic={topic}, type={question_type.value}, steps={len(steps)}")
        return plan

    def advance_step_plan(
        self, session_id: str, evaluation: StepEvaluationResult
    ) -> Optional[StepPlan]:
        """
        根据步骤评估结果推进步骤计划

        - step_completed 且 should_advance → 推进到下一步
        - step_completed 但 !should_advance → 留在当前步骤，重新引导
        - !step_completed → 标记为卡住，提供提示
        """
        session = self._sessions.get(session_id)
        if not session or not session.active_step_plan:
            return None

        plan = session.active_step_plan

        if evaluation.step_completed and evaluation.should_advance:
            next_step = plan.advance_step()
            if next_step:
                logger.info(
                    f"步骤推进: session={session_id}, "
                    f"step {plan.current_step_index}/{len(plan.steps)}"
                )
            else:
                logger.info(f"步骤计划完成: session={session_id}, topic={plan.topic}")
        elif evaluation.step_completed and not evaluation.should_advance:
            plan.mark_current_retry()
            logger.info(f"步骤完成但暂不推进: session={session_id}")
        else:
            plan.mark_current_stuck()
            logger.info(
                f"步骤卡住: session={session_id}, "
                f"attempts={plan.current_step.attempts if plan.current_step else 0}"
            )

        # 记录学生回答摘要
        if plan.current_step and evaluation.concept_mastered:
            plan.current_step.student_response_summary = evaluation.concept_mastered

        session.active_step_plan = plan
        self._on_dirty(session_id)
        return plan

    def clear_step_plan(self, session_id: str):
        session = self.get_or_create(session_id)
        session.active_step_plan = None
        self._on_dirty(session_id)

    def get_step_plan_summary(self, session_id: str) -> Optional[dict]:
        """步骤计划摘要（API/前端展示）"""
        plan = self.peek_step_plan(session_id)
        if not plan:
            return None
        return {
            "plan_id": plan.plan_id,
            "topic": plan.topic,
            "question_type": plan.question_type.value,
            "guidance_mode": plan.guidance_mode.value,
            "total_steps": len(plan.steps),
            "current_step_index": plan.current_step_index,
            "progress": plan.progress_text,
            "is_complete": plan.is_complete,
            "completed_steps": plan.completed_steps_count,
            "current_step": {
                "index": plan.current_step.index if plan.current_step else None,
                "title": plan.current_step.title if plan.current_step else None,
                "description": plan.current_step.description if plan.current_step else None,
                "status": plan.current_step.status.value if plan.current_step else None,
                "attempts": plan.current_step.attempts if plan.current_step else None,
            } if plan.current_step else None,
            "steps": [
                {
                    "index": s.index,
                    "title": s.title,
                    "status": s.status.value,
                    "attempts": s.attempts,
                    "description": s.description,
                    "hint": s.hint,
                }
                for s in plan.steps
            ],
        }

    def build_step_plan_prompt_section(self, session_id: str) -> str:
        """
        构建步骤计划的 Prompt 片段（让导师 LLM 知道当前步骤上下文）
        精简格式：只显示已完成步骤摘要 + 当前步骤详情
        """
        plan = self.get_active_step_plan(session_id)
        if not plan:
            return ""

        parts = [f"【步骤进度 {plan.progress_text}】"]
        for s in plan.steps:
            if s.status == StepStatus.COMPLETED:
                parts.append(f"  ✅ Step {s.index}: {s.title}")
            elif s.status == StepStatus.IN_PROGRESS:
                parts.append(f"  👉 Step {s.index}: {s.title} [当前]")
            elif s.status == StepStatus.STUCK:
                parts.append(f"  ⚠️ Step {s.index}: {s.title} [卡住]")

        current = plan.current_step
        if current:
            parts.append(f"【当前 Step {current.index}：{current.title}】")
            parts.append(f"  {current.description}")
            if current.hint:
                parts.append(f"  提示：{current.hint}")
            if current.attempts > 0:
                parts.append(f"  已尝试：{current.attempts}次")
            parts.append("只引导当前步骤，等学生完成后系统自动推进。")

        return "\n".join(parts)
