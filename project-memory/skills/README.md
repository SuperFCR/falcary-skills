# Skills：项目记忆交给 Agent 维护

每个目录是一个 [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)（`SKILL.md` + frontmatter）。记忆放在项目自己的 `.agent-memory/` 里，Claude Code、Codex 和不同账号读写同一份；换 agent、换账号或会话因额度用尽中断后，新会话读一下项目记忆就能接着干。你说过一次的做事偏好进入 `MEMORY.md` 的 `## Preferences` 段，之后每个会话、每个 agent 都照做。装好后直接用自然语言提需求，例如“记住这个”“以后回复都用中文”“今天先到这”“换 Codex 接着做”。

| Skill | 什么时候用 | 典型说法 |
| --- | --- | --- |
| [project-memory](project-memory/SKILL.md) | 总规则：记忆在哪、怎么读、存什么、偏好、风险分级、安装接线、迁移与升级、`pmem` 速查 | “给这个项目建记忆”“迁移 Claude 的记忆” |
| [memory-learn](memory-learn/SKILL.md) | 当场记一条：偏好优先，其次是确认的做法、反复失败的故障、新约束；违反过的偏好改得更具体 | “记住这个”“以后别这样”“我说过了”“remember this” |
| [memory-reflect](memory-reflect/SKILL.md) | 收尾或提交前回顾整段对话：必做偏好扫描，批量写记忆并刷新交接 | “今天先到这”“收尾”“wrap up” |
| [memory-handoff](memory-handoff/SKILL.md) | 交出与接手任务状态；里程碑、长任务前、停下前随手更新 | “交接一下”“换个账号继续”“继续上次的任务” |
| [memory-health](memory-health/SKILL.md) | 体检与整理：核实、合并、拆分、归档、压缩索引，审一遍偏好 | “整理记忆”“memory health” |

日常顺序：会话开始读交接 → 干活中随时 memory-learn、里程碑时 memory-handoff → 收尾 memory-reflect；每周或 lint 报警时 memory-health。

## 安装

**Claude Code 插件（推荐）**：在 Claude Code 中执行

```text
/plugin install project-memory --marketplace SuperFCR/falcary-skills
```

较早版本分两步：`/plugin marketplace add SuperFCR/falcary-skills`，再 `/plugin install project-memory@falcary-skills`。

**复制到本人 skills 目录**（Claude Code 与 Codex 通用）：

```bash
git clone https://github.com/SuperFCR/falcary-skills.git && cd falcary-skills/project-memory
./install.sh --link-cli --wire                   # → ${CLAUDE_CONFIG_DIR:-~/.claude}/skills/
./install.sh --codex --wire                      # → ${CODEX_HOME:-~/.codex}/skills/
./install.sh --config-dir ~/.claude-work --wire  # 每个 Claude 配置目录或账号各一次，可重复
./install.sh --target DIR                        # 其他工具的 skills 目录
```

已存在的同名 skill 会先移到 `<目标目录>.bak/<name>-<时间>`。`--link-cli` 把 `~/.local/bin/pmem` 链接到装好的 `pmem.py`；`--wire` 在全局 `CLAUDE.md` 或 `AGENTS.md` 里写入 project-memory 块，等同于 `pmem wire claude` / `pmem wire codex`。

**每个项目一次**，git 仓库里任意位置都行，非 git 目录在顶层运行：

```bash
pmem init             # 建 .agent-memory/，接线 Claude Code；从 1.0 升级时也跑一次
pmem import --claude  # 导入 Claude 按路径存放的旧记忆，可先加 --dry-run
pmem status           # 核对接线
```

没有 `pmem` 命令时用 `python3 <skills目录>/project-memory/scripts/pmem.py`。插件不会把 `pmem` 放到 PATH 上：在任一项目 `pmem init` 之后，可以把它装好的稳定副本 `${XDG_DATA_HOME:-~/.local/share}/project-memory/pmem.py` 链接到 PATH 上；Claude Code 会话开始注入的上下文里也写着这个路径。

完整说明见[中文文档](../README.zh-CN.md)。

## 约定

- 记忆一事一文件，带 `name` / `description` / `metadata.type` / `metadata.modified` frontmatter，`MEMORY.md` 里一行索引；交接只放当前任务状态。
- 偏好就是 `feedback` 记忆，索引行 `- [标题](name.md) — <规则>` 放在 `## Preferences` 段；agent 直接改文件存下，回复末尾写 `✓ preference: <规则>` 供你否决，否决的规则会被归档。被违反的偏好改得更具体、移到段首。上级目录的偏好被子项目继承，冲突时近者胜；写进全局偏好块（`pmem pref promote`）要你同意，问的时候会列出要改的每个文件（`--dry-run`），`--only --target` 只写你点名的文件。只有 Claude Code 有提醒存偏好的 hook，Codex 靠指令和 memory-reflect。
- 从 1.0 升级：先更新每一份装好的副本（每个 Claude 配置目录、`install.sh --codex`），再在每个项目带上当初的选项（`--no-hooks`、`--no-claude`、`--here`、`--track`）跑 `pmem init`，最后重跑 `pmem wire`；`pmem status` 会标出旧版本的全局块。
- 新建记忆、记下你刚说的偏好、更新自己的交接直接做；改写已有记忆或偏好做完说明；归档仍有独有内容或支撑偏好的记忆、删除或放宽偏好与约束、改全局偏好、改 CLAUDE.md / AGENTS.md 等指令文件（包括 `pmem wire`）、新建规则或 skill 一律先问。
- 从不删除：退役用 `pmem archive`，默认有私有 git 历史可恢复。
- 不写密钥、令牌、密码的值，只记存在哪里；记忆默认被排除在项目 git 之外；`pmem init --track` 不再排除，由你随项目提交，同一仓库只用一种模式。
- skill 正文用中文，对任何 agent 都适用；脚本只用 Python 3.9+ 标准库。
