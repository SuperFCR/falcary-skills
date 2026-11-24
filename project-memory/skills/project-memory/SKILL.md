---
name: project-memory
description: 项目内记忆 .agent-memory/ 的总规则，Claude Code、Codex 与不同账号共用一份。内容包括记忆目录在哪、会话开始与压缩后怎么读、什么值得存、怎么存、用户的做事偏好（MEMORY.md 的 Preferences 段，越用越懂你）、改动风险分级、故障记录格式、并行工作、安装接线、从 Claude 自带记忆、旧 /learn /reflect /evolve 体系或 1.0 升级迁移，以及 pmem CLI 速查。在有 .agent-memory/ 的项目里开始或恢复任务、调试报错前、要新建、修改或忘掉记忆、用户说明以后要你怎么做事、切换 agent 或账号时使用；用户说“项目记忆”“初始化记忆”“迁移记忆”“忘掉这条”“我的偏好”“pmem”“project memory”“set up project memory”“import Claude memory”“forget this memory”“where is the memory”“my preferences”时也用。只记一条经验或偏好用 memory-learn，收尾回顾用 memory-reflect，交接用 memory-handoff，整理体检用 memory-health。
---

# project-memory：放在项目里的共享记忆

项目学到的知识存在项目自己的 `.agent-memory/` 里，任何 agent、任何账号读写同一份。会话因额度用尽中断、换 agent 或换账号时，新会话只要“读项目记忆”就能接上；用户说过一次的做事偏好，之后每个会话都照做。

**调用 CLI**：PATH 上有 `pmem` 就用它。否则用 Claude Code 会话开始注入的那行 `Project memory: … — CLI: python3 '<路径>' (pmem)` 给出的路径，即 init 装好的稳定副本 `${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py`；或 `python3 <skills_root>/project-memory/scripts/pmem.py`，`<skills_root>` 是本 skill 目录的上一级。下文统一写 `pmem`。CLI 不可用、命令要等审批，或 `pmem pref` 报 `invalid choice: 'pref'`（那份 CLI 还是 1.0）时，按下文格式直接读写文件，规则不变。

## 位置与结构

记忆目录是从工作目录向上最近的 `.agent-memory/`。在 git linked worktree 里从主 checkout 的对应位置找起，不越过 `$HOME`，最近者生效，嵌套的不合并；唯一的例外是偏好，上级目录的会被继承，见“偏好”。`pmem where` 打印路径，`pmem status` 报告接线问题。解析到 `$HOME`、其上级、`/`，或项目根本身及其上级的 `.agent-memory`，例如仓库自带的符号链接，一律不用：命令拒绝，hooks 不出声。

| 路径 | 内容 | 谁写 |
| --- | --- | --- |
| `MEMORY.md` | 索引：第一条链接是 HANDOFF；`## Preferences` 段每条偏好一行，`## Memories` 段每条其他记忆一行 `- [标题](name.md) — 一句钩子` | agent、pmem |
| `<name>.md` | 一事一文件 | agent、pmem、Claude 原生记忆 |
| `HANDOFF.md`、`handoffs/<key>.md` | 主 checkout 与各并行工作流的实时任务状态，不是记忆 | agent，见 [memory-handoff](../memory-handoff/SKILL.md) |
| `.checkpoints/<key>.md`、`<key>.prev.md` | Claude hooks 在停止、出错、压缩前自动写的检查点；换了会话时，上一个会话的那份先移到 `.prev.md` | 只由 hook 写，不要手改 |
| `.archive/` | 退役的记忆；已结束工作流的交接在 `.archive/handoffs/` | `pmem archive` |
| `.git/` | 私有历史，默认开启 | pmem 自动快照 |

不要创建 `team/`、`logs/`、`sessions/`、`proposals/`，它们是 Claude 的保留名；记忆名不能是 `memory` 或 `handoff`。目录里不要放符号链接：pmem 和 hooks 都不跟随，`pmem lint` 报 ERROR。

## 读：会话开始、恢复、压缩后

