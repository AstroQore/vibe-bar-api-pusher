"""Unit tests for pusher.py.

Fixtures under ``tests/fixtures/vibebar/`` mirror a real ``~/.vibebar/``
layout, including ``settings.json`` with ``miscProviderInstances`` and
the matching ``quotas/quota-v1-<sha256>.json`` files for both single- and
multi-instance scenarios. The quota files preserve the multi-source state
vibe-bar leaves on disk after promoting Gemini / Antigravity / Grok from
misc to partial-primary (legacy misc-tab snapshots coexist with fresh
partial-primary writers).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pusher  # noqa: E402

FIXTURE_VIBEBAR = Path(__file__).parent / "fixtures" / "vibebar"
FIXTURE_QUOTAS = FIXTURE_VIBEBAR / "quotas"
FIXTURE_SETTINGS = FIXTURE_VIBEBAR / "settings.json"


def load_fixture_json(rel_path: str) -> dict:
    with (FIXTURE_VIBEBAR / rel_path).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def make_cfg(**overrides) -> pusher.Config:
    defaults = dict(
        api_url="http://example.invalid",
        api_token="t",
        api_method="PUT",
        quotas_dir=FIXTURE_QUOTAS,
        settings_path=FIXTURE_SETTINGS,
        timeout_seconds=1.0,
        log_level="INFO",
    )
    defaults.update(overrides)
    return pusher.Config(**defaults)


# ---------------------------------------------------------------------------
# Time + scalar helpers
# ---------------------------------------------------------------------------


def test_apple_to_unix_round_trip():
    assert pusher.apple_to_unix(0) == pusher.APPLE_EPOCH_OFFSET
    assert pusher.apple_to_unix(799163279.08) == pytest.approx(1777470479.08)


def test_humanize_reset_past_returns_none():
    now = pusher.apple_to_unix(1000)
    assert pusher.humanize_reset(500, now_unix=now) is None
    assert pusher.humanize_reset(1000, now_unix=now) is None


def test_humanize_reset_minutes_hours_days():
    now = pusher.apple_to_unix(0)
    assert pusher.humanize_reset(30, now_unix=now) == "Resets in <1m"
    assert pusher.humanize_reset(5 * 60, now_unix=now) == "Resets in 5m"
    assert pusher.humanize_reset(5 * 3600 + 12 * 60, now_unix=now) == "Resets in 5h 12m"
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


def test_clamp_percent():
    assert pusher._clamp_percent(0) == 100
    assert pusher._clamp_percent(2) == 98
    assert pusher._clamp_percent(100) == 0
    assert pusher._clamp_percent(1.3333) == 99
    assert pusher._clamp_percent(-5) == 100
    assert pusher._clamp_percent(150) == 0


def test_slugify_collapses_punctuation():
    assert pusher.slugify("HY Standard") == "hy-standard"
    assert pusher.slugify("Z.ai · GLM") == "z-ai-glm"
    assert pusher.slugify("") == "x"


# ---------------------------------------------------------------------------
# PROVIDER_META + lookup
# ---------------------------------------------------------------------------


def test_lookup_meta_covers_all_tooltype_cases():
    """Every ToolType case in vibe-bar should have a PROVIDER_META row.

    Mirror vibe-bar's `Sources/VibeBarCore/Models/ToolType.swift` exactly so a
    new upstream tool is impossible to forget — the cli is the only thing
    talking to the dashboard and an unknown tool gets a slugified fallback,
    which is fine but unbranded.
    """
    expected = {
        "claude", "codex", "alibaba", "alibabaTokenPlan", "gemini", "antigravity",
        "grok", "copilot", "zai", "minimax", "kimi", "cursor", "mimo", "iflytek",
        "tencentHunyuan", "tencentTokenPlan", "volcengine", "baiduQianfan",
        "openCodeGo", "kilo", "kiro", "ollama", "openRouter", "warp",
    }
    assert expected <= set(pusher.PROVIDER_META.keys())


def test_primary_tools_includes_partial_primary():
    """Primary + partial-primary all merge into one card per tool."""
    assert pusher.PRIMARY_TOOLS == {"claude", "codex", "gemini", "antigravity", "grok"}


def test_lookup_meta_unknown_fallback():
    meta = pusher.lookup_meta("someNewProvider")
    assert meta["id"] == "some-new-provider"
    assert meta["display_name"] == "Somenewprovider"
    assert meta["subtitle"] is None
    assert meta["icon"] == "S"


# ---------------------------------------------------------------------------
# Bucket transform
# ---------------------------------------------------------------------------


def test_transform_bucket_full_fields():
    now = pusher.apple_to_unix(799163279.08)
    bucket = {
        "id": "five_hour",
        "title": "5 Hours",
        "groupTitle": "Claude Sonnet",
        "resetAt": 799173600.07,
        "usedPercent": 2,
    }
    m = pusher.transform_bucket(bucket, now_unix=now)
    assert m == {
        "label": "5 Hours",
        "percent_remaining": 98,
        "detail": None,
        "reset_text": "Resets in 2h 52m",
        "state": None,
    }


def test_transform_bucket_with_minimax_detail():
    now = pusher.apple_to_unix(800477287)
    bucket = {
        "id": "minimax.weekly",
        "title": "Weekly",
        "groupTitle": "43297/45000 · Weekly",
        "resetAt": 800726400,
        "usedPercent": 3.78,
    }
    m = pusher.transform_bucket(bucket, now_unix=now)
    assert m["label"] == "Weekly"
    assert m["percent_remaining"] == 96
    assert m["detail"] == "43297/45000"
    assert m["reset_text"].startswith("Resets in")


def test_transform_bucket_missing_required_fields_returns_none():
    assert pusher.transform_bucket({}) is None
    assert pusher.transform_bucket({"title": "x"}) is None
    assert pusher.transform_bucket({"usedPercent": 50}) is None


# ---------------------------------------------------------------------------
# Instance loading + hash mapping
# ---------------------------------------------------------------------------


def test_load_instances_groups_by_tool():
    insts = pusher.load_instances(FIXTURE_SETTINGS)
    # Gemini/antigravity/grok are NOT here — vibe-bar promoted them out of
    # miscProviderInstances when it added partial-primary tiers.
    assert set(insts.keys()) == {
        "baiduQianfan", "alibabaTokenPlan",
        "openCodeGo", "tencentTokenPlan", "kimi", "minimax",
    }
    assert len(insts["openCodeGo"]) == 2
    assert len(insts["tencentTokenPlan"]) == 2
    assert len(insts["baiduQianfan"]) == 1


def test_load_instances_preserves_visibility_flag():
    insts = pusher.load_instances(FIXTURE_SETTINGS)
    assert insts["kimi"][0].is_visible is False
    assert insts["baiduQianfan"][0].is_visible is True


def test_load_instances_missing_settings_returns_empty():
    assert pusher.load_instances(FIXTURE_VIBEBAR / "nope.json") == {}


def test_load_instances_skips_malformed_entries(tmp_path: Path):
    bad = tmp_path / "settings.json"
    bad.write_text(
        json.dumps({"miscProviderInstances": [
            {"id": "ok", "tool": "kimi"},
            "not a dict",
            {"id": "", "tool": "mimo"},
            {"tool": "claude"},
        ]}),
        encoding="utf-8",
    )
    out = pusher.load_instances(bad)
    assert list(out.keys()) == ["kimi"]
    assert out["kimi"][0].instance_id == "ok"


def test_quota_file_for_instance_matches_misc_hash():
    """sha256('misc-' + instance_id) must address the real on-disk file."""
    p = pusher.quota_file_for_instance("openCodeGo", FIXTURE_QUOTAS)
    assert p is not None
    expected = hashlib.sha256(b"misc-openCodeGo").hexdigest()
    assert p.name == f"quota-v1-{expected}.json"


def test_quota_file_for_instance_missing_returns_none():
    assert pusher.quota_file_for_instance("does-not-exist", FIXTURE_QUOTAS) is None


# ---------------------------------------------------------------------------
# Primary-tool merging + staleness filter
# ---------------------------------------------------------------------------


def test_collect_primary_files_picks_up_all_tool_matches():
    files = pusher.collect_primary_files(FIXTURE_QUOTAS, "claude")
    # 2 quota-v1-* files match tool=claude in the fixture
    tools = {raw.get("tool") for _, raw in files}
    assert tools == {"claude"}
    assert len(files) >= 2


def test_merge_primary_files_prefers_freshest_with_full_fields():
    files = pusher.collect_primary_files(FIXTURE_QUOTAS, "claude")
    merged = pusher.merge_primary_files(files)
    assert merged["tool"] == "claude"
    bucket_ids = {b["id"] for b in merged["buckets"]}
    assert "five_hour" in bucket_ids
    five_hour = next(b for b in merged["buckets"] if b["id"] == "five_hour")
    assert "resetAt" in five_hour
    assert "rawWindowSeconds" in five_hour


def test_merge_primary_files_falls_back_to_older_bucket_if_fresh_lacks_resetAt():
    fresh = ("fresh.json", {
        "tool": "claude",
        "queriedAt": 1000,
        "buckets": [{"id": "weekly", "title": "Weekly", "usedPercent": 25}],
    })
    older_but_within_window = ("older.json", {
        "tool": "claude",
        "queriedAt": 500,  # 500s younger, well within 3600s staleness window
        "buckets": [{
            "id": "weekly", "title": "Weekly", "usedPercent": 30,
            "resetAt": 5000, "rawWindowSeconds": 604800,
        }],
    })
    merged = pusher.merge_primary_files([fresh, older_but_within_window])
    weekly = next(b for b in merged["buckets"] if b["id"] == "weekly")
    assert weekly["resetAt"] == 5000
    assert merged["queriedAt"] == 1000


def test_merge_primary_files_drops_stale_legacy_misc_file():
    """A file queried >1h before the freshest one is dropped wholesale.

    Real-world cause: vibe-bar migrated Gemini/Antigravity/Grok from misc to
    partial-primary, leaving the old `misc-<tool>` snapshot file on disk
    forever frozen at the pre-migration `queriedAt`. Its bucket id schema
    doesn't match the new writer's, so merging it bleeds stale buckets.
    """
    fresh = ("fresh.json", {
        "tool": "antigravity",
        "queriedAt": 801391019,  # current writer
        "buckets": [{"id": "gemini-3.5-flash-medium", "title": "Med", "usedPercent": 0}],
    })
    stale_legacy = ("legacy.json", {
        "tool": "antigravity",
        "queriedAt": 800989541,  # 5 days behind — pre-migration snapshot
        "buckets": [{
            "id": "antigravity.MODEL_PLACEHOLDER_M132", "title": "PH",
            "usedPercent": 0,
        }],
    })
    merged = pusher.merge_primary_files([fresh, stale_legacy])
    bucket_ids = {b["id"] for b in merged["buckets"]}
    assert "gemini-3.5-flash-medium" in bucket_ids
    # Stale legacy bucket id must NOT survive
    assert "antigravity.MODEL_PLACEHOLDER_M132" not in bucket_ids


def test_merge_primary_files_keeps_co_fresh_files():
    """Two files within the staleness window both contribute their buckets."""
    a = ("a.json", {
        "tool": "claude",
        "queriedAt": 1000,
        "buckets": [{"id": "five_hour", "title": "5 Hours", "usedPercent": 10, "resetAt": 9999}],
    })
    b = ("b.json", {
        "tool": "claude",
        "queriedAt": 900,  # 100s older, within 3600s window
        "buckets": [{"id": "weekly", "title": "Weekly", "usedPercent": 5, "resetAt": 9999}],
    })
    merged = pusher.merge_primary_files([a, b])
    bucket_ids = {x["id"] for x in merged["buckets"]}
    assert bucket_ids == {"five_hour", "weekly"}


# ---------------------------------------------------------------------------
# Card building
# ---------------------------------------------------------------------------


def test_build_provider_card_single_instance_keeps_bare_id():
    raw = load_fixture_json(
        "quotas/quota-v1-37def6683d2862dcfdc82a45bd3cf3fd41bf8921197af5beff000c9bca664ed3.json"
    )
    meta = pusher.lookup_meta("baiduQianfan")
    card = pusher.build_provider_card(
        meta, instance_meta=None, raw=raw, multi_instance=False,
    )
    assert card["id"] == "baidu-qianfan"
    assert card["display_name"] == "Qianfan"
    assert card["subtitle"] == "Coding Plan"
    assert card["metrics"]
    assert any("Weekly" == m["label"] for m in card["metrics"])


def test_build_provider_card_multi_instance_appends_compound_id():
    raw = load_fixture_json(
        "quotas/quota-v1-93a57b672d76c68db2ddbb6111712370006144f286392db878da735fc4244159.json"
    )
    meta = pusher.lookup_meta("openCodeGo")
    inst = pusher.InstanceMeta(
        tool="openCodeGo",
        instance_id="openCodeGo-e4bb8e3d-b4b8-4961-b6f3-c9580566f74f",
        display_name="Github",
        is_visible=True,
    )
    card = pusher.build_provider_card(meta, instance_meta=inst, raw=raw, multi_instance=True)
    assert card["id"] == "opencode-go::github"
    assert card["display_name"] == "OpenCode Go"
    assert card["subtitle"] == "Workspace · Github"


def test_build_provider_card_multi_uses_plan_when_no_display_name():
    raw = load_fixture_json(
        "quotas/quota-v1-07065a11422e6fa831c8a211dc17dd9e12c38f19b9c9e4fd10f33d050c74e735.json"
    )
    meta = pusher.lookup_meta("tencentTokenPlan")
    card = pusher.build_provider_card(meta, instance_meta=None, raw=raw, multi_instance=True)
    assert card["id"] == "tencent-token-plan::standard"
    assert "Standard" in card["subtitle"]


# ---------------------------------------------------------------------------
# End-to-end build_envelope against the fixture vibebar/
# ---------------------------------------------------------------------------


def test_build_envelope_against_fixture():
    payload = pusher.build_envelope(make_cfg())
    ids = [p["id"] for p in payload["providers"]]
    assert len(ids) == len(set(ids))  # no duplicate ids

    # All five primary / partial-primary tools collapse to exactly one card
    assert ids.count("claude-cli") == 1
    assert ids.count("codex-cli") == 1
    assert ids.count("gemini-cli") == 1
    assert ids.count("antigravity") == 1
    assert ids.count("grok") == 1

    # Single-instance misc tools keep bare ids
    assert "baidu-qianfan" in ids
    assert "alibaba-token-plan" in ids
    assert "minimax" in ids

    # Multi-instance misc tools fan out via compound ids
    assert "opencode-go::google" in ids
    assert "opencode-go::github" in ids
    assert "tencent-token-plan::generic" in ids
    assert "tencent-token-plan::hy" in ids

    # kimi instance is isVisible=false in fixture settings → must not appear
    assert not any(i == "kimi" or i.startswith("kimi::") for i in ids)


def test_build_envelope_grok_card_renders_with_metric():
    payload = pusher.build_envelope(make_cfg())
    grok = next(p for p in payload["providers"] if p["id"] == "grok")
    assert grok["display_name"] == "Grok"
    assert grok["subtitle"] == "xAI"
    assert len(grok["metrics"]) == 1
    assert grok["metrics"][0]["label"] == "Monthly"


def test_build_envelope_antigravity_drops_legacy_buckets():
    """Stale misc-antigravity buckets must not appear on the fresh card."""
    payload = pusher.build_envelope(make_cfg())
    antig = next(p for p in payload["providers"] if p["id"] == "antigravity")
    labels = {m["label"] for m in antig["metrics"]}
    # New writer's labels are the real model names; legacy MODEL_PLACEHOLDER_*
    # shortLabels should not survive into a metric label.
    assert all("MODEL_PLACEHOLDER" not in lab for lab in labels)


def test_build_envelope_multi_instance_carries_subtitle_label():
    payload = pusher.build_envelope(make_cfg())
    by_id = {p["id"]: p for p in payload["providers"]}
    assert by_id["opencode-go::google"]["subtitle"] == "Workspace · Google"
    assert by_id["opencode-go::github"]["subtitle"] == "Workspace · Github"
    assert by_id["tencent-token-plan::generic"]["subtitle"] == "Token Plan · Generic"
    assert by_id["tencent-token-plan::hy"]["subtitle"] == "Token Plan · Hy"


def test_build_envelope_legacy_fallback_when_no_settings(tmp_path: Path):
    """No settings.json → primary tools still merge; misc tools degrade to
    one-card-per-file using their bare PROVIDER_META ids (no compound ids)."""
    cfg = make_cfg(settings_path=tmp_path / "nope.json")
    payload = pusher.build_envelope(cfg)
    ids = [p["id"] for p in payload["providers"]]
    assert ids.count("claude-cli") == 1
    assert ids.count("antigravity") == 1
    assert ids.count("grok") == 1
    assert not any("::" in i for i in ids)


def test_build_envelope_missing_dir_raises(tmp_path: Path):
    cfg = make_cfg(quotas_dir=tmp_path / "nope")
    with pytest.raises(SystemExit):
        pusher.build_envelope(cfg)


# ---------------------------------------------------------------------------
# Config masking
# ---------------------------------------------------------------------------


def test_config_masked_token():
    cfg = make_cfg(api_token="abcdefghijklmnop")
    masked = cfg.masked_token()
    assert masked.startswith("abcd")
    assert masked.endswith("nop")
    assert "…" in masked
