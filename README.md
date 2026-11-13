# sla-task-firstreply

Local automation that watches the Cloud Security Alliance IT-Operations Zendesk queue for tickets awaiting a first reply, drafts a fixed acknowledgment template for each, and lets you review and edit every draft, then post it as a public reply or an internal-only comment. Nothing is posted without your click. It runs entirely on this machine via `launchd`, with no dependency on a cloud scheduler or a live Cowork session.

> The full, unabridged documentation (per-feature history and design rationale) is kept in [`docs/DESIGN.md`](docs/DESIGN.md). This README is the condensed version.

## How it works

Two main `launchd` services share a JSON file, and a third job watches them:

- **`scripts/first_reply_check.py`** runs every 2 minutes, Mon–Fri 9am–4pm local, and looks back up to 72 hours (`ZENDESK_LOOKBACK_HOURS`) so a sleeping Mac doesn't create a blind spot. It pulls recent IT-Operations tickets, keeps those with no public reply yet, drafts the acknowledgment text, and adds new candidates to `scripts/output/pending_drafts.json`. It never posts to Zendesk. It sends a macOS notification, prefixed "CSA", with a sound (`ZENDESK_NOTIFICATION_SOUND`, default `Glass`) when something new appears, or when the check itself is broken (throttled to once an hour).

  Notifications go out through up to three independent paths every time: [terminal-notifier](https://github.com/julienXX/terminal-notifier) (if installed; its banner is click-through to the review page), a plain AppleScript banner, and the optional helper `.app` below. The redundancy is deliberate: on this Mac, neither terminal-notifier nor `osascript` reliably shows a banner when `launchd` triggers them, even though both exit cleanly, because macOS treats a headless `launchd` process and an interactive Terminal command as differently permissioned senders. The helper app is the path most likely to work, since real `.app` bundles get their own row in Notification settings. Every attempt's exit code and error text is logged to `scripts/output/first_reply_check.log`; a permission-denied message there points at System Settings → Notifications, not at this code.
- **`scripts/review_server.py`** is a local-only web server at **http://127.0.0.1:8765/**, kept running (`KeepAlive`). It is the only piece that can post a comment to Zendesk, and only when you click **Public Reply** (visible to the requester) or **Internal Comment** (agent-only) on a ticket, optionally after editing the draft. It also has a manual **Refresh data** button (runs the detection logic on demand, bypassing business hours) and a self-service OAuth reconnect flow at `/reconnect`.
- **`scripts/watchdog.py`** is re-run by `launchd` every 5 minutes and alerts if the review server stops answering. It checks the lightweight `/healthz` route, not a Zendesk-backed page.

All scripts share config and logic through **`scripts/_common.py`**, so they can't quietly drift apart (an earlier bug came from duplicated logic).

## Setup

### Quick start

1. **Check out the repos in the expected places.** The `launchd` plists and `scripts/reload_launchd.sh` use absolute paths, so this repo goes at `~/Desktop/git/sla-task-firstreply` and the Zendesk client at `~/Desktop/git/zendeskmcp/zendesk-mcp-server`, already set up with its own `.venv` (see Prerequisites below).

   ```
   cd ~/Desktop/git
   git clone https://github.com/cvee-csa/sla-task-firstreply.git
   ```

2. **Install this repo's one runtime dependency into that venv**, because the services run on that venv's Python:

   ```
   cd ~/Desktop/git/sla-task-firstreply
   ~/Desktop/git/zendeskmcp/zendesk-mcp-server/.venv/bin/python3 -m pip install -r requirements.txt
   ```

3. **Create `.env`** in this repo's root. Copy `zendesk-mcp-server/.env.example` and fill it in, with `ZENDESK_OAUTH_TOKEN_PATH` set to the absolute path of `zendesk-mcp-server/.zendesk_oauth_tokens.json` (see Config below). Never commit it; it is git-ignored.

4. **Install and start the three services:**

   ```
   zsh scripts/reload_launchd.sh
   ```

5. **Open http://127.0.0.1:8765/ and connect Zendesk.** If an amber banner says the connection needs attention, click **Reconnect now**, approve access in the tab it opens, then paste the redirect URL back into the page. Tickets are detected on the next 2-minute run (Mon–Fri, 9am–4pm), or click **Refresh data** to scan now.

6. **Optional:** the notification helper app, and the sibling repos for `/sla` and `/standup`, are described below.

Cloning to a different folder or user account: change `REPO_ROOT` in `scripts/reload_launchd.sh` and the absolute paths in the three `scripts/*.plist` files before step 4.

**Prerequisites:** the sibling `zendesk-mcp-server` repo (at `~/Desktop/git/zendeskmcp/zendesk-mcp-server`) checked out with its own `.venv`, with `zendesk_mcp_server` installed (`pip install -e .`) alongside `zenpy`, `requests` and `python-dotenv`. This repo's `launchd` plists point at that venv's Python directly.

**Config:** start from `zendesk-mcp-server`'s `.env.example`, or use this repo's own `.env` (git-ignored, never committed). One field is unusual: `ZENDESK_OAUTH_TOKEN_PATH` is an **absolute path** to `zendesk-mcp-server/.zendesk_oauth_tokens.json`, not a copy, because that server also refreshes the file, and one shared token file avoids two copies drifting apart.

Optional knobs, all with defaults:

| Variable | Default | Purpose |
|---|---|---|
| `ZENDESK_REVIEW_PORT` | `8765` | Review server port |
| `ZENDESK_NOTIFICATION_SOUND` | `Glass` | Any name from `/System/Library/Sounds`, or empty for silent |
| `ZENDESK_LOOKBACK_HOURS` | `72` | Lookback window |
| `ZENDESK_NOTIFIER_APP_PATH` | `~/Applications/CSA Notifier.app` | Helper app location |
| `STANDUP_PEOPLE` | `Catherine,Jesstin,Nekoashi,Ryan` | The only people offered in the `/standup` owner filters and the By person picker, spelled as the notes spell them (case-insensitive); others in the notes still appear under Everyone |
| `OVERVIEW_PEOPLE` | `Catherine Vee,Jesstin Rojas,Nekoashi Wolf,Ryan Bergsma` | Cards on `/overview`, in order; the first is you |
| `SLA_HISTORY_FILE` | `~/Desktop/git/sla-report-slack/data/sla_history.json` | Where `/sla` reads its daily snapshots (read-only) |
| `SLA_RESOLUTION_HRS` | `40` | Business-hour limit for the resolution-age rule (5 business days x 8 h) |
| `SLA_TICKET_SCAN_PAGES` | `10` | Pages of 100 tickets the live SLA scan reads before noting the queue may be larger |
| `IT_SECURITY_COLUMN_LIMIT` | `5` | Recent IT-Security tickets on `/security` (the name predates that page moving; kept so old `.env` files still work) |

**My tasks, "IT-Operations" Assignee/Requester:** set `ZENDESK_IT_OPERATIONS_EMAIL=...` in `.env` to the email of the shared Zendesk *user* account that "IT-Operations" should resolve to. It must be a real user, not the `IT_OPERATIONS_GROUP_ID` group: Zendesk's `requester_id` needs a user id, and Assignee uses the same lookup (`resolve_it_operations_agent_id`) for consistency. If unset, choosing "IT-Operations" fails with a clear on-page error instead of falling back silently; the default, "Catherine Vee", is unaffected.

**`/sla`:** needs the sibling `sla-report-slack` repo cloned at `~/Desktop/git/sla-report-slack` (or `SLA_HISTORY_FILE` pointed at its `data/sla_history.json`). This tool only reads that file; it never pulls, writes or commits there, so run `git pull` in that repo to get newer data. Without the file, `/sla` says so and the rest of the tool keeps working.

**`/standup`:** no setup if the private `meetingnotestool` repo is cloned at `~/Desktop/git/meetingnotestool`. This tool reads that project's built `dist/standup_data.json` and writes only its `decisions.json`, when you click a decision button. Set `STANDUP_DATA_FILE` in `.env` if it lives elsewhere, and `STANDUP_DEFAULT_PERSON` (default `Ryan`) to change whose items `/standup/people` opens on. Without the file, `/standup` explains why and every other page keeps working.

### Notification helper app (one-time, optional but recommended)

A small Automator application gives macOS a real notification sender to track. About five minutes of GUI setup:

1. Open **Automator**, choose **File → New → Application**.
2. Add a **Run AppleScript** action and replace its placeholder code with the contents of `scripts/csa_notifier.applescript`. If this repo moves, update the `payloadPath` line near the top of that script.
3. **File → Save** as `CSA Notifier` in `~/Applications` (the default `ZENDESK_NOTIFIER_APP_PATH`; set that variable if you save elsewhere).
4. Double-click `CSA Notifier.app` once. A "CSA Notifier — Ready" banner should appear; approve notifications if prompted, then in System Settings → Notifications confirm "CSA Notifier" is allowed with an alert style other than "None".

`first_reply_check.py` finds the app at that path automatically. Without it, the tool still works but falls back to terminal-notifier/osascript, which may not display anything from a headless context.

### Install the services

`scripts/reload_launchd.sh` is the install step and the reload step. It copies all three plists (first-reply check, review server, watchdog) into `~/Library/LaunchAgents/` and (re)loads each with `launchctl bootout` then `bootstrap`. It is safe to re-run, so run it again after pulling changes:

```
zsh scripts/reload_launchd.sh
```

To do one service by hand, copy its plist into `~/Library/LaunchAgents/` and register it, using `bootstrap` rather than the older `launchctl load`, which can silently leave an old definition running:

```
cp scripts/com.csa.zendesk-review-server.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.csa.zendesk-review-server 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist
```

After changing a script, reload it the same way. For just the review server, see the restart command under Troubleshooting.

## Day to day

Open **http://127.0.0.1:8765/** to see what's waiting on you. The header nav links to every page below; each link carries a status badge where one makes sense, and the active page is highlighted.

### The main queue (`/firstreply`)

Each card shows:

- the subject, badged **TEST** for `[Test]`/`[Tooling]` tickets and **HIGH**/**URGENT** when Zendesk's priority field says so (normal/low are not badged);
- a requester name resolved from Zendesk by id, with a flag explaining why if it couldn't be confirmed (worth reading before posting, since first-name collisions are real);
- the requester's full original text (long text scrolls in its own box rather than being cut off);
- an editable draft that opens with "Hey _name_!" ("Hey there!" when no name could be confirmed).

Pick **Public Reply** (requester sees it), **Internal Comment** (agent-only), **Claim** (assign the ticket to you), or **Remove** (drops it from the queue without posting; confirmation required). A removed ticket is never re-detected, even if Zendesk still shows no reply, so handle it in Zendesk if it still needs one. **⌘Enter** / **Ctrl+Enter** posts the draft as a Public Reply.

Claiming an unassigned ticket happens immediately; claiming one assigned to a colleague asks you to confirm first. After a Claim you land back on the page you clicked it from.

**Someone else already replied.** Two checks guard against posting over another reply. Every scan (cron tick or manual Refresh) re-checks all pending tickets' comments and pulls any that someone else has answered, with a notification. Then, when you click Public Reply or Internal Comment, the server re-checks once more. If a reply exists at that point, it shows a confirmation page with **Yes, post anyway** and **Remove instead (don't post)**; navigating away leaves the ticket untouched. Both checks fail open on a transient API error, and both count a reply from this tool's own Zendesk account as a reply, which matters on self-created test tickets where requester and replier are the same person.

**Someone else is assigned.** If a pending ticket's Assignee is anyone other than this tool's account, the card shows an amber "Also being worked on in Zendesk — currently assigned to _name_" notice and an empty draft box, so nothing goes out on autopilot over a colleague's ticket. This is informational and gates nothing. The stored draft is untouched and reappears if the assignee is cleared. The assignee is refreshed on every scan.

**Feedback and history.** After each action, a confirmation banner (a toast that fades on its own) echoes what was sent, or why nothing was, with the ticket URL as a clickable link; it has an **Undo** where the action is reversible. Buttons disable and relabel themselves ("Posting…") while a request runs. A collapsed **Replied today (N)** section lists what you sent through this tool today (Removed and auto-pulled tickets are excluded). A **Recently worked on (30)** column shows your most recent replies regardless of day, including replies you sent directly in Zendesk. That merge scans the IT-Operations group within a per-load budget (`WORKED_ON_COMMENT_CHECK_LIMIT`, default 40); a small note appears if Zendesk is unreachable or the budget ran out, so a gap reads as "might be missing something". A ticket already in local history is never re-checked, so a later direct-in-Zendesk reply on it won't move it. Open/closed state of these sections is remembered per browser.

**Refresh and countdowns.** The page reloads itself every 60 seconds as a local re-render of the pending file (not a Zendesk scan), skipping the reload if you're mid-edit in a textarea. The header shows pending/posted-today counts plus live "last checked" and "next check" values; "next check" is exact outside business hours (a resume time) and a rough `~` estimate otherwise. If the Zendesk connection needs attention, an amber banner offers **Reconnect now**: approve access in the tab it opens, then paste the redirect URL back into the page.

Tickets detected before the full-text change keep their old truncated "..." text until posted.

### Look and feel

The page follows your Mac's light/dark setting. A header control overrides it: **Auto / Light / Dark / B68FBF / Halloween / Christmas**. The choice is remembered per browser (`localStorage`, not written to any tracked file), and **Auto** clears it.

- **B68FBF** is a custom theme derived entirely from the hex color `#B68FBF`. It splits the six action buttons between two complementary shades (`#B68FBF` and its 30°-rotated sibling `#9E8FBF`) instead of six distinct hues, and swaps in its own logo and recolored topic icons.
- **Halloween** (near-black, pumpkin-orange accent, purple secondary buttons) and **Christmas** (deep pine-black, bright green accent, cranberry secondary buttons) are seasonal dark themes with a small pumpkin or tree glyph beside the header title. Christmas is the only place red/green appears in the buttons.
- Every other theme avoids the red/green traffic-light convention: each action has its own hue, and the connection warning is amber. All themes leave the amber warning/alert colors unchanged, since those are functional signals.

Layout: on wide screens the header is a left sidebar (theme toggle, nav, then the title with the pending badge, the Zendesk status button and Refresh at the bottom); on narrow screens it is a card at the top. A frosted-glass blur falls back to a solid background where `backdrop-filter` is unsupported. Every action has a small icon, and only one action per page is a solid button (Public Reply on the queue; the reconnect buttons on `/reconnect`), with the rest outlined. Cards have a "TICKET" label, a flat default look with a hover lift, and a colored initials bubble next to any requester/assignee name the tool is confident in (never next to a guess). The tab title is "Workflow Asset", prefixed with the pending count, e.g. "(3) Workflow Asset".

**Loading indicator.** The thin bar at the top of the window shows real progress for navigations you start. If a load passes about 1.5 seconds, the content dims slightly and a centered card names the destination with the actual request count ("Loading Tasks… 3 of 8 requests"). Routes with no baseline yet show an animated stripe instead of a number. See the Design principles note for how it is measured.

### SLA metrics (`/sla`)

Four Chart.js trend charts over the IT-Operations queue's daily snapshots. They share one **Last 7 / Last 14 / All time** toggle, scroll-to-zoom and drag-to-pan (the circular-arrow button resets the zoom), and each exports a PNG.

- **Queue health over time:** meeting SLA, due soon and SLA not met as stacked areas, with compliance % as a dashed line on the right axis. Clicking a day lists that day's breached tickets as links into Zendesk.
- **First response time:** business-hour median, average and p90 against the 2 h limit. The tooltip also shows the share of tickets answered within the limit that day. An occasional ticket left for days would flatten every other line, so the axis stops at 4x the limit: a p90 beyond it is drawn at the top edge as a triangle, and the tooltip keeps its true value.
- **Breaches by rule:** each day's breaches stacked by rule (initial response, requester wait, no update, resolution age). A ticket can breach several rules, so a bar can be taller than that day's breached count. Days from before the history file recorded rules are left blank, not zero, and a note under the chart gives the first recorded date.
- **Agent activity:** public replies plus internal notes per agent per day, stacked, with a checkbox per agent. Leadership accounts listed in `_common.SLA_AGENT_ACTIVITY_EXCLUDE` are dropped before the data reaches the page.

The numbers come from the separate `sla-report-slack` repo's `data/sla_history.json`, which that repo's GitHub Actions job appends to on weekdays, so they are only as fresh as this Mac's last `git pull` of it. The page labels them "Trend history as of" the newest snapshot's date. If the file is missing or unreadable, the page says why instead of drawing empty charts. A day missing a field (for example response times on early snapshots) is a gap in the line, never a zero, because 0 h is a real median.

**Notes.** To explain a stretch of days (for example time out sick and working remotely), add a `"note"` field to those days' entries in `sla_history.json`, such as `"note": "Out sick, working remotely"`. Each note is shaded as a band on all four charts, labeled at the top, listed above the charts with its dates, and added to the hover tooltip for those days. Consecutive days with identical text become one band; a note is trimmed and capped at 300 characters, and non-text values are ignored. The file belongs to `sla-report-slack`, so edit it there and commit and push, after a `git pull` because its workflow commits to it too. Its job rewrites only the current day's entry (a note on today's date is lost if it re-runs that day) and keeps the last 90 days, so a note ages out with its day.

Charts need the network: Chart.js, hammer.js and the zoom plugin load from cdnjs (pinned versions), and offline the charts simply don't draw. Colors are read from the active theme's CSS variables, and the charts rebuild when the theme changes.

The thresholds quoted at the top of the page (initial response 2 h, requester wait 4 h, no update 8 h, resolution age 40 h, all business hours) are also applied to the live queue. Each `/sla` load runs a scan of non-solved tickets (`_common.fetch_sla_tickets`, cached 2 minutes and bounded by `SLA_TICKET_SCAN_PAGES`) whose only visible results are the **SLA** nav badge's breached count and an on-page note if the scan failed or hit its cap. The page lists no per-ticket rows. Live elapsed hours are computed here from ticket comments, so they can differ by a few minutes from what `sla-report-slack` records for the same ticket.

### My tasks (`/tasks`)

Turns "create a personal task ticket in Zendesk" into a two-step flow, so a jotted idea doesn't become a ticket the moment it's typed. It is meant to behave like the sibling "Zendesk Personal Task Tracker" skill.

1. **Draft (local only).** The **Add task** form takes a subject, optional notes, an optional **Category** (**[Documentation]** or **[Configuration]**, which auto-fills a leading bracketed tag on the subject, so the tag does ride along into Zendesk) and an optional Estimated minutes (it only pre-fills the promote form). Each draft can be **Edited** inline, moved **up/down** (a simple two-entry swap), or **Discarded**. Edit and Move refuse on an already-promoted task, since its ticket should be edited in Zendesk.
2. **Promote.** **Make Zendesk Ticket from Task** is the only moment a ticket is created: self-requested and self-assigned by default, priority `low`, type `task`, tagged `personal_task` (which the main queue excludes), filed under the IT-Operations group, with the "IT Requests" form's custom fields set (Department always IT; Ticket Allocation chosen; Estimated minutes required). The form also offers **Assignee** and **Requester** (Catherine Vee, your own account from `ZENDESK_EMAIL`, or "IT-Operations", resolved independently via `_common.resolve_personal_task_identity`) and **Allocation** (**CSA** or **CSAIF**). The CSAIF API value is a best guess that hasn't been confirmed against the live form: if promoting with CSAIF fails field validation, check `_common.PERSONAL_TASK_ALLOCATION_CSAIF`. The group is set by a follow-up `update_ticket` call because the shared `create_ticket` has no group parameter; if it fails, the task is still promoted, with a warning. Nothing is posted on the ticket at creation. The chosen identities and allocation are remembered and resent on solve, since Zendesk requires the same custom fields on both writes.
3. **Solve (in Zendesk).** The page lists only drafts; once a task is promoted it is no longer shown here, and you solve its ticket in Zendesk. The old **Open** panel with its **Quick Solve** and **Mark done** buttons was removed. The **Add + Quick Solve** button on the new-task form still creates an already-solved ticket in one step (it needs Estimated minutes), and the solve routines behind the removed buttons remain in `_common.py` and `review_server.py` but have no button.

The draft list is read from a local `personal_tasks.json`, not a live Zendesk query (the client can filter only by one group id), and shows full width in two columns (one on a narrow screen). Each page load first reconciles that file against live ticket status (`_common.reconcile_personal_tasks`), so a promoted task solved in Zendesk moves into the **Recently completed** collapsible at the bottom.

Both `pending_drafts.json` and `personal_tasks.json` archive terminal entries (posted, dismissed, handled elsewhere, completed) older than 30 days into sibling `*_archive.json` files, as part of the detection scan and the `/tasks` reconcile pass respectively. Active entries are never touched.

### Read-only pages

All of these share one row renderer (`_render_readonly_ticket_row`), show no drafting or already-replied checks, and offer **Claim** on tickets not assigned to you.

- **IT-Security** (`/security`): that group's `IT_SECURITY_COLUMN_LIMIT` most recent tickets in any status, a recent-activity snapshot rather than a first-reply queue. Claiming from here changes only Zendesk; the page's cache refreshes on its own.
- **Blocked/Standup** (`/flagged`): every non-solved/closed IT-Operations ticket tagged `block` or `standup`, in two side-by-side panels (a ticket with both tags appears in both). It is separate from the main queue on purpose, because that queue shows only tickets still needing a first reply. Zendesk's API has no "any ticket with this tag" filter, so `_common.fetch_flagged_tickets` pages through the group sorted by `updated_at` and checks tags in Python, bounded by `FLAGGED_TICKET_SCAN_PAGES` (default 10) and Zendesk's hard 1000-result `/search.json` ceiling (`ZENDESK_SEARCH_RESULT_CAP`; a page past it returns a 422). If a page fails mid-scan, results already found are kept, and a note says the scan stopped early.
- **Recently Solved** (`/solved`): your last `RECENTLY_SOLVED_LIMIT` (40) solved tickets in the IT-Operations group, with a "solved *N*h ago" time. Solved tickets assigned to you in other groups don't appear, because the scan is group-scoped. The same "stopped early" note applies (`SOLVED_TICKET_SCAN_PAGES`, default 10).
- **Overview** (`/overview`): who holds which open IT-Operations tickets. One card per person (you first, then Jesstin, Nekoashi and Ryan) and an Unassigned card, each listing that person's new/open/pending/hold tickets oldest first, with age, an SLA-not-met or due-soon chip naming the rule, and Claim on tickets that aren't yours. Tickets held by anyone else are only counted in a note. It reuses the `/sla` page's cached scan (`_common.fetch_sla_tickets`, personal tasks excluded), so it adds no Zendesk calls of its own. `OVERVIEW_PEOPLE` sets the names (comma-separated, exact Zendesk display names, first = you).

**Nav badges.** The count of personal tasks not yet in Zendesk and the Zendesk OAuth warning come from cheap local files, so they are always current. The SLA, Blocked/Standup and IT-Security counts read those pages' in-process caches (`_common.cached_sla_breach_count`/`cached_flagged_ticket_count`/`cached_security_ticket_count`) and never call Zendesk, so they are "warm from your last visit" and disappear when the 2-minute cache goes stale. Badges are neutral gray except the warning ones.

### Standup (`/standup`)

Tracker for the Operations Standup notes. Everything except the Zendesk tickets section comes from the separate `meetingnotestool` project, which parses the notes and owns all status logic; these pages display its view and record your decisions. Pages are server-rendered, work without JavaScript, and filter through plain query parameters.

- **Overview** (`/standup`): what changed since an earlier standup (`?since=` picks any of the last 8), your open action items (oldest first), the active topics you own, active work grouped by what it's waiting on, Zendesk tickets, your recently resolved items, and quiet topics needing a decision.
- **Workday recap** (`/standup/yesterday`): your ticket activity for the previous workday (public replies, internal notes, tickets you solved), so you don't recount it for standup. `?date=YYYY-MM-DD` and previous/next links choose another workday. The data comes from a scan of the IT-Operations group, cached per day, and falls back to this tool's local reply history if Zendesk is unreachable. A **Copy as text** button copies the list.
- **By person** (`/standup/people?person=&q=`): one person's action items, as the notes attribute them. Open items come first (oldest first, with how long each has been open), then done and dropped ones, newest first. Each row shows the standup it came from (linked to those notes) and its topic, and an owner the notes only implied is shown with the dotted underline. A picker lists only the people in `STANDUP_PEOPLE` (not the Group/Unassigned buckets) and opens on `STANDUP_DEFAULT_PERSON` (default `Ryan`). It is read-only, with no decision buttons on anyone else's items.
- **Topic history** (`/standup/topic/<key>`), **Workstreams** (`/standup/workstreams?q=&owner=`), **Meetings** (`/standup/meetings[/<date>]`), **Action items** (`/standup/actions?show=open|resolved|all&owner=&q=`), and **Closed** (`/standup/closed`).

Owners with a dotted underline were inferred, and a "check" tag marks a guessed name or detail. The nav badge counts your open standup action items.

**Your decisions.** Since the notes rarely say when something is finished, you can record calls: **Done** / **Dropped** on your open action items; **Still open** (for an item the notes resolved by mistake) or **Undo** on resolved ones; **Done** / **Dropped** / **Keep watching** on quiet topics. Each button returns you to the page you clicked it on. Each call is written to `meetingnotestool/decisions.json` (`_common.record_standup_decision`, locked, atomic) and shown immediately. That file holds only IDs, the decision and a timestamp, and is the only thing this tool writes in that project. Its next build applies the decisions permanently; a topic raised again at a later standup returns for a new decision. A decision is refused if its item isn't in the current view or the `meetingnotestool` folder is missing. `STANDUP_DECISIONS_FILE` redirects the write. That project's data is private, so none of it is stored here and tests use made-up fixtures.

**Links into the rest of the tool.** **Add to My tasks** on an open action item saves it as a local My tasks draft (`_common.add_standup_action_to_tasks`); nothing reaches Zendesk until you promote it. The draft records its source, so the button becomes an **In My tasks** chip and a second click can't duplicate it. Its notes name only the standup, so promoting it doesn't carry the meeting notes into Zendesk. Ticket numbers in the notes ("ticket 123456", "#123456") link to the ticket, and the **Zendesk tickets** section lists open `standup`-tagged tickets (from the same cached read as `/flagged`, the page's only Zendesk call) and then any other mentioned tickets. If Zendesk isn't connected, it says so.

## Design principles

The reasoning behind each decision is in [`docs/DESIGN.md`](docs/DESIGN.md); this is the condensed list.

**Safety and review**

- **Nothing posts to Zendesk without a human click.** The cron script only reads and drafts; only the review server writes, and only on an explicit Public Reply, Internal Comment, Claim or promote click. Remove only stops this tool tracking a ticket.
- **A candidate stays on the page until you act on it, you remove it, or Zendesk resolves it.** The one automatic exit is the per-scan already-replied sweep (Zendesk's own state overriding the queue). Clicking a post button on an already-replied ticket stops and asks.
- **Queue mutations re-check current status inside the same lock** and never trust what the browser last rendered, so a stale tab can't overwrite what a scan or another tab already did.
- **A failed post leaves the draft intact.** There are no bulk actions: every reply is reviewed individually.
- **The assignee badge is a hint, never a gate.** Zendesk's Assignee can be stale or irrelevant, so it never blocks a post or pulls a ticket. It costs one extra `get_ticket` per pending ticket per scan, an accepted tradeoff.
- **Claim is a plain assignee change**, reversible and nothing posted, so it needs no interstitial. Taking over someone else's ticket asks for one in-page confirmation.
- **Localhost only, no login.** The server binds to `127.0.0.1`; there is deliberately no auth layer beyond being logged into this Mac.
- **Credential-bearing files are tightened.** `AUTH_STATE_FILE`, `OAUTH_PENDING_FILE` and the shared token file are `chmod`'d to `0600` after writes (`_harden_file_permissions`), best-effort.
- **Private data stays out of this public repo.** `/standup` reads `meetingnotestool` data from that checkout at request time (`_common.standup_snapshot`, mtime-cached), never copying or caching it. `scripts/tests/conftest.py` points `STANDUP_DATA_FILE` and `STANDUP_DECISIONS_FILE` at temp paths, so tests can't read real notes or write real decisions.
- **A read-only dependency stays read-only.** This tool only reads `sla-report-slack`'s `sla_history.json`. It never pulls, writes or commits there, so it can't create a merge commit or send that repo's email.

**Data, caching and concurrency**

- **Slow reads are stale-while-revalidate; safety checks are never cached.** The main queue's live "Recently worked on" scan (`fetch_recently_worked_on`, tens of seconds cold) is served instantly from its last result; once older than 2 minutes, one background thread refreshes it (a failed refresh keeps the last good result and backs off 30 seconds). Only a missing cache, `force`, or a result over an hour old makes a request wait. Only the live half is cached; local reply history is merged in fresh on every call. The review server fills this cache at startup (`_common.warm_caches`). Separately, `_common.cached_ticket_comments` caches comment lists keyed on (ticket id, `updated_at`), which is exact because a ticket's comments can't change without its `updated_at` changing (6-hour expiry and a 1000-entry LRU bound as backstops). It is used only where the ticket came from a live listing in the same scan (candidate detection, Recently worked on, the workday recap, the SLA scan). The click-time already-replied check and the per-scan re-check of pending tickets always read live.
- **Local-first, with bounded live reads.** The main queue reads only local files and a cached auth state, so loading it never depends on Zendesk being reachable. Exceptions are narrow: the Recently worked on sidebar's live half, `/security` and `/flagged`. Each result is cached in-process for 2 minutes (the cron cadence), served stale-but-instant, and falls back to the last good result (or local-only data) on failure.
- **Scans that Zendesk can't filter are done in Python, bounded and honest.** `fetch_flagged_tickets`, `fetch_recently_solved_tickets`, `fetch_recently_worked_on` and the workday recap page through a group sorted by `updated_at`, with page/comment budgets and a "stopped early" note, so a gap reads as "might be missing", never "nothing more". Tickets already known locally are never re-checked.
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

- **Read another tool's numbers; don't recompute them.** Daily history comes from `sla-report-slack`; this page reshapes `sla_history.json` (`_common.sla_chart_data`) and says how fresh it is. Only the nav badge's live count is computed here.
- **Missing is not zero.** A field absent from a day's snapshot becomes `null`, drawn as a gap, because 0 h is a real median and 0 breaches is a real day. Rule counts are blank on days from before the history recorded them.
- **Outliers are clamped visibly, not hidden.** Response-time values beyond 4x the limit sit at the top of the axis as triangles with the true value in the tooltip, so one bad day doesn't flatten the rest.
- **Charts match the sibling dashboard.** Chart.js (4.4.1) with the zoom plugin is the one external script dependency in the tool, a deliberate exception to the one-self-contained-response style, so these charts behave like `sla_dashboard.html`'s. Theme colors are read from CSS variables at build time.
- **Keep only what's asked for.** An earlier KPI strip, rule cards and awaiting-first-response list were removed on request. The live scan stays, because it feeds the nav badge.

**UI**

- **A new workflow gets a new page**, and a page's status shows where you are: nav badges are computed from cheap local files or warm caches, never a new Zendesk call. A badge exists only where the count is a signal, so Recently Solved has none.
- **One primary action per page, everything else outlined.** Icons are a fixed set of hand-authored inline SVGs (`_ICONS`, `currentColor`). Decorative topic icons (`_topic_icon()`) are separate self-contained colored PNG badges with empty `alt` text and degrade to nothing if a file is missing.
- **Avatars only beside names the tool trusts** (`_avatar_html`, `_requester_avatar_prefix`), never beside an unconfirmed guess.
- **The toast and the auth banner share CSS but not behavior.** The toast (`data-toast`) floats and auto-dismisses; the auth warning stays docked, since a standing condition shouldn't vanish on a timer.
- **The fade-on-submit is cosmetic**, not an optimistic update. If a post fails, the card reappears.
- **A header is a glance, not a dashboard.** The main page uses two small stat chips.
- **Use the width available.** Compact rows go in `.mini-ticket-grid` and side-by-side panels in `.two-panel`; short pages like `/reconnect` are capped and centered instead of padded.
- **A basic accessibility pass, not a full audit.** The toast is a live region, the queue sits in `<main>`, and flag text meets WCAG AA contrast in light and dark. There has been no full screen-reader or keyboard-only pass.
- **Two sections answering the same question should be one.** Recently worked on replaced the old "Last N replies" section. Its history widened to 30 and subjects wrap rather than truncating.

**Theming**

- **A theme's swaps are complete, and done in CSS where possible.** B68FBF swaps the logo, topic-icon set and avatar palette (`.avatar-hue-N`, generated from `_AVATAR_HUES`/`_AVATAR_HUES_B68FBF`) via classes on `<html>` that `theme_init_script` sets before first paint, so there is no flash. The favicon and `theme-color` meta need a few lines of JS in `theme_init_script`/`applyTheme()`. `::selection` and `scrollbar-color` read `--accent`/`--bg` and follow every theme for free.
- **Seasonal themes override only variables.** Halloween and Christmas reuse the whole variable set, add `--on-accent` for text on the bright active pills, and force a dark `color-scheme`. Functional signal colors come from the Dark theme.

**Loading indicator**

- **Show measured progress where possible, nothing precise where not.** There is no client-side router, so every navigation is a full reload the browser can't see into; progress is measured on the server. A click or submit sets a short-lived `csa_pid` cookie. A transparent wrapper that `make_client()` puts around the Zendesk client counts the API calls the request makes (`_common.progress_*`). The page polls `/__progress?id=...` for done-versus-expected, where expected is the call count of the previous load of the same route.
- **The first load of a route has no baseline**, so the bar creeps toward 90% instead of showing an unjustified number, and is capped at 95% until the server reports the load finished. A load with no API calls (warm caches) finishes in milliseconds. The cookie rides along a form's redirect, so a POST that redirects to a slow GET is one continuous bar. If the navigation never reaches the server, the bar hides after a few seconds.
- **Quiet by design.** The automatic `/firstreply` refresh starts neither bar nor card. Same-page `#` links, `mailto:`/`tel:`, modifier-clicks and `target="_blank"` are excluded. `/__progress` and `/healthz` touch only in-memory state, answer instantly even while a page is blocked on Zendesk, and `/__progress` is left out of the access log. The bar uses `--accent`, so it follows every theme.
- **The detail card is a second stage.** After about 1.5 seconds the dim and card appear; neither blocks clicks.

**Verification**

- **Checks that don't outlive a session don't protect anything.** `scripts/tests/` is pure Python with no browser and no real Zendesk credentials, wired into `.github/workflows/tests.yml` so it runs on every push and PR.

## Repo layout

```
requirements.txt                              runtime dependencies (pinned)
requirements-dev.txt                          + pytest, for running scripts/tests/
docs/DESIGN.md                                full original documentation and design rationale
.github/workflows/tests.yml                   CI: runs scripts/tests/ on every push/PR
scripts/
  _common.py                                  shared config, pending-store, progress tracking and scan logic
  first_reply_check.py                        the 2-minute detect-and-draft cron job
  review_server.py                            the review/approve web server + OAuth reconnect
  watchdog.py                                 standalone health check for the services above
  com.csa.zendesk-first-reply-check.plist     launchd config for the cron job
  com.csa.zendesk-review-server.plist         launchd config for the web server
  com.csa.zendesk-watchdog.plist              launchd config for the watchdog (every 5 min)
  reload_launchd.sh                           (re)installs all three services
  csa_notifier.applescript                    source for the optional "CSA Notifier.app" helper
  recolor_topic_icons.py                      generates the B68FBF topic-icon set
  assets/                                     csa-logo.png, b68fbf-logo.png, topic-icons/
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

**Covered:** the personal-task lifecycle and its shared validation; the atomic-write-under-flock guarantee for both stores (with real concurrent writer threads); the nav badge caches and their locks; form-body parsing edge cases; the standup pages and the workday recap; the measured-progress counting, history and `/__progress` wiring (including a real HTTP test); the seasonal-theme markup; the comment cache and stale-while-revalidate behavior, including that click-time checks stay live; the `/sla` chart series (gaps versus zeros) and markup; the `/overview` grouping by assignee and its page; the `/tasks` page layout (drafts only, two columns) and its nav badge.

**Not covered:** anything JS-driven in a real browser (the loading bar's drawing and the detail card, the `/sla` charts' drawing, theme switching, the category auto-fill listener) and full end-to-end page rendering. Those are checked manually with Playwright during development. Promote, complete and quick-solve, which need a live-ish client, are not covered yet either.

## Troubleshooting

- **Logs** are in `scripts/output/`: `first_reply_check.log` (one line per cron run), `review_server.out.log` / `review_server.err.log`, and `watchdog.out.log` / `watchdog.err.log`. `watchdog_state.json` is the watchdog's "already alerted" memory, and its modification time shows when it last ran.
- **The page won't load:** confirm the service is running with `launchctl list | grep com.csa`.
- **Tickets aren't being detected:** look for "not authenticated" in the cron log; that's the OAuth token, fixed through the in-page `/reconnect` flow.
- **A refresh reports "N candidate(s) found, 0 new":** expected, not a bug. `find_candidates()` skips any ticket id already tracked in `pending_drafts.json` (whatever its status), most commonly because it's already `posted` while Zendesk still shows it `new`/`open`. It stops appearing once the ticket's status changes.
- **`ModuleNotFoundError: No module named 'zendesk_mcp_server'` after a restart:** the sibling repo's venv lost its editable install (the `.pth` file was present but not read, cause never found). Confirm with `zendesk-mcp-server/.venv/bin/python3 -c "import zendesk_mcp_server"`, then reinstall with `.venv/bin/python3 -m pip install -e .` from that repo's root. Both plists also set `PYTHONPATH` to that repo's `src/` as a standing safety net.
- **Plist changes didn't take effect:** `launchctl unload`/`load` can silently no-op, leaving the old definition running. `reload_launchd.sh` uses `launchctl bootout`/`bootstrap` instead; prefer those if you reload by hand. A loaded job doesn't pick up an edited plist on disk until it is booted out and bootstrapped again.
- **`reload_launchd.sh` details:** it isn't executable in git, so run `zsh scripts/reload_launchd.sh`. If it prints `Bootstrap failed: 5: Input/output error` for one service, the service may have been mid-restart and be running fine: check `launchctl list | grep com.csa` (a PID in the first column means running) and `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/`.
- **Restarting just the review server** (needed after changing `review_server.py` or `_common.py`; `watchdog.py` changes need no reload because `launchd` re-runs it): kill whatever holds the port first, since `bootout` can race with a hung process, then wait a few seconds before the port accepts connections.

  ```
  lsof -ti :8765 | xargs kill -9 2>/dev/null; launchctl bootout gui/$(id -u)/com.csa.zendesk-review-server 2>/dev/null; sleep 2; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist
  ```
- **The watchdog** checks every 5 minutes that the review server answers HTTP (via the lightweight `/healthz` route, so a slow Zendesk scan can't trip it) and that `first_reply_check` has completed a scan recently during business hours, and notifies as soon as either looks stuck. It has no dependency on `_common.py` or `zendesk_mcp_server`, so it keeps working if that import chain breaks, which is why it exists: the `ModuleNotFoundError` above once broke both services unnoticed. A "Connection reset by peer" alert with a working page usually means a hung request, not a down server; restart as above.
- **`watchdog.err.log` fills with `can't open file '.../watchdog.py': [Errno 1] Operation not permitted`:** macOS privacy protection is blocking the watchdog's interpreter from reading `~/Desktop` (this happened with the Command Line Tools `/usr/bin/python3`). The watchdog plist points at the standalone uv-managed CPython the other two services use. If a `uv` upgrade removes that exact version, update the path in `com.csa.zendesk-watchdog.plist`, copy it to `~/Library/LaunchAgents/`, and reload the watchdog.
