"""Vibe Bar Quota Pusher.

Reads vibe-bar's local quota JSON files from ~/.vibebar/quotas/ and uploads a
dashboard-shaped snapshot to a remote API. Designed to be invoked once per
launchd interval (StartInterval=600) rather than running its own loop.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

APPLE_EPOCH_OFFSET = 978_307_200

PROVIDER_META: dict[str, dict] = {
    "claude":         {"id": "claude-cli",     "display_name": "Claude",       "subtitle": "Claude Code CLI",   "icon": "C"},
    "codex":          {"id": "codex-cli",      "display_name": "Codex",        "subtitle": "OpenAI Codex CLI",  "icon": "Cx"},
    "gemini":         {"id": "gemini-cli",     "display_name": "Gemini",       "subtitle": "Gemini CLI",        "icon": "G"},
    "antigravity":    {"id": "antigravity",    "display_name": "Antigravity",  "subtitle": "Google AI",         "icon": "Ag"},
    "copilot":        {"id": "copilot",        "display_name": "Copilot",      "subtitle": "GitHub Copilot",    "icon": "Co"},
    "cursor":         {"id": "cursor",         "display_name": "Cursor",       "subtitle": "Cursor IDE",        "icon": "Cu"},
    "zai":            {"id": "zai",            "display_name": "Z.ai",         "subtitle": "GLM coding plan",   "icon": "Z"},
    "minimax":        {"id": "minimax",        "display_name": "MiniMax",      "subtitle": "MiniMax coding",    "icon": "Mx"},
    "kimi":           {"id": "kimi",           "display_name": "Kimi",         "subtitle": "Moonshot Kimi",     "icon": "K"},
    "alibaba":        {"id": "alibaba",        "display_name": "Qwen",         "subtitle": "Alibaba Qwen",      "icon": "Q"},
    "mimo":           {"id": "mimo",           "display_name": "MiMo",         "subtitle": "Xiaomi MiMo",       "icon": "Mi"},
    "iflytek":        {"id": "iflytek",        "display_name": "Spark",        "subtitle": "iFlytek Spark",     "icon": "Sp"},
    "tencentHunyuan": {"id": "tencent-hunyuan","display_name": "Hunyuan",      "subtitle": "Tencent Hunyuan",   "icon": "Hy"},
    "volcengine":     {"id": "volcengine",     "display_name": "Volcengine",   "subtitle": "ByteDance",         "icon": "Vc"},
    "openCodeGo":     {"id": "opencode-go",    "display_name": "OpenCode Go",  "subtitle": "OpenCode Go",       "icon": "Og"},
    "kilo":           {"id": "kilo",           "display_name": "Kilo",         "subtitle": "Kilo",              "icon": "Kl"},
    "kiro":           {"id": "kiro",           "display_name": "Kiro",         "subtitle": "Kiro",              "icon": "Kr"},
    "ollama":         {"id": "ollama",         "display_name": "Ollama",       "subtitle": "Local LLM",         "icon": "Ol"},
    "openRouter":     {"id": "openrouter",     "display_name": "OpenRouter",   "subtitle": "OpenRouter",        "icon": "Or"},
}


@dataclass
class Config:
    api_url: str
    api_token: str
    api_method: str
    quotas_dir: Path
    timeout_seconds: float
    log_level: str

    def masked_token(self) -> str:
        if len(self.api_token) <= 8:
            return "***"
        return f"{self.api_token[:4]}…{self.api_token[-3:]}"


def load_config() -> Config:
    load_dotenv()
    api_url = os.environ.get("VIBEBAR_API_URL", "").strip()
    api_token = os.environ.get("VIBEBAR_API_TOKEN", "").strip()
    if not api_url:
        raise SystemExit("VIBEBAR_API_URL is required (see .env.example)")
    if not api_token:
        raise SystemExit("VIBEBAR_API_TOKEN is required (see .env.example)")
    return Config(
        api_url=api_url,
        api_token=api_token,
        api_method=os.environ.get("VIBEBAR_API_METHOD", "PUT").strip().upper(),
        quotas_dir=Path(os.environ.get("VIBEBAR_QUOTAS_DIR", "~/.vibebar/quotas")).expanduser(),
        timeout_seconds=float(os.environ.get("VIBEBAR_TIMEOUT_SECONDS", "10")),
        log_level=os.environ.get("VIBEBAR_LOG_LEVEL", "INFO").strip().upper(),
    )


def apple_to_unix(apple_seconds: float) -> float:
    return float(apple_seconds) + APPLE_EPOCH_OFFSET


def humanize_reset(reset_at_apple: float, now_unix: float | None = None) -> str | None:
    """Format an Apple-epoch reset timestamp as a human "Resets in …" string.

    Returns None if reset is in the past or invalid.
    """
    try:
        reset_unix = apple_to_unix(reset_at_apple)
    except (TypeError, ValueError):
        return None
    if now_unix is None:
        now_unix = time.time()
    delta = reset_unix - now_unix
    if delta <= 0:
        return None
    if delta < 60:
        return "Resets in <1m"
    if delta < 3600:
        minutes = int(delta // 60)
        return f"Resets in {minutes}m"
    if delta < 86400:
        hours = int(delta // 3600)
        minutes = int((delta % 3600) // 60)
        return f"Resets in {hours}h {minutes}m"
    days = int(delta // 86400)
    hours = int((delta % 86400) // 3600)
    return f"Resets in {days}d {hours}h"


_DETAIL_PATTERN = re.compile(r"^\s*([\d.]+\s*/\s*[\d.]+)\b")


def parse_detail(group_title: str | None) -> str | None:
    """Extract a "used/total" fragment from a groupTitle like '4464/4500 · 5 hours'."""
    if not group_title:
        return None
    match = _DETAIL_PATTERN.search(group_title)
    return match.group(1).replace(" ", "") if match else None


def lookup_meta(tool: str) -> dict:
    if tool in PROVIDER_META:
        return PROVIDER_META[tool]
    fallback_id = re.sub(r"(?<!^)(?=[A-Z])", "-", tool).lower() if tool else "unknown"
    return {
        "id": fallback_id or "unknown",
        "display_name": tool.title() if tool else "Unknown",
        "subtitle": None,
        "icon": (tool[:1].upper() if tool else "?"),
    }


def _clamp_percent(used: float) -> int:
    remaining = 100 - float(used)
    return max(0, min(100, round(remaining)))


def transform_bucket(bucket: dict, now_unix: float | None = None) -> dict | None:
    try:
        used = bucket["usedPercent"]
        label = bucket["title"]
    except KeyError:
        return None
    reset_text = None
    if "resetAt" in bucket:
        reset_text = humanize_reset(bucket["resetAt"], now_unix=now_unix)
    return {
        "label": str(label),
        "percent_remaining": _clamp_percent(used),
        "detail": parse_detail(bucket.get("groupTitle")),
        "reset_text": reset_text,
        "state": None,
    }


def transform_to_provider(raw: dict, source_file: str, now_unix: float | None = None) -> dict | None:
    tool = raw.get("tool")
    if not tool:
        logging.warning("skipping %s: missing 'tool' field", source_file)
        return None
    meta = lookup_meta(tool)
    metrics: list[dict] = []
    for bucket in raw.get("buckets", []):
        metric = transform_bucket(bucket, now_unix=now_unix)
        if metric is None:
            logging.warning("skipping bucket in %s: missing required fields (%s)", source_file, bucket.get("id"))
            continue
        metrics.append(metric)
    updated_at = None
    if "queriedAt" in raw:
        try:
            updated_at = int(apple_to_unix(raw["queriedAt"]))
        except (TypeError, ValueError):
            updated_at = None
    return {
        "id": meta["id"],
        "display_name": meta["display_name"],
        "subtitle": meta["subtitle"],
        "icon": meta["icon"],
        "updated_at": updated_at,
        "error": raw.get("error"),
        "metrics": metrics,
    }


def read_quota_files(quotas_dir: Path) -> list[tuple[str, dict]]:
    if not quotas_dir.is_dir():
        raise SystemExit(f"Quota directory does not exist: {quotas_dir} (is vibe-bar running?)")
    results: list[tuple[str, dict]] = []
    for path in sorted(quotas_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fp:
                results.append((path.name, json.load(fp)))
        except FileNotFoundError:
            logging.warning("file vanished during read: %s", path.name)
        except json.JSONDecodeError as exc:
            logging.warning("invalid JSON in %s: %s", path.name, exc)
        except OSError as exc:
            logging.warning("could not read %s: %s", path.name, exc)
    return results


def build_envelope(cfg: Config, now_unix: float | None = None) -> dict:
    files = read_quota_files(cfg.quotas_dir)
    providers: list[dict] = []
    for name, raw in files:
        provider = transform_to_provider(raw, source_file=name, now_unix=now_unix)
        if provider is not None:
            providers.append(provider)
    return {"providers": providers}


def upload(payload: dict, cfg: Config) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {cfg.api_token}",
        "Content-Type": "application/json",
        "User-Agent": "vibe-bar-pusher/0.1 (+darwin)",
    }
    return requests.request(
        cfg.api_method,
        cfg.api_url,
        headers=headers,
        json=payload,
        timeout=cfg.timeout_seconds,
    )


def _classify_and_exit(response: requests.Response) -> int:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    if 200 <= status < 300:
        logging.info("upload ok: status=%s body=%s", status, body)
        return 0
    if 400 <= status < 500:
        logging.error("upload rejected: status=%s body=%s", status, body)
        return 2
    logging.warning("upload server error: status=%s body=%s", status, body)
    return 0


def cmd_once(cfg: Config) -> int:
    payload = build_envelope(cfg)
    logging.info("built payload with %d provider(s)", len(payload["providers"]))
    try:
        response = upload(payload, cfg)
    except requests.RequestException as exc:
        logging.warning("upload network failure: %s", exc)
        return 0
    return _classify_and_exit(response)


def cmd_dry_run(cfg: Config) -> int:
    payload = build_envelope(cfg)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    logging.info("dry-run printed %d provider(s) — nothing was sent", len(payload["providers"]))
    return 0


def cmd_ping(cfg: Config) -> int:
    payload = {"providers": []}
    logging.info("ping: sending empty payload to %s", cfg.api_url)
    try:
        response = upload(payload, cfg)
    except requests.RequestException as exc:
        logging.error("ping network failure: %s", exc)
        return 2
    return _classify_and_exit(response)


def cmd_print_config(cfg: Config) -> int:
    print(f"VIBEBAR_API_URL          = {cfg.api_url}")
    print(f"VIBEBAR_API_TOKEN        = {cfg.masked_token()}")
    print(f"VIBEBAR_API_METHOD       = {cfg.api_method}")
    print(f"VIBEBAR_QUOTAS_DIR       = {cfg.quotas_dir}")
    print(f"VIBEBAR_TIMEOUT_SECONDS  = {cfg.timeout_seconds}")
    print(f"VIBEBAR_LOG_LEVEL        = {cfg.log_level}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push vibe-bar quota snapshot to remote dashboard.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Read once, upload, exit. Used by launchd.")
    group.add_argument("--dry-run", action="store_true", help="Print payload to stdout, do not upload.")
    group.add_argument("--ping", action="store_true", help="Upload empty payload to verify URL/token.")
    group.add_argument("--print-config", action="store_true", help="Print effective .env (token masked).")
    args = parser.parse_args(argv)

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    if args.once:
        return cmd_once(cfg)
    if args.dry_run:
        return cmd_dry_run(cfg)
    if args.ping:
        return cmd_ping(cfg)
    if args.print_config:
        return cmd_print_config(cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
