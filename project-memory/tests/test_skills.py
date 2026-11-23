"""Structure checks for the skills, manifests and docs. No agent runs and nothing is installed."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]          # project-memory/
REPO = ROOT.parent                                   # the falcary-skills checkout
SKILLS = ROOT / 'skills'
PMEM = SKILLS / 'project-memory' / 'scripts' / 'pmem.py'
TEMPLATES = SKILLS / 'project-memory' / 'templates'
EXPECTED_SKILLS = {'project-memory', 'memory-learn', 'memory-reflect', 'memory-handoff',
                   'memory-health'}
TEXT_SUFFIXES = {'.md', '.py', '.sh', '.json', '.yml', '.yaml', '.txt', ''}

CJK = re.compile(r'[一-鿿]')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
ALLOWED_EMAIL = re.compile(r'noreply|@example\.(?:com|org|net)$|\.(?:invalid|test)$|^git@github\.com$',
                           re.I)
HOME_PATH = re.compile(r'(?<![\w.-])/(home|Users)/([A-Za-z0-9._-]+)')
ALLOWED_HOME_USERS = {'u', 'Shared'}
INLINE_LINK = re.compile(r'\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
REFERENCE_LINK = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)')
YAML_INDICATORS = tuple('[]{}&*!|>\'"%@`,#') + ('- ', '? ', ': ')


# -- small helpers ---------------------------------------------------------------------------
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
    """Top-level keys of a leading YAML frontmatter block -> {key: (value, raw)}; None if absent."""
    lines = path.read_text(encoding='utf-8').split('\n')
    if not lines or lines[0] != '---':
        return None
    try:
        end = lines.index('---', 1)
    except ValueError:
        return None
    block, meta, i = lines[1:end], {}, 0
    while i < len(block):
        match = re.match(r'^([A-Za-z_][\w-]*):(?:[ \t]+(.*))?$', block[i])
        i += 1
        if not match:
            continue
        key, raw = match.group(1), (match.group(2) or '').strip()
        if raw[:1] in ('>', '|'):
            parts = []
            while i < len(block) and (block[i][:1] in (' ', '\t') or not block[i].strip()):
                parts.append(block[i].strip())
                i += 1
            joiner = ' ' if raw[0] == '>' else '\n'
            meta[key] = (joiner.join(p for p in parts if p), raw)
        else:
            meta[key] = (unquote_yaml(raw), raw)
    return meta


def without_code(text):
    """Drop HTML comments, fenced code blocks and inline code spans."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    kept, fence = [], None
    for line in text.split('\n'):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = None
            kept.append('')
            continue
        kept.append('' if fence else re.sub(r'``.*?``|`[^`]*`', '', line))
    return kept


def local_link_targets(path):
    """(line number, target) for every relative link or image in a markdown file."""
    found = []
    for number, line in enumerate(without_code(path.read_text(encoding='utf-8')), 1):
        targets = INLINE_LINK.findall(line)
        ref = REFERENCE_LINK.match(line)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target) or target.startswith('#'):
                continue
            if any(c in target for c in '<>{}$'):
                continue  # placeholder, not a link
            target = unquote(target.split('#', 1)[0].split('?', 1)[0])
            if target:
                found.append((number, target))
    return found


def text_files():
    roots = [p for p in ROOT.rglob('*') if p.is_file()]
    extra = [REPO / 'README.md', REPO / '.claude-plugin' / 'marketplace.json',
             REPO / '.github' / 'workflows' / 'project-memory.yml']
    for path in roots + [p for p in extra if p.is_file()]:
        if '__pycache__' in path.parts or path.suffix not in TEXT_SUFFIXES:
            continue
        yield path


def skill_dirs():
    return sorted(p.parent for p in SKILLS.glob('*/SKILL.md'))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


