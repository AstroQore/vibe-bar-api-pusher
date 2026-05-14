"""Unit tests for pusher.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pusher  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with (FIXTURES / name).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def test_apple_to_unix_round_trip():
    assert pusher.apple_to_unix(0) == pusher.APPLE_EPOCH_OFFSET
    assert pusher.apple_to_unix(799163279.08) == pytest.approx(1777470479.08)


def test_humanize_reset_past_returns_none():
    now = pusher.apple_to_unix(1000)
    assert pusher.humanize_reset(500, now_unix=now) is None
    assert pusher.humanize_reset(1000, now_unix=now) is None


def test_humanize_reset_under_a_minute():
    now = pusher.apple_to_unix(0)
    assert pusher.humanize_reset(30, now_unix=now) == "Resets in <1m"


def test_humanize_reset_minutes():
    now = pusher.apple_to_unix(0)
    assert pusher.humanize_reset(5 * 60, now_unix=now) == "Resets in 5m"


def test_humanize_reset_hours_minutes():
    now = pusher.apple_to_unix(0)
    assert pusher.humanize_reset(5 * 3600 + 12 * 60, now_unix=now) == "Resets in 5h 12m"


def test_humanize_reset_days_hours():
    now = pusher.apple_to_unix(0)
    assert pusher.humanize_reset(86400 + 22 * 3600, now_unix=now) == "Resets in 1d 22h"


def test_humanize_reset_invalid_input():
    assert pusher.humanize_reset("not a number") is None
    assert pusher.humanize_reset(None) is None


def test_parse_detail_extracts_used_total():
    assert pusher.parse_detail("4464/4500 · 5 hours") == "4464/4500"
    assert pusher.parse_detail("43297/45000 · Weekly") == "43297/45000"
    assert pusher.parse_detail("5.53 / 19 credits") == "5.53/19"


def test_parse_detail_no_match():
    assert pusher.parse_detail(None) is None
    assert pusher.parse_detail("") is None
    assert pusher.parse_detail("Claude Sonnet") is None
    assert pusher.parse_detail("Gemini 2.5 Pro") is None


def test_lookup_meta_known_tools():
    claude = pusher.lookup_meta("claude")
    assert claude["id"] == "claude-cli"
    assert claude["display_name"] == "Claude"
    assert claude["icon"] == "C"

    minimax = pusher.lookup_meta("minimax")
    assert minimax["id"] == "minimax"
    assert minimax["display_name"] == "MiniMax"

    hunyuan = pusher.lookup_meta("tencentHunyuan")
    assert hunyuan["id"] == "tencent-hunyuan"


def test_lookup_meta_unknown_fallback():
    meta = pusher.lookup_meta("somenewprovider")
    assert meta["id"] == "somenewprovider"
    assert meta["display_name"] == "Somenewprovider"
    assert meta["icon"] == "S"
    assert meta["subtitle"] is None

    meta_camel = pusher.lookup_meta("fooBarBaz")
    assert meta_camel["id"] == "foo-bar-baz"


def test_lookup_meta_empty():
    meta = pusher.lookup_meta("")
    assert meta["id"] == "unknown"
    assert meta["display_name"] == "Unknown"
    assert meta["icon"] == "?"


def test_clamp_percent_normal():
    assert pusher._clamp_percent(0) == 100
    assert pusher._clamp_percent(2) == 98
    assert pusher._clamp_percent(50) == 50
    assert pusher._clamp_percent(100) == 0


def test_clamp_percent_floats_and_bounds():
    assert pusher._clamp_percent(1.3333) == 99
    assert pusher._clamp_percent(98.7) == 1
    assert pusher._clamp_percent(-5) == 100  # clamp upper
    assert pusher._clamp_percent(150) == 0  # clamp lower


def test_transform_bucket_full_fields():
    bucket = {
        "id": "five_hour",
        "title": "5 Hours",
        "groupTitle": "Claude Sonnet",
        "rawWindowSeconds": 18000,
        "resetAt": 799173600.07,
        "usedPercent": 2,
    }
    now = pusher.apple_to_unix(799163279.08)  # 2.87 hours before resetAt
    metric = pusher.transform_bucket(bucket, now_unix=now)
    assert metric == {
        "label": "5 Hours",
        "percent_remaining": 98,
        "detail": None,  # groupTitle has no "used/total" pattern
        "reset_text": "Resets in 2h 52m",
        "state": None,
    }


def test_transform_bucket_minimax_detail():
    bucket = {
        "id": "minimax.weekly",
        "title": "Weekly",
        "groupTitle": "43297/45000 · Weekly",
        "resetAt": 800726400,
        "usedPercent": 3.78,
    }
    now = pusher.apple_to_unix(800477287)
    metric = pusher.transform_bucket(bucket, now_unix=now)
    assert metric["label"] == "Weekly"
    assert metric["percent_remaining"] == 96
    assert metric["detail"] == "43297/45000"
    assert metric["reset_text"].startswith("Resets in 2d")


def test_transform_bucket_missing_required_fields():
    assert pusher.transform_bucket({}) is None
    assert pusher.transform_bucket({"title": "x"}) is None
    assert pusher.transform_bucket({"usedPercent": 50}) is None


def test_transform_to_provider_cli_claude():
    raw = load_fixture("cli-claude.json")
    now = pusher.apple_to_unix(raw["queriedAt"])
    provider = pusher.transform_to_provider(raw, "cli-claude.json", now_unix=now)
    assert provider["id"] == "claude-cli"
    assert provider["display_name"] == "Claude"
    assert provider["icon"] == "C"
    assert provider["updated_at"] == int(pusher.apple_to_unix(raw["queriedAt"]))
    assert provider["error"] is None
    assert len(provider["metrics"]) == 4
    five_hour = provider["metrics"][0]
    assert five_hour["label"] == "5 Hours"
    assert five_hour["percent_remaining"] == 98
    assert five_hour["state"] is None


def test_transform_to_provider_minimax():
    raw = load_fixture("minimax-sample.json")
    now = pusher.apple_to_unix(raw["queriedAt"])
    provider = pusher.transform_to_provider(raw, "minimax-sample.json", now_unix=now)
    assert provider["id"] == "minimax"
    assert provider["display_name"] == "MiniMax"
    weekly_metric = next(m for m in provider["metrics"] if m["label"] == "Weekly")
    assert weekly_metric["detail"] is not None
    assert weekly_metric["detail"].endswith("/45000")
    assert weekly_metric["percent_remaining"] >= 90


def test_transform_to_provider_unknown_tool_falls_back():
    raw = {
        "tool": "newprovider",
        "queriedAt": 799163279.08,
        "buckets": [
            {"id": "daily", "title": "Daily", "resetAt": 799173600, "usedPercent": 25},
        ],
    }
    provider = pusher.transform_to_provider(raw, "newprovider.json", now_unix=pusher.apple_to_unix(799163279.08))
    assert provider["id"] == "newprovider"
    assert provider["display_name"] == "Newprovider"
    assert len(provider["metrics"]) == 1


def test_transform_to_provider_missing_tool_returns_none():
    assert pusher.transform_to_provider({"buckets": []}, "anon.json") is None


def test_transform_to_provider_skips_bad_buckets():
    raw = {
        "tool": "claude",
        "queriedAt": 0,
        "buckets": [
            {"id": "ok", "title": "Good", "resetAt": 100000, "usedPercent": 10},
            {"id": "bad", "title": "Missing usedPercent"},
            {"id": "ok2", "title": "Also good", "resetAt": 100000, "usedPercent": 5},
        ],
    }
    provider = pusher.transform_to_provider(raw, "x.json", now_unix=pusher.apple_to_unix(0))
    assert len(provider["metrics"]) == 2
    assert [m["label"] for m in provider["metrics"]] == ["Good", "Also good"]


def test_build_envelope_uses_fixture_dir():
    cfg = pusher.Config(
        api_url="http://example.invalid",
        api_token="t",
        api_method="PUT",
        quotas_dir=FIXTURES,
        timeout_seconds=1.0,
        log_level="INFO",
    )
    envelope = pusher.build_envelope(cfg)
    assert "providers" in envelope
    ids = {p["id"] for p in envelope["providers"]}
    # The 4 fixture files should produce 3 distinct providers (claude, gemini, minimax, antigravity)
    assert {"claude-cli", "gemini-cli", "minimax", "antigravity"}.issubset(ids)


def test_read_quota_files_missing_dir_raises():
    with pytest.raises(SystemExit):
        pusher.read_quota_files(Path("/nonexistent/path/to/.vibebar/quotas"))


def test_config_masked_token():
    cfg = pusher.Config(
        api_url="https://x",
        api_token="abcdefghijklmnop",
        api_method="PUT",
        quotas_dir=Path("/tmp"),
        timeout_seconds=10,
        log_level="INFO",
    )
    masked = cfg.masked_token()
    assert masked.startswith("abcd")
    assert masked.endswith("nop")
    assert "…" in masked
