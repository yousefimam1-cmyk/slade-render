#!/usr/bin/env bash
# YouTube join on the runner (the "join" job, after every part of a video rendered):  yt_join_job.sh <vid> <parts dir> <code.tar.enc> <outdir>
# <parts dir> holds the encrypted part outputs <vid>-aud.out.tar.enc, <vid>-cNN.out.tar.enc (GitHub artifacts, copied inside GitHub: fast).
# Decrypts them in parallel, joins with the PC's own yt_join.py (stream copy, frame-checked), sums the part QA, then (QA PASS + token):
# hand-off to Social Lamma + previews + <outdir>/<vid>.full.tar.enc; <outdir>/<vid>.out.tar.enc = sidecars (srt, chapters, description,
# thumbnail, layout, parts.json, qa) + handoff.json, no video. Hand-off failed -> the joined mp4 goes inside <vid>.out.tar.enc (one file instead
# of all the parts). Missing part -> no output at all: the PC falls back to the parts. env RENDER_KEY, SOCIAL_LAMMA_TOKEN, SOCIAL_DIRECT (0 = off).
# Sync (subtitle timing) is only a warning; the joined video must have audio, >= 1 s and the audio's length, else it stays on the PC path.
set -euo pipefail
VID="$1"; ART="$(cd "$2" && pwd)"; CODE="$3"; OUT="$4"; HERE="$(cd "$(dirname "$0")" && pwd)"
[[ "$VID" =~ ^[A-Za-z0-9_]{1,30}$ ]] || { echo "bad vid"; exit 2; }
[ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY missing"; exit 2; }
if [ -z "${SOCIAL_LAMMA_TOKEN:-}" ]; then echo "JOIN SKIP $VID reason=no_token"; exit 0; fi
RENDER_KEY="$(printf '%s' "$RENDER_KEY" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | tr -d '\r\n')"; export RENDER_KEY
OSSL=(openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass env:RENDER_KEY)
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
PUB_RE='^(HANDOFF (OK|FAIL|SKIP) [A-Za-z0-9_-]+( parts=[0-9]+| code=[a-z0-9_]+| reason=[a-z_]+)|PREVIEWS [A-Za-z0-9_-]+ ok=[0-9])$'
dec() { "${OSSL[@]}" -d -in "$1" | tar -C "$2" --no-same-owner -xf -; }

shopt -s nullglob; parts=("$ART/$VID"-*.out.tar.enc); shopt -u nullglob
[ ${#parts[@]} -gt 0 ] || { echo "JOIN SKIP $VID reason=no_parts"; exit 0; }
for f in "${parts[@]}" "$CODE"; do [ "$(head -c 8 "$f")" = "Salted__" ] || { echo "not encrypted: $(basename "$f")"; exit 1; }; done
mkdir -p "$W/code" "$W/p" "$W/pkg/yt"
dec "$CODE" "$W"
export W
printf '%s\n' "${parts[@]}" | xargs -P "$(nproc)" -I{} bash -c 'set -o pipefail; b="$(basename "$1" .out.tar.enc)"; mkdir -p "$W/p/$b" &&
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass env:RENDER_KEY -d -in "$1" | tar -C "$W/p/$b" --no-same-owner -xf -' _ {}
# every part's yt/ files side by side (chunks, m4a, sidecars, part qa); part logs -> one encrypted render.log
for d in "$W"/p/*/; do
  b="$(basename "$d")"; [ -d "$d/yt" ] && cp -n "$d"/yt/* "$W/pkg/yt/" 2>/dev/null || true
  [ -f "$d/render.log" ] && { echo "===== $b"; cat "$d/render.log"; } >>"$W/pkg/render.log"
done
[ -f "$W/pkg/yt/$VID.parts.json" ] || { echo "JOIN SKIP $VID reason=no_aud_part"; exit 0; }
need=$(python3 -c 'import json,sys; P=json.load(open(sys.argv[1])); print(" ".join(sys.argv[2] + "-" + c["part"] + ".mp4" for c in P["chunks"]))' "$W/pkg/yt/$VID.parts.json" "$VID")
for n in $need "$VID.m4a"; do [ -f "$W/pkg/yt/$n" ] || { echo "JOIN SKIP $VID reason=missing_part"; exit 0; }; done
python3 "$W/code/yt_join.py" "$W/pkg/yt" "$VID" --out "$W/pkg/yt/$VID.mp4" --clean >>"$W/pkg/render.log" 2>&1 || { echo "JOIN FAIL $VID code=join"; exit 1; }
AUD_SECS=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$W/pkg/yt/$VID.m4a" 2>/dev/null || echo 0)
rm -f "$W/pkg/yt/$VID.m4a"
echo "JOIN OK $VID"
# QA of the whole video = every part's QA (qa_media already ran per part on its runner)
QAL=$(python3 - "$W/pkg/yt" "$VID" "$HERE" "${AUD_SECS:-0}" <<'EOF'
import glob, json, os, re, sys
d, vid, here, aud = sys.argv[1:5]; parts = {}; codes = []; warn = []
sys.path.insert(0, here)
try:                                        # hard checks on the joined video itself: exists, not tiny, has audio, length = the audio it was built on
    import qa_media; codes += qa_media.final_codes(os.path.join(d, vid + ".mp4"), float(aud) or None)
except Exception: warn.append("qa_error")
for p in sorted(glob.glob(os.path.join(d, vid + "-*.qa.json"))):
    try: q = json.load(open(p, encoding="utf-8"))
    except Exception: continue
    pid = os.path.basename(p)[:-8]; parts[pid] = q.get("status")
    if q.get("status") == "FAIL": codes += [c for c in q.get("codes") or [] if re.fullmatch(r"[a-z_]+", c)]
    warn += [w for w in q.get("warn") or [] if re.fullmatch(r"[a-z_]+", w)]
codes, warn = sorted(set(codes)), sorted(set(warn))
res = {"id": vid, "status": "FAIL" if codes else "PASS", "codes": codes, "warn": warn, "parts": parts, "checks": {}}
json.dump(res, open(os.path.join(d, vid + ".qa.json"), "w"), indent=1)
print((f"QA FAIL {vid} codes={','.join(codes)}" if codes else f"QA PASS {vid}") + (f" warn={','.join(warn)}" if warn else ""))
EOF
)
echo "$QAL" | grep -E '^QA (PASS|FAIL) [A-Za-z0-9_-]+( codes=[a-z_,]+)?( warn=[a-z_,]+)?$' || true
# SOCIAL_DIRECT=0 (opt-out) keeps today's path: the PC downloads the video
if [[ "$QAL" == "QA PASS "* ]] && [[ "$(printf '%s' "${SOCIAL_DIRECT:-1}" | tr 'A-Z' 'a-z')" != @(0|false|off|no|n) ]]; then
  v="$W/pkg/yt/$VID.mp4"; hrc=0; frc=0
  python3 "$HERE/handoff.py" --file "$v" --id "$VID" --out "$W/pkg/yt/$VID.handoff.json" --filename "$VID.mp4" >"$W/ho.line" 2>/dev/null & hp=$!
  python3 "$HERE/previews.py" "$v" "$W/pkg/yt" "$VID" --yt >"$W/pv.line" 2>/dev/null & pp=$!
  ( tar -C "$W/pkg" -cf - "yt/$VID.mp4" | "${OSSL[@]}" -e -salt -out "$OUT/$VID.full.tar.enc" ) & fp=$!
  wait "$pp" || true; wait "$fp" || frc=$?; wait "$hp" || hrc=$?
  cat "$W/ho.line" "$W/pv.line" 2>/dev/null | grep -E "$PUB_RE" || true
  if [ $hrc -eq 0 ] && [ $frc -eq 0 ] && [ -s "$W/pkg/yt/$VID.handoff.json" ]; then rm -f "$v"; else rm -f "$OUT/$VID.full.tar.enc" "$W/pkg/yt/$VID.handoff.json"; fi
fi
tar -C "$W/pkg" -cf - . | "${OSSL[@]}" -e -salt -out "$OUT/$VID.out.tar.enc"
echo "encrypted output: $VID.out.tar.enc ($(du -h "$OUT/$VID.out.tar.enc" | cut -f1))"
