"""pmem.py 黑盒测试：只依据设计规格，通过子进程调用；所有文件都在临时目录中。

运行：cd project-memory && python3 -m unittest discover tests
"""
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

try:
    from tests.support import (HAS_GIT, ISO_UTC, MARK, PMEM, TEMPLATES, TIMEOUT, PmemCase,
                               body_of, fm_get, is_memory_link_line, lines_linking, link_target,
                               memory_link_lines, read_text, rp, tree_state, write)
except ImportError:  # pragma: no cover - when run from inside tests/
    from support import (HAS_GIT, ISO_UTC, MARK, PMEM, TEMPLATES, TIMEOUT, PmemCase,
                         body_of, fm_get, is_memory_link_line, lines_linking, link_target,
                         memory_link_lines, read_text, rp, tree_state, write)

GIT_REQUIRED = unittest.skipUnless(HAS_GIT, 'git is not installed')

# ---------------------------------------------------------------------------------------------
# Spec constants (copied from the design spec; the shipped templates must match them).

MEMORY_TEMPLATE = (
    '# Project memory\n'
    '\n'
    '> Shared by every agent and account working on this project. Never delete files here'
    ' — retire them with `pmem archive`. HANDOFF.md and handoffs/ hold live task state:'
    ' update them, never prune them.\n'
    '\n'
    '- [HANDOFF — current state](HANDOFF.md) — read before any task at session start;'
    ' update at milestones\n'
    '\n'
    '## Preferences\n'
    '> How the user wants you to work: follow every line, every session; on conflict this project'
    ' beats outer folders and the global list. Save a new one at once by editing this file:'
    ' `- [<title>](<name>.md) — <rule>` here (not under Memories) plus `<name>.md` as a `feedback`'
    ' memory with **Why:** and **How to apply:**; then end your reply with'
    ' `✓ preference: <rule>`.\n'
    '\n'
    '## Memories\n'
)
HANDOFF_LINE = ('- [HANDOFF — current state](HANDOFF.md) — read before any task at session start;'
                ' update at milestones')

HANDOFF_TEMPLATE = (
    '---\n'
    'name: handoff\n'
    'description: Live task state for resuming or switching agents (not a memory) — read first,'
    ' keep current, never delete\n'
    'metadata:\n'
    '  type: project\n'
    '  modified: {now_utc}\n'
    '  by: {by}\n'
    '  workstream: {key}\n'
    '---\n'
    '\n'
    '## Goal\n'
    '(no active task)\n'
    '\n'
    '## Done\n'
    '\n'
    '## Next step\n'
    '\n'
    '## Live state\n'
    '<!-- branches, worktrees, running jobs/PIDs, remote dirs, URLs — each with a command to'
    ' check it -->\n'
    '\n'
    '## Decisions and constraints in force\n'
    '\n'
    '## Open questions\n'
)

BEGIN = '<!-- BEGIN project-memory -->'
END = '<!-- END project-memory -->'
WIRE_BLOCK = '\n'.join([
    BEGIN,
    '## Project memory (`.agent-memory/`)',
    '- Project knowledge lives inside the project, shared by every agent and account: the nearest'
    ' `.agent-memory/` from the working directory upward (never `$HOME`; in a git worktree, look'
    ' from the main checkout). It takes precedence over any other memory location, including'
    ' your built-in memory store and older per-repo notes files such as CLAUDE.local.md.',
    '- At session start and after compaction, run `pmem context` if available (handoff, any newer'
    ' auto checkpoint, inherited preferences, index); otherwise read `HANDOFF.md` (or your'
    ' worktree\'s `handoffs/<name>.md`) and a newer `.checkpoints/<key>.md` — no agent loads these'
    ' natively — then `MEMORY.md` if it is not already in your context. Open single memories only'
    ' when relevant and verify them against the current code before acting.',
    '- Follow every line under `## Preferences` in that `MEMORY.md`, in the `MEMORY.md` of any'
    ' `.agent-memory/` further up, and in this file\'s preferences block; on conflict the nearest'
    ' wins. When the user states a lasting preference or corrects how you work, save it at once by'
    ' editing the files: `- [<title>](<name>.md) — <rule>` under `## Preferences` plus `<name>.md`'
    ' as a `feedback` memory with Why and How to apply (`pmem pref add` does the same; `--up` for'
    ' the whole workspace), then end your reply with `✓ preference: <rule>`. If you broke an'
    ' existing one, sharpen its line and move it to the top. Task-specific instructions are not'
    ' preferences.',
    '- Before debugging an error, search the memory for its key tokens (`pmem find ...` or grep)'
    ' and try a recorded fix first.',
    '- Save right away when the user confirms an unusual approach, when a failure took 3+ attempts'
    ' (symptom, root cause, fix), or when you repeat a multi-step procedure: one fact per file with'
    ' `name`/`description`/`metadata.type`/`metadata.modified` (UTC) frontmatter plus one line in'
    ' `MEMORY.md`. Never save secrets; a preference that holds for every project goes to the global'
    ' preferences block only with the user\'s consent (`pmem pref promote`).',
    '- Keep the handoff current at milestones (step done, before long or remote jobs, before'
    ' stopping), setting `metadata.modified` and `by`, so another agent can resume if this'
    ' session ends abruptly. Never delete memory files; retire them with `pmem archive`.',
    '- If the sandbox blocks writes to `.agent-memory/`, ask for approval or a relaunch that makes'
    ' it writable (e.g. start from the project root, or Codex `--add-dir <project root>`); never'
    ' save elsewhere. Details: the `project-memory` skill; CLI `pmem` (`pmem status` reports'
    ' wiring problems).',
    END,
])

GITIGNORE_LINES = ['.checkpoints/', '.pmem.lock', '.pmem-tmp-*', '.consolidate-lock', '.*.tmp']
SUBCOMMANDS = ['init', 'where', 'status', 'context', 'handoff', 'add', 'find', 'index', 'lint',
               'archive', 'import', 'wire', 'snapshot', 'diff', 'history', 'restore', 'sessions',
               'hook']

LEGACY_INDEX = (
    '# Memory Index\n'
    '\n'
    '- [Alpha](alpha.md) — alpha hook\n'
    '- [Beta](beta.md) — beta hook\n'
    '\n'
    '## Gotchas\n'
    'Always run make before test\n'
    '<!-- generated by a tool -->\n'
    '- plain bullet without link\n'
)


# ---------------------------------------------------------------------------------------------
# Independent re-implementations of spec algorithms.

def base36(number):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    if number == 0:
        return '0'
    out = ''
    while number:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
    return out


def claude_slug(path):
    """Claude Code's project slug: UTF-16 units outside [A-Za-z0-9] -> '-', long -> hash suffix."""
    data = path.encode('utf-16-le', 'surrogatepass')
    units = [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]
    slug = ''.join(chr(u) if (48 <= u <= 57 or 65 <= u <= 90 or 97 <= u <= 122) else '-'
                   for u in units)
    if len(slug) <= 200:
        return slug
    h = 0
    for unit in units:
        h = ((h << 5) - h + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return slug[:200] + '-' + base36(abs(h))


def handoff_regex(template, by, key, name=None):
    text = template
    if name is not None:
        text = text.replace('name: handoff\n', 'name: %s\n' % name, 1)
    pattern = re.escape(text)
    pattern = pattern.replace(re.escape('{now_utc}'), ISO_UTC)
    pattern = pattern.replace(re.escape('{by}'), re.escape(by))
    pattern = pattern.replace(re.escape('{key}'), re.escape(key))
    return r'\A' + pattern + r'\Z'


def mem_file(name, desc, body='Body.', type_='project', modified='2026-11-01T00:00:00Z'):
    return ('---\nname: %s\ndescription: %s\nmetadata:\n  type: %s\n  modified: %s\n---\n\n%s\n'
            % (name, desc, type_, modified, body))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def non_link_lines(text):
    return [line for line in text.splitlines() if line.strip() and not is_memory_link_line(line)]


# -- static Python 3.9 compatibility checks ----------------------------------------------------

NEWER_ATTRS = {('datetime', 'UTC'), ('itertools', 'pairwise'), ('hashlib', 'file_digest'),
               ('contextlib', 'chdir'), ('enum', 'StrEnum'), ('typing', 'TypeAlias')}
NEWER_MODULES = {'tomllib'}


def _has_union(node):
    return node is not None and any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                                    for n in ast.walk(node))


def py39_problems(tree):
    problems = []
    lazy = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
               and any(a.name == 'annotations' for a in n.names) for n in tree.body)
    for node in ast.walk(tree):
        line = getattr(node, 'lineno', '?')
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not lazy:
            args = node.args
            every = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            every += [a for a in (args.vararg, args.kwarg) if a is not None]
            if any(_has_union(a.annotation) for a in every) or _has_union(node.returns):
                problems.append('line %s: PEP 604 union in a runtime annotation' % line)
        elif isinstance(node, ast.AnnAssign) and not lazy and _has_union(node.annotation):
            problems.append('line %s: PEP 604 union in a runtime annotation' % line)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else '')
            keywords = {k.arg for k in node.keywords}
            if name == 'zip' and 'strict' in keywords:
                problems.append('line %s: zip(strict=) needs 3.10' % line)
            if name == 'realpath' and 'strict' in keywords:
                problems.append('line %s: realpath(strict=) needs 3.10' % line)
            if name in ('glob', 'iglob') and 'root_dir' in keywords:
                problems.append('line %s: glob(root_dir=) needs 3.10' % line)
            if name == 'dataclass' and keywords & {'slots', 'kw_only', 'match_args'}:
                problems.append('line %s: dataclass options need 3.10' % line)
            if name == 'isinstance' and len(node.args) == 2 and _has_union(node.args[1]):
                problems.append('line %s: isinstance with X | Y needs 3.10' % line)
            if isinstance(func, ast.Name) and name in ('aiter', 'anext'):
                problems.append('line %s: %s() needs 3.10' % (line, name))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (node.value.id, node.attr) in NEWER_ATTRS:
                problems.append('line %s: %s.%s is newer than 3.9' % (line, node.value.id, node.attr))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split('.')[0] in NEWER_MODULES or any(
                    (node.module, a.name) in NEWER_ATTRS for a in node.names):
                problems.append('line %s: import from %s is newer than 3.9' % (line, node.module))
        elif isinstance(node, ast.Import):
            if any(a.name.split('.')[0] in NEWER_MODULES for a in node.names):
                problems.append('line %s: module newer than 3.9' % line)
    return problems


def imported_modules(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split('.')[0])
    return found


# ---------------------------------------------------------------------------------------------

class Helpers(PmemCase):
    """Shared shortcuts for the black-box tests."""

    def where(self, cwd):
        return rp(self.pmem('where', cwd=cwd).stdout.strip())

    def add(self, root, name, desc, body='Body.', type_='project', *extra):
        return self.pmem('add', type_, name, '-d', desc, '--body', body, *extra, cwd=root)

    def index_text(self, mem):
        return read_text(Path(mem) / 'MEMORY.md')

    def append_index(self, mem, *lines):
        text = self.index_text(mem)
        if not text.endswith('\n'):
            text += '\n'
        write(Path(mem) / 'MEMORY.md', text + ''.join(line + '\n' for line in lines))

    def session_payload(self, cwd, source='startup', sid='0123456789abcdef'):
        return {'session_id': sid, 'transcript_path': str(self.base / 'nocfg' / 'projects' / 'x' /
                                                         (sid + '.jsonl')),
                'cwd': str(cwd), 'hook_event_name': 'SessionStart', 'source': source}

    def stop_payload(self, cwd, message='done for now', active=False, event='Stop', **extra):
        data = {'session_id': 'sess-1234',
                'transcript_path': str(self.base / 'nocfg' / 'projects' / 'x' / 'sess-1234.jsonl'),
                'cwd': str(cwd), 'hook_event_name': event, 'stop_hook_active': active,
                'last_assistant_message': message}
        data.update(extra)
        return data

    def lint_rc(self, root, *args):
        return self.pmem('lint', *args, cwd=root, rc=None).returncode


# =============================================================================================
# CLI surface

class CliTests(Helpers):
    def test_version(self):
        result = self.pmem('--version')
        self.assertIn('pmem 1.1.0', result.stdout)

    def test_every_subcommand_has_help(self):
        for command in SUBCOMMANDS:
            with self.subTest(command=command):
                self.assertTrue(self.pmem(command, '--help').stdout.strip())
        for hook in ('session-start', 'checkpoint'):
            with self.subTest(hook=hook):
                self.pmem('hook', hook, '--help')

    def test_unknown_command_is_a_usage_error(self):
        self.pmem('no-such-command', rc=2)

    def test_script_is_executable_stdlib_python39(self):
        source = PMEM.read_text(encoding='utf-8')
        self.assertTrue(source.startswith('#!/usr/bin/env python3\n'))
        self.assertEqual(stat.S_IMODE(os.stat(str(PMEM)).st_mode) & 0o111, 0o111)
        tree = ast.parse(source, str(PMEM), feature_version=(3, 9))
        self.assertEqual(py39_problems(tree), [])
        if hasattr(sys, 'stdlib_module_names'):
            third_party = sorted(m for m in imported_modules(tree)
                                 if m != '__future__' and m not in sys.stdlib_module_names)
            self.assertEqual(third_party, [])


# =============================================================================================
# Templates == spec == constants (checked through behaviour, black-box)

class TemplateTests(Helpers):
    def test_shipped_template_files_match_the_spec(self):
        for name, expected in (('MEMORY.md', MEMORY_TEMPLATE), ('HANDOFF.md', HANDOFF_TEMPLATE),
                               ('wire-block.md', WIRE_BLOCK + '\n')):
            with self.subTest(template=name):
                text = read_text(TEMPLATES / name)
                self.assertEqual(text.rstrip('\n'), expected.rstrip('\n'))
                self.assertTrue(text.endswith('\n'))

    def test_init_and_handoff_write_the_shipped_templates(self):
        root, mem = self.init_project(self.work / 'tpl', '--no-history')
        self.assertEqual((mem / 'MEMORY.md').read_bytes(), (TEMPLATES / 'MEMORY.md').read_bytes())
        template = read_text(TEMPLATES / 'HANDOFF.md')
        self.assertRegex(read_text(mem / 'HANDOFF.md'), handoff_regex(template, 'pmem init', 'main'))
        for name in ('MEMORY.md', 'HANDOFF.md'):
            self.assertEqual(stat.S_IMODE(os.stat(str(mem / name)).st_mode), 0o644)
        result = self.pmem('handoff', 'path', '--key', 'side-task', cwd=root)
        keyed = mem / 'handoffs' / 'side-task.md'
        self.assertEqual(rp(result.stdout.strip()), str(keyed))
        self.assertRegex(read_text(keyed),
                         handoff_regex(template, 'pmem', 'side-task', name='handoff-side-task'))

    def test_wire_writes_the_shipped_block(self):
        target = write(self.base / 'wired' / 'AGENTS.md', 'x\n')
        self.pmem('wire', target)
        self.assertEqual(read_text(target), 'x\n\n' + read_text(TEMPLATES / 'wire-block.md'))


# =============================================================================================
# Discovery

