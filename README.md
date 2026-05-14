# vibe-bar-api-pusher

A small local daemon that reads quota snapshots from
[vibe-bar](https://github.com/AstroQore/vibe-bar)'s on-disk store
(`~/.vibebar/quotas/`) and pushes them to a remote quotas dashboard API
on a schedule. Does not modify vibe-bar itself.

- **Source**: `~/.vibebar/quotas/*.json` (read-only)
- **Sink**: `PUT <your-dashboard>/api/quotas` (any compatible endpoint, see Payload below)
- **Schedule**: macOS launchd LaunchAgent, every 10 minutes

## Install

```bash
git clone https://github.com/AstroQore/vibe-bar-api-pusher.git
cd vibe-bar-api-pusher
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # fill VIBEBAR_API_URL and VIBEBAR_API_TOKEN
```

## Verify before scheduling

```bash
.venv/bin/python pusher.py --print-config   # check the env values
.venv/bin/python pusher.py --dry-run | jq . # see exactly what would be sent
.venv/bin/python pusher.py --ping           # send empty payload to test auth (this clears the dashboard)
.venv/bin/python pusher.py --once           # do a real push
```

## Schedule with launchd

Edit the three `/Users/YOUR_USERNAME/path/to/vibe-bar-api-pusher/...` paths
in `launchd/com.astroqore.vibebar-pusher.plist.example` to match where you
cloned the repo, then install it:

```bash
cp launchd/com.astroqore.vibebar-pusher.plist.example \
   ~/Library/LaunchAgents/com.astroqore.vibebar-pusher.plist
launchctl load ~/Library/LaunchAgents/com.astroqore.vibebar-pusher.plist

# Confirm it's running
launchctl list | grep vibebar
tail -f ~/Library/Logs/vibebar-pusher.log
```

Trigger a run immediately without waiting 10 minutes:

```bash
launchctl kickstart -k gui/$(id -u)/com.astroqore.vibebar-pusher
```

To stop and remove:

```bash
launchctl unload ~/Library/LaunchAgents/com.astroqore.vibebar-pusher.plist
rm ~/Library/LaunchAgents/com.astroqore.vibebar-pusher.plist
```

If you edit the plist (e.g. change `StartInterval`), unload and reload it.

## Subcommands

| Command | What it does |
|---------|--------------|
| `--once` | Read quotas → transform → PUT → exit. Used by launchd. |
| `--dry-run` | Read + transform, print payload to stdout. No HTTP. |
| `--ping` | PUT an empty `{"providers": []}` to verify URL + token. Clears dashboard until next `--once`. |
| `--print-config` | Print the effective `.env` (token is masked). |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or transient server/network failure (will retry next cycle) |
| 1 | `~/.vibebar/quotas/` does not exist — start vibe-bar first |
| 2 | API rejected the request (4xx). Check token / URL / payload shape. |
| 3 | `.env` is missing a required value |

## Payload schema

PUT body (the API also accepts POST as equivalent). Response on success:
`{"ok": true, "providers_received": <N>, "uploaded_at": <unix-float>}`.

```json
{
  "providers": [
    {
      "id": "claude-cli",
      "display_name": "Claude",
      "subtitle": "Claude Code CLI",
      "icon": "C",
      "updated_at": 1747304879,
      "error": null,
      "metrics": [
        {
          "label": "5 Hours",
          "percent_remaining": 98,
          "detail": null,
          "reset_text": "Resets in 2h 44m",
          "state": null
        }
      ]
    }
  ]
}
```

Notes:
- `percent_remaining` is **remaining** percent (0=empty, 100=full), not used.
- `reset_text` is a free-form string; the pusher formats it.
- Whatever you PUT is the full picture — providers omitted from the payload disappear from the dashboard.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest tests/
```
