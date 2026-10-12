"""
双通道 LLM 网关（含熔断器）

统一入口：
  - text()      普通文本调用
  - json()      结构化 JSON 调用（Pydantic 校验）
  - stream()    流式文本调用
  - available   是否可用（未配置 API Key 时 Agent 层走降级策略）
  - status()    健康状态描述（含熔断器状态，供 /health 观测）

调用顺序：主通道（langchain-openai）→ 后备通道（httpx 直连）。
任何一层失败自动切换，保证可用性（双通道兜底）。

熔断器（Circuit Breaker，深度优化新增）：
  - 连续 N 次「主备双失败」后进入熔断冷却期，期间直接抛 LLMUnavailableError，
    Agent 层立即走规则降级——避免降级模式下每轮重复 6-10 次注定失败的
    网络往返（实测将降级模式一轮对话从 ~3s 压到 <100ms）
  - 401/403/404 等非瞬态错误（如额度耗尽）→ 长冷却（默认 600s）
  - 429/5xx/超时等瞬态错误 → 短冷却（默认 120s，且需连续 3 次才熔断）
  - 冷却结束后半开：放行一次探测请求，成功则恢复，失败则重新熔断
"""

import logging
import time
from typing import AsyncIterator, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.fallback import FallbackChannel, safe_parse_json

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """LLM 完全不可用（未配置或主备均失败）"""


# 非瞬态 HTTP 状态码：重试没有意义（认证失败/额度耗尽/模型不存在）
_HARD_STATUS_CODES = {401, 403, 404}


def _is_hard_error(e: BaseException) -> bool:
    """判断错误是否为非瞬态（重试无意义）"""
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status in _HARD_STATUS_CODES
    msg = str(e)
    # 后备通道把 HTTP 状态封装进消息：LLM API 错误 (HTTP 403)
    if "HTTP 401" in msg or "HTTP 403" in msg or "HTTP 404" in msg:
        return True
    return False


class _CircuitBreaker:
    """轻量熔断器（进程内，毫秒级开销）"""

    def __init__(self, failure_threshold: int, soft_cooldown: float, hard_cooldown: float):
        self.failure_threshold = max(1, failure_threshold)
        self.soft_cooldown = soft_cooldown
        self.hard_cooldown = hard_cooldown
        self.consecutive_failures = 0
        self.opened_at: float = 0.0
        self.cooldown: float = 0.0
        self.last_error: str = ""

    def blocked(self) -> bool:
        """是否处于熔断冷却期（True = 调用方应直接快速失败）"""
        if self.consecutive_failures < self.failure_threshold:
            return False
        elapsed = time.time() - self.opened_at
        if elapsed >= self.cooldown:
            return False  # 冷却结束 → 半开，放行探测
        return True

    def remaining_cooldown(self) -> float:
        return max(0.0, self.cooldown - (time.time() - self.opened_at))

    def record_success(self):
        if self.consecutive_failures > 0:
            logger.info(
                f"[LLM熔断器] 恢复正常（此前连续失败 {self.consecutive_failures} 次）"
            )
        self.consecutive_failures = 0
        self.last_error = ""

    def record_failure(self, error: BaseException):
        self.consecutive_failures += 1
        self.last_error = f"{type(error).__name__}: {str(error)[:120]}"
        hard = _is_hard_error(error)
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = time.time()
            self.cooldown = self.hard_cooldown if hard else self.soft_cooldown
            logger.warning(
                f"[LLM熔断器] 连续 {self.consecutive_failures} 次双通道失败，"
                f"熔断 {self.cooldown:.0f}s（{'非瞬态错误' if hard else '瞬态错误'}）："
                f"{self.last_error}"
            )

    def status_text(self) -> str:
        if self.consecutive_failures == 0:
            return "ok"
        if self.blocked():
            return f"degraded(熔断冷却中, 剩余{self.remaining_cooldown():.0f}s)"
        return f"half-open(连续失败{self.consecutive_failures}次)"


