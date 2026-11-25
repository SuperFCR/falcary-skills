#!/usr/bin/env python3
"""Zero-dependency Git branch, commit, stage-hygiene and SemVer helper for proj-standards."""

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

TRUNK_BRANCHES = ("main", "master", "develop")
PURPOSE_BRANCH_TYPES = ("feature", "feat", "bugfix", "fix", "hotfix", "release", "chore")
AI_BRANCH_TYPES = ("ai", "copilot", "cursor", "claude", "codex")
DEFAULT_BRANCH_TYPES = PURPOSE_BRANCH_TYPES + AI_BRANCH_TYPES
DESC_RE_STR = r"[a-z0-9]+(?:\.[a-z0-9]+)*(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)*"
BRANCH_TYPE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

DEFAULT_COMMIT_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

COMMIT_HEADER_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[^()\r\n]*)\))?(?P<bang>!)?:\s*(?P<desc>.*)$"
)
COMMIT_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
GIT_AUTO_COMMIT_RE = re.compile(
    r"^(?:Merge\b|Revert\s+\"|fixup!\s|squash!\s|amend!\s)"
)
SCISSORS_LINE = "# ------------------------ >8 ------------------------"

PRIVATE_EXACT_NAMES = {
    "claude.local.md",
    "secrets.env",
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")
ENV_ALLOWED_TAILS = (".example", ".sample", ".template", ".schema", ".dist")
WEIGHT_EXTENSIONS = (".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".gguf", ".h5")

SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("GitHub classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API secret key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Tailscale auth key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{16,}\b")),
)
ALLOW_SECRET_MARKER = re.compile(r"(?:repoguard|pmem):allow-secret")

HOOK_MARKER = "# managed-by: proj-standards repoguard"
VALID_HOOK_NAMES = ("pre-commit", "commit-msg", "pre-push")
SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _run_git(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=check,
    )


def _git_root(cwd: Optional[Path] = None) -> Optional[Path]:
    res = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if res.returncode != 0:
        return None
    out = res.stdout.strip()
    return Path(out) if out else None


def _load_config(cwd: Optional[Path] = None) -> Tuple[Dict[str, object], Optional[str]]:
    base = _git_root(cwd) or cwd or Path.cwd()
    cfg_path = base / ".proj-standards.json"
    if not cfg_path.is_file():
        return {}, None
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"Failed to read {cfg_path.name}: {exc}"
    if not isinstance(raw, dict):
        return {}, f"Invalid {cfg_path.name}: expected a JSON object"
    return raw, None


def _normalize_extra_types(values: Iterable[str]) -> Tuple[str, ...]:
    seen = []
    for item in values:
        cleaned = str(item).strip().rstrip("/")
        if cleaned and BRANCH_TYPE_RE.match(cleaned) and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def build_branch_regex(extra_types: Sequence[str] = ()) -> re.Pattern:
    types: List[str] = list(DEFAULT_BRANCH_TYPES)
    for item in _normalize_extra_types(extra_types):
        if item not in types:
            types.append(item)
    type_group = "|".join(re.escape(t) for t in types)
    trunk_group = "|".join(re.escape(t) for t in TRUNK_BRANCHES)
    return re.compile(rf"^(?:{trunk_group}|(?:{type_group})/{DESC_RE_STR})$")