1. **先读交接**。经 `pmem init` 接线的 Claude Code，SessionStart hook 已把当前交接、需要注意的检查点、其他交接列表和上级目录继承的偏好注入上下文，`MEMORY.md` 由原生记忆加载；原生加载被关掉时，hook 会把索引一并注入并给出警告。其他 agent，或没看到注入时，运行 `pmem context` 一次拿到这些和索引；Codex 压缩上下文后要再运行一次。
2. **看到完整的 `## Auto checkpoint` 段，在第一轮回复里补上断档**。它说明上个会话因错误停止，标题写明错误类型，如额度用尽的 `rate_limit`；或者检查点比交接晚 10 分钟以上，交接落后了。按顺序做：
   - 先 `pmem sessions --since-handoff --tail 20` 看上个会话最后做了什么，再核实现场。
   - 再更新交接，把旧 transcript 路径记进 Live state。改交接会把 `--since-handoff` 的截止时间推到现在，所以顺序不能反；交接比检查点新之后，完整段不再出现。
   - 别拖：本会话的第一个检查点会把旧的移到 `<key>.prev.md`，本会话里仍会显示；再换一次会话它就被挤掉了，之后要加 `--config-dir <上个账号的配置目录>` 才找得到旧 transcript。
   - 平常只有一行：Stop 之后是 “Last session ended normally at …; handoff is current.”，压缩之后是 “Last checkpoint: PreCompact at … (session …)”，照常读交接即可。
3. **照偏好做**。`## Preferences` 的每一行、注入的 `## Inherited preferences` 和全局指令文件里的偏好块，整个会话都生效，见下文“偏好”。
4. **单条记忆按需打开**，只读描述与当前任务相关的。记忆是写下那一刻的快照，动手前对照当前代码核实路径、函数和命令；不符就更新它。
5. **调试前先查记忆**。遇到报错先 `pmem find <报错里的关键 token>`，或 `grep -ril <token> <memdir>`；命中就先试记录的修复，没命中再自己推理。

## 存：什么值得记

| type | 存什么 | 正文 |
| --- | --- | --- |
| `feedback` | 偏好：用户要你长期怎么做事，含纠正与确认过的做法；索引行在 `## Preferences` | 规则 + `**Why:**` + `**How to apply:**` |
| `project` | 本项目的事实、决策、约束、环境坑、故障 | 事实 + `**Why:**` + `**How to apply:**` |
| `reference` | 外部资源在哪：文档、面板、远程目录、凭据存放处 | 位置与用途 |
| `user` | 用户在本项目里的角色与分工；对你行为的要求记成 `feedback` | 只记与本项目有关的 |

不记：代码、git 历史、README 已经写明的东西；一次性的任务进度，它属于交接；只管本次任务的指令；密钥、令牌、密码，只记“存在哪”，例如环境变量名、钥匙串条目、文件路径，永不记值；`--track` 模式下的个人信息。

**保存时机**：发生就存，不等收尾。

| 触发 | 做法 |
| --- | --- |
| 用户说明以后要你怎么做、纠正你，或确认了一个不寻常的做法 | 关乎你长期怎么做事的，当场存成偏好，见下文；只关乎某块代码或环境的记成 project。单条流程见 [memory-learn](../memory-learn/SKILL.md) |
| 一个故障试了 3 次以上才解决 | 按下面的故障格式记成 `fix-<topic>` |
| 同一套多步操作第二次出现 | project 或 reference 记忆，写下完整命令序列 |
| 到达里程碑、启动长任务、准备停下 | 更新交接 |
| 会话收尾、提交前 | [memory-reflect](../memory-reflect/SKILL.md) 全量回顾，含偏好扫描 |

## 存：怎么写

