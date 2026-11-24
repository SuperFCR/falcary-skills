# project-memory

**English** | [简体中文](README.zh-CN.md)

**Memory that lives in the project, not in the agent.** Claude Code, Codex and every account you use read and write one `.agent-memory/` folder, so you can resume a project or switch agents at any moment, even when a session dies on a usage limit. A preference you state once is followed by every agent in every later session.

## The problem

Claude Code keeps its auto-memory under `<config-dir>/projects/<slug of the launch path>/memory/`. That location is tied to one agent, one config dir or account, one machine path and one launch path: Codex never sees it, a second account starts empty, and every worktree or sub-directory grows its own separate memory. When a session stops on a usage limit and you switch to Codex or another account, the new session starts blind. project-memory moves the memory into the project itself and teaches every agent to read it first.

## How it works

```mermaid
flowchart LR
    subgraph Project["your project"]
        MEM[".agent-memory/<br/>MEMORY.md · HANDOFF.md<br/>one fact per file"]
    end
    CC["Claude Code<br/>any config dir or account"] <-->|"autoMemoryDirectory<br/>+ SessionStart / Stop / prompt hooks"| MEM
    CX["Codex CLI"] <-->|"AGENTS.md block:<br/>pmem context, or read HANDOFF + MEMORY"| MEM
    OT["other agents"] <-->|"pmem context"| MEM
```

- **Claude Code.** `pmem init` writes `autoMemoryDirectory` into the project's `.claude/settings.local.json`, so Claude's native memory loads `MEMORY.md` from `.agent-memory/` and saves new memories there. The same file adds hooks: `SessionStart` injects the current handoff, any checkpoint that needs attention and the preferences inherited from outer folders; `Stop`, `StopFailure` and `PreCompact` write a mechanical checkpoint; `UserPromptSubmit` nudges the agent when a message looks like a lasting preference. The settings file lives in the project, so every config dir and account picks it up.
- **Codex.** `pmem wire codex` adds a short block to `${CODEX_HOME:-~/.codex}/AGENTS.md`: find the nearest `.agent-memory/`, run `pmem context` (or read `HANDOFF.md`, a newer checkpoint and `MEMORY.md`), follow every line under `## Preferences`, save new preferences by editing the files, and stamp `metadata.modified` on every edit.
- **Anything else.** `pmem context` prints the handoff, the checkpoint, the inherited preferences and the index in one go.
- **Five skills** tell the agent when to read, save, hand off and clean up. `pmem`, a single-file Python CLI for Python 3.9 and newer with only the standard library, does the bookkeeping.

## Install

You need Python 3.9 or newer and Git. Pick one way per agent.

**Claude Code plugin**, inside Claude Code:

```text
/plugin install project-memory --marketplace SuperFCR/falcary-skills
```

On older Claude Code versions use two steps: `/plugin marketplace add SuperFCR/falcary-skills`, then `/plugin install project-memory@falcary-skills`.

**Copy install**, for Claude Code, Codex or any agent that reads `SKILL.md` folders:

```bash
git clone https://github.com/SuperFCR/falcary-skills.git && cd falcary-skills/project-memory
./install.sh --link-cli              # → ${CLAUDE_CONFIG_DIR:-~/.claude}/skills, plus ~/.local/bin/pmem
./install.sh --codex --wire          # → ${CODEX_HOME:-~/.codex}/skills, plus the AGENTS.md block
./install.sh --config-dir ~/.claude-work --config-dir ~/.claude-personal   # several accounts
./install.sh --target DIR            # any other skills directory
```

An existing skill with the same name is moved to `<target>.bak/<name>-<time>`, outside the directory agents scan. `--wire` runs `pmem wire codex` for `--codex` and `pmem wire claude` for each Claude config dir.