# -- tests -----------------------------------------------------------------------------------
class SkillTests(unittest.TestCase):
    def test_expected_skills_exist(self):
        self.assertEqual({d.name for d in skill_dirs()}, EXPECTED_SKILLS)

    def test_frontmatter_names_and_descriptions(self):
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                skill = d / 'SKILL.md'
                self.assertNotIn(b'\r', skill.read_bytes(), 'use LF line endings')
                meta = frontmatter(skill)
                self.assertIsNotNone(meta, 'SKILL.md must start with a closed --- frontmatter')
                name = meta.get('name', ('', ''))[0]
                description = meta.get('description', ('', ''))[0]
                self.assertEqual(name, d.name)
                self.assertRegex(name, r'^(project-memory|memory-[a-z0-9]+(-[a-z0-9]+)*)$')
                self.assertLessEqual(len(name), 64)
                self.assertGreater(len(description), 40)
                self.assertLess(len(description), 1024)
                self.assertRegex(description, CJK, 'description needs Chinese text')
                self.assertRegex(description, r'[A-Za-z]{3,}', 'description needs English triggers')
                for key, (_, raw) in meta.items():
                    if raw and raw[0] not in '"\'>|':  # plain YAML scalars must stay parseable
                        self.assertFalse(raw.startswith(YAML_INDICATORS), f'{key}: quote it')
                        self.assertNotIn(': ', raw, f'{key}: quote a value containing ": "')
                        self.assertNotIn(' #', raw, f'{key}: quote a value containing " #"')

    def test_relative_links_resolve(self):
        docs = [p for p in ROOT.rglob('*.md')
                if TEMPLATES not in p.parents and '__pycache__' not in p.parts]
        self.assertTrue(docs)
        for doc in docs:
            for number, target in local_link_targets(doc):
                with self.subTest(file=str(doc.relative_to(REPO)), line=number, target=target):
                    base = REPO if target.startswith('/') else doc.parent
                    self.assertTrue((base / target.lstrip('/')).exists())
        root_readme = REPO / 'README.md'
        for number, target in local_link_targets(root_readme):
            if target.startswith('project-memory'):
                with self.subTest(file='README.md', line=number, target=target):
                    self.assertTrue((REPO / target).exists())

    def test_skills_explain_the_cli_fallback(self):
        core = (SKILLS / 'project-memory' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('project-memory/scripts/pmem.py', core)
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                self.assertIn('pmem', (d / 'SKILL.md').read_text(encoding='utf-8'))

    def test_templates_are_shipped(self):
        for name in ('MEMORY.md', 'HANDOFF.md', 'wire-block.md'):
            self.assertTrue((TEMPLATES / name).is_file(), name)
        handoff = (TEMPLATES / 'HANDOFF.md').read_text(encoding='utf-8')
        for placeholder in ('{now_utc}', '{by}', '{key}'):
            self.assertIn(placeholder, handoff)
        block = (TEMPLATES / 'wire-block.md').read_text(encoding='utf-8').strip().split('\n')
        self.assertEqual(block[0], '<!-- BEGIN project-memory -->')
        self.assertEqual(block[-1], '<!-- END project-memory -->')


class PackageTests(unittest.TestCase):
    def test_plugin_manifest(self):
        plugin = read_json(ROOT / '.claude-plugin' / 'plugin.json')
        self.assertEqual(plugin['name'], 'project-memory')
        self.assertRegex(plugin['version'], r'^\d+\.\d+\.\d+$')
        self.assertEqual(plugin['license'], 'MIT')
        self.assertEqual(plugin['author']['name'], 'SuperFCR')
        self.assertTrue(plugin['homepage'].endswith('/tree/main/project-memory'))
        self.assertLess(len(plugin['description']), 1024)
        self.assertTrue(plugin['skills'])
        for rel in plugin['skills']:
            self.assertTrue((ROOT / rel).is_dir(), rel)
        # Hooks are wired per project by `pmem init`, never by the plugin itself.
        self.assertNotIn('hooks', plugin)
        self.assertFalse((ROOT / 'hooks' / 'hooks.json').exists())

    def test_marketplace_entry(self):
        plugin = read_json(ROOT / '.claude-plugin' / 'plugin.json')
        market = read_json(REPO / '.claude-plugin' / 'marketplace.json')
        names = [p['name'] for p in market['plugins']]
        self.assertEqual(len(names), len(set(names)))
        entry = next(p for p in market['plugins'] if p['name'] == 'project-memory')
        self.assertEqual(entry['source'], './project-memory')
        self.assertEqual(entry['version'], plugin['version'])
        self.assertTrue(entry['description'])
        self.assertIn('project-memory', market['metadata']['description'])
        for other in market['plugins']:
            source = other['source']
            if isinstance(source, str) and source.startswith('./'):
                self.assertTrue((REPO / source).is_dir(), source)

    def test_pmem_is_an_executable_script_with_the_plugin_version(self):
        plugin = read_json(ROOT / '.claude-plugin' / 'plugin.json')
        self.assertTrue(PMEM.is_file())
        self.assertTrue(os.access(str(PMEM), os.X_OK), 'pmem.py must be mode 0755')
        self.assertTrue(PMEM.read_text(encoding='utf-8').startswith('#!/usr/bin/env python3\n'))
        with tempfile.TemporaryDirectory(prefix='pmem-version-') as home:
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(('CLAUDE', 'CODEX', 'XDG_'))}
            env.update(HOME=home, XDG_DATA_HOME=os.path.join(home, 'xdg'),
                       CODEX_HOME=os.path.join(home, 'codex'),
                       CLAUDE_CONFIG_DIR=os.path.join(home, 'claude'))
            result = subprocess.run([sys.executable, str(PMEM), '--version'], env=env, cwd=home,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'pmem ' + plugin['version'])

    def test_python_sources_parse_as_python_3_9(self):
        for path in sorted(ROOT.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            with self.subTest(file=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding='utf-8'), str(path), feature_version=(3, 9))

    def test_workflow_covers_the_package(self):
        workflow = (REPO / '.github' / 'workflows' / 'project-memory.yml').read_text(encoding='utf-8')
        for needle in ("project-memory/**", 'shellcheck', 'install.sh', 'macos-latest', "'3.9'"):
            self.assertIn(needle, workflow)

    def test_readmes_share_one_structure(self):
        english = (ROOT / 'README.md').read_text(encoding='utf-8')
        chinese = (ROOT / 'README.zh-CN.md').read_text(encoding='utf-8')
        def headings(text):
            return [line.split(' ', 1)[0] for line in without_code(text) if line.startswith('#')]
        self.assertEqual(headings(english), headings(chinese))
        for text in (english, chinese):
            self.assertIn('```mermaid', text)
            for needle in ('pmem init', 'pmem import --claude', 'pmem wire codex', '2.1.322', '0.164'):
                self.assertIn(needle, text)
            for name in EXPECTED_SKILLS:
                self.assertIn(name, text)
        self.assertIn('(README.zh-CN.md)', english)
        self.assertIn('(README.md)', chinese)

    def test_no_private_markers(self):
        for path in text_files():
            text = path.read_text(encoding='utf-8', errors='replace')
            with self.subTest(file=str(path.relative_to(REPO))):
                emails = [m for m in EMAIL.findall(text) if not ALLOWED_EMAIL.search(m)]
                self.assertEqual(emails, [], 'only noreply/example addresses may appear')
                homes = ['/%s/%s' % m for m in HOME_PATH.findall(text)
                         if m[1] not in ALLOWED_HOME_USERS]
                self.assertEqual(homes, [], 'use /home/u or a placeholder')


if __name__ == '__main__':
    unittest.main()
