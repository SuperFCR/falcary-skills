"""project-memory 1.1 "preferences", round 2: regression tests for the review findings C1-C15.

Each test reproduces a reported failure (inputs taken from the review repros) and checks the
round-2 behaviour. Black-box: pmem.py runs only as a subprocess in the hermetic environment of
tests/support.py (HOME, XDG_*, CODEX_HOME and CLAUDE_CONFIG_DIR redirected).

Run: cd project-memory && python3 -m unittest tests.test_prefs_round2
"""
import json
import os
import re
import time
import unittest
from pathlib import Path

try:
    from tests.support import body_of, fm_get, lines_linking, read_text, rp, tree_state, write
    from tests.test_prefs import (GIT_REQUIRED, MAIN_BEGIN, MAIN_END, MEMORIES_HEADING,
                                  MEMORY_TEMPLATE, MEMORY_TEMPLATE_10, NOT_ROOT, PREFS_HEADING,
                                  SUB_BEGIN, SUB_END, PrefCase, entries, inherited, memfile,
                                  pref_headings, rules, section, sub_block, sub_block_rules)
except ImportError:  # pragma: no cover - when run from inside tests/
    from support import body_of, fm_get, lines_linking, read_text, rp, tree_state, write
    from test_prefs import (GIT_REQUIRED, MAIN_BEGIN, MAIN_END, MEMORIES_HEADING,
                            MEMORY_TEMPLATE, MEMORY_TEMPLATE_10, NOT_ROOT, PREFS_HEADING,
                            SUB_BEGIN, SUB_END, PrefCase, entries, inherited, memfile,
                            pref_headings, rules, section, sub_block, sub_block_rules)

VIOLATION_LEAD = 'project-memory: the user says you broke an existing preference: '
CAPTURE_LEAD = 'project-memory: this message may state how the user wants you to work'
FENCE = '`' * 3


def fb(name, rule, body=None):
    return memfile(name, rule, body if body is not None else rule, 'feedback')


class Round2Case(PrefCase):
    def legacy(self, name, index, files=None):
        """A project whose MEMORY.md (and memory files) are replaced by hand-written ones."""
        root, mem = self.project(name)
        for fname, content in (files or {}).items():
            write(mem / fname, content)
        write(mem / 'MEMORY.md', index)
        return root, mem

    def lint(self, root):
        return self.pmem('lint', cwd=root, rc=None).out


# =============================================================================================
# C1: only canonical index lines are moved or removed; nothing is ever deleted as a duplicate

