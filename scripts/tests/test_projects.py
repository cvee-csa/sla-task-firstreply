"""/projects: the Airtable Projects board and its allowlisted write-back.

Airtable is replaced by FakeAirtable (installed over _projects._request), so no
test touches the network or a real base. Every record, person and address here
is made up."""
import http.client
import json
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

import _common
import _projects
import review_server

REC1 = "recAAAAAAAAAAAAAA"
REC2 = "recBBBBBBBBBBBBBB"
REC3 = "recCCCCCCCCCCCCCC"
PEOPLE = "Alex Example:alex@example.com,Sam Example:sam@example.com"


def _record(rec_id, name, **fields):
    base = {"Project name": name, "Status": "ACTIVE"}
    base.update(fields)
    return {"id": rec_id, "createdTime": "2026-01-01T00:00:00.000Z", "fields": base}


def _records():
    return {
        REC1: _record(REC1, "Rotate <b>keys</b>", **{
            "Assigned to": {"id": "usr1", "email": "alex@example.com", "name": "Alex Example"},
            "End date": "2026-01-15", "Blockers": "Waiting on vendor", "Sprint Notes": "Started",
            "Source Zendesk Ticket ID": "12345", "Urgency Rating": 4,
        }),
        REC2: _record(REC2, "Laptop refresh", Status="INTAKE", **{"End date": "2099-01-01"}),
        REC3: _record(REC3, "Old audit", Status="COMPLETE", **{"Actual Completion Date": "2026-02-02"}),
    }


class FakeAirtable:
    """Just enough of the REST API: list (with offset paging), get, patch, create, delete."""

    def __init__(self, records=None, page_size=100):
        self.records = records if records is not None else _records()
        self.calls = []
        self.fail = None
        self.page_size = page_size
        self.drop_writes = False      # simulate Airtable accepting a PATCH but returning stale values
        self.drop_creates = False     # simulate a POST whose reply holds no record

    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if self.fail:
            raise _projects.AirtableError(self.fail)
        parts = path.split("/")
        if method == "GET" and len(parts) == 2:
            ids = list(self.records)
            offset = dict(params or []).get("offset")
            start = ids.index(offset) if offset else 0
            chunk = ids[start:start + self.page_size]
            data = {"records": [self.records[i] for i in chunk]}
            if start + self.page_size < len(ids):
                data["offset"] = ids[start + self.page_size]
            return data
        if method == "POST" and len(parts) == 2:
            assert body["typecast"] is False
            self.created = getattr(self, "created", 0) + 1
            rid = "rec" + f"{self.created:014d}".replace("0", "N")[:14]
            fields = {}
            for name, value in body["fields"].items():
                if name == "Assigned to":
                    who = {"alex@example.com": "Alex Example", "sam@example.com": "Sam Example"}[value["email"]]
                    fields[name] = {"id": "usrX", "email": value["email"], "name": who}
                else:
                    fields[name] = value
            self.records[rid] = {"id": rid, "createdTime": "2026-01-01T00:00:00.000Z", "fields": fields}
            return {} if self.drop_creates else self.records[rid]
        if method == "DELETE" and len(parts) == 3:
            return {"id": parts[2], "deleted": True} if self.records.pop(parts[2], None) else {}
        rec = self.records[parts[2]]
        if method == "GET":
            return rec
        assert method == "PATCH"
        assert body["typecast"] is False
        if not self.drop_writes:
            for name, value in body["fields"].items():
                if value is None:
                    rec["fields"].pop(name, None)
                elif name == "Assigned to":
                    who = {"alex@example.com": "Alex Example", "sam@example.com": "Sam Example"}[value["email"]]
                    rec["fields"][name] = {"id": "usrX", "email": value["email"], "name": who}
                else:
                    rec["fields"][name] = value
        return rec

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def airtable(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "pat-test-token")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTESTBASE")
    monkeypatch.setenv("AIRTABLE_TABLE_ID", "tblTESTTABLE")
    monkeypatch.setenv("AIRTABLE_VIEW_ID", "viwTESTVIEW")
    monkeypatch.setenv("PROJECTS_ASSIGNEES", PEOPLE)
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    return fake


def _form(rec_fields, **changes):
    """The form the card posts: each slug at its current value, with `changes` applied."""
    now = _projects.current_values(_projects.normalize(rec_fields))
    form = {}
    for slug, value in now.items():
        form[slug] = [changes.get(slug, value)]
        form[f"orig_{slug}"] = [value]
    return form


# --- reading -----------------------------------------------------------------------

def test_not_configured_shows_setup_help_and_makes_no_call(monkeypatch):
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    result = _projects.fetch_projects()
    assert result["configured"] is False and fake.calls == []
    page = review_server._render_projects_page(result).decode()
    assert "Airtable is not set up" in page and "AIRTABLE_TOKEN=" in page
    assert "pat-" not in page


def test_normalize_reads_the_allowlisted_shapes(airtable):
    item = _projects.normalize(_records()[REC1])
    assert item["name"] == "Rotate <b>keys</b>" and item["assignee"] == "Alex Example"
    assert item["assignee_email"] == "alex@example.com" and item["ticket_ids"] == ["12345"]
    assert item["urgency"] == "4" and item["url"].endswith(f"viwTESTVIEW/{REC1}")


def test_fetch_follows_offsets_and_caches(airtable, monkeypatch):
    airtable.page_size = 2
    first = _projects.fetch_projects()
    assert [i["id"] for i in first["items"]] == [REC1, REC2, REC3]
    gets = [c for c in airtable.calls if c[0] == "GET"]
    assert len(gets) == 2 and ("offset", REC3) in gets[1][2]
    _projects.fetch_projects()
    assert len([c for c in airtable.calls if c[0] == "GET"]) == 2          # served from cache
    _projects.fetch_projects(force=True)
    assert len([c for c in airtable.calls if c[0] == "GET"]) == 4


def test_failed_reload_keeps_showing_the_last_copy(airtable, monkeypatch):
    _projects.fetch_projects()
    airtable.fail = "Airtable returned 503"
    stale = _projects.fetch_projects(force=True)
    assert stale["stale"] is True and len(stale["items"]) == 3 and "503" in stale["error"]
    page = review_server._render_projects_page(stale).decode()
    assert "last copy that loaded" in page and "Laptop refresh" in page


def test_first_load_failure_is_an_error_not_a_crash(airtable):
    airtable.fail = "Airtable returned 403 (INVALID_PERMISSIONS)"
    result = _projects.fetch_projects()
    assert result["items"] == [] and "403" in result["error"]
    assert "403" in review_server._render_projects_page(result).decode()


