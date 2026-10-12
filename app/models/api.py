"""
智能导学 Agent（Harness 重构版）- API 模型

API v2 重新设计为 RESTful 风格：
  POST   /api/v2/conversations/{cid}/messages          发送消息（非流式）
  POST   /api/v2/conversations/{cid}/messages/stream   发送消息（SSE 流式）
  GET    /api/v2/conversations/{cid}                   会话摘要
  DELETE /api/v2/conversations/{cid}                   重置会话
  GET    /api/v2/conversations/{cid}/steps             步骤进度
  POST   /api/v2/conversations/{cid}/steps/skip        跳过当前步骤
  POST   /api/v2/conversations/{cid}/resolve           标记问题已解决
  GET    /api/v2/conversations/{cid}/profile           学生画像
  PATCH  /api/v2/conversations/{cid}/profile           更新学生画像
  GET    /api/v2/health                                健康检查
"""

from typing import Optional

from pydantic import BaseModel, Field

from app import __version__

from app.models.domain import (
    AgentPhase,
    CognitiveLevel,
    GuidanceMode,
    QuestionType,
)


# ============================================================
# 请求模型
# ============================================================

class MessageIn(BaseModel):
    """发送消息请求"""
    content: str = Field(min_length=1, description="学生消息内容")


class ProfileUpdateIn(BaseModel):
    """画像更新请求（约束可更新字段，防止任意字段篡改）"""
    identity: Optional[str] = None
    grade: Optional[str] = None
    major: Optional[str] = None
    location: Optional[str] = None
    preferred_learning_style: Optional[str] = None
    preferred_encouragement_style: Optional[str] = None
    preferred_difficulty: Optional[str] = None


# ============================================================
# 响应模型
# ============================================================

class AgentTrace(BaseModel):
    """本轮 Agent 协作轨迹（学习体验：让学生看见 Agent 如何协作）"""
    routes: list[dict] = Field(default_factory=list)     # [{from,to,reason,source}]
    agents: list[dict] = Field(default_factory=list)     # [{agent,phase,ms}]


class MessageResponse(BaseModel):
    """消息响应（完整引导结果）"""
    reply: str
    conversation_id: str
    turn_id: str = ""
    question_type: Optional[QuestionType] = None
    cognitive_level: Optional[CognitiveLevel] = None
    guidance_mode: Optional[GuidanceMode] = None
    phase: AgentPhase = AgentPhase.DONE
    example: Optional[str] = None
    encouragement: Optional[str] = None
    classification_reasoning: Optional[str] = None
    detected_emotions: list[str] = Field(default_factory=list)  # 诊断师检测到的情绪信号
    is_continuation: bool = False
    profile_summary: Optional[str] = None
    step_plan: Optional[dict] = None
    step_progress: Optional[str] = None
    current_step_title: Optional[str] = None
    trace: Optional[AgentTrace] = None


class StepPlanSummary(BaseModel):
    """步骤计划摘要"""
    plan_id: str
    topic: str
    question_type: str
    guidance_mode: str
    total_steps: int
    current_step_index: int
    progress: str
    is_complete: bool
    completed_steps: int
    current_step: Optional[dict] = None
    steps: list[dict] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    """会话摘要"""
    conversation_id: str
    phase: str
    message_count: int
    active_problem: Optional[str] = None
    problem_turn_count: int = 0
    last_problem_resolved: bool = True
    resolved_problems: list[str] = Field(default_factory=list)
    step_plan: Optional[StepPlanSummary] = None
    profile: dict = Field(default_factory=dict)
    user_profile: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """健康检查"""
    status: str = "ok"
    service: str = "智能导学 Agent (Harness)"
    version: str = __version__
    architecture: str = "harness + langgraph multi-agent"
    db_persistent: bool = False
    checkpointer: str = "memory"
    llm_channel: str = "unavailable"
    api_key_enabled: bool = False
    rate_limit_enabled: bool = False
    features: list[str] = Field(default_factory=list)
