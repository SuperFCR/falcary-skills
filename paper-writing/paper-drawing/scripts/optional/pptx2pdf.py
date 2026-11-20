#!/usr/bin/env python3
"""Export a .pptx/.key to PDF on macOS using Microsoft PowerPoint (default) or Keynote.

Usage:
    python3 pptx2pdf.py IN.pptx OUT.pdf [--app powerpoint|keynote] [--timeout 300]

PowerPoint strategy (learned the hard way, PowerPoint 16.x is sandboxed):
  1. If a presentation with the same file name is ALREADY OPEN in PowerPoint,
     export that window directly (fast, ~5 s even for a 130 MB deck). Unsaved
     edits in that window ARE included; it is left open.
  2. Otherwise open the ORIGINAL file IN THE BACKGROUND via LaunchServices
     (`open -g -a`; plus `-j` = launch hidden if PowerPoint wasn't running), wait
     until it appears in `presentations`, export, close it without saving, and
     quit PowerPoint again if we launched it. PowerPoint never comes to the
     front (tested: 2 s for a 1.8 MB deck, focus stays on the user's app).
     There is no truly window-less mode: AppleScript has no "open without
     window" for PowerPoint; a window is created behind other apps and closed.
     - AppleScript `open POSIX file ...` fails with error -9074 (sandbox).
     - Copies placed inside PowerPoint's container were NOT opened reliably.
     - `repeat with p in presentations` + `name of p` errors (-2763); iterate by
       index (`presentation i`) instead.
  3. If PowerPoint never shows the deck (hidden modal dialog, recovery prompt,
     lock file from another session...), the script exits non-zero: ask the user
     to open the deck in PowerPoint manually, then re-run (path 1 kicks in).
  The PDF is always written into PowerPoint's container tmp dir (always
  writable without a "Grant File Access" dialog) and then moved to OUT.pdf.

Known output characteristics (PowerPoint "save as PDF"): text and shapes stay
vector, raster images are re-encoded as JPEG at roughly 300-570 ppi of their
placed size -- fine for print; no further downsampling needed.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid

PPT_CONTAINER_TMP = os.path.expanduser(
    "~/Library/Containers/com.microsoft.Powerpoint/Data/tmp")

PPT_FIND = r'''
on run argv
    set baseName to item 1 of argv
    tell application "Microsoft PowerPoint"
        repeat with i from 1 to (count of presentations)
            set p to presentation i
            if (name of p) is baseName then
                try
                    if saved of p then return "open-saved"
                    return "open-unsaved"
                on error
                    return "open"
                end try
            end if
        end repeat
    end tell
    return "closed"
end run
'''

PPT_EXPORT = r'''
on run argv
    set baseName to item 1 of argv
    set outPath to item 2 of argv
    set closeAfter to (item 3 of argv is "close")
    set waitSecs to (item 4 of argv as integer)
    with timeout of (waitSecs + 600) seconds
        tell application "Microsoft PowerPoint"
            set pres to missing value
            repeat (waitSecs * 2) times
                repeat with i from 1 to (count of presentations)
                    if (name of presentation i) is baseName then set pres to presentation i
                end repeat
                if pres is not missing value then exit repeat
                delay 0.5
            end repeat
            if pres is missing value then error "deck never appeared in PowerPoint (hidden dialog?)"
            save pres in (POSIX file outPath) as save as PDF
            if closeAfter then close pres saving no
        end tell
    end timeout
    return "exported"
end run
'''

PPT_QUIT_IF_IDLE = r'''
tell application "Microsoft PowerPoint"
    if (count of presentations) is 0 then quit saving no
end tell
'''

KEYNOTE_SCRIPT = r'''
on run argv
    set inPath to item 1 of argv
    set outPath to item 2 of argv
    with timeout of (item 3 of argv as integer) seconds
        tell application "Keynote"
            set doc to open (POSIX file inPath)
            export doc to (POSIX file outPath) as PDF with properties {PDF image quality:Best}
            close doc saving no
        end tell
    end timeout
    return "exported"
end run
'''


def osa(script, *args, timeout=None):
    try:
        r = subprocess.run(["osascript", "-", *map(str, args)], input=script,
                           text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.exit("osascript timed out: check the app for a modal dialog")
    if r.returncode != 0:
        sys.exit(f"osascript failed: {r.stderr.strip()}\n"
                 "-> Ask the user to open the deck in the app manually (and dismiss any "
                 "dialog), then re-run this script.")
    return r.stdout.strip()


def wait_file(path, tries=120):
    for _ in range(tries):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            s1 = os.path.getsize(path)
            time.sleep(0.5)
            if os.path.getsize(path) == s1:
                return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--app", default="powerpoint", choices=["powerpoint", "keynote"])
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for the deck to open")
    ap.add_argument("--keep-app", action="store_true",
                    help="don't quit PowerPoint afterwards even if we launched it")
    a = ap.parse_args()

    inp = os.path.abspath(a.inp)
    out = os.path.abspath(a.out)
    if not os.path.exists(inp):
        sys.exit(f"input not found: {inp}")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    t0 = time.time()

    if a.app == "keynote":
        osa(KEYNOTE_SCRIPT, inp, out, a.timeout, timeout=a.timeout + 30)
        tmp_out, note = out, ""
    else:
        base = unicodedata.normalize("NFC", os.path.basename(inp))
        # Check the process FIRST: any `tell application "Microsoft PowerPoint"`
        # would launch it (visibly) and defeat the hidden -j launch below.
        was_running = subprocess.run(["pgrep", "-x", "Microsoft PowerPoint"],
                                     capture_output=True).returncode == 0
        state = osa(PPT_FIND, base, timeout=30) if was_running else "closed"
        os.makedirs(PPT_CONTAINER_TMP, exist_ok=True)
        tmp_out = os.path.join(PPT_CONTAINER_TMP, f"export_{uuid.uuid4().hex[:8]}.pdf")
        if state == "closed":
            # Background mode: -g = don't bring PowerPoint to the front;
            # -j = if PowerPoint is not running, launch it hidden.
            flags = ["-g"] if was_running else ["-g", "-j"]
            subprocess.run(["open", *flags, "-a", "Microsoft PowerPoint", inp], check=True)
            osa(PPT_EXPORT, base, tmp_out, "close", a.timeout, timeout=a.timeout + 630)
            note = "opened in background, exported, closed it"
            if not was_running and not a.keep_app:
                osa(PPT_QUIT_IF_IDLE, timeout=60)
                note += "; quit PowerPoint (it was not running before)"
        else:
            osa(PPT_EXPORT, base, tmp_out, "keep", 5, timeout=660)
            note = "exported the window already open in PowerPoint"
            if state == "open-unsaved":
                note += " (it has UNSAVED edits -- they are included in the PDF)"

    if not wait_file(tmp_out):
        sys.exit("export reported success but no PDF was produced")
    if tmp_out != out:
        shutil.move(tmp_out, out)
    print(f"{out} ({os.path.getsize(out)/1e6:.1f} MB, {time.time()-t0:.0f}s) {note}")


if __name__ == "__main__":
    main()
