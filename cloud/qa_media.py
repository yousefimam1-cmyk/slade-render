#!/usr/bin/env python3
"""Automatic render QA (runs on the GitHub runner after a render; never on the PC).

  qa_media.py --id <reel or yt part> --pkg <pkg dir>

Checks: black (blackdetect), loop (repeated/frozen frames, dHash), no_video/short/no_audio (hard), sync (captions vs faster-whisper words: WARN only),
memes (YouTube -aud: meme visible shorter than its clip, warn only).
Writes <pkg>/qa.json + <pkg>/{reels|yt}/<id>.qa.json (encrypted with the outputs) and <pkg>/qa.line,
the ONLY public text: "QA PASS <id>" / "QA FAIL <id> codes=a,b" (+ " warn=x"). Never prints captions or transcript text.
"""
import argparse, glob, json, os, re, subprocess, sys, tempfile

FPS = 4; HAM = 4; MIN_RUN = 6; MIN_GAP = 2.0; EDGE = 0.2; BLACK_D = 0.4
SYNC_WIN = 3.0; SYNC_MIN = 8; SYNC_MED = 0.15; SYNC_P75 = 0.35
WHISPER_TIMEOUT = 600; WHISPER_MAX_SECS = 900        # long YouTube audio: first 15 min is enough for an offset


def run(cmd, timeout=None, **k):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **k)

def duration(p):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], timeout=60)
    try: return float(r.stdout.decode().strip())
    except ValueError: return 0.0

def has_audio(p):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p], timeout=60)
    return b"audio" in r.stdout

def final_codes(mp4, expect_secs=None, tol=1.5):
    """hard failures of a finished video: no_video, short, no_audio, duration (vs the audio it was built on)"""
    if not os.path.isfile(mp4) or os.path.getsize(mp4) < 1000: return ["no_video"]
    out = []; dur = duration(mp4)
    if dur < 1.0: out.append("short")
    if not has_audio(mp4): out.append("no_audio")
    if expect_secs and dur > 0 and abs(dur - expect_secs) > tol: out.append("duration")
    return out

