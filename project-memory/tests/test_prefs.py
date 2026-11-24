"""project-memory 1.1 "preferences": black-box tests for spec section 15.

pmem.py runs only as a subprocess. tests/support.py redirects HOME, XDG_*, CODEX_HOME and
CLAUDE_CONFIG_DIR into a fresh temporary tree reached through a symlink. Projects here are plain
folders initialised with --no-history, so only the private-history tests need git.

Run: cd project-memory && python3 -m unittest tests.test_prefs
"""
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

try:
    from tests.support import (HAS_GIT, MARK, PMEM, REPO, TEMPLATES, TIMEOUT, PmemCase, body_of,
                               fm_get, is_memory_link_line, lines_linking, link_target, read_text,
                               rp, tree_state, write)
except ImportError:  # pragma: no cover - when run from inside tests/
    from support import (HAS_GIT, MARK, PMEM, REPO, TEMPLATES, TIMEOUT, PmemCase, body_of,
                         fm_get, is_memory_link_line, lines_linking, link_target, read_text,
                         rp, tree_state, write)

GIT_REQUIRED = unittest.skipUnless(HAS_GIT, 'git is not installed')
NOT_ROOT = unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0,
                           'file permissions do not stop root')

# ---------------------------------------------------------------------------------------------
# Spec constants (copied from the 1.1 spec; the shipped templates must match them).

VERSION = '1.1.0'

MEMORY_TEMPLATE_10 = (
    '# Project memory\n'
    '\n'
    '> Shared by every agent and account working on this project. Never delete files here'
    ' — retire them with `pmem archive`. HANDOFF.md and handoffs/ hold live task state:'
    ' update them, never prune them.\n'
    '\n'
    '- [HANDOFF — current state](HANDOFF.md) — read before any task at session start;'
    ' update at milestones\n'
)
HANDOFF_LINE = MEMORY_TEMPLATE_10.splitlines()[-1]
PREFS_HEADING = '## Preferences'
MEMORIES_HEADING = '## Memories'
PREFS_BLOCKQUOTE = (
    '> How the user wants you to work: follow every line, every session; on conflict this project'
    ' beats outer folders and the global list. Save a new one at once by editing this file:'
    ' `- [<title>](<name>.md) — <rule>` here (not under Memories) plus `<name>.md` as a'
    ' `feedback` memory with **Why:** and **How to apply:**; then end your reply with'
    ' `✓ preference: <rule>`.'
)
MEMORY_TEMPLATE = (MEMORY_TEMPLATE_10 + '\n' + PREFS_HEADING + '\n' + PREFS_BLOCKQUOTE + '\n\n'
                   + MEMORIES_HEADING + '\n')

MAIN_BEGIN = '<!-- BEGIN project-memory -->'
MAIN_END = '<!-- END project-memory -->'
WIRE_BLOCK = '\n'.join([
    MAIN_BEGIN,
    '## Project memory (`.agent-memory/`)',
    '- Project knowledge lives inside the project, shared by every agent and account: the nearest'
    ' `.agent-memory/` from the working directory upward (never `$HOME`; in a git worktree, look'
    ' from the main checkout). It takes precedence over any other memory location, including'
    ' your built-in memory store and older per-repo notes files such as CLAUDE.local.md.',
    '- At session start and after compaction, run `pmem context` if available (handoff, any newer'
    ' auto checkpoint, inherited preferences, index); otherwise read `HANDOFF.md` (or your'
    ' worktree\'s `handoffs/<name>.md`) and a newer `.checkpoints/<key>.md` — no agent loads'
    ' these natively — then `MEMORY.md` if it is not already in your context. Open single'
    ' memories only when relevant and verify them against the current code before acting.',
    '- Follow every line under `## Preferences` in that `MEMORY.md`, in the `MEMORY.md` of any'
    ' `.agent-memory/` further up, and in this file\'s preferences block; on conflict the nearest'
    ' wins. When the user states a lasting preference or corrects how you work, save it at once'
    ' by editing the files: `- [<title>](<name>.md) — <rule>` under `## Preferences` plus'
    ' `<name>.md` as a `feedback` memory with Why and How to apply (`pmem pref add` does the'
    ' same; `--up` for the whole workspace), then end your reply with `✓ preference: <rule>`. If'
    ' you broke an existing one, sharpen its line and move it to the top. Task-specific'
    ' instructions are not preferences.',
    '- Before debugging an error, search the memory for its key tokens (`pmem find ...` or grep)'
    ' and try a recorded fix first.',
    '- Save right away when the user confirms an unusual approach, when a failure took 3+'
    ' attempts (symptom, root cause, fix), or when you repeat a multi-step procedure: one fact'
    ' per file with `name`/`description`/`metadata.type`/`metadata.modified` (UTC) frontmatter'
    ' plus one line in `MEMORY.md`. Never save secrets; a preference that holds for every'
    ' project goes to the global preferences block only with the user\'s consent'
    ' (`pmem pref promote`).',
    '- Keep the handoff current at milestones (step done, before long or remote jobs, before'
    ' stopping), setting `metadata.modified` and `by`, so another agent can resume if this'
    ' session ends abruptly. Never delete memory files; retire them with `pmem archive`.',
    '- If the sandbox blocks writes to `.agent-memory/`, ask for approval or a relaunch that makes'
    ' it writable (e.g. start from the project root, or Codex `--add-dir <project root>`); never'
    ' save elsewhere. Details: the `project-memory` skill; CLI `pmem` (`pmem status` reports'
    ' wiring problems).',
    MAIN_END,
])

SUB_BEGIN = '<!-- BEGIN project-memory:preferences -->'
SUB_END = '<!-- END project-memory:preferences -->'
SUB_HEADING = ('## Preferences for every project (managed by `pmem pref promote` /'
               ' `pmem pref demote`; a project\'s own Preferences win on conflict)')

INHERITED_HEADING = ("## Inherited preferences (outer folders; this project's own Preferences"
                     " win on conflict)")
OUTSIDE_HEADING = '## Preferences recorded outside the section'
HOOK_EVENTS = ['PreCompact', 'SessionStart', 'Stop', 'StopFailure', 'UserPromptSubmit']

# ---------------------------------------------------------------------------------------------
# Independent readers for the format (simple model, enough for the inputs these tests build).

PREF_HEADING_RE = re.compile(r'^##\s+preferences\s*#*\s*$', re.I)
ANY_HEADING_RE = re.compile(r'^#{1,6}\s')
ENTRY_RE = re.compile(r'^- \[(?:\\.|[^\]\\])*\]\([^)\s]*\) — (?P<rule>.*)$')
INHERITED_RE = re.compile(r'^- (?P<rule>.*?)\s+\(from (?P<dir>.+)\)\s*$')
OVERFLOW_RE = re.compile(r'(?m)^- (?:…|\.\.\.) ?(\d+) more\b.*pmem pref list')
STATUS_RE = re.compile(r'(?m)^preferences:\s*(\d+) project, (\d+) inherited'
                       r'(?: \((\d+) skipped\))?, (\d+) global')


def crlf(text):
    return text.replace('\n', '\r\n')


def visible_flags(lines):
    """False for lines inside fenced code or HTML comments."""
    flags = []
    fence = comment = False
    for line in lines:
        stripped = line.strip()
        if comment:
            flags.append(False)
            comment = '-->' not in line
        elif fence:
            flags.append(False)
            fence = not stripped.startswith(('```', '~~~'))
        elif stripped.startswith(('```', '~~~')):
            flags.append(False)
            fence = True
        elif stripped.startswith('<!--'):
            flags.append(False)
            comment = '-->' not in stripped[4:]
        else:
            flags.append(True)
    return flags


def pref_headings(text):
    lines = text.splitlines()
    flags = visible_flags(lines)
    return [i for i, line in enumerate(lines)
            if flags[i] and PREF_HEADING_RE.match(line.lstrip('\ufeff'))]


def section(text):
    """Lines strictly inside the single visible Preferences section (None if not exactly one)."""
    lines = text.splitlines()
    flags = visible_flags(lines)
    heads = pref_headings(text)
    if len(heads) != 1:
        return None
    start = heads[0]
    end = next((j for j in range(start + 1, len(lines))
                if flags[j] and ANY_HEADING_RE.match(lines[j])), len(lines))
    return lines[start + 1:end]


def entries(text):
    return [line for line in (section(text) or []) if line.startswith('- ')]


def rules(text):
    found = []
    for line in entries(text):
        match = ENTRY_RE.match(line)
        found.append(match.group('rule') if match else line[2:])
    return found


def non_link_lines(text):
    return [line for line in text.splitlines() if line.strip() and not is_memory_link_line(line)]


def memfile(name, desc, body='Body.', type_='project', extra=''):
    return ('---\nname: %s\ndescription: %s\nmetadata:\n  type: %s\n'
            '  modified: 2026-11-01T00:00:00Z\n%s---\n\n%s\n' % (name, desc, type_, extra, body))


def sub_block(*rules_, eol='\n', extra=()):
    return eol.join([SUB_BEGIN, SUB_HEADING] + ['- ' + r for r in rules_] + list(extra)
                    + [SUB_END])


def sub_block_rules(text):
    lines = text.splitlines()
    start, end = lines.index(SUB_BEGIN), lines.index(SUB_END)
    return [line[2:] for line in lines[start + 1:end] if line.startswith('- ')]


def inherited(text):
    """[(rule, memdir, full line)] of the inherited-preferences section in ``text``."""
    lines = text.splitlines()
    if INHERITED_HEADING not in lines:
        return []
    found = []
    for line in lines[lines.index(INHERITED_HEADING) + 1:]:
        if line.startswith('#'):
            break
        match = INHERITED_RE.match(line)
        if match:
            found.append((match.group('rule'), match.group('dir'), line))
    return found


def today_candidates():
    now = time.time()
    return {time.strftime('%Y-%m-%d', time.localtime(now)),
            time.strftime('%Y-%m-%d', time.gmtime(now))}


# ---------------------------------------------------------------------------------------------

