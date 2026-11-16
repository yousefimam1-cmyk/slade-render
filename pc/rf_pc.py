#!/usr/bin/env python3
"""PC side of the Slade render farm. RENDER_KEY lives only here (user env var, SET_RENDER_KEY.bat) and in the repo secret.

  rf_pc.py bundle  [--segs DIR] [--reels r01,rG1]   pieces + assets + plan -> <segs>\\<reel>.tar.enc, + code.tar.enc
  rf_pc.py ytbundle --plan pro_plan_v5.json --vids y1,y2 [--segs DIR]
                                                   YouTube: one bundle per part (<vid>-aud = audio track + sidecars, <vid>-cNN = video chunk)
                                                   -> <segs>\\<vid>-<part>.tar.enc + code.tar.enc + yt_parts.txt (the id list for the cloud run)
  rf_pc.py decrypt [--in DIR] [--out DIR]          <in>\\*.out.tar.enc -> reels folder (mp4 + caption.txt + srt), YouTube parts -> youtube\\<vid>\\
                                                   (joined into youtube\\<vid>.mp4 as soon as every part of a video is there), logs\\
  rf_pc.py check                                   key present? which cipher backend?
  rf_pc.py enc <in.tar> <out.enc> | dec <in.enc> <out.tar>   raw file primitives (tests)

Format = `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:RENDER_KEY` ("Salted__"+salt, PBKDF2-HMAC-SHA256
-> 32 B key + 16 B IV, PKCS#7). Backend: openssl.exe if it is OpenSSL >= 1.1.1, else Python `cryptography` (uv run --with cryptography).
Tars are streamed straight into the cipher: no plaintext tar is ever written.
"""
import os, re, io, sys, json, glob, shutil, tarfile, argparse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ITER, MAGIC = 200000, b"Salted__"
CODE_FILES = ["farm_pro.py", "farm_gpu.py", "farm_sfx.py", "farm_caps.py", "farm_yt4.py", "farm_discord.py", "farm_music.py", "yt_join.py", "gaming.cube"]
CODE_DIRS = ["g30_sfx", "g30_fonts", "fonts", "models", os.path.join("memes", "synth")]
CHUNK = 1 << 20

def key():
    k = (os.environ.get("RENDER_KEY") or "").strip()
    if not k and os.name == "nt":                       # a cmd window opened before SET_RENDER_KEY.bat does not see the new var yet
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h: k = str(winreg.QueryValueEx(h, "RENDER_KEY")[0]).strip()
        except OSError: k = ""
    if len(k) < 16: sys.exit("RENDER_KEY not set: run SET_RENDER_KEY.bat (same value as the GitHub secret)")
    return k

def backend():
    want = os.environ.get("RF_BACKEND", "auto")
    if want in ("auto", "openssl") and shutil.which("openssl"):
        v = subprocess.run(["openssl", "version"], capture_output=True, text=True).stdout
        m = re.match(r"OpenSSL (\d+)\.(\d+)\.(\d+)", v)
        if m and tuple(map(int, m.groups())) >= (1, 1, 1): return "openssl"
    if want == "openssl": sys.exit("openssl >= 1.1.1 not found")
    return "py"

OSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITER), "-md", "sha256", "-salt", "-pass", "env:RENDER_KEY"]

