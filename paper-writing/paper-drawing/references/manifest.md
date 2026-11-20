# Manifest schema: `PAPER/.paper-drawing/figures.json`

Local-only (dot-dir; overleaf-ctl excludes `.*` from pushes). It records where each paper figure comes from, so re-syncs are deterministic. `sync_figures.py` reads it, and writes back `page`, `match_text`, `last_sync` and `last_size_pt`.

```json
{
  "version": 1,
  "figures": [
    {
      "id": "appx_qual_comparison_1",
      "source_pdf": "/abs/path/deck.pdf",
      "source_deck": "/abs/path/deck.pptx",
      "page": 4,
      "match_text": "auto-filled on first sync",
      "out": "figures/fig_appx_qual_comparison_1.pdf",
      "tex": "sections/X_appendix.tex",
      "label": "fig:appx_qual_comparison_1",
      "env": "figure*",
      "placement": "p",
      "graphics": "width=\\textwidth,height=.88\\textheight,keepaspectratio",
      "caption": "\\textbf{...} ...",
      "anchor": "end",
      "crop": {"margin_pt": 1, "drop_edge_rules": true, "strip_bands": [], "thresh": 250,
               "dpi": 150, "jpeg_quality": 0}
    }
  ]
}
```

| Field | Req. | Notes |
| :-- | :-- | :-- |
| `id` | ✓ | Stable key for `--only`, and the preview file name. |
| `source_pdf` | ✓ | The user-exported PDF. `--source-pdf` overrides it for the selected entries (and saves the new path). |
| `source_deck` | | Enables the staleness warning (deck newer than PDF). |
| `page` | ✓ | 1-based page in `source_pdf`. Auto-corrected via `match_text` when slides move. |
| `match_text` | | Distinctive page text. Auto-filled from the longest text line. Clear it to re-fingerprint. |
| `out` | ✓ | Path relative to PAPER, exactly as written in `\includegraphics`. |
| `tex` | ✓ | Target `.tex`, relative to PAPER. |
| `label` | ✓ | Block identity for the upsert. Must be unique in the paper. |
| `env`/`placement`/`graphics` | | Defaults: `figure*`, `t`, `width=\linewidth`. `graphics` is also used for the printed-size audit. On re-sync, a changed `graphics` rewrites the `\includegraphics` options. |
| `caption` | | Used only when inserting a new block; it never overwrites. Missing → `\TODO{caption}`. |
| `anchor` | | Where a new block goes: `end` (default) or `after:<label>`. |
| `crop` | | Options for `crop_pdf.compute_box`. The default is outer whitespace only. |

Upsert semantics: find the uncommented `figure`/`figure*` environment containing `\label{<label>}`. If it exists, replace only its `\includegraphics[...]{...}`; caption, placement and other lines stay as the user left them. If it doesn't exist, insert `build_block()` at `anchor`. More than one match → error.