class PrefCase(PmemCase):
    """Shared builders for the preference tests."""

    def setUp(self):
        old = os.umask(0o022)                 # the ancestor ownership rule rejects g+w / o+w
        self.addCleanup(os.umask, old)
        super().setUp()
        self._sids = itertools.count(1)

    # -- projects --------------------------------------------------------------------------

    def project(self, name, *args, parent=None):
        """Plain (non-git) project with its own memory -> (root, memdir realpath)."""
        root = Path(parent if parent is not None else self.work) / name
        root.mkdir(parents=True, exist_ok=True)
        extra = ('--here',) if parent is not None else ()
        self.pmem('init', '--no-history', *(extra + args), cwd=root)
        mem = Path(rp(root / '.agent-memory'))
        self.assertTrue((mem / 'MEMORY.md').is_file())
        return root, mem

    def pref(self, *args, cwd=None, rc=0, env=None):
        return self.pmem('pref', *args, cwd=cwd, rc=rc, env=env)

    @staticmethod
    def index(mem):
        return read_text(Path(mem) / 'MEMORY.md')

    def write_feedback(self, mem, name, rule, title=None, type_='feedback'):
        """Backing file + the canonical entry line for it."""
        write(Path(mem) / (name + '.md'), memfile(name, rule, rule, type_))
        return '- [%s](%s.md) — %s' % (title or name, name, rule)

    def write_index(self, mem, prefs=(), memories=(), memories_heading=True, head=None):
        text = (MEMORY_TEMPLATE_10 if head is None else head) + '\n' + PREFS_HEADING + '\n'
        text += PREFS_BLOCKQUOTE + '\n' + ''.join(line + '\n' for line in prefs)
        if memories_heading:
            text += '\n' + MEMORIES_HEADING + '\n' + ''.join(line + '\n' for line in memories)
        write(Path(mem) / 'MEMORY.md', text)
        return text

    def append_lines(self, mem, *lines):
        text = self.index(mem)
        if text and not text.endswith('\n'):
            text += '\n'
        write(Path(mem) / 'MEMORY.md', text + ''.join(line + '\n' for line in lines))

    def add_to_section(self, mem, *new_lines, after=None):
        """Insert lines at the end of the Preferences section (or right after ``after``)."""
        path = Path(mem) / 'MEMORY.md'
        lines = read_text(path).split('\n')
        head = next(i for i, line in enumerate(lines) if PREF_HEADING_RE.match(line))
        end = next((j for j in range(head + 1, len(lines)) if ANY_HEADING_RE.match(lines[j])),
                   len(lines))
        pos = end
        while pos > head + 1 and not lines[pos - 1].strip():
            pos -= 1
        if after is not None:
            pos = lines.index(after) + 1
        lines[pos:pos] = list(new_lines)
        write(path, '\n'.join(lines))

    # -- assertions ------------------------------------------------------------------------

    def assertInSection(self, text, line):
        self.assertIsNotNone(section(text), 'not exactly one Preferences section:\n' + text)
        self.assertIn(line, section(text), text)

    def assertOutsideSection(self, text, line):
        self.assertIn(line, text.splitlines(), text)
        self.assertNotIn(line, section(text) or [], text)

    def assertUnderMemories(self, text, line):
        self.assertOutsideSection(text, line)
        lines = text.splitlines()
        self.assertIn(MEMORIES_HEADING, lines, text)
        self.assertLess(lines.index(PREFS_HEADING), lines.index(MEMORIES_HEADING), text)
        self.assertLess(lines.index(MEMORIES_HEADING), lines.index(line), text)

    def section_links(self, text, filename):
        """Lines of the Preferences section linking ``filename`` (asserts one section)."""
        sec = section(text)
        self.assertIsNotNone(sec, 'not exactly one Preferences section:\n' + text)
        return lines_linking('\n'.join(sec), filename)

    def assertMentionsPath(self, text, path):
        self.assertTrue(str(path) in text or rp(path) in text, '%s not in:\n%s' % (path, text))

    def assertNoTraceback(self, result):
        self.assertNotIn('Traceback', result.out, result.out)

    # -- hooks -----------------------------------------------------------------------------

    def session_payload(self, cwd, sid='0123456789abcdef'):
        return {'session_id': sid, 'cwd': str(cwd), 'hook_event_name': 'SessionStart',
                'source': 'startup',
                'transcript_path': str(self.base / 'nocfg' / 'projects' / 'x' / (sid + '.jsonl'))}

    def session_context(self, root, env=None):
        return self.context_of(self.hook('session-start', self.session_payload(root), cwd=root,
                                         env=env))

    def prompt_payload(self, cwd, prompt, source=None, sid=None):
        sid = sid or '%08x-0000-4000-8000-prompttest' % next(self._sids)
        data = {'session_id': sid, 'cwd': str(cwd), 'hook_event_name': 'UserPromptSubmit',
                'prompt': prompt, 'permission_mode': 'default',
                'transcript_path': str(self.base / 'nocfg' / 'projects' / 'x' / (sid + '.jsonl'))}
        if source is not None:
            data['source'] = source
        return data

    def prompt_hook(self, root, prompt, source=None, sid=None, env=None):
        """`pmem hook prompt`, run from $HOME: the memory must come from the payload's cwd."""
        payload = json.dumps(self.prompt_payload(root, prompt, source, sid), ensure_ascii=False)
        return self.hook('prompt', payload, cwd=self.home, env=env)

    def nudge_of(self, result):
        """additionalContext of a UserPromptSubmit result (asserts the JSON shape)."""
        self.assertTrue(result.stdout.strip(), 'no nudge:\n' + result.out)
        self.assertTrue(result.stdout.isascii(), result.stdout)
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        self.assertNotIn('decision', data)                   # never blocks a prompt
        self.assertIsNot(data.get('continue', True), False)
        out = data['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'UserPromptSubmit')
        self.assertIsInstance(out['additionalContext'], str)
        return out['additionalContext']

    def assertSilent(self, result):
        self.assertEqual(result.stdout, '', result.out)

    # -- global instruction files ----------------------------------------------------------

    @staticmethod
    def backups(path):
        path = Path(path)
        return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + '.bak-'))

    def wired(self, path, head):
        """`path` with `head` and the main block (written by `pmem wire`)."""
        write(path, head)
        self.pmem('wire', path)
        text = read_text(path)
        self.assertIn(MAIN_END, text)
        return text

    def global_targets(self):
        agents = self.codex_home / 'AGENTS.md'
        claude = self.claude_cfg / 'CLAUDE.md'
        write(agents, '# Codex rules\n')
        write(claude, '# Claude rules\n')
        self.pmem('wire', 'codex')
        self.pmem('wire', 'claude')
        return agents, claude

    def pref_status(self, cwd):
        out = self.pmem('status', cwd=cwd, rc=None).out
        match = STATUS_RE.search(out)
        self.assertIsNotNone(match, 'no preferences line in status:\n' + out)
        project, inherit, skipped, glob = match.groups()
        return int(project), int(inherit), int(skipped or 0), int(glob)


# =============================================================================================
# Templates, constants and versions

class TemplateAndVersionTests(PrefCase):
    def test_shipped_templates_match_the_spec(self):
        for name, expected in (('MEMORY.md', MEMORY_TEMPLATE), ('wire-block.md', WIRE_BLOCK + '\n')):
            with self.subTest(template=name):
                text = read_text(TEMPLATES / name)
                self.assertEqual(text.rstrip('\n'), expected.rstrip('\n'))
                self.assertTrue(text.endswith('\n'))

    def test_init_writes_the_template_and_wire_writes_the_block(self):
        root, mem = self.project('tpl')
        self.assertEqual(read_text(mem / 'MEMORY.md'), MEMORY_TEMPLATE)
        self.assertEqual(section(MEMORY_TEMPLATE), [PREFS_BLOCKQUOTE, ''])
        self.pmem('lint', '--strict', cwd=root)
        target = write(self.base / 'wired' / 'AGENTS.md', 'x\n')
        self.pmem('wire', target)
        self.assertEqual(read_text(target), 'x\n\n' + WIRE_BLOCK + '\n')

    def test_version_is_1_1_everywhere(self):
        self.assertEqual(self.pmem('--version').stdout.strip(), 'pmem ' + VERSION)
        plugin = json.loads(read_text(REPO / '.claude-plugin' / 'plugin.json'))
        self.assertEqual(plugin['version'], VERSION)
        market = json.loads(read_text(REPO.parent / '.claude-plugin' / 'marketplace.json'))
        self.assertEqual([p['version'] for p in market['plugins'] if p['name'] == 'project-memory'],
                         [VERSION])
        rows = [line for line in read_text(REPO.parent / 'README.md').splitlines()
                if line.startswith('| [**project-memory**]')]
        self.assertEqual(len(rows), 2, rows)              # English and Chinese tables
        for row in rows:
            self.assertIn('`%s`' % VERSION, row)

    def test_pref_and_prompt_subcommands_have_help(self):
        for args in (('pref',), ('pref', 'list'), ('pref', 'add'), ('pref', 'remove'),
                     ('pref', 'violated'), ('pref', 'promote'), ('pref', 'demote'),
                     ('hook', 'prompt')):
            with self.subTest(args=args):
                self.assertTrue(self.pmem(*(args + ('--help',))).stdout.strip())


# =============================================================================================
# Migration of existing indexes

