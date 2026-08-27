#!/bin/zsh
# Reloads the three Zendesk local-automation launchd services from
# sla-task-firstreply/scripts/, and renames the staged .env into place.
# Safe to re-run: every step below is written to no-op cleanly if it was
# already done on a previous attempt.
#
# Uses `launchctl bootout` + `bootstrap` rather than the older `unload` +
# `load`. On this Mac, unload/load silently failed to actually apply a
# changed plist -- the job kept running with its OLD definition even
# though the reload reported success -- which is exactly what made an
# earlier PYTHONPATH fix look like it hadn't worked when it actually just
# hadn't been applied yet. bootout/bootstrap are the modern equivalent and
# reliably tear a job down and re-register it from the plist on disk.

REPO_ROOT="/Users/catherinevee/Desktop/git/sla-task-firstreply"
cd "$REPO_ROOT" || { echo "Could not cd to $REPO_ROOT"; exit 1; }

if [ -f env-for-dotenv.txt ]; then
    mv env-for-dotenv.txt .env
    echo "Renamed env-for-dotenv.txt -> .env"
elif [ -f .env ]; then
    echo ".env already in place -- skipping rename"
else
    echo "WARNING: neither env-for-dotenv.txt nor .env found in $REPO_ROOT"
fi

cp scripts/com.csa.zendesk-first-reply-check.plist ~/Library/LaunchAgents/
cp scripts/com.csa.zendesk-review-server.plist ~/Library/LaunchAgents/
cp scripts/com.csa.zendesk-watchdog.plist ~/Library/LaunchAgents/
echo "Copied all three plists into ~/Library/LaunchAgents/"

reload_job() {
    local label="$1"
    # bootout errors (to stderr, suppressed) if the job isn't loaded yet --
    # expected and fine on a first run. bootstrap's own errors are NOT
    # suppressed: a real failure here (e.g. a malformed plist) should be
    # visible instead of looking like a silent success.
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    if launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/"$label".plist; then
        echo "Reloaded $label"
    else
        echo "FAILED to (re)load $label -- check the plist above for the actual error"
    fi
}

reload_job "com.csa.zendesk-first-reply-check"
reload_job "com.csa.zendesk-review-server"
reload_job "com.csa.zendesk-watchdog"

echo ""
echo "Done. Check http://127.0.0.1:8765/ in a browser, and tail:"
echo "  $REPO_ROOT/scripts/output/first_reply_check.log"
echo "  $REPO_ROOT/scripts/output/review_server.out.log"
echo "  $REPO_ROOT/scripts/output/watchdog.out.log"
echo ""
echo "Optional cleanup once you've confirmed everything works:"
echo "  rm -rf /Users/catherinevee/Desktop/git/zendeskmcp/zendesk-mcp-server/scripts/_to_delete"