@GIT_REQUIRED
class DiscoveryTests(Helpers):
    def test_where_from_subdirs_and_dash_c(self):
        root, mem = self.init_project(self.work / 'plain', '--no-history')
        deep = root / 'a' / 'b'
        deep.mkdir(parents=True)
        self.assertEqual(self.where(deep), str(mem))
        self.assertEqual(rp(self.pmem('-C', deep, 'where').stdout.strip()), str(mem))
        outside = self.work / 'other'
        outside.mkdir()
        result = self.pmem('where', cwd=outside, rc=1)
        self.assertIn('pmem init', result.out)

    def test_nearest_memory_wins_and_non_directories_are_skipped(self):
        outer, outer_mem = self.init_project(self.work / 'ws', '--no-history')
        inner = outer / 'inner'
        inner.mkdir()
        self.pmem('init', '--here', '--no-history', cwd=inner)
        (inner / 'x').mkdir()
        self.assertEqual(self.where(inner / 'x'), rp(inner / '.agent-memory'))
        (outer / 'y').mkdir()
        self.assertEqual(self.where(outer / 'y'), str(outer_mem))
        # a regular file or a symlink to a file named .agent-memory is not a memory dir
        write(outer / 'filecase' / '.agent-memory', 'not a directory\n')
        self.assertEqual(self.where(outer / 'filecase'), str(outer_mem))
        some_file = write(self.base / 'some-file', 'x\n')
        (outer / 'linkfile').mkdir()
        os.symlink(str(some_file), str(outer / 'linkfile' / '.agent-memory'))
        self.assertEqual(self.where(outer / 'linkfile'), str(outer_mem))
        # a symlink to a directory is allowed
        target = self.base / 'shared-memory'
        write(target / 'MEMORY.md', '# shared\n')
        (outer / 'linked').mkdir()
        os.symlink(str(target), str(outer / 'linked' / '.agent-memory'))
        self.assertEqual(self.where(outer / 'linked'), rp(target))

    def test_home_and_its_ancestors_are_refused(self):
        before = tree_state(self.base)
        cases = ((('init',), self.home), (('init', '--here'), self.home),
                 (('-C', rp(self.home), 'init'), self.work), (('init', '--no-history'), self.base))
        for args, cwd in cases:
            with self.subTest(args=args, cwd=str(cwd)):
                self.pmem(*args, cwd=cwd, rc=2)
        self.assertFalse(os.path.lexists(str(self.home / '.agent-memory')))
        self.assertFalse(os.path.lexists(str(self.base / '.agent-memory')))
        self.assertEqual(tree_state(self.base), before)

    def test_memory_at_home_is_never_discovered(self):
        write(self.home / '.agent-memory' / 'MEMORY.md', '# stray\n')
        project = self.work / 'p'
        project.mkdir()
        self.pmem('where', cwd=project, rc=1)
        self.assertEqual(self.hook('session-start', self.session_payload(project), cwd=project).stdout, '')
        result = self.pmem('init', '--no-history', cwd=project)
        self.assertNotIn('reusing', result.stdout)
        self.assertTrue((project / '.agent-memory' / 'MEMORY.md').is_file())
        self.assertEqual(os.listdir(str(self.home / '.agent-memory')), ['MEMORY.md'])
        self.assertEqual(read_text(self.home / '.agent-memory' / 'MEMORY.md'), '# stray\n')

    def test_home_as_a_git_repo_is_refused(self):
        self.make_repo(self.home, {'.bashrc': '# rc\n'})
        notes = self.home / 'notes'
        notes.mkdir()
        before = tree_state(self.base)
        self.pmem('init', cwd=notes, rc=2)
        self.assertNotEqual(self.pmem('add', 'project', 'x-note', '-d', 'd', '--body', 'b',
                                      cwd=notes, rc=None).returncode, 0)
        self.assertNotEqual(self.pmem('handoff', 'path', cwd=notes, rc=None).returncode, 0)
        self.assertEqual(self.hook('session-start', self.session_payload(notes), cwd=notes).stdout, '')
        self.hook('checkpoint', self.stop_payload(notes), cwd=notes)
        self.assertFalse(os.path.lexists(str(self.home / '.agent-memory')))
        self.assertEqual(tree_state(self.base), before)

    def test_filesystem_root_is_refused(self):
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('running as root')
        if os.path.lexists('/.agent-memory'):
            self.skipTest('/.agent-memory exists on this machine')
        self.pmem('-C', '/', 'init', '--no-history', rc=2)
        self.assertFalse(os.path.lexists('/.agent-memory'))

    def test_linked_worktree_shares_the_main_memory(self):
        main = self.make_repo(self.work / 'app')
        wt = self.add_worktree(main, self.work / 'App.Feat_2', 'feat')
        sub = wt / 'src' / 'deep'
        sub.mkdir(parents=True)
        self.pmem('init', cwd=sub)                       # created at the main checkout
        mem = Path(rp(main / '.agent-memory'))
        self.assertTrue((mem / 'MEMORY.md').is_file())
        self.assertFalse(os.path.lexists(str(wt / '.agent-memory')))
        self.assertEqual(self.where(sub), str(mem))
        self.assertEqual(self.settings(main)['autoMemoryDirectory'], str(mem))
        exclude = read_text(main / '.git' / 'info' / 'exclude').splitlines()
        self.assertEqual(exclude.count('/.agent-memory'), 1)
        self.assertIn('reusing', self.pmem('init', cwd=wt).stdout)
        self.pmem('init', '--here', cwd=wt, rc=2)       # memory inside a linked worktree
        self.assertFalse(os.path.lexists(str(wt / '.agent-memory')))
        # per-worktree handoff and checkpoint keys
        key = 'app-feat-2'
        keyed = mem / 'handoffs' / (key + '.md')
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=sub).stdout.strip()), str(keyed))
        text = read_text(keyed)
        self.assertEqual(fm_get(text, 'name'), 'handoff-' + key)
        self.assertEqual(fm_get(text, 'workstream'), key)
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=main).stdout.strip()),
                         str(mem / 'HANDOFF.md'))
        self.hook('checkpoint', self.stop_payload(sub, message='worktree message'), cwd=sub)
        self.assertTrue((mem / '.checkpoints' / (key + '.md')).is_file())
        self.assertFalse((mem / '.checkpoints' / 'main.md').exists())
        self.set_goal(mem / 'HANDOFF.md', 'MAIN-GOAL-TOKEN')
        self.set_goal(keyed, 'WT-GOAL-TOKEN')
        context = self.context_of(self.hook('session-start', self.session_payload(sub), cwd=sub))
        self.assertIn('WT-GOAL-TOKEN', context)
        self.assertNotIn('MAIN-GOAL-TOKEN', context)
        self.assertIn(key, self.pmem('handoff', 'list', cwd=main).stdout)

    def test_bare_container_layout_uses_the_container(self):
        container = self.work / 'proj'
        bare = self.make_bare(container / '.bare')
        trunk = self.bare_worktree(bare, container / 'trunk', 'main', new=False)
        feat = self.bare_worktree(bare, container / 'feat', 'feat')
        self.pmem('init', cwd=trunk)
        mem = rp(container / '.agent-memory')
        self.assertTrue(os.path.isfile(os.path.join(mem, 'MEMORY.md')))
        self.assertFalse(os.path.lexists(str(trunk / '.agent-memory')))
        self.assertEqual(self.where(feat), mem)
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=feat).stdout.strip()),
                         os.path.join(mem, 'handoffs', 'feat.md'))
        self.assertEqual(self.settings(container)['autoMemoryDirectory'], mem)

    def test_linked_worktree_named_main_never_takes_the_main_handoff(self):
        # bare container layout: <proj>/.bare + <proj>/main + <proj>/feat; HANDOFF.md is the container's
        container = self.work / 'boxed'
        bare = self.make_bare(container / '.bare')
        trunk = self.bare_worktree(bare, container / 'main', 'main', new=False)
        feat = self.bare_worktree(bare, container / 'feat', 'feat')
        self.pmem('init', cwd=trunk)
        mem = Path(rp(container / '.agent-memory'))
        keyed = mem / 'handoffs' / 'worktree-main.md'
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=trunk).stdout.strip()), str(keyed))
        self.assertEqual(fm_get(read_text(keyed), 'workstream'), 'worktree-main')
        self.assertEqual(fm_get(read_text(keyed), 'name'), 'handoff-worktree-main')
        self.assertRegex(self.pmem('status', cwd=trunk, rc=None).stdout,
                         r'(?m)^handoff key:\s+worktree-main\s*$')
        self.hook('checkpoint', self.stop_payload(trunk), cwd=trunk)
        self.assertTrue((mem / '.checkpoints' / 'worktree-main.md').is_file())
        self.assertFalse((mem / '.checkpoints' / 'main.md').exists())
        self.set_goal(mem / 'HANDOFF.md', 'CONTAINER-GOAL')
        self.set_goal(keyed, 'WT-MAIN-GOAL')
        context = self.context_of(self.hook('session-start', self.session_payload(trunk), cwd=trunk))
        self.assertIn('WT-MAIN-GOAL', context)
        self.assertNotIn('CONTAINER-GOAL', context)
        self.assertRegex(self.pmem('handoff', 'list', cwd=feat).stdout, r'(?m)^worktree-main\s.*\syes\s')
        self.assertNotIn('no git worktree named', self.pmem('lint', cwd=feat, rc=None).out)
        # any linked worktree whose sanitized name is "main" (here "Main")
        repo = self.make_repo(self.work / 'plain-repo')
        other = self.add_worktree(repo, self.work / 'wts' / 'Main', 'side')
        self.pmem('init', '--no-history', cwd=repo)
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=other).stdout.strip()),
                         os.path.join(rp(repo / '.agent-memory'), 'handoffs', 'worktree-main.md'))

    def test_bare_repo_without_container_has_no_main_checkout(self):
        bare = self.make_bare(self.home / 'repos' / 'proj.git')
        wt = self.bare_worktree(bare, self.work / 'wt1', 'main', new=False)
        result = self.pmem('init', cwd=wt, rc=None)
        self.assertNotEqual(result.returncode, 0, result.out)
        self.assertFalse(os.path.lexists(str(wt / '.agent-memory')))
        self.assertFalse(os.path.lexists(str(self.home / 'repos' / '.agent-memory')))
        self.pmem('init', '--here', cwd=wt, rc=2)
        self.assertFalse(os.path.lexists(str(wt / '.agent-memory')))
        self.pmem('init', '--here', '--force', '--no-history', cwd=wt)
        self.assertTrue((wt / '.agent-memory' / 'MEMORY.md').is_file())
        self.assertEqual(self.lint_rc(wt), 1)
        self.assertEqual(self.pmem('status', cwd=wt, rc=None).returncode, 1)

    def test_submodule_is_its_own_root(self):
        lib = self.make_repo(self.base / 'lib-src')
        parent = self.make_repo(self.work / 'parent')
        self.git(parent, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(lib), 'lib')
        self.git(parent, 'commit', '-q', '-m', 'add submodule')
        sub = parent / 'lib'
        self.pmem('init', cwd=sub)
        mem = rp(sub / '.agent-memory')
        self.assertTrue(os.path.isfile(os.path.join(mem, 'MEMORY.md')))
        self.assertFalse(os.path.lexists(str(parent / '.agent-memory')))
        self.assertEqual(self.where(sub), mem)
        self.assertTrue(self.check_ignored(sub, '.agent-memory'))
        self.assertTrue(self.check_ignored(sub, '.claude/settings.local.json'))
        self.assertEqual(self.git_out(sub, 'status', '--porcelain'), '')


class NoGitTests(Helpers):
    def test_missing_git_means_not_git(self):
        env = dict(self.env, PATH=str(self.bin))         # only the python3 wrapper
        project = self.work / 'nogit'
        (project / 'sub').mkdir(parents=True)
        self.pmem('init', '--no-history', cwd=project, env=env)
        mem = rp(project / '.agent-memory')
        for name in ('MEMORY.md', 'HANDOFF.md'):
            self.assertTrue(os.path.isfile(os.path.join(mem, name)))
        self.assertEqual(rp(self.pmem('where', cwd=project / 'sub', env=env).stdout.strip()), mem)
        context = self.context_of(self.hook('session-start', self.session_payload(project),
                                            cwd=project, env=env))
        self.assertIn(mem, context)


# =============================================================================================
# init

@GIT_REQUIRED
class InitTests(Helpers):
    def test_git_repo_layout_and_second_run_changes_nothing(self):
        repo = self.make_repo(self.work / 'repo')
        (repo / 'src').mkdir()
        self.pmem('init', cwd=repo / 'src')
        mem = Path(rp(repo / '.agent-memory'))
        for name in ('MEMORY.md', 'HANDOFF.md', '.gitignore'):
            self.assertTrue((mem / name).is_file(), name)
        for name in ('.archive', '.checkpoints', '.git'):
            self.assertTrue((mem / name).is_dir(), name)
        ignore = read_text(mem / '.gitignore').splitlines()
        for line in GITIGNORE_LINES:
            self.assertEqual(ignore.count(line), 1, line)
        self.assertNotIn('HANDOFF.md', ignore)
        self.assertNotIn('handoffs/', ignore)
        # private history
        self.assertIn('pmem init', self.mem_subject(mem))
        self.assertEqual(self.git_out(mem, 'log', '-1', '--format=%an'), 'pmem')
        self.assertEqual(self.mem_status(mem), '')
        # outer repo: excluded once, nothing shows up in git status
        exclude = read_text(repo / '.git' / 'info' / 'exclude').splitlines()
        self.assertEqual(exclude.count('/.agent-memory'), 1)
        self.assertEqual(exclude.count('/.claude/settings.local.json'), 1)
        self.assertTrue(self.check_ignored(repo, '.agent-memory'))
        self.assertTrue(self.check_ignored(repo, '.claude/settings.local.json'))
        self.assertEqual(self.git_out(repo, 'status', '--porcelain', '--untracked-files=all'), '')
        self.assertIn('private-excluded', self.pmem('status', cwd=repo).out)
        # Claude wiring and the stable hook copy
        self.assertEqual(self.settings(repo)['autoMemoryDirectory'], str(mem))
        stable = self.xdg_data / 'project-memory' / 'pmem.py'
        self.assertEqual(stable.read_bytes(), PMEM.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(str(stable)).st_mode), 0o755)
        self.assertNoTempFiles(mem)

        def state():
            return (tree_state(repo), self.repo_state(repo), self.mem_head(mem),
                    tree_state(self.xdg_data))
        before = state()
        self.assertIn('reusing', self.pmem('init', cwd=repo).stdout)
        self.assertEqual(state(), before)

    def test_existing_files_are_never_overwritten(self):
        repo = self.make_repo(self.work / 'keep')
        mem = repo / '.agent-memory'
        write(mem / 'MEMORY.md', '# mine\n\nprose line\n')
        write(mem / '.gitignore', 'custom-entry\n.pmem.lock')          # no final newline
        self.assertIn('reusing', self.pmem('init', cwd=repo).stdout)
        self.assertEqual(read_text(mem / 'MEMORY.md'), '# mine\n\nprose line\n')
        self.assertTrue((mem / 'HANDOFF.md').is_file())
        lines = read_text(mem / '.gitignore').splitlines()
        self.assertEqual(lines[:2], ['custom-entry', '.pmem.lock'])
        for line in GITIGNORE_LINES:
            self.assertEqual(lines.count(line), 1, lines)

    def test_here_in_a_subdir_excludes_the_path_from_toplevel(self):
        repo = self.make_repo(self.work / 'mono')
        pkg = repo / 'pkg' / 'a'
        pkg.mkdir(parents=True)
        self.pmem('init', '--here', cwd=pkg)
        mem = rp(pkg / '.agent-memory')
        exclude = read_text(repo / '.git' / 'info' / 'exclude').splitlines()
        self.assertEqual(exclude.count('/pkg/a/.agent-memory'), 1)
        self.assertTrue(self.check_ignored(repo, 'pkg/a/.agent-memory'))
        self.assertEqual(self.where(pkg), mem)
        self.assertFalse(os.path.lexists(str(repo / '.agent-memory')))
        self.assertEqual(self.settings(repo)['autoMemoryDirectory'], mem)   # Claude root = main root

    def test_existing_ignore_rules_add_no_exclude_lines(self):
        repo = self.make_repo(self.work / 'ign', {
            '.gitignore': '.agent-memory/\n.claude/settings.local.json\n', 'README.md': 'x\n'})
        before = self.repo_state(repo)
        self.pmem('init', cwd=repo)
        self.assertEqual(self.repo_state(repo), before)
        self.assertTrue(self.check_ignored(repo, '.agent-memory'))

    def test_track_mode(self):
        repo = self.make_repo(self.work / 'tracked')
        self.pmem('init', '--track', cwd=repo)
        mem = repo / '.agent-memory'
        self.assertFalse((mem / '.git').exists())
        ignore = read_text(mem / '.gitignore').splitlines()
        for line in GITIGNORE_LINES + ['HANDOFF.md', 'handoffs/']:
            self.assertEqual(ignore.count(line), 1, line)
        self.assertNotIn('/.agent-memory', read_text(repo / '.git' / 'info' / 'exclude').splitlines())
        self.assertFalse(self.check_ignored(repo, '.agent-memory/MEMORY.md'))
        self.assertTrue(self.check_ignored(repo, '.agent-memory/HANDOFF.md'))
        self.assertTrue(self.check_ignored(repo, '.claude/settings.local.json'))
        self.add(repo, 'my-pref', 'Prefers short answers', 'Short.', 'user')
        self.git(repo, 'add', '.agent-memory')
        self.git(repo, 'commit', '-q', '-m', 'track memory')
        status = self.pmem('status', cwd=repo).out
        self.assertIn('tracked', status)
        self.assertNotIn('tracked-unexpectedly', status)
        self.assertEqual(self.lint_rc(repo), 0)
        self.assertEqual(self.lint_rc(repo, '--strict'), 1)       # personal memory in tracked mode
        before = (tree_state(repo), self.repo_state(repo))
        self.pmem('init', '--track', cwd=repo)
        self.assertEqual((tree_state(repo), self.repo_state(repo)), before)

    def test_tracked_memory_without_track_flag_is_refused(self):
        # §9 D: reusing a tracked memory privately is refused (exit 2) unless --force
        repo = self.make_repo(self.work / 'oops')
        self.pmem('init', '--track', cwd=repo)
        self.git(repo, 'add', '.agent-memory')
        self.git(repo, 'commit', '-q', '-m', 'track memory')
        before = (tree_state(repo), self.repo_state(repo), tree_state(self.xdg_data))
        result = self.pmem('init', cwd=repo, rc=2)
        self.assertIn('tracked-unexpectedly', result.out)
        self.assertIn('rm -r --cached', result.out)
        self.assertEqual((tree_state(repo), self.repo_state(repo), tree_state(self.xdg_data)), before)
        result = self.pmem('init', '--force', cwd=repo)
        self.assertIn('tracked-unexpectedly', result.out)
        self.assertIn('rm -r --cached', result.out)

    def test_no_history_mode(self):
        repo = self.make_repo(self.work / 'nohist')
        self.pmem('init', '--no-history', cwd=repo)
        self.assertFalse((repo / '.agent-memory' / '.git').exists())
        self.assertTrue(self.check_ignored(repo, '.agent-memory'))
        self.pmem('snapshot', cwd=repo, rc=1)

    def test_non_git_workspace_wires_child_repos(self):
        ws = self.work / 'ws'
        repo_a = self.make_repo(ws / 'repo-a')
        repo_b = self.make_repo(ws / 'group' / 'repo-b')
        too_deep = self.make_repo(ws / 'x' / 'y' / 'repo-c')
        hidden = self.make_repo(ws / '.hidden' / 'repo-d')
        vendored = self.make_repo(ws / 'node_modules' / 'repo-e')
        own = self.make_repo(ws / 'repo-f')
        write(own / '.agent-memory' / 'MEMORY.md', '# own memory\n')
        external = self.make_repo(self.work / 'external')
        foreign_wt = self.add_worktree(external, ws / 'ext-wt', 'ws-branch')
        result = self.pmem('init', cwd=ws)
        mem = rp(ws / '.agent-memory')
        self.assertEqual(self.settings(ws)['autoMemoryDirectory'], mem)
        for child in (repo_a, repo_b):
            with self.subTest(child=child.name):
                data = self.settings(child)
                self.assertEqual(data['autoMemoryDirectory'], mem)
                self.assertEqual(len(self.our_hook_commands(data)), 5)
                self.assertTrue(self.check_ignored(child, '.claude/settings.local.json'))
                self.assertEqual(self.git_out(child, 'status', '--porcelain', '--untracked-files=all'), '')
                self.assertIn(child.name, result.stdout)
        for skipped in (too_deep, hidden, vendored, own, foreign_wt, external):
            with self.subTest(skipped=str(skipped)):
                self.assertFalse(os.path.lexists(str(self.settings_path(skipped))))
        (repo_a / 'src').mkdir()
        self.assertEqual(self.where(repo_a / 'src'), mem)

        repos = (repo_a, repo_b, too_deep, hidden, vendored, own, external)

        def state():
            return (tree_state(ws), [self.repo_state(r) for r in repos], self.mem_head(mem),
                    tree_state(external))
        before = state()
        self.assertIn('reusing', self.pmem('init', cwd=ws).stdout)
        self.assertEqual(state(), before)

    def test_no_children_flag(self):
        ws = self.work / 'ws2'
        repo_a = self.make_repo(ws / 'repo-a')
        self.pmem('init', '--no-children', '--no-history', cwd=ws)
        self.assertTrue(self.settings_path(ws).is_file())
        self.assertFalse(os.path.lexists(str(self.settings_path(repo_a))))

    def test_non_git_subdir_reuses_the_parent_memory(self):
        ws, _mem = self.init_project(self.work / 'ws3', '--no-history')
        (ws / 'notes').mkdir()
        self.assertIn('reusing', self.pmem('init', '--no-history', cwd=ws / 'notes').stdout)
        self.assertFalse(os.path.lexists(str(ws / 'notes' / '.agent-memory')))


# =============================================================================================
# Claude settings wiring

