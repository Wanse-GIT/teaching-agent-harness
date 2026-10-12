"""
Harness - Tutor Runner（统一运行入口）

Harness 工程的最终收口：
  1. 输入守卫（guardrails）
  2. 记录用户消息（工作记忆）
  3. 装配轮次上下文（context assembler）
  4. 构建 LangGraph 初始状态并执行主图（supervisor 多 Agent 协作）
  5. 组装最终回复（policies.compose_reply）+ 记录助手消息 + 批量持久化
  6. 触发反思后台子图（反射→巩固→进化→步骤计划懒加载）

支持两种模式：
  - run():          非流式，返回 MessageResponse
  - run_stream():   SSE 流式，边执行边推送 phase/agent/route/token/final 事件

用户隔离（v5.0）：
  - 外部 conversation_id 在入口处翻译为 u:{uid}:{cid}（工作记忆/图线程）
  - 画像使用 p:{uid}（跨会话共享、跨用户隔离）
  - user=None 时映射 guest 空间（开发模式，兼容旧行为）
  - 对外响应始终回传原始 conversation_id
"""

import asyncio
import logging
import uuid

from app.config import settings
from app.harness.context import context_assembler
from app.harness.events import TurnEventBus
from app.harness.graph import build_main_graph
from app.harness.guardrails import check_input
from app.harness.policies import compose_reply
from app.harness.reflection import run_reflection
from app.harness.scoping import scoped_conversation, scoped_profile
from app.harness.state import empty_state
from app.memory import memory
from app.models.api import AgentTrace, MessageResponse
from app.models.domain import (
    AgentPhase,
    ClassificationResult,
    CognitiveState,
    GuidanceAction,
    PerceptionResult,
)

logger = logging.getLogger(__name__)

_UNCONFIGURED_REPLY = (
    "⚠️ Agent 的 LLM 未配置，当前无法提供智能引导。\n\n"
    "请在项目根目录的 `.env` 文件中配置：\n"
    "- LLM_API_KEY=你的API密钥\n"
    "- LLM_BASE_URL=API地址\n"
    "- LLM_MODEL=模型名称\n\n"
    "支持豆包、DeepSeek、通义千问、OpenAI 等任何 OpenAI 兼容 API。\n"
    "配置后重启服务即可。"
)

_FALLBACK_REPLY = (
    "我理解你的问题了。让我来帮你——你能告诉我目前对这个问题的了解有多少吗？"
)


