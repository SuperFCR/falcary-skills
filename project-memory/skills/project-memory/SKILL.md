---
name: project-memory
description: 项目内记忆 .agent-memory/ 的总规则，Claude Code、Codex 与不同账号共用一份。内容包括记忆目录在哪、会话开始与压缩后怎么读、什么值得存、怎么存、改动风险分级、故障记录格式、并行工作、安装接线、从 Claude 自带记忆或旧 /learn /reflect /evolve 体系迁移，以及 pmem CLI 速查。在有 .agent-memory/ 的项目里开始或恢复任务、调试报错前、要新建、修改或忘掉记忆、切换 agent 或账号时使用；用户说“项目记忆”“初始化记忆”“迁移记忆”“忘掉这条”“pmem”“project memory”“set up project memory”“import Claude memory”“forget this memory”“where is the memory”时也用。只记一条经验用 memory-learn，收尾回顾用 memory-reflect，交接用 memory-handoff，整理体检用 memory-health。
---

# project-memory：放在项目里的共享记忆

项目学到的知识存在项目自己的 `.agent-memory/` 里，任何 agent、任何账号读写同一份。会话因额度用尽中断、换 agent 或换账号时，新会话只要“读项目记忆”就能接上。

**调用 CLI**：PATH 上有 `pmem` 就用它。否则用 Claude Code 会话开始注入的那行 `Project memory: … — CLI: python3 '<路径>' (pmem)` 给出的路径，即 init 装好的稳定副本 `${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py`；或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。CLI 不可用时按下文格式直接读写文件，规则不变。

## 位置与结构

记忆目录是从工作目录向上最近的 `.agent-memory/`。在 git linked worktree 里从主 checkout 的对应位置找起，不越过 `$HOME`，最近者生效，嵌套的不合并。`pmem where` 打印路径，`pmem status` 报告接线问题。解析到 `$HOME`、其上级、`/`，或项目根本身及其上级的 `.agent-memory`，例如仓库自带的符号链接，一律不用：命令拒绝，hooks 不出声。

| 路径 | 内容 | 谁写 |
| --- | --- | --- |
| `MEMORY.md` | 索引，每条记忆一行 `- [标题](name.md) — 一句钩子`；第一条链接必须是 HANDOFF | agent、pmem |
| `<name>.md` | 一事一文件 | agent、pmem、Claude 原生记忆 |
| `HANDOFF.md`、`handoffs/<key>.md` | 主 checkout 与各并行工作流的实时任务状态，不是记忆 | agent，见 [memory-handoff](../memory-handoff/SKILL.md) |
| `.checkpoints/<key>.md`、`<key>.prev.md` | Claude hooks 在停止、出错、压缩前自动写的检查点；换了会话时，上一个会话的那份先移到 `.prev.md` | 只由 hook 写，不要手改 |
| `.archive/` | 退役的记忆；已结束工作流的交接在 `.archive/handoffs/` | `pmem archive` |
| `.git/` | 私有历史，默认开启 | pmem 自动快照 |

不要创建 `team/`、`logs/`、`sessions/`、`proposals/`，它们是 Claude 的保留名；记忆名不能是 `memory` 或 `handoff`。目录里不要放符号链接：pmem 和 hooks 都不跟随，`pmem lint` 报 ERROR。

## 读：会话开始、恢复、压缩后

