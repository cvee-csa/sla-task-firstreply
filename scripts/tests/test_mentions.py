"""Tagging teammates (@mentions) on an Internal Comment.

Zendesk stores a mention as <a href="/users/ID" class="atmention">Name</a> at
the top of the comment's html_body (copied from a comment written in Zendesk's
own composer). These tests exercise the real Handler._post with a fake client
that records what it was asked to post; all names and ids are made up.
"""
import json

import pytest

import _common
import review_server

PEOPLE = ["Alex Owner", "Ryan Example", "Nekoashi Sample"]


@pytest.fixture(autouse=True)
def people(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", list(PEOPLE))
    monkeypatch.setenv("ZENDESK_EMAIL", "")


class FakeClient:
    def __init__(self, keep_mentions=True, readback_fails=False, known=None, existing_public=False):
        self.client = object()
        self.keep_mentions = keep_mentions
        self.readback_fails = readback_fails
        self.posted = []
        self.comments = []
        if existing_public:
            self.comments.append({"public": True, "author_id": 99, "html_body": "someone answered"})
        known = PEOPLE[1:] if known is None else known
        self.users = [
            {"id": 11 + i, "name": n, "role": "agent", "email": f"u{i}@example.com"} for i, n in enumerate(known)
        ]

    def search_users(self, query):
        return [u for u in self.users if query.casefold() in u["name"].casefold()]

    def post_comment(self, ticket_id, comment, public=True):
        self.posted.append((ticket_id, comment, public))
        body = comment if self.keep_mentions else comment.replace(' class="atmention"', "")
        self.comments.append({"public": public, "author_id": 1, "html_body": body})

    def get_ticket_comments(self, ticket_id):
        if self.readback_fails and self.posted:
            raise RuntimeError("boom")
        return list(self.comments)


def _seed_pending():
    _common.PENDING_FILE.write_text(json.dumps({
        "500": {"ticket_id": "500", "status": "pending", "subject": "Printer", "requester_id": 7,
                "url": "https://example.zendesk.com/agent/tickets/500", "draft": "Hey!"},
    }))


def _post(monkeypatch, client, **kwargs):
    monkeypatch.setattr(_common, "make_client", lambda: client)
    handler = review_server.Handler.__new__(review_server.Handler)
    kwargs.setdefault("public", False)
    return handler._post("500", kwargs.pop("text", "Heads up"), **kwargs)


RYAN = '<a href="/users/11" class="atmention">Ryan Example</a>'
NEKO = '<a href="/users/12" class="atmention">Nekoashi Sample</a>'


# --- helpers ------------------------------------------------------------------

def test_choices_exclude_you_and_keep_order():
    assert list(_common.mention_choices().values()) == ["Ryan Example", "Nekoashi Sample"]
    assert "self" not in _common.mention_choices()


def test_clean_keys_dedupes_orders_and_rejects_unknown():
    assert _common.clean_mention_keys(["nekoashi", "ryan", "ryan"]) == ["ryan", "nekoashi"]
    assert _common.clean_mention_keys([]) == []
    with pytest.raises(ValueError):
        _common.clean_mention_keys(["ryan", "self"])      # yourself is not offered
    with pytest.raises(ValueError):
        _common.clean_mention_keys(["12345"])


def test_mention_html_matches_zendesks_own_markup_and_escapes_names():
    assert _common.mention_html([(11, "Ryan Example"), (12, "Nekoashi Sample")]) == f"{RYAN} {NEKO}<br><br>"
    assert _common.mention_html([]) == ""
    out = _common.mention_html([(11, 'A <b>&"x"')])
    assert "<b>" not in out and "&lt;b&gt;&amp;" in out


# --- posting ------------------------------------------------------------------

def test_internal_comment_sends_real_mentions_and_confirms_them(monkeypatch):
    _seed_pending()
    client = FakeClient()
    msg = _post(monkeypatch, client, mention_keys=["ryan", "nekoashi"])
    assert client.posted == [(500, f"{RYAN} {NEKO}<br><br>Heads up", False)]
    assert "Tagged: Ryan Example, Nekoashi Sample (mention confirmed in Zendesk)." in msg
    entry = _common.load_pending()["500"]
    assert entry["status"] == "posted" and entry["posted_public"] is False
    assert entry["draft"] == "Heads up"            # the stored draft has no anchors in it


def test_no_tags_leaves_the_comment_exactly_as_typed(monkeypatch):
    _seed_pending()
    client = FakeClient()
    msg = _post(monkeypatch, client)
    assert client.posted == [(500, "Heads up", False)]
    assert "Tagged" not in msg


def test_public_reply_ignores_tags_and_says_so(monkeypatch):
    _seed_pending()
    client = FakeClient()
    msg = _post(monkeypatch, client, public=True, mention_keys=["ryan"])
    assert client.posted == [(500, "Heads up", True)]
    assert "Tags apply to internal comments only" in msg and "Tagged:" not in msg


def test_forged_choice_posts_nothing(monkeypatch):
    _seed_pending()
    client = FakeClient()
    msg = _post(monkeypatch, client, mention_keys=["ryan", "99999"])
    assert "Unknown person to tag" in msg and "Nothing was posted" in msg
    assert client.posted == [] and _common.load_pending()["500"]["status"] == "pending"


def test_person_missing_in_zendesk_posts_nothing(monkeypatch):
    _seed_pending()
    client = FakeClient(known=["Ryan Example"])        # Nekoashi has no Zendesk user
    msg = _post(monkeypatch, client, mention_keys=["ryan", "nekoashi"])
    assert "Could not find a Zendesk user named Nekoashi Sample" in msg
    assert client.posted == [] and _common.load_pending()["500"]["status"] == "pending"


def test_warns_when_zendesk_does_not_keep_a_mention(monkeypatch):
    _seed_pending()
    client = FakeClient(keep_mentions=False)
    msg = _post(monkeypatch, client, mention_keys=["ryan"])
    assert "Zendesk did not keep the mention for Ryan Example" in msg
    assert _common.load_pending()["500"]["status"] == "posted"      # the comment itself did post


def test_warns_when_the_read_back_fails(monkeypatch):
    _seed_pending()
    client = FakeClient(readback_fails=True)
    msg = _post(monkeypatch, client, mention_keys=["ryan"])
    assert "could not be read back" in msg and "boom" in msg
    assert _common.load_pending()["500"]["status"] == "posted"


def test_confirm_page_carries_the_tags_through(monkeypatch):
    _seed_pending()
    client = FakeClient(existing_public=True)
    result = _post(monkeypatch, client, mention_keys=["ryan"])
    assert isinstance(result, tuple) and result[0] == "confirm"
    page = result[1].decode()
    assert 'name="mention" value="ryan"' in page and "Tagging: Ryan Example" in page
    assert client.posted == []
    # The "post anyway" click sends the same field back.
    msg = _post(monkeypatch, client, mention_keys=["ryan"], confirmed=True)
    assert client.posted == [(500, f"{RYAN}<br><br>Heads up", False)]
    assert "mention confirmed in Zendesk" in msg


# --- the card -----------------------------------------------------------------

def _card():
    return review_server._render_ticket({
        "ticket_id": "500", "subject": "Printer", "url": "https://example.zendesk.com/agent/tickets/500",
        "draft": "Hey!", "status": "pending", "requester_name": "Pat", "name_confidence": "confirmed",
    })


def test_card_offers_everyone_but_you():
    card = _card()
    assert 'name="mention" value="ryan"' in card and 'name="mention" value="nekoashi"' in card
    assert 'value="self"' not in card and "Alex Owner" not in card
    assert "Tag on internal comment:" in card


def test_card_has_no_tag_row_when_you_are_alone(monkeypatch):
    monkeypatch.setattr(_common, "OVERVIEW_PEOPLE", ["Alex Owner"])
    assert 'name="mention"' not in _card() and "tag-row" not in _card()


def test_picker_css_present_with_single_braces():
    css = review_server._page("<p>x</p>").decode().split("</style>")[0]
    assert ".tag-chip:has(input:checked) {" in css and ".tag-row {" in css
    assert ".tag-row {{" not in css