def validate_branch(name: str, extra_types: Sequence[str] = ()) -> Tuple[bool, str]:
    """Validate branch name against Conventional Branch 1.1.0."""
    if not name or not name.strip():
        return False, "Branch name must not be empty"
    if any(c.isspace() for c in name):
        return False, "Branch name must not contain whitespace"
    if any("A" <= c <= "Z" for c in name):
        return False, "Branch name must be lowercase only (a-z)"

    if name in TRUNK_BRANCHES:
        return True, "Valid trunk branch"

    extra_clean = _normalize_extra_types(extra_types)
    allowed_types = list(DEFAULT_BRANCH_TYPES) + [t for t in extra_clean if t not in DEFAULT_BRANCH_TYPES]

    if "/" not in name:
        return False, "Prefixed branch must match <type>/<description> (or trunk: main, master, develop)"

    btype, desc = name.split("/", 1)
    if btype not in allowed_types:
        return False, f"Unknown branch type '{btype}'; expected one of: {', '.join(allowed_types)}"
    if not desc:
        return False, "Description after '<type>/' must not be empty"
    if "/" in desc:
        return False, "Description must not contain '/'"
    if "_" in desc:
        return False, "Description must not contain underscores ('_'); use hyphens ('-')"
    if not re.fullmatch(r"[a-z0-9.-]+", desc):
        return False, "Description may only contain lowercase letters (a-z), digits (0-9), '-' and '.'"
    if desc[0] in "-." or desc[-1] in "-.":
        return False, "Description must not start or end with '-' or '.'"
    if "--" in desc or ".." in desc or "-." in desc or ".-" in desc:
        return False, "Description must not contain consecutive or adjacent '-' / '.' separators"

    pattern = build_branch_regex(extra_clean)
    if not pattern.fullmatch(name):
        return False, "Branch name does not match Conventional Branch 1.1.0 grammar"
    return True, "Valid Conventional Branch 1.1.0 name"


def _sanitize_desc(raw: str) -> str:
    text = raw.strip().lower()
    text = re.sub(r"[_\s/\\]+", "-", text)
    text = re.sub(r"[^a-z0-9.-]+", "-", text)
    text = re.sub(r"\.{2,}", ".", text)
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"(?:\.-|-\.)+", "-", text)
        text = re.sub(r"-{2,}", "-", text)
        text = text.strip(".-")
    return text or "work"


def suggest_branch_name(
    raw: str,
    default_type: str = "feat",
    extra_types: Sequence[str] = (),
) -> str:
    """Return a Conventional Branch 1.1.0 compliant suggestion for any input."""
    extra_clean = _normalize_extra_types(extra_types)
    ok, _ = validate_branch(raw, extra_clean)
    if ok:
        return raw

    allowed_types = list(DEFAULT_BRANCH_TYPES) + [t for t in extra_clean if t not in DEFAULT_BRANCH_TYPES]
    fallback = default_type if default_type in allowed_types else "feat"
    stripped = raw.strip()
    lowered = stripped.lower()

    if lowered in TRUNK_BRANCHES:
        return lowered

    if "/" in stripped:
        head, tail = stripped.split("/", 1)
        head_low = head.strip().lower()
        chosen_type = head_low if head_low in allowed_types else fallback
        raw_desc = tail if head_low in allowed_types else f"{head}-{tail}"
    else:
        chosen_type = fallback
        raw_desc = stripped

    candidate = f"{chosen_type}/{_sanitize_desc(raw_desc)}"
    ok, _ = validate_branch(candidate, extra_clean)
    if not ok:
        return f"{fallback}/work"
    return candidate


