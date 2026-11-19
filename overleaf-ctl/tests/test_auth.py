# tests/test_auth.py
from unittest import mock

import overleaf_sync.auth as auth


def test_default_host_constant():
    assert auth.DEFAULT_HOST == "git.overleaf.com"


SCOPED_KEY = "credential.https://git.overleaf.com.helper"


def _fake_git_config(calls, effective=""):
    """Fake subprocess.run: the `--get-urlmatch` lookup answers ``effective``."""
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--get-urlmatch" in cmd:
            return mock.Mock(returncode=0 if effective else 1,
                             stdout=effective + "\n" if effective else "", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    return fake_run


def test_ensure_credential_helper_sets_host_scoped_when_unset(monkeypatch):
    """No helper applies to the host: set the platform default for that host
    only, never the global catch-all `credential.helper` (it would change how
    every other host, e.g. GitHub, stores credentials)."""
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    calls = []
    with mock.patch.object(auth.subprocess, "run", side_effect=_fake_git_config(calls)):
        helper = auth.ensure_credential_helper()

    assert helper == "osxkeychain"
    assert calls[0] == ["git", "config", "--get-urlmatch", "credential.helper",
                        "https://git.overleaf.com"]
    assert calls[1:] == [["git", "config", "--global", SCOPED_KEY, "osxkeychain"]]


def test_ensure_credential_helper_noop_when_already_set():
    """If a helper already applies to the host, do NOT overwrite it."""
    calls = []
    with mock.patch.object(auth.subprocess, "run",
                           side_effect=_fake_git_config(calls, effective="store")):
        helper = auth.ensure_credential_helper()

    assert helper == "store"
    assert len(calls) == 1 and "--get-urlmatch" in calls[0], calls


def test_ensure_credential_helper_scopes_custom_host(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    calls = []
    with mock.patch.object(auth.subprocess, "run", side_effect=_fake_git_config(calls)):
        auth.ensure_credential_helper("git.example.org")

    assert calls[0][-1] == "https://git.example.org"
    assert calls[1] == ["git", "config", "--global",
                        "credential.https://git.example.org.helper", "osxkeychain"]


def _isolated_git_config(tmp_path, monkeypatch):
    """Point git at an empty global config; nothing on this machine is touched."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)

    def get(*args):
        import subprocess
        return subprocess.run(["git", "config", "--global", *args],
                              capture_output=True, text=True).stdout.strip()
    return get


def test_ensure_credential_helper_real_git_leaves_other_hosts_alone(tmp_path, monkeypatch):
    get = _isolated_git_config(tmp_path, monkeypatch)
    monkeypatch.setattr(auth.sys, "platform", "linux")

    helper = auth.ensure_credential_helper()

    assert helper.startswith("cache --timeout=")
    assert get("--get", SCOPED_KEY) == helper
    assert get("--get", "credential.helper") == ""  # catch-all untouched
    assert get("--get-urlmatch", "credential.helper", "https://github.com") == ""


def test_ensure_credential_helper_real_git_respects_existing(tmp_path, monkeypatch):
    get = _isolated_git_config(tmp_path, monkeypatch)
    import subprocess
    # A host-scoped helper the user configured beforehand...
    subprocess.run(["git", "config", "--global", SCOPED_KEY, "store"], check=True)
    assert auth.ensure_credential_helper() == "store"
    assert get("--get-all", SCOPED_KEY) == "store"
    # ...or a catch-all one: both already apply to the host.
    subprocess.run(["git", "config", "--global", "--unset", SCOPED_KEY], check=True)
    subprocess.run(["git", "config", "--global", "credential.helper", "osxkeychain"], check=True)
    assert auth.ensure_credential_helper() == "osxkeychain"
    assert get("--get", SCOPED_KEY) == ""


def test_store_token_feeds_credential_approve_stdin():
    """store_token must pipe the exact credential payload to `git credential approve`."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        captured["kwargs"] = kwargs
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        auth.store_token("olp_secrettoken123")

    # Command is `git credential approve` — token must NOT be in argv.
    assert captured["cmd"] == ["git", "credential", "approve"]
    assert all("olp_secrettoken123" not in part for part in captured["cmd"])

    # Exact stdin payload format (trailing blank line terminates the request).
    assert captured["input"] == (
        "protocol=https\n"
        "host=git.overleaf.com\n"
        "username=git\n"
        "password=olp_secrettoken123\n"
        "\n"
    )
    # Payload must be passed as text (not bytes) on stdin.
    assert captured["kwargs"].get("text") is True


def test_store_token_honors_host_and_username():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        auth.store_token("tok", host="git.example.org", username="alice")

    assert captured["input"] == (
        "protocol=https\n"
        "host=git.example.org\n"
        "username=alice\n"
        "password=tok\n"
        "\n"
    )


def test_store_token_raises_on_git_failure():
    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=1, stdout="", stderr="boom")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        try:
            auth.store_token("tok")
        except RuntimeError as exc:
            # Error message must NOT leak the token.
            assert "tok" not in str(exc)
        else:
            raise AssertionError("expected RuntimeError on git credential failure")


def test_store_token_never_logs_or_persists_token(tmp_path, capsys):
    """The token must not appear in stdout/stderr nor be written to any file."""
    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        auth.store_token("olp_supersecret")

    out = capsys.readouterr()
    assert "olp_supersecret" not in out.out
    assert "olp_supersecret" not in out.err

    # No file under tmp_path (or cwd) should contain the token.
    leaked = []
    for p in tmp_path.rglob("*"):
        if p.is_file() and "olp_supersecret" in p.read_text(errors="ignore"):
            leaked.append(str(p))
    assert not leaked, f"token leaked into files: {leaked}"


# ---- platform-dispatched credential helper ---------------------------------

def test_default_helper_per_platform(monkeypatch):
    """Hardcoding osxkeychain poisons non-mac global git config; pick the
    platform's native helper instead."""
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    assert auth.default_credential_helper() == "osxkeychain"
    monkeypatch.setattr(auth.sys, "platform", "win32")
    assert auth.default_credential_helper() == "manager"
    # Linux: in-memory cache, with a timeout long enough to outlive a session
    # (git's stock 15 minutes drops the login mid-work).
    monkeypatch.setattr(auth.sys, "platform", "linux")
    assert auth.default_credential_helper() == (
        f"cache --timeout={auth.LINUX_CACHE_TIMEOUT_SECONDS}")
    assert auth.LINUX_CACHE_TIMEOUT_SECONDS >= 24 * 60 * 60


def test_ensure_credential_helper_uses_platform_helper_on_windows(monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "win32")
    calls = []
    with mock.patch.object(auth.subprocess, "run", side_effect=_fake_git_config(calls)):
        auth.ensure_credential_helper()

    assert calls[1:] == [["git", "config", "--global", SCOPED_KEY, "manager"]]


# ---- get_credential / erase_credential --------------------------------------

def test_get_credential_returns_stored_token():
    def fake_run(cmd, **kwargs):
        assert cmd == ["git", "credential", "fill"]
        # must never allow interactive prompting
        env = kwargs.get("env") or {}
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        return mock.Mock(returncode=0,
                         stdout="protocol=https\nhost=git.overleaf.com\n"
                                "username=git\npassword=olp_storedtok\n",
                         stderr="")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        assert auth.get_credential() == "olp_storedtok"


def test_get_credential_none_when_absent():
    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=128, stdout="", stderr="could not read")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        assert auth.get_credential() is None


def test_erase_credential_feeds_reject_via_stdin():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch.object(auth.subprocess, "run", side_effect=fake_run):
        auth.erase_credential(host="git.overleaf.com")

    assert captured["cmd"] == ["git", "credential", "reject"]
    assert captured["input"] == (
        "protocol=https\nhost=git.overleaf.com\nusername=git\n\n")