class CanonicalLineTests(Round2Case):
    # the exact MEMORY.md of the review repro (1.0 style, user prose linking a feedback memory)
    REPRO = ('# Project memory\n'
             '> Before writing anything, read [reply style](reply-style.md).\n'
             '- [HANDOFF — current state](HANDOFF.md) — read\n'
             '- [Reply style](reply-style.md) — insight first, numbers in tables\n'
             '- [Deadline](deadline.md) — due Dec 1\n'
             '## Notes\n'
             'When drafting the rebuttal, follow [reply style](reply-style.md) strictly; R2 '
             'complained.\n')
    ENTRY = '- [Reply style](reply-style.md) — insight first, numbers in tables'
    QUOTE = '> Before writing anything, read [reply style](reply-style.md).'
    NOTE = ('When drafting the rebuttal, follow [reply style](reply-style.md) strictly; R2 '
            'complained.')

    def assertKeepsEveryLine(self, before, after):
        lines = after.splitlines()
        for line in before.splitlines():
            if line.strip():
                self.assertIn(line, lines, after)

    def test_init_on_the_repro_index_moves_the_entry_and_deletes_nothing(self):
        root = self.work / 'pr'
        mem = root / '.agent-memory'
        write(mem / 'reply-style.md', fb('reply-style', 'insight first, numbers in tables'))
        write(mem / 'deadline.md', memfile('deadline', 'due Dec 1'))
        write(mem / 'MEMORY.md', self.REPRO)
        result = self.pmem('init', '--no-history', '--no-claude', cwd=root)
        self.assertNotIn('dropped', result.out)
        self.assertIn('moved 1 feedback line', result.out)
        self.assertIn('left 2 other references', result.out)
        text = self.index(mem)
        self.assertKeepsEveryLine(self.REPRO, text)
        self.assertEqual(entries(text), [self.ENTRY])
        self.assertOutsideSection(text, self.QUOTE)
        self.assertEqual(text.splitlines().index(self.QUOTE), 1)          # never moved
        lines = text.splitlines()
        self.assertLess(lines.index('## Notes'), lines.index(self.NOTE))
        self.assertEqual(len(lines_linking(text, 'reply-style.md')), 3)
        listing = self.pref('list', cwd=root).out
        self.assertIn('insight first, numbers in tables  [reply-style]', listing)
        lint = self.lint(root)
        self.assertEqual(len(re.findall(r'(?m)^INFO: MEMORY\.md:\d+: other reference to '
                                        r'reply-style\.md left in place', lint)), 2, lint)
        self.pmem('init', '--no-history', '--no-claude', cwd=root)
        self.assertEqual(self.index(mem), text)                           # idempotent
        self.pmem('index', '--fix', cwd=root)
        self.pref('add', 'Answer in English', cwd=root)
        self.pmem('add', 'feedback', 'no-emoji', '-d', 'Never use emoji', cwd=root)
        self.assertKeepsEveryLine(self.REPRO, self.index(mem))

    def test_unrelated_writes_never_delete_a_note_that_links_a_preference(self):
        root, mem = self.project('pr2')
        self.pref('add', 'Numbers go in tables, not prose', '--name', 'numbers-tables', cwd=root)
        note = ('- Paper deadline Dec 1; follow [numbers rule](numbers-tables.md) in every table '
                'caption too.')
        self.append_lines(mem, '## Notes', note)
        before = self.index(mem)
        for args in (('pref', 'add', 'Answer in English'), ('index', '--fix'),
                     ('add', 'feedback', 'no-emoji', '-d', 'Never use emoji'),
                     ('pref', 'violated', 'numbers-tables', '--rule',
                      'Numbers always go in tables, never in prose')):
            with self.subTest(command=' '.join(args)):
                result = self.pmem(*args, cwd=root)
                self.assertNotIn('dropped', result.out)
                text = self.index(mem)
                self.assertIn(note, text.splitlines())
                self.assertOutsideSection(text, note)
        self.assertKeepsEveryLine('\n'.join(l for l in before.splitlines()
                                            if 'numbers-tables' not in l), self.index(mem))

    def test_non_canonical_feedback_lines_are_left_in_place_and_reported(self):
        index = (MEMORY_TEMPLATE_10 + '* [fb star](fb-star.md) — Use pnpm\n'
                 '1. [fb num](fb-num.md) — No emojis\n'
                 'See [fb prose](fb-prose.md) for the reply style.\n')
        root, mem = self.legacy('n2', index, {'fb-star.md': fb('fb-star', 'Use pnpm'),
                                              'fb-num.md': fb('fb-num', 'No emojis'),
                                              'fb-prose.md': fb('fb-prose', 'Short replies')})
        result = self.pmem('init', '--no-history', cwd=root)
        self.assertNotIn('moved', result.out)
        text = self.index(mem)
        self.assertKeepsEveryLine(index, text)
        self.assertEqual(entries(text), [])
        for line in index.splitlines()[-3:]:
            self.assertOutsideSection(text, line)
        lint = self.lint(root)
        for name in ('fb-star', 'fb-num', 'fb-prose'):
            self.assertRegex(lint, r'(?m)^WARN: %s\.md: feedback memory %s has no canonical index '
                                   r'line .*add `- \[title\]\(%s\.md\) — rule` under ## '
                                   r'Preferences' % (name, name, name))
        self.assertIn('3 feedback memories without a `- [title](name.md) — rule` line',
                      self.pmem('status', cwd=root, rc=None).out)
        self.pref('violated', 'fb-star', '--rule', 'Always use pnpm', cwd=root, rc=2)
        self.pmem('index', '--fix', cwd=root)
        self.assertEqual(self.index(mem), text)

    def test_an_indented_feedback_line_is_not_moved_and_survives_pref_remove(self):
        root, mem = self.project('n1')
        self.pref('add', 'Answer in English', '--name', 'fb-a', cwd=root)
        write(mem / 'fb-nested.md', fb('fb-nested', 'Use tabs'))
        write(mem / 'topic.md', memfile('topic', 'group'))
        nested = '  - [fb nested](fb-nested.md) — Use tabs'
        self.append_lines(mem, '- [Topic](topic.md) — group', nested)
        self.pmem('index', '--fix', cwd=root)
        text = self.index(mem)
        lines = text.splitlines()
        self.assertEqual(lines[lines.index(nested) - 1], '- [Topic](topic.md) — group')
        self.pref('remove', 'fb-a', cwd=root)
        self.assertIn(nested, self.index(mem).splitlines())
        self.assertNotIn('unindexed', self.pmem('index', cwd=root, rc=None).out)
        # a link line nested under a preference entry is not deleted with that entry
        self.pref('add', 'Answer in German', '--name', 'fb-b', cwd=root)
        inner = '  - [fb nested again](fb-nested.md) — see also'
        detail = '  detail of the German rule'
        self.add_to_section(mem, inner, detail,
                            after='- [fb b](fb-b.md) — Answer in German')
        self.pref('remove', 'fb-b', cwd=root)
        lines = self.index(mem).splitlines()
        self.assertIn(inner, lines)
        self.assertNotIn(detail, lines)

    def test_non_canonical_entries_are_never_rewritten_or_removed(self):
        root, mem = self.project('nc')
        write(mem / 'tabs.md', fb('tabs', 'Use tabs'))
        odd = '- Use tabs, see [tabs](tabs.md)'
        self.write_index(mem, prefs=[odd])
        before = tree_state(mem)
        for args in (('violated', 'tabs', '--rule', 'Use tabs everywhere'), ('remove', 'tabs')):
            with self.subTest(command=args[0]):
                result = self.pref(*args, cwd=root, rc=2)
                self.assertIn('is not a `- [title](tabs.md) — rule` line', result.out)
                self.assertEqual(tree_state(mem), before)
        result = self.pref('add', 'Use tabs, really', '--name', 'tabs', cwd=root)
        self.assertIn('left it in place', result.out)
        text = self.index(mem)
        self.assertIn(odd, entries(text))
        self.assertIn('- [tabs](tabs.md) — Use tabs, really', entries(text))
        self.assertRegex(self.lint(root), r'(?m)^INFO: MEMORY\.md:\d+: other reference to tabs\.md')

    def test_index_fix_does_not_move_a_prose_line_that_links_the_handoff(self):
        index = ('# Project memory\n\n- [Fact](fact.md) — a fact\n'
                 'See [the handoff](HANDOFF.md) before you start.\n')
        root, mem = self.legacy('hp-prose', index, {'fact.md': memfile('fact', 'a fact')})
        result = self.pmem('index', '--fix', cwd=root)
        self.assertIn('note: left MEMORY.md:4 in place', result.out)
        self.assertEqual(self.index(mem), index)


