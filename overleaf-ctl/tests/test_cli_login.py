from click.testing import CliRunner

import overleaf_sync.cli as cli_mod
from overleaf_sync.cli import main


def test_login_token_via_stdin(monkeypatch):
    calls = {}

    def fake_ensure(host="git.overleaf.com"):
        calls["ensure"] = host
        return "osxkeychain"

    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper", fake_ensure)
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": None)

    def fake_store(token, host="git.overleaf.com", username="git"):
        calls["token"] = token
        calls["host"] = host

    monkeypatch.setattr(cli_mod.auth, "store_token", fake_store)

    runner = CliRunner()
    result = runner.invoke(main, ["login", "--token-stdin"], input="olp_secret123\n")

    assert result.exit_code == 0
    assert calls["ensure"] == "git.overleaf.com"
    assert calls["token"] == "olp_secret123"
    assert calls["host"] == "git.overleaf.com"
    # token must never be echoed
    assert "olp_secret123" not in result.output


def test_login_token_via_getpass_and_host_override(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper",
                        lambda host: calls.update(ensure=host) or "osxkeychain")
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": None)
    monkeypatch.setattr(cli_mod.auth, "store_token",
                        lambda token, host="git.overleaf.com", username="git": calls.update(token=token, host=host))
    monkeypatch.setattr(cli_mod, "getpass", lambda prompt="": "olp_fromprompt")

    runner = CliRunner()
    result = runner.invoke(main, ["login", "--host", "git.example.com"])

    assert result.exit_code == 0
    assert calls["token"] == "olp_fromprompt"
    assert calls["host"] == "git.example.com"
    # the helper is ensured for the SAME host the token is stored under
    assert calls["ensure"] == "git.example.com"
    assert "olp_fromprompt" not in result.output


def test_login_existing_credential_confirm_overwrite(monkeypatch):
    """Re-login with a stored credential must show the current identity
    (masked token, never the full secret) and ask before overwriting."""
    calls = {}
    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper",
                        lambda host: "osxkeychain")
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": "olp_oldsecrettoken99")
    monkeypatch.setattr(cli_mod.auth, "store_token",
                        lambda token, host="git.overleaf.com", username="git":
                        calls.update(token=token))
    monkeypatch.setattr(cli_mod, "getpass", lambda prompt="": "olp_newtoken")

    runner = CliRunner()
    result = runner.invoke(main, ["login"], input="y\n")

    assert result.exit_code == 0, result.output
    assert calls["token"] == "olp_newtoken"
    # current identity shown, masked — full old token never echoed
    assert "olp_" in result.output and "en99" in result.output
    assert "olp_oldsecrettoken99" not in result.output


def test_login_existing_credential_decline_keeps(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper",
                        lambda host: "osxkeychain")
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": "olp_oldsecrettoken99")
    monkeypatch.setattr(cli_mod.auth, "store_token",
                        lambda *a, **k: calls.update(stored=True))

    result = CliRunner().invoke(main, ["login"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "stored" not in calls
    assert "保留" in result.output


def test_logout_no_credential(monkeypatch):
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": None)
    erased = {}
    monkeypatch.setattr(cli_mod.auth, "erase_credential",
                        lambda host="git.overleaf.com", username="git": erased.update(yes=True))
    result = CliRunner().invoke(main, ["logout"])
    assert result.exit_code == 0
    assert not erased
    assert "没有" in result.output


def test_logout_confirm_erases(monkeypatch):
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": "olp_oldsecrettoken99")
    erased = {}
    monkeypatch.setattr(cli_mod.auth, "erase_credential",
                        lambda host="git.overleaf.com", username="git": erased.update(host=host))
    result = CliRunner().invoke(main, ["logout"], input="y\n")
    assert result.exit_code == 0, result.output
    assert erased["host"] == "git.overleaf.com"
    assert "olp_oldsecrettoken99" not in result.output  # masked only


def test_logout_decline_keeps(monkeypatch):
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": "olp_tok")
    erased = {}
    monkeypatch.setattr(cli_mod.auth, "erase_credential",
                        lambda **k: erased.update(yes=True))
    result = CliRunner().invoke(main, ["logout"], input="n\n")
    assert result.exit_code == 0
    assert not erased


def test_login_reports_helper_and_warns_when_memory_only(monkeypatch):
    """With the Linux fallback the token only lives in memory; saying it went
    to "the system credential store" would be false."""
    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper",
                        lambda host: "cache --timeout=2592000")
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": None)
    monkeypatch.setattr(cli_mod.auth, "store_token",
                        lambda token, host="git.overleaf.com", username="git": None)

    result = CliRunner().invoke(main, ["login", "--token-stdin"], input="olp_secret123\n")

    assert result.exit_code == 0, result.output
    assert "cache --timeout=2592000" in result.output
    assert "内存" in result.output
    assert "credential.https://git.overleaf.com.helper" in result.output.replace("\n", "")


def test_login_persistent_helper_has_no_memory_warning(monkeypatch):
    monkeypatch.setattr(cli_mod.auth, "ensure_credential_helper", lambda host: "store")
    monkeypatch.setattr(cli_mod.auth, "get_credential",
                        lambda host="git.overleaf.com", username="git": None)
    monkeypatch.setattr(cli_mod.auth, "store_token",
                        lambda token, host="git.overleaf.com", username="git": None)

    result = CliRunner().invoke(main, ["login", "--token-stdin"], input="olp_secret123\n")

    assert result.exit_code == 0, result.output
    assert "store" in result.output
    assert "内存" not in result.output
