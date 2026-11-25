# Skills：把 Git 仓库与工程规范交给 Agent 维护

每个目录是一个 Agent Skill（`SKILL.md` + frontmatter），把分支命名（[Conventional Branch 1.1.0](https://conventionalbranch.org/zh/)）、约定式与里程碑原子提交（Conventional Commits 1.0.0）、共享工作区防误提交与仓库卫生落地成可直接执行的护栏。

| Skill | 什么时候用 | 典型说法 |
| --- | --- | --- |
| [git-branching](git-branching/SKILL.md) | 新建分支、起 git worktree、校验或建议合规分支名、安全清理已合并 worktree | “开个新分支”“按规范起个 worktree”“检查分支名合不合规” |
| [git-committing](git-committing/SKILL.md) | 完成大模块后按包边界原子提交、写 Conventional Commit、多人/多 Agent 共享工作区防误提 | “把这个大 part 提交了”“只提交我的文件，别动暂存区”“整理 commit 历史” |
| [repo-hygiene](repo-hygiene/SKILL.md) | 提交前扫描私密文件/密钥/大文件、区分 `.gitignore` 与 `.git/info/exclude`、SemVer 发版、安装 hooks/CI | “准备发版算下版本号”“给仓库装 git hooks”“导出 commit-check 配置” |

## 安装

**Claude Code 插件（推荐）**：在 Claude Code 中执行

```text
/plugin install proj-standards --marketplace SuperFCR/falcary-skills
```

**复制到本人 skills 目录**（Claude Code、Codex 与 Antigravity 通用）：

```bash
git clone https://github.com/SuperFCR/falcary-skills.git && cd falcary-skills/proj-standards
./install.sh --link-cli              # → ${CLAUDE_CONFIG_DIR:-~/.claude}/skills，并链接 ~/.local/bin/repoguard
./install.sh --codex                 # → ${CODEX_HOME:-~/.codex}/skills
./install.sh --antigravity           # → ${ANTIGRAVITY_HOME:-~/.gemini/config}/skills
./install.sh --target DIR            # 其他 agent 的 skills 目录
```

已存在的同名 skill 会先安全备份到 `<目标目录>.bak/<name>-<时间>`。完整文档见[中文 README](../README.zh-CN.md)。

## 约定

- **分支命名**：只对新建分支严格执行 Conventional Branch 1.1.0（`<type>/<description>`，全小写 kebab-case + 可选版本号点号），不强迫改仓库既有历史分支；默认用途前缀优先（`feat/`、`fix/`、`hotfix/`、`release/`、`chore/`），单 Agent 独立分支可用 `ai/`、`claude/`、`codex/`、`copilot/`、`cursor/`，二者不叠加。
- **里程碑原子提交**：每完成一个大 Part/独立包且验证通过即独立提交；多会话共享工作区严禁盲目 `git add -A`，已有其他暂存条目时用精确路径 + `git commit --only -- <paths>`。
- **高危操作先问**：`git push`、强推、历史改写、`git reset --hard`、`git branch -D`、`git worktree remove`、覆盖自定义 git hook 一律先说明影响并征得用户同意。