1. **先读交接**。经 `pmem init` 接线的 Claude Code，SessionStart hook 已把当前交接、需要注意的检查点和其他交接列表注入上下文，`MEMORY.md` 由原生记忆加载；原生加载被关掉时，hook 会把索引一并注入并给出警告。其他 agent，或没看到注入时，运行 `pmem context`，一次拿到交接、检查点和索引。
2. **看到完整的 `## Auto checkpoint` 段，在第一轮回复里补上断档**。它说明上个会话因错误停止，标题写明错误类型，如额度用尽的 `rate_limit`；或者检查点比交接晚 10 分钟以上，交接落后了。按顺序做：
   - 先 `pmem sessions --since-handoff --tail 20` 看上个会话最后做了什么，再核实现场。
   - 再更新交接，把旧 transcript 路径记进 Live state。改交接会把 `--since-handoff` 的截止时间推到现在，所以顺序不能反；交接比检查点新之后，完整段不再出现。
   - 别拖：本会话的第一个检查点会把旧的移到 `<key>.prev.md`，本会话里仍会显示；再换一次会话它就被挤掉了，之后要加 `--config-dir <上个账号的配置目录>` 才找得到旧 transcript。

   平常只有一行：Stop 之后是 “Last session ended normally at …; handoff is current.”，压缩之后是 “Last checkpoint: PreCompact at … (session …)”，照常读交接即可。
3. **单条记忆按需打开**，只读描述与当前任务相关的。记忆是写下那一刻的快照，动手前对照当前代码核实路径、函数和命令；不符就更新它。
4. **调试前先查记忆**。遇到报错先 `pmem find <报错里的关键 token>`，或 `grep -ril <token> <memdir>`；命中就先试记录的修复，没命中再自己推理。

## 存：什么值得记

| type | 存什么 | 正文 |
| --- | --- | --- |
| `feedback` | 用户对做事方式的纠正或确认 | 规则 + `**Why:**` + `**How to apply:**` |
| `project` | 本项目的事实、决策、约束、环境坑、故障 | 事实 + `**Why:**` + `**How to apply:**` |
| `reference` | 外部资源在哪：文档、面板、远程目录、凭据存放处 | 位置与用途 |
| `user` | 用户在本项目里的角色、分工、偏好 | 只记与本项目有关的 |

不记：

- 代码、git 历史、README 已经写明的东西。
- 一次性的任务进度，它属于交接。
- 对所有项目都成立的个人偏好，它属于全局指令文件：`${CLAUDE_CONFIG_DIR:-~/.claude}/CLAUDE.md`（每个配置目录各一份）或 `${CODEX_HOME:-~/.codex}/AGENTS.md`，改它前先问。
- 密钥、令牌、密码。只记“存在哪”，例如环境变量名、钥匙串条目、文件路径，永不记值。
- `--track` 模式下的个人信息。

**保存时机**：发生就存，不等收尾。

| 触发 | 做法 |
| --- | --- |
| 用户纠正你，或确认了一个不寻常的做法 | [memory-learn](../memory-learn/SKILL.md)，记成 feedback |
| 一个故障试了 3 次以上才解决 | 按下面的故障格式记成 `fix-<topic>` |
| 同一套多步操作第二次出现 | project 或 reference 记忆，写下完整命令序列 |
| 到达里程碑、启动长任务、准备停下 | 更新交接 |
| 会话收尾、提交前 | [memory-reflect](../memory-reflect/SKILL.md) 全量回顾 |

## 存：怎么写

1. **先查重**：`pmem find <2–3 个关键词>`，在 name、描述和正文里做 AND 匹配，词少更准。已有且充分就不写；已有但不全或过时就改原文件；全新才新建。
2. **写入**：`pmem add TYPE NAME -d "描述" --stdin` 从 stdin 读正文，写好 frontmatter 并追加索引行；索引行超过 150 字符时它会警告，缩短 `-d` 或 `--title`。手写时格式如下，写完在 `MEMORY.md` 末尾追加一行 `- [标题](tests-need-redis.md) — 钩子`，追加前重读索引，只加这一行。

```markdown
---
name: tests-need-redis
description: 集成测试依赖本地 6379 端口的 Redis，未启动时报连接拒绝而不是跳过
metadata:
  type: project
  modified: 2026-11-23T02:31:00Z
---

跑集成测试前先确认 Redis 在运行。
**Why:** 测试夹具不检查 Redis 是否可用，失败信息看起来像代码错误。
**How to apply:** 跑 `pytest tests/integration` 前先 `redis-cli ping`。
```

