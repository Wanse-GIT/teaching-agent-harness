"""
智能导学 Agent（Harness 重构版）- FastAPI 应用入口

架构：Harness 编排层 + LangGraph 多 Agent 协作 + 三层记忆 + 双通道 LLM

启动方式：
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

启动流程（lifespan）：
  1. 初始化三层记忆（PostgreSQL 可用则加载历史，否则降级纯内存）
  2. 初始化 LangGraph Checkpointer（PostgresSaver / InMemorySaver 降级）
  3. 构建 Supervisor 主图 + 反思后台子图
  4. 启动会话清理后台任务
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router as rest_router
from app.api.sse import router as sse_router
from app.api.auth_routes import router as auth_router
from app.api.admin_routes import router as admin_router
from app.config import settings
from app.memory import memory

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 日志降噪（深度优化）：非 debug 模式下压掉 httpx/httpcore/openai 的 INFO/DEBUG 噪声
if not settings.debug:
    for _noisy in ("httpx", "httpcore", "openai", "openai._base_client"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

_cleanup_task: asyncio.Task | None = None
_checkpointer_pool = None  # psycopg AsyncConnectionPool（关闭时需显式 close）


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task

    # ===== 1. 三层记忆初始化（含 PostgreSQL 降级逻辑） =====
    logger.info("正在初始化三层记忆框架...")
    memory.init()

    # ===== 1.5 用户管理系统初始化（v5.0） =====
    from app.auth import user_manager
    from app.memory.persistence import persistence
    user_manager.bind(persistence)
    user_manager.load()   # 加载用户 + 无管理员时引导 bootstrap 管理员
    logger.info(
        f"用户系统就绪：{len(user_manager.list_users())} 个账号，"
        f"管理员 {user_manager.admin_count} 名"
    )

    # ===== 2. LangGraph Checkpointer（图状态持久化） =====
    checkpointer, pool = await _init_checkpointer()
    global _checkpointer_pool
    _checkpointer_pool = pool

    # ===== 3. 构建 Harness Runner（主图 + 反思子图） =====
    from app.harness.runner import runner
    runner.init(checkpointer)

    # ===== 4. 会话清理后台任务 =====
    _cleanup_task = asyncio.create_task(
        memory.periodic_cleanup(
            ttl_hours=settings.session_ttl_hours,
            interval_minutes=settings.session_cleanup_interval_minutes,
        )
    )
    logger.info(
        f"会话清理任务已启动（TTL={settings.session_ttl_hours}h, "
        f"间隔={settings.session_cleanup_interval_minutes}min）"
    )

    if settings.api_key_enabled:
        logger.info("API Key 认证已启用")
    else:
        logger.warning("API Key 认证未启用（开发模式），建议生产环境设置 API_KEY")

    if settings.auth_required:
        logger.info("用户登录强制模式已启用（AUTH_REQUIRED=true）")
    else:
        logger.warning("匿名访问映射 guest 空间（开发模式）；生产建议 AUTH_REQUIRED=true")

    yield

    # ===== 关闭 =====
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass

    try:
        from app.llm.gateway import shutdown_llm_gateway
        await shutdown_llm_gateway()
    except Exception as e:
        logger.warning(f"关闭 LLM 网关失败: {e}")

    # 关闭 Checkpointer 连接池（修复：此前创建后未关闭，存在泄漏）
    if _checkpointer_pool is not None:
        try:
            await _checkpointer_pool.close()
            logger.info("Checkpointer 连接池已关闭")
        except Exception as e:
            logger.warning(f"关闭 Checkpointer 连接池失败: {e}")

    memory.shutdown()
    logger.info("应用关闭")


async def _init_checkpointer():
    """
    初始化 LangGraph Checkpointer：
      PostgreSQL 可用 → AsyncPostgresSaver（图状态持久化，支持会话恢复）
      不可用          → InMemorySaver（进程内，降级）

    返回 (checkpointer, pool)；pool 可能为 None（降级模式），
    调用方需在应用关闭时 await pool.close()。
    """
    if not memory.db_available:
        logger.warning("数据库不可用，Checkpointer 降级为 InMemorySaver")
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), None

    try:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # 修复：池内连接必须与 AsyncPostgresSaver.from_conn_string 的内建配置保持一致，
        # 否则 setup() 迁移会因隐式事务失败（CREATE INDEX CONCURRENTLY cannot run
        # inside a transaction block）——用户实测踩坑点：
        #   autocommit=True       → 迁移中的 CREATE INDEX CONCURRENTLY 不能在事务块内执行
        #   row_factory=dict_row  → setup() 内部以 row["v"] 按列名取值，默认元组行会报错
        #   prepare_threshold=0   → 与库内建连接行为一致，避免预备语句缓存串扰
        pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            max_size=5,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await pool.open(wait=True, timeout=15)
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        logger.info("LangGraph Checkpointer: AsyncPostgresSaver 已启用")
        return checkpointer, pool
    except Exception as e:
        logger.warning(f"PostgresSaver 初始化失败，降级为 InMemorySaver: {e}")
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), None


app = FastAPI(
    title="智能导学 Agent (Harness)",
    description=(
        "Harness 编排 + LangGraph 多Agent协作（Supervisor调度） | "
        "感知→分类→评估→引导→反思→进化 | "
        "教学性/解答性/事实性问题 → 差异化引导策略 | 三层记忆框架 | "
        "多用户隔离（画像/会话按用户隔离，教学经验跨用户统一整理）"
    ),
    version=__version__,
    lifespan=lifespan,
)

# CORS（深度优化：未配置来源时禁用 credentials，避免 "*" + credentials 违反浏览器安全规范）
cors_origins = settings.cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_credentials=bool(cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info(f"CORS 允许来源: {cors_origins or ['*']}")

# 路由
app.include_router(rest_router)
app.include_router(sse_router)
app.include_router(auth_router)     # v5.0：/api/v2/auth/*
app.include_router(admin_router)   # v5.0：/api/v2/admin/*

# 静态文件（现代 UI）
STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    """返回前端页面"""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "智能导学 Agent (Harness) API 正在运行", "docs": "/docs"}
