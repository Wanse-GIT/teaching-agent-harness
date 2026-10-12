"""
智能导学 Agent（Harness 重构版）- 领域模型

教学理念不变，类型契约与原项目保持一致：
  - 闭环架构：感知 → 分类 → 评估 → 步骤计划 → 步骤评估 → 引导 → 反思 → 进化
  - 核心差异化：学习意愿检测（eager/troubled/indifferent）→ 三种引导策略
  - 步骤追踪器 + 用户画像 + 策略经验进化

新增（Harness 工程化）：
  - RouteDecision：Supervisor 调度决策
  - StrategyStats.effectiveness_history：策略效果曲线（供前端仪表盘）
  - profile_updates 键名归一化（修复原项目 camelCase/snake_case 不一致的隐患）
"""

import time
from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field


# ============================================================
# 教学理念核心类型（与原项目一致）
# ============================================================

class LearningDesireLevel(str, Enum):
    """学习意愿强度 - 分类核心判据"""
    EAGER = "eager"                   # 主动学习意愿：好奇探索、系统学习、深入理解
    TROUBLED = "troubled"             # 被动学习意愿：遇到困难、急需帮助、急迫感>学习欲
    INDIFFERENT = "indifferent"       # 无学习意愿：快速查证、一句话即可满足


class QuestionType(str, Enum):
    """问题类型 - 基于学习意愿的分类映射"""
    TEACHING = "teaching"             # eager → 示例复现引导
    PROBLEM_SOLVING = "problem_solving"  # troubled → 苏格拉底式引导
    FACTUAL = "factual"               # indifferent → 直接回答


class GuidanceMode(str, Enum):
    """引导模式"""
    EXAMPLE_REPRODUCTION = "example_reproduction"  # 示例复现模式（教学性）
    SOCRATIC_QUESTIONING = "socratic_questioning"  # 苏格拉底式提问（解答性）
    DIRECT_ANSWER = "direct_answer"                # 直接回答模式（事实性）


class CognitiveLevel(str, Enum):
    """认知层级（Bloom 分类法）"""
    REMEMBER = "remember"
    UNDERSTAND = "understand"
    APPLY = "apply"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"
    CREATE = "create"


class AgentPhase(str, Enum):
    """Agent 处理阶段（对外展示用）"""
    IDLE = "idle"
    ROUTING = "routing"
    DIAGNOSING = "diagnosing"
    ASSESSING_STEP = "assessing_step"
    GUIDING = "guiding"
    REFLECTING = "reflecting"
    DONE = "done"


# ============================================================
# 步骤追踪器（与原项目一致）
# ============================================================