1. **先查重**：`pmem find <2–3 个关键词>`，在 name、描述和正文里做 AND 匹配，词少更准。已有且充分就不写；已有但不全或过时就改原文件；全新才新建。
2. **写入**：`pmem add TYPE NAME -d "描述" --stdin` 从 stdin 读正文，写好 frontmatter 并加索引行：`feedback` 加在 `## Preferences` 段末尾，其他类型加在 `## Memories` 下；索引行超过 150 字符时它会警告，缩短 `-d` 或 `--title`。手写时格式如下，写完在 `## Memories` 段末尾加一行 `- [标题](tests-need-redis.md) — 钩子`，加前重读索引，只加这一行。

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
   - frontmatter 每个值写在一行里，`metadata` 用上面的缩进写法：多行值 pmem 读不全，`metadata: {…}` 这种写法在 `pmem add --force` 改写时会丢掉其他键。改已有文件时更新 `metadata.modified`，保留不认识的键。
   - 用绝对日期 `2026-11-23`，不写“昨天”；时间戳用 UTC，`date -u +%Y-%m-%dT%H:%M:%SZ`。相关记忆用 `[[name]]` 互链；单文件小于 4 KB；普通索引行不超过 150 字符。代码段、HTML 注释和代码块里的链接不算索引行。
4. 接线后，Claude 的原生记忆写入也落在这里，格式兼容。不论用哪种方式写，都先查重、守风险表。

## 偏好：越用越懂你

用户说过一次的做事方式，之后每个会话、每个 agent 和账号都照做；被违反过的规则会被改得更具体。偏好就是 `type: feedback` 的记忆，只是索引行放在 `MEMORY.md` 的 `## Preferences` 段，而这一段每个会话都在上下文里。

**什么算偏好**：在这里大多数会话都适用、打开任何相关文件之前就该遵守的、关于你行为的长期规则，如语言、语气、篇幅、格式、工作流与审批步骤、用户坚持用的工具。只在碰到某块代码或环境时才相关的事实是普通记忆；只管本次任务的指令两者都不是，照做，不记。

| 用户原话 | 归类 | 处理 |
| --- | --- | --- |
| “以后回复都用中文，代码和提交信息用英文” | 偏好 | 规则：`回复用中文；代码和提交信息用英文` |
| "Stop ending every answer with a recap, I read the diff" | 偏好 | 规则：`Don't end replies with a recap of what you did; the user reads the diff` |
| “集成测试要先起 Redis，不然报连接拒绝” | 项目事实 | `project` 记忆，跑测试时才相关 |
| "The paper builds with xelatex; pdflatex breaks the CJK fonts" | 项目事实 | `project` 记忆，编译时才相关 |
| “这次只改 app.py，别动别的文件” | 任务指令 | 照做，不记 |
| "Rename `uid` to `user_id` in this function" | 任务指令 | 照做，不记 |

**格式**：一条偏好是段里一行 `- [<标题>](<name>.md) — <规则>` 加一个 feedback 文件；行尾的钩子就是规则本身，例如 `- [commit style](commit-style.md) — When writing a commit message: English, imperative mood, no emoji`。

- 规则是一句祈使句：单行，不超过 120 字符，不含 `<!--` 或 `-->`，不以 `#` 或 `>` 开头，方括号写成 `\[`、`\]`。细节和例子写进文件正文。
- 只在某种情境下成立的规则以触发条件开头：`When <情境>: <规则>`。写到工作区级的应当这样写（约定，pmem 不检查）。
- 文件 `<name>.md`：`metadata.type: feedback`，`description` 与规则同文，含 `: `（如 `When …:` 规则）时整体加双引号，否则 YAML 读不了、`pmem lint` 警告；正文是规则加 `**Why:**`（用户原话与日期）和 `**How to apply:**`。手写一行 `- <规则>` 而不建文件，pmem 也认，但不要这样写。
- 每段不超过 20 条，多了交给 [memory-health](../memory-health/SKILL.md) 合并。
- 归属看类型：每个 `feedback` 记忆都是偏好，`pmem init`、`pmem index --fix` 会把它的行移回段里。pmem 只移动、改写行首就是 `- [标题](name.md)` 的规范行；正文、引用块、缩进或编号列表里指向它的链接原样保留，`pmem lint` 报出来，`pref remove`、`pref violated` 遇到非规范条目拒绝，先手改成规范行再重跑。
- 一条其实是只在某块代码或环境相关的项目事实，就把文件的 `metadata.type` 改成 `project` 再挪行，`pmem pref remove` 两步一起做；用户不要的偏好则归档，见下。