**Once per Claude config dir.** Claude Code keeps plugins, skills and the global `CLAUDE.md` per config dir (`~/.claude` or `$CLAUDE_CONFIG_DIR`). If you switch accounts by switching config dirs, install the plugin in each one, or pass every dir to `install.sh --config-dir`. The project wiring is shared: it lives in the project, and the hook script is a stable copy at `${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py`, outside any config dir. After updating the plugin or skills, run `pmem init` with the updated `pmem.py` from the skill folder: one run in any project refreshes that copy, and a run in each project adds whatever the new version wires there. Coming from 1.0, follow [Upgrading from 1.0](#upgrading-from-10).

**The `pmem` command.** `--link-cli` puts it on your PATH; the plugin does not. Plugin users can ask the agent to "set up project memory"; it runs `pmem.py` from the skill folder. After the first `pmem init`, link the stable copy with `ln -s "${XDG_DATA_HOME:-$HOME/.local/share}/project-memory/pmem.py" ~/.local/bin/pmem`; Claude sessions also name that copy in their injected context (`CLI: python3 '…'`). Without a link, `pmem` below means `python3 <skills dir>/project-memory/scripts/pmem.py`.

## Quick start

Once per project. In a Git repository run it anywhere: it lands in the main checkout root. Elsewhere, run it in the project's top folder or pass `-C <root>`:

```bash
pmem init              # create .agent-memory/, private history, Claude wiring
pmem import --claude   # bring over Claude's old per-path memories; try --dry-run first
pmem wire codex        # once per machine: tell Codex where to look
pmem status            # check the wiring
```

Start a new Claude Code session afterwards; settings are read at startup. Notes kept in `CLAUDE.local.md` can come along with `pmem import --file CLAUDE.local.md`. Memories Claude kept for parent dirs, a user-level `autoMemoryDirectory` or sessions launched in sub-directories are listed as candidates, not imported; sub-directory ones are confirmed by the `cwd` in their transcripts. Bring each in with `pmem import <dir>` after a look. `init` is idempotent, so running it again only repairs what is missing.

## Daily use

You rarely call the skills by name. They fire on what happens in the conversation:

| Skill | Fires when | You might say |
| --- | --- | --- |
| [project-memory](skills/project-memory/SKILL.md) | Session start or resume, before debugging an error, any memory question; also setup and migration | "set up project memory", "where is the memory" |
| [memory-learn](skills/memory-learn/SKILL.md) | Right after you state a preference or correct the agent, a confirmed unusual approach, a fix that took 3+ attempts, a new constraint | "remember this", "from now on…", "I told you already" |
| [memory-reflect](skills/memory-reflect/SKILL.md) | End of session or before a commit: sweeps the whole conversation, your preferences first | "done for today", "wrap up" |
| [memory-handoff](skills/memory-handoff/SKILL.md) | Milestones, before long or remote jobs, before stopping, when switching agents | "hand off", "continue the last task", "switch to Codex" |
| [memory-health](skills/memory-health/SKILL.md) | Weekly, or when `pmem lint` warns: verify, merge, split, archive, shrink the index, review the preferences | "memory health", "clean up the memory" |

A typical day: the agent reads the handoff at session start, saves lessons and your preferences as they happen, updates the handoff at each milestone, and runs a reflect sweep at the end. Agents act on their own for low-risk changes such as a new memory, a preference you just stated or their own handoff; report rewrites of existing memories and sharpened preferences; and ask before archiving a memory that still holds unique content or backs a preference, removing or weakening a preference or rule, changing the global preferences, or touching `CLAUDE.md` / `AGENTS.md`, `pmem wire` included. Nothing is ever deleted: `pmem archive` moves files to `.archive/`, and that is also what "forget this" does. Dropping only the index line is not enough, because `pmem index --fix` would add it back.

## Preferences: agents that learn how you work

Say once how you want the agent to work, and every later session follows it, in Claude Code and Codex, on every account. A preference is an ordinary `feedback` memory whose index line sits under `## Preferences` in `MEMORY.md`, the part of the memory that is in context from the first turn of every session. When an agent breaks a preference, the line itself gets sharper; a counter would change nothing, because the line is all the model sees.

| Step | What happens |
| --- | --- |
| 1. You say it | "From now on, answer in Chinese", "too long again", "don't end your answers with a recap, I read the diff". In Claude Code a `UserPromptSubmit` hook spots such short messages and adds a note asking the agent whether this is a lasting preference. |
| 2. The agent saves it | It edits the files directly, which in Claude Code needs no permission prompt for the project's own memory: `- [<title>](<name>.md) — <rule>` under `## Preferences`, and `<name>.md` as a `feedback` memory with **Why:** and **How to apply:**. `pmem pref add` does the same in one command. The same reply already follows the rule and still does the rest of your message. |
| 3. You see it | The reply ends with `✓ preference: <rule>`. Reject it and the agent archives the rule (`pmem archive`); if only the wording is off, tell it how to put it. |
| 4. Every later session | Claude Code loads `MEMORY.md` natively; Codex and other agents get it through `pmem context`. Projects inside a workspace also inherit the preferences of the `.agent-memory/` folders above them. |
| 5. It gets broken | "I told you already", "you did it again": the agent rewrites the rule more precisely, with the situation and a counter-example, updates the file's description, moves the rule to the top and logs the date under **Violations:** in its file (`pmem pref violated`). The hook's note names the rules you most likely mean. A rule inherited from an outer folder is sharpened there (`pmem pref violated --up`), never copied into the project. |
| 6. It holds everywhere | Only with your consent, `pmem pref promote` copies the rule into a preferences block of your global `CLAUDE.md` and `AGENTS.md`, which every project sees. The agent names every file it would change (`--dry-run` lists them), and `--only --target FILE` limits it to the files you pick. |

**What counts as a preference.** A standing rule about the agent's behaviour that applies in most sessions of the project before any particular file is opened: language, tone, length, format, workflow and approval steps, tools you insist on. A fact about the code or environment that matters only when the agent touches that area is an ordinary memory, and an instruction for the current task is neither.

| You say | It is | Recorded as |
| --- | --- | --- |
| "Stop ending every answer with a recap, I read the diff" | Preference | `- [no recap](no-trailing-recap.md) — Don't end replies with a recap of what you did; the user reads the diff` |
| "Integration tests need Redis running first" | Project fact | An ordinary `project` memory under `## Memories` |
| "Only touch app.py this time" | Task instruction | Nothing; the agent just follows it |

```markdown
- [HANDOFF — current state](HANDOFF.md) — read before any task at session start; update at milestones

## Preferences
> How the user wants you to work: follow every line, every session; on conflict this project beats outer folders and the global list. …

- [no recap](no-trailing-recap.md) — Don't end replies with a recap of what you did; the user reads the diff
- [commit style](commit-style.md) — When writing a commit message: English, imperative mood, no emoji

## Memories
- [tests need Redis](tests-need-redis.md) — Integration tests need Redis on port 6379
```

Each rule is one imperative line; a rule that holds only in some situations starts with its trigger, `When <situation>: <rule>`, and details go into the file body. Such a rule contains `: `, so the `description` in its file must be quoted; `pmem lint` warns otherwise.

**Scope and precedence.** The agent asks itself whether the rule would hold in the sibling projects of the workspace, and records it at the narrowest layer that fits:

| Layer | Where | Written by |
| --- | --- | --- |
| This project | `## Preferences` in the project's `MEMORY.md` | The agent, by default |
| Workspace | `## Preferences` in each `.agent-memory/` above the project's own one (a workspace whose repositories keep their own memories). If the repositories share the workspace memory, as after `pmem init` in the top folder, its section already covers them all | `pmem pref add --up`, when you said the rule applies broadly; such rules should start with `When …:` (a convention; pmem does not check it). Writing an outer memory may need your approval |
| Every project | The `project-memory:preferences` block after the project-memory block in your global `CLAUDE.md` and `AGENTS.md` | `pmem pref promote`, only with your consent |

On conflict the nearer one wins: the project, then the nearest outer folder, then the global block. Claude sessions get the outer folders' rules at session start under `## Inherited preferences`; other agents get them from `pmem context`. An outer memory counts only if you own it and its `MEMORY.md`, neither is group- or world-writable, and no directory on the way is world-writable without the sticky bit, so a stray `/tmp/.agent-memory` from another user injects nothing; `$HOME` and `/` are never used. `pmem pref list`, `pmem status` and the session start name the folders they skipped and why; `pmem init` removes group and world write access from your own memory folder and its `MEMORY.md` (1.0 created them group-writable under umask 002). Feedback memories that Claude's native memory or an older pmem indexed outside the section are listed at session start under `## Preferences recorded outside the section` until `pmem index --fix` moves them in.

**A child repository with its own memory.** After `pmem init` in a workspace's top folder, every child repository uses the workspace memory, so `pmem pref add --up` there stops with `no usable memory dir above`: the project already writes to the workspace. To give one child its own memory, run `pmem init --here --force` inside it; `--force` replaces its link to the workspace memory. The child then inherits the workspace's preferences, so run the `pmem import` that init suggests only if the child should also get copies of the workspace's other memories.

**Capture is automatic only in Claude Code.** The prompt hook is narrow and stays out of the way:

| Prompt hook | Behaviour |
| --- | --- |
| Messages examined | At most 300 characters once pasted content and code blocks are dropped; typed and SDK prompts, not scheduled or loop wake-ups |
| Signals | Lasting-rule words ("from now on", "以后" opening a clause, "记住"; "by default" and "every time you…" only in statements, not in questions), style complaints ("too long", "太长"), corrections addressed to the agent ("don't … your answers", "别用…你"), violation markers ("I told you", "you … again", "又这样", "我跟你说过") |
| Nudges | At most one per session every 3 minutes. After a violation marker the note leads with up to 3 rules you may have broken, those sharing the most words with your message first ("table" and "表格" count as the same word), outer folders' rules included and labelled, and asks the agent to sharpen the rule, update its description, move it to the top and log the date under **Violations:** |
| Cost | No git call and no memory lookup for messages without a signal; 5-second timeout; always exits 0 and never blocks a prompt |

The note says the message *may* state a preference and that task instructions are not preferences, and the `✓ preference:` line lets you veto a wrong capture on the spot. Codex has no such hook: its global block tells it to save preferences, and memory-reflect sweeps your messages for missed ones at the end of a session, so expect fewer automatic captures there.

| Limit | Value |
| --- | --- |
| Rule length; `pmem pref add` refuses longer rules | 120 characters |
| Entries per section before `pmem lint` warns | 20 |
| Preference line length before `pmem lint` warns | 220 characters |
| Inherited preferences shown at session start; the handoff always fits | 40 lines and 2,000 characters |

**Commands.** Agents usually edit the files; these do the same in one step and add the bookkeeping:

| Command | Purpose |
| --- | --- |
| `pmem pref list [--all] [--json]` | Preferences of this project, of each outer folder (skipped ones with the reason) and of each global file, with a warning when global files differ; `--all` adds every project registered by `pmem init` or `pmem pref add`, to spot a rule repeated across projects |
| `pmem pref add RULE [--name NAME] [--title T] [--why TEXT] [--how TEXT] [--up] [--force]` | Save a preference and its feedback file, replacing the entry backed by the same name; the same rule already saved under another name stops with exit 2 unless `--force`; `--up` writes to the nearest outer memory |
| `pmem pref remove NAME\|RULE` | Take an entry that is really a project fact out of the section; its file stays as a `project` memory, indexed under `## Memories`. A preference you reject is archived instead |
| `pmem pref violated NAME --rule R [--note T] [--up]` | Replace the rule with a sharper one, update the description, log the date, move it to the top; `--up` edits a rule inherited from the nearest outer memory, `pmem -C <dir> pref violated …` one further up |
| `pmem pref promote NAME\|RULE [--config-dir DIR]... [--target FILE]... [--keep-local] [--only] [--dry-run]` | Append the rule to the preferences block of every global file that has the project-memory block, naming each file written; the local entry goes only after every write succeeded, and its file stays as a `project` memory. `--only` uses just the `--target` and `--config-dir` files; `--dry-run` prints the files and the change and writes nothing |
| `pmem pref demote RULE [--config-dir DIR]... [--target FILE]... [--to-project] [--only] [--dry-run]` | Remove the rule from the global blocks; a partial RULE needs at least 4 characters (2 in Chinese, Japanese or Korean) and must match a single rule; `--to-project` first adds it back to this project |

Without `--only`, promote and demote also use the default files that carry the project-memory block: `${CODEX_HOME:-~/.codex}/AGENTS.md` and the `CLAUDE.md` in `$CLAUDE_CONFIG_DIR` and in `~/.claude`. Global edits are minimal: pmem appends or removes only the matching line in each file, keeps every other byte and each file's line endings, backs the files up first, and writes nothing if any target fails its checks. Differences between your global files are reported, never merged. `pmem wire --remove` keeps the preferences block.

### Upgrading from 1.0

1.0 has no preferences, and a 1.0 copy left anywhere keeps writing feedback outside the section and knows no `pmem pref`. In this order:

1. Update every installed copy: the plugin in each Claude config dir, or `./install.sh --config-dir <dir>` for each, and `./install.sh --codex` for Codex.
2. Run `pmem init` once in each project with the 1.1 `pmem.py`, repeating the options you used before (`--no-hooks`, `--no-claude`, `--here`, `--track`); a plain `pmem init` adds every hook back. It adds `## Preferences` after the HANDOFF line, moves the existing feedback lines into it, adds the prompt hook and refreshes the stable copy. If `pmem --version` still prints `pmem 1.0.0`, run `python3 <skills dir>/project-memory/scripts/pmem.py init` instead.
3. Run `pmem wire codex` and `pmem wire claude --config-dir <dir>` again, so the global blocks carry the preference instructions. `pmem status` warns when the block in a global file or in the project's `AGENTS.md` was written by another pmem version (`… is from another pmem version → pmem wire …`).
4. Run memory-health once: the moved 1.0 hooks are often descriptions such as "How the user wants …" and work better as imperative rules.

From 1.1 on, `pmem init` never replaces a newer stable copy with an older one (`init --force` does), but a 1.0 `pmem init` still would, and it also drops the prompt hook from that project, which is why step 1 comes first. `pmem status` warns when the stable copy's version differs from the running CLI or when the prompt hook is missing. An agent that reports `invalid choice: 'pref'` ran a 1.0 copy.

## Switching agents and accounts

The handoff is the contract between sessions: goal, what is done, the next step, live state with a command to check each item, decisions in force and open questions. Agents keep it current at milestones, so an abrupt stop loses little.

When a Claude Code session dies on a usage limit:

| Step | What happens |
| --- | --- |
| 1. Session stops | The `StopFailure` (or `Stop`) hook writes `.checkpoints/<key>.md`: time, error, session id, transcript path, branch, HEAD, dirty file count, the tail of the last assistant message. A checkpoint left by an earlier session moves to `<key>.prev.md` first. The hook never edits the handoff. |
| 2. You switch | Open Codex in the same project, or Claude Code with another account. |
| 3. Next session starts | Claude gets the handoff injected together with the full checkpoint, headed by the error type, e.g. `rate_limit`. Codex's AGENTS.md block has it run `pmem context`, which shows the same; without `pmem` it reads the handoff and the checkpoint file. |
| 4. Catch up, in the first reply | `pmem sessions --since-handoff --tail 20` lists this workstream's sessions since the handoff, including the dead one in another config dir named by the checkpoints and sessions launched in sub-directories, and prints their last messages. |
| 5. Continue | The agent verifies the live state (branch, HEAD, jobs, remote dirs), records the old transcript path, updates the handoff and carries on. |

Why the first reply: the new session's own checkpoints push the failure out. It survives as `<key>.prev.md` for the rest of that session and is gone after the next one; from then on only `pmem sessions --config-dir <old account dir>` finds the old transcript. Read the sessions before editing the handoff, because the edit moves the `--since-handoff` cutoff to now.

pmem weighs the current checkpoint and the previous session's. Either one is shown in full if it is a `StopFailure` newer than the handoff, or more than 10 minutes newer than the handoff, meaning the handoff fell behind; the `Stop` hook fires after every turn, so a slightly newer checkpoint is normal. Otherwise the session start shows one line: "Last session ended normally at … (session …, transcript …); handoff is current." after a stop, or "Last checkpoint: PreCompact at … (session …)" after a compaction. The handoff's time is the later of its `metadata.modified` and the file's modification time, so an agent that forgets the timestamp raises no false alarm. Once the handoff is newer than the failure, the full section goes away.

Codex sessions get no automatic checkpoint, so the handoff is what carries the state; `pmem sessions` still finds Codex transcripts.

## What's on disk

```text
<project>/.agent-memory/
  MEMORY.md              index: preferences, then memories; loaded natively by Claude Code
  HANDOFF.md             live state of the main checkout (not a memory)
  handoffs/<key>.md      live state of parallel workstreams (linked worktrees or --key)
  <name>.md              one fact per file
  .archive/              retired memories (handoffs under .archive/handoffs/), hidden from native scans
  .checkpoints/<key>.md  written by hooks only; <key>.prev.md keeps the previous session's
  .git/                  private history (default)
  .gitignore, .imported.json, .pmem.lock
```

A memory file uses the same frontmatter Claude Code writes natively:

```markdown
---
name: tests-need-redis
description: Integration tests need Redis on port 6379; without it they fail with connection refused instead of skipping
metadata:
  type: project
  modified: 2026-11-23T02:31:00Z
---

Start Redis before the integration tests.
**Why:** the fixtures never check for Redis, so the failure looks like a code bug.
**How to apply:** run `redis-cli ping` before `pytest tests/integration`.
```

- **Types:** `user`, `feedback`, `project`, `reference`. A fix that took several attempts becomes `fix-<topic>`, its description starting with the verbatim error, its body holding `**Symptom:**`, `**Root cause:**` and `**Fix:**`.
- **Index:** one line per memory in `MEMORY.md`, `- [Title](name.md) — hook`, with the HANDOFF line first, preference lines under `## Preferences` and all others under `## Memories`. pmem only moves and edits such lines, with the link right after the `- ` at the start of the line, and leaves everything else alone: prose, quotes and nested or numbered lists that link to a memory stay where they are, and `pmem lint` reports those that link to a preference. The exceptions: `pmem init`, `pmem pref add` and `pmem add feedback` add the `## Preferences` heading, its note and `## Memories` to an index that lacks them, and a command that would otherwise append inside `## Preferences` first adds a `## Memories` heading. Links inside code spans, HTML comments and fenced blocks do not count; while a code fence or comment in the index is left open, pmem refuses to edit it.
- **Links:** `[[name]]` between memories; absolute dates only.

Claude Code loads at most 200 lines or 25,000 characters of the index and shows the first 4096 bytes of a memory file when recalling it, so `pmem lint` sets tighter limits:

| Check | Warn | Error |
| --- | --- | --- |
| Index lines | 150 | 200 |
| Index characters | 20,000 | 25,000 |
| Memory or handoff file | 4 KB | |
| Index line length | 150 chars | |
| Preference line length | 220 chars | |
| Preferences per section | 20 | |
| Unclosed HTML comment in the index | | any |
| Unclosed code fence in the index | | any |
| Handoff age | 14 days | |

## Git, privacy and private history

- **Private by default.** `pmem init` adds `.agent-memory` and `.claude/settings.local.json` to the project's `.git/info/exclude`, so nothing shows up in `git status` and nothing is committed or pushed.
- **Private history.** The memory folder is its own small Git repository. pmem snapshots after every change it makes and, in the background, at each Claude session start. Those snapshots fold in everyone's edits, so review with `pmem diff --since-health`: everything native memory or other agents changed since the last memory-health run. `pmem history` shows the log and `pmem restore` brings back a file. History commands only ever touch `.agent-memory/.git`; if it is broken, pmem reports "private history broken" and never falls back to the project's repository. As a nested repository the folder is skipped by the project's `git clean -fdx`; only `git clean -ffdx` removes it. Opt out with `--no-history`; the memory is then just an ignored folder, and `git clean -fdx` deletes it.
- **Shared with the team.** `pmem init --track` stops excluding the memory, so you commit it with the project; handoffs and checkpoints stay ignored. In that mode, keep personal `user` facts out and run `pmem lint --strict` before committing.
- **One mode per repository.** Git treats ignored files as disposable. Once anyone commits `.agent-memory/`, with `--track` or by accident, a `git pull` or checkout of that commit silently overwrites a private memory, and switching back can delete it. pmem warns at init, in `status` and `lint`, and at session start whenever any ref tracks `.agent-memory`. Snapshot before such a pull: `pmem restore` only recovers up to the last snapshot.
- **No secrets.** Memories record where a credential lives, never its value. `pmem lint` flags private keys, cloud and GitHub tokens, passwords in URLs and `password=`-style assignments; mark a deliberate line with `pmem:allow-secret`. A value that slipped in stays in the private history after you delete it, and in project commits with `--track`: treat it as leaked and rotate it.
- **Background consolidation off.** Claude's auto-dream may rewrite or delete memory files, so init sets `autoDreamEnabled: false` for the project. `--allow-dream` leaves it alone; the private history makes its edits reviewable.
- **Refusals.** pmem refuses a `.agent-memory` that resolves to `$HOME`, one of its parents, `/`, or the project root or one of its parents, such as a symlink shipped in a repository, and it never follows symlinks inside the folder; `pmem lint` reports them as errors. `pmem init` also refuses, unless you pass `--force`, to reuse a memory folder the project's Git tracks without `--track`, and to write Claude settings when the Claude root would be `$HOME`, one of its parents or `/`, as when `$HOME` is a dotfiles repository.

## Worktrees, parallel agents and non-git workspaces

- **Linked worktrees share the main checkout's memory.** pmem and Claude Code both resolve `.agent-memory/` from the main checkout, so all worktrees see the same facts; `pmem status` inside a worktree confirms it. Each linked worktree gets its own handoff, `handoffs/<worktree-name>.md`, while the main checkout uses `HANDOFF.md`. A worktree named `main`, common in bare-repository layouts, gets `handoffs/worktree-main.md`. Once a worktree is removed, memory-health retires its handoff with `pmem archive handoffs/<name>`; `HANDOFF.md` itself is never archived.
- **Parallel agents.** Prefer one worktree per agent. Two agents in one checkout each need a key: start one as `PMEM_KEY=agent-b claude` or `PMEM_KEY=agent-b codex`, and its hooks and every pmem command use `handoffs/agent-b.md` and a separate checkpoint. `--key NAME` on a single command works too, but hooks only see the environment variable. Codex passes `PMEM_KEY` on to the commands it runs unless its `shell_environment_policy` drops names containing `KEY` (`ignore_default_excludes = false`). Index edits run under a file lock, and one fact per file keeps write conflicts rare.
- **Never inside a linked worktree.** `git worktree remove` deletes ignored files, so `pmem init` refuses to create memory there and `pmem status` reports it as an error.
- **Non-git workspaces.** Run `pmem init` in the top folder of a workspace that holds several repositories, such as a paper with its code and tools: it creates one memory there and wires every child repository within two levels to it. Outside Git, Claude Code reads `.claude/settings.local.json` only from the folder it starts in, so start Claude in the top folder or inside a child repository. A session started in a plain sub-folder gets neither the memory nor the hooks, and `pmem status` there reports the wiring as not effective. A repository with its own `.agent-memory/` keeps it: the nearest memory wins and nested memories are never merged, except that preferences are inherited. `pmem init --here --force` gives a child repository already wired to the workspace memory its own (see [Preferences](#preferences-agents-that-learn-how-you-work)).
- **New clone or machine.** `.claude/settings.local.json` is machine-local, so run `pmem init` again there. In private mode the memory does not travel with the project; sync `.agent-memory/` yourself, it is a Git repository.

## Codex notes

- Codex reads `${CODEX_HOME:-~/.codex}/AGENTS.md` and the `AGENTS.md` files from the repository root down to the working directory, not `CLAUDE.md`. `pmem wire codex` adds the project-memory block to the global file; run it once per `CODEX_HOME`. `pmem wire <root>/AGENTS.md` puts it in a project file instead, and `pmem status` recognises either. Verified: Codex launched in a sub-directory found and read `<root>/.agent-memory/MEMORY.md`.
- If your project keeps its instructions only in `CLAUDE.md`, set `project_doc_fallback_filenames = ["CLAUDE.md"]` in Codex's `config.toml` so Codex reads them too.
- **Checkpoints from Claude.** The block asks Codex to run `pmem context` at session start, which shows the handoff together with any checkpoint a stopped Claude session left. That needs `pmem` on Codex's PATH, e.g. via `install.sh --link-cli`; otherwise Codex reads `HANDOFF.md` and `.checkpoints/<key>.md` directly.
- **Sandbox.** With `workspace-write`, Codex may write only below its working directory, plus `/tmp` and `$TMPDIR` unless those are excluded, so a project under `/tmp` never shows the problem. Launched from a sub-directory, a child repository or a linked worktree, it cannot save to the memory above it: launch it from the project root, or add `--add-dir <project root>`. Not the memory folder itself: Codex keeps `.git` read-only inside every writable root, so `--add-dir <memdir>` would block the private history. `pmem status` prints the root to launch from and the `--add-dir` path.
- **Preferences.** The block tells Codex to follow the `## Preferences` lines of the project, of outer folders and of the global block, and to save a new preference by editing the files. Codex has no capture hook, so it saves a preference only when it notices one itself or during a memory-reflect sweep, and it learns inherited preferences from `pmem context` or by reading the outer `MEMORY.md` files, which the block asks it to do at session start and again after compaction. Saving a workspace-level preference (`--up`) from a child project needs `--add-dir <workspace root>`.
- Codex has its own global `[memories]` feature. It is separate and can stay on; the block tells Codex that project knowledge belongs in `.agent-memory/`.
- Codex hooks are not wired in this version, so a Codex session that stops abruptly leaves no checkpoint and gets no preference nudge.

## Coming from /learn, /reflect and /evolve

An older harness wrote lessons into Claude's per-path memory and routed signals through an `evolve-queue.jsonl` processed by background agents. This package replaces it:

| Old | New | What changed |
| --- | --- | --- |
| `/learn` | [memory-learn](skills/memory-learn/SKILL.md) | Same 30-second single lesson, written to the shared `.agent-memory/` and deduplicated with `pmem find` |
| `/reflect` | [memory-reflect](skills/memory-reflect/SKILL.md) | Sweeps the conversation and writes directly under the change-risk table; still reminds you of unfinished entries in an experiment log |
| `/evolve` report and context slimming | [memory-health](skills/memory-health/SKILL.md) + `pmem lint` | Stale, oversized and duplicate memories, broken links and missing paths are lint findings; `pmem diff --since-health` replaces the change log |
| `/evolve sweep`, evolve-trigger rule, queue | Save triggers in [project-memory](skills/project-memory/SKILL.md) | The agent that saw the event saves it at once |
| harness-evolution rule | Failure-mode convention + change-risk table | `fix-<topic>` memories with Symptom / Root cause / Fix; "search memory before debugging" |
| harness-health | Not ported | Auditing a project's own rules and agent files against its code is project-specific; `pmem lint --paths` covers path drift inside memories |

**Why the queue, the background agents and the learning hooks are gone.** A queued signal reached a background agent without the conversation that produced it, so the lesson lost its "why" while risky edits happened unattended. Background agents also spent tokens after every task and raced the main session on the same files. Now the agent that saw the event writes the memory immediately and asks before anything risky; the only hooks left write checkpoints, never touch memories and always exit 0.

**Retiring the old harness**, which the agent does with you once `pmem status` reports legacy markers:

1. `pmem init`, then `pmem import --claude` for the old per-path memory and `pmem import --file CLAUDE.local.md` for notes.
2. Turn every unprocessed line of `.claude/evolve/evolve-queue.jsonl` into a memory or an explicit drop.
3. With your consent, move the old rules, skills and `.claude/evolve/` to `.claude/legacy-harness/`, and remove hooks that called them. Move, don't delete.
4. Run memory-health once to merge `*-imported*` duplicates and split `imported-notes*` into single facts.

## pmem at a glance

| Command | Purpose |
| --- | --- |
| `pmem init` | Create or reuse the memory and wire Claude Code with five hooks; idempotent, and upgrades a 1.0 index |
| `pmem status` | Memory, index size, handoffs, history, Claude and Codex wiring, git mode, preferences, legacy sources |
| `pmem context` | Handoff, checkpoints of this and the previous session (in full after an error stop or when the handoff lags), inherited preferences, other handoffs and the index in one go |
| `pmem handoff [show\|path\|list]` | Show, locate or list handoffs |
| `pmem add` · `find` · `archive` | Save, search and retire memories and finished handoffs |
| `pmem index [--fix]` · `lint` | Keep the index and the files consistent, feedback lines inside `## Preferences` |
| `pmem pref list` · `add` · `remove` · `violated` · `promote` · `demote` | Manage preferences of this project, the workspace and the global block |
| `pmem import` | Bring in Claude per-path memories, folders or notes files; never modifies sources |
| `pmem wire codex\|claude\|FILE` | Add, update or remove the block in a global or project instruction file, with a backup |
| `pmem snapshot` · `diff [--since-health]` · `history` · `restore` | Private history; `--since-health` reviews everything since the last memory-health run |
| `pmem sessions` | Recent Claude and Codex transcripts of this project, sub-directory launches included, labelled by workstream, with their last messages |

Every subcommand has `--help`, and `--key` defaults to `$PMEM_KEY`; the full reference is in the [project-memory skill](skills/project-memory/SKILL.md). The skills are written in Chinese; agents follow them in any language.

## Limitations

- Needs a Claude Code version that supports `autoMemoryDirectory`. Verified with Claude Code 2.1.322 and Codex CLI 0.164; re-check after upgrading.
- Claude's native memory is off with `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, in `--bare` mode (`CLAUDE_CODE_SIMPLE`), with `autoMemoryEnabled: false`, or when `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` points elsewhere. When the SessionStart hook runs in such a session, it injects `MEMORY.md` itself with a warning; `pmem status` reports the environment variables and settings involved.
- Codex hooks are not wired: Codex gets no checkpoints and no preference nudges, and needs `--add-dir <project root>` to write the memory from below the project root.
- The prompt hook's signals are a heuristic for English and Chinese that errs toward silence: questions, requests such as "try again" and phrases such as "改完以后…" do not fire. A preference phrased otherwise is left to the agent and to memory-reflect.
- The frontmatter parser reads a small YAML subset: keep each value on one line. Multi-line values are not read, and `pmem add --force` drops the extra keys of a flow-style `metadata: {…}`.
- `pmem wire` does not support files with lone-CR line endings.
- Linux and macOS only, WSL included; native Windows is untested.

## Development

```bash
cd project-memory
python3 -m unittest discover -s tests -t . -v
```

The tests need Git and run in temporary directories with `HOME`, `XDG_DATA_HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR` redirected. CI runs them on Ubuntu and macOS and with Python 3.9, and runs shellcheck on `install.sh`.

## License

[MIT](../LICENSE)