3. **约定**：
   - `name` 为 kebab-case 且等于文件名；`description` 一行，写进将来检索会用的词，如报错签名、命令名；含 `: ` 或 `#` 时整体加双引号。
   - frontmatter 每个值写在一行里，`metadata` 用上面的缩进写法：多行值 pmem 读不全，`metadata: {…}` 这种写法在 `pmem add --force` 改写时会丢掉其他键。
   - 用绝对日期 `2026-11-23`，不写“昨天”；时间戳用 UTC，`date -u +%Y-%m-%dT%H:%M:%SZ`。
   - 相关记忆用 `[[name]]` 互链；单文件小于 4 KB；索引行不超过 150 字符。代码段、HTML 注释和代码块里的链接不算索引行。
   - 改已有文件时更新 `metadata.modified`，保留不认识的 frontmatter 键。
4. 接线后，Claude 的原生记忆写入也落在这里，格式兼容。不论用哪种方式写，都先查重、守风险表。

## 故障格式

type 为 `project`，名字 `fix-<topic>`，`description` 以原样的报错签名开头，正文三段：

```markdown
---
name: fix-libcudnn-import
description: "ImportError: libcudnn.so.9: cannot open shared object file — LD_LIBRARY_PATH 指向旧 CUDA"
metadata:
  type: project
  modified: 2026-11-23T02:31:00Z
---

**Symptom:** worker 启动即报 `ImportError: libcudnn.so.9: cannot open shared object file: No such file or directory`
**Root cause:** shell 配置把旧 CUDA 的 lib 目录放在 LD_LIBRARY_PATH 最前面。
**Fix:** 启动脚本只追加当前 CUDA 的 lib 目录；排查时先 `echo $LD_LIBRARY_PATH`。
```

项目若自己维护故障表，例如 `.claude/agents/*.md` 或 `docs/troubleshooting.md`，也可以只在表里追加一行 `| 症状 | 根因与修复 |`，不必两处都写。

## 改动风险

| 风险 | 改动 | 做法 |
| --- | --- | --- |
| LOW | 新建记忆、加索引行、修死链、给故障表追加一行、更新自己工作流的交接 | 直接做 |
| MEDIUM | 改写已有记忆；改写别的工作流的交接；合并重复后归档内容已被完全吸收的副本 | 做完在回复里说明 |
| HIGH | 归档仍有独有内容的记忆；与已有 feedback 记忆或规则相矛盾、或放宽它；改 CLAUDE.md、AGENTS.md 等任何指令文件，包括用 `pmem wire` 写块；新建规则或 skill 文件 | 先问，同意后再做 |

- **绝不擅自放宽约束**。某条记忆或规则挡住了实现，先问用户，不要改掉它。
- 只改项目自己的 `.claude/skills/`、`.agents/skills/`；已安装的插件或全局 skill 副本一律不改，它们的问题记成 feedback 记忆。
- **从不删除**。退役用 `pmem archive NAME --reason "..."`，文件进 `.archive/`，可恢复；交接只更新、不删除，`HANDOFF.md` 不可归档，已删除 worktree 的交接由 memory-health 用 `pmem archive handoffs/<key>` 退役。
- 用户说“忘掉某条记忆”，这本身就是同意：`pmem archive`。只删索引行不算忘掉，`pmem index --fix` 会把它加回来。

## 并行工作

