"""Helpers for routing WeChat traffic through an optional HTTP relay."""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from urllib.parse import quote

PROXY_ENV_NAME = "WECHAT_PROXY_BASE_URL"
_SERVER_CONF_FILE = Path(__file__).resolve().parents[1] / "server.conf"


def _read_proxy_config() -> tuple[bool, str | None]:
    """Return (enabled, base_url) from server.conf [proxy] section."""
    conf = configparser.ConfigParser()
    if not _SERVER_CONF_FILE.exists():
        return False, None

    try:
        conf.read(_SERVER_CONF_FILE, encoding="utf-8")
    except (configparser.Error, OSError):
        return False, None

    enabled = conf.getboolean("proxy", "enabled", fallback=False)
    base_url = conf.get("proxy", "base_url", fallback="").strip().rstrip("/")
    if not base_url:
        return enabled, None
    return enabled, base_url


def get_proxy_base_url() -> str | None:
    """Return normalized proxy base URL, preferring server.conf over env."""
    enabled, conf_base_url = _read_proxy_config()
    if enabled and conf_base_url:
        return conf_base_url

    if enabled and not conf_base_url:
        return None

    value = (os.getenv(PROXY_ENV_NAME) or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def wrap_url(target_url: str) -> str:
    """Wrap a target URL to go through machine-B relay when enabled."""
    proxy_base = get_proxy_base_url()
    if not proxy_base:
        return target_url
    return f"{proxy_base}/proxy?url={quote(target_url, safe='')}"
