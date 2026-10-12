"""
双通道 LLM 层 - 后备通道（httpx 直连，异步连接池）

当主通道（langchain-openai）不可用/调用失败时自动切换。
保留原项目 httpx 连接池优化的优点，升级为全异步实现。
"""

import json
import re
import logging
from typing import Any, AsyncIterator

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def safe_parse_json(text: str) -> dict[str, Any] | None:
    """安全解析 JSON，兼容 markdown 代码块包裹（移植自原项目）"""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试从文本中提取 JSON 对象
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


class FallbackChannel:
    """基于 httpx.AsyncClient 的后备通道（OpenAI 兼容协议直连）"""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=settings.llm_timeout,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=60,
                ),
            )
        return self._client

    async def aclose(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        logger.info("[LLM后备通道] httpx 连接已关闭")

    def _payload(self, system_prompt: str, user_prompt: str, stream: bool = False) -> dict:
        return {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "stream": stream,
        }

    @staticmethod
    def _headers() -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
        }

    async def ainvoke_text(self, system_prompt: str, user_prompt: str) -> str:
        client = self._get_client()
        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        try:
            resp = await client.post(
                url, headers=self._headers(), json=self._payload(system_prompt, user_prompt)
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as e:
            logger.error(f"[LLM后备通道] HTTP {e.response.status_code}: {e.response.text[:200]}")
            raise RuntimeError(f"LLM API 错误 (HTTP {e.response.status_code})")
        except httpx.TimeoutException:
            raise RuntimeError("LLM 调用超时，请稍后重试")
        except KeyError as e:
            raise RuntimeError(f"LLM 返回格式异常: 缺少字段 {e}")

    async def ainvoke_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """强制 JSON 输出：第一次失败会追加提示重试一次（移植原项目策略）"""
        content = await self.ainvoke_text(system_prompt, user_prompt)
        parsed = safe_parse_json(content)
        if parsed is not None:
            return parsed

        retry_user = (
            f"{user_prompt}\n\n"
            "请确保你的输出是纯 JSON 格式，不要包含任何其他文字或 markdown 代码块标记。"
            "直接输出 JSON 对象。"
        )
        content = await self.ainvoke_text(system_prompt, retry_user)
        parsed = safe_parse_json(content)
        if parsed is not None:
            return parsed
        raise ValueError("LLM 未能返回有效的 JSON 格式输出")

    async def astream_text(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        """SSE 流式调用（OpenAI stream 协议）"""
        client = self._get_client()
        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        try:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(),
                json=self._payload(system_prompt, user_prompt, stream=True),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0]["delta"].get("content")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except httpx.HTTPStatusError as e:
            logger.error(f"[LLM后备通道流式] HTTP {e.response.status_code}")
            raise RuntimeError(f"LLM API 错误 (HTTP {e.response.status_code})")
        except httpx.TimeoutException:
            raise RuntimeError("LLM 流式调用超时")
