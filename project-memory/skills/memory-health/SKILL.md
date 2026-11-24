---
name: memory-health
description: 给项目的 .agent-memory/ 做体检和整理。先查看上次体检以来 Claude 原生写入或其他 agent 的改动，再跑 lint 与路径核查，然后对照代码核实过时条目、合并重复（含导入产生的 *-imported* 与 imported-notes）、拆分超长文件、归档作废记忆、把索引压到上限以内、修链接、处理已删除 worktree 的交接，并审一遍 Preferences 段的偏好（条数、祈使句、触发条件、矛盾、重复、屡次被违反的规则），最后快照并给一张汇总表。用户说“整理记忆”“记忆体检”“清理一下记忆”“memory health”“clean up memory”“lint memory”“/evolve”时使用；pmem import 之后、pmem lint 或 pmem status 报警告时也用，平时建议每周一次。
---

# memory-health：记忆体检与整理

目标是让记忆**少而准**：每条都与当前代码一致，索引装得进上下文。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。

## 1. 现状与改动

1. `pmem status`：先处理 ERROR 级接线问题，例如 `autoMemoryDirectory` 指向别处、记忆目录在 linked worktree 里；hooks 在但脚本缺失、缺 UserPromptSubmit hook、稳定副本版本与 CLI 不一致或缺偏好段时，请用户用新版重跑 `pmem init`，步骤见 project-memory 的“从 1.0 升级”；全局块报 `from another pmem version` 时请用户重跑提示里的 `pmem wire …`（HIGH）。上级记忆被跳过、偏好没被继承（`pmem pref list`、`status` 和会话开始的 WARNING 给出原因）时告诉用户；多半是 1.0 在 umask 002 下建的目录组可写，用户同意后在那一层重跑 `pmem init` 或照提示 `chmod go-w`。报 “private history broken”（嵌套的 `.git` 坏了），或项目某个提交跟踪了 `.agent-memory/` 时，告诉用户，不要自己动 git。
2. `pmem diff --since-health`：看上次体检以来的全部增删改，来源可能是 Claude 原生写入、其他 agent，或开启了的后台整理。不要用裸 `pmem diff`：每次 Claude 会话开始，hook 都会把之前的改动快照进 `session-start` 提交，裸 diff 在会话里几乎总是空的。不该发生的删除或改写用 `pmem restore NAME`，必要时加 `--rev REV`，恢复前先 `pmem history NAME` 找版本。没有私有历史时，`--track` 模式改用 `git diff --stat -- .agent-memory`，`--no-history` 模式跳过这步。

## 2. 检查

```bash
pmem lint --paths     # ERROR / WARN / INFO，--paths 核对反引号里的相对路径
pmem index            # 索引缺项、死链、HANDOFF 行位置、段外的 feedback 行
pmem pref list        # 本项目、继承与全局的偏好，被跳过的上级与全局块的不一致
```

## 3. 处理发现

| 发现 | 处理 | 风险 |
| --- | --- | --- |
| 死链、HANDOFF 行不在首位、feedback 行在 `## Preferences` 段外 | `pmem index --fix`；缺偏好段时它只提示，改跑 `pmem init` | LOW |
| `unclosed HTML comment at MEMORY.md:N`、`unclosed code fence at MEMORY.md:N` | 补上 `-->` 或 ```` ``` ````，或删掉多余的开头；它后面的内容原生加载看不到或成了代码，`index --fix` 与 `pmem pref` 都拒绝写 | LOW |
| `feedback memory X has no canonical index line`、`other reference to X left in place` | pmem 只动 `- [标题](X.md) — 规则` 这种规范行：在 `## Preferences` 里补一行规范的；其余引用是正文就留着，是重复的索引行就删掉那一行（文件不动） | LOW |
| `index line inside Preferences`：非 feedback 的普通索引行在偏好段里 | 移到 `## Memories` 下；是对你行为的规则就改成 feedback | MEDIUM |
| 未进索引的文件 | 先在第 1 步的 diff 或 `pmem history` 里看索引行是怎么没的：有意移出、用户要“忘掉”的，`pmem archive <stem>` 退役；其余 `pmem index --fix` 补回 | LOW，归档有独有内容的为 HIGH |
| 缺 frontmatter 或时间戳、type 无效、name 与文件名不一致 | 补全或改正；改文件名时同步索引行和 `[[链接]]` | MEDIUM |
| `the description contains ": " but is not quoted` | 整个 `description` 加双引号 | LOW |
| 超过 N 天未更新、`--paths` 报路径不存在 | 对照代码核实：仍成立就更新路径并刷新 `modified`；部分过时就改写；整条作废就归档 | MEDIUM，归档为 HIGH |
| `name` 重复、`*-imported*` 需合并 | 并入原文件，保留更新、更具体的事实与不认识的 frontmatter 键，再 `pmem archive <副本> --reason "merged into <name>"` | MEDIUM |
| `imported-notes*` | 拆成一事一文件的记忆，逐条先查重，拆完归档原文件 | MEDIUM |
| 文件超过 4 KB | 拆成一事一文件，用 `[[name]]` 互链 | MEDIUM |
| 索引超过 150 行或 20,000 字符、普通索引行超过 150 字符、偏好行超过 220 字符 | 缩短钩子；合并同主题的小记忆；归档作废的 | MEDIUM，归档为 HIGH |
| 断开的 `[[链接]]`、指向已归档记忆 | 改指向现有记忆或去掉链接 | LOW |
| 疑似密钥 | 立即删掉值，改记“存在哪”，并告诉用户：旧值仍在 `.agent-memory/.git` 历史里，`--track` 模式下可能还在项目提交里，按已泄露处理、轮换凭据；误报的行加 `pmem:allow-secret` | MEDIUM |
| 记忆目录里有符号链接 | pmem 与 hooks 都不跟随它；问清来源后换成普通文件或移出目录 | HIGH |
| `--track` 模式下有 `type: user` 记忆 | 建议移到全局指令文件或改为不含个人信息 | HIGH |
| 交接超过 14 天、所属 worktree 已删除 | 问用户：未完成的条目并入 `HANDOFF.md` 或写成记忆；征得同意后 `pmem archive handoffs/<key> --reason "worktree removed"`，文件进 `.archive/handoffs/`，不手动移动、不删除；`HANDOFF.md` 只更新、不归档 | HIGH |
| 出现保留目录 `team/`、`logs/` 等 | 只报告，不动 | — |