**怎么存**，发生就存：

1. 查重：对照上下文里的 `## Preferences` 和继承的偏好，再 `pmem find <关键词>`。同义的已在，按下面“被违反时”改那一条。
2. 直接编辑文件：写 `<name>.md`，在 `## Preferences` 段末尾、下一个标题之前加一行，不要加到 `## Memories` 下。在 Claude Code 里改本项目的记忆目录不需要审批，写上级工作区的、跑 `pmem` 可能要。`pmem pref add "<规则>" --name <name> --why "…" --how "…"` 是一步到位的捷径。
3. 本轮回复就照新规则做，同一条消息里的任务照常完成；回复的最后一行写 `✓ preference: <规则>`，写到上级工作区时行尾加 ` (workspace)`。用户否决这一行或说“不要这条偏好了”，就 `pmem archive <name> --reason "user vetoed"`，否决本身就是同意，不再问；只是说法不对就按用户的话改写那一行和 `description`。
4. 索引还没有 `## Preferences` 段（1.0 的记忆）时，照[模板](templates/MEMORY.md)在 HANDOFF 行之后插入 `## Preferences` 标题、它的说明引用块和 `## Memories` 标题再写，并请用户用新版跑一次 `pmem init`；`pmem pref add` 会自己补段。

**存在哪一层**：问一句“这条在本工作区的兄弟项目里也成立吗？”

| 范围 | 写到哪 | 条件 |
| --- | --- | --- |
| 本项目 | 最近的 `.agent-memory/` | 默认；拿不准就放这里，并在回复里说明可以改到工作区级 |
| 整个工作区 | 上一级的 `.agent-memory/`：`pmem pref add … --up`，或手改那份 `MEMORY.md` | 用户说过它普遍适用；规则应以 `When …:` 开头。`--up` 报 `no usable memory dir above` 说明本项目用的就是工作区的记忆，直接写本项目。Codex 从子项目写上级要 `--add-dir <工作区根目录>`，写不进就问 |
| 所有项目 | 全局 `CLAUDE.md`、`AGENTS.md` 里的偏好块：`pmem pref promote NAME` | 只在用户同意后；先 `--dry-run`，问时说出它要写的每个文件，只写用户点名的用 `--only --target <文件>` |

冲突时近者胜：本项目 > 较近的上级 > 全局块。注入的 `## Inherited preferences` 只来自当前用户拥有、别人不可写的上级记忆目录，被跳过的会在会话开始时报 WARNING；`## Preferences recorded outside the section` 列出写在段外的本项目 feedback，照样遵守，并用 `pmem index --fix` 移进段里。偏好也管 skill 规定的输出格式：与 memory-reflect、memory-health 的汇总表等冲突时按偏好来，例如不要表格就改成同样字段的列表。

**被违反时**：做错了已有偏好，或用户说“我说过了”“又这样”“again”，不新建条目：把那一行改得更具体（补上触发情境和一个反例），移到段首，在文件正文 `**Violations:**` 下追加 `- YYYY-MM-DD: <这次怎么错的>`，同步改 `description`。`pmem pref violated NAME --rule "<更具体的规则>" --note "…"` 一次做完，`--rule` 必须与原文不同。只记次数不改规则没用：上下文里只有那一行。被违反的是继承来的（标着 `(from <目录>)`），就改那一份：最近一层用 `pmem pref violated NAME --up --rule …`，任一层用 `pmem -C <那个目录> pref violated NAME --rule …`（NAME 用 `pmem -C <那个目录> pref list` 查），或手改那份 `MEMORY.md`；不要在本项目另建一条。

