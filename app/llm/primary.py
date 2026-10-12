"""
双通道 LLM 层 - 主通道（langchain-openai ChatOpenAI）

与 LangGraph 生态无缝集成，支持任意 OpenAI 兼容厂商：
豆包 / DeepSeek / 通义千问 / OpenAI / Kimi 等。

仅当 LLM_API_KEY 已配置时才初始化（懒加载）。
"""

import logging
from typing import AsyncIterator

from app.config import settings

logger = logging.getLogger(__name__)


class PrimaryChannel:
    """基于 langchain-openai 的主通道"""

    def __init__(self):
        self._llm = None
        self._stream_llm = None

    def _ensure_init(self):
        if self._llm is not None:
            return
        from langchain_openai import ChatOpenAI

        common = dict(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
            max_retries=1,
        )
        self._llm = ChatOpenAI(temperature=settings.llm_temperature, **common)
        # 流式通道：稍高温度会让教学语言更自然，这里保持一致
        self._stream_llm = ChatOpenAI(
            temperature=settings.llm_temperature, streaming=True, **common
        )
        logger.info(
            f"[LLM主通道] ChatOpenAI 初始化: model={settings.llm_model}, "
            f"base_url={settings.llm_base_url}"
        )

    async def ainvoke_text(self, system_prompt: str, user_prompt: str) -> str:
        """单次文本调用"""
        self._ensure_init()
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await self._llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        return resp.content or ""

    async def astream_text(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        """流式文本调用（逐 token）"""
        self._ensure_init()
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        async for chunk in self._stream_llm.astream(messages):
            if chunk.content:
                yield chunk.content

    async def aclose(self):
        self._llm = None
        self._stream_llm = None