@GIT_REQUIRED
class ClaudeSettingsTests(Helpers):
    USER_STOP = {'hooks': [{'type': 'command', 'command': 'echo mine'}]}
    USER_POST = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo post'}]}

    def project(self, name):
        path = self.work / name
        path.mkdir(parents=True)
        return path

    def assert_ours_once(self, data):
        events = sorted(event for event, _group, _hook in self.our_hook_commands(data))
        self.assertEqual(events, ['PreCompact', 'SessionStart', 'Stop', 'StopFailure',
                                  'UserPromptSubmit'])

    def test_fresh_settings_and_runnable_hook_commands(self):
        root, mem = self.init_project(self.work / 'fresh', '--no-history')
        text = read_text(self.settings_path(root))
        data = json.loads(text)
        self.assertEqual(text, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        self.assertEqual(data['autoMemoryDirectory'], str(mem))
        self.assertIs(data['autoDreamEnabled'], False)
        self.assert_ours_once(data)
        commands = {}
        for event, group, hook in self.our_hook_commands(data):
            with self.subTest(event=event):
                command = hook['command']
                self.assertEqual(hook.get('type'), 'command')
                self.assertTrue(command.startswith("python3 '"), command)
                self.assertIn('2>/dev/null || true', command)
                self.assertTrue(command.rstrip().endswith(MARK), command)
                wanted = {'SessionStart': 'hook session-start',
                          'UserPromptSubmit': 'hook prompt'}.get(event, 'hook checkpoint')
                self.assertIn(wanted, command)
                self.assertEqual(hook.get('timeout'), 5 if event == 'UserPromptSubmit' else 10)
                if event == 'SessionStart':
                    self.assertEqual(group.get('matcher'), 'startup|resume|clear|compact')
                commands[event] = command
        # the commands work through a shell, inside and outside the project
        result = self.run_cmd(['/bin/sh', '-c', commands['SessionStart']], cwd=root,
                              stdin=self.session_payload(root))
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn(str(mem), self.context_of(result))
        result = self.run_cmd(['/bin/sh', '-c', commands['Stop']], cwd=root,
                              stdin=self.stop_payload(root, message='via the shell'))
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn('via the shell', read_text(mem / '.checkpoints' / 'main.md'))
        outside = self.work / 'outside'
        outside.mkdir()
        result = self.run_cmd(['/bin/sh', '-c', commands['SessionStart']], cwd=outside,
                              stdin=self.session_payload(outside))
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_merge_keeps_user_settings_and_is_idempotent(self):
        root = self.project('merge')
        original = {'permissions': {'allow': ['Bash(ls:*)']}, 'env': {'GREETING': 'grüß dich'},
                    'hooks': {'Stop': [self.USER_STOP], 'PostToolUse': [self.USER_POST]}}
        path = write(self.settings_path(root), '\ufeff' + json.dumps(original, indent=4))
        self.pmem('init', '--no-history', cwd=root)
        text = read_text(path)
        data = json.loads(text)
        self.assertEqual(text, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        self.assertIn('grüß dich', text)
        self.assertEqual(data['permissions'], original['permissions'])
        self.assertEqual(data['env'], original['env'])
        self.assertIn(self.USER_STOP, data['hooks']['Stop'])
        self.assertEqual(data['hooks']['PostToolUse'], [self.USER_POST])
        self.assert_ours_once(data)
        before = path.read_bytes()
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(path.read_bytes(), before)
        # hooks the user deleted come back, theirs stay
        data['hooks']['Stop'] = [self.USER_STOP]
        write(path, json.dumps(data))
        self.pmem('init', '--no-history', cwd=root)
        data = self.settings(root)
        self.assertIn(self.USER_STOP, data['hooks']['Stop'])
        self.assert_ours_once(data)

    def test_whitespace_only_settings_count_as_empty(self):
        root = self.project('blank')
        write(self.settings_path(root), ' \n\t\n')
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(self.settings(root)['autoMemoryDirectory'], rp(root / '.agent-memory'))

    def test_invalid_settings_are_never_written(self):
        for i, bad in enumerate(('{"permissions": ', 'not json at all\n', '[1, 2]\n',
                                 '"just a string"\n', '\ufeff{"a": 1,,}\n', 'null\n')):
            with self.subTest(bad=bad):
                root = self.project('bad-%d' % i)
                write(self.settings_path(root), bad)
                before = tree_state(root / '.claude')
                result = self.pmem('init', '--no-history', cwd=root, rc=None)
                self.assertNotEqual(result.returncode, 0, result.out)
                self.assertEqual(tree_state(root / '.claude'), before)
        write(self.settings_path(root), '{}\n')                        # positive control
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(self.settings(root)['autoMemoryDirectory'], rp(root / '.agent-memory'))

    def test_settings_symlink_is_written_through_and_mode_kept(self):
        root = self.project('linked-settings')
        real = write(self.base / 'dotfiles' / 'settings.local.json', '{"model": "x"}\n')
        os.chmod(str(real), 0o600)
        self.settings_path(root).parent.mkdir()
        os.symlink(str(real), str(self.settings_path(root)))
        self.pmem('init', '--no-history', cwd=root)
        self.assertTrue(self.settings_path(root).is_symlink())
        data = json.loads(read_text(real))
        self.assertEqual(data['model'], 'x')
        self.assertEqual(data['autoMemoryDirectory'], rp(root / '.agent-memory'))
        self.assertEqual(stat.S_IMODE(os.stat(str(real)).st_mode), 0o600)
        self.assertNoTempFiles(real.parent)

    def test_no_hooks_removes_only_ours(self):
        root, mem = self.init_project(self.work / 'nohooks', '--no-history')
        data = self.settings(root)
        data['hooks']['Stop'].append(self.USER_STOP)
        write(self.settings_path(root), json.dumps(data, indent=2))
        self.pmem('init', '--no-history', '--no-hooks', cwd=root)
        data = self.settings(root)
        self.assertEqual(self.our_hook_commands(data), [])
        self.assertNotIn(MARK, read_text(self.settings_path(root)))
        self.assertIn(self.USER_STOP, data['hooks']['Stop'])
        self.assertEqual(data['autoMemoryDirectory'], str(mem))

    def test_allow_dream_leaves_the_setting_alone(self):
        fresh = self.project('dream-a')
        self.pmem('init', '--no-history', '--allow-dream', cwd=fresh)
        self.assertNotIn('autoDreamEnabled', self.settings(fresh))
        kept = self.project('dream-b')
        write(self.settings_path(kept), json.dumps({'autoDreamEnabled': True}))
        self.pmem('init', '--no-history', '--allow-dream', cwd=kept)
        self.assertIs(self.settings(kept)['autoDreamEnabled'], True)
        self.pmem('init', '--no-history', cwd=kept)
        self.assertIs(self.settings(kept)['autoDreamEnabled'], False)

    def test_memory_directory_elsewhere_needs_force(self):
        root = self.project('elsewhere')
        other = self.base / 'old-memory'
        other.mkdir()
        path = write(self.settings_path(root), json.dumps({'autoMemoryDirectory': str(other)}))
        before = path.read_bytes()
        result = self.pmem('init', '--no-history', cwd=root, rc=None)
        self.assertNotEqual(result.returncode, 0, result.out)
        self.assertEqual(path.read_bytes(), before)
        result = self.pmem('init', '--no-history', '--force', cwd=root)
        self.assertEqual(self.settings(root)['autoMemoryDirectory'], rp(root / '.agent-memory'))
        self.assertIn(str(other), result.out)
        self.assertIn('pmem import', result.out)

    def test_equivalent_spelling_of_the_same_directory_is_accepted(self):
        root = self.project('same')
        rel = os.path.relpath(str(root), str(self.home))
        write(self.settings_path(root),
              json.dumps({'autoMemoryDirectory': '~/' + rel + '/.agent-memory/'}))
        self.pmem('init', '--no-history', cwd=root)
        value = self.settings(root)['autoMemoryDirectory']   # kept or normalised: both fine
        if value.startswith('~/'):
            value = str(self.home) + value[1:]
        self.assertEqual(rp(value.rstrip('/')), rp(root / '.agent-memory'))
        self.assert_ours_once(self.settings(root))

    def test_tracked_settings_file_is_refused_without_force(self):
        repo = self.make_repo(self.work / 'shared', {
            '.claude/settings.local.json': '{"permissions": {}}\n', 'README.md': 'x\n'})
        path = self.settings_path(repo)
        before = path.read_bytes()
        result = self.pmem('init', cwd=repo, rc=None)
        self.assertNotEqual(result.returncode, 0, result.out)
        self.assertEqual(path.read_bytes(), before)
        self.pmem('init', '--force', cwd=repo)
        self.assertEqual(self.settings(repo)['autoMemoryDirectory'], rp(repo / '.agent-memory'))

    def test_no_claude_writes_no_settings(self):
        root, _mem = self.init_project(self.work / 'noclaude', '--no-history', '--no-claude')
        self.assertFalse(os.path.lexists(str(self.settings_path(root))))

    def test_stable_hook_copy_location_and_refresh(self):
        root, _mem = self.init_project(self.work / 'stable', '--no-history')
        stable = self.xdg_data / 'project-memory' / 'pmem.py'
        write(stable, '# stale copy\n')
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(stable.read_bytes(), PMEM.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(str(stable)).st_mode), 0o755)
        # without XDG_DATA_HOME the copy goes to ~/.local/share
        env = dict(self.env)
        del env['XDG_DATA_HOME']
        other = self.work / 'stable-2'
        other.mkdir()
        self.pmem('init', '--no-history', cwd=other, env=env)
        fallback = self.home / '.local' / 'share' / 'project-memory' / 'pmem.py'
        self.assertEqual(fallback.read_bytes(), PMEM.read_bytes())
        self.assertIn(str(fallback.relative_to(self.home)), read_text(self.settings_path(other)))


# =============================================================================================
# add / find / index / lint / archive

@GIT_REQUIRED
class MemoryCommandTests(Helpers):
    def setUp(self):
        super().setUp()
        self.root, self.mem = self.init_project(self.work / 'proj')

    def index(self):
        return self.index_text(self.mem)

    def test_add_writes_a_native_compatible_memory(self):
        desc = 'Use uv: never pip # really'
        self.add(self.root, 'use-uv', desc, 'Always use uv.\n\n**Why:** speed', 'feedback')
        path = self.mem / 'use-uv.md'
        text = read_text(path)
        self.assertEqual(fm_get(text, 'name'), 'use-uv')
        self.assertEqual(fm_get(text, 'description'), desc)
        self.assertEqual(fm_get(text, 'type'), 'feedback')
        self.assertRegex(fm_get(text, 'modified') or '', '^' + ISO_UTC + '$')
        self.assertIn('Always use uv.', body_of(text))
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o644)
        self.assertEqual(lines_linking(self.index(), 'use-uv.md'), ['- [use-uv](use-uv.md) — ' + desc])
        self.assertEqual(memory_link_lines(self.index())[0], HANDOFF_LINE)
        self.assertEqual(self.mem_status(self.mem), '')                  # auto-snapshot
        self.pmem('add', 'reference', 'dash-board', '-d', 'Where the dashboards live',
                  '--title', 'Dashboards', '--stdin', cwd=self.root, stdin='Body from stdin\n')
        self.assertIn('Body from stdin', read_text(self.mem / 'dash-board.md'))
        self.assertEqual(lines_linking(self.index(), 'dash-board.md'),
                         ['- [Dashboards](dash-board.md) — Where the dashboards live'])
        self.pmem('lint', '--strict', cwd=self.root)
        self.assertNoTempFiles(self.mem)

    def test_add_validation_and_force(self):
        bad_calls = (('bogus', 'x-y'), ('project', 'Bad_Name'), ('project', 'Upper'),
                     ('project', 'memory'), ('project', 'handoff'), ('project', 'HANDOFF'),
                     ('project', '../escape'), ('project', 'a/b'))
        for type_, name in bad_calls:
            with self.subTest(type=type_, name=name):
                before = tree_state(self.mem)
                result = self.pmem('add', type_, name, '-d', 'd', '--body', 'x', cwd=self.root, rc=None)
                self.assertNotEqual(result.returncode, 0, result.out)
                self.assertEqual(tree_state(self.mem), before)
        self.add(self.root, 'build-cmd', 'How to build', 'make it')
        self.pmem('add', 'project', 'build-cmd', '-d', 'How to build v2', '--body', 'make all',
                  cwd=self.root, rc=2)
        self.assertNotIn('make all', read_text(self.mem / 'build-cmd.md'))
        self.pmem('add', 'project', 'build-cmd', '-d', 'How to build v2', '--body', 'make all',
                  '--force', cwd=self.root)
        self.assertIn('make all', read_text(self.mem / 'build-cmd.md'))
        self.assertEqual(len(lines_linking(self.index(), 'build-cmd.md')), 1)
        outside = self.work / 'no-memory'
        outside.mkdir()
        self.assertNotEqual(self.pmem('add', 'project', 'x-y', '-d', 'd', '--body', 'b',
                                      cwd=outside, rc=None).returncode, 0)
        self.assertFalse(os.path.lexists(str(outside / '.agent-memory')))

    def test_index_line_endings_are_kept(self):
        crlf = self.index().replace('\n', '\r\n')
        write(self.mem / 'MEMORY.md', crlf)
        self.add(self.root, 'crlf-fact', 'Added to a CRLF index')
        raw = (self.mem / 'MEMORY.md').read_bytes()
        self.assertTrue(raw.startswith(crlf.encode('utf-8')))
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))
        self.assertEqual(len(lines_linking(raw.decode('utf-8').replace('\r', ''), 'crlf-fact.md')), 1)

    def test_parallel_adds_keep_every_index_line(self):
        root, mem = self.init_project(self.work / 'parallel', '--no-history')
        names = ['para-%d' % i for i in range(6)]
        env = dict(self.env, PWD=str(root))
        procs = [subprocess.Popen([sys.executable, str(PMEM), 'add', 'project', name, '-d',
                                   'parallel ' + name, '--body', name],
                                  cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for name in names]
        for proc in procs:
            out, err = proc.communicate(timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, (out + err).decode('utf-8', 'replace'))
        index = self.index_text(mem)
        for name in names:
            self.assertEqual(len(lines_linking(index, name + '.md')), 1, index)
            self.assertTrue((mem / (name + '.md')).is_file())
        self.assertEqual(memory_link_lines(index)[0], HANDOFF_LINE)

    def test_find(self):
        self.add(self.root, 'use-uv', 'Use uv not pip', 'Always use uv for installs.', 'feedback')
        self.add(self.root, 'deploy-steps', 'Deploy procedure', 'Run make deploy then check the dashboard.')
        result = self.pmem('find', 'UV', cwd=self.root)
        self.assertIn('use-uv', result.stdout)
        self.assertIn('use-uv.md', result.stdout)
        self.assertNotIn('deploy-steps', result.stdout)
        result = self.pmem('find', 'deploy', 'DASHBOARD', cwd=self.root)
        self.assertIn('deploy-steps', result.stdout)
        self.assertNotIn('use-uv', result.stdout)
        self.pmem('find', 'uv', 'dashboard', cwd=self.root, rc=1)            # AND
        self.pmem('find', 'zzz-no-such-term', cwd=self.root, rc=1)
        self.set_goal(self.mem / 'HANDOFF.md', 'Ship the quokka migration')
        self.assertIn('HANDOFF.md', self.pmem('find', 'quokka', cwd=self.root).stdout)
        self.pmem('archive', 'deploy-steps', cwd=self.root)
        self.pmem('find', 'dashboard', cwd=self.root, rc=1)
        self.assertIn('deploy-steps', self.pmem('find', 'dashboard', '--archive', cwd=self.root).stdout)

    def test_index_report_and_fix(self):
        write(self.mem / 'kept.md', mem_file('kept', 'kept fact'))
        write(self.mem / 'orphan.md', mem_file('orphan', 'An unindexed fact'))
        write(self.mem / 'plain.md', '\n\nFirst real line here\nsecond line\n')
        write(self.mem / 'my file.md', mem_file('my-file', 'Spaced name'))
        write(self.mem / 'handoffs' / 'side.md', mem_file('handoff-side', 'side handoff'))
        write(self.mem / '.archive' / 'retired.md', mem_file('retired', 'retired fact'))
        write(self.mem / '.hidden.md', mem_file('hidden', 'hidden fact'))
        original = (
            '# Project memory\n'
            '\n'
            'Intro prose that must stay.\n'
            '\n'
            '- [Existing](kept.md) — kept fact\n'
            + HANDOFF_LINE + '\n'
            '- [My](my%20file.md#top) — percent-encoded link\n'
            '- [Gone](gone.md) — dangling\n'
            '- see [guide](docs/guide.md) and [site](https://example.com/a.md)\n'
            '\n'
            '## Notes\n'
            'Closing prose.\n'
        )
        write(self.mem / 'MEMORY.md', original)
        report = self.pmem('index', cwd=self.root, rc=None)
        for name in ('orphan.md', 'plain.md', 'gone.md'):
            self.assertIn(name, report.out)
        self.assertEqual(self.index(), original)                          # report only
        self.pmem('index', '--fix', cwd=self.root)
        fixed = self.index()
        self.assertEqual(memory_link_lines(fixed)[0], HANDOFF_LINE)
        self.assertEqual(fixed.splitlines().count(HANDOFF_LINE), 1)
        self.assertEqual(lines_linking(fixed, 'gone.md'), [])
        self.assertEqual(lines_linking(fixed, 'orphan.md'), ['- [orphan](orphan.md) — An unindexed fact'])
        self.assertEqual(lines_linking(fixed, 'plain.md'), ['- [plain](plain.md) — First real line here'])
        self.assertEqual(lines_linking(fixed, 'my file.md'), ['- [My](my%20file.md#top) — percent-encoded link'])
        self.assertEqual(lines_linking(fixed, 'kept.md'), ['- [Existing](kept.md) — kept fact'])
        for line in fixed.splitlines():
            target = link_target(line) or ''
            self.assertFalse(target.startswith(('handoffs/', '.archive', '.hidden')), line)
        self.assertEqual(non_link_lines(fixed), non_link_lines(original))
        self.pmem('index', '--fix', cwd=self.root)
        self.assertEqual(self.index(), fixed)

    def test_index_fix_inserts_a_missing_handoff_line(self):
        write(self.mem / 'kept.md', mem_file('kept', 'kept fact'))
        original = '# Project memory\n\nprose\n\n- [Kept](kept.md) — kept fact\n'
        write(self.mem / 'MEMORY.md', original)
        self.pmem('index', '--fix', cwd=self.root)
        fixed = self.index()
        self.assertEqual(link_target(memory_link_lines(fixed)[0]), 'HANDOFF.md')
        self.assertEqual(lines_linking(fixed, 'kept.md'), ['- [Kept](kept.md) — kept fact'])
        self.assertEqual(non_link_lines(fixed), non_link_lines(original))

    def test_lint_accepts_native_style_frontmatter(self):
        write(self.mem / 'native-style.md', (
            '---\n'
            'name: "native-style"\n'
            "description: 'Native writes: it''s compatible'\n"
            'metadata:\n'
            '    node_type: memory\n'
            '    type: feedback\n'
            '    originSessionId: 0f0e-11\n'
            '    modified: 2026-11-20T01:02:03.456Z\n'
            '---\n\nPrefer the native format.\n\n**Why:** compatibility.\n'
            '**How to apply:** keep frontmatter small.\n'))
        write(self.mem / 'top-level.md', (
            '---\n'
            'name: top-level\n'
            'description: "Top-level type with \\"escapes\\" and \\u00e9"\n'
            'type: reference\n'
            'updated: 2026-11-21 08:00\n'
            '---\n\nReference body.\n'))
        write(self.mem / 'offset-time.md', mem_file('offset-time', 'Offset timestamp',
                                                    modified='2026-11-22T10:00:00+08:00'))
        write(self.mem / 'ancient.md', mem_file('ancient', 'Very old fact', modified='2001-01-01T00:00:00Z'))
        write(self.mem / 'MEMORY.md', self.index().replace(
            '## Memories\n', '- [native-style](native-style.md) — Native writes\n\n## Memories\n'))
        self.append_index(self.mem, '- [top-level](top-level.md) — Top-level type',
                          '- [offset-time](offset-time.md) — Offset timestamp',
                          '- [ancient](ancient.md) — Very old fact')
        self.pmem('lint', '--strict', cwd=self.root)
        self.pmem('lint', '--strict', '--stale-days', '1', cwd=self.root)   # stale is INFO only
        write(self.mem / 'team' / 'note.md', mem_file('note', 'reserved dir'))  # INFO only
        self.pmem('lint', '--strict', cwd=self.root)

    def test_lint_errors_exit_1(self):
        clean_index = self.index()
        files = {
            'no-frontmatter.md': 'just text\n',
            'unclosed.md': '---\nname: unclosed\ndescription: d\n' + 'extra: x\n' * 40 + '---\nbody\n',
            'no-desc.md': '---\nname: no-desc\nmetadata:\n  type: project\n---\nbody\n',
            'no-name.md': '---\ndescription: d\nmetadata:\n  type: project\n---\nbody\n',
            'no-type.md': '---\nname: no-type\ndescription: d\n---\nbody\n',
            'bad-type.md': '---\nname: bad-type\ndescription: d\nmetadata:\n  type: opinion\n---\nbody\n',
        }
        for name, content in files.items():
            with self.subTest(file=name):
                write(self.mem / name, content)
                self.append_index(self.mem, '- [%s](%s) — case' % (name[:-3], name))
                self.assertEqual(self.lint_rc(self.root), 1)
                (self.mem / name).unlink()
                write(self.mem / 'MEMORY.md', clean_index)
        with self.subTest(case='dangling link'):
            self.append_index(self.mem, '- [Ghost](ghost.md) — gone')
            self.assertEqual(self.lint_rc(self.root), 1)
            write(self.mem / 'MEMORY.md', clean_index)
        with self.subTest(case='duplicate name'):
            write(self.mem / 'dup-one.md', mem_file('dup', 'first'))
            write(self.mem / 'dup-two.md', mem_file('dup', 'second'))
            self.append_index(self.mem, '- [one](dup-one.md) — first', '- [two](dup-two.md) — second')
            self.assertEqual(self.lint_rc(self.root), 1)
            for name in ('dup-one.md', 'dup-two.md'):
                (self.mem / name).unlink()
            write(self.mem / 'MEMORY.md', clean_index)
        with self.subTest(case='missing HANDOFF.md'):
            saved = (self.mem / 'HANDOFF.md').read_bytes()
            (self.mem / 'HANDOFF.md').unlink()
            self.assertEqual(self.lint_rc(self.root), 1)
            write(self.mem / 'HANDOFF.md', saved)
        self.assertEqual(self.lint_rc(self.root), 0)
        outside = self.work / 'nothing-here'
        outside.mkdir()
        self.assertEqual(self.lint_rc(outside), 1)

    def test_lint_warnings_fail_only_with_strict(self):
        clean_index = self.index()
        handoff = (self.mem / 'HANDOFF.md').read_bytes()
        write(self.mem / '.archive' / 'retired-one.md', mem_file('retired-one', 'retired'))

        def memory(name, text, index_line=True):
            write(self.mem / (name + '.md'), text)
            if index_line:
                self.append_index(self.mem, '- [%s](%s.md) — case' % (name, name))

        def unindexed():
            memory('lonely', mem_file('lonely', 'not in the index'), index_line=False)

        def handoff_not_first():
            write(self.mem / 'early.md', mem_file('early', 'first'))
            write(self.mem / 'MEMORY.md', clean_index.replace(
                HANDOFF_LINE, '- [early](early.md) — first\n' + HANDOFF_LINE))

        def long_index_line():
            memory('wordy', mem_file('wordy', 'long line'), index_line=False)
            self.append_index(self.mem, '- [wordy](wordy.md) — ' + 'w' * 200)

        def needs_merge():
            memory('foo', mem_file('foo', 'local'))
            memory('foo-imported', mem_file('foo', 'imported copy'))

        cases = [
            ('unindexed', unindexed),
            ('handoff not first', handoff_not_first),
            ('big file', lambda: memory('big', mem_file('big', 'big', 'x' * 5000))),
            ('broken wiki link', lambda: memory('linker', mem_file('linker', 'links',
                                                                   'See [[nowhere-at-all]].'))),
            ('name differs from stem', lambda: memory('stem-a', mem_file('stem-b', 'mismatch'))),
            ('unparseable timestamp', lambda: memory('when', mem_file('when', 'bad time',
                                                                      modified='someday'))),
            ('old handoff', lambda: self.set_modified(self.mem / 'HANDOFF.md', '2001-01-01T00:00:00Z',
                                                      mtime=978307200)),
            ('long index line', long_index_line),
            ('needs merge', needs_merge),
        ]
        for label, make in cases:
            with self.subTest(case=label):
                make()
                result = self.pmem('lint', cwd=self.root, rc=0)
                self.assertEqual(self.lint_rc(self.root, '--strict'), 1, result.out)
                if label == 'needs merge':
                    self.assertIn('needs merge', result.out)
                for path in self.mem.iterdir():
                    if path.is_file() and path.suffix == '.md' and path.name not in (
                            'MEMORY.md', 'HANDOFF.md'):
                        path.unlink()
                write(self.mem / 'MEMORY.md', clean_index)
                write(self.mem / 'HANDOFF.md', handoff)
        with self.subTest(case='links to archived'):
            memory('pointer', mem_file('pointer', 'points at an archived memory', 'See [[retired-one]].'))
            result = self.pmem('lint', cwd=self.root)
            self.assertIn('links to archived', result.out)
        self.assertEqual(self.lint_rc(self.root, '--strict'), 1)

    def test_lint_secret_rules(self):
        github = 'gh' + 'p_' + 'a1B2c3D4e5' * 3 + 'f6G7h8'
        aws = 'AK' + 'IA' + 'ABCDEFGHIJ234567'
        cases = [
            ('clean', 'Nothing sensitive here.', 0),
            ('github token', 'pat ' + github, 1),
            ('aws key', 'key ' + aws, 1),
            ('private key', '-----BEGIN ' + 'RSA PRIVATE KEY-----', 1),
            ('slack', 'xo' + 'xb-' + '1234567890-abcdef', 1),
            ('openai style', 's' + 'k-' + 'Ab3_' * 6, 1),
            ('url credentials', 'https://deploy:' + 'pa55w0rd' + '@' + 'gitserver/x', 1),
            ('password value', 'pass' + 'word: hunter2abc9', 1),
            ('env reference', 'tok' + 'en = $GITHUB_TOKEN', 0),
            ('placeholder', 'api' + '_key: <your-key-1>', 0),
            ('env name', 'sec' + 'ret: MY_SECRET_ENV1', 0),
            ('short value', 'pass' + 'word: ab1', 0),
            ('no digit', 'pass' + 'word: abcdefghijkl', 0),
            ('allowed', 'pat ' + github + ' pmem:allow-secret', 0),
        ]
        self.append_index(self.mem, '- [sample](sample.md) — sample')
        for label, line, expected in cases:
            with self.subTest(case=label):
                write(self.mem / 'sample.md', mem_file('sample', 'sample', line))
                self.assertEqual(self.lint_rc(self.root, '--strict'), expected)
                if expected:
                    self.assertEqual(self.lint_rc(self.root), 0)

    def test_lint_paths(self):
        write(self.root / 'docs' / 'real.md', 'x\n')
        body = ('See `docs/real.md`, `~/notes/x`, `$HOME/a/b`, `https://example.com/a/b`, '
                '`/etc/hosts/x`, `<root>/x`, `-C dir/x` and `two words/x`.')
        write(self.mem / 'paths.md', mem_file('paths', 'path references', body))
        self.append_index(self.mem, '- [paths](paths.md) — path references')
        self.pmem('lint', '--strict', '--paths', cwd=self.root)
        write(self.mem / 'paths.md', mem_file('paths', 'path references',
                                              body + ' Also `src/missing/file.py`.'))
        self.pmem('lint', '--strict', cwd=self.root)
        result = self.pmem('lint', '--strict', '--paths', cwd=self.root, rc=1)
        self.assertIn('src/missing/file.py', result.out)

    def test_lint_index_size_caps(self):
        clean_index = self.index()
        cases = [
            ('over 150 lines', ['note line %d' % i for i in range(160)], 0),
            ('over 200 lines', ['note line %d' % i for i in range(210)], 1),
            ('over 20k chars', ['c' * 140 for _ in range(145)], 0),
            ('over 25k chars', ['c' * 145 for _ in range(180)], 1),
        ]
        for label, lines, plain_rc in cases:
            with self.subTest(case=label):
                write(self.mem / 'MEMORY.md', clean_index + '\n'.join(lines) + '\n')
                self.assertEqual(self.lint_rc(self.root), plain_rc)
                self.assertEqual(self.lint_rc(self.root, '--strict'), 1)
        write(self.mem / 'MEMORY.md', clean_index)

    def test_archive(self):
        self.add(self.root, 'old-note', 'Old note', 'version one')
        write(self.mem / 'feedback_legacy.md', mem_file('feedback_legacy', 'legacy name'))
        self.append_index(self.mem, '- [legacy](feedback_legacy.md) — legacy name')
        self.pmem('archive', 'old-note', 'feedback_legacy', '--reason', 'superseded by new-note',
                  cwd=self.root)
        archive = self.mem / '.archive'
        self.assertFalse((self.mem / 'old-note.md').exists())
        self.assertFalse((self.mem / 'feedback_legacy.md').exists())
        moved = read_text(archive / 'old-note.md')
        self.assertTrue(any(re.match(r'^archived: .+ — superseded by new-note\s*$', line)
                            for line in moved.splitlines()), moved)
        self.assertTrue((archive / 'feedback_legacy.md').is_file())
        self.assertEqual(lines_linking(self.index(), 'old-note.md'), [])
        self.assertEqual(lines_linking(self.index(), 'feedback_legacy.md'), [])
        self.assertEqual(memory_link_lines(self.index()), [HANDOFF_LINE])
        self.add(self.root, 'old-note', 'Old note again', 'version two')
        self.pmem('archive', 'old-note', cwd=self.root)
        self.assertIn('version one', read_text(archive / 'old-note.md'))
        self.assertIn('version two', read_text(archive / 'old-note-2.md'))
        self.assertEqual(self.mem_status(self.mem), '')
        for bad in ('handoff', 'HANDOFF', 'memory', 'MEMORY', '../outside', 'no-such-note',
                    '.gitignore'):
            with self.subTest(name=bad):
                before = tree_state(self.mem)
                result = self.pmem('archive', bad, cwd=self.root, rc=None)
                self.assertNotEqual(result.returncode, 0, result.out)
                self.assertEqual(tree_state(self.mem), before)
        self.pmem('lint', cwd=self.root)

    def test_archive_retires_handoffs_but_never_the_main_one(self):
        handoff = Path(self.pmem('handoff', 'path', '--key', 'old-stream', cwd=self.root).stdout.strip())
        self.set_goal(handoff, 'OLD-STREAM-GOAL')
        self.add(self.root, 'stale-note', 'Stale note')
        main_before = (self.mem / 'HANDOFF.md').read_bytes()
        self.pmem('archive', 'handoffs/old-stream', 'stale-note', '--reason', 'worktree removed',
                  cwd=self.root)
        archived = self.mem / '.archive' / 'handoffs' / 'old-stream.md'
        self.assertFalse(handoff.exists())
        text = read_text(archived)
        self.assertIn('OLD-STREAM-GOAL', text)
        self.assertTrue(any(re.match(r'^archived: .+ — worktree removed\s*$', line)
                            for line in text.splitlines()), text)
        self.assertTrue((self.mem / '.archive' / 'stale-note.md').is_file())
        self.assertEqual(lines_linking(self.index(), 'stale-note.md'), [])
        self.assertEqual((self.mem / 'HANDOFF.md').read_bytes(), main_before)
        self.assertEqual(self.mem_status(self.mem), '')
        index_before = self.index()
        again = Path(self.pmem('handoff', 'path', '--key', 'old-stream', cwd=self.root).stdout.strip())
        self.pmem('archive', 'handoffs/old-stream.md', cwd=self.root)        # with .md; collision
        self.assertFalse(again.exists())
        self.assertTrue((self.mem / '.archive' / 'handoffs' / 'old-stream-2.md').is_file())
        self.assertIn('OLD-STREAM-GOAL', read_text(archived))
        self.assertEqual(self.index(), index_before)
        self.assertEqual(self.mem_status(self.mem), '')
        self.assertIn('old-stream', self.pmem('find', 'OLD-STREAM-GOAL', '--archive', cwd=self.root).stdout)
        self.pmem('find', 'OLD-STREAM-GOAL', cwd=self.root, rc=1)
        for bad in ('HANDOFF', 'HANDOFF.md', 'handoffs/missing', 'handoffs/../MEMORY', 'handoffs/',
                    'handoffs', '.archive/handoffs/old-stream', 'logs/x', '/abs/handoffs/x'):
            with self.subTest(name=bad):
                before = tree_state(self.mem)
                result = self.pmem('archive', bad, cwd=self.root, rc=None)
                self.assertNotEqual(result.returncode, 0, result.out)
                self.assertEqual(tree_state(self.mem), before)
        self.assertTrue((self.mem / 'HANDOFF.md').is_file())
        self.pmem('lint', cwd=self.root)


# =============================================================================================
# private history

@GIT_REQUIRED
class HistoryTests(Helpers):
    def test_snapshot_diff_and_history(self):
        root, mem = self.init_project(self.work / 'hist')
        self.assertIn('nothing to snapshot', self.pmem('snapshot', cwd=root).out)
        self.add(root, 'keep-me', 'Keep me', 'v1')
        self.add(root, 'drop-me', 'Drop me', 'bye')
        write(mem / 'keep-me.md', read_text(mem / 'keep-me.md') + 'edited\n')
        (mem / 'drop-me.md').unlink()
        write(mem / 'brand-new.md', mem_file('brand-new', 'Brand new'))
        diff = self.pmem('diff', cwd=root).out
        for name in ('keep-me.md', 'drop-me.md', 'brand-new.md'):
            self.assertIn(name, diff)
        self.pmem('snapshot', '-m', 'manual checkpoint', cwd=root)
        self.assertEqual(self.mem_subject(mem), 'manual checkpoint')
        self.assertEqual(self.mem_status(mem), '')
        history = self.pmem('history', cwd=root).stdout
        self.assertIn('manual checkpoint', history)
        self.assertIn('pmem init', history)
        latest = self.pmem('history', '-n', '1', cwd=root).stdout
        self.assertIn('manual checkpoint', latest)
        self.assertNotIn('pmem init', latest)
        self.assertIn('keep-me', self.pmem('history', 'keep-me', cwd=root).stdout)

    def test_restore(self):
        root, mem = self.init_project(self.work / 'restore')
        self.add(root, 'fact', 'A fact', 'version one')
        version_one = (mem / 'fact.md').read_bytes()
        self.pmem('add', 'project', 'fact', '-d', 'A fact', '--body', 'version two', '--force', cwd=root)
        version_two = (mem / 'fact.md').read_bytes()
        index = self.index_text(mem)
        # deleted (and snapshotted) file + index line come back from the last commit that had it
        (mem / 'fact.md').unlink()
        write(mem / 'MEMORY.md', '\n'.join(l for l in index.splitlines()
                                           if link_target(l) != 'fact.md') + '\n')
        self.pmem('snapshot', '-m', 'oops', cwd=root)
        self.pmem('restore', 'fact', cwd=root)
        self.assertEqual((mem / 'fact.md').read_bytes(), version_two)
        self.assertEqual(len(lines_linking(self.index_text(mem), 'fact.md')), 1)
        # unsnapshotted edits block a restore unless --force
        write(mem / 'fact.md', 'local edit\n')
        self.assertNotEqual(self.pmem('restore', 'fact', cwd=root, rc=None).returncode, 0)
        self.assertEqual(read_text(mem / 'fact.md'), 'local edit\n')
        shas = self.git_out(mem, 'log', '--format=%H', '--', 'fact.md').split()
        older = [s for s in shas
                 if self.git(mem, 'show', s + ':fact.md', rc=None).stdout == version_one.decode('utf-8')]
        self.assertTrue(older)
        self.pmem('restore', 'fact', '--rev', older[0], '--force', cwd=root)
        self.assertEqual((mem / 'fact.md').read_bytes(), version_one)
        # handoffs are restored by relative path
        handoff = (mem / 'HANDOFF.md').read_bytes()
        (mem / 'HANDOFF.md').unlink()
        self.pmem('snapshot', '-m', 'lost handoff', cwd=root)
        self.pmem('restore', 'HANDOFF.md', cwd=root)
        self.assertEqual((mem / 'HANDOFF.md').read_bytes(), handoff)

    def test_commands_need_private_history(self):
        root, _mem = self.init_project(self.work / 'nohist', '--no-history')
        self.pmem('snapshot', cwd=root, rc=1)
        self.assertNotEqual(self.pmem('restore', 'anything', cwd=root, rc=None).returncode, 0)


# =============================================================================================
# import

@GIT_REQUIRED
class ImportTests(Helpers):
    def legacy_dir(self, project_path, cfg=None):
        return Path(cfg or self.claude_cfg) / 'projects' / claude_slug(str(project_path)) / 'memory'

    def test_slug_helper_matches_the_spec(self):
        self.assertEqual(claude_slug('/home/u/.local/share/x'), '-home-u--local-share-x')
        self.assertEqual(claude_slug('/t/项目😀'), '-t-' + '-' * 4)     # emoji = 2 UTF-16 units
        self.assertRegex(claude_slug('/p/' + 'a' * 300), r'^-p-a{197}-[0-9a-z]+$')

    def test_import_claude_legacy_memory(self):
        root, mem = self.init_project(self.work / 'legacy-proj')
        src = self.legacy_dir(rp(root))
        write(src / 'MEMORY.md', LEGACY_INDEX)
        write(src / 'alpha.md', mem_file('alpha', 'Alpha fact', 'alpha body'))
        write(src / 'beta.md', mem_file('beta', 'Beta fact', 'beta body'))
        write(src / 'gamma.md', mem_file('gamma', 'Gamma desc', 'gamma body'))
        write(src / '.hidden.md', mem_file('hidden', 'hidden'))
        write(src / 'sessions' / 'old.md', mem_file('old', 'reserved dir'))
        write(src / 'notes.txt', 'not markdown\n')
        source_before = tree_state(src)
        self.assertIn('import --claude', self.pmem('status', cwd=root, rc=None).out)
        self.pmem('import', '--claude', cwd=root)
        for name in ('alpha.md', 'beta.md', 'gamma.md'):
            self.assertEqual((mem / name).read_bytes(), (src / name).read_bytes(), name)
        for name in ('.hidden.md', 'sessions', 'notes.txt'):
            self.assertFalse(os.path.lexists(str(mem / name)), name)
        index = self.index_text(mem)
        self.assertEqual(lines_linking(index, 'alpha.md'), ['- [Alpha](alpha.md) — alpha hook'])
        self.assertEqual(lines_linking(index, 'beta.md'), ['- [Beta](beta.md) — beta hook'])
        self.assertEqual(lines_linking(index, 'gamma.md'), ['- [gamma](gamma.md) — Gamma desc'])
        lines = index.splitlines()
        headings = [l for l in lines if re.match(r'^## Imported \d{4}-\d{2}-\d{2}\s*$', l)]
        self.assertEqual(len(headings), 1, index)
        self.assertLess(lines.index(headings[0]), lines.index('- [Alpha](alpha.md) — alpha hook'))
        self.assertEqual(memory_link_lines(index)[0], HANDOFF_LINE)
        for line in lines_linking(index, 'imported-notes.md'):
            self.assertLessEqual(len(line), 150, line)          # no lint WARN from pmem's own line
        notes = read_text(mem / 'imported-notes.md')
        self.assertEqual(fm_get(notes, 'type'), 'project')
        self.assertTrue((fm_get(notes, 'description') or '').startswith(
            'Inline notes from a legacy memory index'), notes)
        for needle in ('## Gotchas', 'Always run make before test', '- plain bullet without link'):
            self.assertIn(needle, notes)
        self.assertNotIn('generated by a tool', notes)
        self.assertNotIn('(alpha.md)', notes)
        manifest = json.loads(read_text(mem / '.imported.json'))
        entry = manifest[rp(src)]
        self.assertEqual(entry['alpha.md']['sha256'], sha256(src / 'alpha.md'))
        self.assertTrue(entry['alpha.md']['target'].endswith('alpha.md'))
        self.assertIn('__inline__', entry)
        self.assertEqual(tree_state(src), source_before)
        self.assertEqual(self.mem_status(mem), '')
        self.pmem('lint', cwd=root)
        before = (tree_state(root), self.mem_head(mem))
        self.pmem('import', '--claude', cwd=root)
        self.assertEqual((tree_state(root), self.mem_head(mem)), before)
        self.assertEqual(tree_state(src), source_before)

    def test_conflicts_identical_files_and_needs_merge(self):
        root, mem = self.init_project(self.work / 'conflict')
        self.add(root, 'alpha', 'Local alpha', 'local version')
        local_alpha = (mem / 'alpha.md').read_bytes()
        beta = mem_file('beta', 'Beta fact', 'same everywhere')
        write(mem / 'beta.md', beta)
        first = self.base / 'src1'
        write(first / 'MEMORY.md', '- [Alpha](alpha.md) — alpha hook\n- [Beta](beta.md) — beta hook\n')
        write(first / 'alpha.md', mem_file('alpha', 'Legacy alpha', 'legacy version'))
        write(first / 'beta.md', beta)
        result = self.pmem('import', first, cwd=root)
        self.assertIn('needs merge', result.out)
        self.assertEqual((mem / 'alpha.md').read_bytes(), local_alpha)
        self.assertEqual((mem / 'alpha-imported.md').read_bytes(), (first / 'alpha.md').read_bytes())
        self.assertFalse((mem / 'beta-imported.md').exists())
        self.assertEqual(lines_linking(self.index_text(mem), 'alpha-imported.md'),
                         ['- [Alpha](alpha-imported.md) — alpha hook'])
        second = self.base / 'src2'                  # same bytes as an existing *-imported* file
        write(second / 'alpha.md', (first / 'alpha.md').read_bytes())
        self.pmem('import', second, cwd=root)
        self.assertFalse((mem / 'alpha-imported-2.md').exists())
        third = self.base / 'src3'
        write(third / 'alpha.md', mem_file('alpha', 'Third alpha', 'third version'))
        self.assertIn('needs merge', self.pmem('import', third, cwd=root).out)
        self.assertEqual((mem / 'alpha-imported-2.md').read_bytes(), (third / 'alpha.md').read_bytes())
        self.assertIn('needs merge', self.pmem('lint', cwd=root).out)

    def test_claude_sources_cover_paths_accounts_and_worktrees(self):
        root = self.make_repo(self.work / 'multi')
        wt = self.add_worktree(root, self.work / 'multi-wt', 'side')
        self.pmem('init', cwd=root)
        mem = Path(rp(root / '.agent-memory'))
        account_two = self.base / 'account-two'
        self.assertNotEqual(claude_slug(rp(root)), claude_slug(str(root)))
        planted = {
            'real-only.md': self.legacy_dir(rp(root)),
            'logical-only.md': self.legacy_dir(str(root)),
            'acct2-only.md': self.legacy_dir(rp(root), account_two),
            'wt-only.md': self.legacy_dir(rp(wt)),
        }
        for name, directory in planted.items():
            write(directory / name, mem_file(name[:-3], 'From ' + name))
        ancestor = self.legacy_dir(rp(self.work))
        write(ancestor / 'ancestor-only.md', mem_file('ancestor-only', 'ancestor'))
        user_level = self.base / 'user-level-memory'
        write(user_level / 'user-level-only.md', mem_file('user-level-only', 'user level'))
        write(self.claude_cfg / 'settings.json', json.dumps({'autoMemoryDirectory': str(user_level)}))
        self.pmem('import', '--claude', '--config-dir', account_two, cwd=root)
        for name in planted:
            self.assertTrue((mem / name).is_file(), name)
        for name in ('ancestor-only.md', 'user-level-only.md'):
            self.assertFalse(os.path.lexists(str(mem / name)), name)
        self.assertEqual(os.listdir(str(user_level)), ['user-level-only.md'])

    def test_sub_directory_memories_are_listed_not_imported(self):
        root, mem = self.init_project(self.work / 'proj')
        real = rp(root)
        sub = os.path.join(real, 'tools')
        os.mkdir(sub)
        sibling = os.path.join(rp(self.work), 'proj-other')          # /a/proj vs /a/proj-other
        os.mkdir(sibling)
        gone = os.path.join(real, 'removed-dir')                   # no transcript left
        self.assertTrue(claude_slug(sibling).startswith(claude_slug(real) + '-'))
        for path, name in ((sub, 'sub-note'), (sibling, 'other-note'), (gone, 'gone-note')):
            write(self.legacy_dir(path) / (name + '.md'), mem_file(name, 'From ' + name))
        for path in (sub, sibling):
            write(self.legacy_dir(path).parent / 'transcript.jsonl', json.dumps(
                {'type': 'user', 'cwd': path, 'message': {'role': 'user', 'content': 'hi'}}) + '\n')
        out = self.pmem('import', '--claude', cwd=root).stdout
        candidates = [l for l in out.splitlines() if l.startswith('candidate (not imported)')]
        self.assertTrue(any(str(self.legacy_dir(sub)) in l and 'launched in ' + sub in l
                            for l in candidates), out)
        self.assertTrue(any(str(self.legacy_dir(gone)) in l for l in candidates), out)
        self.assertNotIn(claude_slug(sibling), out)
        for name in ('sub-note.md', 'other-note.md', 'gone-note.md'):
            self.assertFalse(os.path.lexists(str(mem / name)), name)

    def test_slugs_for_cjk_emoji_and_long_paths(self):
        long_rel = os.path.join('a' * 120, 'b' * 120)
        for i, rel in enumerate(('项目-😀 demo', long_rel)):
            with self.subTest(path=rel):
                root, mem = self.init_project(self.work / rel, '--no-history')
                name = 'legacy-note-%d.md' % i
                write(self.legacy_dir(rp(root)) / name, mem_file(name[:-3], 'legacy note'))
                self.pmem('import', '--claude', cwd=root)
                self.assertTrue((mem / name).is_file())
        self.assertGreater(len(claude_slug(rp(self.work / long_rel))), 200)

    def test_crlf_and_undecodable_source_index(self):
        root, mem = self.init_project(self.work / 'crlf', '--no-history')
        src = self.base / 'crlf-src'
        write(src / 'MEMORY.md', b'# Index\r\n\r\n- [Alpha](alpha.md) \xe2\x80\x94 alpha hook\r\n'
                                 b'note with a bad byte \xff here\r\n')
        alpha = mem_file('alpha', 'Alpha fact', 'alpha body').replace('\n', '\r\n').encode('utf-8')
        write(src / 'alpha.md', alpha)
        self.pmem('import', src, cwd=root)
        self.assertEqual((mem / 'alpha.md').read_bytes(), alpha)
        raw = (mem / 'MEMORY.md').read_bytes()
        self.assertNotIn(b'\r', raw)
        self.assertEqual(lines_linking(raw.decode('utf-8'), 'alpha.md'), ['- [Alpha](alpha.md) — alpha hook'])
        notes = sorted(mem.glob('imported-notes*.md'))
        self.assertEqual(len(notes), 1)
        self.assertIn(b'note with a bad byte', notes[0].read_bytes())

    def test_dry_run_writes_nothing(self):
        root, mem = self.init_project(self.work / 'dry')
        src = self.base / 'dry-src'
        write(src / 'MEMORY.md', LEGACY_INDEX)
        write(src / 'alpha.md', mem_file('alpha', 'Alpha fact'))
        write(src / 'beta.md', mem_file('beta', 'Beta fact'))
        before = (tree_state(root), self.mem_head(mem))
        result = self.pmem('import', '--dry-run', src, cwd=root)
        self.assertIn('alpha.md', result.out)
        self.assertEqual((tree_state(root), self.mem_head(mem)), before)
        self.assertFalse((mem / '.imported.json').exists())

    def test_import_file_becomes_a_notes_memory(self):
        root, mem = self.init_project(self.work / 'localnotes')
        local = write(root / 'CLAUDE.local.md', '# Local notes\n\nDev server port is 8080.\n')
        source = local.read_bytes()
        existing = set(os.listdir(str(mem)))
        self.pmem('import', '--file', local, cwd=root)
        new = sorted(set(os.listdir(str(mem))) - existing - {'.imported.json', '.pmem.lock'})
        self.assertEqual(len(new), 1, new)
        self.assertTrue(new[0].startswith('imported-notes') and new[0].endswith('.md'), new)
        self.assertIn('Dev server port is 8080.', read_text(mem / new[0]))
        self.assertEqual(local.read_bytes(), source)
        self.pmem('lint', cwd=root)
        before = (tree_state(root), self.mem_head(mem))
        self.pmem('import', '--file', local, cwd=root)
        self.assertEqual((tree_state(root), self.mem_head(mem)), before)

    def test_import_needs_a_memory_dir(self):
        outside = self.work / 'none'
        outside.mkdir()
        self.pmem('import', '--claude', cwd=outside, rc=1)
        self.assertFalse(os.path.lexists(str(outside / '.agent-memory')))


# =============================================================================================
# wire

class WireTests(Helpers):
    def wire(self, *args, rc=0):
        return self.pmem('wire', *args, cwd=self.home, rc=rc)

    @staticmethod
    def backups(path):
        path = Path(path)
        return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + '.bak-'))

    def test_insert_is_exact_and_idempotent(self):
        target = write(self.base / 'notes' / 'AGENTS.md', 'line1\nline2\n')
        self.wire(target)
        self.assertEqual(read_text(target), 'line1\nline2\n\n' + WIRE_BLOCK + '\n')
        backups = self.backups(target)
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r'^AGENTS\.md\.bak-\d{14}(-\d+)?$')
        self.assertEqual(backups[0].read_bytes(), b'line1\nline2\n')
        self.assertIn('ok', self.wire(target).stdout)
        self.assertEqual(len(self.backups(target)), 1)
        self.assertEqual(read_text(target), 'line1\nline2\n\n' + WIRE_BLOCK + '\n')

    def test_insert_then_remove_restores_the_original_bytes(self):
        originals = {
            'plain': b'line1\nline2\n',
            'no-final-newline': b'line1\nline2',
            'empty': b'',
            'trailing-blank': b'a\n\n',
            'crlf': b'a\r\nb\r\n',
            'crlf-no-final-newline': b'a\r\nb',
            'bom': b'\xef\xbb\xbf# Title\n',
            'bom-crlf': b'\xef\xbb\xbf# T\r\nx\r\n',
            'utf8': '中文 — ok\n'.encode('utf-8'),
        }
        for label, data in originals.items():
            with self.subTest(case=label):
                target = write(self.base / 'rt' / label / 'FILE.md', data)
                self.wire(target)
                wired = target.read_bytes()
                self.assertEqual(wired.count(BEGIN.encode()), 1)
                self.assertIn(WIRE_BLOCK.split('\n')[1].encode(), wired)
                if data:
                    stripped = data.rstrip(b'\r\n')
                    self.assertTrue(wired.startswith(stripped), wired[:80])
                self.wire(target, '--remove')
                self.assertEqual(target.read_bytes(), data)
        target = write(self.base / 'nfn' / 'FILE.md', b'line1\nline2')
        self.wire(target)
        self.assertTrue(target.read_bytes().startswith(b'line1\nline2\n\n' + WIRE_BLOCK.encode()))

    def test_eol_style_and_bom_are_preserved(self):
        crlf = write(self.base / 'eol' / 'crlf.md', b'a\r\nb\r\n')
        self.wire(crlf)
        self.assertEqual(crlf.read_bytes(),
                         b'a\r\nb\r\n\r\n' + WIRE_BLOCK.replace('\n', '\r\n').encode() + b'\r\n')
        bom = write(self.base / 'eol' / 'bom.md', b'\xef\xbb\xbf# T\n')
        self.wire(bom)
        self.assertEqual(bom.read_bytes(), b'\xef\xbb\xbf# T\n\n' + WIRE_BLOCK.encode() + b'\n')

    def test_replace_keeps_surrounding_lines_and_remove_takes_one_blank_line(self):
        old = BEGIN + '   \nold rule line\n' + END + '\t'
        target = write(self.base / 'replace.md', 'top\n\n' + old + '\nbottom\n')
        self.wire(target)
        self.assertEqual(read_text(target), 'top\n\n' + WIRE_BLOCK + '\nbottom\n')
        self.wire(target, '--remove')
        self.assertEqual(read_text(target), 'top\nbottom\n')
        old = BEGIN + '\nold rule line\n' + END
        crlf = write(self.base / 'replace-crlf.md', ('top\n\n' + old + '\nbottom\n').replace('\n', '\r\n'))
        self.wire(crlf)
        self.assertEqual(crlf.read_bytes(),
                         ('top\n\n' + WIRE_BLOCK + '\nbottom\n').replace('\n', '\r\n').encode())

    def test_malformed_markers_are_never_rewritten(self):
        cases = {
            'begin-only': 'keep 1\nkeep 2\n' + BEGIN + '\nkeep 4\n',
            'end-only': 'keep 1\n' + END + '\nkeep 3\n',
            'end-before-begin': 'keep\n' + END + '\n' + BEGIN + '\nkeep\n',
            'two-blocks': BEGIN + '\nx\n' + END + '\n' + BEGIN + '\ny\n' + END + '\n',
            'two-begins': BEGIN + '\n' + BEGIN + '\n' + END + '\n',
        }
        for label, text in cases.items():
            for extra in ((), ('--remove',)):
                with self.subTest(case=label, args=extra):
                    target = write(self.base / 'bad' / label / ('r' if extra else 'w') / 'FILE.md', text)
                    self.wire(target, *extra, rc=1)
                    self.assertEqual(read_text(target), text)
                    self.assertEqual(self.backups(target), [])
        target = write(self.base / 'bad' / 'numbers.md', cases['begin-only'])
        self.assertRegex(self.wire(target, rc=1).out, r'\b3\b')

    def test_writes_through_symlinks(self):
        real = write(self.base / 'real' / 'AGENTS.md', 'real rules\n')
        link = self.base / 'linked' / 'AGENTS.md'
        link.parent.mkdir()
        os.symlink(str(real), str(link))
        self.wire(link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(read_text(real), 'real rules\n\n' + WIRE_BLOCK + '\n')
        self.wire(link, '--remove')
        self.assertTrue(link.is_symlink())
        self.assertEqual(read_text(real), 'real rules\n')

    def test_backups_never_clobber_existing_files(self):
        target = write(self.base / 'bk' / 'AGENTS.md', 'original\n')
        now = time.time()
        planted = set()
        for delta in range(-2, 15):
            for convert in (time.localtime, time.gmtime):
                path = target.parent / ('AGENTS.md.bak-' + time.strftime('%Y%m%d%H%M%S', convert(now + delta)))
                if path not in planted:
                    write(path, 'planted\n')
                    planted.add(path)
        self.wire(target)
        mine = [p for p in self.backups(target) if p.read_bytes() == b'original\n']
        self.assertEqual(len(mine), 1)
        self.assertRegex(mine[0].name, r'\.bak-\d{14}-\d+$')
        for path in planted:
            self.assertEqual(path.read_bytes(), b'planted\n')

    def test_I_L15_backups_keep_the_original_mode(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                target = write(self.base / ('mode-%o' % mode) / 'CLAUDE.md', 'secret prefs\r\nline2')
                os.chmod(str(target), mode)
                link = self.base / ('mode-link-%o' % mode) / 'CLAUDE.md'
                link.parent.mkdir()
                os.symlink(str(target), str(link))
                self.wire(link)
                backups = self.backups(link)
                self.assertEqual(len(backups), 1)
                self.assertEqual(stat.S_IMODE(os.stat(str(backups[0])).st_mode), mode)
                self.assertEqual(backups[0].read_bytes(), b'secret prefs\r\nline2')
                self.assertEqual(stat.S_IMODE(os.stat(str(target)).st_mode), mode)

    def test_new_file_gets_no_backup(self):
        target = self.base / 'fresh' / 'AGENTS.md'
        target.parent.mkdir()
        self.wire(target)
        self.assertEqual(os.listdir(str(target.parent)), ['AGENTS.md'])
        self.assertIn(WIRE_BLOCK, read_text(target))
        self.assertEqual(read_text(target).count(BEGIN), 1)

    def test_codex_and_claude_targets(self):
        agents = write(self.codex_home / 'AGENTS.md', '# Codex rules\n')
        self.wire('codex')
        self.assertEqual(read_text(agents), '# Codex rules\n\n' + WIRE_BLOCK + '\n')
        self.wire('codex', '--remove')
        self.assertEqual(read_text(agents), '# Codex rules\n')
        self.wire('claude')
        self.assertIn(WIRE_BLOCK, read_text(self.claude_cfg / 'CLAUDE.md'))
        default = (self.claude_cfg / 'CLAUDE.md').read_bytes()
        first, second = self.base / 'cfg-a', self.base / 'cfg-b'
        first.mkdir()
        second.mkdir()
        self.wire('claude', '--config-dir', first, '--config-dir', second)
        for directory in (first, second):
            self.assertIn(WIRE_BLOCK, read_text(directory / 'CLAUDE.md'))
        self.assertEqual((self.claude_cfg / 'CLAUDE.md').read_bytes(), default)
        self.assertFalse(os.path.lexists(str(self.home / '.claude')))
        env = dict(self.env)
        del env['CODEX_HOME']
        (self.home / '.codex').mkdir()
        self.pmem('wire', 'codex', cwd=self.home, env=env)
        self.assertIn(WIRE_BLOCK, read_text(self.home / '.codex' / 'AGENTS.md'))


# =============================================================================================
# hooks

@GIT_REQUIRED
class HookTests(Helpers):
    def test_hooks_are_silent_outside_projects(self):
        outside = self.work / 'outside'
        outside.mkdir()
        before = tree_state(self.base)
        payloads = {
            'session json': self.session_payload(outside),
            'stop json': self.stop_payload(outside),
            'empty': '',
            'invalid': '{not json',
            'array': '[1, 2]',
            'missing cwd': {'hook_event_name': 'Stop'},
            'nonexistent cwd': self.stop_payload(self.work / 'does-not-exist'),
            'home': self.session_payload(self.home),
        }
        for which, extra in (('session-start', ()), ('session-start', ('--with-index',)),
                             ('checkpoint', ())):
            for label, payload in payloads.items():
                with self.subTest(hook=which, extra=extra, payload=label):
                    self.assertEqual(self.hook(which, payload, cwd=outside, extra=extra).stdout, '')
        self.assertEqual(tree_state(self.base), before)

    def test_session_start_reports_handoff_checkpoint_and_index(self):
        root, mem = self.init_project(self.work / 'hooked')
        self.add(root, 'index-probe-mem', 'Index probe memory')
        handoff = mem / 'HANDOFF.md'
        self.set_goal(handoff, 'GOAL-PROBE-17')
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=self.home))
        self.assertIn('GOAL-PROBE-17', context)                 # memory resolved from input cwd
        self.assertIn(str(mem), context)
        self.assertNotIn('index-probe-mem', context)
        self.assertLessEqual(len(context), 8000)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root,
                                            extra=('--with-index',)))
        self.assertIn('index-probe-mem', context)
        self.set_modified(handoff, '2020-01-01T00:00:00Z', mtime=1577836800)
        self.hook('checkpoint', self.stop_payload(root, message='CKPT-PROBE-42'), cwd=root)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertIn('CKPT-PROBE-42', context)
        self.assertIn('Auto checkpoint', context)
        self.set_modified(handoff, '2099-01-01T00:00:00Z', mtime=4070908800)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertNotIn('CKPT-PROBE-42', context)
        self.pmem('handoff', 'path', '--key', 'zebra-stream', cwd=root)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertIn('zebra-stream', context)

    def test_session_start_truncates_and_snapshots(self):
        root, mem = self.init_project(self.work / 'big')
        self.set_goal(mem / 'HANDOFF.md', 'Z' * 20000)
        context = self.context_of(self.hook('session-start', self.session_payload(
            root, source='resume', sid='feedc0de12345678'), cwd=root))
        self.assertLessEqual(len(context), 8000)
        self.wait_snapshot(mem, 'session-start resume feedc0de')   # detached background snapshot
        self.assertEqual(self.mem_status(mem), '')
        full = self.pmem('context', cwd=root).stdout
        self.assertIn('Z' * 20000, full)
        self.assertIn('# Project memory', full)

    def test_checkpoint_writes_only_the_checkpoint(self):
        root, mem = self.init_project(self.work / 'ckpt', git=True)
        protected = {name: (mem / name).read_bytes() for name in ('MEMORY.md', 'HANDOFF.md')}
        head = self.mem_head(mem)
        message = 'H' * 1000 + 'M' * 1000 + 'END-PROBE'
        self.hook('checkpoint', self.stop_payload(root, message=message, session_id='sess-abc'), cwd=root)
        checkpoint = read_text(mem / '.checkpoints' / 'main.md')
        self.assertTrue(checkpoint.startswith('# Auto checkpoint (main)'), checkpoint[:200])
        self.assertRegex(checkpoint, r'(?m)^- event:\s*Stop\s*$')
        self.assertRegex(checkpoint, r'(?m)^- agent:\s*claude-code\s*$')
        self.assertRegex(checkpoint, r'(?m)^- session_id:\s*sess-abc\s*$')
        self.assertRegex(checkpoint, r'(?m)^- branch:\s*main\s*$')
        self.assertIn('## Last assistant message (tail)', checkpoint)
        self.assertIn('END-PROBE', checkpoint)
        self.assertNotIn('H' * 500, checkpoint)
        for name, data in protected.items():
            self.assertEqual((mem / name).read_bytes(), data, name)
        self.assertEqual(self.mem_head(mem), head)
        self.assertEqual(self.mem_status(mem), '')            # .checkpoints/ is ignored
        self.assertNoTempFiles(mem)
        self.hook('checkpoint', self.stop_payload(root, event='StopFailure', message='cut off',
                                                  error='rate_limit', error_details='usage limit'),
                  cwd=root)
        checkpoint = read_text(mem / '.checkpoints' / 'main.md')
        self.assertRegex(checkpoint, r'(?m)^- event:\s*StopFailure\s*$')
        self.assertRegex(checkpoint, r'(?m)^- error:.*rate_limit')
        self.hook('checkpoint', {'session_id': 's', 'cwd': str(root), 'hook_event_name': 'PreCompact',
                                 'trigger': 'auto', 'transcript_path': ''}, cwd=root)
        self.assertRegex(read_text(mem / '.checkpoints' / 'main.md'), r'(?m)^- event:\s*PreCompact\s*$')
        self.assertEqual(self.mem_head(mem), head)

    def test_hook_does_not_wait_for_stdin_eof(self):
        # Claude Code may keep the hook's stdin open; the hook must not wait for EOF.
        root, mem = self.init_project(self.work / 'open-stdin', '--no-history')
        env = dict(self.env, PWD=str(root))
        proc = subprocess.Popen([sys.executable, str(PMEM), 'hook', 'checkpoint'], cwd=str(root),
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        try:
            proc.stdin.write(json.dumps(self.stop_payload(root, message='OPEN-STDIN-PROBE')).encode())
            proc.stdin.flush()
            started = time.time()
            proc.wait(timeout=TIMEOUT)
            self.assertLess(time.time() - started, 10)
        finally:
            if proc.poll() is None:
                proc.kill()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()
            proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn('OPEN-STDIN-PROBE', read_text(mem / '.checkpoints' / 'main.md'))

    def test_stop_hook_active_writes_nothing(self):
        root, mem = self.init_project(self.work / 'active')
        before = tree_state(mem)
        self.hook('checkpoint', self.stop_payload(root, active=True), cwd=root)
        self.assertEqual(tree_state(mem), before)
        self.assertFalse((mem / '.checkpoints' / 'main.md').exists())

    def test_hooks_never_fail_on_a_broken_memory_dir(self):
        root, mem = self.init_project(self.work / 'broken', '--no-history')
        shutil.rmtree(str(mem / '.checkpoints'))
        write(mem / '.checkpoints', 'now a file\n')
        self.hook('checkpoint', self.stop_payload(root), cwd=root)
        self.assertEqual(read_text(mem / '.checkpoints'), 'now a file\n')
        (mem / 'HANDOFF.md').unlink()
        (mem / 'HANDOFF.md').mkdir()
        write(mem / 'MEMORY.md', b'# idx\n\xff\xfe bad bytes\n')
        for extra in ((), ('--with-index',)):
            with self.subTest(extra=extra):
                result = self.hook('session-start', self.session_payload(root), cwd=root, extra=extra)
                if result.stdout.strip():
                    json.loads(result.stdout)


# =============================================================================================
# handoff / context / status

@GIT_REQUIRED
class HandoffContextStatusTests(Helpers):
    def test_handoff_path_key_show_and_list(self):
        root, mem = self.init_project(self.work / 'ho', git=True)
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=root).stdout.strip()), str(mem / 'HANDOFF.md'))
        keyed = mem / 'handoffs' / 'data-prep.md'
        result = self.pmem('handoff', 'path', '--key', 'data-prep', cwd=root)
        self.assertEqual(rp(result.stdout.strip()), str(keyed))
        text = read_text(keyed)
        self.assertEqual(fm_get(text, 'name'), 'handoff-data-prep')
        self.assertEqual(fm_get(text, 'by'), 'pmem')
        self.assertEqual(fm_get(text, 'workstream'), 'data-prep')
        self.set_goal(keyed, 'KEYED-GOAL')
        self.pmem('handoff', 'path', '--key', 'data-prep', cwd=root)
        self.assertIn('KEYED-GOAL', read_text(keyed))                   # never overwritten
        self.set_goal(mem / 'HANDOFF.md', 'MAIN-GOAL')
        self.assertIn('MAIN-GOAL', self.pmem('handoff', cwd=root).stdout)
        self.assertIn('MAIN-GOAL', self.pmem('handoff', 'show', cwd=root).stdout)
        self.assertIn('KEYED-GOAL', self.pmem('handoff', 'show', '--key', 'data-prep', cwd=root).stdout)
        self.assertIn('data-prep', self.pmem('handoff', 'list', cwd=root).stdout)
        handoffs_rel = os.path.join(os.path.relpath(str(root), str(self.base)), '.agent-memory', 'handoffs')
        for bad in ('../evil', 'a/b', '../../escape'):
            with self.subTest(key=bad):
                before = tree_state(self.base)
                self.pmem('handoff', 'path', '--key', bad, cwd=root, rc=None)
                after = tree_state(self.base)
                changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                for path in changed:
                    self.assertEqual(os.path.dirname(path), handoffs_rel, changed)

    def test_context(self):
        outside = self.work / 'none'
        outside.mkdir()
        self.pmem('context', cwd=outside, rc=1)
        root, mem = self.init_project(self.work / 'ctx', '--no-history')
        self.set_goal(mem / 'HANDOFF.md', 'CTX-GOAL')
        self.add(root, 'ctx-mem', 'Context memory')
        out = self.pmem('context', cwd=root).stdout
        self.assertIn('CTX-GOAL', out)
        self.assertIn('](ctx-mem.md)', out)
        self.assertLess(out.index('CTX-GOAL'), out.index('](ctx-mem.md)'))
        keyed = Path(self.pmem('handoff', 'path', '--key', 'side-quest', cwd=root).stdout.strip())
        self.set_goal(keyed, 'SIDE-GOAL')
        out = self.pmem('context', '--key', 'side-quest', cwd=root).stdout
        self.assertIn('SIDE-GOAL', out)
        self.assertIn('](ctx-mem.md)', out)

    def test_status_modes_and_wiring_errors(self):
        repo = self.make_repo(self.work / 'st')
        self.pmem('init', cwd=repo)
        mem = rp(repo / '.agent-memory')
        result = self.pmem('status', cwd=repo)
        self.assertIn(mem, result.out)
        self.assertIn('private-excluded', result.out)
        self.assertIsInstance(json.loads(self.pmem('status', '--json', cwd=repo).stdout), dict)
        data = self.settings(repo)
        data['autoMemoryDirectory'] = str(self.base / 'somewhere-else')
        write(self.settings_path(repo), json.dumps(data, indent=2))
        self.pmem('status', cwd=repo, rc=1)
        user_settings = self.claude_cfg / 'settings.json'
        write(user_settings, json.dumps({'autoMemoryEnabled': False}))
        self.assertIn('autoMemoryEnabled', self.pmem('status', cwd=repo, rc=None).out)
        local = self.settings(repo)
        local['autoMemoryEnabled'] = True                        # local settings win over user
        write(self.settings_path(repo), json.dumps(local, indent=2))
        self.assertNotIn('autoMemoryEnabled', self.pmem('status', cwd=repo, rc=None).out)
        user_settings.unlink()
        plain, _ = self.init_project(self.work / 'plain-status', '--no-history')
        self.assertIn('no-git', self.pmem('status', cwd=plain).out)
        (plain / 'sub').mkdir()
        self.assertIn('--add-dir', self.pmem('status', cwd=plain / 'sub').out)

    def test_checkpoint_in_full_only_after_a_failure_or_a_lagging_handoff(self):
        # The Stop hook fires after every turn, so a checkpoint is nearly always a bit newer than
        # the handoff; only an error stop or a handoff > 10 min behind deserves the full section.
        root, mem = self.init_project(self.work / 'ck', '--no-history')
        handoff = mem / 'HANDOFF.md'
        transcript = str(self.base / 'cfg-x' / 'projects' / 'p' / 'sess-1234.jsonl')

        def handoff_age(seconds):
            when = time.time() - seconds
            self.set_modified(handoff, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when)),
                              mtime=when)

        def views():
            return {
                'session-start': self.context_of(self.hook('session-start', self.session_payload(root),
                                                           cwd=root)),
                'context': self.pmem('context', cwd=root).stdout,
                'handoff show': self.pmem('handoff', 'show', cwd=root).stdout,
            }

        handoff_age(300)
        self.hook('checkpoint', self.stop_payload(root, message='TAIL-PROBE-1',
                                                  transcript_path=transcript), cwd=root)
        for age in (300, -3600):                     # checkpoint 5 min newer / older than the handoff
            handoff_age(age)
            for view, out in views().items():
                with self.subTest(case='normal', age=age, view=view):
                    lines = [l for l in out.splitlines() if l.startswith('Last session ended normally at ')]
                    self.assertEqual(len(lines), 1, out)
                    self.assertTrue(lines[0].endswith(
                        '(session sess-123, transcript %s); handoff is current.' % transcript), lines[0])
                    self.assertNotIn('TAIL-PROBE-1', out)
                    self.assertNotIn('Auto checkpoint', out)
        handoff_age(1200)                            # the handoff lags the checkpoint by 20 min
        for view, out in views().items():
            with self.subTest(case='stale', view=view):
                self.assertIn('## Auto checkpoint newer than the handoff', out)
                self.assertIn('TAIL-PROBE-1', out)
                self.assertNotIn('ended normally', out)
        handoff_age(60)
        self.hook('checkpoint', self.stop_payload(root, event='StopFailure', message='TAIL-PROBE-2',
                                                  error='rate_limit', error_details='usage limit'),
                  cwd=root)
        for view, out in views().items():
            with self.subTest(case='failure', view=view):
                heads = [l for l in out.splitlines() if l.startswith('## Auto checkpoint')]
                self.assertEqual(len(heads), 1, out)
                self.assertIn('rate_limit', heads[0])
                self.assertIn('TAIL-PROBE-2', out)
                self.assertNotIn('ended normally', out)
        self.assertIn('stopped on rate_limit', self.pmem('status', cwd=root, rc=None).stdout)
        data = json.loads(self.pmem('status', '--json', cwd=root, rc=None).stdout)
        self.assertEqual([h['checkpoint'] for h in data['handoffs']], ['failure'])


