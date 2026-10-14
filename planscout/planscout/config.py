"""Runtime settings, read from environment variables."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import DATA_DIR
from .connectors.oauth2 import OAuth2Config, load_oauth_configs
from .security import TokenCipher

log = logging.getLogger("planscout")


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_path: str = "planscout.db"
    secret_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    encryption_key: str = field(default_factory=TokenCipher.generate_key)
    sandbox_enabled: bool = True
    https_only_cookies: bool = False
    base_url: str | None = None  # e.g. https://planscout.example.com, used to build OAuth redirect URIs
    data_dir: Path = DATA_DIR
    oauth_configs: dict[str, OAuth2Config] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        settings = cls(
            db_path=env.get("PLANSCOUT_DB_PATH", "planscout.db"),
            sandbox_enabled=_flag(env.get("PLANSCOUT_SANDBOX"), True),
            https_only_cookies=_flag(env.get("PLANSCOUT_HTTPS_ONLY"), False),
            base_url=env.get("PLANSCOUT_BASE_URL") or None,
            oauth_configs=load_oauth_configs(env),
        )
        if env.get("PLANSCOUT_SECRET_KEY"):
            settings.secret_key = env["PLANSCOUT_SECRET_KEY"]
        else:
            log.warning("PLANSCOUT_SECRET_KEY not set; using a random key (sessions reset on restart)")
        if env.get("PLANSCOUT_ENCRYPTION_KEY"):
            settings.encryption_key = env["PLANSCOUT_ENCRYPTION_KEY"]
        else:
            log.warning("PLANSCOUT_ENCRYPTION_KEY not set; stored carrier tokens will be unreadable after restart")
        return settings
