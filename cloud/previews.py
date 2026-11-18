#!/usr/bin/env python3
"""Small review files made on the runner, so the PC never needs the full video just to look at it.

  previews.py <video.mp4> <outdir> <stem> [--yt]

-> <stem>.qa.jpg      5x5 contact sheet (same as the PC's QA_SHEET.bat: fps=1/S, scale=384:-2, tile=5x5, S = duration/25)
   <stem>.thumb.jpg   360 px wide frame at 1 s (same as new_stream.py)
   <stem>.preview.mp4 low-res review copy (reel: 360x640 CRF 30; YouTube: 480 px high-motion-safe CRF 33, 15 fps) - review only, never posted
The three ffmpeg runs go in parallel. Public stdout: one "PREVIEWS <stem> ok=N" line.
"""
import os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

FF = ["ffmpeg", "-nostdin", "-y", "-v", "error"]


def dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: return 0.0


def jobs(src, outdir, stem, yt=False):
    d = dur(src) or 30.0; sec = max(1, int(round(d / 25.0))); o = lambda e: os.path.join(outdir, stem + e)
    if yt: pv = ["-vf", "scale=-2:480:flags=bicubic,fps=15", "-c:v", "libx264", "-preset", "veryfast", "-crf", "33", "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
    else: pv = ["-vf", "scale=-2:640:flags=bicubic", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k"]
    return [
        FF + ["-i", src, "-vf", f"fps=1/{sec},scale=384:-2,tile=5x5", "-frames:v", "1", "-update", "1", o(".qa.jpg")],
        FF + ["-ss", "1" if d > 1.5 else "0", "-i", src, "-frames:v", "1", "-vf", "scale=360:-2", o(".thumb.jpg")],
        FF + ["-i", src, "-map", "0:v:0", "-map", "0:a:0?"] + pv + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", o(".preview.mp4")],
    ]


def make(src, outdir, stem, yt=False):
    os.makedirs(outdir, exist_ok=True)
    with ThreadPoolExecutor(3) as ex: rcs = list(ex.map(lambda c: subprocess.run(c, capture_output=True).returncode, jobs(src, outdir, stem, yt)))
    return sum(1 for r in rcs if r == 0)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--yt"]; yt = "--yt" in sys.argv
    stem = a[2] if re.fullmatch(r"[A-Za-z0-9_ -]{1,60}", a[2]) else "x"
    n = make(a[0], a[1], stem, yt); print(f"PREVIEWS {stem if re.fullmatch(r'[A-Za-z0-9_-]{1,40}', stem) else 'x'} ok={n}"); sys.exit(0 if n else 1)
