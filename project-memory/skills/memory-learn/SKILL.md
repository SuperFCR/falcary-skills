---
name: memory-learn
description: 半分钟内把当前对话里最值得记住的一条经验写进项目的 .agent-memory/，先查重再写，最后一行确认。经验包括用户的纠正、用户确认过的非常规做法、试了 3 次以上才解决的故障、新发现的约束、外部资源的位置。用户说“记住这个”“记一下”“以后别这样”“learn this”“remember this”“note this down”“/learn”，或刚被用户纠正、刚解决一个反复失败的问题时使用。要一次回顾多条经验或会话收尾时改用 memory-reflect。
---

# memory-learn：捕获一条经验

只捕获**一条**，30 秒内完成，不做全量回顾；多条或收尾用 [memory-reflect](../memory-reflect/SKILL.md)。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。

前提：`pmem where` 能找到记忆目录。找不到就告诉用户先在项目里 `pmem init`，不要写到别处。

## 1. 识别

用户直接给了内容，就用用户的话，忠实记录，不发挥、不改意思。否则回顾对话，挑最值得记的一条：

| 信号 | 例子 | 记成 |
| --- | --- | --- |
| 用户纠正了你 | “不是这样”“别再这么做”、否决了某次工具调用 | feedback |
| 多轮才解决的故障 | 同一个报错试了 3 次以上 | project `fix-<topic>` |
| 违反了已有 feedback | 记忆里写了，这次没遵守 | 强化原条目，不新建 |
| 发现新约束 | “这台机器不能装 X”“这个接口限流每分钟 60 次” | project |
| 确认了非显然的做法 | 用户说“对，就这样”，但做法不寻常 | feedback |
| 外部资源的位置 | 文档、面板、远程目录、凭据存放处 | reference |

## 2. 分类

| 去向 | 条件 |
| --- | --- |
| `feedback` 记忆 | 关于 agent 怎么做事；正文加 `**Why:**`、`**How to apply:**` |
| `project` 记忆 | 关于代码、架构、流程、环境的事实或决策 |
| `fix-<topic>` 记忆 | 具体报错到修复；按 project-memory 的故障格式写 |
| 项目自己的故障表 | 项目已有故障表，如 `.claude/agents/*.md`、`docs/troubleshooting.md`：追加一行 `\| 症状 \| 根因与修复 \|` 代替 `fix-` 记忆 |
| `reference` 记忆 | 外部资源在哪、做什么用 |
| `user` 记忆 | 只限与本项目有关的角色或偏好；对所有项目成立的偏好属于全局指令文件，先问用户再写 |
| 项目自己的 skill | skill 步骤不对或不全，且它在项目的 `.claude/skills/` 或 `.agents/skills/` 里：提出修改并先问。已安装的插件或全局 skill 不改，改记一条 feedback |

## 3. 查重（必须先做）

`pmem find <2–3 个关键词>`，AND 匹配，词少更准；故障就用报错里的关键 token。用到项目故障表时也 grep 那张表。

| 结果 | 动作 |
| --- | --- |
| 已有且充分 | 回复“已经记录过了：`<file>`”，结束 |
| 已有但不全或过时 | 改原文件，更新 `metadata.modified` |
| 违反了已有 feedback | 把原条目写得更具体：触发场景更明确、补上这次的例子，不另建重复条目 |
| 与已有 feedback 相矛盾 | 先问用户以哪个为准，不擅自改弱原规则 |
| 全新 | 新建 |

## 4. 写入

新建用 `pmem add`，它写好 frontmatter 并在 `MEMORY.md` 追加一行索引；提示索引行超过 150 字符时，缩短 `-d` 或 `--title` 后加 `--force` 重写：

```bash
pmem add feedback ask-before-force-push -d "推送前先问；共享分支永不 force push" --stdin <<'EOF'
需要 force push 时先停下问用户。
**Why:** 2026-11-23 一次 force push 覆盖了同事在共享分支上的提交。
**How to apply:** 任何 `git push --force` 或 `--force-with-lease` 前先说明影响并等确认。
EOF
```

手写或更新已有文件时：

- frontmatter 含 `name`（kebab-case，等于文件名）、`description`（一行，写进检索会用的词）、`metadata.type`、`metadata.modified`（`date -u +%Y-%m-%dT%H:%M:%SZ`）。`description` 含 `: ` 或 `#` 时整体加双引号；每个值写在一行里。
- 新文件在 `MEMORY.md` 末尾追加 `- [标题](name.md) — 钩子`，追加前重读索引，只加这一行。
- 日期写绝对日期；相关记忆用 `[[name]]` 互链；保留不认识的 frontmatter 键。

## 5. 确认

只输出一行：

```text
✓ learned: <经验摘要> → <写入的文件>
```

更新已有文件时写 `✓ learned (updated): ...`。

## 规则

- 一次只记一条，最多改两处：记忆文件加 `MEMORY.md` 的一行，或者项目故障表的一行。
- 用户给了内容就忠实记录，不自己发挥。
- 不写密钥、令牌、密码的值，只记它存在哪里。
- 不扫实验日志、不改交接，那是 memory-reflect 与 [memory-handoff](../memory-handoff/SKILL.md) 的事。
- 沙箱拒绝写入 `.agent-memory/` 时，申请批准，或请用户从项目根目录重启、Codex 加 `--add-dir <项目根目录>`；只加记忆目录不够，它下面的 `.git` 会是只读的。绝不存到 `~/.claude/projects/...` 等别处。
