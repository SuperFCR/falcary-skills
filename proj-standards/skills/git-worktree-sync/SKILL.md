---
name: git-worktree-sync
description: "轻量化多人 Git 协作同步与 git worktree 全生命周期规范：区分 2 层主干流与 3 层漏斗流（main ↔ 个人主线 ↔ 本机并行 worktree），通过 repoguard worktree-status 一键体检漂移与合并态，规范向下吸收 main、merge/rebase 边界、多 Agent 目录防撞与安全拆除。多人协作同步、并行开多个 worktree、解分叉冲突或清理旧 worktree 时使用；Use for multi-developer Git sync, parallel worktrees, drift checks, or safe worktree cleanup."
---

# git-worktree-sync：轻量多人同步与 Worktree 协作流

把两件事分开，零仪式感、开箱即用：
- **Worktree** 管「同一台机器上的并行干活」（多任务 / 多 Agent 物理隔离桌面，共用同一份 `.git`）；
- **远端分支 + 双向同步** 管「多人 / 多机协作」（通过 `main` 汇合，不互相往对方分支强塞）。

> 体检 CLI：[`../repo-hygiene/scripts/repoguard.py`](../repo-hygiene/scripts/repoguard.py)（安装后为 `repoguard`）。分支命名细则见 [`git-branching`](../git-branching/SKILL.md)，局部提交护栏见 [`git-committing`](../git-committing/SKILL.md)。

---

## 1. 选协作模型（二选一）

| 模型 | 适用场景 | 拓扑 |
|---|---|---|
| **2 层主干流** | 单人 / 小改动 / 快速 PR | `main` ↔ 本机短期 `wt: feat/*` |
| **3 层漏斗流** | 多人并行大模块、每人带多 Agent | `main`（公共真相源）↔ 个人/模块主线（如 `dev/alice`、`dev/bob`）↔ 各自本机 `wt: feat/*`（合完即删） |

- **只往上一层合**：`wt` → 个人主线（测试通过）→ `main`。
- **目录尽量正交**：协作者按包/目录划分责任田、用接口/schema 对接，合并几乎零冲突。

---

## 2. 日常 5 条命令

```bash
# 1. 全局体检（所有 worktree 分支、脏状态、落后 origin/main 多少、是否已合入可删）
repoguard worktree-status

# 2. 开新 worktree（默认兄弟目录，避免污染主仓 glob/pytest；若仓内已有 .worktrees/ 则沿用并确保已忽略）
git worktree add -b feat/<slug> ../<repo>_wt_<slug> <base-branch>

# 3. 向下同步主线（任一 worktree 执行 fetch，全机所有 worktree 瞬间共享最新 origin/*）
git fetch origin
git merge origin/main            # 在个人主线里消化队友刚合进 main 的改动

# 4. 收尾前检查是否已并入目标分支
git merge-base --is-ancestor <branch> <target-branch>

# 5. 征得用户同意后，从主仓安全拆除（绝不用 rm -rf）
git worktree remove ../<repo>_wt_<slug> && git worktree prune
```

### 环境引导边界（新建 worktree 时按需执行）

- **只读依赖**（如兼容的 `.venv`）：可软链共享，避免重复安装。
- **可变本机配置**（`.env`、`CLAUDE.local.md`、本地数据库/锁文件）：**按需复制**，绝不软链共享状态；且确保被 `.git/info/exclude` 排除。
- **防套娃**：创建前确认不在另一个临时 worktree 目录里嵌套创建（查看 `git rev-parse --git-dir` 与 `--git-common-dir`）。

---

## 3. 同步铁律（4 条）

1. **同一分支不能双检**：所有 worktree 共用 `.git`，同一本地分支只能在一个 worktree 检出。
2. **向上交卷 + 向下吸收**：谁合了 `main` 通知队友；另一方及时把 `origin/main` 合并回个人主线，**冲突只在个人线解，永远不带到 `main` 上**。
3. **`merge` vs `rebase` 边界**：**已推送或多人共享的分支（`main`、个人主线）只用 `merge`，严禁 `rebase` + `force push`**；仅纯本机未推送的短期 `wt` 可按需 `rebase`。
4. **长周期 worktree 中途同步**：跨越多次 `main` 更新的长任务 `wt`，中途从父分支 `git merge <base>` 一次，别攒到最后爆大冲突。

---

## 4. 多 Agent 并行防撞（3 条）

1. **开工前先对齐**：跑 `repoguard worktree-status` 确认目标分支无人占用；进入 worktree 后跑 `git rev-parse --show-toplevel` 确认当前目录正确，不误改主目录。
2. **同文件串行改**：两个并行任务要改同一文件时改为串行，不要同时开两个 worktree 硬改。
3. **测试绿灯再合回**：每个 `wt` 合回上级线前先跑对应测试 + `repoguard check-stage`，再按 [`git-committing`](../git-committing/SKILL.md) 提交。

---

## 5. 风险分级

- 🟢 **直接执行**：`repoguard worktree-status`、`git worktree list`、`git fetch`、`git merge-base --is-ancestor`、`git rev-parse`、按用户要求开合规新 worktree。
- 🟡 **做完说明**：在个人主线或独立 `wt` 内做无冲突的向下 `git merge` 同步、软链 `.venv` 或复制被忽略的本机配置。
- 🔴 **必须先问用户**：合并进 `main`、`git push`、`git worktree remove`（尤其 `--force`）、`git branch -D`、任何 `rebase` 已推送分支或 `--force` 操作。