class StepStatus(str, Enum):
    """步骤状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    STUCK = "stuck"


class StepItem(BaseModel):
    """单个引导步骤（原子性：完成一步才进入下一步）"""
    index: int
    title: str
    description: str
    hint: str = ""
    expected_concepts: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    student_response_summary: Optional[str] = None
    completed_at: Optional[float] = None


class StepPlan(BaseModel):
    """步骤计划 - 完整的引导步骤序列"""
    plan_id: str = ""
    topic: str
    question_type: QuestionType
    guidance_mode: GuidanceMode
    example: Optional[str] = None
    steps: list[StepItem] = Field(default_factory=list)
    current_step_index: int = 0
    is_complete: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def current_step(self) -> Optional[StepItem]:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    @property
    def progress_text(self) -> str:
        total = len(self.steps)
        current = min(self.current_step_index + 1, total)
        return f"{current}/{total}"

    @property
    def completed_steps_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)

    def advance_step(self) -> Optional[StepItem]:
        """推进到下一步，返回下一步（教学理念：宁可推进也不要卡住）"""
        if self.current_step and self.current_step.status in (
            StepStatus.IN_PROGRESS, StepStatus.STUCK
        ):
            self.current_step.status = StepStatus.COMPLETED
            self.current_step.completed_at = time.time()

        self.current_step_index += 1
        self.updated_at = time.time()

        if self.current_step_index >= len(self.steps):
            self.is_complete = True
            return None

        next_step = self.current_step
        if next_step:
            next_step.status = StepStatus.IN_PROGRESS
        return next_step

    def mark_current_stuck(self):
        if self.current_step:
            self.current_step.status = StepStatus.STUCK
            self.current_step.attempts += 1
        self.updated_at = time.time()

    def mark_current_retry(self):
        if self.current_step:
            self.current_step.attempts += 1
            self.current_step.status = StepStatus.IN_PROGRESS
        self.updated_at = time.time()


# ============================================================
# 用户画像（长期记忆，与原项目一致）
# ============================================================

class UserProfile(BaseModel):
    """用户画像 - 跟踪用户的长期学习特征

    v5.2 白手起家：不再预设任何身份信息（原版默认「昆明大一计算机学生」已移除），
    身份/年级/专业/所在地/在学课程/个性标签全部通过对话由反思链路逐步发现填充。
    保留的仅是与具体人无关的运营默认值（引导风格/难度档位等）。
    """
    identity: str = ""
    grade: str = ""
    major: str = ""
    location: str = ""

    known_topics: list[str] = Field(default_factory=list)
    weak_topics: list[str] = Field(default_factory=list)
    interest_topics: list[str] = Field(default_factory=list)
    current_courses: list[str] = Field(default_factory=list)  # 由反思从对话中发现补充

    # 运营默认值（非个人信息，可被反思调整）
    preferred_learning_style: str = "visual"
    preferred_encouragement_style: str = "growth_mindset"
    preferred_difficulty: str = "moderate"

    total_sessions: int = 0
    total_study_time_minutes: float = 0.0
    streak_days: int = 0
    last_active_timestamp: float = 0.0

    cognitive_level_trajectory: list[str] = Field(default_factory=list)
    avg_engagement: float = 0.5
    avg_frustration: float = 0.0

    personality_tags: list[str] = Field(default_factory=list)  # 由反思观察补充
    effective_strategies: list[str] = Field(default_factory=list)
    ineffective_strategies: list[str] = Field(default_factory=list)


# ============================================================
# 各 Agent 的输入输出（与原项目语义一致）
# ============================================================

class PerceptionResult(BaseModel):
    """感知模块输出（诊断师）"""
    raw_input: str
    intent: str = "unknown"
    subject: Optional[str] = None
    topic: Optional[str] = None
    prerequisites: list[str] = Field(default_factory=list)
    difficulty_indicators: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    emotional_tone: str = "neutral"
    is_continuation: bool = False
    continuation_topic: Optional[str] = None


class ClassificationResult(BaseModel):
    """分类模块输出（基于学习意愿检测）"""
    question_type: QuestionType = QuestionType.PROBLEM_SOLVING
    learning_desire_level: LearningDesireLevel = LearningDesireLevel.INDIFFERENT
    learning_desire_score: float = 0.0
    detected_emotions: list[str] = Field(default_factory=list)
    confidence: float = 0.7
    reasoning: str = ""
    sub_categories: list[str] = Field(default_factory=list)
    is_continuation: bool = False


class CognitiveState(BaseModel):
    """认知状态"""
    level: CognitiveLevel = CognitiveLevel.UNDERSTAND
    confidence_score: float = 0.5
    knowledge_gaps: list[str] = Field(default_factory=list)
    learning_style: str = "visual"
    engagement_level: float = 0.7
    frustration_level: float = 0.0


class GuidanceAction(BaseModel):
    """引导动作（导师输出）"""
    mode: GuidanceMode
    content: str
    example: Optional[str] = None
    follow_up_questions: list[str] = Field(default_factory=list)
    encouragement: Optional[str] = None
    is_continuation: bool = False
    continuation_context: Optional[str] = None
    step_progress: Optional[str] = None
    current_step_title: Optional[str] = None


class StepEvaluationResult(BaseModel):
    """步骤评估结果（步骤评估官输出）"""
    step_completed: bool = True
    student_understood: float = 0.5
    should_advance: bool = True
    stay_reason: Optional[str] = None
    hint_to_provide: Optional[str] = None
    concept_mastered: Optional[str] = None


class ReflectionResult(BaseModel):
    """反思结果（反思进化师输出）"""
    strategy_effectiveness: float = 0.5
    student_progress: float = 0.5
    adjustments: list[str] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    profile_updates: dict = Field(default_factory=dict)


# ============================================================
# Supervisor 调度决策（Harness 新增）
# ============================================================

ROUTE_TARGETS = ("diagnostician", "step_assessor", "tutor", "finish")


class RouteDecision(BaseModel):
    """调度官的路由决策"""
    target: str = "diagnostician"            # diagnostician/step_assessor/tutor/finish
    reason: str = ""                          # 调度理由（前端流水线 tooltip 展示）
    source: str = "rules"                     # llm | rules（决策来源）


# ============================================================
# 会话状态（短期记忆）
# ============================================================

class ChatMessage(BaseModel):
    """聊天消息"""
    id: str
    role: str  # "user" | "assistant"
    content: str
    timestamp: float
    question_type: Optional[QuestionType] = None
    guidance_mode: Optional[str] = None
    cognitive_level: Optional[CognitiveLevel] = None
    problem_resolved: Optional[bool] = None
    step_progress: Optional[str] = None


class SessionState(BaseModel):
    """会话状态（短期/工作记忆的核心结构）"""
    session_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    perception: Optional[PerceptionResult] = None
    classification: Optional[ClassificationResult] = None
    cognitive_state: CognitiveState = Field(default_factory=CognitiveState)
    current_guidance: Optional[GuidanceAction] = None
    phase: AgentPhase = AgentPhase.IDLE
    history_classifications: list[ClassificationResult] = Field(default_factory=list)
    history_cognitive_states: list[CognitiveState] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    # ===== 会话连续性 =====
    active_problem: Optional[str] = None
    active_problem_type: Optional[QuestionType] = None
    active_problem_start: Optional[float] = None
    problem_turn_count: int = 0
    last_problem_resolved: bool = True
    resolved_problems: list[str] = Field(default_factory=list)

    # ===== 步骤追踪器 =====
    active_step_plan: Optional[StepPlan] = None


# ============================================================
# 策略经验（程序性记忆）
# ============================================================

class StrategyStats(BaseModel):
    """策略统计（新增 effectiveness_history 效果曲线）"""
    total_uses: int = 0
    avg_effectiveness: float = 0.0
    avg_progress: float = 0.0
    best_adjustments: list[str] = Field(default_factory=list)
    effectiveness_history: list[float] = Field(default_factory=list)  # 最近N次效果值


# ============================================================
# LLM 结构化输出模型（camelCase 自动映射，与原项目一致）
# ============================================================

class LLMPerceptionOutput(BaseModel):
    """LLM 感知输出的自动映射模型"""
    intent: str = Field(default="unknown", alias="intent")
    subject: Optional[str] = Field(default=None, alias="subject")
    topic: Optional[str] = Field(default=None, alias="topic")
    prerequisites: list[str] = Field(default_factory=list, alias="prerequisites")
    difficulty_indicators: list[str] = Field(default_factory=list, alias="difficultyIndicators")
    keywords: list[str] = Field(default_factory=list, alias="keywords")
    emotional_tone: str = Field(default="neutral", alias="emotionalTone")
    is_continuation: bool = Field(default=False, alias="isContinuation")
    continuation_topic: Optional[str] = Field(default=None, alias="continuationTopic")
    model_config = {"populate_by_name": True}


class LLMClassificationOutput(BaseModel):
    """LLM 分类输出的自动映射模型"""
    question_type: str = Field(default="problem_solving", alias="questionType")
    learning_desire_level: str = Field(default="indifferent", alias="learningDesireLevel")
    learning_desire_score: float = Field(default=0.0, ge=0.0, le=1.0, alias="learningDesireScore")
    detected_emotions: list[str] = Field(default_factory=list, alias="detectedEmotions")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0, alias="confidence")
    reasoning: str = Field(default="", alias="reasoning")
    sub_categories: list[str] = Field(default_factory=list, alias="subCategories")
    is_continuation: bool = Field(default=False, alias="isContinuation")
    continuation_reason: Optional[str] = Field(default=None, alias="continuationReason")
    model_config = {"populate_by_name": True}


class LLMAssessmentOutput(BaseModel):
    """LLM 认知评估输出的自动映射模型"""
    level: str = Field(default="understand", alias="level")
    confidence_score: float = Field(default=0.5, ge=0.0, le=1.0, alias="confidenceScore")
    knowledge_gaps: list[str] = Field(default_factory=list, alias="knowledgeGaps")
    learning_style: str = Field(default="visual", alias="learningStyle")
    engagement_level: float = Field(default=0.7, ge=0.0, le=1.0, alias="engagementLevel")
    frustration_level: float = Field(default=0.0, ge=0.0, le=1.0, alias="frustrationLevel")
    model_config = {"populate_by_name": True}


class LLMStepEvalOutput(BaseModel):
    """LLM 步骤评估输出的自动映射模型"""
    step_completed: bool = Field(default=True, alias="stepCompleted")
    student_understood: float = Field(default=0.5, ge=0.0, le=1.0, alias="studentUnderstood")
    should_advance: bool = Field(default=True, alias="shouldAdvance")
    stay_reason: Optional[str] = Field(default=None, alias="stayReason")
    hint_to_provide: Optional[str] = Field(default=None, alias="hintToProvide")
    concept_mastered: Optional[str] = Field(default=None, alias="conceptMastered")
    model_config = {"populate_by_name": True}


class LLMReflectionOutput(BaseModel):
    """LLM 反思输出的自动映射模型"""
    strategy_effectiveness: float = Field(default=0.5, ge=0.0, le=1.0, alias="strategyEffectiveness")
    student_progress: float = Field(default=0.5, ge=0.0, le=1.0, alias="studentProgress")
    adjustments: list[str] = Field(default_factory=list, alias="adjustments")
    insights: list[str] = Field(default_factory=list, alias="insights")
    profile_updates: dict = Field(default_factory=dict, alias="profileUpdates")
    model_config = {"populate_by_name": True}


# ============================================================
# 工具函数
# ============================================================

_PROFILE_UPDATE_KEY_ALIASES = {
    # camelCase（LLM输出）→ snake_case（内部存储），修复原项目键名不一致隐患
    "addKnownTopics": "add_known_topics",
    "addWeakTopics": "add_weak_topics",
    "addInterestTopics": "add_interest_topics",
    "addEffectiveStrategies": "add_effective_strategies",
    "addIneffectiveStrategies": "add_ineffective_strategies",
    "addPersonalityTags": "add_personality_tags",
    "removePersonalityTags": "remove_personality_tags",
    "preferredLearningStyle": "preferred_learning_style",
    "preferredEncouragementStyle": "preferred_encouragement_style",
    "preferredDifficulty": "preferred_difficulty",
    # v5.2 身份发现：identity/grade/major/location 键名本就是 snake_case 直通，
    # 仅需映射 addCurrentCourses
    "addCurrentCourses": "add_current_courses",
}


def normalize_profile_updates(raw: dict) -> dict:
    """将反思输出的 profile_updates 键名归一化为 snake_case"""
    out = {}
    for k, v in (raw or {}).items():
        if v in (None, [], ""):
            continue
        out[_PROFILE_UPDATE_KEY_ALIASES.get(k, k)] = v
    return out


def gen_id(prefix: str = "id") -> str:
    """生成唯一 ID"""
    return f"{prefix}-{int(time.time() * 1000)}-{id(object()):x}"


def now() -> float:
    return time.time()


def dumps(value: Any) -> dict:
    """Pydantic 模型 → JSON 安全 dict（作为 LangGraph 状态值）"""
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
