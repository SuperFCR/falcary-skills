# paper-writing

Agent skills for producing LaTeX papers. Each subfolder is a standalone skill (`SKILL.md` + scripts) for Claude Code, Codex, Antigravity, or any agent that reads `SKILL.md`.

| Skill | What it does |
| :-- | :-- |
| [**paper-drawing**](paper-drawing/) | Paper figures from PowerPoint/Keynote/WPS: you export the deck to PDF, and the agent crops each page to its content (vector-preserving), audits it at printed width (embedded fonts, ppi, text size), inserts or updates the `figure` blocks in your `.tex` with captions, compiles, and checks the rendered pages. A manifest makes re-exports re-syncable without duplicating anything. |

Related: [overleaf-ctl](../overleaf-ctl/) for Overleaf sync, local compilation and the writing workflow. `paper-drawing` uses it for compiling and pushing when it's available.

## Install

```bash
git clone https://github.com/SuperFCR/falcary-skills.git
# link the skill into your agent's skill directory, e.g.
ln -s "$PWD/falcary-skills/paper-writing/paper-drawing" ~/.claude/skills/paper-drawing
# Codex: ~/.codex/skills/   Antigravity: ~/.gemini/config/skills/
pip3 install --user pypdf pillow numpy && brew install poppler
```

---

## 简体中文

写 LaTeX 论文用的 agent skill。每个子文件夹都是独立的 skill。

| Skill | 用途 |
| :-- | :-- |
| [**paper-drawing**](paper-drawing/) | 论文配图：你从 PPT/Keynote/WPS 导出 PDF，agent 负责把每页裁到内容边界（保留矢量），按论文里的实际印刷宽度检查质量（字体嵌入、分辨率、字号），在 `.tex` 中插入或就地更新 figure 并写 caption，最后编译并检查渲染出的页面。用清单文件记录图的来源，重新导出后再同步一次即可，不会产生重复。 |

相关：[overleaf-ctl](../overleaf-ctl/)（Overleaf 同步、本地编译、写作流程）。