def _clean_commit_text(raw: str, strip_comments: bool = True) -> str:
    lines = raw.splitlines()
    if strip_comments:
        kept: List[str] = []
        for line in lines:
            if line.strip() == SCISSORS_LINE:
                break
            if line.startswith("#"):
                continue
            kept.append(line)
        lines = kept
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def validate_commit_message(
    raw: str,
    extra_types: Sequence[str] = (),
    allow_git_auto: bool = True,
    strict: bool = False,
) -> Tuple[bool, str, List[str]]:
    """Validate commit message against Conventional Commits 1.0.0."""
    cleaned = _clean_commit_text(raw, strip_comments=True)
    if not cleaned:
        return False, "Commit message is empty", []

    header = cleaned.splitlines()[0].rstrip()
    warnings: List[str] = []

    if allow_git_auto and GIT_AUTO_COMMIT_RE.match(header):
        return True, "Valid git auto-generated commit header", warnings

    match = COMMIT_HEADER_RE.match(header)
    if not match:
        return False, "Header must match <type>[optional (scope)][!]: <description>", warnings

    ctype = match.group("type")
    scope = match.group("scope")
    desc = match.group("desc")

    allowed = list(DEFAULT_COMMIT_TYPES) + [
        t.strip() for t in extra_types if t.strip() and t.strip() not in DEFAULT_COMMIT_TYPES
    ]
    if ctype not in allowed:
        return False, f"Unknown commit type '{ctype}'; expected one of: {', '.join(allowed)}", warnings

    if scope is not None:
        if not scope or not COMMIT_SCOPE_RE.fullmatch(scope):
            return False, f"Invalid commit scope '({scope})'; use lowercase a-z0-9._/-", warnings

    colon_pos = header.find(":")
    after_colon = header[colon_pos + 1 :]
    if not after_colon.startswith(" ") or not desc.strip():
        return False, "Commit header must include a single space and non-empty description after ':'", warnings

    if len(header) > 72:
        msg = f"Header exceeds 72 characters ({len(header)} chars)"
        if strict:
            return False, msg, warnings
        warnings.append(msg)

    return True, "Valid Conventional Commit 1.0.0 header", warnings


def _is_private_path(rel_path: str, extra_patterns: Sequence[str]) -> Optional[str]:
    norm = rel_path.replace("\\", "/")
    base = os.path.basename(norm)
    base_low = base.lower()

    if base_low in PRIVATE_EXACT_NAMES:
        return f"private/local file '{norm}' should stay in .git/info/exclude, not staged"
    if base_low.startswith(".env.") and not any(base_low.endswith(suf) for suf in ENV_ALLOWED_TAILS):
        return f"environment file '{norm}' may contain secrets and should not be staged"
    if any(base_low.endswith(suf) for suf in PRIVATE_SUFFIXES):
        return f"key/certificate file '{norm}' must not be staged"
    for pat in extra_patterns:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(base, pat):
            return f"excluded pattern '{pat}' matched staged file '{norm}'"
    return None