# =============================================================================================
# sessions

class SessionsTests(Helpers):
    def setUp(self):
        super().setUp()
        self.root, self.mem = self.init_project(self.work / 'sess', '--no-history')
        real = rp(self.root)
        self.slug = claude_slug(real)
        projects = self.claude_cfg / 'projects' / self.slug
        now = time.time()
        self.claude_new = self.write_claude(projects / '22222222-new.jsonl', real, now - 300)
        self.claude_old = self.write_claude(projects / '11111111-old.jsonl', real, 1000000000)
        day = self.codex_home / 'sessions' / '2026' / '11' / '23'
        self.codex_new = self.write_codex(day / 'rollout-2026-11-23T10-00-00-new.jsonl',
                                          os.path.join(real, 'sub'), now - 100)
        self.codex_old = self.write_codex(day / 'rollout-2026-11-23T09-00-00-old.jsonl', real, 1000000000)
        self.codex_other = self.write_codex(day / 'rollout-2026-11-23T11-00-00-other.jsonl',
                                            rp(self.work), now - 50, user='OTHER-PROJECT-PROBE')

    @staticmethod
    def dump(path, records, mtime):
        write(path, ''.join(json.dumps(r) + '\n' for r in records))
        os.utime(str(path), (mtime, mtime))
        return path

    def write_claude(self, path, cwd, mtime):
        records = [
            {'type': 'user', 'cwd': cwd, 'sessionId': path.stem,
             'message': {'role': 'user', 'content': 'hello CLAUDE-USER-PROBE'}},
            {'type': 'assistant', 'cwd': cwd, 'message': {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'THINK-PROBE'},
                {'type': 'text', 'text': 'CLAUDE-ASSIST-PROBE'},
                {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'TOOL-INPUT-PROBE'}}]}},
            {'type': 'user', 'cwd': cwd, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'TOOL-RESULT-PROBE'}]}},
            {'type': 'assistant', 'cwd': cwd, 'message': {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'L' * 2000}]}},
        ]
        return self.dump(path, records, mtime)

    def write_codex(self, path, cwd, mtime, user='CODEX-USER-PROBE'):
        records = [
            {'timestamp': '2026-11-23T10:00:00.000Z', 'type': 'session_meta',
             'payload': {'id': path.stem, 'cwd': cwd, 'cli_version': '0.164.0'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': [
                {'type': 'input_text', 'text': 'DEV-PROBE'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': user}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'shell',
                                                  'arguments': '{"cmd": "FUNC-PROBE"}', 'call_id': 'c1'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'CODEX-ASSIST-PROBE'}]}},
        ]
        return self.dump(path, records, mtime)

    def test_lists_project_transcripts_newest_first(self):
        out = self.pmem('sessions', cwd=self.root).stdout
        for path in (self.claude_new, self.claude_old, self.codex_new, self.codex_old):
            self.assertIn(path.name, out)
        self.assertNotIn(self.codex_other.name, out)
        self.assertLess(out.index(self.claude_new.name), out.index(self.claude_old.name))
        self.assertLess(out.index(self.codex_new.name), out.index(self.codex_old.name))
        out = self.pmem('sessions', '--since', '2020-01-01T00:00:00Z', cwd=self.root).stdout
        self.assertIn(self.claude_new.name, out)
        self.assertIn(self.codex_new.name, out)
        self.assertNotIn(self.claude_old.name, out)
        self.assertNotIn(self.codex_old.name, out)

    def test_tail_shows_only_text_messages(self):
        out = self.pmem('sessions', '--tail', '10', cwd=self.root).stdout
        for probe in ('CLAUDE-USER-PROBE', 'CLAUDE-ASSIST-PROBE', 'CODEX-USER-PROBE', 'CODEX-ASSIST-PROBE'):
            self.assertIn(probe, out)
        for probe in ('THINK-PROBE', 'TOOL-INPUT-PROBE', 'TOOL-RESULT-PROBE', 'DEV-PROBE', 'FUNC-PROBE',
                      'OTHER-PROJECT-PROBE'):
            self.assertNotIn(probe, out)
        self.assertIn('L' * 100, out)
        self.assertNotIn('L' * 501, out)

    def test_since_handoff_and_extra_config_dirs(self):
        self.set_modified(self.mem / 'HANDOFF.md', '2025-06-01T00:00:00Z', mtime=1748736000)
        out = self.pmem('sessions', '--since-handoff', cwd=self.root).stdout
        self.assertIn(self.claude_new.name, out)
        self.assertNotIn(self.claude_old.name, out)
        # a config dir is derived from the transcript path recorded in a checkpoint
        other = self.base / 'acct-3' / 'projects' / self.slug / '33333333-acct.jsonl'
        self.write_claude(other, rp(self.root), time.time() - 10)
        self.hook('checkpoint', self.stop_payload(self.root, transcript_path=str(other)), cwd=self.root)
        self.assertIn(other.name, self.pmem('sessions', cwd=self.root).stdout)
        explicit = self.base / 'acct-4' / 'projects' / self.slug / '44444444-acct.jsonl'
        self.write_claude(explicit, rp(self.root), time.time() - 20)
        out = self.pmem('sessions', '--config-dir', self.base / 'acct-4', cwd=self.root).stdout
        self.assertIn(explicit.name, out)

    def test_sub_directory_launches_count_but_look_alike_siblings_do_not(self):
        real = rp(self.root)
        deep = os.path.join(real, 'pkg', 'deep')
        os.makedirs(deep)
        sibling = os.path.join(rp(self.work), 'sess-other')          # /a/proj vs /a/proj-other
        os.mkdir(sibling)
        self.assertTrue(claude_slug(sibling).startswith(self.slug + '-'))
        logical = os.path.join(str(self.root), 'pkg')                # launched via the symlinked path
        self.assertNotEqual(claude_slug(logical), claude_slug(os.path.join(real, 'pkg')))
        projects = self.claude_cfg / 'projects'
        now = time.time()
        in_deep = self.write_claude(projects / claude_slug(deep) / 'aaaaaaaa-deep.jsonl', deep, now - 30)
        in_logical = self.write_claude(projects / claude_slug(logical) / 'bbbbbbbb-logical.jsonl',
                                       logical, now - 40)
        in_sibling = self.write_claude(projects / claude_slug(sibling) / 'cccccccc-sibling.jsonl',
                                       sibling, now - 20)
        no_cwd = self.dump(projects / claude_slug(os.path.join(real, 'pkg2')) / 'dddddddd-nocwd.jsonl',
                           [{'type': 'summary', 'summary': 'no cwd here'}], now - 10)
        out = self.pmem('sessions', '--limit', '20', cwd=self.root).stdout
        for path in (in_deep, in_logical, self.claude_new, self.claude_old):
            self.assertIn(path.name, out)
        for path in (in_sibling, no_cwd):
            self.assertNotIn(path.name, out)
        self.assertLess(out.index(in_deep.name), out.index(in_logical.name))

    def test_T_malformed_codex_lines_are_skipped(self):
        with open(str(self.codex_new), 'a') as handle:
            handle.write('"response_item"\n[1, 2]\n{"type": "response_item", "payload": 3}\n')
        result = self.pmem('sessions', '--tail', '5', cwd=self.root)
        self.assertNotIn('Traceback', result.out)
        self.assertIn('CODEX-ASSIST-PROBE', result.stdout)


