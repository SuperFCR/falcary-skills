<div align="center">

# 🧰 falcary-skills

**A small, growing collection of agent skills.**<br/>
Each folder stands on its own and plugs into Claude Code, Codex, or any agent that reads `SKILL.md`.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Skills](https://img.shields.io/badge/skills-8-brightgreen.svg)
![Agents](https://img.shields.io/badge/agents-Claude%20Code%20%7C%20Codex-8A2BE2.svg)
[![pocketrig CI](https://github.com/SuperFCR/falcary-skills/actions/workflows/pocketrig.yml/badge.svg)](https://github.com/SuperFCR/falcary-skills/actions/workflows/pocketrig.yml)

**English** · [简体中文](#简体中文)

</div>

---

## 📦 What's inside

| Folder | Skills | What it does | Version |
| :-- | :-: | :-- | :-: |
| [**overleaf-ctl**](overleaf-ctl/) | 1 | Write LaTeX papers with an agent: guarded two-way sync with Overleaf, local compilation, and a Chinese-draft → English-TeX workflow with a log of every writing round. | `1.1.0` |
| [**paper-writing**](paper-writing/) | 1 | Paper figures: crop your exported PPT/Keynote PDF pages to vector figures, audit them at print width, insert/update them in your `.tex` with captions, and verify the compiled pages (`paper-drawing`). | `0.1.0` |
| [**pocketrig**](pocketrig/) | 6 | Turn an always-on computer into your AI coding workstation: reach it from your phone and Mac over Tailscale, keep it online 24/7, and run heavy jobs on GPU nodes with the results back on your phone. | `1.0.0` |

More are on the way. Each new skill gets its own folder and a row in this table.

## ✨ overleaf-ctl

Edit your Overleaf paper locally, let an agent help, and keep the two in step without leaking local notes.

- **Guarded sync.** `sync` commits, rebases onto Overleaf, and pushes. Dotfiles, build outputs, and your writing records are blocked from ever reaching Overleaf, and conflicts stop and wait for you.
- **Local compilation.** `compile` runs `latexmk` and installs missing TeX packages on demand.
- **Chinese draft first.** Every section has a Chinese draft in `.writing/zh/`. `writing status` shows which side changed since the two were last aligned, including edits made on Overleaf.
- **A log of why.** `writing log` records the intent of each round. `.writing/` is its own Git repository, so drafts have history too.

```mermaid
flowchart LR
    A["pull"] --> B["writing status"]
    B --> C["edit the Chinese draft<br/>.writing/zh/"]
    C --> D["translate into .tex"]
    D --> E["compile"]
    E --> F["writing align"]
    F --> G["writing log"]
    G --> H["sync"]
```

**Install** (Python ≥ 3.10 and Git; a TeX distribution only for local compilation):

```bash
git clone https://github.com/SuperFCR/falcary-skills.git
cd falcary-skills/overleaf-ctl
python3 scripts/install.py --link-skill claude --link-cli   # or --link-skill codex | agents
overleaf-ctl --version
```

Documentation: [English](overleaf-ctl/README.md) · [简体中文](overleaf-ctl/README.zh-CN.md)

## ✨ pocketrig

Your workstation, in your pocket. Drive the always-on computer at home from your phone.

<div align="center">
<a href="pocketrig/README.md"><img src="pocketrig/docs/promo/pocketrig-highlights.gif" alt="PocketRig highlights" width="720"></a>
</div>

- **Online 24/7.** Each instance is managed by systemd / launchd and starts at boot; on Windows, a scheduled task keeps WSL alive. Lock your phone or close your Mac, and the agent keeps running.
- **Results on your phone.** Heavy compute stays on the GPU node. Only small results such as metrics, curves, and log summaries come back to the workstation.
- **Let an agent do the setup.** Six skills cover setup, always-on, connecting devices, remote compute, diagnosis, and team use. The agent pauses for your confirmation before sudo, network changes, or reboots.
- **Yours first, then your team's.** Start with just yourself, and give family or a small team their own system users and instances later.

**Install** as a Claude Code plugin:

```text
/plugin install pocketrig --marketplace SuperFCR/falcary-skills
```

For Codex or other tools, clone this repository and run `./pocketrig/skills/install.sh --codex` (or `--target <dir>`). The skills and detailed docs are written in Chinese; agents can follow them in any language.

Documentation: [English](pocketrig/README.md) · [简体中文](pocketrig/README.zh-CN.md)

## 🗂 Repository layout

```text
falcary-skills/
├── overleaf-ctl/        # LaTeX writing skill and its Python CLI
├── paper-writing/       # paper-production skills: paper-drawing (figures → cropped PDF → .tex)
├── pocketrig/           # six workstation skills, installers for Linux / macOS / Windows, docs
├── .claude-plugin/      # marketplace manifest: installs pocketrig as a Claude Code plugin
├── .github/workflows/   # CI for pocketrig
├── LICENSE
└── README.md
```

Inside each folder, `SKILL.md` is what the agent reads, the README is what you read, and installation steps live next to them.

## 📄 License

[MIT](LICENSE), for every skill in this repository unless its folder says otherwise.

---

<a id="简体中文"></a>

## 简体中文

falcary 的 agent skill 合集。每个顶层文件夹都能独立使用，可以接入 Claude Code、Codex，或任何读取 `SKILL.md` 的 agent。

| 文件夹 | Skill 数 | 用途 | 版本 |
| :-- | :-: | :-- | :-: |
| [**overleaf-ctl**](overleaf-ctl/) | 1 | 和 agent 一起写 LaTeX 论文：带保护的 Overleaf 双向同步、本地编译，以及“先写中文稿、再译成英文 TeX”的写作流程，每一轮修改都有记录。 | `1.1.0` |
| [**paper-writing**](paper-writing/) | 1 | 论文配图（`paper-drawing`）：把你导出的 PPT/Keynote PDF 页面裁成矢量图，按印刷宽度做质量检查，写进 `.tex` 并配 caption，编译后核对页面。 | `0.1.0` |
| [**pocketrig**](pocketrig/) | 6 | 把一台常开电脑变成 AI 编程工作站：手机和 Mac 经 Tailscale 连接，7×24 在线，重任务交给 GPU 节点，结果回到手机上看。 | `1.0.0` |

安装 overleaf-ctl：

```bash
git clone https://github.com/SuperFCR/falcary-skills.git
cd falcary-skills/overleaf-ctl
python3 scripts/install.py --link-skill claude --link-cli
```

安装 pocketrig（Claude Code 插件）：

```text
/plugin install pocketrig --marketplace SuperFCR/falcary-skills
```

完整说明见 [overleaf-ctl 中文文档](overleaf-ctl/README.zh-CN.md)和 [pocketrig 中文文档](pocketrig/README.zh-CN.md)。许可：[MIT](LICENSE)。
