#!/usr/bin/env bash
# Direct-upload self-test (runner): make a 3 s mp4 (small = single PUT) and a ~25 MB one (forces the multipart path), upload both to
# Social Lamma as MEDIA ONLY (no post is created, no tool other than get_media_upload_url / complete_media_upload is called), print
# "SELFTEST OK <kind> media=<id> secs=<upload time>". Test media only, so printing its id is fine. env SOCIAL_LAMMA_TOKEN (never printed).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; bad=0
[ -n "${SOCIAL_LAMMA_TOKEN:-}" ] || { echo "SELFTEST SKIP reason=no_token"; exit 3; }
gen() { # <out> <bitrate>
  ffmpeg -nostdin -v error -y -f lavfi -i "testsrc2=s=720x1280:r=30" -f lavfi -i "sine=f=440" -t 3 \
    -vf "noise=alls=${3:-0}:allf=t" -c:v libx264 -preset ultrafast -b:v "$2" -minrate "$2" -maxrate "$2" -bufsize "$2" -c:a aac -shortest "$1"
}
gen "$W/small.mp4" 1M 0; gen "$W/big.mp4" 60M 100
for k in small big; do
  f="$W/$k.mp4"; python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import qa_media as Q; print("final_codes", Q.final_codes(sys.argv[2]))' "$HERE" "$f"
  t0=$(date +%s.%N); line=$(python3 "$HERE/handoff.py" --file "$f" --id "selftest_$k" --out "$W/$k.json" --filename "selftest_$k.mp4" 2>&1); rc=$?; t1=$(date +%s.%N)
  echo "$line (rc=$rc, $(du -h "$f" | cut -f1))"
  if [ $rc -eq 0 ] && [ -s "$W/$k.json" ]; then
    python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("SELFTEST OK %s media=%s bytes=%s secs=%.1f" % (sys.argv[2], r["media_id"], r["bytes"], float(sys.argv[3])-float(sys.argv[4])))' "$W/$k.json" "$k" "$t1" "$t0"
  else echo "SELFTEST FAIL $k"; bad=1; fi
done
exit $bad
