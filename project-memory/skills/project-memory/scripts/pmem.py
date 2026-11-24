#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""pmem: project memory shared by every agent and account working on a project.

Memory lives inside the project in ``<root>/.agent-memory/``: ``MEMORY.md`` (index, loaded
natively by Claude Code through ``autoMemoryDirectory``), ``HANDOFF.md`` (live task state),
``handoffs/<key>.md`` (parallel workstreams) and one fact per ``<name>.md`` file. Preferences are
``feedback`` memories whose index lines sit under ``## Preferences`` in MEMORY.md (1.1).

Single file, Python >= 3.9, standard library only, Linux and macOS.
See the ``project-memory`` skill for the protocol agents follow.
"""

import argparse
import collections
import contextlib
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None
try:
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

VERSION = '1.1.0'
MEMDIR = '.agent-memory'
TYPES = ('user', 'feedback', 'project', 'reference')
RESERVED_DIRS = ('team', 'logs', 'sessions', 'proposals')
RESERVED_STEMS = ('memory', 'handoff')
HOOK_MARKER = '# project-memory'
WIRE_BEGIN = '<!-- BEGIN project-memory -->'
WIRE_END = '<!-- END project-memory -->'
GITIGNORE_LINES = ('.checkpoints/', '.pmem.lock', '.pmem-tmp-*', '.consolidate-lock', '.*.tmp')
GITIGNORE_TRACK_LINES = ('HANDOFF.md', 'handoffs/')
INDEX_SOFT_LINES, INDEX_MAX_LINES = 150, 200
INDEX_SOFT_CHARS, INDEX_MAX_CHARS = 20000, 25000
INDEX_LINE_CHARS = 150
FILE_SOFT_BYTES = 4096
FRONTMATTER_MAX_LINES = 30
HOOK_CONTEXT_CAP = 8000
CHECKPOINT_TAIL = 1500
HANDOFF_STALE_DAYS = 14
CHECKPOINT_GRACE = 600  # s: a checkpoint this much newer than the handoff means the handoff lags
SESSION_MSG_CHARS = 500
HOOK_GIT_TIMEOUT = 3
REF_SCAN_TIMEOUT = 10  # s: `git log --all -- .agent-memory` outside hooks (hooks use HOOK_GIT_TIMEOUT)
STALE_GIT_LOCK = 600   # s: an older .git/index.lock in the private history is removed by snapshot
LINK_PARSE_MAX = 2000  # longer lines are never parsed for links
OTHER_HANDOFFS_MAX = 10  # other handoffs listed in the size-capped session-start context
HOOK_EVENTS = (   # (event, matcher, `pmem hook` subcommand, timeout in s)
    ('SessionStart', 'startup|resume|clear|compact', 'session-start', 10),
    ('UserPromptSubmit', None, 'prompt', 5),
    ('Stop', None, 'checkpoint', 10),
    ('StopFailure', None, 'checkpoint', 10),
    ('PreCompact', None, 'checkpoint', 10),
)
PREF_BEGIN = '<!-- BEGIN project-memory:preferences -->'
PREF_END = '<!-- END project-memory:preferences -->'
PREF_GLOBAL_HEADING = ('## Preferences for every project (managed by `pmem pref promote` / `pmem pref '
                       "demote`; a project's own Preferences win on conflict)")
PREF_RULE_MAX = 120        # chars of a rule (pref add / violated refuse longer)
PREF_LINE_WARN = 220       # lint: preference entries are exempt from the 150 WARN, warn above this
PREF_MAX_ENTRIES = 20      # lint: entries per section
INHERIT_MAX_LINES, INHERIT_MAX_CHARS, INHERIT_RULE_CHARS = 40, 2000, 160
PROMPT_MAX_CHARS = 300     # hook prompt: longer messages (after dropping pasted/fenced text) are skipped
PROMPT_INTERVAL = 180      # s: at most one nudge per session in this interval
PROMPT_NUDGE_CHARS = 600
LIST_ALL_MAX_LINES = 200

PREF_HEADING = '## Preferences'
PREF_BLOCKQUOTE = ('> How the user wants you to work: follow every line, every session; on conflict this '
                   'project beats outer folders and the global list. Save a new one at once by editing '
                   'this file: `- [<title>](<name>.md) — <rule>` here (not under Memories) plus '
                   '`<name>.md` as a `feedback` memory with **Why:** and **How to apply:**; then end your '
                   'reply with `✓ preference: <rule>`.')
MEMORIES_HEADING = '## Memories'
HANDOFF_LINE = ('- [HANDOFF — current state](HANDOFF.md) — read before any task at session start; '
                'update at milestones')

# Templates (also shipped as skills/project-memory/templates/*; a test asserts files == constants).
MEMORY_TEMPLATE = """\
# Project memory

> Shared by every agent and account working on this project. Never delete files here — retire them with `pmem archive`. HANDOFF.md and handoffs/ hold live task state: update them, never prune them.

""" + HANDOFF_LINE + "\n\n" + PREF_HEADING + "\n" + PREF_BLOCKQUOTE + "\n\n" + MEMORIES_HEADING + "\n"

HANDOFF_TEMPLATE = """\
---
name: handoff
description: Live task state for resuming or switching agents (not a memory) — read first, keep current, never delete
metadata:
  type: project
  modified: {now_utc}
  by: {by}
  workstream: {key}
---

## Goal
(no active task)

## Done

## Next step

## Live state
<!-- branches, worktrees, running jobs/PIDs, remote dirs, URLs — each with a command to check it -->

## Decisions and constraints in force

## Open questions
"""

WIRE_BLOCK = """\
<!-- BEGIN project-memory -->
## Project memory (`.agent-memory/`)
- Project knowledge lives inside the project, shared by every agent and account: the nearest `.agent-memory/` from the working directory upward (never `$HOME`; in a git worktree, look from the main checkout). It takes precedence over any other memory location, including your built-in memory store and older per-repo notes files such as CLAUDE.local.md.
- At session start and after compaction, run `pmem context` if available (handoff, any newer auto checkpoint, inherited preferences, index); otherwise read `HANDOFF.md` (or your worktree's `handoffs/<name>.md`) and a newer `.checkpoints/<key>.md` — no agent loads these natively — then `MEMORY.md` if it is not already in your context. Open single memories only when relevant and verify them against the current code before acting.
- Follow every line under `## Preferences` in that `MEMORY.md`, in the `MEMORY.md` of any `.agent-memory/` further up, and in this file's preferences block; on conflict the nearest wins. When the user states a lasting preference or corrects how you work, save it at once by editing the files: `- [<title>](<name>.md) — <rule>` under `## Preferences` plus `<name>.md` as a `feedback` memory with Why and How to apply (`pmem pref add` does the same; `--up` for the whole workspace), then end your reply with `✓ preference: <rule>`. If you broke an existing one, sharpen its line and move it to the top. Task-specific instructions are not preferences.
- Before debugging an error, search the memory for its key tokens (`pmem find ...` or grep) and try a recorded fix first.
- Save right away when the user confirms an unusual approach, when a failure took 3+ attempts (symptom, root cause, fix), or when you repeat a multi-step procedure: one fact per file with `name`/`description`/`metadata.type`/`metadata.modified` (UTC) frontmatter plus one line in `MEMORY.md`. Never save secrets; a preference that holds for every project goes to the global preferences block only with the user's consent (`pmem pref promote`).
- Keep the handoff current at milestones (step done, before long or remote jobs, before stopping), setting `metadata.modified` and `by`, so another agent can resume if this session ends abruptly. Never delete memory files; retire them with `pmem archive`.
- If the sandbox blocks writes to `.agent-memory/`, ask for approval or a relaunch that makes it writable (e.g. start from the project root, or Codex `--add-dir <project root>`); never save elsewhere. Details: the `project-memory` skill; CLI `pmem` (`pmem status` reports wiring problems).
<!-- END project-memory -->
"""

TEMPLATES = {'MEMORY.md': MEMORY_TEMPLATE, 'HANDOFF.md': HANDOFF_TEMPLATE,
             'wire-block.md': WIRE_BLOCK}

QUIET = False          # hooks: never print warnings
GIT_TIMEOUT = None     # hooks: 3 s per git call


# ---------------------------------------------------------------------------------------------
# Basic helpers

class PmemError(Exception):
    """A reportable failure; ``code`` is the exit status (1 problem, 2 usage/refused)."""

    def __init__(self, message, code=1):
        Exception.__init__(self, message)
        self.code = code


def eprint(*args):
    if not QUIET:
        print(*args, file=sys.stderr)


def warn(message):
    eprint('pmem: warning: ' + message)


def enc(text):
    try:
        return text.encode('utf-8', 'surrogateescape')
    except UnicodeEncodeError:
        return text.encode('utf-8', 'replace')


def dec(data):
    return data.decode('utf-8', 'surrogateescape')


def clean_text(text):
    """Make text safe to print/serialise: undo surrogateescape, replace lone surrogates."""
    return enc(text).decode('utf-8', 'replace')


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline='') as f:
        return f.read()


def nfc(text):
    return unicodedata.normalize('NFC', text)


def oneline(text):
    return ' '.join((text or '').split())


def kebab(text):
    k = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return k or 'worktree'


def worktree_key(path):
    """Handoff key of a linked worktree: its kebab basename, never ``main`` (the main checkout's
    HANDOFF.md), so a worktree named ``main`` (bare container layouts) gets ``worktree-main``."""
    k = kebab(os.path.basename(path))
    return 'worktree-main' if k == 'main' else k


def now_utc_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def now_local_iso():
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today():
    return _dt.date.today().isoformat()


def fmt_epoch(ts):
    if ts is None:
        return '-'
    return _dt.datetime.fromtimestamp(ts).astimezone().strftime('%Y-%m-%d %H:%M')


def is_under(child, parent):
    """True if ``child`` equals ``parent`` or lies inside it (both normalised absolute paths)."""
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def home_dir():
    h = os.environ.get('HOME')
    if h:
        return os.path.realpath(h)
    try:
        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return None


def display_path(path):
    home = home_dir()
    if home and path and is_under(path, home) and path != home:
        return '~' + path[len(home):]
    return path


def rejected_root(path, home):
    """Project roots may not be ``/``, HOME or an ancestor of HOME."""
    path = os.path.realpath(path)
    if os.path.dirname(path) == path:
        return True
    return bool(home) and is_under(home, path)


def same_path(value, path):
    """Compare a settings value (absolute or ~/...) with a real path."""
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.rstrip('/') or '/'
    a = os.path.realpath(os.path.expanduser(v))
    return nfc(a) == nfc(os.path.realpath(path))


def truthy_env(name):
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ('', '0', 'false', 'no', 'off')


def guard(memdir, path):
    """Refuse ``path`` if it, or any directory between ``memdir`` (a realpath) and it, is a
    symlink: pmem never reads or writes through links inside the memory dir. -> path."""
    if not memdir:
        return path
    rel = os.path.relpath(path, memdir)
    if rel == os.curdir:
        return path
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise PmemError('%s is outside the memory dir %s' % (path, memdir), 1)
    cur = memdir
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise PmemError('refusing to follow the symlink %s inside the memory dir (pmem never '
                            'reads or writes through links there; replace it with a regular file)'
                            % cur, 1)
    return path


def memdir_symlinks(memdir):
    """Every symlink inside the memory dir (the private history ``.git`` excluded)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(memdir):
        if dirpath == memdir:
            dirnames[:] = [d for d in dirnames if d != '.git']
            filenames = [f for f in filenames if f != '.git']
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                found.append(p)
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    return sorted(found)


def atomic_write(path, data, mode=None, memdir=None, follow=True):
    """Write bytes atomically; keep the mode of an existing target (new: 0644).
    ``follow``: write through a symlinked ``path`` (user-owned global files); else replace the
    link itself. ``memdir``: ``path`` lies inside the memory dir, where symlinks are refused."""
    if memdir:
        guard(memdir, path)
    if follow:
        target = os.path.realpath(path)
    else:
        target = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(path))),
                              os.path.basename(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix='.pmem-tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is None:
            try:
                st = os.stat(target) if follow else os.lstat(target)
                mode = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else 0o644
            except OSError:
                mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def memory_lock(memdir):
    """Exclusive flock on .agent-memory/.pmem.lock (no-op without fcntl)."""
    if fcntl is None:
        yield
        return
    path = os.path.join(memdir, '.pmem.lock')
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    except OSError as e:
        raise PmemError('cannot open the lock file %s: %s' % (path, e), 1)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------
# Lines with preserved line endings

def to_lines(text):
    """Split into [content, eol] pairs; eol is '\\n', '\\r\\n' or '' (last line without newline)."""
    parts = text.split('\n')
    lines = []
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            if part:
                lines.append([part, ''])
        elif part.endswith('\r'):
            lines.append([part[:-1], '\r\n'])
        else:
            lines.append([part, '\n'])
    return lines


def from_lines(lines):
    return ''.join(c + e for c, e in lines)


def eol_of(lines):
    return '\r\n' if lines and lines[0][1] == '\r\n' else '\n'


def append_line(lines, text, eol):
    if lines and lines[-1][1] == '':
        lines[-1][1] = eol
    lines.append([text, eol])


def first_nonblank(text, limit=None):
    for line in text.split('\n'):
        s = line.strip().lstrip('\ufeff')
        if s and s != '---':
            return s[:limit] if limit else s
    return ''


# ---------------------------------------------------------------------------------------------
# slug() — Claude Code's per-project directory name

def base36(n):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    if n == 0:
        return '0'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def _slug_units(path):
    raw = path.encode('utf-16-le', 'surrogatepass')
    units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
    s = ''.join(chr(u) if (48 <= u <= 57 or 65 <= u <= 90 or 97 <= u <= 122) else '-'
                for u in units)
    return s, units


def slug(path):
    """Every UTF-16 code unit not in [A-Za-z0-9] -> '-'; > 200 units -> prefix + '-' + base36 hash."""
    s, units = _slug_units(path)
    if len(s) <= 200:
        return s
    h = 0
    for u in units:
        h = ((h << 5) - h + u) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return s[:200] + '-' + base36(abs(h))


def slug_prefix(path):
    """Prefix shared by the slugs of every path below ``path`` (hashed long slugs included).
    Names with this prefix may also come from siblings such as ``<path>-other``: check their cwd."""
    s, _ = _slug_units(path)
    return (s + '-')[:200]


# ---------------------------------------------------------------------------------------------
# Timestamps

TS_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})'
                   r'(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?'
                   r'(Z|[+-]\d{2}:\d{2})?$')


def parse_ts(value):
    """Parse YYYY-MM-DD([ T]HH:MM(:SS(.f+)?)?)?(Z|+-HH:MM)? -> epoch seconds (naive = local)."""
    if value is None:
        return None
    m = TS_RE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        micro = int((frac or '0')[:6].ljust(6, '0'))
        dt = _dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), micro)
        if tz is None:
            return dt.timestamp()
        if tz == 'Z':
            offset = 0
        else:
            offset = (1 if tz[0] == '+' else -1) * (int(tz[1:3]) * 60 + int(tz[4:6]))
        return dt.replace(tzinfo=_dt.timezone(_dt.timedelta(minutes=offset))).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------------------------
# Frontmatter: a small tolerant YAML subset (no PyYAML)

FM_KEY_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?$')
BLOCK_SCALAR_RE = re.compile(r'^[|>][+-]?[0-9]?$')
YAML_ESCAPES = {'n': '\n', 't': '\t', '"': '"', '\\': '\\', '/': '/', 'r': '\r', '0': '\0',
                'b': '\b', 'f': '\f', ' ': ' ', 'e': '\x1b', 'a': '\x07', 'v': '\x0b',
                'N': '\x85', '_': '\xa0', 'L': '\u2028', 'P': '\u2029', '\t': '\t'}


def strip_yaml_comment(text):
    """Drop a trailing ``# comment`` from a plain (unquoted) YAML value; ``# c`` alone -> ''."""
    s = (text or '').strip()
    if not s or s[0] in '"\'':
        return s
    if s[0] == '#':
        return ''
    m = re.search(r'[ \t]#', s)
    return s[:m.start()].rstrip() if m else s


def yaml_scalar(text):
    s = strip_yaml_comment(text)
    if not s:
        return ''
    if s[0] == '"':
        out, i = [], 1
        while i < len(s):
            c = s[i]
            if c == '\\' and i + 1 < len(s):
                n = s[i + 1]
                width = {'x': 2, 'u': 4, 'U': 8}.get(n)
                if width and re.match(r'[0-9A-Fa-f]{%d}$' % width, s[i + 2:i + 2 + width]):
                    out.append(chr(int(s[i + 2:i + 2 + width], 16)))
                    i += 2 + width
                    continue
                out.append(YAML_ESCAPES.get(n, '\\' + n))
                i += 2
                continue
            if c == '"':
                break
            out.append(c)
            i += 1
        value = ''.join(out)
        try:  # join UTF-16 surrogate pairs written as two \\u escapes
            value = value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
        except UnicodeDecodeError:
            pass
        return value
    if s[0] == "'":
        out, i = [], 1
        while i < len(s):
            c = s[i]
            if c == "'":
                if s[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(c)
            i += 1
        return ''.join(out)
    return s


def yaml_quote(value):
    """Emit a scalar plain when safe, else double-quoted (JSON escapes are valid YAML)."""
    s = value
    if (not s or s != s.strip() or s[0] in '-?:,[]{}#&*!|>\'"%@`' or ': ' in s or ' #' in s
            or s.endswith(':') or any(ord(c) < 32 or c in '\x7f\x85\u2028\u2029\ufeff' for c in s)
            or re.match(r'(?i)(true|false|yes|no|on|off|null|y|n|~)$', s)
            or re.match(r'[-+.0-9_eE]+$', s) or TS_RE.match(s)):
        return json.dumps(s, ensure_ascii=False)
    return s


class Frontmatter(object):
    """Parsed frontmatter. ``entries`` keeps raw lines so rewrites preserve unknown keys."""

    def __init__(self):
        self.top = collections.OrderedDict()
        self.meta = collections.OrderedDict()
        self.entries = []   # [kind ('top'|'meta'|'other'), key, raw lines, indent]

    def get(self, key):
        v = self.top.get(key)
        return v if isinstance(v, str) else None

    @property
    def name(self):
        return (self.get('name') or '').strip()

    @property
    def description(self):
        return (self.get('description') or '').strip()

    @property
    def type(self):
        v = self.meta.get('type')
        if not isinstance(v, str) or not v.strip():
            v = self.get('type')
        return (v or '').strip()

    @property
    def by(self):
        v = self.meta.get('by')
        if not isinstance(v, str) or not v.strip():
            v = self.get('by')
        return (v or '').strip()

    def timestamp_values(self):
        vals = [self.meta.get('modified'), self.meta.get('updated'),
                self.get('modified'), self.get('updated')]
        return [v for v in vals if isinstance(v, str) and v.strip()]


def split_frontmatter(text):
    """-> (frontmatter lines or None, body, index of the closing '---' line or None)."""
    if text.startswith('\ufeff'):
        text = text[1:]
    lines = text.split('\n')
    if lines[0].rstrip() != '---':
        return None, text, None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == '---':
            return [l.rstrip('\r') for l in lines[1:i]], '\n'.join(lines[i + 1:]), i
    return [l.rstrip('\r') for l in lines[1:]], '', None


def parse_frontmatter(fm_lines):
    fm = Frontmatter()
    current = None
    in_meta = False
    meta_indent = None
    for raw in fm_lines:
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(' \t'))
        if not stripped or stripped.startswith('#'):
            if current is not None:
                current[2].append(raw)
            else:
                fm.entries.append(['other', None, [raw], 0])
            continue
        if indent == 0:
            m = FM_KEY_RE.match(raw.rstrip())
            if m:
                key, rest = m.group(1), (m.group(2) or '')
                current = ['top', key, [raw], 0]
                fm.entries.append(current)
                in_meta = False
                if key == 'metadata':
                    rest_s = strip_yaml_comment(rest)
                    if not rest_s:
                        in_meta, meta_indent = True, None
                        fm.top[key] = None
                    elif rest_s.startswith('{') and rest_s.endswith('}'):
                        for part in rest_s[1:-1].split(','):
                            if ':' in part:
                                k, v = part.split(':', 1)
                                fm.meta[k.strip()] = yaml_scalar(v)
                        fm.top[key] = None
                    else:
                        fm.top[key] = yaml_scalar(rest)
                else:
                    fm.top[key] = yaml_scalar(rest)
                continue
        elif in_meta and (meta_indent is None or indent <= meta_indent):
            m = FM_KEY_RE.match(stripped)
            if m:
                meta_indent = indent
                current = ['meta', m.group(1), [raw], indent]
                fm.entries.append(current)
                fm.meta[m.group(1)] = yaml_scalar(m.group(2) or '')
                continue
        if current is not None:
            current[2].append(raw)
        else:
            fm.entries.append(['other', None, [raw], 0])
    # block scalars (| or >) take their indented continuation lines
    for kind, key, raw, _ in fm.entries:
        if kind not in ('top', 'meta') or len(raw) < 2:
            continue
        m = FM_KEY_RE.match(raw[0].strip())
        rest = strip_yaml_comment(m.group(2) or '') if m else ''
        if BLOCK_SCALAR_RE.match(rest):
            parts = [l.strip() for l in raw[1:]]
            value = ('\n' if rest[0] == '|' else ' ').join(parts).strip()
            (fm.top if kind == 'top' else fm.meta)[key] = value
    return fm


def render_memory(name, description, mtype, modified, body, old=None, meta=None):
    """Memory file text; unknown frontmatter keys of ``old`` are preserved. ``meta``: extra
    metadata keys (an existing key of the same name is replaced)."""
    meta = meta or {}
    out = ['---', 'name: ' + yaml_quote(name), 'description: ' + yaml_quote(oneline(description))]
    meta_extra = []
    if old is not None:
        for kind, key, raw, indent in old.entries:
            if kind == 'other' or (kind == 'top' and key not in
                                   ('name', 'description', 'metadata', 'type', 'modified', 'updated')):
                out.extend(raw)
            elif kind == 'meta' and key not in ('type', 'modified', 'updated') and key not in meta:
                for line in raw:
                    lead = len(line) - len(line.lstrip(' \t'))
                    meta_extra.append('  ' + line[min(lead, indent):] if line.strip() else line)
    meta_extra += ['  %s: %s' % (k, v if isinstance(v, int) else yaml_quote(str(v)))
                   for k, v in meta.items()]
    out += ['metadata:', '  type: ' + mtype, '  modified: ' + modified] + meta_extra + ['---', '']
    text = '\n'.join(out) + '\n'
    if body:
        text += body if body.endswith('\n') else body + '\n'
    return text


class MemFile(object):
    """A markdown file in the memory dir with its parsed frontmatter."""

    def __init__(self, path, memdir=None):
        self.path = path
        self.fname = os.path.basename(path)
        self.stem = self.fname[:-3] if self.fname.endswith('.md') else self.fname
        self.error = None
        self.text = ''
        try:
            if os.path.islink(path):
                raise PmemError('symlink inside the memory dir (never followed)', 1)
            guard(memdir, path)
            self.text = read_text(path)
        except PmemError as e:
            self.error = str(e)
        except FileNotFoundError:
            self.error = 'missing'
        except OSError as e:
            self.error = str(e)
        fm_lines, self.body, self.close = split_frontmatter(self.text)
        self.has_fm = fm_lines is not None
        self.fm = parse_frontmatter(fm_lines) if fm_lines is not None else None

    @property
    def closed(self):
        return self.has_fm and self.close is not None

    @property
    def name(self):
        return (self.fm.name if self.fm else '') or ''

    @property
    def description(self):
        return (self.fm.description if self.fm else '') or ''

    @property
    def type(self):
        return (self.fm.type if self.fm else '') or ''

    def timestamp(self):
        """-> (epoch, unparseable flag). Newest parseable value; else file mtime."""
        vals = self.fm.timestamp_values() if self.fm else []
        good = [t for t in (parse_ts(v) for v in vals) if t is not None]
        bad = len(good) < len(vals)
        if good:
            return max(good), bad
        return self.mtime(), bad

    def has_timestamp(self):
        return bool(self.fm and self.fm.timestamp_values())

    def mtime(self):
        try:
            return os.lstat(self.path).st_mtime
        except OSError:
            return None

    def handoff_timestamp(self):
        """Handoffs: newest of the frontmatter timestamp and the file mtime (agents often edit a
        handoff without bumping ``modified``). -> (epoch, unparseable flag, display string)."""
        ts, bad = self.timestamp()
        mt = self.mtime()
        vals = self.fm.timestamp_values() if self.fm else []
        if mt is not None and (ts is None or not vals or mt > ts + 2):
            return max(t for t in (ts, mt) if t is not None), bad, fmt_epoch(mt)
        return ts, bad, (vals[0] if vals else fmt_epoch(ts))


# ---------------------------------------------------------------------------------------------
# Index (MEMORY.md) parsing

# Bare link destinations exclude '(' and '<', angle-bracket ones exclude '<': a failed match never
# rescans the rest of the line, so matching stays linear even on pathological lines.
MD_LINK_RE = re.compile(r'\[(?:\\.|[^\[\]\\\n]|\[[^\[\]\n]*\])*\]'
                        r'\(\s*(<[^<>\n]*>|[^\s()<]*)(?:\s+(?:"[^"\n]*"|\'[^\'\n]*\'))?\s*\)')
MEM_TARGET_RE = re.compile(r'^[^/:\\]+\.md$')
FENCE_OPEN_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
FENCE_CLOSE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})[ \t]*$')
WIKI_LINK_RE = re.compile(r'\[\[([^\[\]\n]+)\]\]')
WIKI_TARGET_RE = re.compile(r'^[^\s\'"\[\]][^\'"\[\]]*$')


def _blank(chars, a, b):
    for k in range(a, min(b, len(chars))):
        chars[k] = ' '


def mask_inline(line, in_comment):
    """Blank out inline code spans and HTML comments (same length). -> (masked, in_comment)."""
    n = len(line)
    if n > LINK_PARSE_MAX:  # never link-parsed; only track comment state (linear)
        i = 0
        while True:
            if in_comment:
                j = line.find('-->', i)
                if j < 0:
                    break
                in_comment, i = False, j + 3
            else:
                j = line.find('<!--', i)
                if j < 0:
                    break
                in_comment, i = True, j + 4
        return ' ' * n, in_comment
    chars = list(line)
    i = 0
    if in_comment:
        j = line.find('-->')
        if j < 0:
            return ' ' * n, True
        _blank(chars, 0, j + 3)
        i, in_comment = j + 3, False
    while i < n:
        ch = line[i]
        if ch == '\\':
            i += 2
            continue
        if ch == '`':
            k = i
            while k < n and line[k] == '`':
                k += 1
            run, j, close = k - i, k, -1
            while j < n:
                if line[j] == '`':
                    m = j
                    while m < n and line[m] == '`':
                        m += 1
                    if m - j == run:
                        close = j
                        break
                    j = m
                else:
                    j += 1
            if close < 0:
                i = k          # unmatched backticks are literal
                continue
            _blank(chars, i, close + run)
            i = close + run
            continue
        if ch == '<' and line.startswith('<!--', i):
            j = line.find('-->', i + 4)
            if j < 0:
                _blank(chars, i, n)
                return ''.join(chars), True
            _blank(chars, i, j + 3)
            i = j + 3
            continue
        i += 1
    return ''.join(chars), in_comment


def fence_scan(contents, eof_closes=False):
    """-> (flags, index of the first fence that never closes or None). ``flags[i]`` is True for
    the lines of a fenced code block that closes (fence lines included). A fence that never
    closes masks nothing (its opening line is ordinary text), unless ``eof_closes`` (CommonMark:
    it runs to the end; used for prompts). Fences never open inside an HTML comment. Linear."""
    n = len(contents)
    closer = [None] * n
    for i, c in enumerate(contents):
        m = FENCE_CLOSE_RE.match(c)
        if m:
            closer[i] = (m.group(1)[0], len(m.group(1)))
    longest = {'`': [0] * (n + 1), '~': [0] * (n + 1)}   # longest closing fence at or below i
    for i in range(n - 1, -1, -1):
        for ch in '`~':
            longest[ch][i] = longest[ch][i + 1]
        if closer[i] and closer[i][1] > longest[closer[i][0]][i]:
            longest[closer[i][0]][i] = closer[i][1]
    flags = [False] * n
    unclosed = None
    in_comment = False
    i = 0
    while i < n:
        c = contents[i]
        if not in_comment:
            m = FENCE_OPEN_RE.match(c)
            if m and not (m.group(1)[0] == '`' and '`' in m.group(2)):
                ch, size = m.group(1)[0], len(m.group(1))
                if longest[ch][i + 1] >= size:
                    j = i + 1
                    while not (closer[j] and closer[j][0] == ch and closer[j][1] >= size):
                        j += 1
                    for k in range(i, j + 1):
                        flags[k] = True
                    i = j + 1
                    continue
                if unclosed is None:
                    unclosed = i
                if eof_closes:
                    for k in range(i, n):
                        flags[k] = True
                    break
        _, in_comment = mask_inline(c, in_comment)
        i += 1
    return flags, unclosed


def mask_lines(contents):
    """Line contents with code spans, HTML comments (multi-line too) and fenced code blocks
    blanked out, keeping every length, so link offsets stay valid on the original lines.
    A code fence that never closes hides nothing (see fence_scan)."""
    flags, _ = fence_scan(contents)
    out = []
    in_comment = False
    for c, fenced in zip(contents, flags):
        if fenced:
            out.append(' ' * len(c))
            continue
        masked, in_comment = mask_inline(c, in_comment)
        out.append(masked)
    return out


def memory_link_target(line):
    """Target of a memory link line, else None (``line`` already masked, see mask_lines).

    A memory link line is a line whose FIRST markdown link target, with any #anchor stripped
    and percent-decoded, matches ^[^/:\\\\]+\\.md$. Lines over LINK_PARSE_MAX chars never are.
    """
    if len(line) > LINK_PARSE_MAX:
        return None
    m = MD_LINK_RE.search(line)
    if not m:
        return None
    target = m.group(1)
    if target.startswith('<') and target.endswith('>'):
        target = target[1:-1]
    target = urllib.parse.unquote(target.split('#', 1)[0], errors='surrogateescape')
    return target if MEM_TARGET_RE.match(target) and '\n' not in target else None


def line_target(line):
    """Memory link target of one standalone line (code spans/comments ignored)."""
    return memory_link_target(mask_lines([line])[0])


def md_target(fname):
    return ''.join('%%%02X' % ord(c) if c in ' ()<>[]#%`' else c for c in fname)


def md_text(text):
    return (oneline(text).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            .replace('`', '\\`'))


def index_line(title, fname, description):
    line = '- [%s](%s)' % (md_text(title), md_target(fname))
    d = oneline(description)
    return line + ' — ' + d if d else line


def replace_link_target(line, new_fname, masked=None):
    m = MD_LINK_RE.search(masked if masked is not None else line)
    if not m:
        return line
    old = line[m.start(1):m.end(1)]
    anchor = ''
    inner = old[1:-1] if old.startswith('<') and old.endswith('>') else old
    if '#' in inner:
        anchor = '#' + inner.split('#', 1)[1]
    return line[:m.start(1)] + md_target(new_fname) + anchor + line[m.end(1):]


def generated_index_line(path, fname=None):
    """Index line for a file: name/description from frontmatter, else stem + first line."""
    mf = MemFile(path)
    fname = fname or mf.fname
    stem = fname[:-3] if fname.endswith('.md') else fname
    if mf.closed:
        return index_line(mf.name or stem, fname, mf.description or first_nonblank(mf.body, 120))
    return index_line(stem, fname, first_nonblank(mf.text, 120))


def indexable(fname):
    """True if pmem can write an index line for ``fname`` that it recognises again."""
    return line_target(index_line('x', fname, '')) == fname


def read_index(memdir):
    """-> (text, bom, lines) or (None, None, None) when MEMORY.md does not exist.
    Any other read problem raises PmemError (an unreadable index is never replaced)."""
    path = os.path.join(memdir, 'MEMORY.md')
    try:
        guard(memdir, path)
        text = read_text(path)
    except FileNotFoundError:
        return None, None, None
    except OSError as e:
        raise PmemError('cannot read %s: %s' % (path, e), 1)
    bom = text.startswith('\ufeff')
    lines = to_lines(text[1:] if bom else text)
    return text, bom, lines


def link_lines(lines):
    """[(index, target)] of the memory link lines (lines: [content, eol] pairs or strings)."""
    contents = [l if isinstance(l, str) else l[0] for l in lines]
    out = []
    for i, c in enumerate(mask_lines(contents)):
        t = memory_link_target(c)
        if t:
            out.append((i, t))
    return out


def canonical_target(masked):
    """Memory link target of a CANONICAL index line (``masked``: see mask_lines), else None:
    column 0, `- `, then the memory link right away (`- [title](name.md) — hook`). Only such
    lines are ever moved or removed by pmem; other references (prose, blockquotes, `*`,
    numbered or indented bullets) are left in place."""
    if not masked.startswith('- ') or len(masked) > LINK_PARSE_MAX or not MD_LINK_RE.match(masked, 2):
        return None
    return memory_link_target(masked)


def canonical_lines(lines, masked=None):
    """[(index, target)] of the canonical index lines (lines: [content, eol] pairs or strings)."""
    if masked is None:
        masked = mask_lines([l if isinstance(l, str) else l[0] for l in lines])
    out = []
    for i, m in enumerate(masked):
        t = canonical_target(m)
        if t:
            out.append((i, t))
    return out


def memory_files(memdir):
    """Top-level memory files: *.md except MEMORY.md/HANDOFF.md (reserved stems), dot-files and
    symlinks (never followed; lint reports them)."""
    out = []
    try:
        names = sorted(os.listdir(memdir))
    except OSError:
        return out
    for fn in names:
        if fn.startswith('.') or not fn.endswith('.md') or fn[:-3].lower() in RESERVED_STEMS:
            continue
        p = os.path.join(memdir, fn)
        if os.path.isfile(p) and not os.path.islink(p):
            out.append(fn)
    return out


def analyze_index(memdir, lines):
    """-> dict(dangling=[(lineno, target)], handoff='ok'|'missing'|'not-first', linked=set,
    unindexed=[file], unindexable=[file])."""
    links = link_lines(lines)
    dangling = [(i + 1, t) for i, t in links
                if t != 'HANDOFF.md' and not os.path.isfile(os.path.join(memdir, t))]
    hpos = [i for i, t in links if t == 'HANDOFF.md']
    if not hpos:
        handoff = 'missing'
    elif hpos[0] != links[0][0]:
        handoff = 'not-first'
    else:
        handoff = 'ok'
    linked = set(nfc(t) for _, t in links)
    files = memory_files(memdir)
    return {'dangling': dangling, 'handoff': handoff, 'linked': linked,
            'unindexed': [f for f in files if nfc(f) not in linked and indexable(f)],
            'unindexable': [f for f in files if not indexable(f)],
            'handoff_line': hpos[0] + 1 if hpos else None}


