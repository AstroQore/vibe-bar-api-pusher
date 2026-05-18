"""Vibe Bar Quota Pusher.

Reads vibe-bar's local quota JSON files from ~/.vibebar/quotas/ and uploads a
dashboard-shaped snapshot to a remote API. Designed to be invoked once per
launchd interval (StartInterval=600) rather than running its own loop.

Multi-account model (vibe-bar >= PR #17):
* Primary tools (`claude`, `codex`) — one card per tool. All files with the
  matching `tool` field are merged into one logical observation, with buckets
  de-duplicated by id (preferring entries with full reset / window fields and
  freshest queriedAt). This collapses the legacy `cli-<tool>.json` plus any
  `quota-v1-*.json` reports of the same primary subscription.
* Misc tools — one card per `miscProviderInstance` in `settings.json`. Each
  instance has a stable id; its quota lives in `quota-v1-<sha256("misc-"+id)>.json`.
  The instance's user-set `displayName` (e.g. "Generic" vs "Hy") becomes the
  card subtitle and is slugified into the compound dashboard id
  `<tool-slug>::<account-slug>` so multiple instances of the same tool don't
  collide on the receiver.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

APPLE_EPOCH_OFFSET = 978_307_200

# Tools that vibe-bar treats as "primary" — they have CLI + cookie scrape
# variants that should all collapse into one dashboard card. Misc tools live
# in `miscProviderInstances` and each visible instance is its own card.
PRIMARY_TOOLS: frozenset[str] = frozenset({"claude", "codex"})

# Display metadata for every vibe-bar ToolType, mirroring the source-of-truth
# in vibe-bar's `Sources/VibeBarCore/Models/ToolType.swift`. New tools added
# upstream just need a row here.
PROVIDER_META: dict[str, dict] = {
    "claude":           {"id": "claude",            "display_name": "Claude",        "subtitle": "Claude Code",      "icon": "C"},
    "codex":            {"id": "codex",             "display_name": "Codex",         "subtitle": "OpenAI CodeX",     "icon": "Cx"},
    "alibaba":          {"id": "alibaba",           "display_name": "Bailian",       "subtitle": "Coding Plan",      "icon": "Ba"},
    "alibabaTokenPlan": {"id": "alibaba-token",     "display_name": "Bailian",       "subtitle": "Token Plan",       "icon": "Ba"},
    "gemini":           {"id": "gemini",            "display_name": "Gemini",        "subtitle": "Usage",            "icon": "G"},
    "antigravity":      {"id": "antigravity",       "display_name": "Antigravity",   "subtitle": "Local LSP",        "icon": "Ag"},
    "copilot":          {"id": "copilot",           "display_name": "Copilot",       "subtitle": "GitHub Copilot",   "icon": "Co"},
    "zai":              {"id": "zai",               "display_name": "Z.ai",          "subtitle": "Coding Plan",      "icon": "Z"},
    "minimax":          {"id": "minimax",           "display_name": "MiniMax",       "subtitle": "Token Plan",       "icon": "Mx"},
    "kimi":             {"id": "kimi",              "display_name": "Kimi",          "subtitle": "Kimi for coding",  "icon": "K"},
    "cursor":           {"id": "cursor",            "display_name": "Cursor",        "subtitle": "Cursor",           "icon": "Cu"},
    "mimo":             {"id": "mimo",              "display_name": "MiMo",          "subtitle": "Token Plan",       "icon": "Mi"},
    "iflytek":          {"id": "iflytek",           "display_name": "Spark",         "subtitle": "Coding Plan",      "icon": "Sp"},
    "tencentHunyuan":   {"id": "tencent-hunyuan",   "display_name": "Tencent",       "subtitle": "Coding Plan",      "icon": "Tc"},
    "tencentTokenPlan": {"id": "tencent-token",     "display_name": "Tencent",       "subtitle": "Token Plan",       "icon": "Tc"},
    "volcengine":       {"id": "volcengine",        "display_name": "Volcengine",    "subtitle": "Coding Plan",      "icon": "Vc"},
    "baiduQianfan":     {"id": "baidu-qianfan",     "display_name": "Qianfan",       "subtitle": "Coding Plan",      "icon": "Bd"},
    "openCodeGo":       {"id": "opencode-go",       "display_name": "OpenCode Go",   "subtitle": "Workspace",        "icon": "Og"},
    "kilo":             {"id": "kilo",              "display_name": "Kilo",          "subtitle": "Credits",          "icon": "Kl"},
    "kiro":             {"id": "kiro",              "display_name": "Kiro",          "subtitle": "CLI Usage",        "icon": "Kr"},
    "ollama":           {"id": "ollama",            "display_name": "Ollama",        "subtitle": "Cloud",            "icon": "Ol"},
    "openRouter":       {"id": "openrouter",        "display_name": "OpenRouter",    "subtitle": "Credits",          "icon": "Or"},
    "warp":             {"id": "warp",              "display_name": "Warp",          "subtitle": "AI Credits",       "icon": "Wp"},
}


@dataclass
class Config:
    api_url: str
    api_token: str
    api_method: str
    quotas_dir: Path
    settings_path: Path
    timeout_seconds: float
    log_level: str

    def masked_token(self) -> str:
        if len(self.api_token) <= 8:
            return "***"
        return f"{self.api_token[:4]}…{self.api_token[-3:]}"


@dataclass(frozen=True)
class InstanceMeta:
    tool: str
    instance_id: str
    display_name: str | None
    is_visible: bool


def load_config() -> Config:
    load_dotenv()
    api_url = os.environ.get("VIBEBAR_API_URL", "").strip()
    api_token = os.environ.get("VIBEBAR_API_TOKEN", "").strip()
    if not api_url:
        raise SystemExit("VIBEBAR_API_URL is required (see .env.example)")
    if not api_token:
        raise SystemExit("VIBEBAR_API_TOKEN is required (see .env.example)")
    quotas_dir = Path(os.environ.get("VIBEBAR_QUOTAS_DIR", "~/.vibebar/quotas")).expanduser()
    settings_path_env = os.environ.get("VIBEBAR_SETTINGS_PATH")
    if settings_path_env:
        settings_path = Path(settings_path_env).expanduser()
    else:
        # Default: sibling to the quotas dir
        settings_path = quotas_dir.parent / "settings.json"
    return Config(
        api_url=api_url,
        api_token=api_token,
        api_method=os.environ.get("VIBEBAR_API_METHOD", "PUT").strip().upper(),
        quotas_dir=quotas_dir,
        settings_path=settings_path,
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


def slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics into single hyphens, trim."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "x"


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


def read_json_safe(path: Path) -> dict | None:
    """Best-effort JSON read. Swallows transient races + corrupt files."""
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logging.warning("invalid JSON in %s: %s", path.name, exc)
        return None
    except OSError as exc:
        logging.warning("could not read %s: %s", path.name, exc)
        return None


def load_instances(settings_path: Path) -> dict[str, list[InstanceMeta]]:
    """Read `miscProviderInstances` from vibe-bar's settings.json.

    Returns a dict tool -> list of InstanceMeta. Empty dict if settings.json is
    missing, malformed, or doesn't have the field (older vibe-bar versions).
    """
    raw = read_json_safe(settings_path) if settings_path.is_file() else None
    if not raw:
        if settings_path.exists():
            logging.warning("settings.json present but unreadable: %s", settings_path)
        else:
            logging.info("settings.json not found at %s; multi-account disabled", settings_path)
        return {}
    out: dict[str, list[InstanceMeta]] = defaultdict(list)
    for inst in raw.get("miscProviderInstances") or []:
        if not isinstance(inst, dict):
            continue
        tool = inst.get("tool")
        iid = inst.get("id")
        if not isinstance(tool, str) or not isinstance(iid, str) or not tool or not iid:
            continue
        out[tool].append(
            InstanceMeta(
                tool=tool,
                instance_id=iid,
                display_name=(inst.get("displayName") if isinstance(inst.get("displayName"), str) else None),
                is_visible=bool(inst.get("isVisible", True)),
            )
        )
    return dict(out)


def quota_file_for_instance(instance_id: str, quotas_dir: Path) -> Path | None:
    """Find the quota-v1-<sha256>.json file vibe-bar would write for this instance."""
    account_id = f"misc-{instance_id}"
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    candidate = quotas_dir / f"quota-v1-{digest}.json"
    return candidate if candidate.exists() else None


def collect_primary_files(quotas_dir: Path, tool: str) -> list[tuple[str, dict]]:
    """All quota files matching this primary tool (legacy cli + quota-v1-*)."""
    out: list[tuple[str, dict]] = []
    cli = quotas_dir / f"cli-{tool}.json"
    if cli.is_file():
        d = read_json_safe(cli)
        if d and d.get("tool") == tool:
            out.append((cli.name, d))
    for path in sorted(quotas_dir.glob("quota-v1-*.json")):
        d = read_json_safe(path)
        if d and d.get("tool") == tool:
            out.append((path.name, d))
    return out


def merge_primary_files(files: list[tuple[str, dict]]) -> dict:
    """Merge per-tool files into one synthetic raw quota dict.

    Bucket de-duplication strategy: keep the bucket from the freshest file by
    default, but if that bucket lacks `resetAt`, fall back to an older file's
    fuller copy. Top-level fields (`tool`, `plan`, `queriedAt`) come from the
    freshest file.
    """
    sorted_files = sorted(files, key=lambda nr: nr[1].get("queriedAt") or 0, reverse=True)
    bucket_map: dict[str, dict] = {}
    for _name, raw in sorted_files:
        for bucket in raw.get("buckets") or []:
            bid = bucket.get("id")
            if not bid:
                continue
            existing = bucket_map.get(bid)
            if existing is None:
                bucket_map[bid] = bucket
            elif "resetAt" not in existing and "resetAt" in bucket:
                # Older but more-complete; swap in the fuller record
                bucket_map[bid] = bucket
    freshest_raw = sorted_files[0][1]
    return {
        "tool": freshest_raw.get("tool"),
        "plan": freshest_raw.get("plan"),
        "email": freshest_raw.get("email"),
        "queriedAt": freshest_raw.get("queriedAt"),
        "error": freshest_raw.get("error"),
        "buckets": list(bucket_map.values()),
    }


def build_provider_card(
    base_meta: dict,
    instance_meta: InstanceMeta | None,
    raw: dict,
    multi_instance: bool,
    now_unix: float | None = None,
) -> dict:
    """Turn raw quota + identity into the dashboard's provider entry."""
    pid = base_meta["id"]
    sub_parts: list[str] = []
    if base_meta.get("subtitle"):
        sub_parts.append(base_meta["subtitle"])

    if multi_instance:
        # Disambiguate id and subtitle when multiple cards share a tool.
        account_label = None
        if instance_meta and instance_meta.display_name:
            account_label = instance_meta.display_name
        elif raw.get("plan"):
            account_label = str(raw["plan"])
        elif instance_meta:
            # No human label: fall back to a short slice of the instance id.
            tail = instance_meta.instance_id.rsplit("-", 1)[-1]
            account_label = tail[:8] if tail else "x"
        if account_label:
            pid = f"{pid}::{slugify(account_label)}"
            sub_parts.append(account_label)

    subtitle = " · ".join(sub_parts) if sub_parts else None

    metrics: list[dict] = []
    for bucket in raw.get("buckets") or []:
        m = transform_bucket(bucket, now_unix=now_unix)
        if m is None:
            logging.warning("skipping bucket missing required fields: id=%s", bucket.get("id"))
            continue
        metrics.append(m)

    updated_at = None
    if "queriedAt" in raw:
        try:
            updated_at = int(apple_to_unix(raw["queriedAt"]))
        except (TypeError, ValueError):
            updated_at = None

    return {
        "id": pid,
        "display_name": base_meta["display_name"],
        "subtitle": subtitle,
        "icon": base_meta["icon"],
        "updated_at": updated_at,
        "error": raw.get("error"),
        "metrics": metrics,
    }


