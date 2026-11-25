---
name: repo-hygiene
description: "治理 Git 仓库卫生、隐私防泄、SemVer 2.0.0 发版与本地/CI 强制校验护栏：区分 .gitignore 与 .git/info/exclude，在提交前拦截私钥、秘钥、本机真实路径与大体积权重，计算下一版本号并生成 changelog 草稿，以及安装零依赖 repoguard Git Hooks 或导出 commit-check/GitHub Rulesets 配置。配置仓库规范、准备发版打 tag 或安装 git hooks 时使用；Use for stage audits, repo hygiene, SemVer releases, Git hooks, or commit-check enforcement."
---

# repo-hygiene：仓库卫生、防泄漏、SemVer 发版与自动化护栏

确保团队协作仓与公开仓**不泄漏本机信息与凭据、不膨胀、可自动校验分支与提交、发版口径一致**。

> 核心 CLI：[`scripts/repoguard.py`](scripts/repoguard.py)（安装时可用 `--link-cli` 链接为 `~/.local/bin/repoguard`，纯 Python 3.9+ 标准库，零外部依赖）。

---

## 1. 私有文件分层：`.gitignore` vs `.git/info/exclude`

| 机制 | 作用域 | 该放什么 | 绝对禁止 |
|---|---|---|---|
| `.gitignore`（提交入库） | 所有克隆与协作者 | 通用的构建产物、依赖缓存、日志/临时实验输出（`__pycache__/`、`.pytest_cache/`、`.venv/`、`.DS_Store`） | 仅属于你这台机器的路径、别人仓库的私有约定文件名 |
| `.git/info/exclude`（仅本机） | 仅当前本地仓库克隆 | 本机私密台账（`CLAUDE.local.md`）、私有环境脚本、本机调试目录 | 期望团队 CI/其他成员也忽略的构建规则 |

- 在任意工作区写入 `CLAUDE.local.md` 前，先用 `git check-ignore -q CLAUDE.local.md || echo CLAUDE.local.md >> .git/info/exclude` 确认其被本机排除，绝不提交入库。
- 提交前若怀疑把本机文件误加到暂存区，跑 `repoguard check-stage`。

---

## 2. 防泄密与大文件拦截（`repoguard check-stage`）

提交前运行一次只读扫描：

```bash
repoguard check-stage
```

默认严格检查暂存区（删除动作 `D` 不拦截，方便清理误提文件）：
1. **私密/高危文件名**：`CLAUDE.local.md`、`secrets.env`、`.env` / `.env.*`（放行 `.env.example` / `.sample` / `.template` / `.schema`）、`*.pem` / `*.key` / `id_rsa` / `id_ed25519` 等。
2. **暂存体积红线**：读取 Git 索引 blob 真实大小；模型权重后缀（`.pt`、`.pth`、`.ckpt`、`.safetensors`、`.onnx`、`.gguf`、`.h5`）超过 **1.0 MiB** 报错，其余文件默认超过 **5.0 MiB** 报错（可通过 `.proj-standards.json` 的 `max_staged_mb` 或 `--max-mb` 调整）。
3. **新增行疑似凭据**：匹配私钥头（`-----BEGIN ... PRIVATE KEY-----`）、常见 GitHub Token、AWS AKIA、API key (`sk-...`)、Tailscale auth key（可在明确无害的测试/文档占位行末尾注释 `repoguard:allow-secret` 显式豁免）。
4. **隐私卫生提示**：公开仓库的示例路径用 `/home/u`、`~` 或占位符，不用本机真实用户名路径或真实个人邮箱（仅用 `noreply` / `@example.com`）。

---

## 3. SemVer 2.0.0 发版工作流

结合 `release/vX.Y.Z` 分支规范与 Conventional Commits 历史自动推导版本：

1. 查看上次 SemVer tag 至今的变更并计算推荐版本号与 CHANGELOG 草稿：

   ```bash
   repoguard suggest-version
   ```

   推导规则：`!` / `BREAKING CHANGE:` → `MAJOR+1`；含 `feat` → `MINOR+1`；仅 `fix` / 其他 → `PATCH+1`。
2. 如项目按分支发版，从主干新建 `release/vX.Y.Z`（校验合乎 Conventional Branch 1.1.0）；更新版本号与发布说明并提交（`chore(release): prepare vX.Y.Z`）。
3. **打 tag 与推送前先问用户**，确认后创建附注标签并按需推送：

   ```bash
   git tag -a vX.Y.Z -m "release: vX.Y.Z"
   ```

---

## 4. 自动化校验护栏与项目扩展配置

### 4.1 可选项目配置 `.proj-standards.json`

放在仓库根目录，用于放行团队自定义分支/提交类型或调整体积阈值（其余保持默认严谨口径）：

```bash
repoguard print-config proj-standards > .proj-standards.json
```

支持字段：`allow_branch_types`、`allow_commit_types`、`max_staged_mb`、`private_files_extra`。

### 4.2 本机 Git Hooks（安装前先问用户）

支持零依赖安装 `pre-commit`（`check-stage`）、`commit-msg`（`check-commit`）与 `pre-push`（`check-branch`），自动适配普通仓库与 `git worktree`：

```bash
repoguard install-hooks
```

若目标路径已有用户自己的非 `repoguard` hook，脚本会**默认拒绝覆盖**（exit 1）并提示加 `--force` 或手动合并；未取得用户确认不擅自写 `.git/hooks/`。

### 4.3 团队/CI 强制校验配置导出

直接导出与 `Conventional Branch 1.1.0` 官方一致性完全对齐的配置文件：

```bash
repoguard print-config commit-check     # 生成 commit-check.toml 片段（配合 commit-check / pre-commit / GitHub Actions）
repoguard print-config github-ruleset   # 生成 GitHub Repository Ruleset 正则校验 JSON
```

---

## 5. 风险分级

- 🟢 **直接执行**：`repoguard check-stage` / `check-branch` / `check-commit` / `suggest-version` / `print-config`、`git check-ignore`。
- 🟡 **做完说明**：向 `.git/info/exclude` 追加 `CLAUDE.local.md` 等本机私有配置排除规则。
- 🔴 **必须先问用户**：运行 `repoguard install-hooks`（尤其带 `--force`）、修改项目根 `.gitignore` / `.proj-standards.json`、创建并推送 `git tag`、执行 `git push`。

详细三方规范许可归属见 [THIRD_PARTY.md](../../THIRD_PARTY.md)。
