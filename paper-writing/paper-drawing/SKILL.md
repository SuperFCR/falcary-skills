---
name: paper-drawing
description: >-
  Paper figure production for LaTeX papers (CVPR/ICCV/NeurIPS/ICLR...). Use when the
  user has figures drawn in PowerPoint/Keynote/WPS and wants them as tightly cropped
  vector PDFs inserted into a .tex file (main text or appendix) with proper captions;
  when a deck was edited/re-exported and the paper figures must be updated; or for
  cropping figure PDFs, removing white margins, auditing figure quality (fonts, ppi,
  text size at print width), or adding qualitative-result figures. Router skill.
---

# Paper Drawing

Router for paper-figure tasks. Read only what the task needs.

| Sub-workflow | When | File |
| :-- | :-- | :-- |
| **paper-figure-export** (default) | The user exported slides to PDF → crop outer whitespace → put the files in `figures/` → insert or update the `.tex` floats → captions → compile check. Also covers **re-syncing** after the deck changes. | [workflows/paper-figure-export.md](workflows/paper-figure-export.md) |
| _(planned)_ plot-to-tex | matplotlib/seaborn plots in paper style | – |
| _(planned)_ figure-design | drawing method/pipeline/teaser figures (see prior art) | – |

References (load on demand): [caption-guide](references/caption-guide.md) · [quality-checklist](references/quality-checklist.md) · [manifest schema](references/manifest.md) · [prior art and what we borrowed](references/prior-art.md)

## Division of labour

- **The user** edits the deck and exports the PDF (PowerPoint/Keynote/WPS). Once a deck has been hand-edited, the deck is the **only source of truth**: always re-export from it, and never regenerate the deck from anything else.
- **The agent** maps slides to pages, crops, audits, places the figures, writes captions, compiles and verifies.
- [scripts/optional/pptx2pdf.py](scripts/optional/pptx2pdf.py) (PowerPoint/Keynote background export via AppleScript, macOS) is used **only** when the user explicitly asks the agent to export.

## Scripts (`scripts/`)

| Script | Role |
| :-- | :-- |
| `crop_pdf.py` | Crops to the outer-whitespace bbox. Vector-preserving: only the page boxes change. Opt-in extras: `--drop-edge-rules`, `--strip-bands top,bottom`. Also `--split` and `--preview`. |
| `audit_pdf.py` | Audits a figure at its **printed** width: embedded fonts, Type 3 fonts, effective ppi, smallest text size, file size. |
| `sync_figures.py` | Manifest-driven and idempotent: crop → audit → copy → **upsert** the `\includegraphics` block by `\label`. It follows slides that moved, keeps hand-edited captions, and flags a PDF older than its deck. |
| `check_build.py` | Compiles the paper (or a scratch copy with a commented `\input` enabled), then checks which files LaTeX really used, labels, warnings and the physical pages, and renders a contact sheet. |

Dependencies: `pip3 install --user pypdf` (plus Pillow, numpy), poppler (`brew install poppler`), and `overleaf-ctl` or `latexmk` for compiling.

## Global conventions

- **Default crop = outer whitespace only.** Keep every method name, column label, legend, panel letter and prompt box. Stripping a title or caption band is opt-in, and only when the user wants it.
- Never overwrite a figure the paper already uses unless the task is to update it. Use stable semantic names (`fig_appx_qual_comparison_1.pdf`) so that re-syncs replace files in place.
- Never edit `main.tex` structure (e.g. enabling the appendix) without asking. Verify on a scratch copy (`check_build.py --scratch ... --enable-input ...`).
- Done = files placed, `.tex` updated, audit clean, build check `RESULT: OK`, **and** you have looked at the rendered pages.