**提醒 hook**（只有 Claude Code）：消息像长期偏好时附一段提示，只说“可能”；任务指令照做、不存。用户说你违反了已有偏好时，提示先列出最相关的几条规则，按“被违反时”处理。Codex 靠全局块和 memory-reflect 的偏好扫描。

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
| LOW | 新建记忆、加索引行、修死链、给故障表追加一行、更新自己工作流的交接；记下用户刚在本项目说的偏好 | 直接做 |
| MEDIUM | 改写已有记忆；改写别的工作流的交接；合并重复后归档内容已被完全吸收的副本；把已有偏好改得更具体；用户说过普遍适用时把偏好写到工作区级（`--up`） | 做完在回复里说明 |
| HIGH | 归档仍有独有内容的记忆，或支撑某条偏好的记忆；删除、放宽偏好或与之矛盾；`pmem pref promote`、`demote` 改全局偏好（问时说出要写的每个文件）；改 CLAUDE.md、AGENTS.md 等任何指令文件，包括用 `pmem wire` 写块；新建规则或 skill 文件 | 先问，同意后再做 |

- **绝不擅自放宽约束**。某条偏好、记忆或规则挡住了实现，先问用户，不要改掉它。
- 只改项目自己的 `.claude/skills/`、`.agents/skills/`；已安装的插件或全局 skill 副本一律不改，它们的问题记成 feedback 记忆。
- **从不删除**。退役用 `pmem archive NAME --reason "..."`，文件进 `.archive/`，可恢复；交接只更新、不删除，`HANDOFF.md` 不可归档，已删除 worktree 的交接由 memory-health 用 `pmem archive handoffs/<key>` 退役。用户说“忘掉某条记忆”或“不要这条偏好了”，这本身就是同意：`pmem archive`。只删索引行不算忘掉，`pmem index --fix` 会把它加回来。

## 并行工作

- 记忆目录全项目一份，位于主 checkout；交接按工作流分开：主 checkout 用 `HANDOFF.md`，linked worktree 自动用 `handoffs/<worktree 名>.md`（名字规整为小写短横线形式；规整后为 `main` 的用 `worktree-main`，不占主 checkout 的 `HANDOFF.md`）。
- 首选一个 agent 一个 worktree（`git worktree add`）。同一 checkout 里的多个 agent 各用一个 key：用 `PMEM_KEY=<名字>` 启动，如 `PMEM_KEY=agent-b claude`、`PMEM_KEY=agent-b codex`，hooks 和每条 pmem 命令都以它作 `--key` 的默认值，交接是 `handoffs/<名字>.md`，检查点也分开。只在单条命令上加 `--key` 时，hooks 仍按本 checkout 注入交接、写检查点。
- 不改别的工作流的交接，接管按 memory-handoff 的规则来。一事一文件，并发写很少冲突；`MEMORY.md` 由 pmem 加锁修改，手改时先重读，只追加或改自己那一行。
- Codex 在子目录、子仓库或 linked worktree 里启动时，沙箱不允许写上级的 `.agent-memory/`：从项目根目录启动，或 `codex --add-dir <项目根目录>`。不要只加记忆目录：Codex 把每个可写根下的 `.git` 设为只读，私有历史就快照不了。`/tmp` 与 `$TMPDIR` 默认可写，放在那里的项目碰不到这个限制。不论哪个 agent，写不进去就问，绝不存到别处。

## 安装与接线

每个项目一次：git 项目在任意位置运行都会落到主 checkout 根目录；非 git 目录在顶层运行，或 `pmem -C <根目录> init`。

```bash
pmem init      # 创建或复用 .agent-memory/，接线 Claude Code；幂等
pmem status    # 核对接线、hooks、git 模式、Codex 块、偏好
```

`pmem init` 写入 `.agent-memory/`（`MEMORY.md` 含 `## Preferences` 段，默认 `git init` 私有历史；自己的记忆目录和 `MEMORY.md` 组或其他人可写时 `chmod go-w`）、`<git-common-dir>/info/exclude`、`.claude/settings.local.json`（`autoMemoryDirectory`、`autoDreamEnabled: false`、5 个 hooks：SessionStart 注入交接与继承的偏好，Stop、StopFailure、PreCompact 写检查点，UserPromptSubmit 提醒存偏好），以及 `${XDG_DATA_HOME:-~/.local/share}/project-memory/` 下的稳定副本 `pmem.py`（旧版本不会覆盖新版本）与 `projects.txt` 登记。

