# overleaf-ctl — Overleaf Git-enabled 本地同步与编译 CLI

`overleaf-ctl` 把 Overleaf 写作流变成「本地 VSCode 编辑 + git 双向同步 + 本地 TinyTeX 编译」：

- **同步**走 Overleaf Git-enabled 项目的原生 git（`https://git.overleaf.com/<project-id>`）。这是 Overleaf premium integration 能力，不适用于没有 Git/GitHub integration 权限的免费项目。
- **编译**在本地用 TinyTeX（对齐 Overleaf 的 pdflatex 引擎）跑 `latexmk`，缺包自动 `tlmgr install` 补上。
- **sync 语义是自动提交式（类 Dropbox）**：auto-commit → `pull --rebase` → `push`。冲突时**永远停下让你手动解决，绝不自动覆盖**。

`overleaf-ctl --version` 应输出 **1.1.0**。

同步保护和新增 writing/check-push 命令以 [工具层入口](../SKILL.md) 为准；sync/push 指定 origin 的当前 upstream 分支，并检查本地记录泄漏。

## 环境准备

`<skill_dir>` 指根入口 SKILL.md 所在的完整安装目录。依赖与安装以 [README](../../README.md#接入与使用) 为准。已有命令可用时直接使用；命令不在 PATH 时先尝试该目录的 `.venv/bin/overleaf-ctl`（Windows 为 `.venv/Scripts/overleaf-ctl.exe`），不要误判成必须重装。

新安装使用 Python ≥ 3.10 和 Git：

```bash
python3 <skill_dir>/scripts/install.py --link-skill codex --link-cli
python3 <skill_dir>/scripts/install.py --check
```

安装器仅准备 CLI 的独立虚拟环境和运行依赖。Node/npm 可选，TeX 编译需要另备引擎和 latexmk；tlmgr 只用于 TeX Live/TinyTeX 自动补包。不会因安装 CLI 而安装 TeX、改全局 pip 配置或登录账号。测试依赖显式用 `--dev`，pip 源可显式用 `--index-url`。

Git 凭据使用对 Overleaf 主机生效的 credential helper：已有配置（通用或该主机专属）原样沿用；没有时只为该主机配置平台默认值（macOS 为 Keychain，Windows 为 Git Credential Manager，Linux 为内存 cache），不改动全局通用的 `credential.helper`。令牌不进入项目 registry、档案或仓库。同步需要对应 Overleaf 项目的 Git 访问权限；本地写作档案初始化不需要远端。

## 命令速查

```bash
overleaf-ctl login [--host H] [--token-stdin]            # 存 git token（已有凭据时打码显示并确认覆盖）
overleaf-ctl logout [--host H] [--yes]                   # 显示当前凭据（打码）并确认后删除
overleaf-ctl clone <url> <别名> [--path DIR]             # git clone（默认 ~/overleaf/<别名>）+ 登记 registry
overleaf-ctl register <路径> <别名>                       # 已有本地 git 仓库登记进 registry
overleaf-ctl list                                         # 表格：别名 / 路径 / 远端 / 状态
overleaf-ctl sync <别名> [--no-commit] [--message M]      # 核心：auto-commit → pull --rebase → push
overleaf-ctl pull <别名>                                  # git pull --rebase
overleaf-ctl push <别名>                                  # git push
overleaf-ctl status <别名>                                # 工作区状态 + ahead/behind + 冲突文件
overleaf-ctl open <别名>                                  # code <path>（VSCode 打开）
overleaf-ctl compile <别名> [--main F] [--engine E] [--open] [--no-auto-install]  # 本地 latexmk 编译
overleaf-ctl writing init <别名>|--path DIR [--scaffold] [--local-only P]  # 写作档案、空中文稿、.writing Git 仓库
overleaf-ctl writing status <别名>|--path DIR             # 各章中文稿与 TeX 的对齐状态
overleaf-ctl writing align <别名>|--path DIR -s 章节 | --all   # 记录章节中英已一致
overleaf-ctl writing log <别名>|--path DIR -m 说明        # 追加本轮日志并提交 .writing
```

- 针对单项目的命令第一个参数都是**别名**；别名不存在时会列出已登记的别名。
- registry 在 `~/.config/overleaf-sync/projects.json`（目录 0700 / 文件 0600），只存 `path`/`remote`/`main`/`engine`，**不存 token**。

## 认证（一次性，token 交给 credential helper）

```bash
overleaf-ctl login                       # 隐藏输入读取 token（推荐）
overleaf-ctl login --token-stdin         # 从 stdin 读 token（CI / 脚本）
echo "$OVERLEAF_TOKEN" | overleaf-ctl login --token-stdin
overleaf-ctl login --host git.overleaf.com   # 默认 host，自建实例可覆盖（v1 只验证 overleaf.com）
```

`login` 做两件事：

1. 确认有 credential helper 对该主机生效（`git config --get-urlmatch credential.helper https://<host>`）。已有则不动；没有时只写该主机的 `credential.https://<host>.helper`（mac→`osxkeychain`，win→`manager`，linux→`cache --timeout=2592000`），GitHub 等其他主机的凭据行为不受影响。
2. 把 `username=git` + `password=<token>` 喂给 `git credential approve`，由该 helper 保存。之后所有 git 操作免输入。token 失效/更换：重跑 `overleaf-ctl login` 覆盖即可。

Linux 默认的 `cache` 只把 token 留在内存：超时（30 天）或缓存进程退出（重启、WSL 关闭）后要重新 `login`，命令会给出提示。需要持久保存时，先自行为该主机选定 helper 再 `login`，例如 `git config --global credential.https://git.overleaf.com.helper store`（明文写入 `~/.git-credentials`）或 libsecret。

已登录时再次 `login` 会显示当前凭据（打码）并询问覆盖；`overleaf-ctl logout [--yes]` 删除凭据。

> 出现 `401`/`403`/`Unauthorized` → 提示先 `overleaf-ctl login`（或 token 过期，重跑 login 覆盖）。

## 典型流程

**首次接入一个 Overleaf 项目：**
```bash
overleaf-ctl login                                       # 1. 存 token（一次性）
overleaf-ctl clone https://git.overleaf.com/<项目id> mypaper   # 2. clone + 登记
overleaf-ctl open mypaper                                # 3. VSCode 打开开始写
```
**URL 换算**：用户常给的是网页地址 `https://www.overleaf.com/project/<项目id>` 或 `https://cn.overleaf.com/project/<项目id>`，它本身不能 clone；`clone` 会自动换成 `https://git.overleaf.com/<项目id>`（cn 站与主站共用同一个 git 端点），registry 记录换算后的地址。只读分享链接（`/read/...`）不含项目 id，无法换算。别名已被登记时 `clone` 在克隆前就报错，不会留下未登记的目录。`--path DIR` 表示 DIR 就是项目根目录本身（不会再建别名子目录）。

已经在本地 clone 过的仓库，用 `overleaf-ctl register <路径> <别名>` 登记即可（会校验是 git 仓库且 remote 指向 overleaf）。

**日常同步（写完一轮）：** 已 `writing init` 的项目，`sync` 会先用同一条说明提交 `.writing`（有上游时再拉取并推送），再同步论文；`pull` 也会拉取 `.writing` 并刷新 `status.md`。
```bash
overleaf-ctl sync mypaper                                # auto-commit → pull --rebase → push
overleaf-ctl sync mypaper --message "改 intro"            # 自定义提交信息
overleaf-ctl sync mypaper --no-commit                    # 不自动提交（要求工作区已 clean，否则拒绝执行）
```

**本地编译出 PDF：**
```bash
overleaf-ctl compile mypaper                             # 默认 pdflatex，缺包自动补，最多重试 30 轮
overleaf-ctl compile mypaper --open                      # 编译后用系统默认程序打开 PDF
overleaf-ctl compile mypaper --main paper.tex --engine xelatex   # 指定主文件 / 引擎
overleaf-ctl compile mypaper --no-auto-install           # 关掉缺包自动补
```
主文件探测顺序：registry 里的 `main` / `--main` > 找同时含 `\documentclass` 与 `\begin{document}` 的 `.tex`（先扫根目录，根目录没有再递归扫子文件夹，跳过 `.git`/`.outputs`/`.writing`——子文件夹布局如 `T2V/main.tex` 开箱即用）。多个候选时交互式列编号让用户选，选择记入 registry 下次不再问；非交互场景报错列候选，用 `--main` 指定。缺少 TeX 工具时，按 README 准备相应发行版；CLI 安装器不安装 TeX。

编译产物统一写到项目内 `.outputs/`：`main.pdf`、`main.aux`、`main.log`、`main.bbl` 等都不再落在项目根目录。克隆、登记、编译和同步会在 `.git/info/exclude` 写入 `.*`：任意层级以 `.` 开头的文件和文件夹（如 `.vscode/`、`.env`、`sec/.cache/`、`.writing/`、`.outputs/`）都保留在本地，不参与同步；普通 `.tex`、`.bib`、`.pdf` 扩展名不受此规则影响。已跟踪或强制加入提交的隐藏文件会在提交/推送检查中被拦截，不会自动删除或取消跟踪。

### TeX 缺包自动/手动处理

默认 `overleaf-ctl compile` 会自动补 TeX 包：`latexmk` 失败后读取 `.outputs/<main>.log` 和 `.outputs/<main>.blg`，解析缺失的 `.sty` / `.cls` / `.fd` / `.tfm` / `.bst` 文件，然后执行 `tlmgr search --global --file "/<missing-file>"` 找包名，再 `tlmgr install <package>`，最多重试 30 轮。TeX 遇到第一个缺失文件就停，所以通常一轮只补一个包；新模板缺包多时可先批量安装以节省时间。找不到 tlmgr、`tlmgr install` 失败或缺失文件不属于 TeX Live 时，命令照常输出编译日志末尾，并在其后说明自动补包停下的原因。

手动排查和补包：

```bash
overleaf-ctl compile mypaper --no-auto-install
tail -80 ~/overleaf/mypaper/.outputs/main.log
tail -80 ~/overleaf/mypaper/.outputs/main.blg 2>/dev/null || true

tlmgr search --global --file "/multirow.sty"
tlmgr install multirow

# TinyTeX 不在 PATH 时直接调用：
~/Library/TinyTeX/bin/*/tlmgr search --global --file "/multirow.sty"
~/Library/TinyTeX/bin/*/tlmgr install multirow
```

如果日志是 `File \`xxx.sty' not found`、`I couldn't open style file xxx.bst`、字体 metric 缺失，优先按上面查包安装。如果是 `Undefined control sequence`、`Missing $ inserted`、`Runaway argument`，通常是论文源码语法问题，不要误判成缺包。

## 冲突处理（sync 的关键路径）

`overleaf-ctl sync` **绝不 abort、绝不 force**。`pull --rebase` 遇冲突时保留 rebase 现场并退出（非 0），打印冲突文件：

1. 在 **VSCode 里逐个解决冲突文件**（搜 `<<<<<<<`），`git add` 标记已解决。
2. 重跑 `overleaf-ctl sync <别名>`：它检测到未完成的 rebase 会自动 `git rebase --continue` 然后 `push`。
3. 如果还有未解决冲突（`git diff --diff-filter=U` 非空），sync 会再次打印冲突文件并提示「解决后重跑 overleaf-ctl sync <别名>」。

绝不替用户 `git stash` 或丢弃改动。`--no-commit` 且工作区脏时，sync 直接拒绝并提示先 `git commit`/`git stash`。
