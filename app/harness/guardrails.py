"""
Harness - 输入守卫栏（Guardrails）

安全防线（移植自原项目 routes.py，从 API 层收口到 Harness 层）：
  - 空消息校验
  - 长度限制
  - Prompt 注入模式检测（基本防线）
  - 输入清洗
"""

import re
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# 常见的 Prompt 注入攻击模式（英文模式，中文误报率低）
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|above)\s+instructions",
    r"ignore\s+(all\s+)?(previous\s+)?(all\s+)?above\s+instructions",
    r"ignore\s+all\s+previous\s+instructions",
    r"forget\s+(all\s+)?(previous|above)\s+",
    r"disregard\s+(all\s+)?(previous|above)\s+",
    r"you\s+are\s+now\s+",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*",
    r"override\s+(all\s+)?(previous|safety)\s+",
]


class GuardrailResult:
    def __init__(self, ok: bool, content: str = "", error: str = ""):
        self.ok = ok
        self.content = content
        self.error = error


def check_input(message: str) -> GuardrailResult:
    """输入守卫：校验 → 注入检测 → 清洗。返回 GuardrailResult"""
    if not message or not message.strip():
        return GuardrailResult(False, error="消息不能为空")

    if len(message) > settings.max_input_length:
        return GuardrailResult(
            False,
            error=f"消息长度超过限制（最大 {settings.max_input_length} 字符）",
        )

    text_lower = message.lower()
    for pattern in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            logger.warning(f"检测到疑似 Prompt 注入攻击: {message[:100]}")
            return GuardrailResult(False, error="输入内容包含不安全的指令，请修改后重试。")

    # 清洗：去除首尾空白
    return GuardrailResult(True, content=message.strip())
