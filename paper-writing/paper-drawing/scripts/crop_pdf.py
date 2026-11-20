#!/usr/bin/env python3
"""Crop PDF pages to the tight bounding box of their visible content (pdfcrop-like)
WITHOUT re-rendering: only MediaBox/CropBox/TrimBox/BleedBox/ArtBox change, so
vectors, text and embedded images stay byte-identical.

Default policy: remove OUTER WHITESPACE ONLY -- every label, method name, legend,
prompt box inside the slide is kept. Anything more aggressive is opt-in:
  --drop-edge-rules       ignore thin divider lines isolated at an edge
  --strip-bands top,bottom  remove a slide-title / caption-like TEXT band at that
                          edge (heuristic adapted from cropfig's crop_bounds.py:
                          a band is "text" if it has no saturated colour and no
                          long dark run; colourful or line-art bands are kept)

Requires: pypdf, Pillow, numpy, poppler (pdftoppm).

Usage
  python3 crop_pdf.py deck.pdf --pages 3,4,5 --split --out-pattern out/fig_p{page}.pdf --preview
  python3 crop_pdf.py deck.pdf --pages 3-5 -o out.pdf
"""
import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject
except ImportError:
    sys.exit("pypdf missing: pip3 install --user pypdf")

BOX_ATTRS = ("mediabox", "cropbox", "trimbox", "bleedbox", "artbox")


# ---------------------------------------------------------------- helpers
def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    for p in out:
        if not 1 <= p <= n:
            sys.exit(f"page {p} out of range 1..{n}")
    return out


def render_page(pdf, page, dpi, tmpdir):
    stem = os.path.join(tmpdir, f"p{page}")
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
                    "-png", "-singlefile", pdf, stem], check=True)
    return np.asarray(Image.open(stem + ".png").convert("RGB"))


def _runs(profile):
    """[(start, end_exclusive)] of True runs in a 1-D bool array."""
    a = np.concatenate(([0], profile.astype(np.int8), [0]))
    d = np.diff(a)
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def drop_edge_rules(mask, r0, r1, c0, c1, max_thick, min_gap):
    """Iteratively strip thin rules isolated at an edge. Returns bounds, dropped."""
    dropped = []
    changed = True
    while changed:
        changed = False
        sub = mask[r0:r1, c0:c1]
        rows = np.where(sub.any(axis=1))[0]
        cols = np.where(sub.any(axis=0))[0]
        if len(rows) == 0:
            break
        r0, r1 = r0 + rows[0], r0 + rows[-1] + 1
        c0, c1 = c0 + cols[0], c0 + cols[-1] + 1
        sub = mask[r0:r1, c0:c1]
        for side in ("bottom", "top", "right", "left"):
            prof = sub.any(axis=1) if side in ("top", "bottom") else sub.any(axis=0)
            if side in ("bottom", "right"):
                prof = prof[::-1]
            if prof.all():
                continue
            run = int(np.argmin(prof))
            if run == 0 or run > max_thick:
                continue
            rest = prof[run:]
            if not rest.any():
                continue
            gap = int(np.argmax(rest))
            if gap < min_gap:
                continue
            cut = run + gap
            if side == "bottom":
                r1 -= cut
            elif side == "top":
                r0 += cut
            elif side == "right":
                c1 -= cut
            else:
                c0 += cut
            dropped.append(f"rule-{side}")
            changed = True
            break
    return r0, r1, c0, c1, dropped


