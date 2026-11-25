"""Unit tests for repoguard.py covering Conventional Branch 1.1.0, Conventional Commits 1.0.0,
staged security/size checks, SemVer suggestions, and Git hook installation."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOGUARD = ROOT / "skills" / "repo-hygiene" / "scripts" / "repoguard.py"

SPEC = importlib.util.spec_from_file_location("repoguard", str(REPOGUARD))
repoguard = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(repoguard)


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "chore: initial commit")
    return path


class BranchTests(unittest.TestCase):
    OFFICIAL_FIXTURES = [
        ("main", True),
        ("master", True),
        ("develop", True),
        ("feature/add-login-page", True),
        ("feat/add-login-page", True),
        ("bugfix/fix-header-bug", True),
        ("fix/header-bug", True),
        ("hotfix/security-patch", True),
        ("release/v1.2.0", True),
        ("chore/upgrade-node-v20.11", True),
        ("chore/update-dependencies", True),
        ("feature/issue-123-new-login", True),
        ("chore/123", True),
        ("feature/a", True),
        ("ai/refactor-auth-flow", True),
        ("claude/security-patch", True),
        ("codex/optimize-query", True),
        ("copilot/add-login-page", True),
        ("cursor/fix-header-bug", True),
        ("Feature/Add-Login", False),
        ("Main", False),
        ("feature/new--login", False),
        ("feature/-new-login", False),
        ("feature/new-login-", False),
        ("feature/new..login", False),
        ("feature/.new", False),
        ("feature/new.", False),
        ("release/v1.-2.0", False),
        ("fix/header bug", False),
        ("fix/header_bug", False),
        ("unknown/some-task", False),
        ("feature", False),
        ("feature/", False),
        ("/add-login", False),
        ("feature/a/b", False),
    ]

    def test_official_conventional_branch_1_1_0_fixtures(self):
        for branch, expected in self.OFFICIAL_FIXTURES:
            with self.subTest(branch=branch):
                ok, reason = repoguard.validate_branch(branch)
                self.assertEqual(ok, expected, f"{branch}: {reason}")

    def test_custom_allowed_types_cli_and_config(self):
        ok, _ = repoguard.validate_branch("exp/ablation-v2", extra_types=["exp"])
        self.assertTrue(ok)
        with tempfile.TemporaryDirectory(prefix="rg-cfg-") as tmp:
            repo = init_repo(Path(tmp) / "repo")
            (repo / ".proj-standards.json").write_text(
                json.dumps({"allow_branch_types": ["exp"]}), encoding="utf-8"
            )
            res = subprocess.run(
                [sys.executable, str(REPOGUARD), "check-branch", "exp/ablation-v2"],
                cwd=str(repo),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_suggest_branch_name_produces_valid_names(self):
        dirty = [
            "Feature/Add_Login_Page",
            "fix/header bug ",
            "release/v1.-2.0--rc.",
            "unknown/some_task",
            "claude/feat/login",
            "just_a_raw_topic",
            "___",
            "Main",
        ]
        for raw in dirty:
            with self.subTest(raw=raw):
                sugg = repoguard.suggest_branch_name(raw)
                ok, reason = repoguard.validate_branch(sugg)
                self.assertTrue(ok, f"{raw!r} -> {sugg!r} invalid: {reason}")


class CommitTests(unittest.TestCase):
    def test_conventional_commits(self):
        valid = [
            "feat: add login",
            "feat(upstream): freeze immutable script bundle",
            "fix(cli/auth)!: drop legacy token fallback",
            "Merge pull request #12 from example/feat-x",
            'Revert "feat: add login"',
            "fixup! feat: add login",
        ]
        for msg in valid:
            with self.subTest(msg=msg):
                ok, reason, _ = repoguard.validate_commit_message(msg)
                self.assertTrue(ok, reason)

        invalid = [
            "",
            "# comment only\n",
            "updated stuff",
            "foo: unknown type",
            "feat(BadScope): upper scope",
            "feat:missing space",
            "feat:   ",
        ]
        for msg in invalid:
            with self.subTest(msg=msg):
                ok, _, _ = repoguard.validate_commit_message(msg)
                self.assertFalse(ok)

    def test_file_and_strict_long_header(self):
        with tempfile.TemporaryDirectory(prefix="rg-msg-") as tmp:
            msg_file = Path(tmp) / "COMMIT_EDITMSG"
            long_msg = "feat(core): " + ("x" * 75) + "\n# Please enter commit message\n"
            msg_file.write_text(long_msg, encoding="utf-8")

            ok_normal, _, warns = repoguard.validate_commit_message(
                msg_file.read_text(encoding="utf-8"), strict=False
            )
            self.assertTrue(ok_normal)
            self.assertTrue(warns)

            res_strict = subprocess.run(
                [sys.executable, str(REPOGUARD), "check-commit", "--file", str(msg_file), "--strict"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_strict.returncode, 1)


class StageAndHooksTests(unittest.TestCase):
    def test_check_stage_catches_private_files_secrets_and_large_weights(self):
        with tempfile.TemporaryDirectory(prefix="rg-stage-") as tmp:
            repo = init_repo(Path(tmp) / "repo")

            # Clean code change + .env.example should pass
            (repo / ".env.example").write_text("API_KEY=replace-me\n", encoding="utf-8")
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            git(repo, "add", ".env.example", "app.py")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertTrue(ok, rep)

            # CLAUDE.local.md staged -> fail
            (repo / "CLAUDE.local.md").write_text("# private\n", encoding="utf-8")
            git(repo, "add", "CLAUDE.local.md")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertFalse(ok)
            self.assertTrue(any("CLAUDE.local.md" in e for e in rep["errors"]))
            git(repo, "reset", "HEAD", "--", "CLAUDE.local.md")
            (repo / "CLAUDE.local.md").unlink()

            # .env staged -> fail; deleting committed .env -> allowed
            (repo / ".env").write_text("PLAIN=1\n", encoding="utf-8")
            git(repo, "add", ".env")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertFalse(ok)
            git(repo, "commit", "--no-verify", "-m", "chore: simulate committed env")
            git(repo, "rm", ".env")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertTrue(ok, rep)
            git(repo, "commit", "-m", "chore: remove env")

            # Secret token dynamically constructed -> fail; with allow-secret -> pass
            fake_tok = "ghp_" + "a" * 30
            (repo / "leak.py").write_text(f"TOKEN = '{fake_tok}'\n", encoding="utf-8")
            git(repo, "add", "leak.py")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertFalse(ok)
            self.assertTrue(any("GitHub" in e for e in rep["errors"]))

            (repo / "leak.py").write_text(f"TOKEN = '{fake_tok}'  # repoguard:allow-secret\n", encoding="utf-8")
            git(repo, "add", "leak.py")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertTrue(ok, rep)
            git(repo, "reset", "HEAD", "--", "leak.py")
            (repo / "leak.py").unlink()

            # Oversized weight artifact (> 1 MiB) -> fail
            (repo / "model.pt").write_bytes(b"\x00" * (1024 * 1024 + 4096))
            git(repo, "add", "model.pt")
            ok, rep = repoguard.check_staged(cwd=repo)
            self.assertFalse(ok)
            self.assertTrue(any("weight/model" in e for e in rep["errors"]))

    def test_install_hooks_worktree_safe_and_live_enforcement(self):
        with tempfile.TemporaryDirectory(prefix="rg-hooks-") as tmp:
            base = Path(tmp)
            repo = init_repo(base / "repo")
            wt = base / "wt"
            git(repo, "worktree", "add", "-b", "feat/hook-test", str(wt))

            hook_dir = Path(git(wt, "rev-parse", "--git-path", "hooks").stdout.strip())
            if not hook_dir.is_absolute():
                hook_dir = wt / hook_dir
            hook_dir.mkdir(parents=True, exist_ok=True)
            foreign = hook_dir / "pre-commit"
            foreign.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")

            # Refuse overwriting existing custom hook without --force
            ok, msg, _ = repoguard.install_hooks(["pre-commit", "commit-msg"], force=False, cwd=wt)
            self.assertFalse(ok)
            self.assertIn("Refusing", msg)

            # Install with --force
            ok, msg, written = repoguard.install_hooks(["pre-commit", "commit-msg", "pre-push"], force=True, cwd=wt)
            self.assertTrue(ok, msg)
            self.assertEqual(len(written), 3)

            # Live commit-msg hook rejects bad commit header and accepts valid header
            (wt / "ok.txt").write_text("hello\n", encoding="utf-8")
            git(wt, "add", "ok.txt")
            bad = git(wt, "commit", "-m", "bad commit msg", check=False)
            self.assertNotEqual(bad.returncode, 0)
            good = git(wt, "commit", "-m", "feat(hooks): verify live commit hook")
            self.assertEqual(good.returncode, 0, good.stderr)

            # Live pre-commit hook blocks CLAUDE.local.md
            (wt / "CLAUDE.local.md").write_text("secret note\n", encoding="utf-8")
            git(wt, "add", "CLAUDE.local.md")
            blocked = git(wt, "commit", "-m", "chore: should fail", check=False)
            self.assertNotEqual(blocked.returncode, 0)


class SemVerAndConfigTests(unittest.TestCase):
    def test_suggest_version_progression(self):
        with tempfile.TemporaryDirectory(prefix="rg-ver-") as tmp:
            repo = init_repo(Path(tmp) / "repo")
            git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")

            # fix -> patch
            (repo / "a.txt").write_text("1\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "fix: patch bug")
            ok, rep = repoguard.suggest_version(cwd=repo)
            self.assertTrue(ok, rep)
            self.assertEqual(rep["next_version"], "v0.1.1")
            self.assertEqual(rep["bump"], "patch")

            # feat -> minor
            (repo / "a.txt").write_text("2\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "feat(core): new api")
            ok, rep = repoguard.suggest_version(cwd=repo)
            self.assertTrue(ok, rep)
            self.assertEqual(rep["next_version"], "v0.2.0")
            self.assertEqual(rep["bump"], "minor")

            # BREAKING CHANGE footer -> major
            (repo / "a.txt").write_text("3\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "feat(core): change contract", "-m", "BREAKING CHANGE: drop old field")
            ok, rep = repoguard.suggest_version(cwd=repo)
            self.assertTrue(ok, rep)
            self.assertEqual(rep["next_version"], "v1.0.0")
            self.assertEqual(rep["bump"], "major")

    def test_print_config_outputs_valid_formats(self):
        toml_txt = repoguard.print_config("commit-check")
        self.assertIn("require_description_grammar = true", toml_txt)
        ruleset = json.loads(repoguard.print_config("github-ruleset"))
        self.assertEqual(ruleset["target"], "branch")
        starter = json.loads(repoguard.print_config("proj-standards"))
        self.assertIn("allow_branch_types", starter)


if __name__ == "__main__":
    unittest.main()
