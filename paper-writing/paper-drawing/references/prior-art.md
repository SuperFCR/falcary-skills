# Prior art: what was reviewed and what was borrowed

Reviewed 2026-11 (public `SKILL.md` + key scripts).

| Skill | What it does | Borrowed | Not borrowed / why |
| :-- | :-- | :-- | :-- |
| [cropfig](https://github.com/youngeun1209/oh-my-claudecode-research/tree/main/skills/cropfig) | PPTX/KEY → per-slide PDF → crop (CropBox, vector kept) → copy to `figures/` and outline PNGs | The CropBox crop driven by a low-dpi probe render; the text-band classifier (`crop_bounds.py`: colour saturation + long dark runs), as the opt-in `--strip-bands`; idempotent re-runs | Stripping title/caption bands **by default**: on qualitative grids it removes column labels. Also not taken: auto-export by default (fragile on Mac), and its "never touch .tex" stance (we upsert by `\label`). |
| [CVPR-Figure](https://github.com/ChizkiyahuOhayon/CVPR-Figure) | Spec → editable PPTX → PDF; audits of the PPTX and the shipped PDF | "Once edited, the PPTX is the only source"; auditing at the **printed width** (venue width table); the font-embedded / Type 3 / ppi / tiny-text / raster-page checks (`audit_pdf.py`); app export settings | Its spec/render engine and PPTX auditor (arrow glue, subscripts, theme shadows), which belong to a future *figure-design* sub-workflow. |
| [editable-figure](https://github.com/yzhao062/anywhere-agents/tree/main/skills/editable-figure) | Editable PPTX figures, insertion, compile, compare | Tracing the actual inclusion path, compiling, inspecting the rendered page (`check_build.py`); one canonical float fragment / caption place; judging space use on the final page | Author-specific aesthetic preferences; the Windows-only PowerPoint helper. |
| [Anthropic pptx](https://github.com/anthropics/skills/tree/main/skills/pptx) | General PPTX create/edit, LibreOffice → PDF | – | No crop or LaTeX loop. LibreOffice export was rejected by the user for fidelity reasons. |
| [paper-figure](https://github.com/wbopan/paper-figure) | D3/SVG → tight PDF + LaTeX | Idea for a future *plot-to-tex* sub-workflow | The input is code, not a deck. |

Design decisions that differ from all of them:
- **The user exports the PDF**; the agent never drives the slide app by default (permissions, versions and hidden dialogs make that fragile; tested on PowerPoint 16.115).
- A **manifest** (`.paper-drawing/figures.json`) makes the deck → paper mapping explicit and re-runnable, and follows slides that move via text fingerprints.
