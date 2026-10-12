"""
智能导学 Agent（Harness 重构版）- 配置模块

所有配置优先从环境变量读取，也可通过 .env 文件设置。

Harness 新增配置：
  - MAX_SUPERVISOR_HOPS：Supervisor 单轮最大调度次数（防死循环）
  - REFLECTION_IN_BACKGROUND：反思是否后台执行（默认是，保证回复延迟）
  - STREAM_ENABLED：是否启用 SSE 流式输出
  - HISTORY_WINDOW：注入 Prompt 的历史消息条数
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置 - 优先从环境变量/.env文件读取"""

    # ---- LLM 配置（OpenAI 兼容，双通道共用）----
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_timeout: float = 60.0

    # ---- 数据库配置 ----
    database_url: str = "postgresql://postgres:postgres@localhost:5433/postgres"

    # ---- 服务配置 ----
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # ---- CORS ----
    cors_origins: str = (
        "http://localhost:3000,http://localhost:8000,"
        "http://127.0.0.1:3000,http://127.0.0.1:8000"
    )

    # ---- 会话 TTL ----
    session_ttl_hours: int = 24
    session_cleanup_interval_minutes: int = 30

    # ---- API 认证 ----
    api_key: str = ""

    # ---- 用户管理系统（v5.0 新增）----
    # JWT 签名密钥；留空时自动生成并持久化到 .auth_secret 文件（重启不失效）
    auth_secret: str = ""
    # 访问令牌有效期（小时）
    auth_token_expire_hours: int = 72
    # 是否强制登录（True 时匿名访问 401；False 开发模式映射到共享 guest 用户空间）
    auth_required: bool = False
    # 初始管理员引导（仅在数据库无任何管理员时创建一次）
    admin_bootstrap_username: str = "admin"
    admin_bootstrap_password: str = "admin123456"
    # 是否允许自由注册（False 时仅管理员可创建用户）
    auth_allow_registration: bool = True

    # ---- 教学经验库（v5.0 新增：跨用户统一整理）----
    # 反思效果评分达到该阈值才沉淀为教学经验
    experience_min_effectiveness: float = 0.65
    # 经验条目上限（按 updated_at LRU 淘汰）
    experience_max_items: int = 500

    # ---- 请求限流 ----
    rate_limit_enabled: bool = True
    rate_limit_per_minute: str = "30/minute"
    rate_limit_chat_per_minute: str = "10/minute"

    # ---- 输入限制 ----
    max_input_length: int = 2000

    # ---- LLM 熔断器（深度优化新增）----
    # 连续 N 次「主备双失败」后进入熔断冷却，期间快速失败走规则降级
    llm_circuit_failure_threshold: int = 3
    # 瞬态错误（429/5xx/超时/网络）冷却秒数
    llm_circuit_cooldown_transient: float = 120.0
    # 非瞬态错误（401/403/404，如额度耗尽）冷却秒数
    llm_circuit_cooldown_permanent: float = 600.0

    # ---- SSE 流式看门狗（深度优化新增）----
    # 单个事件的最大等待秒数（防止后台任务挂起导致客户端永久等待）
    sse_idle_timeout: float = 180.0

    # ---- Harness 编排配置 ----
    # Supervisor 单轮最大调度次数（超过则强制 finish，防止循环）
    max_supervisor_hops: int = 6
    # Supervisor 是否使用 LLM 路由（False = 纯规则路由，延迟最低）
    supervisor_llm_routing: bool = True
    # 反思/进化/步骤计划懒加载是否在后台异步执行（教学体验优化：不阻塞回复）
    reflection_in_background: bool = True
    # 是否启用 SSE 流式输出
    stream_enabled: bool = True
    # 注入 Prompt 的历史消息窗口
    history_window: int = 6

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return origins

    @property
    def api_key_enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.llm_api_key
            and self.llm_api_key.strip()
            and self.llm_api_key not in ("your-api-key-here", "placeholder")
        )


settings = Settings()