def place_handoff(lines, eol, line, links):
    """Insert the HANDOFF line before the first link line — or before the Preferences heading
    when the first link line is at or after it (never into the section)."""
    sec = pref_section(lines)
    first = links[0][0] if links else None
    if sec and (first is None or first >= sec[0]):
        lines[sec[0]:sec[0]] = [line, ['', eol]]
    elif first is not None:
        lines.insert(first, line)
    else:
        append_line(lines, line[0], eol)


def pref_index_line(path, fname=None):
    """Preference entry for a feedback memory file, built like `pmem pref add` builds one:
    `- [name](name.md) — description` with the title and the rule escaped."""
    mf = MemFile(path)
    fname = fname or mf.fname
    stem = fname[:-3] if fname.endswith('.md') else fname
    rule = oneline(mf.description or first_nonblank(mf.body if mf.closed else mf.text, 120))
    title = (mf.name if mf.closed else '') or stem
    line = entry_line(title, fname, rule) if rule else index_line(title, fname, '')
    return line if line_target(line) == fname else index_line(stem, fname, '')


def fix_index_lines(memdir, lines, eol):
    """Pure-ish index repair; never touches non-memory-link lines and moves or removes only
    canonical index lines (see canonical_target). -> actions (lines starting with 'note: '
    report what is left for the user and change nothing)."""
    err = unclosed_error(lines)
    if err:
        raise err
    actions, notes = [], []
    drop = set()
    masked = mask_lines([c for c, _ in lines])
    sec = pref_section(lines, masked)
    in_sec = dict((e.idx, e) for e in section_entries(lines, sec, masked))
    canon = dict(canonical_lines(lines, masked))
    for i, t in link_lines(lines):
        if t == 'HANDOFF.md' or os.path.isfile(os.path.join(memdir, t)):
            continue
        if i not in canon:
            notes.append('note: left MEMORY.md:%d in place: it links to the missing %s — edit it by '
                         'hand' % (i + 1, t))
            continue
        e = in_sec.get(i)
        if e is not None and e.hook:
            # a preference whose file is missing: the rule stays as a bare entry
            lines[i][0] = '- ' + e.hook
            actions.append('kept the preference "%s" without its missing file %s' % (e.hook, t))
            continue
        actions.append('removed dangling index line for %s' % t)
        drop.add(i)
    lines[:] = [l for i, l in enumerate(lines) if i not in drop]
    links = link_lines(lines)
    hpos = [i for i, t in links if t == 'HANDOFF.md']
    sec = pref_section(lines)
    if not hpos:
        place_handoff(lines, eol, [HANDOFF_LINE, eol], links)
        actions.append('inserted the HANDOFF line')
    elif hpos[0] != links[0][0] and not canonical_target(mask_lines([lines[hpos[0]][0]])[0]):
        notes.append('note: left MEMORY.md:%d in place: it is the first line linking HANDOFF.md but not '
                     'a `- [title](HANDOFF.md) — hook` line, so pmem does not move it — make the '
                     'HANDOFF line the first link line by hand' % (hpos[0] + 1))
    elif hpos[0] != links[0][0]:
        moved = lines.pop(hpos[0])
        if moved[1] == '':
            moved[1] = eol
        place_handoff(lines, eol, moved, link_lines(lines))
        actions.append('moved the HANDOFF line to be the first link line')
    elif sec and sec[0] < hpos[0] < sec[1] and line_target(lines[hpos[0]][0]) == 'HANDOFF.md' \
            and canonical_target(mask_lines([lines[hpos[0]][0]])[0]):
        moved = lines.pop(hpos[0])
        if moved[1] == '':
            moved[1] = eol
        place_handoff(lines, eol, moved, link_lines(lines))
        actions.append('moved the HANDOFF line out of ## Preferences (above the heading)')
    moved, left = move_feedback_into_section(memdir, lines, eol)
    if moved:
        actions.append('moved %d feedback line%s into Preferences — make each hook an imperative rule '
                       '(memory-health)' % (moved, '' if moved == 1 else 's'))
        if left:
            notes.append('note: ' + left_note(left))
    linked = set(nfc(t) for _, t in link_lines(lines))
    for fn in memory_files(memdir):
        if nfc(fn) in linked:
            continue
        if not indexable(fn):
            actions.append('cannot index %s — rename it (`:`, `\\` or line breaks cannot appear in '
                           'an index link)' % fn)
            continue
        sec = pref_section(lines)
        if sec and memory_type(memdir, fn) == 'feedback':
            section_insert(lines, eol, sec, [[pref_index_line(os.path.join(memdir, fn)), eol]])
        else:
            line = generated_index_line(os.path.join(memdir, fn))
            if line_target(line) != fn:   # e.g. odd text in the title: fall back to the stem
                line = index_line(fn[:-3], fn, '')
            append_ordinary(lines, eol, line)
        linked.add(nfc(fn))
        actions.append('indexed %s' % fn)
    return actions + notes


def update_index_locked(memdir, func):
    """``update_index`` for a caller that already holds the memory lock."""
    path = os.path.join(memdir, 'MEMORY.md')
    text, bom, lines = read_index(memdir)
    if text is None:
        text, bom, lines = '', False, to_lines(MEMORY_TEMPLATE)
    result = func(lines, eol_of(lines))
    new = ('\ufeff' if bom else '') + from_lines(lines)
    if new != text:
        atomic_write(path, enc(new), memdir=memdir)
    return result


def update_index(memdir, func):
    """Run ``func(lines, eol)`` on MEMORY.md under the lock (fresh read); write when changed."""
    with memory_lock(memdir):
        return update_index_locked(memdir, func)


# ---------------------------------------------------------------------------------------------
# Fences and HTML comments; marker lines of managed blocks

def fence_flags(contents, eof_closes=False):
    """True for each line that is part of a fenced code block (fence lines included); see
    fence_scan (a fence that never closes counts only with ``eof_closes``)."""
    return fence_scan(contents, eof_closes)[0]


def unclosed_fence(contents):
    """1-based line number of a code fence that never closes (outside HTML comments), else
    None. pmem does not treat the rest of the file as code, but renderers and agents may."""
    n = fence_scan(contents)[1]
    return None if n is None else n + 1


def unclosed_comment(contents):
    """1-based line number of an HTML comment that is still open at the end (outside fenced code
    and code spans), else None. Everything after it is hidden from agents and from pmem."""
    flags, _ = fence_scan(contents)
    in_comment = False
    start = None
    for n, (c, fenced) in enumerate(zip(contents, flags), 1):
        if fenced:
            continue
        was = in_comment
        _, in_comment = mask_inline(c, in_comment)
        if in_comment and (not was or '-->' in c):
            start = n
    return start if in_comment else None


def unclosed_problem(contents):
    """(kind, 1-based line) of an unclosed HTML comment or code fence in an index, else None."""
    n = unclosed_comment(contents)
    if n:
        return 'HTML comment', n
    n = unclosed_fence(contents)
    if n:
        return 'code fence', n
    return None


def marker_lines(contents, marker):
    """Indexes of the lines that are exactly ``marker`` (surrounding whitespace and CR ignored),
    outside fenced code blocks. ``contents``: line contents of a text whose BOM was removed."""
    flags = fence_flags(contents)
    return [i for i, c in enumerate(contents) if not flags[i] and c.strip() == marker]


def text_lines(raw):
    """bytes -> (bom, [content, eol] lines)."""
    bom = raw.startswith(b'\xef\xbb\xbf')
    return bom, to_lines(dec(raw[3:] if bom else raw))


# ---------------------------------------------------------------------------------------------
# Preferences: the `## Preferences` section of MEMORY.md

PREF_HEADING_RE = re.compile(r'^ {0,3}##\s+preferences\s*#*\s*$', re.I)
MEMORIES_HEADING_RE = re.compile(r'^ {0,3}##\s+memories\s*#*\s*$', re.I)
HEADING_RE = re.compile(r'^ {0,3}#{1,6}(?:[ \t]|$)')
SEP_RE = re.compile(r'^[\s—–:-]+')


class Entry(object):
    """One `- ` line of the Preferences section plus its indented continuation lines."""

    def __init__(self, idx, end, content, target, rule, title, hook='', canonical=False):
        self.idx, self.end, self.content = idx, end, content
        self.target, self.rule, self.title = target, rule, title
        self.hook = hook             # text after the memory link ('' when there is none)
        self.canonical = canonical   # `- [title](name.md) …` (see canonical_target)
        self.name = target[:-3] if target and target.endswith('.md') else None
        self.kind = None     # pref (feedback file) | bare (no link) | dangling | other

    @property
    def is_pref(self):
        return self.kind in ('pref', 'bare', 'dangling')


def pref_section(lines, masked=None):
    """(heading index, end index exclusive) of the first `## Preferences` heading (comments and
    fenced code ignored), the section running to the next heading; None if there is none."""
    contents = [l if isinstance(l, str) else l[0] for l in lines]
    masked = masked if masked is not None else mask_lines(contents)
    head = None
    for i, m in enumerate(masked):
        if head is None:
            if PREF_HEADING_RE.match(m):
                head = i
        elif HEADING_RE.match(m):
            return head, i
    return (head, len(masked)) if head is not None else None


def entry_parts(content, masked):
    """-> (memory link target or None, rule text, link text or None, hook text) of an entry line:
    the rule is the hook, i.e. the text after the memory link (`- [title](name.md) — rule`),
    else the text before it, else the title."""
    target = memory_link_target(masked)
    m = MD_LINK_RE.search(masked) if target else None
    dash = content.find('- ')
    if not m:
        return None, content[dash + 2:].strip(), None, ''
    link = content[m.start():m.end()]
    k = link.find('](')
    title = link[1:k] if k > 0 else ''
    before = content[dash + 2:m.start()].strip() if 0 <= dash < m.start() else ''
    after = SEP_RE.sub('', content[m.end():]).strip()
    rule = after or before.rstrip(' —–:-').strip() or title
    return target, rule, title, after


def indent_of(text):
    return len(text) - len(text.lstrip(' \t'))


def entry_block(lines, i):
    """[start, end) of line ``i`` plus its continuation lines: the following non-blank lines that
    are indented deeper than it."""
    def text(k):
        return lines[k] if isinstance(lines[k], str) else lines[k][0]
    base = indent_of(text(i))
    j = i + 1
    while j < len(lines) and text(j).strip() and indent_of(text(j)) > base:
        j += 1
    return i, j


def section_entries(lines, sec=None, masked=None):
    """Entries of the Preferences section (kind not yet classified)."""
    contents = [l if isinstance(l, str) else l[0] for l in lines]
    masked = masked if masked is not None else mask_lines(contents)
    sec = sec if sec is not None else pref_section(lines, masked)
    if not sec:
        return []
    out = []
    i, end = sec[0] + 1, sec[1]
    while i < end:
        if masked[i].startswith('- '):
            j = min(entry_block(contents, i)[1], end)
            target, rule, title, hook = entry_parts(contents[i], masked[i])
            if target or rule:
                out.append(Entry(i, j, contents[i], target, rule, title, hook,
                                 bool(target) and canonical_target(masked[i]) == target))
            i = j
        else:
            i += 1
    return out


def memory_type(memdir, fname, cache=None):
    """metadata.type of a top-level memory file ('' if unreadable), None if it does not exist."""
    if cache is not None and fname in cache:
        return cache[fname]
    p = os.path.join(memdir, fname)
    if os.path.islink(p) or not os.path.isfile(p) or '/' in fname:
        t = None
    else:
        t = MemFile(p, memdir).type
    if cache is not None:
        cache[fname] = t
    return t


def classify(memdir, entries, cache=None):
    cache = {} if cache is None else cache
    for e in entries:
        if not e.target:
            e.kind = 'bare'
        elif e.target == 'HANDOFF.md':
            e.kind = 'other'
        else:
            t = memory_type(memdir, e.target, cache)
            e.kind = 'dangling' if t is None else ('pref' if t == 'feedback' else 'other')
    return entries


def pref_entries(memdir, lines, cache=None):
    """Classified preference entries of an index (lines: [content, eol] pairs or strings)."""
    return [e for e in classify(memdir, section_entries(lines), cache) if e.is_pref]


def norm_rule(text):
    """Comparison key of a rule: NFKC, casefold, no leading ⚠, no non-word characters."""
    s = unicodedata.normalize('NFKC', text or '').strip()
    if s.startswith('⚠'):
        s = s[1:]
    return re.sub(r'\W+', '', s.casefold())


def rule_md(rule):
    """Rule text for an index line: `[` and `]` escaped outside code spans."""
    masked, _ = mask_inline(rule, False)
    out = []
    for i, ch in enumerate(rule):
        if ch in '[]' and masked[i] == ch and not (i and rule[i - 1] == '\\'):
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)


def check_rule(rule, check_len=True):
    """Validate a rule (exit 2 on problems). -> the stripped rule."""
    if rule is None or not rule.strip():
        raise PmemError('RULE is empty', 2)
    if '\n' in rule or '\r' in rule:
        raise PmemError('RULE must be a single line', 2)
    rule = rule.strip()
    if check_len and len(rule) > PREF_RULE_MAX:
        raise PmemError('RULE has %d characters (max %d): put detail in the body (--why/--how) and keep '
                        'the rule one short imperative line' % (len(rule), PREF_RULE_MAX), 2)
    if '<!--' in rule or '-->' in rule:
        raise PmemError('RULE must not contain <!-- or -->', 2)
    if rule.startswith(('#', '>')):
        raise PmemError('RULE must not start with # or >', 2)
    return rule


def entry_line(title, fname, rule):
    """`- [title](name.md) — rule` (falls back to the stem as title if the link would not parse)."""
    line = '- [%s](%s) — %s' % (md_text(title), md_target(fname), rule_md(rule))
    if line_target(line) != fname:
        line = '- [%s](%s) — %s' % (md_text(fname[:-3]), md_target(fname), rule_md(rule))
    return line


def blank_line_before(lines, j):
    """Whether a list entry inserted at ``j`` needs a blank line first (after a blockquote)."""
    return j > 0 and lines[j - 1][0].lstrip().startswith('>')


def section_insert(lines, eol, sec, block, top=False):
    """Insert ``block`` ([content, eol] pairs) at the end of the section (``top``: before its
    first entry). -> index of the first inserted line."""
    head, end = sec
    if top:
        entries = section_entries(lines, sec)
        if entries:
            j = entries[0].idx
            lines[j:j] = block
            return j
    j = end
    while j > head + 1 and not lines[j - 1][0].strip():
        j -= 1
    if lines[j - 1][1] == '':
        lines[j - 1][1] = eol
    pre = [['', eol]] if blank_line_before(lines, j) else []
    post = [['', eol]] if j < len(lines) and lines[j][0].strip() else []
    lines[j:j] = pre + block + post
    return j + len(pre)


def append_ordinary(lines, eol, text):
    """Append an ordinary index line (or a block of [content, eol] lines) at the end, never into
    the Preferences section: if the file ends inside it, a blank line and `## Memories` come
    first."""
    sec = pref_section(lines)
    if sec and sec[1] == len(lines):
        if lines and lines[-1][0].strip():
            append_line(lines, '', eol)
        append_line(lines, MEMORIES_HEADING, eol)
    for c in ([text] if isinstance(text, str) else [c for c, _ in text]):
        append_line(lines, c, eol)


def insert_section(lines, eol, mode):
    """Add `## Preferences` + blockquote + `## Memories`. ``mode`` 'init': only right after the
    HANDOFF line; 'add': else before the first memory link line, else at the end. An existing
    `## Memories` heading is reused: when it is the next heading after that point (or the
    heading above it), the section goes right before it and no second one is added.
    -> True if inserted."""
    links = link_lines(lines)
    hpos = [i for i, t in links if t == 'HANDOFF.md']
    if hpos:
        at = entry_block(lines, hpos[0])[1]           # after its continuation lines, if any
    elif mode != 'add':
        return False
    elif links:
        at = links[0][0]
    else:
        at = len(lines)
    masked = mask_lines([c for c, _ in lines])
    heads = [i for i, m in enumerate(masked) if HEADING_RE.match(m)]
    nxt = next((i for i in heads if i >= at), None)
    prev = [i for i in heads if i < at]
    mem = None
    if nxt is not None and MEMORIES_HEADING_RE.match(masked[nxt]):
        mem = nxt
    elif prev and MEMORIES_HEADING_RE.match(masked[prev[-1]]):
        mem = prev[-1]
    if mem is not None:
        at = mem
        while at > 0 and not lines[at - 1][0].strip():
            at -= 1
        if at > 0 and lines[at - 1][1] == '':
            lines[at - 1][1] = eol
        block = [[PREF_HEADING, eol], [PREF_BLOCKQUOTE, eol], ['', eol]]
        if at > 0:
            block.insert(0, ['', eol])
        lines[at:mem] = block
        return True
    block = [['', eol], [PREF_HEADING, eol], [PREF_BLOCKQUOTE, eol], ['', eol],
             [MEMORIES_HEADING, eol]]
    if at > 0 and lines[at - 1][1] == '':
        lines[at - 1][1] = eol
    if hpos:
        lines[at:at] = block
        return True
    if at == 0 or not lines[at - 1][0].strip():
        block = block[1:]
    lines[at:at] = block
    return True


def move_feedback_into_section(memdir, lines, eol, cache=None):
    """Move the canonical index line (with its continuation lines) of every feedback memory that
    has no entry in the Preferences section to the end of it, keeping text and order. Nothing
    else moves and nothing is ever deleted: other references to a feedback memory (prose,
    blockquotes, other bullets, a second index line) stay where they are.
    -> (moved count, [(line number, file)] of references to feedback memories left outside)."""
    cache = {} if cache is None else cache
    sec = pref_section(lines)
    if not sec:
        return 0, []
    inside = set(nfc(e.target) for e in section_entries(lines, sec) if e.target)
    blocks, seen = [], set()
    for i, t in canonical_lines(lines):
        if sec[0] <= i < sec[1] or nfc(t) in inside or nfc(t) in seen:
            continue
        if memory_type(memdir, t, cache) != 'feedback':
            continue
        seen.add(nfc(t))
        blocks.append(entry_block(lines, i))
    if blocks:
        drop, moved_lines = set(), []
        for a, b in blocks:
            drop.update(range(a, b))
            moved_lines += [[lines[k][0], eol] for k in range(a, b)]
        lines[:] = [l for k, l in enumerate(lines) if k not in drop]
        section_insert(lines, eol, pref_section(lines), moved_lines)
    outside, extra = feedback_refs(memdir, lines, cache)
    left = sorted([(ln, t) for ln, t, _ in outside] + extra)
    return len(blocks), left


def feedback_refs(memdir, lines, cache=None):
    """Memory link lines that point to feedback memories, except each one's entry in the
    Preferences section (its first canonical entry, else its first entry). -> (outside, extra).
    outside: [(line number, file, canonical)] — one per feedback memory with no entry in the
    section: its first canonical index line, else its first other reference. extra: [(line number,
    file)] — every other such line, inside the section or not (pmem never deletes them)."""
    cache = {} if cache is None else cache
    contents = [l if isinstance(l, str) else l[0] for l in lines]
    masked = mask_lines(contents)
    sec = pref_section(lines, masked)
    primary = {}
    if sec:
        ents = [e for e in section_entries(lines, sec, masked) if e.target]
        for e in sorted(ents, key=lambda x: (not x.canonical, x.idx)):
            primary.setdefault(nfc(e.target), e.idx)
    canon = dict(canonical_lines(lines, masked))
    refs = collections.OrderedDict()
    for i, t in link_lines(lines):
        if memory_type(memdir, t, cache) != 'feedback':
            continue
        refs.setdefault(nfc(t), []).append((i, t, i in canon))
    outside, extra = [], []
    for key, found in refs.items():
        if key in primary:
            extra += [(i + 1, t) for i, t, _ in found if i != primary[key]]
            continue
        first = next((x for x in found if x[2]), found[0])
        outside.append((first[0] + 1, first[1], first[2]))
        extra += [(i + 1, t) for i, t, c in found if (i, t, c) != first]
    outside.sort()
    extra.sort()
    return outside, extra


UNCLOSED_WHAT = {'HTML comment': 'an HTML comment', 'code fence': 'a code fence'}
UNCLOSED_CLOSE = {'HTML comment': '-->', 'code fence': 'a closing ``` (or ~~~) line'}


def unclosed_error(lines):
    """PmemError for an index with an unclosed comment or code fence (writers refuse), else None."""
    prob = unclosed_problem([l if isinstance(l, str) else l[0] for l in lines])
    if not prob:
        return None
    hides = ('hides the rest of the index from agents and from pmem' if prob[0] == 'HTML comment'
             else 'turns the rest of the index into code')
    return PmemError('MEMORY.md:%d opens %s that never closes, which %s; close it with %s and '
                     'rerun — nothing written' % (prob[1], UNCLOSED_WHAT[prob[0]], hides,
                                                   UNCLOSED_CLOSE[prob[0]]), 1)


def left_note(left):
    """Report line for references to feedback memories that pmem left in place."""
    shown = ', '.join('MEMORY.md:%d (%s)' % x for x in left[:5])
    return ('left %d other reference%s to feedback memories in place: %s%s — pmem only moves '
            '`- [title](name.md) — rule` lines; fix the others by hand (pmem lint)' % (
                len(left), '' if len(left) == 1 else 's', shown, ', …' if len(left) > 5 else ''))


def index_over_cap(lines):
    """Memory files the native loader no longer sees (past line 200 / 25,000 chars), or None if
    the index fits."""
    total = sum(len(c) + len(e) for c, e in lines)
    if len(lines) <= INDEX_MAX_LINES and total <= INDEX_MAX_CHARS:
        return None
    cut, chars = min(len(lines), INDEX_MAX_LINES), 0
    for i, (c, e) in enumerate(lines):
        chars += len(c) + len(e)
        if chars > INDEX_MAX_CHARS:
            cut = min(cut, i)
            break
    return [t for i, t in link_lines(lines) if i >= cut]


def migrate_index(memdir, lines, eol, mode=None):
    """1.1 migration of MEMORY.md lines in place. ``mode``: None (move into an existing section
    only), 'init' or 'add' (insert the section first, see insert_section). -> report lines."""
    report = []
    prob = unclosed_problem([c for c, _ in lines])
    if prob:
        report.append('warning: MEMORY.md:%d opens %s that never closes; preferences not migrated '
                      '— close it with %s' % (prob[1], UNCLOSED_WHAT[prob[0]],
                                              UNCLOSED_CLOSE[prob[0]]))
        return report
    if not pref_section(lines):
        if mode is None:
            return report
        if not insert_section(lines, eol, mode):
            report.append('hint: MEMORY.md has no HANDOFF line, so no ## Preferences section was added '
                          '— run `pmem index --fix`, then `pmem init` again')
            return report
        report.append('updated MEMORY.md: added the ## Preferences and ## Memories sections')
    moved, left = move_feedback_into_section(memdir, lines, eol)
    if moved:
        report.append('moved %d feedback line%s into Preferences — make each hook an imperative rule '
                      '(memory-health)' % (moved, '' if moved == 1 else 's'))
        if left:
            report.append(left_note(left))
    if moved or report:
        hidden = index_over_cap(lines)
        if hidden is not None:
            report.append('warning: MEMORY.md now has %d lines / %d chars, over the native load cap '
                          '(200 lines / 25,000 chars); agents no longer see: %s — shrink it '
                          '(memory-health)' % (len(lines), sum(len(c) + len(e) for c, e in lines),
                                               ', '.join(hidden) or 'the last lines'))
    return report


def remove_entry(lines, e):
    """Delete an entry with its continuation lines, except continuation lines that link to a
    memory (references are never deleted: they stay where they are); no double blank line is left
    behind. -> the removed lines."""
    tail = lines[e.idx + 1:e.end]
    linked = set(k for k, _ in link_lines(tail))
    removed = [lines[e.idx]] + [l for k, l in enumerate(tail) if k not in linked]
    kept = [l for k, l in enumerate(tail) if k in linked]
    lines[e.idx:e.end] = kept
    i = e.idx + len(kept)
    if 0 < i < len(lines) and not lines[i - 1][0].strip() and not lines[i][0].strip():
        del lines[i]
    return removed


def editable(e):
    """pmem edits, moves or removes only bare `- rule` entries and canonical index lines."""
    return not e.target or e.canonical


def not_canonical(e):
    """Why pmem leaves entry ``e`` alone (see editable)."""
    return ('the entry for %s at MEMORY.md:%d is not a `- [title](%s) — rule` line (pmem edits only '
            'such lines)' % (e.target, e.idx + 1, e.target))


def left_in_place(e):
    return 'note: %s; left it in place — fix it by hand' % not_canonical(e)


def entry_with_rule(e, rule):
    """The entry's line with its rule replaced (memory link and title kept)."""
    if not e.target:
        return '- ' + rule_md(rule)
    m = MD_LINK_RE.search(mask_lines([e.content])[0])
    return '- ' + e.content[m.start():m.end()] + ' — ' + rule_md(rule)


# ---------------------------------------------------------------------------------------------
# Preferences: outer folders (inheritance), registry

def owner_problem(memdir):
    """Why an outer (or registered) memory dir is not trusted, else None: the dir and its
    MEMORY.md must be yours and not group/world-writable, and no directory on the path may be
    world-writable without the sticky bit."""
    uid = os.getuid() if hasattr(os, 'getuid') else None
    for p in (memdir, os.path.join(memdir, 'MEMORY.md')):
        try:
            st = os.lstat(p)
        except OSError as e:
            return 'cannot stat %s (%s)' % (p, e.strerror or e)
        if stat.S_ISLNK(st.st_mode):
            return '%s is a symlink' % p
        if uid is not None and st.st_uid != uid:
            return '%s is owned by another user (uid %d)' % (p, st.st_uid)
        if st.st_mode & 0o022:
            return '%s is group- or world-writable (chmod go-w %s)' % (p, shell_quote(p))
    d = os.path.dirname(memdir)
    while True:
        try:
            st = os.stat(d)
        except OSError as e:
            return 'cannot stat %s (%s)' % (d, e.strerror or e)
        if st.st_mode & 0o002 and not st.st_mode & stat.S_ISVTX:
            return 'the directory %s on its path is world-writable without the sticky bit' % d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def outer_memdirs(root, home):
    """Memory dirs above the project root ``root``, nearest first: (used [memdir], skipped
    [(memdir, reason)]). HOME, its ancestors and / are never used (as in discovery)."""
    used, skipped = [], []
    if not root:
        return used, skipped
    d = os.path.dirname(os.path.realpath(root))
    while not rejected_root(d, home):
        cand = os.path.join(d, MEMDIR)
        if os.path.islink(cand):
            skipped.append((cand, 'it is a symlink (never followed for inherited preferences)'))
        elif os.path.isdir(cand) and os.path.lexists(os.path.join(cand, 'MEMORY.md')):
            why = memdir_problem(cand, d, home) or owner_problem(cand)
            if why:
                skipped.append((cand, why))
            else:
                used.append(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return used, skipped


def read_prefs(memdir):
    """Preference entries of a memory dir's MEMORY.md ([] when missing or unreadable)."""
    try:
        _, _, lines = read_index(memdir)
    except PmemError:
        return []
    return pref_entries(memdir, lines) if lines else []


def inherited_prefs(ctx):
    """-> (used [(memdir, entries)], skipped [(memdir, reason)]) for the project of ``ctx``."""
    used, skipped = outer_memdirs(ctx.root, ctx.home)
    return [(m, read_prefs(m)) for m in used], skipped


def cut(text, n):
    text = oneline(text)
    return text if len(text) <= n else text[:n - 1] + '…'


INHERIT_HEAD = ("## Inherited preferences (outer folders; this project's own Preferences win on "
                "conflict)")
OUTSIDE_HEAD = '## Preferences recorded outside the section'


def capped_section(head, items, max_chars=INHERIT_MAX_CHARS, max_lines=INHERIT_MAX_LINES):
    """``head`` + item lines within max_lines AND max_chars; the rest -> `- … N more — pmem pref
    list`. None when there are no items."""
    if not items:
        return None
    out = [head]
    size = len(head)
    for k, item in enumerate(items):
        rest = len(items) - k
        more = '- … %d more — pmem pref list' % rest
        reserve = 0 if rest == 1 else len(more) + 1
        if k >= max_lines or size + 1 + len(item) + reserve > max_chars:
            out.append(more)
            break
        out.append(item)
        size += 1 + len(item)
    return '\n'.join(out)


def inherited_lines(used):
    return ['- %s  (from %s)' % (cut(e.rule, INHERIT_RULE_CHARS), m)
            for m, entries in used for e in entries]


def outside_lines(memdir, lines):
    out = []
    for _, t, _canonical in feedback_outside(memdir, lines):
        mf = MemFile(os.path.join(memdir, t), memdir)
        out.append('- %s — %s' % (mf.name or t[:-3], cut(mf.description or first_nonblank(mf.body, 160),
                                                          INHERIT_RULE_CHARS)))
    return out


def registry_path():
    return os.path.join(os.path.dirname(stable_hook_path()), 'projects.txt')


def register_memdir(memdir):
    """Append the memory dir to the registry (one O_APPEND write; never rewritten). -> warning
    text or None."""
    path = registry_path()
    real = os.path.realpath(memdir)
    if '\n' in real or '\r' in real:
        return None
    try:
        try:
            if real in read_text(path).splitlines():
                return None
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0),
                     0o644)
        try:
            os.write(fd, enc(real + '\n'))
        finally:
            os.close(fd)
    except OSError as e:
        return 'cannot register %s in %s (%s); `pmem pref list --all` will not show it' % (
            real, path, e.strerror or e)
    return None


def registered_memdirs():
    """Registered memory dirs that still exist and pass the safety and ownership rules."""
    try:
        text = read_text(registry_path())
    except OSError:
        return []
    out, seen, home = [], set(), home_dir()
    for line in text.splitlines():
        p = line.strip()
        if not p or not os.path.isabs(p) or p in seen:
            continue
        seen.add(p)
        if not os.path.isdir(p) or os.path.islink(p) or os.path.basename(p) != MEMDIR:
            continue
        if memdir_problem(p, os.path.dirname(p), home) or owner_problem(p):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------------------------
# Preferences: the global block in CLAUDE.md / AGENTS.md

class GlobalFile(object):
    """A global instruction file with its project-memory block and preferences sub-block."""

    def __init__(self, path, error=None):
        self.path = path
        self.exists = False
        self.raw = b''
        self.error = error
        self.main = self.sub = None
        self.bom, self.lines, self.eol = False, [], '\n'
        if error:
            return
        try:
            self.raw = read_bytes(path)
            self.exists = True
        except FileNotFoundError:
            pass
        except OSError as e:
            self.error = 'cannot read it: %s' % (e.strerror or e)
            return
        self.bom, self.lines = text_lines(self.raw)
        self.eol = eol_of(self.lines)
        self.error = block_problem(self.lines)
        if self.error:
            return
        contents = [c for c, _ in self.lines]
        mb, me = marker_lines(contents, WIRE_BEGIN), marker_lines(contents, WIRE_END)
        sb, se = marker_lines(contents, PREF_BEGIN), marker_lines(contents, PREF_END)
        self.main = (mb[0], me[0]) if mb else None
        self.sub = (sb[0], se[0]) if sb else None

    def entries(self):
        """[(start, end, rule)] of the `- ` lines in the preferences block."""
        if not self.sub:
            return []
        out = []
        i, end = self.sub[0] + 1, self.sub[1]
        while i < end:
            c = self.lines[i][0]
            if c.startswith('- '):
                j = min(entry_block(self.lines, i)[1], end)
                out.append((i, j, c[2:].strip()))
                i = j
            else:
                i += 1
        return out

    def rules(self):
        return [r for _, _, r in self.entries()]

    def data(self, lines=None):
        return (b'\xef\xbb\xbf' if self.bom else b'') + enc(from_lines(lines or self.lines))


def block_problem(lines):
    """Why the project-memory markers of a file are malformed, else None (main block: one BEGIN
    before one END, or none; preferences block: the same, and outside the main block)."""
    contents = [c for c, _ in lines]
    flags = fence_flags(contents)
    fenced = [i for i, c in enumerate(contents)
              if flags[i] and c.strip() in (WIRE_BEGIN, WIRE_END, PREF_BEGIN, PREF_END)]
    if fenced:
        return ('project-memory markers inside a code block (line(s) %s) — fix by hand'
                % ', '.join(str(i + 1) for i in fenced))
    mb, me = marker_lines(contents, WIRE_BEGIN), marker_lines(contents, WIRE_END)
    sb, se = marker_lines(contents, PREF_BEGIN), marker_lines(contents, PREF_END)
    if (mb or me) and not (len(mb) == 1 and len(me) == 1 and mb[0] < me[0]):
        return ('unbalanced project-memory markers (BEGIN at line(s) %s; END at line(s) %s)'
                % (', '.join(str(i + 1) for i in mb) or 'none',
                   ', '.join(str(i + 1) for i in me) or 'none'))
    if (sb or se) and not (len(sb) == 1 and len(se) == 1 and sb[0] < se[0]):
        return ('malformed preferences block (BEGIN at line(s) %s; END at line(s) %s)'
                % (', '.join(str(i + 1) for i in sb) or 'none',
                   ', '.join(str(i + 1) for i in se) or 'none'))
    if mb and sb and not (se[0] < mb[0] or sb[0] > me[0]):
        return ('the preferences block (lines %d-%d) overlaps the project-memory block (lines %d-%d)'
                % (sb[0] + 1, se[0] + 1, mb[0] + 1, me[0] + 1))
    return None


def global_candidates(config_dirs=(), targets=(), only=False):
    """Global instruction files: Codex AGENTS.md, <cfg>/CLAUDE.md of $CLAUDE_CONFIG_DIR, ~/.claude
    and each --config-dir, then each --target; deduped by realpath. ``only``: just the
    --config-dir and --target files (all explicit). -> [(path, explicit)]"""
    if only:
        cands = [(os.path.join(os.path.abspath(os.path.expanduser(d)), 'CLAUDE.md'), True)
                 for d in config_dirs]
    else:
        cands = [(codex_agents_path(), False)]
        cands += [(os.path.join(d, 'CLAUDE.md'), False)
                  for d in claude_config_dirs(config_dirs, both=True)]
    cands += [(os.path.abspath(os.path.expanduser(t)), True) for t in targets]
    out, seen = [], {}
    for p, explicit in cands:
        r = os.path.realpath(p)
        if r in seen:
            if explicit:
                out[seen[r]] = (out[seen[r]][0], True)
            continue
        seen[r] = len(out)
        out.append((p, explicit))
    return out


def global_files(config_dirs=(), targets=(), only=False):
    """Read targets: candidates with the main block or the preferences block, plus every explicit
    --target. -> [GlobalFile] (an unreadable or malformed one carries ``error``)."""
    out = []
    for p, explicit in global_candidates(config_dirs, targets, only):
        if os.path.isdir(p):
            if explicit:
                out.append(GlobalFile(p, 'it is a directory'))
            continue
        g = GlobalFile(p)
        if explicit or g.error and g.exists or g.main or g.sub:
            out.append(g)
    return out


