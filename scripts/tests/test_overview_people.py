"""/overview groups open IT-Operations tickets by assignee: the pure grouping in
_common.group_tickets_by_person and the page built from it. Fixtures are made up."""

import _common
import review_server

PEOPLE = ["Me Person", "Jess Jones", "Neko Nakamura", "Ryan Rivers"]


def _t(tid, assignee=None, assignee_id=None, created="2026-10-20T10:00:00Z", status="open", sla="on_track",
       ours=False, subject=None, **kw):
    item = {
        "ticket_id": tid, "subject": subject or f"Ticket {tid}", "status": status, "url": f"https://x.example/{tid}",
        "assignee_name": assignee, "assignee_id": assignee_id if assignee_id is not None else (1 if assignee else None),
        "already_ours": ours, "created_at": created, "sla_status": sla, "breached_rules": [], "due_soon_rules": [],
        "requester_name": "Req Uester", "requester_id": 9,
    }
    item.update(kw)
    return item


def test_groups_by_name_case_insensitively_in_list_order():
    groups = _common.group_tickets_by_person(
        [_t(1, "jess jones"), _t(2, "Ryan Rivers"), _t(3, "NEKO NAKAMURA"), _t(4, "Jess Jones")], PEOPLE)
    assert [c["name"] for c in groups["people"]] == PEOPLE
    assert [i["ticket_id"] for i in groups["people"][1]["items"]] == [1, 4]
    assert [i["ticket_id"] for i in groups["people"][2]["items"]] == [3]
    assert groups["people"][0]["items"] == []
    assert groups["unassigned"] == [] and groups["others"] == 0


def test_own_account_lands_on_first_card_even_without_a_resolved_name():
    groups = _common.group_tickets_by_person([_t(1, assignee=None, assignee_id=7, ours=True)], PEOPLE)
    assert [i["ticket_id"] for i in groups["people"][0]["items"]] == [1]
    assert groups["people"][0]["is_me"] is True and groups["people"][1]["is_me"] is False
    assert groups["unassigned"] == [] and groups["others"] == 0


def test_unassigned_and_other_assignees_are_kept_apart():
    groups = _common.group_tickets_by_person([_t(1), _t(2, "Someone Else"), _t(3, assignee=None, assignee_id=55)], PEOPLE)
    assert [i["ticket_id"] for i in groups["unassigned"]] == [1]
    # assigned (by id) to somebody not in the list, with or without a resolved name
    assert groups["others"] == 2
    assert all(not c["items"] for c in groups["people"])


def test_oldest_first_and_missing_created_at_last():
    groups = _common.group_tickets_by_person([
        _t(1, "Jess Jones", created="2026-10-25T10:00:00Z"),
        _t(2, "Jess Jones", created=None),
        _t(3, "Jess Jones", created="2026-10-01T10:00:00Z"),
    ], PEOPLE)
    assert [i["ticket_id"] for i in groups["people"][1]["items"]] == [3, 1, 2]


def test_breach_counts_per_person():
    groups = _common.group_tickets_by_person([
        _t(1, "Jess Jones", sla="breached"), _t(2, "Jess Jones", sla="due_soon"),
        _t(3, "Jess Jones", sla="breached"), _t(4, "Ryan Rivers"),
    ], PEOPLE)
    jess = groups["people"][1]
    assert (jess["breached"], jess["due_soon"]) == (2, 1)
    assert (groups["people"][3]["breached"], groups["people"][3]["due_soon"]) == (0, 0)


def test_default_people_are_the_four_requested():
    assert [n.split()[0] for n in _common.OVERVIEW_PEOPLE] == ["Catherine", "Jesstin", "Nekoashi", "Ryan"]


def _render(items, **kw):
    return review_server._render_overview_page({"items": items, "error": None, "capped": False, **kw}).decode()


def test_page_has_a_card_per_person_plus_unassigned_in_order(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    page = _render([_t(1, "Jess Jones"), _t(2), _t(3, "Outsider Name")])
    positions = [page.index(f"{n} (") for n in PEOPLE + ["Unassigned"]]
    assert positions == sorted(positions)
    assert "Jess Jones (1)" in page and "Unassigned (1)" in page and "Me Person (0)" in page
    assert "No open tickets." in page
    assert "1 more open ticket is assigned to someone else and not shown." in page


def test_page_shows_sla_state_and_escapes_subjects(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    page = _render([
        _t(1, "Jess Jones", sla="breached", breached_rules=[("no_update", "9h")], subject="<script>x</script>"),
        _t(2, "Jess Jones", sla="due_soon", due_soon_rules=[("resolution_age", "3h")]),
    ])
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "SLA not met: no update" in page
    assert "Due soon: resolution age" in page
    assert "1 not met" in page and "1 due soon" in page
    assert "Opened " in page


def test_page_keeps_claim_on_tickets_that_arent_yours(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    page = _render([_t(1, "Jess Jones"), _t(2, "Me Person", ours=True)])
    assert 'action="/claim/1"' in page and 'action="/claim/2"' not in page
    assert 'name="return_to" value="/overview"' in page


def test_page_error_and_capped_states(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    err = review_server._render_overview_page({"items": [], "error": "Zendesk isn't connected", "capped": False}).decode()
    assert "Zendesk isn&#x27;t connected" in err or "Zendesk isn't connected" in err
    assert "person-card" not in err.split("</style>")[-1]
    capped = _render([_t(1, "Jess Jones")], capped=True)
    assert "Some tickets may be missing" in capped
    # an error alongside a last-good result still shows the cards
    both = review_server._render_overview_page({"items": [_t(1, "Jess Jones")], "error": "stale", "capped": False}).decode()
    assert "Jess Jones (1)" in both