class MigrationTests(PrefCase):
    BUILD = '- [Build cmd](build-cmd.md) — how to build'

    def legacy(self, name, index, files=None):
        root, mem = self.project(name)
        for fname, content in (files or {}).items():
            write(mem / fname, content)
        write(mem / 'MEMORY.md', index)
        return root, mem

    def init(self, root):
        return self.pmem('init', '--no-history', cwd=root)

    def test_a_1_0_template_becomes_the_1_1_template(self):
        tail = self.BUILD + '\nClosing prose.\n'
        cases = {
            'lf': (MEMORY_TEMPLATE_10, MEMORY_TEMPLATE),
            'crlf': (crlf(MEMORY_TEMPLATE_10), crlf(MEMORY_TEMPLATE)),
            'bom': ('\ufeff' + MEMORY_TEMPLATE_10, '\ufeff' + MEMORY_TEMPLATE),
            'bom crlf': ('\ufeff' + crlf(MEMORY_TEMPLATE_10), '\ufeff' + crlf(MEMORY_TEMPLATE)),
            'no final eol': (MEMORY_TEMPLATE_10.rstrip('\n'), MEMORY_TEMPLATE),
            'crlf no final eol': (crlf(MEMORY_TEMPLATE_10).rstrip('\r\n'), crlf(MEMORY_TEMPLATE)),
            'lines after handoff': (MEMORY_TEMPLATE_10 + tail, MEMORY_TEMPLATE + tail),
            'crlf lines after handoff': (crlf(MEMORY_TEMPLATE_10 + tail),
                                         crlf(MEMORY_TEMPLATE + tail)),
        }
        root, mem = self.legacy('m-tpl', MEMORY_TEMPLATE_10,
                                {'build-cmd.md': memfile('build-cmd', 'how to build')})
        for label, (before, expected) in cases.items():
            with self.subTest(case=label):
                write(mem / 'MEMORY.md', before)
                self.init(root)
                self.assertEqual((mem / 'MEMORY.md').read_bytes(), expected.encode('utf-8'))
                self.init(root)                                       # idempotent
                self.assertEqual((mem / 'MEMORY.md').read_bytes(), expected.encode('utf-8'))

    def test_feedback_lines_move_into_the_new_section(self):
        rebuttal = '- [Rebuttal style](rebuttal-style.md) — insight-first prose'
        cont = '  numbers go in tables, never in parentheses'
        deploy = '- [Deploy](deploy.md) — deploy steps'
        tabs = '- [Tabs](tabs.md) — use tabs'
        original = (MEMORY_TEMPLATE_10 + self.BUILD + '\n' + rebuttal + '\n' + cont + '\n'
                    + deploy + '\n\n## Notes\nSome prose.\n' + tabs + '\n')
        root, mem = self.legacy('m-move', original, {
            'build-cmd.md': memfile('build-cmd', 'how to build'),
            'rebuttal-style.md': memfile('rebuttal-style', 'insight-first prose', type_='feedback'),
            'deploy.md': memfile('deploy', 'deploy steps'),
            'tabs.md': memfile('tabs', 'use tabs', type_='feedback'),
        })
        result = self.init(root)
        self.assertIn('moved 2 feedback lines into Preferences', result.out)
        text = self.index(mem)
        self.assertTrue(text.startswith(MEMORY_TEMPLATE_10), text)
        sec = section(text)
        self.assertIsNotNone(sec, text)
        self.assertEqual(sec[0], PREFS_BLOCKQUOTE)
        self.assertEqual([line for line in sec[1:] if line.strip()], [rebuttal, cont, tabs])
        lines = text.splitlines()
        self.assertEqual(lines.count(MEMORIES_HEADING), 1)
        order = [lines.index(x) for x in (PREFS_HEADING, MEMORIES_HEADING, self.BUILD, deploy,
                                          '## Notes', 'Some prose.')]
        self.assertEqual(order, sorted(order), text)
        # nothing lost or duplicated: only the three new lines were added
        added = [PREFS_HEADING, PREFS_BLOCKQUOTE, MEMORIES_HEADING]
        self.assertEqual(sorted(line for line in lines if line.strip()),
                         sorted([line for line in original.splitlines() if line.strip()] + added))
        again = self.init(root)
        self.assertEqual(self.index(mem), text)
        self.assertNotIn('moved', again.out)

    def test_crlf_bom_index_keeps_its_line_endings_when_lines_move(self):
        tabs = '- [Tabs](tabs.md) — use tabs'
        original = '\ufeff' + crlf(MEMORY_TEMPLATE_10 + self.BUILD + '\n' + tabs + '\n')
        root, mem = self.legacy('m-crlf', original, {
            'build-cmd.md': memfile('build-cmd', 'how to build'),
            'tabs.md': memfile('tabs', 'use tabs', type_='feedback')})
        self.init(root)
        raw = (mem / 'MEMORY.md').read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf# Project memory\r\n'), raw[:40])
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), raw)
        text = raw.decode('utf-8')
        self.assertInSection(text, tabs)
        self.assertUnderMemories(text, self.BUILD)

    def test_index_without_a_handoff_line(self):
        tabs = '- [Tabs](tabs.md) — use tabs'
        original = '# mine\n\nprose line\n' + tabs + '\n' + self.BUILD + '\n'
        root, mem = self.legacy('m-nohandoff', original, {
            'tabs.md': memfile('tabs', 'use tabs', type_='feedback'),
            'build-cmd.md': memfile('build-cmd', 'how to build')})
        self.init(root)
        self.assertEqual(self.index(mem), original)              # init only hints
        # pref add inserts the section before the first memory link line
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        text = self.index(mem)
        self.assertTrue(text.startswith('# mine\n\nprose line\n'), text)
        self.assertInSection(text, tabs)
        self.assertEqual(len(self.section_links(text, 'answer-english.md')), 1)
        self.assertUnderMemories(text, self.BUILD)
        # with no link line at all, `add feedback` puts it at the end
        write(mem / 'MEMORY.md', '# mine\n\nprose line\n')
        self.pmem('add', 'feedback', 'no-emoji', '-d', 'Never use emoji in commit messages',
                  '--body', 'x', cwd=root)
        text = self.index(mem)
        self.assertTrue(text.startswith('# mine\n\nprose line\n'), text)
        self.assertEqual(len(pref_headings(text)), 1, text)
        self.assertEqual(len(self.section_links(text, 'no-emoji.md')), 1, text)

    def test_only_init_pref_add_and_add_feedback_insert_the_section(self):
        root, mem = self.legacy('m-who', MEMORY_TEMPLATE_10)
        self.pmem('add', 'project', 'build-cmd', '-d', 'how to build', '--body', 'x', cwd=root)
        self.assertEqual(pref_headings(self.index(mem)), [])
        write(mem / 'orphan.md', memfile('orphan', 'an unindexed fact'))
        self.pmem('index', '--fix', cwd=root, rc=None)
        self.assertEqual(pref_headings(self.index(mem)), [])
        self.pmem('add', 'feedback', 'no-emoji', '-d', 'Never use emoji', '--body', 'x', cwd=root)
        text = self.index(mem)
        self.assertEqual(len(pref_headings(text)), 1, text)
        self.assertEqual(len(self.section_links(text, 'no-emoji.md')), 1, text)

    def test_heading_variants_are_adopted(self):
        emoji = '- [No emoji](no-emoji.md) — no emoji'
        tabs = '- [Tabs](tabs.md) — use tabs'
        root, mem = self.legacy('m-variants', MEMORY_TEMPLATE_10, {
            'tabs.md': memfile('tabs', 'use tabs', type_='feedback'),
            'no-emoji.md': memfile('no-emoji', 'no emoji', type_='feedback')})
        for heading in ('## preferences', '## Preferences ', '##   PREFERENCES  ##',
                        '## Preferences #'):
            with self.subTest(heading=heading):
                write(mem / 'MEMORY.md', MEMORY_TEMPLATE_10 + emoji + '\n\n' + heading + '\n'
                      + tabs + '\n')
                self.init(root)
                text = self.index(mem)
                self.assertEqual(len(pref_headings(text)), 1, text)
                self.assertIn(heading, text.splitlines())
                self.assertInSection(text, tabs)
                self.assertInSection(text, emoji)
                self.init(root)
                self.assertEqual(self.index(mem), text)
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        text = self.index(mem)
        self.assertEqual(len(pref_headings(text)), 1, text)
        self.assertEqual(len(self.section_links(text, 'answer-english.md')), 1)

    def test_headings_in_comments_or_fences_do_not_count(self):
        tabs = '- [Tabs](tabs.md) — use tabs'
        root, mem = self.legacy('m-hidden', MEMORY_TEMPLATE_10,
                                {'tabs.md': memfile('tabs', 'use tabs', type_='feedback')})
        for label, hidden in (('comment', '<!--\n## Preferences\n-->\n'),
                              ('fence', '```\n## Preferences\n```\n')):
            with self.subTest(case=label):
                write(mem / 'MEMORY.md', MEMORY_TEMPLATE_10 + hidden + tabs + '\n')
                self.init(root)
                text = self.index(mem)
                heads = pref_headings(text)
                self.assertEqual(len(heads), 1, text)
                lines = text.splitlines()
                self.assertLess(lines.index(HANDOFF_LINE), heads[0])
                self.assertLess(heads[0], lines.index(hidden.splitlines()[0]))
                self.assertIn(hidden, text)                          # left alone
                self.assertInSection(text, tabs)

    def test_index_near_the_cap_is_written_with_a_warning(self):
        lines = ['- [m%03d](m%03d.md) — fact %03d' % (i, i, i) for i in range(1, 196)]
        root, mem = self.legacy('m-cap', MEMORY_TEMPLATE_10 + '\n'.join(lines) + '\n')
        self.assertEqual(len(self.index(mem).splitlines()), 200)
        result = self.init(root)
        text = self.index(mem)
        self.assertEqual(len(pref_headings(text)), 1)
        self.assertEqual(len(text.splitlines()), 205)
        for i in range(191, 196):                     # pushed past line 200
            self.assertIn('m%03d' % i, result.out)


# =============================================================================================
# Section-aware writers (spec 3.3 / 3.4)

class SectionAwareWriterTests(PrefCase):
    TABS_RULE = 'Use tabs, never spaces, in every Makefile'

    def prefs_last(self, mem):
        """An index whose last section is Preferences (no ## Memories heading)."""
        tabs = self.write_feedback(mem, 'tabs', self.TABS_RULE, title='Tabs')
        self.write_index(mem, prefs=[tabs], memories_heading=False)
        return tabs

    def test_add_never_appends_into_the_section(self):
        root, mem = self.project('w-add')
        tabs = self.prefs_last(mem)
        self.pmem('add', 'project', 'build-cmd', '-d', 'How to build', '--body', 'make', cwd=root)
        build = '- [build-cmd](build-cmd.md) — How to build'
        text = self.index(mem)
        self.assertUnderMemories(text, build)
        self.assertEqual(entries(text), [tabs])
        self.pmem('add', 'project', 'deploy', '-d', 'How to deploy', '--body', 'x', cwd=root)
        text = self.index(mem)
        self.assertEqual(text.splitlines().count(MEMORIES_HEADING), 1, text)
        self.assertUnderMemories(text, '- [deploy](deploy.md) — How to deploy')
        self.pmem('add', 'feedback', 'no-emoji', '-d', 'Never use emoji in commit messages',
                  '--body', 'x', cwd=root)
        text = self.index(mem)
        self.assertInSection(text, '- [no-emoji](no-emoji.md) — Never use emoji in commit messages')
        self.assertEqual(text.splitlines().count(MEMORIES_HEADING), 1, text)
        self.pmem('lint', '--strict', cwd=root)

    def test_index_fix_routes_new_lines_by_type(self):
        root, mem = self.project('w-fix')
        tabs = self.prefs_last(mem)
        write(mem / 'orphan.md', memfile('orphan', 'An unindexed fact'))
        write(mem / 'loose-rule.md', memfile('loose-rule', 'Ask before force-pushing',
                                             type_='feedback'))
        self.pmem('index', '--fix', cwd=root)
        text = self.index(mem)
        orphan = lines_linking(text, 'orphan.md')
        self.assertEqual(len(orphan), 1, text)
        self.assertUnderMemories(text, orphan[0])
        loose = lines_linking(text, 'loose-rule.md')
        self.assertEqual(len(loose), 1, text)
        self.assertInSection(text, loose[0])
        self.assertIn(tabs, entries(text))
        self.pmem('index', '--fix', cwd=root)
        self.assertEqual(self.index(mem), text)

    def test_index_fix_moves_feedback_lines_into_an_existing_section(self):
        root, mem = self.project('w-move')
        tabs = self.write_feedback(mem, 'tabs', 'use tabs', title='Tabs')
        self.append_lines(mem, tabs)                               # under ## Memories
        self.assertOutsideSection(self.index(mem), tabs)
        result = self.pmem('index', '--fix', cwd=root)
        self.assertRegex(result.out, r'moved 1 feedback lines? into Preferences')
        self.assertEqual(entries(self.index(mem)), [tabs])

    def test_index_fix_never_inserts_the_heading(self):
        root, mem = self.project('w-noheading')
        self.write_feedback(mem, 'tabs', 'use tabs', title='Tabs')
        original = MEMORY_TEMPLATE_10 + '- [Tabs](tabs.md) — use tabs\nClosing prose.\n'
        write(mem / 'MEMORY.md', original)
        result = self.pmem('index', '--fix', cwd=root, rc=None)
        self.assertRegex(result.out, r'(?i)preferences section missing')
        self.assertIn('pmem init', result.out)
        self.assertEqual(self.index(mem), original)

    def test_add_force_keeps_the_rule_text(self):
        root, mem = self.project('w-force')
        tabs = self.write_feedback(mem, 'tabs', self.TABS_RULE, title='Tabs')
        self.write_index(mem, prefs=[tabs])
        result = self.pmem('add', 'feedback', 'tabs', '-d', 'Indentation preference', '--body',
                           'new body', '--force', cwd=root)
        self.assertIn('kept preference line', result.out)
        self.assertEqual(entries(self.index(mem)), [tabs])
        self.assertEqual(len(lines_linking(self.index(mem), 'tabs.md')), 1)
        text = read_text(mem / 'tabs.md')
        self.assertEqual(fm_get(text, 'description'), 'Indentation preference')
        self.assertIn('new body', body_of(text))

    def test_missing_handoff_line_goes_above_the_section(self):
        root, mem = self.project('w-handoff')
        tabs = self.write_feedback(mem, 'tabs', 'use tabs', title='Tabs')
        write(mem / 'build-cmd.md', memfile('build-cmd', 'how to build'))
        build = '- [Build](build-cmd.md) — how to build'
        original = ('# Project memory\n\n' + PREFS_HEADING + '\n' + PREFS_BLOCKQUOTE + '\n' + tabs
                    + '\n\n' + MEMORIES_HEADING + '\n' + build + '\n')
        write(mem / 'MEMORY.md', original)
        self.pmem('index', '--fix', cwd=root)
        text = self.index(mem)
        lines = text.splitlines()
        handoff = lines_linking(text, 'HANDOFF.md')
        self.assertEqual(len(handoff), 1, text)
        self.assertLess(lines.index(handoff[0]), lines.index(PREFS_HEADING), text)
        self.assertEqual(entries(text), [tabs])
        self.assertUnderMemories(text, build)
        self.assertEqual(non_link_lines(text), non_link_lines(original))

    def test_archive_reports_the_removed_preference(self):
        root, mem = self.project('w-archive')
        self.pref('add', 'Use tabs in every Makefile', '--name', 'tabs', cwd=root)
        result = self.pmem('archive', 'tabs', cwd=root)
        self.assertIn('removed preference: Use tabs in every Makefile', result.out)
        self.assertEqual(entries(self.index(mem)), [])
        self.assertEqual(lines_linking(self.index(mem), 'tabs.md'), [])
        self.assertTrue((mem / '.archive' / 'tabs.md').is_file())

    def test_unclosed_comment_is_an_error_and_blocks_appends(self):
        root, mem = self.project('w-comment')
        write(mem / 'fact.md', memfile('fact', 'A fact'))
        text = MEMORY_TEMPLATE + '<!-- unclosed note\n- [fact](fact.md) — A fact\n'
        write(mem / 'MEMORY.md', text)
        number = text.splitlines().index('<!-- unclosed note') + 1
        lint = self.pmem('lint', cwd=root, rc=1)
        self.assertIn('unclosed HTML comment at MEMORY.md:%d' % number, lint.out)
        self.pmem('index', '--fix', cwd=root, rc=None)
        self.assertEqual(self.index(mem), text)
        self.pmem('index', '--fix', cwd=root, rc=None)
        self.assertEqual(self.index(mem), text)

    def test_import_routes_lines_by_type(self):
        root, mem = self.project('w-import')
        tabs = self.prefs_last(mem)
        src = self.base / 'old-memory'
        write(src / 'MEMORY.md', '- [Alpha](alpha.md) — alpha hook\n'
                                 '- [Terse](terse.md) — keep replies short\n')
        write(src / 'alpha.md', memfile('alpha', 'Alpha fact'))
        write(src / 'terse.md', memfile('terse', 'Keep replies short', type_='feedback'))
        self.pmem('import', src, cwd=root)
        text = self.index(mem)
        alpha = lines_linking(text, 'alpha.md')
        terse = lines_linking(text, 'terse.md')
        self.assertEqual((len(alpha), len(terse)), (1, 1), text)
        self.assertOutsideSection(text, alpha[0])
        self.assertInSection(text, terse[0])
        self.assertIn(tabs, entries(text))

    @GIT_REQUIRED
    def test_restore_routes_lines_and_keeps_preference_entries(self):
        root = self.work / 'w-restore'
        root.mkdir()
        self.pmem('init', cwd=root)                                 # with private history
        mem = Path(rp(root / '.agent-memory'))
        self.pmem('add', 'project', 'fact', '-d', 'A fact', '--body', 'v1', cwd=root)
        self.pref('add', 'Use tabs in every Makefile', '--name', 'tabs', '--title', 'Tabs',
                  cwd=root)
        tabs = entries(self.index(mem))
        self.assertEqual(len(tabs), 1)
        self.write_index(mem, prefs=tabs, memories_heading=False)  # Preferences is last
        (mem / 'fact.md').unlink()
        self.pmem('snapshot', '-m', 'drop fact', cwd=root)
        self.pmem('restore', 'fact', cwd=root)
        text = self.index(mem)
        fact = lines_linking(text, 'fact.md')
        self.assertEqual(len(fact), 1, text)
        self.assertOutsideSection(text, fact[0])
        self.assertEqual(entries(text), tabs)
        (mem / 'tabs.md').unlink()
        self.pmem('snapshot', '-m', 'drop tabs file', cwd=root)
        result = self.pmem('restore', 'tabs', cwd=root)
        self.assertIn('kept preference line', result.out)
        self.assertTrue((mem / 'tabs.md').is_file())
        self.assertEqual(entries(self.index(mem)), tabs)
        self.assertEqual(len(lines_linking(self.index(mem), 'tabs.md')), 1)