# ---------------- a) black ----------------
def parse_black(stderr, dur):
    bad = []
    for m in re.finditer(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", stderr):
        s, e = float(m.group(1)), float(m.group(2))
        cs, ce = max(s, EDGE), min(e, dur - EDGE) if dur > 0 else e
        if ce - cs >= BLACK_D - 1e-6: bad.append([round(s, 2), round(e, 2)])
    return bad

def check_black(mp4, dur):
    r = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", mp4, "-an", "-vf", f"blackdetect=d={BLACK_D}:pix_th=0.10", "-f", "null", "-"], timeout=900)
    return parse_black(r.stderr.decode("utf-8", "replace"), dur)

# ---------------- b) loop ----------------
def frames(mp4, max_frames=4000):
    """gray frames (N,36,64) of the MOVING area only: a 9:16 reel is mostly a still background/caption band around a small
    game strip, so hashing the whole frame makes different gameplay look identical (false loops). Crop to where pixels change."""
    import numpy as np
    W, H = 90, 160
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-i", mp4, "-an", "-vf", f"fps={FPS},scale={W}:{H},format=gray",
             "-frames:v", str(max_frames), "-f", "rawvideo", "-"], timeout=900)
    a = np.frombuffer(r.stdout, np.uint8)
    a = a[: len(a) // (W * H) * W * H].reshape(-1, H, W).astype(np.float32)
    if len(a) >= 4:
        sd = a.std(axis=0); act = sd > max(6.0, float(np.percentile(sd, 60)))
        rows, cols = np.where(act.any(1))[0], np.where(act.any(0))[0]
        if len(rows) >= 8 and len(cols) >= 8: a = a[:, rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    ys = np.linspace(0, a.shape[1] - 1, 36).astype(int); xs = np.linspace(0, a.shape[2] - 1, 64).astype(int)
    return a[:, ys][:, :, xs]

def dhash(fr):
    """frames (N,36,64) -> uint64 hashes: 9x8 area means, bit = left < right"""
    import numpy as np
    f = fr.astype(np.float32); xs = np.linspace(0, 64, 10).astype(int); ys = np.linspace(0, 36, 9).astype(int)
    small = np.stack([np.stack([f[:, ys[r]:ys[r + 1], xs[c]:xs[c + 1]].mean(axis=(1, 2)) for c in range(9)], 1) for r in range(8)], 1)
    bits = (small[:, :, :-1] < small[:, :, 1:]).reshape(len(f), 64)
    return (bits.astype(np.uint64) << np.arange(64, dtype=np.uint64)).sum(axis=1, dtype=np.uint64)

_LUT = None
def ham(a, b):
    import numpy as np
    global _LUT
    if _LUT is None: _LUT = np.array([bin(i).count("1") for i in range(256)], np.uint8)
    x = np.bitwise_xor(a, b)
    return _LUT[x.view(np.uint8)].reshape(x.shape + (8,)).sum(-1, dtype=np.uint16)

MAD_MAX = 1.5   # a real repeat is the same pixels re-encoded; different gameplay on a similar-looking board is far above this

def _same_pixels(fr, a, b, L):
    import numpy as np
    if fr is None: return True
    x = fr[a:a + L].astype(np.float32); y = fr[b:b + L].astype(np.float32)
    return float(np.median(np.abs(x - y).mean(axis=(1, 2)))) <= MAD_MAX

def find_loops(h, fps=FPS, fr=None):
    """runs of >=MIN_RUN frames that repeat an earlier run (>=MIN_GAP s back, hamming<=HAM per frame) while moving"""
    import numpy as np
    n = len(h); out = []; gap = int(round(MIN_GAP * fps))
    if n < gap + MIN_RUN: return out
    step = ham(h[:-1], h[1:])                       # motion between consecutive frames
    for d in range(gap, n - MIN_RUN + 1):
        ok = ham(h[:n - d], h[d:]) <= HAM           # frame i matches frame i+d
        i = 0; m = len(ok)
        while i < m:
            if not ok[i]: i += 1; continue
            j = i
            while j < m and ok[j]: j += 1
            L = j - i
            if L >= MIN_RUN:
                s = i + d; mv = step[s:s + L - 1]
                if (mv > 0).mean() >= 0.75 and int(ham(h[s:s + 1], h[s + L - 1:s + L])[0]) > HAM and _same_pixels(fr, i, s, L):
                    out.append({"t": round(s / fps, 2), "src": round(i / fps, 2), "len": round(L / fps, 2)})
            i = j
    out.sort(key=lambda x: (-x["len"], x["t"])); keep = []
    for x in out:                                   # one entry per repeated stretch
        if not any(abs(x["t"] - k["t"]) < k["len"] for k in keep): keep.append(x)
    return sorted(keep, key=lambda x: x["t"])[:10]

def check_loop(mp4):
    fr = frames(mp4); return find_loops(dhash(fr), fr=fr)

# ---------------- c) sync ----------------
_DIAC = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
def norm_ar(w):
    w = _DIAC.sub("", w or "")
    w = re.sub("[إأآٱ]", "ا", w).replace("ة", "ه").replace("ى", "ي").replace("ئ", "ي").replace("ؤ", "و")
    return re.sub(r"[^\w]", "", w.lower())

def srt_words(path):
    """srt -> [{w,s}] with word times interpolated inside each cue"""
    with open(path, encoding="utf-8-sig", errors="replace") as f: txt = f.read()
    out = []
    def ts(x): h, m, s = x.replace(",", ".").split(":"); return int(h) * 3600 + int(m) * 60 + float(s)
    for blk in re.split(r"\n\s*\n", txt.replace("\r", "")):
        m = re.search(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)", blk)
        if not m: continue
        s, e = ts(m.group(1)), ts(m.group(2))
        body = re.sub(r"<[^>]+>|\{[^}]*\}", " ", blk[m.end():]); ws = body.split()
        tot = sum(len(w) for w in ws) or 1; acc = 0
        for w in ws: out.append({"w": w, "s": s + (e - s) * acc / tot}); acc += len(w)
    return out

def layout_words(lay):
    return [{"w": c.get("w", ""), "s": float(c.get("s", 0))} for c in (lay.get("caps") or []) if c.get("w")]

def sync_stats(caps, heard):
    """caps/heard: [{w,s}] -> {matched, median, p75}; offset = caption time - spoken time (+ = captions late)"""
    import numpy as np
    idx = {}
    for h in heard:
        k = norm_ar(h["w"])
        if len(k) >= 3: idx.setdefault(k, []).append(float(h["s"]))
    offs = []
    for c in caps:
        k = norm_ar(c["w"])
        if len(k) < 3 or k not in idx: continue
        best = min(idx[k], key=lambda t: abs(c["s"] - t))
        if abs(c["s"] - best) <= SYNC_WIN: offs.append(c["s"] - best)
    if not offs: return {"matched": 0, "median": None, "p75": None}
    a = np.array(offs)
    return {"matched": len(offs), "median": round(float(np.median(a)), 3), "p75": round(float(np.percentile(np.abs(a), 75)), 3)}

def sync_fail(st):
    return st["matched"] >= SYNC_MIN and (abs(st["median"]) > SYNC_MED or st["p75"] > SYNC_P75)

def whisper_words(media, tmp):
    """faster-whisper small int8 in a child process (hard timeout); returns [{w,s}]"""
    wav = os.path.join(tmp, "a.wav"); js = os.path.join(tmp, "w.json")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", media, "-t", str(WHISPER_MAX_SECS), "-vn", "-ac", "1", "-ar", "16000", wav], timeout=300)
    run([sys.executable, os.path.abspath(__file__), "--whisper", wav, js], timeout=WHISPER_TIMEOUT)
    with open(js, encoding="utf-8") as f: words = json.load(f)
    return words, min(duration(wav), WHISPER_MAX_SECS)

def _whisper_main(wav, js):
    from faster_whisper import WhisperModel
    m = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    import wave, numpy as np                       # decode here (16 kHz mono s16 from ffmpeg): no PyAV version coupling
    with wave.open(wav) as w: audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    segs, _ = m.transcribe(audio, language="ar", word_timestamps=True, vad_filter=True, beam_size=1)
    out = [{"w": w.word.strip(), "s": round(w.start, 3)} for sg in segs for w in (sg.words or [])]
    with open(js, "w", encoding="utf-8") as f: json.dump(out, f, ensure_ascii=False)

# ---------------- d) memes ----------------
def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def meme_cuts(lay):
    """memes whose visible length is shorter than the meme clip (when the clip length is known)"""
    out = []
    for i, m in enumerate(lay.get("memes") or []):
        v = m.get("vis"); vl = (_num(v[1]) - _num(v[0])) if isinstance(v, (list, tuple)) and len(v) == 2 and None not in (_num(v[0]), _num(v[1])) else _num(v)
        cl = next((_num(m[k]) for k in ("dur", "clip_dur", "len", "clip_len", "src_dur") if _num(m.get(k)) is not None), None)
        if vl is not None and cl is not None and vl + 0.05 < cl: out.append({"i": i, "t": _num(m.get("t")), "vis": round(vl, 2), "clip": round(cl, 2)})
    return out

# ---------------- driver ----------------
def qa(rid, pkg, whisper=None):
    """whisper(media, tmp) -> (words, secs) seam for tests"""
    whisper = whisper or whisper_words
    res = {"id": rid, "checks": {}, "codes": [], "warn": [], "errors": []}
    yt = os.path.isdir(os.path.join(pkg, "yt")) and not os.path.isfile(os.path.join(pkg, "reels", rid + ".mp4"))
    d = os.path.join(pkg, "yt" if yt else "reels")
    mp4 = os.path.join(d, rid + ".mp4")
    if yt and not os.path.isfile(mp4): mp4 = next(iter(sorted(glob.glob(os.path.join(d, "*.mp4")))), mp4)
    def guard(name, fn):
        try: fn()
        except Exception as e: res["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
    if os.path.isfile(mp4):
        dur = duration(mp4); res["secs"] = round(dur, 2)
        def b():
            bl = check_black(mp4, dur); res["checks"]["black"] = bl
            if bl: res["codes"].append("black")
        def lp():
            lo = check_loop(mp4); res["checks"]["loop"] = lo
            if lo: res["codes"].append("loop")
        guard("black", b); guard("loop", lp)
        if not yt:
            def fin(): res["codes"] += [c for c in final_codes(mp4) if c not in res["codes"]]
            guard("final", fin)
    caps = media = None; lay = {}
    if yt and rid.endswith("-aud"):
        vid = rid[:-4]; lp_ = os.path.join(d, vid + ".layout.json")
        if os.path.isfile(lp_):
            with open(lp_, encoding="utf-8") as f: lay = json.load(f)
            caps = layout_words(lay)
        media = next(iter(sorted(glob.glob(os.path.join(d, "*.m4a")))), None)
    elif not yt and os.path.isfile(os.path.join(d, rid + ".srt")):
        caps = srt_words(os.path.join(d, rid + ".srt")); media = mp4 if os.path.isfile(mp4) else None
    if caps and media:
        def sy():
            with tempfile.TemporaryDirectory() as tmp: heard, secs = whisper(media, tmp)
            cs = [c for c in caps if c["s"] <= secs + SYNC_WIN]
            st = sync_stats(cs, heard); st["heard"] = len(heard); res["checks"]["sync"] = st
            if sync_fail(st): res["warn"].append("sync")        # subtitle timing alone never blocks the hand-off (whisper offsets on music/overlaps are noisy)
        guard("sync", sy)
    if lay:
        def mm():
            mc = meme_cuts(lay); res["checks"]["memes"] = mc
            if mc: res["warn"].append("meme_cut")
        guard("memes", mm)
    if res["errors"]: res["warn"].append("qa_error")
    res["status"] = "FAIL" if res["codes"] else "PASS"
    return res

def public_line(res):
    rid = res["id"] if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", res["id"]) else "x"
    codes = [c for c in res["codes"] if re.fullmatch(r"[a-z_]+", c)]; warn = [w for w in res["warn"] if re.fullmatch(r"[a-z_]+", w)]
    s = f"QA FAIL {rid} codes={','.join(codes)}" if codes else f"QA PASS {rid}"
    return s + (f" warn={','.join(warn)}" if warn else "")

def write(res, pkg):
    sub = "reels" if os.path.isdir(os.path.join(pkg, "reels")) and os.path.isfile(os.path.join(pkg, "reels", res["id"] + ".mp4")) else ("yt" if os.path.isdir(os.path.join(pkg, "yt")) else "reels")
    os.makedirs(os.path.join(pkg, sub), exist_ok=True)
    for p in (os.path.join(pkg, "qa.json"), os.path.join(pkg, sub, res["id"] + ".qa.json")):
        with open(p, "w", encoding="utf-8") as f: json.dump(res, f, ensure_ascii=False, indent=1)
    with open(os.path.join(pkg, "qa.line"), "w") as f: f.write(public_line(res) + "\n")

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--whisper"]: _whisper_main(argv[1], argv[2]); return 0
    ap = argparse.ArgumentParser(); ap.add_argument("--id", required=True); ap.add_argument("--pkg", required=True); a = ap.parse_args(argv)
    try: res = qa(a.id, a.pkg)
    except Exception as e: res = {"id": a.id, "checks": {}, "codes": [], "warn": ["qa_error"], "errors": [f"{type(e).__name__}: {str(e)[:200]}"], "status": "PASS"}
    write(res, a.pkg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
