#!/usr/bin/env bash
# One reel (or one YouTube part, e.g. y1-aud / y1-c03), one runner:  job.sh <id> <dir with ID.tar.enc + code.tar.enc> <outdir>
# env RENDER_KEY (repo secret). Writes <outdir>/<reel>.out.tar.enc = reels/<reel>.mp4 + .caption.txt + .srt + render.log
# + qa.json / <reels|yt>/<id>.qa.json from qa_media.py (public log: one "QA PASS|FAIL <id> codes=..." line, codes only).
# The public log only shows progress lines; the full log (it can contain transcript text) travels encrypted.
# Speed (2026-11-18):
#  - bundle/media.json -> fetch_media.py puts the content-addressed source media (release "src-cache", uploaded once) back in place
#  - final encode: x264 preset slow CRF 22 (same or better SSIM/PSNR than the old veryfast CRF 20, ~12-22% smaller); env overrides
#  - env SOCIAL_LAMMA_TOKEN set + QA PASS: handoff.py uploads the reel straight to Social Lamma, previews.py makes qa.jpg/thumb.jpg/preview.mp4,
#    and the video goes to <outdir>/<reel>.full.tar.enc (fetched only on request) while <reel>.out.tar.enc stays small (no mp4, + handoff.json).
#    Token empty or hand-off failed -> exactly today's package (mp4 inside .out.tar.enc).
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
# automatic QA (black / loop / caption sync / memes): never breaks the render, public line = codes only
qa() {
  timeout 1500 python3 "$HERE/qa_media.py" --id "$REEL" --pkg "$W/pkg" >"$W/pkg/qa.log" 2>&1 || true
  grep -E '^QA (PASS|FAIL) [A-Za-z0-9_-]+( codes=[a-z_,]+)?( warn=[a-z_,]+)?$' "$W/pkg/qa.line" 2>/dev/null || echo "QA PASS $REEL warn=qa_error"
  rm -f "$W/pkg/qa.line"
}

# reels and YouTube parts: same quality-targeted final encode (runner CPU is free, the user's bandwidth is not)
export YT_PRESET="${YT_PRESET:-slow}" YT_CRF="${YT_CRF:-22}" RF_PRESET="${RF_PRESET:-slow}" RF_CRF="${RF_CRF:-22}"
PUB_RE='^(HANDOFF (OK|FAIL|SKIP) [A-Za-z0-9_-]+( parts=[0-9]+| code=[a-z0-9_]+| reason=[a-z_]+)|PREVIEWS [A-Za-z0-9_-]+ ok=[0-9]|MEDIA [0-9]+ files from cache|MEDIA FAIL code=[a-z_]+)$'
# hand-off: <pkg> <id> <video inside pkg> [--yt]. Upload + previews + encrypted full copy run in parallel. -> 0 handed off (video removed from pkg)
hand() {
  local pkg="$1" id="$2" v="$3" yt="${4:-}" d hrc=0 frc=0; d="$(dirname "$v")"
  [ -n "${SOCIAL_LAMMA_TOKEN:-}" ] && [ -f "$v" ] || return 1
  python3 "$HERE/handoff.py" --file "$v" --id "$id" --out "$d/$id.handoff.json" --filename "$id.mp4" >"$W/ho.line" 2>/dev/null & local hp=$!
  python3 "$HERE/previews.py" "$v" "$d" "$id" $yt >"$W/pv.line" 2>/dev/null & local pp=$!
  ( tar -C "$pkg" -cf - "${v#"$pkg"/}" | "${OSSL[@]}" -e -salt -out "$OUT/$id.full.tar.enc" ) & local fp=$!
  wait "$pp" || true; wait "$fp" || frc=$?; wait "$hp" || hrc=$?
  cat "$W/ho.line" "$W/pv.line" 2>/dev/null | grep -E "$PUB_RE" || true
  if [ $hrc -eq 0 ] && [ $frc -eq 0 ] && [ -s "$d/$id.handoff.json" ]; then rm -f "$v"; return 0; fi
  rm -f "$OUT/$id.full.tar.enc" "$d/$id.handoff.json"; return 1
}

dec "$IN/code.tar.enc" "$W"
dec "$IN/$REEL.tar.enc" "$W"
if [ -f "$W/bundle/media.json" ]; then          # upload-once source cache
  python3 "$HERE/fetch_media.py" "$W/bundle" ${MEDIA_SRC:+--src "$MEDIA_SRC"} | grep -E "$PUB_RE" || { echo "source media missing from the cache"; exit 1; }
fi
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
    python3 "$W/code/farm_yt4.py" --job "$W/bundle" --out "$W/pkg/yt"
  } >"$LOG" 2>&1 || rc=$?
  grep -E '^(STEP |OK  |FAIL |reel [A-Za-z0-9_-]+ \|)' "$LOG" || true
  ls "$W/pkg/yt" | grep -qE '\.(mp4|m4a)$' || { [ $rc -ne 0 ] || rc=1; echo "no part produced (full log is in the encrypted output)"; }
  qa
  tar -C "$W/pkg" -cf - . | "${OSSL[@]}" -e -salt -out "$OUT/$REEL.out.tar.enc"
  echo "encrypted output: $REEL.out.tar.enc ($(du -h "$OUT/$REEL.out.tar.enc" | cut -f1)) rc=$rc"
  exit $rc
fi
{
  echo "reel $REEL | $(nproc) vCPU | $(ffmpeg -version | head -1)"
  python3 "$HERE/assemble.py" "$W/bundle" "$(nproc)" && \
  python3 "$HERE/cloud_render.py" --code "$W/code" --bundle "$W/bundle" --reel "$REEL" --out "$W/render" --preset "$RF_PRESET" --crf "$RF_CRF"
} >"$LOG" 2>&1 || rc=$?
# public progress only (no subtitles, captions, meme wants or errors with text)
grep -E '^(STEP |PROGRESS |OK  |FAIL |assemble: [A-Za-z0-9_]+\.mp4 [0-9]+ piece|  render path|MEMORY STOP|STOPPED|reel [A-Za-z0-9_-]+ \|)' "$LOG" \
  | sed -E 's#(/[^ ]*/)?([A-Za-z0-9_-]+\.mp4)#\2#g' || true
for f in "$W/render/reels/$REEL".mp4 "$W/render/reels/$REEL".caption.txt "$W/render/reels/$REEL".srt "$W/render/reels/$REEL".ass; do
  [ -f "$f" ] && cp "$f" "$W/pkg/reels/"
done
[ -f "$W/pkg/reels/$REEL.mp4" ] || { [ $rc -ne 0 ] || rc=1; echo "no video produced (full log is in the encrypted output)"; }
QAL="$(qa)"; echo "$QAL"
# direct hand-off only for a good render (QA FAIL stays on the PC for review, as today)
if [ $rc -eq 0 ] && [[ "$QAL" == "QA PASS "* ]]; then hand "$W/pkg" "$REEL" "$W/pkg/reels/$REEL.mp4" || true; fi
tar -C "$W/pkg" -cf - . | "${OSSL[@]}" -e -salt -out "$OUT/$REEL.out.tar.enc"
echo "encrypted output: $REEL.out.tar.enc ($(du -h "$OUT/$REEL.out.tar.enc" | cut -f1)) rc=$rc"
exit $rc