def _parse_staged_name_status(raw: bytes) -> List[Tuple[str, str]]:
    """Return [(status_code, path)] for non-deleted staged paths using NUL-delimited output."""
    tokens = [tok.decode("utf-8", errors="replace") for tok in raw.split(b"\x00") if tok]
    entries: List[Tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if status.startswith(("R", "C")):
            if i + 1 < len(tokens):
                new_path = tokens[i + 1]
                entries.append((status, new_path))
                i += 2
            elif i < len(tokens):
                entries.append((status, tokens[i]))
                i += 1
        else:
            if i < len(tokens):
                path = tokens[i]
                i += 1
                if not status.startswith("D"):
                    entries.append((status, path))
    return entries


def check_staged(
    cwd: Optional[Path] = None,
    max_mb: Optional[float] = None,
) -> Tuple[bool, Dict[str, object]]:
    """Inspect staged files for private files, oversized blobs, and secret literals."""
    root = _git_root(cwd)
    if root is None:
        return False, {"error": "Not inside a git repository", "errors": ["Not inside a git repository"], "warnings": [], "staged_files": []}

    cfg, cfg_err = _load_config(root)
    if cfg_err:
        return False, {"error": cfg_err, "errors": [cfg_err], "warnings": [], "staged_files": []}

    effective_max_mb = max_mb if max_mb is not None else float(cfg.get("max_staged_mb", 5.0))
    extra_priv = [str(x) for x in cfg.get("private_files_extra", [])] if isinstance(cfg.get("private_files_extra"), list) else []

    status_res = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-z"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if status_res.returncode != 0:
        err = status_res.stderr.decode("utf-8", errors="replace").strip()
        return False, {"error": err, "errors": [err], "warnings": [], "staged_files": []}

    staged_entries = _parse_staged_name_status(status_res.stdout)
    staged_paths = [path for _, path in staged_entries]

    errors: List[str] = []
    warnings: List[str] = []

    for status, rel_path in staged_entries:
        priv_reason = _is_private_path(rel_path, extra_priv)
        if priv_reason:
            errors.append(priv_reason)

        if status.startswith(("A", "M", "C", "R", "T")):
            size_res = _run_git(["cat-file", "-s", f":0:{rel_path}"], cwd=root)
            if size_res.returncode == 0 and size_res.stdout.strip().isdigit():
                size_bytes = int(size_res.stdout.strip())
                size_mb = size_bytes / (1024.0 * 1024.0)
                is_weight = rel_path.lower().endswith(WEIGHT_EXTENSIONS)
                limit_mb = min(effective_max_mb, 1.0) if is_weight else effective_max_mb
                if size_mb > limit_mb:
                    kind = "weight/model artifact" if is_weight else "staged file"
                    errors.append(f"{kind} '{rel_path}' is {size_mb:.2f} MiB (limit {limit_mb:.2f} MiB)")

    diff_res = _run_git(["diff", "--cached", "--unified=0", "--no-color", "-M", "--no-ext-diff"], cwd=root)
    if diff_res.returncode == 0:
        cur_file = "<staged>"
        for line in diff_res.stdout.splitlines():
            if line.startswith("+++ b/"):
                cur_file = line[6:]
                continue
            if line.startswith("+++ "):
                continue
            if not line.startswith("+"):
                continue
            added = line[1:]
            if ALLOW_SECRET_MARKER.search(added):
                continue
            for label, pat in SECRET_PATTERNS:
                if pat.search(added):
                    errors.append(f"suspected {label} added in '{cur_file}'")

    top_dirs = sorted({p.split("/", 1)[0] if "/" in p else "." for p in staged_paths})
    return len(errors) == 0, {
        "staged_count": len(staged_paths),
        "staged_files": staged_paths,
        "top_dirs": top_dirs,
        "errors": errors,
        "warnings": warnings,
    }


def _latest_semver_tag(root: Path) -> Optional[str]:
    res = _run_git(["tag", "--sort=-v:refname"], cwd=root)
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        tag = line.strip()
        if SEMVER_TAG_RE.match(tag):
            return tag
    return None


def suggest_version(cwd: Optional[Path] = None, since_tag: Optional[str] = None) -> Tuple[bool, Dict[str, object]]:
    """Compute recommended next SemVer 2.0.0 tag and changelog summary from git history."""
    root = _git_root(cwd)
    if root is None:
        return False, {"error": "Not inside a git repository"}

    base_tag = since_tag if since_tag is not None else _latest_semver_tag(root)
    if base_tag:
        m = SEMVER_TAG_RE.match(base_tag)
        if not m:
            return False, {"error": f"Tag '{base_tag}' is not valid SemVer (vX.Y.Z)"}
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        rev_range = f"{base_tag}..HEAD"
    else:
        major, minor, patch = 0, 0, 0
        rev_range = "HEAD"

    log_res = _run_git(["log", "--format=%H%x1f%s%x1f%b%x1e", rev_range], cwd=root)
    if log_res.returncode != 0:
        return False, {"error": log_res.stderr.strip() or "Failed to read git log"}

    breaking: List[str] = []
    feats: List[str] = []
    fixes: List[str] = []
    others: List[str] = []

    for rec in log_res.stdout.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split("\x1f", 2)
        sha = parts[0][:7]
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""

        m = COMMIT_HEADER_RE.match(subject)
        is_breaking = bool(m and m.group("bang")) or bool(
            re.search(r"^BREAKING[ -]CHANGE:\s+", body, re.M)
        )
        ctype = m.group("type") if m else ""
        entry = f"- {subject} ({sha})"

        if is_breaking:
            breaking.append(entry)
        elif ctype == "feat":
            feats.append(entry)
        elif ctype == "fix":
            fixes.append(entry)
        else:
            others.append(entry)

    total = len(breaking) + len(feats) + len(fixes) + len(others)
    if total == 0:
        bump = "none"
        next_ver = f"v{major}.{minor}.{patch}"
    elif breaking:
        bump = "major"
        next_ver = f"v{major + 1}.0.0"
    elif feats:
        bump = "minor"
        next_ver = f"v{major}.{minor + 1}.0"
    else:
        bump = "patch"
        next_ver = f"v{major}.{minor}.{patch + 1}"

    return True, {
        "current_tag": base_tag or "v0.0.0",
        "next_version": next_ver,
        "bump": bump,
        "commits": total,
        "breaking": breaking,
        "feat": feats,
        "fix": fixes,
        "other": others,
    }


def _hook_script(hook_name: str, script_path: Path) -> str:
    quoted_script = shlex.quote(str(script_path.resolve()))
    if hook_name == "pre-commit":
        subcmd = "check-stage"
    elif hook_name == "commit-msg":
        subcmd = 'check-commit --file "$1"'
    elif hook_name == "pre-push":
        subcmd = "check-branch --allow-detached"
    else:
        raise ValueError(f"Unsupported hook: {hook_name}")

    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "set -e\n"
        f"SCRIPT={quoted_script}\n"
        "if command -v repoguard >/dev/null 2>&1; then\n"
        f"  exec repoguard {subcmd}\n"
        "fi\n"
        "exec python3 \"$SCRIPT\" "
        f"{subcmd}\n"
    )