class LLMGateway:
    """双通道 LLM 网关（支持注入自定义通道用于测试）"""

    def __init__(self, primary=None, fallback: Optional[FallbackChannel] = None):
        self._primary = primary          # 懒加载，避免无 key 时 import 开销
        self._fallback = fallback or FallbackChannel()
        self.last_channel: str = ""      # 观测性：记录最近一次成功使用的通道
        from app.config import settings
        self._breaker = _CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            soft_cooldown=settings.llm_circuit_cooldown_transient,
            hard_cooldown=settings.llm_circuit_cooldown_permanent,
        )

    # ------------------------------------------------------------
    # 通道管理
    # ------------------------------------------------------------

    def _get_primary(self):
        if self._primary is None:
            from app.config import settings
            if not settings.llm_configured:
                raise LLMUnavailableError(
                    "未配置 LLM_API_KEY！请在 .env 文件中设置：\n"
                    "  LLM_API_KEY=你的API密钥\n"
                    "  LLM_BASE_URL=API地址\n"
                    "  LLM_MODEL=模型名称"
                )
            from app.llm.primary import PrimaryChannel
            self._primary = PrimaryChannel()
        return self._primary

    @property
    def available(self) -> bool:
        try:
            self._get_primary()
            return True
        except LLMUnavailableError:
            return False

    def status(self) -> str:
        """健康状态（供 /health 与日志观测）"""
        if not self.available:
            return "unconfigured"
        if self._breaker.blocked():
            return f"degraded:{self._breaker.status_text()}"
        return self._breaker.status_text() if self._breaker.consecutive_failures else (
            self.last_channel or "available"
        )

    def _check_breaker(self):
        """熔断期直接快速失败（Agent 层已有规则降级兜底）"""
        if self._breaker.blocked():
            raise LLMUnavailableError(
                f"LLM 通道熔断中（{self._breaker.status_text()}，"
                f"上次错误：{self._breaker.last_error}）"
            )

    async def aclose(self):
        if self._primary is not None:
            await self._primary.aclose()
            self._primary = None
        await self._fallback.aclose()
        self._breaker.record_success()  # 重置熔断状态

    # ------------------------------------------------------------
    # 文本调用（主 → 备，带熔断）
    # ------------------------------------------------------------

    async def text(self, system_prompt: str, user_prompt: str) -> str:
        self._check_breaker()
        try:
            primary = self._get_primary()
            result = await primary.ainvoke_text(system_prompt, user_prompt)
            self.last_channel = "primary"
            self._breaker.record_success()
            return result
        except LLMUnavailableError:
            raise
        except Exception as primary_error:
            logger.warning(f"[LLM网关] 主通道失败，切换后备通道: {primary_error}")
            try:
                result = await self._fallback.ainvoke_text(system_prompt, user_prompt)
                self.last_channel = "fallback"
                self._breaker.record_success()
                return result
            except Exception as fallback_error:
                self._breaker.record_failure(fallback_error)
                raise fallback_error

    # ------------------------------------------------------------
    # 结构化 JSON 调用（主 → 备，Pydantic 校验 + 一次重试）
    # ------------------------------------------------------------

    async def json_dict(self, system_prompt: str, user_prompt: str) -> dict:
        """调用 LLM 并解析为原始 dict（JSON 解析失败自动重试一次）"""
        last_err: Exception | None = None
        for attempt in range(2):
            raw = await self.text(system_prompt, user_prompt)
            parsed = safe_parse_json(raw)
            if parsed is None:
                last_err = ValueError(f"LLM 输出不是有效 JSON: {raw[:120]}")
                logger.warning(f"[LLM网关] JSON 解析失败(第{attempt + 1}次)，重试")
                continue
            return parsed
        raise last_err or ValueError("LLM 结构化调用失败")

    async def json(
        self,
        system_prompt: str,
        user_prompt: str,
        model_cls: Type[T],
    ) -> T:
        """调用 LLM 并解析为 Pydantic 模型（校验失败自动重试一次）"""
        last_err: Exception | None = None
        for attempt in range(2):
            raw = await self.text(system_prompt, user_prompt)
            parsed = safe_parse_json(raw)
            if parsed is None:
                last_err = ValueError(f"LLM 输出不是有效 JSON: {raw[:120]}")
                logger.warning(f"[LLM网关] JSON 解析失败(第{attempt + 1}次)，重试")
                continue
            try:
                return model_cls.model_validate(parsed)
            except ValidationError as e:
                last_err = e
                logger.warning(f"[LLM网关] Pydantic 校验失败(第{attempt + 1}次): {e}")
                # 校验失败时补充提示重试
                user_prompt = (
                    f"{user_prompt}\n\n注意：你上次的输出字段格式不符合要求（{e.errors()[0].get('loc')}），"
                    "请严格按照要求的JSON格式重新输出。"
                )
        raise last_err or ValueError("LLM 结构化调用失败")

    # ------------------------------------------------------------
    # 流式调用（主通道流式 → 失败则后备通道整段分片）
    # ------------------------------------------------------------

    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        self._check_breaker()
        got_any = False
        try:
            primary = self._get_primary()
            async for delta in primary.astream_text(system_prompt, user_prompt):
                got_any = True
                self.last_channel = "primary"
                yield delta
            if got_any:
                self._breaker.record_success()
                return
            # 主通道返回空内容 → 走后备
            logger.warning("[LLM网关] 主通道流式返回为空，切换后备通道")
        except LLMUnavailableError:
            raise
        except Exception as e:
            if got_any:
                # 已向用户推送过 token，不能重头再来（会造成内容重复）——
                # 优雅结束本次流，导师层会使用已收到的部分内容
                logger.warning(
                    f"[LLM网关] 主通道流式中途失败，保留已输出内容并结束本次流: {e}"
                )
                self._breaker.record_failure(e)
                return
            logger.warning(f"[LLM网关] 主通道流式失败，切换后备通道: {e}")

        # 后备通道：整段生成后按块分片（模拟流式体验）
        try:
            full = await self._fallback.ainvoke_text(system_prompt, user_prompt)
            self.last_channel = "fallback"
            self._breaker.record_success()
        except Exception as e:
            self._breaker.record_failure(e)
            raise
        for i in range(0, len(full), 24):
            yield full[i:i + 24]


# ============================================================
# 全局网关单例
# ============================================================

_gateway: LLMGateway | None = None


def get_llm_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


async def shutdown_llm_gateway():
    global _gateway
    if _gateway is not None:
        await _gateway.aclose()
        _gateway = None