- 非 git 的工作区目录：init 还会给 2 层以内的子 git 仓库接线。git 之外 Claude Code 只读启动目录的 `.claude/settings.local.json`，所以要在顶层或某个子仓库里启动；在普通子文件夹启动的会话既没有记忆也没有 hooks，`pmem status` 在那里会报 “not effective from this directory”。
- 常用选项：`--track` 不再把记忆排除在项目 git 之外，由你随项目提交；`--here` 在当前子目录建独立记忆；`--no-claude` 不接线 Claude；`--no-hooks` 不接（并移除）hooks；`--allow-dream` 保留 Claude 的后台整理，它可能改写或删除记忆文件。子仓库已接到工作区记忆、又要有自己的记忆时用 `pmem init --here --force`；它随后继承工作区的偏好，init 提示的 `pmem import` 只在要复制工作区的其他记忆时才做。
- init 以 exit 2 拒绝时照提示做；要加 `--force` 先问用户。
- 每台机器、每个 agent 配置各一次：`pmem wire codex` 写入 `${CODEX_HOME:-~/.codex}/AGENTS.md`；`pmem wire claude --config-dir <dir>` 给每个 Claude 配置目录或账号各写一次，默认 `${CLAUDE_CONFIG_DIR:-~/.claude}`；`pmem wire <项目根>/AGENTS.md` 写进项目文件，`pmem status` 两种都认，块是旧版本时报 `from another pmem version`。它改的是指令文件，属 HIGH，用户没有明确要求时先问。`pmem pref promote` 只写进已有这个块的文件，偏好块放在它后面，`wire --remove` 保留偏好块。
- `settings.local.json` 只属于本机，新克隆或新机器上要重新 `pmem init`。私有模式下记忆不随项目 git 走，换机器需自己同步 `.agent-memory/`，它本身就是 git 仓库。

## 迁移

**Claude 自带记忆**，按启动路径分散在 `<config>/projects/<slug>/memory/`：

1. `pmem import --claude --dry-run` 看计划，再去掉 `--dry-run`。用过多个配置目录或账号就加 `--config-dir <dir>`，可重复。上级目录、用户级 `autoMemoryDirectory` 的记忆，以及在子目录启动 Claude 留下的记忆只列为候选：前两种可能属于别的项目，子目录的按 transcript 里的 `cwd` 核实过，没有 transcript 的会注明无法核实。看过后用 `pmem import <dir>` 显式导入。
2. 旧笔记文件：`pmem import --file CLAUDE.local.md` 整体导入为 `imported-notes*` 记忆；之后 CLAUDE.local.md 只留本机配置与指令，改它前先问。
3. 用 [memory-health](../memory-health/SKILL.md) 处理 `needs merge` 的 `*-imported*` 与 `imported-notes*`：合并，拆成一事一文件；导入的 feedback 会进 `## Preferences`，一并改成祈使句规则。再按导入摘要的提示退役旧目录：征得同意后移走，不删除。导入可重复运行，已导入的不会重复。

**旧的 learn / reflect / evolve 体系**。`pmem status` 发现 `.claude/evolve/`，或提到 `evolve-queue` 的规则、skill 时会提示：把 `.claude/evolve/evolve-queue.jsonl` 里未处理的每一行按 memory-learn 变成一条记忆或明确放弃，给用户一张表；征得同意后把旧规则、旧 skill 与 `.claude/evolve/` 移到 `.claude/legacy-harness/`，移动不删除；CLAUDE.md、AGENTS.md 里提到旧命令的段落先问再改。对应关系：`/learn` → memory-learn；`/reflect` 与扫尾 sweep → memory-reflect；`/evolve` → memory-health；故障表与“调试前先查”规则 → 本 skill 的故障格式与读协议。

**从 1.0 升级**。`pmem status` 报稳定副本版本不一致、缺 UserPromptSubmit hook 或缺偏好段时，请用户按顺序做：

