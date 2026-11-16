# sla-task-firstreply

Local automation that watches an IT-Operations Zendesk queue for tickets awaiting a first reply, drafts a fixed acknowledgment template for each, and lets you review and edit every draft, then post it as a public reply or an internal-only comment. Nothing is posted without your click. It runs entirely on your own Mac via `launchd`, with no dependency on a cloud scheduler or an always-open session.

> The full, unabridged documentation (per-feature history and design rationale) is kept in [`docs/DESIGN.md`](docs/DESIGN.md). This README is the condensed version.

## How it works

Two main `launchd` services share a JSON file, and a third job watches them:

- **`scripts/first_reply_check.py`** runs every 2 minutes, Mon–Fri 9am–4pm local, and looks back up to 72 hours (`ZENDESK_LOOKBACK_HOURS`) so a sleeping Mac does not create a blind spot. It pulls recent IT-Operations tickets, keeps those with no public reply yet, drafts the acknowledgment text, and adds new candidates to `scripts/output/pending_drafts.json`. It never posts to Zendesk. It sends a macOS notification, prefixed "CSA", with a sound (`ZENDESK_NOTIFICATION_SOUND`, default `Glass`) when something new appears, or when the check itself is broken (throttled to once an hour).

  Notifications go out through up to three independent paths every time: [terminal-notifier](https://github.com/julienXX/terminal-notifier) (if installed; its banner is click-through to the review page), a plain AppleScript banner, and the optional helper `.app` below. The redundancy is deliberate: on the Mac this was built on, neither terminal-notifier nor `osascript` reliably showed a banner when `launchd` triggered them, even though both exit cleanly, because macOS treats a headless `launchd` process and an interactive Terminal command as differently permissioned senders. The helper app is the path most likely to work, since real `.app` bundles get their own row in Notification settings. Every attempt's exit code and error text is logged to `scripts/output/first_reply_check.log`; a permission-denied message there points at System Settings → Notifications, not at this code.
- **`scripts/review_server.py`** is a local-only web server at **http://127.0.0.1:8765/**, kept running (`KeepAlive`). It is the only piece that can post a comment to Zendesk, and only when you click **Public Reply** (visible to the requester) or **Internal Comment** (agent-only) on a ticket, optionally after editing the draft. It also has a manual **Refresh data** button (runs the detection logic on demand, bypassing business hours) and a self-service OAuth reconnect flow at `/reconnect`.
- **`scripts/watchdog.py`** is re-run by `launchd` every 5 minutes and alerts if the review server stops answering. It checks the lightweight `/healthz` route, not a Zendesk-backed page.

All scripts share config and logic through **`scripts/_common.py`**, so they can't quietly drift apart (an earlier bug came from duplicated logic).

## Setup

macOS only: the services run under `launchd`, and notifications use macOS facilities. Plan on 20 to 30 minutes, most of it creating the Zendesk credentials.

### Prerequisites

Everything in this list has to exist before the Quick start works.

1. **macOS with Python 3.10 or newer.** The services were built and run on 3.12; CI runs 3.10. Check with `python3 --version`.
2. **The sibling `zendesk-mcp-server` project**, a separate repository that supplies the Zendesk client this tool imports (`zendesk_mcp_server`). It is not on PyPI, so it has to be cloned from `https://github.com/cvee-csa/zendeskmcp` (the tool uses the `zendesk-mcp-server` folder inside that repository) and given its own virtualenv. Put it next to this repo, as `../zendeskmcp/zendesk-mcp-server` or `../zendesk-mcp-server`, or anywhere else and tell the installer where (`ZENDESK_MCP_DIR`, Quick start step 4). From inside its folder:

   ```
   python3 -m venv .venv
   .venv/bin/python3 -m pip install -e .
   ```

   Every service runs on that venv's Python, which is why the next Quick start step installs into it.
3. **A Zendesk agent account** for the person this tool acts as ("Me" in the UI), plus access to Zendesk **Admin Center** (or an admin who can hand you the values below):
   - an **API token**: Admin Center → Apps and integrations → APIs → Zendesk API → Settings → enable token access → add a token (the value is shown once);
   - an **OAuth client**: the same page, **OAuth Clients** tab → add. Set the client kind to *Confidential* and add the redirect URL `https://localhost/callback` (the default `ZENDESK_REDIRECT_URI`; if a different URL is registered, set `ZENDESK_REDIRECT_URI` to the same string). Zendesk shows the client secret once, on save. The client's *Unique identifier* is `ZENDESK_CLIENT_ID`.

   - the numeric **IDs of two groups** (queues): the one whose tickets get a first-reply draft (IT-Operations), and the one shown read-only on the Security page (IT-Security). Open each group in Admin Center → People → Teams → Groups and read the number at the end of the URL. They are set in `.env` in Quick start step 3.

   Zendesk's admin screens change from time to time; if a menu name differs, search Zendesk's documentation for "API token" and "OAuth client".
4. **Optional, for `/projects`:** access to the Airtable base "IT Operations Projects (2026 Reset)" and permission to create a personal access token for it. The steps are in "Airtable setup" below.
5. **Optional:** [terminal-notifier](https://github.com/julienXX/terminal-notifier) (`brew install terminal-notifier`) for click-through notification banners, and the sibling repos for `/sla` and `/standup` (see below).

### Quick start

1. **Clone this repo** anywhere you like. Cloning to `~/Desktop/git/sla-task-firstreply` is the one location the optional notification helper app assumes (see its section).

   ```
   git clone https://github.com/cvee-csa/sla-task-firstreply.git
   cd sla-task-firstreply
   ```

2. **Install this repo's one runtime dependency into the zendesk-mcp-server venv**, because the services run on that venv's Python (replace the path with your checkout):

   ```
   /path/to/zendesk-mcp-server/.venv/bin/python3 -m pip install -r requirements.txt
   ```

3. **Create `.env`** from the committed template and fill in the required values (table below: five Zendesk credentials, plus the two group ids unless this is the original team's account). `.env` is git-ignored; never commit it.

   ```
   cp .env.example .env
   ```

   | Variable | Required | Where it comes from |
   |---|---|---|
   | `ZENDESK_SUBDOMAIN` | yes | The part before `.zendesk.com` in your Zendesk URL |
   | `ZENDESK_EMAIL` | yes | The agent account this tool acts as |
   | `ZENDESK_API_KEY` | yes | The API token from Prerequisites step 3 |
   | `ZENDESK_CLIENT_ID` | yes | The OAuth client's *Unique identifier* |
   | `ZENDESK_CLIENT_SECRET` | yes | The OAuth client's secret |
   | `ZENDESK_IT_OPERATIONS_GROUP_ID` | yes, unless this is the original team's Zendesk | The group id of the queue to watch (Prerequisites step 3). Unset, it defaults to the original team's group, which on any other Zendesk account matches nothing |
   | `ZENDESK_IT_SECURITY_GROUP_ID` | yes, same condition | The group id of the queue shown on the Security page. Left unset on another account, that page shows nothing useful |
   | `ZENDESK_REDIRECT_URI` | no | Default `https://localhost/callback`; must match a registered redirect URL exactly |
   | `ZENDESK_OAUTH_TOKEN_PATH` | no | Default `.zendesk_oauth_tokens.json` in this repo's root (see Config) |

   The ids are digits only; a malformed value stops the services at startup with a message naming the variable (see `review_server.err.log`). The acknowledgment wording ("someone from our IT Operations team will follow up…") is fixed text in `build_drafts` in `scripts/_common.py`; edit it there for another team. Also review `STANDUP_PEOPLE`, `STANDUP_TRACKED_PERSON` and `OVERVIEW_PEOPLE` now. Their built-in defaults are the original team's names, so on another team the standup owner filter and the `/overview` cards show the wrong people until these are set (table below).

4. **Check, then install and start the three services:**

   ```
   zsh scripts/reload_launchd.sh --check
   zsh scripts/reload_launchd.sh
   ```

   `--check` finds the zendesk-mcp-server checkout, confirms its venv can import the Zendesk client, and confirms `.env` exists, without installing anything. The installer works out every path from where it sits, so no files need editing for a different folder or user account. If the checkout is not next to this repo, prefix both commands with `ZENDESK_MCP_DIR=/path/to/zendesk-mcp-server`. A `WARNING` about an import failure means step 2 or the editable install in Prerequisites step 2 is incomplete.

5. **Verify the services are up:**

   ```
   curl http://127.0.0.1:8765/healthz      # prints: ok
   launchctl list | grep com.csa           # three rows; a number in the first column means running
   tail scripts/output/review_server.err.log scripts/output/launchd.err.log
   ```

   Empty or absent error logs are the good case. If `healthz` does not answer after about ten seconds, read `review_server.err.log` and see Troubleshooting.

6. **Open http://127.0.0.1:8765/ and connect Zendesk.** If an amber banner says the connection needs attention, click **Reconnect now**, approve access in the tab it opens, then paste the redirect URL back into the page. The tab that opens at `https://localhost/callback` is expected to show a connection error; its address bar is what gets pasted. Tickets are detected on the next 2-minute run (Mon–Fri, 9am–4pm local), or click **Refresh data** to scan now.

7. **Optional:** the notification helper app, and the sibling repos for `/sla` and `/standup`, are described below.

**Config:** `.env` lives in this repo's root (copy `.env.example`). One field is unusual: `ZENDESK_OAUTH_TOKEN_PATH`. Left unset, the OAuth tokens are stored in `.zendesk_oauth_tokens.json` in this repo's root. Set it to an **absolute path** only when `zendesk-mcp-server` is also used on its own and should share one token file (it refreshes the file too, and a single shared copy avoids two copies drifting apart).

Optional knobs, all with defaults:

| Variable | Default | Purpose |
|---|---|---|
| `ZENDESK_REVIEW_PORT` | `8765` | Review server port |
| `ZENDESK_NOTIFICATION_SOUND` | `Glass` | Any name from `/System/Library/Sounds`, or empty for silent |
| `ZENDESK_LOOKBACK_HOURS` | `72` | Lookback window |
| `ZENDESK_NOTIFIER_APP_PATH` | `~/Applications/CSA Notifier.app` | Helper app location |
| `STANDUP_PEOPLE` | `Catherine,Jesstin,Nekoashi,Ryan` | The only people offered in the `/standup` owner filter and the only owners whose items can be added to My tasks, spelled as the notes spell them (case-insensitive); others in the notes still appear under Everyone |
| `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID`, `AIRTABLE_VIEW_ID` | (unset) | The Airtable base, table and view behind `/projects`; the token needs `data.records:read` and `data.records:write` on that base only. Without the first three, `/projects` shows setup instructions |
| `PROJECTS_ASSIGNEES` | (unset) | `Name:email,Name:email`: the people offered as Assignee on `/projects` (Airtable's API cannot list collaborators) |
| `STANDUP_TRACKED_PERSON` | `Ryan` | Whose standup updates the third `/standup` tab lists (also the tab's label); case-insensitive |
| `OVERVIEW_PEOPLE` | `Catherine Vee,Jesstin Rojas,Nekoashi Wolf,Ryan Bergsma` | Cards on `/overview`, in order; the first is you. Also the people offered for tagging and for the My tasks Assignee/Requester |
| `SLA_HISTORY_FILE` | `~/Desktop/git/sla-report-slack/data/sla_history.json` | Where `/sla` reads its daily snapshots (read-only) |
| `SLA_RESOLUTION_HRS` | `40` | Business-hour limit for the resolution-age rule (5 business days x 8 h) |
| `SLA_TICKET_SCAN_PAGES` | `10` | Pages of 100 tickets the live SLA scan reads before noting the queue may be larger |
| `IT_SECURITY_COLUMN_LIMIT` | `5` | Recent IT-Security tickets on `/security` (the name predates that page moving; kept so old `.env` files still work) |

**My tasks, Assignee/Requester:** these two selects offer only the people in `OVERVIEW_PEOPLE` (by default Catherine Vee, shown as "Me", plus Jesstin Rojas, Nekoashi Wolf and Ryan Bergsma). "Me" is this tool's own account (`ZENDESK_EMAIL`); each other person is looked up in Zendesk by their exact display name (`_common.resolve_personal_task_identity`), so a name that matches no Zendesk user fails with a clear on-page error instead of falling back silently. Both default to "Me". There is no "IT-Operations" option, and `ZENDESK_IT_OPERATIONS_EMAIL` is no longer used.

**`/sla`:** needs the sibling `sla-report-slack` repo cloned at `~/Desktop/git/sla-report-slack` (or `SLA_HISTORY_FILE` pointed at its `data/sla_history.json`). This tool never pulls, writes or commits there. **Update history** on the `/sla` page runs `git fetch` in that repo (plus `git show`, to read the file out of `origin/main`) and saves the newer copy under this tool's own `scripts/output/sla_history_remote.json`, so it needs this Mac's git to reach GitHub without a prompt, the same access a `git pull` in Terminal uses (a service started by launchd can reach the login keychain while you are logged in; if it cannot, the page says what git reported). Run `git pull` in that repo to bring the checkout itself up to date. Without the file, `/sla` says so and the rest of the tool keeps working.

**`/standup`:** no setup if the private `meetingnotestool` repo is cloned at `~/Desktop/git/meetingnotestool`. This tool only reads that project's built `dist/standup_data.json` and writes nothing there. Set `STANDUP_DATA_FILE` in `.env` if it lives elsewhere. Without the file, `/standup` explains why and every other page keeps working.

### Airtable setup (optional, for `/projects`)

Without this the Projects page only says what is missing; every other page works. Do it once:

1. **Find the three IDs.** Open the Projects table in Airtable in a browser. The address looks like `https://airtable.com/appXXXXXXXXXXXXXX/tblYYYYYYYYYYYYYY/viwZZZZZZZZZZZZZZ`. The part starting `app` is `AIRTABLE_BASE_ID`, `tbl` is `AIRTABLE_TABLE_ID`, and `viw` is `AIRTABLE_VIEW_ID` (the view is optional; without it every record is read).
2. **Create a personal access token yourself** at `https://airtable.com/create/tokens`: give it a name, add the scopes `data.records:read` and `data.records:write` (the write scope also covers creating and deleting records, which the Diagnostics write test uses), and under Access add only the one base. Airtable shows the token once.
3. **Put the values in `.env`** (`AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID`, optionally `AIRTABLE_VIEW_ID`), typing or pasting them yourself. The token goes nowhere else: not into chat, a ticket, a commit or a screenshot. Keep the file private with `chmod 600 .env`.
4. **List the people who can be assigned** in `PROJECTS_ASSIGNEES` as `Name:email,Name:email` (Airtable's API cannot list a base's collaborators). Use each person's Airtable account email.
5. **Restart the review server** (see "Restarting just the review server" under Troubleshooting). Settings are read at start-up.
6. **Check it.** Open `http://127.0.0.1:8765/diagnostics`, press **Test Airtable connection** (one read, writes nothing), then optionally **Run write test** (creates, edits and deletes one temporary record named "[Smoke test] ...", after a confirmation step). A 401 or 403 means the token, its scopes or its base access is wrong; a 404 means a wrong base or table ID.

### Notification helper app (one-time, optional but recommended)

A small Automator application gives macOS a real notification sender to track. About five minutes of GUI setup:

1. Open **Automator**, choose **File → New → Application**.
2. Add a **Run AppleScript** action and replace its placeholder code with the contents of `scripts/csa_notifier.applescript`. The script builds `payloadPath` from your home folder plus `Desktop/git/sla-task-firstreply/scripts/output/notify_payload.json`; if this repo is cloned somewhere else, edit the part after the home folder on the `payloadPath` line near the top.
3. **File → Save** as `CSA Notifier` in `~/Applications` (the default `ZENDESK_NOTIFIER_APP_PATH`; set that variable if you save elsewhere).
4. Double-click `CSA Notifier.app` once. A "CSA Notifier — Ready" banner should appear; approve notifications if prompted, then in System Settings → Notifications confirm "CSA Notifier" is allowed with an alert style other than "None".

`first_reply_check.py` finds the app at that path automatically. Without it, the tool still works but falls back to terminal-notifier/osascript, which may not display anything from a headless context.

### Install the services

`scripts/reload_launchd.sh` is the install step and the reload step. It resolves this repo's location from its own, finds the `zendesk-mcp-server` checkout (`$ZENDESK_MCP_DIR`, else a sibling folder), copies all three plists (first-reply check, review server, watchdog) into `~/Library/LaunchAgents/` with those paths filled in, and (re)loads each with `launchctl bootout` then `bootstrap`. The plists in the repo keep the original author's paths and are never modified; only the copies in `~/Library/LaunchAgents/` are rewritten, so a plist edited by hand there is overwritten on the next run. It also creates `scripts/output/`, which is git-ignored and which `launchd` needs for the log files. It is safe to re-run, so run it again after pulling changes:

```
zsh scripts/reload_launchd.sh            # install / reload
zsh scripts/reload_launchd.sh --check    # resolve paths and run the prerequisite checks only
```

The watchdog deliberately avoids the zendesk-mcp-server venv (so it keeps working when that venv breaks). Its interpreter is the one in the committed plist when that exists on the machine, otherwise the real interpreter underneath the venv. Set `WATCHDOG_PYTHON=/path/to/python3` to choose another one.

To do one service by hand, take its generated plist from `~/Library/LaunchAgents/` (run the installer once first, because the copy in the repo still has the original author's paths) and register it, using `bootstrap` rather than the older `launchctl load`, which can silently leave an old definition running:

```
launchctl bootout gui/$(id -u)/com.csa.zendesk-review-server 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist
```

After changing a script, reload it the same way. For just the review server, see the restart command under Troubleshooting.

## Day to day

Open **http://127.0.0.1:8765/** to see what's waiting on you. The header nav links to every page below; the active page is highlighted. The links carry no count badges.

### The main queue (`/firstreply`)

Each card shows:

- the subject, badged **TEST** for `[Test]`/`[Tooling]` tickets and **HIGH**/**URGENT** when Zendesk's priority field says so (normal/low are not badged);
- a requester name resolved from Zendesk by id, with a flag explaining why if it couldn't be confirmed (worth reading before posting, since first-name collisions are real);
- the requester's full original text (long text scrolls in its own box rather than being cut off);
- an editable draft that opens with "Hey _name_!" ("Hey there!" when no name could be confirmed).

Pick **Public Reply** (requester sees it), **Internal Comment** (agent-only), **Claim** (assign the ticket to you), or **Remove** (drops it from the queue without posting; confirmation required). A removed ticket is never re-detected, even if Zendesk still shows no reply, so handle it in Zendesk if it still needs one. **⌘Enter** / **Ctrl+Enter** posts the draft as a Public Reply.

**Tagging teammates.** Under the draft, a **Tag on internal comment** row lists everyone in `OVERVIEW_PEOPLE` except you. Ticked people are @mentioned at the top of an **Internal Comment**, the same way Zendesk's own composer does it (a `class="atmention"` link to the person's user id, found by exact display name), which is how a teammate gets pulled onto a ticket. After posting, the comment is read back and the banner says whether Zendesk kept each mention; if a person has no matching Zendesk user, nothing is posted. Tags are ignored on a **Public Reply** and the banner says so: Zendesk can only mention agents (never the requester), and a mention in a public reply would CC the agent where the requester can see it. The requester's name in the greeting is plain text, not a mention.

Claiming an unassigned ticket happens immediately; claiming one assigned to a colleague asks you to confirm first. After a Claim you land back on the page you clicked it from.

**Someone else already replied.** Two checks guard against posting over another reply. Every scan (cron tick or manual Refresh) re-checks all pending tickets' comments and pulls any that someone else has answered, with a notification. Then, when you click Public Reply or Internal Comment, the server re-checks once more. If a reply exists at that point, it shows a confirmation page with **Yes, post anyway** and **Remove instead (don't post)**; navigating away leaves the ticket untouched. Both checks fail open on a transient API error, and both count a reply from this tool's own Zendesk account as a reply, which matters on self-created test tickets where requester and replier are the same person.

**Someone else is assigned.** If a pending ticket's Assignee is anyone other than this tool's account, the card shows an amber "Also being worked on in Zendesk — currently assigned to _name_" notice and an empty draft box, so nothing goes out on autopilot over a colleague's ticket. This is informational and gates nothing. The stored draft is untouched and reappears if the assignee is cleared. The assignee is refreshed on every scan.

**Feedback and history.** After each action, a confirmation banner (a toast that fades on its own) echoes what was sent, or why nothing was, with the ticket URL as a clickable link; it has an **Undo** where the action is reversible. Buttons disable and relabel themselves ("Posting…") while a request runs. A collapsed **Replied today (N)** section lists what you sent through this tool today (Removed and auto-pulled tickets are excluded). A **Recently worked on (30)** column shows your most recent replies regardless of day, including replies you sent directly in Zendesk. That merge scans the IT-Operations group within a per-load budget (`WORKED_ON_COMMENT_CHECK_LIMIT`, default 40); a small note appears if Zendesk is unreachable or the budget ran out, so a gap reads as "might be missing something". A ticket already in local history is never re-checked, so a later direct-in-Zendesk reply on it won't move it. Open/closed state of these sections is remembered per browser.

**Refresh and countdowns.** The page reloads itself every 60 seconds as a local re-render of the pending file (not a Zendesk scan), skipping the reload if you're mid-edit in a textarea. The status text at the very bottom of the sidebar, below the Background control, shows the posted-today count plus live "Last checked" and "Pending Refresh" values (each on its own line); "Pending Refresh" is exact outside business hours (a resume time) and a rough `~` estimate otherwise. If the Zendesk connection needs attention, an amber banner offers **Reconnect now**: approve access in the tab it opens, then paste the redirect URL back into the page.

Tickets detected before the full-text change keep their old truncated "..." text until posted.

### Look and feel

The page follows your Mac's light/dark setting. A header control overrides it, left to right: **Abyss / Galaxy / Dark / Tomorrow Blue / Lesbian pride / Halloween / Christmas**. Each button is a line icon (anchor, orbit, moon with star, terminal, cloud with moon, ghost, snowflake); hover for the theme's name. The choice is remembered per browser (`localStorage`, not written to any tracked file). There is no longer an Auto button: with nothing saved, the page follows the Mac's light/dark setting, and a browser that had saved Light, Auto or B68FBF is back on that default after this change. To return to the default after picking a theme, clear the site's data for `127.0.0.1:8765` (or run `localStorage.removeItem('csa-theme')` in the browser console).

**About This Tool.** The pill at the top-right of every page opens a short panel on where the data comes from: live Zendesk reads (cached about 2 minutes), the 2-minute first-reply check and its local pending file, the `sla-report-slack` history behind the SLA charts (with an Update history button that fetches newer snapshots from GitHub into this tool's own folder), the `meetingnotestool` standup view, local My tasks drafts, and what the browser keeps (theme and Background image). It also says that Zendesk is written to only on your click, and that Airtable is written to only from the Projects page, after a review step (plus the Diagnostics write test you start yourself). It links to the Diagnostics page. The text is static (`_ABOUT_TOOL_HTML` in `review_server.py`); update it when a data source changes. Escape or a click outside closes it.

**Background image.** The **Background** control under the theme toggle sets an image as the page background. **Collapse menu** in the same panel closes that panel and nothing else (it does not hide the sidebar). **Choose image…** takes any image file, scales it down to at most 1920px on its long edge, and keeps it as a JPEG in this browser's `localStorage` (a very large image that won't fit is applied to the current page only, with a note). **Fade** sets how much of the theme's own color covers the image (0 to 95%, default 60%) so text stays readable in every theme, and **Remove** clears it. While an image is set the interface switches to a liquid-glass look: cards, board columns, filter boxes, inputs and chips become translucent, blurred panels with a bright top edge, rounder corners and a soft shadow, all built from the active theme's own colors, and loose headings get a soft halo for contrast. Browsers without `backdrop-filter` keep the solid cards. With no image, nothing changes. After choosing an image, a notification confirms it. The image is read back from storage first, so the message is accurate: when it was saved, it says the image stays in this browser and does not need to be uploaded again after the page is closed (clearing the browser's site data, or a private window, removes it); when it could not be saved, a red notification says it will need to be uploaded again if the page is closed or reloaded. Removing the image shows a notification too. Nothing is uploaded or written to disk by the server; another browser starts without the image. The image is applied in `<head>` before first paint, so it doesn't flash in late.
- **Abyss** is VS Code's Abyss palette: a deep navy ground (`#000c18`), `#060621` panels, the editor's `#6688cc` blue for muted text (body text is a lighter tint of it, for contrast) and Abyss gold (`#ddbb88`) as the accent. The button hues are darkened Abyss token colors (badge blue, purple, slate, rose, gold). The Abyss values were written from memory of the VS Code theme and could not be checked against the source file, so small differences are possible.
- **Tomorrow Blue** is VS Code's built-in Tomorrow Night Blue: `#002451` ground, the Tomorrow pastels (`#bbdaff` blue as the accent, plus purple, aqua, red and orange) darkened for the button fills so white text stays readable. The background, selection (`#003f8e`) and ANSI-style palette were confirmed against a published copy of the theme.
- **Lesbian pride** is a soft, light theme: a page wash running through the flag's seven bands (orange tints at the top, white in the middle, rose and magenta tints at the bottom), warm-white cards, and the flag's own colors (`#d52d00`, `#ef7627`, `#ff9a56`, `#ffffff`, `#d162a4`, `#b55690`, `#a30262`) as tints and as deepened shades on text and buttons. It is the only light named theme and sets `color-scheme: light`. Its toggle button is a cloud-and-moon icon. In the wide layout the same empty sidebar stretch the bats use (`#side-sky`) holds a canvas of soft clouds drifting slowly across it, with small pastel hearts and petals (the flag's oranges and pinks) floating up and swaying. Same rules as the other effects: only while this theme is active, paused while the tab is hidden, a still frame with reduced motion, nothing drawn when there is no spare room.
- **Halloween** (near-black, pumpkin-orange accent, purple secondary buttons) and **Christmas** (deep pine-black, bright green accent, cranberry secondary buttons) are seasonal dark themes. Christmas is the only place red/green appears in the buttons. Christmas also has falling snow: one fixed canvas (about 40 to 160 white flakes, scaled to the window) that ignores the mouse and sits above the page but below the toast and loading overlay. It runs only while Christmas is active, pauses while the tab is hidden, and is not shown when the OS asks for reduced motion. Halloween has bats: in the wide layout, the empty stretch of the sidebar between the nav and the footer (`#side-sky`) holds a canvas where up to four small orange-rimmed bats flap across at different heights and speeds, leave one side and return a few seconds later. Same rules as the snow: only while Halloween is active, paused while the tab is hidden, and with reduced motion a still row of bats is drawn instead. Nothing shows on narrow windows, short windows with no spare room, or with the menu collapsed.
- **Galaxy** is a dark theme built around a galaxy photo (`scripts/assets/galaxy-bg.jpg`, 1280px wide, about 270 KB, served by the review server at `/assets/galaxy-bg.jpg`). The photo is the page background, under a dark veil, and the liquid-glass panels described above are always on in this theme. The colors come from the photo: blue-black space, the coral star-forming lanes as the accent (with dark text on the active pills), and teal, blue, violet, slate, coral and orange hues for the action buttons. Choosing your own background image with **Background** takes priority over the galaxy photo. The credit line under the Background control is shown only while the photo is the background.
- **Image credit:** NGC 7253 and Arp 278, Gemini North. Image: International Gemini Observatory / NOIRLab / NSF / AURA. Processing: J. Miller and M. Rodriguez (International Gemini Observatory / NSF NOIRLab), T.A. Rector (University of Alaska Anchorage / NSF NOIRLab), D. de Martin and M. Zamani (NSF NOIRLab). Licensed [CC BY 4.0](https://noirlab.edu/public/copyright/); seen via NASA's Astronomy Picture of the Day, 2026-09-04. Keep the credit if the image is reused, and do not use it to imply NOIRLab endorses anything.
- Every other theme avoids the red/green traffic-light convention: each action has its own hue, and the connection warning is amber. All themes leave the amber warning/alert colors unchanged, since those are functional signals.

Layout: on wide screens the header is a left sidebar (theme toggle, nav, then the title with the pending badge, the Zendesk status button, Refresh, Background and the status text at the bottom); on narrow screens it is a card at the top. A frosted-glass blur falls back to a solid background where `backdrop-filter` is unsupported. Every action has a small icon, and only one action per page is a solid button (Public Reply on the queue; the reconnect buttons on `/reconnect`), with the rest outlined. Cards have a "TICKET" label, a flat default look with a hover lift, and a colored initials bubble next to any requester/assignee name the tool is confident in (never next to a guess). The tab title is "Workflow Asset", prefixed with the pending count, e.g. "(3) Workflow Asset".

**Loading indicator.** The thin bar at the top of the window shows real progress for navigations you start. If a load passes about 1.5 seconds, the content dims slightly and a centered card shows a progress ring with the percent in the middle, beside the destination name and the actual request count ("Loading Tasks… 3 of 8 requests"). The ring is a plain arc (no stripes). Routes with no baseline yet show a short arc spinning around the ring instead of a number. In the Halloween, Lesbian pride and Christmas themes (and only those) the card also gets a theme look, with a small sprite riding the arc's leading end: an orange-rimmed card with a purple-to-orange arc and a flapping bat, a pink card with an arc in the flag's colors and a beating heart, or a snow-capped card with a red arc on a green ring and a turning snowflake. With reduced motion nothing spins or flaps. The thin bar at the top is unchanged in every theme. See the Design principles note for how it is measured.

### SLA metrics (`/sla`)

Four Chart.js trend charts over the IT-Operations queue's daily snapshots. They share one **Last 7 / Last 14 / All time** toggle, scroll-to-zoom and drag-to-pan (the circular-arrow button resets the zoom), and each exports a PNG.

- **Queue health over time:** meeting SLA, due soon and SLA not met as stacked areas, with compliance % as a dashed line on the right axis. Clicking a day lists that day's breached tickets as links into Zendesk.
- **First response time:** business-hour median, average and p90 against the 2 h limit. The tooltip also shows the share of tickets answered within the limit that day. An occasional ticket left for days would flatten every other line, so the axis stops at 4x the limit: a p90 beyond it is drawn at the top edge as a triangle, and the tooltip keeps its true value.
- **Breaches by rule:** each day's breaches stacked by rule (initial response, requester wait, no update, resolution age). A ticket can breach several rules, so a bar can be taller than that day's breached count. Days from before the history file recorded rules are left blank, not zero, and a note under the chart gives the first recorded date.
- **Agent activity:** public replies plus internal notes per agent per day, stacked, with a checkbox per agent. Leadership accounts listed in `_common.SLA_AGENT_ACTIVITY_EXCLUDE` are dropped before the data reaches the page.

The numbers come from the separate `sla-report-slack` repo's `data/sla_history.json`, which that repo's GitHub Actions job appends to on weekdays, so the checkout's copy is only as fresh as this Mac's last `git pull` of it. The page labels them "Trend history as of" the newest snapshot's date, says whether that came from the local checkout or from GitHub, and shows how many days old it is once it is 3 or more. **Update history** (`_common.update_sla_history`, a POST to `/sla/update`) is the fix without a pull: it fetches the remote branch and reads `data/sla_history.json` from `origin/main`, saves that copy in this tool's own output folder when its newest day is later than what the page has, and reports what changed ("Updated: the history now runs to 2026-11-13 (was 2026-10-29), 2 new day(s).", "Already current…", or why it failed, leaving the data as it was). It is deliberately not a `git pull`: the checkout may hold uncommitted work, and a fetch moves only git's remote-tracking branch, so no file in the working tree changes and a later manual `git pull` works as before. The page uses whichever copy ends on the later date, and the checkout wins a tie, so once you pull, the saved copy is simply ignored. The update runs without a shell, with time limits and git's prompts disabled, only one at a time, and refuses requests that did not come from this tool's own pages. When the newest saved snapshot is older than today, the charts get one more point at the end, labelled "(live)": today's counts (open tickets, on track, due soon, breached, and breaches by rule) taken from the open-ticket scan the page already runs. It is added only when that scan succeeded and was not cut short by its page limit, and never when a saved snapshot for today exists; response-time, compliance and agent-activity series stay blank for it. If the file is missing or unreadable, the page says why instead of drawing empty charts. A day missing a field (for example response times on early snapshots) is a gap in the line, never a zero, because 0 h is a real median.

**Notes.** To explain a stretch of days (for example time out sick and working remotely), add a `"note"` field to those days' entries in `sla_history.json`, such as `"note": "Out sick, working remotely"`. Each note is shaded as a band on all four charts, labeled at the top, listed above the charts with its dates, and added to the hover tooltip for those days. Consecutive days with identical text become one band; a note is trimmed and capped at 300 characters, and non-text values are ignored. The file belongs to `sla-report-slack`, so edit it there and commit and push, after a `git pull` because its workflow commits to it too. Its job rewrites only the current day's entry (a note on today's date is lost if it re-runs that day) and keeps the last 90 days, so a note ages out with its day.

Charts need the network: Chart.js, hammer.js and the zoom plugin load from cdnjs (pinned versions), and offline the charts simply don't draw. Colors are read from the active theme's CSS variables, and the charts rebuild when the theme changes.

The thresholds quoted at the top of the page (initial response 2 h, requester wait 4 h, no update 8 h, resolution age 40 h, all business hours) are also applied to the live queue. Each `/sla` load runs a scan of non-solved tickets (`_common.fetch_sla_tickets`, cached 2 minutes and bounded by `SLA_TICKET_SCAN_PAGES`) whose visible results are the **SLA** nav badge's breached count, an on-page note if the scan failed or hit its cap, and the live last point on the charts when the saved history ends before today. The page lists no per-ticket rows. Live elapsed hours are computed here from ticket comments, so they can differ by a few minutes from what `sla-report-slack` records for the same ticket.

### My tasks (`/tasks`)

Turns "create a personal task ticket in Zendesk" into a two-step flow, so a jotted idea doesn't become a ticket the moment it's typed. It is meant to behave like the sibling "Zendesk Personal Task Tracker" skill.

1. **Draft (local only).** The **Add task** form takes a subject, optional notes, an optional **Category** (**[Documentation]** or **[Configuration]**, which auto-fills a leading bracketed tag on the subject, so the tag does ride along into Zendesk) and an optional Estimated minutes (it only pre-fills the promote form). Each draft can be **Edited** inline, moved **up/down** (a simple two-entry swap), or **Discarded**. Edit and Move refuse on an already-promoted task, since its ticket should be edited in Zendesk.
2. **Promote.** **Make Zendesk Ticket from Task** is the only moment a ticket is created: requested by and assigned to you by default (Assignee and Requester can each be you, Jesstin, Nekoashi or Ryan), priority `low`, type `task`, tagged `personal_task` (which the main queue excludes), filed under the IT-Operations group, with the "IT Requests" form's custom fields set (Department always IT; Ticket Allocation chosen; Estimated minutes required). The form also offers **Assignee** and **Requester** (Catherine Vee, your own account from `ZENDESK_EMAIL`, or "IT-Operations", resolved independently via `_common.resolve_personal_task_identity`) and **Allocation** (**CSA** or **CSAIF**). The CSAIF API value is a best guess that hasn't been confirmed against the live form: if promoting with CSAIF fails field validation, check `_common.PERSONAL_TASK_ALLOCATION_CSAIF`. The group is set by a follow-up `update_ticket` call because the shared `create_ticket` has no group parameter; if it fails, the task is still promoted, with a warning. Nothing is posted on the ticket at creation. The chosen identities and allocation are remembered and resent on solve, since Zendesk requires the same custom fields on both writes.
3. **Solve (in Zendesk).** The page lists only drafts; once a task is promoted it is no longer shown here, and you solve its ticket in Zendesk. The old **Open** panel with its **Quick Solve** and **Mark done** buttons was removed. The **Add + Quick Solve** button on the new-task form still creates an already-solved ticket in one step (it needs Estimated minutes), and the solve routines behind the removed buttons remain in `_common.py` and `review_server.py` but have no button.

The draft list is read from a local `personal_tasks.json`, not a live Zendesk query (the client can filter only by one group id), and the page is a sideways-scrolling board like `/overview`: the main body (the new-task form and the drafts, in two columns, or one on a narrow screen) takes nearly the full width, with the **Recently completed** column to its right, its left edge showing as a cue; scroll sideways (swipe, drag the scroll bar, or hold Shift and use the wheel) to see it, and a line under the heading says so. Each page load first reconciles that file against live ticket status (`_common.reconcile_personal_tasks`), so a promoted task solved in Zendesk moves into the **Recently completed** column (the last 10, newest first, with when each was completed; an empty-state line shows before any are).

Both `pending_drafts.json` and `personal_tasks.json` archive terminal entries (posted, dismissed, handled elsewhere, completed) older than 30 days into sibling `*_archive.json` files, as part of the detection scan and the `/tasks` reconcile pass respectively. Active entries are never touched.

### Read-only pages

All of these share one row renderer (`_render_readonly_ticket_row`), show no drafting or already-replied checks, and offer **Claim** on tickets not assigned to you.

- **IT-Security** (`/security`, no longer a nav tab: it is a block on Overview, which links here for the full list): that group's `IT_SECURITY_COLUMN_LIMIT` most recent tickets in any status, a recent-activity snapshot rather than a first-reply queue. Claiming from here changes only Zendesk; the page's cache refreshes on its own.
- **Discussion** (`/flagged`, no longer a nav tab: it is a block on Overview, which links here for the full list): every non-solved/closed IT-Operations ticket tagged `block` or `standup`, in two side-by-side panels (a ticket with both tags appears in both). It is separate from the main queue on purpose, because that queue shows only tickets still needing a first reply. Zendesk's API has no "any ticket with this tag" filter, so `_common.fetch_flagged_tickets` pages through the group sorted by `updated_at` and checks tags in Python, bounded by `FLAGGED_TICKET_SCAN_PAGES` (default 10) and Zendesk's hard 1000-result `/search.json` ceiling (`ZENDESK_SEARCH_RESULT_CAP`; a page past it returns a 422). If a page fails mid-scan, results already found are kept, and a note says the scan stopped early.
- **Recently Solved** (`/solved`): your last `RECENTLY_SOLVED_LIMIT` (40) solved tickets in the IT-Operations group, with a "solved *N*h ago" time. Solved tickets assigned to you in other groups don't appear, because the scan is group-scoped. The same "stopped early" note applies (`SOLVED_TICKET_SCAN_PAGES`, default 10).
- **Overview** (`/overview`): who holds which open IT-Operations tickets. One column per person (you first, then Jesstin, Nekoashi and Ryan) and an Unassigned column, each listing that person's new/open/pending/hold tickets oldest first, with age, an SLA-not-met or due-soon chip naming the rule, and Claim on tickets that aren't yours. Tickets held by anyone else are only counted in a note. The page is a Trello-style board: the columns sit in one row that scrolls sideways, and each column scrolls vertically past a screenful. A line under the heading says so (swipe, drag the scroll bar, or hold Shift and use the mouse wheel). To the right of the people, two columns replace what used to be the Discussion and Security nav tabs. **Discussion** shows the tickets tagged `block` or `standup` (five of each at most, with a link to `/flagged`). **Security** shows the IT-Security group's most recent tickets (five at most, with Claim and a link to `/security`). The ticket cards reuse the `/sla` page's cached scan (`_common.fetch_sla_tickets`, personal tasks excluded); the two blocks read `_common.fetch_flagged_tickets`' and `_common.fetch_recent_tickets`' own caches, so a cold load adds one listing scan for each. The Discussion and Security full pages keep Overview highlighted in the nav. Standup stays a nav tab. `OVERVIEW_PEOPLE` sets the names (comma-separated, exact Zendesk display names, first = you).

**`/projects`:** needs an Airtable personal access token that you create and paste into `.env` yourself; see the Projects section below. Without it `/projects` says what to add and every other page keeps working.

**No tab count badges.** The nav links used to show open tasks, standup items and SLA breaches; those numbers were removed. The "N pending" badge beside the title and the count in the browser tab's title remain.

### Standup (`/standup`)

Three read-only lists built from the Operations Standup notes. They come from the separate `meetingnotestool` project, which parses the notes and owns all status logic; these pages only display its view. Pages are server-rendered, work without JavaScript, and filter through plain query parameters.

- **Action items** (`/standup?show=open|resolved|all&owner=&q=`): every action item mentioned at a standup and whose it is, newest first. The owner filter **defaults to you**: a link with no `owner` shows your items only, where "you" is the notes' own `me` when that name is one of `STANDUP_PEOPLE`, otherwise the first name in `STANDUP_PEOPLE`. Choose **Everyone** (`owner=all`) to see all of them; that choice is kept in the page's links, and **Clear** returns to your items. A search box and an owner filter (only `STANDUP_PEOPLE`; no Group or Unassigned option) narrow the list; picking one owner adds a summary line (open, done and dropped counts). Each row shows the standup it was raised at (linked to those notes) and its topic. An owner the notes only implied is shown with a dotted underline, and a "check" tag marks a guessed detail. The owner is the person the notes assign the item to, which is not necessarily the person who said it. **Add to My tasks** on an open item owned by one of `STANDUP_PEOPLE` turns it into a local My tasks draft; other owners' items (Group, Unassigned, anyone else) have no button.
- **Topics** (`/standup/topics?q=`): every topic the action items belong to, A to Z with letter headings, each with its status, how many standups raised it and how many open items it has. Opening one shows **Topic history** (`/standup/topic/<key>`): every update across standups, newest first, with each action item under the update it came from, so a topic can be followed over time.
- **The tracked person's items** (`/standup/tracked?q=`, labelled with the person's name, Ryan by default; set `STANDUP_TRACKED_PERSON`): every topic update that person gave at a standup, grouped under that standup's date (linked to the notes), newest date first, so nothing they said is forgotten.

The old `/standup/actions` and `/standup/people` URLs redirect to Action items and `/standup/workstreams` and `/standup/closed` to Topics, keeping the search and owner filters that still exist. The pages that were removed (Overview, Needs attention, Meetings, Workday recap, bulk resolve) redirect to Action items. The nav badge counts your open standup action items.

**Links into the rest of the tool.** **Add to My tasks** saves an open action item as a local My tasks draft (`_common.add_standup_action_to_tasks`); nothing reaches Zendesk until you promote it. For a teammate's item the draft's notes say whose it is. The draft records its source, so the button becomes an **In My tasks** chip and a second click can't duplicate it. Its notes name only the standup, so promoting it doesn't carry the meeting notes into Zendesk. Ticket numbers in the notes ("ticket 123456", "#123456") link to the ticket when `ZENDESK_SUBDOMAIN` is set.

**What was removed.** This tool no longer records Done/Dropped decisions, bulk resolve, a decision log or backups. `meetingnotestool`'s build still applies a `decisions.json` if one exists, but nothing here writes it; any leftover `decisions.log.jsonl` and `decisions_backups/` there can be deleted.

### Projects (`/projects`)

The Airtable table "Projects" in the base "IT Operations Projects (2026 Reset)", as a **Board** or a **Table** (toggle at the top, `?view=table`). The board has one column per Status in workflow order (INTAKE, SCOPING, ACTIVE, BLOCKED, COMPLETE, CANCELLED), with the projects that have no Status first in a **Needs triage** column. A status spelled with other case or stray spaces ("Active ") shares its column with the real one. Each card shows the assignee, dates, Zendesk ticket chips, workstream, a two-to-three-line preview of the description, blockers and sprint notes, and a "Past end date" chip when a project is past its end date and not COMPLETE or CANCELLED. Like `/overview`, the board scrolls sideways. COMPLETE, CANCELLED and any column with no projects start collapsed to a narrow strip showing only the heading and count; click a heading to open or close one (the choice is remembered per browser, and typing in the filter box opens any collapsed column that holds a match until the box is emptied). A card has a red left edge when it is past its end date and an amber one when it is BLOCKED (red wins), and shows its assignee, dates and chips on separate lines. The table lists the same projects in rows (Project, Status, Workstream, Assignee, Ticket, Blockers); clicking a column heading sorts by it and clicking again reverses (`?sort=name`, `?sort=-name`; the default is Needs triage first, then workflow order), and a Start, End or Completed column appears only when some project has that date. A filter box narrows cards or rows as you type. **Refresh data** on this page reloads from Airtable only; it never scans Zendesk.

Clicking a project's name or **Details & edit** opens a side panel (Escape, the × or a click outside closes it) with the full description and a small form for exactly these fields and no others. Without JavaScript the same link, `/projects?open=<record>`, renders the page with the panel already open. The fields: Project name (required, one line), Status, Assignee, Requester (one line), Urgency Rating, Start date, End date, Completed on, Blockers, Sprint notes and Description. Text fields are limited to 255 characters for one-line fields and 10,000 for long text. Urgency Rating is chosen from the five existing options (`_projects.URGENCY_OPTIONS`) or cleared; a current value that matches none of them is shown as "(not editable)" and left untouched unless another option is picked. The panel header shows "Requested by" when a requester is set. The form posts a short name for each field ("status", "assignee", ...), never an Airtable field name, so a forged request cannot reach any other column (`_projects.FIELD_SLUGS`). Saving takes two steps. **Review changes** shows the exact before and after (nothing is sent yet); **Save to Airtable** then sends one `PATCH` containing only the fields that changed, with `typecast` off so Airtable never invents a new Status option. Before writing, the tool re-reads the record: if a field was changed in Airtable after the page loaded, the save stops with a message instead of overwriting it. After writing it checks Airtable's reply against what was sent and says "(confirmed)" or names the field that did not match. The banner offers **Undo**, which restores the replaced values, but only while the record still holds what that save wrote. Each save is logged locally in `scripts/output/projects_edits.jsonl`, and a full snapshot of the table is kept as a backup (see "Snapshots" below); those are the only Airtable data kept on disk. Marking a project COMPLETE can fill in today as the completed date when the form's checkbox is ticked and no date is set.

**New project.** The **+ New project** button (top of the board and table, or `/projects/new`) opens a form for Project name (required), Status (INTAKE preselected), Assignee, Requester, Urgency Rating, End date and Description. Like an edit it takes two steps: **Review new project** shows what will be created (nothing is sent), and **Create in Airtable** sends one `POST` with only the filled-in fields and `typecast` off. Only those seven fields can be set; the form posts short names, never Airtable field names (`_projects.CREATE_SLUGS`). A name that already exists (ignoring case) is refused, which also stops a double click from creating two. After creating, the tool checks Airtable's reply, says "(confirmed)" or names the field that did not match, and opens the new project's panel. **A new project has no Undo**, and nothing in this tool ever deletes a project: if one was a mistake, delete it in Airtable (the log entry in `projects_edits.jsonl` records its record ID).

**Snapshots.** Before the first save (edit or create) in any 6-hour stretch, the tool reads the whole table, every field and not only the ones the page shows, and writes it to `scripts/output/projects_snapshots/projects-<date>-<time>.json` (git-ignored, mode `0600`). The newest 30 are kept. A snapshot that cannot be taken never blocks a save; the banner then says "Snapshot skipped" with the reason. There is no restore button: to recover, open the file, find the record by its `id`, and copy the values back in Airtable (or ask for a restore script). The snapshots hold real project data, so keep them off shared drives and out of commits.

**Setup.** Create an Airtable personal access token yourself (Developer hub, Personal access tokens) with the scopes `data.records:read` and `data.records:write`, limited to the one base, and put it in `.env` as `AIRTABLE_TOKEN` along with `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_ID` and optionally `AIRTABLE_VIEW_ID` (see `.env.example`). Without them `/projects` shows these instructions. Airtable's API cannot list a base's collaborators, so the people who can be picked as Assignee are listed in `PROJECTS_ASSIGNEES` as `Name:email` pairs; without it the Assignee select is disabled and everything else still works. Reads are cached in memory for about 2 minutes, and a failed reload keeps showing the last copy with a note.

**Protection on these routes.** Every POST route in the app, not only the Projects ones, sits behind one guard at the top of `Handler.do_POST` (`_same_origin_request_allowed`): a request whose `Origin` or `Referer` is another site, or whose `Host` is not this machine, gets HTTP 403 before any route code runs, so a web page you happen to have open cannot make your browser post a Zendesk reply, create a task or edit Airtable through this local server. A request with neither header (curl, a script) is allowed, since a cross-site browser form always carries one. The refused request's body is read and dropped so the connection stays in step. A test lists every POST route and fails if one is missing the guard.

### Diagnostics (`/diagnostics`)

Reached from the link in the **About This Tool** panel (it is not in the sidebar). The page runs local checks only, instantly, with no Zendesk or Airtable call: the `.env` file exists and is not readable by other accounts; the Zendesk settings are set (names are reported, never values), the OAuth token file exists and the last recorded connection check passed; the Airtable settings, the assignee list, how many projects the server last loaded, and how many snapshots and log entries exist; the SLA history file's age and the `sla-report-slack` checkout; the three background services (running, idle, failed or not loaded, from `launchctl list`); and that the output folder is writable. Each row is marked OK, Check, Problem or Info in words as well as color.

Two buttons call Airtable, and only when pressed. **Test Airtable connection** reads one record and writes nothing. **Run write test** first shows what it will do and asks for confirmation, then creates one record named "[Smoke test] <date and time>" (name only), edits its Sprint Notes, checks Airtable's reply at each step, reads the record again and, only if it still carries that name, deletes it. Only the record the test created is ever deleted. If a step fails the result page says so, and if a record could be left behind it names the record to delete in Airtable. Neither button runs automatically.

## Design principles

The reasoning behind each decision is in [`docs/DESIGN.md`](docs/DESIGN.md); this is the condensed list.

**Safety and review**

- **Nothing posts to Zendesk without a human click.** The cron script only reads and drafts; only the review server writes, and only on an explicit Public Reply, Internal Comment, Claim or promote click. Remove only stops this tool tracking a ticket.
- **A candidate stays on the page until you act on it, you remove it, or Zendesk resolves it.** The one automatic exit is the per-scan already-replied sweep (Zendesk's own state overriding the queue). Clicking a post button on an already-replied ticket stops and asks.
- **Queue mutations re-check current status inside the same lock** and never trust what the browser last rendered, so a stale tab can't overwrite what a scan or another tab already did.
- **A failed post leaves the draft intact.** There are no bulk actions: every reply is reviewed individually.
- **The assignee badge is a hint, never a gate.** Zendesk's Assignee can be stale or irrelevant, so it never blocks a post or pulls a ticket. It costs one extra `get_ticket` per pending ticket per scan, an accepted tradeoff.
- **Claim is a plain assignee change**, reversible and nothing posted, so it needs no interstitial. Taking over someone else's ticket asks for one in-page confirmation.
- **Localhost only, no login.** The server binds to `127.0.0.1`; there is deliberately no auth layer beyond being logged into your macOS user account.
- **Credential-bearing files are tightened.** `AUTH_STATE_FILE`, `OAUTH_PENDING_FILE` and the shared token file are `chmod`'d to `0600` after writes (`_harden_file_permissions`), best-effort.
- **Private data stays out of this public repo.** `/standup` reads `meetingnotestool` data from that checkout at request time (`_common.standup_snapshot`, mtime-cached), never copying or caching it, and writes nothing there. `scripts/tests/conftest.py` points `STANDUP_DATA_FILE` at a temp path, so tests can't read real notes.
- **Airtable writes are allowlisted, reviewed, conflict-checked, backed up and (for edits) reversible.** `/projects` edits only the eleven fields in `_projects.FIELD_SLUGS` and creates records with only the seven in `_projects.CREATE_SLUGS`, always after a review page, never over a value that changed in Airtable since the page loaded, with the reply read back, a local log that powers Undo for edits, and a full snapshot taken before the first save in each 6-hour stretch. Nothing deletes a project except the Diagnostics write test, and only the temporary record it just created. The token lives only in `.env`.
- **Every POST route is same-origin guarded.** One check at the top of `do_POST` refuses a cross-site `Origin`/`Referer` or a non-local `Host` with 403 before anything runs.
- **A read-only dependency stays read-only.** This tool only reads `sla-report-slack`'s `sla_history.json`. It never pulls, writes or commits there, so it can't create a merge commit or send that repo's email. **Update history** respects that: it runs `git fetch`, which moves only the remote-tracking branch, and keeps the newer copy in this tool's own output folder.

**Data, caching and concurrency**

- **Slow reads are stale-while-revalidate; safety checks are never cached.** The main queue's live "Recently worked on" scan (`fetch_recently_worked_on`, tens of seconds cold) is served instantly from its last result; once older than 2 minutes, one background thread refreshes it (a failed refresh keeps the last good result and backs off 30 seconds). Only a missing cache, `force`, or a result over an hour old makes a request wait. Only the live half is cached; local reply history is merged in fresh on every call. The review server fills this cache at startup (`_common.warm_caches`). Separately, `_common.cached_ticket_comments` caches comment lists keyed on (ticket id, `updated_at`), which is exact because a ticket's comments can't change without its `updated_at` changing (6-hour expiry and a 1000-entry LRU bound as backstops). It is used only where the ticket came from a live listing in the same scan (candidate detection, Recently worked on, the SLA scan). The click-time already-replied check and the per-scan re-check of pending tickets always read live.
- **Local-first, with bounded live reads.** The main queue reads only local files and a cached auth state, so loading it never depends on Zendesk being reachable. Exceptions are narrow: the Recently worked on sidebar's live half, `/security` and `/flagged`. Each result is cached in-process for 2 minutes (the cron cadence), served stale-but-instant, and falls back to the last good result (or local-only data) on failure.
- **Scans that Zendesk can't filter are done in Python, bounded and honest.** `fetch_flagged_tickets`, `fetch_recently_solved_tickets`, `fetch_recently_worked_on` page through a group sorted by `updated_at`, with page/comment budgets and a "stopped early" note, so a gap reads as "might be missing", never "nothing more". Tickets already known locally are never re-checked.
- **Writes are atomic.** `pending_drafts.json` and `personal_tasks.json` are written through a temp file and `os.replace()` (`_atomic_write_json`). The loaders fail open on a parse error, so a torn file would otherwise silently make every entry vanish.
- **A file lock and a network call are different things.** Never hold a file lock across a slow Zendesk call. `reconcile_personal_tasks` reads from the network on an unlocked pass first, then locks only for the fast write, re-checking each entry's state.
- **Real Zendesk writes need an in-process guard against happening twice.** The server is a `ThreadingHTTPServer`, so a double-click can land overlapping requests. Promote, complete, quick-solve, discard, edit and move all take `_begin_personal_task_action`, and posting a reply has its own per-ticket `_begin_pending_ticket_action`. A status check inside the file lock isn't enough, because the lock can't be held across the live call.
- **A retry can't redo what already succeeded.** Each Quick Solve comment's posted state is recorded the moment it succeeds, independently of the solve.
- **Read-through caches get their own locks**, guarding only the dict read/write (never the fetch), and `_flagged_ticket_cache` is replaced atomically so a reader never sees a gap.
- **Transient failures aren't cached; real answers are.** `_resolve_agent_id_by_email` caches a resolved id for `AGENT_ID_CACHE_TTL_SECONDS` but not a failed `search_users` call.
- **A ticket's "waiting" clock and Zendesk's "last touched" clock are different facts.** Cards show both. `updated_at` is captured at detection and refreshed each scan from the `get_ticket` call already made for the assignee badge, so it costs no extra API call.
- **One predicate, not two copies.** `check_already_replied` and the detection check share `_first_public_reply_from_other`, so they can't drift. The same lesson shaped `_common.py` (an earlier launchd OAuth-path bug came from duplicated logic).
- **Exclude by what a ticket is, not where it lives.** Personal tasks are kept out of the queue by the `personal_task` tag (checked in `find_candidates()`), since Zendesk, not this tool, picks a new ticket's group.
- **Malformed requests fail open.** All nine POST body reads go through `Handler._parse_form_body()`, which survives a non-numeric or negative `Content-Length` and a non-UTF-8 body by degrading to an empty form.
- **Escape for nested contexts in the order the browser undoes them.** The Claim `confirm()` embeds an assignee name in a JS string inside an HTML attribute: JS-escape first, then HTML-escape.
- **No invalid escape sequences in embedded scripts.** A plain Python string with a JS regex is a `SyntaxWarning` today; those scripts use raw strings, and the file is compiled with warnings as errors.

**My tasks**

- **Jotting an idea and committing to a ticket are separate actions.** Adding saves a local draft; the ticket exists only after an explicit promote, which can't be undone (only marked done).
- **A draft can be edited or reordered until it becomes a ticket, never after.** Edit and Move refuse on promoted tasks, because the ticket's truth then lives in Zendesk.
- **A value given early only pre-fills a later ask.** Estimated minutes at draft time pre-fills the promote form but never skips it.
- **Closed sets get a `<select>`.** Assignee, Requester and Allocation are dropdowns, so an invalid choice is impossible from the UI. Assignee and Requester are resolved independently, and the IT-Operations group is a fixed default rather than a form field.

**SLA page**

- **Read another tool's numbers; don't recompute them.** Daily history comes from `sla-report-slack`; this page reshapes `sla_history.json` (`_common.sla_chart_data`) and says how fresh it is. Only the live breach scan on `/sla` (and the one live point it can add to the charts) is computed here.
- **Missing is not zero.** A field absent from a day's snapshot becomes `null`, drawn as a gap, because 0 h is a real median and 0 breaches is a real day. Rule counts are blank on days from before the history recorded them.
- **Outliers are clamped visibly, not hidden.** Response-time values beyond 4x the limit sit at the top of the axis as triangles with the true value in the tooltip, so one bad day doesn't flatten the rest.
- **Charts match the sibling dashboard.** Chart.js (4.4.1) with the zoom plugin is the one external script dependency in the tool, a deliberate exception to the one-self-contained-response style, so these charts behave like `sla_dashboard.html`'s. Theme colors are read from CSS variables at build time.
- **Keep only what's asked for.** An earlier KPI strip, rule cards and awaiting-first-response list were removed on request. The live scan stays, because Overview uses it.

**UI**

- **A new workflow gets a new page**, and a page's status shows where you are through the highlighted nav link. Anything counted for a header is computed from cheap local files or warm caches, never a new Zendesk call.
- **One primary action per page, everything else outlined.** Icons are a fixed set of hand-authored inline SVGs (`_ICONS`, `currentColor`). Decorative topic icons (`_topic_icon()`) are separate self-contained colored PNG badges with empty `alt` text and degrade to nothing if a file is missing. The Projects page uses them for its heading, one badge per status column (keys `project-<STATUS>`; a status without a key shows none) and the edit panel's dates legend.
- **Avatars only beside names the tool trusts** (`_avatar_html`, `_requester_avatar_prefix`), never beside an unconfirmed guess.
- **The toast and the auth banner share CSS but not behavior.** The toast (`data-toast`) floats and auto-dismisses; the auth warning stays docked, since a standing condition shouldn't vanish on a timer.
- **The fade-on-submit is cosmetic**, not an optimistic update. If a post fails, the card reappears.
- **A header is a glance, not a dashboard.** The main page uses two small stat chips.
- **Use the width available.** Compact rows go in `.mini-ticket-grid` and side-by-side panels in `.two-panel`; short pages like `/reconnect` are capped and centered instead of padded.
- **A basic accessibility pass, not a full audit.** The toast is a live region, the queue sits in `<main>`, and flag text meets WCAG AA contrast in light and dark. There has been no full screen-reader or keyboard-only pass.
- **Two sections answering the same question should be one.** Recently worked on replaced the old "Last N replies" section. Its history widened to 30 and subjects wrap rather than truncating.

**Theming**

- **Themes are class-based and done in CSS where possible.** Each theme is a token block on `html.theme-<name>`; `theme_init_script` sets the class before first paint, so there is no flash, and `applyTheme()` repeats it on click. The `theme-color` meta needs a few lines of JS in those two scripts. The avatar palette is generated into `.avatar-hue-N` rules from `_AVATAR_HUES`. `::selection` and `scrollbar-color` read `--accent`/`--bg` and follow every theme for free.
- **Seasonal themes override only variables.** Galaxy follows the same pattern (plus the photo background and glass panels), and Halloween and Christmas reuse the whole variable set, add `--on-accent` for text on the bright active pills, and force a dark `color-scheme`. Functional signal colors come from the Dark theme.

**Loading indicator**

- **Show measured progress where possible, nothing precise where not.** There is no client-side router, so every navigation is a full reload the browser can't see into; progress is measured on the server. A click or submit sets a short-lived `csa_pid` cookie. A transparent wrapper that `make_client()` puts around the Zendesk client counts the API calls the request makes (`_common.progress_*`). The page polls `/__progress?id=...` for done-versus-expected, where expected is the call count of the previous load of the same route.
- **The first load of a route has no baseline**, so the bar creeps toward 90% instead of showing an unjustified number, and is capped at 95% until the server reports the load finished. A load with no API calls (warm caches) finishes in milliseconds. The cookie rides along a form's redirect, so a POST that redirects to a slow GET is one continuous bar. If the navigation never reaches the server, the bar hides after a few seconds.
- **Quiet by design.** The automatic `/firstreply` refresh starts neither bar nor card. Same-page `#` links, `mailto:`/`tel:`, modifier-clicks and `target="_blank"` are excluded. `/__progress` and `/healthz` touch only in-memory state, answer instantly even while a page is blocked on Zendesk, and `/__progress` is left out of the access log. The bar uses `--accent`, so it follows every theme.
- **The detail card is a second stage.** After about 1.5 seconds the dim and card appear; neither blocks clicks.

**Verification**

- **Checks that don't outlive a session don't protect anything.** `scripts/tests/` is pure Python with no browser and no real Zendesk credentials, wired into `.github/workflows/tests.yml` so it runs on every push and PR.

## Repo layout

```
.env.example                                  template for the git-ignored .env (copy it, fill it in)
requirements.txt                              runtime dependencies (pinned)
requirements-dev.txt                          + pytest, for running scripts/tests/
docs/DESIGN.md                                full original documentation and design rationale
.github/workflows/tests.yml                   CI: runs scripts/tests/ on every push/PR
scripts/
  _common.py                                  shared config, pending-store, progress tracking and scan logic
  first_reply_check.py                        the 2-minute detect-and-draft cron job
  review_server.py                            the review/approve web server + OAuth reconnect
  _projects.py                                the Airtable Projects read, allowlisted edits, create, snapshots, write test and undo log for /projects
  _diagnostics.py                             the local health checks behind /diagnostics
  watchdog.py                                 standalone health check for the services above
  com.csa.zendesk-first-reply-check.plist     launchd config for the cron job
  com.csa.zendesk-review-server.plist         launchd config for the web server
  com.csa.zendesk-watchdog.plist              launchd config for the watchdog (every 5 min)
  reload_launchd.sh                           (re)installs all three services; works out paths itself, `--check` for a dry run
  csa_notifier.applescript                    source for the optional "CSA Notifier.app" helper
  assets/                                     csa-logo.png, galaxy-bg.jpg, topic-icons/
  output/                                     logs and runtime state (git-ignored)
  tests/                                      pytest suite
```

`CSA Notifier.app` itself is not checked in: it's a per-machine build artifact you create via Automator and keep in `~/Applications`.

## Running tests

```
pip install -r requirements-dev.txt
python3 -m pytest scripts/tests/ -v
```

The suite runs in seconds with no real Zendesk credentials, no network and no sibling `zendesk-mcp-server` checkout. `scripts/tests/conftest.py` puts a stand-in (`scripts/tests/stub_pkg/`) ahead of the real `zendesk_mcp_server` on `sys.path` and repoints every `_common.py` file path at a fresh temp directory per test, so no test touches the live `pending_drafts.json` or `personal_tasks.json`. Fixtures are all made up.

**Covered:** `/sla` Update history against real temporary git repositories (a newer snapshot pushed from a second clone is fetched, the checkout's working tree and uncommitted edits are untouched, a later `git pull --ff-only` still works, failures change nothing, bad or missing remote data is rejected, git missing or slow, one update at a time, credentials never shown, the route and its cross-site refusal) and the live "today" point (when it is added and when it is not); the `/projects` board and edit flow (against a fake Airtable: paging and caching, stale fallback, field allowlist and forged form fields, validation, conflicts, read-back, undo, per-record guard, the same-origin refusal, and the real HTTP routes); the About This Tool panel (present on every page, names each data source, closes with Escape in markup) and the Overview's sideways-scroll hint; the personal-task lifecycle and its shared validation; the atomic-write-under-flock guarantee for both stores (with real concurrent writer threads); the lock around the read-through ticket caches (a concurrency test); tagging teammates on internal comments (real `Handler._post` with a fake client: mention markup and escaping, rejected values, a person missing in Zendesk, Zendesk dropping a mention, a failed read-back, tags on the confirm page, ignored on public replies, the card markup); form-body parsing edge cases; the three standup lists (action items with person filter and Add to My tasks, topics A to Z with history, the tracked person's updates by date) and the redirects from the old pages; the measured-progress counting, history and `/__progress` wiring (including a real HTTP test); the seasonal-theme markup, the Halloween bat container and its wiring, and a Node syntax check of the page's inline scripts (skipped without Node); the comment cache and stale-while-revalidate behavior, including that click-time checks stay live; the `/sla` chart series (gaps versus zeros) and markup; the `/overview` grouping by assignee and its page; the `/tasks` page layout (drafts only, two columns) and the absence of count badges on the nav tabs; the Assignee/Requester choices (only the four people, exact-name lookup, rejection of anything else); the background picker's markup, head script and CSS; the install tooling (`reload_launchd.sh` run under zsh with a stubbed `launchctl` and a temporary home folder, so the generated plists, XML escaping, `--check` mode and the missing-`.env` and missing-checkout failures are exercised, plus `.env.example` against the settings the code reads). The install tests are skipped where zsh is absent.

**Not covered:** anything JS-driven in a real browser (the loading bar's drawing and the detail card, the `/sla` charts' drawing, theme switching, choosing and removing a background image, the category auto-fill listener) and full end-to-end page rendering. Those are checked manually with Playwright during development. Promote, complete and quick-solve, which need a live-ish client, are not covered yet either.

## Troubleshooting

- **Logs** are in `scripts/output/`: `first_reply_check.log` (one line per cron run), `review_server.out.log` / `review_server.err.log`, and `watchdog.out.log` / `watchdog.err.log`. `watchdog_state.json` is the watchdog's "already alerted" memory, and its modification time shows when it last ran.
- **The page won't load:** confirm the service is running with `launchctl list | grep com.csa`.
- **Tickets aren't being detected:** look for "not authenticated" in the cron log; that's the OAuth token, fixed through the in-page `/reconnect` flow.
- **A refresh reports "N candidate(s) found, 0 new":** expected, not a bug. `find_candidates()` skips any ticket id already tracked in `pending_drafts.json` (whatever its status), most commonly because it's already `posted` while Zendesk still shows it `new`/`open`. It stops appearing once the ticket's status changes.
- **`ModuleNotFoundError: No module named 'zendesk_mcp_server'` after a restart:** the sibling repo's venv lost its editable install (the `.pth` file was present but not read, cause never found). Confirm with `zendesk-mcp-server/.venv/bin/python3 -c "import zendesk_mcp_server"`, then reinstall with `.venv/bin/python3 -m pip install -e .` from that repo's root. Both plists also set `PYTHONPATH` to that repo's `src/` as a standing safety net.
- **Plist changes didn't take effect:** `launchctl unload`/`load` can silently no-op, leaving the old definition running. `reload_launchd.sh` uses `launchctl bootout`/`bootstrap` instead; prefer those if you reload by hand. A loaded job doesn't pick up an edited plist on disk until it is booted out and bootstrapped again.
- **"Refused: this request did not come from this tool's own pages" (HTTP 403):** the POST guard saw a cross-site `Origin`/`Referer` or a `Host` that is not `127.0.0.1`/`localhost`. Open the tool at `http://127.0.0.1:8765/` (not through a tunnel, proxy or another hostname) and try again.
- **Airtable errors on `/projects` or in Diagnostics:** `401` or `403` means the token is wrong, expired, lacks `data.records:read`/`data.records:write`, or does not include the base; `404` means a wrong base or table ID; `422` means a value Airtable would not accept (the tool never lets Airtable create new select options). Fix `.env`, restart the review server, and use Diagnostics to retest.
- **A write test failed and may have left a record:** the result page names the record. Search the Projects table for "[Smoke test]" and delete it in Airtable.
- **`reload_launchd.sh` details:** it isn't executable in git, so run `zsh scripts/reload_launchd.sh`. If it prints `Bootstrap failed: 5: Input/output error` for one service, the service may have been mid-restart and be running fine: check `launchctl list | grep com.csa` (a PID in the first column means running) and `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/`.
- **Restarting just the review server** (needed after changing `review_server.py`, `_projects.py`, `_diagnostics.py` or `_common.py`, or any `.env` value; `watchdog.py` changes need no reload because `launchd` re-runs it): kill whatever holds the port first, since `bootout` can race with a hung process, then wait a few seconds before the port accepts connections.

  ```
  lsof -ti :8765 | xargs kill -9 2>/dev/null; launchctl bootout gui/$(id -u)/com.csa.zendesk-review-server 2>/dev/null; sleep 2; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist
  ```
- **The watchdog** checks every 5 minutes that the review server answers HTTP (via the lightweight `/healthz` route, so a slow Zendesk scan can't trip it) and that `first_reply_check` has completed a scan recently during business hours, and notifies as soon as either looks stuck. It has no dependency on `_common.py` or `zendesk_mcp_server`, so it keeps working if that import chain breaks, which is why it exists: the `ModuleNotFoundError` above once broke both services unnoticed. A "Connection reset by peer" alert with a working page usually means a hung request, not a down server; restart as above.
- **`watchdog.err.log` fills with `can't open file '.../watchdog.py': [Errno 1] Operation not permitted`:** macOS privacy protection is blocking the watchdog's interpreter from reading a protected folder such as `~/Desktop` (this happened with the Command Line Tools `/usr/bin/python3`). The watchdog must run on a standalone interpreter that already has that access, such as the uv-managed CPython the other two services' venv is built on. If a `uv` upgrade removes the interpreter it was using, re-run the installer with `WATCHDOG_PYTHON=/path/to/python3 zsh scripts/reload_launchd.sh` to point the watchdog at another one.
