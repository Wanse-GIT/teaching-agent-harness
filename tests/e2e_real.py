#!/usr/bin/env python3
"""
真实 LLM 端到端测试（需要 .env 中配置有效的 LLM_API_KEY）

场景（对应原项目 test_step_tracker.py 的教学闭环）：
  轮1: 教学性问题 → 感知分类(eager/teaching) → 引导 → 后台生成步骤计划
  轮2: 延续回答 → 步骤评估推进
  轮3: 继续推进 / 查看画像与策略进化
  轮4: 事实性问题 → 直接回答 + 知识拓展（轻量快速路径）

用法：
  python3 tests/e2e_real.py [--base-url http://localhost:8000]
  或直接调用 harness（免启动服务）：python3 tests/e2e_real.py --inline
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def banner(title=""):
    print("\n" + "=" * 66)
    if title:
        print(f"  {title}")
        print("=" * 66)


def print_step_plan(plan):
    if not plan:
        print("  （无步骤计划）")
        return
    print(f"  📋 主题: {plan.get('topic')}  进度: {plan.get('progress')}"
          f"  完成: {plan.get('is_complete')}")
    for s in plan.get("steps", []):
        icon = {"completed": "✅", "in_progress": "🔄", "pending": "⏸",
                "stuck": "⚠️", "skipped": "⏭"}.get(s.get("status"), "·")
        print(f"    {icon} Step {s['index']}: {s['title']} (尝试{s['attempts']}次)")


# ============================================================
# inline 模式：直接调用 harness（不启动 HTTP 服务）
# ============================================================

async def run_inline():
    banner("真实 LLM e2e（inline 模式，直接调用 Harness）")
    from app.memory import memory
    memory.init()
    from app.harness.runner import runner
    runner.init()

    sid = f"e2e-{int(time.time() * 1000)}"
    results = []

    turns = [
        ("我想学习Python的range函数，能从零教我吗？", "teaching"),
        ("好的，我试了 for i in range(3) 输出了 0 1 2，我懂了", None),  # 延续
        ("那 range(2, 10, 2) 是什么意思？我试了下输出 2 4 6 8", None),
    ]

    for i, (msg, expect_type) in enumerate(turns, 1):
        banner(f"轮{i}: {msg[:30]}")
        t0 = time.time()
        r = await runner.run(sid, msg)
        dt = time.time() - t0
        results.append(r)
        print(f"  ⏱ 耗时 {dt:.1f}s | LLM通道: 事件轨迹={len(r.trace.agents) if r.trace else 0}个Agent")
        print(f"  📊 分类: {r.question_type.value if r.question_type else '?'}"
              f" | 模式: {r.guidance_mode.value if r.guidance_mode else '?'}"
              f" | 延续: {r.is_continuation}")
        print(f"  💬 回复: {r.reply[:150]}...")
        if r.encouragement:
            print(f"  💜 鼓励: {r.encouragement[:60]}")
        if r.step_plan:
            print_step_plan(r.step_plan)
        if expect_type:
            assert r.question_type and r.question_type.value == expect_type, \
                f"期望 {expect_type}，得到 {r.question_type}"

    profile = memory.profile.get_or_create(sid)
    banner("画像进化结果")
    print(f"  已掌握: {profile.known_topics}")
    print(f"  薄弱: {profile.weak_topics}")
    print(f"  兴趣: {profile.interest_topics}")
    print(f"  交互轮次: {profile.total_sessions}, 认知轨迹: {profile.cognitive_level_trajectory[-5:]}")
    stats = memory.strategy.export_stats()
    for k, v in stats.items():
        print(f"  策略[{k}]: 使用{v.total_uses}次, 平均效果{v.avg_effectiveness:.2f}, "
              f"曲线={([round(x,2) for x in v.effectiveness_history])}")

    # 事实性验证
    banner("事实性问题（轻量快速路径）")
    t0 = time.time()
    r4 = await runner.run(sid, "顺便问下，Python是哪年发布的？")
    dt = time.time() - t0
    print(f"  ⏱ 耗时 {dt:.1f}s | 分类: {r4.question_type.value} | 模式: {r4.guidance_mode.value}")
    print(f"  💬 {r4.reply[:120]}...")
    assert r4.question_type.value == "factual"

    banner("✅ inline e2e 全部通过")


# ============================================================
# server 模式：通过 HTTP API 测试（需先启动服务）
# ============================================================

def run_server(base_url: str):
    import httpx

    banner(f"真实 LLM e2e（server 模式: {base_url}）")
    client = httpx.Client(base_url=base_url, timeout=180)
    cid = f"e2e-{int(time.time() * 1000)}"

    # 健康检查
    r = client.get("/api/v2/health")
    print(f"  健康检查: {r.json()['status']} | db={r.json()['db_persistent']} "
          f"| llm={r.json()['llm_channel']}")

    # 教学性问题（SSE 流式）
    banner("轮1: 教学性问题（SSE 流式观察 Agent 协作）")
    events_seen = set()
    final = None
    with client.stream(
        "POST", f"/api/v2/conversations/{cid}/messages/stream",
        json={"content": "我想学习Python的range函数，能从零教我吗？"},
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev_type = line[6:].strip()
                events_seen.add(ev_type)
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if ev_type == "route":
                    print(f"  [调度] → {data.get('to')} ({data.get('source')}): {data.get('reason')}")
                elif ev_type == "agent_exit":
                    print(f"  [完成] {data.get('agent')} ({data.get('ms')}ms)")
                elif ev_type == "final":
                    final = data.get("payload")

    print(f"  事件类型: {sorted(events_seen)}")
    assert final, "未收到 final 事件"
    print(f"  📊 {final['question_type']} | {final['guidance_mode']}")
    print(f"  💬 {final['reply'][:140]}...")
    assert final["question_type"] == "teaching"

    # 非流式延续
    banner("轮2: 延续回答")
    r = client.post(f"/api/v2/conversations/{cid}/messages",
                    json={"content": "我试了 for i in range(3) 输出了 0 1 2，我懂了"})
    data = r.json()
    print(f"  延续: {data['is_continuation']} | 步骤: {data['step_plan']['progress'] if data['step_plan'] else '-'}")
    if data["step_plan"]:
        print_step_plan(data["step_plan"])

    # 会话摘要
    banner("会话摘要")
    r = client.get(f"/api/v2/conversations/{cid}")
    s = r.json()
    print(f"  消息数: {s['message_count']} | 活跃问题: {s['active_problem']}")

    banner("✅ server e2e 全部通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--inline", action="store_true", help="直接调用 harness（免启动服务）")
    args = parser.parse_args()

    if args.inline:
        asyncio.run(run_inline())
    else:
        run_server(args.base_url)
