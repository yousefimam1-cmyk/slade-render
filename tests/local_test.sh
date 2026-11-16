#!/usr/bin/env bash
# End-to-end local test of the render farm on a synthetic stream (no GitHub, throwaway key, never the real RENDER_KEY):
#  prep_segments cut -> run CUT_SEGMENTS.bat's own ffmpeg lines -> rf_pc bundle (Python cipher, as on Windows without openssl)
#  -> cloud/job.sh (openssl, as on the runner) -> rf_pc decrypt -> compare with a direct farm_pro render of the full source (PSNR).
#  tests/local_test.sh [workdir] [scripts_dir]
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"; T="${1:-/tmp/rf_test}"; S="${2:-/mnt/user-data/outputs/Scripts}"
RENDER_KEY="test-only-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"; export RENDER_KEY
rm -rf "$T/gen" "$T/pc" "$T/cloud_in" "$T/cloud_out" "$T/ref"; mkdir -p "$T"
python3 "$R/tests/make_fixture.py" "$T"
python3 "$R/prep_segments.py" cut "$T/plan.json" --source "$T/src.mp4" --out "$T/gen"

# PC emulation: Scripts dir with the farm code + rf_pc.py, segs from the .bat lines
PC="$T/pc"; SEG="$PC/scan/agent/segs"; mkdir -p "$SEG/pieces" "$SEG/assets/memes" "$SEG/assets/propack"
for f in farm_pro.py farm_gpu.py farm_sfx.py farm_caps.py; do [ -f "$S/$f" ] && cp "$S/$f" "$PC/"; done
cp -r "$S/g30_sfx" "$S/g30_fonts" "$PC/" 2>/dev/null || true
cp "$R/pc/rf_pc.py" "$PC/"; cp "$T/gen/plan_segs.json" "$SEG/"
python3 - "$T/gen/CUT_SEGMENTS.bat" "$T/src.mp4" "$SEG" <<'EOF'
import sys, shlex, subprocess
bat, src, seg = sys.argv[1:4]; n = 0
for ln in open(bat, encoding="utf-8"):
    if " ffmpeg " not in ln: continue
    cmd = ln.split(" ffmpeg ", 1)[1].split(" || ")[0].replace('"%SRC%"', shlex.quote(src))
    cmd = cmd.replace('"%OUT%\\pieces\\', '"' + seg + '/pieces/').replace("\\", "/")
    subprocess.run("ffmpeg " + cmd, shell=True, check=True); n += 1
print(f"bat: {n} stream-copy cuts done")
EOF
cp "$T"/pc_memes/*.wav "$SEG/assets/memes/"; cp "$T/memes/manifest.json" "$SEG/assets/memes/"; cp "$T"/pc_propack/* "$SEG/assets/propack/"
RF_BACKEND=py python3 "$PC/rf_pc.py" bundle --segs "$SEG"

# nothing plaintext may leave the PC
for f in "$SEG"/*.tar.enc; do
  [ "$(head -c 8 "$f")" = "Salted__" ] || { echo "NOT ENCRYPTED: $f"; exit 1; }
  if strings "$f" | grep -qE '"subs"|ftyp|RIFF|plan.json'; then echo "PLAINTEXT LEAK in $f"; exit 1; fi
done
echo "bundles encrypted: $(cd "$SEG" && echo *.tar.enc)"

# cloud: exactly what the workflow runs (release download == copy)
mkdir -p "$T/cloud_in" "$T/cloud_out"; cp "$SEG"/*.tar.enc "$T/cloud_in/"
for reel in t01 tG; do (cd "$T" && bash "$R/cloud/job.sh" "$reel" "$T/cloud_in" "$T/cloud_out"); done
for f in "$T"/cloud_out/*.enc; do [ "$(head -c 8 "$f")" = "Salted__" ] || { echo "output not encrypted"; exit 1; }; done

# back on the PC: Python decrypt of openssl output
mkdir -p "$PC/scan/agent/cloud_out"; cp "$T"/cloud_out/*.out.tar.enc "$PC/scan/agent/cloud_out/"
RF_BACKEND=py python3 "$PC/rf_pc.py" decrypt --out "$T/final"

# reference: farm_pro straight on the full synthetic source, same encoder settings
REFTMP="$(mktemp -d)"; TMPDIR="$REFTMP" python3 - "$PC" "$T" <<'EOF'
import sys, os
pc, t = sys.argv[1:3]; sys.path.insert(0, pc); import farm_pro as fp
o = fp.vargs; fp.vargs = lambda k, e, p: o(k, "x264", p) if p or k == "mezz" else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4"]
rc = fp.main([os.path.join(t, "plan.json"), "--out", os.path.join(t, "ref"), "--enc", "x264", "--memes", os.path.join(t, "memes", "manifest.json"),
              "--meme-dir", os.path.join(t, "pc_memes"), "--propack", os.path.join(t, "pc_propack"), "--min-free-gb", "1", "--max-mem-gb", "12"])
sys.exit(rc)
EOF
python3 - "$T" <<'EOF'
import sys, subprocess, re
t = sys.argv[1]; bad = 0
for r in ("t01", "tG"):
    a, b = f"{t}/final/{r}.mp4", f"{t}/ref/reels/{r}.mp4"
    d = [float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", x], capture_output=True, text=True).stdout) for x in (a, b)]
    e = subprocess.run(["ffmpeg", "-v", "info", "-i", a, "-i", b, "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-"], capture_output=True, text=True).stderr
    p = float(re.search(r"average:([0-9.inf]+)", e).group(1).replace("inf", "99"))
    ok = abs(d[0] - d[1]) < 0.1 and p > 30; bad += not ok
    print(f"{r}: cloud {d[0]:.2f}s vs direct {d[1]:.2f}s, PSNR {p:.1f} dB -> {'OK' if ok else 'MISMATCH'}")
sys.exit(bad)
EOF
echo "LOCAL TEST PASSED"
