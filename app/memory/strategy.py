"""
三层记忆框架 - 第三层：策略经验记忆（程序性记忆）

职责：
  - 按 (问题类型 × 引导模式) 维护策略效果统计
  - 记录策略效果曲线（供前端仪表盘绘制）
  - 积累有效的调整建议（best_adjustments）
"""

import logging
from typing import Callable, Dict, Optional

from app.models.domain import QuestionType, StrategyStats

logger = logging.getLogger(__name__)


class StrategyMemory:
    """策略经验记忆（对应原 agent_strategy_stats 语义，新增效果曲线）"""

    def __init__(self, history_limit: int = 30):
        self._stats: Dict[str, StrategyStats] = {}
        self._on_dirty: Callable[[str], None] = lambda key: None
        self._history_limit = history_limit

    def set_dirty_hook(self, hook: Callable[[str], None]):
        self._on_dirty = hook

    @staticmethod
    def make_key(question_type: QuestionType, guidance_mode: str) -> str:
        return f"{question_type.value}_{guidance_mode}"

    def get(self, key: str) -> StrategyStats:
        return self._stats.get(key, StrategyStats())

    def clear(self):
        self._stats.clear()

    def export_stats(self) -> dict[str, StrategyStats]:
        return dict(self._stats)

    def import_stats(self, stats: dict[str, StrategyStats]):
        self._stats.update(stats)

    # ------------------------------------------------------------
    # 更新（反思进化师写回）
    # ------------------------------------------------------------

    def update(
        self,
        question_type: QuestionType,
        guidance_mode: str,
        effectiveness: float,
        progress: float,
        adjustments: list[str],
    ) -> StrategyStats:
        key = self.make_key(question_type, guidance_mode)
        stats = self._stats.get(key, StrategyStats())
        n = stats.total_uses
        stats.avg_effectiveness = (stats.avg_effectiveness * n + effectiveness) / (n + 1)
        stats.avg_progress = (stats.avg_progress * n + progress) / (n + 1)
        stats.total_uses = n + 1
        if adjustments:
            stats.best_adjustments = (stats.best_adjustments + adjustments[-3:])[-6:]
        # 效果曲线（前端可视化）
        stats.effectiveness_history = (stats.effectiveness_history + [effectiveness])[
            -self._history_limit:
        ]
        self._stats[key] = stats
        self._on_dirty(key)
        return stats

    # ------------------------------------------------------------
    # 上下文提示（Harness 上下文装配：让导师知道哪些策略对该学生有效）
    # ------------------------------------------------------------

    def get_strategy_hints(self, question_type: Optional[QuestionType] = None) -> list[str]:
        """生成策略提示列表（供导师 Prompt 参考）"""
        hints: list[str] = []
        for key, stats in self._stats.items():
            if stats.total_uses < 2:
                continue
            if question_type and not key.startswith(f"{question_type.value}_"):
                continue
            if stats.avg_effectiveness >= 0.7:
                mode = key.split("_", 1)[1] if "_" in key else key
                hints.append(
                    f"策略「{mode}」在{question_type.value if question_type else key}场景"
                    f"平均效果 {stats.avg_effectiveness:.2f}（{stats.total_uses}次使用），可优先采用"
                )
            if stats.avg_effectiveness < 0.4 and stats.total_uses >= 3:
                mode = key.split("_", 1)[1] if "_" in key else key
                hints.append(
                    f"策略「{mode}」历史效果偏低（{stats.avg_effectiveness:.2f}），注意调整方式"
                )
        return hints[:4]
