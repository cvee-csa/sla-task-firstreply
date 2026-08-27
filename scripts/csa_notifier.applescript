use framework "Foundation"
use scripting additions

-- This path must match _common.OUTPUT_DIR / "notify_payload.json" in
-- first_reply_check.py -- i.e. scripts/output/notify_payload.json inside
-- this repo checkout. Update it here if you ever move the repo.
set payloadPath to "/Users/catherinevee/Desktop/git/sla-task-firstreply/scripts/output/notify_payload.json"

try
	set nsData to current application's NSData's dataWithContentsOfFile:payloadPath
	set nsDict to current application's NSJSONSerialization's JSONObjectWithData:nsData options:0 |error|:(missing value)
	set theTitle to (nsDict's valueForKey:"title") as text
	set theMessage to (nsDict's valueForKey:"message") as text
	set theSound to (nsDict's valueForKey:"sound") as text
on error
	-- No payload file yet (e.g. this is the first manual test run) --
	-- show a generic banner instead of failing silently, so double-clicking
	-- this app is a real end-to-end test of whether macOS will display its
	-- notifications at all.
	set theTitle to "CSA Notifier"
	set theMessage to "Ready -- this is a one-time test banner, not a real ticket alert."
	set theSound to "Glass"
end try

if theSound is "" then
	display notification theMessage with title theTitle
else
	display notification theMessage with title theTitle sound name theSound
end if
