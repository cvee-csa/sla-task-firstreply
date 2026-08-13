# sla-task-firstreply

Local automation that watches the Cloud Security Alliance IT-Operations Zendesk queue for tickets awaiting a first reply, drafts a fixed acknowledgment template for each one, and lets you review and edit every draft, then post it as a public reply or an internal-only comment, before anything is ever posted. Runs entirely on this machine via `launchd` — no dependency on a cloud scheduler or a live Cowork session for day-to-day operation.

## How it works

Two independent `launchd` services, plus a shared JSON file between them:

- **`scripts/first_reply_check.py`** — runs every 2 minutes during business hours (Mon–Fri, 9am–4pm local), looking back up to 72 hours (`ZENDESK_LOOKBACK_HOURS`) for candidate tickets, so a stretch with this Mac asleep or shut down doesn't create a blind spot. Pulls recent IT-Operations tickets from Zendesk, filters to ones with no public reply yet, drafts the acknowledgment text, and adds new candidates to `scripts/output/pending_drafts.json`. It never posts anything to Zendesk — detection and drafting only. Fires a native macOS notification (with a sound, `ZENDESK_NOTIFICATION_SOUND`, default `Glass`) when something new shows up, or when the check itself is broken (throttled to once an hour). With [terminal-notifier](https://github.com/julienXX/terminal-notifier) installed (`brew install terminal-notifier`), the new-ticket notification also shows the CSA logo as its icon and is click-through (jumps straight to the review page); without it, notifications still fire and play sound, just via a plain AppleScript banner with the default icon and no click-through.
- **`scripts/review_server.py`** — a small local-only web server at **http://127.0.0.1:8765/**, kept running continuously (`KeepAlive`). This is the only piece that can actually post a comment to Zendesk, and only when you click **Public Reply** (visible to the requester) or **Internal Comment** (agent-only) on a specific ticket, optionally after editing the draft text yourself. There's no separate reject/dismiss action — every candidate stays on the page until you post it one way or the other. Also has a manual **Refresh from Zendesk now** button (runs the same detection logic on demand, bypassing the business-hours check) and a self-service **OAuth reconnect** flow at `/reconnect` for when the Zendesk token needs renewing.

Both scripts share config and logic through **`scripts/_common.py`**, so the two can't quietly drift apart — a real bug earlier in this project's life was caused by duplicated logic doing the same thing two slightly different ways.

## Setup

**Prerequisites:** the sibling `zendesk-mcp-server` repo checked out with its own `.venv`, with `zendesk_mcp_server` installed into it (`pip install -e .`) alongside `zenpy`, `requests`, and `python-dotenv`. This repo's `launchd` plists point at that venv's Python directly rather than duplicating it.

**Config:** copy `.env.example` from `zendesk-mcp-server` as a starting point, or use this repo's own `.env` (already git-ignored, never committed). One field is deliberately unusual: `ZENDESK_OAUTH_TOKEN_PATH` here is set to an **absolute path** pointing back at `zendesk-mcp-server/.zendesk_oauth_tokens.json`, not a local copy — that file is also read and refreshed by `zendesk-mcp-server`'s own MCP server (the live Zendesk connector used in Cowork sessions), so both consumers deliberately share one token file rather than risking two independently-refreshing copies drifting apart.

A few more optional knobs, all with sensible defaults so you only need to touch them if you want something different: `ZENDESK_REVIEW_PORT` (default `8765`), `ZENDESK_NOTIFICATION_SOUND` (default `Glass` — any name from `/System/Library/Sounds`, or empty for a silent notification), and `ZENDESK_LOOKBACK_HOURS` (default `72`).

**Install the services:**

```
cp scripts/com.csa.zendesk-first-reply-check.plist ~/Library/LaunchAgents/
cp scripts/com.csa.zendesk-review-server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.csa.zendesk-first-reply-check.plist
launchctl load ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist
```

`scripts/reload_launchd.sh` does the copy/reload steps for you (safe to re-run) — handy after pulling changes to this repo.

**After changing any script:** unload and reload the relevant plist so `launchd` picks up the new code:

```
launchctl unload ~/Library/LaunchAgents/com.csa.zendesk-first-reply-check.plist
launchctl load ~/Library/LaunchAgents/com.csa.zendesk-first-reply-check.plist
```

## Day to day

Open **http://127.0.0.1:8765/** any time to see what's waiting on you. Each card shows the ticket subject (badged **TEST** if it's a `[Test]`/`[Tooling]`-tagged ticket), a resolved requester name (resolved directly from Zendesk by id where possible, with a flag explaining why if it couldn't be confirmed — worth reading before posting, since first-name collisions are real), a short excerpt of what the person actually asked, and an editable draft (the greeting rotates between a few variants so replies don't all open identically). Pick **Public Reply** to post the draft where the requester sees it, or **Internal Comment** to leave it as an agent-only note instead. Since a posted ticket's card disappears from the list, a confirmation banner echoes back exactly what was sent — public or internal, plus the text itself — so that's the record of what happened, not just a generic "done." A status bar up top shows pending/posted-today counts and when Zendesk was last checked.

If the Zendesk connection needs attention (expired token, etc.), a red banner appears with a **Reconnect now** link — click it, approve access in the browser tab it opens, then paste the URL you get redirected to back into the page. No Cowork session required for this.

## Design principles

- **Nothing posts without a human clicking Public Reply or Internal Comment.** The cron script can only read and draft; only the review server can write, and only on an explicit click.
- **Every candidate stays on the page until you act on it.** There's no reject/dismiss action — post it publicly, post it as an internal note, or leave it pending.
- **A failed post leaves the draft intact.** Nothing is lost if Zendesk is briefly unreachable or the token needs reconnecting.
- **No bulk actions.** Every reply gets individually reviewed — a "select all, post" button would defeat the point of the review gate.
- **Localhost only, no login.** The review server binds to `127.0.0.1` and is never reachable off this Mac. There's no additional auth layer on top of "you're logged into this machine," which is an intentional tradeoff for a single-user local tool, not an oversight.

## Repo layout

```
scripts/
  _common.py                                  shared config, pending-store, and scan logic
  first_reply_check.py                        the 2-minute detect-and-draft cron job
  review_server.py                            the review/approve web server + OAuth reconnect
  com.csa.zendesk-first-reply-check.plist      launchd config for the cron job
  com.csa.zendesk-review-server.plist          launchd config for the web server
  reload_launchd.sh                           convenience script to (re)install both services
  assets/csa-logo.png                          logo shown on the review page and (via terminal-notifier) the notification icon
  output/                                      logs and runtime state (git-ignored)
```

## Troubleshooting

- `scripts/output/first_reply_check.log` — the cron job's own log, one line per run.
- `scripts/output/review_server.out.log` / `review_server.err.log` — the web server's log (`launchd`'s stdout/stderr capture).
- If the page won't load, confirm the service is actually running: `launchctl list | grep com.csa`.
- If tickets aren't being detected, check the cron log for "not authenticated" — that's the OAuth token, fixable via the in-page `/reconnect` flow.
