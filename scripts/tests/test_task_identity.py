"""Assignee/Requester on the Tasks page: only the people in OVERVIEW_PEOPLE
(you, Jesstin, Nekoashi and Ryan by default) are offered, accepted and
resolved. Names and ids are made up."""
import re

import _common
import review_server

PEOPLE = ["Alex Doe", "Sam Roe", "Kai Poe"]
USERS = {"Sam Roe": 22, "Kai Poe": 33}


class FakeClient:
    def __init__(self, users=None):
        self.client = object()
        self.users = USERS if users is None else users
        self.searches = []
        self.created = []

    def search_users(self, query):
        self.searches.append(query)
        out = [{"id": uid, "name": name, "role": "agent"} for name, uid in self.users.items() if query.casefold() in name.casefold()]
        return out + [{"id": 999, "name": query + " Jr", "role": "end-user"}]    # a near-match that must be ignored

    def create_ticket(self, **kw):
        self.created.append(kw)
        return {"id": 5000}

    def update_ticket(self, *a, **kw):
        return {}


def _options(html_text, name):
    block = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', html_text, re.S).group(1)
    return re.findall(r'value="([^"]*)"[^>]*>([^<]*)<', block)


def test_default_people_are_you_jesstin_nekoashi_and_ryan_only():
    people = _common.personal_task_identity_people()
    assert [n.split()[0] for n in people.values()] == ["Catherine", "Jesstin", "Nekoashi", "Ryan"]
    assert list(people) == ["self", "jesstin", "nekoashi", "ryan"]
    assert _common.personal_task_identity_labels()["self"] == "Me (Catherine Vee)"


def test_select_offers_exactly_the_configured_people_and_defaults_to_you(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    out = review_server._identity_select_html("assignee_identity")
    assert _options(out, "assignee_identity") == [("self", "Me (Alex Doe)"), ("sam", "Sam Roe"), ("kai", "Kai Poe")]
    assert 'value="self" selected' in out
    assert "IT-Operations" not in out


def test_you_resolve_to_your_own_account_and_others_by_exact_display_name(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    client = FakeClient()
    assert _common.resolve_personal_task_identity(client, "self", 11) == (11, None)
    assert _common.resolve_personal_task_identity(client, "sam", 11) == (22, None)
    assert client.searches == ["Sam Roe"]                                   # looked up once, near-match ignored
    _common.resolve_personal_task_identity(client, "sam", 11)
    assert client.searches == ["Sam Roe"]                                   # cached
    user_id, err = _common.resolve_personal_task_identity(FakeClient(users={}), "kai", 11)
    assert user_id is None and "Kai Poe" in err
    assert _common.resolve_personal_task_identity(client, "self", None)[0] is None


def test_other_choices_are_refused(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    for bad in ("it_operations", "casey", "", "SAM"):
        assert _common.resolve_personal_task_identity(FakeClient(), bad, 11)[0] is None


def test_promote_uses_the_chosen_people_and_rejects_anyone_else(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    client = FakeClient()
    local_id = _common.add_personal_task("Do a thing", "", log_fn=lambda *a, **k: None)["local_id"]
    bad = _common.promote_personal_task(client, local_id, 11, 15, identity_assignee="it_operations", log_fn=lambda *a, **k: None)
    assert bad["ok"] is False and "Invalid Assignee/Requester" in bad["error"] and client.created == []
    ok = _common.promote_personal_task(client, local_id, 11, 15, identity_assignee="kai", identity_requester="sam", log_fn=lambda *a, **k: None)
    assert ok["ok"] and client.created[0]["assignee_id"] == 33 and client.created[0]["requester_id"] == 22
    task = _common.load_personal_tasks()[local_id]
    assert task["assignee_identity"] == "kai" and task["requester_identity"] == "sam"


def test_promote_defaults_both_fields_to_you(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", PEOPLE)
    client = FakeClient()
    local_id = _common.add_personal_task("Another", "", log_fn=lambda *a, **k: None)["local_id"]
    assert _common.promote_personal_task(client, local_id, 11, 15, log_fn=lambda *a, **k: None)["ok"]
    assert client.created[0]["assignee_id"] == 11 and client.created[0]["requester_id"] == 11
