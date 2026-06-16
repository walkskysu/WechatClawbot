"""Run a simple HTTP relay on machine B for WeChat API/CDN traffic."""

from __future__ import annotations

import argparse
import configparser
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

_CONF_FILE = Path(__file__).resolve().parent / "server.conf"

import aiohttp
from aiohttp import web

DEFAULT_ALLOWED_SUFFIXES = [
    "weixin.qq.com",
]
HOP_BY_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _load_conf() -> configparser.ConfigParser:
    conf = configparser.ConfigParser()
    if _CONF_FILE.exists():
        try:
            conf.read(_CONF_FILE, encoding="utf-8")
        except (configparser.Error, OSError):
            pass
    return conf


def _parse_allowed_suffixes() -> list[str]:
    raw = (os.getenv("PROXY_ALLOW_HOST_SUFFIXES") or "").strip()
    if not raw:
        return DEFAULT_ALLOWED_SUFFIXES
    items = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return items or DEFAULT_ALLOWED_SUFFIXES


def _parse_allowed_ips(conf: configparser.ConfigParser) -> list[str]:
    """Return whitelisted client IPs from server.conf [proxy] section.

    Falls back to PROXY_ALLOWED_IPS env var when the config key is absent/empty.
    An empty result means all IPs are allowed.
    """
    raw = conf.get("proxy", "allowed_ips", fallback="").strip()
    if not raw:
        raw = (os.getenv("PROXY_ALLOWED_IPS") or "").strip()
    if not raw:
        return []
    return [ip.strip() for ip in raw.split(",") if ip.strip()]


@web.middleware
async def ip_whitelist_middleware(
    request: web.Request, handler
) -> web.StreamResponse:
    allowed_ips: list[str] = request.app["allowed_ips"]
    if allowed_ips:
        client_ip = request.remote or ""
        if client_ip not in allowed_ips:
            logging.warning(
                "blocked request from %s — not in IP whitelist", client_ip
            )
            return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


def _is_allowed_host(host: str, allowed_suffixes: list[str]) -> bool:
    host_l = host.lower()
    for suffix in allowed_suffixes:
        if host_l == suffix or host_l.endswith(f".{suffix}"):
            return True
    return False


def _filter_request_headers(headers: aiohttp.typedefs.LooseHeaders) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        key_l = key.lower()
        if key_l in HOP_BY_HOP_HEADERS or key_l == "host":
            continue
        out[key] = value
    return out


def _filter_response_headers(headers: aiohttp.typedefs.LooseHeaders) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        key_l = key.lower()
        if key_l in HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_proxy(request: web.Request) -> web.Response:
    app = request.app
    allowed_suffixes: list[str] = app["allowed_suffixes"]
    timeout: aiohttp.ClientTimeout = app["timeout"]

    target_url = (request.query.get("url") or "").strip()
    if not target_url:
        return web.json_response({"error": "missing query param: url"}, status=400)

    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return web.json_response({"error": "invalid target url"}, status=400)

    if not _is_allowed_host(parsed.hostname, allowed_suffixes):
        return web.json_response(
            {
                "error": "target host is not allowed",
                "host": parsed.hostname,
            },
            status=403,
        )

    body = await request.read()
    headers = _filter_request_headers(request.headers)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                request.method,
                target_url,
                data=body if body else None,
                headers=headers,
            ) as upstream:
                payload = await upstream.read()
                resp_headers = _filter_response_headers(upstream.headers)
                return web.Response(
                    status=upstream.status,
                    body=payload,
                    headers=resp_headers,
                )
    except Exception as exc:
        logging.exception("proxy request failed: %s", exc)
        return web.json_response(
            {
                "error": "proxy upstream request failed",
                "detail": str(exc),
            },
            status=502,
        )


def create_app() -> web.Application:
    conf = _load_conf()
    max_body_mb = int(os.getenv("PROXY_MAX_BODY_MB", "200"))
    timeout_seconds = float(os.getenv("PROXY_TIMEOUT_SECONDS", "600"))

    app = web.Application(
        client_max_size=max_body_mb * 1024 * 1024,
        middlewares=[ip_whitelist_middleware],
    )
    app["allowed_suffixes"] = _parse_allowed_suffixes()
    app["timeout"] = aiohttp.ClientTimeout(total=timeout_seconds)
    app["allowed_ips"] = _parse_allowed_ips(conf)
    app.router.add_get("/healthz", handle_health)
    app.router.add_route("*", "/proxy", handle_proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat relay proxy server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--log-level",
        default=os.getenv("PROXY_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    allowed_ips = _parse_allowed_ips(_load_conf())
    logging.info("starting proxy on %s:%s", args.host, args.port)
    logging.info("allowed host suffixes: %s", ", ".join(_parse_allowed_suffixes()))
    if allowed_ips:
        logging.info("IP whitelist: %s", ", ".join(allowed_ips))
    else:
        logging.info("IP whitelist: disabled (all IPs allowed)")
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
