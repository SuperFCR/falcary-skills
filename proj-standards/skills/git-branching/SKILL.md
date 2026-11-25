---
name: git-branching
description: "按 Conventional Branch 1.1.0 规范创建、命名与校验 Git 分支及 git worktree：支持 feature/feat、bugfix/fix、hotfix、release、chore 用途前缀与 ai/claude/codex/copilot/cursor 智能体来源前缀，规范 worktree 并行隔离与安全清理。新建分支、开 worktree、校验分支名或清理已合并分支时使用；Use when creating branches, naming branches, checking Conventional Branch compliance, or managing git worktrees."
---

# git-branching：Conventional Branch 1.1.0 与 Worktree 协作规范

基于 [Conventional Branch 1.1.0](https://conventionalbranch.org/zh/) 规范分支命名与 `git worktree` 并行开发边界。所有规则围绕「人类可读、跨系统大小写一致、CI/CD 与工具可确定性解析」。

> 命令行兜底：优先使用 `repoguard`；未链接到 `PATH` 时直接运行 `python3 <skills目录>/repo-hygiene/scripts/repoguard.py`（本包相对路径 [`../repo-hygiene/scripts/repoguard.py`](../repo-hygiene/scripts/repoguard.py)）。

---

## 1. 分支命名格式（Conventional Branch 1.1.0）

除主干分支外，新分支**必须**采用单层斜杠结构：

```text
<type>/<description>
```

### 1.1 允许的前缀（Types）

**用途前缀（Purpose Prefixes）** — 描述工作意图（推荐团队/多 Agent 接力时默认优先使用）：

| 前缀 | 别名（等价有效） | 用途与示例 |
|---|---|---|
| `feature/` | `feat/` | 新功能或特性增强（`feature/add-login-page`、`feat/upstream-contract`） |
| `bugfix/` | `fix/` | 非紧急缺陷修复（`bugfix/fix-header-bug`、`fix/header-bug`） |
| `hotfix/` | — | 线上/主线紧急修复（`hotfix/security-patch`、`hotfix/issue-123-auth-crash`） |
| `release/` | — | 发布准备分支，允许用 `.` 写版本号（`release/v1.2.0`） |
| `chore/` | — | 依赖升级、文档、构建配置等非功能任务（`chore/upgrade-node-v20.11`） |

**AI 智能体来源前缀（AI Agent Source Prefixes，v1.1.0 新增）** — 识别由编码智能体自主开启的分支：

| 前缀 | 来源智能体 | 示例 |
|---|---|---|
| `ai/` | 供应商中立通用前缀 | `ai/refactor-auth-flow` |
| `claude/` | Anthropic Claude Code | `claude/security-patch` |
| `codex/` | OpenAI Codex | `codex/optimize-query` |
| `copilot/` | GitHub Copilot | `copilot/add-login-page` |
| `cursor/` | Cursor | `cursor/fix-header-bug` |

**主干分支（Trunk Branches）**：`main`、`master`、`develop` 不加前缀；切勿新建与主干同名的分支，始终从主干拉出工作分支。可选环境分支（如 `staging`、`production`）若仓库采用，应在扩展配置里声明，不属于标准形式文法。

### 1.2 用途前缀 vs AI 来源前缀怎么选（禁止叠前缀）

ABNF 文法严格限定只有一个 `/`（`claude/feat/login` **非法**）。按场景二选一：

1. **多 Agent 接力 / 人类主导功能分支 → 用途前缀优先**（如 `feat/episode-planning`、`fix/lock-hash`）。原因：一个分支经常由 Claude Code、Codex 与开发者共同接力完成，用途比首个打开分支的工具更稳定。
2. **单 Agent 无人值守开的 PR / 独立探索分支 → AI 来源前缀**（如 `claude/audit-stage-guard`、`codex/bench-cache`、`ai/draft-schema`）。

### 1.3 严格命名规则与正则

1. **全小写**：`a-z`、`0-9`、连字符 `-`、点号 `.`；禁止大写、下划线 `_`、空格、特殊字符或第二个 `/`。
2. **点号（`.`）边界**：任何分支的 `<description>` 都可以带 `.`（常用于版本号，如 `release/v1.2.0`、`chore/upgrade-node-v20.11`）。
3. **禁止连续或边界分隔符**：不得出现 `--`、`..`、`-.`、`.-`，描述不得以 `-` 或 `.` 开头或结尾。
4. **工单号小写嵌入**：若关联 Issue/工单，统一小写放入描述（如 `PROJ-123` 写为 `feature/proj-123-add-oauth`），一般保持 2–5 个 kebab-case 词。

权威校验正则与常见正反例：

```regex
^(?:main|master|develop|(?:feature|feat|bugfix|fix|hotfix|release|chore|ai|copilot|cursor|claude|codex)/[a-z0-9]+(?:\.[a-z0-9]+)*(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)*)$
```

| 分支名 | 有效 | 说明 |
|---|---|---|
| `feat/add-login-page` / `fix/header-bug` | ✅ | 标准用途前缀 |
| `release/v1.2.0` / `chore/upgrade-node-v20.11` | ✅ | 描述中允许版本点号 |
| `claude/security-patch` / `codex/optimize-query` | ✅ | 1.1.0 智能体来源前缀 |
| `Feature/Add-Login` / `fix/header_bug` | ❌ | 含大写或下划线（大小写不敏感文件系统风险） |
| `feature/new--login` / `release/v1.-2.0` | ❌ | 连续分隔符或点与连字符相邻 |
| `feature/-login` / `feature/login.` / `claude/feat/a` | ❌ | 首尾分隔符或超过一个 `/` |
| `unknown/some-task` | ❌ | 未注册前缀（如需扩展，配置到 `.proj-standards.json`） |

### 1.4 历史分支豁免与项目自定义扩展

- **只管新建、不折腾旧分支**：仓库里已存在且仍在协作的历史分支（如旧项目集成主线 `refactor/v3-slim`、团队个人约定分支等）保持原样，**禁止**擅自重命名或 `branch -D`。只在**新建分支**时严格执行本规范。
- **团队扩展前缀**：确实需要自定义前缀（如 `experiment/`、`dependabot/`）时，在项目根 `.proj-standards.json` 添加 `"allow_branch_types": ["experiment"]` 并同步到 [第三方说明与校验配置](../repo-hygiene/SKILL.md)。

---

## 2. 开分支与 Git Worktree 标准工作流

### 2.1 开新分支前

1. 检查当前仓库状态与已有工作树，确认基准分支无脏改动：

```bash
git status -sb
git worktree list
```

2. 确定基准分支（若用户未指定，先看 `refs/remotes/origin/HEAD`，否则按 `develop` → `main` → `master` 探测）。
3. 校验拟用分支名；若不合规，`repoguard` 会直接给出规范化建议：

```bash
repoguard check-branch "<candidate-branch>"
```

4. 创建并切换（拉取远端更新如失败，立即停止汇报原因）：

```bash
git checkout -b feat/<short-kebab-description> <base-branch>
```

### 2.2 并行开发：一任务一分支一 Worktree

同一本地工作区需要多个 Agent 或多个独立模块并行时，优先用 `git worktree` 物理隔离，而不是在同一个 checkout 里反复切分支或混改文件：

```bash
git worktree add -b feat/<task-slug> ../<repo>_wt_<task-slug> <base-branch>
```

- **清理 Worktree 红线**：
  1. 绝不用 `rm -rf` 硬删 worktree 目录；
  2. 删除前必须检查是否已并入目标分支（`git merge-base --is-ancestor <branch> <target>`）且工作区无未保存/未跟踪重要文件；
  3. **征得用户明确同意后**再执行 `git worktree remove <path>`（以及按需询问是否 `git branch -d <branch>`）。

---

## 3. 风险分级

- 🟢 **直接执行**：只读查询 `git branch` / `git worktree list`、运行 `repoguard check-branch`、按用户要求新建合规分支或 worktree。
- 🟡 **做完说明**：将用户口述中含空格/大写/下划线的分支草稿自动清洗成合规 kebab-case 分支名后再创建。
- 🔴 **必须先问用户**：`git push`（含 `-u` 新建远端分支）、`git branch -D` 强删未合并分支、强制重命名已有远端/协作历史分支、`git worktree remove --force`。

规范来源与署名见 [THIRD_PARTY.md](../../THIRD_PARTY.md)；多人同步与 worktree 生命周期见 [`git-worktree-sync`](../git-worktree-sync/SKILL.md)；提交与原子边界规范见 [`git-committing`](../git-committing/SKILL.md)。
