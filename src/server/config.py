from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Marketing Services Platform"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    git_sha: str = "unknown"  # SHA del build (env GIT_SHA, bakeado por el CI)
    # Bearer para ver sha/version en /health/deep (#246). Vacío => nunca se exponen.
    health_token: str = ""

    database_url: str = (
        "postgresql+asyncpg://marketing:marketing_password@localhost:5432/marketing_platform"
    )
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = "change_this_in_production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    magic_link_expire_minutes: int = 15

    cors_allowed_origins: str = "http://localhost:3000,http://localhost:3001,https://crm.mirkocalzadilla.com,https://mirkocalzadilla.com"

    # WhatsApp / Meta Cloud API
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_verify_token: str = "change_this_verify_token"
    whatsapp_api_version: str = "v19.0"
    # UUID del agente usado para nuevas conversaciones entrantes de WhatsApp
    whatsapp_default_agent_id: str = ""
    whatsapp_app_secret: str = ""

    # Media storage — downloaded inbound media + generated QR images
    media_root: str = "uploads"
    media_base_url: str = "http://localhost:8000"

    # QR de pago (imagen pública servida desde el repo web) — cierre `pago_qr`.
    payment_qr_url: str = "https://raw.githubusercontent.com/funnelops-marketing-services/web/main/public/qr-pagos.jpeg"

    # Observabilidad — Sentry (error tracking). DSN vacío => Sentry desactivado.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    sentry_release: str = ""

    # LLM runtime — provider seleccionable; model IDs pinned (nunca alias -latest)
    llm_provider: str = "anthropic"  # "anthropic" (default) | "openai"
    llm_max_tokens: int = 1024
    # Anthropic (default)
    anthropic_api_key: str = ""
    llm_model_haiku: str = "claude-haiku-4-5-20251001"
    llm_model_sonnet: str = "claude-sonnet-4-6"
    # Visión (leer un comprobante): tarea acotada, tier barato. Pineado como el resto.
    llm_model_vision: str = "claude-haiku-4-5-20251001"
    # Techo propio: la extracción devuelve un objeto chico, no una respuesta al lead.
    llm_vision_max_tokens: int = 1024
    # OpenAI (alterno) — snapshots pineados, sin alias -latest/-mini suelto
    openai_api_key: str = ""
    llm_openai_model_loop: str = "gpt-4o-mini-2024-07-18"
    llm_openai_model_summary: str = "gpt-4o-mini-2024-07-18"
    llm_openai_model_vision: str = "gpt-4o-mini-2024-07-18"

    # Resumen IA — cada cuántos turnos del lead se refresca dentro de la misma etapa (#254).
    # Throttle vs. costo de LLM: más bajo = resumen más fresco, más llamadas Haiku.
    summary_refresh_every_n_turns: int = 3

    @property
    def whatsapp_api_url(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_api_version}/{self.whatsapp_phone_number_id}/messages"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
