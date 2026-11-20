#!/usr/bin/env python3
"""Manifest-driven, idempotent figure sync: source PDF page -> cropped figure PDF in
the paper -> \\includegraphics block in the target .tex.

Re-running after the user re-exports the deck updates the figure files and the
existing LaTeX blocks IN PLACE (matched by \\label). Captions the user edited in the
.tex are never overwritten. Nothing is duplicated.

Manifest: <paper>/.paper-drawing/figures.json (dot-dir => local-only for overleaf-ctl).
Schema: see references/manifest.md. Minimal entry:
  {"id": "appx_rl", "source_pdf": "/abs/deck.pdf", "page": 3,
   "out": "figures/fig_appx_rl.pdf", "tex": "sections/X_appendix.tex",
   "label": "fig:appx_rl", "env": "figure*", "placement": "t",
   "graphics": "width=\\\\textwidth", "caption": "\\\\textbf{...} ..."}

Usage
  python3 sync_figures.py PAPER                    # sync all entries
  python3 sync_figures.py PAPER --only appx_rl     # subset (comma list of ids)
  python3 sync_figures.py PAPER --source-pdf NEW.pdf   # user re-exported to a new path
  python3 sync_figures.py PAPER --dry-run          # report only, write nothing
  python3 sync_figures.py PAPER --no-tex           # update figure files only
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from crop_pdf import compute_box, write_pdf, write_preview  # noqa: E402
from audit_pdf import audit  # noqa: E402
from pypdf import PdfReader  # noqa: E402

MANIFEST_REL = os.path.join(".paper-drawing", "figures.json")
FIG_RE = re.compile(r"\\begin\{(figure\*?)\}(\[[^\]]*\])?(.*?)\\end\{\1\}", re.S)
INC_RE = re.compile(r"\\includegraphics(\[[^\]]*\])?\{([^}]*)\}")


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def page_text(pdf, page):
    r = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", pdf, "-"],
                       capture_output=True, text=True)
    return norm(r.stdout)


def fingerprint(pdf, page, n=60):
    """A distinctive text snippet of the page: longest line, trimmed."""
    r = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", pdf, "-"],
                       capture_output=True, text=True)
    lines = sorted((norm(l) for l in r.stdout.splitlines()), key=len, reverse=True)
    return lines[0][:n] if lines and lines[0] else ""


def resolve_page(pdf, entry, npages, log):
    page = int(entry["page"])
    fp = entry.get("match_text")
    if not fp:
        return page
    if page <= npages and fp in page_text(pdf, page):
        return page
    hits = [p for p in range(1, npages + 1) if fp in page_text(pdf, p)]
    if len(hits) == 1:
        log(f"  page moved: {page} -> {hits[0]} (matched '{fp[:40]}...')")
        return hits[0]
    raise RuntimeError(f"match_text not found uniquely (hits={hits}); slides changed? "
                       f"fix 'page'/'match_text' in the manifest")


def sha(path):
    if not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


def find_block(tex_src, label):
    """Return (match, n_matches) of uncommented figure envs containing \\label{label}."""
    found = []
    for m in FIG_RE.finditer(tex_src):
        line_start = tex_src.rfind("\n", 0, m.start()) + 1
        if "%" in tex_src[line_start:m.start()]:
            continue
        if f"\\label{{{label}}}" in m.group(3):
            found.append(m)
    return (found[0] if found else None), len(found)


def build_block(e):
    cap = e.get("caption") or "\\TODO{caption}"
    env = e.get("env", "figure*")
    pl = e.get("placement", "t")
    gr = e.get("graphics", "width=\\linewidth")
    return ("\\begin{" + env + "}[" + pl + "]\n"
            "    \\centering\n"
            "    \\includegraphics[" + gr + "]{" + e["out"] + "}\n"
            "    \\caption{" + cap + "}\n"
            "    \\label{" + e["label"] + "}\n"
            "\\end{" + env + "}\n")


def upsert_tex(paper, e, dry, log):
    tex_path = os.path.join(paper, e["tex"])
    src = open(tex_path, encoding="utf8").read()
    m, n = find_block(src, e["label"])
    if n > 1:
        raise RuntimeError(f"{n} figure blocks carry \\label{{{e['label']}}} in {e['tex']}")
    if m:
        body = m.group(0)
        inc = INC_RE.search(body)
        if not inc:
            raise RuntimeError(f"block {e['label']} has no \\includegraphics")
        want = f"\\includegraphics[{e['graphics']}]{{{e['out']}}}" if e.get("graphics") \
            else f"\\includegraphics{inc.group(1) or ''}{{{e['out']}}}"
        if inc.group(0) == want:
            log(f"  tex: block {e['label']} up to date (caption untouched)")
            return "unchanged"
        new_body = body[:inc.start()] + want + body[inc.end():]
        src = src[:m.start()] + new_body + src[m.end():]
        action = f"updated includegraphics in existing block {e['label']}"
    else:
        block = build_block(e)
        anchor = e.get("anchor", "end")
        if anchor.startswith("after:"):
            am, _ = find_block(src, anchor[6:])
            if not am:
                raise RuntimeError(f"anchor block {anchor[6:]} not found")
            pos = am.end()
            src = src[:pos] + "\n\n" + block.rstrip("\n") + src[pos:]
        else:
            src = src.rstrip("\n") + "\n\n" + block
        action = f"inserted new block {e['label']} ({anchor})"
    if not dry:
        open(tex_path, "w", encoding="utf8").write(src)
    log(f"  tex: {action}{' [dry-run]' if dry else ''}")
    return action


def global_checks(paper, entries, log):
    """Duplicate labels / figure files referenced more than once across all .tex."""
    texs = []
    for root, dirs, files in os.walk(paper):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        texs += [os.path.join(root, f) for f in files if f.endswith(".tex")]
    allsrc = ""
    for t in texs:
        # drop comments
        allsrc += re.sub(r"(?<!\\)%.*", "", open(t, encoding="utf8", errors="ignore").read())
    ok = True
    for e in entries:
        nl = allsrc.count(f"\\label{{{e['label']}}}")
        stem = os.path.splitext(e["out"])[0]
        nf = len(re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{" + re.escape(stem)
                            + r"(?:\.pdf)?\}", allsrc))
        if nl != 1 or nf != 1:
            ok = False
            log(f"  CHECK {e['id']}: label x{nl}, file referenced x{nf} (expected 1/1)")
    if ok:
        log("global: every label and figure file is referenced exactly once")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paper")
    ap.add_argument("--manifest", help=f"default: PAPER/{MANIFEST_REL}")
    ap.add_argument("--only", default="")
    ap.add_argument("--source-pdf", help="override source_pdf for the selected entries")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-tex", action="store_true")
    ap.add_argument("--strict", action="store_true", help="stale PDF => error")
    a = ap.parse_args()

    paper = os.path.abspath(a.paper)
    mpath = a.manifest or os.path.join(paper, MANIFEST_REL)
    man = json.load(open(mpath, encoding="utf8"))
    sel = set(filter(None, a.only.split(",")))
    entries = [e for e in man["figures"] if not sel or e["id"] in sel]
    if not entries:
        sys.exit("no manifest entries selected")
    errors = 0
    log = print

    for e in entries:
        log(f"[{e['id']}] -> {e['out']}")
        try:
            if a.source_pdf:
                e["source_pdf"] = os.path.abspath(a.source_pdf)
            src = e["source_pdf"]
            if not os.path.exists(src):
                raise RuntimeError(f"source PDF missing: {src}")
            deck = e.get("source_deck")
            if deck and os.path.exists(deck) and os.path.getmtime(deck) > os.path.getmtime(src):
                msg = "source PDF is OLDER than the deck -- ask the user to re-export"
                if a.strict:
                    raise RuntimeError(msg)
                log(f"  WARN {msg}")
            reader = PdfReader(src)
            page = resolve_page(src, e, len(reader.pages), log)
            if not e.get("match_text"):
                e["match_text"] = fingerprint(src, page)
            e["page"] = page
            c = e.get("crop", {})
            with tempfile.TemporaryDirectory() as td:
                box, info = compute_box(src, reader.pages[page - 1], page,
                                        c.get("dpi", 150), c.get("thresh", 250),
                                        c.get("margin_pt", 1.0),
                                        c.get("drop_edge_rules", False),
                                        tuple(c.get("strip_bands", [])), td)
                tmp_out = os.path.join(td, "fig.pdf")
                write_pdf(reader, [page], {page: box}, tmp_out, c.get("jpeg_quality", 0))
                log(f"  crop: {info['size'][0]:.1f}x{info['size'][1]:.1f}pt "
                    f"aspect {info['aspect']:.3f}"
                    + (f" dropped {info['dropped']}" if info["dropped"] else ""))
                s = audit(tmp_out, graphics=e.get("graphics", "width=\\linewidth"),
                          env=e.get("env", "figure*"), quiet=True)
                log(f"  audit: printed {s['printed_pt'][0]:.0f}x{s['printed_pt'][1]:.0f}pt, "
                    f"min {s['min_ppi_printed']} ppi, min text ~{s['min_text_pt_printed']} pt, "
                    f"{s['mb']} MB")
                for lv, code, msg in s["findings"]:
                    log(f"    {lv} {code}: {msg}")
                dest = os.path.join(paper, e["out"])
                old, new = sha(dest), sha(tmp_out)
                if old == new:
                    log("  file: unchanged")
                elif not a.dry_run:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(tmp_out, "rb") as fi, open(dest, "wb") as fo:
                        fo.write(fi.read())
                    pv = os.path.join(paper, ".paper-drawing", "previews", e["id"] + ".png")
                    log(f"  file: {'updated' if old else 'created'}"
                        f"  preview: {write_preview(dest, 80, pv)}")
                else:
                    log(f"  file: would {'update' if old else 'create'} [dry-run]")
            if not a.no_tex:
                upsert_tex(paper, e, a.dry_run, log)
            e["last_sync"] = dt.datetime.now().isoformat(timespec="seconds")
            e["last_size_pt"] = [round(info["size"][0], 1), round(info["size"][1], 1)]
        except Exception as ex:  # noqa: BLE001
            errors += 1
            log(f"  ERROR: {ex}")

    if not a.no_tex:
        global_checks(paper, entries, log)
    if not a.dry_run:
        json.dump(man, open(mpath, "w", encoding="utf8"), indent=2, ensure_ascii=False)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
