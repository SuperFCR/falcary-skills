#!/usr/bin/env python3
"""Compile the paper (optionally a scratch copy with a commented-out \\input enabled)
and verify the figures as they actually appear in the built PDF.

Checks
  - each figure file is really used by LaTeX (`<use figures/x.pdf>` in the log),
    and the size LaTeX saw;
  - each \\label is defined once and has a number (aux);
  - the physical PDF page of each figure (searched via "Figure N" caption text);
  - LaTeX warnings: undefined/multiply-defined refs, Float too large, Overfull near
    the target file;
  - renders the figure pages to PNG + one contact sheet for visual inspection.

Never edits the real main.tex: with --scratch the paper is rsync'ed to DIR
(excluding .git/.writing/.outputs/.paper-drawing) and only the copy is patched.

Usage
  python3 check_build.py PAPER --labels fig:a,fig:b
  python3 check_build.py PAPER --manifest                    # labels from manifest
  python3 check_build.py PAPER --manifest --scratch /tmp/x --enable-input X_appendix
  python3 check_build.py PAPER --manifest --no-compile       # analyse existing .outputs
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys


def enable_input(main_tex, name):
    """Uncomment `% \\input{...name}` and the contiguous commented LaTeX commands
    directly above it (e.g. % \\clearpage, % \\appendix, % \\maketitlesupplementary).
    Plain-text comments (not starting with a backslash) stop the block."""
    lines = open(main_tex, encoding="utf8").read().split("\n")
    idx = [i for i, l in enumerate(lines)
           if re.match(r"\s*%\s*\\(input|include)\{[^}]*" + re.escape(name), l)]
    if not idx:
        if any(re.match(r"\s*\\(input|include)\{[^}]*" + re.escape(name), l) for l in lines):
            return []  # already enabled
        sys.exit(f"no commented \\input{{...{name}}} in {main_tex}")
    changed = []
    i = idx[0]
    j = i
    while j >= 0 and re.match(r"\s*%\s*\\", lines[j]):
        lines[j] = re.sub(r"^(\s*)%\s?", r"\1", lines[j], count=1)
        changed.append(lines[j].strip())
        j -= 1
    open(main_tex, "w", encoding="utf8").write("\n".join(lines))
    return list(reversed(changed))


def compile_paper(path, main):
    if shutil.which("overleaf-ctl"):
        cmd = ["overleaf-ctl", "compile", "--path", path, "--main", main, "--no-auto-install"]
    elif shutil.which("latexmk"):
        cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", "-outdir=.outputs", main]
    else:
        sys.exit("neither overleaf-ctl nor latexmk found")
    r = subprocess.run(cmd, cwd=path, capture_output=True, text=True)
    print(f"compile: {' '.join(cmd[:2])} -> exit {r.returncode}")
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paper")
    ap.add_argument("--main", default="main.tex")
    ap.add_argument("--labels", default="")
    ap.add_argument("--manifest", action="store_true",
                    help="take labels/files from PAPER/.paper-drawing/figures.json")
    ap.add_argument("--scratch", help="compile a copy in this dir")
    ap.add_argument("--enable-input", default="",
                    help="with --scratch: uncomment \\input{...NAME} (+ commands above)")
    ap.add_argument("--no-compile", action="store_true")
    ap.add_argument("--render-dir", help="where to put page PNGs (default: build/.outputs/figcheck)")
    a = ap.parse_args()

    paper = os.path.abspath(a.paper)
    labels, files = [], []
    if a.manifest:
        man = json.load(open(os.path.join(paper, ".paper-drawing", "figures.json")))
        labels = [e["label"] for e in man["figures"]]
        files = [e["out"] for e in man["figures"]]
    labels += [l for l in a.labels.split(",") if l]

    build = paper
    if a.scratch:
        build = os.path.abspath(a.scratch)
        shutil.rmtree(build, ignore_errors=True)
        os.makedirs(build)
        subprocess.run(["rsync", "-a", "--exclude", ".git", "--exclude", ".writing",
                        "--exclude", ".outputs", "--exclude", ".paper-drawing",
                        paper + "/", build + "/"], check=True)
        if a.enable_input:
            ch = enable_input(os.path.join(build, a.main), a.enable_input)
            print(f"scratch: enabled in copy -> {ch}")
    if not a.no_compile and not compile_paper(build, a.main):
        sys.exit(1)

    stem = os.path.splitext(a.main)[0]
    out = os.path.join(build, ".outputs")
    log = open(os.path.join(out, stem + ".log"), encoding="utf8", errors="ignore").read()
    aux = open(os.path.join(out, stem + ".aux"), encoding="utf8", errors="ignore").read()
    pdf = os.path.join(out, stem + ".pdf")
    problems = 0

    # files actually used + size LaTeX saw
    for f in files:
        m = re.search(r"<" + re.escape(f) + r", id=\d+, ([\d.]+)pt x ([\d.]+)pt>", log)
        used = f"<use {f}>" in log
        if used and m:
            print(f"file  {f}: used, {float(m.group(1)):.1f}x{float(m.group(2)):.1f}pt")
        else:
            problems += 1
            print(f"file  {f}: NOT USED by this build (wrong path, or input not compiled?)")

    # labels -> figure numbers, then physical pages via caption text
    npages = int(re.search(r"Pages:\s+(\d+)",
                           subprocess.run(["pdfinfo", pdf], capture_output=True,
                                          text=True).stdout).group(1))
    page_txt = {}
    fig_pages = {}
    for lab in labels:
        defs = re.findall(r"\\newlabel\{" + re.escape(lab) + r"\}\{\{([^}]*)\}\{([^}]*)\}", aux)
        if len(defs) != 1:
            problems += 1
            print(f"label {lab}: defined {len(defs)}x in aux")
            continue
        num = defs[0][0]
        found = None
        for p in range(1, npages + 1):
            if p not in page_txt:
                page_txt[p] = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), pdf, "-"],
                                             capture_output=True, text=True).stdout
            if re.search(r"Figure\s+" + re.escape(num) + r"[.:]", page_txt[p]):
                found = p
                break
        fig_pages[lab] = found
        print(f"label {lab}: Figure {num}, physical page {found}")

    warn_pats = [r"LaTeX Warning: Reference `[^']+' on page \d+ undefined",
                 r"LaTeX Warning: Label `[^']+' multiply defined",
                 r"LaTeX Warning: Float too large[^\n]*",
                 r"LaTeX Warning: There were undefined references"]
    for pat in warn_pats:
        for w in sorted(set(re.findall(pat, log))):
            problems += 1
            print(f"warn  {w}")

    pages = sorted({p for p in fig_pages.values() if p})
    if pages:
        rd = a.render_dir or os.path.join(out, "figcheck")
        os.makedirs(rd, exist_ok=True)
        pngs = []
        for p in pages:
            stem_p = os.path.join(rd, f"page{p:03d}")
            subprocess.run(["pdftoppm", "-r", "70", "-png", "-singlefile", "-f", str(p),
                            "-l", str(p), pdf, stem_p], check=True)
            pngs.append(stem_p + ".png")
        try:
            from PIL import Image
            ims = [Image.open(x) for x in pngs]
            W = sum(i.width for i in ims) + 12 * (len(ims) - 1)
            H = max(i.height for i in ims)
            sheet = Image.new("RGB", (W, H), (128, 128, 128))
            x = 0
            for im in ims:
                sheet.paste(im, (x, 0))
                x += im.width + 12
            sp = os.path.join(rd, "contact_sheet.png")
            sheet.save(sp)
            print(f"render: {len(pngs)} page(s) -> {sp}  (LOOK AT IT)")
        except Exception as ex:  # noqa: BLE001
            print(f"render: {pngs} (contact sheet failed: {ex})")

    print("RESULT:", "OK" if problems == 0 else f"{problems} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
