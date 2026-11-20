#!/usr/bin/env python3
"""Audit a figure PDF *at the size it will be printed* in the paper.

Checks (levels: ERROR / WARN / NOTE), adapted from CVPR-Figure's audit_pdf:
  font-not-embedded  ERROR  venue checkers (CVPR/ICCV/NeurIPS...) reject this
  type3-font         WARN   bitmap fonts
  image-low-ppi      ERROR  raster image < 200 ppi at printed size
  image-soft         WARN   raster image < 300 ppi at printed size
  image-jpeg         NOTE   images stored as JPEG (normal for photos from PPT export)
  text-tiny          WARN   smallest text height at printed size < 5 pt (approx.,
                            from word boxes; ~font size)
  page-is-raster     WARN   whole figure is one picture with no vector text
  file-large         WARN   > 8 MB

Printed width: pass --width-pt directly, or --graphics "<includegraphics options>"
with --env figure|figure* (e.g. 'width=\\textwidth,height=.88\\textheight,keepaspectratio').
Venue widths (pt): CVPR/ICCV text 496.8 / column 237.1; ICML 487.8/234.9;
AAAI 504.0/238.5; NeurIPS/ICLR 397.5 (single column).

Usage
  python3 audit_pdf.py fig.pdf --width-pt 496.8
  python3 audit_pdf.py fig.pdf --graphics 'width=\\textwidth' --env 'figure*'
Exit code 1 if any ERROR.
"""
import argparse
import html
import os
import re
import subprocess
import sys

from pypdf import PdfReader

LEN = {"textwidth": 496.8, "linewidth": None, "columnwidth": 237.1, "textheight": 639.0}


def printed_scale(pw, ph, graphics, env="figure*", text_w=496.8, col_w=237.1, text_h=639.0):
    """Scale factor applied by \\includegraphics[graphics] to a pw x ph pt page."""
    lw = text_w if env.endswith("*") else col_w
    units = {"textwidth": text_w, "linewidth": lw, "columnwidth": col_w,
             "textheight": text_h, "pt": 1.0, "bp": 1.00375, "in": 72.27,
             "cm": 28.4528, "mm": 2.84528}

    def length(v):
        m = re.fullmatch(r"\s*([0-9.]*)\s*\\?([a-z]+)\s*", v)
        if not m:
            return None
        k = float(m.group(1)) if m.group(1) else 1.0
        return k * units.get(m.group(2), float("nan"))

    opts = dict(kv.split("=", 1) if "=" in kv else (kv, "")
                for kv in [s.strip() for s in graphics.split(",") if s.strip()])
    w = length(opts["width"]) if "width" in opts else None
    h = length(opts["height"]) if "height" in opts else None
    if "scale" in opts:
        return float(opts["scale"])
    if w and h and "keepaspectratio" in opts:
        return min(w / pw, h / ph)
    if w:
        return w / pw
    if h:
        return h / ph
    return 1.0


def fonts(pdf):
    r = subprocess.run(["pdffonts", pdf], capture_output=True, text=True)
    lines = r.stdout.splitlines()
    if len(lines) < 2:
        return []
    spans, pos = [], 0
    for dash in lines[1].split(" "):
        spans.append((pos, pos + len(dash)))
        pos += len(dash) + 1
    out = []
    for ln in lines[2:]:
        cols = [ln[s:e].strip() for s, e in spans]
        if len(cols) >= 4:
            out.append({"name": cols[0], "type": cols[1], "emb": cols[3]})
    return out


def images(pdf):
    r = subprocess.run(["pdfimages", "-list", pdf], capture_output=True, text=True)
    out = []
    for ln in r.stdout.splitlines()[2:]:
        f = ln.split()
        if len(f) >= 14 and f[2] == "image":
            try:
                out.append({"page": int(f[0]), "w": int(f[3]), "h": int(f[4]), "enc": f[8],
                            "xppi": int(f[12]), "yppi": int(f[13])})
            except ValueError:
                pass
    return out


def word_heights(pdf):
    r = subprocess.run(["pdftotext", "-bbox", pdf, "-"], capture_output=True, text=True)
    hs = []
    for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                         r'yMax="([\d.]+)">(.*?)</word>', r.stdout):
        txt = html.unescape(m.group(5)).strip()
        if len(txt) >= 2 and re.search(r"[A-Za-z]", txt):
            hs.append(float(m.group(4)) - float(m.group(2)))
    return hs


def audit(pdf, width_pt=None, graphics=None, env="figure*", quiet=False):
    rd = PdfReader(pdf)
    pg = rd.pages[0]
    pw, ph = float(pg.mediabox.width), float(pg.mediabox.height)
    if width_pt:
        scale = width_pt / pw
    elif graphics:
        scale = printed_scale(pw, ph, graphics, env)
    else:
        scale = 1.0
    findings = []

    def add(level, code, msg):
        findings.append((level, code, msg))

    for f in fonts(pdf):
        if f["emb"] != "yes":
            add("ERROR", "font-not-embedded", f"{f['name']} ({f['type']})")
        if "Type 3" in f["type"]:
            add("WARN", "type3-font", f["name"])

    ims = images(pdf)
    if ims:
        eff = [min(i["xppi"], i["yppi"]) / scale for i in ims]
        lo = min(eff)
        if lo < 200:
            add("ERROR", "image-low-ppi", f"min {lo:.0f} ppi at printed size")
        elif lo < 300:
            add("WARN", "image-soft", f"min {lo:.0f} ppi at printed size")
        njpg = sum(i["enc"] in ("jpeg", "jpx") for i in ims)
        if njpg:
            add("NOTE", "image-jpeg", f"{njpg}/{len(ims)} images JPEG-encoded")
    hs = word_heights(pdf)
    if hs:
        tiny = min(hs) * scale
        if tiny < 5.0:
            add("WARN", "text-tiny", f"smallest text ~{tiny:.1f} pt at printed size")
    elif len(ims) == 1:
        i = ims[0]
        area = (i["w"] / i["xppi"] * 72) * (i["h"] / i["yppi"] * 72)
        if area > 0.8 * pw * ph:
            add("WARN", "page-is-raster", "single image, no vector text")
    mb = os.path.getsize(pdf) / 1e6
    if mb > 8:
        add("WARN", "file-large", f"{mb:.1f} MB")

    summary = {
        "file": pdf, "pages": len(rd.pages), "size_pt": (pw, ph),
        "printed_pt": (pw * scale, ph * scale), "scale": scale,
        "images": len(ims),
        "min_ppi_printed": round(min(min(i["xppi"], i["yppi"]) for i in ims) / scale)
        if ims else None,
        "min_text_pt_printed": round(min(hs) * scale, 1) if hs else None,
        "mb": round(mb, 2), "findings": findings,
    }
    if not quiet:
        print(f"{pdf}: {pw:.1f}x{ph:.1f}pt -> printed {pw*scale:.1f}x{ph*scale:.1f}pt "
              f"(scale {scale:.3f}), {len(ims)} images, min "
              f"{summary['min_ppi_printed']} ppi, min text ~{summary['min_text_pt_printed']} pt, "
              f"{mb:.2f} MB")
        for lv, code, msg in findings:
            print(f"  {lv:5s} {code}: {msg}")
        if not findings:
            print("  OK")
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--width-pt", type=float)
    ap.add_argument("--graphics", help="includegraphics options string")
    ap.add_argument("--env", default="figure*")
    a = ap.parse_args()
    err = False
    for p in a.pdfs:
        s = audit(p, a.width_pt, a.graphics, a.env)
        err |= any(f[0] == "ERROR" for f in s["findings"])
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