# =============================================================================================
# C2: pref add --force keeps the body of an existing file

class ForceKeepsBodyTests(Round2Case):
    def test_force_on_a_user_memory_keeps_why_and_how(self):
        root, mem = self.project('bl')
        body = ('The user prefers terse replies.\n\n**Why:** they read on a phone during commutes.\n'
                '**How to apply:** max 5 lines unless code is needed.')
        self.pmem('add', 'user', 'reply-style', '-d', 'User likes terse replies', '--body', body,
                  cwd=root)
        result = self.pref('add', 'Keep replies under 5 lines', '--name', 'reply-style', cwd=root,
                           rc=2)
        self.assertIn('--force', result.out)
        self.pref('add', 'Keep replies under 5 lines', '--name', 'reply-style', '--force', cwd=root)
        text = read_text(mem / 'reply-style.md')
        self.assertEqual(fm_get(text, 'type'), 'feedback')
        self.assertEqual(fm_get(text, 'description'), 'Keep replies under 5 lines')
        got = body_of(text)
        self.assertNotIn('(not recorded)', got)
        for line in ('The user prefers terse replies.',
                     '**Why:** they read on a phone during commutes.',
                     '**How to apply:** max 5 lines unless code is needed.'):
            self.assertIn(line, got.splitlines())
        self.assertEqual(got.strip().splitlines()[0], 'Keep replies under 5 lines')
        self.assertEqual(rules(self.index(mem)), ['Keep replies under 5 lines'])
        # only the lines that are given are replaced
        self.pref('add', 'Keep replies under 6 lines', '--name', 'reply-style', '--force', '--how',
                  'count the lines', cwd=root)
        got = body_of(read_text(mem / 'reply-style.md'))
        self.assertIn('**Why:** they read on a phone during commutes.', got.splitlines())
        self.assertIn('**How to apply:** count the lines', got.splitlines())
        self.assertNotIn('max 5 lines unless code is needed', got)
        self.assertEqual(got.strip().splitlines()[0], 'Keep replies under 6 lines')

    def test_round_trip_and_violations_keep_the_body(self):
        root, mem = self.project('rt')
        self.pref('add', 'Use tabs in Makefiles', '--name', 'tabs', '--why', 'make needs tabs',
                  '--how', 'check the editor settings', cwd=root)
        self.pref('violated', 'tabs', '--rule', 'Always use tabs in Makefiles', '--note',
                  'used spaces', cwd=root)
        self.pref('remove', 'tabs', cwd=root)
        self.pref('add', 'Always use tabs in Makefiles', '--name', 'tabs', cwd=root, rc=2)
        self.pref('add', 'Always use tabs in Makefiles', '--name', 'tabs', '--force', cwd=root)
        got = body_of(read_text(mem / 'tabs.md'))
        for line in ('**Why:** make needs tabs', '**How to apply:** check the editor settings',
                     '**Violations:**'):
            self.assertIn(line, got.splitlines())
        self.assertRegex(got, r'(?m)^- \d{4}-\d{2}-\d{2}: used spaces$')
        self.assertEqual(got.count('Always use tabs in Makefiles'), 1)


# =============================================================================================
# C3: code fences

class FenceTests(Round2Case):
    def test_wire_stays_idempotent_after_an_unclosed_fence(self):
        path = self.work / 'g' / 'fence.md'
        write(path, '# rules\n\n%sbash\n# an example I never closed\nls\n\n' % FENCE)
        first = self.pmem('wire', path)
        self.assertIn('inserted block into', first.out)
        text = read_text(path)
        for _ in range(2):
            again = self.pmem('wire', path)
            self.assertRegex(again.out, r'(?m)^ok ')
            self.assertEqual(read_text(path), text)
        self.assertEqual(text.count(MAIN_BEGIN), 1)

    def test_markers_inside_a_code_block_are_refused(self):
        path = self.work / 'g' / 'fenced.md'
        content = '# rules\n%s\n%s\n%s\n' % (FENCE, MAIN_BEGIN, FENCE)
        write(path, content)
        result = self.pmem('wire', path, rc=1)
        self.assertIn('markers inside a code block', result.out)
        self.assertIn('fix by hand', result.out)
        self.assertEqual(read_text(path), content)
        # a preferences block hidden in a fence: promote and demote refuse, nothing written
        agents, claude = self.global_targets()
        hidden = read_text(claude) + '\n%s\n%s\n%s\n' % (FENCE, sub_block('Old rule'), FENCE)
        write(claude, hidden)
        root, _mem = self.project('fenced-proj')
        self.pref('add', 'Answer in English', '--name', 'english', cwd=root)
        before = (read_text(agents), read_text(claude))
        for args in (('promote', 'english'), ('demote', 'Old rule')):
            with self.subTest(command=args[0]):
                result = self.pref(*args, cwd=root, rc=1)
                self.assertIn('markers inside a code block', result.out)
                self.assertEqual((read_text(agents), read_text(claude)), before)
        self.pmem('wire', 'claude', rc=1)
        self.assertEqual(read_text(claude), hidden)

    def test_unclosed_fence_in_the_index_is_an_error_and_blocks_writes(self):
        root, mem = self.project('f2')
        text = self.index(mem).replace(MEMORIES_HEADING,
                                       'Example:\n%s\n- [x](x.md) — y\n\n%s' % (FENCE,
                                                                                MEMORIES_HEADING))
        write(mem / 'MEMORY.md', text)
        number = text.splitlines().index(FENCE) + 1
        lint = self.pmem('lint', cwd=root, rc=1)
        self.assertIn('ERROR: MEMORY.md: unclosed code fence at MEMORY.md:%d' % number, lint.out)
        self.assertIn('unclosed code fence at MEMORY.md:%d' % number,
                      self.pmem('index', cwd=root, rc=1).out)
        for _ in range(2):
            result = self.pref('add', 'Answer in English', cwd=root, rc=1)
            self.assertIn('never closes', result.out)
        self.pmem('index', '--fix', cwd=root, rc=1)
        self.assertEqual(self.index(mem), text)
        self.assertFalse((mem / 'answer-in-english.md').exists())

    def test_prompt_with_an_unclosed_fence_ignores_the_code(self):
        root, _mem = self.project('pf')
        payload = json.dumps(self.prompt_payload(root, 'what does this do?\n%s\nfrom now on use '
                                                       'tabs\n' % FENCE))
        self.assertSilent(self.hook('prompt', payload, cwd=self.home))