class TutorRunner:
    """智能导学 Runner（Harness 统一入口）"""

    def __init__(self):
        self._graph = None

    # ------------------------------------------------------------
    # 初始化（FastAPI lifespan 调用）
    # ------------------------------------------------------------

    def init(self, checkpointer=None):
        self._graph = build_main_graph(checkpointer)
        return self

    @property
    def graph(self):
        if self._graph is None:
            self.init()
        return self._graph

    # ------------------------------------------------------------
    # 非流式执行
    # ------------------------------------------------------------

    async def run(
        self, conversation_id: str, message: str, user: dict | None = None
    ) -> MessageResponse:
        bus = TurnEventBus(streaming=False)
        try:
            guard = check_input(message)
            if not guard.ok:
                raise ValueError(guard.error)

            if not self._llm_available():
                return self._unconfigured_response(conversation_id, guard.content, bus, user)

            response = await self._execute(conversation_id, guard.content, bus, user)
            return response
        except ValueError as e:
            raise
        except Exception as e:
            logger.error(f"Runner 执行失败: {e}", exc_info=True)
            bus.error(str(e))
            return self._fallback_response(conversation_id, bus, user)

    # ------------------------------------------------------------
    # SSE 流式执行
    # ------------------------------------------------------------

    async def run_stream(
        self, conversation_id: str, message: str, user: dict | None = None
    ) -> TurnEventBus:
        """流式执行：返回已启动的事件总线；最终结果通过 final 事件推送"""
        bus = TurnEventBus(streaming=True)

        async def _run():
            try:
                guard = check_input(message)
                if not guard.ok:
                    bus.error(guard.error)
                    bus.emit("final", payload=self._error_payload(guard.error))
                    bus.done()
                    return

                if not self._llm_available():
                    payload = self._unconfigured_response(
                        conversation_id, guard.content, bus, user
                    )
                    bus.emit("final", payload=payload.model_dump(mode="json"))
                    bus.done()
                    return

                response = await self._execute(conversation_id, guard.content, bus, user)
                bus.emit("final", payload=response.model_dump(mode="json"))
                bus.done()
            except Exception as e:
                logger.error(f"流式 Runner 执行失败: {e}", exc_info=True)
                bus.error(str(e))
                payload = self._fallback_response(conversation_id, bus, user)
                bus.emit("final", payload=payload.model_dump(mode="json"))
                bus.done()

        asyncio.create_task(_run())
        return bus

    # ------------------------------------------------------------
    # 核心执行流程
    # ------------------------------------------------------------

    async def _execute(
        self, conversation_id: str, message: str, bus: TurnEventBus, user: dict | None = None
    ) -> MessageResponse:
        turn_id = uuid.uuid4().hex[:12]

        # v5.0 隔离键翻译：外部会话 ID → u:{uid}:{cid}（下游 Agent 零改动）
        sid = scoped_conversation(user, conversation_id)
        profile_key = scoped_profile(user)

        # 1. 记录用户消息（工作记忆，用户隔离空间）
        memory.working.add_message(sid, "user", message)

        # 2. 装配轮次上下文（画像按用户；教学经验库按主题检索）
        bus.phase("routing", "调度中")
        ctx = context_assembler.assemble(sid, message, profile_key=profile_key)

        # 3. 构建图状态并执行（Supervisor 多 Agent 协作）
        state = empty_state()
        state.update({
            "conversation_id": sid,              # 下游 Agent 全部经由该键访问隔离空间
            "profile_key": profile_key,          # 画像专用键（tutor/诊断/巩固）
            "user_message": message,
            "turn_id": turn_id,
            "context": ctx.to_dict(),
        })

        config = {
            "configurable": {"bus": bus, "thread_id": sid},
            "recursion_limit": max(12, settings.max_supervisor_hops * 3),
        }
        result = await self.graph.ainvoke(state, config=config)

        # 4. 组装最终回复（Harness 收口）
        return await self._finalize(
            conversation_id, sid, profile_key, turn_id, result, bus, user
        )

    async def _finalize(
        self,
        conversation_id: str,
        sid: str,
        profile_key: str,
        turn_id: str,
        result: dict,
        bus: TurnEventBus,
        user: dict | None = None,
    ) -> MessageResponse:
        bus.phase("guiding", "组装回复")

        try:
            guidance = GuidanceAction.model_validate(result["guidance"])
            classification = ClassificationResult.model_validate(result["classification"])
            cognitive_state = CognitiveState.model_validate(result["cognitive_state"])
            perception = PerceptionResult.model_validate(result.get("perception") or {})
        except (KeyError, Exception) as e:
            logger.error(f"最终状态缺失或非法: {e}")
            return self._fallback_response(conversation_id, bus, user)

        is_step_complete = bool(result.get("is_step_complete"))
        plan_any = memory.working.peek_step_plan(sid)
        # 展示用计划：未完成时显示进度行；刚完成时保留（庆祝模式需要计划数据）
        display_plan = plan_any

        # 补充步骤进度信息到引导动作
        if display_plan:
            guidance.step_progress = display_plan.progress_text
            if display_plan.current_step:
                guidance.current_step_title = display_plan.current_step.title

        # 组装回复（v4.3 回复卫生：衔接语 + 正文；进度行由前端 chip 渲染，
        # 计划示例不再追加——修复轮2+ 重复输出轮1示例的问题）
        reply = compose_reply(
            guidance, classification.question_type, display_plan, is_step_complete
        )

        # 记录助手消息（工作记忆，用户隔离空间）
        memory.working.add_message(
            sid,
            "assistant",
            reply,
            question_type=classification.question_type,
            guidance_mode=guidance.mode.value,
            cognitive_level=cognitive_state.level,
            step_progress=guidance.step_progress,
        )

        # 记录当前引导到会话状态
        memory.working.update(sid, current_guidance=guidance)

        # 批量持久化脏数据（移植优化）
        memory.flush()

        # 5. 触发反思后台子图（反思→巩固→进化→步骤计划懒加载）
        snapshot = {
            "conversation_id": sid,
            "profile_key": profile_key,
            "user": {"id": (user or {}).get("id", "guest")},
            "user_message": result.get("user_message", ""),
            "perception": perception,
            "classification": classification,
            "cognitive_state": cognitive_state,
            "guidance": guidance,
            "history_text": (result.get("context") or {}).get("history_text", ""),
            "profile_section": (result.get("context") or {}).get("profile_section", ""),
        }
        await self._trigger_reflection(snapshot)

        step_plan_summary = memory.working.get_step_plan_summary(sid)
        bus.phase("done", "完成")

        return MessageResponse(
            reply=reply,
            conversation_id=conversation_id,   # 对外始终回传原始（未隔离）ID
            turn_id=turn_id,
            question_type=classification.question_type,
            cognitive_level=cognitive_state.level,
            guidance_mode=guidance.mode,
            phase=AgentPhase.DONE,
            example=guidance.example,
            encouragement=guidance.encouragement,
            classification_reasoning=classification.reasoning,
            detected_emotions=classification.detected_emotions,
            is_continuation=classification.is_continuation,
            profile_summary=memory.profile.get_summary_text(profile_key),
            step_plan=step_plan_summary,
            step_progress=guidance.step_progress,
            current_step_title=guidance.current_step_title,
            trace=AgentTrace(**bus.agent_trace()),
        )

    # ------------------------------------------------------------
    # 反思触发（后台 / 同步可配置）
    # ------------------------------------------------------------

    async def _trigger_reflection(self, snapshot: dict):
        if settings.reflection_in_background:
            asyncio.create_task(run_reflection(snapshot))
            logger.info("[Harness] 反思子图已提交后台执行")
        else:
            # 同步模式：等待反思完成后返回（测试/调试用）
            await run_reflection(snapshot)

    # ------------------------------------------------------------
    # 异常/未配置响应
    # ------------------------------------------------------------

    def _llm_available(self) -> bool:
        from app.llm.gateway import get_llm_gateway
        try:
            return get_llm_gateway().available
        except Exception:
            return False

    def _unconfigured_response(
        self, conversation_id: str, message: str, bus: TurnEventBus, user: dict | None = None
    ) -> MessageResponse:
        sid = scoped_conversation(user, conversation_id)
        # 记录用户消息（历史完整性：用户确实说了这句话）
        memory.working.add_message(sid, "user", message)
        memory.working.add_message(sid, "assistant", _UNCONFIGURED_REPLY)
        memory.flush()
        return MessageResponse(
            reply=_UNCONFIGURED_REPLY,
            conversation_id=conversation_id,
            phase=AgentPhase.IDLE,
        )

    def _fallback_response(
        self, conversation_id: str, bus: TurnEventBus, user: dict | None = None
    ) -> MessageResponse:
        sid = scoped_conversation(user, conversation_id)
        memory.working.add_message(sid, "assistant", _FALLBACK_REPLY)
        memory.flush()
        bus.phase("done", "完成")
        return MessageResponse(
            reply=_FALLBACK_REPLY,
            conversation_id=conversation_id,
            phase=AgentPhase.IDLE,
            trace=AgentTrace(**bus.agent_trace()),
        )

    def _error_payload(self, error: str) -> dict:
        return MessageResponse(
            reply=f"输入处理失败：{error}",
            conversation_id="",
            phase=AgentPhase.IDLE,
        ).model_dump(mode="json")


# 全局 Runner 单例
runner = TutorRunner()
