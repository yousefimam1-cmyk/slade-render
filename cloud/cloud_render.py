#!/usr/bin/env python3
"""Run the PC's farm_pro.py (snapshot from code.tar.enc, not a fork) on one reel's bundle, cloud settings:
final encode libx264 -preset veryfast -crf 20 (mezzanine/preview untouched), threads = vCPUs, memory guards sized for a runner.

  cloud_render.py --code DIR --bundle DIR --reel ID --out DIR
"""
import os, sys, argparse

ap = argparse.ArgumentParser(); ap.add_argument("--code", required=True); ap.add_argument("--bundle", required=True)
ap.add_argument("--reel", required=True); ap.add_argument("--out", required=True); ap.add_argument("--crf", default="20"); ap.add_argument("--preset", default="veryfast")
a = ap.parse_args()
sys.path.insert(0, os.path.abspath(a.code))
import farm_pro as fp

_orig = fp.vargs
def vargs(kind, enc, preview):
    if preview or kind == "mezz": return _orig(kind, "x264", preview)
    return ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf), "-threads", str(fp.SAFE["threads"])]
fp.vargs = vargs

b = os.path.abspath(a.bundle); n = os.cpu_count() or 4
args = [os.path.join(b, "plan.json"), "--only", a.reel, "--out", os.path.abspath(a.out), "--enc", "x264",
        "--memes", os.path.join(b, "memes", "manifest.json"), "--meme-dir", os.path.join(b, "memes"), "--propack", os.path.join(b, "propack"),
        "--threads", str(n), "--min-free-gb", "1", "--max-mem-gb", os.environ.get("RF_MAX_MEM_GB", "12")]
os.chdir(b)
sys.exit(fp.main(args))