- 记忆目录全项目一份，位于主 checkout；交接按工作流分开：主 checkout 用 `HANDOFF.md`，linked worktree 自动用 `handoffs/<worktree 名>.md`（名字规整为小写短横线形式；规整后为 `main` 的用 `worktree-main`，不占主 checkout 的 `HANDOFF.md`）。
- 首选一个 agent 一个 worktree（`git worktree add`）。同一 checkout 里的多个 agent 各用一个 key：用 `PMEM_KEY=<名字>` 启动，如 `PMEM_KEY=agent-b claude`、`PMEM_KEY=agent-b codex`，hooks 和每条 pmem 命令都以它作 `--key` 的默认值，交接是 `handoffs/<名字>.md`，检查点也分开。只在单条命令上加 `--key` 时，hooks 仍按本 checkout 注入交接、写检查点。
- 不改别的工作流的交接，接管按 memory-handoff 的规则来。一事一文件，并发写很少冲突；`MEMORY.md` 由 pmem 加锁修改，手改时先重读，只追加或改自己那一行。
- Codex 在子目录、子仓库或 linked worktree 里启动时，沙箱不允许写上级的 `.agent-memory/`：从项目根目录启动，或 `codex --add-dir <项目根目录>`。不要只加记忆目录：Codex 把每个可写根下的 `.git` 设为只读，私有历史就快照不了。`/tmp` 与 `$TMPDIR` 默认可写，放在那里的项目碰不到这个限制。不论哪个 agent，写不进去就问，绝不存到别处。

## 安装与接线

每个项目一次：git 项目在任意位置运行都会落到主 checkout 根目录；非 git 目录在顶层运行，或 `pmem -C <根目录> init`。

```bash
pmem init      # 创建或复用 .agent-memory/，接线 Claude Code；幂等
pmem status    # 核对接线、hooks、git 模式、Codex 块
```

| `pmem init` 写入 | 内容 |
| --- | --- |
| `.agent-memory/` | `MEMORY.md`、`HANDOFF.md`、`.archive/`、`.checkpoints/`、`.gitignore`；默认 `git init` 私有历史 |
| `<git-common-dir>/info/exclude` | 把 `.agent-memory` 与 `.claude/settings.local.json` 排除出项目 git |
| `.claude/settings.local.json` | `autoMemoryDirectory` 指向记忆目录、`autoDreamEnabled: false`、4 个 hooks：SessionStart 注入交接，Stop、StopFailure、PreCompact 写检查点 |
| `${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py` | hooks 调用的稳定副本，不随插件更新或配置目录变化；装不上时 init 警告并且不写 hooks |

- 非 git 的工作区目录：init 还会给 2 层以内的子 git 仓库接线。git 之外 Claude Code 只读启动目录的 `.claude/settings.local.json`，所以要在顶层或某个子仓库里启动；在普通子文件夹启动的会话既没有记忆也没有 hooks，`pmem status` 在那里会报 “not effective from this directory”。
- 常用选项：`--track` 不再把记忆排除在项目 git 之外，由你随项目提交；`--here` 在当前子目录建独立记忆；`--no-claude` 不接线 Claude；`--allow-dream` 保留 Claude 的后台整理，它可能改写或删除记忆文件。
- init 拒绝（exit 2）的情况：记忆目录解析到 `$HOME`、其上级、`/`，或项目根本身及其上级；不加 `--track` 却要复用已被项目 git 跟踪的记忆目录；Claude 根目录是 `$HOME`、其上级或 `/`，例如 `$HOME` 本身是 dotfiles 仓库。后两种可以加 `--force` 越过，越过前先问用户。
- 每台机器、每个 agent 配置各一次：`pmem wire codex` 写入 `${CODEX_HOME:-~/.codex}/AGENTS.md`；`pmem wire claude --config-dir <dir>` 给每个 Claude 配置目录或账号各写一次，默认 `${CLAUDE_CONFIG_DIR:-~/.claude}`；`pmem wire <项目根>/AGENTS.md` 写进项目文件，`pmem status` 两种都认。它改的是指令文件，属 HIGH，用户没有明确要求时先问。
- `settings.local.json` 只属于本机，新克隆或新机器上要重新 `pmem init`。私有模式下记忆不随项目 git 走，换机器需自己同步 `.agent-memory/`，它本身就是 git 仓库。

