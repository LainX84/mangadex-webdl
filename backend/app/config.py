"""Process configuration (environment + Docker secrets).

Runtime-tunable behaviour (rate limits, intervals, naming) lives in the database
(see ``app.settings_service``); this module only holds deployment configuration.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip() or None


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MDM_", env_file=None, extra="ignore")

    # --- Infrastructure -------------------------------------------------------
    database_url: str | None = None
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "mangadex_manager"
    db_user: str = "mangadex_manager"
    db_password: str | None = None
    db_password_file: str | None = None

    redis_url: str = "redis://redis:6379/0"

    # --- Paths ----------------------------------------------------------------
    downloads_root: Path = Path("/downloads")
    data_dir: Path = Path("/data")
    # Downloads in progress: one flat folder per chapter, moved into the library only
    # when complete. Empty = ``{downloads_root}/.mdm-staging``. Put it on the same
    # mount as the library (e.g. a sibling folder under one bind mount) so the final
    # move is an atomic rename; on another filesystem the chapter is copied instead.
    staging_root: Path | None = None
    # Previous copies of chapters (redownloads, same-group re-uploads, new MangaDex
    # versions), kept as ``{replaced_root}/{Manga}/[...]/{Chapter} {timestamp}``.
    # Empty = ``{downloads_root}/.mdm-replaced``.
    replaced_root: Path | None = None

    # Library safety guard: a sentinel file that must exist at the library root.
    # If the NFS share is not mounted the (empty) mountpoint directory will not
    # contain it, so nothing is ever written into the local disk by accident.
    library_sentinel: str = ".mdm-library"
    # Folder that holds the sentinel. Empty = the library root itself. Set it to the folder
    # above the library (e.g. the bind-mount root ``/mangadex``) so that moving the whole
    # content of the library folder by hand never takes the sentinel with it. It must be
    # on the same filesystem as the library (checked).
    sentinel_dir: Path | None = None
    # Comma separated filesystem types accepted for the downloads root
    # (e.g. "nfs,nfs4"). Empty = do not check the filesystem type.
    expected_fs_types: str = ""

    # --- Web / security -------------------------------------------------------
    public_url: str | None = None  # e.g. https://manga.example.com
    trusted_proxies: str = ""  # comma separated IPs/CIDRs of reverse proxies
    cookie_secure: str = "auto"  # auto | true | false
    session_idle_hours: int = 24 * 7
    session_absolute_days: int = 30
    login_max_failures: int = 5
    login_window_minutes: int = 15
    login_lockout_max_seconds: int = 900

    admin_username: str | None = None
    admin_password_file: str | None = None

    # --- MangaDex -------------------------------------------------------------
    mangadex_api_url: str = "https://api.mangadex.org"
    mangadex_uploads_url: str = "https://uploads.mangadex.org"
    mangadex_report_url: str = "https://api.mangadex.network/report"
    user_agent: str = "MangaDexWebDL/1.0 (+https://github.com/lainx84/mangadex-webdl; self-hosted, personal use)"
    http_timeout_seconds: float = 30.0

    # --- MangaDex account (optional; personal API client, read-only use) -----------
    # Docker secrets, one value per file. Empty or missing files = account not configured.
    mangadex_auth_url: str = "https://auth.mangadex.org/realms/mangadex/protocol/openid-connect/token"
    md_client_id_file: str = "/run/secrets/md_client_id"
    md_client_secret_file: str = "/run/secrets/md_client_secret"
    md_username_file: str = "/run/secrets/md_username"
    md_password_file: str = "/run/secrets/md_password"

    # --- Comics archive (optional, read-only) ----------------------------------
    # Mount point of the existing CBZ collection; only read to suggest the first
    # chapter to download. Missing or empty directory = feature off.
    archive_root: Path | None = Path("/archive")

    # --- Misc -----------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    static_dir: Path = Path("/app/static")
    environment: str = "production"

    worker_queues: str = Field(default="downloads,scans,default")

    @field_validator("cookie_secure")
    @classmethod
    def _cookie_secure(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"auto", "true", "false"}:
            raise ValueError("MDM_COOKIE_SECURE must be auto, true or false")
        return v

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = self.db_password or _read_secret_file(self.db_password_file) or ""
        from sqlalchemy.engine import URL

        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        nets = []
        for item in self.trusted_proxies.split(","):
            item = item.strip()
            if item:
                nets.append(ipaddress.ip_network(item, strict=False))
        return nets

    @property
    def expected_fs_type_set(self) -> set[str]:
        return {t.strip() for t in self.expected_fs_types.split(",") if t.strip()}

    @property
    def admin_password(self) -> str | None:
        return _read_secret_file(self.admin_password_file)

    @field_validator("staging_root", "replaced_root", "sentinel_dir", mode="before")
    @classmethod
    def _empty_path(cls, v):
        return None if v in ("", None) else v

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


@lru_cache
def get_config() -> Config:
    return Config()


def reset_config_cache() -> None:
    get_config.cache_clear()
