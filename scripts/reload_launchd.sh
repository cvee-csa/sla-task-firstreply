#!/bin/zsh
# Reloads the two Zendesk local-automation launchd services from
# sla-task-firstreply/scripts/, and renames the staged .env into place.
# Safe to re-run: every step below is written to no-op cleanly if it was
# already done on a previous attempt.

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
echo "Copied both plists into ~/Library/LaunchAgents/"

launchctl unload ~/Library/LaunchAgents/com.csa.zendesk-first-reply-check.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.csa.zendesk-first-reply-check.plist 2>/dev/null
echo "Reloaded com.csa.zendesk-first-reply-check"

launchctl unload ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.csa.zendesk-review-server.plist 2>/dev/null
echo "Reloaded com.csa.zendesk-review-server"

echo ""
echo "Done. Check http://127.0.0.1:8765/ in a browser, and tail:"
echo "  $REPO_ROOT/scripts/output/first_reply_check.log"
echo "  $REPO_ROOT/scripts/output/review_server.out.log"
echo ""
echo "Optional cleanup once you've confirmed everything works:"
echo "  rm -rf /Users/catherinevee/Desktop/git/zendeskmcp/zendesk-mcp-server/scripts/_to_delete"
