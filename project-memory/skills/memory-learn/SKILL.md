---
name: memory-learn
description: 半分钟内把当前对话里最值得记住的一条经验或偏好写进项目的 .agent-memory/，先查重再写，最后一行确认。用户说明以后要你怎么做事、纠正你时，先走偏好路径：一句祈使句规则写进 MEMORY.md 的 Preferences 段，之后每个会话都照做；违反过的偏好改得更具体。其他经验包括用户确认过的非常规做法、试了 3 次以上才解决的故障、新发现的约束、外部资源的位置。用户说“记住这个”“记一下”“以后别这样”“以后都…”“我说过了”“learn this”“remember this”“note this down”“from now on”“/learn”，或刚被用户纠正、刚解决一个反复失败的问题时使用。要一次回顾多条经验或会话收尾时改用 memory-reflect。
---

# memory-learn：捕获一条经验或偏好

只捕获**一条**，30 秒内完成，不做全量回顾；多条或收尾用 [memory-reflect](../memory-reflect/SKILL.md)。通用规则见 [project-memory](../project-memory/SKILL.md)。

**调用 CLI**：PATH 上有 `pmem` 就用它；否则用会话开始注入的 `CLI: python3 '…'` 那个路径，或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。命令要等审批，或 `pmem pref` 报 `invalid choice: 'pref'`（CLI 还是 1.0）时，直接编辑文件，格式见下。

前提：`pmem where` 能找到记忆目录，或会话里已注入记忆目录的路径。找不到就告诉用户先在项目里 `pmem init`，不要写到别处。

## 1. 先问：这是偏好吗

偏好是在这里大多数会话都适用、打开任何相关文件之前就该遵守的、关于你行为的长期规则：语言、语气、篇幅、格式、工作流与审批步骤、用户坚持用的工具。是就走第 2 节；否则走第 3 节。

| 用户的话 | 是偏好吗 | 为什么 |
| --- | --- | --- |
| “以后回复别加总结，我看 diff” | 是 | 每次回复都适用 |
| “太长了，凝练一点” | 第一次：不是，照改这次回复；再出现，或用户说“以后”“from now on”：是 | 一次抱怨只针对这次回复；重复或明说长期才是对篇幅的长期要求 |
| “集成测试要先起 Redis” | 否，project 记忆 | 只在跑测试时相关 |
| “这次别动别的文件” | 否，照做不记 | 只管本次任务 |

## 2. 偏好路径

1. **查重**：对照上下文里的 `## Preferences`、注入的 `## Inherited preferences`，再 `pmem find <关键词>`。
   - 同义的已在，而这次是被违反了：不新建，按第 4 步改那一条。
   - 与已有偏好矛盾：先问用户以哪个为准，不擅自改弱原规则。
2. **写成一条规则**：忠实于用户的原意和措辞，但写成一句祈使句，单行，不超过 120 字符；不含 `<!--`、`-->`，不以 `#` 或 `>` 开头。只在某种情境下成立的，以 `When <情境>: ` 开头；这种规则含 `: `，写进 `description` 时整体加双引号，否则 YAML 读不了、`pmem lint` 警告。细节、例子放文件正文。
3. **选层级并写入**：这条在本工作区的兄弟项目里也成立、且用户说过它普遍适用，写到上一级记忆（`--up`）；拿不准就写本项目；`--up` 报 `no usable memory dir above` 说明本项目用的就是工作区的记忆，写本项目即可。全局只在用户同意后由 `pmem pref promote` 做，本 skill 不做。直接编辑文件；在 Claude Code 里改本项目的记忆目录不需要审批，写上级工作区的可能要：
   - 写 `<name>.md`：

```markdown
---
name: no-trailing-recap
description: Don't end replies with a recap of what you did; the user reads the diff
metadata:
  type: feedback
  modified: 2026-11-23T02:31:00Z
---

Don't end replies with a recap of what you did; the user reads the diff.
**Why:** 用户 2026-11-23 说 "Stop ending every answer with a recap, I read the diff"。
**How to apply:** 回复以结果或待确认的问题结束；要列改动时只列文件名。
```

   - 在 `MEMORY.md` 的 `## Preferences` 段末尾、下一个标题之前加一行 `- [no recap](no-trailing-recap.md) — Don't end replies with a recap of what you did; the user reads the diff`。加前重读索引，只加这一行；不要加到 `## Memories` 下。没有 `## Preferences` 段时，照[模板](../project-memory/templates/MEMORY.md)在 HANDOFF 行之后插入 `## Preferences` 标题、说明引用块和 `## Memories` 标题，并提醒用户用新版跑一次 `pmem init`。
   - 等价的命令是 `pmem pref add "<规则>" --name <name> --why "…" --how "…" [--up]`，它还会补段、登记项目；`pmem add feedback` 也会把行放进段里，它的 `-d` 就是规则。
