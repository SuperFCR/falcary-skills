from pathlib import Path

import pytest
from click.testing import CliRunner

import overleaf_sync.cli as cli_mod
from overleaf_sync.cli import main
from overleaf_sync.gitops import GitError
from overleaf_sync.registry import Project


def test_clone_default_path_and_registers(monkeypatch, tmp_path):
    clone_calls = {}
    added = {}

    monkeypatch.setattr(cli_mod.gitops, "clone",
                        lambda url, dest: clone_calls.update(url=url, dest=str(dest)))
    monkeypatch.setattr(cli_mod.registry, "add_project",
                        lambda project: added.update(project=project))
    # default dest is ~/overleaf/<alias>; redirect HOME so the test is hermetic
    monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))

    runner = CliRunner()
    result = runner.invoke(
        main, ["clone", "https://git.overleaf.com/PID", "mypaper"])

    assert result.exit_code == 0, result.output
    expected_dest = tmp_path / "overleaf" / "mypaper"
    assert clone_calls["url"] == "https://git.overleaf.com/PID"
    assert clone_calls["dest"] == str(expected_dest)
    proj = added["project"]
    assert isinstance(proj, Project)
    assert proj.alias == "mypaper"
    assert proj.remote == "https://git.overleaf.com/PID"
    assert proj.path == str(expected_dest)


def test_clone_custom_path(monkeypatch, tmp_path):
    added = {}
    monkeypatch.setattr(cli_mod.gitops, "clone", lambda url, dest: None)
    monkeypatch.setattr(cli_mod.registry, "add_project",
                        lambda project: added.update(project=project))

    dest = tmp_path / "custom"
    runner = CliRunner()
    result = runner.invoke(
        main, ["clone", "https://git.overleaf.com/PID", "mypaper", "--path", str(dest)])

    assert result.exit_code == 0, result.output
    assert added["project"].path == str(dest)


def test_clone_giterror_exits_1_with_auth_hint(monkeypatch, tmp_path):
    def boom(url, dest):
        raise GitError("git clone failed (exit 128): fatal: Authentication failed (401)")
    monkeypatch.setattr(cli_mod.gitops, "clone", boom)
    monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))

    runner = CliRunner()
    result = runner.invoke(
        main, ["clone", "https://git.overleaf.com/PID", "mypaper"])

    assert result.exit_code == 1
    assert "Authentication failed" in result.output
    assert "overleaf-ctl login" in result.output


def test_clone_failure_does_not_register(monkeypatch, tmp_path):
    def boom(url, dest):
        raise GitError("git clone failed (exit 128): fatal: repository not found")
    add_calls = []
    monkeypatch.setattr(cli_mod.gitops, "clone", boom)
    monkeypatch.setattr(cli_mod.registry, "add_project",
                        lambda project: add_calls.append(project))
    monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))

    runner = CliRunner()
    result = runner.invoke(
        main, ["clone", "https://git.overleaf.com/PID", "mypaper"])

    assert result.exit_code == 1
    assert add_calls == []  # no half-registered project on failed clone


def test_relative_clone_destination_is_registered_as_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured = {}
    monkeypatch.setattr(cli_mod.gitops, 'clone', lambda url, dest: None)
    monkeypatch.setattr(cli_mod.registry, 'add_project', lambda project: captured.update(project=project))
    result = CliRunner().invoke(main, ['clone', 'https://git.overleaf.com/PID', 'paper', '--path', 'papers/new'])
    assert result.exit_code == 0, result.output
    assert captured['project'].path == str(tmp_path / 'papers/new')


@pytest.mark.parametrize("given", [
    "https://www.overleaf.com/project/0123456789abcdef01234567",
    "https://cn.overleaf.com/project/0123456789abcdef01234567",
    "https://overleaf.com/project/0123456789abcdef01234567/",
    "https://cn.overleaf.com/project/0123456789abcdef01234567?foo=1#bar",
])
def test_clone_accepts_project_page_url(monkeypatch, tmp_path, given):
    """The URL people copy from the browser is not cloneable; map it to the git
    endpoint (the cn mirror shares git.overleaf.com) for both clone and registry."""
    clone_calls = {}
    added = {}
    monkeypatch.setattr(cli_mod.gitops, "clone",
                        lambda url, dest: clone_calls.update(url=url))
    monkeypatch.setattr(cli_mod.registry, "add_project",
                        lambda project: added.update(project=project))

    result = CliRunner().invoke(main, ["clone", given, "paper", "--path", str(tmp_path / "p")])

    assert result.exit_code == 0, result.output
    expected = "https://git.overleaf.com/0123456789abcdef01234567"
    assert clone_calls["url"] == expected
    assert added["project"].remote == expected


def test_clone_taken_alias_fails_before_cloning(monkeypatch, tmp_path, patched_registry_path):
    """Registering after the clone would fail and strand an unregistered clone."""
    cli_mod.registry.add_project(
        Project(alias="paper", path="/elsewhere", remote="https://git.overleaf.com/OLD"))
    clone_calls = []
    monkeypatch.setattr(cli_mod.gitops, "clone",
                        lambda url, dest: clone_calls.append(url))

    result = CliRunner().invoke(
        main, ["clone", "https://git.overleaf.com/PID", "paper", "--path", str(tmp_path / "new")])

    assert result.exit_code == 2
    assert clone_calls == []
    assert "already registered" in result.output
    assert cli_mod.registry.get_project("paper").path == "/elsewhere"


def test_clone_error_text_with_brackets_is_printed_verbatim(monkeypatch, tmp_path):
    def boom(url, dest):
        raise GitError("git clone failed (exit 128): fatal: [remote rejected] see [/var/log/x]")
    monkeypatch.setattr(cli_mod.gitops, "clone", boom)

    result = CliRunner().invoke(
        main, ["clone", "https://git.overleaf.com/PID", "paper", "--path", str(tmp_path / "p")])

    assert result.exit_code == 1
    assert "[remote rejected] see [/var/log/x]" in result.output.replace("\n", "")