def install_hooks(
    hooks: Sequence[str],
    force: bool = False,
    cwd: Optional[Path] = None,
) -> Tuple[bool, str, List[str]]:
    """Install repoguard hooks into the current repo/worktree hooks directory."""
    root = _git_root(cwd)
    if root is None:
        return False, "Not inside a git repository", []

    hook_dir_res = _run_git(["rev-parse", "--git-path", "hooks"], cwd=root)
    if hook_dir_res.returncode != 0:
        return False, hook_dir_res.stderr.strip() or "Failed to locate git hooks directory", []
    raw_dir = Path(hook_dir_res.stdout.strip())
    hook_dir = raw_dir if raw_dir.is_absolute() else (root / raw_dir)

    invalid = [h for h in hooks if h not in VALID_HOOK_NAMES]
    if invalid:
        return False, f"Unknown hook(s): {', '.join(invalid)}", []

    for h in hooks:
        target = hook_dir / h
        if target.exists() and not force:
            text = target.read_text(encoding="utf-8", errors="replace")
            if HOOK_MARKER not in text:
                return (
                    False,
                    f"Refusing to overwrite existing custom hook '{target}' (pass --force to replace)",
                    [],
                )

    hook_dir.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    written: List[str] = []
    for h in hooks:
        target = hook_dir / h
        target.write_text(_hook_script(h, script_path), encoding="utf-8")
        target.chmod(0o755)
        written.append(str(target))
    return True, f"Installed {len(written)} hook(s) in {hook_dir}", written


