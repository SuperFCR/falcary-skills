"""Chinese drafts, their alignment with TeX sections, and the round log.

Layout under the paper repo's local-only ``.writing/`` directory:

- ``zh/<stem>.md``: the Chinese draft of ``sections/<stem>.tex`` (paired by file
  name). Drafts without a same-named TeX file are free-form documents.
- ``align.json``: per-section content hashes recorded when the draft and the TeX
  were last confirmed to say the same thing.
- ``log.md``: one entry per writing round (intent, reason, touched files).
- ``status.md``: generated per-section view; machine-local, not committed.

``.writing/`` is its own Git repository, separate from the Overleaf repository.
It may track a private remote so drafts follow the user across machines.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import gitops

STATE_DIR = ".writing"
DRAFT_DIR = "zh"
ALIGN_FILE = "align.json"
LOG_FILE = "log.md"
STATUS_FILE = "status.md"

LOG_HEADER = (
    "# 写作日志\n\n"
    "每轮一条，只追加：本轮意图与原因、动了哪些文件、相关章节的对齐状态。"
    "具体改动看 git diff。\n"
)
# status.md is regenerated from shared data on every machine; committing it
# would only produce merge conflicts.
RECORDS_GITIGNORE = "status.md\n"
# Two machines appending log entries concurrently: keep both sides' lines.
RECORDS_GITATTRIBUTES = "log.md merge=union\n"

# Records that describe the round itself, not material written in it.
_BOOKKEEPING = {LOG_FILE, STATUS_FILE, ALIGN_FILE, ".gitignore", ".gitattributes"}

_SECTION_DIRS = {"sections", "sec"}
_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[.*?\])?\s*\{", re.S)

STATES = {
    "未起草": "中文稿为空或不存在。",
    "未对齐": "中文稿有内容，但还没有记录过与 TeX 的对齐。",
    "已对齐": "上次对齐后两边都没改。",
    "中文领先": "中文稿在上次对齐后改过，英文还没跟上。",
    "英文领先": "TeX 在上次对齐后改过（含 Overleaf 上的修改），中文稿可能过期。",
    "两边都改": "上次对齐后两边都改过，需要先核对。",
}


class DraftError(ValueError):
    """A drafts operation cannot proceed; the message is user-facing."""


@dataclass
class Section:
    key: str
    tex: str                  # repo-relative TeX path
    draft: str                # .writing-relative draft path, e.g. zh/1_intro.md
    referenced: bool          # reached from a main file's \input/\include chain
    state: str = ""
    aligned_at: str | None = None


@dataclass
class StatusReport:
    sections: list[Section]
    other_drafts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- discovery

def state_dir(repo: str | Path) -> Path:
    return Path(repo).resolve() / STATE_DIR


def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _tex_files(repo: Path) -> list[str]:
    files = []
    for path in sorted(repo.rglob("*.tex")):
        rel = path.relative_to(repo)
        if any(part.startswith(".") for part in rel.parts) or path.is_symlink():
            continue
        files.append(rel.as_posix())
    return files


def _main_files(repo: Path, tex_files: list[str]) -> list[str]:
    mains = []
    for name in tex_files:
        text = _strip_comments(_read(repo / name))
        if _DOCUMENTCLASS_RE.search(text) and "\\begin{document}" in text:
            mains.append(name)
    return mains


def _referenced(repo: Path, tex_files: list[str], mains: list[str]) -> set[str]:
    """TeX files reachable from any main file.

    TeX resolves \\input paths against the directory it runs in (the main
    file's folder), not against the including file, so every file reached from
    a main resolves against that main's folder.
    """
    known = set(tex_files)
    seen: set[str] = set()
    stack = [(main, posixpath.dirname(main)) for main in mains]
    while stack:
        name, root = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for target in _INPUT_RE.findall(_strip_comments(_read(repo / name))):
            path = posixpath.normpath(posixpath.join(root, target.strip()))
            for candidate in (path, path + ".tex"):
                if candidate in known:
                    stack.append((candidate, root))
                    break
    return seen - set(mains)


def _section_tex(repo: Path, tex_files: list[str], mains: list[str],
                 referenced: set[str]) -> list[str]:
    """TeX files that get a Chinese draft.

    Files directly inside a ``sections/`` or ``sec/`` folder, including ones no
    main file inputs (an appendix compiled separately). Without such a folder,
    the files the main files input.
    """
    in_dirs = [t for t in tex_files if PurePosixPath(t).parent.name in _SECTION_DIRS]
    if in_dirs:
        return in_dirs
    return [t for t in tex_files if t in referenced and t not in mains]


def discover(repo: str | Path) -> list[Section]:
    """Pair each section TeX file with its draft path (by file name)."""
    root = Path(repo).resolve()
    tex_files = _tex_files(root)
    mains = _main_files(root, tex_files)
    referenced = _referenced(root, tex_files, mains)
    sections = _section_tex(root, tex_files, mains, referenced)
    stems: dict[str, int] = {}
    for tex in sections:
        stem = PurePosixPath(tex).stem
        stems[stem] = stems.get(stem, 0) + 1
    result = []
    for tex in sections:
        stem = PurePosixPath(tex).stem
        # Same file name in two section folders: mirror the full path instead.
        key = stem if stems[stem] == 1 else tex[: -len(".tex")]
        result.append(Section(key=key, tex=tex, draft=f"{DRAFT_DIR}/{key}.md",
                              referenced=tex in referenced))
    return result


def initialized(repo: str | Path) -> bool:
    return (state_dir(repo) / DRAFT_DIR).is_dir()


def _require_initialized(repo: str | Path) -> None:
    if not initialized(repo):
        raise DraftError(
            f"{state_dir(repo) / DRAFT_DIR} 不存在；先运行 overleaf-ctl writing init。")


# ---------------------------------------------------------------- alignment

def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:16]


def _has_text(path: Path) -> bool:
    return path.is_file() and bool(_read(path).strip())


def load_alignment(repo: str | Path) -> dict[str, dict]:
    path = state_dir(repo) / ALIGN_FILE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        sections = value.get("sections", {})
        if not isinstance(sections, dict):
            raise ValueError("sections must be an object")
        return sections
    except (ValueError, AttributeError) as exc:
        raise DraftError(f"{path} 无法解析：{exc}") from exc


def _save_alignment(repo: str | Path, sections: dict[str, dict]) -> None:
    path = state_dir(repo) / ALIGN_FILE
    payload = {"version": 1, "sections": dict(sorted(sections.items()))}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _state(repo: Path, section: Section, baseline: dict | None) -> str:
    draft = state_dir(repo) / section.draft
    if not _has_text(draft):
        return "未起草"
    if (not baseline or baseline.get("tex") != section.tex
            or baseline.get("draft") != section.draft):
        return "未对齐"
    draft_changed = _digest(draft) != baseline.get("draft_sha")
    tex_changed = _digest(repo / section.tex) != baseline.get("tex_sha")
    if draft_changed and tex_changed:
        return "两边都改"
    if draft_changed:
        return "中文领先"
    if tex_changed:
        return "英文领先"
    return "已对齐"


def status(repo: str | Path) -> StatusReport:
    root = Path(repo).resolve()
    _require_initialized(root)
    alignment = load_alignment(root)
    sections = discover(root)
    for section in sections:
        baseline = alignment.get(section.key)
        section.state = _state(root, section, baseline)
        section.aligned_at = (baseline or {}).get("aligned_at")
    paired = {s.draft for s in sections}
    drafts_root = state_dir(root) / DRAFT_DIR
    others = sorted(
        p.relative_to(state_dir(root)).as_posix()
        for p in drafts_root.rglob("*.md")
        if p.relative_to(state_dir(root)).as_posix() not in paired
    )
    return StatusReport(sections=sections, other_drafts=others)


def _resolve(sections: list[Section], names: list[str]) -> list[Section]:
    by_name: dict[str, Section] = {}
    for section in sections:
        for alias in (section.key, section.tex, section.draft,
                      PurePosixPath(section.tex).stem):
            by_name.setdefault(alias, section)
    chosen, unknown = [], []
    for name in names:
        section = by_name.get(name.strip().rstrip("/"))
        if section is None:
            unknown.append(name)
        elif section not in chosen:
            chosen.append(section)
    if unknown:
        raise DraftError(
            f"未知章节：{', '.join(unknown)}；可选：{', '.join(s.key for s in sections)}")
    return chosen


def align(repo: str | Path, names: list[str] | None = None, all_sections: bool = False,
          now: datetime | None = None) -> list[Section]:
    """Record that each named section's draft and TeX now say the same thing."""
    root = Path(repo).resolve()
    _require_initialized(root)
    sections = discover(root)
    if all_sections:
        chosen = [s for s in sections if _has_text(state_dir(root) / s.draft)]
    else:
        chosen = _resolve(sections, names or [])
    if not chosen:
        raise DraftError("没有可对齐的章节（中文稿都为空，或未指定章节）。")
    empty = [s.key for s in chosen if not _has_text(state_dir(root) / s.draft)]
    if empty:
        raise DraftError(f"中文稿为空，不能对齐：{', '.join(empty)}")
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    alignment = load_alignment(root)
    for section in chosen:
        alignment[section.key] = {
            "draft": section.draft,
            "tex": section.tex,
            "draft_sha": _digest(state_dir(root) / section.draft),
            "tex_sha": _digest(root / section.tex),
            "aligned_at": stamp,
        }
    _save_alignment(root, alignment)
    write_status(root)
    return chosen


# ------------------------------------------------------------------- status

def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def render_status(report: StatusReport) -> str:
    lines = [
        "# 章节状态",
        "",
        "由 `overleaf-ctl writing status` 生成，不进 `.writing` 的 git；以命令的实时输出为准。",
        "",
        "| 章节 | 状态 | 中文稿 | TeX | 被 main 引用 | 上次对齐 |",
        "|---|---|---|---|---|---|",
    ]
    for s in report.sections:
        lines.append(f"| {s.key} | {s.state} | {s.draft} | {s.tex} | "
                     f"{'是' if s.referenced else '否'} | {_short_time(s.aligned_at)} |")
    if not report.sections:
        lines.append("| — | 没有找到章节 TeX 文件 | | | | |")
    if report.other_drafts:
        lines += ["", "无同名 TeX 的中文稿：" + "、".join(report.other_drafts)]
    lines += ["", "状态说明："]
    lines += [f"- {name}：{meaning}" for name, meaning in STATES.items()]
    return "\n".join(lines) + "\n"


def write_status(repo: str | Path, report: StatusReport | None = None) -> Path:
    report = report or status(repo)
    path = state_dir(repo) / STATUS_FILE
    path.write_text(render_status(report), encoding="utf-8")
    return path


def refresh_status(repo: str | Path) -> None:
    """Regenerate status.md when drafts are set up; otherwise do nothing."""
    if initialized(repo):
        write_status(repo)


# ------------------------------------------------------------ records repo

def records_repo(repo: str | Path) -> Path | None:
    """``.writing`` as its own Git repository, or None.

    Both checks matter: without ``.writing/.git``, ``git -C .writing`` would
    act on the enclosing paper repository.
    """
    state = state_dir(repo)
    if not (state / ".git").exists():
        return None
    top = gitops._run(state, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != state:
        return None
    return state


def init_records_repo(repo: str | Path) -> bool:
    """Make ``.writing`` its own Git repository. Return True if newly created."""
    state = state_dir(repo)
    if records_repo(repo) is not None:
        return False
    if (state / ".git").exists():
        raise DraftError(f"{state / '.git'} 已存在但不是有效的 Git 仓库，请先检查。")
    gitops._run(state, ["init", "-q"], check=True)
    if records_repo(repo) is None:
        raise DraftError(f"{state} 初始化为 Git 仓库失败。")
    return True


def _split(output: str) -> set[str]:
    return {p for p in output.split("\0") if p}


def _pending_record_changes(state: Path) -> list[str]:
    """Files in .writing changed since its last commit (all files before the first)."""
    paths = _split(gitops._run(state, ["ls-files", "--others", "--exclude-standard", "-z"],
                               check=True).stdout)
    if gitops._run(state, ["rev-parse", "--verify", "-q", "HEAD"]).returncode == 0:
        paths |= _split(gitops._run(state, ["diff", "--name-only", "-z", "HEAD"],
                                    check=True).stdout)
    else:
        paths |= _split(gitops._run(state, ["ls-files", "-z"], check=True).stdout)
    return sorted(p for p in paths if p not in _BOOKKEEPING)


def _unsynced_paper_changes(repo: Path) -> list[str]:
    """Paper files changed locally and not yet on the remote branch."""
    if not gitops.is_git_repo(repo):
        return []
    base = gitops._run(repo, ["merge-base", "HEAD", "@{upstream}"])
    if base.returncode == 0:
        ref = base.stdout.strip()
    elif gitops._run(repo, ["rev-parse", "--verify", "-q", "HEAD"]).returncode == 0:
        ref = "HEAD"
    else:
        ref = None
    paths = _split(gitops._run(repo, ["ls-files", "--others", "--exclude-standard", "-z"],
                               check=True).stdout)
    if ref:
        paths |= _split(gitops._run(repo, ["diff", "--name-only", "-z", ref], check=True).stdout)
    return sorted(paths)


def _ensure_identity(repo: str | Path, state: Path) -> None:
    """Give ``.writing`` the paper repository's identity when it has none.

    A paper clone often carries its committer identity in its own config;
    without this, commits and rebases in ``.writing`` fail where the paper's
    succeed. Nothing is written when ``.writing`` already resolves an identity.
    """
    for key in ("user.name", "user.email"):
        if gitops._run(state, ["config", key]).stdout.strip():
            continue
        value = gitops._run(Path(repo).resolve(), ["config", key]).stdout.strip()
        if value:
            gitops._run(state, ["config", "--local", key, value], check=True)


def commit_message(message: str) -> str:
    """First line as the subject, the rest as the body after a blank line."""
    title, _, body = message.strip().partition("\n")
    body = body.strip()
    return f"{title.strip()}\n\n{body}" if body else title.strip()


def commit_records(repo: str | Path, message: str) -> bool:
    """Commit everything in ``.writing``. Return False when there was nothing to commit."""
    state = records_repo(repo)
    if state is None:
        return False
    _ensure_identity(repo, state)
    gitops._run(state, ["add", "-A"], check=True)
    if gitops._run(state, ["diff", "--cached", "--quiet"]).returncode == 0:
        return False
    gitops._run(state, ["commit", "-q", "-m", commit_message(message)], check=True)
    return True


class RecordsConflict(gitops.GitError):
    def __init__(self, files: list[str]):
        listing = "\n".join(f"  - .writing/{f}" for f in files) or "  （见 git -C .writing status）"
        super().__init__(
            "写作档案 .writing 的 pull --rebase 冲突，已保留现场：\n" + listing
            + "\n解决后执行 git -C .writing add <文件>，再重跑同一命令。")


def _has_upstream(state: Path) -> bool:
    return gitops._run(state, ["rev-parse", "--verify", "-q", "@{upstream}"]).returncode == 0


def sync_records(repo: str | Path, message: str) -> str:
    """Commit ``.writing``; with an upstream, rebase onto it and push. Return a summary."""
    state = records_repo(repo)
    if state is None:
        return ""
    _ensure_identity(repo, state)
    if gitops.rebase_in_progress(state):
        unmerged = gitops.unmerged_files(state)
        if unmerged:
            raise RecordsConflict(unmerged)
        if gitops.rebase_continue(state).conflict:
            raise RecordsConflict(gitops.unmerged_files(state))
    committed = commit_records(repo, message)
    if not _has_upstream(state):
        return "写作档案：已本地提交（未配置远端）" if committed else "写作档案：无改动（未配置远端）"
    pulled = gitops.pull_rebase(state)
    if pulled.conflict:
        raise RecordsConflict(gitops.unmerged_files(state))
    if gitops.get_status(state).ahead:
        gitops._run(state, ["push", "-q"], check=True)
        return "写作档案：已提交并推送" if committed else "写作档案：已推送"
    return "写作档案：已与远端一致"


def pull_records(repo: str | Path) -> str:
    """Bring ``.writing`` up to date with its upstream, if it has one."""
    state = records_repo(repo)
    if state is None or not _has_upstream(state):
        return ""
    if gitops.rebase_in_progress(state):
        raise RecordsConflict(gitops.unmerged_files(state))
    if gitops.get_status(state).dirty:
        return "写作档案有未提交的改动，本次未拉取；sync 时会先提交再拉取。"
    _ensure_identity(repo, state)
    pulled = gitops.pull_rebase(state)
    if pulled.conflict:
        raise RecordsConflict(gitops.unmerged_files(state))
    return "写作档案：" + (pulled.output or "已是最新")


# ---------------------------------------------------------------------- log

def append_log(repo: str | Path, message: str, now: datetime | None = None) -> str:
    """Append one round entry to log.md and return it.

    The first line of ``message`` is the title; the rest is the body. Touched
    files and the touched sections' alignment states are added automatically.
    """
    root = Path(repo).resolve()
    _require_initialized(root)
    message = message.strip()
    if not message:
        raise DraftError("日志说明为空。")
    title, _, body = message.partition("\n")
    state = state_dir(root)
    records = records_repo(root)
    drafts_changed = _pending_record_changes(records) if records else []
    paper_changed = _unsynced_paper_changes(root)
    report = status(root)
    touched = [s for s in report.sections
               if (s.draft in drafts_changed and s.state != "未起草") or s.tex in paper_changed]
    stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    lines = [f"## {stamp} · {title.strip()}", ""]
    if body.strip():
        lines += [body.strip(), ""]
    if records:
        lines.append("- 写作档案改动：" + ("、".join(drafts_changed) or "无"))
    else:
        lines.append("- 写作档案改动：（.writing 不是 Git 仓库，无法列出）")
    lines.append("- 论文未同步改动：" + ("、".join(paper_changed) or "无"))
    if touched:
        lines.append("- 章节状态：" + "；".join(f"{s.key} {s.state}" for s in touched))
    entry = "\n".join(lines) + "\n"
    log = state / LOG_FILE
    existing = log.read_text(encoding="utf-8") if log.exists() else LOG_HEADER
    if not existing.endswith("\n"):
        existing += "\n"
    log.write_text(existing + "\n" + entry, encoding="utf-8")
    write_status(root, report)
    return entry
