---
name: memory-reflect
description: 会话收尾或提交前回顾整段对话，把用户纠正、重复出现的多步流程、查找顺序错误（没先查项目记忆就去外部搜）、违反已有 feedback、调试 3 轮以上的故障整理进项目的 .agent-memory/；若记忆里登记了实验或运行日志，也检查其中失败和未收尾的条目；随后刷新交接，最后给一张汇总表。用户说“好了”“就这样”“今天先到这”“收尾”“回顾一下”“done for today”“wrap up”“reflect”“/reflect”，或准备 commit、一段长任务告一段落时使用。只记一条经验用 memory-learn。
---

# memory-reflect：收尾回顾

在当前会话里当场完成：不写队列、不派后台 agent。发现什么就按风险表写进记忆，再刷新交接。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。

前提：`pmem where` 能找到记忆目录；找不到就告诉用户先 `pmem init`，到此为止。

## 1. 扫对话

从头到尾过一遍本次对话，找下列信号：

| 信号 | 怎么认出来 | 动作 |
| --- | --- | --- |
| 用户纠正 | “不要”“别”“不是”“错了”“don't”；否决了工具调用；同一指令说了两遍 | 新建或更新 feedback |
| 违反已有 feedback | 对照已加载的索引：有条目却没遵守 | 把原条目写得更具体，不另建 |
| 查找顺序错误 | 先去外部搜索或长时间推理，后来发现记忆、项目文档或故障表里早有答案 | feedback：遇到这类问题先查哪里 |
| 3 轮以上的故障 | 同一报错反复出现、多次修改才解决 | `fix-<topic>` 记忆或项目故障表追加一行 |
| 重复的多步流程 | 同一套命令做了 2 次以上 | project 或 reference 记忆写下完整命令序列；值得做成项目 skill 时只提建议 |
| 新约束或决策 | 用户定下的规则、环境限制、选型理由 | project |
| 用到的记忆已过时 | 记忆里的路径、命令与现状不符 | 更新它；整条作废则建议归档，等用户确认 |

## 2. 扫实验或运行日志（有才做）

只在某条记忆或交接指向了实验或运行日志时做，例如 `experiments/LOG.md`：

```bash
grep -nEi 'fail|error|diverged|nan|oom|crashed|❌' <日志>   # 失败条目
grep -nEi 'pending|running|todo|🔄' <日志>                 # 未收尾条目
```

- 失败条目：有 commit 就 `git show --stat <commit>` 定位改动，按第 1 节的信号表归类后记录。
- 未收尾条目：提醒用户“`<条目>` 还没回填结果”，不自动标记、不改日志。

## 3. 查重与写入

每个发现都按 [memory-learn](../memory-learn/SKILL.md) 的第 3、4 步处理：先 `pmem find`，已有且充分就跳过，已有但不全就更新，全新才新建。

| 风险 | 例子 | 做法 |
| --- | --- | --- |
| LOW | 新记忆、索引行、修死链、故障表追加一行、刷新自己工作流的交接 | 直接做 |
| MEDIUM | 改写已有记忆、改写别的工作流的交接、合并后归档内容已被完全吸收的副本 | 做完写进汇总表 |
| HIGH | 归档仍有独有内容的记忆、与已有 feedback 或规则相矛盾、放宽约束、改 CLAUDE.md 或 AGENTS.md 等指令文件（包括 `pmem wire`）、新建规则或 skill | 只列为建议，问用户 |

## 4. 刷新交接

按 [memory-handoff](../memory-handoff/SKILL.md) 的“交出”步骤更新当前交接：Done、Next step、Live state 与实际一致。持久的经验已在第 3 步进了记忆，不放进交接。

## 5. 快照

私有历史开着时运行 `pmem snapshot -m "reflect: <一句摘要>"`；没有私有历史，如 `--track` 或 `--no-history` 模式，跳过。

## 6. 汇报

有发现时给一张表，HIGH 项放最后，写成要用户回答的问题：

```text
| 信号 | 动作 | 文件 | 风险 |
| --- | --- | --- | --- |
| 用户纠正：推送前要先问 | 新建 feedback | ask-before-force-push.md | LOW |
| 违反已有 feedback | 补充触发场景 | run-tests-on-node.md | MEDIUM |
| 交接 | 更新 Done、Next step | HANDOFF.md | LOW |
| 规则过严挡住实现 | 建议放宽，等你确认 | .claude/rules/style.md | HIGH |
```

什么都没发现时只回一行：`✓ reflect: nothing to learn`，交接有更新就在后面加一句。

## 规则

- 不在用户的任务进行中打断；收尾或用户要求时才做。
- 一事一文件；同一主题已有记忆时更新，不另建。
- 绝不擅自放宽约束，不改已安装的插件或全局 skill，不删除任何文件。
- 不写密钥的值，只记它存在哪里。