def test_page_has_a_column_per_status_and_escapes_everything(airtable):
    page = review_server._render_projects_page(_projects.fetch_projects(), today="2026-06-01").decode()
    body = page.split("</style>")[-1]
    visible = re.sub(r"<template.*?</template>", "", body, flags=re.S)      # what is on the board itself
    for status in _projects.STATUS_ORDER:
        assert f'data-status="{status}"' in body
    assert "Rotate &lt;b&gt;keys&lt;/b&gt;" in body and "<b>keys</b>" not in body
    assert "Past end date" in body                        # REC1 ended in January and is ACTIVE
    assert visible.count("Past end date") == 1            # not the future one, not the completed one
    assert "Waiting on vendor" in body and "#12345" in body
    assert 'href="/projects"' in body and "Projects</span>" in body
    assert "pat-test-token" not in page


def test_projects_page_uses_the_csa_topic_icons(airtable):
    page = review_server._render_projects_page(_projects.fetch_projects(), today="2026-06-01").decode()
    body = page.split("</style>")[-1]
    visible = re.sub(r"<template.*?</template>", "", body, flags=re.S)
    assert re.search(r"<h2><img src=\"data:image/png;base64,[^\"]+\" alt=\"\" class=\"topic-icon\">Projects</h2>", visible)
    for status in ("INTAKE", "SCOPING", "ACTIVE", "BLOCKED", "COMPLETE", "CANCELLED"):
        column = visible.split(f'data-status="{status}"')[1].split("</h2>")[0]
        assert 'class="topic-icon-sm"' in column, status
    assert "project-dates-legend" in body                 # panel templates carry the dates legend


def test_nav_links_use_badges_and_fall_back_to_line_glyphs():
    nav = review_server._top_nav_html("/projects")
    links = re.findall(r'<a class="top-nav-link[^>]*>.*?</a>', nav, flags=re.S)
    assert len(links) == 7
    for link in links:
        assert ('class="topic-icon-nav"' in link) != ("<svg" in link)       # exactly one kind of icon
    with_badge = [lk for lk in links if 'class="topic-icon-nav"' in lk]
    assert len(with_badge) == 7                                       # every link, Overview included
    assert "First Reply" in links[0] and 'class="topic-icon-nav"' in links[0]


def test_overview_heading_has_its_badge():
    assert review_server._topic_icon("overview") != ""
    assert review_server._TOPIC_ICON_FILES["overview"] == "matrix-csa-blue.png"