def is_text_band(rgb):
    """cropfig-style classification: True if band looks like plain dark text."""
    f = rgb.astype(np.float32) / 255.0
    mx, mn = f.max(axis=-1), f.min(axis=-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    if (sat > 0.15).mean() > 0.005:        # colourful -> figure content
        return False
    dark = rgb.mean(axis=-1) < 130
    longest = max((e - s for row in dark for s, e in _runs(row)), default=0)
    if longest > rgb.shape[1] * 0.25:      # long line / box edge -> figure content
        return False
    return True


def strip_text_bands(im, mask, r0, r1, c0, c1, sides, min_gap):
    """Remove ONE text-only band at the requested edges (title / caption)."""
    dropped = []
    for side in sides:
        prof = mask[r0:r1, c0:c1].any(axis=1)
        segs = _runs(prof)
        if len(segs) < 2:
            break
        s, e = segs[0] if side == "top" else segs[-1]
        nb = segs[1] if side == "top" else segs[-2]
        gap = (nb[0] - e) if side == "top" else (s - nb[1])
        if gap < min_gap or not is_text_band(im[r0 + s:r0 + e, c0:c1]):
            continue
        if side == "top":
            r0 += nb[0]
        else:
            r1 = r0 + nb[1]
        dropped.append(f"text-band-{side}")
    return r0, r1, dropped


def compute_box(pdf, page_obj, page_no, dpi=150, thresh=250, margin_pt=1.0,
                edge_rules=False, strip_bands=(), tmpdir=None):
    """Return (RectangleObject | None, info dict) for one page."""
    own_tmp = tmpdir is None
    td = tempfile.mkdtemp() if own_tmp else tmpdir
    im = render_page(pdf, page_no, dpi, td)
    h, w = im.shape[:2]
    mask = (im < thresh).any(axis=2)
    info = {"page": page_no, "dropped": []}
    if not mask.any():
        info["blank"] = True
        return None, info
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    min_gap = max(3, round(dpi / 15))
    if edge_rules:
        r0, r1, c0, c1, d = drop_edge_rules(mask, r0, r1, c0, c1,
                                            max(2, round(dpi / 50)), min_gap)
        info["dropped"] += d
    if strip_bands:
        r0, r1, d = strip_text_bands(im, mask, r0, r1, c0, c1, strip_bands, min_gap)
        info["dropped"] += d
        if d:  # re-tighten columns after removing a band
            sub = mask[r0:r1]
            cols = np.where(sub.any(axis=0))[0]
            c0, c1 = cols[0], cols[-1] + 1
    vis = page_obj.cropbox  # pdftoppm renders the CropBox
    sx, sy = float(vis.width) / w, float(vis.height) / h
    m = margin_pt
    llx = max(float(vis.left), float(vis.left) + c0 * sx - m)
    urx = min(float(vis.right), float(vis.left) + c1 * sx + m)
    ury = min(float(vis.top), float(vis.top) - r0 * sy + m)
    lly = max(float(vis.bottom), float(vis.top) - r1 * sy - m)
    info.update(src=(float(vis.width), float(vis.height)),
                size=(urx - llx, ury - lly), aspect=(urx - llx) / (ury - lly))
    if int(page_obj.get("/Rotate", 0) or 0) % 360:
        info["warning"] = "page has /Rotate; bbox mapping assumes none"
    return RectangleObject([llx, lly, urx, ury]), info


def write_pdf(reader, pages, boxes, out, jpeg_quality=0):
    w = PdfWriter()
    for p in pages:
        w.add_page(reader.pages[p - 1])
        if boxes.get(p) is not None:
            for attr in BOX_ATTRS:
                setattr(w.pages[-1], attr, boxes[p])
    if jpeg_quality:
        for pg in w.pages:
            for img in pg.images:
                try:
                    if img.image.mode in ("RGBA", "LA", "P"):
                        continue
                    img.replace(img.image, quality=jpeg_quality)
                except Exception as e:  # noqa: BLE001
                    print(f"  skip image {img.name}: {e}")
    w.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        w.write(f)


def write_preview(pdf_path, dpi=60, out_png=None):
    stem = os.path.splitext(out_png)[0] if out_png else \
        os.path.splitext(pdf_path)[0] + ".preview"
    os.makedirs(os.path.dirname(os.path.abspath(stem)), exist_ok=True)
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_path, stem],
                   check=True)
    return stem + ".png"


def describe(info):
    if info.get("blank"):
        return f"page {info['page']}: blank, left uncropped"
    s = (f"page {info['page']}: {info['src'][0]:.0f}x{info['src'][1]:.0f}pt -> "
         f"{info['size'][0]:.1f}x{info['size'][1]:.1f}pt (aspect w/h={info['aspect']:.3f})")
    if info["dropped"]:
        s += f"  dropped: {', '.join(info['dropped'])}"
    if info.get("warning"):
        s += f"  WARNING: {info['warning']}"
    return s


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp")
    ap.add_argument("-o", "--out", help="output PDF (multi-page) when not --split")
    ap.add_argument("--pages", default="", help="e.g. 3,4,5 or 3-5 (1-based). default: all")
    ap.add_argument("--split", action="store_true", help="one output PDF per page")
    ap.add_argument("--out-pattern", help="with --split, e.g. out/fig_p{page}.pdf")
    ap.add_argument("--margin-pt", type=float, default=1.0)
    ap.add_argument("--thresh", type=int, default=250,
                    help="pixel value below which a pixel counts as content")
    ap.add_argument("--dpi", type=int, default=150, help="probe dpi for bbox detection")
    ap.add_argument("--drop-edge-rules", action="store_true")
    ap.add_argument("--strip-bands", default="",
                    help="comma list of top,bottom: remove a text-only title/caption band")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--jpeg-quality", type=int, default=0,
                    help="re-encode raster images as JPEG (lossy; only if too large)")
    a = ap.parse_args()

    reader = PdfReader(a.inp)
    pages = parse_pages(a.pages, len(reader.pages))
    bands = tuple(s for s in a.strip_bands.split(",") if s)
    if a.split and not a.out_pattern:
        a.out_pattern = os.path.splitext(a.out or a.inp)[0] + "_p{page}.pdf"
    if not a.split and not a.out:
        a.out = os.path.splitext(a.inp)[0] + "_crop.pdf"

    boxes = {}
    with tempfile.TemporaryDirectory() as td:
        for p in pages:
            box, info = compute_box(a.inp, reader.pages[p - 1], p, a.dpi, a.thresh,
                                    a.margin_pt, a.drop_edge_rules, bands, td)
            boxes[p] = box
            print(describe(info))

    outs = []
    if a.split:
        for i, p in enumerate(pages, 1):
            out = a.out_pattern.format(page=p, i=i)
            write_pdf(reader, [p], boxes, out, a.jpeg_quality)
            outs.append(out)
    else:
        write_pdf(reader, pages, boxes, a.out, a.jpeg_quality)
        outs.append(a.out)
    for o in outs:
        line = f"wrote {o} ({os.path.getsize(o)/1e6:.2f} MB)"
        if a.preview:
            line += f"  preview: {write_preview(o)}"
        print(line)
    print("next: python3 audit_pdf.py <out.pdf> --width-pt <printed width>")


if __name__ == "__main__":
    main()