# ---- pure-Python stream cipher objects (file-like) ----
def _kdf(k, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    d = PBKDF2HMAC(algorithm=hashes.SHA256(), length=48, salt=salt, iterations=ITER).derive(k.encode("utf-8")); return d[:32], d[32:]

class EncWriter(io.RawIOBase):
    def __init__(s, fo, k):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        salt = os.urandom(8); kk, iv = _kdf(k, salt); s.fo = fo; fo.write(MAGIC + salt)
        s.c = Cipher(algorithms.AES(kk), modes.CBC(iv)).encryptor(); s.p = padding.PKCS7(128).padder()
    def writable(s): return True
    def write(s, b): s.fo.write(s.c.update(s.p.update(bytes(b)))); return len(b)
    def finish(s): s.fo.write(s.c.update(s.p.finalize()) + s.c.finalize())

class DecReader(io.RawIOBase):
    def __init__(s, fi, k):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        h = fi.read(16)
        if h[:8] != MAGIC: raise ValueError("not an openssl Salted__ file")
        kk, iv = _kdf(k, h[8:16]); s.fi = fi; s.buf = b""; s.eof = False
        s.c = Cipher(algorithms.AES(kk), modes.CBC(iv)).decryptor(); s.u = padding.PKCS7(128).unpadder()
    def readable(s): return True
    def readinto(s, b):
        while len(s.buf) < len(b) and not s.eof:
            ct = s.fi.read(CHUNK)
            if ct: s.buf += s.u.update(s.c.update(ct))
            else:
                try: s.buf += s.u.update(s.c.finalize()) + s.u.finalize()
                except ValueError: raise ValueError("bad decrypt (wrong RENDER_KEY?)")
                s.eof = True
        n = min(len(b), len(s.buf)); b[:n] = s.buf[:n]; s.buf = s.buf[n:]; return n

# ---- tar <-> encrypted file ----
def encrypt_tree(root, arc, out):
    """tar root/arc (streamed) -> out (.enc)"""
    k = key(); tmp = out + ".part"
    with open(tmp, "wb") as fo:
        if backend() == "openssl":
            p = subprocess.Popen(OSSL + ["-e"], stdin=subprocess.PIPE, stdout=fo, env=dict(os.environ, RENDER_KEY=k))
            with tarfile.open(fileobj=p.stdin, mode="w|", format=tarfile.PAX_FORMAT) as t: t.add(os.path.join(root, arc), arcname=arc)
            p.stdin.close()
            if p.wait(): raise RuntimeError("openssl encrypt failed")
        else:
            w = EncWriter(fo, k)
            with tarfile.open(fileobj=w, mode="w|", format=tarfile.PAX_FORMAT) as t: t.add(os.path.join(root, arc), arcname=arc)
            w.finish()
    os.replace(tmp, out); return out

def _safe_extract(t, dest):
    if hasattr(tarfile, "data_filter"): t.extractall(dest, filter="data"); return
    for m in t:
        p = os.path.realpath(os.path.join(dest, m.name))
        if not p.startswith(os.path.realpath(dest) + os.sep) or m.issym() or m.islnk(): raise ValueError(f"unsafe tar member {m.name}")
        t.extract(m, dest)

def decrypt_tree(enc, dest):
    k = key(); os.makedirs(dest, exist_ok=True)
    with open(enc, "rb") as fi:
        if backend() == "openssl":
            p = subprocess.Popen(OSSL + ["-d"], stdin=fi, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, RENDER_KEY=k))
            with tarfile.open(fileobj=p.stdout, mode="r|") as t: _safe_extract(t, dest)
            if p.wait(): raise RuntimeError(f"bad decrypt {os.path.basename(enc)} (wrong RENDER_KEY?)")
        else:
            with tarfile.open(fileobj=io.BufferedReader(DecReader(fi, k), CHUNK), mode="r|") as t: _safe_extract(t, dest)
    return dest

def enc_file(src, out):
    k = key()
    with open(src, "rb") as fi, open(out, "wb") as fo:
        if backend() == "openssl":
            if subprocess.run(OSSL + ["-e"], stdin=fi, stdout=fo, env=dict(os.environ, RENDER_KEY=k)).returncode: raise RuntimeError("encrypt failed")
        else:
            w = EncWriter(fo, k)
            for b in iter(lambda: fi.read(CHUNK), b""): w.write(b)
            w.finish()

def dec_file(src, out):
    k = key()
    with open(src, "rb") as fi, open(out, "wb") as fo:
        if backend() == "openssl":
            if subprocess.run(OSSL + ["-d"], stdin=fi, stdout=fo, env=dict(os.environ, RENDER_KEY=k)).returncode: raise RuntimeError("bad decrypt")
        else:
            r = io.BufferedReader(DecReader(fi, k), CHUNK)
            for b in iter(lambda: r.read(CHUNK), b""): fo.write(b)

# ---- bundle ----
def stage_code(scripts, stage, segs):
    c = os.path.join(stage, "code"); os.makedirs(c, exist_ok=True)
    for f in CODE_FILES:
        if os.path.isfile(os.path.join(scripts, f)): shutil.copy2(os.path.join(scripts, f), c)
    if not os.path.isfile(os.path.join(c, "farm_pro.py")): sys.exit(f"farm_pro.py not found in {scripts}")
    for d in CODE_DIRS:
        if os.path.isdir(os.path.join(scripts, d)): shutil.copytree(os.path.join(scripts, d), os.path.join(c, d), dirs_exist_ok=True)
    encrypt_tree(stage, "code", os.path.join(segs, "code.tar.enc")); print("code.tar.enc")