# =============================================================================================
# C4: section headings

class HeadingTests(Round2Case):
    def test_an_indented_heading_is_adopted(self):
        index = MEMORY_TEMPLATE_10 + '\n ## Preferences\n- [x](x.md) — y\n'
        root, mem = self.legacy('h3', index, {'x.md': fb('x', 'y')})
        self.pmem('init', '--no-history', cwd=root)
        text = self.index(mem)
        self.assertEqual(text, index)
        self.assertEqual(text.count('## Preferences'), 1)
        self.assertIn('y  [x]', self.pref('list', cwd=root).out)
        self.pref('add', 'Answer in English', cwd=root)
        self.assertEqual(self.index(mem).count('## Preferences'), 1)

    def test_an_existing_memories_heading_is_not_duplicated(self):
        index = MEMORY_TEMPLATE_10 + '\n## Memories\n- [x](x.md) — y\n- [z](z.md) — zz\n'
        root, mem = self.legacy('dm', index, {'x.md': fb('x', 'y'), 'z.md': memfile('z', 'zz')})
        self.pmem('init', '--no-history', cwd=root)
        text = self.index(mem)
        self.assertEqual(text.count(MEMORIES_HEADING), 1, text)
        self.assertEqual(len(pref_headings(text)), 1, text)
        self.assertEqual(entries(text), ['- [x](x.md) — y'])
        self.assertUnderMemories(text, '- [z](z.md) — zz')


# =============================================================================================
# C5, C9: pref demote / promote: matching, --only, --dry-run

class GlobalOptionTests(Round2Case):
    def test_demote_refuses_empty_short_and_ambiguous_rules(self):
        agents, claude = self.global_targets()
        root, _mem = self.project('dm-arg')
        self.pref('add', 'Rule A first', '--name', 'rule-a', cwd=root)
        self.pref('promote', 'rule-a', '--keep-local', cwd=root)
        before = (read_text(agents), read_text(claude))
        for arg in ('', '   ', 'e', 'Rul'):
            with self.subTest(rule=arg):
                self.pref('demote', arg, cwd=root, rc=2)
                self.assertEqual((read_text(agents), read_text(claude)), before)
        self.pref('add', 'Rule B second', '--name', 'rule-b', cwd=root)
        self.pref('promote', 'rule-b', '--keep-local', cwd=root)
        before = (read_text(agents), read_text(claude))
        result = self.pref('demote', 'Rule', cwd=root, rc=2)
        self.assertIn('matches several', result.out)
        self.assertEqual((read_text(agents), read_text(claude)), before)
        self.pref('demote', 'A first', cwd=root)
        self.assertEqual(sub_block_rules(read_text(agents)), ['Rule B second'])
        # CJK: two characters are enough
        self.pref('add', '回答用中文', '--name', 'zh', cwd=root)
        self.pref('promote', 'zh', '--keep-local', cwd=root)
        self.pref('demote', '中文', cwd=root)
        self.assertEqual(sub_block_rules(read_text(claude)), ['Rule B second'])

    def test_promote_only_and_dry_run_touch_only_the_named_files(self):
        agents, claude = self.global_targets()
        target = self.work / 'ws-claude' / 'CLAUDE.md'
        self.wired(target, '# workspace rules\n')
        root, mem = self.project('only')
        self.pref('add', 'When opening PRs: link the issue', '--name', 'pr-link', cwd=root)
        states = lambda: {p: read_text(p) for p in (agents, claude, target)}
        before, index = states(), self.index(mem)
        dry = self.pref('promote', 'pr-link', '--target', target, '--dry-run', cwd=root)
        for path in (agents, claude, target):
            self.assertMentionsPath(dry.out, path)
        self.assertIn('nothing written', dry.out)
        self.assertEqual((states(), self.index(mem)), (before, index))
        dry = self.pref('promote', 'pr-link', '--target', target, '--only', '--dry-run', cwd=root)
        self.assertMentionsPath(dry.out, target)
        self.assertNotIn(str(agents), dry.out)
        self.assertNotIn(str(claude), dry.out)
        self.assertEqual((states(), self.index(mem)), (before, index))
        result = self.pref('promote', 'pr-link', '--target', target, '--only', cwd=root)
        self.assertMentionsPath(result.out, target)
        self.assertMentionsPath(result.out, mem / 'MEMORY.md')
        after = states()
        self.assertEqual((after[agents], after[claude]), (before[agents], before[claude]))
        self.assertEqual(sub_block_rules(after[target]), ['When opening PRs: link the issue'])
        self.assertEqual(rules(self.index(mem)), [])
        # demote --only reads and writes only the named file
        self.pref('add', 'Answer in English', '--name', 'english', cwd=root)
        self.pref('promote', 'english', '--keep-local', cwd=root)
        result = self.pref('demote', 'link the issue', '--only', '--config-dir', self.claude_cfg,
                           cwd=root, rc=2)
        self.assertIn('no global preference matches', result.out)
        dry = self.pref('demote', 'link the issue', '--only', '--target', target, '--dry-run',
                        cwd=root)
        self.assertIn('- - When opening PRs: link the issue', dry.out)
        self.assertEqual(sub_block_rules(read_text(target)), ['When opening PRs: link the issue'])
        result = self.pref('demote', 'link the issue', '--only', '--target', target, cwd=root)
        self.assertMentionsPath(result.out, target)
        self.assertEqual(sub_block_rules(read_text(target)), [])
        self.assertEqual(sub_block_rules(read_text(claude)), ['Answer in English'])
        self.pref('promote', 'english', '--only', cwd=root, rc=2)       # --only needs a file

    def test_demote_to_project_names_the_memory_file(self):
        agents, _claude = self.global_targets()
        root, mem = self.project('dtp')
        self.pref('add', 'Answer in English', '--name', 'english', cwd=root)
        self.pref('promote', 'english', cwd=root)
        result = self.pref('demote', 'Answer in English', '--to-project', cwd=root)
        self.assertMentionsPath(result.out, mem / 'MEMORY.md')
        self.assertMentionsPath(result.out, agents)
        self.assertEqual(rules(self.index(mem)), ['Answer in English'])


