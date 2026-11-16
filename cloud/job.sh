#!/usr/bin/env bash
# One reel (or one YouTube part, e.g. y1-aud / y1-c03), one runner:  job.sh <id> <dir with ID.tar.enc + code.tar.enc> <outdir>
# env RENDER_KEY (repo secret). Writes <outdir>/<reel>.out.tar.enc = reels/<reel>.mp4 + .caption.txt + .srt + render.log.
# The public log only shows progress lines; the full log (it can contain transcript text) travels encrypted.
set -euo pipefail
REEL="$1"; IN="$(cd "$2" && pwd)"; OUT="$3"; HERE="$(cd "$(dirname "$0")" && pwd)"
[[ "$REEL" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo "bad reel id"; exit 2; }
[ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY missing"; exit 2; }
# same normalisation as rf_pc.py (strip surrounding whitespace / newline)
RENDER_KEY="$(printf '%s' "$RENDER_KEY" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | tr -d '\r\n')"; export RENDER_KEY
OSSL=(openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass env:RENDER_KEY)
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
dec() { "${OSSL[@]}" -d -in "$1" | tar -C "$2" --no-same-owner -xf -; }

dec "$IN/code.tar.enc" "$W"
dec "$IN/$REEL.tar.enc" "$W"
mkdir -p "$W/pkg/reels" "$W/render" "$W/tmp"
export TMPDIR="$W/tmp"     # private temp: farm_pro's one-render lock and work files stay per job
LOG="$W/pkg/render.log"; rc=0
if [ -f "$W/bundle/job.json" ]; then
  # ---- YouTube part (farm_yt4): "<vid>-aud" = whole audio track + sidecars, "<vid>-cNN" = one frame-exact video chunk ----
  mkdir -p "$W/pkg/yt"
  {
    echo "reel $REEL | $(nproc) vCPU | $(ffmpeg -version | head -1)"
    if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("need_demucs") else 1)' "$W/bundle/job.json"; then
      echo "STEP demucs install"
      pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu && pip install --quiet demucs soundfile
    fi
    YT_PRESET="${YT_PRESET:-veryfast}" YT_CRF="${YT_CRF:-20}" python3 "$W/code/farm_yt4.py" --job "$W/bundle" --out "$W/pkg/yt"
  } >"$LOG" 2>&1 || rc=$?
  grep -E '^(STEP |OK  |FAIL |reel [A-Za-z0-9_-]+ \|)' "$LOG" || true
  ls "$W/pkg/yt" | grep -qE '\.(mp4|m4a)$' || { [ $rc -ne 0 ] || rc=1; echo "no part produced (full log is in the encrypted output)"; }
  tar -C "$W/pkg" -cf - . | "${OSSL[@]}" -e -salt -out "$OUT/$REEL.out.tar.enc"
  echo "encrypted output: $REEL.out.tar.enc ($(du -h "$OUT/$REEL.out.tar.enc" | cut -f1)) rc=$rc"
  exit $rc
fi
{
  echo "reel $REEL | $(nproc) vCPU | $(ffmpeg -version | head -1)"
  python3 "$HERE/assemble.py" "$W/bundle" "$(nproc)" && \
  python3 "$HERE/cloud_render.py" --code "$W/code" --bundle "$W/bundle" --reel "$REEL" --out "$W/render"
} >"$LOG" 2>&1 || rc=$?
# public progress only (no subtitles, captions, meme wants or errors with text)
grep -E '^(STEP |PROGRESS |OK  |FAIL |assemble: [A-Za-z0-9_]+\.mp4 [0-9]+ piece|  render path|MEMORY STOP|STOPPED|reel [A-Za-z0-9_-]+ \|)' "$LOG" \
  | sed -E 's#(/[^ ]*/)?([A-Za-z0-9_-]+\.mp4)#\2#g' || true
for f in "$W/render/reels/$REEL".mp4 "$W/render/reels/$REEL".caption.txt "$W/render/reels/$REEL".srt "$W/render/reels/$REEL".ass; do
  [ -f "$f" ] && cp "$f" "$W/pkg/reels/"
done
[ -f "$W/pkg/reels/$REEL.mp4" ] || { [ $rc -ne 0 ] || rc=1; echo "no video produced (full log is in the encrypted output)"; }
tar -C "$W/pkg" -cf - . | "${OSSL[@]}" -e -salt -out "$OUT/$REEL.out.tar.enc"
echo "encrypted output: $REEL.out.tar.enc ($(du -h "$OUT/$REEL.out.tar.enc" | cut -f1)) rc=$rc"
exit $rc
