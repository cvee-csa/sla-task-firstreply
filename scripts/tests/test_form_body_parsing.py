"""Handler._parse_form_body() centralizes what used to be three lines
(Content-Length parse, body read, parse_qs) duplicated at nine separate
do_POST branches -- see its own docstring in review_server.py. This exercises
the three ways a raw request used to be able to make that raise straight
past every other fail-open banner this app shows: a non-numeric
Content-Length, a negative one, and a body that isn't valid UTF-8. None of
these should ever reach a client as a raised exception; each should just
degrade to "no form fields."

Builds a bare Handler instance via __new__ (skipping BaseHTTPRequestHandler's
socket-based __init__ entirely, which expects a real connected socket) and
hand-sets just the two attributes _parse_form_body actually reads: .headers
(anything with a dict-like .get) and .rfile (any read(n)-capable stream).
"""
import io

import review_server


class _FakeHeaders:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _make_handler(body: bytes, content_length):
    handler = review_server.Handler.__new__(review_server.Handler)
    headers = {}
    if content_length is not None:
        headers["Content-Length"] = content_length
    handler.headers = _FakeHeaders(headers)
    handler.rfile = io.BytesIO(body)
    return handler


def test_parse_form_body_normal_case():
    handler = _make_handler(b"subject=Write+docs&est_mins=30", "30")
    form = handler._parse_form_body()
    assert form == {"subject": ["Write docs"], "est_mins": ["30"]}


def test_parse_form_body_no_content_length_header_is_empty_form():
    handler = _make_handler(b"", None)
    assert handler._parse_form_body() == {}


def test_parse_form_body_non_numeric_content_length_degrades_to_empty_form():
    """This is the concrete bug the audit flagged: int(self.headers.get(
    "Content-Length", 0)) used to raise a raw, uncaught ValueError here."""
    handler = _make_handler(b"subject=Test", "not-a-number")
    assert handler._parse_form_body() == {}


def test_parse_form_body_blank_content_length_degrades_to_empty_form():
    handler = _make_handler(b"subject=Test", "")
    assert handler._parse_form_body() == {}


def test_parse_form_body_negative_content_length_degrades_to_empty_form():
    """A technically-valid int() (e.g. "-1") used to reach
    self.rfile.read(length) with a negative count, which is undefined/hangs
    on a real socket-backed stream rather than erroring cleanly."""
    handler = _make_handler(b"subject=Test", "-1")
    assert handler._parse_form_body() == {}


def test_parse_form_body_invalid_utf8_degrades_to_empty_form():
    invalid_utf8 = b"subject=\xff\xfe"
    handler = _make_handler(invalid_utf8, str(len(invalid_utf8)))
    assert handler._parse_form_body() == {}


def test_parse_form_body_zero_content_length_is_empty_form_without_reading():
    handler = _make_handler(b"", "0")
    assert handler._parse_form_body() == {}
