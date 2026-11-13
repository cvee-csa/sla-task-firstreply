#!/bin/zsh
# Installs, or reloads, the three Zendesk local-automation launchd services
# from sla-task-firstreply/scripts/.
#
# Usage:
#   zsh scripts/reload_launchd.sh            install / reload all three jobs
#   zsh scripts/reload_launchd.sh --check    resolve paths and run the
#                                            prerequisite checks only; touches
#                                            neither ~/Library/LaunchAgents
#                                            nor launchctl
#
# Paths are NOT hard-coded. This repo's location comes from where this script
# lives, and the sibling zendesk-mcp-server checkout is found in this order:
#   1. $ZENDESK_MCP_DIR, if set
#   2. ../zendeskmcp/zendesk-mcp-server   (next to this repo)
#   3. ../zendesk-mcp-server              (next to this repo)
# A candidate counts only if it has a .venv/bin/python3.
#
# The three plists in this folder still contain the original author's paths,
# so they stay valid as written. On every run those paths are replaced with
# the resolved ones while the plists are copied into ~/Library/LaunchAgents;
# the files in the repo are never modified. On a checkout that already sits at
# the original paths the replacement changes nothing.
#
# Optional override for the watchdog's interpreter (see the watchdog plist for
# why it avoids the zendesk-mcp-server venv): $WATCHDOG_PYTHON. Without it, the
# plist's interpreter is kept if it exists on this machine, and otherwise the
# real interpreter underneath the venv is used.
#
# Uses `launchctl bootout` + `bootstrap` rather than the older `unload` +
# `load`: unload/load can silently fail to apply a changed plist (the job keeps
# running with its OLD definition even though the reload reported success).
# bootout/bootstrap tear a job down and re-register it from the plist on disk.
#
# Safe to re-run: every step is written to no-op cleanly if already done.

check_only=0
[[ "$1" == "--check" ]] && check_only=1

REPO_ROOT="${0:A:h:h}"
cd "$REPO_ROOT" || { echo "Could not cd to $REPO_ROOT"; exit 1; }

# Paths exactly as they appear in the committed plists.
PLIST_REPO="/Users/catherinevee/Desktop/git/sla-task-firstreply"
PLIST_MCP="/Users/catherinevee/Desktop/git/zendeskmcp/zendesk-mcp-server"
PLIST_WATCHDOG_PY="/Users/catherinevee/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12"

MCP_DIR=""
for candidate in "${ZENDESK_MCP_DIR:-}" \
                 "$REPO_ROOT/../zendeskmcp/zendesk-mcp-server" \
                 "$REPO_ROOT/../zendesk-mcp-server"; do
    if [[ -n "$candidate" && -x "$candidate/.venv/bin/python3" ]]; then
        MCP_DIR="${candidate:A}"
        break
    fi
done
if [[ -z "$MCP_DIR" ]]; then
    echo "ERROR: could not find the zendesk-mcp-server checkout (needs a .venv/bin/python3)."
    echo "  Set ZENDESK_MCP_DIR to its folder and re-run, for example:"
    echo "    ZENDESK_MCP_DIR=\$HOME/code/zendesk-mcp-server zsh scripts/reload_launchd.sh"
    exit 1
fi
VENV_PY="$MCP_DIR/.venv/bin/python3"

if [[ -n "${WATCHDOG_PYTHON:-}" ]]; then
    WATCHDOG_PY="$WATCHDOG_PYTHON"
elif [[ -x "$PLIST_WATCHDOG_PY" ]]; then
    WATCHDOG_PY="$PLIST_WATCHDOG_PY"
else
    WATCHDOG_PY="$("$VENV_PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
fi
if [[ ! -x "$WATCHDOG_PY" ]]; then
    echo "ERROR: watchdog interpreter not executable: $WATCHDOG_PY"
    exit 1
fi

echo "Repo:               $REPO_ROOT"
echo "zendesk-mcp-server: $MCP_DIR"
echo "Services python:    $VENV_PY"
echo "Watchdog python:    $WATCHDOG_PY"

# Legacy: an older setup staged the file as env-for-dotenv.txt.
if [[ -f env-for-dotenv.txt && ! -f .env ]]; then
    mv env-for-dotenv.txt .env
    echo "Renamed env-for-dotenv.txt -> .env"
fi
if [[ ! -f .env ]]; then
    echo "ERROR: $REPO_ROOT/.env not found. Create it first:"
    echo "  cp .env.example .env     # then fill in the values"
    exit 1
fi

# Warn (not abort) when the services' imports fail: the plists set PYTHONPATH,
# so this repeats what launchd will do.
if ! PYTHONPATH="$MCP_DIR/src" "$VENV_PY" -c \
        'from zendesk_mcp_server.zendesk_client import ZendeskClient; import dotenv' 2>/dev/null; then
    echo "WARNING: $VENV_PY cannot import zendesk_mcp_server and python-dotenv."
    echo "  Fix with:"
    echo "    $VENV_PY -m pip install -r $REPO_ROOT/requirements.txt"
    echo "    (and, inside $MCP_DIR:  .venv/bin/python3 -m pip install -e .)"
fi

if (( check_only )); then
    echo "Check complete (--check): nothing installed."
    exit 0
fi

if ! command -v launchctl >/dev/null 2>&1; then
    echo "ERROR: launchctl not found. These services need macOS (launchd)."
    exit 1
fi

# launchd opens the log files before the job starts and refuses to start the
# job when their folder is missing; scripts/output/ is git-ignored, so a fresh
# checkout does not have it.
mkdir -p scripts/output
AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    print -rn -- "$s"
}

install_plist() {
    local name="$1" content
    content="$(<"scripts/$name.plist")"
    content="${content//$PLIST_WATCHDOG_PY/$(xml_escape "$WATCHDOG_PY")}"
    content="${content//$PLIST_MCP/$(xml_escape "$MCP_DIR")}"
    content="${content//$PLIST_REPO/$(xml_escape "$REPO_ROOT")}"
    print -r -- "$content" > "$AGENTS_DIR/$name.plist"
    if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$AGENTS_DIR/$name.plist" >/dev/null; then
        echo "WARNING: plutil reports $AGENTS_DIR/$name.plist is not a valid plist"
    fi
}

install_plist com.csa.zendesk-first-reply-check
install_plist com.csa.zendesk-review-server
install_plist com.csa.zendesk-watchdog
echo "Installed all three plists into $AGENTS_DIR"

reload_job() {
    local label="$1"
    # bootout errors (to stderr, suppressed) if the job isn't loaded yet --
    # expected and fine on a first run. bootstrap's own errors are NOT
    # suppressed: a real failure here (e.g. a malformed plist) should be
    # visible instead of looking like a silent success.
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    if launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$label.plist"; then
        echo "Reloaded $label"
    else
        echo "FAILED to (re)load $label -- check the plist above for the actual error"
    fi
}

reload_job "com.csa.zendesk-first-reply-check"
reload_job "com.csa.zendesk-review-server"
reload_job "com.csa.zendesk-watchdog"

echo ""
echo "Done. Verify with:"
echo "  curl http://127.0.0.1:8765/healthz"
echo "and tail:"
echo "  $REPO_ROOT/scripts/output/first_reply_check.log"
echo "  $REPO_ROOT/scripts/output/review_server.out.log"
echo "  $REPO_ROOT/scripts/output/watchdog.out.log"
