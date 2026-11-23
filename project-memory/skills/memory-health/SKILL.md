---
name: memory-health
description: 给项目的 .agent-memory/ 做体检和整理。先查看上次体检以来 Claude 原生写入或其他 agent 的改动，再跑 lint 与路径核查，然后对照代码核实过时条目、合并重复（含导入产生的 *-imported* 与 imported-notes）、拆分超长文件、归档作废记忆、把索引压到上限以内、修链接、处理已删除 worktree 的交接，最后快照并给一张汇总表。用户说“整理记忆”“记忆体检”“清理一下记忆”“memory health”“clean up memory”“lint memory”“/evolve”时使用；pmem import 之后、pmem lint 或 pmem status 报警告时也用，平时建议每周一次。
---

# memory-health：记忆体检与整理

目标是让记忆**少而准**：每条都与当前代码一致，索引装得进上下文。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。

## 1. 现状与改动

1. `pmem status`：先处理 ERROR 级接线问题，例如 `autoMemoryDirectory` 指向别处、记忆目录在 linked worktree 里；hooks 在但脚本缺失时重跑 `pmem init`。报 “private history broken”（嵌套的 `.git` 坏了），或项目某个提交跟踪了 `.agent-memory/` 时，告诉用户，不要自己动 git。
2. `pmem diff --since-health`：看上次体检以来的全部增删改，来源可能是 Claude 原生写入、其他 agent，或开启了的后台整理。不要用裸 `pmem diff`：每次 Claude 会话开始，hook 都会把之前的改动快照进 `session-start` 提交，裸 diff 在会话里几乎总是空的。不该发生的删除或改写用 `pmem restore NAME`，必要时加 `--rev REV`，恢复前先 `pmem history NAME` 找版本。没有私有历史时，`--track` 模式改用 `git diff --stat -- .agent-memory`，`--no-history` 模式跳过这步。

## 2. 检查

```bash
pmem lint --paths     # ERROR / WARN / INFO，--paths 核对反引号里的相对路径
pmem index            # 索引缺项、死链、HANDOFF 行位置
```

## 3. 处理发现

| 发现 | 处理 | 风险 |
| --- | --- | --- |
| 死链、HANDOFF 行不在首位 | `pmem index --fix` | LOW |
| 未进索引的文件 | 先在第 1 步的 diff 或 `pmem history` 里看索引行是怎么没的：有意移出、用户要“忘掉”的，`pmem archive <stem>` 退役；其余 `pmem index --fix` 补回 | LOW，归档有独有内容的为 HIGH |
| 缺 frontmatter 或时间戳、type 无效、name 与文件名不一致 | 补全或改正；改文件名时同步索引行和 `[[链接]]` | MEDIUM |
| 超过 N 天未更新、`--paths` 报路径不存在 | 对照代码核实：仍成立就更新路径并刷新 `modified`；部分过时就改写；整条作废就归档 | MEDIUM，归档为 HIGH |
| `name` 重复、`*-imported*` 需合并 | 并入原文件，保留更新、更具体的事实与不认识的 frontmatter 键，再 `pmem archive <副本> --reason "merged into <name>"` | MEDIUM |
| `imported-notes*` | 拆成一事一文件的记忆，逐条先查重，拆完归档原文件 | MEDIUM |
| 文件超过 4 KB | 拆成一事一文件，用 `[[name]]` 互链 | MEDIUM |
| 索引超过 150 行或 20,000 字符、单行超过 150 字符 | 缩短钩子；合并同主题的小记忆；归档作废的 | MEDIUM，归档为 HIGH |
| 断开的 `[[链接]]`、指向已归档记忆 | 改指向现有记忆或去掉链接 | LOW |
| 疑似密钥 | 立即删掉值，改记“存在哪”，并告诉用户：旧值仍在 `.agent-memory/.git` 历史里，`--track` 模式下可能还在项目提交里，按已泄露处理、轮换凭据；误报的行加 `pmem:allow-secret` | MEDIUM |
| 记忆目录里有符号链接 | pmem 与 hooks 都不跟随它；问清来源后换成普通文件或移出目录 | HIGH |
| `--track` 模式下有 `type: user` 记忆 | 建议移到全局指令文件或改为不含个人信息 | HIGH |
| 交接超过 14 天、所属 worktree 已删除 | 问用户：未完成的条目并入 `HANDOFF.md` 或写成记忆；征得同意后 `pmem archive handoffs/<key> --reason "worktree removed"`，文件进 `.archive/handoffs/`，不手动移动、不删除；`HANDOFF.md` 只更新、不归档 | HIGH |
| 出现保留目录 `team/`、`logs/` 等 | 只报告，不动 | — |

核实就是去看代码：打开记忆里提到的文件、函数、命令，确认还在、还对。没法核实的不归档，在汇总表里标“待确认”。

## 4. 问与做

- LOW、MEDIUM 直接做；合并后归档内容已被完全吸收的副本算 MEDIUM，归档仍有独有内容的记忆算 HIGH。
- 遇到 HIGH 项，或本次归档与合并合计超过 5 个时，先给出计划表，问“全部 / 选择 / 跳过”，按回答执行。
- 绝不擅自放宽 feedback 记忆或规则；两条记忆相互矛盾时问用户以哪条为准。

## 5. 收尾

1. 再跑 `pmem lint`，确认没有 ERROR、警告数下降。
2. `pmem snapshot -m "memory-health"`：提交信息以 `memory-health` 开头，下次 `pmem diff --since-health` 就从这里算起；没有私有历史时跳过。
3. 汇报：

```text
| 项目 | 动作 | 风险 |
| --- | --- | --- |
| build-cache-path.md | 路径已改，按代码更新 | MEDIUM |
| deploy-steps-imported.md | 并入 deploy-steps.md 后归档 | MEDIUM |
| old-api-quirk.md | 接口已删除，建议归档，等你确认 | HIGH |

| 指标 | 之前 | 之后 |
| --- | --- | --- |
| 记忆数 | 42 | 38 |
| 索引行 / 字符 | 61 / 7,900 | 55 / 6,800 |
| ERROR / WARN | 2 / 9 | 0 / 3 |
```

## 范围

- 只整理 `.agent-memory/`。项目自己的规则、agent、skill 与代码是否漂移，不在本 skill 范围；用户要求时作为普通任务处理，改动按风险表先问。
- 不改 `.checkpoints/`，不删除任何文件。
