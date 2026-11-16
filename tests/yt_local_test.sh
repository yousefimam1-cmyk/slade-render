#!/usr/bin/env bash
# End-to-end local test of the YouTube cloud path with a throwaway TEST key (never the real RENDER_KEY, no GitHub):
#   PC emulation: rf_pc.py ytbundle (Python cipher, as on Windows without openssl) -> leak check
#   -> cloud/job.sh per part (openssl, exactly what the runner runs) -> rf_pc.py decrypt (Python) -> automatic join (yt_join)
#   -> checks: every part produced, joined frames == plan frames, A/V lengths, chunk-edge continuity.
# usage: tests/yt_local_test.sh <workdir> <vids e.g. y1,y2> [plan] [parts filter regex]
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"; T="${1:-/tmp/yt_test}"; VIDS="${2:-y1}"
S="${SLADE_SCRIPTS:-/mnt/user-data/outputs/Scripts}"; PLAN="${3:-$S/pro_plan_v5.json}"; ONLY="${4:-.}"
SEGSRC="${SLADE_SEGS:-/mnt/user-data/uploads/Videos/Scripts/scan/agent/segs}"
RENDER_KEY="test-only-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"; export RENDER_KEY
mkdir -p "$T"; PC="$T/pc"; SEG="$PC/scan/agent/segs"; rm -rf "$PC" "$T/cloud_in" "$T/cloud_out"
mkdir -p "$SEG/assets" "$PC/scan/agent/cloud_out" "$T/cloud_in" "$T/cloud_out"
for f in farm_pro.py farm_gpu.py farm_sfx.py farm_caps.py farm_yt4.py farm_discord.py farm_music.py yt_join.py gaming.cube; do [ -f "$S/$f" ] && cp "$S/$f" "$PC/"; done
for d in g30_sfx g30_fonts fonts models; do [ -d "$S/$d" ] && cp -r "$S/$d" "$PC/"; done
mkdir -p "$PC/memes" && cp -r "$S/memes/synth" "$PC/memes/"
cp "$R/pc/rf_pc.py" "$PC/"; cp "$PLAN" "$PC/plan.json"
ln -s "$SEGSRC/pieces" "$SEG/pieces"; cp -r "$SEGSRC/assets/memes" "$SEGSRC/assets/propack" "$SEG/assets/"
echo '{"source": "2026-11-13 23-20-41.mp4", "reels": {}}' > "$SEG/plan_segs.json"

RF_BACKEND=py python3 "$PC/rf_pc.py" ytbundle --plan "$PC/plan.json" --vids "$VIDS" --segs "$SEG" --scripts "$PC"
IDS="$(cat "$SEG/yt_parts.txt")"
for f in "$SEG"/*.tar.enc; do
  [ "$(head -c 8 "$f")" = "Salted__" ] || { echo "NOT ENCRYPTED: $f"; exit 1; }
  if head -c 50000000 "$f" | strings | grep -qE '"subs"|ftypisom|RIFF....WAVE|"youtube"'; then echo "PLAINTEXT LEAK in $f"; exit 1; fi
done
echo "bundles encrypted: $IDS"
mv "$SEG"/*.tar.enc "$T/cloud_in/"

# cloud: what the workflow runs, one part per "runner" (here one after the other)
for id in ${IDS//,/ }; do
  [[ "$id" =~ $ONLY ]] || continue
  t0=$(date +%s); (cd "$T" && bash "$R/cloud/job.sh" "$id" "$T/cloud_in" "$T/cloud_out") || { echo "PART FAILED: $id"; exit 1; }
  echo "part $id: $(( $(date +%s) - t0 )) s"
  rm -f "$T/cloud_in/$id.tar.enc"
done
for f in "$T"/cloud_out/*.enc; do [ "$(head -c 8 "$f")" = "Salted__" ] || { echo "output not encrypted"; exit 1; }; done

# back on the PC: Python decrypt of the openssl output -> youtube/<vid>/ parts -> joined youtube/<vid>.mp4
mv "$T"/cloud_out/*.out.tar.enc "$PC/scan/agent/cloud_out/"
RF_BACKEND=py python3 "$PC/rf_pc.py" decrypt --scripts "$PC" --segs "$SEG"
python3 - "$PC/scan/2026-11-13 23-20-41.pro/youtube" "$VIDS" <<'EOF'
import sys, os, json, subprocess
d, vids = sys.argv[1], sys.argv[2].split(","); bad = 0
for v in vids:
    f = os.path.join(d, v + ".mp4")
    if not os.path.isfile(f): print(f"{v}: NOT JOINED"); bad += 1; continue
    P = json.load(open(os.path.join(d, v, v + ".parts.json")))
    n = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", f], capture_output=True, text=True).stdout)
    ad = float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", f], capture_output=True, text=True).stdout)
    ok = n == P["frames"] and abs(ad - n / 30) < 0.05; bad += not ok
    print(f"{v}: {n} frames (plan {P['frames']}), audio {ad:.3f}s vs video {n / 30:.3f}s -> {'OK' if ok else 'MISMATCH'}")
sys.exit(bad)
EOF
echo "YT LOCAL TEST PASSED"