# =============================================================================================
# C6: prompt-hook signals

class PromptSignalTests(Round2Case):
    # the 22 task prompts of the review (all fired with the first 1.1 signals)
    TASKS = ['改完以后跑一下测试', '提交以后告诉我结果', '跑完以后把日志贴给我', '每次运行都报错，帮我看看',
             'it crashes every time I run it', 'try again', 'run the tests again', 'again please',
             'The build failed again, can you look?', '默认用的是哪个端口？',
             'What port does it use by default?', 'Can you stop the server?',
             'Could you use pathlib instead?', "Why don't you check the logs first",
             '你看看这个函数是不是错了', '你刚才说过的那个方法再试一次', '你说过 X 是对的吗？',
             '把这些变量名统一成驼峰', 'set the expiry to a date in the future', '下次运行前先清缓存',
             'remember to commit after this', '不要急，先看日志']
    MUST_BE_SILENT = ['改完以后跑一下测试', '提交以后告诉我结果', '每次运行都报错',
                      'it crashes every time I run it', 'try again', 'run the tests again',
                      '默认用的是哪个端口？', 'What port does it use by default?', '你说过 X 是对的吗？',
                      'a date in the future']
    MUST_FIRE = ['I’d prefer you use pnpm', 'Don’t ever push to main',
                 'I don’t like it when you add docstrings', 'Stop using emojis.',
                 'Never use tabs in this repo.', "don't end your answers with a recap, I read the diff",
                 '又这样，太长了', '别再在结尾总结了，我看 diff', '以后回答都用中文',
                 'From now on, answer in Chinese', 'too long again',
                 # NFKC: full-width apostrophe, traditional characters, 以后 + 每次
                 'I＇d prefer short answers', '以後回答請用繁體中文', '記住：不要自動提交',
                 '以后每次提交前都跑测试', '回复请简洁']

    def setUp(self):
        super().setUp()
        self.root, self.mem = self.project('sig')

    def fires(self, prompt):
        return bool(self.prompt_hook(self.root, prompt).stdout.strip())

    def test_task_prompts_are_mostly_silent(self):
        for prompt in self.MUST_BE_SILENT:
            with self.subTest(prompt=prompt):
                self.assertSilent(self.prompt_hook(self.root, prompt))
        fired = [p for p in self.TASKS if self.fires(p)]
        self.assertLessEqual(len(fired), 3, fired)

    def test_lasting_preferences_fire(self):
        for prompt in self.MUST_FIRE:
            with self.subTest(prompt=prompt):
                context = self.nudge_of(self.prompt_hook(self.root, prompt))
                self.assertIn('✓ preference', context)

    def test_violation_markers_need_the_agent(self):
        self.write_index(self.mem, prefs=[self.write_feedback(self.mem, 'tables', 'No tables')])
        for prompt in ('I told you not to use tables, again', 'Again you used a table',
                       'you did it again!', '我跟你说过了，不要用表格', '我说过了，你别再用表格',
                       '我说过了不要用表格，又用了', '你又忘了'):
            with self.subTest(prompt=prompt):
                self.assertTrue(self.nudge_of(self.prompt_hook(self.root, prompt))
                                .startswith(VIOLATION_LEAD))
        for prompt in ('too long again', 'From now on, answer in Chinese', 'Stop using emojis.'):
            with self.subTest(prompt=prompt):
                self.assertTrue(self.nudge_of(self.prompt_hook(self.root, prompt))
                                .startswith(CAPTURE_LEAD))
        for prompt in ('Could you run the tests again?', 'The build failed again, can you look?',
                       '你刚才说过的那个方法再试一次'):
            with self.subTest(prompt=prompt):
                self.assertSilent(self.prompt_hook(self.root, prompt))


