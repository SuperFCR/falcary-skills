"""Chinese drafts, alignment, round log, and the .writing records repository."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from overleaf_sync import drafts, gitops, writing
from overleaf_sync.cli import main
from overleaf_sync.registry import Project
from tests.conftest import _clone, _git

MAIN = r"""\documentclass{article}
\begin{document}
\begin{abstract}\input{sections/0_abstract}\end{abstract}
\input{sections/1_intro}
\input{sections/3_method.tex}
% \input{sections/9_old}
\end{document}
"""


def write(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_paper(repo, push=True):
    """A paper with section files, one of them not input by main."""
    write(repo, "main.tex", MAIN)
    for name in ["0_abstract", "1_intro", "3_method", "9_old", "X_appendix"]:
        write(repo, f"sections/{name}.tex", f"English {name}.\n")
    write(repo, "tables/tab.tex", "table\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "paper", cwd=repo)
    if push:
        _git("push", "-q", cwd=repo)
    return repo


def register(monkeypatch, repo):
    monkeypatch.setattr("overleaf_sync.cli.registry.get_project",
                        lambda alias: Project(alias=alias, path=str(repo), remote=str(repo)))


def states(repo):
    return {s.key: s.state for s in drafts.status(repo).sections}


NOW = datetime(2026, 11, 14, 16, 5, tzinfo=timezone.utc)


# ------------------------------------------------------------------ pairing

def test_discover_pairs_section_files_by_name(local_clone):
    make_paper(local_clone)
    found = {s.key: s for s in drafts.discover(local_clone)}
    assert sorted(found) == ["0_abstract", "1_intro", "3_method", "9_old", "X_appendix"]
    assert found["1_intro"].tex == "sections/1_intro.tex"
    assert found["1_intro"].draft == "zh/1_intro.md"
    # Input chain: commented-out and never-input files are flagged, not dropped.
    assert found["3_method"].referenced and found["0_abstract"].referenced
    assert not found["9_old"].referenced and not found["X_appendix"].referenced
    assert "tables/tab.tex" not in {s.tex for s in found.values()}


def test_discover_without_section_folder_uses_main_inputs(tmp_path):
    write(tmp_path, "paper/main.tex",
          r"\documentclass{article}\begin{document}\input{intro}\input{parts/method}\end{document}")
    write(tmp_path, "paper/intro.tex", "x")
    write(tmp_path, "paper/parts/method.tex", r"\input{parts/detail}")
    write(tmp_path, "paper/parts/detail.tex", "y")  # resolved from main's folder
    write(tmp_path, "paper/unused.tex", "z")
    found = {s.key: s.tex for s in drafts.discover(tmp_path)}
    assert found == {"intro": "paper/intro.tex", "method": "paper/parts/method.tex",
                     "detail": "paper/parts/detail.tex"}


def test_discover_mirrors_path_when_names_collide(tmp_path):
    write(tmp_path, "sections/1_intro.tex", "a")
    write(tmp_path, "supp/sections/1_intro.tex", "b")
    write(tmp_path, ".writing/sections/2_x.tex", "local only")  # never a section
    found = {s.tex: s.draft for s in drafts.discover(tmp_path)}
    assert found == {"sections/1_intro.tex": "zh/sections/1_intro.md",
                     "supp/sections/1_intro.tex": "zh/supp/sections/1_intro.md"}


# --------------------------------------------------------------------- init

def test_init_creates_empty_drafts_records_repo_and_status(local_clone):
    make_paper(local_clone)
    result = writing.initialize(local_clone)
    state = local_clone / ".writing"
    for name in ["0_abstract", "1_intro", "3_method", "9_old", "X_appendix"]:
        assert (state / f"zh/{name}.md").read_text() == ""
        assert f".writing/zh/{name}.md" in result["created"]
    assert drafts.records_repo(local_clone) == state.resolve()
    assert result["records_committed"] is True  # skeleton committed with the paper's identity
    assert _git("-C", ".writing", "status", "--porcelain", cwd=local_clone).stdout == ""
    assert "未起草" in (state / "status.md").read_text()
    # status.md is generated per machine; log.md merges by union across machines
    assert _git("-C", ".writing", "check-ignore", "status.md", cwd=local_clone).returncode == 0
    assert "merge=union" in (state / ".gitattributes").read_text()
    assert not gitops.get_status(local_clone).dirty


def test_init_keeps_existing_drafts_and_other_documents(local_clone):
    make_paper(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "已有中文稿")
    write(local_clone, ".writing/zh/0_motivation.md", "动机逻辑")
    writing.initialize(local_clone)
    assert (local_clone / ".writing/zh/1_intro.md").read_text() == "已有中文稿"
    report = drafts.status(local_clone)
    assert report.other_drafts == ["zh/0_motivation.md"]
    assert {s.key: s.state for s in report.sections}["1_intro"] == "未对齐"


def test_init_refuses_broken_records_git_dir(local_clone):
    make_paper(local_clone)
    write(local_clone, ".writing/.git", "gitdir: /nonexistent\n")
    with pytest.raises(drafts.DraftError, match="不是有效的 Git 仓库"):
        writing.initialize(local_clone)


# ---------------------------------------------------------------- alignment

def test_alignment_states_follow_both_sides(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    draft = local_clone / ".writing/zh/1_intro.md"
    tex = local_clone / "sections/1_intro.tex"
    assert states(local_clone)["1_intro"] == "未起草"
    draft.write_text("第一段：事件相机……\n")
    assert states(local_clone)["1_intro"] == "未对齐"
    drafts.align(local_clone, ["1_intro"], now=NOW)
    assert states(local_clone)["1_intro"] == "已对齐"
    draft.write_bytes(draft.read_bytes().replace(b"\n", b"\r\n"))  # line endings only
    assert states(local_clone)["1_intro"] == "已对齐"
    draft.write_text("第一段：改写后的逻辑。\n")
    assert states(local_clone)["1_intro"] == "中文领先"
    drafts.align(local_clone, ["sections/1_intro.tex"], now=NOW)  # any section name form
    tex.write_text("Polished English.\n")
    assert states(local_clone)["1_intro"] == "英文领先"
    draft.write_text("第一段：再改。\n")
    assert states(local_clone)["1_intro"] == "两边都改"
    record = json.loads((local_clone / ".writing/align.json").read_text())["sections"]["1_intro"]
    assert record["tex"] == "sections/1_intro.tex" and record["draft"] == "zh/1_intro.md"
    assert record["aligned_at"].startswith("2026-11-14T16:05")


def test_align_rejects_empty_unknown_and_uninitialized(local_clone, tmp_path):
    make_paper(local_clone)
    with pytest.raises(drafts.DraftError, match="writing init"):
        drafts.align(local_clone, ["1_intro"])
    writing.initialize(local_clone)
    with pytest.raises(drafts.DraftError, match="为空"):
        drafts.align(local_clone, ["1_intro"])
    with pytest.raises(drafts.DraftError, match="未知章节：2_related"):
        drafts.align(local_clone, ["2_related"])
    with pytest.raises(drafts.DraftError, match="没有可对齐"):
        drafts.align(local_clone, all_sections=True)
    assert not (local_clone / ".writing/align.json").exists()


def test_align_all_takes_only_written_drafts(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "引言")
    write(local_clone, ".writing/zh/3_method.md", "方法")
    done = drafts.align(local_clone, all_sections=True)
    assert sorted(s.key for s in done) == ["1_intro", "3_method"]
    assert states(local_clone)["0_abstract"] == "未起草"


def test_corrupt_alignment_file_is_reported(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/align.json", "{bad")
    with pytest.raises(drafts.DraftError, match="align.json"):
        drafts.status(local_clone)


# ------------------------------------------------------------ records repo

def test_records_repo_never_falls_through_to_the_paper(local_clone):
    """Without .writing/.git, `git -C .writing` would act on the paper repo."""
    make_paper(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "草稿")
    assert drafts.records_repo(local_clone) is None
    assert drafts.commit_records(local_clone, "x") is False
    assert drafts.sync_records(local_clone, "x") == ""
    assert _git("diff", "--cached", "--name-only", cwd=local_clone).stdout == ""
    assert _git("rev-list", "--count", "HEAD", cwd=local_clone).stdout.strip() == "2"


def test_records_commit_borrows_paper_identity(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    assert _git("-C", ".writing", "config", "--local", "user.email",
                cwd=local_clone).stdout.strip() == "test@example.com"
    write(local_clone, ".writing/zh/1_intro.md", "草稿")
    assert drafts.commit_records(local_clone, "第一稿") is True
    author = _git("-C", ".writing", "log", "-1", "--format=%an <%ae>|%s", cwd=local_clone).stdout
    assert author.strip() == "Test User <test@example.com>|第一稿"
    assert drafts.commit_records(local_clone, "again") is False  # nothing new


# ---------------------------------------------------------------------- log

def test_log_records_round_and_commits(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "引言中文稿")
    write(local_clone, "sections/1_intro.tex", "Translated intro.\n")
    drafts.align(local_clone, ["1_intro"], now=NOW)
    entry = drafts.append_log(local_clone, "改写引言第二段\n原因：gap 说法太泛。\n下一步：方法。", now=NOW)
    assert entry.startswith("## 2026-11-14 16:05 · 改写引言第二段\n")
    assert "原因：gap 说法太泛。" in entry
    assert "- 写作档案改动：zh/1_intro.md\n" in entry
    assert "- 论文未同步改动：sections/1_intro.tex\n" in entry
    assert "- 章节状态：1_intro 已对齐\n" in entry
    log = (local_clone / ".writing/log.md").read_text()
    assert log.startswith(drafts.LOG_HEADER) and log.endswith(entry)
    assert drafts.commit_records(local_clone, "改写引言第二段\n原因：gap 说法太泛。") is True
    logged = _git("-C", ".writing", "log", "-1", "--format=%s|%b", cwd=local_clone).stdout
    assert logged.strip() == "改写引言第二段|原因：gap 说法太泛。"
    # The next round lists only what changed since.
    write(local_clone, ".writing/zh/3_method.md", "方法中文稿")
    second = drafts.append_log(local_clone, "方法初稿", now=NOW)
    assert "- 写作档案改动：zh/3_method.md\n" in second
    # Unwritten sections are omitted; the intro TeX is still unsynced, so it stays listed.
    assert "- 章节状态：1_intro 已对齐；3_method 未对齐\n" in second
    assert "0_abstract" not in second


def test_log_rejects_empty_message(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    with pytest.raises(drafts.DraftError):
        drafts.append_log(local_clone, "  \n")


# ------------------------------------------------- sync across two machines

def _records_remote(local_clone, tmp_path):
    remote = tmp_path / "records.git"
    _git("init", "-q", "--bare", str(remote))
    _git("-C", ".writing", "remote", "add", "origin", str(remote), cwd=local_clone)
    drafts.commit_records(local_clone, "init")
    _git("-C", ".writing", "push", "-q", "-u", "origin", "HEAD", cwd=local_clone)
    return remote


def test_concurrent_log_entries_merge_without_conflict(local_clone, second_clone, tmp_path, monkeypatch):
    make_paper(local_clone)
    _git("pull", "-q", "--rebase", cwd=second_clone)
    writing.initialize(local_clone)
    remote = _records_remote(local_clone, tmp_path)
    _git("clone", "-q", str(remote), str(second_clone / ".writing"))
    # Machine A writes a round and syncs.
    write(local_clone, ".writing/zh/1_intro.md", "A 的引言")
    drafts.append_log(local_clone, "A：引言", now=NOW)
    register(monkeypatch, local_clone)
    first = CliRunner().invoke(main, ["sync", "paper", "--message", "A：引言"])
    assert first.exit_code == 0, first.output
    # Machine B, not yet pulled, writes its own round.
    write(second_clone, ".writing/zh/3_method.md", "B 的方法")
    drafts.append_log(second_clone, "B：方法", now=NOW)
    register(monkeypatch, second_clone)
    second = CliRunner().invoke(main, ["sync", "paper", "--message", "B：方法"])
    assert second.exit_code == 0, second.output
    assert "已提交并推送" in second.output
    log = (second_clone / ".writing/log.md").read_text()
    assert "A：引言" in log and "B：方法" in log and "<<<<<<<" not in log
    assert (second_clone / ".writing/zh/1_intro.md").read_text() == "A 的引言"
    subjects = _git("log", "--format=%s", "HEAD", cwd=remote).stdout.splitlines()
    assert subjects[:2] == ["B：方法", "A：引言"]


def test_sync_uses_one_message_for_paper_and_records(local_clone, bare_remote, monkeypatch):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "引言")
    write(local_clone, "sections/1_intro.tex", "New intro.\n")
    register(monkeypatch, local_clone)
    result = CliRunner().invoke(main, ["sync", "paper", "--message", "改写引言"])
    assert result.exit_code == 0, result.output
    assert "写作档案：已本地提交（未配置远端）" in result.output
    assert _git("log", "-1", "--format=%s", "main", cwd=bare_remote).stdout.strip() == "改写引言"
    assert _git("-C", ".writing", "log", "-1", "--format=%s",
                cwd=local_clone).stdout.strip() == "改写引言"
    files = _git("ls-tree", "-r", "--name-only", "main", cwd=bare_remote).stdout
    assert ".writing" not in files


def test_sync_no_commit_leaves_records_alone(local_clone, monkeypatch):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "引言")
    register(monkeypatch, local_clone)
    result = CliRunner().invoke(main, ["sync", "paper", "--no-commit"])
    assert result.exit_code == 0, result.output
    assert "未处理" in result.output
    pending = _git("-C", ".writing", "status", "--porcelain", cwd=local_clone).stdout
    assert "zh/1_intro.md" in pending


def test_records_conflict_stops_before_paper_sync(local_clone, second_clone, bare_remote,
                                                   tmp_path, monkeypatch):
    make_paper(local_clone)
    writing.initialize(local_clone)
    remote = _records_remote(local_clone, tmp_path)
    other = tmp_path / "other-records"
    _git("clone", "-q", str(remote), str(other))
    write(other, "zh/1_intro.md", "另一台机器的版本")
    _git("-C", str(other), "add", "-A")
    _git("-C", str(other), "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-qm", "o")
    _git("-C", str(other), "push", "-q")
    write(local_clone, ".writing/zh/1_intro.md", "本机的版本")
    write(local_clone, "sections/1_intro.tex", "Local tex change.\n")
    before = _git("rev-parse", "main", cwd=bare_remote).stdout
    register(monkeypatch, local_clone)
    result = CliRunner().invoke(main, ["sync", "paper", "--message", "m"])
    assert result.exit_code == 1, result.output
    assert ".writing/zh/1_intro.md" in result.output
    assert "论文仓库本次未同步" in result.output
    assert _git("rev-parse", "main", cwd=bare_remote).stdout == before
    assert gitops.rebase_in_progress(local_clone / ".writing")


def test_pull_refreshes_status_after_overleaf_edit(local_clone, second_clone, monkeypatch):
    make_paper(local_clone)
    writing.initialize(local_clone)
    write(local_clone, ".writing/zh/1_intro.md", "引言")
    drafts.align(local_clone, ["1_intro"])
    # A coauthor edits the TeX on Overleaf.
    _git("pull", "-q", "--rebase", cwd=second_clone)
    write(second_clone, "sections/1_intro.tex", "Coauthor rewrote the gap.\n")
    _git("commit", "-qam", "Update on Overleaf.", cwd=second_clone)
    _git("push", "-q", cwd=second_clone)
    register(monkeypatch, local_clone)
    result = CliRunner().invoke(main, ["pull", "paper"])
    assert result.exit_code == 0, result.output
    assert "| 1_intro | 英文领先 |" in (local_clone / ".writing/status.md").read_text()


# ---------------------------------------------------------------------- CLI

def test_cli_status_align_log_with_local_path(local_clone):
    make_paper(local_clone)
    runner = CliRunner()
    path = ["--path", str(local_clone)]
    uninitialized = runner.invoke(main, ["writing", "status", *path])
    assert uninitialized.exit_code == 1 and "writing init" in uninitialized.output
    assert runner.invoke(main, ["writing", "init", *path]).exit_code == 0
    write(local_clone, ".writing/zh/1_intro.md", "引言")
    aligned = runner.invoke(main, ["writing", "align", *path, "-s", "1_intro"])
    assert aligned.exit_code == 0, aligned.output
    assert "zh/1_intro.md ↔ sections/1_intro.tex" in aligned.output
    shown = runner.invoke(main, ["writing", "status", *path])
    assert shown.exit_code == 0, shown.output
    assert "1_intro" in shown.output and "已对齐" in shown.output and "未起草" in shown.output
    logged = runner.invoke(main, ["writing", "log", *path, "-m", "引言初稿"])
    assert logged.exit_code == 0, logged.output
    assert "已提交 .writing" in logged.output
    assert _git("-C", ".writing", "log", "-1", "--format=%s",
                cwd=local_clone).stdout.strip() == "引言初稿"


def test_cli_align_needs_sections_or_all(local_clone):
    make_paper(local_clone)
    writing.initialize(local_clone)
    runner = CliRunner()
    assert runner.invoke(main, ["writing", "align", "--path", str(local_clone)]).exit_code == 2
    both = runner.invoke(main, ["writing", "align", "--path", str(local_clone), "-s", "1_intro", "--all"])
    assert both.exit_code == 2


def test_cli_log_reports_commit_failure_without_losing_entry(local_clone, monkeypatch):
    make_paper(local_clone)
    writing.initialize(local_clone)

    def boom(repo, message):
        raise gitops.GitError("Author identity unknown")

    monkeypatch.setattr(drafts, "commit_records", boom)
    result = CliRunner().invoke(main, ["writing", "log", "--path", str(local_clone), "-m", "一轮"])
    assert result.exit_code == 1
    assert "日志已写入" in result.output and "不要重复运行" in result.output
    assert "一轮" in (local_clone / ".writing/log.md").read_text()


def test_init_without_any_identity_leaves_skeleton_uncommitted(tmp_path, monkeypatch):
    """No identity anywhere (this WSL's situation): init still succeeds."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL"]:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "empty-gitconfig").write_text("[user]\n\tuseConfigOnly = true\n")
    paper = tmp_path / "paper"
    _git("init", "-q", str(paper))
    write(paper, "sections/1_intro.tex", "x")
    result = writing.initialize(paper)
    assert result["records_repo"] == "created" and result["records_committed"] is False
    assert (paper / ".writing/zh/1_intro.md").exists()


def test_commit_message_splits_subject_and_body():
    assert drafts.commit_message("标题") == "标题"
    assert drafts.commit_message(" 标题 \n原因\n下一步 ") == "标题\n\n原因\n下一步"
