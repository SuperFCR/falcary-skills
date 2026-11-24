---
name: memory-reflect
description: 会话收尾或提交前回顾整段对话。必做一次偏好扫描：逐条看用户的发言，找重复的纠正、用户两次用同样方式改写你的输出、风格抱怨、“以后…”这类长期要求，逐条判定存或跳过，违反过的偏好改得更具体。再把查找顺序错误（没先查项目记忆就去外部搜）、调试 3 轮以上的故障、重复的多步流程、新约束整理进项目的 .agent-memory/；若记忆里登记了实验或运行日志，也检查其中失败和未收尾的条目；随后刷新交接，最后给一张汇总表。用户说“好了”“就这样”“今天先到这”“收尾”“回顾一下”“done for today”“wrap up”“reflect”“/reflect”，或准备 commit、一段长任务告一段落时使用。只记一条经验用 memory-learn。
---

# memory-reflect：收尾回顾

在当前会话里当场完成：不写队列、不派后台 agent。发现什么就按风险表写进记忆，再刷新交接。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。命令要等审批时直接编辑文件。

前提：`pmem where` 能找到记忆目录；找不到就告诉用户先 `pmem init`，到此为止。

## 1. 偏好扫描（必做）

偏好进了 `## Preferences`，之后每个会话都照做；漏掉一条，用户就得一再重复。Codex 没有提醒 hook，这一步是它记下偏好的主要途径。只看**用户的发言**，从头到尾逐条找：

| 信号 | 例子 |
| --- | --- |
| 同一纠正出现两次以上 | 两次让你“别在结尾加总结” |
| 用户两次用同样方式改写你的输出 | 都把长段落改成要点、都删掉 emoji、都把英文改回中文 |
| 风格抱怨 | “太长了”“啰嗦”“凝练一点”“too verbose” |
| 明说的长期要求 | “以后”“每次”“默认用”“from now on”“by default” |
| 违反已有偏好 | “我说过了”“又这样”“again”，或你做的与 `## Preferences` 某行相反 |

每个命中判定一次，全部写进汇总表：

- **存**：长期适用于你的行为，按 [memory-learn](../memory-learn/SKILL.md) 第 2 节写一句不超过 120 字符的祈使句规则，情境性的以 `When …:` 开头。
- **改**：违反了已有偏好，把那一行换成更具体的规则并移到段首，同步 `description`，在文件 `**Violations:**` 下记日期，或 `pmem pref violated NAME --rule "…" --note "…"`。继承来的规则（标着 `(from <目录>)`）改那一份：`--up` 或 `pmem -C <那个目录> pref violated …`，不在本项目另建。
- **跳过**：只管本次任务的指令、对内容本身的分歧、已有且被遵守的偏好、只出现一次且当场改过的风格抱怨；写明理由。

用户一句话就能否决，所以存下的每条都在汇总里给出原文规则；被否决的 `pmem archive <name> --reason "user vetoed"`，不再问。`pmem pref list --all` 显示同一规则已出现在 2 个以上项目时，只**建议**：同一工作区的用 `pmem pref add … --up` 写到上级记忆，跨工作区的经用户同意后 `pmem pref promote`，建议时列出它要写的每个文件（`pmem pref promote NAME --dry-run`）；不自己做。

## 2. 其他信号

| 信号 | 怎么认出来 | 动作 |
| --- | --- | --- |
| 查找顺序错误 | 先去外部搜索或长时间推理，后来发现记忆、项目文档或故障表里早有答案 | 偏好：遇到这类问题先查哪里 |
| 3 轮以上的故障 | 同一报错反复出现、多次修改才解决 | `fix-<topic>` 记忆或项目故障表追加一行 |
| 重复的多步流程 | 同一套命令做了 2 次以上 | project 或 reference 记忆写下完整命令序列；值得做成项目 skill 时只提建议 |
| 新约束或决策 | 用户定下的项目规则、环境限制、选型理由 | project |
| 用到的记忆已过时 | 记忆里的路径、命令与现状不符 | 更新它；整条作废则建议归档，等用户确认 |

## 3. 扫实验或运行日志（有才做）

只在某条记忆或交接指向了实验或运行日志时做，例如 `experiments/LOG.md`：

```bash
grep -nEi 'fail|error|diverged|nan|oom|crashed|❌' <日志>   # 失败条目
grep -nEi 'pending|running|todo|🔄' <日志>                 # 未收尾条目
```

- 失败条目：有 commit 就 `git show --stat <commit>` 定位改动，按第 2 节的信号表归类后记录。
- 未收尾条目：提醒用户“`<条目>` 还没回填结果”，不自动标记、不改日志。

## 4. 查重与写入

每个发现都按 memory-learn 处理：先 `pmem find`，已有且充分就跳过，已有但不全就更新，全新才新建。

| 风险 | 例子 | 做法 |
| --- | --- | --- |
| LOW | 新记忆、索引行、修死链、故障表追加一行、刷新自己工作流的交接、用户在本会话说过的本项目偏好 | 直接做 |
| MEDIUM | 改写已有记忆、改写别的工作流的交接、合并后归档内容已被完全吸收的副本、把已有偏好改得更具体、用户说过普遍适用的偏好写到工作区级 | 做完写进汇总表 |
| HIGH | 归档仍有独有内容或支撑偏好的记忆、删除或放宽偏好、与已有偏好或规则相矛盾、`pmem pref promote`、改 CLAUDE.md 或 AGENTS.md 等指令文件（包括 `pmem wire`）、新建规则或 skill | 只列为建议，问用户 |

## 5. 刷新交接

按 [memory-handoff](../memory-handoff/SKILL.md) 的“交出”步骤更新当前交接：Done、Next step、Live state 与实际一致。持久的经验和偏好已在第 1、4 步进了记忆，不放进交接。

## 6. 快照

私有历史开着时运行 `pmem snapshot -m "reflect: <一句摘要>"`；没有私有历史，如 `--track` 或 `--no-history` 模式，跳过。

## 7. 汇报

有发现时给一张表（用户偏好不要表格时改成同样字段的列表），HIGH 项放最后，写成要用户回答的问题：

```text
| 信号 | 动作 | 文件 | 风险 |
| --- | --- | --- | --- |
| 偏好：两次让我别加结尾总结 | 存：Don't end replies with a recap of what you did | no-trailing-recap.md | LOW |
| 偏好：“这次别动 utils.py” | 跳过：只管本次任务 | — | — |
| 违反偏好：又用了表格 | 改：When answering a comparison: short paragraphs, no tables | no-tables.md | MEDIUM |
| 交接 | 更新 Done、Next step | HANDOFF.md | LOW |
| 同一规则已在 3 个项目 | 建议 pmem pref promote，等你确认 | reply-language.md | HIGH |
```

什么都没发现时只回一行：`✓ reflect: nothing to learn`，交接有更新就在后面加一句。

## 规则

- 不在用户的任务进行中打断；收尾或用户要求时才做。
- 一事一文件；同一主题已有记忆时更新，不另建。
- 绝不擅自放宽约束，不改已安装的插件或全局 skill，不删除任何文件。
- 不写密钥的值，只记它存在哪里。
