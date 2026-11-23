"""project-memory 测试公共设施。

每个用例都在新的临时目录中运行：HOME、XDG_DATA_HOME、XDG_CONFIG_HOME、CODEX_HOME、
CLAUDE_CONFIG_DIR 全部重定向，临时根目录经由符号链接访问（覆盖 realpath 逻辑）。
pmem.py 只以子进程黑盒调用（sys.executable），测试不导入它的实现。
"""
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[1]                  # project-memory/
SKILL_DIR = REPO / 'skills' / 'project-memory'
PMEM = SKILL_DIR / 'scripts' / 'pmem.py'
TEMPLATES = SKILL_DIR / 'templates'
GIT = shutil.which('git')
HAS_GIT = GIT is not None
MARK = '# project-memory'
ISO_UTC = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z'
TEST_EMAIL = 'noreply' + '@' + 'example.com'
TIMEOUT = 60

# 继承自调用方、会让测试碰到真实配置或改变 git/agent 行为的变量。
_DROP_PREFIXES = ('GIT_', 'CLAUDE', 'CODEX_', 'XDG_')
_DROP_NAMES = {'PWD', 'OLDPWD', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'PYTHONSAFEPATH'}


def rp(path):
    """realpath as str."""
    return os.path.realpath(str(path))


def read_text(path):
    with open(str(path), 'rb') as handle:
        return handle.read().decode('utf-8', 'surrogateescape')


def write(path, data):
    """Write str (utf-8, no newline translation) or bytes; create parents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8', 'surrogateescape')
    path.write_bytes(data)
    return path


def frontmatter_block(text):
    match = re.match(r'\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)', text, re.S)
    return match.group(1) if match else None


def _unquote_yaml(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def fm_get(text, key):
    """First `key:` value in the frontmatter (any indentation), unquoted; None if absent."""
    block = frontmatter_block(text)
    if block is None:
        return None
    for line in block.splitlines():
        match = re.match(r'^\s*' + re.escape(key) + r':[ \t]*(.*?)\s*$', line)
        if match:
            return _unquote_yaml(match.group(1))
    return None


def body_of(text):
    match = re.match(r'\A\ufeff?---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', text, re.S)
    return text[match.end():] if match else text


def link_target(line):
    """First markdown link target of a line, percent-decoded, #anchor stripped."""
    match = re.search(r'\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)', line)
    if not match:
        return None
    return unquote(match.group(1).split('#', 1)[0])


def is_memory_link_line(line):
    target = link_target(line)
    return target is not None and re.match(r'^[^/:\\]+\.md$', target) is not None


def memory_link_lines(text):
    return [line for line in text.splitlines() if is_memory_link_line(line)]


def lines_linking(text, filename):
    return [line for line in text.splitlines() if link_target(line) == filename]


def tree_state(root, skip_names=('.git', '.pmem.lock')):
    """Snapshot a directory tree: relpath -> ('d',) | ('l', target) | ('f', bytes, mode).

    Entries named `.git` (dir or file) and lock files are skipped, so git's own index refreshes
    do not count as changes; callers compare git HEADs separately.
    """
    root = str(root)
    state = {}
    if not os.path.lexists(root):
        return state
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for name in sorted(dirnames):
            if name in skip_names:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(full):
                state[rel] = ('l', os.readlink(full))
            else:
                state[rel] = ('d',)
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name in skip_names:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(full):
                state[rel] = ('l', os.readlink(full))
            else:
                with open(full, 'rb') as handle:
                    state[rel] = ('f', handle.read(), stat.S_IMODE(os.lstat(full).st_mode))
    return state


def temp_leftovers(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d != '.git']
        found.extend(os.path.join(dirpath, f) for f in filenames if f.startswith('.pmem-tmp-'))
    return found


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


