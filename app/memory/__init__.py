"""
三层记忆框架 - 组装门面

    WorkingMemory              工作记忆（短期会话，按用户隔离）
    ProfileMemory              长期画像记忆（按用户隔离，跨会话共享）
    StrategyMemory             策略经验记忆（全局）
    TeachingExperienceLibrary  教学经验库（v5.0：跨用户统一整理）
    MemoryConsolidator         记忆巩固器（进化执行者，兼经验沉淀）
    Persistence                PostgreSQL 持久化（脏标记批量写 + 降级）

对外暴露全局单例 `memory`：
    memory.working.get_or_create(sid)
    memory.profile.get_summary_text(sid)
    memory.strategy.update(...)
    memory.experience.record(...)          # 教学经验沉淀
    memory.consolidator.consolidate_turn(...)
    memory.flush()          # 批量持久化脏数据
    memory.load_from_db()   # 启动加载
"""

import asyncio
import logging

from app.config import settings
from app.memory.working import WorkingMemory
from app.memory.profile import ProfileMemory
from app.memory.strategy import StrategyMemory
from app.memory.experience import TeachingExperienceLibrary
from app.memory.consolidator import MemoryConsolidator
from app.memory.persistence import persistence

logger = logging.getLogger(__name__)


class MemoryFacade:
    """三层记忆门面（v5.0：+ 教学经验库）"""

    def __init__(self):
        self.working = WorkingMemory()
        self.profile = ProfileMemory()
        self.strategy = StrategyMemory()
        self.experience = TeachingExperienceLibrary(
            max_items=settings.experience_max_items
        )
        self.consolidator = MemoryConsolidator(
            self.working, self.profile, self.strategy, self.experience
        )

        # 注入持久化脏标记钩子
        self.working.set_dirty_hook(persistence.hook_working)
        self.profile.set_dirty_hook(persistence.hook_profile)
        self.strategy.set_dirty_hook(persistence.hook_strategy)
        self.experience.set_dirty_hook(persistence.hook_experience)

    # ------------------------------------------------------------
    # 初始化（FastAPI lifespan 调用）
    # ------------------------------------------------------------

    def init(self) -> bool:
        """初始化持久化；DB 可用则加载历史数据"""
        ok = persistence.init()
        if ok:
            persistence.load(self.working, self.profile, self.strategy)
            persistence.load_experiences(self.experience)
        else:
            logger.warning("使用纯内存模式（重启后记忆丢失，服务功能不受影响）")
        return ok

    def shutdown(self):
        self.flush()
        persistence.shutdown()

    # ------------------------------------------------------------
    # 批量持久化
    # ------------------------------------------------------------

    def flush(self):
        persistence.flush(
            self.working, self.profile, self.strategy, self.experience
        )

    @property
    def db_available(self) -> bool:
        return persistence.db_available

    # ------------------------------------------------------------
    # 后台清理任务
    # ------------------------------------------------------------

    async def periodic_cleanup(self, ttl_hours: int, interval_minutes: int):
        """定期清理过期会话（防内存泄漏）"""
        while True:
            try:
                await asyncio.sleep(interval_minutes * 60)
                persistence.cleanup_expired(self.working, ttl_hours)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"会话清理任务异常: {e}")

    # ------------------------------------------------------------
    # 重置会话（API 调用）
    # ------------------------------------------------------------

    def reset_session(self, session_id: str):
        self.working.clear(session_id)
        persistence.delete_session(session_id)


# 全局单例
memory = MemoryFacade()