def probe_dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: return None

def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _put(src, dst):
    if os.path.exists(dst): return
    try: os.link(src, dst)
    except OSError: shutil.copy2(src, dst)

def cmd_bundle(a):
    segs = a.segs; ps = json.load(open(os.path.join(segs, "plan_segs.json"), encoding="utf-8")); key()
    want = [x.strip() for x in a.reels.split(",") if x.strip()] if a.reels else list(ps["reels"])
    pieces = os.path.join(segs, "pieces"); memes = os.path.join(segs, "assets", "memes"); ppd = os.path.join(segs, "assets", "propack")
    ppf = [os.path.join(r, f) for r, _, fs in os.walk(ppd) for f in fs] if os.path.isdir(ppd) else []
    stage = os.path.join(segs, "_stage"); shutil.rmtree(stage, ignore_errors=True)
    print(f"cipher backend: {backend()}")
    try:
        stage_code(a.scripts, stage, segs)
        for rid in want:
            R = ps["reels"][rid]; warn = []; b = os.path.join(stage, rid, "bundle")
            for d in ("pieces", "memes", "propack"): os.makedirs(os.path.join(b, d))
            for u in R["units"]:
                for p in u["pieces"]:
                    src = os.path.join(pieces, p["name"])
                    if not os.path.isfile(src): sys.exit(f"{rid}: missing piece {p['name']} (run CUT_SEGMENTS.bat)")
                    d = probe_dur(src); w = p["b"] - p["a"]
                    if d is not None and abs(d - w) > 0.75: warn.append(f"{p['name']} is {d:.2f}s, expected {w:.2f}s (keyframe-snapped cut? sync may be off)")
                    _put(src, os.path.join(b, "pieces", p["name"]))
            if os.path.isfile(os.path.join(memes, "manifest.json")): shutil.copy2(os.path.join(memes, "manifest.json"), os.path.join(b, "memes"))
            else: warn.append("no memes\\manifest.json: audio memes will be skipped")
            for mid in R["assets"]["memes"]:
                f = os.path.join(memes, mid + ".wav")
                if os.path.isfile(f): _put(f, os.path.join(b, "memes", mid + ".wav"))
                else: warn.append(f"meme {mid}.wav missing")
            for cn in R["assets"]["clips"]:
                hit = [f for f in ppf if norm(cn) and norm(cn) in norm(os.path.splitext(os.path.basename(f))[0])]
                for f in hit: _put(f, os.path.join(b, "propack", os.path.basename(f)))
                if not hit: warn.append(f"clip {cn} missing")
            json.dump(R["plan"], open(os.path.join(b, "plan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            json.dump(dict(units=R["units"], fill=ps["fill"]), open(os.path.join(b, "units.json"), "w"), indent=1)
            out = encrypt_tree(os.path.join(stage, rid), "bundle", os.path.join(segs, rid + ".tar.enc"))
            shutil.rmtree(os.path.join(stage, rid), ignore_errors=True)
            print(f"{rid}.tar.enc  {os.path.getsize(out) / 2 ** 20:.1f} MB" + "".join(f"\n  WARNING {x}" for x in warn))
    finally: shutil.rmtree(stage, ignore_errors=True)
    print(f"done: {len(want)} reels + code.tar.enc in {segs} (encrypted only: give these *.tar.enc to the agent)")

def _extract_audio(src, dst):
    """audio-only copy of a piece (both tracks, stream copy) for the YouTube audio part: a fraction of the upload"""
    if os.path.exists(dst): return
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:a", "-c", "copy", dst], capture_output=True, text=True)
    if r.returncode: sys.exit(f"audio extract failed for {os.path.basename(src)}: {r.stderr[-300:]}")

def cmd_ytbundle(a):
    """one encrypted bundle per YouTube part: job.json + plan.json (youtube only) + the pieces that part reads + memes/propack it needs"""
    sys.path.insert(0, a.scripts); import farm_yt4 as Y
    segs = a.segs; key(); plan = json.load(open(os.path.join(a.scripts, a.plan) if not os.path.isabs(a.plan) else a.plan, encoding="utf-8-sig"))
    pdir = os.path.join(segs, "pieces"); P = Y.pieces_of(pdir); names = [os.path.basename(p[2]) for p in P]
    memes = os.path.join(segs, "assets", "memes"); ppd = os.path.join(segs, "assets", "propack")
    ppf = [os.path.join(r, f) for r, _, fs in os.walk(ppd) for f in fs] if os.path.isdir(ppd) else []
    yplan = {k: v for k, v in plan.items() if k not in ("reels", "post_order")}
    stage = os.path.join(segs, "_stage_yt"); shutil.rmtree(stage, ignore_errors=True); ids = []
    print(f"cipher backend: {backend()}")
    try:
        stage_code(a.scripts, stage, segs)
        for vid in [v.strip() for v in a.vids.split(",") if v.strip()]:
            y = next((v for v in plan["youtube"] if v["id"] == vid), None)
            if not y: sys.exit(f"{vid} not in {a.plan}")
            cp = Y.plan_chunks(y, P); want_m = {m["id"] for m in y.get("memes", []) if m.get("id")}; want_c = {m["clip"] for m in y.get("memes", []) if m.get("clip")}
            parts = [dict(cp["aud"], part="aud")] + cp["chunks"]
            for pt in parts:
                pid = f"{vid}-{pt['part']}"; b = os.path.join(stage, pid, "bundle"); warn = []
                for d in ("pieces", "memes", "propack"): os.makedirs(os.path.join(b, d))
                for n in pt["pieces"]:
                    src = os.path.join(pdir, n)
                    if not os.path.isfile(src): sys.exit(f"{pid}: missing piece {n} (run CUT_YT.bat)")
                    if pt["part"] == "aud": _extract_audio(src, os.path.join(b, "pieces", os.path.splitext(n)[0] + ".mka"))
                    else: _put(src, os.path.join(b, "pieces", n))
                if pt["part"] == "aud" and cp["aud"].get("thumb_piece"): _put(os.path.join(pdir, cp["aud"]["thumb_piece"]), os.path.join(b, "pieces", cp["aud"]["thumb_piece"]))
                if os.path.isfile(os.path.join(memes, "manifest.json")): shutil.copy2(os.path.join(memes, "manifest.json"), os.path.join(b, "memes"))
                for mid in want_m:
                    f = os.path.join(memes, mid + ".wav")
                    if os.path.isfile(f): _put(f, os.path.join(b, "memes", mid + ".wav"))
                    else: warn.append(f"meme {mid}.wav missing")
                for cn in want_c:
                    hit = [f for f in ppf if norm(cn) and norm(cn) in norm(os.path.splitext(os.path.basename(f))[0])]
                    for f in hit: _put(f, os.path.join(b, "propack", os.path.basename(f)))
                    if not hit: warn.append(f"clip {cn} missing")
                job = dict(vid=vid, part=pt["part"], pieces=names, chunks=cp["chunks"], frames=cp["frames"],
                           need_demucs=bool(pt["part"] == "aud" and y.get("discord_audio")))
                json.dump(job, open(os.path.join(b, "job.json"), "w"), indent=1)
                json.dump(yplan, open(os.path.join(b, "plan.json"), "w", encoding="utf-8"), ensure_ascii=False)
                out = encrypt_tree(os.path.join(stage, pid), "bundle", os.path.join(segs, pid + ".tar.enc"))
                shutil.rmtree(os.path.join(stage, pid), ignore_errors=True); ids.append(pid)
                print(f"{pid}.tar.enc  {os.path.getsize(out) / 2 ** 20:.1f} MB" + "".join(f"\n  WARNING {x}" for x in warn))
    finally: shutil.rmtree(stage, ignore_errors=True)
    open(os.path.join(segs, "yt_parts.txt"), "w").write(",".join(ids) + "\n")
    print(f"done: {len(ids)} parts + code.tar.enc in {segs}\ncloud ids (yt_parts.txt): {','.join(ids)}")

def cmd_decrypt(a):
    src = a.inp; out = a.out
    if not out:
        try:
            ps = json.load(open(os.path.join(a.segs, "plan_segs.json"), encoding="utf-8"))
            stem = os.path.splitext(os.path.basename((ps.get("source") or "cloud").replace("\\", "/")))[0]
        except OSError: stem = "cloud"
        out = os.path.join(a.scripts, "scan", stem + ".pro", "reels")
    os.makedirs(out, exist_ok=True); logs = os.path.join(src, "logs"); os.makedirs(logs, exist_ok=True); n = 0; yt_dirs = set()
    for e in sorted(glob.glob(os.path.join(src, "*.out.tar.enc"))):
        rid = os.path.basename(e)[:-len(".out.tar.enc")]; tmp = os.path.join(src, "_dec_" + rid)
        shutil.rmtree(tmp, ignore_errors=True); decrypt_tree(e, tmp)
        for f in glob.glob(os.path.join(tmp, "reels", "*")):
            if os.path.isfile(f): shutil.copy2(f, out)
        ytf = [f for f in glob.glob(os.path.join(tmp, "yt", "*")) if os.path.isfile(f)]
        if ytf:   # YouTube part: <vid>-aud (m4a + sidecars) or <vid>-cNN (video chunk)
            vid = rid.split("-")[0]; yd = os.path.join(os.path.dirname(out), "youtube", vid); os.makedirs(yd, exist_ok=True)
            for f in ytf: shutil.copy2(f, yd)
            yt_dirs.add((vid, yd))
        if os.path.isfile(os.path.join(tmp, "render.log")): shutil.copy2(os.path.join(tmp, "render.log"), os.path.join(logs, rid + ".render.log"))
        ok = os.path.isfile(os.path.join(tmp, "reels", rid + ".mp4")) or any(f.endswith((".mp4", ".m4a")) for f in ytf); shutil.rmtree(tmp, ignore_errors=True); n += ok
        print(f"{'OK  ' if ok else 'FAIL'} {rid}" + ("" if ok else f"  (see {logs}\\{rid}.render.log)"))
    for vid, yd in sorted(yt_dirs): yt_try_join(a.scripts, vid, yd)
    print(f"{n} parts/reels decrypted; reels -> {out}")

def yt_try_join(scripts, vid, yd):
    """join <yd>/<vid>-cNN.mp4 + <vid>.m4a -> youtube/<vid>.mp4 (+ sidecars) once every part is there"""
    pj = os.path.join(yd, vid + ".parts.json")
    if not os.path.isfile(pj): print(f"{vid}: waiting for the aud part (parts.json)"); return
    need = [f"{vid}-{c['part']}.mp4" for c in json.load(open(pj))["chunks"]] + [vid + ".m4a"]
    miss = [x for x in need if not os.path.isfile(os.path.join(yd, x))]
    if miss: print(f"{vid}: still missing {', '.join(miss)}"); return
    sys.path.insert(0, scripts); import yt_join
    final = os.path.join(os.path.dirname(yd), vid + ".mp4"); yt_join.join(yd, vid, final)
    for ext in (".srt", ".layout.json", ".chapters.txt", ".description.txt", ".thumbnail.png"):
        if os.path.isfile(os.path.join(yd, vid + ext)): shutil.copy2(os.path.join(yd, vid + ext), os.path.dirname(yd))
    print(f"{vid}: joined -> {final}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    segs = os.path.join(HERE, "scan", "agent", "segs")
    p = sp.add_parser("bundle"); p.add_argument("--segs", default=segs); p.add_argument("--reels"); p.add_argument("--scripts", default=HERE); p.set_defaults(f=cmd_bundle)
    p = sp.add_parser("ytbundle"); p.add_argument("--plan", required=True); p.add_argument("--vids", required=True); p.add_argument("--segs", default=segs)
    p.add_argument("--scripts", default=HERE); p.set_defaults(f=cmd_ytbundle)
    p = sp.add_parser("decrypt"); p.add_argument("--in", dest="inp", default=os.path.join(HERE, "scan", "agent", "cloud_out")); p.add_argument("--out")
    p.add_argument("--segs", default=segs); p.add_argument("--scripts", default=HERE); p.set_defaults(f=cmd_decrypt)
    p = sp.add_parser("check"); p.set_defaults(f=lambda a: (key(), print(f"RENDER_KEY set, cipher backend {backend()}")) and 0)
    p = sp.add_parser("enc"); p.add_argument("src"); p.add_argument("out"); p.set_defaults(f=lambda a: enc_file(a.src, a.out))
    p = sp.add_parser("dec"); p.add_argument("src"); p.add_argument("out"); p.set_defaults(f=lambda a: dec_file(a.src, a.out))
    a = ap.parse_args(argv); return a.f(a) or 0

if __name__ == "__main__": sys.exit(main())