# =============================================================================================
# pmem pref add

class PrefAddTests(PrefCase):
    def test_default_name_title_and_feedback_file(self):
        root, mem = self.project('pa-default')
        rule = 'Use tabs for indentation in every Makefile'
        self.pref('add', rule, '--why', 'WHY-PROBE', '--how', 'HOW-PROBE', cwd=root)
        name = 'use-tabs-for-indentation-in-every'
        text = read_text(mem / (name + '.md'))
        self.assertEqual(fm_get(text, 'name'), name)
        self.assertEqual(fm_get(text, 'description'), rule)
        self.assertEqual(fm_get(text, 'type'), 'feedback')
        for needle in (rule, 'WHY-PROBE', 'HOW-PROBE'):
            self.assertIn(needle, body_of(text))
        self.assertEqual(entries(self.index(mem)),
                         ['- [use tabs for indentation in every](%s.md) — %s' % (name, rule)])
        self.pmem('lint', '--strict', cwd=root)
        before = (mem / 'MEMORY.md').read_bytes()
        self.pref('add', rule, cwd=root)                            # same rule again: no change
        self.assertEqual((mem / 'MEMORY.md').read_bytes(), before)
        self.assertEqual(read_text(mem / (name + '.md')), text)

    def test_names_for_cjk_and_long_rules_and_explicit_title(self):
        root, mem = self.project('pa-names')
        self.pref('add', '回复一律用中文', cwd=root)
        targets = [link_target(line) for line in entries(self.index(mem))]
        self.assertEqual(len(targets), 1)
        self.assertRegex(targets[0], r'^pref-[0-9a-f]{8}\.md$')
        self.assertEqual(fm_get(read_text(mem / targets[0]), 'description'), '回复一律用中文')
        self.pref('add', 'Prefer descriptive variable names everywhere in this repository',
                  cwd=root)
        name = 'prefer-descriptive-variable-names-everywhere-in'
        self.assertTrue((mem / (name + '.md')).is_file())
        line = lines_linking(self.index(mem), name + '.md')
        self.assertEqual(len(line), 1)
        title = re.match(r'^- \[([^\]]*)\]', line[0]).group(1)
        self.assertLessEqual(len(title), 40)
        self.assertTrue(title.startswith('prefer descriptive'), title)
        self.pref('add', 'Keep replies short', '--name', 'short', '--title', 'Brevity', cwd=root)
        self.assertIn('- [Brevity](short.md) — Keep replies short', entries(self.index(mem)))

    def test_rule_validation(self):
        root, mem = self.project('pa-valid')
        before = tree_state(mem)
        bad = {
            '121 chars': 'x' * 121,
            '121 CJK chars': '中' * 121,
            'comment open': 'Never leave <!-- TODO markers in docs',
            'comment close': 'Close every comment -->',
            'heading': '# Use headings sparingly',
            'quote': '> Quote sources',
            'two lines': 'line one\nline two',
        }
        for label, rule in bad.items():
            with self.subTest(case=label):
                result = self.pref('add', rule, '--name', 'bad-rule', cwd=root, rc=None)
                self.assertNotEqual(result.returncode, 0, result.out)
                self.assertNoTraceback(result)
                self.assertEqual(tree_state(mem), before)
                if label.startswith('121'):
                    self.assertIn('put detail in the body', result.out)
        self.pref('add', 'y' * 120, '--name', 'ok-ascii-rule-with-a-long-name', cwd=root)
        self.pref('add', '中' * 120, '--name', 'ok-cjk-rule-with-a-long-name', cwd=root)
        text = self.index(mem)
        self.assertEqual(rules(text), ['y' * 120, '中' * 120])
        self.assertTrue(all(150 < len(line) <= 220 for line in entries(text)), entries(text))
        self.pmem('lint', '--strict', cwd=root)          # preference lines: no 150-char WARN

    def test_brackets_are_escaped_and_survive_index_fix(self):
        root, mem = self.project('pa-brackets')
        self.pref('add', 'Read [CONTRIBUTING](CONTRIBUTING.md) before editing', '--name',
                  'contrib-first', cwd=root)
        self.pref('add', 'Cite papers as [Author Year](https://doi.org/x)', '--name', 'cite-style',
                  cwd=root)
        text = self.index(mem)
        for name in ('contrib-first', 'cite-style'):
            with self.subTest(name=name):
                line = lines_linking(text, name + '.md')
                self.assertEqual(len(line), 1, text)
                self.assertInSection(text, line[0])
        self.assertNotIn('[CONTRIBUTING](', text)
        self.assertNotIn('[Author Year](', text)
        for _ in range(2):
            self.pmem('index', '--fix', cwd=root)
            self.assertEqual(self.index(mem), text)
        self.pmem('lint', '--strict', cwd=root)

    def test_replace_in_place_order_and_collisions(self):
        root, mem = self.project('pa-replace')
        english = self.write_feedback(mem, 'answer-english', 'Answer in English', title='English')
        self.write_index(mem, prefs=[
            english,
            self.write_feedback(mem, 'tabs', 'Use tabs in Makefiles', title='Tabs'),
            '- Use uv, not pip',                                   # hand-written bare entry
            self.write_feedback(mem, 'no-mock', '不要用 mock', title='No mock'),
        ])
        # (1) the entry backed by NAME is replaced where it stands
        self.pref('add', 'Use tabs in Makefiles and Go files', '--name', 'tabs', cwd=root)
        self.assertEqual(rules(self.index(mem)), ['Answer in English',
                                                  'Use tabs in Makefiles and Go files',
                                                  'Use uv, not pip', '不要用 mock'])
        # (2) a bare entry with the same normalized rule becomes a backed one in place
        self.pref('add', 'use UV; not PIP!', '--name', 'use-uv', cwd=root)
        found = entries(self.index(mem))
        self.assertEqual(len(found), 4, found)
        self.assertEqual(link_target(found[2]), 'use-uv.md')
        self.assertEqual(rules(self.index(mem))[2], 'use UV; not PIP!')
        # (2) backed by another name: refused (NFKC, case, whitespace and punctuation ignored)
        before = tree_state(mem)
        for rule, other in (('ＡＮＳＷＥＲ in english.', 'answer-english'), ('不要用mock', 'no-mock')):
            with self.subTest(rule=rule):
                result = self.pref('add', rule, '--name', 'other-name', cwd=root, rc=2)
                self.assertIn('already recorded as ' + other, result.out)
                self.assertEqual(tree_state(mem), before)
        # --force takes the entry over; the old file gets an ordinary index line
        self.pref('add', 'ANSWER in english.', '--name', 'reply-language', '--force', cwd=root)
        text = self.index(mem)
        first = entries(text)[0]
        self.assertEqual(link_target(first), 'reply-language.md')
        self.assertEqual(rules(text)[0], 'ANSWER in english.')
        self.assertEqual(fm_get(read_text(mem / 'reply-language.md'), 'type'), 'feedback')
        self.assertTrue((mem / 'answer-english.md').is_file())
        old = lines_linking(text, 'answer-english.md')
        self.assertEqual(len(old), 1, text)
        self.assertUnderMemories(text, old[0])

    def test_other_index_lines_of_name_are_removed(self):
        root, mem = self.project('pa-stray')
        write(mem / 'stray.md', memfile('stray', 'old hook', type_='feedback'))
        self.append_lines(mem, '- [Stray](stray.md) — old hook')
        self.pref('add', 'Keep commits small', '--name', 'stray', cwd=root)
        text = self.index(mem)
        line = lines_linking(text, 'stray.md')
        self.assertEqual(len(line), 1, text)
        self.assertInSection(text, line[0])
        self.assertTrue(line[0].endswith(' — Keep commits small'), line[0])

    def test_force_rewrites_an_existing_file_keeping_metadata(self):
        root, mem = self.project('pa-force')
        native = ('---\nname: native-rule\ndescription: old hook\nmetadata:\n  type: feedback\n'
                  '  originSessionId: abc-123\n---\n\nOld body\n')
        write(mem / 'native-rule.md', native)
        rule = 'Never commit generated files'
        self.pref('add', rule, '--name', 'native-rule', cwd=root)
        self.assertEqual(read_text(mem / 'native-rule.md'), native)        # kept
        self.assertEqual(rules(self.index(mem)), [rule])
        self.pref('add', rule, '--name', 'native-rule', '--force', cwd=root)
        text = read_text(mem / 'native-rule.md')
        self.assertEqual(fm_get(text, 'description'), rule)
        self.assertIn('originSessionId: abc-123', text)
        self.assertIn(rule, body_of(text))
        self.assertEqual(rules(self.index(mem)), [rule])

    def test_up_targets_the_nearest_ancestor(self):
        ws, ws_mem = self.project('ws-up')
        proj, mem = self.project('child', parent=ws)
        before = self.index(mem)
        self.pref('add', 'When writing papers: use active voice', '--name', 'active-voice', '--up',
                  cwd=proj)
        self.assertEqual(self.index(mem), before)
        self.assertEqual(rules(self.index(ws_mem)), ['When writing papers: use active voice'])
        self.assertTrue((ws_mem / 'active-voice.md').is_file())
        self.assertFalse((mem / 'active-voice.md').exists())
        lone, lone_mem = self.project('lone')
        state = tree_state(lone_mem)
        self.pref('add', 'Some workspace rule', '--up', cwd=lone, rc=2)
        self.assertEqual(tree_state(lone_mem), state)

    def test_add_migrates_a_1_0_index(self):
        root, mem = self.project('pa-migrate')
        tabs = self.write_feedback(mem, 'tabs', 'use tabs', title='Tabs')
        write(mem / 'MEMORY.md', MEMORY_TEMPLATE_10 + tabs + '\n')
        result = self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        self.assertRegex(result.out, r'moved 1 feedback lines? into Preferences')
        text = self.index(mem)
        self.assertTrue(text.startswith(MEMORY_TEMPLATE_10), text)
        self.assertEqual(sorted(rules(text)), ['Answer in English', 'use tabs'])
        self.assertIn(MEMORIES_HEADING, text.splitlines())

    @GIT_REQUIRED
    def test_add_snapshots_and_registers(self):
        root = self.work / 'pa-git'
        root.mkdir()
        self.pmem('init', cwd=root)
        mem = Path(rp(root / '.agent-memory'))
        registry = self.xdg_data / 'project-memory' / 'projects.txt'
        if registry.exists():
            registry.unlink()
        self.pref('add', 'Answer in English', cwd=root)
        self.assertEqual(self.mem_status(mem), '')
        self.assertIn(str(mem), read_text(registry).splitlines())