## 迁移

**Claude 自带记忆**，按启动路径分散在 `<config>/projects/<slug>/memory/`：

1. `pmem import --claude --dry-run` 看计划，再去掉 `--dry-run`。用过多个配置目录或账号就加 `--config-dir <dir>`，可重复。有些目录只列为候选、不自动导入：上级目录和用户级 `autoMemoryDirectory` 的记忆可能属于别的项目；在子目录启动 Claude 留下的记忆，按 transcript 里的 `cwd` 核实过属于本项目，没有 transcript 的会注明无法核实。看过后用 `pmem import <dir>` 显式导入。
2. 旧笔记文件：`pmem import --file CLAUDE.local.md` 整体导入为 `imported-notes*` 记忆；之后 CLAUDE.local.md 只留本机配置与指令，改它前先问。
3. 用 [memory-health](../memory-health/SKILL.md) 处理 `needs merge` 的 `*-imported*` 与 `imported-notes*`：合并，拆成一事一文件。
4. 按导入摘要的提示退役旧目录：征得同意后移走，不删除。导入可重复运行，已导入的不会重复。

**旧的 learn / reflect / evolve 体系**。`pmem status` 发现 `.claude/evolve/`，或提到 `evolve-queue` 的规则、skill 时会提示：

1. `.claude/evolve/evolve-queue.jsonl` 里未处理的每一行，按 memory-learn 变成一条记忆，或明确列为放弃；给用户一张表。
2. 征得同意后，把旧规则、旧 skill 与 `.claude/evolve/` 移到 `.claude/legacy-harness/`。移动，不删除。
3. 对应关系：`/learn` → memory-learn；`/reflect` 与扫尾 sweep → memory-reflect；`/evolve` 日报与瘦身 → memory-health；agent 故障表与“调试前先查”规则 → 本 skill 的故障格式与读协议。队列和后台 agent 不再需要，发现即在当前会话里写。
4. CLAUDE.md、AGENTS.md 里提到旧命令的段落，提出修改建议，先问再改。

## 与 CLAUDE.md、CLAUDE.local.md、AGENTS.md 的关系

| 文件 | 放什么 | 谁改 |
| --- | --- | --- |
| 项目 `CLAUDE.md`、`AGENTS.md` | 指令与约定：构建命令、代码规范、流程 | 人；agent 改属 HIGH |
| `CLAUDE.local.md` | 本机配置，如连接信息、本机路径 | 人；旧的学习笔记迁入 `.agent-memory/` |
| 全局 `CLAUDE.md`、`AGENTS.md` | 跨项目个人偏好，以及 `pmem wire` 写入的 project-memory 块 | 人；块由 pmem 维护 |
| `.agent-memory/` | 干活中学到的知识与交接 | agent，按风险表 |

指令文件说“该怎么做”，记忆记“学到了什么”。在各种记忆位置中，`.agent-memory/` 优先于内置记忆和旧笔记。记忆与指令文件冲突时，指出冲突，问用户以哪边为准，再更新落后的一方。

## 隐私与历史

- 默认私有：`.agent-memory/` 被排除在项目 git 之外，有自己的 `.git` 历史，不推送。pmem 的写命令自动快照，每次 Claude 会话开始也在后台快照一次。历史命令只作用于 `.agent-memory/.git`；它损坏时 pmem 报 “private history broken”，绝不往项目仓库提交。
- `--no-history` 时记忆只是一个被忽略的目录，项目的 `git clean -fdx` 会把它删掉；有私有历史时只有 `git clean -ffdx` 会删。
- `--track` 需显式选择：记忆目录不再被排除，由你随项目提交，交接与检查点仍被忽略。不写密钥和个人 `user` 事实，提交前跑 `pmem lint --strict`。
- 同一仓库只用一种模式。git 把被忽略的文件当作可丢弃：有人提交了 `.agent-memory/`，别人 pull 或 checkout 那个提交时私有记忆会被静默覆盖，切回去还可能被删。任何 ref 跟踪了 `.agent-memory` 时，pmem 在 init、status、lint 和会话开始时警告；看到就告诉用户，pull 或 checkout 前先 `pmem snapshot`，因为 `pmem restore` 只能恢复到最后一次快照。
- `pmem lint` 会标出疑似密钥；确属误报的行加 `pmem:allow-secret`。已经写进去的值删掉后仍留在私有历史里，`--track` 时还可能在项目提交里：按已泄露处理，请用户轮换凭据。