# =============================================================================================
# Spec §9 amendments: one regression test per finding of the adversarial verification
# (finding ids A..W and L<n> as in the spec; each test replays the original repro).

IS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0
NOT_ROOT = unittest.skipIf(IS_ROOT, 'file permissions do not stop root')


def local_iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(epoch))


def utc_iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


class AmendmentHelpers(Helpers):
    def assertNoTraceback(self, result):
        self.assertNotIn('Traceback', result.out, result.out)

    def chmod_back(self, path, mode=0o755):
        self.addCleanup(lambda: os.path.exists(str(path)) and os.chmod(str(path), mode))

    def set_checkpoint_time(self, path, epoch):
        text = read_text(path)
        new, count = re.subn(r'(?m)^- time: .*$', '- time: ' + local_iso(epoch), text, count=1)
        self.assertEqual(count, 1, text)
        write(path, new)

    def handoff_at(self, handoff, epoch):
        """Frontmatter `modified` and the file mtime both at ``epoch``."""
        self.set_modified(handoff, utc_iso(epoch), mtime=epoch)


@GIT_REQUIRED
class SafetyAmendmentTests(AmendmentHelpers):
    def test_A_broken_private_history_never_commits_to_the_project_repo(self):
        for case in ('empty .git dir', 'HEAD removed'):
            with self.subTest(case=case):
                repo = self.make_repo(self.work / case.replace(' ', '-'), {'code.py': 'v1\n'})
                if case == 'empty .git dir':
                    self.pmem('init', '--no-history', cwd=repo)
                    (repo / '.agent-memory' / '.git').mkdir()
                else:
                    self.pmem('init', cwd=repo)
                    (repo / '.agent-memory' / '.git' / 'HEAD').unlink()
                head = self.git_out(repo, 'rev-parse', 'HEAD')
                write(repo / 'code.py', 'v1\nWIP\n')
                write(repo / '.env.local', 'secret\n')
                self.hook('session-start', self.session_payload(repo), cwd=repo)
                result = self.add(repo, 'note', 'A note')
                self.assertIn('private history broken', result.stderr)
                self.assertEqual(self.pmem('snapshot', cwd=repo, rc=1).returncode, 1)
                self.assertIn('broken', self.pmem('diff', cwd=repo, rc=1).out)
                status = self.pmem('status', cwd=repo).out
                self.assertRegex(status, r'(?m)^history:\s+broken')
                self.assertIn('private history broken', self.pmem('lint', cwd=repo).out)
                self.pmem('init', cwd=repo)                     # warns, never commits
                time.sleep(1.5)                                 # the background snapshot
                self.assertEqual(self.git_out(repo, 'rev-parse', 'HEAD'), head)
                authors = self.git_out(repo, 'log', '--all', '--format=%an').split('\n')
                self.assertNotIn('pmem', authors)
                dirty = self.git_out(repo, 'status', '--porcelain')
                self.assertIn('code.py', dirty)
                self.assertIn('.env.local', dirty)

    def test_B_memory_dir_resolving_to_home_or_the_project_is_refused(self):
        write(self.home / '.gitignore', '*.log\n')
        write(self.home / '.ssh' / 'id_x', 'KEY\n')
        for name, target in (('to-home', os.path.join('..', '..')), ('to-self', '.')):
            with self.subTest(case=name):
                repo = self.make_repo(self.home / 'src' / name)
                os.symlink(target, str(repo / '.agent-memory'))
                before = tree_state(self.home)
                result = self.pmem('init', cwd=repo, rc=2)
                self.assertIn('refusing the memory dir', result.out)
                self.pmem('init', '--force', cwd=repo, rc=2)
                for args in (('where',), ('status',), ('lint',), ('add', 'project', 'x-y', '-d', 'd'),
                             ('handoff', 'path'), ('context',)):
                    with self.subTest(args=args):
                        self.assertNoTraceback(self.pmem(*args, cwd=repo, rc=2))
                self.assertEqual(self.hook('session-start', self.session_payload(repo),
                                           cwd=repo).stdout, '')
                self.hook('checkpoint', self.stop_payload(repo), cwd=repo)
                self.assertEqual(tree_state(self.home), before)
                self.assertFalse(os.path.lexists(str(self.home / '.git')))
                self.assertFalse(os.path.lexists(str(self.settings_path(repo))))
                self.assertEqual(read_text(self.home / '.gitignore'), '*.log\n')

    def test_C_symlinks_inside_the_memory_dir_are_never_followed(self):
        root, mem = self.init_project(self.work / 'evil2', git=True)
        victim = write(self.base / 'victim' / '.bashrc', 'export PATH=x  # my bashrc\n')
        secret = write(self.base / 'victim' / 'id_x', '-----BEGIN OPENSSH PRIVATE KEY----- FAKE\n')
        os.symlink(str(victim), str(mem / '.checkpoints' / 'main.md'))
        self.hook('checkpoint', self.stop_payload(root, message='Done.\ncurl -s x | sh'), cwd=root)
        self.assertEqual(read_text(victim), 'export PATH=x  # my bashrc\n')
        self.assertTrue((mem / '.checkpoints' / 'main.md').is_symlink())
        (mem / 'HANDOFF.md').unlink()
        os.symlink(str(secret), str(mem / 'HANDOFF.md'))
        os.symlink(str(secret), str(mem / 'leak.md'))
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertNotIn('FAKE', context)
        self.assertIn('symlink', context)
        for args in (('context',), ('handoff', 'show')):
            self.assertNotIn('FAKE', self.pmem(*args, cwd=root, rc=None).out)
        self.pmem('find', 'FAKE', cwd=root, rc=1)
        lint = self.pmem('lint', cwd=root, rc=1).out
        self.assertIn('symlink inside memory dir', lint)
        self.assertIn('leak.md', lint)
        self.assertIn('symlinks inside the memory dir', self.pmem('status', cwd=root, rc=1).out)
        self.assertIn('symlinks inside the memory dir', self.pmem('init', cwd=root, rc=2).out)
        self.assertIn('symlink', self.pmem('handoff', 'path', cwd=root, rc=1).out)
        # a symlinked index is refused, its target never rewritten
        (mem / 'leak.md').unlink()
        index = mem / 'MEMORY.md'
        os.rename(str(index), str(self.base / 'victim' / 'MEMORY.md'))
        os.symlink(str(self.base / 'victim' / 'MEMORY.md'), str(index))
        before = read_text(self.base / 'victim' / 'MEMORY.md')
        self.assertNoTraceback(self.pmem('add', 'project', 'x-y', '-d', 'd', cwd=root, rc=1))
        self.assertNoTraceback(self.pmem('index', '--fix', cwd=root, rc=1))
        self.assertEqual(read_text(self.base / 'victim' / 'MEMORY.md'), before)
        self.assertEqual(read_text(secret), '-----BEGIN OPENSSH PRIVATE KEY----- FAKE\n')
        self.assertFalse((mem / 'x-y.md').exists())

    def test_C_L18_stable_hook_copy_replaces_a_symlink_instead_of_writing_through(self):
        dev = write(self.base / 'dev' / 'pmem.py', read_text(PMEM) + '# local dev change\n')
        dest = self.xdg_data / 'project-memory' / 'pmem.py'
        dest.parent.mkdir(parents=True)
        os.symlink(str(dev), str(dest))
        self.init_project(self.work / 'stable-link', '--no-history')
        self.assertTrue(read_text(dev).endswith('# local dev change\n'))
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_bytes(), PMEM.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(str(dest)).st_mode), 0o755)

    def test_D_claude_root_at_home_is_refused_and_reported(self):
        self.make_repo(self.home, {'.bashrc': '# rc\n'})
        settings = write(self.home / '.claude' / 'settings.local.json', '{"theme": "dark"}\n')
        proj = self.home / 'notes' / 'proj'
        proj.mkdir(parents=True)
        result = self.pmem('init', '--here', cwd=proj, rc=2)
        self.assertIn('home directory', result.out)
        self.assertEqual(read_text(settings), '{"theme": "dark"}\n')
        self.assertFalse(os.path.lexists(str(proj / '.agent-memory')))
        self.pmem('init', '--here', '--no-claude', '--no-history', cwd=proj)
        self.assertEqual(read_text(settings), '{"theme": "dark"}\n')
        self.assertIn('home directory or above', self.pmem('status', cwd=proj).out)
        context = self.context_of(self.hook('session-start', self.session_payload(proj), cwd=proj))
        self.assertIn('home directory or above', context)
        self.pmem('init', '--here', '--force', '--no-history', cwd=proj)   # explicit override
        self.assertIn('autoMemoryDirectory', read_text(settings))

    def test_E_memory_tracked_in_any_ref_is_reported(self):
        repo = self.make_repo(self.work / 'refs')
        self.pmem('init', cwd=repo)
        mem = Path(rp(repo / '.agent-memory'))
        self.add(repo, 'mine', 'B private')
        index_before = self.index_text(mem)
        self.assertNotIn('tracks', self.pmem('status', cwd=repo).out)
        mate = self.make_repo(self.base / 'teammate')
        write(mate / '.agent-memory' / 'MEMORY.md', '# team memory\n')
        self.git(mate, 'add', '-f', '.agent-memory')
        self.git(mate, 'commit', '-q', '-m', 'track memory')
        self.git(repo, 'fetch', '-q', str(mate), 'main:refs/remotes/mate/main')
        hazard = 'tracks .agent-memory/ while this memory is private'
        status = self.pmem('status', cwd=repo).out
        self.assertIn(hazard, status)
        self.assertIn('pmem restore', status)
        lint = self.pmem('lint', cwd=repo).out
        self.assertIn(hazard, lint)
        self.assertEqual(self.lint_rc(repo, '--strict'), 1)
        self.assertIn(hazard, self.pmem('init', cwd=repo).out)
        self.assertIn(hazard, self.context_of(self.hook('session-start', self.session_payload(repo),
                                                         cwd=repo)))
        self.assertEqual(self.index_text(mem), index_before)

    def test_F_unreadable_index_is_never_replaced(self):
        root, mem = self.init_project(self.work / 'unreadable')
        for i in (1, 2):
            self.add(root, 'keep%d' % i, 'valuable %d' % i)
        self.append_index(mem, '- hand-written note: deploy key lives in vault path X')
        original = (mem / 'MEMORY.md').read_bytes()
        src = self.base / 'unreadable-src'
        write(src / 'imp.md', mem_file('imp', 'imported'))
        commands = (('add', 'project', 'newone', '-d', 'new', '--body', 'b'), ('index', '--fix'),
                    ('archive', 'keep1'), ('import', src), ('restore', 'keep2', '--force'))
        aside = self.base / 'index-aside'
        os.rename(str(mem / 'MEMORY.md'), str(aside))
        (mem / 'MEMORY.md').mkdir()                             # IsADirectoryError
        for args in commands:
            with self.subTest(case='directory', args=args):
                self.assertNoTraceback(self.pmem(*args, cwd=root, rc=1))
        (mem / 'MEMORY.md').rmdir()
        os.rename(str(aside), str(mem / 'MEMORY.md'))
        if not IS_ROOT:
            os.chmod(str(mem / 'MEMORY.md'), 0)
            self.chmod_back(mem / 'MEMORY.md', 0o644)
            for args in commands:
                with self.subTest(case='mode 000', args=args):
                    self.assertNoTraceback(self.pmem(*args, cwd=root, rc=1))
            os.chmod(str(mem / 'MEMORY.md'), 0o644)
        self.assertEqual((mem / 'MEMORY.md').read_bytes(), original)
        self.assertTrue((mem / 'keep1.md').is_file())
        self.assertFalse((mem / 'newone.md').exists())
        self.assertFalse((mem / 'imp.md').exists())

    @NOT_ROOT
    def test_F_os_errors_print_a_message_not_a_traceback(self):
        root, mem = self.init_project(self.work / 'readonly')
        os.chmod(str(mem), 0o555)
        self.chmod_back(mem)
        result = self.pmem('add', 'project', 'x-y', '-d', 'd', '--body', 'b', cwd=root, rc=1)
        self.assertNoTraceback(result)
        self.assertIn('pmem: ', result.stderr)
        self.assertIn('Permission denied', result.stderr)

    def test_G_stale_git_lock_is_removed_and_a_fresh_one_reported(self):
        root, mem = self.init_project(self.work / 'locks')
        lock = mem / '.git' / 'index.lock'
        write(lock, '')
        old = time.time() - 3600
        os.utime(str(lock), (old, old))
        result = self.add(root, 'after-lock', 'Added after a crashed git')
        self.assertIn('removed a stale', result.stderr)
        self.assertFalse(lock.exists())
        self.assertEqual(self.mem_status(mem), '')
        self.assertEqual(self.mem_subject(mem), 'add after-lock')
        write(lock, '')
        self.assertIn('index.lock exists', self.pmem('status', cwd=root).out)
        write(mem / 'pending.md', mem_file('pending', 'pending'))
        self.assertNoTraceback(self.pmem('snapshot', cwd=root, rc=1))
        self.assertTrue(lock.exists())                        # a fresh lock is never removed
        lock.unlink()

    def test_G_parallel_snapshots_are_serialized(self):
        root, mem = self.init_project(self.work / 'parallel-history')
        names = ['ser-%d' % i for i in range(8)]
        env = dict(self.env, PWD=str(root))
        procs = [subprocess.Popen([sys.executable, str(PMEM), 'add', 'project', name, '-d', name,
                                   '--body', name], cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for name in names]
        for proc in procs:
            out, err = proc.communicate(timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, err.decode('utf-8', 'replace'))
            self.assertNotIn(b'index.lock', err)
            self.assertNotIn(b'failed', err)
        self.assertEqual(self.mem_status(mem), '')
        tracked = self.git_out(mem, 'ls-files').split('\n')
        for name in names:
            self.assertIn(name + '.md', tracked)

    @unittest.skipIf(fcntl is None, 'needs fcntl')
    def test_G_session_start_snapshots_in_the_background(self):
        # The hook never runs a writing git itself (a kill-timeout left stale index.locks): it
        # starts a detached snapshot, serialized under the memory lock, and returns at once.
        root, mem = self.init_project(self.work / 'bg', git=True)
        write(mem / 'native-write.md', mem_file('native-write', 'written by Claude natively'))
        outer = self.git_out(root, 'rev-parse', 'HEAD')
        with open(str(mem / '.pmem.lock'), 'a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)         # another pmem holds the lock
            started = time.time()
            result = self.hook('session-start', self.session_payload(root, sid='abcdef0123'),
                               cwd=root)
            self.assertLess(time.time() - started, 15)
            self.assertIn(str(mem), self.context_of(result))
            time.sleep(0.5)
            self.assertIn('native-write.md', self.mem_status(mem))   # still waiting for the lock
        self.wait_snapshot(mem, 'session-start startup abcdef01')
        self.assertEqual(self.mem_status(mem), '')
        self.assertEqual(self.git_out(root, 'rev-parse', 'HEAD'), outer)

    @NOT_ROOT
    def test_H_unwritable_hook_location_wires_without_hooks(self):
        ro = self.base / 'ro-xdg'
        ro.mkdir()
        os.chmod(str(ro), 0o555)
        self.chmod_back(ro)
        repo = self.make_repo(self.work / 'nohookdir')
        result = self.pmem('init', '--no-history', cwd=repo, env=dict(self.env, XDG_DATA_HOME=str(ro)))
        self.assertNoTraceback(result)
        self.assertIn('WITHOUT hooks', result.stderr)
        data = self.settings(repo)
        self.assertEqual(data['autoMemoryDirectory'], rp(repo / '.agent-memory'))
        self.assertEqual(self.our_hook_commands(data), [])

    def test_H_status_reports_hooks_whose_script_is_missing(self):
        root, _mem = self.init_project(self.work / 'script-gone', '--no-history')
        self.assertRegex(self.pmem('status', cwd=root).out, r'(?m)^hooks:\s+present\s*$')
        (self.xdg_data / 'project-memory' / 'pmem.py').unlink()
        out = self.pmem('status', cwd=root).out
        self.assertIn('script missing', out)
        self.assertIn('pmem init', out)

    def test_I_same_name_adds_never_both_succeed(self):
        root, mem = self.init_project(self.work / 'dup-race', '--no-history')
        env = dict(self.env, PWD=str(root))
        procs = [subprocess.Popen([sys.executable, str(PMEM), 'add', 'project', 'dup', '-d', 'd%d' % i,
                                   '--body', 'BODY-%d' % i], cwd=str(root), env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
                 for i in range(8)]
        codes = []
        winner = None
        for i, proc in enumerate(procs):
            out, _err = proc.communicate(timeout=TIMEOUT)
            codes.append(proc.returncode)
            if proc.returncode == 0:
                self.assertIn(b'added', out)
                winner = i
        self.assertEqual(sorted(codes), [0] + [2] * 7)
        self.assertIn('BODY-%d' % winner, read_text(mem / 'dup.md'))
        self.assertEqual(len(lines_linking(self.index_text(mem), 'dup.md')), 1)

    def test_I_restore_refuses_directories(self):
        root, mem = self.init_project(self.work / 'restore-dir')
        self.pmem('handoff', 'path', '--key', 'wk', cwd=root)
        self.pmem('snapshot', cwd=root)
        shutil.rmtree(str(mem / 'handoffs'))
        self.pmem('snapshot', '-m', 'del', cwd=root)
        result = self.pmem('restore', 'handoffs/', cwd=root, rc=2)
        self.assertIn('not a file', result.out)
        self.assertFalse(os.path.lexists(str(mem / 'handoffs')))
        self.pmem('restore', 'handoffs/wk.md', cwd=root)
        self.assertTrue((mem / 'handoffs' / 'wk.md').is_file())
        self.pmem('handoff', 'path', '--key', 'wk3', cwd=root)

    def test_I_relative_xdg_data_home_is_ignored(self):
        repo = self.make_repo(self.work / 'relxdg')
        self.pmem('init', '--no-history', cwd=repo, env=dict(self.env, XDG_DATA_HOME='data'))
        self.assertFalse(os.path.lexists(str(repo / 'data')))
        fallback = self.home / '.local' / 'share' / 'project-memory' / 'pmem.py'
        self.assertEqual(fallback.read_bytes(), PMEM.read_bytes())
        self.assertIn(rp(fallback), [rp(p) for p in re.findall(r"python3 '([^']+)'",
                                                               read_text(self.settings_path(repo)))])
        self.assertEqual(self.git_out(repo, 'status', '--porcelain'), '')

    def test_I_line_breaks_in_paths_never_reach_exclude_files(self):
        repo = self.make_repo(self.work / 'nlrepo')
        odd = repo / 'nl\n*'
        odd.mkdir()
        exclude = repo / '.git' / 'info' / 'exclude'
        before = exclude.read_bytes() if exclude.exists() else None
        result = self.pmem('init', '--here', '--no-claude', '--no-history', cwd=odd)
        self.assertIn('line break', result.stderr)
        self.assertEqual(exclude.read_bytes() if exclude.exists() else None, before)
        self.assertNotIn('\n*\n', read_text(exclude) if exclude.exists() else '')

    def test_L13_memory_forced_into_a_linked_worktree_is_found_and_flagged(self):
        main = self.make_repo(self.work / 'wtf')
        wt = self.add_worktree(main, self.work / 'wtf-wt', 'x')
        self.pmem('init', '--here', cwd=wt, rc=2)
        self.pmem('init', '--here', '--force', '--no-history', cwd=wt)
        mem = rp(wt / '.agent-memory')
        self.assertEqual(self.where(wt), mem)
        self.assertIn('linked worktree', self.pmem('status', cwd=wt, rc=1).out)
        self.assertIn('linked worktree', self.pmem('lint', cwd=wt, rc=1).out)
        context = self.context_of(self.hook('session-start', self.session_payload(wt), cwd=wt))
        self.assertIn('linked worktree', context)


@GIT_REQUIRED
class BehaviourAmendmentTests(AmendmentHelpers):
    def test_J_handoff_edits_count_even_without_a_new_timestamp(self):
        root, mem = self.init_project(self.work / 'stale-stamp', '--no-history')
        handoff = mem / 'HANDOFF.md'
        now = time.time()
        self.handoff_at(handoff, now - 2400)                          # stamped 40 min ago
        self.hook('checkpoint', self.stop_payload(
            root, message='Old session: next step was the negative-input test'), cwd=root)
        self.set_checkpoint_time(mem / '.checkpoints' / 'main.md', now - 1200)   # 20 min ago
        text = read_text(handoff).replace('## Next step\n', '## Next step\nAdd rate limiting\n')
        write(handoff, text)                  # edited now, `modified` left 40 min old (Codex)
        for args in (('context',), ('handoff', 'show')):
            out = self.pmem(*args, cwd=root).stdout
            self.assertNotIn('Auto checkpoint newer', out)
            self.assertNotIn('negative-input', out)
            self.assertIn('ended normally', out)
        listing = self.pmem('handoff', 'list', cwd=root).stdout
        self.assertNotIn(utc_iso(now - 2400), listing)
        self.set_modified(handoff, '2001-01-01T00:00:00Z')            # mtime stays current
        self.assertNotIn('older than', self.pmem('lint', cwd=root).out)

    def test_K_checkpoints_keep_the_previous_session_and_its_tail(self):
        root, mem = self.init_project(self.work / 'ck-prev', '--no-history')
        handoff = mem / 'HANDOFF.md'
        ckdir = mem / '.checkpoints'
        cfg_a = self.base / 'acct-a'
        slug = claude_slug(rp(root))
        trans_a = write(cfg_a / 'projects' / slug / 'sess-A.jsonl', json.dumps(
            {'type': 'user', 'cwd': rp(root), 'message': {'role': 'user', 'content': 'hi A'}}) + '\n')
        self.handoff_at(handoff, time.time() - 60)
        # PreCompact without a message keeps the same session's tail and is never "ended normally"
        self.hook('checkpoint', self.stop_payload(root, message='A-TAIL', session_id='sess-A',
                                                  transcript_path=str(trans_a)), cwd=root)
        self.hook('checkpoint', {'session_id': 'sess-A', 'cwd': str(root), 'trigger': 'auto',
                                 'hook_event_name': 'PreCompact', 'transcript_path': str(trans_a)},
                  cwd=root)
        current = read_text(ckdir / 'main.md')
        self.assertRegex(current, r'(?m)^- event:\s*PreCompact\s*$')
        self.assertIn('A-TAIL', current)
        out = self.pmem('handoff', 'show', cwd=root).stdout
        self.assertIn('Last checkpoint: PreCompact at ', out)
        self.assertNotIn('ended normally', out)
        # session A dies on a usage limit; account B's first turn ends normally
        self.hook('checkpoint', self.stop_payload(root, event='StopFailure', message='A-FAIL-TAIL',
                                                  session_id='sess-A', error='rate_limit',
                                                  transcript_path=str(trans_a)), cwd=root)
        self.assertFalse((ckdir / 'main.prev.md').exists())          # same session: overwritten
        self.hook('checkpoint', self.stop_payload(root, message='B-TAIL', session_id='sess-B'),
                  cwd=root)
        self.assertIn('A-FAIL-TAIL', read_text(ckdir / 'main.prev.md'))
        self.assertIn('B-TAIL', read_text(ckdir / 'main.md'))
        views = {
            'session-start': self.context_of(self.hook('session-start', self.session_payload(root),
                                                       cwd=root)),
            'context': self.pmem('context', cwd=root).stdout,
            'handoff show': self.pmem('handoff', 'show', cwd=root).stdout,
        }
        for view, out in views.items():
            with self.subTest(view=view):
                heads = [l for l in out.splitlines() if l.startswith('## Auto checkpoint')]
                self.assertEqual(len(heads), 1, out)
                self.assertIn('earlier session', heads[0])
                self.assertIn('rate_limit', heads[0])
                self.assertIn('A-FAIL-TAIL', out)
                self.assertIn('Last session ended normally', out)
                # the one-liner must not contradict the pending failure section after it
                self.assertNotIn('handoff is current', out)
                self.assertIn('the earlier checkpoint below still needs reconciling', out)
        self.assertIn('earlier checkpoint: stopped on rate_limit', self.pmem('status', cwd=root).out)
        # the dead account's transcript is still found through the kept checkpoint
        self.assertIn('sess-A.jsonl', self.pmem('sessions', cwd=root).stdout)
        # once the handoff is updated, the old failure is history
        self.handoff_at(handoff, time.time() + 5)
        out = self.pmem('context', cwd=root).stdout
        self.assertNotIn('A-FAIL-TAIL', out)
        self.assertNotIn('## Auto checkpoint', out)
        # a failing retry from another session keeps the previous checkpoint too
        self.hook('checkpoint', self.stop_payload(root, event='StopFailure', message='C-FAIL',
                                                  session_id='sess-C', error='model_not_found'),
                  cwd=root)
        self.assertIn('B-TAIL', read_text(ckdir / 'main.prev.md'))

    def test_L_pmem_key_selects_the_workstream_everywhere(self):
        root, mem = self.init_project(self.work / 'keyed', '--no-history')
        env = dict(self.env, PMEM_KEY='agent-b')
        keyed = mem / 'handoffs' / 'agent-b.md'
        self.assertEqual(rp(self.pmem('handoff', 'path', cwd=root, env=env).stdout.strip()), str(keyed))
        self.set_goal(keyed, 'AGENT-B-GOAL')
        self.set_goal(mem / 'HANDOFF.md', 'MAIN-GOAL')
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root,
                                            env=env))
        self.assertIn('AGENT-B-GOAL', context)
        self.assertNotIn('MAIN-GOAL', context)
        self.hook('checkpoint', self.stop_payload(root, message='B-CKPT'), cwd=root, env=env)
        self.assertIn('B-CKPT', read_text(mem / '.checkpoints' / 'agent-b.md'))
        self.assertFalse((mem / '.checkpoints' / 'main.md').exists())
        self.assertIn('AGENT-B-GOAL', self.pmem('handoff', 'show', cwd=root, env=env).stdout)
        self.assertIn('AGENT-B-GOAL', self.pmem('context', cwd=root, env=env).stdout)
        self.assertIn('MAIN-GOAL', self.pmem('handoff', 'show', '--key', 'main', cwd=root,
                                             env=env).stdout)
        self.assertRegex(self.pmem('status', cwd=root, env=env).stdout,
                         r'(?m)^handoff key:\s+agent-b\s*$')
        bad = dict(self.env, PMEM_KEY='Agent_B')
        self.assertIn('PMEM_KEY', self.pmem('handoff', 'path', cwd=root, env=bad, rc=2).out)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root,
                                            env=bad))
        self.assertIn('MAIN-GOAL', context)                          # hooks fall back, never fail

    def test_M_diff_since_the_last_health_review(self):
        root, mem = self.init_project(self.work / 'health')
        self.add(root, 'before-health', 'Before the review')
        write(mem / 'before-health.md', read_text(mem / 'before-health.md') + 'reviewed\n')
        self.pmem('snapshot', '-m', 'memory-health', cwd=root)
        health = self.git_out(mem, 'rev-parse', '--short', 'HEAD')
        self.add(root, 'after-health', 'Written by another agent')
        write(mem / 'native.md', mem_file('native', 'native write'))
        self.pmem('snapshot', '-m', 'session-start startup 1234abcd', cwd=root)
        (mem / 'before-health.md').unlink()
        self.pmem('snapshot', '-m', 'session-start resume 5678abcd', cwd=root)
        write(mem / 'uncommitted.md', mem_file('uncommitted', 'not snapshotted yet'))
        self.assertNotIn('after-health', self.pmem('diff', cwd=root).stdout)
        for args in (('--since-health',), ('--since', health)):
            with self.subTest(args=args):
                out = self.pmem('diff', *args, cwd=root).stdout
                self.assertIn('memory-health', out.splitlines()[0])
                for kind, name in (('added', 'after-health.md'), ('added', 'native.md'),
                                   ('deleted', 'before-health.md'), ('modified', 'MEMORY.md'),
                                   ('added', 'uncommitted.md')):
                    self.assertRegex(out, r'(?m)^%s\s+%s$' % (kind, re.escape(name)))
        self.pmem('diff', '--since', 'no-such-rev', cwd=root, rc=2)
        self.pmem('diff', '--since', health, '--since-health', cwd=root, rc=2)
        fresh, _ = self.init_project(self.work / 'health-fresh')
        self.assertIn('first snapshot', self.pmem('diff', '--since-health', cwd=fresh).stdout)

    def test_N_size_cap_keeps_checkpoint_warnings_and_pointer(self):
        root, mem = self.init_project(self.work / 'capped', '--no-history')
        handoff = mem / 'HANDOFF.md'
        done = ''.join('- finished step %03d of the long migration plan\n' % i for i in range(200))
        write(handoff, read_text(handoff).replace('## Done\n', '## Done\n' + done))
        self.assertGreater(len(read_text(handoff)), 8000)
        write(self.settings_path(root), '{}\n')                   # a wiring warning too
        self.hook('checkpoint', self.stop_payload(root, event='StopFailure', error='rate_limit',
                                                  message='was midway through migrating the DB'),
                  cwd=root)
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertLessEqual(len(context), 8000)
        for needle in ('WARNING (project memory)', '## Auto checkpoint', 'rate_limit',
                       'migrating the DB', 'Project memory: %s' % mem, 'truncated by pmem'):
            self.assertIn(needle, context)

    def test_N_native_memory_off_appends_the_index_with_a_warning(self):
        root, mem = self.init_project(self.work / 'native-off', '--no-history')
        self.add(root, 'index-probe', 'Index probe memory')
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        self.assertNotIn('](index-probe.md)', context)
        user_settings = self.claude_cfg / 'settings.json'
        cases = {
            'env disable': (dict(self.env, CLAUDE_CODE_DISABLE_AUTO_MEMORY='1'), None),
            'env simple': (dict(self.env, CLAUDE_CODE_SIMPLE='1'), None),
            'override elsewhere': (dict(self.env, CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=str(
                self.base / 'elsewhere')), None),
            'user settings': (self.env, {'autoMemoryEnabled': False}),
        }
        for label, (env, settings) in cases.items():
            with self.subTest(case=label):
                if settings is not None:
                    write(user_settings, json.dumps(settings))
                context = self.context_of(self.hook('session-start', self.session_payload(root),
                                                    cwd=root, env=env))
                self.assertIn('](index-probe.md)', context)
                self.assertIn('native memory is off', context)
                if user_settings.exists():
                    user_settings.unlink()
        same = dict(self.env, CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=str(mem))
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root,
                                            env=same))
        self.assertNotIn('native memory is off', context)

    def test_N_hook_and_status_json_are_ascii_only(self):
        root, mem = self.init_project(self.work / 'ascii', '--no-history')
        self.set_goal(mem / 'HANDOFF.md', 'Ship \U0001F600 feature')
        result = self.hook('session-start', self.session_payload(root), cwd=root,
                           env=dict(self.env, PYTHONIOENCODING='latin-1'))
        self.assertTrue(result.stdout.isascii())
        self.assertIn('Ship \U0001F600 feature', self.context_of(result))
        text = (mem / 'HANDOFF.md').read_bytes().replace(b'by: pmem init', b'by: agent-\xff\xfe')
        write(mem / 'HANDOFF.md', text)
        result = self.pmem('status', '--json', cwd=root, rc=None)
        self.assertTrue(result.stdout.isascii())
        data = json.loads(result.stdout)
        self.assertTrue(data['handoffs'][0]['by'].startswith('agent-'))

    def test_O_pointer_line_names_the_cli(self):
        root, mem = self.init_project(self.work / 'pointer', '--no-history')
        stable = rp(self.xdg_data / 'project-memory' / 'pmem.py')
        context = self.context_of(self.hook('session-start', self.session_payload(root), cwd=root))
        line = [l for l in context.splitlines() if l.startswith('Project memory: ')]
        self.assertEqual(len(line), 1, context)
        self.assertRegex(line[0], r"^Project memory: %s — CLI: python3 '(.+)' \(pmem\); follow the "
                                  r"project-memory skill; update the handoff at milestones\.$"
                         % re.escape(str(mem)))
        self.assertEqual(rp(re.search(r"python3 '([^']+)'", line[0]).group(1)), stable)

    def test_P_status_flags_non_git_sub_directory_launches(self):
        ws, _mem = self.init_project(self.work / 'ws-nogit', '--no-history', '--no-children')
        deep = ws / 'notes' / 'deep'
        deep.mkdir(parents=True)
        self.assertRegex(self.pmem('status', cwd=ws).stdout, r'(?m)^claude wiring:\s+ok ')
        out = self.pmem('status', cwd=deep).stdout
        self.assertRegex(out, r'(?m)^claude wiring:\s+not effective from this directory')
        self.assertRegex(out, r'(?m)^WARN: claude wiring: not effective from this directory .*'
                              r'launch from %s$' % re.escape(rp(ws)))

    def test_V_status_finds_the_codex_block_in_project_agents_files(self):
        repo = self.make_repo(self.work / 'codex-proj')
        self.pmem('init', '--no-history', cwd=repo)
        app = repo / 'src' / 'app'
        app.mkdir(parents=True)
        out = self.pmem('status', cwd=app).stdout
        self.assertIn('pmem wire codex', out)
        self.assertIn('codex: launch from %s or add --add-dir %s' % (rp(repo), rp(repo)), out)
        self.pmem('wire', repo / 'AGENTS.md')
        out = self.pmem('status', cwd=app).stdout
        self.assertRegex(out, r'(?m)^codex block:\s+project \(%s\)$'
                         % re.escape(str(Path(rp(repo)) / 'AGENTS.md')))
        self.assertNotIn('pmem wire codex', out)
        data = json.loads(self.pmem('status', '--json', cwd=app).stdout)
        self.assertEqual(rp(data['codex']['project_block']), rp(repo / 'AGENTS.md'))

    def test_Q_sessions_are_labelled_and_filtered_by_workstream(self):
        repo = self.make_repo(self.work / 'qproj')
        wt = self.add_worktree(repo, self.work / 'qproj-wt', 'feat')
        self.pmem('init', '--no-history', cwd=repo)
        mem = Path(rp(repo / '.agent-memory'))
        now = time.time()
        projects = self.claude_cfg / 'projects'

        def claude(cwd, name, mtime):
            path = write(projects / claude_slug(cwd) / name, json.dumps(
                {'type': 'user', 'cwd': cwd, 'message': {'role': 'user', 'content': 'hi'}}) + '\n')
            os.utime(str(path), (mtime, mtime))
        claude(rp(repo), 'main-old.jsonl', now - 7200)
        claude(rp(repo), 'main-new.jsonl', now - 60)
        claude(rp(wt), 'wt-new.jsonl', now - 30)
        rollout = write(self.codex_home / 'sessions' / '2026' / '11' / '23' / 'rollout-wt.jsonl',
                        json.dumps({'type': 'session_meta', 'payload': {'cwd': os.path.join(rp(wt), 'src')}})
                        + '\n')
        os.utime(str(rollout), (now - 20, now - 20))
        out = self.pmem('sessions', cwd=repo).stdout
        self.assertRegex(out, r'(?m)^claude\s.*\smain\s+\S*main-new\.jsonl$')
        self.assertRegex(out, r'(?m)^claude\s.*\sqproj-wt\s+\S*wt-new\.jsonl$')
        self.assertRegex(out, r'(?m)^codex\s.*\sqproj-wt\s+\S*rollout-wt\.jsonl$')
        handoff = Path(self.pmem('handoff', 'path', cwd=wt).stdout.strip())
        self.handoff_at(handoff, now - 3600)
        out = self.pmem('sessions', '--since-handoff', cwd=wt).stdout
        self.assertIn('wt-new.jsonl', out)
        self.assertIn('rollout-wt.jsonl', out)
        self.assertNotIn('main-new.jsonl', out)
        self.handoff_at(mem / 'HANDOFF.md', now - 3600)
        out = self.pmem('sessions', '--since-handoff', cwd=repo).stdout
        self.assertIn('main-new.jsonl', out)
        self.assertNotIn('main-old.jsonl', out)
        self.assertNotIn('wt-new.jsonl', out)

    def test_J_sessions_since_handoff_uses_the_file_mtime(self):
        root, mem = self.init_project(self.work / 'sess-mtime', '--no-history')
        projects = self.claude_cfg / 'projects' / claude_slug(rp(root))
        now = time.time()
        for name, age in (('before-edit.jsonl', 1800), ('after-edit.jsonl', 30)):
            path = write(projects / name, json.dumps(
                {'type': 'user', 'cwd': rp(root), 'message': {'role': 'user', 'content': 'x'}}) + '\n')
            os.utime(str(path), (now - age, now - age))
        handoff = mem / 'HANDOFF.md'
        self.set_modified(handoff, '2020-01-01T00:00:00Z', mtime=now - 600)   # edited 10 min ago
        out = self.pmem('sessions', '--since-handoff', cwd=root).stdout
        self.assertIn('after-edit.jsonl', out)
        self.assertNotIn('before-edit.jsonl', out)


