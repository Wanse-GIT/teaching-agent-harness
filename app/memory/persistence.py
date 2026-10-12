"""
三层记忆框架 - PostgreSQL 持久化层

存储策略：内存（一级缓存）+ PostgreSQL（持久化）双写
  - 启动时从 DB 加载到内存
  - 写入使用脏标记 + 批量 flush（减少每请求 DB 写入次数，移植原项目优化）
  - DB 不可用时自动降级为纯内存模式（重启后数据丢失，服务不受影响）

表结构（JSONB 存储复杂对象，与 Pydantic 模型灵活映射）：
  - agent_sessions           工作记忆
  - agent_user_profiles      长期画像记忆
  - agent_strategy_stats     策略经验记忆
  - agent_users              用户账号（v5.0）
  - agent_teaching_insights  教学经验库（v5.0，跨用户共享）
"""

import logging
import time

from sqlalchemy import Column, Float, JSON, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# 注意：UserStore/ExperienceStore 分别定义在 models/user.py 与 memory/experience.py，
# 为避免循环导入，在 init() 前 import 注册到同一 Base.metadata。

logger = logging.getLogger(__name__)

Base = declarative_base()


# ============================================================
# ORM 模型
# ============================================================

class SessionStore(Base):
    """工作记忆表：整个 SessionState 序列化为 JSON"""
    __tablename__ = "agent_sessions"
    session_id = Column(String(128), primary_key=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(Float, default=0.0)


class UserProfileStore(Base):
    """长期画像记忆表"""
    __tablename__ = "agent_user_profiles"
    session_id = Column(String(128), primary_key=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(Float, default=0.0)


class StrategyStatsStore(Base):
    """策略经验记忆表：key = "{question_type}_{guidance_mode}\""""
    __tablename__ = "agent_strategy_stats"
    key = Column(String(128), primary_key=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(Float, default=0.0)


class ExperienceStore(Base):
    """教学经验库表（v5.0：跨用户统一整理的教学经验）
    id = {topic}|{question_type}|{guidance_mode} 归一化"""
    __tablename__ = "agent_teaching_insights"
    id = Column(String(160), primary_key=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(Float, default=0.0)


# ============================================================
# Persistence 管理器
# ============================================================

class Persistence:
    """PostgreSQL 持久化管理器（脏标记 + 批量写 + 启动加载）"""

    def __init__(self):
        self.db_available = False
        self._engine = None
        self._session_factory = None
        self._dirty_sessions: set[str] = set()
        self._dirty_profiles: set[str] = set()
        self._dirty_strategy_stats: set[str] = set()
        self._dirty_experiences: set[str] = set()

    # ------------------------------------------------------------
    # 初始化 / 降级
    # ------------------------------------------------------------

    def init(self) -> bool:
        """初始化数据库连接；失败时降级为纯内存模式"""
        try:
            # 注册 v5.0 新表（用户表 + 教学经验库）到同一 Base
            from app.models.user import UserStore  # noqa: F401

            self._engine = create_engine(
                settings.database_url,
                echo=False,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
            Base.metadata.create_all(self._engine)
            self.db_available = True
            logger.info("PostgreSQL 持久化已启用")
            return True
        except Exception as e:
            logger.warning(f"PostgreSQL 不可用，降级为纯内存模式: {e}")
            self.db_available = False
            return False

    def shutdown(self):
        if self._engine is not None:
            try:
                self._engine.dispose()
            except Exception:
                pass
        self.db_available = False

    # ------------------------------------------------------------
    # 脏标记钩子（注入到三层记忆）
    # ------------------------------------------------------------

    def hook_working(self, session_id: str):
        self._dirty_sessions.add(session_id)

    def hook_profile(self, session_id: str):
        self._dirty_profiles.add(session_id)

    def hook_strategy(self, key: str):
        self._dirty_strategy_stats.add(key)

    def hook_experience(self, insight_id: str):
        self._dirty_experiences.add(insight_id)

    # ------------------------------------------------------------
    # 批量写回
    # ------------------------------------------------------------

    def flush(self, working=None, profile=None, strategy=None, experience=None):
        """批量持久化所有脏数据（请求结束/后台任务结束时调用）"""
        if not self.db_available:
            self._clear_dirty()
            return

        try:
            db = self._session_factory()
            try:
                now_ts = time.time()
                sessions = working.export_sessions() if working else {}
                profiles = profile.export_profiles() if profile else {}
                stats_map = strategy.export_stats() if strategy else {}
                insights = experience.export_insights() if experience else {}

                for sid in list(self._dirty_sessions):
                    s = sessions.get(sid)
                    if s is None:
                        # 会话已被清除 → 删除 DB 记录
                        db.query(SessionStore).filter_by(session_id=sid).delete()
                    else:
                        db.merge(SessionStore(
                            session_id=sid, data=s.model_dump(mode="json"), updated_at=now_ts
                        ))
                for sid in list(self._dirty_profiles):
                    p = profiles.get(sid)
                    if p is not None:
                        db.merge(UserProfileStore(
                            session_id=sid, data=p.model_dump(mode="json"), updated_at=now_ts
                        ))
                for key in list(self._dirty_strategy_stats):
                    st = stats_map.get(key)
                    if st is not None:
                        db.merge(StrategyStatsStore(
                            key=key, data=st.model_dump(mode="json"), updated_at=now_ts
                        ))
                for iid in list(self._dirty_experiences):
                    ins = insights.get(iid)
                    if ins is not None:
                        db.merge(ExperienceStore(
                            id=iid, data=ins.model_dump(mode="json"), updated_at=now_ts
                        ))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            self._clear_dirty()
        except Exception as e:
            logger.error(f"批量持久化失败（保留脏标记待重试）: {e}")

    def _clear_dirty(self):
        self._dirty_sessions.clear()
        self._dirty_profiles.clear()
        self._dirty_strategy_stats.clear()
        self._dirty_experiences.clear()

    # ------------------------------------------------------------
    # 启动加载
    # ------------------------------------------------------------

    def load(self, working=None, profile=None, strategy=None):
        """启动时从 DB 加载历史数据到内存缓存"""
        if not self.db_available:
            return
        from app.models.domain import (
            ClassificationResult,
            CognitiveState,
            CognitiveLevel,
            PerceptionResult,
            SessionState,
            StepPlan,
            StepItem,
            StepStatus,
            StrategyStats,
            UserProfile,
            QuestionType,
            GuidanceMode,
        )
        from pydantic import TypeAdapter

        def _from_json(model_cls, payload):
            # 枚举/嵌套字段由 Pydantic 校验器自动恢复
            try:
                return model_cls.model_validate(payload)
            except Exception as e:
                logger.warning(f"加载数据校验失败({model_cls.__name__}): {e}")
                return None

        try:
            db = self._session_factory()
            try:
                # 会话
                for row in db.query(SessionStore).all():
                    s = _from_json(SessionState, row.data)
                    if s:
                        working.import_sessions({row.session_id: s})
                # 画像
                for row in db.query(UserProfileStore).all():
                    p = _from_json(UserProfile, row.data)
                    if p:
                        profile.import_profiles({row.session_id: p})
                # 策略
                for row in db.query(StrategyStatsStore).all():
                    st = _from_json(StrategyStats, row.data)
                    if st:
                        strategy.import_stats({row.key: st})
            finally:
                db.close()
            logger.info(
                f"已从 DB 加载: {len(working.export_sessions())} 会话, "
                f"{len(profile.export_profiles())} 画像, {len(strategy.export_stats())} 策略"
            )
        except Exception as e:
            logger.error(f"从 DB 加载失败: {e}")

    def load_experiences(self, experience):
        """启动时从 DB 加载教学经验库"""
        if not self.db_available:
            return
        from app.memory.experience import TeachingInsight

        try:
            db = self._session_factory()
            try:
                for row in db.query(ExperienceStore).all():
                    try:
                        ins = TeachingInsight.model_validate(row.data)
                        experience.import_insights({row.id: ins})
                    except Exception as e:
                        logger.warning(f"加载教学经验失败({row.id}): {e}")
            finally:
                db.close()
            logger.info(f"已从 DB 加载教学经验 {len(experience.export_insights())} 条")
        except Exception as e:
            logger.error(f"从 DB 加载教学经验失败: {e}")

    # ------------------------------------------------------------
    # 用户账号存储（v5.0：即时写，保证注册/改密一致性）
    # ------------------------------------------------------------

    def save_user(self, record, password_hash: str):
        """保存/更新用户（即时写回）"""
        if not self.db_available:
            return
        from app.models.user import UserStore

        try:
            db = self._session_factory()
            try:
                db.merge(UserStore(
                    id=record.id,
                    username=record.username,
                    password_hash=password_hash,
                    role=record.role,
                    is_active=record.is_active,
                    created_at=record.created_at,
                    last_login_at=record.last_login_at,
                ))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"保存用户失败: {e}")

    def load_users(self):
        """加载全部用户 [(UserRecord, password_hash)]"""
        if not self.db_available:
            return []
        from app.models.user import UserRecord, UserStore

        users = []
        try:
            db = self._session_factory()
            try:
                for row in db.query(UserStore).all():
                    record = UserRecord(
                        id=row.id,
                        username=row.username,
                        role=row.role,
                        is_active=row.is_active,
                        created_at=row.created_at,
                        last_login_at=row.last_login_at,
                    )
                    users.append((record, row.password_hash))
            finally:
                db.close()
        except Exception as e:
            logger.error(f"加载用户失败: {e}")
        return users

    def delete_user(self, user_id: str):
        """删除用户账号行（记忆数据由 UserManager 级联清理）"""
        if not self.db_available:
            return
        from app.models.user import UserStore

        try:
            db = self._session_factory()
            try:
                db.query(UserStore).filter_by(id=user_id).delete()
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"删除用户失败: {e}")

    # ------------------------------------------------------------
    # 会话清理
    # ------------------------------------------------------------

    def delete_session(self, session_id: str):
        """从 DB 删除会话（重置会话时）"""
        if not self.db_available:
            return
        try:
            db = self._session_factory()
            try:
                db.query(SessionStore).filter_by(session_id=session_id).delete()
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"删除会话持久化记录失败: {e}")

    def delete_profile(self, profile_id: str):
        """从 DB 删除画像（删除用户时级联）"""
        if not self.db_available:
            return
        try:
            db = self._session_factory()
            try:
                db.query(UserProfileStore).filter_by(session_id=profile_id).delete()
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"删除画像持久化记录失败: {e}")

    def cleanup_expired(self, working, ttl_hours: int) -> int:
        """清理过期会话（内存 + DB），返回清理数量"""
        cutoff = time.time() - ttl_hours * 3600
        expired = []
        for sid, s in working.export_sessions().items():
            if s.updated_at and s.updated_at < cutoff:
                expired.append(sid)
        for sid in expired:
            working.clear(sid)
            self.delete_session(sid)
        if expired:
            logger.info(f"清理过期会话 {len(expired)} 个（TTL={ttl_hours}h）")
        return len(expired)


# ============================================================
# 全局单例
# ============================================================

persistence = Persistence()