def print_config(kind: str) -> str:
    if kind == "commit-check":
        return (
            "[commit_check]\n"
            'branch_name_pattern = "^(?:main|master|develop|(?:feature|feat|bugfix|fix|hotfix|release|chore|ai|copilot|cursor|claude|codex)/[a-z0-9]+(?:\\\\.[a-z0-9]+)*(?:-[a-z0-9]+(?:\\\\.[a-z0-9]+)*)*)$"\n'
            'require_description_grammar = true\n'
            'allow_branch_types = [\n'
            '  "feature", "feat", "bugfix", "fix", "hotfix", "release", "chore",\n'
            '  "ai", "copilot", "cursor", "claude", "codex"\n'
            ']\n'
        )
    if kind == "github-ruleset":
        regex = build_branch_regex().pattern
        return json.dumps(
            {
                "name": "Conventional Branch 1.1.0",
                "target": "branch",
                "enforcement": "active",
                "rules": [
                    {
                        "type": "branch_name_pattern",
                        "parameters": {
                            "operator": "regex_match",
                            "pattern": regex,
                            "negate": False,
                            "name": "Conventional Branch 1.1.0",
                        },
                    }
                ],
            },
            indent=2,
        ) + "\n"
    if kind == "proj-standards":
        return json.dumps(
            {
                "allow_branch_types": [],
                "allow_commit_types": [],
                "max_staged_mb": 5.0,
                "private_files_extra": ["CLAUDE.local.md", "*.local.env"],
            },
            indent=2,
        ) + "\n"
    raise ValueError(f"Unknown config kind: {kind}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repoguard",
        description="Conventional Branch 1.1.0, Conventional Commits 1.0.0 and Git hygiene guard",
    )
    parser.add_argument("--version", action="version", version=f"repoguard {VERSION}")
    sub = parser.add_subparsers(dest="command")

    p_branch = sub.add_parser("check-branch", help="Validate branch name against Conventional Branch 1.1.0")
    p_branch.add_argument("name", nargs="?", help="Branch name (defaults to current git HEAD)")
    p_branch.add_argument("--allow-type", action="append", default=[], help="Extra allowed branch prefix")
    p_branch.add_argument("--default-type", default="feat", help="Fallback prefix for suggestions (default: feat)")
    p_branch.add_argument("--allow-detached", action="store_true", help="Exit 0 when HEAD is detached")
    p_branch.add_argument("--no-suggest", action="store_true", help="Omit sanitized branch suggestion")
    p_branch.add_argument("--json", action="store_true", help="Output JSON")

    p_commit = sub.add_parser("check-commit", help="Validate commit message against Conventional Commits 1.0.0")
    p_commit.add_argument("message", nargs="?", help="Commit message text (or '-' for stdin)")
    p_commit.add_argument("--file", dest="msg_file", help="Path to commit message file (e.g. .git/COMMIT_EDITMSG)")
    p_commit.add_argument("--allow-type", action="append", default=[], help="Extra allowed commit type")
    p_commit.add_argument("--no-git-auto", action="store_true", help="Disallow Merge/Revert/fixup! headers")
    p_commit.add_argument("--strict", action="store_true", help="Fail if header exceeds 72 characters")
    p_commit.add_argument("--json", action="store_true", help="Output JSON")

    p_stage = sub.add_parser("check-stage", help="Audit staged changes for secrets, private files and size")
    p_stage.add_argument("--max-mb", type=float, default=None, help="Max staged file size in MiB (default: 5.0)")
    p_stage.add_argument("--json", action="store_true", help="Output JSON")

    p_ver = sub.add_parser("suggest-version", help="Suggest next SemVer tag from commits since latest tag")
    p_ver.add_argument("--since", default=None, help="Base tag (defaults to latest SemVer tag)")
    p_ver.add_argument("--json", action="store_true", help="Output JSON")

    p_hooks = sub.add_parser("install-hooks", help="Install repoguard Git hooks into the current repo")
    p_hooks.add_argument(
        "--hooks",
        default="pre-commit,commit-msg,pre-push",
        help="Comma-separated hooks to install (default: pre-commit,commit-msg,pre-push)",
    )
    p_hooks.add_argument("--force", action="store_true", help="Overwrite existing non-repoguard hooks")

    p_cfg = sub.add_parser("print-config", help="Print enforcement configuration templates")
    p_cfg.add_argument("kind", choices=("commit-check", "github-ruleset", "proj-standards"))

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    cfg, cfg_err = _load_config()
    if cfg_err and args.command in ("check-branch", "check-commit", "check-stage"):
        print(f"repoguard: {cfg_err}", file=sys.stderr)
        return 2

    if args.command == "check-branch":
        extra: List[str] = list(args.allow_type)
        if isinstance(cfg.get("allow_branch_types"), list):
            extra.extend(str(x) for x in cfg["allow_branch_types"])

        branch_name = args.name
        if branch_name is None:
            res = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"])
            if res.returncode != 0:
                if args.allow_detached:
                    payload = {"valid": True, "branch": "HEAD", "reason": "Detached HEAD allowed", "suggestion": None}
                    print(json.dumps(payload) if args.json else "OK: detached HEAD allowed")
                    return 0
                payload = {"valid": False, "branch": None, "reason": "Could not determine current branch (detached HEAD or not a git repo)", "suggestion": None}
                print(json.dumps(payload) if args.json else payload["reason"], file=sys.stdout if args.json else sys.stderr)
                return 1
            branch_name = res.stdout.strip()

        ok, reason = validate_branch(branch_name, extra)
        suggestion = None if (ok or args.no_suggest) else suggest_branch_name(branch_name, args.default_type, extra)
        if args.json:
            print(json.dumps({"valid": ok, "branch": branch_name, "reason": reason, "suggestion": suggestion}))
        else:
            if ok:
                print(f"OK: {branch_name} ({reason})")
            else:
                msg = f"INVALID: {branch_name} — {reason}"
                if suggestion:
                    msg += f"\nSuggestion: {suggestion}"
                print(msg, file=sys.stderr)
        return 0 if ok else 1

    if args.command == "check-commit":
        extra_c: List[str] = list(args.allow_type)
        if isinstance(cfg.get("allow_commit_types"), list):
            extra_c.extend(str(x) for x in cfg["allow_commit_types"])

        if args.msg_file:
            raw = Path(args.msg_file).read_text(encoding="utf-8", errors="replace")
        elif args.message == "-":
            raw = sys.stdin.read()
        elif args.message is not None:
            raw = args.message
        else:
            print("repoguard check-commit: provide a message argument or --file PATH", file=sys.stderr)
            return 2

        ok, reason, warns = validate_commit_message(
            raw,
            extra_types=extra_c,
            allow_git_auto=not args.no_git_auto,
            strict=args.strict,
        )
        if args.json:
            print(json.dumps({"valid": ok, "reason": reason, "warnings": warns}))
        else:
            if ok:
                print(f"OK: {reason}")
                for w in warns:
                    print(f"WARNING: {w}", file=sys.stderr)
            else:
                print(f"INVALID: {reason}", file=sys.stderr)
        return 0 if ok else 1

    if args.command == "check-stage":
        ok, report = check_staged(max_mb=args.max_mb)
        if args.json:
            print(json.dumps({"valid": ok, **report}))
        else:
            count = report.get("staged_count", 0)
            dirs = ", ".join(report.get("top_dirs", [])) or "-"
            if ok:
                print(f"OK: {count} staged file(s) clean (areas: {dirs})")
            else:
                print(f"INVALID: {count} staged file(s) failed checks (areas: {dirs}):", file=sys.stderr)
                for err in report.get("errors", []):
                    print(f"  - {err}", file=sys.stderr)
        return 0 if ok else 1

    if args.command == "suggest-version":
        ok, report = suggest_version(since_tag=args.since)
        if not ok:
            print(json.dumps(report) if args.json else report.get("error", "error"), file=sys.stdout if args.json else sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report))
        else:
            print(f"Current: {report['current_tag']} -> Next: {report['next_version']} ({report['bump']} bump, {report['commits']} commit(s))")
            for label, key in (("Breaking Changes", "breaking"), ("Features", "feat"), ("Fixes", "fix"), ("Other", "other")):
                items = report[key]
                if items:
                    print(f"\n### {label}")
                    for line in items:
                        print(line)
        return 0

    if args.command == "install-hooks":
        selected = [h.strip() for h in args.hooks.split(",") if h.strip()]
        if not selected:
            print("repoguard install-hooks: no hooks selected", file=sys.stderr)
            return 2
        ok, msg, written = install_hooks(selected, force=args.force)
        if ok:
            print(msg)
            for p in written:
                print(f"  installed: {p}")
            return 0
        print(f"repoguard install-hooks: {msg}", file=sys.stderr)
        return 1

    if args.command == "print-config":
        sys.stdout.write(print_config(args.kind))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
