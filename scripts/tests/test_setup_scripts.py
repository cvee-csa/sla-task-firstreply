"""Setup/install tooling: reload_launchd.sh, watchdog.py paths, .env.example.

reload_launchd.sh is run for real under zsh with a stub `launchctl` first on
PATH and HOME pointed at a temp dir, so nothing touches the real
~/Library/LaunchAgents. Skipped where zsh is not installed.
"""
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
REPO = SCRIPTS_DIR.parent
SCRIPT = SCRIPTS_DIR / "reload_launchd.sh"
PLISTS = [
    "com.csa.zendesk-first-reply-check",
    "com.csa.zendesk-review-server",
    "com.csa.zendesk-watchdog",
]
AUTHOR = "/Users/catherinevee"

needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _make_env(tmp_path: Path, repo_name="checkout", with_env=True, with_mcp=True):
    """A throwaway copy of the repo's install files plus a fake
    zendesk-mcp-server (a venv python that just execs the test interpreter),
    a stub launchctl and an empty HOME."""
    repo = tmp_path / repo_name
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "reload_launchd.sh")
    for label in PLISTS:
        shutil.copy(SCRIPTS_DIR / f"{label}.plist", repo / "scripts" / f"{label}.plist")
    if with_env:
        (repo / ".env").write_text("ZENDESK_SUBDOMAIN=x\n")

    mcp = tmp_path / "mcp-checkout" / "zendesk-mcp-server"
    if with_mcp:
        _exe(mcp / ".venv" / "bin" / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        stub = mcp / "src" / "zendesk_mcp_server"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text("")
        (stub / "zendesk_client.py").write_text("class ZendeskClient:\n    pass\n")

    bin_dir = tmp_path / "bin"
    log = tmp_path / "launchctl.log"
    _exe(bin_dir / "launchctl", f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "ZENDESK_MCP_DIR": str(mcp),
    }
    return repo, mcp, home, log, env


def _run(repo: Path, env: dict, *args: str):
    return subprocess.run(
        ["zsh", str(repo / "scripts" / "reload_launchd.sh"), *args],
        cwd="/", env=env, capture_output=True, text=True, timeout=60,
    )


@needs_zsh
def test_install_substitutes_paths_into_the_copied_plists(tmp_path):
    repo, mcp, home, log, env = _make_env(tmp_path)
    r = _run(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr

    agents = home / "Library" / "LaunchAgents"
    repo_r, mcp_r = repo.resolve(), mcp.resolve()
    for label in PLISTS:
        text = (agents / f"{label}.plist").read_text()
        assert AUTHOR not in text, f"{label}: author path left behind"
        data = plistlib.loads(text.encode())
        assert data["WorkingDirectory"] == str(repo_r)
        assert data["ProgramArguments"][1].startswith(str(repo_r) + "/scripts/")
        assert data["StandardOutPath"].startswith(str(repo_r) + "/scripts/output/")
    first = plistlib.loads((agents / f"{PLISTS[0]}.plist").read_bytes())
    assert first["ProgramArguments"][0] == str(mcp_r / ".venv" / "bin" / "python3")
    assert first["EnvironmentVariables"]["PYTHONPATH"] == str(mcp_r / "src")
    # The watchdog avoids the venv: its interpreter is the real one underneath.
    watchdog = plistlib.loads((agents / f"{PLISTS[2]}.plist").read_bytes())
    assert Path(watchdog["ProgramArguments"][0]).exists()
    assert ".venv" not in watchdog["ProgramArguments"][0]


@needs_zsh
def test_install_leaves_repo_plists_untouched_and_creates_output_dir(tmp_path):
    repo, _mcp, _home, _log, env = _make_env(tmp_path)
    before = {l: (repo / "scripts" / f"{l}.plist").read_bytes() for l in PLISTS}
    assert not (repo / "scripts" / "output").exists()
    assert _run(repo, env).returncode == 0
    for label in PLISTS:
        assert (repo / "scripts" / f"{label}.plist").read_bytes() == before[label]
    assert (repo / "scripts" / "output").is_dir()


@needs_zsh
def test_install_boots_out_then_bootstraps_every_job(tmp_path):
    repo, _mcp, home, log, env = _make_env(tmp_path)
    assert _run(repo, env).returncode == 0
    calls = log.read_text().splitlines()
    for label in PLISTS:
        out = next(i for i, c in enumerate(calls) if c.startswith("bootout") and c.endswith(label))
        boot = next(i for i, c in enumerate(calls)
                    if c.startswith("bootstrap") and c.endswith(f"{label}.plist"))
        assert out < boot


@needs_zsh
def test_install_xml_escapes_special_characters_in_paths(tmp_path):
    repo, _mcp, home, _log, env = _make_env(tmp_path, repo_name="a&b")
    r = _run(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (home / "Library" / "LaunchAgents" / f"{PLISTS[0]}.plist").read_text()
    assert "a&amp;b" in text
    data = plistlib.loads(text.encode())  # raises if the XML is malformed
    assert data["WorkingDirectory"].endswith("a&b")


@needs_zsh
def test_check_mode_installs_nothing(tmp_path):
    repo, _mcp, home, log, env = _make_env(tmp_path)
    r = _run(repo, env, "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Check complete" in r.stdout
    assert not (home / "Library").exists()
    assert not log.exists()
    assert not (repo / "scripts" / "output").exists()


@needs_zsh
def test_missing_dotenv_stops_with_a_pointer_to_the_example(tmp_path):
    repo, _mcp, home, log, env = _make_env(tmp_path, with_env=False)
    r = _run(repo, env)
    assert r.returncode == 1
    assert ".env.example" in r.stdout
    assert not log.exists()


@needs_zsh
def test_missing_mcp_checkout_names_the_override_variable(tmp_path):
    repo, _mcp, home, log, env = _make_env(tmp_path, with_mcp=False)
    r = _run(repo, env)
    assert r.returncode == 1
    assert "ZENDESK_MCP_DIR" in r.stdout
    assert not log.exists()


@needs_zsh
def test_sibling_checkout_is_found_without_the_override(tmp_path):
    repo, mcp, _home, _log, env = _make_env(tmp_path)
    sibling = tmp_path / "zendeskmcp" / "zendesk-mcp-server"
    shutil.copytree(mcp, sibling, symlinks=True)
    # The copied venv python is a wrapper script, not a symlink, so it still works.
    del env["ZENDESK_MCP_DIR"]
    r = _run(repo, env, "--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert str(sibling.resolve()) in r.stdout


def test_committed_plists_still_hold_the_substitution_anchors():
    """The script replaces these exact strings; if a plist is edited so one no
    longer matches, installs on other machines would silently keep the
    author's path."""
    script = SCRIPT.read_text()
    anchors = re.findall(r'^PLIST_\w+="([^"]+)"', script, re.M)
    assert len(anchors) == 3
    combined = "".join((SCRIPTS_DIR / f"{l}.plist").read_text() for l in PLISTS)
    for anchor in anchors:
        assert anchor in combined, anchor


@pytest.mark.parametrize("label", PLISTS)
def test_committed_plists_are_well_formed_xml(label):
    """A bare `--` inside an XML comment is illegal; launchd tolerates it but
    stricter parsers (and plistlib) reject the file."""
    plistlib.loads((SCRIPTS_DIR / f"{label}.plist").read_bytes())


def test_watchdog_repo_root_follows_the_file_location():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import watchdog
    finally:
        sys.path.pop(0)
    assert watchdog.REPO_ROOT == Path(watchdog.__file__).resolve().parent.parent
    assert AUTHOR not in (SCRIPTS_DIR / "watchdog.py").read_text().split("REPO_ROOT =")[1].splitlines()[0]


def test_applescript_payload_path_is_home_relative():
    text = (SCRIPTS_DIR / "csa_notifier.applescript").read_text()
    assert "path to home folder" in text
    assert AUTHOR not in text


# --- .env.example ------------------------------------------------------------

def _example_names():
    text = (REPO / ".env.example").read_text()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))


def test_env_example_lists_every_required_zendesk_setting():
    names = _example_names()
    for required in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_KEY",
                     "ZENDESK_CLIENT_ID", "ZENDESK_CLIENT_SECRET", "ZENDESK_REDIRECT_URI"):
        assert required in names


def test_env_example_names_are_all_read_by_the_code():
    code = (SCRIPTS_DIR / "_common.py").read_text() + (SCRIPTS_DIR / "review_server.py").read_text() \
        + (SCRIPTS_DIR / "first_reply_check.py").read_text() + (SCRIPTS_DIR / "_projects.py").read_text()
    for name in _example_names():
        assert f'"{name}"' in code, f"{name} in .env.example is not read anywhere"


def test_env_example_holds_no_credentials():
    for line in (REPO / ".env.example").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ("ZENDESK_API_KEY", "ZENDESK_CLIENT_ID", "ZENDESK_CLIENT_SECRET", "AIRTABLE_TOKEN"):
            assert value == "", f"{key} must be blank in the example file"


def test_gitignore_keeps_secrets_ignored_but_allows_the_example():
    lines = (REPO / ".gitignore").read_text().splitlines()
    assert ".env" in lines and ".env.*" in lines
    assert lines.index("!.env.example") > lines.index(".env.*")
