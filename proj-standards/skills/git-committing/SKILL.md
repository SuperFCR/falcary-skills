---
name: git-committing
description: "管理 Git 原子提交与里程碑包边界：结合 Conventional Commits 1.0.0 与大 Part 完成后立即独立 commit 纪律，在多 Agent/共享工作区中通过 git diff --cached 前置核查、git commit --only 局部提交与 repoguard check-stage 严防误提交与破坏历史。编写 commit、完成阶段性模块、提交代码或回退提交时使用；Use when committing changes, splitting milestones into commits, or handling shared working trees."
---

# git-committing：Conventional Commits、里程碑原子提交与共享工作区防误触

解决两个核心工程问题：**大 Part / 包边界长期堆在工作区混提交导致难以按模块回退与发版**，以及**多 Agent 并行工作区误用 `git add -A` 把他人改动或已有暂存内容混进本次提交**。

> 命令行工具：[`../repo-hygiene/scripts/repoguard.py`](../repo-hygiene/scripts/repoguard.py)（安装后为 `repoguard`）。

---

## 1. 铁律：每一个大 Part 完成后立即按模块单独 Commit

1. **按 package / 里程碑边界拆分提交**：每完成一个相对独立的大模块或里程碑（如新增一套 CLI、独立子包、Schema + 实现 + 单测闭环）并通过对应验证后，**立即做一次独立 `git commit`**，不要等好几个大模块全部改完再混成一锅提交。
2. **先验后提**：代码/配置变更提交前先跑对应模块的轻量测试或静态检查；文档单独提交时核对链接与数据来源一致。
3. **只本地提交、按指令推送**：除非用户明确要求 `git push` 或“顺便推到远端”，否则默认只做本地 commit 并回报短 hash 与包含文件范围。

---

## 2. 提交格式（Conventional Commits 1.0.0）

标准头格式：

```text
<type>(<scope>)[!]: <description>
```

| `type` | 适用场景 | 常配分支前缀（`git-branching`） |
|---|---|---|
| `feat` | 新增功能、模块或对外契约 | `feature/`、`feat/` |
| `fix` | 修复缺陷 | `bugfix/`、`fix/`、`hotfix/` |
| `refactor` | 重构（既不新增功能也不修缺陷） | `feature/`、`chore/` |
| `perf` | 性能优化 | `feature/`、`chore/` |
| `docs` | 仅文档/注释变更 | `chore/` |
| `test` | 补充或修正测试（未改生产逻辑） | `chore/`、`feat/` |
| `build` / `ci` | 构建依赖、打包、CI workflow | `chore/` |
| `chore` | 常规杂务、脚手架、发版说明 | `chore/`、`release/` |
| `revert` | 回退既有提交 | `fix/`、`hotfix/` |

- **`<scope>`（强烈推荐）**：写清包或模块边界，全小写（如 `feat(upstream): ...`、`fix(cli): ...`、`docs(roadmap): ...`），便于按 package 追踪变更与生成 CHANGELOG。
- **破坏性变更**：header 在 `:` 前加 `!`（如 `feat(api)!: drop v1 payload`），或在正文空一行后写脚注 `BREAKING CHANGE: <说明>`；发版工具会据此推导 `MAJOR` 升级。
- **标题精简**：不超过 72 个字符，祈使句或清晰动宾短语；不要出现无意义的 `wip`、`update`、`fix bug`。

用 CLI 本地快速校验：

```bash
repoguard check-commit "feat(upstream): add admission guard and asset schema"
```

---

## 3. 多 Agent / 共享工作区防误提交协议（必做 5 步）

在一个工作区里有多个 Agent 或用户自己同时留有未提交改动/既有暂存区内容时，**严禁执行 `git add -A` 或 `git add .`**：

1. **同时看工作区与暂存区**：

   ```bash
   git status -uall --short
   git diff --cached --name-status
   ```

2. **精确暂存本人路径**：只针对本次任务负责的文件/目录执行 `git add -- <own-paths>`；无关修改（M）、未跟踪草稿（??）、以及他人早已放进暂存区的重命名/增删（如预先 staged 的 `R100`）一律不碰。
3. **暂存区审计（防私钥/秘钥/本地文件/超大产物）**：

   ```bash
   repoguard check-stage
   ```

4. **用 `--only` 隔离提交（关键护栏）**：当步骤 1 发现暂存区**已经存在不属于本次任务的条目**时，若直接 `git commit -m ...` 会连别人的暂存改动一起吃进去！必须使用：

   ```bash
   git commit --only -m "<type>(<scope>): <description>" -- <own-paths>
   ```

   `--only` 会基于 `HEAD` + 本次点名的 `<own-paths>` 构建临时索引提交，使既有的其他暂存改动完好留在索引中。
5. **提交后双重验证**：核对新提交只包含自己的文件、且原有暂存区未被破坏：

   ```bash
   git show --stat --name-status HEAD
   git diff --cached --name-status
   ```

---

## 4. 风险分级与不可逆操作红线

| 等级 | 操作 | 原则 |
|---|---|---|
| 🟢 直接执行 | `git status`/`log`/`diff`、`repoguard check-commit`/`check-stage`、只针对自己负责路径的本地 `git commit --only` | 安全可追踪 |
| 🟡 做完说明 | 拆分混合改动为多笔局部提交、将过长/不规范提交说明整理为 Conventional Commits | 向用户回报拆分与 commit hash |
| 🔴 必须先问用户 | `git push`、`git push --force` / `--force-with-lease`、`git reset --hard`、`git checkout --` / `git restore` 丢弃未提交改动、`git clean -fdx`、`git branch -D`、修改已推送历史（已推送历史优先 `git revert`） | 先说明影响文件/提交与可逆性 |

分支与 worktree 配合规范见 [`git-branching`](../git-branching/SKILL.md)；密钥防护与自动化 Hooks 配置见 [`repo-hygiene`](../repo-hygiene/SKILL.md)。
