# Quality checklist (figure PDFs that actually ship)

Run `audit_pdf.py` on the cropped PDF at its **printed** width (`sync_figures.py` does this automatically with the manifest's `graphics`/`env`).

| Code | Level | Meaning → fix |
| :-- | :-- | :-- |
| `font-not-embedded` | ERROR | Venue checkers reject it. Re-export; in PowerPoint, uncheck "bitmap text when fonts are not embedded", or install the font and re-export. |
| `type3-font` | WARN | Bitmap glyphs. Usually caused by an unusual font; replace it in the deck. |
| `image-low-ppi` | ERROR | < 200 ppi at printed size. Re-export at best quality, or replace the source image. |
| `image-soft` | WARN | 200–300 ppi. Acceptable for video frames; mention it. |
| `image-jpeg` | NOTE | JPEG-encoded images. Normal for photo/video panels from PPT export. Only lossless sources (screenshots, line art) are worth re-exporting. |
| `text-tiny` | WARN | Smallest text < ~5 pt at print (approximate, from word boxes). Enlarge it in the deck, or give the figure more width. |
| `page-is-raster` | WARN | The whole figure is one image, i.e. it was exported as a picture. Re-export as PDF from the deck. |
| `file-large` | WARN | > 8 MB. `jpeg_quality` 85 in `crop`, then re-audit. |

Printed widths (pt): CVPR/ICCV 496.8 / column 237.1 · AAAI 504.0 / 238.5 · ICML 487.8 / 234.9 · NeurIPS/ICLR 397.5.

## Things the audit can't see (check by eye on the contact sheet)

- Labels clipped at the crop edge; a stray divider or slide-number left in.
- Text that is readable on the slide but too small after scaling (compare with the body text at 100%).
- A float page with a lone thin strip; a figure far from its first reference.
- Caption claims not supported by what is visible.
- Theme shadows or soft edges that look blurry in print. These are mostly a LibreOffice export issue; PowerPoint and Keynote exports are fine.

## Field notes

- PowerPoint (Mac) "Export → PDF" on the test deck: text and shapes stay vector, photos are JPEG at ~350–570 ppi of placed size, and fonts are embedded.
- Cropping by page boxes keeps off-box content in the file. It is invisible in LaTeX/Preview/Acrobat; `remove_unreferenced` drops other pages' resources.