1. 先更新每一份装好的副本：每个 Claude 配置目录的插件或 `install.sh --config-dir <dir>`，Codex 的 `install.sh --codex`。留下的 1.0 副本会继续把 feedback 写到段外，也没有 `pmem pref`；1.0 的 `pmem init` 还会去掉项目的 UserPromptSubmit hook、把稳定副本换回 1.0。
2. 每个项目用新版跑一次 `pmem init`，带上当初用过的选项（`--no-hooks`、`--no-claude`、`--here`、`--track`），不带就会接回全部 hooks：补上 `## Preferences` 段、把已有 feedback 行移进去、加 UserPromptSubmit hook、刷新稳定副本。PATH 上的 `pmem` 还是 1.0 时用 `python3 <skills_root>/project-memory/scripts/pmem.py init`。移进段里的 1.0 钩子多是描述，按 memory-health 改写成祈使句规则。
3. 重跑 `pmem wire codex` 和 `pmem wire claude --config-dir <dir>`，让全局块带上偏好的说明（HIGH，先问）；`pmem status` 对旧版的块报 `from another pmem version`。

## 与 CLAUDE.md、CLAUDE.local.md、AGENTS.md 的关系

| 文件 | 放什么 | 谁改 |
| --- | --- | --- |
| 项目 `CLAUDE.md`、`AGENTS.md` | 指令与约定：构建命令、代码规范、流程 | 人；agent 改属 HIGH |
| `CLAUDE.local.md` | 本机配置，如连接信息、本机路径 | 人；旧的学习笔记迁入 `.agent-memory/` |
| 全局 `CLAUDE.md`、`AGENTS.md` | 跨项目的个人指令；`pmem wire` 写入的 project-memory 块，以及 `pmem pref promote`、`demote` 维护的全局偏好块 | 人；两个块由 pmem 维护 |
| `.agent-memory/` | 干活中学到的知识、偏好与交接 | agent，按风险表 |

指令文件说“该怎么做”，记忆记“学到了什么”。在各种记忆位置中，`.agent-memory/` 优先于内置记忆和旧笔记。记忆与指令文件冲突时，指出冲突，问用户以哪边为准，再更新落后的一方。

## 隐私与历史

- 默认私有：`.agent-memory/` 被排除在项目 git 之外，有自己的 `.git` 历史，不推送。pmem 的写命令自动快照，每次 Claude 会话开始也在后台快照一次。历史命令只作用于 `.agent-memory/.git`；它损坏时 pmem 报 “private history broken”，绝不往项目仓库提交。`--no-history` 时记忆只是一个被忽略的目录，项目的 `git clean -fdx` 会把它删掉；有私有历史时只有 `git clean -ffdx` 会删。
- `--track` 需显式选择：记忆目录不再被排除，由你随项目提交，交接与检查点仍被忽略。不写密钥和个人 `user` 事实，提交前跑 `pmem lint --strict`。
- 同一仓库只用一种模式；pmem 警告有 ref 跟踪 `.agent-memory` 时告诉用户：pull 或 checkout 那个提交会静默覆盖私有记忆，之前先 `pmem snapshot`。
- `pmem lint` 会标出疑似密钥；确属误报的行加 `pmem:allow-secret`。已经写进去的值删掉后仍留在私有历史里，`--track` 时还可能在项目提交里：按已泄露处理，请用户轮换凭据。

## CLI 速查

全局选项 `-C DIR` 指定起点目录；每个子命令都有 `--help`；`--key` 的默认值取环境变量 `PMEM_KEY`。退出码：0 正常，1 有问题或没找到，2 用法错误或被拒绝。

