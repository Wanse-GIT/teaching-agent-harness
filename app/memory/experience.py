"""
三层记忆框架 - 教学经验库（v5.0 新增：跨用户统一整理）

与三层记忆的关系：
  - 工作记忆 / 画像记忆：用户隔离（各自私有）
  - 策略经验记忆：全局（问题类型 × 引导模式的效果统计）
  - 教学经验库：全局 + 按知识主题聚合 —— 把散落在各个用户会话中的
    高效教学实践提炼成可复用的「教学洞见」，所有用户共享

沉淀时机（由 MemoryConsolidator.consolidate_turn 触发）：
  反思评分 strategy_effectiveness >= EXPERIENCE_MIN_EFFECTIVENESS
  且能提取到知识主题（诊断师感知 topic）时，按
  (topic × question_type × guidance_mode) 去重合并：
    - sample_count 累加、effectiveness 滑动平均
    - 洞见文本取最新的有效调整建议（教学实践会进化）
    - 贡献者计数（匿名，仅数量不留身份）

消费方式：
  - 上下文装配器注入 Tutor Prompt（top-N 按主题匹配）
  - 管理员 API 全库浏览/删除（统一整理入口）
"""

import logging
import time
from typing import Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TeachingInsight(BaseModel):
    """一条教学经验（洞见）"""
    id: str                                  # topic|question_type|guidance_mode
    topic: str
    question_type: str
    guidance_mode: str
    insight: str = ""                        # 经验文本（最新版本）
    effectiveness: float = 0.0               # 滑动平均效果
    sample_count: int = 0                    # 累计样本轮数
    contributor_count: int = 0               # 贡献用户数（匿名计数）
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def key(self) -> str:
        return make_insight_key(self.topic, self.question_type, self.guidance_mode)


def make_insight_key(topic: str, question_type: str, guidance_mode: str) -> str:
    """经验去重键：主题|问题类型|引导模式（均归一化）"""
    return "|".join(
        _normalize(x) for x in (topic, question_type, guidance_mode)
    )[:160]


def _normalize(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "")[:48] or "未标注"


class TeachingExperienceLibrary:
    """教学经验库（内存缓存 + Persistence 脏标记批量写）"""

    def __init__(self, max_items: int = 500):
        self._insights: dict[str, TeachingInsight] = {}
        self._on_dirty: Callable[[str], None] = lambda iid: None
        self._max_items = max_items
        self._contributors: dict[str, set[str]] = {}   # id -> 匿名贡献者集合（进程内）

    def set_dirty_hook(self, hook: Callable[[str], None]):
        self._on_dirty = hook

    def bind_limits(self, max_items: int):
        self._max_items = max_items

    # ------------------------------------------------------------
    # 沉淀（反思巩固时调用）
    # ------------------------------------------------------------

    def record(
        self,
        topic: str,
        question_type: str,
        guidance_mode: str,
        effectiveness: float,
        adjustments: Optional[list[str]] = None,
        contributor: Optional[str] = None,
        min_effectiveness: float = 0.65,
    ) -> Optional[TeachingInsight]:
        """沉淀/合并一条教学经验；效果不达标或无主题时返回 None"""
        if not topic or not str(topic).strip():
            return None
        if effectiveness < min_effectiveness:
            return None

        topic = str(topic).strip()
        qtype = str(question_type or "").strip() or "未知"
        mode = str(guidance_mode or "").strip() or "未知"
        key = make_insight_key(topic, qtype, mode)
        now = time.time()

        existing = self._insights.get(key)
        if existing is None:
            insight = TeachingInsight(
                id=key, topic=topic, question_type=qtype, guidance_mode=mode,
                insight=self._compose_text(mode, adjustments),
                effectiveness=effectiveness, sample_count=1,
                contributor_count=1, created_at=now, updated_at=now,
            )
            self._insights[key] = insight
            self._contributors[key] = {contributor} if contributor else set()
            self._evict_if_full()
            self._on_dirty(key)
            logger.info(f"[经验库] 新增洞见: {key}（效果 {effectiveness:.2f}）")
            return insight

        # 合并：样本计数、效果 EMA、贡献者计数、洞见文本进化
        n = existing.sample_count
        existing.sample_count = n + 1
        existing.effectiveness = (existing.effectiveness * n + effectiveness) / (n + 1)
        if contributor and contributor not in self._contributors.get(key, set()):
            existing.contributor_count += 1
            self._contributors.setdefault(key, set()).add(contributor)
        new_text = self._compose_text(mode, adjustments)
        if new_text:
            existing.insight = new_text          # 教学实践进化：取最新有效做法
        existing.updated_at = now
        self._on_dirty(key)
        return existing

    @staticmethod
    def _compose_text(guidance_mode: str, adjustments: Optional[list[str]]) -> str:
        """把反思的调整建议提炼成经验文本"""
        parts = [a.strip() for a in (adjustments or []) if a and a.strip()]
        if not parts:
            return f"「{guidance_mode}」引导模式在该主题上效果良好，可优先采用"
        return "；".join(parts[:3])

    def _evict_if_full(self):
        """超过上限时按 updated_at 淘汰最旧条目"""
        if len(self._insights) <= self._max_items:
            return
        oldest = sorted(self._insights.items(), key=lambda kv: kv[1].updated_at)
        for key, _ in oldest[: len(oldest) - self._max_items]:
            self._insights.pop(key, None)
            self._contributors.pop(key, None)

    # ------------------------------------------------------------
    # 消费
    # ------------------------------------------------------------

    def get_hints(
        self,
        topic: Optional[str] = None,
        question_type: Optional[str] = None,
        limit: int = 3,
    ) -> list[str]:
        """生成教学经验提示（注入 Tutor Prompt，按主题匹配优先）"""
        scored: list[tuple[float, TeachingInsight]] = []
        t_norm = _normalize(topic) if topic else ""
        for ins in self._insights.values():
            if ins.sample_count < 1:
                continue
            if question_type and _normalize(ins.question_type) != _normalize(question_type):
                continue
            score = ins.effectiveness * (1 + 0.5 * min(ins.sample_count, 10) / 10)
            if t_norm and t_norm in ins.id:
                score += 2.0    # 主题命中大幅加权
            scored.append((score, ins))
        scored.sort(key=lambda kv: kv[0], reverse=True)

        hints = []
        for _, ins in scored[:limit]:
            hints.append(
                f"[{ins.topic}·{ins.guidance_mode}] {ins.insight}"
                f"（{ins.sample_count} 轮验证，平均效果 {ins.effectiveness:.2f}）"
            )
        return hints

    # ------------------------------------------------------------
    # 管理（管理员 API）
    # ------------------------------------------------------------

    def list_insights(self, topic: Optional[str] = None) -> list[TeachingInsight]:
        items = sorted(self._insights.values(), key=lambda i: i.updated_at, reverse=True)
        if topic:
            t = _normalize(topic)
            items = [i for i in items if t in i.id]
        return items

    def delete(self, insight_id: str) -> bool:
        if insight_id not in self._insights:
            return False
        self._insights.pop(insight_id, None)
        self._contributors.pop(insight_id, None)
        self._on_dirty(insight_id)
        return True

    # ------------------------------------------------------------
    # 导入导出（Persistence）
    # ------------------------------------------------------------

    def export_insights(self) -> dict[str, TeachingInsight]:
        return dict(self._insights)

    def import_insights(self, insights: dict[str, TeachingInsight]):
        self._insights.update(insights)

    def __len__(self):
        return len(self._insights)