# =============================================================================================
# pmem pref remove / violated

class PrefRemoveTests(PrefCase):
    def test_remove_by_name_rule_or_bare_text(self):
        root, mem = self.project('pr')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        self.pref('add', 'Use tabs in Makefiles', '--name', 'tabs', cwd=root)
        self.add_to_section(mem, '- Keep replies under ten lines')
        self.pref('remove', 'answer-english', cwd=root)
        self.pref('remove', 'Use tabs in Makefiles', cwd=root)
        text = self.index(mem)
        self.assertEqual(rules(text), ['Keep replies under ten lines'])
        for name in ('answer-english', 'tabs'):
            with self.subTest(name=name):
                self.assertTrue((mem / (name + '.md')).is_file())
                line = lines_linking(text, name + '.md')
                self.assertEqual(len(line), 1, text)
                self.assertUnderMemories(text, line[0])
        self.pref('remove', 'Keep replies under ten lines', cwd=root)
        self.assertEqual(entries(self.index(mem)), [])
        before = tree_state(mem)
        result = self.pref('remove', 'no such rule anywhere', cwd=root, rc=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tree_state(mem), before)


class PrefViolatedTests(PrefCase):
    def violations(self, mem, name):
        body = body_of(read_text(mem / (name + '.md')))
        self.assertEqual(body.count('**Violations:**'), 1, body)
        after = body.split('**Violations:**', 1)[1]
        return re.findall(r'(?m)^- (\d{4}-\d{2}-\d{2}): (.+?)\s*$', after)

    def test_violated_sharpens_the_rule_logs_it_and_moves_it_to_the_top(self):
        root, mem = self.project('pv')
        self.pref('add', 'Answer in English', '--name', 'answer-english', '--title', 'English',
                  cwd=root)
        self.pref('add', 'Use short paragraphs', '--name', 'short-paras', '--title',
                  'Short paragraphs', cwd=root)
        entry = '- [Short paragraphs](short-paras.md) — Use short paragraphs'
        self.assertIn(entry, entries(self.index(mem)))
        self.add_to_section(mem, '  not tables either', after=entry)
        before = tree_state(mem)
        self.pref('violated', 'short-paras', cwd=root, rc=2)                   # --rule required
        same = self.pref('violated', 'short-paras', '--rule', 'use SHORT paragraphs!', cwd=root,
                         rc=None)
        self.assertNotEqual(same.returncode, 0, same.out)
        self.pref('violated', 'ghost', '--rule', 'Anything at all', cwd=root, rc=2)
        self.assertEqual(tree_state(mem), before)

        sharper = 'Use short paragraphs, never tables, for comparisons'
        self.pref('violated', 'short-paras', '--rule', sharper, '--note', 'replied with a table',
                  cwd=root)
        text = self.index(mem)
        top = '- [Short paragraphs](short-paras.md) — ' + sharper
        self.assertEqual(entries(text)[0], top)
        sec = section(text)
        self.assertEqual(sec[sec.index(top) + 1], '  not tables either')    # continuation moved
        self.assertEqual(rules(text)[1:], ['Answer in English'])
        file_text = read_text(mem / 'short-paras.md')
        self.assertEqual(fm_get(file_text, 'description'), sharper)
        self.assertEqual(fm_get(file_text, 'violations'), '1')
        logged = self.violations(mem, 'short-paras')
        self.assertEqual([note for _date, note in logged], ['replied with a table'])
        self.assertIn(logged[0][0], today_candidates())

        sharpest = 'Use short paragraphs; never tables, even for comparisons'
        self.pref('violated', 'short-paras', '--rule', sharpest, '--note', 'table again',
                  cwd=root)
        self.assertEqual(entries(self.index(mem))[0],
                         '- [Short paragraphs](short-paras.md) — ' + sharpest)
        self.assertEqual(fm_get(read_text(mem / 'short-paras.md'), 'violations'), '2')
        self.assertEqual([note for _d, note in self.violations(mem, 'short-paras')],
                         ['replied with a table', 'table again'])
        self.assertNotIn('⚠', self.index(mem))
        lint = self.pmem('lint', cwd=root)
        self.assertRegex(lint.out, r'(?m)^INFO:.*short-paras')
        self.pmem('lint', '--strict', cwd=root)                              # INFO only

    def test_parallel_violations_are_serialized(self):
        root, mem = self.project('pv-race')
        self.pref('add', 'Use short paragraphs', '--name', 'short-paras', cwd=root)
        env = dict(self.env, PWD=str(root))
        procs = [subprocess.Popen([sys.executable, str(PMEM), 'pref', 'violated', 'short-paras',
                                   '--rule', 'Sharper rule variant number %d' % i,
                                   '--note', 'NOTE-%d' % i],
                                  cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(4)]
        for proc in procs:
            out, err = proc.communicate(timeout=TIMEOUT)
            self.assertEqual(proc.returncode, 0, (out + err).decode('utf-8', 'replace'))
        notes = sorted(note for _d, note in self.violations(mem, 'short-paras'))
        self.assertEqual(notes, ['NOTE-0', 'NOTE-1', 'NOTE-2', 'NOTE-3'])
        self.assertEqual(fm_get(read_text(mem / 'short-paras.md'), 'violations'), '4')
        self.assertEqual(len(lines_linking(self.index(mem), 'short-paras.md')), 1)


# =============================================================================================
# Inheritance (spec 4)

class InheritanceTests(PrefCase):
    def inherited_rules(self, proj):
        return [rule for rule, _dir, _line in inherited(self.session_context(proj))]

    def test_session_start_and_context_show_inherited_preferences(self):
        outer, outer_mem = self.project('outer')
        ws, ws_mem = self.project('ws', parent=outer)
        proj, mem = self.project('proj', parent=ws)
        self.pref('add', 'OUTER-RULE answer in English', '--name', 'outer-rule', cwd=outer)
        self.pref('add', 'WS-RULE use active voice', '--name', 'ws-rule', cwd=ws)
        self.pref('add', 'PROJECT-RULE keep it short', '--name', 'proj-rule', cwd=proj)
        self.set_goal(mem / 'HANDOFF.md', 'GOAL-PROBE-INHERIT')
        context = self.session_context(proj)
        self.assertIn(INHERITED_HEADING, context.splitlines())
        found = inherited(context)
        self.assertEqual([rule for rule, _d, _l in found],
                         ['WS-RULE use active voice', 'OUTER-RULE answer in English'])
        self.assertEqual([rp(d) for _r, d, _l in found], [str(ws_mem), str(outer_mem)])
        for _rule, _dir, line in found:
            self.assertNotIn('](', line)                               # bare rule text
        self.assertLess(context.index('GOAL-PROBE-INHERIT'), context.index(INHERITED_HEADING))
        self.assertNotIn(OUTSIDE_HEADING, context)                      # nothing when none
        out = self.pmem('context', cwd=proj).stdout
        self.assertEqual([rule for rule, _d, _l in inherited(out)],
                         ['WS-RULE use active voice', 'OUTER-RULE answer in English'])
        listing = self.pref('list', cwd=proj).stdout
        for rule in ('PROJECT-RULE keep it short', 'WS-RULE use active voice',
                     'OUTER-RULE answer in English'):
            self.assertIn(rule, listing)
        self.assertEqual(self.pref_status(proj)[:3], (1, 2, 0))

    def test_feedback_outside_the_section_is_listed_until_fixed(self):
        root, mem = self.project('outside')
        write(mem / 'no-emoji.md', memfile('no-emoji', 'Never use emoji in commit messages',
                                           type_='feedback'))
        self.append_lines(mem, '- [No emoji](no-emoji.md) — no emoji')
        for label, text in (('session-start', self.session_context(root)),
                            ('context', self.pmem('context', cwd=root).stdout)):
            with self.subTest(view=label):
                self.assertIn(OUTSIDE_HEADING, text.splitlines())
                self.assertIn('no-emoji — Never use emoji in commit messages', text)
        self.pmem('index', '--fix', cwd=root)
        self.assertNotIn(OUTSIDE_HEADING, self.session_context(root))

    def test_caps_keep_the_handoff(self):
        ws, ws_mem = self.project('ws-cap')
        proj, mem = self.project('proj', parent=ws)
        self.add_to_section(ws_mem, *['- CAP-RULE-%02d ' % i + 'x' * 190 for i in range(60)])
        for i in range(30):
            name = 'loose-%02d' % i
            write(mem / (name + '.md'), memfile(name, 'LOOSE-%02d ' % i + 'y' * 140,
                                                type_='feedback'))
            self.append_lines(mem, '- [%s](%s.md) — loose' % (name, name))
        handoff = mem / 'HANDOFF.md'
        self.set_goal(handoff, 'GOAL-PROBE-CAP')
        done = ''.join('- finished step %03d of the migration\n' % i for i in range(80))
        write(handoff, read_text(handoff).replace('## Done\n', '## Done\n' + done)
              .replace('## Open questions\n', '## Open questions\nEND-PROBE-CAP\n'))
        self.assertLess(len(read_text(handoff)), 4000)
        context = self.session_context(proj)
        self.assertLessEqual(len(context), 8000)
        self.assertIn('GOAL-PROBE-CAP', context)
        self.assertIn('END-PROBE-CAP', context)                   # the whole handoff survives
        found = inherited(context)
        self.assertTrue(0 < len(found) <= 40, len(found))
        self.assertLessEqual(sum(len(line) + 1 for _r, _d, line in found), 2000)
        for rule, _dir, _line in found:
            self.assertTrue(rule.startswith('CAP-RULE-'), rule)
            self.assertLessEqual(len(rule), 161)
        overflow = OVERFLOW_RE.search(context)
        self.assertIsNotNone(overflow, context)
        self.assertEqual(int(overflow.group(1)) + len(found), 60)

    def test_ownership_rule_and_world_writable_parents(self):
        shared = self.work / 'shared'
        shared.mkdir()
        self.addCleanup(os.chmod, str(shared), 0o755)
        ws, ws_mem = self.project('ws', parent=shared)
        self.pref('add', 'OPEN-RULE answer in English', '--name', 'open-rule', cwd=ws)
        proj, _mem = self.project('proj', parent=ws)
        self.assertEqual(self.inherited_rules(proj), ['OPEN-RULE answer in English'])
        os.chmod(str(shared), 0o777)                                   # world-writable, no sticky
        self.assertEqual(self.inherited_rules(proj), [])
        listing = self.pref('list', cwd=proj).out
        self.assertMentionsPath(listing, ws / '.agent-memory')
        self.assertRegex(listing, r'(?i)skip')
        self.assertEqual(self.pref_status(proj)[:3], (0, 0, 1))
        os.chmod(str(shared), 0o1777)                                  # sticky: fine again
        self.assertEqual(self.inherited_rules(proj), ['OPEN-RULE answer in English'])
        os.chmod(str(shared), 0o755)
        for path, mode, normal in ((ws_mem, 0o775, 0o755), (ws_mem / 'MEMORY.md', 0o664, 0o644),
                                   (ws_mem / 'MEMORY.md', 0o646, 0o644)):
            with self.subTest(path=path.name, mode=oct(mode)):
                os.chmod(str(path), mode)
                try:
                    self.assertEqual(self.inherited_rules(proj), [])
                finally:
                    os.chmod(str(path), normal)
        self.assertEqual(self.inherited_rules(proj), ['OPEN-RULE answer in English'])

    def test_home_and_above_are_never_inherited(self):
        home_index = '# home\n\n## Preferences\n- HOME-RULE never inherit this\n'
        write(self.home / '.agent-memory' / 'MEMORY.md', home_index)
        write(self.base / '.agent-memory' / 'MEMORY.md',
              '# above\n\n## Preferences\n- ABOVE-HOME-RULE never inherit this\n')
        proj, _mem = self.project('proj-home')
        self.assertNotIn('HOME-RULE', self.session_context(proj))
        self.assertNotIn('HOME-RULE', self.pref('list', cwd=proj).stdout)
        self.pref('add', 'Some workspace rule', '--up', cwd=proj, rc=2)
        self.assertEqual(read_text(self.home / '.agent-memory' / 'MEMORY.md'), home_index)

    def test_symlinked_ancestors_are_refused(self):
        evil = '# evil\n\n## Preferences\n- EVIL-%s rule from elsewhere\n'
        # a) the ancestor memory dir resolves to $HOME
        ws_a = self.work / 'ws-a'
        proj_a, _ = self.project('proj', parent=ws_a)
        write(self.home / 'MEMORY.md', evil % 'A')
        os.symlink(str(self.home), str(ws_a / '.agent-memory'))
        # b) the ancestor index is a symlink to a file elsewhere
        ws_b, ws_b_mem = self.project('ws-b')
        proj_b, _ = self.project('proj', parent=ws_b)
        outside = write(self.base / 'elsewhere' / 'MEMORY.md', evil % 'B')
        (ws_b_mem / 'MEMORY.md').unlink()
        os.symlink(str(outside), str(ws_b_mem / 'MEMORY.md'))
        # c) the ancestor memory dir resolves to its own project root
        ws_c = self.work / 'ws-c'
        proj_c, _ = self.project('proj', parent=ws_c)
        write(ws_c / 'MEMORY.md', evil % 'C')
        os.symlink('.', str(ws_c / '.agent-memory'))
        for label, proj in (('to home', proj_a), ('index symlink', proj_b), ('to root', proj_c)):
            with self.subTest(case=label):
                self.assertNotIn('EVIL-', self.session_context(proj))
                listing = self.pref('list', cwd=proj, rc=None)
                self.assertNoTraceback(listing)
                self.assertNotIn('EVIL-', listing.stdout)


# =============================================================================================
# Global block (spec 5)

class GlobalBlockTests(PrefCase):
    def test_promote_adds_a_sub_block_after_main_end_and_removes_the_local_entry(self):
        agents, claude = self.global_targets()
        root, mem = self.project('gp')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        before = {p: read_text(p) for p in (agents, claude)}
        saved = {p: len(self.backups(p)) for p in (agents, claude)}
        self.pref('promote', 'answer-english', cwd=root)
        for path in (agents, claude):
            with self.subTest(target=path.name):
                self.assertEqual(read_text(path).rstrip('\n'),
                                 (before[path] + '\n' + sub_block('Answer in English')).rstrip('\n'))
                self.assertEqual(len(self.backups(path)), saved[path] + 1)
        text = self.index(mem)
        self.assertEqual(entries(text), [])
        self.assertTrue((mem / 'answer-english.md').is_file())
        old = lines_linking(text, 'answer-english.md')
        self.assertEqual(len(old), 1, text)
        self.assertUnderMemories(text, old[0])
        listing = self.pref('list', cwd=root).out
        self.assertIn('Answer in English', listing)
        self.assertNotRegex(listing, r'(?i)diverg|differ')
        self.assertEqual(self.pref_status(root), (0, 0, 0, 1))
        # by rule text, keeping the local entry; a symlinked --config-dir is the same file
        self.pref('add', 'Use tabs in Makefiles', '--name', 'tabs', cwd=root)
        cfg_link = self.base / 'cfg-link'
        os.symlink(str(self.claude_cfg), str(cfg_link))
        self.pref('promote', 'Use tabs in Makefiles', '--keep-local', '--config-dir', cfg_link,
                  cwd=root)
        for path in (agents, claude):
            self.assertEqual(sub_block_rules(read_text(path)),
                             ['Answer in English', 'Use tabs in Makefiles'])
        self.assertEqual(rules(self.index(mem)), ['Use tabs in Makefiles'])
        # promoting a rule a target already has (normalized) adds nothing
        snapshot = {p: read_text(p) for p in (agents, claude)}
        self.pref('promote', 'tabs', '--keep-local', cwd=root)
        for path in (agents, claude):
            self.assertEqual(read_text(path), snapshot[path])

    def test_new_sub_block_sits_between_main_end_and_following_text(self):
        root, _mem = self.project('gp-mid')
        target = self.base / 'rules' / 'CUSTOM.md'
        before = self.wired(target, 'top\n') + '\nbottom\n'
        write(target, before)
        extra_cfg = self.base / 'cfg-extra'
        extra = extra_cfg / 'CLAUDE.md'
        extra_before = self.wired(extra, '# Extra account\n')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        self.pref('promote', 'answer-english', '--target', target, '--config-dir', extra_cfg,
                  cwd=root)
        self.assertEqual(read_text(target), before.replace(
            MAIN_END + '\n\nbottom', MAIN_END + '\n\n' + sub_block('Answer in English')
            + '\n\nbottom'))
        self.assertEqual(sub_block_rules(read_text(extra)), ['Answer in English'])
        self.assertTrue(read_text(extra).startswith(extra_before.rstrip('\n')))

    def test_minimal_edits_keep_eol_bom_and_divergence(self):
        root, _mem = self.project('gp-min')
        agents = self.codex_home / 'AGENTS.md'
        claude = self.claude_cfg / 'CLAUDE.md'
        a_text = self.wired(agents, '\ufeff# Codex rules\r\n')
        write(agents, a_text + '\r\n' + sub_block('Rule one', eol='\r\n') + '\r\n')
        c_text = self.wired(claude, '# Claude rules\n')
        write(claude, c_text + '\n' + sub_block('Rule one', 'Claude only rule', extra=()).replace(
            '- Rule one\n', '- Rule one\n  with a continuation sentence\nSome prose the user added.\n')
            + '\n')
        for label, out in (('pref list', self.pref('list', cwd=root).out),
                           ('status', self.pmem('status', cwd=root, rc=None).out)):
            with self.subTest(view=label):
                self.assertRegex(out, r'(?i)diverg|differ')
        self.pref('add', 'Prefer small focused commits', '--name', 'small-commits', cwd=root)
        before_a, before_c = read_text(agents), read_text(claude)
        self.pref('promote', 'small-commits', cwd=root)
        after_a, after_c = read_text(agents), read_text(claude)
        self.assertEqual(after_a, before_a.replace(
            '\r\n' + SUB_END, '\r\n- Prefer small focused commits\r\n' + SUB_END))
        self.assertEqual(after_c, before_c.replace(
            '\n' + SUB_END, '\n- Prefer small focused commits\n' + SUB_END))
        self.assertNotIn('Claude only rule', after_a)                  # never merged
        raw = agents.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))
        # demote: normalized match, removed with continuation lines from every target
        self.pref('demote', 'rule one', cwd=root)
        self.assertEqual(read_text(agents), after_a.replace('- Rule one\r\n', ''))
        self.assertEqual(read_text(claude), after_c.replace(
            '- Rule one\n  with a continuation sentence\n', ''))

    @NOT_ROOT
    def test_two_phase_write_failure_writes_nothing(self):
        agents, claude = self.global_targets()
        root, mem = self.project('gp-fail')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        index_before = self.index(mem)

        def state():
            return tree_state(self.codex_home), tree_state(self.claude_cfg)
        for locked in (self.codex_home, self.claude_cfg):
            with self.subTest(locked=locked.name):
                before = state()
                os.chmod(str(locked), 0o555)
                try:
                    self.pref('promote', 'answer-english', cwd=root, rc=1)
                finally:
                    os.chmod(str(locked), 0o755)
                self.assertEqual(state(), before)
                self.assertEqual(self.index(mem), index_before)
        with self.subTest(case='malformed second target'):
            write(claude, read_text(claude) + '\n' + SUB_BEGIN + '\n' + sub_block('x') + '\n')
            before = state()
            self.pref('promote', 'answer-english', cwd=root, rc=1)
            self.assertEqual(state(), before)
            self.assertEqual(self.index(mem), index_before)

    def test_wire_keeps_the_sub_block(self):
        agents, claude = self.global_targets()
        root, mem = self.project('gp-wire')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        self.pref('add', 'Use tabs in Makefiles', '--name', 'tabs', cwd=root)
        self.pref('promote', 'answer-english', cwd=root)
        self.pref('promote', 'tabs', cwd=root)
        promoted = read_text(agents)
        block = sub_block('Answer in English', 'Use tabs in Makefiles')
        self.assertIn(block, promoted)
        lines = promoted.split('\n')
        lines[lines.index(MAIN_BEGIN) + 2] = '- old rule line'           # an outdated main block
        write(agents, '\n'.join(lines))
        self.pmem('wire', 'codex')
        self.assertEqual(read_text(agents), promoted)
        result = self.pmem('wire', 'codex', '--remove')
        self.assertRegex(result.out, r'kept the global preferences block \(2 rules\)')
        removed = read_text(agents)
        self.assertNotIn(MAIN_BEGIN, removed)
        self.assertIn(block, removed)
        listing = self.pref('list', cwd=root).stdout                    # still read
        self.assertIn('Answer in English', listing)
        self.assertIn('Use tabs in Makefiles', listing)
        self.pmem('wire', 'codex')                                       # main block comes back
        rewired = read_text(agents)
        self.assertIn(MAIN_BEGIN, rewired)
        self.assertIn(block, rewired)
        # promote needs a file with the main block
        self.pmem('wire', 'codex', '--remove')
        self.pmem('wire', 'claude', '--remove')
        self.pref('add', 'Keep commits small', '--name', 'small-commits', cwd=root)
        before = (tree_state(self.codex_home), tree_state(self.claude_cfg), self.index(mem))
        result = self.pref('promote', 'small-commits', cwd=root, rc=1)
        self.assertIn('pmem wire', result.out)
        self.assertEqual((tree_state(self.codex_home), tree_state(self.claude_cfg),
                          self.index(mem)), before)
        # with no project memory, list exits 1 but still shows the global rules
        nowhere = self.work / 'nowhere'
        nowhere.mkdir()
        result = self.pref('list', cwd=nowhere, rc=1)
        self.assertIn('Answer in English', result.stdout)

    def test_wire_remove_of_a_top_block_leaves_no_leading_blank_line(self):
        root, _mem = self.project('gp-top')
        target = self.base / 'rules' / 'ONLY.md'
        write(target, '')
        self.pmem('wire', target)                       # the main block is the whole file
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        self.pref('promote', 'answer-english', '--keep-local', '--target', target, cwd=root)
        self.pmem('wire', target, '--remove')
        self.assertEqual(read_text(target), sub_block('Answer in English') + '\n')

    def test_malformed_or_nested_sub_blocks_are_refused(self):
        root, mem = self.project('gp-bad')
        self.pref('add', 'Answer in English', '--name', 'answer-english', cwd=root)
        agents = self.codex_home / 'AGENTS.md'
        main = self.wired(agents, '# Codex rules\n')
        cases = {
            'two begins': main + '\n' + SUB_BEGIN + '\n' + sub_block('A rule') + '\n',
            'begin only': main + '\n' + SUB_BEGIN + '\n- A rule\n',
            'end before begin': main + '\n' + SUB_END + '\n- A rule\n' + SUB_BEGIN + '\n',
            'nested': main.replace(MAIN_END, sub_block('A rule') + '\n' + MAIN_END),
            'overlapping': main.replace(MAIN_END, SUB_BEGIN + '\n- A rule\n' + MAIN_END)
            + SUB_END + '\n',
        }
        entry = entries(self.index(mem))
        for label, text in cases.items():
            write(agents, text)
            before = tree_state(self.codex_home)
            for args in (('pref', 'promote', 'answer-english'), ('pref', 'demote', 'A rule'),
                         ('wire', 'codex'), ('wire', 'codex', '--remove')):
                with self.subTest(case=label, args=args):
                    self.assertNoTraceback(self.pmem(*args, cwd=root, rc=1))
                    self.assertEqual(tree_state(self.codex_home), before)
            self.assertEqual(entries(self.index(mem)), entry)

    def test_demote_matching_and_to_project(self):
        root, mem = self.project('gp-demote')
        agents = self.codex_home / 'AGENTS.md'
        main = self.wired(agents, '# Codex rules\n')
        write(agents, main + '\n' + sub_block('Use tabs', 'Use tabs in Go files',
                                              'Use tabs in Makefiles', 'Answer in English') + '\n')
        before = read_text(agents)
        self.pref('demote', 'use TABS!', cwd=root)                  # normalized-equal wins
        self.assertEqual(read_text(agents), before.replace('- Use tabs\n', '', 1))
        before = read_text(agents)
        self.pref('demote', 'use tabs in', cwd=root, rc=2)          # ambiguous substring
        self.assertEqual(read_text(agents), before)
        self.pref('demote', 'GO FILES', cwd=root)                   # unique substring
        self.assertEqual(read_text(agents), before.replace('- Use tabs in Go files\n', ''))
        self.pref('demote', 'Answer in English', '--to-project', cwd=root)
        self.assertEqual(sub_block_rules(read_text(agents)), ['Use tabs in Makefiles'])
        text = self.index(mem)
        self.assertEqual(rules(text), ['Answer in English'])
        backing = link_target(entries(text)[0])
        self.assertTrue(backing and (mem / backing).is_file(), text)
        self.assertEqual(fm_get(read_text(mem / backing), 'type'), 'feedback')