核实就是去看代码：打开记忆里提到的文件、函数、命令，确认还在、还对。没法核实的不归档，在汇总表里标“待确认”。

## 4. 审偏好

`## Preferences` 每个会话都在上下文里，一条错的或含糊的规则会一直误导 agent，所以逐行看：

| 发现 | 处理 | 风险 |
| --- | --- | --- |
| 一段超过 20 条 | 合并同义、去掉过时的；砍哪条由用户定 | MEDIUM，删除为 HIGH |
| 从 1.0 移进来的钩子是描述，如“How the user wants…”“回复风格偏好” | 按文件正文改写成一句祈使句规则，同步改 `description` | MEDIUM |
| 只在某种情境下成立却没有触发条件，工作区级的尤其要查 | 改成 `When <情境>: <规则>` | MEDIUM |
| 两条相互矛盾，或与上级、全局的规则冲突 | 问用户以哪条为准；冲突时近者胜，但不要靠它掩盖矛盾 | HIGH |
| 同义重复 | 合并进描述更具体的那条，另一个 `pmem archive <name> --reason "merged into <name>"`；归档会删掉它的偏好行，先列给用户 | 合并 MEDIUM，归档 HIGH |
| lint INFO：文件 `**Violations:**` 里已有 2 条以上，改写过仍被违反 | 问用户这条该怎么说才会被遵守，按用户的措辞改 | MEDIUM |
| lint INFO `description out of sync with the rule`：手改过偏好行，文件没跟上 | 把 `description`（和正文第一行）改成偏好行上的规则；是改规则时漏记的违反，补进 `**Violations:**` | LOW |
| `type: user` 的记忆其实是对你行为的规则 | 改成 `type: feedback`，`pmem index --fix` 把它的行移进偏好段 | MEDIUM |
| 从 1.0 移进来的 feedback 其实是只在某块代码或环境相关的事实 | `pmem pref remove <name>`：文件改成 `project` 记忆，行移到 `## Memories`；只挪行不改类型，`pmem index --fix` 会把它移回来 | MEDIUM |
| `- <规则>` 这种没有文件的手写行（lint INFO `no feedback file`） | 补一个 feedback 文件并改成 `- [标题](name.md) — <规则>` | LOW |
| 几个项目的 `pmem pref list --all` 里出现同一规则 | 只建议 `--up` 或 `pmem pref promote`，等用户同意 | HIGH |

不放宽、不删除任何偏好，除非用户同意。

## 5. 问与做

- LOW、MEDIUM 直接做；合并后归档内容已被完全吸收的副本算 MEDIUM，归档仍有独有内容的记忆算 HIGH。
- 遇到 HIGH 项，或本次归档与合并合计超过 5 个时，先给出计划表，问“全部 / 选择 / 跳过”，按回答执行。
- 绝不擅自放宽偏好、feedback 记忆或规则；两条记忆相互矛盾时问用户以哪条为准。

## 6. 收尾

1. 再跑 `pmem lint`，确认没有 ERROR、警告数下降。
2. `pmem snapshot -m "memory-health"`：提交信息以 `memory-health` 开头，下次 `pmem diff --since-health` 就从这里算起；没有私有历史时跳过。
3. 汇报（用户偏好不要表格时改成同样字段的列表）：

```text
| 项目 | 动作 | 风险 |
| --- | --- | --- |
| build-cache-path.md | 路径已改，按代码更新 | MEDIUM |
| reply-style.md | 偏好钩子改成祈使句：Answer in short paragraphs; no tables | MEDIUM |
| deploy-steps-imported.md | 并入 deploy-steps.md 后归档 | MEDIUM |
| old-api-quirk.md | 接口已删除，建议归档，等你确认 | HIGH |

| 指标 | 之前 | 之后 |
| --- | --- | --- |
| 记忆数 | 42 | 38 |
| 偏好条数 | 24 | 18 |
| 索引行 / 字符 | 61 / 7,900 | 55 / 6,800 |
| ERROR / WARN | 2 / 9 | 0 / 3 |
```

## 范围

- 只整理 `.agent-memory/`。项目自己的规则、agent、skill 与代码是否漂移，不在本 skill 范围；用户要求时作为普通任务处理，改动按风险表先问。
- 不改 `.checkpoints/`，不删除任何文件。