4. **被违反时**：把那一行换成更具体的规则（补上触发情境和一个反例），同步改 `description`，把这一行移到段首，在文件正文 `**Violations:**` 下追加 `- YYYY-MM-DD: <这次怎么错的>`。命令是 `pmem pref violated NAME --rule "<更具体的规则>" --note "…"`，`--rule` 必须与原文不同。只记次数不改规则没用，上下文里只有那一行。
   - 被违反的是继承来的（注入的 `## Inherited preferences` 或提醒里标着 `(from <目录>)`）就改那一份：`pmem pref violated NAME --up --rule …` 改最近一层，`pmem -C <那个目录> pref violated NAME --rule …` 改任一层，NAME 用 `pmem -C <那个目录> pref list` 查；或手改那份 `MEMORY.md`。不要在本项目另建一条。
   - pmem 拒绝并说那一条 is not a `- [title](name.md) — rule` line 时，先把它手改成这种规范行再重跑。

## 3. 其他经验

| 信号 | 例子 | 记成 |
| --- | --- | --- |
| 多轮才解决的故障 | 同一个报错试了 3 次以上 | project `fix-<topic>`，按 project-memory 的故障格式；项目已有故障表，如 `.claude/agents/*.md`、`docs/troubleshooting.md`，就追加一行 `\| 症状 \| 根因与修复 \|` |
| 发现新约束 | “这台机器不能装 X”“这个接口限流每分钟 60 次” | project |
| 确认了非显然的做法，只在某块代码相关 | 用户说“对，就这样”，但做法不寻常 | project；成了长期做事方式就是偏好 |
| 外部资源的位置 | 文档、面板、远程目录、凭据存放处 | reference |
| 用户在本项目的角色或分工 | “这个模块归我管，那个归 A” | user |
| 项目自己的 skill 步骤不对 | skill 在项目的 `.claude/skills/` 或 `.agents/skills/` 里 | 提出修改并先问；已安装的插件或全局 skill 不改，改记一条 feedback |

先 `pmem find <2–3 个关键词>` 查重，AND 匹配，词少更准；故障就用报错里的关键 token。已有且充分，回复“已经记录过了：`<file>`”后结束；已有但不全或过时，改原文件并更新 `metadata.modified`；全新才新建：

```bash
pmem add project tests-need-redis -d "集成测试依赖本地 6379 的 Redis，未启动时报连接拒绝" --stdin <<'EOF'
跑集成测试前先确认 Redis 在运行。
**Why:** 测试夹具不检查 Redis 是否可用，失败看起来像代码错误。
**How to apply:** 跑 `pytest tests/integration` 前先 `redis-cli ping`。
EOF
```

手写时 frontmatter 含 `name`（kebab-case，等于文件名）、`description`（一行，含 `: ` 或 `#` 时加双引号）、`metadata.type`、`metadata.modified`（`date -u +%Y-%m-%dT%H:%M:%SZ`），在 `## Memories` 段末尾加 `- [标题](name.md) — 钩子`。日期写绝对日期，相关记忆用 `[[name]]` 互链，保留不认识的 frontmatter 键。

## 4. 确认

确认只占回复的最后一行。同一条消息里还有任务就照常完成，本轮回复就按新偏好做：

```text
✓ preference: <规则>
✓ learned: <经验摘要> → <写入的文件>
```

偏好用第一种，写到上级工作区时行尾加 ` (workspace)`，改得更具体时写 `✓ preference (sharpened): <新规则>`；其他经验用第二种，更新已有文件时写 `✓ learned (updated): ...`。

用户否决 ✓ 行或说“不要这条”：`pmem archive <name> --reason "user vetoed"`，否决就是同意，不再问；只是说法不对就按用户的话改写那一行和 `description`。

## 规则

- 一次只记一条，最多改两处：记忆文件加 `MEMORY.md` 的一行，或者项目故障表的一行。
- 用户给了内容就忠实记录，不自己发挥；偏好只把措辞收成一句祈使句，不改意思、不放宽。
- 不写密钥、令牌、密码的值，只记它存在哪里。
- 不扫实验日志、不改交接，那是 memory-reflect 与 [memory-handoff](../memory-handoff/SKILL.md) 的事。
- 沙箱拒绝写入 `.agent-memory/` 时，申请批准，或请用户从项目根目录重启、Codex 加 `--add-dir <项目根目录>`（写工作区级偏好时是工作区根目录）；只加记忆目录不够，它下面的 `.git` 会是只读的。绝不存到 `~/.claude/projects/...` 等别处。