## CLI 速查

全局选项 `-C DIR` 指定起点目录；每个子命令都有 `--help`；`--key` 的默认值取环境变量 `PMEM_KEY`。退出码：0 正常，1 有问题或没找到，2 用法错误或被拒绝。

| 命令 | 作用 |
| --- | --- |
| `pmem --version` | 打印 `pmem 1.0.0` |
| `pmem init [--here] [--track] [--no-history] [--no-claude] [--no-hooks] [--allow-dream] [--no-children] [--force]` | 创建或复用记忆目录并接线，幂等 |
| `pmem where` | 打印记忆目录，没有则退出 1 |
| `pmem status [--json]` | 记忆、索引容量、交接、历史、Claude 接线与 hook 脚本、git 模式、Codex 块、待迁移来源 |
| `pmem context [--key K]` | 交接、检查点（当前与上一个会话的；出错停止或晚于交接 10 分钟以上时完整显示，否则一行）、其他交接、索引，一次输出 |
| `pmem handoff [show\|path\|list] [--key K]` | 显示当前交接；打印路径，缺则按模板创建；列出全部交接 |
| `pmem add TYPE NAME -d DESCRIPTION [--title T] [--body TEXT \| --stdin] [--force]` | 新建记忆并追加索引行 |
| `pmem find TERM... [--archive]` | 在 name、描述、正文里不分大小写地 AND 搜索，覆盖记忆与交接 |
| `pmem index [--fix]` | 检查索引；`--fix` 补漏、删死链、把 HANDOFF 行移到首位 |
| `pmem lint [--strict] [--stale-days N] [--paths]` | 格式、容量、链接、符号链接、密钥、过期检查，`N` 默认 90 |
| `pmem archive NAME... [--reason TEXT]` | 记忆移入 `.archive/` 并删除索引行；`handoffs/<key>` 把已结束工作流的交接移入 `.archive/handoffs/`；`HANDOFF.md` 不可归档 |
| `pmem import [--claude] [--config-dir DIR]... [--file PATH]... [SRC_DIR]... [--dry-run]` | 导入旧记忆，不改来源，可重复运行 |
| `pmem wire codex\|claude\|FILE [--config-dir DIR]... [--remove]` | 在指令文件里写入、更新或移除 project-memory 块，改前备份 |
| `pmem snapshot [-m MSG]` | 提交私有历史 |
| `pmem diff [--since REV \| --since-health]` | 默认看上次快照以来的增删改；`--since-health` 从上次 memory-health 快照算起 |
| `pmem history [-n N] [NAME]` | 私有历史日志，`N` 默认 20 |
| `pmem restore NAME [--rev REV] [--force]` | 从历史恢复一个文件并补回索引行 |
| `pmem sessions [--since-handoff] [--since ISO] [--tail N] [--config-dir DIR]... [--limit N]` | 列出本项目近期的 Claude 与 Codex 会话记录，含在子目录启动的，每条标出所属工作流；`--since-handoff` 只列当前工作流交接之后的；`--limit` 默认 10 |
| `pmem hook session-start [--with-index]` | 供 SessionStart hook 调用，从 stdin 读 JSON |
| `pmem hook checkpoint` | 供 Stop、StopFailure、PreCompact hook 调用，写检查点 |
