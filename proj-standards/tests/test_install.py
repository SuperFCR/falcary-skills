"""install.sh tests in a throwaway HOME: default Claude dir, --codex, --antigravity, --config-dir, --target, --link-cli."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SKILLS = ROOT / "skills"
REPOGUARD_REL = Path("repo-hygiene", "scripts", "repoguard.py")
BACKUP_NAME = re.compile(r"^(?P<name>[a-z0-9-]+)-\d{14}(-\d+)?$")


def skill_names():
    return sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ps-install-")
        self.addCleanup(tmp.cleanup)
        real = Path(tmp.name, "real")
        real.mkdir()
        self.base = Path(tmp.name, "via-link")
        self.base.symlink_to(real, target_is_directory=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.claude = self.base / "claude-config"
        self.codex = self.base / "codex-home"
        self.antigravity = self.base / "antigravity-home"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "CODEX", "ANTIGRAVITY", "XDG_"))
        }
        env.update(
            HOME=str(self.home),
            CLAUDE_CONFIG_DIR=str(self.claude),
            CODEX_HOME=str(self.codex),
            ANTIGRAVITY_HOME=str(self.antigravity),
        )
        self.env = env

    def run_install(self, *args, env=None, script=INSTALL):
        return subprocess.run(
            ["bash", str(script), *args],
            env=env or self.env,
            cwd=str(self.base),
            capture_output=True,
            text=True,
            timeout=120,
        )

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
        self.assertEqual(sorted(p.name for p in skills_dir.iterdir()), names)
        for name in names:
            self.assertEqual(
                (skills_dir / name / "SKILL.md").read_bytes(),
                (SKILLS / name / "SKILL.md").read_bytes(),
            )
        self.assertTrue(os.access(str(skills_dir / REPOGUARD_REL), os.X_OK))

    def backups(self, skills_dir):
        bak = Path(str(skills_dir) + ".bak")
        return sorted(p.name for p in bak.iterdir()) if bak.is_dir() else []

    def test_installer_is_executable(self):
        self.assertTrue(os.access(str(INSTALL), os.X_OK))
        self.assertTrue(INSTALL.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n"))

    def test_default_codex_and_antigravity_targets(self):
        self.ok()
        self.assert_installed(self.claude / "skills")
        self.ok("--codex")
        self.assert_installed(self.codex / "skills")
        self.ok("--antigravity")
        self.assert_installed(self.antigravity / "skills")

        del self.env["CLAUDE_CONFIG_DIR"]
        self.ok()
        self.assert_installed(self.home / ".claude" / "skills")

    def test_config_dir_and_relative_target(self):
        first, second = self.base / "acct-a", self.base / "acct-b"
        self.ok("--config-dir", str(first), "--config-dir", str(second), "--config-dir", str(first) + "/")
        self.assert_installed(first / "skills")
        self.assert_installed(second / "skills")
        self.ok("--target", "agents/skills")
        self.assert_installed(self.base / "agents" / "skills")

    def test_existing_skill_is_backed_up_outside_scanned_dir(self):
        skills_dir = self.claude / "skills"
        (skills_dir / "git-branching").mkdir(parents=True)
        (skills_dir / "git-branching" / "SKILL.md").write_text("old\n", encoding="utf-8")
        (skills_dir / "unrelated").mkdir()
        (skills_dir / "unrelated" / "SKILL.md").write_text("keep\n", encoding="utf-8")
        self.ok()
        self.ok()
        self.assertEqual(
            sorted(p.name for p in skills_dir.iterdir()),
            sorted(skill_names() + ["unrelated"]),
        )
        backups = self.backups(skills_dir)
        for name in backups:
            self.assertRegex(name, BACKUP_NAME)
        ours = [n for n in backups if BACKUP_NAME.match(n).group("name") == "git-branching"]
        self.assertEqual(len(ours), 2)

    def test_refuses_to_install_over_source_and_skips_pycache(self):
        checkout = self.base / "checkout"
        shutil.copytree(str(ROOT), str(checkout), symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        cache = checkout / "skills" / "repo-hygiene" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "junk.pyc").write_bytes(b"\0")
        res = self.run_install("--target", str(checkout / "skills"), script=checkout / "install.sh")
        self.assertNotEqual(res.returncode, 0)
        self.ok("--target", str(self.base / "out"), script=checkout / "install.sh")
        self.assertEqual(list((self.base / "out").rglob("__pycache__")), [])

    def test_link_cli_and_usage_errors(self):
        self.refused("--bogus")
        self.refused("--codex", "--target", str(self.base / "x"))
        self.refused("--codex", "--antigravity")
        bindir = self.home / ".local" / "bin"
        result = self.ok("--link-cli")
        link = bindir / "repoguard"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(str(link)), os.path.realpath(str(self.claude / "skills" / REPOGUARD_REL)))
        self.assertIn("not on PATH", result.stderr)
        ver = subprocess.run([str(link), "--version"], env=self.env, capture_output=True, text=True)
        self.assertEqual(ver.returncode, 0, ver.stderr)
        self.assertRegex(ver.stdout.strip(), r"^repoguard \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
