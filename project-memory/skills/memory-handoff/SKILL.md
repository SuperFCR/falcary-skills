---
name: memory-handoff
description: 维护项目 .agent-memory/ 里的交接文件 HANDOFF.md 与 handoffs/<key>.md，让另一个 agent 或账号能无缝接手，包括本会话因额度用尽突然中断之后。交出时写目标、已完成、下一步、可核查的现场状态、生效的决策与约束、未决问题；接手时读交接和自动检查点，在第一轮回复里补上断档、核实现场后再动手。用户说“交接”“切换 agent”“换个账号继续”“换 Codex 或 Claude 接着做”“继续上次的任务”“handoff”“resume”“pick up where we left off”“switch agent”时使用；完成一个步骤、启动长任务或远程任务、做高风险操作前、上下文压缩后、准备停下时也要用。
---

# memory-handoff：交接与接手

交接文件记录**此刻的任务状态**，不是记忆：目的是让任何 agent 在本会话突然结束后也能接着干。持久的经验写进记忆，用户对做事方式的长期要求写进 `MEMORY.md` 的 `## Preferences`，都用 [memory-learn](../memory-learn/SKILL.md)。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。

| 工作流 | 交接文件 |
| --- | --- |
| 主 checkout | `HANDOFF.md` |
| linked worktree | `handoffs/<worktree 名>.md`，自动选择；名为 `main` 的 worktree 用 `handoffs/worktree-main.md` |
| 同一 checkout 里的另一个 agent | `handoffs/<key>.md`。用 `PMEM_KEY=<key>` 启动该 agent，如 `PMEM_KEY=agent-b claude`，hooks 和每条 pmem 命令就都用这个 key；只给单条命令加 `--key <key>` 时，hooks 注入的交接和写的检查点仍属于本 checkout。能用 worktree 就用 worktree |

`.checkpoints/<key>.md` 由 Claude Code 的 hooks 在停止、出错、压缩前自动写入，只记录最后一条回复和 git 现场；换了会话时，上一个会话的那份先移到 `<key>.prev.md`。不要手改，也不能代替交接。

## 交出

1. `pmem handoff path [--key K]` 取得路径，文件缺失时按[模板](../project-memory/templates/HANDOFF.md)创建。
2. 写之前**马上重读**。`modified` 或 `by` 与你上次读到的不同，说明别的 agent 改过：合并它的内容，不要覆盖。
3. 改写各段：

| 段 | 写什么 |
| --- | --- |
| Goal | 当前任务一句话，加上用户的验收标准 |
| Done | 已完成的步骤，每步一行，带 commit 或文件 |
| Next step | 下一个具体动作，接手者照着就能执行 |
| Live state | 分支、worktree、运行中的任务与 PID、远程目录、URL；每项一行，后附核查命令，如 `git -C <path> status -sb`、`kill -0 <pid>`、调度器的查询命令 |
| Decisions and constraints in force | 只在本任务内有效的决定和用户约束。用户说“以后都这样”的长期要求不写这里：它是偏好，按 memory-learn 存进 `## Preferences`，否则下个任务就丢了 |
| Open questions | 等用户回答的问题 |

4. frontmatter 的 `metadata` 下：`modified` 设为 `date -u +%Y-%m-%dT%H:%M:%SZ`，`by` 设为当前 agent 的标识，如 `claude-code`、`codex`，多账号时加区分标签；`name` 与 `workstream` 不动。漏改 `modified` 时 pmem 拿文件修改时间兜底，但复制或恢复文件会重置它，所以照样要改。
5. 控制在 4 KB 以内：Done 压成每步一行，删掉已解决的问题；值得长期保留的经验移进记忆，长期偏好移进 `## Preferences`。
6. 任务结束时把 Goal 改成 `(no active task)`，Done 留一行总结。交接文件从不删除。

## 接手

1. **读**：Claude Code 接线后，SessionStart hook 已注入交接；否则运行 `pmem handoff show [--key K]`，它会同时考虑当前和上一个会话的检查点：出错停止的、或比交接晚 10 分钟以上的显示完整段落；否则只有一行，Stop 后是 “Last session ended normally …; handoff is current.”，压缩后是 “Last checkpoint: PreCompact at …”。再用 `pmem handoff list` 看其他工作流。
2. **在第一轮回复里补上断档**：出现完整的检查点段，或交接看起来过时，例如 Next step 已出现在 `git log` 里、`modified` 很久以前，先运行 `pmem sessions --since-handoff --tail 20`，读上个会话最后的对话；它只列本 checkout 或本 worktree 的会话，同一 checkout 里靠 `PMEM_KEY` 区分的几个 agent 在这里分不开。别拖到后面：本会话的检查点会把旧的挤到 `<key>.prev.md`，再换一次会话就没了，之后要加 `--config-dir <上个账号的配置目录>` 才找得到。
3. **核实现场再动手**：`git status -sb`、`git log --oneline -5` 对上分支和 HEAD；逐条运行 Live state 里的核查命令，确认任务、PID、远程目录。与交接不符时以现实为准，并记下差异。
4. **防止抢活**：`by` 是另一个 agent、交接在 30 分钟以内改过（看 `pmem handoff list` 的时间），对方可能还在干活，先问用户再接管。两种情况不用问：显示了完整的 `## Auto checkpoint` 段，说明对方会话已停止；用户明确让你接手。
5. **汇报三行**：

```text
目标：<Goal>
现状：<核实后的状态，与交接不符之处>
下一步：<将要执行的动作>
```

6. **最后改交接**：把 `by` 改成自己，旧 transcript 路径记进 Live state；内容没变就不动 `modified`。改动会刷新文件时间，pmem 随即把交接当成最新：完整检查点段不再显示，`--since-handoff` 的截止时间也推到现在，所以这一步放在补断档和核实之后。

## 什么时候更新

| 时机 | 更新哪些段 |
| --- | --- |
| 完成一个步骤 | Done、Next step |
| 启动长任务或远程任务前 | Live state：任务 ID、日志路径、核查命令 |
| 高风险操作前，如 rebase、迁移、批量删除 | Next step、Decisions，写明回退办法 |
| 上下文压缩后、从摘要恢复时 | 先按“接手”第 2、3 步对齐，再补全各段 |
| 准备停下 | 全部各段 |
| 用户说“切换”“换个 agent”“交接”“handoff” | 按“交出”完整走一遍 |

会话随时可能因额度用尽而中断，hook 的检查点只留下最后一条回复。交接是否跟得上，决定了下一个 agent 能不能接着干。更新自己工作流的交接属 LOW，直接做，不必每次在回复里报告。

## 规则

- 只改自己工作流的交接；改写别的工作流的交接属 MEDIUM，接管前按“接手”第 4 步判断要不要先问。
- 交接里不写密钥的值；时间写绝对时间。
- 不改 `.checkpoints/`，不删任何交接文件。worktree 删除后它的交接由 [memory-health](../memory-health/SKILL.md) 用 `pmem archive handoffs/<key>` 退役。