| 命令 | 作用 |
| --- | --- |
| `pmem init [--here] [--track] [--no-history] [--no-claude] [--no-hooks] [--allow-dream] [--no-children] [--force]` | 创建或复用记忆目录并接线，幂等；给 1.0 索引补上偏好段并移入 feedback 行 |
| `pmem where` | 打印记忆目录，没有则退出 1 |
| `pmem status [--json]` | 记忆、索引容量、交接、历史、Claude 接线与 hook 脚本、git 模式、Codex 块、偏好（本项目、继承、全局）、待迁移来源 |
| `pmem context [--key K]` | 交接、检查点（当前与上一个会话的；出错停止或晚于交接 10 分钟以上时完整显示，否则一行）、继承的偏好、段外的偏好、其他交接、索引，一次输出 |
| `pmem handoff [show\|path\|list] [--key K]` | 显示当前交接；打印路径，缺则按模板创建；列出全部交接 |
| `pmem add TYPE NAME -d DESCRIPTION [--title T] [--body TEXT \| --stdin] [--force]` | 新建记忆并加索引行，feedback 的进偏好段；`--force` 重写文件，保留已有的偏好行 |
| `pmem find TERM... [--archive]` | 在 name、描述、正文里不分大小写地 AND 搜索，覆盖记忆与交接 |
| `pmem index [--fix]` | 检查索引；`--fix` 补漏、删死链（文件缺失的偏好保留为 `- <规则>`）、把 HANDOFF 行移到首位、把 feedback 行移进已有的偏好段（缺段时提示 `pmem init`）；有未闭合的代码块或注释时拒绝 |
| `pmem lint [--strict] [--stale-days N] [--paths]` | 格式、容量、链接、偏好段（规范行、描述与规则是否一致）、未闭合的代码块与注释、符号链接、密钥、过期检查，`N` 默认 90 |
| `pmem archive NAME... [--reason TEXT]` | 记忆移入 `.archive/` 并删除索引行，删掉偏好时打印那条规则；`handoffs/<key>` 把已结束工作流的交接移入 `.archive/handoffs/`；`HANDOFF.md` 不可归档 |
| `pmem pref list [--all] [--json]` | 本项目、各上级（含被跳过的及原因）、全局块（逐个文件，不一致时警告）的偏好；`--all` 再列出所有登记过的项目 |
| `pmem pref add RULE [--name NAME] [--title T] [--why TEXT] [--how TEXT] [--up] [--force]` | 写一条偏好及其 feedback 文件，就地替换同名的条目；同一规则已存为别的名字时退出 2，`--force` 才替换；`--up` 写到最近的上级记忆 |
| `pmem pref remove NAME\|RULE` | 把其实是项目事实的条目移出偏好段，文件保留并改成 `project` 记忆，在 `## Memories` 下留一行 |
| `pmem pref violated NAME --rule R [--note T] [--up]` | 换成更具体的规则、同步描述、记下日期、移到段首；`--up` 改最近上级里继承来的那条 |
| `pmem pref promote NAME\|RULE [--config-dir DIR]... [--target FILE]... [--keep-local] [--only] [--dry-run]` | 写进每个带 project-memory 块的全局文件（默认 Codex 的 `AGENTS.md` 与 Claude 配置目录的 `CLAUDE.md`）的偏好块，逐个打印写了哪些文件，全部写成功后才移出本地条目；`--only` 只用 `--target`、`--config-dir` 指定的文件；`--dry-run` 只打印要改的文件和改动 |
| `pmem pref demote RULE [--config-dir DIR]... [--target FILE]... [--to-project] [--only] [--dry-run]` | 从全局偏好块删除；部分匹配至少 4 个字符（中日韩 2 个）且只能匹配一条；`--to-project` 先加回本项目 |
| `pmem import [--claude] [--config-dir DIR]... [--file PATH]... [SRC_DIR]... [--dry-run]` | 导入旧记忆，不改来源，可重复运行 |
| `pmem wire codex\|claude\|FILE [--config-dir DIR]... [--remove]` | 在指令文件里写入、更新或移除 project-memory 块，改前备份 |
| `pmem snapshot [-m MSG]` · `pmem history [-n N] [NAME]` | 提交私有历史；看私有历史日志，`N` 默认 20 |
| `pmem diff [--since REV \| --since-health]` | 默认看上次快照以来的增删改；`--since-health` 从上次 memory-health 快照算起 |
| `pmem restore NAME [--rev REV] [--force]` | 从历史恢复一个文件并补回索引行，保留已有的偏好行 |
| `pmem sessions [--since-handoff] [--since ISO] [--tail N] [--config-dir DIR]... [--limit N]` | 列出本项目近期的 Claude 与 Codex 会话记录，含在子目录启动的，每条标出所属工作流；`--since-handoff` 只列当前工作流交接之后的；`--limit` 默认 10 |