# =============================================================================================
# C7: violation nudge

class ViolationNudgeTests(Round2Case):
    FRENCH = 'Reply to the user in French, even when the user writes in English'
    TABLES = 'Never use Markdown tables in answers; use short paragraphs'

    def rules_part(self, context):
        self.assertTrue(context.startswith(VIOLATION_LEAD), context)
        end = context.index('. Sharpen that rule line')
        return context[len(VIOLATION_LEAD):end]

    def test_the_broken_rule_leads_ranked_by_shared_words(self):
        root, mem = self.project('vn')
        self.pref('add', self.FRENCH, '--name', 'french', cwd=root)
        self.pref('add', self.TABLES, '--name', 'no-tables', cwd=root)
        context = self.nudge_of(self.prompt_hook(root, 'I told you not to use tables, again'))
        part = self.rules_part(context)
        self.assertTrue(part.startswith('"%s"' % self.TABLES), part)        # ranked first, whole
        self.assertIn('"%s"' % self.FRENCH, part)
        self.assertIn('update its description', context)
        self.assertIn('**Violations:**', context)
        self.assertTrue(any('- %s: <what happened>' % day in context
                            for day in (time.strftime('%Y-%m-%d'),
                                        time.strftime('%Y-%m-%d', time.gmtime()))), context)
        self.assertNotIn('Otherwise ignore this note', context)
        self.assertIn('✓ preference: <rule>', context)                     # capture note follows
        self.assertLess(context.index('Sharpen that rule line'), context.index('✓ preference'))
        self.assertLessEqual(len(context) - len(part), 600)
        # CJK bigrams rank a Chinese rule (the real-run repro)
        self.pref('add', '回答不使用表格，直接用短段落', cwd=root)
        context = self.nudge_of(self.prompt_hook(root, '我说过了不要用表格，又用了'))
        self.assertTrue(self.rules_part(context).startswith('"回答不使用表格，直接用短段落"'), context)

    def test_topics_rank_across_languages(self):
        # the real-run repro: an English rule after a decoy, a Chinese complaint (and vice versa)
        root, mem = self.project('vn-topics')
        self.pref('add', self.FRENCH, '--name', 'french', cwd=root)
        self.pref('add', self.TABLES, '--name', 'no-tables', cwd=root)
        self.pref('add', '提交前先跑测试', '--name', 'test-first', cwd=root)
        part = self.rules_part(self.nudge_of(self.prompt_hook(root, '我说过了不要用表格，又用了')))
        self.assertTrue(part.startswith('"%s"' % self.TABLES), part)
        part = self.rules_part(self.nudge_of(self.prompt_hook(
            root, 'I told you to run the tests before every commit')))
        self.assertTrue(part.startswith('"提交前先跑测试"'), part)

    def test_long_paths_keep_the_base_within_600_and_the_rules_whole(self):
        for depth, label in ((1, '100-char'), (4, '203-char')):
            with self.subTest(path=label):
                parts = ['src', 'github.com', 'acme-corp', 'payments-platform', 'services',
                         'ledger-api-gateway'] + ['deep-directory-name-%02d' % i for i in range(depth * 3)]
                root = self.work.joinpath(*parts)
                root.mkdir(parents=True)
                _root, mem = self.project(root.name, parent=root.parent)
                self.assertGreater(len(str(mem)), 100)
                self.pref('add', self.FRENCH, '--name', 'french', cwd=root)
                self.pref('add', self.TABLES, '--name', 'no-tables', cwd=root)
                long_rule = 'When writing ' + 'very ' * 17 + 'long answers: stop'
                self.assertEqual(len(long_rule), 116)
                self.pref('add', long_rule, '--name', 'long', cwd=root)
                context = self.nudge_of(self.prompt_hook(root, 'you did it again, I told you'))
                part = self.rules_part(context)
                self.assertIn('"%s"' % self.FRENCH, part)
                self.assertIn('"%s"' % self.TABLES, part)
                self.assertIn('"%s"' % long_rule, part)            # <= 160 chars: shown whole
                self.assertLessEqual(len(context) - len(part), 600)
                self.assertIn('/.agent-memory/MEMORY.md', context)
                plain = self.nudge_of(self.prompt_hook(root, 'From now on, answer in Chinese'))
                self.assertLessEqual(len(plain), 600)
                self.assertTrue(plain.endswith('Otherwise ignore this note.'))
                self.assertIn('/.agent-memory/MEMORY.md', plain)

    def test_rules_are_cut_at_160_and_at_most_three_are_shown(self):
        root, mem = self.project('vn-cut')
        long_rule = 'Answer ' + 'x' * 200
        self.write_index(mem, prefs=[self.write_feedback(mem, 'long', long_rule)] + [
            self.write_feedback(mem, 'r%d' % i, 'Rule number %d' % i) for i in range(4)])
        part = self.rules_part(self.nudge_of(self.prompt_hook(root, '你又忘了')))
        shown = re.findall(r'"([^"]*)"', part)
        self.assertEqual(len(shown), 3, part)
        self.assertEqual(len(shown[0]), 160)
        self.assertTrue(shown[0].endswith('…'))

    def test_inherited_rules_are_candidates_and_labelled(self):
        ws, ws_mem = self.project('ws')
        proj, _mem = self.project('app', parent=ws)
        self.pref('add', 'Never use Markdown tables', '--name', 'no-tables', cwd=ws)
        self.pref('add', 'Answer in English', '--name', 'english', cwd=proj)
        part = self.rules_part(self.nudge_of(self.prompt_hook(proj, 'you used a table again')))
        self.assertTrue(part.startswith('"Never use Markdown tables" (from '), part)
        label = re.search(r'\(from ([^)]*)\)', part).group(1)
        self.assertTrue(str(ws_mem).endswith(label.lstrip('~…')) or label.startswith('~'), label)
        self.assertIn('"Answer in English"', part)

    def test_no_rules_points_to_the_section(self):
        root, mem = self.project('vn-none')
        context = self.nudge_of(self.prompt_hook(root, 'I told you to use tabs'))
        part = self.rules_part(context)
        self.assertTrue(part.startswith('see ## Preferences in '), part)
        self.assertTrue(part.endswith('/MEMORY.md'), part)