def global_divergence(files):
    """True if the normalized rule sets of the targets differ."""
    sets = [frozenset(norm_rule(r) for r in g.rules()) for g in files if not g.error]
    return len(set(sets)) > 1


def writable_problem(path):
    """Why ``path`` (written through a symlink, backed up next to the given path) cannot be
    replaced atomically, else None."""
    real = os.path.realpath(path)
    for d in sorted(set([os.path.dirname(real), os.path.dirname(os.path.abspath(path))])):
        if not os.access(d, os.W_OK | os.X_OK):
            return 'the directory %s is not writable' % d
    if os.path.exists(real) and not os.access(real, os.W_OK):
        return 'it is not writable'
    return None


def write_globals(plans):
    """Phase 2 of a global edit: back up and write each changed file. ``plans``: [(GlobalFile,
    new bytes)] that all passed phase 1. -> paths written."""
    done = []
    for g, new in plans:
        if new == g.raw:
            print('ok %s (unchanged)' % g.path)
            continue
        print('backup %s' % backup_file(g.path, g.raw))
        atomic_write(g.path, new)
        print('wrote %s' % g.path)
        done.append(g.path)
    return done


# ---------------------------------------------------------------------------------------------
# git

class GitResult(object):
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err


def git_env():
    env = dict(os.environ)
    for k in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX',
              'GIT_NAMESPACE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(k, None)
    env.update({'LC_ALL': 'C', 'LANGUAGE': '', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
    return env


def git(args, cwd=None, timeout=None, raw=False, env=None):
    """Run git; None if git is not installed. ``raw``: keep stdout as bytes; ``env``: extra vars."""
    full_env = git_env()
    full_env.update(env or {})
    try:
        p = subprocess.run(['git'] + list(args), cwd=cwd, env=full_env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout if timeout is not None else GIT_TIMEOUT)
    except FileNotFoundError:
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        return GitResult(-1, b'' if raw else '', str(e))
    return GitResult(p.returncode, p.stdout if raw else dec(p.stdout), dec(p.stderr))


PMEM_GIT = ['-c', 'user.name=pmem', '-c', 'user.email=pmem@localhost', '-c', 'commit.gpgsign=false',
            '-c', 'core.hooksPath=/dev/null', '-c', 'core.quotePath=false', '-c', 'gc.auto=0']


def mgit(memdir, args, raw=False):
    """git on the private history only: explicit --git-dir/--work-tree (never the project repo,
    even when the nested .git is broken) plus a ceiling at the memory dir's parent."""
    return git(['--git-dir=' + os.path.join(memdir, '.git'), '--work-tree=' + memdir] + PMEM_GIT
               + list(args), cwd=memdir, raw=raw,
               env={'GIT_CEILING_DIRECTORIES': os.path.dirname(memdir)})


class GitProbeError(Exception):
    pass


class GitInfo(object):
    """Result of the §4 probe. ``main`` is None when there is no main checkout."""

    def __init__(self):
        self.is_git = False
        self.top = self.gitdir = self.common = self.main = None
        self.linked = self.bare = False
        self.warnings = []
        self._worktrees = None

    def worktrees(self):
        """`git worktree list` entries (first = main/bare): dicts with path, real, bare."""
        if self._worktrees is None:
            self._worktrees = []
            if self.is_git:
                r = git(['-C', self.top, 'worktree', 'list', '--porcelain'])
                if r is not None and r.rc == 0:
                    self._worktrees = parse_worktrees(r.out)
        return self._worktrees

    def linked_worktree_paths(self):
        return [w['real'] for w in self.worktrees()[1:]]


def parse_worktrees(out):
    entries = []
    for line in out.split('\n'):
        if line.startswith('worktree '):
            p = line[len('worktree '):]
            entries.append({'path': p, 'real': os.path.realpath(p), 'bare': False})
        elif line == 'bare' and entries:
            entries[-1]['bare'] = True
    return entries


def git_probe(start):
    """Git probing per spec §4 (never uses --path-format)."""
    gi = GitInfo()
    r = git(['-C', start, 'rev-parse', '--show-toplevel'])
    if r is None:
        return gi
    if r.rc != 0:
        if r.rc == 128 and 'not a git repository' in r.err:
            return gi
        raise GitProbeError(r.err.strip() or 'git rev-parse --show-toplevel failed (rc %d)' % r.rc)
    top = r.out.split('\n')[0]
    if not top:
        raise GitProbeError('git rev-parse --show-toplevel printed nothing (not inside a work tree)')
    gi.is_git = True
    gi.top = os.path.realpath(top)
    r = git(['-C', gi.top, 'rev-parse', '--git-dir', '--git-common-dir'])
    lines = r.out.rstrip('\n').split('\n') if (r is not None and r.rc == 0) else []
    if len(lines) != 2 or not all(l and not l.startswith('-') and
                                  os.path.isdir(os.path.join(gi.top, l)) for l in lines):
        gi.warnings.append('git too old (need >= 2.13); treating %s as the main checkout' % gi.top)
        gi.main = gi.top
        return gi
    gi.gitdir = os.path.realpath(os.path.join(gi.top, lines[0]))
    gi.common = os.path.realpath(os.path.join(gi.top, lines[1]))
    if gi.gitdir == gi.common:
        gi.main = gi.top
        return gi
    gi.linked = True
    gdir = '--git-dir=' + gi.common
    r = git([gdir, 'rev-parse', '--is-bare-repository'], cwd=gi.common)
    gi.bare = r is not None and r.rc == 0 and r.out.strip() == 'true'
    parent = os.path.dirname(gi.common)
    if not gi.bare:
        if os.path.basename(gi.common) == '.git':
            gi.main = parent
        else:
            r = git([gdir, 'config', 'core.worktree'], cwd=gi.common)
            value = r.out.rstrip('\n') if (r is not None and r.rc == 0) else ''
            gi.main = os.path.realpath(os.path.join(gi.common, value)) if value else None
    else:
        r = git([gdir, 'worktree', 'list', '--porcelain'], cwd=gi.common)
        entries = parse_worktrees(r.out) if (r is not None and r.rc == 0) else []
        if entries and all(is_under(e['real'], parent) for e in entries):
            gi.main = parent
        else:
            gi.main = None
    return gi


def repo_top(path):
    """Top of the work tree containing ``path`` (None if not in a work tree)."""
    r = git(['-C', path, 'rev-parse', '--show-toplevel'])
    if r is None or r.rc != 0 or not r.out.strip():
        return None
    return os.path.realpath(r.out.split('\n')[0])


def repo_common_dir(top):
    r = git(['-C', top, 'rev-parse', '--git-common-dir'])
    if r is None or r.rc != 0:
        return None
    line = r.out.split('\n')[0]
    if not line or line.startswith('-'):
        return None
    p = os.path.realpath(os.path.join(top, line))
    return p if os.path.isdir(p) else None


def gitignore_escape(path):
    return re.sub(r'([*?\[\\])', r'\\\1', path)


def git_tracked(top, rel):
    r = git(['--literal-pathspecs', '-C', top, 'ls-files', '--', rel])
    return [l for l in r.out.split('\n') if l] if (r is not None and r.rc == 0) else []


def git_ignored(top, rel):
    r = git(['-C', top, 'check-ignore', '-q', '--', rel])
    return r is not None and r.rc == 0


def ensure_excluded(top, rel, out):
    """Append '/<rel>' to <common-dir>/info/exclude unless present or already ignored."""
    if '\n' in rel or '\r' in rel:
        out.append('warning: %r contains a line break, which an exclude line cannot express; '
                   'not excluded — add it to .gitignore yourself or rename the directory' % rel)
        return False
    line = '/' + gitignore_escape(rel.replace(os.sep, '/'))
    common = repo_common_dir(top)
    if common is None:
        out.append('warning: cannot locate the git dir of %s; add %s to .gitignore yourself'
                   % (top, line))
        return False
    exclude = os.path.join(common, 'info', 'exclude')
    try:
        text = read_text(exclude)
    except OSError:
        text = ''
    if line in [l.rstrip('\r') for l in text.split('\n')]:
        out.append('ok exclude %s' % line)
        return False
    if git_ignored(top, rel):
        out.append('ok %s already ignored in %s' % (rel, top))
        return False
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    if text and not text.endswith('\n'):
        text += '\n'
    atomic_write(exclude, enc(text + line + '\n'))
    out.append('excluded %s in %s' % (line, exclude))
    return True


# ---------------------------------------------------------------------------------------------
# Discovery

class Context(object):
    """Where we are: start dir, git info, memory dir, project/Claude roots, handoff key."""

    def __init__(self):
        self.start = self.logical = self.home = None
        self.git = GitInfo()
        self.memdir = self.root = self.claude_root = None
        self.bad_memdir = None   # (path, reason): a .agent-memory pmem refuses to use
        self.key = self.checkout_key = 'main'
        self.key_error = None


KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def start_dirs(arg_dir):
    if arg_dir:
        logical = os.path.abspath(os.path.expanduser(arg_dir))
        if not os.path.isdir(logical):
            raise PmemError('-C %s: not a directory' % arg_dir, 2)
    else:
        logical = os.getcwd()
        env_pwd = os.environ.get('PWD')
        if env_pwd and os.path.isabs(env_pwd):
            try:
                if os.path.samefile(env_pwd, logical):
                    logical = os.path.normpath(env_pwd)
            except OSError:
                pass
    return os.path.realpath(logical), logical


def memdir_problem(cand, project_dir, home):
    """Why the memory dir ``cand`` of ``project_dir`` is refused (e.g. a symlink shipped by a
    repository that points at HOME or at the project itself), else None."""
    real = os.path.realpath(cand)
    if rejected_root(real, home):
        return 'it resolves to %s: your home directory, an ancestor of it, or /' % real
    if is_under(os.path.realpath(project_dir), real):
        return 'it resolves to %s: the project directory itself or one of its ancestors' % real
    return None


def find_memdir(start, home, stop=None):
    """Walk upward from ``start`` (up to ``stop`` inclusive, if given); the first dir holding a
    DIRECTORY .agent-memory wins; stop at HOME and above. -> (memdir realpath, root, problem)."""
    d = start
    while True:
        if rejected_root(d, home):
            return None, None, None
        cand = os.path.join(d, MEMDIR)
        if os.path.isdir(cand):
            return os.path.realpath(cand), d, memdir_problem(cand, d, home)
        if stop is not None and d == stop:
            return None, None, None
        parent = os.path.dirname(d)
        if parent == d:
            return None, None, None
        d = parent


def resolve(arg_dir=None, for_init=False):
    real, logical = start_dirs(arg_dir)
    for p in (logical, real):  # inside a memory dir -> the project that holds it
        parts = p.split(os.sep)
        if MEMDIR in parts:
            base = os.sep.join(parts[:parts.index(MEMDIR)]) or os.sep
            real, logical = os.path.realpath(base), base
            break
    ctx = Context()
    ctx.start, ctx.logical, ctx.home = real, logical, home_dir()
    try:
        ctx.git = git_probe(real)
    except GitProbeError as e:
        if for_init:
            raise PmemError('git failed in %s:\n%s' % (real, e), 1)
        warn('git failed in %s (%s); continuing as not in git' % (real, e))
        ctx.git = GitInfo()
    for w in ctx.git.warnings:
        warn(w)
    gi = ctx.git
    if gi.is_git:
        rel = os.path.relpath(real, gi.top)
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            rel = ''
        walk_from = os.path.normpath(os.path.join(gi.main or gi.top, rel))
        if gi.linked and gi.main:
            # main checkout first, then a memory forced into this worktree (reported as an
            # error by status/lint), then the main checkout's ancestors
            found = find_memdir(walk_from, ctx.home, stop=gi.main)
            if not found[0]:
                found = find_memdir(real, ctx.home, stop=gi.top)
            parent = os.path.dirname(gi.main)
            if not found[0] and parent != gi.main:
                found = find_memdir(parent, ctx.home)
        else:
            found = find_memdir(walk_from, ctx.home)
    else:
        found = find_memdir(real, ctx.home)
    memdir, root, problem = found
    if problem:
        ctx.bad_memdir = (os.path.join(root, MEMDIR), problem)
    else:
        ctx.memdir, ctx.root = memdir, root
    ctx.claude_root = (gi.main or gi.top) if gi.is_git else (ctx.root or real)
    ctx.key = ctx.checkout_key = worktree_key(gi.top) if gi.linked else 'main'
    env_key = (os.environ.get('PMEM_KEY') or '').strip()
    if env_key:
        if KEY_RE.match(env_key):
            ctx.key = env_key
        else:
            ctx.key_error = 'PMEM_KEY must be kebab-case ([a-z0-9][a-z0-9-]*): %r' % env_key
    return ctx


def refused_memdir_error(ctx):
    path, why = ctx.bad_memdir
    return PmemError('refusing the memory dir %s: %s. pmem never reads or writes there; remove or '
                     'fix that %s entry' % (path, why, MEMDIR), 2)


def require_memory(ctx):
    if ctx.bad_memdir:
        raise refused_memdir_error(ctx)
    if not ctx.memdir:
        raise PmemError('no project memory (%s/) found from %s — run `pmem init` in the project root'
                        % (MEMDIR, ctx.start), 1)
    return ctx.memdir


def check_key(key):
    if key is None:
        return None
    if not KEY_RE.match(key):
        raise PmemError('--key must be kebab-case ([a-z0-9][a-z0-9-]*): %r' % key, 2)
    return key


def workstream_key(ctx, arg=None):
    """--key, else env PMEM_KEY, else the checkout's key (linked worktree name or ``main``)."""
    if arg is not None:
        return check_key(arg)
    if ctx.key_error:
        raise PmemError(ctx.key_error, 2)
    return ctx.key


def linked_worktree_containing(path, gi):
    if not path or not gi.is_git:
        return None
    for wt in gi.linked_worktree_paths():
        if is_under(path, wt):
            return wt
    return None


def history_state(memdir):
    """-> ('off' | 'on' | 'broken', reason). 'on' only for a valid repository at <memdir>/.git;
    a broken one is never used (git would otherwise fall back to the project repository)."""
    gd = os.path.join(memdir, '.git') if memdir else None
    if not gd or not os.path.lexists(gd):
        return 'off', None
    if os.path.islink(gd) or not os.path.isdir(gd):
        return 'broken', '%s is not a directory' % gd
    r = mgit(memdir, ['rev-parse', '--git-dir'])
    if r is None:
        return 'broken', 'git is not installed'
    if r.rc != 0:
        return 'broken', (r.err.strip().split('\n')[0] or 'git rev-parse failed') + ' (%s)' % gd
    return 'on', None


def git_lock_age(memdir):
    """Age in seconds of the private history's index.lock, None if there is none."""
    try:
        return max(0.0, time.time() - os.lstat(os.path.join(memdir, '.git', 'index.lock')).st_mtime)
    except OSError:
        return None


def snapshot(memdir, message):
    """Commit all changes in the private history, serialized under the memory lock.
    -> True committed, False clean, None n/a/failed."""
    if not memdir:
        return None
    state, reason = history_state(memdir)
    if state == 'off':
        return None
    if state == 'broken':
        warn('private history broken: %s — fix or move it away; pmem never commits anywhere else'
             % reason)
        return None
    try:
        with memory_lock(memdir):
            return snapshot_locked(memdir, message)
    except PmemError as e:
        warn('private history: %s' % e)
        return None


def snapshot_locked(memdir, message):
    age = git_lock_age(memdir)
    if age is not None and age > STALE_GIT_LOCK:
        lock = os.path.join(memdir, '.git', 'index.lock')
        try:
            os.unlink(lock)
            warn('removed a stale %s (%d min old; left by an interrupted git)' % (lock, age // 60))
        except OSError as e:
            warn('cannot remove the stale %s: %s' % (lock, e))
    st = mgit(memdir, ['status', '--porcelain'])
    if st is None or st.rc != 0:
        warn('private history: git status failed: %s' % (st.err.strip() if st else 'git missing'))
        return None
    if not st.out.strip():
        return False
    for args in (['add', '-A'], ['commit', '--no-verify', '-q', '-m', message]):
        r = mgit(memdir, args)
        if r is None or r.rc != 0:
            err = r.err.strip() if r else 'git missing'
            hint = ''
            if 'Read-only file system' in err:
                hint = (' (a sandbox keeps %s/.git read-only — e.g. Codex with `--add-dir <memory '
                        'dir>`; relaunch from the project root or with `--add-dir <project root>`)'
                        % memdir)
            warn('private history: git %s failed: %s%s' % (args[0], err, hint))
            return None
    return True


# ---------------------------------------------------------------------------------------------
# Handoffs and checkpoints

def handoff_path(memdir, key):
    if key == 'main':
        return os.path.join(memdir, 'HANDOFF.md')
    return os.path.join(memdir, 'handoffs', key + '.md')


def checkpoint_path(memdir, key, prev=False):
    return os.path.join(memdir, '.checkpoints', key + ('.prev.md' if prev else '.md'))


def render_handoff(key, by):
    text = (HANDOFF_TEMPLATE.replace('{now_utc}', now_utc_iso())
            .replace('{by}', by).replace('{key}', key))
    if key != 'main':
        text = text.replace('\nname: handoff\n', '\nname: handoff-%s\n' % key, 1)
    return text


def list_handoffs(memdir):
    """[(key, path)] of the handoffs (symlinks are skipped: never followed)."""
    out = []
    main = os.path.join(memdir, 'HANDOFF.md')
    if os.path.isfile(main) and not os.path.islink(main):
        out.append(('main', main))
    hdir = os.path.join(memdir, 'handoffs')
    if os.path.islink(hdir):
        return out
    try:
        names = sorted(os.listdir(hdir))
    except OSError:
        names = []
    for fn in names:
        p = os.path.join(hdir, fn)
        if fn.endswith('.md') and not fn.startswith('.') and os.path.isfile(p) \
                and not os.path.islink(p):
            out.append((fn[:-3], p))
    return out


def handoff_info(key, path, memdir=None):
    mf = MemFile(path, memdir)
    ts, _, shown = mf.handoff_timestamp()
    return {'key': key, 'path': path, 'ts': ts, 'modified': shown,
            'by': (mf.fm.by if mf.fm else '') or '-'}


CHECKPOINT_TAIL_HEAD = '## Last assistant message (tail)'


def read_checkpoint(memdir, key, prev=False):
    """-> dict(text, ts, time, event, error, session_id, transcript, tail, prev) or None if
    missing/unreadable. ``ts`` is the checkpoint's own ``- time:`` (file mtime if unparseable)."""
    p = checkpoint_path(memdir, key, prev)
    try:
        guard(memdir, p)
        text = read_text(p)
        mtime = os.lstat(p).st_mtime
    except (OSError, PmemError):
        return None
    fields = {}
    for line in text.split('\n'):
        if line.startswith('## '):
            break
        m = re.match(r'^- ([a-z_ ]+):[ \t]*(.*?)\s*$', line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
    for k, v in list(fields.items()):
        if v == '-':
            fields[k] = ''
    tail = ''
    if CHECKPOINT_TAIL_HEAD in text:
        tail = text.split(CHECKPOINT_TAIL_HEAD, 1)[1].strip('\n')
    ts = parse_ts(fields.get('time'))
    return {'text': text, 'ts': mtime if ts is None else ts, 'path': p, 'prev': prev,
            'time': fields.get('time') or fmt_epoch(mtime),
            'event': fields.get('event', ''),
            'error': fields.get('error', '').split(' — ', 1)[0].strip(),
            'session_id': fields.get('session_id', ''),
            'transcript': fields.get('transcript', ''),
            'tail': tail}


def checkpoint_state(memdir, key, handoff_ts, prev=False):
    """A checkpoint (current, or ``prev``: the one before a different session took over)
    classified against its handoff (None if there is none):
    ``failure`` — a StopFailure (the session stopped on an error) not older than the handoff;
    ``stale``   — more than CHECKPOINT_GRACE after the handoff (or no handoff): the handoff lags;
    ``normal``  — anything else: the Stop hook fires after every turn, so a checkpoint slightly
                  newer than the handoff just means the session ended normally."""
    ck = read_checkpoint(memdir, key, prev)
    if ck is None:
        return None
    if ck['event'] == 'StopFailure' and (handoff_ts is None or ck['ts'] + 1 > handoff_ts):
        ck['state'] = 'failure'
    elif handoff_ts is None or ck['ts'] > handoff_ts + CHECKPOINT_GRACE:
        ck['state'] = 'stale'
    else:
        ck['state'] = 'normal'
    ck['handoff_ts'] = handoff_ts
    return ck


def checkpoint_section(ck, earlier_pending=False):
    """Text for assemble_context: the full checkpoint (failure/stale) or one line (normal).
    ``earlier_pending``: an earlier session's failure/stale checkpoint follows, so the one-line
    form must not claim that the handoff is current."""
    sid, tr = ck['session_id'][:8] or '-', ck['transcript'] or '-'
    if ck['state'] == 'normal':
        if ck['event'] == 'Stop':
            tail = ('; the earlier checkpoint below still needs reconciling.' if earlier_pending
                    else '; handoff is current.')
            return ('Last session ended normally at %s (session %s, transcript %s)%s'
                    % (ck['time'], sid, tr, tail))
        if ck['event'] == 'StopFailure':
            return ('Last checkpoint: StopFailure (%s) at %s (session %s, transcript %s); the '
                    'handoff was updated after it.' % (ck['error'] or 'unknown error', ck['time'],
                                                      sid, tr))
        return ('Last checkpoint: %s at %s (session %s, transcript %s).'
                % (ck['event'] or 'unknown event', ck['time'], sid, tr))
    which = (' (earlier session, kept in %s/.checkpoints/%s when another session took over)'
             % (MEMDIR, os.path.basename(ck['path'])) if ck['prev'] else '')
    if ck['state'] == 'failure':
        head = ('## Auto checkpoint%s: the previous session stopped on an error (%s) at %s — '
                'reconcile before continuing' % (which, ck['error'] or 'unknown error', ck['time']))
    else:
        head = ('## Auto checkpoint%s newer than the handoff — the previous session may have ended '
                'abruptly; reconcile before continuing' % which)
    return head + '\n\n' + ck['text'].rstrip('\n')


def checkpoint_sections(memdir, key, handoff_ts):
    """Sections for the current checkpoint (full or one line) and, when it still matters (a
    failure or a lagging handoff), the previous session's checkpoint."""
    out = []
    cur = checkpoint_state(memdir, key, handoff_ts)
    prev = checkpoint_state(memdir, key, handoff_ts, prev=True)
    pending = bool(prev and prev['state'] in ('failure', 'stale'))
    if cur:
        out.append(checkpoint_section(cur, earlier_pending=pending))
    if pending:
        out.append(checkpoint_section(prev))
    return out


def worktree_keys(gi):
    return set(worktree_key(w['real']) for w in gi.worktrees()[1:]) if gi.is_git else None


# ---------------------------------------------------------------------------------------------
# Claude wiring (settings.local.json)

def settings_path(root):
    return os.path.join(root, '.claude', 'settings.local.json')


def read_settings(path):
    """-> (dict or None when missing, error string or None)."""
    try:
        raw = read_bytes(path)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, str(e)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None, 'not valid UTF-8'
    if not text.strip():
        return {}, None
    try:
        obj = json.loads(text, object_pairs_hook=collections.OrderedDict)
    except ValueError as e:
        return None, 'invalid JSON (%s)' % e
    if not isinstance(obj, dict):
        return None, 'not a JSON object'
    return obj, None


def stable_hook_path():
    """${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py; a relative XDG_DATA_HOME is
    ignored (XDG base directory spec)."""
    base = os.environ.get('XDG_DATA_HOME') or ''
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(os.path.abspath(base), 'project-memory', 'pmem.py')


def cli_path():
    """Script path to show agents: the stable hook copy, else this file."""
    stable = stable_hook_path()
    return stable if os.path.isfile(stable) else os.path.realpath(__file__)


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def hook_command(sub):
    return "python3 %s hook %s 2>/dev/null || true  %s" % (shell_quote(stable_hook_path()), sub,
                                                            HOOK_MARKER)


def is_our_hook(h):
    return isinstance(h, dict) and HOOK_MARKER in str(h.get('command', ''))


def our_hook_events(obj):
    hooks = obj.get('hooks') if isinstance(obj, dict) else None
    found = set()
    if isinstance(hooks, dict):
        for ev, groups in hooks.items():
            for g in groups if isinstance(groups, list) else []:
                if isinstance(g, dict) and isinstance(g.get('hooks'), list):
                    if any(is_our_hook(h) for h in g['hooks']):
                        found.add(ev)
    return found


def plan_claude_wiring(root, memdir, hooks=True, allow_dream=False, force=False):
    """Validate and compute new settings. -> (path, old obj or None, new obj, notes). Raises."""
    path = settings_path(root)
    obj, err = read_settings(path)
    if err:
        raise PmemError('%s: %s — fix or remove it; nothing written' % (path, err), 1)
    notes = []
    top = repo_top(root)
    if top:
        rel = os.path.relpath(path, top)
        if git_tracked(top, rel):
            if not force:
                raise PmemError('%s is tracked by git; writing machine-specific paths there would leak '
                                'them to collaborators. Untrack it (git rm --cached) or rerun with '
                                '--force' % path, 2)
            notes.append('warning: %s is tracked by git (--force)' % path)
    new = copy.deepcopy(obj) if obj is not None else collections.OrderedDict()
    old = new.get('autoMemoryDirectory')
    if old is not None and not same_path(old, memdir):
        if not force:
            raise PmemError('%s: autoMemoryDirectory already points to %s, not %s. Rerun with --force '
                            'to replace it, then `pmem import %s`' % (path, old, memdir, old), 2)
        oldp = os.path.expanduser(str(old)) if isinstance(old, str) else str(old)
        notes.append('replaced autoMemoryDirectory (was %s) — import it with: pmem import %s'
                     % (old, shell_quote(oldp)))
        new['autoMemoryDirectory'] = memdir
    elif old is None:
        new['autoMemoryDirectory'] = memdir
    if not allow_dream:
        new['autoDreamEnabled'] = False
    had_hooks = 'hooks' in new
    hk = new.get('hooks')
    if hk is not None and not isinstance(hk, dict):
        raise PmemError('%s: "hooks" is not an object; nothing written' % path, 1)
    hk = hk if hk is not None else collections.OrderedDict()
    was_empty = had_hooks and not hk
    for ev in list(hk.keys()):
        groups = hk[ev]
        if not isinstance(groups, list):
            continue
        kept, changed = [], False
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get('hooks'), list):
                hs = [h for h in g['hooks'] if not is_our_hook(h)]
                if len(hs) != len(g['hooks']):
                    changed = True
                    if not hs:
                        continue
                    g = copy.copy(g)
                    g['hooks'] = hs
            kept.append(g)
        if changed:
            if kept:
                hk[ev] = kept
            else:
                del hk[ev]
    if hooks:
        for ev, matcher, sub, timeout in HOOK_EVENTS:
            group = collections.OrderedDict()
            if matcher:
                group['matcher'] = matcher
            group['hooks'] = [collections.OrderedDict([('type', 'command'),
                                                       ('command', hook_command(sub)),
                                                       ('timeout', timeout)])]
            if ev in hk and not isinstance(hk[ev], list):
                raise PmemError('%s: hooks.%s is not a list; nothing written' % (path, ev), 1)
            hk.setdefault(ev, []).append(group)
    if hk or was_empty:
        new['hooks'] = hk
    elif had_hooks:
        del new['hooks']
    return path, obj, new, notes


def write_claude_settings(plan, out):
    path, old, new, notes = plan
    out.extend(notes)
    if old is not None and old == new:
        out.append('ok %s' % path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        data = (json.dumps(new, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    except UnicodeEncodeError:
        data = (json.dumps(new, indent=2) + '\n').encode('utf-8')
    atomic_write(path, data)
    out.append('%s %s' % ('updated' if old is not None else 'created', path))


def hook_script_ready(out, cache, force=False):
    """Install the stable hook copy (once per run) BEFORE any hook entry points to it.
    -> True if a hook script is in place."""
    if 'ready' not in cache:
        dest = stable_hook_path()
        try:
            install_stable_copy(out, force)
            cache['ready'] = True
        except OSError as e:
            cache['ready'] = os.path.isfile(dest) and not os.path.islink(dest)
            out.append('warning: cannot install the hook script %s (%s); %s' % (
                dest, e, 'the hooks keep using the existing, outdated copy' if cache['ready'] else
                'Claude wiring is written WITHOUT hooks (no checkpoints, no session-start context) '
                '— make the directory writable and rerun `pmem init`'))
    return cache['ready']


def wire_claude_root(root, memdir, args, out, cache, plan=None):
    """§5 wiring of one Claude root: hook script first, then settings.local.json."""
    hooks = not args.no_hooks and hook_script_ready(out, cache, args.force)
    if plan is None or hooks != (not args.no_hooks):
        plan = plan_claude_wiring(root, memdir, hooks=hooks, allow_dream=args.allow_dream,
                                  force=args.force)
    write_claude_settings(plan, out)


VERSION_RE = re.compile(rb'(?m)^VERSION = [\'"]([0-9][0-9A-Za-z.+-]*)[\'"]')


def script_version(path):
    """VERSION of a pmem.py copy (None if missing, unreadable or without one)."""
    try:
        with open(path, 'rb') as f:
            m = VERSION_RE.search(f.read(65536))
    except OSError:
        return None
    return m.group(1).decode('ascii', 'replace') if m else None


def version_key(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or '')[:3]) if v else ()


def install_stable_copy(out, force=False):
    """Copy this script to the stable hook path (atomic, 0755). A symlink there is replaced by a
    regular file, never written through; a copy with a NEWER version is kept unless ``force``.
    Raises OSError when the location is not writable."""
    dest = stable_hook_path()
    try:
        src = read_bytes(os.path.realpath(__file__))
    except OSError as e:
        out.append('warning: cannot read own source for the hook copy: %s' % e)
        return
    link = os.path.islink(dest)
    try:
        same = not link and read_bytes(dest) == src
    except OSError:
        same = False
    if same:
        out.append('ok hook script %s' % dest)
        return
    theirs = None if link else script_version(dest)
    if theirs and version_key(theirs) > version_key(VERSION) and not force:
        out.append('warning: kept the hook script %s: it is pmem %s, newer than this pmem %s — run '
                   '`pmem init` with the newer copy (or `pmem init --force` to downgrade)'
                   % (dest, theirs, VERSION))
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    atomic_write(dest, src, mode=0o755, follow=False)
    out.append('%s hook script %s' % ('installed (replacing a symlink)' if link else 'installed',
                                      dest))


def hook_script_paths(obj):
    """Scripts our hook commands run (``python3 '<path>' hook ...``)."""
    paths = []
    hooks = obj.get('hooks') if isinstance(obj, dict) else None
    for groups in (hooks.values() if isinstance(hooks, dict) else []):
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get('hooks') if isinstance(g, dict) else None) or []:
                if not is_our_hook(h):
                    continue
                try:
                    tokens = shlex.split(str(h.get('command', '')))
                except ValueError:
                    continue
                if len(tokens) >= 2 and tokens[1] not in paths:
                    paths.append(tokens[1])
    return paths


def claude_wiring_state(root, memdir):
    """-> dict(state=ok|missing|elsewhere|invalid, path, value, hooks, script)."""
    path = settings_path(root)
    obj, err = read_settings(path)
    st = {'path': path, 'value': None, 'hooks': 'absent', 'state': 'missing', 'error': err,
          'script': None, 'prompt_hook': False}
    if err:
        st['state'] = 'invalid'
        return st
    obj = obj or {}
    v = obj.get('autoMemoryDirectory')
    st['value'] = v
    if v is None:
        st['state'] = 'missing'
    elif memdir and same_path(v, memdir):
        st['state'] = 'ok'
    else:
        st['state'] = 'elsewhere'
    events = our_hook_events(obj)
    n = len(events & set(e[0] for e in HOOK_EVENTS))
    st['hooks'] = 'present' if n == len(HOOK_EVENTS) else ('partial' if n else 'absent')
    st['prompt_hook'] = 'UserPromptSubmit' in events
    if n:
        missing = [p for p in hook_script_paths(obj) if not os.path.isfile(p)]
        st['script'] = ('missing (%s)' % ', '.join(missing)) if missing else 'ok'
    return st


def claude_config_dirs(extra=None, both=False):
    env = os.environ.get('CLAUDE_CONFIG_DIR')
    default = os.path.join(os.path.expanduser('~'), '.claude')
    dirs = ([env, default] if both else [env or default]) + list(extra or [])
    out, seen = [], set()
    for d in dirs:
        if not d:
            continue
        d = os.path.abspath(os.path.expanduser(d))
        r = os.path.realpath(d)
        if r not in seen:
            seen.add(r)
            out.append(d)
    return out


def settings_layers(root):
    """Claude settings files that may set autoMemoryEnabled/autoDreamEnabled, highest first."""
    return (('local', settings_path(root)),
            ('project', os.path.join(root, '.claude', 'settings.json')),
            ('user', os.path.join(claude_config_dirs()[0], 'settings.json')))


def native_memory_off(ctx):
    """Why Claude Code's native auto-memory does not load this memory dir here ([] = it does):
    env switches, an override pointing elsewhere, merged ``autoMemoryEnabled: false``."""
    eff = []
    if truthy_env('CLAUDE_CODE_DISABLE_AUTO_MEMORY'):
        eff.append('disabled by CLAUDE_CODE_DISABLE_AUTO_MEMORY')
    if truthy_env('CLAUDE_CODE_SIMPLE'):
        eff.append('disabled by CLAUDE_CODE_SIMPLE (--bare)')
    override = os.environ.get('CLAUDE_COWORK_MEMORY_PATH_OVERRIDE')
    if override and not (ctx.memdir and same_path(override, ctx.memdir)):
        eff.append('overridden by CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=%s' % override)
    for label, path in settings_layers(ctx.claude_root):  # the first layer setting the key wins
        obj, _ = read_settings(path)
        if isinstance(obj, dict) and 'autoMemoryEnabled' in obj:
            if obj.get('autoMemoryEnabled') is False:
                eff.append('autoMemoryEnabled: false in %s settings (%s)' % (label, path))
            break
    return eff


def track_intended(memdir):
    """--track mode: the memory's own .gitignore keeps HANDOFF.md out of the project repo."""
    try:
        path = guard(memdir, os.path.join(memdir, '.gitignore'))
        return 'HANDOFF.md' in [l.strip() for l in read_text(path).split('\n')]
    except (OSError, PmemError):
        return False


def tracked_in_refs(ctx):
    """Private mode only: short sha of a commit in ANY ref of the project repository that tracks
    the memory dir (a checkout/pull of it overwrites the ignored private files), else None."""
    if not ctx.memdir or not ctx.root or track_intended(ctx.memdir):
        return None
    top = repo_top(ctx.root)
    if not top:
        return None
    rel = os.path.relpath(os.path.join(ctx.root, MEMDIR), top)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    r = git(['--literal-pathspecs', '-C', top, 'log', '--all', '-1', '--format=%h', '--', rel],
            timeout=GIT_TIMEOUT or REF_SCAN_TIMEOUT)
    if r is None or r.rc != 0:
        return None
    return r.out.strip() or None


def ref_hazard(sha, ctx):
    return ('commit %s in the project repository tracks %s/ while this memory is private: a git '
            'checkout or pull of such commits overwrites (and later deletes) the private files; '
            '`pmem restore` recovers up to the last snapshot — keep memory either private or '
            'tracked (--track), not both' % (sha, os.path.relpath(os.path.join(ctx.root, MEMDIR),
                                                                  repo_top(ctx.root) or ctx.root)))


def claude_root_refused(ctx):
    return bool(ctx.claude_root) and rejected_root(ctx.claude_root, ctx.home)


def claude_root_warning(ctx):
    return ('the Claude root %s is your home directory or above (it is a git work tree), so pmem '
            'does not wire it: Claude Code settings there would apply to every project below it; '
            '`git init` this project to give it its own root' % ctx.claude_root)


def prompt_hook_warning(path):
    return ('the UserPromptSubmit hook entry (preference capture) is missing in %s — run pmem init'
            % path)


def version_warnings():
    """The stable hook copy's VERSION differs from this CLI's."""
    stable = stable_hook_path()
    if not os.path.isfile(stable) or os.path.realpath(stable) == os.path.realpath(__file__):
        return []
    theirs = script_version(stable)
    if theirs == VERSION:
        return []
    return ['the hook script %s is pmem %s but this CLI is pmem %s — update every installed copy, '
            'then run pmem init with the newest one' % (stable, theirs or 'of unknown version',
                                                        VERSION)]


def wiring_warnings(ctx):
    w = []
    if claude_root_refused(ctx):
        w.append(claude_root_warning(ctx))
    else:
        st = claude_wiring_state(ctx.claude_root, ctx.memdir)
        if st['state'] == 'missing':
            w.append('Claude Code native memory is not redirected here (%s has no '
                     'autoMemoryDirectory) — run `pmem init`' % st['path'])
        elif st['state'] == 'elsewhere':
            w.append('Claude Code autoMemoryDirectory in %s points to %s, not %s — run `pmem status`'
                     % (st['path'], st['value'], ctx.memdir))
        elif st['state'] == 'invalid':
            w.append('%s: %s' % (st['path'], st['error']))
        if st['hooks'] != 'absent' and not st['prompt_hook']:
            w.append(prompt_hook_warning(st['path']))
    w += version_warnings()
    wt = linked_worktree_containing(ctx.root, ctx.git)
    if wt:
        w.append('the memory dir %s is inside the linked worktree %s; `git worktree remove` would '
                 'delete it — move it to the main checkout' % (ctx.memdir, wt))
    sha = tracked_in_refs(ctx)
    if sha:
        w.append(ref_hazard(sha, ctx))
    return w


# ---------------------------------------------------------------------------------------------
# Context assembly (hook session-start, context, handoff show)

TRUNCATED_NOTE = '\n[... truncated by pmem; run `pmem context` for the full text]'


def fit(body, room):
    if len(body) <= room:
        return body
    return body[:max(0, room - len(TRUNCATED_NOTE))] + TRUNCATED_NOTE


def pref_context_parts(ctx):
    """[inherited preferences section, outside-section list, skipped outer memories] (the first
    two None when empty)."""
    used, skipped = inherited_prefs(ctx)
    inherited = capped_section(INHERIT_HEAD, inherited_lines(used))
    try:
        _, _, lines = read_index(ctx.memdir)
    except PmemError:
        lines = None
    outside = capped_section(OUTSIDE_HEAD, outside_lines(ctx.memdir, lines)) if lines else None
    return inherited, outside, skipped


def assemble_context(ctx, key, with_index=False, with_others=True, with_warnings=True, cap=None,
                     native_check=False, with_prefs=False):
    """Handoff, checkpoint(s), other handoffs, inherited preferences, pointer line (+ MEMORY.md).
    With ``cap`` the fixed parts are first trimmed to cap/2, then only the handoff and index
    bodies are truncated: the handoff always survives, so do the pointer line and (shortened)
    warnings, checkpoints and the list of other handoffs."""
    memdir = ctx.memdir
    warns = wiring_warnings(ctx) if with_warnings else []
    if native_check:
        off = native_memory_off(ctx)
        if off:
            with_index = True
            warns.append('Claude Code native memory is off here (%s): MEMORY.md is appended below; '
                         'open single memories in %s when relevant' % ('; '.join(off), memdir))
    hp = handoff_path(memdir, key)
    rel = os.path.relpath(hp, memdir)
    hts = None
    try:
        guard(memdir, hp)
        present = os.path.isfile(hp)
    except PmemError:
        present = None
    if present is None:
        hbody = ('(refused: %s/%s is or lies behind a symlink inside the memory dir; pmem never '
                 'follows it — replace it with a regular file)' % (MEMDIR, rel))
    elif present:
        mf = MemFile(hp, memdir)
        hts, _, _ = mf.handoff_timestamp()
        hbody = mf.text.rstrip('\n') if not mf.error else '(cannot read it: %s)' % mf.error
    else:
        hbody = ('(no handoff for workstream "%s" yet — create it with `pmem handoff path`)' % key)
    hhead = '## Current handoff (%s/%s)\n\n' % (MEMDIR, rel)
    # fixed parts in trimming order (least important first); the pointer line is never trimmed
    fixed = collections.OrderedDict()
    fixed['warnings'] = '\n'.join('WARNING (project memory): ' + w for w in warns) or None
    fixed['checkpoints'] = '\n\n'.join(checkpoint_sections(memdir, key, hts)) or None
    if with_others:
        others = [handoff_info(k, p, memdir) for k, p in list_handoffs(memdir) if k != key]
        if others:
            shown = others[:OTHER_HANDOFFS_MAX] if cap else others
            lines = ['- %s (%s/%s) — modified %s — by %s' % (
                h['key'], MEMDIR, os.path.relpath(h['path'], memdir), h['modified'], h['by'])
                for h in shown]
            if len(others) > len(shown):
                lines.append('- ... and %d more — `pmem handoff list`' % (len(others) - len(shown)))
            fixed['others'] = '## Other handoffs\n' + '\n'.join(lines)
    if with_prefs:
        try:
            fixed['inherited'], fixed['outside'], skipped = pref_context_parts(ctx)
            if skipped:
                warns.append('outer memory %s is skipped, so its preferences are not inherited: %s%s'
                             % (skipped[0][0], skipped[0][1], ' (and %d more — pmem pref list)'
                                % (len(skipped) - 1) if len(skipped) > 1 else ''))
                fixed['warnings'] = '\n'.join('WARNING (project memory): ' + w for w in warns)
        except Exception as e:   # never lose the handoff over the preferences
            fixed['inherited'] = '%s\n(cannot list them: %s — run `pmem pref list`)' % (
                INHERIT_HEAD, e)
    pointer = ('Project memory: %s — CLI: python3 %s (pmem); follow the project-memory skill; update '
               'the handoff at milestones.' % (memdir, shell_quote(cli_path())))
    ibody = None
    if with_index:
        try:
            text, _, _ = read_index(memdir)
        except PmemError as e:
            text = '(%s)' % e
        if text is not None:
            ibody = text.lstrip('\ufeff').rstrip('\n')

    def render(hb, ib):
        parts = [fixed['warnings']] if fixed['warnings'] else []
        parts.append(hhead + hb)
        parts += [fixed[k] for k in ('checkpoints', 'others', 'inherited', 'outside')
                  if fixed.get(k)]
        parts.append(pointer)
        if ib is not None:
            parts.append('## MEMORY.md\n\n' + ib)
        return '\n\n'.join(parts)

    if cap:
        budget = cap // 2
        for k in ('outside', 'inherited', 'others', 'checkpoints', 'warnings'):
            excess = len(render('', '' if ibody is not None else None)) - budget
            if excess <= 0:
                break
            part = fixed.get(k)
            if part:
                fixed[k] = fit(part, max(len(TRUNCATED_NOTE) + 80, len(part) - excess))
        room = cap - len(render('', '' if ibody is not None else None))
        hbody = fit(hbody, room)
        if ibody is not None:
            ibody = fit(ibody, room - len(hbody))
    return render(hbody, ibody)


# ---------------------------------------------------------------------------------------------
# Commands: init

def drop_group_write(path):
    """chmod go-w a file or dir of yours that is group/world-writable (outer folders' preferences
    are only inherited from memory dirs that are not). -> report line or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not hasattr(os, 'getuid') or st.st_uid != os.getuid() \
            or not st.st_mode & 0o022:
        return None
    old = stat.S_IMODE(st.st_mode)
    try:
        os.chmod(path, old & ~0o022)
    except OSError as e:
        return ('warning: %s is group- or world-writable and chmod go-w failed (%s); projects below '
                'it will not inherit its preferences' % (path, e.strerror or e))
    return ('chmod go-w %s (%04o -> %04o): projects below inherit preferences only from memory '
            'dirs that are not group/world-writable' % (path, old, old & ~0o022))


class Out(list):
    """Action log that prints as it goes (warnings/errors to stderr)."""

    def append(self, line):
        if line.startswith(('warning:', 'error:')):
            eprint('pmem: ' + line)
        else:
            print(line)
        list.append(self, line)

    def extend(self, lines):
        for line in lines:
            self.append(line)


def init_gitignore(memdir, track, out):
    path = os.path.join(memdir, '.gitignore')
    want = list(GITIGNORE_LINES) + (list(GITIGNORE_TRACK_LINES) if track else [])
    try:
        text = read_text(guard(memdir, path))
    except FileNotFoundError:
        text = None
    have = set(l.strip() for l in (text or '').split('\n'))
    missing = [l for l in want if l not in have]
    if not missing:
        out.append('ok .gitignore')
        return
    lines = to_lines(text or '')
    eol = eol_of(lines)
    for l in missing:
        append_line(lines, l, eol)
    atomic_write(path, enc(from_lines(lines)), memdir=memdir)
    out.append('%s %s (%s)' % ('updated' if text is not None else 'created', path, ', '.join(missing)))


def wire_children(root, memdir, args, out, wired, cache):
    """Non-git workspace: wire child repos (<= 2 levels) whose main root is inside the workspace."""
    problems = 0
    done = set(os.path.realpath(w) for w in wired)
    cands = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if name.startswith('.') or name == 'node_modules' or not os.path.isdir(p):
            continue
        if os.path.exists(os.path.join(p, '.git')):
            cands.append(p)
            continue
        try:
            subs = sorted(os.listdir(p))
        except OSError:
            continue
        for sub in subs:
            q = os.path.join(p, sub)
            if sub.startswith('.') or sub == 'node_modules' or not os.path.isdir(q):
                continue
            if os.path.exists(os.path.join(q, '.git')):
                cands.append(q)
    for child in cands:
        if os.path.isdir(os.path.join(child, MEMDIR)):
            out.append('skip child %s (has its own %s)' % (child, MEMDIR))
            continue
        try:
            gi = git_probe(child)
        except GitProbeError as e:
            out.append('skip child %s (git: %s)' % (child, e))
            continue
        if not gi.is_git:
            continue
        main = os.path.realpath(gi.main) if gi.main else None
        if main is None or not is_under(main, os.path.realpath(root)):
            out.append('skip child %s (main checkout outside the workspace)' % child)
            continue
        if main in done:
            continue
        done.add(main)
        if os.path.isdir(os.path.join(main, MEMDIR)):
            out.append('skip child %s (has its own %s)' % (main, MEMDIR))
            continue
        try:
            plan = plan_claude_wiring(main, memdir, hooks=not args.no_hooks,
                                      allow_dream=args.allow_dream, force=args.force)
        except PmemError as e:
            out.append('error: child %s: %s' % (main, e))
            problems += 1
            continue
        out.append('child repo %s:' % main)
        wire_claude_root(main, memdir, args, out, cache, plan)
        top = repo_top(main)
        if top:
            ensure_excluded(top, os.path.relpath(settings_path(main), top), out)
    return problems


def cmd_init(args):
    ctx = resolve(args.C, for_init=True)
    gi = ctx.git
    out = Out()
    if ctx.bad_memdir and not args.here:
        raise refused_memdir_error(ctx)
    reuse = bool(ctx.memdir) and not args.here
    if reuse:
        memdir, root = ctx.memdir, ctx.root
        print('reusing %s' % memdir)
    else:
        if args.here:
            root = ctx.start
        elif gi.is_git:
            if gi.main is None:
                raise PmemError('%s is a linked worktree of a bare repository without a container '
                                'layout, so there is no main checkout to hold the memory. Run '
                                '`pmem -C <dir> init --here` for a directory outside the worktrees, '
                                'or `pmem init --here --force` to keep it in this worktree' % gi.top, 2)
            root = gi.main
        else:
            root = ctx.start
        memdir = os.path.join(root, MEMDIR)
    if rejected_root(root, ctx.home):
        raise PmemError('refusing to create project memory in %s (home directory, an ancestor of it, '
                        'or /); use -C <project>' % root, 2)
    if os.path.lexists(memdir):
        problem = memdir_problem(memdir, root, ctx.home)
        if problem:
            raise PmemError('refusing the memory dir %s: %s. pmem never reads or writes there; '
                            'remove or fix that %s entry' % (memdir, problem, MEMDIR), 2)
    wt = linked_worktree_containing(os.path.realpath(root), gi)
    if wt and not args.force:
        raise PmemError('%s is inside the linked worktree %s. `git worktree remove` deletes ignored '
                        'files, so the memory would be lost with it. Run init from the main checkout '
                        '(or rerun with --force)' % (root, wt), 2)
    if os.path.lexists(memdir) and not os.path.isdir(memdir):
        raise PmemError('%s exists and is not a directory' % memdir, 1)
    if os.path.isdir(memdir):
        links = memdir_symlinks(os.path.realpath(memdir))
        if links:
            raise PmemError('symlinks inside the memory dir (pmem never follows them; replace them '
                            'with regular files): %s' % ', '.join(links), 2)
    claude_root = (gi.main or gi.top) if gi.is_git else root
    if not args.no_claude and rejected_root(claude_root, ctx.home) and not args.force:
        raise PmemError('the Claude root %s is your home directory or above (it is a git work tree): '
                        'settings there would send every project below it to this memory. `git '
                        'init` the project, use --no-claude, or rerun with --force' % claude_root, 2)
    top = repo_top(root)
    rel = os.path.relpath(os.path.join(root, MEMDIR), top) if top else None
    tracked = bool(top) and not args.track and bool(git_tracked(top, rel))
    if tracked and not args.force:
        raise PmemError('%s is tracked by git in %s (tracked-unexpectedly): reusing it as private '
                        'memory would mix both modes, and git checkouts would overwrite it. Rerun '
                        'with --track to keep sharing it, untrack it with: git -C %s rm -r --cached '
                        '%s, or rerun with --force' % (memdir, top, shell_quote(top),
                                                       shell_quote(rel)), 2)
    memreal = os.path.realpath(memdir)
    plan = None
    if not args.no_claude:
        plan = plan_claude_wiring(claude_root, memreal, hooks=not args.no_hooks,
                                  allow_dream=args.allow_dream, force=args.force)

    # 2. files
    if not os.path.isdir(memdir):
        os.makedirs(memdir, 0o755)
        out.append('created %s' % memdir)
    memdir = os.path.realpath(memdir)
    for fname, content in (('MEMORY.md', MEMORY_TEMPLATE),
                           ('HANDOFF.md', render_handoff('main', 'pmem init'))):
        p = os.path.join(memdir, fname)
        if os.path.lexists(p):
            out.append('ok %s' % fname)
            if fname == 'MEMORY.md':     # 1.1: the Preferences section, feedback lines moved in
                try:
                    with memory_lock(memdir):
                        out.extend(update_index_locked(
                            memdir, lambda lines, eol: migrate_index(memdir, lines, eol, 'init')))
                except PmemError as e:
                    out.append('warning: MEMORY.md not migrated to 1.1 (no ## Preferences '
                               'section): %s' % e)
        else:
            atomic_write(p, enc(content), memdir=memdir)
            out.append('created %s' % p)
    for p in (memdir, os.path.join(memdir, 'MEMORY.md')):
        msg = drop_group_write(p)
        if msg:
            out.append(msg)
    for d in ('.archive', '.checkpoints'):
        p = os.path.join(memdir, d)
        if os.path.isdir(p):
            out.append('ok %s/' % d)
        else:
            os.makedirs(p)
            out.append('created %s/' % p)
    # 3. .gitignore
    init_gitignore(memdir, args.track, out)
    # 4. private history
    hstate, hwhy = history_state(memdir)
    if not args.track and not args.no_history:
        if hstate == 'broken':
            out.append('warning: private history broken: %s — fix or move it away (pmem never '
                       'commits anywhere else); no snapshot taken' % hwhy)
        else:
            if hstate == 'off':
                r = git(['-c', 'init.defaultBranch=main', 'init', '-q', memdir])
                if r is None:
                    out.append('warning: git not found; private history disabled')
                elif r.rc != 0:
                    out.append('warning: git init failed: %s' % r.err.strip())
                else:
                    out.append('created private history %s/.git' % memdir)
            res = snapshot(memdir, 'pmem init')
            out.append('snapshot: pmem init' if res else ('ok history' if res is False else
                                                          'history: off'))
    elif args.track and hstate != 'off':
        out.append('warning: %s/.git exists: the outer repository cannot track files inside a '
                   'nested repository; move it away to use --track' % memdir)
    # 5. outer git
    if top:
        if not args.track:
            ensure_excluded(top, rel, out)
            if tracked:
                out.append('warning: tracked-unexpectedly: memory files are tracked by git in %s '
                           '(--force); untrack them with: git -C %s rm -r --cached %s'
                           % (top, shell_quote(top), shell_quote(rel)))
            ictx = Context()
            ictx.memdir, ictx.root = memdir, root
            sha = tracked_in_refs(ictx)
            if sha:
                out.append('warning: ' + ref_hazard(sha, ictx))
        else:
            out.append('reminder: with --track you commit the memory with the project yourself: '
                       'no secrets, no personal `user` facts; run `pmem lint --strict` before '
                       'committing')
    cache = {}
    if plan is not None:
        ctop = repo_top(claude_root)
        if ctop:
            ensure_excluded(ctop, os.path.relpath(settings_path(claude_root), ctop), out)
        # 6. Claude wiring: hook script first, then the settings that point to it
        wire_claude_root(claude_root, memreal, args, out, cache, plan)
    problems = 0
    # 7. children of a non-git workspace
    if not top and not args.no_children and not args.no_claude:
        problems = wire_children(root, memdir, args, out, [claude_root], cache)
    w = register_memdir(memdir)
    if w:
        out.append('warning: ' + w)
    if not any(l.startswith(('created', 'updated', 'installed', 'excluded', 'snapshot', 'replaced',
                             'moved', 'dropped', 'chmod'))
               for l in out):
        print('ok — nothing to change')
    return 1 if problems else 0


# ---------------------------------------------------------------------------------------------
# Commands: where, status

def cmd_where(args):
    ctx = resolve(args.C)
    print(require_memory(ctx))
    return 0


def git_mode(ctx):
    root, memdir = ctx.root, ctx.memdir
    top = repo_top(root)
    if not top:
        return 'no-git'
    rel = os.path.relpath(os.path.join(root, MEMDIR), top)
    intended = track_intended(memdir)
    if git_tracked(top, rel):
        return 'tracked' if intended else 'tracked-unexpectedly'
    if git_ignored(top, rel):
        return 'private-excluded'
    return 'tracked' if intended else 'not-ignored'


def import_sources(ctx, extra_cfgs, candidates=True):
    """Claude memory dirs for this project (§6 import --claude) and listed-only candidates."""
    paths = project_paths(ctx)
    sources, seen = [], set()
    if ctx.memdir:
        seen.add(os.path.realpath(ctx.memdir))
    for cfg in claude_config_dirs(extra_cfgs):
        for p in paths:
            d = os.path.join(cfg, 'projects', slug(p), 'memory')
            if os.path.isdir(d):
                r = os.path.realpath(d)
                if r not in seen:
                    seen.add(r)
                    sources.append(d)
    cands = []
    if not candidates:
        return sources, cands
    roots = project_roots(ctx)
    exact = set(slug(p) for p in paths)
    for cfg in claude_config_dirs(extra_cfgs):
        obj, _ = read_settings(os.path.join(cfg, 'settings.json'))
        v = (obj or {}).get('autoMemoryDirectory')
        if isinstance(v, str) and v.strip():
            d = os.path.realpath(os.path.expanduser(v))
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                cands.append((d, 'user-level autoMemoryDirectory in %s; may cover other projects'
                              % os.path.join(cfg, 'settings.json')))
        anc = os.path.dirname(ctx.claude_root)
        while not rejected_root(anc, ctx.home):
            d = os.path.join(cfg, 'projects', slug(anc), 'memory')
            if os.path.isdir(d) and os.path.realpath(d) not in seen:
                seen.add(os.path.realpath(d))
                cands.append((d, 'memory of the ancestor dir %s; may cover other projects' % anc))
            parent = os.path.dirname(anc)
            if parent == anc:
                break
            anc = parent
        for name in sub_project_dirs(cfg, roots, exact):  # sessions launched in a sub-directory
            d = os.path.join(cfg, 'projects', name, 'memory')
            if not os.path.isdir(d) or os.path.realpath(d) in seen:
                continue
            cwds = transcript_cwds(os.path.dirname(d))
            inside = [c for c in cwds if under_any(c, roots)]
            if inside:
                why = 'memory of Claude sessions launched in %s' % inside[0]
            elif cwds:
                continue  # transcripts belong to another project that shares the name prefix
            else:
                why = ('its name matches a sub-directory of this project, but no transcript is left '
                       'to confirm it')
            seen.add(os.path.realpath(d))
            cands.append((d, why))
    return sources, cands


def sub_project_dirs(cfg, roots, exact):
    """Names under <cfg>/projects starting with slug(root)+'-' for a root (not in ``exact``):
    Claude's per-launch-dir folders of sub-directories (and of look-alike siblings)."""
    prefixes = tuple(sorted(set(slug_prefix(r) for r in roots)))
    try:
        names = sorted(os.listdir(os.path.join(cfg, 'projects')))
    except OSError:
        return []
    return [n for n in names if n not in exact and n.startswith(prefixes)]


def claude_transcript_cwd(path, max_lines=50):
    """The first ``cwd`` field in the first ``max_lines`` lines of a Claude transcript."""
    try:
        with open(path, 'rb') as f:
            for i, raw in enumerate(f):
                if i >= max_lines:
                    break
                if b'"cwd"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get('cwd'), str) and obj['cwd']:
                    return obj['cwd']
    except OSError:
        pass
    return None


def transcript_cwds(projdir, limit=20):
    """cwds of the newest ``limit`` transcripts in a Claude project dir."""
    files = []
    try:
        for fn in os.listdir(projdir):
            fp = os.path.join(projdir, fn)
            if fn.endswith('.jsonl') and os.path.isfile(fp):
                files.append((os.stat(fp).st_mtime, fp))
    except OSError:
        return []
    files.sort(reverse=True)
    cwds = (claude_transcript_cwd(fp) for _, fp in files[:limit])
    return [c for c in cwds if c]


def under_any(path, roots):
    """True if ``path`` (as given or resolved) is one of ``roots`` or lies below one."""
    variants = [os.path.normpath(path)]
    if os.path.exists(path):
        variants.append(os.path.realpath(path))
    return any(is_under(v, r) for v in variants for r in roots)


def logical_variant(real_path, ctx):
    if not real_path or not ctx.logical or ctx.logical == ctx.start:
        return None
    if not is_under(ctx.start, real_path):
        return None
    rel = os.path.relpath(ctx.start, real_path)
    if rel == os.curdir:
        return ctx.logical
    parts, lparts = rel.split(os.sep), ctx.logical.split(os.sep)
    if len(lparts) > len(parts) and lparts[-len(parts):] == parts:
        return os.sep.join(lparts[:-len(parts)]) or os.sep
    return None


def project_paths(ctx):
    """P set: Claude root, memory root, start dir (real and logical) + every worktree path."""
    out = []
    for p in (ctx.claude_root, ctx.root, ctx.start):
        if p:
            out.append(p)
            out.append(os.path.realpath(p))
            lv = logical_variant(os.path.realpath(p), ctx)
            if lv:
                out.append(lv)
    out.append(ctx.logical)
    for w in ctx.git.worktrees():
        if not w['bare']:
            out.append(w['path'])
            out.append(w['real'])
    seen, res = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            res.append(p)
    return res


def project_roots(ctx):
    """Root/worktree set: memory root, Claude root and every non-bare worktree (real + logical)."""
    cands = [ctx.root, ctx.claude_root]
    for w in ctx.git.worktrees():
        if not w['bare']:
            cands += [w['path'], w['real']]
    roots = []
    for p in cands:
        if not p:
            continue
        real = os.path.realpath(p)
        for q in (os.path.normpath(p), real, logical_variant(real, ctx)):
            if q and q not in roots:
                roots.append(q)
    return roots or [ctx.start]


def load_manifest(memdir):
    path = os.path.join(memdir, '.imported.json')
    try:
        raw = read_bytes(path)
    except FileNotFoundError:
        return collections.OrderedDict()
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=collections.OrderedDict)
    except ValueError as e:
        raise PmemError('%s is not valid JSON (%s); fix or move it' % (path, e), 1)
    if not isinstance(obj, dict):
        raise PmemError('%s is not a JSON object; fix or move it' % path, 1)
    return obj


def pending_legacy(ctx):
    if not ctx.memdir:
        return []
    try:
        manifest = load_manifest(ctx.memdir)
    except PmemError:
        manifest = {}
    sources, _ = import_sources(ctx, [], candidates=False)
    res = []
    for src in sources:
        known = manifest.get(os.path.realpath(src)) or {}
        shas = set(v.get('sha256') for v in known.values() if isinstance(v, dict))
        n = 0
        for fn in sorted(os.listdir(src)):
            p = os.path.join(src, fn)
            if (fn.startswith('.') or not fn.endswith('.md') or fn == 'MEMORY.md'
                    or fn[:-3].lower() in RESERVED_STEMS or not os.path.isfile(p)):
                continue
            try:
                if sha256(read_bytes(p)) not in shas:
                    n += 1
            except OSError:
                pass
        if n:
            res.append({'path': src, 'pending': n})
    return res


def legacy_harness(ctx):
    found = []
    roots = []
    for r in (ctx.claude_root, ctx.root):
        if r and r not in roots:
            roots.append(r)
    for r in roots:
        cdir = os.path.join(r, '.claude')
        if os.path.isdir(os.path.join(cdir, 'evolve')):
            found.append(os.path.join(cdir, 'evolve') + os.sep)
        cands = []
        rules = os.path.join(cdir, 'rules')
        if os.path.isdir(rules):
            cands += [os.path.join(rules, f) for f in sorted(os.listdir(rules)) if f.endswith('.md')]
        skills = os.path.join(cdir, 'skills')
        if os.path.isdir(skills):
            cands += [os.path.join(skills, d, 'SKILL.md') for d in sorted(os.listdir(skills))]
        for p in cands:
            try:
                if os.path.isfile(p) and 'evolve-queue' in read_text(p):
                    found.append(p)
            except OSError:
                pass
    return found


def block_present(path, marker=WIRE_BEGIN):
    """True if the file has the marker line (BOM-aware; fenced code ignored)."""
    try:
        raw = read_bytes(path)
    except OSError:
        return False
    _, lines = text_lines(raw)
    return bool(marker_lines([c for c, _ in lines], marker))


def block_outdated(path):
    """True if the file's project-memory block is not this version's WIRE_BLOCK (EOL and trailing
    whitespace ignored); False without a (well-formed) block."""
    try:
        raw = read_bytes(path)
    except OSError:
        return False
    _, lines = text_lines(raw)
    if block_problem(lines):
        return False
    contents = [c for c, _ in lines]
    b, e = marker_lines(contents, WIRE_BEGIN), marker_lines(contents, WIRE_END)
    if not b or not e:
        return False
    return ([c.rstrip() for c in contents[b[0]:e[0] + 1]]
            != [l.rstrip() for l in WIRE_BLOCK.rstrip('\n').split('\n')])


def rewire_command(path):
    """The `pmem wire …` command that rewrites the block in ``path``."""
    real = os.path.realpath(path)
    if real == os.path.realpath(codex_agents_path()):
        return 'pmem wire codex'
    if os.path.basename(path) == 'CLAUDE.md':
        dirs = claude_config_dirs(both=True)
        for k, d in enumerate(dirs):
            if real == os.path.realpath(os.path.join(d, 'CLAUDE.md')):
                return 'pmem wire claude' + ('' if k == 0 and d == claude_config_dirs()[0]
                                             else ' --config-dir %s' % shell_quote(d))
    return 'pmem wire %s' % shell_quote(path)


def codex_agents_path():
    base = os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    return os.path.join(os.path.abspath(os.path.expanduser(base)), 'AGENTS.md')


def project_agents_files(ctx):
    """AGENTS.md files Codex reads for the start dir: from the git (or project) root down."""
    top = ctx.git.top if ctx.git.is_git else (ctx.root or ctx.start)
    if not top or not is_under(ctx.start, top):
        return []
    dirs, d = [], ctx.start
    while True:
        dirs.append(d)
        if d == top:
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return [os.path.join(d, 'AGENTS.md') for d in reversed(dirs)]


def clean_json(obj):
    """Strings made printable (surrogate escapes replaced) before json.dumps(ensure_ascii=True)."""
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, dict):
        return collections.OrderedDict((clean_json(k), clean_json(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    return obj


def memories_by_type(memdir):
    counts = collections.OrderedDict()
    for fn in memory_files(memdir):
        mf = MemFile(os.path.join(memdir, fn))
        t = mf.type if mf.type in TYPES else ('invalid' if mf.type else 'untyped')
        counts[t] = counts.get(t, 0) + 1
    return counts


def pref_status(ctx, lines, hints, warnings):
    """Preferences summary for `pmem status` (adds hints and warnings)."""
    memdir = ctx.memdir
    lines = lines or []
    sec = pref_section(lines) if lines else None
    n = len(pref_entries(memdir, lines)) if sec else 0
    used, skipped = inherited_prefs(ctx)
    files = global_files()
    rules = set()
    for g in files:
        rules.update(norm_rule(r) for r in g.rules())
    info = collections.OrderedDict([
        ('project', n), ('section', bool(sec)),
        ('inherited', sum(len(e) for _, e in used)),
        ('inherited_from', [m for m, _ in used]),
        ('skipped', [collections.OrderedDict([('memory_dir', m), ('reason', r)]) for m, r in skipped]),
        ('global', len(rules)), ('targets', [g.path for g in files]),
        ('diverged', global_divergence(files)),
        ('outside', [t for _, t, c in feedback_outside(memdir, lines) if c]),
        ('no_entry_line', [t for _, t, c in feedback_outside(memdir, lines) if not c])])
    if lines and not sec:
        hints.append('preferences section missing in MEMORY.md → run pmem init')
    if n > PREF_MAX_ENTRIES:
        hints.append('%d preferences in ## Preferences (keep <= %d) → memory-health' % (
            n, PREF_MAX_ENTRIES))
    if info['outside']:
        hints.append('%d feedback memor%s indexed outside ## Preferences (%s) → pmem index --fix%s' % (
            len(info['outside']), 'y' if len(info['outside']) == 1 else 'ies',
            ', '.join(info['outside'][:5]), '' if sec else ' after pmem init'))
    if info['no_entry_line']:
        hints.append('%d feedback memor%s without a `- [title](name.md) — rule` line (%s) → add one '
                     'under ## Preferences by hand (pmem lint)' % (
                         len(info['no_entry_line']), 'y' if len(info['no_entry_line']) == 1 else 'ies',
                         ', '.join(info['no_entry_line'][:5])))
    for m, r in skipped:
        hints.append('outer memory %s skipped for inherited preferences: %s' % (m, r))
    bad = [g for g in files if g.error]
    for g in bad:
        warnings.append('global preferences: %s: %s' % (g.path, g.error))
    if info['diverged']:
        warnings.append('the global preference blocks have diverged between %s — compare them by '
                        'hand (`pmem pref list`); pmem never merges them' % ', '.join(info['targets']))
    return info


def cmd_status(args):
    ctx = resolve(args.C)
    if not ctx.memdir:
        err = str(refused_memdir_error(ctx)) if ctx.bad_memdir else 'no memory dir'
        if args.json:
            print(json.dumps(clean_json({'start': ctx.start, 'memory_dir': None, 'errors': [err]}),
                             indent=2))
        else:
            print('start dir:     %s' % ctx.start)
            if ctx.bad_memdir:
                print('memory dir:    refused')
                print('ERROR: ' + err)
            else:
                print('memory dir:    none — run `pmem init` in the project root')
        return 2 if ctx.bad_memdir else 1
    memdir = ctx.memdir
    errors, warnings, hints = [], [], []
    key = ctx.key
    if ctx.key_error:
        errors.append(ctx.key_error)
        key = ctx.checkout_key
    info = collections.OrderedDict()
    info['start'] = ctx.start
    info['project_root'] = ctx.root
    info['memory_dir'] = memdir
    info['claude_root'] = ctx.claude_root
    info['handoff_key'] = key
    by_type = memories_by_type(memdir)
    info['memories'] = {'total': sum(by_type.values()), 'by_type': by_type}
    try:
        text, _, lines = read_index(memdir)
        if text is None:
            errors.append('MEMORY.md is missing — run `pmem init`')
    except PmemError as e:
        text, lines = None, None
        errors.append(str(e))
    if text is None:
        info['index'] = None
    else:
        nl, nc = len(lines), len(text)
        info['index'] = {'lines': nl, 'chars': nc, 'soft_lines': INDEX_SOFT_LINES,
                         'max_lines': INDEX_MAX_LINES, 'soft_chars': INDEX_SOFT_CHARS,
                         'max_chars': INDEX_MAX_CHARS}
        if nl > INDEX_MAX_LINES or nc > INDEX_MAX_CHARS:
            warnings.append('MEMORY.md exceeds the native load cap (200 lines / 25,000 chars) — '
                            'shrink it (memory-health)')
        elif nl > INDEX_SOFT_LINES or nc > INDEX_SOFT_CHARS:
            warnings.append('MEMORY.md is above 150 lines / 20,000 chars — consider shrinking it')
    links = memdir_symlinks(memdir)
    if links:
        errors.append('symlinks inside the memory dir (never followed; replace them with regular '
                      'files): %s' % ', '.join(links))
    hos = []
    for k, p in list_handoffs(memdir):
        h = handoff_info(k, p, memdir)
        ck = checkpoint_state(memdir, k, h['ts'])
        prev = checkpoint_state(memdir, k, h['ts'], prev=True)
        h['checkpoint_newer'] = bool(ck) and (h['ts'] is None or ck['ts'] > h['ts'])
        h['checkpoint'] = ck['state'] if ck else None
        h['checkpoint_error'] = ck['error'] if ck and ck['state'] == 'failure' else None
        h['prev_checkpoint'] = prev['state'] if prev else None
        h['prev_checkpoint_error'] = prev['error'] if prev and prev['state'] == 'failure' else None
        h['current'] = (k == key)
        del h['ts']
        hos.append(h)
    info['handoffs'] = hos
    hstate, hwhy = history_state(memdir)
    hist = {'enabled': hstate == 'on', 'state': hstate, 'uncommitted': None, 'lock_age': None}
    if hstate == 'on':
        r = mgit(memdir, ['status', '--porcelain'])
        if r is not None and r.rc == 0:
            hist['uncommitted'] = len([l for l in r.out.split('\n') if l.strip()])
        age = git_lock_age(memdir)
        if age is not None:
            hist['lock_age'] = int(age)
            lock = os.path.join(memdir, '.git', 'index.lock')
            if age > STALE_GIT_LOCK:
                warnings.append('stale %s (%d min old): snapshots fail until it goes — the next '
                                '`pmem snapshot` removes it' % (lock, age // 60))
            else:
                warnings.append('%s exists (%d s old): a git operation is running in the private '
                                'history, or crashed' % (lock, age))
    elif hstate == 'broken':
        warnings.append('private history broken: %s — fix or move it away; pmem never commits '
                        'anywhere else' % hwhy)
    info['history'] = hist
    refused_root = claude_root_refused(ctx)
    st = claude_wiring_state(ctx.claude_root, memdir)
    top = repo_top(ctx.claude_root)
    st['tracked'] = bool(top and git_tracked(top, os.path.relpath(st['path'], top)))
    st['effective_here'] = True
    if refused_root:
        warnings.append(claude_root_warning(ctx))
    if st['state'] == 'elsewhere':
        errors.append('Claude autoMemoryDirectory in %s points to %s — run `pmem init --force` '
                      'then `pmem import %s`' % (st['path'], st['value'], st['value']))
    elif st['state'] == 'missing' and not refused_root:
        warnings.append('Claude wiring missing — run `pmem init`')
    elif st['state'] == 'invalid':
        warnings.append('%s: %s' % (st['path'], st['error']))
    if not ctx.git.is_git and ctx.start != ctx.claude_root and st['state'] == 'ok':
        st['effective_here'] = False
        warnings.append('claude wiring: not effective from this directory — outside git Claude Code '
                        'reads .claude/settings.local.json only from the launch dir; launch from %s'
                        % ctx.claude_root)
    if st['tracked']:
        warnings.append('%s is tracked by git (machine paths leak to collaborators)' % st['path'])
    if st['script'] and st['script'] != 'ok':
        warnings.append('Claude hooks are present but their script is %s → run `pmem init`'
                        % st['script'])
    if st['hooks'] != 'absent' and not st['prompt_hook']:
        warnings.append(prompt_hook_warning(st['path']))
    stable = stable_hook_path()
    info['stable_copy'] = {'path': stable, 'version': script_version(stable), 'cli': VERSION}
    warnings += version_warnings()
    eff = native_memory_off(ctx)
    dream = 'unset'
    for label, path in settings_layers(ctx.claude_root):  # the first layer setting it wins
        obj, _ = read_settings(path)
        if isinstance(obj, dict) and 'autoDreamEnabled' in obj:
            dream = 'on' if obj.get('autoDreamEnabled') else 'off'
            break
    for e in eff:
        warnings.append('Claude native memory: ' + e)
    st['effective'] = eff or ['enabled']
    st['auto_dream'] = dream
    if dream == 'on':
        warnings.append('autoDreamEnabled is on: background consolidation may rewrite/delete '
                        'memories (review with `pmem diff`)')
    info['claude'] = st
    lw = linked_worktree_containing(ctx.root, ctx.git)
    if lw:
        errors.append('memory dir is inside the linked worktree %s — `git worktree remove` would '
                      'delete it' % lw)
    mode = git_mode(ctx)
    info['git_mode'] = mode
    if mode == 'tracked-unexpectedly':
        warnings.append('memory files are tracked by git without --track (git rm -r --cached)')
    elif mode == 'not-ignored':
        warnings.append('memory dir is neither ignored nor tracked — run `pmem init`')
    sha = tracked_in_refs(ctx)
    info['tracked_in_refs'] = sha
    if sha:
        warnings.append(ref_hazard(sha, ctx))
    agents = codex_agents_path()
    project_agents = [p for p in project_agents_files(ctx) if block_present(p)]
    codex = {'agents_md': agents, 'block': block_present(agents),
             'project_block': project_agents[0] if project_agents else None, 'hint': None}
    if not is_under(memdir, ctx.start):
        codex['hint'] = 'codex: launch from %s or add --add-dir %s' % (ctx.root, ctx.root)
    info['codex'] = codex
    cglobal = os.path.join(claude_config_dirs()[0], 'CLAUDE.md')
    info['claude_global'] = {'path': cglobal, 'block': block_present(cglobal)}
    legacy = pending_legacy(ctx)
    info['legacy_sources'] = legacy
    for l in legacy:
        hints.append('%d file(s) in %s not imported yet → pmem import --claude' % (l['pending'],
                                                                                 l['path']))
    harness = legacy_harness(ctx)
    info['legacy_harness'] = harness
    if harness:
        hints.append('legacy learn/reflect/evolve harness found (%s) → see the migration section of '
                     'the project-memory skill' % ', '.join(harness))
    seen_blocks = set()
    for path in [g.path for g in global_files() if g.main] + project_agents[:1]:
        if os.path.realpath(path) in seen_blocks:
            continue
        seen_blocks.add(os.path.realpath(path))
        if block_outdated(path):
            warnings.append('the project-memory block in %s is from another pmem version → %s'
                            % (path, rewire_command(path)))
    if not codex['block'] and not codex['project_block']:
        hints.append('Codex: no project-memory block in %s or the project\'s AGENTS.md → pmem wire '
                     'codex' % agents)
    if not info['claude_global']['block']:
        hints.append('Claude: no project-memory block in %s → pmem wire claude' % cglobal)
    if codex['hint']:
        hints.append(codex['hint'])
    prefs = pref_status(ctx, lines, hints, warnings)
    info['preferences'] = prefs
    info['errors'], info['warnings'], info['hints'] = errors, warnings, hints
    if args.json:
        print(json.dumps(clean_json(info), indent=2))
        return 1 if errors else 0
    p = print
    p('project root:   %s' % ctx.root)
    p('memory dir:     %s' % memdir)
    if ctx.start != ctx.root:
        p('start dir:      %s' % ctx.start)
    p('handoff key:    %s' % key)
    p('memories:       %d%s' % (info['memories']['total'], (' (' + ', '.join(
        '%s %d' % kv for kv in by_type.items()) + ')') if by_type else ''))
    if info['index']:
        ix = info['index']
        p('index:          %d lines (soft 150, max 200), %d chars (soft 20000, max 25000)'
          % (ix['lines'], ix['chars']))
    for h in hos:
        flag = {'failure': '  [checkpoint: stopped on %s]' % (h['checkpoint_error'] or 'an error'),
                'stale': '  [checkpoint newer]'}.get(h['checkpoint'], '')
        flag += {'failure': '  [earlier checkpoint: stopped on %s]'
                            % (h['prev_checkpoint_error'] or 'an error'),
                 'stale': '  [earlier checkpoint newer]'}.get(h['prev_checkpoint'], '')
        p('handoff:        %-12s modified %s by %s%s%s' % (
            h['key'], h['modified'], h['by'], '  [current]' if h['current'] else '', flag))
    if hstate == 'on':
        p('history:        on, %s uncommitted change(s)' % (
            hist['uncommitted'] if hist['uncommitted'] is not None else '?'))
    else:
        p('history:        %s' % ('broken' if hstate == 'broken' else 'off'))
    state_txt = {'ok': 'ok', 'missing': 'missing — run `pmem init`', 'elsewhere':
                 'points elsewhere (%s)' % st['value'], 'invalid': 'invalid settings file'}
    if refused_root and st['state'] != 'ok':
        wiring = 'not wired — the Claude root is your home directory or above'
    elif not st['effective_here']:
        wiring = ('not effective from this directory — outside git Claude Code reads '
                  '.claude/settings.local.json only from the launch dir; launch from %s'
                  % ctx.claude_root)
    else:
        wiring = state_txt[st['state']]
    p('claude wiring:  %s (%s)%s' % (wiring, st['path'],
                                     ' [tracked settings]' if st['tracked'] else ''))
    p('claude memory:  %s' % '; '.join(st['effective']))
    p('autoDream:      %s' % dream)
    hooks_txt = st['hooks']
    if st['script'] and st['script'] != 'ok':
        hooks_txt += ' but script %s → pmem init' % st['script']
    p('hooks:          %s' % hooks_txt)
    p('git mode:       %s' % mode)
    if codex['project_block']:
        codex_txt = 'project (%s)' % codex['project_block']
    else:
        codex_txt = '%s (%s)' % ('present' if codex['block'] else 'missing', agents)
    p('codex block:    %s' % codex_txt)
    p('claude block:   %s (%s)' % ('present' if info['claude_global']['block'] else 'missing',
                                   cglobal))
    p('preferences:    %d project, %d inherited (%d skipped), %d global (%s%s)' % (
        prefs['project'], prefs['inherited'], len(prefs['skipped']), prefs['global'],
        ('targets ' + ', '.join(prefs['targets'])) if prefs['targets'] else 'no targets',
        '; diverged' if prefs['diverged'] else ''))
    for e in errors:
        p('ERROR: ' + e)
    for w in warnings:
        p('WARN: ' + w)
    for h in hints:
        p('hint: ' + h)
    return 1 if errors else 0


# ---------------------------------------------------------------------------------------------
# Commands: context, handoff

def cmd_context(args):
    ctx = resolve(args.C)
    require_memory(ctx)
    key = workstream_key(ctx, args.key)
    print(assemble_context(ctx, key, with_index=True, with_prefs=True))
    return 0


def cmd_handoff(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    key = workstream_key(ctx, args.key)
    action = args.action or 'show'
    if action == 'path':
        p = guard(memdir, handoff_path(memdir, key))
        if not os.path.lexists(p):
            guard(memdir, os.path.dirname(p))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with memory_lock(memdir):
                create_exclusive(p, enc(render_handoff(key, 'pmem')), memdir)
        print(p)
        return 0
    if action == 'list':
        keys = worktree_keys(ctx.git)
        rows = []
        for k, p in list_handoffs(memdir):
            h = handoff_info(k, p, memdir)
            if k == 'main' or keys is None:
                wt = '-'
            else:
                wt = 'yes' if k in keys else 'no'
            rows.append((k, h['modified'], h['by'], wt, p))
        if not rows:
            print('no handoffs — create one with `pmem handoff path`')
            return 1
        w = max(len(r[0]) for r in rows)
        print('%-*s  %-25s  %-16s  %-8s  %s' % (w, 'key', 'modified', 'by', 'worktree', 'path'))
        for r in rows:
            print('%-*s  %-25s  %-16s  %-8s  %s' % (w, r[0], r[1], r[2], r[3], r[4]))
        return 0
    p = handoff_path(memdir, key)
    if not os.path.isfile(p):
        print(assemble_context(ctx, key, with_others=False, with_warnings=False))
        eprint('pmem: no handoff for workstream "%s" — create it with `pmem handoff path`' % key)
        return 1
    print(assemble_context(ctx, key, with_others=False, with_warnings=False))
    return 0


# ---------------------------------------------------------------------------------------------
# Commands: add, find, index, archive

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
ARCHIVE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def create_exclusive(path, data, memdir=None):
    """Create a NEW file with O_EXCL (never through a symlink), mode 0644. -> False if it exists."""
    if memdir:
        guard(memdir, path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                     0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return True


def memory_text(memdir, path, name, description, mtype, body=None, meta=None, exists=True):
    """New text of a memory file rewritten like `add --force`: unknown frontmatter keys and the
    body (unless ``body`` is given) are kept, so are CRLF line endings. ``meta``: metadata keys
    to set (replacing existing ones)."""
    old, eol = None, '\n'
    if exists:
        mf = MemFile(path, memdir)
        if mf.error:
            raise PmemError('cannot read %s: %s' % (path, mf.error), 1)
        old = mf.fm if mf.closed else None
        if body is None:
            body = mf.body.lstrip('\r\n') if mf.closed else mf.text
        if mf.text.split('\n', 1)[0].endswith('\r'):
            eol = '\r\n'
    body = (body or '').replace('\r\n', '\n')
    text = render_memory(name, description, mtype, now_utc_iso(), body, old, meta)
    return text.replace('\n', eol) if eol != '\n' else text


def cmd_add(args):
    if not NAME_RE.match(args.name) or args.name.lower() in RESERVED_STEMS:
        raise PmemError('NAME must match ^[a-z0-9][a-z0-9-]*$ and not be "memory"/"handoff": %r'
                        % args.name, 2)
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    fname = args.name + '.md'
    path = os.path.join(memdir, fname)
    body = None
    if args.stdin:
        body = dec(sys.stdin.buffer.read())
    elif args.body is not None:
        body = args.body
    feedback = args.type == 'feedback'
    if feedback and oneline(args.description):    # built like `pmem pref add` builds entries
        line = entry_line(args.title or args.name, fname, oneline(args.description))
    else:
        line = index_line(args.title or args.name, fname, args.description)
    if line_target(line) != fname:
        line = index_line(args.name, fname, args.description)
    notes = []

    def edit(lines, eol):
        prob = unclosed_problem([c for c, _ in lines])
        if prob:
            notes.append('warning: MEMORY.md:%d opens %s that never closes, which %s everything after '
                         'it (this line too) — close it with %s' % (
                             prob[1], UNCLOSED_WHAT[prob[0]],
                             'hides' if prob[0] == 'HTML comment' else 'turns into code',
                             UNCLOSED_CLOSE[prob[0]]))
        masked = mask_lines([c for c, _ in lines])
        sec = pref_section(lines, masked)
        mine = [e for e in section_entries(lines, sec, masked) if e.target
                and nfc(e.target) == nfc(fname)] if sec else []
        canon = [i for i, t in canonical_lines(lines, masked) if nfc(t) == nfc(fname)]
        res = 'index ok'
        if mine:
            e = next((x for x in mine if x.canonical), mine[0])
            if feedback:          # a preference entry keeps its rule text
                res = 'kept preference line'
            elif not e.canonical:
                res = 'index ok'
            else:   # no longer a feedback memory: its line (new text) goes under Memories
                block = [[line, eol]] + [[c, eol] for c, _ in remove_entry(lines, e)[1:]]
                append_ordinary(lines, eol, block)
                res = 'moved its index line from ## Preferences to %s' % MEMORIES_HEADING
        elif canon and args.force:
            lines[canon[0]][0] = line   # other references to it stay where they are
        if feedback:
            notes.extend(migrate_index(memdir, lines, eol, 'add'))
        if not mine and not canon:
            sec = pref_section(lines)
            if feedback and sec:
                section_insert(lines, eol, sec, [[line, eol]])
            else:
                append_ordinary(lines, eol, line)
            res = 'indexed'
        return res

    with memory_lock(memdir):
        read_index(memdir)            # an unreadable index stops us before any write
        guard(memdir, path)
        exists = os.path.lexists(path)
        if exists and not args.force:
            raise PmemError('%s already exists — update it by hand, or rerun with --force to rewrite '
                            'it' % path, 2)
        text = memory_text(memdir, path, args.name, args.description, args.type, body,
                           exists=exists)
        if exists:
            atomic_write(path, enc(text), memdir=memdir)
        elif not create_exclusive(path, enc(text), memdir):
            raise PmemError('%s already exists — update it by hand, or rerun with --force to rewrite '
                            'it' % path, 2)
        res = update_index_locked(memdir, edit)
    print('%s %s (%s)' % ('rewrote' if exists else 'added', path, res))
    for n in notes:
        (eprint('pmem: ' + n) if n.startswith('warning:') else print(n))
    limit = PREF_LINE_WARN if feedback else INDEX_LINE_CHARS
    if len(line) > limit and res != 'kept preference line':
        warn('its index line has %d chars (> %d; `pmem lint` warns) — shorten -d/--title'
             % (len(line), limit))
    snapshot(memdir, 'add %s' % args.name)
    return 0


def search_files(memdir, archive=False):
    files = [os.path.join(memdir, f) for f in memory_files(memdir)]
    files += [p for _, p in list_handoffs(memdir)]
    if archive:
        for adir in (os.path.join(memdir, '.archive'), os.path.join(memdir, '.archive', 'handoffs')):
            try:
                if os.path.islink(adir):
                    continue
                files += [os.path.join(adir, f) for f in sorted(os.listdir(adir))
                          if f.endswith('.md') and os.path.isfile(os.path.join(adir, f))
                          and not os.path.islink(os.path.join(adir, f))]
            except OSError:
                pass
    return files


def cmd_find(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    terms = [t.casefold() for t in args.terms]
    n = 0
    for p in search_files(memdir, args.archive):
        mf = MemFile(p, memdir)
        # name, description and body, not the raw frontmatter (keys like `type: project` or
        # `modified:` would match every memory)
        hay = '\n'.join((mf.stem, mf.name, mf.description,
                         mf.body if mf.closed else mf.text)).casefold()
        if all(t in hay for t in terms):
            n += 1
            name = mf.name or mf.stem
            desc = mf.description or first_nonblank(mf.body if mf.closed else mf.text, 120)
            print('%s — %s — %s/%s' % (name, oneline(desc), MEMDIR,
                                      os.path.relpath(p, memdir).replace(os.sep, '/')))
    if not n:
        eprint('pmem: no memory matches %s' % ' '.join(args.terms))
        return 1
    return 0


def feedback_outside(memdir, lines, cache=None):
    """[(line number, file, canonical)] of feedback memories with no entry in the Preferences
    section that are referenced outside it (all of them when there is no section): their
    canonical index line, else (canonical False) their first other reference."""
    return feedback_refs(memdir, lines, cache)[0]


def cmd_index(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    if args.fix:
        missing = not os.path.isfile(os.path.join(memdir, 'MEMORY.md'))
        actions = update_index(memdir, lambda lines, eol: fix_index_lines(memdir, lines, eol))
        if missing:
            actions.insert(0, 'created MEMORY.md from the template')
        for a in actions:
            print(a)
        if [a for a in actions if not a.startswith('note: ')]:
            snapshot(memdir, 'index --fix')
        elif not actions:
            print('ok — index is consistent')
        _, _, lines = read_index(memdir)
        if lines is not None and not pref_section(lines):
            print('preferences section missing — run pmem init')
        return 0
    text, _, lines = read_index(memdir)
    if text is None:
        print('MEMORY.md missing — run `pmem init` or `pmem index --fix`')
        return 1
    a = analyze_index(memdir, lines)
    problems = 0
    n = unclosed_comment([c for c, _ in lines])
    if n:
        print('unclosed HTML comment at MEMORY.md:%d — close it with --> (it hides the rest of the '
              'index)' % n)
        problems += 1
    n = unclosed_fence([c for c, _ in lines])
    if n:
        print('unclosed code fence at MEMORY.md:%d — close it with ``` (it turns the rest of the '
              'index into code)' % n)
        problems += 1
    for f in a['unindexed']:
        print('unindexed: %s' % f)
        problems += 1
    for f in a['unindexable']:
        print('cannot be indexed: %s — rename it (`:`, `\\` or line breaks cannot appear in an '
              'index link)' % f)
        problems += 1
    for ln, t in a['dangling']:
        print('dangling: MEMORY.md:%d links to missing %s' % (ln, t))
        problems += 1
    if a['handoff'] == 'missing':
        print('handoff line: missing (must be the first link line)')
        problems += 1
    elif a['handoff'] == 'not-first':
        print('handoff line: MEMORY.md:%d is not the first link line' % a['handoff_line'])
        problems += 1
    has_sec = bool(pref_section(lines))
    for ln, t, canonical in feedback_outside(memdir, lines):
        if canonical:
            print('feedback memory %s indexed outside ## Preferences (MEMORY.md:%d)' % (t, ln))
            if has_sec:
                problems += 1
        else:
            print('feedback memory %s has no canonical index line (only MEMORY.md:%d refers to it) — '
                  'add `- [title](%s) — rule` under ## Preferences by hand' % (t, ln, t))
    if not has_sec:
        print('preferences section missing — run pmem init')
    if problems:
        print('run `pmem index --fix` to repair')
        return 1
    print('ok — index is consistent')
    return 0


def cmd_archive(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    names = []   # relative paths without .md: "<stem>" or "handoffs/<key>"
    for raw in args.names:
        rel = raw[:-3] if raw.endswith('.md') else raw
        sub, _, name = rel.rpartition('/')
        if sub not in ('', 'handoffs') or not ARCHIVE_NAME_RE.match(name) \
                or (not sub and name.lower() in RESERVED_STEMS):
            raise PmemError('cannot archive %r (give a memory stem or handoffs/<key>; MEMORY.md and '
                            'HANDOFF.md are never archived)' % raw, 2)
        guard(memdir, os.path.join(memdir, rel + '.md'))
        if not os.path.isfile(os.path.join(memdir, rel + '.md')):
            raise PmemError('no %s file %s.md in %s' % ('handoff' if sub else 'memory', rel, memdir), 1)
        if rel not in names:
            names.append(rel)
    keys = worktree_keys(ctx.git)
    for rel in names:
        if rel.startswith('handoffs/') and keys and rel[len('handoffs/'):] in keys:
            warn('the worktree of %s.md still exists; its next session starts a fresh handoff' % rel)
    fnames = set(nfc(n + '.md') for n in names if '/' not in n)

    removed_prefs = []
    left = []

    def edit(lines, eol):
        entries = dict((e.idx, e) for e in pref_entries(memdir, lines))
        hits = [i for i, t in canonical_lines(lines) if nfc(t) in fnames]
        for i in reversed(hits):
            e = entries.get(i)
            if e is not None:     # a preference entry goes with its continuation lines
                remove_entry(lines, e)
                removed_prefs.insert(0, e.rule)
            else:
                del lines[i]
        # prose, blockquotes and other bullets that mention it are never deleted
        left.extend((i + 1, t) for i, t in link_lines(lines) if nfc(t) in fnames)
        return len(hits)

    moved = []
    with memory_lock(memdir):
        if fnames:
            read_index(memdir)         # an unreadable index stops us before anything moves
        for rel in names:
            guard(memdir, os.path.join(memdir, rel + '.md'))
            adir = guard(memdir, os.path.join(memdir, '.archive', os.path.dirname(rel)))
            os.makedirs(adir, exist_ok=True)
            name = os.path.basename(rel)
            dest = os.path.join(adir, name + '.md')
            n = 2
            while os.path.lexists(dest):
                dest = os.path.join(adir, '%s-%d.md' % (name, n))
                n += 1
            os.rename(os.path.join(memdir, rel + '.md'), dest)
            if args.reason:
                text = read_text(dest)
                lines = to_lines(text)
                eol = eol_of(lines)
                if lines and lines[-1][0].strip():
                    append_line(lines, '', eol)
                append_line(lines, 'archived: %s — %s' % (today(), oneline(args.reason)), eol)
                atomic_write(dest, enc(from_lines(lines)), memdir=memdir)
            moved.append((rel, dest))
        dropped = update_index_locked(memdir, edit) if fnames else 0
    for rel, dest in moved:
        print('archived %s.md → %s/%s' % (rel, MEMDIR, os.path.relpath(dest, memdir)))
    if fnames:
        print('dropped %d index line(s)' % dropped)
    for rule in removed_prefs:
        print('removed preference: %s' % rule)
    if left:
        print('left %d other line(s) that link to it in place: %s — edit them by hand (they now '
              'link to a missing file)' % (len(left), ', '.join('MEMORY.md:%d (%s)' % x for x in left)))
    snapshot(memdir, 'archive %s%s' % (' '.join(names),
                                       (' — ' + oneline(args.reason)) if args.reason else ''))
    return 0


# ---------------------------------------------------------------------------------------------
# Commands: pref

VIOL_HEAD_RE = re.compile(r'^\*\*Violations:\*\*\s*$')
VIOL_ITEM_RE = re.compile(r'^\s*-\s+\d{4}-\d{2}-\d{2}\b')


def violations_block(body_lines):
    """(start, end) of the `**Violations:**` line and its list, else None."""
    for i, line in enumerate(body_lines):
        if VIOL_HEAD_RE.match(line.strip()):
            j = i + 1
            while j < len(body_lines) and body_lines[j].strip() and (
                    body_lines[j].lstrip().startswith('- ') or body_lines[j][:1] in (' ', '\t')):
                j += 1
            return i, j
    return None


def violation_count(body):
    lines = body.replace('\r\n', '\n').split('\n')
    blk = violations_block(lines)
    return len([l for l in lines[blk[0] + 1:blk[1]] if VIOL_ITEM_RE.match(l)]) if blk else 0


def add_violation(body, note):
    """Body with ``- <today>: note`` appended to its **Violations:** list. -> (body, count)."""
    lines = body.replace('\r\n', '\n').rstrip('\n').split('\n') if body.strip() else []
    item = '- %s: %s' % (today(), oneline(note))
    blk = violations_block(lines)
    if blk:
        lines[blk[1]:blk[1]] = [item]
    else:
        if lines and lines[-1].strip():
            lines.append('')
        lines += ['**Violations:**', item]
    text = '\n'.join(lines) + '\n'
    return text, violation_count(text)


def pref_body(rule, why, how, old_body=None):
    body = '%s\n\n**Why:** %s\n**How to apply:** %s\n' % (rule, oneline(why) or '(not recorded)',
                                                         oneline(how) or '(not recorded)')
    if old_body:
        lines = old_body.replace('\r\n', '\n').split('\n')
        blk = violations_block(lines)
        if blk:
            body += '\n' + '\n'.join(lines[blk[0]:blk[1]]) + '\n'
    return body


WHY_LINE_RE = re.compile(r'^\s*\*\*Why:\*\*')
HOW_LINE_RE = re.compile(r'^\s*\*\*How to apply:\*\*')


def replace_rule_line(lines, olds, rule):
    """Replace the first non-blank body line with ``rule`` when it equals (normalized) one of
    ``olds`` or the rule itself. -> its index, or None when it was left alone (or missing)."""
    keys = set(norm_rule(o) for o in olds if o and norm_rule(o))
    keys.add(norm_rule(rule))
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is not None and norm_rule(lines[first]) in keys:
        lines[first] = rule
        return first
    return None


def refresh_pref_body(old_body, rule, olds, why=None, how=None):
    """Body of an existing memory recorded again with `pref add --force`: a first line equal to
    an old rule or description becomes RULE (else RULE is put first); only the Why/How lines that
    were given are written; everything else (other Why/How lines, **Violations:**, notes) stays."""
    lines = old_body.replace('\r\n', '\n').strip('\n').split('\n') if old_body.strip() else []
    first = replace_rule_line(lines, olds, rule)
    if first is None:
        lines[0:0] = [rule, ''] if lines else [rule]
        first = 0
    for regex, label, value in ((WHY_LINE_RE, '**Why:**', why),
                                (HOW_LINE_RE, '**How to apply:**', how)):
        value = oneline(value or '')
        if not value:
            continue
        new = '%s %s' % (label, value)
        at = next((i for i, l in enumerate(lines) if regex.match(l)), None)
        if at is not None:
            lines[at] = new
            continue
        why_at = next((i for i, l in enumerate(lines) if WHY_LINE_RE.match(l)), None)
        how_at = next((i for i, l in enumerate(lines) if HOW_LINE_RE.match(l)), None)
        if regex is HOW_LINE_RE and why_at is not None:
            lines.insert(why_at + 1, new)
        elif regex is WHY_LINE_RE and how_at is not None:
            lines.insert(how_at, new)
        else:                         # a new paragraph right after the rule's paragraph
            j = first + 1
            while j < len(lines) and lines[j].strip():
                j += 1
            lines[j:j] = ['', new] + ([''] if j < len(lines) else [])
    return '\n'.join(lines) + '\n'


def default_pref_name(rule):
    words = re.findall(r'[A-Za-z0-9]+', rule)[:6]
    name = '-'.join(w.lower() for w in words)[:64].strip('-')
    if not name or not NAME_RE.match(name) or name in RESERVED_STEMS:
        name = 'pref-' + hashlib.sha256(enc(rule)).hexdigest()[:8]
    return name


def is_cjk(ch):
    o = ord(ch)
    return (0x3040 <= o <= 0x30ff or 0x3400 <= o <= 0x4dbf or 0x4e00 <= o <= 0x9fff
            or 0xf900 <= o <= 0xfaff or 0xac00 <= o <= 0xd7af or 0x20000 <= o <= 0x2fa1f)


def check_part(arg, what='preference'):
    """RULE given as part of a rule must be long enough to pick one: >= 4 characters, >= 2 when
    it has CJK characters. Exit 2 otherwise."""
    a = (arg or '').strip()
    if len(a) < (2 if any(is_cjk(c) for c in a) else 4):
        raise PmemError('no %s is exactly %r, and a part of a rule must have at least 4 characters '
                        '(2 for CJK) — give more of the rule (see `pmem pref list`)' % (what, a), 2)


def match_entry(entries, arg, what='preference'):
    """The entry for NAME or RULE: backed by NAME.md, else the same normalized rule, else a unique
    case-insensitive substring (>= 4 characters, 2 for CJK) of a rule. Exit 2 when empty, too
    short, ambiguous or not found."""
    arg = (arg or '').strip()
    if not arg:
        raise PmemError('NAME|RULE is empty', 2)
    if NAME_RE.match(arg):
        named = [e for e in entries if e.name == arg]
        if named:
            return next((e for e in named if editable(e)), named[0])
    key = norm_rule(arg)
    for e in entries:
        if key and norm_rule(e.rule) == key:
            return e
    check_part(arg, what)
    hits = [e for e in entries if arg.casefold() and arg.casefold() in e.rule.casefold()]
    if not hits and key:
        hits = [e for e in entries if key in norm_rule(e.rule)]
    keys = set(norm_rule(e.rule) for e in hits)
    if len(keys) > 1:
        raise PmemError('%r matches several %ss — be more specific:\n%s' % (
            arg, what, '\n'.join('  - %s' % e.rule for e in hits)), 2)
    if not hits:
        raise PmemError('no %s matches %r (see `pmem pref list`)' % (what, arg), 2)
    return hits[0]


def read_index_for_pref(memdir):
    """(text, bom, lines) for a pref command; refuses an index with an unclosed comment or code
    fence."""
    text, bom, lines = read_index(memdir)
    if text is None:
        text, bom, lines = '', False, to_lines(MEMORY_TEMPLATE)
    err = unclosed_error(lines)
    if err:
        raise err
    return text, bom, lines


def write_index(memdir, text, bom, lines):
    new = ('﻿' if bom else '') + from_lines(lines)
    if new != text:
        atomic_write(os.path.join(memdir, 'MEMORY.md'), enc(new), memdir=memdir)


def unprefer_file(memdir, fname):
    """A feedback memory that no longer backs a preference becomes a `project` memory, else the
    next migration would move its index line back into Preferences. -> True if retyped."""
    path = os.path.join(memdir, fname)
    mf = MemFile(path, memdir)
    if mf.error or not mf.closed or mf.type != 'feedback':
        return False
    atomic_write(path, enc(memory_text(memdir, path, mf.name or mf.stem, mf.description,
                                       'project')), memdir=memdir)
    return True


def index_as_ordinary(memdir, lines, eol, fname):
    """Give a memory an ordinary index line under Memories unless it has one."""
    if any(nfc(t) == nfc(fname) for _, t in link_lines(lines)):
        return
    line = generated_index_line(os.path.join(memdir, fname))
    if line_target(line) != fname:
        line = index_line(fname[:-3], fname, '')
    append_ordinary(lines, eol, line)


def demote_local_entry(memdir, lines, eol, e, report):
    """Remove entry ``e``; its feedback file stays as an ordinary memory."""
    remove_entry(lines, e)
    if e.kind == 'pref':
        if unprefer_file(memdir, e.target):
            report.append('kept %s as an ordinary project memory (indexed under %s)'
                          % (e.target, MEMORIES_HEADING))
        index_as_ordinary(memdir, lines, eol, e.target)


def add_preference(memdir, rule, name=None, title=None, why=None, how=None, force=False):
    """Record a preference in ``memdir`` (caller holds the lock). -> report lines."""
    report = []
    text, bom, lines = read_index_for_pref(memdir)
    eol = eol_of(lines)
    report += [r for r in migrate_index(memdir, lines, eol, 'add')]
    cache = {}
    prefs = [e for e in classify(memdir, section_entries(lines), cache) if e.is_pref]
    key = norm_rule(rule)
    if name is None:
        same = [e for e in prefs if e.name and norm_rule(e.rule) == key]
        if same:
            name = same[0].name
        else:
            base = default_pref_name(rule)
            taken = set(name_key(f) for f in os.listdir(memdir))
            taken |= set(name_key(t) for _, t in link_lines(lines))
            name, n = base, 2
            while name_key(name + '.md') in taken:
                p = os.path.join(memdir, name + '.md')
                if os.path.isfile(p) and memory_type(memdir, name + '.md', cache) == 'feedback' and \
                        norm_rule(MemFile(p, memdir).description) == key:
                    break     # a feedback file with this very rule, not indexed yet
                name, n = '%s-%d' % (base, n), n + 1
    fname = name + '.md'
    path = guard(memdir, os.path.join(memdir, fname))
    exists = os.path.lexists(path)
    mtype = memory_type(memdir, fname, cache) if exists else None
    if exists and mtype != 'feedback' and not force:
        raise PmemError('%s exists as a %s memory — choose another --name, or rerun with --force to '
                        'turn it into a feedback memory' % (fname, mtype or 'non-memory'), 2)
    e1 = [e for e in prefs if e.target and nfc(e.target) == nfc(fname)]
    e2 = [e for e in prefs if norm_rule(e.rule) == key and e not in e1]
    conflicts = [e for e in e2 if e.target and e.kind == 'pref']
    if conflicts and not force:
        raise PmemError('already recorded as %s (%s/%s); rerun with --force to record it as %s '
                        'instead' % (conflicts[0].name, memdir, conflicts[0].target, name), 2)
    keep = next((e for e in e1 if e.canonical), None) or next((e for e in e2 if editable(e)), None)
    report += [left_in_place(e) for e in e1 + e2 if e is not keep and not editable(e)]
    if keep is not None and keep in e1 and not title:
        new_line = entry_with_rule(keep, rule)
    else:
        new_line = entry_line(title or name.replace('-', ' ')[:40], fname, rule)
    # every other entry for this rule and every other canonical index line of this file goes
    # (their continuation lines move to the kept entry); other references stay where they are
    drop, carried = set(), []

    def take(a, b):
        drop.update(range(a, b))
        carried.extend([lines[k][0], eol] for k in range(a + 1, b))
    for e in e1 + e2:
        if e is not keep and not (e.target and not e.canonical):
            take(e.idx, e.end)
    sec = pref_section(lines)
    for i, t in canonical_lines(lines):
        if nfc(t) == nfc(fname) and (keep is None or i != keep.idx) and i not in drop:
            a, b = entry_block(lines, i)
            take(a, min(b, sec[1]) if sec and sec[0] < i < sec[1] else b)
    if keep is not None:
        drop.difference_update(range(keep.idx, keep.end))
        lines[keep.idx][0] = new_line
        lines[keep.end:keep.end] = carried
        shift = len(carried)
        drop = set(i + shift if i >= keep.end else i for i in drop)
        carried = []
    lines[:] = [l for i, l in enumerate(lines) if i not in drop]
    if keep is None:
        section_insert(lines, eol, pref_section(lines), [[new_line, eol]] + carried)
    conflicts = [e for e in conflicts if editable(e)]   # the others stay as they are
    for e in conflicts:      # replaced with --force: their files get ordinary index lines
        index_as_ordinary(memdir, lines, eol, e.target)
    # files first (NAME.md, then the displaced ones), then the index
    if not exists:
        body = pref_body(rule, why, how)
        if not create_exclusive(path, enc(render_memory(name, rule, 'feedback', now_utc_iso(), body)),
                                memdir):
            raise PmemError('%s appeared meanwhile — rerun' % path, 1)
        report.append('created %s' % path)
    elif force:
        mf = MemFile(path, memdir)
        if mf.error:
            raise PmemError('cannot read %s: %s' % (path, mf.error), 1)
        old_body = mf.body.lstrip('\r\n') if mf.closed else mf.text
        olds = [mf.description] + [e.rule for e in e1]
        body = refresh_pref_body(old_body, rule, olds, why, how)
        atomic_write(path, enc(memory_text(memdir, path, name, rule, 'feedback', body)),
                     memdir=memdir)
        report.append('rewrote %s (description and first line; the rest of the body kept)' % path)
    else:
        report.append('kept %s' % path)
    for e in conflicts:
        if unprefer_file(memdir, e.target):
            report.append('%s no longer backs a preference: kept as an ordinary project memory'
                          % e.target)
    write_index(memdir, text, bom, lines)
    report.append('%s preference: %s' % ('replaced' if keep is not None else 'added', rule))
    if len(new_line) > PREF_LINE_WARN:
        report.append('warning: the entry has %d chars (> %d; `pmem lint` warns) — shorten the rule '
                      'or --title' % (len(new_line), PREF_LINE_WARN))
    return report


def print_report(report):
    for r in report:
        if r.startswith('warning:'):
            eprint('pmem: ' + r)
        else:
            print(r)


def pref_memdir(ctx, up):
    memdir = require_memory(ctx)
    if not up:
        return memdir
    used, skipped = outer_memdirs(ctx.root, ctx.home)
    if not used:
        why = ''.join('; skipped %s: %s' % s for s in skipped)
        raise PmemError('--up: no usable memory dir above %s%s' % (ctx.root, why), 2)
    return used[0]


def cmd_pref_add(args):
    rule = check_rule(args.rule)
    if args.name is not None and (not NAME_RE.match(args.name) or args.name in RESERVED_STEMS):
        raise PmemError('--name must match ^[a-z0-9][a-z0-9-]*$ and not be "memory"/"handoff": %r'
                        % args.name, 2)
    ctx = resolve(args.C)
    memdir = pref_memdir(ctx, args.up)
    with memory_lock(memdir):
        report = add_preference(memdir, rule, args.name, args.title, args.why, args.how, args.force)
    print_report(report)
    w = register_memdir(memdir)
    if w:
        warn(w)
    snapshot(memdir, 'pref add %s' % oneline(rule)[:60])
    return 0


def cmd_pref_remove(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    report = []
    with memory_lock(memdir):
        text, bom, lines = read_index_for_pref(memdir)
        e = match_entry(pref_entries(memdir, lines), args.what)
        if not editable(e):
            raise PmemError('%s — make it one by hand, then rerun; nothing removed' % not_canonical(e),
                            2)
        demote_local_entry(memdir, lines, eol_of(lines), e, report)
        write_index(memdir, text, bom, lines)
    print('removed preference: %s' % e.rule)
    print_report(report)
    snapshot(memdir, 'pref remove %s' % (e.name or oneline(e.rule)[:60]))
    return 0


def cmd_pref_violated(args):
    new_rule = check_rule(args.rule)
    ctx = resolve(args.C)
    memdir = pref_memdir(ctx, args.up)
    if args.up:     # the nearest outer memory that has the entry
        memdir = next((m for m, ents in inherited_prefs(ctx)[0]
                       if any(x.name == args.name for x in ents)), memdir)
    with memory_lock(memdir):
        text, bom, lines = read_index_for_pref(memdir)
        hits = [e for e in pref_entries(memdir, lines) if e.name == args.name]
        if not hits:
            hint = ''
            if not args.up:
                outer = [m for m, ents in inherited_prefs(ctx)[0]
                         if any(x.name == args.name for x in ents)]
                if outer:
                    hint = ('; it is inherited from %s — rerun with --up (or `pmem -C %s pref '
                            'violated …`)' % (outer[0], shell_quote(os.path.dirname(outer[0]))))
            raise PmemError('no preference entry for %s under ## Preferences in %s/MEMORY.md%s'
                            % (args.name, memdir, hint), 2)
        e = next((x for x in hits if editable(x)), hits[0])
        if not editable(e):
            raise PmemError('%s — make it one by hand, then rerun; nothing changed' % not_canonical(e),
                            2)
        if e.kind != 'pref':
            raise PmemError('%s/%s is missing — restore it or record the rule again with `pmem pref '
                            'add`' % (memdir, e.target), 1)
        if norm_rule(new_rule) == norm_rule(e.rule):
            raise PmemError('--rule must sharpen the rule: it is the same as the current one (%s)'
                            % e.rule, 2)
        path = os.path.join(memdir, e.target)
        mf = MemFile(path, memdir)
        if mf.error:
            raise PmemError('cannot read %s: %s' % (path, mf.error), 1)
        body = mf.body.lstrip('\r\n') if mf.closed else mf.text
        blines = body.replace('\r\n', '\n').split('\n')
        if replace_rule_line(blines, [e.rule, mf.description], new_rule) is not None:
            body = '\n'.join(blines)      # the body's first line was the old rule
        body, count = add_violation(body, args.note or 'broke the rule "%s"' % e.rule)
        new_text = memory_text(memdir, path, mf.name or args.name, new_rule, 'feedback', body,
                               meta={'violations': count})
        eol = eol_of(lines)
        block = lines[e.idx:e.end]
        block[0] = [entry_with_rule(e, new_rule), block[0][1] or eol]
        del lines[e.idx:e.end]
        section_insert(lines, eol, pref_section(lines), block, top=True)
        atomic_write(path, enc(new_text), memdir=memdir)
        write_index(memdir, text, bom, lines)
    print('sharpened preference %s in %s/MEMORY.md: %s' % (args.name, memdir, new_rule))
    print('was: %s' % e.rule)
    print('moved it to the top of ## Preferences; violations: %d' % count)
    if count >= 2:
        print('hint: still broken after %d rewrites? ask the user how to phrase it' % count)
    snapshot(memdir, 'pref violated %s' % args.name)
    return 0


def entry_json(e):
    return collections.OrderedDict([('name', e.name), ('rule', e.rule), ('kind', e.kind),
                                    ('line', e.idx + 1)])


def global_json(files):
    return [collections.OrderedDict([('path', g.path), ('error', g.error),
                                     ('block', bool(g.main)), ('rules', g.rules() if not g.error
                                                               else [])]) for g in files]


def cmd_pref_list(args):
    ctx = resolve(args.C)
    info = collections.OrderedDict()
    rc = 0
    if ctx.memdir:
        try:
            text, _, lines = read_index(ctx.memdir)
        except PmemError as e:
            text, lines = None, None
            eprint('pmem: %s' % e)
        lines = lines or []
        sec = pref_section(lines) if lines else None
        info['project'] = collections.OrderedDict([
            ('memory_dir', ctx.memdir), ('section', bool(sec)),
            ('entries', [entry_json(e) for e in pref_entries(ctx.memdir, lines)]),
            ('outside', [t for _, t, c in feedback_outside(ctx.memdir, lines) if c]),
            ('no_entry_line', [t for _, t, c in feedback_outside(ctx.memdir, lines) if not c])])
        used, skipped = inherited_prefs(ctx)
        info['inherited'] = [collections.OrderedDict([('memory_dir', m), ('entries', [
            entry_json(e) for e in entries])]) for m, entries in used]
        info['skipped'] = [collections.OrderedDict([('memory_dir', m), ('reason', r)])
                           for m, r in skipped]
    else:
        info['project'] = None
        rc = 2 if ctx.bad_memdir else 1
    files = global_files()
    info['global'] = collections.OrderedDict([('targets', global_json(files)),
                                              ('diverged', global_divergence(files))])
    if args.all:
        rows = []
        for m in registered_memdirs():
            entries = read_prefs(m)
            rows.append(collections.OrderedDict([('memory_dir', m),
                                                 ('entries', [entry_json(e) for e in entries])]))
        info['all'] = rows
    if args.json:
        print(json.dumps(clean_json(info), indent=2))
        return rc
    p = print
    p('Preferences in force — on conflict this project wins, then the nearest outer folder, then '
      'the global block.')
    if info['project'] is None:
        p('')
        p(str(refused_memdir_error(ctx)) if ctx.bad_memdir else
          'no project memory (%s/) found from %s — run `pmem init` in the project root'
          % (MEMDIR, ctx.start))
    else:
        pj = info['project']
        p('')
        p('project: %s/MEMORY.md%s' % (ctx.memdir, '' if pj['section'] else
                                       '  (no ## Preferences section — run pmem init)'))
        for e in pj['entries']:
            p('  - %s%s' % (e['rule'], '  [%s]' % e['name'] if e['name'] else ''))
        if not pj['entries']:
            p('  (none)')
        for t in pj['outside']:
            p('  ! feedback memory %s is indexed outside the section — pmem index --fix' % t)
        for t in pj['no_entry_line']:
            p('  ! feedback memory %s has no `- [title](%s) — rule` line — add one under ## '
              'Preferences by hand' % (t, t))
        for d in info['inherited']:
            p('inherited: %s/MEMORY.md' % d['memory_dir'])
            for e in d['entries']:
                p('  - %s%s' % (e['rule'], '  [%s]' % e['name'] if e['name'] else ''))
            if not d['entries']:
                p('  (none)')
        for d in info['skipped']:
            p('skipped: %s — %s' % (d['memory_dir'], d['reason']))
    if not files:
        p('global: no CLAUDE.md / AGENTS.md with the project-memory block (pmem wire codex|claude)')
    for g in files:
        p('global: %s%s' % (g.path, ' — ERROR: %s' % g.error if g.error else
                            ('' if g.main else '  (no project-memory block)')))
        for r in ([] if g.error else g.rules()):
            p('  - %s' % r)
        if not g.error and not g.rules():
            p('  (none)')
    if info['global']['diverged']:
        p('WARN: the global preference blocks have diverged between these files — compare them by '
          'hand; pmem never merges them')
    if args.all:
        p('')
        p('all registered projects (%s):' % registry_path())
        n = 0
        for row in info['all']:
            block = ['== %s' % row['memory_dir']] + ['  - %s' % e['rule'] for e in row['entries']]
            if n + len(block) > LIST_ALL_MAX_LINES:
                p('… truncated at %d lines' % LIST_ALL_MAX_LINES)
                break
            for line in block:
                p(line)
            n += len(block)
        if not info['all']:
            p('  (none registered — `pmem init` and `pmem pref add` register a project)')
    return rc


def global_targets(args, explicit):
    """Phase 1 read + validation of the global files. -> files (exit 1 on any problem)."""
    files = global_files(args.config_dir, explicit, getattr(args, 'only', False))
    bad = [g for g in files if g.error]
    if bad:
        raise PmemError('nothing written: %s' % '; '.join('%s: %s' % (g.path, g.error) for g in bad), 1)
    return files


def check_plans(plans):
    bad = []
    for g, new in plans:
        if new != g.raw:
            why = writable_problem(g.path)
            if why:
                bad.append('%s: %s' % (g.path, why))
    if bad:
        raise PmemError('nothing written: %s' % '; '.join(bad), 1)


def promote_bytes(g, rule):
    if any(norm_rule(r) == norm_rule(rule) for r in g.rules()):
        return g.raw
    lines = [list(l) for l in g.lines]
    eol = g.eol
    if g.sub:
        ents = g.entries()
        j = ents[-1][1] if ents else g.sub[1]
        lines[j:j] = [['- ' + rule, eol]]
    else:
        e = g.main[1]
        block = [['', eol], [PREF_BEGIN, eol], [PREF_GLOBAL_HEADING, eol], ['- ' + rule, eol],
                 [PREF_END, eol]]
        if lines[e][1] == '':
            lines[e][1] = eol
            block[-1][1] = ''
        elif e + 1 < len(lines) and lines[e + 1][0].strip():
            block.append(['', eol])
        lines[e + 1:e + 1] = block
    return g.data(lines)


def demote_bytes(g, key):
    lines = [list(l) for l in g.lines]
    for i, j, r in reversed(g.entries()):
        if norm_rule(r) == key:
            del lines[i:j]
    return g.data(lines)


def check_only(args):
    if args.only and not (args.targets or args.config_dir):
        raise PmemError('--only needs at least one --target FILE or --config-dir DIR', 2)


def print_plans(plans, verb):
    """--dry-run: every target file and the change."""
    for g, new in plans:
        if new == g.raw:
            print('unchanged %s' % g.path)
            continue
        old_lines = set(c for c, _ in g.lines)
        _, new_lines = text_lines(new)
        print('would %s %s:' % (verb, g.path))
        if verb == 'change':
            for c, _ in new_lines:
                if c not in old_lines:
                    print('  + %s' % c)
        new_set = set(c for c, _ in new_lines)
        for c, _ in g.lines:
            if c not in new_set:
                print('  - %s' % c)


def cmd_pref_promote(args):
    check_only(args)
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    _, _, lines = read_index_for_pref(memdir)
    e = match_entry(pref_entries(memdir, lines), args.what)
    rule = e.rule
    files = global_targets(args, args.targets)
    explicit = set(os.path.realpath(os.path.abspath(os.path.expanduser(t))) for t in args.targets)
    missing = [g.path for g in files if not g.main and (args.only or
                                                         os.path.realpath(g.path) in explicit)]
    if missing:
        raise PmemError('%s has no project-memory block — run `pmem wire %s` first; nothing written'
                        % (missing[0], shell_quote(missing[0])), 1)
    targets = [g for g in files if g.main]
    if not targets:
        raise PmemError('no global instruction file has the project-memory block — run `pmem wire '
                        'codex` / `pmem wire claude` first; nothing written', 1)
    plans = [(g, promote_bytes(g, rule)) for g in targets]
    check_plans(plans)
    if args.dry_run:
        print('dry run — nothing written. `pmem pref promote` would write %d global file%s:'
              % (len(targets), '' if len(targets) == 1 else 's'))
        print_plans(plans, 'change')
        if not args.keep_local:
            print('and take the entry out of %s/MEMORY.md (its file stays as a project memory; '
                  '--keep-local keeps it)' % memdir)
        return 0
    write_globals(plans)
    print('promoted to %d global file%s (%s): %s' % (len(targets), '' if len(targets) == 1 else 's',
                                                      ', '.join(g.path for g in targets), rule))
    if args.keep_local:
        print('kept the local entry (--keep-local)')
        return 0
    report = []
    with memory_lock(memdir):
        text, bom, lines = read_index_for_pref(memdir)
        prefs = pref_entries(memdir, lines)
        same = [x for x in prefs if (e.name and x.name == e.name)
                or norm_rule(x.rule) == norm_rule(rule)]
        report += [left_in_place(x) for x in same if not editable(x)]
        same = [x for x in same if editable(x)]
        if same:
            demote_local_entry(memdir, lines, eol_of(lines), same[0], report)
            write_index(memdir, text, bom, lines)
            report.insert(0, 'removed the local entry from %s/MEMORY.md (the global block has it '
                             'now)' % memdir)
    print_report(report)
    snapshot(memdir, 'pref promote %s' % (e.name or oneline(rule)[:60]))
    return 0


def cmd_pref_demote(args):
    check_only(args)
    if not (args.rule or '').strip():
        raise PmemError('RULE is empty', 2)
    files = global_targets(args, args.targets)
    rows = [(g, r) for g in files for r in g.rules()]
    key = norm_rule(args.rule)
    hits = [x for x in rows if key and norm_rule(x[1]) == key]
    if not hits:
        check_part(args.rule, 'global preference')
        part = args.rule.strip().casefold()
        sub = [x for x in rows if part in x[1].casefold()]
        if not sub and key:
            sub = [x for x in rows if key in norm_rule(x[1])]
        keys = collections.OrderedDict((norm_rule(r), r) for _, r in sub)
        if len(keys) > 1:
            raise PmemError('%r matches several global preferences — be more specific:\n%s'
                            % (args.rule, '\n'.join('  - %s' % r for r in keys.values())), 2)
        if not keys:
            raise PmemError('no global preference matches %r (see `pmem pref list`)' % args.rule, 2)
        key = list(keys)[0]
        hits = [x for x in rows if norm_rule(x[1]) == key]
    rule = hits[0][1]
    plans = [(g, demote_bytes(g, key)) for g in files if any(norm_rule(r) == key for r in g.rules())]
    check_plans(plans)
    if args.dry_run:
        print('dry run — nothing written. `pmem pref demote` would write %d global file%s:'
              % (len(plans), '' if len(plans) == 1 else 's'))
        print_plans(plans, 'change')
        if args.to_project:
            ctx = resolve(args.C)
            print('and first add it to %s/MEMORY.md: %s' % (require_memory(ctx), rule))
        return 0
    memdir = None
    if args.to_project:
        ctx = resolve(args.C)
        memdir = require_memory(ctx)
        with memory_lock(memdir):
            print_report(add_preference(memdir, check_rule(rule, check_len=False)))
        print('recorded it in %s/MEMORY.md' % memdir)
    write_globals(plans)
    print('demoted from %d global file%s (%s): %s' % (len(plans), '' if len(plans) == 1 else 's',
                                                       ', '.join(g.path for g, _ in plans), rule))
    if memdir:
        snapshot(memdir, 'pref demote %s' % oneline(rule)[:60])
    return 0


def cmd_pref(args):
    func = {'list': cmd_pref_list, 'add': cmd_pref_add, 'remove': cmd_pref_remove,
            'violated': cmd_pref_violated, 'promote': cmd_pref_promote,
            'demote': cmd_pref_demote}.get(getattr(args, 'pref_cmd', None))
    if func is None:
        args.pref_parser.print_help(sys.stderr)
        return 2
    return func(args)



# ---------------------------------------------------------------------------------------------
# Commands: lint

# Token rules are anchored so ordinary words (``disk-space-on-...`` contains ``sk-``) never match.
TOKEN_START = r'(?<![A-Za-z0-9_-])'
SECRET_RULES = (
    ('private key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('AWS access key', re.compile(TOKEN_START + r'AKIA[0-9A-Z]{16}')),
    ('GitHub token', re.compile(TOKEN_START + r'ghp_[A-Za-z0-9]{36}')),
    ('GitHub fine-grained token', re.compile(TOKEN_START + r'github_pat_[A-Za-z0-9_]{50,}')),
    ('Slack token', re.compile(TOKEN_START + r'xox[abprs]-[A-Za-z0-9-]{10,}')),
    ('API key (sk-)', re.compile(TOKEN_START + r'sk-[A-Za-z0-9_-]{20,}')),
)
URL_CREDENTIALS_RE = re.compile(r'://[^/\s:@]+:([^/\s@]+)@')
SECRET_ASSIGN_RE = re.compile(r'(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*'
                              r'[`\'"]?([^\s`\'"]+)')
# where a credential lives, not the credential itself
VAULT_WORDS_RE = re.compile(r'(?i)^(1password|bitwarden|lastpass|keepass(xc)?|keychain|vault)')


def secret_hits(text):
    hits = []
    for n, line in enumerate(text.split('\n'), 1):
        if 'pmem:allow-secret' in line:
            continue
        for label, rx in SECRET_RULES:
            if rx.search(line):
                hits.append((n, label))
        for m in URL_CREDENTIALS_RE.finditer(line):
            if not m.group(1).startswith(('$', '<', '%')):
                hits.append((n, 'credentials in URL'))
                break
        for m in SECRET_ASSIGN_RE.finditer(line):
            v = m.group(2)
            if (len(v) >= 8 and re.search(r'\d', v) and not v.startswith(('$', '<'))
                    and not re.match(r'^[A-Z][A-Z0-9_]*$', v) and not TS_RE.match(v)
                    and not re.match(r'\d{4}-\d{2}-\d{2}', v) and all(ord(c) < 128 for c in v)
                    and not VAULT_WORDS_RE.match(v)):
                hits.append((n, '%s assignment' % m.group(1).lower()))
                break
    return hits


BACKTICK_RE = re.compile(r'`([^`\n]+)`')


def path_tokens(text):
    for n, line in enumerate(text.split('\n'), 1):
        for m in BACKTICK_RE.finditer(line):
            tok = m.group(1).strip()
            if ('/' not in tok or ' ' in tok or '://' in tok or tok.startswith(('~', '$', '/', '-', '<'))
                    or re.search(r'[*?{}\[\]]', tok)):
                continue
            yield n, tok


class Findings(object):
    def __init__(self):
        self.items = []

    def add(self, level, where, msg):
        self.items.append((level, where, msg))

    def count(self, level):
        return len([1 for l, _, _ in self.items if l == level])


def wiki_links(text):
    """(line number, target) of [[name]] memory links outside code spans, comments and fenced
    blocks; bash ``[[ -d x ]]``, pandas ``df[['a']]`` or TOML ``[[tool.x]]`` in code never count."""
    out = []
    for n, line in enumerate(mask_lines(text.split('\n')), 1):
        for m in WIKI_LINK_RE.finditer(line):
            target = m.group(1).split('|', 1)[0].split('#', 1)[0]
            if not WIKI_TARGET_RE.match(target):
                continue
            target = target.strip()
            if target.endswith('.md'):
                target = target[:-3]
            if target:
                out.append((n, target))
    return out


def unquoted_colon(fm, key):
    """True if the top-level ``key`` is a plain (unquoted) one-line YAML value containing ': '
    or ending with ':' — invalid YAML that pmem itself tolerates."""
    for kind, k, raw, _ in fm.entries:
        if kind != 'top' or k != key:
            continue
        m = FM_KEY_RE.match(raw[0].rstrip())
        rest = strip_yaml_comment(m.group(2) or '') if m else ''
        if not rest or rest[0] in '"\'' or BLOCK_SCALAR_RE.match(rest):
            return False
        return bool(re.search(r':[ \t]', rest)) or rest.endswith(':')
    return False


def lint_preferences(f, memdir, lines, sec, entries, cache):
    """Preferences checks of `pmem lint` (spec 1.1 §3.2-3.4, §7)."""
    prefs = [e for e in entries if e.is_pref]
    if not sec:
        f.add('WARN', 'MEMORY.md', 'preferences section missing — run `pmem init` (it adds '
              '## Preferences and moves feedback lines into it)')
    elif len(prefs) > PREF_MAX_ENTRIES:
        f.add('WARN', 'MEMORY.md:%d' % (sec[0] + 1), '%d preferences (keep <= %d: merge, sharpen or '
              'retire some — memory-health)' % (len(prefs), PREF_MAX_ENTRIES))
    for e in entries:
        where = 'MEMORY.md:%d' % (e.idx + 1)
        if e.kind == 'other':
            f.add('WARN', where, 'index line inside Preferences (%s is not a feedback memory) — move '
                  'it under %s' % (e.target, MEMORIES_HEADING))
            continue
        if len(e.content) > PREF_LINE_WARN:
            f.add('WARN', where, 'preference line has %d chars (> %d) — shorten the rule, put detail '
                  'in the file' % (len(e.content), PREF_LINE_WARN))
        if e.kind == 'bare':
            f.add('INFO', where, 'preference without a feedback file (no feedback file; `pmem pref add` '
                  'gives it one)')
        elif e.kind == 'pref':
            mf = MemFile(os.path.join(memdir, e.target), memdir)
            n = violation_count(mf.body if mf.closed else mf.text)
            if n >= 2:
                f.add('INFO', e.target, 'lists %d violations — still broken after %d rewrites? ask '
                      'the user how to phrase it (memory-health)' % (n, n))
            if e.hook and mf.closed and mf.description and \
                    norm_rule(mf.description) != norm_rule(e.hook):
                f.add('INFO', e.target, 'description out of sync with the rule at %s — make the '
                      'description the same rule (memory-health)' % where)
    outside, extra = feedback_refs(memdir, lines, cache)
    for ln, t, canonical in outside:
        if canonical:
            f.add('WARN', t, 'feedback memory indexed outside ## Preferences (MEMORY.md:%d) — `pmem '
                  'index --fix`%s' % (ln, '' if sec else ' after `pmem init` adds the section'))
        else:
            f.add('WARN', t, 'feedback memory %s has no canonical index line (only MEMORY.md:%d '
                  'refers to it) — add `- [title](%s) — rule` under ## Preferences' % (t[:-3], ln, t))
    for ln, t in extra:
        f.add('INFO', 'MEMORY.md:%d' % ln, 'other reference to %s left in place (pmem only moves '
              'and edits its `- [title](%s) — rule` line)' % (t, t))


def lint_memory(ctx, stale_days=90, check_paths=False):
    memdir = ctx.memdir
    f = Findings()
    try:
        text, _, lines = read_index(memdir)
        if text is None:
            f.add('ERROR', 'MEMORY.md', 'missing index — run `pmem init`')
    except PmemError as e:
        text = lines = None
        f.add('ERROR', 'MEMORY.md', str(e))
    if not os.path.isfile(os.path.join(memdir, 'HANDOFF.md')):
        f.add('ERROR', 'HANDOFF.md', 'missing handoff — run `pmem init` or `pmem handoff path`')
    for link in memdir_symlinks(memdir):
        f.add('ERROR', os.path.relpath(link, memdir), 'symlink inside memory dir (pmem never '
              'follows it; replace it with a regular file)')
    wt = linked_worktree_containing(ctx.root, ctx.git)
    if wt:
        f.add('ERROR', MEMDIR, 'memory dir is inside the linked worktree %s; `git worktree remove` '
              'would delete it' % wt)
    hstate, hwhy = history_state(memdir)
    if hstate == 'broken':
        f.add('WARN', '.git', 'private history broken: %s — fix or move it away' % hwhy)
    sha = tracked_in_refs(ctx)
    if sha:
        f.add('WARN', MEMDIR, ref_hazard(sha, ctx))
    if text is not None:
        a = analyze_index(memdir, lines)
        for ln, t in a['dangling']:
            f.add('ERROR', 'MEMORY.md:%d' % ln, 'dangling link to missing %s' % t)
        nl, nc = len(lines), len(text)
        if nl > INDEX_MAX_LINES or nc > INDEX_MAX_CHARS:
            f.add('ERROR', 'MEMORY.md', 'index has %d lines / %d chars; the native loader truncates '
                  'at 200 lines / 25,000 chars' % (nl, nc))
        elif nl > INDEX_SOFT_LINES or nc > INDEX_SOFT_CHARS:
            f.add('WARN', 'MEMORY.md', 'index has %d lines / %d chars (keep under 150 / 20,000)'
                  % (nl, nc))
        for f_ in a['unindexed']:
            f.add('WARN', f_, 'unindexed (not linked from MEMORY.md) — `pmem index --fix` to '
                  're-index, or `pmem archive %s` to retire' % f_[:-3])
        for f_ in a['unindexable']:
            f.add('WARN', f_, 'cannot be indexed — rename it (`:`, `\\` or line breaks cannot '
                  'appear in an index link)')
        if a['handoff'] == 'missing':
            f.add('WARN', 'MEMORY.md', 'HANDOFF line missing — `pmem index --fix`')
        elif a['handoff'] == 'not-first':
            f.add('WARN', 'MEMORY.md:%d' % a['handoff_line'], 'HANDOFF line is not the first link '
                  'line — `pmem index --fix`')
        n = unclosed_comment([c for c, _ in lines])
        if n:
            f.add('ERROR', 'MEMORY.md', 'unclosed HTML comment at MEMORY.md:%d — it hides the rest of '
                  'the index from agents and from pmem; close it with -->' % n)
        n = unclosed_fence([c for c, _ in lines])
        if n:
            f.add('ERROR', 'MEMORY.md', 'unclosed code fence at MEMORY.md:%d — it turns the rest of '
                  'the index into code; close it with ``` (pmem refuses to edit the index until '
                  'then)' % n)
        sec = pref_section(lines)
        cache = {}
        entries = classify(memdir, section_entries(lines, sec), cache) if sec else []
        prefs = [e for e in entries if e.is_pref]
        pref_lines = set(e.idx for e in prefs)
        for i, _t in link_lines(lines):  # only memory link lines are ever checked
            c = lines[i][0]
            if i not in pref_lines and len(c) > INDEX_LINE_CHARS:
                f.add('WARN', 'MEMORY.md:%d' % (i + 1), 'index line has %d chars (> 150)' % len(c))
        lint_preferences(f, memdir, lines, sec, entries, cache)
        for n, label in secret_hits(text):
            f.add('WARN', 'MEMORY.md:%d' % n, 'possible secret (%s)' % label)
    mode = git_mode(ctx)
    now = time.time()
    names = collections.defaultdict(list)
    stale, untimed = [], []
    mems = [MemFile(os.path.join(memdir, fn), memdir) for fn in memory_files(memdir)]
    active_names = set()
    for mf in mems:
        active_names.add(mf.stem)
        if mf.name:
            active_names.add(mf.name)
    archived = set()
    adir = os.path.join(memdir, '.archive')
    try:
        for fn in ([] if os.path.islink(adir) else os.listdir(adir)):
            if fn.endswith('.md'):
                archived.add(fn[:-3])
                amf = MemFile(os.path.join(adir, fn), memdir)
                if amf.name:
                    archived.add(amf.name)
    except OSError:
        pass
    handoffs = list_handoffs(memdir)
    for k, p in handoffs:
        active_names.add(os.path.basename(p)[:-3])
        hmf = MemFile(p, memdir)
        if hmf.name:
            active_names.add(hmf.name)
    for mf in mems:
        where = mf.fname
        if mf.error:
            f.add('ERROR', where, 'cannot read: %s' % mf.error)
            continue
        if not mf.has_fm:
            f.add('ERROR', where, 'no frontmatter (need name/description/metadata.type)')
        elif mf.close is None or mf.close >= FRONTMATTER_MAX_LINES:
            f.add('ERROR', where, 'frontmatter not closed within the first 30 lines')
        else:
            if not mf.name:
                f.add('ERROR', where, 'frontmatter: missing name')
            if not mf.description:
                f.add('ERROR', where, 'frontmatter: missing description')
            if not mf.type:
                f.add('ERROR', where, 'frontmatter: missing metadata.type')
            elif mf.type not in TYPES:
                f.add('ERROR', where, 'frontmatter: invalid type %r (user|feedback|project|reference)'
                      % mf.type)
            if mf.name and mf.name != mf.stem:
                f.add('WARN', where, 'name %r differs from the file stem' % mf.name)
            if unquoted_colon(mf.fm, 'description'):
                f.add('WARN', where, 'frontmatter: the description contains ": " but is not quoted — '
                      'quote it (description: "…"), YAML parsers reject it')
            if mf.type == 'user' and mode == 'tracked':
                f.add('WARN', where, 'type user memory in tracked mode (personal facts get committed)')
        if mf.name:
            names[mf.name].append(mf.fname)
        ts, bad = mf.timestamp()
        if bad:
            f.add('WARN', where, 'unparseable timestamp (using file mtime)')
        if not mf.has_timestamp():
            untimed.append(mf.stem)
        if ts is not None and now - ts > stale_days * 86400:
            stale.append(mf.stem)
        try:
            size = os.path.getsize(mf.path)
        except OSError:
            size = 0
        if size > FILE_SOFT_BYTES:
            f.add('WARN', where, '%d bytes (> 4096; recall shows only the first 4096) — split it' % size)
        for n, label in secret_hits(mf.text):
            f.add('WARN', '%s:%d' % (where, n), 'possible secret (%s)' % label)
        for n, target in wiki_links(mf.text):
            if target in active_names:
                continue
            if target in archived:
                f.add('WARN', '%s:%d' % (where, n), '[[%s]] links to archived memory' % target)
            else:
                f.add('WARN', '%s:%d' % (where, n), 'broken link [[%s]]' % target)
        if check_paths:
            for n, tok in path_tokens(mf.text):
                p = re.sub(r':\d+(:\d+)?$', '', tok)
                if not os.path.exists(os.path.join(ctx.root, p)):
                    f.add('WARN', '%s:%d' % (where, n), 'path `%s` does not exist (relative to %s)'
                          % (tok, ctx.root))
    for name, files in sorted(names.items()):
        if len(files) > 1:
            if any('-imported' in fn for fn in files):
                f.add('WARN', ', '.join(files), 'duplicate name %r — needs merge' % name)
            else:
                f.add('ERROR', ', '.join(files), 'duplicate name %r' % name)
    if check_paths and text is not None:
        for n, tok in path_tokens(text):
            p = re.sub(r':\d+(:\d+)?$', '', tok)
            if not os.path.exists(os.path.join(ctx.root, p)):
                f.add('WARN', 'MEMORY.md:%d' % n, 'path `%s` does not exist (relative to %s)'
                      % (tok, ctx.root))
    keys = worktree_keys(ctx.git)
    for k, p in handoffs:
        where = os.path.relpath(p, memdir)
        hmf = MemFile(p, memdir)
        ts, bad, _ = hmf.handoff_timestamp()
        if bad:
            f.add('WARN', where, 'unparseable timestamp (using file mtime)')
        if ts is not None and now - ts > HANDOFF_STALE_DAYS * 86400:
            f.add('WARN', where, 'handoff older than %d days — refresh or reset it' % HANDOFF_STALE_DAYS)
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        if size > FILE_SOFT_BYTES:
            f.add('WARN', where, '%d bytes (> 4096) — move lessons into memories' % size)
        for n, label in secret_hits(hmf.text):
            f.add('WARN', '%s:%d' % (where, n), 'possible secret (%s)' % label)
        if k != 'main' and keys is not None and k not in keys:
            f.add('INFO', where, 'no git worktree named %r exists any more (finished workstream? '
                  'carry over what is left, then `pmem archive handoffs/%s`)' % (k, k))
    for d in RESERVED_DIRS:
        if os.path.isdir(os.path.join(memdir, d)):
            f.add('INFO', d + '/', 'reserved dir present (ignored by native recall)')
    if stale:
        f.add('INFO', MEMDIR, '%d memor%s older than %d days: %s%s' % (
            len(stale), 'y' if len(stale) == 1 else 'ies', stale_days, ', '.join(stale[:10]),
            ', ...' if len(stale) > 10 else ''))
    if untimed:
        f.add('INFO', MEMDIR, '%d memor%s without metadata.modified (age falls back to the file '
              'mtime, which a copy or restore resets): %s%s' % (
                  len(untimed), 'y' if len(untimed) == 1 else 'ies', ', '.join(untimed[:10]),
                  ', ...' if len(untimed) > 10 else ''))
    return f


def cmd_lint(args):
    ctx = resolve(args.C)
    if ctx.bad_memdir:
        print('ERROR: %s' % refused_memdir_error(ctx))
        return 2
    if not ctx.memdir:
        print('ERROR: no memory dir found from %s — run `pmem init`' % ctx.start)
        return 1
    f = lint_memory(ctx, args.stale_days, args.paths)
    order = {'ERROR': 0, 'WARN': 1, 'INFO': 2}
    for level, where, msg in sorted(f.items, key=lambda x: order[x[0]]):
        print('%s: %s: %s' % (level, where, msg))
    e, w, i = f.count('ERROR'), f.count('WARN'), f.count('INFO')
    if not f.items:
        print('ok — no problems found')
    else:
        print('%d error(s), %d warning(s), %d info' % (e, w, i))
    return 1 if (e or (args.strict and w)) else 0


# ---------------------------------------------------------------------------------------------
# Commands: import

def inline_notes(lines):
    """Non-link, non-heading, non-header-blockquote, non-comment, non-blank lines + headings."""
    out = []
    in_comment = False
    header = True
    heading = None
    emitted = None
    masked = mask_lines([c for c, _ in lines])
    for i, (c, _) in enumerate(lines):
        s = c.strip()
        if i == 0:
            s = s.lstrip('\ufeff')
        if in_comment:
            if '-->' in s:
                in_comment = False
            continue
        if s.startswith('<!--'):
            if '-->' not in s[4:]:
                in_comment = True
            continue
        if not s:
            continue
        if s.startswith('#'):
            heading = s
            continue
        if memory_link_target(masked[i]):
            header = False
            continue
        if s.startswith('>') and header:
            continue
        header = False
        if heading and heading != emitted:
            if out:
                out.append('')
            out.append(heading)
            out.append('')
            emitted = heading
        out.append(c.rstrip())
    return out


def name_key(fname):
    """File-name identity for collision checks: NFC + casefold (safe on case-insensitive disks)."""
    return nfc(fname).casefold()


def free_name(stem, taken, first_suffix=''):
    """stem + first_suffix, then -2, -3 ... (``taken``: set of name_key values)."""
    base = stem + first_suffix
    cand = base + '.md'
    n = 2
    while name_key(cand) in taken:
        cand = '%s-%d.md' % (base, n)
        n += 1
    return cand


NOTES_HOOK = 'Inline notes from a legacy memory index — review and split into memories'


def notes_memory(name, description, body):
    return render_memory(name, description, 'project', now_utc_iso(), body.replace('\r\n', '\n'))


def split_import_lines(idx_lines, new_lines):
    """[(line, is_feedback)] -> (lines for the Preferences section, other lines); without a
    section every line is an other line (only init / pref add / add feedback add it)."""
    if not pref_section(idx_lines):
        return [], [l for l, _ in new_lines]
    return [l for l, f in new_lines if f], [l for l, f in new_lines if not f]


def cmd_import(args):
    ctx = resolve(args.C)
    memdir = require_memory(ctx)
    dry = args.dry_run
    srcs = []
    for s in args.src:
        p = os.path.abspath(os.path.expanduser(s))
        if not os.path.isdir(p):
            raise PmemError('%s: not a directory' % s, 1)
        srcs.append(p)
    cands = []
    if args.claude:
        found, cands = import_sources(ctx, args.config_dir)
        srcs += found
        if not found:
            print('no Claude memory dirs found for this project')
    elif args.config_dir:
        raise PmemError('--config-dir only applies with --claude', 2)
    files = []
    for fp in args.file:
        p = os.path.abspath(os.path.expanduser(fp))
        if not os.path.isfile(p):
            raise PmemError('%s: not a file' % fp, 1)
        files.append(p)
    if not srcs and not files and not args.claude:
        raise PmemError('nothing to import: give SRC_DIR..., --claude or --file PATH', 2)
    for d, why in cands:
        print('candidate (not imported): %s — %s; import explicitly if wanted (pmem import <dir>)'
              % (d, why))
    lock = memory_lock(memdir) if not dry else contextlib.nullcontext()
    with lock:
        manifest = load_manifest(memdir)
        taken = set(name_key(f) for f in os.listdir(memdir))
        text, bom, idx_lines = read_index(memdir)
        linked = set(nfc(t) for _, t in link_lines(idx_lines or []))
        counts = collections.OrderedDict((k, 0) for k in
                                         ('imported', 'identical', 'needs-merge', 'notes', 'skipped'))
        report, copies, writes, new_lines = [], [], [], []
        pending = {}        # name_key(target) -> sha256 of a file queued in this run
        pending_notes = {}  # sha256 of an inline-notes block queued in this run -> target
        seen_src = set()
        for src in srcs:
            sreal = os.path.realpath(src)
            if sreal == os.path.realpath(memdir) or sreal in seen_src:
                if sreal == os.path.realpath(memdir):
                    report.append('skipped %s (this is the memory dir itself)' % src)
                continue
            seen_src.add(sreal)
            try:
                listing = sorted(os.listdir(src))
            except OSError as e:
                report.append('skipped %s (%s)' % (src, e))
                counts['skipped'] += 1
                continue
            entry = manifest.setdefault(sreal, collections.OrderedDict()) if not dry else \
                copy.deepcopy(manifest.get(sreal, collections.OrderedDict()))
            shas = set(v.get('sha256') for k, v in entry.items()
                       if k != '__inline__' and isinstance(v, dict))
            imported_now = collections.OrderedDict()
            for fn in listing:
                p = os.path.join(src, fn)
                if fn == 'MEMORY.md' and os.path.isfile(p):
                    continue
                if fn.startswith('.'):
                    report.append('skipped %s (dot-file)' % p)
                    counts['skipped'] += 1
                    continue
                if os.path.isdir(p):
                    report.append('skipped %s/ (%s)' % (p, 'reserved dir' if fn in RESERVED_DIRS
                                                        else 'subdirectory'))
                    counts['skipped'] += 1
                    continue
                if not fn.endswith('.md') or not os.path.isfile(p):
                    report.append('skipped %s (not a .md file)' % p)
                    counts['skipped'] += 1
                    continue
                if fn[:-3].lower() in RESERVED_STEMS:
                    report.append('skipped %s (reserved name)' % p)
                    counts['skipped'] += 1
                    continue
                try:
                    data = read_bytes(p)
                except OSError as e:
                    report.append('skipped %s (%s)' % (p, e))
                    counts['skipped'] += 1
                    continue
                digest = sha256(data)
                if digest in shas:
                    report.append('skipped %s (already imported)' % p)
                    counts['skipped'] += 1
                    continue
                stem = fn[:-3]
                same = None
                pattern = re.compile(r'^%s-imported(-\d+)?\.md$' % re.escape(stem))
                cands_ = [fn] + sorted(x for x in os.listdir(memdir) if pattern.match(x))
                cands_ += sorted(os.path.basename(d) for _, d, _ in copies
                                 if pattern.match(os.path.basename(d)))
                for cand in cands_:
                    if name_key(cand) in pending:          # queued earlier in this run
                        if pending[name_key(cand)] == digest:
                            same = cand
                            break
                        continue
                    cp = os.path.join(memdir, cand)
                    try:
                        if os.path.isfile(cp) and not os.path.islink(cp) and read_bytes(cp) == data:
                            same = cand
                            break
                    except OSError:
                        pass
                if same:
                    report.append('identical %s (= %s)' % (p, same))
                    counts['identical'] += 1
                    entry[fn] = collections.OrderedDict([('sha256', digest), ('target', same)])
                    shas.add(digest)
                    continue
                if name_key(fn) not in taken:
                    target = fn
                    counts['imported'] += 1
                    report.append('imported %s → %s' % (p, target))
                else:
                    target = free_name(stem, taken, '-imported')
                    counts['needs-merge'] += 1
                    report.append('needs merge: %s → %s (differs from %s)' % (p, target, fn))
                taken.add(name_key(target))
                pending[name_key(target)] = digest
                copies.append((p, os.path.join(memdir, target), data))
                entry[fn] = collections.OrderedDict([('sha256', digest), ('target', target)])
                shas.add(digest)
                imported_now[fn] = target
            # index merge
            src_index = os.path.join(src, 'MEMORY.md')
            src_lines = []
            if os.path.isfile(src_index):
                try:
                    src_lines = to_lines(read_text(src_index).lstrip('\ufeff'))
                except OSError as e:
                    report.append('skipped %s (%s)' % (src_index, e))
                    counts['skipped'] += 1
            src_linked = set()
            for c, mc in zip([c for c, _ in src_lines], mask_lines([c for c, _ in src_lines])):
                t = memory_link_target(mc)
                if not t:
                    continue
                src_linked.add(t)
                if t in imported_now and nfc(imported_now[t]) not in linked:
                    lead = len(c) - len(c.lstrip())
                    stripped = c.strip()
                    line = replace_link_target(stripped, imported_now[t],
                                               mc[lead:lead + len(stripped)])
                    fb = MemFile(os.path.join(src, t)).type == 'feedback'
                    if fb and not canonical_target(mask_lines([line])[0]):
                        # prose about a preference: the section gets a real entry, the prose
                        # line is kept with the other imported lines
                        new_lines.append((pref_index_line(os.path.join(src, t), imported_now[t]),
                                          True))
                        new_lines.append((line, False))
                    else:
                        new_lines.append((line, fb))
                    linked.add(nfc(imported_now[t]))
            for fn, target in imported_now.items():
                if fn not in src_linked and nfc(target) not in linked:
                    if indexable(target):
                        fb = MemFile(os.path.join(src, fn)).type == 'feedback'
                        new_lines.append(((pref_index_line if fb else generated_index_line)(
                            os.path.join(src, fn), target), fb))
                    else:
                        report.append('cannot index %s — rename it' % target)
                    linked.add(nfc(target))
            notes = inline_notes(src_lines)
            if notes:
                block = '\n'.join(notes) + '\n'
                digest = sha256(enc(block))
                prev = entry.get('__inline__')
                if isinstance(prev, dict) and prev.get('sha256') == digest:
                    report.append('skipped inline notes of %s (already imported)' % src_index)
                elif digest in pending_notes:
                    report.append('identical inline notes of %s (= %s)'
                                  % (src_index, pending_notes[digest]))
                    entry['__inline__'] = collections.OrderedDict([
                        ('sha256', digest), ('target', pending_notes[digest])])
                else:
                    target = free_name('imported-notes', taken)
                    taken.add(name_key(target))
                    pending_notes[digest] = target
                    desc = ('Inline notes from a legacy memory index (%s) — review and split into '
                            'memories' % display_path(sreal))
                    writes.append((os.path.join(memdir, target),
                                   notes_memory(target[:-3], desc, block)))
                    entry['__inline__'] = collections.OrderedDict([('sha256', digest),
                                                                   ('target', target)])
                    # the index hook omits the (long) source path so the line stays < 150 chars
                    new_lines.append((index_line(target[:-3], target, NOTES_HOOK), False))
                    linked.add(nfc(target))
                    counts['notes'] += 1
                    report.append('notes: inline index notes of %s → %s' % (src_index, target))
        for fp in files:
            freal = os.path.realpath(fp)
            try:
                data = read_bytes(fp)
            except OSError as e:
                report.append('skipped %s (%s)' % (fp, e))
                counts['skipped'] += 1
                continue
            digest = sha256(data)
            entry = manifest.setdefault(freal, collections.OrderedDict()) if not dry else \
                copy.deepcopy(manifest.get(freal, collections.OrderedDict()))
            if any(isinstance(v, dict) and v.get('sha256') == digest for v in entry.values()):
                report.append('skipped %s (already imported)' % fp)
                counts['skipped'] += 1
                continue
            target = free_name('imported-notes', taken)
            taken.add(name_key(target))
            desc = 'Notes imported from %s — review and split into memories' % display_path(freal)
            writes.append((os.path.join(memdir, target),
                           notes_memory(target[:-3], desc, dec(data).lstrip('\ufeff'))))
            entry[os.path.basename(freal)] = collections.OrderedDict([('sha256', digest),
                                                                      ('target', target)])
            new_lines.append((index_line(target[:-3], target, 'Notes imported from %s — review and '
                                         'split into memories' % os.path.basename(freal)[:40]), False))
            linked.add(nfc(target))
            counts['notes'] += 1
            report.append('notes: %s → %s' % (fp, target))
        prefix = '[dry run] ' if dry else ''
        for line in report:
            print(prefix + line)
        if not dry:
            for src_path, dest, data in copies:
                if not create_exclusive(dest, data, memdir):
                    warn('%s appeared meanwhile; not overwritten (rerun `pmem import`)' % dest)
                    continue
                with contextlib.suppress(OSError):
                    shutil.copystat(src_path, dest)
            for d, content in writes:
                if not create_exclusive(d, enc(content), memdir):
                    warn('%s appeared meanwhile; not overwritten (rerun `pmem import`)' % d)
            if copies or writes or counts['identical']:
                manifest = collections.OrderedDict((k, v) for k, v in manifest.items() if v)
                atomic_write(os.path.join(memdir, '.imported.json'),
                             enc(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'),
                             memdir=memdir)
            if new_lines:
                if text is None:
                    text, bom, idx_lines = '', False, to_lines(MEMORY_TEMPLATE)
                eol = eol_of(idx_lines)
                prefs, others = split_import_lines(idx_lines, new_lines)
                if prefs:      # feedback memories go to the end of the Preferences section
                    section_insert(idx_lines, eol, pref_section(idx_lines),
                                   [[l, eol] for l in prefs])
                if others:
                    if idx_lines and idx_lines[-1][0].strip():
                        append_line(idx_lines, '', eol)
                    append_line(idx_lines, '## Imported %s' % today(), eol)
                    for l in others:
                        append_line(idx_lines, l, eol)
                atomic_write(os.path.join(memdir, 'MEMORY.md'),
                             enc(('\ufeff' if bom else '') + from_lines(idx_lines)), memdir=memdir)
        elif new_lines:
            prefs, others = split_import_lines(idx_lines or to_lines(MEMORY_TEMPLATE), new_lines)
            if prefs:
                print('would add to MEMORY.md under "## Preferences":')
                for l in prefs:
                    print('  ' + l)
            if others:
                print('would append to MEMORY.md under "## Imported %s":' % today())
                for l in others:
                    print('  ' + l)
    print('%ssummary: %s' % ('(dry run, nothing written) ' if dry else '',
                             ', '.join('%s %d' % kv for kv in counts.items())))
    if counts['needs-merge'] or counts['notes']:
        print('next: merge the *-imported* files and split imported-notes* into memories '
              '(memory-health), then `pmem lint`')
    if srcs and not dry:
        print('pmem never modifies sources. After review, retire an old dir by moving it aside, '
              'e.g.: mv <dir> <dir>.imported-%s' % today())
    if not dry:
        snapshot(memdir, 'import')
    return 0


# ---------------------------------------------------------------------------------------------
# Commands: wire

def backup_file(path, data):
    """``<path>.bak-<stamp>`` (O_EXCL, -2, -3 on collision) with the original file's mode."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = 0o600
    base = '%s.bak-%s' % (path, time.strftime('%Y%m%d%H%M%S'))
    cand, n = base, 2
    while True:
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                         0o600)
            break
        except FileExistsError:
            cand = '%s-%d' % (base, n)
            n += 1
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        os.fchmod(f.fileno(), mode)
    return cand


def wire_text(raw, remove):
    """Pure wire state machine. -> new bytes (or raises PmemError with marker line numbers)."""
    bom, lines = text_lines(raw)
    eol = eol_of(lines)
    problem = block_problem(lines)   # the preferences block is checked too, never rewritten
    if problem:
        raise PmemError(problem + ('' if 'by hand' in problem else '; fix the file by hand')
                        + ' — nothing written', 1)
    contents = [c for c, _ in lines]
    begins, ends = marker_lines(contents, WIRE_BEGIN), marker_lines(contents, WIRE_END)
    block = [[l, eol] for l in WIRE_BLOCK.rstrip('\n').split('\n')]
    if remove:
        if not begins:
            return raw
        b, e = begins[0], ends[0]
        start = b - 1 if b > 0 and not lines[b - 1][0].strip() else b
        no_final = lines[e][1] == '' and e == len(lines) - 1
        stop = e + 1
        if start == 0 and stop < len(lines) and not lines[stop][0].strip():
            stop += 1       # block at the top (e.g. before the preferences block): no leading blank
        new = lines[:start] + lines[stop:]
        if no_final and new:
            new[-1][1] = ''
    elif begins:
        b, e = begins[0], ends[0]
        if lines[e][1] == '':
            block[-1][1] = ''
        new = lines[:b] + block + lines[e + 1:]
    elif not lines:
        new = block
    else:
        new = [list(x) for x in lines]
        no_final = new[-1][1] == ''
        if no_final:
            new[-1][1] = eol
        new.append(['', eol])
        new += block
        if no_final:
            new[-1][1] = ''
    return (b'\xef\xbb\xbf' if bom else b'') + enc(from_lines(new))


def wire_one(path, remove):
    exists = os.path.exists(path)
    raw = read_bytes(path) if exists else b''
    try:
        new = wire_text(raw, remove)
    except PmemError as e:
        raise PmemError('%s: %s' % (path, e), e.code)
    if remove and exists:
        g = GlobalFile(path)
        if g.sub:
            n = len(g.entries())
            print('kept the global preferences block (%d rules) in %s; remove rules with `pmem pref '
                  'demote`' % (n, path))
    if new == raw and (exists or remove):
        print('ok %s' % path)
        return 0
    if exists:
        print('backup %s' % backup_file(path, raw))
    else:
        os.makedirs(os.path.dirname(os.path.realpath(path)), exist_ok=True)
    atomic_write(path, new)
    if remove:
        print('removed block from %s' % path)
    else:
        _, old_lines = text_lines(raw)
        had = marker_lines([c for c, _ in old_lines], WIRE_BEGIN)
        print('%s %s' % ('updated' if had else 'inserted block into', path))
    return 0


def cmd_wire(args):
    target = args.target
    if target == 'codex':
        paths = [codex_agents_path()]
    elif target == 'claude':
        dirs = []
        for d in args.config_dir or [claude_config_dirs()[0]]:
            d = os.path.abspath(os.path.expanduser(d))
            if d not in dirs:
                dirs.append(d)
        paths = [os.path.join(d, 'CLAUDE.md') for d in dirs]
    else:
        base = os.path.abspath(os.path.expanduser(args.C)) if args.C else os.getcwd()
        paths = [os.path.join(base, os.path.expanduser(target))]
    if args.config_dir and target != 'claude':
        raise PmemError('--config-dir only applies to `pmem wire claude`', 2)
    rc = 0
    for p in paths:
        try:
            if os.path.isdir(p):
                raise PmemError('%s is a directory' % p, 1)
            rc = max(rc, wire_one(p, args.remove))
        except PmemError as e:
            eprint('pmem: ' + str(e))
            rc = max(rc, e.code)
        except OSError as e:
            eprint('pmem: %s: %s' % (p, e))
            rc = max(rc, 1)
    return rc


# ---------------------------------------------------------------------------------------------
# Commands: snapshot, diff, history, restore

def require_history(ctx):
    memdir = require_memory(ctx)
    state, why = history_state(memdir)
    if state == 'broken':
        raise PmemError('private history broken: %s — fix or move it away; pmem never uses '
                        'another repository' % why, 1)
    if state != 'on':
        raise PmemError('no private history in %s (created by `pmem init` unless --track or '
                        '--no-history)' % memdir, 1)
    return memdir


def cmd_snapshot(args):
    memdir = require_history(resolve(args.C))
    res = snapshot(memdir, args.message or 'snapshot')
    if res is None:
        return 1
    if res is False:
        print('nothing to snapshot')
        return 0
    r = mgit(memdir, ['log', '-1', '--format=%h %s'])
    print('snapshot %s' % (r.out.strip() if r is not None and r.rc == 0 else ''))
    return 0


def diff_base(memdir, args):
    """-> (full sha, description) of the baseline for --since / --since-health."""
    if args.since_health:
        r = mgit(memdir, ['log', '-1', '--format=%H', '--grep=^memory-health', 'HEAD'])
        base = r.out.strip() if r is not None and r.rc == 0 else ''
        what = 'the last memory-health snapshot'
        if not base:
            r = mgit(memdir, ['rev-list', '--max-parents=0', 'HEAD'])
            roots = r.out.split() if r is not None and r.rc == 0 else []
            base = roots[-1] if roots else ''
            what = 'the first snapshot (no memory-health snapshot yet)'
        if not base:
            raise PmemError('no snapshots yet', 1)
        return base, what
    r = mgit(memdir, ['rev-parse', '-q', '--verify', args.since + '^{commit}'])
    if r is None or r.rc != 0 or not r.out.strip():
        raise PmemError('--since: unknown revision %r in the private history (see `pmem history`)'
                        % args.since, 2)
    return r.out.strip(), args.since


def cmd_diff(args):
    memdir = require_history(resolve(args.C))
    r = mgit(memdir, ['status', '--porcelain', '-uall'])
    if r is None or r.rc != 0:
        raise PmemError('git status failed: %s' % (r.err.strip() if r else 'git missing'), 1)
    status_rows = [l for l in r.out.split('\n') if l.strip()]
    if args.since or args.since_health:
        base, what = diff_base(memdir, args)
        r = mgit(memdir, ['log', '-1', '--format=%h %s', base])
        print('changes since %s (%s):' % (what, r.out.strip() if r is not None and r.rc == 0
                                          else base[:12]))
        r = mgit(memdir, ['diff', '--name-status', '--no-renames', base])
        if r is None or r.rc != 0:
            raise PmemError('git diff failed: %s' % (r.err.strip() if r else 'git missing'), 1)
        names = {'A': 'added', 'D': 'deleted', 'M': 'modified', 'T': 'modified'}
        rows = []
        for l in r.out.split('\n'):
            if '\t' in l:
                code, path = l.split('\t', 1)
                rows.append((names.get(code[:1], 'modified'), path))
        rows += [('added', l[3:]) for l in status_rows if l.startswith('??')]
        if not rows:
            print('no changes')
            return 0
        for kind, path in sorted(rows, key=lambda x: x[1]):
            print('%-9s %s' % (kind, path))
        st = mgit(memdir, ['diff', '--stat', base])
        if st is not None and st.rc == 0 and st.out.strip():
            print()
            print(st.out.rstrip('\n'))
        return 0
    if not status_rows:
        print('no changes since the last snapshot')
        return 0
    for l in status_rows:
        code, path = l[:2], l[3:]
        if code == '??' or 'A' in code:
            kind = 'added'
        elif 'D' in code:
            kind = 'deleted'
        elif 'R' in code:
            kind = 'renamed'
        else:
            kind = 'modified'
        print('%-9s %s' % (kind, path))
    head = mgit(memdir, ['rev-parse', '-q', '--verify', 'HEAD'])
    if head is not None and head.rc == 0:
        st = mgit(memdir, ['diff', '--stat', 'HEAD'])
        if st is not None and st.rc == 0 and st.out.strip():
            print()
            print(st.out.rstrip('\n'))
    return 0


def memory_relpath(name):
    """NAME -> NAME.md; relative paths (HANDOFF.md, handoffs/x.md, .archive/x.md) kept."""
    rel = name if ('/' in name or name.endswith('.md')) else name + '.md'
    norm = os.path.normpath(rel)
    if os.path.isabs(rel) or norm == os.pardir or norm.startswith(os.pardir + os.sep):
        raise PmemError('path must stay inside the memory dir: %r' % name, 2)
    return norm


def cmd_history(args):
    memdir = require_history(resolve(args.C))
    cmd = ['log', '--oneline', '--stat', '-n', str(args.n)]
    if args.name:
        cmd += ['--', memory_relpath(args.name)]
    r = mgit(memdir, cmd)
    if r is None or r.rc != 0:
        out = r.err.strip() if r else 'git missing'
        if 'does not have any commits' in out:
            print('no snapshots yet')
            return 0
        raise PmemError('git log failed: %s' % out, 1)
    print(r.out.rstrip('\n') if r.out.strip() else 'no history')
    return 0


def cmd_restore(args):
    ctx = resolve(args.C)
    memdir = require_history(ctx)
    rel = memory_relpath(args.name)
    gpath = rel.replace(os.sep, '/')
    path = os.path.join(memdir, rel)
    rev = args.rev
    if rev is None:
        r = mgit(memdir, ['rev-list', '-n', '1', 'HEAD', '--', gpath])
        last = r.out.strip() if r is not None and r.rc == 0 else ''
        if not last:
            raise PmemError('%s never appears in the private history' % rel, 1)
        e = mgit(memdir, ['cat-file', '-e', '%s:%s' % (last, gpath)])
        rev = last[:12] if (e is not None and e.rc == 0) else last[:12] + '^'
    kind = mgit(memdir, ['cat-file', '-t', '%s:%s' % (rev, gpath)])
    if kind is None or kind.rc != 0:
        raise PmemError('cannot read %s at %s: %s' % (rel, rev, kind.err.strip() if kind else ''), 1)
    if kind.out.strip() != 'blob':
        raise PmemError('%s at %s is a %s, not a file — restore the files in it one by one (see '
                        '`pmem history`)' % (rel, rev, kind.out.strip()), 2)
    show = mgit(memdir, ['show', '%s:%s' % (rev, gpath)], raw=True)
    if show is None or show.rc != 0:
        raise PmemError('cannot read %s at %s: %s' % (rel, rev, show.err.strip() if show else ''), 1)
    data = show.out
    guard(memdir, path)
    indexed = (os.path.dirname(rel) == '' and rel.endswith('.md')
               and rel[:-3].lower() not in RESERVED_STEMS)
    if indexed:
        read_index(memdir)            # an unreadable index stops us before any write
    if os.path.lexists(path) and not args.force:
        st = mgit(memdir, ['status', '--porcelain', '--', gpath])
        if st is not None and st.rc == 0 and st.out.strip():
            raise PmemError('%s has changes not in a snapshot — `pmem snapshot` first or rerun with '
                            '--force' % rel, 2)
    if os.path.isfile(path) and read_bytes(path) == data:
        print('ok %s already matches %s' % (rel, rev))
    else:
        guard(memdir, os.path.dirname(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, data, memdir=memdir)
        print('restored %s from %s' % (rel, rev))
    if indexed:
        def edit(lines, eol):
            masked = mask_lines([c for c, _ in lines])
            sec = pref_section(lines, masked)
            mine = [e for e in section_entries(lines, sec, masked)
                    if e.target and nfc(e.target) == nfc(rel)] if sec else []
            if mine:
                return 'kept'
            if any(nfc(t) == nfc(rel) for _, t in canonical_lines(lines, masked)):
                return False
            if sec and memory_type(memdir, rel) == 'feedback':
                section_insert(lines, eol, sec, [[pref_index_line(path), eol]])
            else:
                append_ordinary(lines, eol, generated_index_line(path))
            return True
        res = update_index(memdir, edit)
        if res == 'kept':
            print('kept preference line for %s' % rel)
        elif res:
            print('re-added the index line for %s' % rel)
    snapshot(memdir, 'restore %s from %s' % (rel, rev))
    return 0


# ---------------------------------------------------------------------------------------------
# Commands: sessions

def claude_messages(path, n):
    msgs = collections.deque(maxlen=n)
    with open(path, 'rb') as f:
        for raw in f:
            if b'"user"' not in raw and b'"assistant"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get('type') not in ('user', 'assistant') \
                    or obj.get('isMeta'):
                continue
            msg = obj.get('message')
            content = msg.get('content') if isinstance(msg, dict) else None
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get('text') for b in content if isinstance(b, dict)
                         and b.get('type') == 'text' and isinstance(b.get('text'), str)]
            else:
                texts = []
            text = '\n'.join(t for t in texts if t.strip())
            if text.strip():
                msgs.append((obj['type'], text))
    return list(msgs)


CODEX_SKIP_PREFIXES = ('<environment_context>', '<user_instructions>', '# AGENTS.md instructions')


def codex_messages(path, n):
    msgs = collections.deque(maxlen=n)
    with open(path, 'rb') as f:
        for raw in f:
            if b'response_item' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            p = obj.get('payload')
            if obj.get('type') != 'response_item' or not isinstance(p, dict) \
                    or p.get('type') != 'message' or p.get('role') not in ('user', 'assistant'):
                continue
            content = p.get('content')
            texts = [c.get('text') for c in (content if isinstance(content, list) else [])
                     if isinstance(c, dict) and c.get('type') in ('input_text', 'output_text')
                     and isinstance(c.get('text'), str)]
            text = '\n'.join(t for t in texts if t.strip())
            if text.strip() and not text.lstrip().startswith(CODEX_SKIP_PREFIXES):
                msgs.append((p['role'], text))
    return list(msgs)


def codex_session_cwd(path):
    try:
        with open(path, 'rb') as f:
            obj = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if isinstance(obj, dict) and obj.get('type') == 'session_meta':
        p = obj.get('payload')
        if isinstance(p, dict) and isinstance(p.get('cwd'), str):
            return p['cwd']
    return None


def checkpoint_config_dirs(memdir):
    out = []
    cdir = os.path.join(memdir, '.checkpoints') if memdir else None
    if not cdir or not os.path.isdir(cdir):
        return out
    if os.path.islink(cdir):
        return out
    for fn in sorted(os.listdir(cdir)):   # current and .prev.md checkpoints
        fp = os.path.join(cdir, fn)
        if os.path.islink(fp):
            continue
        try:
            text = read_text(fp)
        except OSError:
            continue
        for line in text.split('\n'):
            if line.startswith('- transcript:'):
                t = line.split(':', 1)[1].strip()
                proj = os.path.dirname(os.path.dirname(t))
                if t and os.path.basename(proj) == 'projects':
                    out.append(os.path.dirname(proj))
    return out


def session_key(cwd, ctx):
    """Workstream key of a transcript from its cwd: a linked worktree's key, else ``main``."""
    if not cwd:
        return '?'
    variants = [os.path.normpath(cwd)]
    if os.path.exists(cwd):
        variants.append(os.path.realpath(cwd))
    for w in ctx.git.worktrees()[1:]:
        if w['bare']:
            continue
        roots = set([w['real'], os.path.normpath(w['path'])])
        if any(is_under(v, r) for v in variants for r in roots):
            return worktree_key(w['real'])
    return 'main'


def cmd_sessions(args):
    ctx = resolve(args.C)
    cutoff = None
    if args.since:
        cutoff = parse_ts(args.since)
        if cutoff is None:
            raise PmemError('--since: expected YYYY-MM-DD[THH:MM[:SS]][Z|+HH:MM], got %r' % args.since,
                            2)
    only_key = None
    if args.since_handoff:
        memdir = require_memory(ctx)
        hp = handoff_path(memdir, workstream_key(ctx))
        if os.path.isfile(hp):
            ts, _, _ = MemFile(hp, memdir).handoff_timestamp()
            if ts is not None:
                cutoff = ts if cutoff is None else max(cutoff, ts)
        only_key = ctx.checkout_key   # transcripts know their cwd, not PMEM_KEY
    limit = max(1, args.limit)
    cands = []
    seen = set()
    cfgs = claude_config_dirs(list(args.config_dir) + checkpoint_config_dirs(ctx.memdir), both=True)
    roots = project_roots(ctx)
    exact = []
    for p in project_paths(ctx):
        if slug(p) not in exact:
            exact.append(slug(p))
    for cfg in cfgs:
        # exact launch dirs as they are; sub-directory launches only if their cwd is ours
        dirs = [(n, False) for n in exact] + [(n, True) for n in sub_project_dirs(cfg, roots, exact)]
        for name, check in dirs:
            d = os.path.join(cfg, 'projects', name)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                fp = os.path.join(d, fn)
                if not fn.endswith('.jsonl') or not os.path.isfile(fp):
                    continue
                r = os.path.realpath(fp)
                if r in seen:
                    continue
                try:
                    m = os.stat(fp).st_mtime
                except OSError:
                    continue
                if cutoff is not None and m <= cutoff:
                    continue
                seen.add(r)
                cands.append((m, 'claude', fp, check))
    codex_dir = os.path.join(os.path.dirname(codex_agents_path()), 'sessions')
    for dirpath, _, fnames in os.walk(codex_dir):
        for fn in fnames:
            if fn.startswith('rollout-') and fn.endswith('.jsonl'):
                fp = os.path.join(dirpath, fn)
                try:
                    m = os.stat(fp).st_mtime
                except OSError:
                    continue
                if cutoff is None or m > cutoff:
                    cands.append((m, 'codex', fp, True))
    cands.sort(key=lambda c: c[0], reverse=True)
    found = []
    for m, agent, fp, check in cands:
        if len(found) >= limit:
            break
        cwd = claude_transcript_cwd(fp) if agent == 'claude' else codex_session_cwd(fp)
        if check and (not cwd or not under_any(cwd, roots)):
            continue
        key = session_key(cwd, ctx)
        if only_key is not None and key not in (only_key, '?'):
            continue
        found.append((m, agent, fp, key))
    if not found:
        print('no agent sessions found for this project%s%s' % (
            ' since %s' % fmt_epoch(cutoff) if cutoff is not None else '',
            ' in workstream %s' % only_key if only_key else ''))
        return 1
    width = max(len(f[3]) for f in found)
    for m, agent, fp, key in found:
        print('%-7s %s  %-*s  %s' % (agent, fmt_epoch(m), width, key, fp))
        if args.tail:
            try:
                msgs = (claude_messages if agent == 'claude' else codex_messages)(fp, args.tail)
            except OSError as e:
                print('    (cannot read: %s)' % e)
                continue
            for role, text in msgs:
                t = oneline(clean_text(text))
                if len(t) > SESSION_MSG_CHARS:
                    t = t[:SESSION_MSG_CHARS - 1] + '…'
                print('    [%s] %s' % (role, t))
    return 0


# ---------------------------------------------------------------------------------------------
# Hooks (never raise, always exit 0, print nothing on failure)

def read_hook_input(wait=2.0):
    """Hook JSON from stdin; {} when empty/invalid. A stdin that stays open (with or without
    data) never hangs the hook: stop at EOF, at the first complete JSON value, or after ``wait``."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        try:
            import select
            fd = sys.stdin.fileno()
            select.select([fd], [], [], 0)
        except Exception:  # no selectable fd: plain blocking read
            raw = sys.stdin.buffer.read()
        else:
            raw, deadline = b'', time.time() + wait
            while True:
                left = deadline - time.time()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    break
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
                try:
                    json.loads(raw.decode('utf-8', 'replace'))
                    break
                except ValueError:
                    continue
        data = json.loads(raw.decode('utf-8', 'replace') or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def hook_context(data, arg_dir):
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else None
    for d in (cwd, arg_dir, None):
        if d is None or os.path.isdir(d):
            try:
                return resolve(d)
            except PmemError:
                continue
    return None


def spawn_snapshot(memdir, message):
    """Snapshot in a detached background process (no kill-timeout on a writing git; the hook
    returns at once). The child validates the history itself."""
    try:
        subprocess.Popen([sys.executable or 'python3', os.path.realpath(__file__), 'hook', 'snapshot',
                          '--memdir', memdir, '-m', message],
                         cwd=memdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except Exception:
        pass


def hook_session_start(args):
    data = read_hook_input()
    ctx = hook_context(data, args.C)
    if ctx is None or not ctx.memdir or not os.access(ctx.memdir, os.R_OK | os.X_OK):
        return
    source = str(data.get('source') or '').strip()
    sid = str(data.get('session_id') or '')[:8]
    if os.path.isdir(os.path.join(ctx.memdir, '.git')):
        spawn_snapshot(ctx.memdir, ' '.join(x for x in ('session-start', source, sid) if x))
    text = assemble_context(ctx, ctx.key, with_index=args.with_index, cap=HOOK_CONTEXT_CAP,
                            native_check=True, with_prefs=True)
    payload = {'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                      'additionalContext': clean_text(text)}}
    sys.stdout.write(json.dumps(payload) + '\n')   # ASCII-only: valid under any stdout encoding
    sys.stdout.flush()


def hook_snapshot(args):
    memdir = os.path.realpath(args.memdir or '')
    if not os.path.isdir(memdir) or rejected_root(memdir, home_dir()):
        return
    if not os.path.isfile(os.path.join(memdir, 'MEMORY.md')):
        return
    snapshot(memdir, args.message or 'session-start')


def git_state(cwd):
    branch = head = dirty = '-'
    r = git(['-C', cwd, 'symbolic-ref', '--short', '-q', 'HEAD'])
    if r is not None and r.rc == 0 and r.out.strip():
        branch = r.out.strip()
    elif r is not None and r.rc == 1:
        branch = '(detached)'
    r = git(['-C', cwd, 'rev-parse', '--short', 'HEAD'])
    if r is not None and r.rc == 0:
        head = r.out.strip()
    r = git(['-C', cwd, 'status', '--porcelain'])
    if r is not None and r.rc == 0:
        dirty = str(len([l for l in r.out.split('\n') if l.strip()]))
    return branch, head, dirty


def hook_checkpoint(args):
    data = read_hook_input()
    event = str(data.get('hook_event_name') or '')
    if event == 'Stop' and data.get('stop_hook_active') is True:
        return
    ctx = hook_context(data, args.C)
    if ctx is None or not ctx.memdir:
        return
    memdir = ctx.memdir
    path = checkpoint_path(memdir, ctx.key)
    prev_path = checkpoint_path(memdir, ctx.key, prev=True)
    try:
        guard(memdir, path)
        guard(memdir, prev_path)
    except PmemError:
        return                       # symlinks inside the memory dir: never written through
    sid = str(data.get('session_id') or '').strip()
    msg = data.get('last_assistant_message')
    msg = msg.rstrip('\n') if isinstance(msg, str) and msg.strip() else None
    old = read_checkpoint(memdir, ctx.key)
    rotate = bool(old and old['session_id'] and sid and old['session_id'] != sid)
    if old and not rotate and msg is None and old['tail'] and old['tail'] != '(none)':
        msg = old['tail']            # e.g. PreCompact: keep the same session's last message
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else ctx.start
    branch, head, dirty = git_state(ctx.start) if ctx.git.is_git else ('-', '-', '-')
    lines = ['# Auto checkpoint (%s)' % ctx.key, '',
             '- time: %s' % now_local_iso(),
             '- event: %s' % (event or '-')]
    if event == 'StopFailure':
        err = oneline(str(data.get('error') or ''))
        details = oneline(str(data.get('error_details') or ''))
        lines.append('- error: %s%s' % (err or '-', (' — ' + details[:300]) if details else ''))
    lines += ['- agent: claude-code',
              '- session_id: %s' % (sid or '-'),
              '- transcript: %s' % (data.get('transcript_path') or '-'),
              '- cwd: %s' % cwd,
              '- branch: %s' % branch,
              '- head: %s' % head,
              '- dirty files: %s' % dirty,
              '', CHECKPOINT_TAIL_HEAD, '']
    lines.append((msg or '(none)')[-CHECKPOINT_TAIL:].rstrip('\n'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if rotate:   # another session took over: keep the previous one's checkpoint
        os.replace(path, prev_path)
    atomic_write(path, enc(clean_text('\n'.join(lines) + '\n')), memdir=memdir)


PROMPT_SKIP_SOURCES = ('system', 'loop_wakeup', 'schedule_wakeup', 'poll_event')
# Signals of `pmem hook prompt`, matched on prompt_text() (NFKC: `，？！：；` arrive as `,?!:;`).
SIGNAL_STRONG_RE = re.compile(
    r'今后|今後|下次|一律|永远不要|永遠不要|永远别|永遠別|别再|別再|不要再|再也别|再也別|记住|記住'
    r'|我(?:更|不)?(?:喜欢|喜歡|习惯|習慣|偏好)|我更?希望你'
    r"|from now on|going forward|next time|\bi(?:'d| would)? prefer\b"
    r"|\bi (?:don'?t )?like (?:it )?when you|never again|don'?t ever|remember (?:that|to)"
    r'|\bi (?:already )?said\b'
    r'|\bstop (?:using|adding|ending|writing|putting|making|doing)\b'
    r'|\bnever (?:use|add|write|put|end)\b', re.I)
# violation markers that count on their own (each is a strong signal too)
SIGNAL_VIOLATION_RE = re.compile(r"又(?:这样|這樣|忘|来|來|写|寫|用)|\bagain\s*!|\btold you\b|\bas i said\b",
                                 re.I)
# 说过/讲过 = "I told you" only as 我…说过 / 跟你说过 and with 你 in the message (你说过 = "you said")
SIGNAL_SAID_RE = re.compile(r'(?:我(?:已经|已經|早就|之前|刚才|剛才|都|明明|不是)?(?:(?:跟|和|对|對|给|給)你)?'
                            r'|(?:跟|和|对|對|给|給)你)(?:说|說|讲|講)(?:过|過)')
SIGNAL_AGAIN_RE = re.compile(r'\bagain\b', re.I)       # with you/your in its clause, not a request
SIGNAL_REQUEST_RE = re.compile(r"^\s*(?:(?:can|could|would|will) you|try|let'?s)\b|\bplease\b", re.I)
# 以后/以後 = "from now on" only at the start of a clause or right before one of these words, and
# never after 完/了/好/之 (改完以后 = "after changing")
SIGNAL_YIHOU_RE = re.compile(r'(?<![完了好之])(?:以后|以後)')
SIGNAL_YIHOU_NEXT = ('都', '不要', '别', '別', '請', '请', '回答', '回复', '回覆', '写', '寫', '用',
                     '一律', '统一', '統一', '所有', '每次')
SIGNAL_TONGYI_RE = re.compile(r'(?:统一|統一)(?:用|成)')      # not after 把 in its clause (a task)
SIGNAL_DEFAULT_RE = re.compile(r'默认(?:都|用)|默認(?:都|用)|\bby default\b', re.I)  # not in a question
SIGNAL_EVERY_RE = re.compile(r'每次|\bevery time\b', re.I)  # not in a question; needs an agent reference
SIGNAL_STYLE_RE = re.compile(
    r'太长|太長|啰嗦|囉嗦|囉唆|写太多|寫太多|太多了|简洁一?点|簡潔一?點|简短一?点|簡短一?點|凝练|凝練'
    r'|too (?:long|verbose|wordy)|more concise|shorter (?:answers|replies)', re.I)
SIGNAL_WEAK_RE = re.compile(r"\bstop\b|\binstead\b|\balways\b|\bnever\b|\bdon'?t\b|不对|不對|错了|錯了"
                            r'|别用|別用|不要用|请简洁|請簡潔|簡潔', re.I)   # not in a question
SIGNAL_AGENT_RE = re.compile(r'\byou\b|\byour\b|你|回复|回覆|回答', re.I)
SIGNAL_YOU_RE = re.compile(r'\byou\b|\byour\b|你', re.I)
SIGNAL_QUESTION_RE = re.compile(
    r'\?|吗|嗎|哪|什么|什麼|是不是|为什么|為什麼|\b(?:what|which|why)\b'
    r'|^\s*(?:can|could|would|will|do|does|did|is|are)\s+(?:you|it|i|we|this|that|there)\b', re.I)
SENTENCE_SPLIT_RE = re.compile(r'(?<=[!?。])|(?<=\.)(?=\s)|\n')
CLAUSE_SPLIT_RE = re.compile(r'[,;:!?。、\n]|\.(?=\s|$)')
CLAUSE_START = ',;:.!?。、 \t\n'
PASTED_RE = re.compile(r'<pasted_content\b[^>]*>.*?(?:</pasted_content\s*>|\Z)', re.I | re.S)
NUDGE = ('project-memory: this message may state how the user wants you to work beyond this task. If '
         'it does, save it now by editing {index}: add `- [<title>](<name>.md) — <rule>` under '
         '## Preferences and write <name>.md as a feedback memory (Why / How to apply); then end '
         'your reply with `✓ preference: <rule>`. Task-specific instructions are not preferences: '
         'just follow them. Otherwise ignore this note.')
# After a violation marker the violation part leads, followed by a shorter capture note. Without
# {rules} each variant stays within PROMPT_NUDGE_CHARS (the path is shortened to fit); the rules
# have their own budget: up to NUDGE_RULES rules of at most NUDGE_RULE_CHARS each.
NUDGE_VIOLATION = ('project-memory: the user says you broke an existing preference: {rules}. Sharpen '
                   'that rule line, update its description, move it to the top of its ## Preferences '
                   'and add `- {date}: <what happened>` under **Violations:** in its file (for a rule '
                   'marked (from <dir>) edit that outer MEMORY.md).')
NUDGE_ALSO = (' If the message also states a new lasting preference, save it too: `- [<title>](<name>.md)'
              ' — <rule>` under ## Preferences in {index} plus <name>.md as a feedback memory (Why / '
              'How to apply); then end your reply with `✓ preference: <rule>`.')
NUDGE_NO_RULES = 'see ## Preferences in {index}'
NUDGE_RULES, NUDGE_RULE_CHARS, NUDGE_LABEL_CHARS = 3, 160, 60
RANK_STOPWORDS = frozenset((
    'the and you your yours for not but with this that these those are was were have has had use '
    'used uses using don didn doesn can will would should could again told said please from now '
    'always never when what which there they them then than into just like also any all one out '
    'get got its our about every time too very more most some such only each here how why who '
    'did does done make made want wanted').split())
RANK_CJK_STOP = frozenset(('不要', '不用', '我说', '说过', '过了', '過了', '你又', '又用', '用了', '以后',
                           '一下', '这个', '那个', '我们', '你们', '的话', '我跟', '跟你', '了不'))
# Topics that preference rules and complaints share across English and Chinese, so that
# "我说过不要用表格" ranks "Never use Markdown tables" first: each English word (after rank_tokens'
# plural cut) or CJK term adds the topic's token '#<topic>'.
RANK_TOPICS = {
    'table': ('table', '表格'),
    'chinese': ('chinese', '中文', '汉语', '漢語'),
    'english': ('english', '英文', '英语', '英語'),
    'recap': ('recap', 'summary', 'summarize', 'summarise', '总结', '總結', '小结', '小結', '摘要'),
    'length': ('long', 'short', 'shorter', 'concise', 'brief', 'verbose', 'wordy', 'length',
               'sentence', '太长', '太長', '简洁', '簡潔', '简短', '簡短', '啰嗦', '囉嗦', '凝练',
               '凝練', '篇幅', '字数', '字數'),
    'comment': ('comment', '注释', '註釋'),
    'commit': ('commit', '提交'),
    'test': ('test', '测试', '測試'),
    'emoji': ('emoji', '表情'),
    'list': ('list', 'bullet', '列表', '要点', '要點'),
    'heading': ('heading', 'header', '标题', '標題'),
    'docstring': ('docstring', '文档字符串', '文檔字符串'),
}
RANK_TOPIC_WORDS = {w: t for t, ws in RANK_TOPICS.items() for w in ws if w.isascii()}
RANK_TOPIC_CJK = tuple((w, t) for t, ws in RANK_TOPICS.items() for w in ws if not w.isascii())


def prompt_text(prompt):
    """The prompt without pasted content and fenced code blocks (a fence that never closes runs
    to the end, as in CommonMark), NFKC-normalized, curly apostrophes mapped to '."""
    text = PASTED_RE.sub(' ', prompt)
    contents = text.split('\n')
    flags = fence_flags(contents, eof_closes=True)
    text = '\n'.join(c for c, f in zip(contents, flags) if not f).strip()
    text = unicodedata.normalize('NFKC', text)
    for ch in '’‘＇ʼ':
        text = text.replace(ch, "'")
    return text


def _parts(regex, text):
    return [p for p in regex.split(text) if p and p.strip()]


def _yihou(text):
    """以后/以後 used as "from now on" (see SIGNAL_YIHOU_RE)."""
    for m in SIGNAL_YIHOU_RE.finditer(text):
        i, j = m.start(), m.end()
        if i == 0 or text[i - 1] in CLAUSE_START or text.startswith(SIGNAL_YIHOU_NEXT, j):
            return True
    return False


def _tongyi(clause):
    m = SIGNAL_TONGYI_RE.search(clause)
    return bool(m) and '把' not in clause[:m.start()]


def prompt_signals(text):
    """-> (matched, violation marker matched). ``text``: see prompt_text."""
    sentences = _parts(SENTENCE_SPLIT_RE, text)
    clauses = _parts(CLAUSE_SPLIT_RE, text)

    def statement(part):
        return not SIGNAL_QUESTION_RE.search(part)
    violation = bool(
        SIGNAL_VIOLATION_RE.search(text)
        or (SIGNAL_SAID_RE.search(text) and SIGNAL_YOU_RE.search(text))
        or any(SIGNAL_AGAIN_RE.search(c) and SIGNAL_YOU_RE.search(c)
               and not SIGNAL_REQUEST_RE.search(c) for c in clauses))
    matched = (violation or bool(SIGNAL_STRONG_RE.search(text) or SIGNAL_STYLE_RE.search(text))
               or _yihou(text) or any(_tongyi(c) for c in clauses)
               or any(SIGNAL_DEFAULT_RE.search(s) and statement(s) for s in sentences)
               or any(SIGNAL_EVERY_RE.search(s) and statement(s) and SIGNAL_AGENT_RE.search(s)
                      for s in sentences)
               or (bool(SIGNAL_AGENT_RE.search(text))
                   and any(SIGNAL_WEAK_RE.search(s) and statement(s) for s in sentences)))
    return matched, violation


def prompt_rate_ok(memdir, sid):
    """At most one nudge per session per PROMPT_INTERVAL; records this one. False also when the
    state file cannot be written (then no nudge at all)."""
    sid8 = re.sub(r'[^A-Za-z0-9_-]', '', sid)[:8] or 'none'
    path = os.path.join(memdir, '.checkpoints', '.prompt-' + sid8)
    try:
        guard(memdir, path)
        now = time.time()
        try:
            last = float(read_text(path).strip() or 0)
        except FileNotFoundError:
            last = 0.0
        if 0 <= now - last < PROMPT_INTERVAL:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, enc('%d\n' % int(now)), memdir=memdir)
    except (OSError, ValueError, PmemError):
        return False
    try:        # state of sessions that ended long ago
        cdir = os.path.dirname(path)
        for fn in os.listdir(cdir):
            p = os.path.join(cdir, fn)
            if fn.startswith('.prompt-') and p != path and now - os.lstat(p).st_mtime > 7 * 86400:
                os.unlink(p)
    except OSError:
        pass
    return True


def short_path(path, room):
    """``path`` in at most ``room`` chars: as is, else with $HOME collapsed to ~, else only its
    last 3 (then 2, 1) components after "…/", else cut from the left."""
    if len(path) <= room:
        return path
    p = display_path(path)
    if len(p) <= room:
        return p
    parts = [x for x in p.split('/') if x]
    for k in (3, 2, 1):
        q = '…/' + '/'.join(parts[-k:])
        if len(q) <= room:
            return q
    return '…' + p[-(room - 1):] if room > 1 else '…'


def rank_tokens(text):
    """Words for ranking rules against a prompt: ASCII words of >= 3 characters (no stop words,
    a plural s dropped), CJK bigrams and the RANK_TOPICS both languages share."""
    s = unicodedata.normalize('NFKC', text or '').casefold()
    out = set()
    for w in re.findall(r'[a-z0-9]+', s):
        if len(w) < 3 or w in RANK_STOPWORDS:
            continue
        if len(w) >= 4 and w.endswith('s') and not w.endswith('ss'):
            w = w[:-1]
        out.add(w)
        if w in RANK_TOPIC_WORDS:
            out.add('#' + RANK_TOPIC_WORDS[w])
    out.update('#' + t for w, t in RANK_TOPIC_CJK if w in s)
    run = ''
    for ch in s + ' ':
        if is_cjk(ch):
            run += ch
            continue
        out.update(run[k:k + 2] for k in range(len(run) - 1) if run[k:k + 2] not in RANK_CJK_STOP)
        run = ''
    return out


def broken_rules(ctx, prompt):
    """Up to NUDGE_RULES rules the user may mean, best first: the project's preferences and the
    inherited ones, ranked by words shared with the prompt (ties: section order, project first).
    -> [(rule, outer memdir or None)]"""
    cands = [(e.rule, None) for e in read_prefs(ctx.memdir)]
    try:
        cands += [(e.rule, m) for m, entries in inherited_prefs(ctx)[0] for e in entries]
    except Exception:
        pass
    words = rank_tokens(prompt)
    ranked = sorted(enumerate(cands), key=lambda x: (-len(words & rank_tokens(x[1][0])), x[0]))
    return [c for _, c in ranked[:NUDGE_RULES]]


def nudge_text(ctx, violation, prompt=''):
    """The note for a matching prompt. After a violation marker it leads with the rules the user
    may mean (their own budget) and what to do with them. Without the rules it never exceeds
    PROMPT_NUDGE_CHARS: the memory path is shortened to fit."""
    tmpl = (NUDGE_VIOLATION.replace('{date}', today()) + NUDGE_ALSO) if violation else NUDGE
    room = PROMPT_NUDGE_CHARS - len(tmpl.replace('{index}', '').replace('{rules}', ''))
    index = short_path(ctx.memdir, room - len('/MEMORY.md')) + '/MEMORY.md'
    text = tmpl.replace('{index}', index)
    if violation:
        shown = ['"%s"%s' % (cut(rule, NUDGE_RULE_CHARS),
                             ' (from %s)' % short_path(m, NUDGE_LABEL_CHARS) if m else '')
                 for rule, m in broken_rules(ctx, prompt)]
        text = text.replace('{rules}', '; '.join(shown) or NUDGE_NO_RULES.replace('{index}', index))
    return text


def hook_prompt(args):
    """UserPromptSubmit: nudge the agent to save a lasting preference. Cheap checks first; the
    memory (git) is looked up only for a matching prompt; one write of ASCII JSON at the end."""
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(2)
    data = read_hook_input(wait=1.0)
    source = data.get('source')
    if isinstance(source, str) and source.strip().lower() in PROMPT_SKIP_SOURCES:
        return
    prompt = data.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return
    text = prompt_text(prompt)
    if not text or len(text) > PROMPT_MAX_CHARS:
        return
    matched, violation = prompt_signals(text)
    if not matched:
        return
    ctx = hook_context(data, args.C)
    if ctx is None or not ctx.memdir or not os.access(ctx.memdir, os.R_OK | os.W_OK | os.X_OK):
        return
    if not prompt_rate_ok(ctx.memdir, str(data.get('session_id') or '')):
        return
    payload = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                      'additionalContext': clean_text(nudge_text(ctx, violation,
                                                                                 text))}}
    out = json.dumps(payload) + '\n'       # ASCII-only
    sys.stdout.write(out)
    sys.stdout.flush()
    if hasattr(signal, 'SIGALRM'):
        signal.alarm(0)


def cmd_hook(args):
    global QUIET, GIT_TIMEOUT
    QUIET = True
    # read-only git calls get a short timeout; the background snapshot never does
    GIT_TIMEOUT = None if args.hook == 'snapshot' else HOOK_GIT_TIMEOUT
    try:
        if args.hook == 'session-start':
            hook_session_start(args)
        elif args.hook == 'checkpoint':
            hook_checkpoint(args)
        elif args.hook == 'snapshot':
            hook_snapshot(args)
        elif args.hook == 'prompt':
            hook_prompt(args)
    except BaseException:  # hooks must never fail the agent
        pass
    return 0


# ---------------------------------------------------------------------------------------------
# CLI

class Formatter(argparse.RawDescriptionHelpFormatter):
    pass


def nonneg_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('expected an integer, got %r' % text)
    if value < 0:
        raise argparse.ArgumentTypeError('must be >= 0, got %d' % value)
    return value


def build_parser():
    p = argparse.ArgumentParser(
        prog='pmem', formatter_class=Formatter,
        description='Project memory shared by every agent and account: <project>/.agent-memory/',
        epilog='Exit codes: 0 ok, 1 problem/not found, 2 usage/refused. '
               'Run `pmem COMMAND --help` for details.')
    p.add_argument('--version', action='version', version='pmem ' + VERSION)
    p.add_argument('-C', metavar='DIR', help='run as if started in DIR')
    sub = p.add_subparsers(dest='cmd', metavar='COMMAND')

    def cmd(name, help_, func, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, formatter_class=Formatter, **kw)
        sp.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.set_defaults(func=func)
        return sp

    sp = cmd('init', 'create or reuse the project memory and wire Claude Code', cmd_init)
    sp.add_argument('--here', action='store_true', help='create the memory in the start dir itself')
    sp.add_argument('--track', action='store_true',
                    help='do not exclude the memory from the project git: you commit it '
                         'with the project (no private history)')
    sp.add_argument('--no-history', action='store_true', help='no private git history')
    sp.add_argument('--no-claude', action='store_true', help='do not write .claude/settings.local.json')
    sp.add_argument('--no-hooks', action='store_true', help='remove/do not add our Claude hooks')
    sp.add_argument('--allow-dream', action='store_true', help='leave autoDreamEnabled untouched')
    sp.add_argument('--no-children', action='store_true',
                    help='non-git workspace: do not wire child repositories')
    sp.add_argument('--force', action='store_true',
                    help='override refusals (other autoMemoryDirectory, tracked settings, '
                         'memory tracked without --track, Claude root at $HOME or above, '
                         'worktree); never a memory dir at $HOME, / or the project root')

    cmd('where', 'print the memory dir (exit 1 if none)', cmd_where)

    sp = cmd('status', 'show memory, wiring and git state', cmd_status)
    sp.add_argument('--json', action='store_true', help='machine-readable output')

    sp = cmd('context', 'print handoff, newer checkpoint, other handoffs, inherited preferences and '
                        'MEMORY.md', cmd_context)
    sp.add_argument('--key', metavar='K',
                    help='workstream key (default: $PMEM_KEY, else from the worktree)')

    sp = cmd('handoff', 'show, locate (create) or list handoffs', cmd_handoff)
    sp.add_argument('action', nargs='?', choices=('show', 'path', 'list'), default='show',
                    metavar='show|path|list',
                    help='show: handoff + newer checkpoint (default); path: print (create) the '
                         'handoff path; list: every handoff')
    sp.add_argument('--key', metavar='K',
                    help='workstream key (default: $PMEM_KEY, else from the worktree)')

    sp = cmd('add', 'write a memory file and its index line', cmd_add)
    sp.add_argument('type', metavar='TYPE', choices=TYPES, help='|'.join(TYPES))
    sp.add_argument('name', metavar='NAME', help='kebab-case file stem')
    sp.add_argument('-d', '--description', required=True, metavar='DESCRIPTION',
                    help='one line used for recall')
    sp.add_argument('--title', metavar='T', help='index link text (default NAME)')
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--body', metavar='TEXT', help='memory body')
    g.add_argument('--stdin', action='store_true', help='read the body from stdin')
    sp.add_argument('--force', action='store_true', help='rewrite an existing memory')

    sp = cmd('find', 'search memories and handoffs (AND, case-insensitive)', cmd_find)
    sp.add_argument('terms', nargs='+', metavar='TERM')
    sp.add_argument('--archive', action='store_true', help='also search .archive/')

    sp = cmd('index', 'check MEMORY.md against the files', cmd_index)
    sp.add_argument('--fix', action='store_true',
                    help='add missing lines, drop dangling ones, put the HANDOFF line first')

    sp = cmd('lint', 'check memory files, index, handoffs and secrets', cmd_lint)
    sp.add_argument('--strict', action='store_true', help='exit 1 on warnings too')
    sp.add_argument('--stale-days', type=int, default=90, metavar='N',
                    help='report memories older than N days (default 90)')
    sp.add_argument('--paths', action='store_true',
                    help='warn about backticked relative paths that do not exist')

    sp = cmd('archive', 'retire memories (NAME) or handoffs (handoffs/KEY) into .archive/',
             cmd_archive)
    sp.add_argument('names', nargs='+', metavar='NAME',
                    help='memory stem, or handoffs/KEY for the handoff of a finished workstream')
    sp.add_argument('--reason', metavar='TEXT', help='append "archived: <date> — TEXT"')

    sp = cmd('import', 'import legacy memory dirs (Claude per-project memory, notes files)',
             cmd_import)
    sp.add_argument('src', nargs='*', metavar='SRC_DIR')
    sp.add_argument('--claude', action='store_true',
                    help="this project's Claude Code memory dirs (<cfg>/projects/<slug>/memory)")
    sp.add_argument('--config-dir', action='append', default=[], metavar='DIR',
                    help='extra Claude config dir (repeatable)')
    sp.add_argument('--file', action='append', default=[], metavar='PATH',
                    help='import a whole file as an imported-notes memory (repeatable)')
    sp.add_argument('--dry-run', action='store_true', help='print the plan, write nothing')

    sp = cmd('wire', 'insert/replace/remove the project-memory block in an instruction file',
             cmd_wire)
    sp.add_argument('target', metavar='codex|claude|FILE')
    sp.add_argument('--config-dir', action='append', default=[], metavar='DIR',
                    help='Claude config dir for `wire claude` (repeatable)')
    sp.add_argument('--remove', action='store_true', help='remove the block')

    sp = cmd('pref', 'preferences: list, add, remove, violated, promote, demote', cmd_pref)
    sp.set_defaults(pref_parser=sp)
    ps = sp.add_subparsers(dest='pref_cmd', metavar='ACTION')

    def pcmd(name, help_):
        q = ps.add_parser(name, help=help_, description=help_, formatter_class=Formatter)
        q.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return q

    q = pcmd('list', 'preferences of this project, of outer folders and of the global block')
    q.add_argument('--all', action='store_true',
                   help='also every registered project (to spot rules repeated across projects)')
    q.add_argument('--json', action='store_true', help='machine-readable output')
    q = pcmd('add', 'record a preference: an entry under ## Preferences plus a feedback memory')
    q.add_argument('rule', metavar='RULE', help='one imperative line (<= %d chars)' % PREF_RULE_MAX)
    q.add_argument('--name', metavar='NAME', help='file stem (default: from the first words of RULE)')
    q.add_argument('--title', metavar='T', help='link text (default: NAME with spaces)')
    q.add_argument('--why', metavar='TEXT', help='**Why:** line of a new file')
    q.add_argument('--how', metavar='TEXT', help='**How to apply:** line of a new file')
    q.add_argument('--up', action='store_true', help='record it in the nearest outer memory '
                                                      '(the workspace)')
    q.add_argument('--force', action='store_true',
                   help='replace an entry with the same rule recorded under another name; rewrite '
                        'an existing NAME.md')
    q = pcmd('remove', 'take a preference out of the section (its file stays as a memory)')
    q.add_argument('what', metavar='NAME|RULE')
    q = pcmd('violated', 'a preference was broken: sharpen its rule, log the date, move it to the top')
    q.add_argument('name', metavar='NAME')
    q.add_argument('--rule', required=True, metavar='R', help='the sharper rule (must differ)')
    q.add_argument('--note', metavar='TEXT', help='what happened (dated line under **Violations:**)')
    q.add_argument('--up', action='store_true',
                   help='the rule is inherited: edit it in the nearest outer memory (general form: '
                        '`pmem -C <outer dir> pref violated …`)')
    q = pcmd('promote', 'copy a preference to the global block of every global instruction file '
                        '(only with the user\'s consent)')
    q.add_argument('what', metavar='NAME|RULE')
    q.add_argument('--config-dir', action='append', default=[], metavar='DIR',
                   help='also <DIR>/CLAUDE.md (repeatable)')
    q.add_argument('--target', dest='targets', action='append', default=[], metavar='FILE',
                   help='also this instruction file (repeatable)')
    q.add_argument('--keep-local', action='store_true', help="keep the project's own entry")
    q.add_argument('--only', action='store_true',
                   help='write only the --target / --config-dir files (not the default Codex and '
                        'Claude files)')
    q.add_argument('--dry-run', action='store_true',
                   help='print every file it would write and the change; write nothing')
    q = pcmd('demote', 'remove a preference from the global blocks')
    q.add_argument('rule', metavar='RULE', help='the rule (or a unique part of it)')
    q.add_argument('--config-dir', action='append', default=[], metavar='DIR',
                   help='also <DIR>/CLAUDE.md (repeatable)')
    q.add_argument('--target', dest='targets', action='append', default=[], metavar='FILE',
                   help='also this instruction file (repeatable)')
    q.add_argument('--to-project', action='store_true',
                   help='first record it as a preference of this project')
    q.add_argument('--only', action='store_true',
                   help='read and write only the --target / --config-dir files (not the default '
                        'Codex and Claude files)')
    q.add_argument('--dry-run', action='store_true',
                   help='print every file it would write and the change; write nothing')

    sp = cmd('snapshot', 'commit all changes to the private history', cmd_snapshot)
    sp.add_argument('-m', dest='message', metavar='MSG', help='commit message')

    sp = cmd('diff', 'changes since the last snapshot (or an older baseline)', cmd_diff)
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--since', metavar='REV', help='changes since this private-history revision')
    g.add_argument('--since-health', action='store_true',
                   help='changes since the last "memory-health" snapshot (else the first one)')

    sp = cmd('history', 'private history log', cmd_history)
    sp.add_argument('-n', type=nonneg_int, default=20, metavar='N',
                    help='number of entries (default 20)')
    sp.add_argument('name', nargs='?', metavar='NAME', help='memory name or relative path')

    sp = cmd('restore', 'restore a memory file from the private history', cmd_restore)
    sp.add_argument('name', metavar='NAME', help='memory name or relative path (HANDOFF.md, ...)')
    sp.add_argument('--rev', metavar='REV',
                    help='revision (default: last snapshot in which the file existed)')
    sp.add_argument('--force', action='store_true', help='overwrite unsnapshotted changes')

    sp = cmd('sessions', "list recent Claude Code and Codex transcripts of this project",
             cmd_sessions)
    sp.add_argument('--since-handoff', action='store_true',
                    help="only this workstream's sessions modified after its handoff")
    sp.add_argument('--since', metavar='ISO', help='only sessions modified after ISO time')
    sp.add_argument('--tail', type=nonneg_int, default=0, metavar='N',
                    help='also print the last N user/assistant messages of each')
    sp.add_argument('--config-dir', action='append', default=[], metavar='DIR',
                    help='extra Claude config dir (repeatable)')
    sp.add_argument('--limit', type=nonneg_int, default=10, metavar='N',
                    help='max sessions (default 10)')

    sp = cmd('hook', 'Claude Code hook entry points (always exit 0)', cmd_hook)
    hs = sp.add_subparsers(dest='hook', metavar='HOOK')
    hs.required = True
    h = hs.add_parser('session-start', help='SessionStart: snapshot + handoff context JSON',
                      description='SessionStart: snapshot + handoff context JSON')
    h.add_argument('--with-index', action='store_true', help='append MEMORY.md')
    h.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    h = hs.add_parser('checkpoint', help='Stop/StopFailure/PreCompact: write .checkpoints/<key>.md',
                      description='Stop/StopFailure/PreCompact: write .checkpoints/<key>.md')
    h.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    h = hs.add_parser('prompt', help='UserPromptSubmit: nudge to save a lasting preference',
                      description='UserPromptSubmit: nudge to save a lasting preference')
    h.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    h = hs.add_parser('snapshot', help='internal: background snapshot started by session-start',
                      description='internal: private-history snapshot that session-start runs '
                                  'in a detached background process')
    h.add_argument('--memdir', metavar='DIR', help='memory dir')
    h.add_argument('-m', dest='message', metavar='MSG', help='commit message')
    h.add_argument('-C', metavar='DIR', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def setup_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            utf8 = (s.encoding or '').lower().replace('-', '') == 'utf8'
            s.reconfigure(errors='surrogateescape' if utf8 else 'backslashreplace')
        except Exception:
            pass


HOOK_SUBCOMMANDS = ('session-start', 'checkpoint', 'snapshot', 'prompt')


def positionals(argv):
    """Leading positional words of argv (skipping -C DIR and other options)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == '-C':
            skip = True
        elif not a.startswith('-'):
            out.append(a)
    return out


def main(argv=None):
    setup_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    pos = positionals(argv)
    if pos[:1] == ['hook'] and len(pos) > 1 and pos[1] not in HOOK_SUBCOMMANDS \
            and not set(argv) & set(('-h', '--help')):
        return 0        # a hook this version does not know (newer settings): stay silent
    if 'hook' in argv:  # hooks tolerate unknown extra arguments
        args, unknown = parser.parse_known_args(argv)
        if unknown and getattr(args, 'cmd', None) != 'hook':
            parser.error('unrecognized arguments: %s' % ' '.join(unknown))
    else:
        args = parser.parse_args(argv)
    if not getattr(args, 'cmd', None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except PmemError as e:
        eprint('pmem: %s' % e)
        return e.code
    except BrokenPipeError:
        with contextlib.suppress(Exception):
            sys.stdout = open(os.devnull, 'w')
        return 0
    except OSError as e:
        eprint('pmem: %s' % e)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
