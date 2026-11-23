"""install.sh in a throwaway HOME: copy, backup location, --codex, --config-dir, --link-cli, --wire.

HOME, XDG_DATA_HOME, CODEX_HOME and CLAUDE_CONFIG_DIR all point into a temporary directory that
is reached through a symlink; the real ~/.claude, ~/.codex and ~/.local are never touched.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'install.sh'
SKILLS = ROOT / 'skills'
PMEM_REL = Path('project-memory', 'scripts', 'pmem.py')
HAVE_PMEM = (SKILLS / PMEM_REL).is_file()
BEGIN = '<!-- BEGIN project-memory -->'
END = '<!-- END project-memory -->'
BACKUP_NAME = re.compile(r'^(?P<name>[a-z0-9-]+)-\d{14}(-\d+)?$')
needs_pmem = unittest.skipUnless(HAVE_PMEM, 'skills/project-memory/scripts/pmem.py is missing')


def skill_names():
    return sorted(p.parent.name for p in SKILLS.glob('*/SKILL.md'))


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pmem-install-')
        self.addCleanup(tmp.cleanup)
        real = Path(tmp.name, 'real')
        real.mkdir()
        self.base = Path(tmp.name, 'via-link')
        self.base.symlink_to(real, target_is_directory=True)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.claude = self.base / 'claude-config'
        self.codex = self.base / 'codex-home'
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('CLAUDE', 'CODEX', 'XDG_', 'PMEM'))}
        env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.base / 'xdg'),
                   CODEX_HOME=str(self.codex), CLAUDE_CONFIG_DIR=str(self.claude))
        self.env = env

    # -- helpers ---------------------------------------------------------------------------
    def run_install(self, *args, env=None, script=INSTALL):
        return subprocess.run(['bash', str(script), *args], env=env or self.env,
                              cwd=str(self.base), capture_output=True, text=True, timeout=120)

    def ok(self, *args, **kwargs):
        result = self.run_install(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def refused(self, *args, code=2):
        result = self.run_install(*args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def assert_installed(self, skills_dir):
        names = skill_names()
        self.assertTrue(names, 'no skills/*/SKILL.md in the checkout')
        self.assertEqual(sorted(p.name for p in skills_dir.iterdir()), names)
        for name in names:
            self.assertEqual((skills_dir / name / 'SKILL.md').read_bytes(),
                             (SKILLS / name / 'SKILL.md').read_bytes())
        if HAVE_PMEM:
            self.assertTrue(os.access(str(skills_dir / PMEM_REL), os.X_OK))

    def backups(self, skills_dir):
        bak = Path(str(skills_dir) + '.bak')
        return sorted(p.name for p in bak.iterdir()) if bak.is_dir() else []

    # -- targets ---------------------------------------------------------------------------
    def test_installer_is_executable(self):
        self.assertTrue(os.access(str(INSTALL), os.X_OK))
        self.assertTrue(INSTALL.read_text(encoding='utf-8').startswith('#!/usr/bin/env bash\n'))

    def test_default_target_is_the_claude_config_dir(self):
        result = self.ok()
        self.assert_installed(self.claude / 'skills')
        self.assertFalse((self.home / '.claude').exists())
        self.assertIn('init', result.stdout)
        self.assertIn('import --claude', result.stdout)
        del self.env['CLAUDE_CONFIG_DIR']
        self.ok()
        self.assert_installed(self.home / '.claude' / 'skills')

    def test_codex_target(self):
        self.ok('--codex')
        self.assert_installed(self.codex / 'skills')
        self.assertFalse(self.claude.exists())
        del self.env['CODEX_HOME']
        self.ok('--codex')
        self.assert_installed(self.home / '.codex' / 'skills')

    def test_config_dir_is_repeatable_and_deduplicated(self):
        first, second = self.base / 'acct-a', self.base / 'acct-b'
        self.ok('--config-dir', str(first), '--config-dir', str(second),
                '--config-dir', str(first) + '/')
        self.assert_installed(first / 'skills')
        self.assert_installed(second / 'skills')
        self.assertEqual(self.backups(first / 'skills'), [])
        self.assertFalse(self.claude.exists())
        self.ok('--config-dir', '~/acct-tilde')
        self.assert_installed(self.home / 'acct-tilde' / 'skills')

    def test_relative_target(self):
        self.ok('--target', 'agents/skills')
        self.assert_installed(self.base / 'agents' / 'skills')

    def test_existing_skill_is_moved_outside_the_scanned_dir(self):
        skills_dir = self.claude / 'skills'
        (skills_dir / 'project-memory').mkdir(parents=True)
        (skills_dir / 'project-memory' / 'SKILL.md').write_text('old\n', encoding='utf-8')
        (skills_dir / 'unrelated').mkdir()
        (skills_dir / 'unrelated' / 'SKILL.md').write_text('keep\n', encoding='utf-8')
        self.ok()
        self.ok()  # same second on fast machines: backup names must still be unique
        self.assertEqual(sorted(p.name for p in skills_dir.iterdir()),
                         sorted(skill_names() + ['unrelated']))
        self.assertEqual((skills_dir / 'unrelated' / 'SKILL.md').read_text(encoding='utf-8'),
                         'keep\n')
        backups = self.backups(skills_dir)
        for name in backups:
            self.assertRegex(name, BACKUP_NAME)
        ours = [n for n in backups if BACKUP_NAME.match(n).group('name') == 'project-memory']
        self.assertEqual(len(ours), 2)
        contents = sorted((skills_dir.parent / 'skills.bak' / n / 'SKILL.md').read_text(
            encoding='utf-8') for n in ours)
        self.assertIn('old\n', contents)
        # Run 1 backs up the old copy; run 2 backs up every skill of run 1; never "unrelated".
        self.assertEqual(len(backups), 1 + len(skill_names()))
        self.assertFalse(any(BACKUP_NAME.match(n).group('name') == 'unrelated' for n in backups))

    def test_refuses_to_install_over_its_own_source_and_skips_caches(self):
        checkout = self.base / 'checkout'
        shutil.copytree(str(ROOT), str(checkout), symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '.git'))
        cache = checkout / 'skills' / 'project-memory' / '__pycache__'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / 'junk.pyc').write_bytes(b'\0')
        before = sorted(p.name for p in (checkout / 'skills').iterdir())
        result = self.run_install('--target', str(checkout / 'skills'),
                                  script=checkout / 'install.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(p.name for p in (checkout / 'skills').iterdir()), before)
        self.ok('--target', str(self.base / 'out'), script=checkout / 'install.sh')
        self.assertEqual(list((self.base / 'out').rglob('__pycache__')), [])

    # -- options ---------------------------------------------------------------------------
    def test_usage_errors_change_nothing(self):
        self.refused('--bogus')
        self.refused('--target')
        self.refused('--config-dir')
        self.refused('--codex', '--target', str(self.base / 'x'))
        self.refused('--config-dir', str(self.base / 'x'), '--codex')
        self.refused('--target', str(self.base / 'x'), '--target', str(self.base / 'y'))
        result = self.refused('--target', str(self.base / 'x'), '--wire')
        self.assertIn('--wire', result.stderr)
        for name in ('x', 'y', 'claude-config', 'codex-home'):
            self.assertFalse((self.base / name).exists(), name)
        help_text = self.ok('--help').stdout
        self.assertIn('--config-dir', help_text)
        self.assertIn('--link-cli', help_text)

    @needs_pmem
    def test_link_cli_points_at_the_first_target_and_warns_about_path(self):
        bindir = self.home / '.local' / 'bin'
        bindir.mkdir(parents=True)
        (bindir / 'pmem').write_text('#!/bin/sh\necho mine\n', encoding='utf-8')
        first, second = self.base / 'acct-a', self.base / 'acct-b'
        result = self.ok('--config-dir', str(first), '--config-dir', str(second), '--link-cli')
        link = bindir / 'pmem'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(str(link)),
                         os.path.realpath(str(first / 'skills' / PMEM_REL)))
        kept = [p for p in bindir.iterdir() if p.name.startswith('pmem.bak-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '#!/bin/sh\necho mine\n')
        self.assertIn('not on PATH', result.stderr)
        version = subprocess.run([str(link), '--version'], env=self.env, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertRegex(version.stdout.strip(), r'^pmem \d+\.\d+\.\d+$')

        self.env['PATH'] = str(bindir) + os.pathsep + self.env.get('PATH', os.defpath)
        result = self.ok('--codex', '--link-cli')
        self.assertNotIn('not on PATH', result.stderr)
        self.assertEqual(os.path.realpath(str(link)),
                         os.path.realpath(str(self.codex / 'skills' / PMEM_REL)))
        self.assertEqual(len([p for p in bindir.iterdir() if p.name.startswith('pmem.bak-')]), 1)

    @needs_pmem
    def test_wire_codex_writes_one_block_into_agents_md(self):
        agents = self.codex / 'AGENTS.md'
        agents.parent.mkdir(parents=True)
        agents.write_text('# My global rules\n', encoding='utf-8')
        for _ in range(2):
            self.ok('--codex', '--wire')
            text = agents.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('# My global rules\n'))
            self.assertEqual(text.count(BEGIN), 1)
            self.assertEqual(text.count(END), 1)
        self.assertFalse(self.claude.exists())

    @needs_pmem
    def test_wire_claude_writes_each_config_dir(self):
        first, second = self.base / 'acct-a', self.base / 'acct-b'
        self.ok('--config-dir', str(first), '--config-dir', str(second), '--wire')
        for cfg in (first, second):
            text = (cfg / 'CLAUDE.md').read_text(encoding='utf-8')
            self.assertEqual(text.count(BEGIN), 1)
            self.assertEqual(text.count(END), 1)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.codex.exists())
        self.ok('--wire')
        self.assertEqual((self.claude / 'CLAUDE.md').read_text(encoding='utf-8').count(BEGIN), 1)


if __name__ == '__main__':
    unittest.main()