# =============================================================================================
# Registry (spec 9)

class RegistryTests(PrefCase):
    def registry(self):
        return self.xdg_data / 'project-memory' / 'projects.txt'

    def test_init_and_pref_add_append_without_rewriting(self):
        registry = write(self.registry(), 'pre-existing line\n')
        inode = os.stat(str(registry)).st_ino
        _a, a_mem = self.project('reg-a')
        b, b_mem = self.project('reg-b')
        text = read_text(registry)
        self.assertTrue(text.startswith('pre-existing line\n'), text)
        for mem in (a_mem, b_mem):
            self.assertIn(str(mem), text.splitlines())
        self.assertEqual(os.stat(str(registry)).st_ino, inode)
        registry.unlink()
        self.pref('add', 'Answer in English', cwd=b)
        self.assertIn(str(b_mem), read_text(registry).splitlines())

    def test_unwritable_registry_is_only_a_warning(self):
        self.registry().mkdir(parents=True)                     # cannot be opened for appending
        root = self.work / 'reg-ro'
        root.mkdir()
        self.assertNoTraceback(self.pmem('init', '--no-history', cwd=root))
        self.assertNoTraceback(self.pref('add', 'Answer in English', '--name', 'answer-english',
                                         cwd=root))
        self.assertEqual(rules(self.index(root / '.agent-memory')), ['Answer in English'])

    def test_list_all_reads_registered_projects(self):
        a, _ = self.project('all-a')
        b, b_mem = self.project('all-b')
        c, c_mem = self.project('all-c')
        self.pref('add', 'A-ONLY answer in English', '--name', 'a-only', cwd=a)
        self.pref('add', 'B-ONLY use tabs', '--name', 'b-only', cwd=b)
        self.pref('add', 'C-ONLY hidden rule', '--name', 'c-only', cwd=c)
        with open(str(self.registry()), 'a', encoding='utf-8') as handle:
            handle.write('%s\n%s\n' % (b_mem, self.work / 'gone' / '.agent-memory'))
        os.chmod(str(c_mem), 0o775)                           # fails the ownership rule
        self.addCleanup(os.chmod, str(c_mem), 0o755)
        result = self.pref('list', '--all', cwd=a)
        self.assertNoTraceback(result)
        self.assertIn('A-ONLY answer in English', result.stdout)
        self.assertEqual(result.stdout.count('B-ONLY use tabs'), 1)
        self.assertMentionsPath(result.stdout, b_mem)
        self.assertNotIn('C-ONLY', result.stdout)
        self.assertNotIn('B-ONLY', self.pref('list', cwd=a).stdout)
        data = json.loads(self.pref('list', '--all', '--json', cwd=a).stdout)
        self.assertIn('B-ONLY use tabs', json.dumps(data, ensure_ascii=False))

    def test_list_all_is_capped(self):
        proj, _ = self.project('cap-list')
        with open(str(self.registry()), 'a', encoding='utf-8') as handle:
            for i in range(30):
                mem = self.work / 'many' / ('p%02d' % i) / '.agent-memory'
                write(mem / 'MEMORY.md', '# p\n\n## Preferences\n' + ''.join(
                    '- MANY-%02d-%02d rule text\n' % (i, j) for j in range(10)))
                handle.write(str(mem) + '\n')
        out = self.pref('list', '--all', cwd=proj).stdout
        shown = [line for line in out.splitlines() if 'MANY-' in line]
        self.assertTrue(0 < len(shown) <= 200, len(shown))
        self.assertLessEqual(len(out.splitlines()), 250)


