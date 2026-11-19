from click.testing import CliRunner

import overleaf_sync.cli as cli_mod
from overleaf_sync.cli import main
from overleaf_sync.gitops import GitError, PullResult
from overleaf_sync.registry import Project


def _stub_resolve(monkeypatch, path="/repo"):
    monkeypatch.setattr(
        cli_mod.registry, "get_project",
        lambda alias: Project(alias=alias, path=path,
                              remote="https://git.overleaf.com/PID"))


def test_pull_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(cli_mod.gitops, "pull_rebase",
                        lambda repo: PullResult(ok=True, conflict=False, output="Already up to date."))
    runner = CliRunner()
    result = runner.invoke(main, ["pull", "mypaper"])
    assert result.exit_code == 0, result.output
    assert "Already up to date." in result.output


def test_pull_conflict_exits_1(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(cli_mod.gitops, "pull_rebase",
                        lambda repo: PullResult(ok=False, conflict=True, output="CONFLICT"))
    monkeypatch.setattr(cli_mod.gitops, "unmerged_files", lambda repo: ["x.tex"])
    runner = CliRunner()
    result = runner.invoke(main, ["pull", "mypaper"])
    assert result.exit_code == 1
    assert "x.tex" in result.output


def test_push_ok(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(cli_mod.gitops, "push", lambda repo: "pushed 2 commits")
    runner = CliRunner()
    result = runner.invoke(main, ["push", "mypaper"])
    assert result.exit_code == 0, result.output
    assert "pushed 2 commits" in result.output


def test_push_error_exits_nonzero(monkeypatch):
    _stub_resolve(monkeypatch)
    def boom(repo):
        raise GitError("rejected: non-fast-forward")
    monkeypatch.setattr(cli_mod.gitops, "push", boom)
    runner = CliRunner()
    result = runner.invoke(main, ["push", "mypaper"])
    assert result.exit_code != 0
    assert "rejected" in result.output


def test_push_error_keeps_git_brackets(monkeypatch):
    """git marks refusals as "[rejected]"; rich markup used to swallow it."""
    _stub_resolve(monkeypatch)

    def boom(repo):
        raise GitError("git push failed (exit 1): ! [rejected] HEAD -> master (fetch first)")
    monkeypatch.setattr(cli_mod.gitops, "push", boom)

    result = CliRunner().invoke(main, ["push", "mypaper"])

    assert result.exit_code == 1
    assert "! [rejected] HEAD -> master (fetch first)" in result.output.replace("\n", "")


def test_pull_and_push_output_keep_git_brackets(monkeypatch):
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(
        cli_mod.gitops, "pull_rebase",
        lambda repo: PullResult(ok=True, conflict=False, output=" * [new branch] x -> origin/x"))
    monkeypatch.setattr(cli_mod.gitops, "push", lambda repo: " * [new branch] HEAD -> x")

    pulled = CliRunner().invoke(main, ["pull", "mypaper"])
    pushed = CliRunner().invoke(main, ["push", "mypaper"])

    assert "[new branch] x -> origin/x" in pulled.output
    assert "[new branch] HEAD -> x" in pushed.output