class PmemCase(TestCase):
    """Base class: hermetic environment + black-box helpers."""

    maxDiff = None

    def setUp(self):
        top = tempfile.mkdtemp(prefix='pmem-test-')
        self.addCleanup(_rmtree, top)
        real = os.path.join(top, 'real')
        os.mkdir(real)
        os.symlink(real, os.path.join(top, 'link'))
        self.top_real = rp(top)
        self.base = Path(top) / 'link'            # every path below goes through the symlink
        self.home = self.base / 'home'
        self.work = self.home / 'work'
        self.claude_cfg = self.base / 'claude-config'
        self.codex_home = self.base / 'codex-home'
        self.xdg_data = self.base / 'xdg-data'
        self.xdg_config = self.base / 'xdg-config'
        self.bin = self.base / 'bin'
        for path in (self.work, self.claude_cfg, self.codex_home, self.xdg_data,
                     self.xdg_config, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        # Hook commands call `python3`; make it the interpreter running the tests.
        self.write_executable(self.bin / 'python3',
                              '#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(_DROP_PREFIXES) and k not in _DROP_NAMES}
        env.update({
            'HOME': str(self.home),
            'XDG_DATA_HOME': str(self.xdg_data),
            'XDG_CONFIG_HOME': str(self.xdg_config),
            'CODEX_HOME': str(self.codex_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_cfg),
            'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', os.defpath),
            'GIT_CEILING_DIRECTORIES': os.pathsep.join([self.top_real, top]),
            'GIT_CONFIG_NOSYSTEM': '1',
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONDONTWRITEBYTECODE': '1',
        })
        self.env = env

    # -- generic helpers ---------------------------------------------------------------------

    @staticmethod
    def write_executable(path, text):
        write(path, text)
        os.chmod(str(path), 0o755)

    def describe(self, args, result):
        return 'pmem %s (cwd=%s) rc=%s\n--- stdout ---\n%s\n--- stderr ---\n%s' % (
            ' '.join(str(a) for a in args), getattr(result, 'cwd', '?'), result.returncode,
            result.stdout, result.stderr)

    def run_cmd(self, argv, cwd=None, stdin='', env=None):
        cwd = str(cwd if cwd is not None else self.home)
        run_env = dict(self.env if env is None else env)
        run_env['PWD'] = cwd
        if not isinstance(stdin, (str, bytes)):
            stdin = json.dumps(stdin)
        result = subprocess.run([str(a) for a in argv], cwd=cwd, env=run_env, input=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, encoding='utf-8', errors='replace',
                                timeout=TIMEOUT)
        result.cwd = cwd
        result.out = result.stdout + result.stderr
        return result

    def pmem(self, *args, cwd=None, stdin='', env=None, rc=0):
        """Run pmem.py; assert the exit code unless rc is None."""
        if not PMEM.is_file():
            self.fail('pmem.py not found at %s' % PMEM)
        result = self.run_cmd([sys.executable, str(PMEM)] + [str(a) for a in args],
                              cwd=cwd, stdin=stdin, env=env)
        if rc is not None:
            self.assertEqual(result.returncode, rc, self.describe(args, result))
        return result

    def hook(self, which, payload=None, cwd=None, extra=(), env=None):
        stdin = '' if payload is None else payload
        result = self.pmem('hook', which, *extra, cwd=cwd, stdin=stdin, env=env, rc=None)
        self.assertEqual(result.returncode, 0, self.describe(('hook', which), result))
        return result

    def context_of(self, result):
        """additionalContext of a SessionStart hook result (asserts the JSON shape)."""
        self.assertTrue(result.stdout.strip(), 'session-start printed nothing:\n' + result.out)
        data = json.loads(result.stdout)
        out = data['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'SessionStart')
        return out['additionalContext']

    def git(self, cwd, *args, rc=0):
        if not HAS_GIT:
            self.skipTest('git not installed')
        result = self.run_cmd([GIT] + [str(a) for a in args], cwd=cwd)
        if rc is not None:
            self.assertEqual(result.returncode, rc,
                             'git %s failed in %s:\n%s' % (' '.join(map(str, args)), cwd, result.out))
        return result

    def git_out(self, cwd, *args):
        return self.git(cwd, *args).stdout.strip()

    # -- git builders --------------------------------------------------------------------------

    def configure_repo(self, path):
        config = Path(path) / '.git' / 'config'
        with open(str(config), 'a') as handle:
            handle.write('[user]\n\tname = Test User\n\temail = %s\n[commit]\n\tgpgsign = false\n'
                         '[tag]\n\tgpgsign = false\n' % TEST_EMAIL)

    def make_repo(self, path, files=None, commit=True):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, '-c', 'init.defaultBranch=main', 'init', '-q')
        self.configure_repo(path)
        for name, data in (files if files is not None else {'README.md': '# test\n'}).items():
            write(path / name, data)
        if commit:
            self.git(path, 'add', '-A')
            self.git(path, 'commit', '-q', '-m', 'initial')
        return path

    def add_worktree(self, repo, path, branch):
        self.git(repo, 'worktree', 'add', '-q', '-b', branch, str(path))
        return Path(path)

    def make_bare(self, bare_path):
        """Bare clone of a fresh seed repo (branch `main` exists)."""
        seed = self.make_repo(self.base / ('seed-' + Path(bare_path).name))
        Path(bare_path).parent.mkdir(parents=True, exist_ok=True)
        self.git(self.base, 'clone', '-q', '--bare', str(seed), str(bare_path))
        return Path(bare_path)

    def bare_worktree(self, bare, path, branch, new=True):
        args = ['--git-dir=' + str(bare), 'worktree', 'add', '-q']
        args += (['-b', branch, str(path)] if new else [str(path), branch])
        self.git(self.base, *args)
        return Path(path)

    def repo_state(self, repo):
        """Outer-repo bits init may touch, plus HEAD."""
        git_dir = Path(repo) / '.git'
        exclude = git_dir / 'info' / 'exclude'
        return {
            'exclude': exclude.read_bytes() if exclude.exists() else None,
            'head': self.git_out(repo, 'rev-parse', 'HEAD'),
        }

    def mem_head(self, mem):
        if not (Path(mem) / '.git').exists():
            return None
        result = self.git(mem, 'rev-parse', 'HEAD', rc=None)
        return result.stdout.strip() if result.returncode == 0 else None

    def mem_status(self, mem):
        return self.git_out(mem, 'status', '--porcelain', '--untracked-files=all')

    def mem_subject(self, mem):
        return self.git_out(mem, 'log', '-1', '--format=%s')

    def mem_commits(self, mem):
        return int(self.git_out(mem, 'rev-list', '--count', 'HEAD'))

    def check_ignored(self, repo, relpath):
        return self.git(repo, 'check-ignore', '-q', relpath, rc=None).returncode == 0

    # -- project builders -----------------------------------------------------------------------

    def init_project(self, path, *init_args, git=False, cwd=None):
        """Create `path` (optionally as a git repo), run `pmem init`, return (root, mem realpath)."""
        path = Path(path)
        if git:
            self.make_repo(path)
        else:
            path.mkdir(parents=True, exist_ok=True)
        self.pmem('init', *init_args, cwd=cwd or path)
        mem = Path(rp(path / '.agent-memory'))
        self.assertTrue((mem / 'MEMORY.md').is_file())
        return path, mem

    def settings_path(self, root):
        return Path(root) / '.claude' / 'settings.local.json'

    def settings(self, root):
        return json.loads(read_text(self.settings_path(root)))

    @staticmethod
    def our_hook_commands(settings):
        """[(event, matcher group, hook dict)] for every hook carrying the project-memory marker."""
        found = []
        for event, groups in (settings.get('hooks') or {}).items():
            for group in groups or []:
                for hook in group.get('hooks') or []:
                    if MARK in str(hook.get('command', '')):
                        found.append((event, group, hook))
        return found

    def set_modified(self, path, iso, mtime=None):
        """Rewrite the first frontmatter `modified:` value (and optionally the file mtime)."""
        text = read_text(path)
        new, count = re.subn(r'(?m)^([ \t]*modified:).*$', lambda m: m.group(1) + ' ' + iso, text,
                             count=1)
        self.assertEqual(count, 1, 'no modified: line in %s' % path)
        write(path, new)
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))

    def set_goal(self, handoff, goal):
        text = read_text(handoff)
        self.assertIn('(no active task)', text)
        write(handoff, text.replace('(no active task)', goal, 1))

    def assertNoTempFiles(self, root):
        self.assertEqual(temp_leftovers(root), [])

    def wait_for(self, predicate, what, timeout=30.0):
        """Poll ``predicate`` (background work such as the session-start snapshot)."""
        deadline = time.time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.time() > deadline:
                self.fail('timed out waiting for ' + what)
            time.sleep(0.05)

    def wait_snapshot(self, mem, prefix):
        """Wait until the private history's last commit subject starts with ``prefix``."""
        def done():
            result = self.git(mem, 'log', '-1', '--format=%s', rc=None)
            return result.returncode == 0 and result.stdout.strip().startswith(prefix)
        self.wait_for(done, 'a snapshot "%s..." in %s' % (prefix, mem))
        # the background process may still be releasing its locks
        self.wait_for(lambda: not (Path(mem) / '.git' / 'index.lock').exists(), 'index.lock')