@GIT_REQUIRED
class ParsingAmendmentTests(AmendmentHelpers):
    def setUp(self):
        super().setUp()
        self.root, self.mem = self.init_project(self.work / 'parse', '--no-history')

    def index(self):
        return self.index_text(self.mem)

    def test_R_links_in_code_comments_and_fences_are_not_index_lines(self):
        write(self.mem / 'kept.md', mem_file('kept', 'kept fact'))
        write(self.mem / 'hidden-fact.md', mem_file('hidden-fact', 'only in a comment'))
        extra = [
            '- [kept](kept.md) — kept fact',
            'Convention: add entries as `- [Title](file.md) — hook`, newest last.',
            '<!-- - [Old idea](old-idea.md) — retired, kept for reference -->',
            '<!-- a multi-line comment',
            '- [Hidden](hidden-fact.md) — commented out',
            '-->',
            '```markdown',
            '- [Fenced](fenced.md) — example inside a code block',
            '```',
        ]
        self.append_index(self.mem, *extra)
        lint = self.pmem('lint', cwd=self.root).out
        self.assertNotIn('dangling', lint)
        self.assertIn('hidden-fact.md: unindexed', lint)           # Claude never sees comments
        self.assertNotIn('kept.md: unindexed', lint)
        before = self.index()
        self.pmem('index', '--fix', cwd=self.root)
        fixed = self.index()
        self.assertTrue(fixed.startswith(before), fixed)            # every line kept
        added = fixed[len(before):].splitlines()
        self.assertEqual([link_target(l) for l in added], ['hidden-fact.md'])
        self.pmem('index', '--fix', cwd=self.root)
        self.assertEqual(self.index(), fixed)
        self.pmem('index', cwd=self.root)

    def test_R_wiki_link_idioms_in_code_are_not_broken_links(self):
        body = ('Guard remote jobs with `[[ -d /data/ckpt ]] || exit 1` before starting.\n'
                "Select columns with `df[['loss', 'step']]`; pyproject uses `[[tool.uv.index]]`.\n"
                '```toml\n[[tool.uv.index]]\n```\n'
                "Also df[['a', 'b']] outside code, and see [[nowhere-at-all]].\n")
        write(self.mem / 'shell-checks.md', mem_file('shell-checks', 'shell idioms', body))
        self.append_index(self.mem, '- [shell-checks](shell-checks.md) — shell idioms')
        broken = [l for l in self.pmem('lint', cwd=self.root).out.splitlines() if 'broken link' in l]
        self.assertEqual(len(broken), 1, broken)
        self.assertIn('[[nowhere-at-all]]', broken[0])

    def test_R_L25_unrecognisable_names_are_never_reindexed(self):
        write(self.mem / 'a:b.md', mem_file('a-b', 'odd name'))
        first = self.pmem('index', '--fix', cwd=self.root).stdout
        self.assertIn('cannot index a:b.md', first)
        once = self.index()
        self.pmem('index', '--fix', cwd=self.root)
        self.pmem('index', '--fix', cwd=self.root)
        self.assertEqual(self.index(), once)
        self.assertNotIn('(a:b.md)', once)
        self.assertIn('cannot be indexed', self.pmem('lint', cwd=self.root).out)
        self.assertIn('cannot be indexed', self.pmem('index', cwd=self.root, rc=1).out)
        (self.mem / 'a:b.md').unlink()
        self.pmem('add', 'reference', 'win-share', '-d', 'UNC root', '--title',
                  'Build share \\\\srv\\builds\\', cwd=self.root)
        self.assertEqual(len(lines_linking(self.index(), 'win-share.md')), 1)
        self.assertIn('ok', self.pmem('index', cwd=self.root).stdout)
        before = self.index()
        self.pmem('index', '--fix', cwd=self.root)
        self.assertEqual(self.index(), before)

    def test_R_L28_pathological_index_lines_stay_fast(self):
        self.append_index(self.mem, '[x](' * 20000, '[x](<' * 20000)
        started = time.time()
        for args in (('lint',), ('index',), ('index', '--fix'), ('add', 'project', 'fast', '-d', 'd')):
            self.pmem(*args, cwd=self.root, rc=None)
        self.assertLess(time.time() - started, 20)

    def test_S_identical_files_from_two_sources_import_once(self):
        a, b = self.base / 'src-a', self.base / 'src-b'
        for src in (a, b):
            write(src / 'MEMORY.md', '# Memory Index\n\n- [Build cache](build-cache.md) — cache\n\n'
                                     '## Gotchas\nAlways run make before test\n')
            write(src / 'build-cache.md', mem_file('build-cache', 'Build cache lives in /cache'))
        out = self.pmem('import', a, b, cwd=self.root).out
        self.assertIn('identical', out)
        self.assertNotIn('needs merge', out)
        self.assertFalse((self.mem / 'build-cache-imported.md').exists())
        self.assertEqual(len(lines_linking(self.index(), 'build-cache.md')), 1)
        self.assertEqual(len(list(self.mem.glob('imported-notes*.md'))), 1)
        self.assertNotIn('needs merge', self.pmem('lint', cwd=self.root).out)
        before = tree_state(self.mem)
        self.pmem('import', a, b, cwd=self.root)
        self.assertEqual(tree_state(self.mem), before)

    def test_S_import_file_strips_the_bom(self):
        local = write(self.root / 'CLAUDE.local.md',
                      b'\xef\xbb\xbf# Local notes\r\nThe staging box is called otter.\r\n')
        self.pmem('import', '--file', local, cwd=self.root)
        notes = [p for p in self.mem.glob('imported-notes*.md')]
        self.assertEqual(len(notes), 1)
        text = read_text(notes[0])
        self.assertNotIn('﻿', text)
        self.assertIn('\n# Local notes\n', text)

    @NOT_ROOT
    def test_S_L27_unreadable_source_files_are_skipped(self):
        src = self.base / 'src-perm'
        write(src / 'a.md', mem_file('a', 'readable'))
        bad = write(src / 'b.md', mem_file('b', 'unreadable'))
        os.chmod(str(bad), 0)
        self.chmod_back(bad, 0o644)
        result = self.pmem('import', src, cwd=self.root)
        self.assertNoTraceback(result)
        self.assertRegex(result.out, r'skipped .*b\.md')
        self.assertTrue((self.mem / 'a.md').is_file())
        self.assertFalse((self.mem / 'b.md').exists())

    def test_T_find_ignores_frontmatter_keys(self):
        self.add(self.root, 'gpu-quota', 'Cluster GPU quota is 8 per user', 'Ask infra for more.')
        self.add(self.root, 'no-force-push', 'Never force-push main', '**Why:** CI breaks.', 'feedback')
        for term in ('project', 'modified', 'metadata', 'feedback', '2026'):
            with self.subTest(term=term):
                self.pmem('find', term, cwd=self.root, rc=1)
        self.assertIn('gpu-quota', self.pmem('find', 'quota', cwd=self.root).stdout)
        self.assertIn('no-force-push', self.pmem('find', 'force-push', cwd=self.root).stdout)
        self.assertIn('no-force-push', self.pmem('find', 'CI', 'breaks', cwd=self.root).stdout)

    def test_T_secret_rules_skip_names_dates_and_credential_locations(self):
        quiet = [
            'fix-disk-space-on-build-runner keeps failing',
            'see risk-assessment-for-deploys first',
            'use flask-sqlalchemy-session-scope',
            'ask-the-oncall-before-restarting-prod',
            'api' + '_key: 1Password vault Infra, item OpenAI-prod',
            'GH tok' + 'en: 1Password → Infra → github-ci',
            'tok' + 'en: `${GH_TOKEN2}` from env',
            'Tok' + 'en: 2026-12-01 expiry',
            '密钥 tok' + 'en:团队保险库第2个条目',
            'DB url: `postgresql://app:${DB_PASS}@db:5432/app`',
        ]
        loud = ['pass' + 'word: hunter2abc9', 'x s' + 'k-' + 'Ab3_' * 6,
                'https://deploy:' + 'pa55w0rd' + '@' + 'gitserver/x', 'key ' + 'AK' + 'IA' + 'ABCDEFGHIJ234567']
        self.append_index(self.mem, '- [sample](sample.md) — sample')
        for line, expected in [(l, 0) for l in quiet] + [(l, 1) for l in loud]:
            with self.subTest(line=line):
                write(self.mem / 'sample.md', mem_file('sample', 'sample', line))
                self.assertEqual(self.lint_rc(self.root, '--strict'), expected)

    def test_T_L23_frontmatter_trailing_comments(self):
        write(self.mem / 'cache-dir.md', (
            '---\nname: cache-dir\ndescription: Where the build cache lives  # short\n'
            'metadata:  # stamped by hand\n  type: project   # was reference\n'
            '  modified: 2026-11-20T01:02:03Z  # moved\n---\n\nThe cache is in /var/cache/build.\n'))
        self.append_index(self.mem, '- [cache-dir](cache-dir.md) — Where the build cache lives')
        self.pmem('lint', '--strict', cwd=self.root)
        self.assertIn('Where the build cache lives — ', self.pmem('find', 'cache', cwd=self.root).stdout)

    def test_T_L24_add_force_keeps_crlf(self):
        write(self.mem / 'native-one.md', ('---\nname: native-one\ndescription: old\nmetadata:\n'
                                           '  type: project\n  originSessionId: abc-123\n---\n\n'
                                           'Body line\nsecond line\n').replace('\n', '\r\n'))
        self.pmem('add', 'project', 'native-one', '-d', 'new desc', '--force', cwd=self.root)
        raw = (self.mem / 'native-one.md').read_bytes()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), raw)
        text = raw.decode('utf-8')
        self.assertIn('Body line\r\nsecond line', text)
        self.assertIn('originSessionId: abc-123', text)
        self.assertEqual(fm_get(text, 'description'), 'new desc')

    def test_T_negative_counts_are_usage_errors(self):
        for args in (('sessions', '--tail', '-1'), ('sessions', '--limit', '-1'),
                     ('history', '-n', '-1')):
            with self.subTest(args=args):
                self.assertNoTraceback(self.pmem(*args, cwd=self.root, rc=2))

    def test_U_add_warns_about_long_index_lines(self):
        result = self.pmem('add', 'project', 'build-needs-legacy-flag', '-d',
                           'Building widgetd on this host needs an extra compile flag; see file for the '
                           'exact flag', '--title', 'Build quirk: extra compile flag', '--body', 'x',
                           cwd=self.root)
        self.assertIn('index line has', result.stderr)
        self.assertIn('> 150', self.pmem('lint', cwd=self.root).out)
        short = self.pmem('add', 'project', 'short-one', '-d', 'Short', '--body', 'x', cwd=self.root)
        self.assertNotIn('index line has', short.stderr)

    def test_U_lint_hints_for_unindexed_and_untimed_memories(self):
        write(self.mem / 'lonely.md', mem_file('lonely', 'dropped from the index on purpose'))
        write(self.mem / 'untimed.md', '---\nname: untimed\ndescription: no timestamp\nmetadata:\n'
                                       '  type: project\n---\n\nbody\n')
        self.append_index(self.mem, '- [untimed](untimed.md) — no timestamp')
        out = self.pmem('lint', cwd=self.root).out
        self.assertIn('`pmem index --fix` to re-index, or `pmem archive lonely` to retire', out)
        self.assertRegex(out, r'(?m)^INFO: .*without metadata\.modified.*untimed')


if __name__ == '__main__':
    unittest.main()