# =============================================================================================
# Capture hook: pmem hook prompt (spec 6)

class PromptHookTests(PrefCase):
    STRONG = 'From now on, answer in English.'

    def setUp(self):
        super().setUp()
        self.root, self.mem = self.project('hp')

    def test_positive_signals_nudge(self):
        positives = {
            'zh scope': '以后回复都用中文',
            'zh remember': '记住：提交前先跑测试',
            'zh dislike': '我不喜欢你自己 push',
            'zh default': '默认用 uv 装依赖',
            'en from now on': self.STRONG,
            'en next time': 'Next time, ask before you push.',
            'en prefer': "I'd prefer tables over bullet lists",
            'en dislike': "I don't like it when you add emojis",
            'en remember': 'Please remember to run the linter first',
            'en default': 'By default use pathlib',
            'style zh': '你写太多了，凝练一点',
            'style en': 'This answer is too long',
            'style concise': 'Be more concise please',
            'weak + your': 'Stop adding comments to your code',
            'weak + zh agent': '你的回答不对',
            'weak + you': 'Always ask before you delete files',
            "don't + your": "Don't end your replies with a summary",
        }
        for label, prompt in positives.items():
            with self.subTest(case=label):
                context = self.nudge_of(self.prompt_hook(self.root, prompt))
                self.assertIn('✓ preference', context)
                self.assertIn('## Preferences', context)
                self.assertLessEqual(len(context), 600)
                match = re.search(r'(\S+)/MEMORY\.md', context)
                self.assertIsNotNone(match, context)
                self.assertEqual(rp(match.group(1)), str(self.mem))

    def test_source_deny_list(self):
        for source in (None, 'user', 'sdk'):
            with self.subTest(source=source):
                self.nudge_of(self.prompt_hook(self.root, self.STRONG, source=source))
        for source in ('system', 'loop_wakeup', 'schedule_wakeup', 'poll_event'):
            with self.subTest(source=source):
                self.assertSilent(self.prompt_hook(self.root, self.STRONG, source=source))

    def test_negatives_stay_silent(self):
        for prompt in ('不要急，先看日志', '报错了', '写一个简短的 README', "don't worry about tests",
                       'stop the server', 'always-on', 'never mind',
                       'Why does the server never start', 'Use pathlib instead of os.path in utils.py',
                       '帮我把这个函数改成异步的，不要改接口',
                       'Reply with exactly OK. Do not use any tools'):
            with self.subTest(prompt=prompt):
                self.assertSilent(self.prompt_hook(self.root, prompt))

    def test_length_gate_pasted_content_and_fences(self):
        cases = [
            ('long', self.STRONG + ' ' + 'x' * 300, False),
            ('short enough', self.STRONG + ' ' + 'x' * 240, True),
            ('signal only in paste', '<pasted_content id="1">from now on use tabs</pasted_content>'
                                     ' please fix the build', False),
            ('signal only in a fence', 'what does this do?\n```\nfrom now on use tabs\n```', False),
            ('long paste dropped', 'From now on keep answers short. <pasted_content id="2">'
                                   + 'log line\n' * 200 + '</pasted_content>', True),
            ('long fence dropped', 'From now on, format code like this:\n```python\n'
                                   + 'x = 1\n' * 100 + '```', True),
            ('huge', 'from now on ' + 'y' * 2000000, False),
        ]
        for label, prompt, nudged in cases:
            with self.subTest(case=label):
                result = self.prompt_hook(self.root, prompt)
                if nudged:
                    self.nudge_of(result)
                else:
                    self.assertSilent(result)

    def test_violation_markers_list_the_top_rules(self):
        top = ['Answer in English', 'Use short paragraphs', 'Never add emoji',
               'Use tabs in Makefiles']
        self.write_index(self.mem, prefs=[self.write_feedback(self.mem, 'rule-%d' % i, rule)
                                          for i, rule in enumerate(top)])
        for prompt in ('我跟你说过了，不要用表格', 'I told you so', 'Again you used a table',
                       '你又忘了'):
            with self.subTest(prompt=prompt):
                context = self.nudge_of(self.prompt_hook(self.root, prompt))
                # round 2 (C7): the violation part leads and names up to 3 rules; none shares a
                # word with these prompts, so section order decides; the rules have their own
                # budget on top of the 600-char base
                self.assertTrue(context.startswith('project-memory: the user says you broke an '
                                                   'existing preference: '), context)
                self.assertEqual([rule for rule in top if '"%s"' % rule in context], top[:3])
                self.assertNotIn('Otherwise ignore this note', context)
                shown = '; '.join('"%s"' % rule for rule in top[:3])
                self.assertLessEqual(len(context) - len(shown), 600)
        context = self.nudge_of(self.prompt_hook(self.root, 'Next time keep answers short'))
        for rule in top:
            self.assertNotIn(rule, context)

    def test_rate_limit_per_session(self):
        sid = 'cafe0123-4567-89ab-cdef-ratelimit'
        self.nudge_of(self.prompt_hook(self.root, self.STRONG, sid=sid))
        state = self.mem / '.checkpoints' / ('.prompt-' + sid[:8])
        self.assertTrue(state.is_file())
        self.assertSilent(self.prompt_hook(self.root, 'Next time, ask before you push.', sid=sid))
        self.nudge_of(self.prompt_hook(self.root, self.STRONG, sid='beef4567-other-session'))
        old = int(time.time()) - 600
        write(state, '%d\n' % old)
        os.utime(str(state), (old, old))
        self.nudge_of(self.prompt_hook(self.root, self.STRONG, sid=sid))

    def test_garbage_input_and_no_project_are_silent(self):
        before = {k: v for k, v in tree_state(self.mem).items() if not k.startswith('.checkpoints')}
        payloads = ['', '   ', '{not json', '[1, 2]', 'null', '"text"',
                    json.dumps({'prompt': 5, 'cwd': str(self.root)}),
                    json.dumps({'prompt': ['from now on'], 'cwd': str(self.root)}),
                    json.dumps({'prompt': 'from now on use tabs'}),
                    json.dumps({'prompt': 'from now on use tabs', 'cwd': str(self.work / 'nope')})]
        for payload in payloads:
            with self.subTest(payload=payload[:40]):
                self.assertSilent(self.hook('prompt', payload, cwd=self.home))
        raw = subprocess.run([sys.executable, str(PMEM), 'hook', 'prompt'], cwd=str(self.home),
                             env=dict(self.env, PWD=str(self.home)), input=b'\xff\xfe\x00{garbage',
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual((raw.returncode, raw.stdout), (0, b''))
        outside = self.work / 'no-memory'
        outside.mkdir()
        self.assertSilent(self.prompt_hook(outside, self.STRONG))
        after = {k: v for k, v in tree_state(self.mem).items() if not k.startswith('.checkpoints')}
        self.assertEqual(after, before)

    def test_ascii_json_for_a_non_ascii_project_path(self):
        root, mem = self.project('hp-项目')
        payload = json.dumps(self.prompt_payload(root, '以后回复都用中文'))     # ASCII on stdin
        result = self.hook('prompt', payload, cwd=self.home,
                           env=dict(self.env, PYTHONIOENCODING='latin-1'))
        context = self.nudge_of(result)                                    # asserts ASCII
        match = re.search(r'(\S+)/MEMORY\.md', context)
        self.assertIsNotNone(match, context)
        self.assertEqual(rp(match.group(1)), str(mem))

    def test_no_git_calls_for_prompts_without_a_signal(self):
        fake = self.base / 'fakebin'
        marker = self.base / 'git-was-called'
        self.write_executable(fake / 'git', '#!/bin/sh\necho "$@" >> %s\nexit 1\n'
                              % shlex.quote(str(marker)))
        env = dict(self.env, PATH=os.pathsep.join([str(fake), str(self.bin), '/usr/bin', '/bin']))
        for prompt in ('报错了', 'stop the server', 'Use pathlib instead of os.path in utils.py'):
            started = time.time()
            self.assertSilent(self.prompt_hook(self.root, prompt, env=env))
            self.assertLess(time.time() - started, 5)
        self.assertFalse(marker.exists(), 'git ran for a prompt without a signal')
        self.nudge_of(self.prompt_hook(self.root, self.STRONG, env=env))

    def test_hook_does_not_wait_for_stdin_eof(self):
        env = dict(self.env, PWD=str(self.home))
        proc = subprocess.Popen([sys.executable, str(PMEM), 'hook', 'prompt'], cwd=str(self.home),
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        try:
            proc.stdin.write(json.dumps(self.prompt_payload(self.root, self.STRONG)).encode())
            proc.stdin.flush()
            started = time.time()
            proc.wait(timeout=TIMEOUT)
            self.assertLess(time.time() - started, 10)
            out = proc.stdout.read().decode('utf-8', 'replace')
        finally:
            if proc.poll() is None:
                proc.kill()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()
            proc.wait()
        self.assertEqual(proc.returncode, 0)
        data = json.loads(out)
        self.assertEqual(data['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')


# =============================================================================================
# init wiring, status, lint, stable copy (spec 6, 10, 12)

class WiringStatusLintTests(PrefCase):
    def our_hooks(self, root):
        found = {}
        for event, group, hook in self.our_hook_commands(self.settings(root)):
            found.setdefault(event, []).append((group, hook))
        return found

    def test_init_registers_the_prompt_hook_with_timeouts(self):
        root, mem = self.project('wired')
        found = self.our_hooks(root)
        self.assertEqual(sorted(found), HOOK_EVENTS)
        for event, hooks in found.items():
            with self.subTest(event=event):
                self.assertEqual(len(hooks), 1)
                hook = hooks[0][1]
                self.assertEqual(hook.get('type'), 'command')
                self.assertEqual(hook.get('timeout'), 5 if event == 'UserPromptSubmit' else 10)
        command = found['UserPromptSubmit'][0][1]['command']
        self.assertIn('hook prompt', command)
        self.assertIn('2>/dev/null || true', command)
        self.assertTrue(command.rstrip().endswith(MARK), command)
        # the command works through a shell
        for prompt, nudged in ((PromptHookTests.STRONG, True), ('stop the server', False)):
            result = self.run_cmd(['/bin/sh', '-c', command], cwd=root,
                                  stdin=self.prompt_payload(root, prompt))
            self.assertEqual(result.returncode, 0, result.out)
            if nudged:
                self.nudge_of(result)
            else:
                self.assertSilent(result)
        settings = self.settings_path(root).read_bytes()
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(self.settings_path(root).read_bytes(), settings)
        # a missing prompt hook is reported and restored
        healthy = self.session_context(root)
        self.assertNotIn('UserPromptSubmit', healthy)
        data = self.settings(root)
        del data['hooks']['UserPromptSubmit']
        write(self.settings_path(root), json.dumps(data, indent=2))
        status = self.pmem('status', cwd=root, rc=None).out
        self.assertIn('UserPromptSubmit', status)
        self.assertIn('pmem init', status)
        self.assertIn('UserPromptSubmit', self.session_context(root))
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(sorted(self.our_hooks(root)), HOOK_EVENTS)
        self.pmem('init', '--no-history', '--no-hooks', cwd=root)
        self.assertEqual(self.our_hooks(root), {})
        self.assertNotIn(MARK, read_text(self.settings_path(root)))

    def test_status_preferences_line(self):
        ws, _ = self.project('st-ws')
        proj, _ = self.project('proj', parent=ws)
        for i in range(2):
            self.pref('add', 'Workspace rule number %d' % i, '--name', 'ws-%d' % i, cwd=ws)
        for i in range(3):
            self.pref('add', 'Project rule number %d' % i, '--name', 'p-%d' % i, cwd=proj)
        agents = self.codex_home / 'AGENTS.md'
        write(agents, self.wired(agents, '# Codex\n') + '\n' + sub_block('Global rule one') + '\n')
        self.assertEqual(self.pref_status(proj), (3, 2, 0, 1))

    def test_lint_preference_rules(self):
        root, mem = self.project('lint-p')
        self.pmem('lint', '--strict', cwd=root)
        clean = self.index(mem)
        clean_status = set(self.pmem('status', cwd=root, rc=None).out.splitlines())
        rule = 'Keep every reply short and plain and put the details in the memory body'

        def reset():
            for path in mem.glob('*.md'):
                if path.name not in ('MEMORY.md', 'HANDOFF.md'):
                    path.unlink()
            write(mem / 'MEMORY.md', clean)

        def entry_of_length(length):
            line = self.write_feedback(mem, 'long-entry', rule, title='T')
            line = line.replace('[T]', '[T' + 't' * (length - len(line)) + ']')
            self.assertEqual(len(line), length)
            return line

        def bare(count):
            return ['- Bare rule number %02d' % i for i in range(count)]

        def feedback_outside():
            write(mem / 'no-emoji.md', memfile('no-emoji', 'no emoji', type_='feedback'))
            self.append_lines(mem, '- [No emoji](no-emoji.md) — no emoji')

        def ordinary_inside():
            write(mem / 'style.md', memfile('style', 'see the style guide'))
            self.write_index(mem, prefs=['- [Style guide](style.md) — see the style guide'])

        cases = [
            # label, setup, plain rc, strict rc, regex expected in the lint output
            ('section missing', lambda: write(mem / 'MEMORY.md', MEMORY_TEMPLATE_10), 0, 1,
             r'(?im)^WARN:.*\bpreferences\b'),
            ('feedback outside', feedback_outside, 0, 1,
             r'(?m)^WARN:.*no-emoji.*pmem index --fix|^WARN:.*pmem index --fix.*no-emoji'),
            ('ordinary line inside', ordinary_inside, 0, 1,
             r'(?m)^WARN:.*index line inside Preferences'),
            ('bare rule', lambda: self.write_index(mem, prefs=['- Keep replies under ten lines']),
             0, 0, r'(?m)^INFO:.*no feedback file'),
            ('20 entries', lambda: self.write_index(mem, prefs=bare(20)), 0, 0, None),
            ('21 entries', lambda: self.write_index(mem, prefs=bare(21)), 0, 1, r'(?m)^WARN:'),
            ('200-char entry', lambda: self.write_index(mem, prefs=[entry_of_length(200)]), 0, 0,
             None),
            ('230-char entry', lambda: self.write_index(mem, prefs=[entry_of_length(230)]), 0, 1,
             r'(?m)^WARN:'),
        ]
        for label, setup, plain_rc, strict_rc, needle in cases:
            with self.subTest(case=label):
                setup()
                try:
                    result = self.pmem('lint', cwd=root, rc=None)
                    self.assertEqual(result.returncode, plain_rc, result.out)
                    strict = self.pmem('lint', '--strict', cwd=root, rc=None)
                    self.assertEqual(strict.returncode, strict_rc, strict.out)
                    if needle:
                        self.assertRegex(result.out, needle)
                    status = self.pmem('status', cwd=root, rc=None).out
                    hints = [line for line in status.splitlines() if line not in clean_status
                             and not STATUS_RE.match(line)]
                    if label == 'feedback outside':
                        self.assertTrue(any('index --fix' in line for line in hints), status)
                    if label == 'section missing':
                        self.assertTrue(any(re.search(r'(?i)\bpreferences\b', line)
                                            for line in hints), status)
                finally:
                    reset()

    def test_stable_copy_is_never_downgraded_and_mismatch_is_reported(self):
        root, _mem = self.project('stable')
        stable = self.xdg_data / 'project-memory' / 'pmem.py'
        source = read_text(PMEM)
        self.assertIn(VERSION, source)
        newer = source.replace(VERSION, '9.9.9')
        write(stable, newer)
        os.chmod(str(stable), 0o755)
        probe = self.run_cmd([sys.executable, str(stable), '--version'], cwd=self.home)
        self.assertEqual(probe.stdout.strip(), 'pmem 9.9.9', probe.out)
        self.assertNoTraceback(self.pmem('init', '--no-history', cwd=root))
        self.assertEqual(read_text(stable), newer)
        self.pmem('init', '--no-history', '--force', cwd=root)          # explicit downgrade
        self.assertEqual(stable.read_bytes(), PMEM.read_bytes())
        self.assertNotIn('1.0.0', self.pmem('status', cwd=root, rc=None).out)
        write(stable, source.replace(VERSION, '1.0.0'))
        self.assertIn('1.0.0', self.pmem('status', cwd=root, rc=None).out)
        self.assertIn('1.0.0', self.session_context(root))
        self.pmem('init', '--no-history', cwd=root)
        self.assertEqual(stable.read_bytes(), PMEM.read_bytes())
        self.assertNotIn('1.0.0', self.pmem('status', cwd=root, rc=None).out)


if __name__ == '__main__':
    unittest.main()
