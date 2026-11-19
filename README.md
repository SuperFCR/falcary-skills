# falcary-skills

Agent skills by falcary. Each top-level folder is one self-contained skill with its own `SKILL.md` and README.

| Skill | Version | What it does |
|---|---|---|
| [overleaf-ctl](overleaf-ctl/) | 1.1.0 | LaTeX paper writing with a Python CLI: guarded Overleaf Git sync, local compile, and writing records (Chinese drafts, their alignment with the TeX, a round log). |

## Install

Each skill documents its own installation. For `overleaf-ctl` (needs Python ≥ 3.10 and Git):

```bash
git clone https://github.com/SuperFCR/falcary-skills.git
cd falcary-skills/overleaf-ctl
python3 scripts/install.py --link-skill claude --link-cli
```

See [overleaf-ctl/README.md](overleaf-ctl/README.md) ([简体中文](overleaf-ctl/README.zh-CN.md)) for requirements and usage.

## License

[MIT](LICENSE), for every skill in this repository unless its folder says otherwise.

---

falcary 的 agent skill 合集。每个顶层文件夹是一个独立的 skill，自带 `SKILL.md` 和 README；安装与用法见各 skill 目录。