# =============================================================================================
# C8: pref violated --up and -C for inherited rules

class InheritedViolationTests(Round2Case):
    def test_up_and_dash_c_sharpen_the_outer_copy(self):
        ws, ws_mem = self.project('ws')
        child, mem = self.project('child', parent=ws)
        self.pref('add', 'Never use Markdown tables', '--name', 'no-tables', cwd=ws)
        result = self.pref('violated', 'no-tables', '--rule', 'Never use Markdown tables, not even '
                           'for comparisons', cwd=child, rc=2)
        self.assertIn('--up', result.out)
        self.assertIn('pmem -C', result.out)
        self.pref('violated', 'no-tables', '--up', '--rule', 'Never use Markdown tables, not even '
                  'for comparisons', cwd=child)
        self.assertEqual(rules(self.index(ws_mem)),
                         ['Never use Markdown tables, not even for comparisons'])
        self.assertEqual(rules(self.index(mem)), [])
        self.pmem('-C', ws, 'pref', 'violated', 'no-tables', '--rule', 'No Markdown tables at all',
                  cwd=child)
        self.assertEqual(rules(self.index(ws_mem)), ['No Markdown tables at all'])
        alone, _ = self.project('alone')
        result = self.pref('violated', 'x', '--up', '--rule', 'y', cwd=alone, rc=2)
        self.assertIn('no usable memory dir above', result.out)


# =============================================================================================
# C10: group-writable memory dirs (umask 002) and the session-start warning

@NOT_ROOT
class OwnershipFixTests(Round2Case):
    def test_init_drops_group_write_and_session_start_warns_about_skipped_ancestors(self):
        ws, ws_mem = self.project('ws')
        child, _mem = self.project('child', parent=ws)
        self.pref('add', 'WS-RULE use active voice', '--name', 'ws-rule', cwd=ws)
        os.chmod(str(ws_mem), 0o775)
        os.chmod(str(ws_mem / 'MEMORY.md'), 0o664)
        context = self.session_context(child)
        self.assertRegex(context, r'WARNING \(project memory\): outer memory \S+ is skipped, so its '
                                  r'preferences are not inherited: .*group- or world-writable')
        self.assertEqual(inherited(context), [])
        result = self.pmem('init', '--here', '--no-history', cwd=ws)
        self.assertEqual(len(re.findall(r'(?m)^chmod go-w ', result.out)), 2, result.out)
        self.assertEqual(os.stat(str(ws_mem)).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(str(ws_mem / 'MEMORY.md')).st_mode & 0o777, 0o644)
        context = self.session_context(child)
        self.assertNotIn('is skipped', context)
        self.assertEqual([r for r, _d, _l in inherited(context)], ['WS-RULE use active voice'])
        self.assertNotIn('chmod', self.pmem('init', '--here', '--no-history', cwd=ws).out)


# =============================================================================================
# C11, C12: index --fix and pmem's own writes

