"""
--- L9_META ---
l9_schema: 1
origin: chassis
engine: graph
layer: [api, auth, config]
tags: [chassis, auth, settings]
owner: platform-team
status: active
--- /L9_META ---

engine/config/settings.py

Application settings via pydantic-settings.
Reads from .env file and environment variables.
Single source of truth for all L9_* configuration.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRETS = frozenset({"password", "change-me-in-production"})


class Settings(BaseSettings):
    """L9 Engine configuration. All env vars prefixed L9_ unless noted."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Project Identity ---
    l9_project: str = "l9-engine"
    l9_env: str = "dev"

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password"
    neo4j_database: str = "neo4j"
    neo4j_pool_size: int = 50
    neo4j_max_connection_lifetime: int = 3600
    neo4j_connection_acquisition_timeout: int = 60

    # CEG-007: `redis_url` was declared here and never read — nothing under
    # engine/ or chassis/ imports redis. Removed with the service and the
    # dependency rather than left as a knob for a component that does not exist.

    # --- API ---
    api_port: int = 8000
    api_workers: int = 4
    api_secret_key: str = "change-me-in-production"
    cors_origins: list[str] = []  # Default deny-all; set via CORS_ORIGINS env var

    # --- API Authentication ---
    # L9_API_KEY env var. Required in production. Used by BearerAuthMiddleware.
    # Generated via: python -c "import secrets; print(secrets.token_urlsafe(48))"
    # Stored in AWS Secrets Manager: clawdbot/l9-api → {"L9_API_KEY": "<value>"}
    l9_api_key: str = ""

    # Issue #400 A5: the former `domains_root = ./domains` here was read by
    # nothing; the pack root is engine/config/settings.py's domains_root only.

    # --- Logging ---
    log_level: str = "INFO"

    # --- GDS ---
    gds_enabled: bool = True

    # --- Match result bound ---
    # Scoring dimensions and their default weights are the domain's
    # (scoring.dimensions[*].defaultweight, ceiling checked per direction at
    # spec load, C-22). The former fixed four-dimension weights (structural,
    # geo, reinforcement, freshness) reached no scoring path and were retired.
    max_results: int = 25

    # --- Temporal Decay ---
    # Mirrors engine/config/settings.py (Stage 2 R10): industry-specific global
    # defaults with no caller were removed there and here.
    decay_transaction_halflife: float = 180.0  # days

    # --- KGE (Phase 4) ---
    kge_enabled: bool = False
    kge_embedding_dim: int = 300
    kge_confidence_threshold: float = 0.3

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Raise if default secrets are used in production environment."""
        if self.l9_env == "prod":
            if self.neo4j_password in _DEFAULT_SECRETS:
                msg = "neo4j_password must be changed from default in production"
                raise ValueError(msg)
            if self.api_secret_key in _DEFAULT_SECRETS:
                msg = "api_secret_key must be changed from default in production"
                raise ValueError(msg)
            if not self.l9_api_key:
                msg = "L9_API_KEY must be set in production"
                raise ValueError(msg)
        return self

    @property
    def is_production(self) -> bool:
        return self.l9_env == "prod"

    @property
    def is_development(self) -> bool:
        return self.l9_env == "dev"


# Singleton — import this instance everywhere
settings = Settings()
