"""Structure checks for proj-standards skills, manifests, README parity, Python 3.9 syntax, and privacy."""
import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SKILLS = ROOT / "skills"
REPOGUARD = SKILLS / "repo-hygiene" / "scripts" / "repoguard.py"
EXPECTED_SKILLS = {"git-branching", "git-committing", "git-worktree-sync", "repo-hygiene"}
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".txt", ""}

CJK = re.compile(r"[一-鿿]")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAIL = re.compile(r"noreply|@example\.(?:com|org|net)$|\.(?:invalid|test)$|^git@github\.com$", re.I)
HOME_PATH = re.compile(r"(?<![\w.-])/(home|Users)/([A-Za-z0-9._-]+)")
ALLOWED_HOME_USERS = {"u", "Shared"}
INLINE_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
YAML_INDICATORS = tuple("[]{}&*!|>'\"%@`,#") + ("- ", "? ", ": ")


def unquote_yaml(value):
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def frontmatter(path):
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    block, meta, i = lines[1:end], {}, 0
    while i < len(block):
        match = re.match(r"^([A-Za-z_][\w-]*):(?:[ \t]+(.*))?$", block[i])
        i += 1
        if not match:
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if raw[:1] in (">", "|"):
            parts = []
            while i < len(block) and (block[i][:1] in (" ", "\t") or not block[i].strip()):
                parts.append(block[i].strip())
                i += 1
            meta[key] = ((" " if raw[0] == ">" else "\n").join(p for p in parts if p), raw)
        else:
            meta[key] = (unquote_yaml(raw), raw)
    return meta


def without_code(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    kept, fence = [], None
    for line in text.split("\n"):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = None
            kept.append("")
            continue
        kept.append("" if fence else re.sub(r"``.*?``|`[^`]*`", "", line))
    return kept


def local_link_targets(path):
    found = []
    for number, line in enumerate(without_code(path.read_text(encoding="utf-8")), 1):
        targets = INLINE_LINK.findall(line)
        ref = REFERENCE_LINK.match(line)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            if any(c in target for c in "<>{}$"):
                continue
            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if target:
                found.append((number, target))
    return found


def skill_dirs():
    return sorted(p.parent for p in SKILLS.glob("*/SKILL.md"))


def text_files():
    roots = [p for p in ROOT.rglob("*") if p.is_file()]
    extra = [
        REPO / "README.md",
        REPO / ".claude-plugin" / "marketplace.json",
        REPO / ".github" / "workflows" / "proj-standards.yml",
    ]
    for path in roots + [p for p in extra if p.is_file()]:
        if "__pycache__" in path.parts or path.suffix not in TEXT_SUFFIXES:
            continue
        yield path


class SkillAndPackageTests(unittest.TestCase):
    def test_expected_skills_exist_and_valid_frontmatter(self):
        self.assertEqual({d.name for d in skill_dirs()}, EXPECTED_SKILLS)
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                skill = d / "SKILL.md"
                self.assertNotIn(b"\r", skill.read_bytes())
                meta = frontmatter(skill)
                self.assertIsNotNone(meta)
                name = meta.get("name", ("", ""))[0]
                desc = meta.get("description", ("", ""))[0]
                self.assertEqual(name, d.name)
                self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertGreater(len(desc), 40)
                self.assertLess(len(desc), 1024)
                self.assertRegex(desc, CJK)
                self.assertRegex(desc, r"[A-Za-z]{3,}")
                for key, (_, raw) in meta.items():
                    if raw and raw[0] not in "\"'>|":
                        self.assertFalse(raw.startswith(YAML_INDICATORS), f"{key}: quote it")
                        self.assertNotIn(": ", raw, f"{key}: quote ': '")
                        self.assertNotIn(" #", raw, f"{key}: quote ' #'")

    def test_skills_index_and_relative_links_resolve(self):
        index = (SKILLS / "README.md").read_text(encoding="utf-8")
        for name in EXPECTED_SKILLS:
            self.assertIn(f"({name}/SKILL.md)", index)
        for doc in ROOT.rglob("*.md"):
            if "__pycache__" in doc.parts:
                continue
            for number, target in local_link_targets(doc):
                with self.subTest(file=str(doc.relative_to(REPO)), line=number, target=target):
                    base = REPO if target.startswith("/") else doc.parent
                    self.assertTrue((base / target.lstrip("/")).exists())
        for number, target in local_link_targets(REPO / "README.md"):
            if target.startswith("proj-standards"):
                with self.subTest(file="README.md", line=number, target=target):
                    self.assertTrue((REPO / target).exists())

    def test_plugin_and_marketplace_manifests(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "proj-standards")
        self.assertRegex(plugin["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(plugin["license"], "MIT")
        self.assertEqual(plugin["author"]["name"], "SuperFCR")
        self.assertTrue(plugin["homepage"].endswith("/tree/main/proj-standards"))
        self.assertLess(len(plugin["description"]), 1024)
        for rel in plugin["skills"]:
            self.assertTrue((ROOT / rel).is_dir())
        names = [p["name"] for p in market["plugins"]]
        self.assertEqual(len(names), len(set(names)))
        entry = next(p for p in market["plugins"] if p["name"] == "proj-standards")
        self.assertEqual(entry["source"], "./proj-standards")
        self.assertEqual(entry["version"], plugin["version"])
        self.assertIn("proj-standards", market["metadata"]["description"])

    def test_repoguard_executable_version_and_py39(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue(REPOGUARD.is_file())
        self.assertTrue(os.access(str(REPOGUARD), os.X_OK))
        self.assertTrue(REPOGUARD.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n"))
        res = subprocess.run([sys.executable, str(REPOGUARD), "--version"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "repoguard " + plugin["version"])
        for path in sorted(ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            with self.subTest(file=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=(3, 9))

    def test_workflow_covers_package(self):
        wf = (REPO / ".github" / "workflows" / "proj-standards.yml").read_text(encoding="utf-8")
        for needle in ("proj-standards/**", "shellcheck", "install.sh", "macos-latest", "'3.9'"):
            self.assertIn(needle, wf)

    def test_readmes_share_structure(self):
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        def headings(txt):
            return [line.split(" ", 1)[0] for line in without_code(txt) if line.startswith("#")]
        self.assertEqual(headings(en), headings(zh))
        for txt in (en, zh):
            self.assertIn("```mermaid", txt)
            for needle in ("repoguard check-branch", "repoguard worktree-status", "repoguard check-stage", "repoguard suggest-version", *EXPECTED_SKILLS):
                self.assertIn(needle, txt)
        self.assertIn("(README.zh-CN.md)", en)
        self.assertIn("(README.md)", zh)

    def test_no_private_markers(self):
        for path in text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(file=str(path.relative_to(REPO))):
                emails = [m for m in EMAIL.findall(text) if not ALLOWED_EMAIL.search(m)]
                self.assertEqual(emails, [], "only noreply/example emails allowed")
                homes = ["/%s/%s" % m for m in HOME_PATH.findall(text) if m[1] not in ALLOWED_HOME_USERS]
                self.assertEqual(homes, [], "use /home/u or placeholder")


if __name__ == "__main__":
    unittest.main()