class IndexRepairTests(Round2Case):
    def test_dangling_preferences_are_always_kept_as_bare_rules(self):
        root, mem = self.project('dg')
        self.pref('add', 'Use tabs', '--title', 'Use tabs', cwd=root)
        self.pref('add', 'Answer in English', cwd=root)
        (mem / 'use-tabs.md').unlink()
        (mem / 'answer-in-english.md').unlink()
        result = self.pmem('index', '--fix', cwd=root)
        self.assertNotIn('removed dangling', result.out)
        self.assertIn('kept the preference "Use tabs"', result.out)
        self.assertEqual(entries(self.index(mem)), ['- Use tabs', '- Answer in English'])

    def test_handoff_line_inside_the_section_is_moved_above_it(self):
        index = ('# Project memory\n\n## Preferences\n'
                 '- [HANDOFF — current state](HANDOFF.md) — read\n- [x](x.md) — y\n\n## Memories\n')
        root, mem = self.legacy('hs', index, {'x.md': fb('x', 'y')})
        self.assertIn('index line inside Preferences', self.lint(root))
        result = self.pmem('index', '--fix', cwd=root)
        self.assertIn('moved the HANDOFF line out of ## Preferences', result.out)
        text = self.index(mem)
        lines = text.splitlines()
        self.assertLess(lines.index('- [HANDOFF — current state](HANDOFF.md) — read'),
                        lines.index(PREFS_HEADING))
        self.assertEqual(entries(text), ['- [x](x.md) — y'])
        self.assertNotIn('index line inside Preferences', self.lint(root))

    def test_add_force_with_another_type_moves_the_line_under_memories(self):
        root, mem = self.project('fz2')
        self.pref('add', 'Keep replies short', '--name', 'aa', cwd=root)
        result = self.pmem('add', 'project', 'aa', '-d', 'a fact now', '--force', cwd=root)
        self.assertIn('moved its index line from ## Preferences', result.out)
        text = self.index(mem)
        self.assertEqual(entries(text), [])
        self.assertUnderMemories(text, '- [aa](aa.md) — a fact now')
        self.assertNotRegex(self.lint(root), r'(?m)^WARN')

    def test_index_fix_escapes_brackets_in_rules(self):
        root, mem = self.project('brk-fix')
        self.pref('add', 'Use [brackets] in titles', '--name', 'brk', cwd=root)
        line = '- [brk](brk.md) — Use \\[brackets\\] in titles'
        self.assertEqual(entries(self.index(mem)), [line])
        write(mem / 'MEMORY.md', self.index(mem).replace(line + '\n', ''))
        self.pmem('index', '--fix', cwd=root)
        self.assertEqual(entries(self.index(mem)), [line])

    @GIT_REQUIRED
    def test_restore_escapes_brackets_in_rules(self):
        root = self.work / 'brk'
        root.mkdir()
        self.pmem('init', cwd=root)
        mem = Path(rp(root / '.agent-memory'))
        self.pref('add', 'Use [brackets] in titles', '--name', 'brk', cwd=root)
        line = '- [brk](brk.md) — Use \\[brackets\\] in titles'
        self.assertEqual(entries(self.index(mem)), [line])
        self.pmem('archive', 'brk', cwd=root)
        self.assertEqual(entries(self.index(mem)), [])
        self.pmem('restore', 'brk', cwd=root)
        self.assertEqual(entries(self.index(mem)), [line])


# =============================================================================================
# C13: global blocks of another pmem version

class BlockVersionTests(Round2Case):
    OLD = '%s\n## Project memory\n- an older block text\n%s\n' % (MAIN_BEGIN, MAIN_END)

    def test_status_flags_a_block_from_another_version(self):
        root, _mem = self.project('bv')
        agents = self.codex_home / 'AGENTS.md'
        claude = self.claude_cfg / 'CLAUDE.md'
        write(agents, '# Codex\n\n' + self.OLD)
        write(claude, '# Claude\n\n' + self.OLD)
        out = self.pmem('status', cwd=root, rc=None).out
        self.assertRegex(out, r'(?m)^WARN: the project-memory block in \S*AGENTS\.md is from another '
                              r'pmem version → pmem wire codex$')
        self.assertRegex(out, r'(?m)^WARN: the project-memory block in \S*CLAUDE\.md is from another '
                              r'pmem version → pmem wire claude')
        self.pmem('wire', 'codex')
        self.pmem('wire', 'claude')
        self.assertNotIn('another pmem version', self.pmem('status', cwd=root, rc=None).out)


# =============================================================================================
# C14, C15: description sync

class DescriptionSyncTests(Round2Case):
    def test_lint_reports_a_description_out_of_sync_and_an_unquoted_colon(self):
        root, mem = self.project('ds')
        self.pref('add', 'Never use Markdown tables in answers', '--name', 'no-tables', cwd=root)
        self.assertNotIn('out of sync', self.lint(root))
        text = self.index(mem)
        write(mem / 'MEMORY.md', text.replace(
            '— Never use Markdown tables in answers',
            '— Never use Markdown tables in any answer, even for comparisons'))
        self.assertRegex(self.lint(root), r'(?m)^INFO: no-tables\.md: description out of sync with '
                                          r'the rule at MEMORY\.md:\d+')
        write(mem / 'cs.md', '---\nname: cs\ndescription: When writing a commit message: English\n'
                             'metadata:\n  type: feedback\n---\n\nx\n')
        self.assertRegex(self.lint(root), r'(?m)^WARN: cs\.md: .*not quoted — quote it')
        write(mem / 'cs.md', '---\nname: cs\ndescription: "When writing a commit message: '
                             'English"\nmetadata:\n  type: feedback\n---\n\nx\n')
        self.assertNotRegex(self.lint(root), r'(?m)^WARN: cs\.md: .*not quoted')

    def test_violated_replaces_the_first_body_line(self):
        root, mem = self.project('vb')
        self.pref('add', 'Answer in short paragraphs', '--name', 'short-paras', cwd=root)
        new = 'When answering: at most 3 short paragraphs, no recap'
        self.pref('violated', 'short-paras', '--rule', new, '--note', 'x', cwd=root)
        text = read_text(mem / 'short-paras.md')
        self.assertEqual(fm_get(text, 'description'), new)
        self.assertEqual(body_of(text).strip().splitlines()[0], new)
        self.assertNotIn('Answer in short paragraphs', body_of(text))
        self.assertNotIn('out of sync', self.lint(root))


if __name__ == '__main__':
    unittest.main()
