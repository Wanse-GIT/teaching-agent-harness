#!/usr/bin/env python3
"""
v4.1 深度优化专项验证（熔断器 / 步骤感知降级 / LLM路由约束 / detected_emotions）

运行：python3 tests/test_v41_features.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LLM_API_KEY", "test-mock-key")
os.environ.setdefault("REFLECTION_IN_BACKGROUND", "false")
os.environ.setdefault("DEBUG", "false")

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 1. 熔断器：连续双失败 → 熔断快速失败 → 半开恢复
# ============================================================

async def test_circuit_breaker():
    from app.llm.gateway import LLMGateway, LLMUnavailableError

    class AlwaysFail:
        async def ainvoke_text(self, s, u):
            raise RuntimeError("LLM API 错误 (HTTP 403)")

        async def aclose(self):
            pass

    gw = LLMGateway(primary=AlwaysFail(), fallback=AlwaysFail())

    # 3 次双失败（403 非瞬态）
    for i in range(3):
        try:
            await gw.text("s", "u")
            raise AssertionError("应当失败")
        except RuntimeError:
            pass

    # 第 4 次：应熔断快速失败（不再有网络调用，立即抛 LLMUnavailableError）
    t0 = time.time()
    try:
        await gw.text("s", "u")
        raise AssertionError("熔断期应快速失败")
    except LLMUnavailableError as e:
        elapsed = time.time() - t0
        assert elapsed < 0.05, f"熔断快速失败应 <50ms，实际 {elapsed*1000:.0f}ms"

    # status 应显示 degraded
    status = gw.status()
    assert "degraded" in status, f"status 应为 degraded: {status}"
    print(f"✅ 1. 熔断器：3次双失败→熔断(快速失败 {elapsed*1000:.1f}ms)→status={status[:40]}")

    # 半开恢复：冷却置零后放行探测，成功后恢复
    gw._breaker.opened_at = time.time() - 99999  # 模拟冷却已过
    class NowOK:
        async def ainvoke_text(self, s, u):
            return "ok"

        async def aclose(self):
            pass
    gw._primary = NowOK()
    gw._fallback = NowOK()
    result = await gw.text("s", "u")
    assert result == "ok"
    assert not gw._breaker.blocked()
    print("✅ 1b. 半开探测成功后熔断恢复")


# ============================================================
# 2. 熔断状态下的全链路：教学闭环依然完整走规则降级
# ============================================================

async def test_degraded_full_flow():
    from app.llm.gateway import LLMGateway
    import app.llm.gateway as gw_module

    class AlwaysFail:
        call_count = 0

        async def ainvoke_text(self, s, u):
            AlwaysFail.call_count += 1
            raise RuntimeError("LLM API 错误 (HTTP 403)")

        async def astream_text(self, s, u):
            raise RuntimeError("LLM API 错误 (HTTP 403)")
            yield  # pragma: no cover

        async def aclose(self):
            pass

    gw_module._gateway = LLMGateway(primary=AlwaysFail(), fallback=AlwaysFail())

    from app.harness.runner import runner
    from app.memory import memory
    runner.init()
    sid = "breaker-flow-1"
    memory.working.clear(sid)

    # 第 1 轮：触发熔断（期间若干双失败）
    t0 = time.time()
    r1 = await runner.run(sid, "我想学习Python的装饰器，从零开始教我")
    t1 = time.time() - t0
    assert r1.question_type.value == "teaching", "规则分类应兜底为 teaching"
    assert r1.step_plan and not r1.step_plan["is_complete"], "降级步骤计划应生成"

    # 第 2 轮：熔断生效，应快速完成（<200ms，无网络调用）
    t0 = time.time()
    calls_before = AlwaysFail.call_count
    r2 = await runner.run(sid, "我试了一下，写出来了，懂了")
    t2 = time.time() - t0
    assert r2.is_continuation, "应判定延续"
    assert r2.step_plan["current_step_index"] == 1, f"步骤应推进: {r2.step_plan['current_step_index']}"
    calls_during = AlwaysFail.call_count - calls_before
    assert calls_during == 0, f"熔断期不应有网络调用，实际 {calls_during} 次"
    assert t2 < 0.2, f"熔断期一轮应 <200ms，实际 {t2*1000:.0f}ms"

    # 第 3 轮：降级引导应感知步骤上下文（含当前步骤标题）
    r3 = await runner.run(sid, "做好了，明白了")
    from app.harness.scoping import scoped_conversation
    plan = memory.working.peek_step_plan(scoped_conversation(None, sid))  # v5.0 隔离键
    assert plan and plan.current_step, "应有活跃计划"
    assert plan.current_step.title in r3.reply, (
        f"降级回复应包含当前步骤「{plan.current_step.title}」: {r3.reply[:80]}"
    )
    print(f"✅ 2. 熔断全链路：轮1={t1*1000:.0f}ms(触发熔断), 轮2={t2*1000:.0f}ms(0次网络调用), "
          f"轮3降级回复感知步骤「{plan.current_step.title}」")


# ============================================================
# 3. LLM 路由教学约束：LLM 给出跳步路由时被强制纠正
# ============================================================

async def test_llm_route_constraints():
    from app.agents.supervisor import _apply_teaching_constraints
    from app.models.domain import RouteDecision
    from app.harness.context import TurnContext

    ctx = TurnContext(conversation_id="x", user_message="hi", has_active_plan=True)

    # guidance 已产出 → LLM 建议继续路由应被纠正为 finish
    d = _apply_teaching_constraints(
        {"guidance": {"mode": "direct_answer"}},
        ctx,
        RouteDecision(target="tutor", reason="llm", source="llm"),
    )
    assert d and d.target == "finish", "guidance已产出必须finish"

    # 未诊断 → LLM 建议 tutor 应被纠正为 diagnostician
    d = _apply_teaching_constraints(
        {}, ctx, RouteDecision(target="tutor", reason="llm", source="llm")
    )
    assert d and d.target == "diagnostician", "未诊断必须先诊断"

    # 活跃计划 + 未评估 + teaching → LLM 建议 tutor 应被纠正为 step_assessor
    d = _apply_teaching_constraints(
        {"classification": {"question_type": "teaching", "learning_desire_level": "eager"}},
        ctx,
        RouteDecision(target="tutor", reason="llm", source="llm"),
    )
    assert d and d.target == "step_assessor", "活跃计划必须先评估步骤"

    # 合法决策（factual → tutor）放行
    d = _apply_teaching_constraints(
        {"classification": {"question_type": "factual", "learning_desire_level": "indifferent"}},
        ctx,
        RouteDecision(target="tutor", reason="llm", source="llm"),
    )
    assert d is None, "factual 走 tutor 应放行"
    print("✅ 3. LLM 路由教学约束：跳步路由被强制纠正，合法路由放行")


# ============================================================
# 4. detected_emotions 透出 API
# ============================================================

async def test_detected_emotions():
    from app.llm.gateway import LLMGateway
    import app.llm.gateway as gw_module
    from app.harness.runner import runner
    from app.memory import memory

    # 规则模式：eager → detected_emotions=['好奇','渴望']
    class AlwaysFail:
        async def ainvoke_text(self, s, u):
            raise RuntimeError("LLM API 错误 (HTTP 403)")

        async def astream_text(self, s, u):
            raise RuntimeError("LLM API 错误 (HTTP 403)")
            yield  # pragma: no cover

        async def aclose(self):
            pass

    gw_module._gateway = LLMGateway(primary=AlwaysFail(), fallback=AlwaysFail())
    sid = "emotions-test-1"
    memory.working.clear(sid)
    r = await runner.run(sid, "我想学习Python的生成器，教我原理")
    assert r.detected_emotions, "detected_emotions 不应为空"
    assert any("好奇" in e or "渴望" in e for e in r.detected_emotions), r.detected_emotions
    print(f"✅ 4. detected_emotions 透出 API: {r.detected_emotions}")


async def main():
    print("=" * 66)
    print("  v4.1 深度优化专项验证")
    print("=" * 66)
    await test_circuit_breaker()
    await test_degraded_full_flow()
    await test_llm_route_constraints()
    await test_detected_emotions()
    print("=" * 66)
    print("  🎉 v4.1 全部专项验证通过！")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
