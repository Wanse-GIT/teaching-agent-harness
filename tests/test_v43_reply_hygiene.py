#!/usr/bin/env python3
"""
v4.3 回复卫生专项验证（修复：轮2+ 重复输出轮1示例 / 重复进度行）

背景（用户实测反馈）：
  A. 教学性问题从第二轮开始，每轮都会在鼓励语前重复输出第一轮生成的示例
     （根因：compose_reply 每轮无条件追加 plan.example）
  B. 偶发重复输出两行几乎相同的进度行：
     📌 4/5：在循环中使用range的变体      ← compose_reply 系统行
     📌 4/5：在循环中使用 range 的变体    ← LLM 从历史学样自输出
     （根因：系统进度行写入历史 → LLM 模仿输出 → 双重拼接；前端 step-chip 是第三处）

v4.3 修复策略（展示职责单一化）：
  1. compose_reply 不再拼接系统「📌 x/y」进度行 → 前端 step-chip 唯一渲染，
     API 保留 step_progress / current_step_title / step_plan 结构化字段
  2. compose_reply 不再追加「示例：{plan.example}」→ 示例由导师正文承载（轮1已展示）
  3. strip_llm_progress_lines 剥离 LLM 自行模仿的进度行（📌 与【步骤进度】变体）
  4. 导师 Prompt 增加硬性禁令（不输出进度标记 / 不重复已展示示例）

运行：python3 tests/test_v43_reply_hygiene.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LLM_API_KEY", "test-mock-key")
os.environ.setdefault("REFLECTION_IN_BACKGROUND", "false")
os.environ.setdefault("DEBUG", "false")

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 1. strip_llm_progress_lines：进度行剥离（各变体）
# ============================================================

def test_strip_progress_lines():
    from app.harness.policies import strip_llm_progress_lines

    # 用户实测样例：LLM 带空格变体（全角冒号）
    assert strip_llm_progress_lines(
        "📌 4/5：在循环中使用 range 的变体\n\n很好，试试改步长。"
    ) == "很好，试试改步长。"

    # 半角冒号 / 无冒号变体
    assert strip_llm_progress_lines("📌 4/5:模仿复现\n正文") == "正文"
    assert strip_llm_progress_lines("📌 4/5 模仿复现\n正文") == "正文"

    # 【步骤进度 x/y】变体
    assert strip_llm_progress_lines("【步骤进度 4/5】修改变式\n正文") == "正文"

    # 连续多行进度标记
    assert strip_llm_progress_lines("📌 2/3：模仿复现\n【步骤进度 2/3】\n正文") == "正文"

    # 正文中间出现的进度行也要剥掉，其余保留
    out = strip_llm_progress_lines("第一行\n📌 2/3：模仿复现\n第三行")
    assert out == "第一行\n第三行"

    # 不误伤：📌 开头但非 x/y 进度格式的正文
    keep = "📌 重点：注意 range 左闭右开\n正文"
    assert strip_llm_progress_lines(keep) == keep

    # 不误伤：普通比例行 / 代码块
    keep2 = "2/3 的初学者会在这里出错\n```python\nfor i in range(3):\n    print(i)\n```"
    assert strip_llm_progress_lines(keep2) == keep2

    # 保守兜底：内容只有进度行时不返回空（保证回复非空）
    assert strip_llm_progress_lines("📌 2/3：模仿复现") == "📌 2/3：模仿复现"

    print("✅ 1. 进度行剥离：📌/【步骤进度】各变体命中，正文零误伤")


# ============================================================
# 2. compose_reply：单一展示职责（无系统进度行 / 无示例追加）
# ============================================================

def test_compose_reply_hygiene():
    from app.harness.policies import compose_reply
    from app.models.domain import (
        GuidanceAction, GuidanceMode, QuestionType, StepItem, StepPlan,
    )

    plan = StepPlan(
        plan_id="p1", topic="range函数",
        question_type=QuestionType.TEACHING,
        guidance_mode=GuidanceMode.EXAMPLE_REPRODUCTION,
        example="for i in range(3):\n    print(i)  # 输出 0,1,2",
        steps=[
            StepItem(index=1, title="认识range的基本用法", description="d1", hint="h1"),
            StepItem(index=2, title="模仿复现", description="d2", hint="h2"),
            StepItem(index=3, title="修改变式", description="d3", hint="h3"),
        ],
        current_step_index=1,  # Step 2 进行中
    )

    guidance = GuidanceAction(
        mode=GuidanceMode.EXAMPLE_REPRODUCTION,
        content="📌 2/3：模仿复现\n\n很好，你已经写出了第一个 range 循环。",  # 模拟 LLM 学样输出
        example="for i in range(3):\n    print(i)  # 输出 0,1,2",
        encouragement="你的循环结构写得正确。",
        is_continuation=True,
        continuation_context="range函数",
    )

    reply = compose_reply(guidance, QuestionType.TEACHING, plan, is_step_complete=False)

    # 修复点 1+3：回复中不应出现任何进度行（系统行已移除 + LLM 模仿行已剥离）
    assert "📌" not in reply, f"回复不应含进度行: {reply[:60]}"
    assert "【步骤进度" not in reply

    # 修复点 2：不应追加计划里存的轮1示例
    assert "示例：" not in reply, f"回复不应追加示例: {reply[-80:]}"

    # 正文与衔接语保留
    assert "很好，你已经写出了第一个 range 循环。" in reply
    assert reply.startswith("继续「range函数」——"), f"衔接语应保留: {reply[:30]}"

    print("✅ 2. compose_reply：无进度行 / 无示例追加 / 衔接语与正文完整")


# ============================================================
# 3. 全链路（MockLLM）：轮2/轮3 模仿输出也要被清理
# ============================================================

PLAN_JSON = """{
  "topic": "Python的range函数",
  "example": "for i in range(3):\\n    print(i)  # 输出 0,1,2",
  "steps": [
    {"index": 1, "title": "认识range的基本用法", "description": "观察range(3)生成0到2", "hint": "左闭右开", "expectedConcepts": ["range"]},
    {"index": 2, "title": "模仿复现", "description": "自己写一个range循环打印0到4", "hint": "range(5)", "expectedConcepts": ["循环"]},
    {"index": 3, "title": "修改变式", "description": "用range(2,10,2)打印偶数", "hint": "第三个参数是步长", "expectedConcepts": ["步长"]}
  ]
}"""

GUIDE_R1 = (
    "来看一个最简单的例子：\n\n```python\nfor i in range(3):\n    print(i)\n```\n\n"
    "输出 0 1 2。请你照着写一个打印 0 到 4 的循环。"
)
# 轮2：模拟 LLM 从历史学样，在正文开头自行输出进度行（v4.2 实测 BUG 场景）
GUIDE_R2_MIMIC = (
    "📌 2/3：模仿复现\n\n"
    "很好，你已经写出了第一个 range 循环。下一步把 range(5) 改成 range(2, 10, 2)，"
    "猜猜输出会是什么？"
)
# 轮3：换一种进度标记变体（【步骤进度】样式）
GUIDE_R3_MIMIC = (
    "【步骤进度 3/3】修改变式\n\n"
    "步长为 2 就会跳着取值。用 range 和循环求 1 到 100 的和，完成后告诉我。"
)


class MockLLM:
    """脚本化 LLM：teaching 全链路 + 轮2/轮3 故意注入进度行模仿"""

    async def ainvoke_text(self, system_prompt: str, user_prompt: str) -> str:
        sp = system_prompt

        if "调度官" in sp:
            if "本轮导师是否已产出引导: 是" in sp:
                return '{"next": "finish", "reason": "引导已产出"}'
            if "已诊断的问题类型" in sp:
                if "是否有活跃的步骤计划: 是" in sp and "本轮是否已有步骤评估结果: 否" in sp:
                    return '{"next": "step_assessor", "reason": "先评估步骤"}'
                return '{"next": "tutor", "reason": "诊断就绪交给导师"}'
            return '{"next": "diagnostician", "reason": "先诊断"}'

        if "情绪感知专家" in sp:
            return (
                '{"intent": "系统学习", "subject": "Python", "topic": "Python的range函数",'
                '"prerequisites": ["循环"], "difficultyIndicators": [], "keywords": ["range"],'
                '"emotionalTone": "渴望", "learningDesire": 0.9, "isContinuation": false,'
                '"continuationTopic": null, "learningDesireLevel": "eager",'
                '"learningDesireScore": 0.9, "detectedEmotions": ["好奇", "渴望"],'
                '"questionType": "teaching", "confidence": 0.9,'
                '"reasoning": "主动从零学习", "subCategories": [], "continuationReason": null}'
            )

        if "认知状态评估" in sp:
            return (
                '{"level": "apply", "confidenceScore": 0.6, "knowledgeGaps": ["range步长"],'
                '"learningStyle": "visual", "engagementLevel": 0.85, "frustrationLevel": 0.1}'
            )

        if "步骤评估专家" in sp:
            return (
                '{"stepCompleted": true, "studentUnderstood": 0.8, "shouldAdvance": true,'
                '"stayReason": null, "hintToProvide": null, "conceptMastered": "range基本用法"}'
            )

        if "教学设计专家" in sp:
            return PLAN_JSON

        if "反思专家" in sp:
            return (
                '{"strategyEffectiveness": 0.8, "studentProgress": 0.7,'
                '"adjustments": ["保持当前节奏"], "insights": ["动手实践反应好"],'
                '"profileUpdates": {"addKnownTopics": ["range函数"],'
                '"addEffectiveStrategies": ["example_reproduction"]}}'
            )

        if "步骤计划已全部完成" in sp:
            return "🎉 恭喜你完成全部步骤。\n\n📚 知识拓展：range 是惰性序列。"

        if "耐心的教学引导者" in sp:
            # 注意：Prompt 中「不要输出进度标记」的指令文本本身就含「【步骤进度」
            # 字样，不能用它判断是否存在活跃计划；【当前 Step N 只在有活跃
            # 计划时由 build_step_plan_prompt_section 注入，以它为准。
            if "【当前 Step 3" in sp:
                return GUIDE_R3_MIMIC
            if "【当前 Step" in sp:
                return GUIDE_R2_MIMIC
            return GUIDE_R1

        raise ValueError(f"MockLLM 未识别的 prompt: {sp[:80]}")

    async def astream_text(self, system_prompt: str, user_prompt: str):
        text = await self.ainvoke_text(system_prompt, user_prompt)
        for i in range(0, len(text), 8):
            yield text[i:i + 8]


async def _run_turn(conv_id: str, message: str):
    from app.llm.gateway import LLMGateway
    import app.llm.gateway as gw_module
    mock = MockLLM()
    gw_module._gateway = LLMGateway(primary=mock, fallback=mock)
    from app.harness.runner import runner
    return await runner.run(conv_id, message)


async def test_full_flow_no_duplication():
    from app.memory import memory

    sid = "v43-hygiene-1"
    memory.working.clear(sid)

    # --- 轮1：首次教学（无计划 → 无示例追加路径，正文自带示例） ---
    r1 = await _run_turn(sid, "我想学Python的range函数，从零开始")
    assert r1.question_type.value == "teaching"
    assert r1.step_plan and r1.step_plan["total_steps"] == 3
    assert "📌" not in r1.reply, f"轮1回复不应含系统进度行: {r1.reply[:60]}"
    assert "示例：for i in range(3)" not in r1.reply
    print(f"   轮1: 计划={r1.step_plan['progress']}, 回复首行=「{r1.reply.splitlines()[0][:30]}」")

    # --- 轮2：LLM 学样输出「📌 2/3」→ 必须被剥离，且不追加轮1示例 ---
    r2 = await _run_turn(sid, "我试了一下，写出来了，输出0到4，懂了")
    assert r2.step_plan["current_step_index"] == 1, "步骤应推进到第2步"
    assert "📌" not in r2.reply, (
        f"轮2 LLM 模仿的进度行应被剥离: {r2.reply[:80]}"
    )
    assert "很好，你已经写出了第一个 range 循环。" in r2.reply, "剥离进度行后正文应保留"
    # v4.2 BUG 回归断言：计划示例（轮1生成）不得再被追加到正文末尾
    assert "示例：for i in range(3)" not in r2.reply, (
        f"轮2不应追加轮1示例: {r2.reply[-80:]}"
    )
    # 结构化字段保持完整（前端 step-chip 唯一渲染来源）
    assert r2.step_progress == "2/3", f"step_progress 应为 2/3: {r2.step_progress}"
    assert r2.current_step_title == "模仿复现"
    assert r2.example and "for i in range(3)" in r2.example, "example 字段仍应透出"
    print(f"   轮2: step_progress={r2.step_progress}（chip 数据源完整），正文无进度行/无追加示例")

    # --- 轮3：【步骤进度】变体同样要剥离 ---
    r3 = await _run_turn(sid, "完成了，range(2,10,2)输出2,4,6,8，原来步长是这样")
    assert r3.step_plan["current_step_index"] == 2
    assert "【步骤进度" not in r3.reply, f"轮3 变体进度标记应被剥离: {r3.reply[:80]}"
    assert "📌" not in r3.reply
    assert "求 1 到 100 的和" in r3.reply, "正文主体应保留"
    print("   轮3: 【步骤进度】变体已剥离，正文完整")

    # --- 历史卫生：写入记忆的回复不再携带进度行（切断 LLM 学样源头） ---
    history = memory.working.get_recent_history_text(sid, 10)
    assert "📌" not in history and "【步骤进度" not in history, "历史不应携带进度标记"
    print("   历史: 已无进度行残留（LLM 学样源头被切断）")

    print("✅ 3. 全链路：轮2/轮3 模仿输出被剥离，示例零重复，chip 数据源完整")


async def main():
    print("=" * 66)
    print("  v4.3 回复卫生专项验证")
    print("=" * 66)
    test_strip_progress_lines()
    test_compose_reply_hygiene()
    from app.harness.runner import runner
    runner.init()
    await test_full_flow_no_duplication()
    print("=" * 66)
    print("  🎉 v4.3 全部专项验证通过！")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
