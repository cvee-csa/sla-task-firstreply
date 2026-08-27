"""Local-only personal-task lifecycle: add -> edit -> reorder -> discard, and
the shared est_mins validation every one of those (plus promote/complete,
covered separately) routes through. None of this needs a real Zendesk
client -- these are the operations _common.py itself documents as "no
Zendesk call at all" (add_personal_task's own docstring).

Promote/complete/quick-solve (the operations that DO need a live client)
aren't covered here yet -- see the "Running tests" section of the README
for that scope note; they remain covered by the ad hoc Playwright-based
verification scripts used during development.
"""
import _common


# --- _parse_est_mins ---------------------------------------------------------

def test_parse_est_mins_blank_is_valid_none():
    assert _common._parse_est_mins(None) == (None, None)
    assert _common._parse_est_mins("") == (None, None)


def test_parse_est_mins_accepts_a_positive_whole_number():
    assert _common._parse_est_mins("45") == (45, None)


def test_parse_est_mins_rejects_zero_and_negative():
    parsed, error = _common._parse_est_mins("0")
    assert parsed is None and error

    parsed, error = _common._parse_est_mins("-5")
    assert parsed is None and error


def test_parse_est_mins_rejects_non_numeric_without_raising():
    parsed, error = _common._parse_est_mins("not a number")
    assert parsed is None and error


def test_parse_est_mins_rejects_over_the_max():
    too_big = _common.PERSONAL_TASK_EST_MINUTES_MAX + 1
    parsed, error = _common._parse_est_mins(str(too_big))
    assert parsed is None
    assert str(_common.PERSONAL_TASK_EST_MINUTES_MAX) in error


# --- add_personal_task --------------------------------------------------------

def test_add_personal_task_requires_a_subject():
    result = _common.add_personal_task("   ", "")
    assert result == {"ok": False, "error": "A task needs a subject."}
    assert _common.draft_personal_tasks() == []


def test_add_personal_task_rejects_invalid_est_mins_without_saving_anything():
    result = _common.add_personal_task("Write onboarding doc", "", est_mins="soon")
    assert result["ok"] is False
    assert _common.draft_personal_tasks() == []


def test_add_personal_task_saves_a_draft_with_the_given_fields():
    result = _common.add_personal_task(
        "Rotate API key", "check the vault first", est_mins="15", category="[Configuration]"
    )
    assert result["ok"] is True
    drafts = _common.draft_personal_tasks()
    assert len(drafts) == 1
    entry = drafts[0]
    assert entry["subject"] == "Rotate API key"
    assert entry["notes"] == "check the vault first"
    assert entry["est_mins"] == 15
    assert entry["category"] == "[Configuration]"
    assert entry["status"] == "draft"
    assert entry["local_id"] == result["local_id"]


def test_add_personal_task_defaults_to_newest_first():
    _common.add_personal_task("First task", "")
    _common.add_personal_task("Second task", "")
    drafts = _common.draft_personal_tasks()
    assert [d["subject"] for d in drafts] == ["Second task", "First task"]


# --- edit_personal_task --------------------------------------------------------

def test_edit_personal_task_updates_fields_in_place():
    added = _common.add_personal_task("Original subject", "original notes")
    local_id = added["local_id"]

    result = _common.edit_personal_task(local_id, "Fixed subject", "fixed notes", est_mins="20", category="[Documentation]")
    assert result == {"ok": True, "error": None}

    entry = _common.draft_personal_tasks()[0]
    assert entry["subject"] == "Fixed subject"
    assert entry["notes"] == "fixed notes"
    assert entry["est_mins"] == 20
    assert entry["category"] == "[Documentation]"


def test_edit_personal_task_rejects_a_blank_subject_without_mutating():
    added = _common.add_personal_task("Keep me", "")
    local_id = added["local_id"]

    result = _common.edit_personal_task(local_id, "   ", "new notes")
    assert result["ok"] is False
    # Original entry untouched -- a rejected edit must not partially apply.
    entry = _common.draft_personal_tasks()[0]
    assert entry["subject"] == "Keep me"
    assert entry["notes"] == ""


def test_edit_personal_task_rejects_unknown_local_id():
    result = _common.edit_personal_task("does-not-exist", "Subject", "")
    assert result == {"ok": False, "error": "No such task."}


# --- move_personal_task_draft --------------------------------------------------

def test_move_personal_task_draft_swaps_with_its_neighbor():
    first = _common.add_personal_task("First", "")
    second = _common.add_personal_task("Second", "")
    # Newest-first order right now: [Second, First].
    assert [d["subject"] for d in _common.draft_personal_tasks()] == ["Second", "First"]

    result = _common.move_personal_task_draft(first["local_id"], "up")
    assert result == {"ok": True, "error": None}
    assert [d["subject"] for d in _common.draft_personal_tasks()] == ["First", "Second"]


def test_move_personal_task_draft_at_the_top_is_a_no_op():
    only = _common.add_personal_task("Only task", "")
    result = _common.move_personal_task_draft(only["local_id"], "up")
    assert result == {"ok": True, "error": None}
    assert [d["subject"] for d in _common.draft_personal_tasks()] == ["Only task"]


def test_move_personal_task_draft_rejects_invalid_direction():
    added = _common.add_personal_task("Task", "")
    result = _common.move_personal_task_draft(added["local_id"], "sideways")
    assert result == {"ok": False, "error": "Invalid direction."}


# --- discard_personal_task ------------------------------------------------------

def test_discard_personal_task_removes_the_draft():
    added = _common.add_personal_task("Discard me", "")
    result = _common.discard_personal_task(added["local_id"])
    assert result["ok"] is True
    assert _common.draft_personal_tasks() == []


def test_discard_personal_task_rejects_unknown_local_id():
    result = _common.discard_personal_task("does-not-exist")
    assert result["ok"] is False