def build_envelope(cfg: Config, now_unix: float | None = None) -> dict:
    if not cfg.quotas_dir.is_dir():
        raise SystemExit(
            f"Quota directory does not exist: {cfg.quotas_dir} (is vibe-bar running?)"
        )

    instances_by_tool = load_instances(cfg.settings_path)
    providers: list[dict] = []

    # Primary tools: merge legacy cli-{tool}.json + every quota-v1-*.json
    # whose tool field matches. Always one card per primary tool.
    for tool in sorted(PRIMARY_TOOLS):
        files = collect_primary_files(cfg.quotas_dir, tool)
        if not files:
            continue
        merged = merge_primary_files(files)
        if not merged.get("buckets"):
            logging.info("primary tool %s has no buckets across %d file(s); skipping", tool, len(files))
            continue
        meta = lookup_meta(tool)
        providers.append(
            build_provider_card(meta, None, merged, multi_instance=False, now_unix=now_unix)
        )

    # Misc tools.
    if instances_by_tool:
        # New path: each visible miscProviderInstance is its own card.
        for tool in sorted(instances_by_tool):
            insts = instances_by_tool[tool]
            meta = lookup_meta(tool)
            visible_insts = [i for i in insts if i.is_visible]
            multi = len(visible_insts) > 1
            for inst in visible_insts:
                quota_file = quota_file_for_instance(inst.instance_id, cfg.quotas_dir)
                if quota_file is None:
                    continue
                raw = read_json_safe(quota_file)
                if raw is None or raw.get("tool") != tool:
                    continue
                providers.append(
                    build_provider_card(meta, inst, raw, multi_instance=multi, now_unix=now_unix)
                )
    else:
        # Legacy fallback: enumerate misc quota files, one card each.
        for path in sorted(cfg.quotas_dir.glob("quota-v1-*.json")):
            raw = read_json_safe(path)
            if raw is None:
                continue
            tool = raw.get("tool")
            if not tool or tool in PRIMARY_TOOLS:
                continue
            meta = lookup_meta(tool)
            providers.append(
                build_provider_card(meta, None, raw, multi_instance=False, now_unix=now_unix)
            )

    return {"providers": providers}


def upload(payload: dict, cfg: Config) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {cfg.api_token}",
        "Content-Type": "application/json",
        "User-Agent": "vibe-bar-pusher/0.2 (+darwin)",
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
    print(f"VIBEBAR_SETTINGS_PATH    = {cfg.settings_path}")
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