def test_new_topic_icon_files_are_small_square_pngs():
    import struct
    for key in ("projects", "project-triage", "project-INTAKE", "project-SCOPING", "project-ACTIVE",
                "project-BLOCKED", "project-COMPLETE", "project-CANCELLED", "dates", "nav-firstreply", "overview"):
        name = review_server._TOPIC_ICON_FILES[key]
        data = (review_server.TOPIC_ICONS_DIR / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 20_000, name
        width, height = struct.unpack(">II", data[16:24])
        assert width == height, name
        assert key in review_server.TOPIC_ICON_DATA_URIS


def test_edit_form_carries_only_allowlisted_fields(airtable):
    body = review_server._render_projects_page(_projects.fetch_projects()).decode().split("</style>")[-1]
    for slug in _projects.FIELD_SLUGS:
        assert f'name="orig_{slug}"' in body
    assert 'name="Project name"' not in body and 'name="Description"' not in body
    assert 'value="Alex Example" selected' in body or 'value="Alex Example"' in body


# --- planning ----------------------------------------------------------------------

def test_untouched_form_plans_no_changes(airtable):
    item = _projects.normalize(_records()[REC1])
    plan = _projects.plan_edit(_form(_records()[REC1]), item)
    assert plan["changes"] == [] and plan["errors"] == [] and plan["conflicts"] == []


def test_plan_sends_only_changed_fields(airtable):
    rec = _records()[REC1]
    plan = _projects.plan_edit(_form(rec, status="BLOCKED", notes="Vendor replied", assignee="Sam Example"),
                               _projects.normalize(rec))
    assert plan["payload"] == {"Status": "BLOCKED", "Sprint Notes": "Vendor replied",
                               "Assigned to": {"email": "sam@example.com"}}
    assert plan["restore"]["Assigned to"] == {"email": "alex@example.com"}


def test_clearing_a_field_sends_null(airtable):
    rec = _records()[REC1]
    plan = _projects.plan_edit(_form(rec, blockers="", end=""), _projects.normalize(rec))
    assert plan["payload"] == {"Blockers": None, "End date": None}
    assert plan["restore"] == {"Blockers": "Waiting on vendor", "End date": "2026-01-15"}


@pytest.mark.parametrize("changes,fragment", [
    ({"status": "DONE"}, "is not one of"),
    ({"end": "15/01/2026"}, "not a date"),
    ({"start": "2026-02-30"}, "not a date"),
    ({"assignee": "Mallory Example"}, "not in the list"),
    ({"notes": "x" * (_projects.TEXT_LIMIT + 1)}, "longer than"),
])
def test_invalid_values_are_errors(airtable, changes, fragment):
    rec = _records()[REC1]
    plan = _projects.plan_edit(_form(rec, **changes), _projects.normalize(rec))
    assert plan["changes"] == [] and any(fragment in e for e in plan["errors"])


def test_a_field_changed_in_airtable_meanwhile_is_a_conflict(airtable):
    rec = _records()[REC1]
    form = _form(rec, status="BLOCKED")
    rec["fields"]["Status"] = "SCOPING"                    # someone edits it in Airtable
    plan = _projects.plan_edit(form, _projects.normalize(rec))
    assert plan["changes"] == [] and plan["conflicts"][0]["slug"] == "status"
    assert plan["conflicts"][0]["now"] == "SCOPING"


def test_unknown_form_keys_are_ignored(airtable):
    rec = _records()[REC1]
    form = _form(rec, status="BLOCKED")
    form["Project name"] = ["Hijacked"]
    form["orig_Project name"] = ["Rotate"]
    form["fields[Description]"] = ["x"]
    plan = _projects.plan_edit(form, _projects.normalize(rec))
    assert list(plan["payload"]) == ["Status"]


def test_completing_fills_todays_date_only_when_asked_and_empty(airtable):
    rec = _records()[REC1]
    form = _form(rec, status="COMPLETE")
    form["complete_today"] = ["yes"]
    plan = _projects.plan_edit(form, _projects.normalize(rec), today="2026-10-29")
    assert plan["payload"]["Actual Completion Date"] == "2026-10-29"
    form.pop("complete_today")
    assert "Actual Completion Date" not in _projects.plan_edit(form, _projects.normalize(rec), today="2026-10-29")["payload"]
    typed = _form(rec, status="COMPLETE", completed="2026-10-01")
    typed["complete_today"] = ["yes"]
    assert _projects.plan_edit(typed, _projects.normalize(rec), today="2026-10-29")["payload"]["Actual Completion Date"] == "2026-10-01"


# --- committing and undoing ----------------------------------------------------------

def _commit(airtable, rec_id, **changes):
    rec = airtable.records[rec_id]
    plan = _projects.plan_edit(_form(rec, **changes), _projects.normalize(rec))
    return _projects.commit_edit(rec_id, plan)


def test_commit_patches_reads_back_and_logs(airtable):
    _projects.fetch_projects()
    result = _commit(airtable, REC1, status="BLOCKED", assignee="Sam Example")
    assert result["ok"] and "(confirmed)" in result["message"] and result["undo_id"]
    method, path, _params, body = airtable.patches()[0]
    assert path == f"appTESTBASE/tblTESTTABLE/{REC1}"
    assert body == {"fields": {"Status": "BLOCKED", "Assigned to": {"email": "sam@example.com"}}, "typecast": False}
    shown = {i["id"]: i for i in _projects.fetch_projects()["items"]}[REC1]       # cache already updated
    assert shown["status"] == "BLOCKED" and shown["assignee"] == "Sam Example"
    log = [json.loads(line) for line in (_common.OUTPUT_DIR / "projects_edits.jsonl").read_text().splitlines()]
    assert log[0]["id"] == result["undo_id"] and log[0]["record"] == REC1
    assert "pat-test-token" not in json.dumps(log)


def test_commit_flags_a_reply_that_does_not_match(airtable):
    airtable.drop_writes = True
    result = _commit(airtable, REC1, notes="Changed")
    assert result["ok"] and "did not match" in result["message"] and "Sprint notes" in result["message"]


def test_commit_failure_saves_nothing_and_logs_nothing(airtable):
    airtable.fail = "Airtable returned 422 (INVALID_VALUE_FOR_COLUMN)"
    result = _commit(airtable, REC1, notes="Changed")
    assert result["ok"] is False and "Nothing was saved" in result["message"] and result["undo_id"] == ""
    assert not (_common.OUTPUT_DIR / "projects_edits.jsonl").exists()


def test_undo_restores_old_values_once(airtable):
    result = _commit(airtable, REC1, status="BLOCKED", assignee="Sam Example", blockers="")
    msg = _projects.undo_edit(result["undo_id"])
    assert msg.startswith("Undone")
    fields = airtable.records[REC1]["fields"]
    assert fields["Status"] == "ACTIVE" and fields["Assigned to"]["email"] == "alex@example.com"
    assert fields["Blockers"] == "Waiting on vendor"
    assert _projects.undo_edit(result["undo_id"]) == "That edit was already undone."


def test_undo_refuses_when_the_record_moved_on(airtable):
    result = _commit(airtable, REC1, status="BLOCKED")
    airtable.records[REC1]["fields"]["Status"] = "SCOPING"
    msg = _projects.undo_edit(result["undo_id"])
    assert msg.startswith("Not undone") and "Status" in msg
    assert len(airtable.patches()) == 1


def test_undo_of_unknown_id(airtable):
    assert "not in the local log" in _projects.undo_edit("0123456789ab")


# --- name, requester, urgency, description ------------------------------------------

URGENT = "5 - Critical / SLA Breach"


def test_plan_covers_name_requester_urgency_and_description(airtable):
    rec = _records()[REC1]
    plan = _projects.plan_edit(
        _form(rec, name="Rotate API keys", requester="Dana Example", urgency=URGENT,
              description="Rotate all keys.\nThen verify."), _projects.normalize(rec))
    assert plan["errors"] == []
    assert plan["payload"] == {"Project name": "Rotate API keys", "Requester": "Dana Example",
                               "Urgency Rating": URGENT, "Description": "Rotate all keys.\nThen verify."}
    assert plan["restore"]["Project name"] == "Rotate <b>keys</b>"


def test_urgency_unknown_current_value_is_left_untouched(airtable):
    rec = _records()[REC1]                                 # fixture holds 4, not an option string
    plan = _projects.plan_edit(_form(rec, notes="Changed"), _projects.normalize(rec))
    assert "Urgency Rating" not in plan["payload"] and plan["errors"] == []


def test_urgency_can_be_set_and_cleared(airtable):
    rec = _records()[REC1]
    rec["fields"]["Urgency Rating"] = "3 - Hard Deadline"
    item = _projects.normalize(rec)
    assert _projects.plan_edit(_form(rec, urgency="1 - No Deadline"), item)["payload"] == {
        "Urgency Rating": "1 - No Deadline"}
    cleared = _projects.plan_edit(_form(rec, urgency=""), item)
    assert cleared["payload"] == {"Urgency Rating": None}
    assert cleared["restore"] == {"Urgency Rating": "3 - Hard Deadline"}


def test_requester_can_be_cleared(airtable):
    rec = _records()[REC1]
    rec["fields"]["Requester"] = "Dana Example"
    plan = _projects.plan_edit(_form(rec, requester=""), _projects.normalize(rec))
    assert plan["payload"] == {"Requester": None} and plan["restore"] == {"Requester": "Dana Example"}


@pytest.mark.parametrize("changes,fragment", [
    ({"urgency": "9 - Whenever"}, "Urgency Rating options"),
    ({"name": ""}, "cannot be empty"),
    ({"name": "Line one\nLine two"}, "single line"),
    ({"requester": "x" * (_projects.LINE_LIMIT + 1)}, "longer than"),
    ({"description": "x" * (_projects.TEXT_LIMIT + 1)}, "longer than"),
])
def test_invalid_name_requester_urgency_description(airtable, changes, fragment):
    rec = _records()[REC1]
    plan = _projects.plan_edit(_form(rec, **changes), _projects.normalize(rec))
    assert plan["changes"] == [] and any(fragment in e for e in plan["errors"])


def test_description_edited_in_airtable_meanwhile_is_a_conflict(airtable):
    rec = _records()[REC1]
    rec["fields"]["Description"] = "Original"
    form = _form(rec, description="Mine")
    rec["fields"]["Description"] = "Theirs"
    plan = _projects.plan_edit(form, _projects.normalize(rec))
    assert plan["changes"] == [] and plan["conflicts"][0]["slug"] == "description"


def test_commit_sends_exact_airtable_field_names_for_the_new_fields(airtable):
    _projects.fetch_projects()
    result = _commit(airtable, REC1, name="Rotate API keys", requester="Dana Example",
                     urgency=URGENT, description="New text")
    assert result["ok"] and "(confirmed)" in result["message"]
    _method, _path, _params, body = airtable.patches()[0]
    assert body == {"fields": {"Project name": "Rotate API keys", "Requester": "Dana Example",
                               "Urgency Rating": URGENT, "Description": "New text"}, "typecast": False}


def test_undo_restores_name_requester_urgency_and_description(airtable):
    airtable.records[REC1]["fields"].update({"Requester": "Dana Example", "Description": "Old text",
                                             "Urgency Rating": "2 - Soft Deadline"})
    result = _commit(airtable, REC1, name="Renamed", requester="", urgency="", description="")
    fields = airtable.records[REC1]["fields"]
    assert fields["Project name"] == "Renamed" and "Requester" not in fields and "Urgency Rating" not in fields
    assert _projects.undo_edit(result["undo_id"]).startswith("Undone")
    fields = airtable.records[REC1]["fields"]
    assert fields["Project name"] == "Rotate <b>keys</b>" and fields["Requester"] == "Dana Example"
    assert fields["Description"] == "Old text" and fields["Urgency Rating"] == "2 - Soft Deadline"


def test_edit_form_and_panel_show_the_new_fields(airtable):
    rec = _records()[REC1]
    rec["fields"].update({"Requester": "Dana <i>Example</i>", "Description": "Line <b>one</b>",
                          "Urgency Rating": "3 - Hard Deadline"})
    body = _page_for(records={REC1: rec, REC2: _records()[REC2], REC3: _records()[REC3]})
    assert 'name="name"' in body and 'name="requester"' in body
    assert '<select name="urgency"' in body and '<option value="3 - Hard Deadline" selected' in body
    assert '<textarea name="description"' in body and "project-desc-edit" in body
    assert "Requested by Dana &lt;i&gt;Example&lt;/i&gt;" in body and "<i>Example</i>" not in body
    assert "Line <b>one</b>" not in body


def test_record_guard_is_exclusive():
    assert _projects.begin_action(REC1) is True
    assert _projects.begin_action(REC1) is False
    _projects.end_action(REC1)
    assert _projects.begin_action(REC1) is True


# --- the HTTP routes -----------------------------------------------------------------

@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _http(port, method, path, form=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = urlencode(form, doseq=True) if form is not None else None
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"} if form is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode()
    out = (resp.status, resp.getheader("Location") or "", data)
    conn.close()
    return out


def test_get_projects_page(airtable, server):
    status, _loc, body = _http(server, "GET", "/projects")
    assert status == 200 and "Laptop refresh" in body and "Past end date" in body


def test_edit_first_shows_a_confirm_page_and_sends_nothing(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    status, _loc, body = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 200 and "Save to Airtable?" in body and "BLOCKED" in body
    assert 'name="confirm" value="yes"' in body
    assert airtable.patches() == []


def test_confirmed_edit_saves_and_redirects_with_undo(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form["confirm"] = ["yes"]
    status, loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 303 and loc.startswith("/projects?msg=") and "&undo=/projects/undo/" in loc
    assert loc.endswith(f"#project-{REC1}")
    assert airtable.records[REC1]["fields"]["Status"] == "BLOCKED"
    undo_path = loc.split("&undo=")[1].split("#")[0]
    status, loc, _ = _http(server, "POST", undo_path, form={})
    assert status == 303 and "Undone" in loc.replace("%20", " ")
    assert airtable.records[REC1]["fields"]["Status"] == "ACTIVE"


def test_forged_fields_never_reach_airtable(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form.update({"confirm": ["yes"], "Project name": ["Hijacked"], "orig_Project name": ["x"],
                 "fields": ['{"Description":"x"}'], "typecast": ["true"]})
    _http(server, "POST", f"/projects/{REC1}/edit", form)
    body = airtable.patches()[0][3]
    assert list(body["fields"]) == ["Status"] and body["typecast"] is False
    assert airtable.records[REC1]["fields"]["Project name"] == "Rotate <b>keys</b>"


def test_no_change_redirects_without_a_write(airtable, server):
    status, loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", _form(airtable.records[REC1]))
    assert status == 303 and "No%20changes" in loc and airtable.patches() == []


def test_conflict_is_409_and_nothing_is_written(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form["confirm"] = ["yes"]
    airtable.records[REC1]["fields"]["Status"] = "SCOPING"
    status, _loc, body = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 409 and "changed in Airtable after this page loaded" in body
    assert airtable.patches() == []


def test_invalid_value_is_400(airtable, server):
    status, _loc, body = _http(server, "POST", f"/projects/{REC1}/edit", _form(airtable.records[REC1], status="DONE"))
    assert status == 400 and "Nothing was saved" in body and airtable.patches() == []


def test_bad_record_id_and_unknown_routes_are_404(airtable, server):
    assert _http(server, "POST", "/projects/not-a-record/edit", {})[0] == 404
    assert _http(server, "POST", "/projects/undo/ZZZ", {})[0] == 404
    assert _http(server, "POST", "/projects", {})[0] == 404


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Referer": "https://evil.example/page"},
    {"Origin": "null"},
    {"Host": "evil.example"},
])
def test_cross_site_posts_are_refused(airtable, server, headers):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form["confirm"] = ["yes"]
    status, _loc, _body = _http(server, "POST", f"/projects/{REC1}/edit", form, headers=headers)
    assert status == 403 and airtable.patches() == []


def test_same_origin_post_is_accepted(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form["confirm"] = ["yes"]
    host = f"127.0.0.1:{server}"
    status, _loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", form,
                            headers={"Origin": f"http://{host}", "Host": host})
    assert status == 303 and len(airtable.patches()) == 1


def test_localhost_and_ipv6_hosts_pass_the_guard():
    ok = review_server._projects_request_allowed
    assert ok({"Host": "localhost:8765"}) and ok({"Host": "[::1]:8765"}) and ok({"Host": "127.0.0.1"})
    assert not ok({"Host": "192.168.1.20:8765"}) and not ok({"Host": ""})


def test_refresh_on_projects_reloads_from_airtable_only(airtable, server, monkeypatch):
    _projects.fetch_projects()
    before = len([c for c in airtable.calls if c[0] == "GET"])
    monkeypatch.setattr(review_server.Handler, "_refresh", lambda self: pytest.fail("must not scan Zendesk"))
    status, loc, _ = _http(server, "POST", "/refresh", {"return_to": ["/projects"]})
    assert status == 303 and loc.startswith("/projects?msg=")
    _http(server, "GET", "/projects")
    assert len([c for c in airtable.calls if c[0] == "GET"]) == before + 1


def test_unconfigured_post_redirects_with_a_message(server, monkeypatch):
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    status, loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", {"status": ["ACTIVE"]})
    assert status == 303 and "not%20set%20up" in loc and fake.calls == []


def test_about_panel_and_nav_mention_projects():
    page = review_server._page("<p>x</p>").decode()
    assert "What it writes to Airtable" in page and "Projects</span>" in page


# --- layout: triage column, table view, preview, side panel -----------------------------

def _page_for(view="board", sort="", open_id="", records=None):
    if records is not None:
        _projects._request.records = records
    result = _projects.fetch_projects(force=True)
    return review_server._render_projects_page(result, today="2026-06-01", view=view, sort=sort, open_id=open_id).decode()


def _board_only(page):
    return re.sub(r"<template.*?</template>", "", page.split("</style>")[-1], flags=re.S)


def _column_labels(page):
    return re.findall(r'<h2 class="column-heading"[^>]*>(?:<img[^>]*>)?([^<]+)</h2>', _board_only(page))


def test_projects_without_a_status_come_first_as_needs_triage(airtable):
    airtable.records[REC2]["fields"].pop("Status")
    labels = _column_labels(_page_for())
    assert labels[0] == "Needs triage"
    assert labels[1:] == list(_projects.STATUS_ORDER)


def test_no_triage_column_when_every_project_has_a_status(airtable):
    assert "Needs triage" not in _column_labels(_page_for())


def test_spelling_variants_of_a_status_share_one_column(airtable):
    airtable.records[REC2]["fields"]["Status"] = "Active "
    airtable.records[REC3]["fields"]["Status"] = "active"
    page = _page_for()
    labels = _column_labels(page)
    assert labels.count("ACTIVE") == 1 and len(labels) == len(set(labels))
    segment = {re.sub(r"^<img[^>]*>", "", seg).split("<", 1)[0]: seg for seg in _board_only(page).split('<h2 class="column-heading">')[1:]}
    active = segment["ACTIVE</h2>".split("<")[0]]
    assert '<span class="stat-chip">3</span>' in active
    assert "Laptop refresh" in active and "Old audit" in active and "Rotate" in active


def test_status_text_is_left_as_airtable_has_it_for_edits_and_undo(airtable):
    airtable.records[REC2]["fields"]["Status"] = "Active "
    item = {i["id"]: i for i in _projects.fetch_projects(force=True)["items"]}[REC2]
    assert item["status"] == "Active " or item["status"] == "Active"      # normalize strips outer spaces
    assert item["status_key"] == "ACTIVE"


def test_card_shows_a_description_preview_and_no_inline_form(airtable):
    airtable.records[REC1]["fields"]["Description"] = "Line one.\n\n" + "word " * 200
    page = _page_for()
    visible = _board_only(page)
    assert 'class="project-preview"' in visible and "Line one. word word" in visible
    assert "word " * 100 not in visible                                      # clipped, not the full text
    assert "…" in visible
    assert "<form" not in visible.split('<div id="project-backdrop"')[0].split('class="overview-board')[1]
    assert "Details &amp; edit" in visible


def test_card_without_a_description_has_no_empty_preview(airtable):
    assert 'class="project-preview"' not in _board_only(_page_for())


def test_preview_helper_breaks_on_a_word():
    out = review_server._project_preview("alpha " * 100)
    assert out.endswith("…") and len(out) <= review_server._PROJECT_PREVIEW_CHARS + 1 and "alph…" not in out


def test_each_project_has_a_panel_template_with_the_full_text_and_form(airtable):
    airtable.records[REC1]["fields"]["Description"] = "FULL-TEXT " * 100
    page = _page_for()
    tpl = re.search(rf'<template id="project-tpl-{REC1}">(.*?)</template>', page, re.S).group(1)
    assert "FULL-TEXT " * 100 in tpl.replace("\n", " ").replace("  ", " ") or "FULL-TEXT FULL-TEXT" in tpl
    assert f'action="/projects/{REC1}/edit"' in tpl and 'name="orig_status"' in tpl
    assert 'data-project-close' in tpl and "Review changes" in tpl
    assert page.count('<template id="project-tpl-') == 3


def test_panel_is_closed_by_default_and_open_with_the_open_parameter(airtable):
    closed = _page_for()
    assert 'id="project-panel" class="project-panel" role="dialog" aria-label="Project details" hidden' in closed
    opened = _page_for(open_id=REC1)
    aside = re.search(r'<aside id="project-panel"[^>]*>(.*?)</aside>', opened, re.S)
    assert " hidden" not in re.search(r'<aside id="project-panel"[^>]*>', opened).group(0)
    assert "Rotate &lt;b&gt;keys&lt;/b&gt;" in aside.group(1) and f'action="/projects/{REC1}/edit"' in aside.group(1)
    assert 'id="project-backdrop" class="project-backdrop">' in opened


def test_card_links_open_the_panel_with_and_without_javascript(airtable):
    visible = _board_only(_page_for())
    assert f'href="/projects?open={REC1}" data-project-open="{REC1}"' in visible
    assert f'href="/projects?view=table&amp;open={REC1}"' in _board_only(_page_for(view="table"))


def test_table_view_lists_every_project_and_omits_columns_empty_for_all(airtable):
    page = _page_for(view="table")
    body = _board_only(page)
    assert body.count('class="project-row"') == 3 and "overview-board" not in body
    heads = re.findall(r'<th scope="col">(?:<a[^>]*>)?([A-Za-z]+)', body)
    assert heads == ["Project", "Status", "Workstream", "Assignee", "Ticket", "Blockers", "End", "Completed"]
    assert "Start" not in heads                                              # no project has a start date
    for rec in airtable.records.values():
        rec["fields"].pop("End date", None); rec["fields"].pop("Actual Completion Date", None)
    heads = re.findall(r'<th scope="col">(?:<a[^>]*>)?([A-Za-z]+)', _board_only(_page_for(view="table")))
    assert heads == ["Project", "Status", "Workstream", "Assignee", "Ticket", "Blockers"]


def test_table_default_order_is_triage_then_workflow_order(airtable):
    airtable.records[REC2]["fields"].pop("Status")
    names = re.findall(r'class="ticket-title-link" href="[^"]*" data-project-open="[^"]*">([^<]+)</a></td>', _board_only(_page_for(view="table")))
    assert names == ["Laptop refresh", "Rotate &lt;b&gt;keys&lt;/b&gt;", "Old audit"]      # triage, ACTIVE, COMPLETE


def test_table_sorting_and_header_links(airtable):
    def names(sort):
        return re.findall(r'data-project-open="[^"]*">([^<]+)</a></td>', _board_only(_page_for(view="table", sort=sort)))
    assert names("name") == ["Laptop refresh", "Old audit", "Rotate &lt;b&gt;keys&lt;/b&gt;"]
    assert names("-name") == ["Rotate &lt;b&gt;keys&lt;/b&gt;", "Old audit", "Laptop refresh"]
    assert names("bogus") == names("status")
    page = _board_only(_page_for(view="table", sort="name"))
    assert 'href="/projects?view=table&amp;sort=-name"' in page and "&#9650;" in page          # next click reverses
    assert _board_only(_page_for(view="table", sort="-name")).count("&#9660;") == 1


def test_table_cells_escape_and_label_tickets(airtable):
    body = _board_only(_page_for(view="table"))
    assert "Rotate &lt;b&gt;keys&lt;/b&gt;" in body and "<b>keys</b>" not in body
    assert "Zendesk #12345" in body and "Waiting on vendor" in body


def test_view_toggle_marks_the_current_view(airtable):
    board = _board_only(_page_for())
    assert 'class="sla-range-btn active" href="/projects" aria-current="page">Board' in board
    table = _board_only(_page_for(view="table"))
    assert 'class="sla-range-btn active" href="/projects?view=table" aria-current="page">Table' in table
    assert 'data-filter-target=".project-row"' in table and 'data-filter-target=".project-card"' in board


def test_edit_forms_remember_the_view(airtable):
    assert 'name="view" value="table"' in _page_for(view="table")
    assert 'name="view" value="table"' not in _page_for()


def test_get_route_reads_view_sort_and_open(airtable, server):
    _status, _loc, body = _http(server, "GET", f"/projects?view=table&sort=-name&open={REC1}")
    assert body.count('class="project-row"') == 3 and "&#9660;" in body
    assert " hidden" not in re.search(r'<aside id="project-panel"[^>]*>', body).group(0)
    _status, _loc, body = _http(server, "GET", "/projects?view=evil&sort=<x>&open=nope")
    assert "overview-board" in body and 'class="project-row"' not in body and "<x>" not in body
    assert " hidden" in re.search(r'<aside id="project-panel"[^>]*>', body).group(0)


def test_saving_from_the_table_returns_to_the_table(airtable, server):
    form = _form(airtable.records[REC1], status="BLOCKED")
    form["view"] = ["table"]
    status, _loc, body = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 200 and 'name="view" value="table"' in body and 'href="/projects?view=table#project-' in body
    form["confirm"] = ["yes"]
    status, loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 303 and loc.startswith("/projects?view=table&msg=") and "&undo=/projects/undo/" in loc
    assert loc.endswith(f"#project-{REC1}")


def test_an_unknown_view_in_the_form_falls_back_to_the_board(airtable, server):
    form = _form(airtable.records[REC1])
    form["view"] = ["javascript:alert(1)"]
    status, loc, _ = _http(server, "POST", f"/projects/{REC1}/edit", form)
    assert status == 303 and loc.startswith("/projects?msg=")


def _columns(page):
    """data-status -> the opening <details ...> tag of each board column."""
    return {m.group(1): m.group(0) for m in re.finditer(r'<details class="[^"]*project-column"[^>]*data-status="([^"]*)"[^>]*>', _board_only(page))}


def test_complete_cancelled_and_empty_columns_start_collapsed(airtable):
    cols = _columns(_page_for())
    assert set(cols) >= {"INTAKE", "ACTIVE", "COMPLETE", "CANCELLED"}
    for key in ("INTAKE", "ACTIVE", "SCOPING", "BLOCKED"):
        # SCOPING and BLOCKED have no project in this data, so they are empty: collapsed.
        assert (" open" in cols[key]) == (key in ("INTAKE", "ACTIVE")), key
    assert " open" not in cols["COMPLETE"] and " open" not in cols["CANCELLED"]   # closed status
    # the count and heading stay visible in the summary of a collapsed column
    page = _board_only(_page_for())
    complete = page.split('data-status="COMPLETE"')[1].split("</details>")[0]
    assert "<summary" in complete and '<span class="stat-chip">1</span>' in complete


def test_a_column_holding_the_opened_project_is_drawn_open_and_pinned(airtable):
    cols = _columns(_page_for(open_id=REC3))                       # REC3 is COMPLETE
    assert " open" in cols["COMPLETE"] and "data-pinned" in cols["COMPLETE"]
    assert " open" not in _columns(_page_for())["COMPLETE"]
    assert "data-pinned" not in "".join(_columns(_page_for()).values())


def test_cards_get_an_edge_for_overdue_or_blocked_and_dates_on_their_own_line(airtable):
    records = _records()
    records[REC2]["fields"]["Status"] = "BLOCKED"                  # future end date, blocked
    page = _board_only(_page_for(records=records))
    by_id = {rec: classes for classes, rec in re.findall(r'<div class="mini-ticket project-card([^"]*)" id="project-(rec\w+)"', page)}
    assert "project-card-overdue" in by_id[REC1]                   # ACTIVE, ended in January 2026
    assert "project-card-blocked" in by_id[REC2] and "overdue" not in by_id[REC2]
    assert by_id[REC3].strip() == ""                               # COMPLETE: no edge
    card1 = page.split(f'id="project-{REC1}"')[1].split("Details &amp; edit")[0]
    assert '<div class="project-dates meta">End Jan 15, 2026</div>' in card1
    assert "project-line" in card1 and "End Jan" not in card1.split("project-dates")[0]


def test_columns_script_is_on_the_board_only_and_valid_javascript(airtable):
    assert "details.project-column" in _page_for()
    assert "details.project-column" not in _page_for(view="table")
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = review_server._PROJECT_COLUMNS_SCRIPT.removeprefix("<script>").removesuffix("</script>")
    result = subprocess.run([node, "--check", "-"], input=src, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_panel_script_is_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = review_server._PROJECT_PANEL_SCRIPT.removeprefix("<script>").removesuffix("</script>")
    result = subprocess.run([node, "--check", "-"], input=src, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


# --- creating a project ----------------------------------------------------------------

def _new_form(**values):
    base = {"name": "Vendor onboarding", "status": "INTAKE", "assignee": "", "requester": "",
            "urgency": "", "end": "", "description": ""}
    base.update(values)
    return {k: [v] for k, v in base.items()}


def test_plan_create_sends_only_filled_allowlisted_fields(airtable):
    form = _new_form(requester="Dana Example", urgency="2 - Soft Deadline", assignee="Sam Example",
                     end="2026-12-01", description="Line one\nLine two")
    form["blockers"] = ["forged"]                      # not a create field: ignored
    form["Project name"] = ["forged"]                  # an Airtable name, never a slug: ignored
    plan = _projects.plan_create(form)
    assert plan["errors"] == []
    assert plan["fields"] == {"Project name": "Vendor onboarding", "Status": "INTAKE", "Requester": "Dana Example",
                              "Urgency Rating": "2 - Soft Deadline", "Assigned to": {"email": "sam@example.com"},
                              "End date": "2026-12-01", "Description": "Line one\nLine two"}


def test_plan_create_leaves_empty_fields_out(airtable):
    plan = _projects.plan_create(_new_form(status=""))
    assert plan["fields"] == {"Project name": "Vendor onboarding"}


@pytest.mark.parametrize("changes,fragment", [
    ({"name": ""}, "cannot be empty"),
    ({"name": "a\nb"}, "single line"),
    ({"name": "x" * (_projects.LINE_LIMIT + 1)}, "longer than"),
    ({"status": "DONE"}, "is not one of"),
    ({"urgency": "9 - Whenever"}, "Urgency Rating options"),
    ({"assignee": "Mallory Example"}, "not in the list"),
    ({"end": "01/12/2026"}, "not a date"),
    ({"description": "x" * (_projects.TEXT_LIMIT + 1)}, "longer than"),
])
def test_plan_create_rejects_bad_values(airtable, changes, fragment):
    plan = _projects.plan_create(_new_form(**changes))
    assert any(fragment in e for e in plan["errors"])


def test_plan_create_refuses_a_duplicate_name_ignoring_case(airtable):
    plan = _projects.plan_create(_new_form(name="  laptop REFRESH "), existing_names=["Laptop refresh"])
    assert any("already exists" in e for e in plan["errors"])


def test_commit_create_posts_reads_back_and_logs(airtable):
    _projects.fetch_projects()
    plan = _projects.plan_create(_new_form(requester="Dana Example", urgency="1 - No Deadline"))
    result = _projects.commit_create(plan)
    assert result["ok"] and "(confirmed)" in result["message"] and _projects.valid_record_id(result["record_id"])
    method, path, _params, body = airtable.posts()[0]
    assert path == "appTESTBASE/tblTESTTABLE"
    assert body == {"fields": {"Project name": "Vendor onboarding", "Status": "INTAKE", "Requester": "Dana Example",
                               "Urgency Rating": "1 - No Deadline"}, "typecast": False}
    assert _projects.fetch_projects()["items"][0]["id"] == result["record_id"]            # shown without a reload
    log = [json.loads(line) for line in (_common.OUTPUT_DIR / "projects_edits.jsonl").read_text().splitlines()]
    assert log[-1]["kind"] == "create" and log[-1]["record"] == result["record_id"]
    assert "pat-test-token" not in json.dumps(log)
    assert "not in the local log" in _projects.undo_edit(log[-1]["id"])                   # a create cannot be undone here
    assert not [c for c in airtable.calls if c[0] == "DELETE"]


def test_commit_create_failure_logs_nothing(airtable):
    airtable.fail = "Airtable returned 422 (INVALID_VALUE_FOR_COLUMN)"
    result = _projects.commit_create(_projects.plan_create(_new_form()))
    assert result["ok"] is False and "Nothing was created" in result["message"] and result["record_id"] == ""
    assert not (_common.OUTPUT_DIR / "projects_edits.jsonl").exists()


def test_commit_create_without_a_record_in_the_reply_is_not_ok(airtable):
    airtable.drop_creates = True
    result = _projects.commit_create(_projects.plan_create(_new_form()))
    assert result["ok"] is False and "held no new record" in result["message"]


def test_commit_create_refuses_a_plan_with_errors(airtable):
    result = _projects.commit_create(_projects.plan_create(_new_form(name="")))
    assert result["ok"] is False and airtable.posts() == []


def test_new_project_page_and_link(airtable, server):
    status, _loc, body = _http(server, "GET", "/projects/new")
    assert status == 200 and 'action="/projects/new"' in body and 'name="name"' in body and 'name="description"' in body
    assert '<option value="INTAKE" selected>' in body
    _s, _l, board = _http(server, "GET", "/projects")
    assert 'href="/projects/new"' in board and "+ New project" in board


def test_new_project_review_step_sends_nothing(airtable, server):
    status, _loc, body = _http(server, "POST", "/projects/new", _new_form(name="Review <me>"))
    assert status == 200 and "Create in Airtable?" in body and "Review &lt;me&gt;" in body and "Review <me>" not in body
    assert 'name="confirm" value="yes"' in body
    assert airtable.posts() == []


def test_new_project_confirm_creates_once_and_opens_it(airtable, server):
    form = _new_form()
    form["confirm"] = ["yes"]
    status, loc, _ = _http(server, "POST", "/projects/new", form)
    assert status == 303 and loc.startswith("/projects?open=rec") and "msg=Created" in loc
    assert len(airtable.posts()) == 1
    status, _loc, body = _http(server, "POST", "/projects/new", form)                    # double submit
    assert status == 400 and "already exists" in body and len(airtable.posts()) == 1


def test_new_project_errors_redisplay_the_form_with_the_values(airtable, server):
    status, _loc, body = _http(server, "POST", "/projects/new", _new_form(requester="Dana", urgency="bogus"))
    assert status == 400 and "Urgency Rating options" in body and 'value="Dana"' in body
    assert airtable.posts() == []


def test_new_project_refuses_a_foreign_origin(airtable, server):
    form = _new_form()
    form["confirm"] = ["yes"]
    status, _loc, _ = _http(server, "POST", "/projects/new", form, headers={"Origin": "https://evil.example"})
    assert status == 403 and airtable.posts() == []


def test_new_project_when_unconfigured_redirects(server, monkeypatch):
    fake = FakeAirtable()
    monkeypatch.setattr(_projects, "_request", fake)
    assert _http(server, "GET", "/projects/new")[0] == 303
    assert _http(server, "POST", "/projects/new", _new_form())[0] == 303 and fake.calls == []


# --- snapshots before saves ----------------------------------------------------------------

def _snapshot_files():
    return sorted((_common.OUTPUT_DIR / "projects_snapshots").glob("projects-*.json")) \
        if (_common.OUTPUT_DIR / "projects_snapshots").exists() else []


def test_first_save_takes_a_full_snapshot_before_writing(airtable):
    airtable.records[REC1]["fields"]["Unread Extra Field"] = "kept in the backup"
    _commit(airtable, REC1, notes="Changed")
    kinds = [c[0] for c in airtable.calls]
    assert kinds.index("GET") < kinds.index("PATCH")                        # snapshot read comes first
    files = _snapshot_files()
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["records"]) == 3 and data["table"] == "tblTESTTABLE"
    assert data["records"][0]["fields"]["Unread Extra Field"] == "kept in the backup"   # every field, not just the page's
    assert "pat-test-token" not in files[0].read_text()
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"


def test_second_save_inside_the_interval_reuses_the_snapshot(airtable):
    _commit(airtable, REC1, notes="One")
    _commit(airtable, REC1, notes="Two")
    assert len(_snapshot_files()) == 1


def test_an_old_snapshot_is_replaced_by_a_new_one(airtable):
    import os
    import time
    _commit(airtable, REC1, notes="One")
    old = _snapshot_files()[0]
    stale = time.time() - _projects.SNAPSHOT_MIN_INTERVAL_SECONDS - 60
    os.utime(old, (stale, stale))
    old.rename(old.with_name("projects-20200101-000000.json"))
    _commit(airtable, REC1, notes="Two")
    assert len(_snapshot_files()) == 2


def test_only_the_newest_snapshots_are_kept(airtable):
    import os
    folder = _common.OUTPUT_DIR / "projects_snapshots"
    folder.mkdir(parents=True)
    for i in range(_projects.SNAPSHOT_KEEP + 3):
        f = folder / f"projects-2020{i:04d}-000000.json"
        f.write_text("{}")
        os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
    result = _projects.take_snapshot(force=True)
    assert result["ok"] and result["taken"]
    assert len(_snapshot_files()) == _projects.SNAPSHOT_KEEP
    assert result["path"] in [f.name for f in _snapshot_files()]


def test_a_failed_snapshot_never_blocks_the_save(airtable, monkeypatch):
    monkeypatch.setattr(_projects, "take_snapshot", lambda force=False: {"ok": False, "error": "disk full"})
    result = _commit(airtable, REC1, notes="Changed")
    assert result["ok"] and "Snapshot skipped: disk full" in result["message"] and len(airtable.patches()) == 1


def test_snapshot_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
    result = _projects.take_snapshot()
    assert result["ok"] is False and "not set up" in result["error"]


# --- the real-write smoke test --------------------------------------------------------------

def test_smoke_test_creates_edits_and_deletes_only_its_own_record(airtable):
    before = set(airtable.records)
    result = _projects.smoke_test()
    assert result["ok"], result
    assert [c[0] for c in airtable.calls if c[0] != "GET"] == ["POST", "PATCH", "DELETE"]
    post = airtable.posts()[0]
    assert post[3]["typecast"] is False and list(post[3]["fields"]) == ["Project name"]
    assert post[3]["fields"]["Project name"].startswith(_projects.SMOKE_PREFIX)
    assert airtable.patches()[0][3]["fields"] == {"Sprint Notes": "smoke test edit"}
    assert set(airtable.records) == before                                  # nothing left, nothing else touched
    deleted = [c[1].split("/")[-1] for c in airtable.calls if c[0] == "DELETE"]
    assert len(deleted) == 1 and deleted[0] not in before
    log = [json.loads(line) for line in (_common.OUTPUT_DIR / "projects_edits.jsonl").read_text().splitlines()]
    assert log[-1]["kind"] == "smoke" and log[-1]["ok"] is True
    assert "pat-test-token" not in json.dumps(log)


def test_smoke_test_create_failure_leaves_nothing(airtable):
    airtable.fail = "Airtable returned 403 (INVALID_PERMISSIONS)"
    result = _projects.smoke_test()
    assert result["ok"] is False and "Nothing was left" in result["message"]
    assert result["steps"][0][1] is False and "403" in result["steps"][0][2]


def test_smoke_test_reports_an_edit_the_reply_did_not_hold(airtable):
    airtable.drop_writes = True
    result = _projects.smoke_test()
    assert result["ok"] is False and result["steps"][1][1] is False
    assert result["steps"][2][1] is True and "Nothing was left" in result["message"]    # still cleaned up


def test_smoke_test_does_not_delete_a_record_that_lost_its_name(airtable):
    class Renaming(FakeAirtable):
        def __call__(self, method, path, params=None, body=None):
            out = super().__call__(method, path, params, body)
            if method == "PATCH":
                self.records[path.split("/")[-1]]["fields"]["Project name"] = "Somebody's real project"
            return out
    fake = Renaming(airtable.records)
    _projects._request = fake
    result = _projects.smoke_test()
    assert result["ok"] is False and "left alone" in result["steps"][2][2]
    assert not [c for c in fake.calls if c[0] == "DELETE"]
    assert result["record_id"] in result["message"]


def test_smoke_test_delete_failure_names_the_record(airtable):
    class NoDelete(FakeAirtable):
        def __call__(self, method, path, params=None, body=None):
            if method == "DELETE":
                raise _projects.AirtableError("Airtable returned 403 (INVALID_PERMISSIONS)")
            return super().__call__(method, path, params, body)
    fake = NoDelete(airtable.records)
    _projects._request = fake
    result = _projects.smoke_test()
    assert result["ok"] is False and result["record_id"] and result["record_id"] in result["message"]
    assert "delete it in Airtable" in result["message"]


def test_smoke_test_refuses_to_run_twice_at_once(airtable):
    assert _projects.begin_action("smoke-test")
    try:
        result = _projects.smoke_test()
    finally:
        _projects.end_action("smoke-test")
    assert result["ok"] is False and "already running" in result["message"] and airtable.calls == []


def test_check_connection(airtable):
    assert _projects.check_connection()["ok"] is True
    airtable.fail = "Airtable returned 401 (AUTHENTICATION_REQUIRED)"
    out = _projects.check_connection()
    assert out["ok"] is False and "401" in out["message"] and "pat-test-token" not in out["message"]
