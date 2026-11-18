#!/usr/bin/env python3
"""Dry run of the fast pipeline against FAKE services (throwaway key, local fake Social Lamma, no GitHub):
  PC rf_pc.py bundle (+ source cache) -> cloud/job.sh per reel / YouTube part (real script; stub farm_pro / farm_yt4 that encode with the
  settings job.sh hands them) -> cloud/yt_join_job.sh (real yt_join.py) -> rf_pc.py decrypt -> checks.
  Run 1 with SOCIAL_LAMMA_TOKEN (hand-off), run 2 without (today's behaviour), run 3 re-bundle (cache: nothing new to upload).
usage: tests/speed_e2e.py <workdir> <dir with PC rf_pc.py + yt_join.py>"""
import hashlib, http.server, json, os, re, shutil, subprocess, sys, tarfile, threading, io

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); T = os.path.abspath(sys.argv[1]); PCSRC = os.path.abspath(sys.argv[2])
KEY = "test-only-" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]; TOKEN = "sl_FAKE_" + hashlib.sha256(os.urandom(8)).hexdigest()[:20]
ENV = dict(os.environ, RENDER_KEY=KEY, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost"); ENV.pop("SOCIAL_LAMMA_TOKEN", None)
FF = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
ok_all = True
def check(c, msg):
    global ok_all; ok_all &= bool(c); print(("PASS " if c else "FAIL ") + msg)

# ---------------- fake Social Lamma ----------------
STORE = {"media": {}, "parts": {}, "mcp_auth_bad": 0, "leak": 0}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers["Content-Length"]); j = json.loads(self.rfile.read(n))
        if self.headers.get("Authorization") != "Bearer " + TOKEN: STORE["mcp_auth_bad"] += 1; return self._send(401, {})
        name, a = j["params"]["name"], j["params"]["arguments"]
        if name == "get_media_upload_url":
            mid = "med_" + hashlib.sha1(os.urandom(8)).hexdigest()[:12]; STORE["media"][mid] = {"size": a["size_bytes"], "name": a["filename"], "done": False}
            ps = 256 * 1024; r = {"uploads": [{"media_id": mid, "upload": {"kind": "multipart", "partSize": ps, "partCount": -(-a["size_bytes"] // ps),
                                   "partUrlTemplate": f"http://127.0.0.1:{PORT}/part/{mid}/{{partNumber}}"}}]}
        elif name == "complete_media_upload":
            mid = a["media_id"]; data = b"".join(STORE["parts"][(mid, p["partNumber"])] for p in a["parts"])
            STORE["media"][mid].update(done=len(data) == STORE["media"][mid]["size"], sha=hashlib.sha256(data).hexdigest()); r = {"ok": True}
        else: return self._send(200, {"jsonrpc": "2.0", "id": j["id"], "error": {"message": "no"}})
        self._send(200, {"jsonrpc": "2.0", "id": j["id"], "result": {"structuredContent": r}})
    def do_PUT(self):
        if "Authorization" in self.headers: STORE["leak"] += 1
        _, _, mid, n = self.path.split("/"); data = self.rfile.read(int(self.headers["Content-Length"]))
        STORE["parts"][(mid, int(n))] = data; self._send(200, {"etag": f'"e{n}"'})
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------------- PC emulation ----------------
shutil.rmtree(T, ignore_errors=True); PC = os.path.join(T, "pc"); SEG = os.path.join(PC, "segs"); os.makedirs(os.path.join(SEG, "pieces")); os.makedirs(os.path.join(SEG, "assets", "memes"))
for f in ("rf_pc.py", "yt_join.py"): shutil.copy2(os.path.join(PCSRC, f), PC)
open(os.path.join(PC, "farm_pro.py"), "w").write('''
import os, sys, json, subprocess
SAFE = {"threads": 2}
def vargs(kind, enc, preview): return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"]
def main(args):
    plan = json.load(open(args[0])); out = args[args.index("--out") + 1]; rid = args[args.index("--only") + 1]
    os.makedirs(os.path.join(out, "reels"), exist_ok=True); r = plan["reels"][0]; src = r["src_file"]
    v = vargs("final", "x264", False); print("STEP final encode " + " ".join(v[:6]), flush=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-vf", "scale=1080:1920,setsar=1", "-t", "4"] + v + ["-c:a", "aac", os.path.join(out, "reels", rid + ".mp4")], check=True)
    open(os.path.join(out, "reels", rid + ".caption.txt"), "w").write("secret caption text")
    open(os.path.join(out, "reels", rid + ".srt"), "w").write("1\\n00:00:00,000 --> 00:00:02,000\\nsecret transcript words\\n")
    print("OK  " + rid); return 0
''')
open(os.path.join(PC, "farm_yt4.py"), "w").write('''
import os, sys, json, argparse, subprocess
ap = argparse.ArgumentParser(); ap.add_argument("--job"); ap.add_argument("--out"); a = ap.parse_args()
J = json.load(open(os.path.join(a.job, "job.json"))); vid = J["vid"]; os.makedirs(a.out, exist_ok=True); FF = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
src = os.path.join(a.job, "pieces", J["pieces"][0])
if J["part"] == "aud":
    subprocess.run(FF + ["-f", "lavfi", "-i", f"sine=f=330:d={J['frames'] / 30:.4f}", "-c:a", "aac", "-b:a", "256k", os.path.join(a.out, vid + ".m4a")], check=True)
    json.dump(dict(vid=vid, frames=J["frames"], chunks=J["chunks"], fps=30), open(os.path.join(a.out, vid + ".parts.json"), "w"))
    for e, t in ((".srt", "1\\n00:00:00,000 --> 00:00:01,000\\nsecret yt words\\n"), (".chapters.txt", "00:00 start\\n"), (".description.txt", "secret desc\\n")):
        open(os.path.join(a.out, vid + e), "w").write(t)
    subprocess.run(FF + ["-i", src, "-frames:v", "1", os.path.join(a.out, vid + ".thumbnail.png")], check=True)
else:
    c = next(c for c in J["chunks"] if c["part"] == J["part"]); n = c["f1"] - c["f0"]
    print("STEP chunk encode preset " + os.environ.get("YT_PRESET", "?") + " crf " + os.environ.get("YT_CRF", "?"), flush=True)
    subprocess.run(FF + ["-ss", f"{c['f0'] / 30:.4f}", "-i", src, "-an", "-vf", "fps=30", "-frames:v", str(n), "-c:v", "libx264", "-preset", os.environ["YT_PRESET"],
                         "-crf", os.environ["YT_CRF"], "-g", "60", "-r", "30", "-video_track_timescale", "15360", os.path.join(a.out, f"{vid}-{J['part']}.mp4")], check=True)
print("OK  " + J["part"])
''')
# source pieces: noisy 1080p so each is > 1 MB (goes through the cache)
for i, (name, secs) in enumerate((("s00001000_00009000.mp4", 8), ("s00020000_00028000.mp4", 8))):
    subprocess.run(FF + ["-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30,noise=alls=8:allf=t", "-f", "lavfi", "-i", f"sine=f={300 + 100 * i}",
                         "-t", str(secs), "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-shortest", os.path.join(SEG, "pieces", name)], check=True)
P1, P2 = sorted(os.listdir(os.path.join(SEG, "pieces")))
ps = {"fill": 24, "source": "stream.mp4", "reels": {}}
for rid, pc in (("r01", P1), ("r02", P2)):
    ps["reels"][rid] = {"units": [{"file": f"u_{rid}.mp4", "pieces": [{"name": pc, "a": 0, "b": 8, "off": 0}]}], "assets": {"memes": [], "clips": []},
                        "plan": {"reels": [{"id": rid, "src_file": f"u_{rid}.mp4"}]}}
json.dump(ps, open(os.path.join(SEG, "plan_segs.json"), "w"))

def pc(*a, env=None):
    r = subprocess.run([sys.executable, os.path.join(PC, "rf_pc.py"), *a], capture_output=True, text=True, env=dict(ENV, RF_BACKEND="py", **(env or {})))
    if r.returncode: print(r.stdout, r.stderr); raise SystemExit("rf_pc failed")
    return r.stdout

def yt_bundles():
    """YouTube parts built with rf_pc's own staging + cache + cipher (what ytbundle does after planning)"""
    sys.path.insert(0, PC); os.environ["RENDER_KEY"] = KEY; os.environ["RF_BACKEND"] = "py"; import rf_pc
    chunks = [{"part": "c01", "f0": 0, "f1": 90}, {"part": "c02", "f0": 90, "f1": 180}]; ids = []
    hc = rf_pc.HashCache(os.path.join(SEG, "cache", "hashes.json")); st = os.path.join(T, "_st")
    for part in ("aud", "c01", "c02"):
        pid = "y1-" + part; b = os.path.join(st, pid, "bundle"); os.makedirs(os.path.join(b, "pieces"))
        shutil.copy2(os.path.join(SEG, "pieces", P1), os.path.join(b, "pieces", P1))
        json.dump(dict(vid="y1", part=part, pieces=[P1], chunks=chunks, frames=180, need_demucs=False), open(os.path.join(b, "job.json"), "w"))
        rf_pc.cache_media(b, os.path.join(SEG, "cache"), pid, hc); rf_pc.encrypt_tree(os.path.join(st, pid), "bundle", os.path.join(SEG, pid + ".tar.enc")); ids.append(pid)
    shutil.rmtree(st); return ids

out1 = pc("bundle", "--segs", SEG, "--scripts", PC); YT = yt_bundles()
print(out1.strip())
sizes = {f: os.path.getsize(os.path.join(SEG, f)) for f in os.listdir(SEG) if f.endswith(".tar.enc")}
cache = sorted(f for f in os.listdir(os.path.join(SEG, "cache")) if f.startswith("f-"))
check(len(cache) == 2, f"source cache: 2 unique assets for 5 bundles (pieces shared by r01 + 3 YouTube parts): {len(cache)}")
check(all(sizes[f] < 200_000 for f in sizes if f != "code.tar.enc"), "plan bundles are small without media: " + ", ".join(f"{k} {v // 1024} KB" for k, v in sorted(sizes.items())))
for f in [os.path.join(SEG, x) for x in sizes] + [os.path.join(SEG, "cache", x) for x in cache]:
    blob = open(f, "rb").read(); check(blob[:8] == b"Salted__" and b"ftyp" not in blob and b"job.json" not in blob, f"encrypted, no plaintext: {os.path.basename(f)}")
idx = open(os.path.join(SEG, "cache", "index", "y1-c01.txt")).read().split()
check(idx and all(re.fullmatch(r"f-[0-9a-f]{40}\.enc", a) for a in idx), "index lists opaque asset names only")
# re-bundle: same footage -> no new encryption, same names
out2 = pc("bundle", "--segs", SEG, "--scripts", PC)
check("0 MB new" in out2 and sorted(f for f in os.listdir(os.path.join(SEG, "cache")) if f.startswith("f-")) == cache, "re-bundle of the same stream: 0 MB new in the cache, same asset names")

# ---------------- cloud ----------------
def cloud(rid, token, outdir):
    env = dict(ENV, MEDIA_SRC=os.path.join(SEG, "cache"), SOCIAL_LAMMA_BASE=f"http://127.0.0.1:{PORT}")
    if token and not re.search(r"-(aud|c[0-9]+)$", rid): env["SOCIAL_LAMMA_TOKEN"] = TOKEN       # as render.yml: parts never hand off
    r = subprocess.run(["bash", os.path.join(R, "cloud", "job.sh"), rid, SEG, outdir], capture_output=True, text=True, env=env, cwd=T)
    return r.returncode, r.stdout + r.stderr

def join(token, outdir, parts_dir):
    env = dict(ENV, SOCIAL_LAMMA_BASE=f"http://127.0.0.1:{PORT}"); env.pop("SOCIAL_LAMMA_TOKEN", None)
    if token: env["SOCIAL_LAMMA_TOKEN"] = TOKEN
    r = subprocess.run(["bash", os.path.join(R, "cloud", "yt_join_job.sh"), "y1", parts_dir, os.path.join(SEG, "code.tar.enc"), outdir], capture_output=True, text=True, env=env, cwd=T)
    return r.returncode, r.stdout + r.stderr

def members(enc):
    d = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-pass", "env:RENDER_KEY", "-in", enc], capture_output=True, env=ENV).stdout
    return {"./" + m.name.lstrip("./") if not m.name.startswith("./") else m.name: m.size for m in tarfile.open(fileobj=io.BytesIO(d))}

def public_ok(log):
    bad = [x for x in (TOKEN, KEY, "secret", "med_", "127.0.0.1") if x in log]
    return not bad, bad

for mode in ("handoff", "no_token"):
    tok = mode == "handoff"; O = os.path.join(T, "out_" + mode); P = os.path.join(T, "parts_" + mode); J = os.path.join(T, "join_" + mode); os.makedirs(P)
    logs = ""
    for rid in ["r01", "r02"] + YT:
        rc, log = cloud(rid, tok, O); logs += log; check(rc == 0, f"[{mode}] job.sh {rid} rc={rc}" + ("" if rc == 0 else "\n" + log[-1500:]))
    for f in os.listdir(O):
        if re.match(r"y1-.*\.out\.tar\.enc$", f): shutil.copy2(os.path.join(O, f), P)       # = download-artifact render-y1-*
    rc, log = join(tok, J, P); logs += log; check(rc == 0, f"[{mode}] yt_join_job.sh rc={rc}" + ("" if rc == 0 else "\n" + log[-1500:]))
    ok, bad = public_ok(logs); check(ok, f"[{mode}] public log has no token/key/media id/caption text {bad}")
    m = members(os.path.join(O, "r01.out.tar.enc"))
    check("STEP final encode -c:v libx264 -preset slow -crf 22" in logs and "STEP chunk encode preset slow crf 22" in logs, f"[{mode}] renderers got x264 slow CRF 22")
    if tok:
        check("./reels/r01.mp4" not in m and "./reels/r01.handoff.json" in m and "./reels/r01.preview.mp4" in m and "./reels/r01.qa.jpg" in m and "./reels/r01.thumb.jpg" in m,
              "[handoff] r01 small package: handoff.json + qa.jpg + thumb.jpg + preview.mp4, no mp4")
        check(os.path.isfile(os.path.join(O, "r01.full.tar.enc")) and "./reels/r01.mp4" in members(os.path.join(O, "r01.full.tar.enc")), "[handoff] r01 full video in r01.full.tar.enc")
        jm = members(os.path.join(J, "y1.out.tar.enc"))
        check("./yt/y1.mp4" not in jm and all(f"./yt/y1{e}" in jm for e in (".handoff.json", ".srt", ".chapters.txt", ".thumbnail.png", ".qa.json", ".qa.jpg", ".preview.mp4")),
              "[handoff] y1 joined in the cloud: small package with sidecars + handoff, no video")
        check("./yt/y1.mp4" in members(os.path.join(J, "y1.full.tar.enc")), "[handoff] y1 full joined video in y1.full.tar.enc")
        done = [v for v in STORE["media"].values() if v["done"]]
        check(len(done) == 3, f"[handoff] fake Social Lamma got 3 complete uploads (r01, r02, y1): {len(done)}; auth refused {STORE['mcp_auth_bad']}, token sent to part URLs {STORE['leak']}")
        check(STORE["leak"] == 0, "[handoff] token never sent to the upload URLs")
        small = sum(os.path.getsize(os.path.join(O, f)) for f in ("r01.out.tar.enc", "r02.out.tar.enc")) + os.path.getsize(os.path.join(J, "y1.out.tar.enc"))
        full = sum(os.path.getsize(os.path.join(d, f)) for d in (O, J) for f in os.listdir(d) if f.endswith(".full.tar.enc"))
        print(f"INFO PC download in hand-off mode: {small / 2**20:.2f} MB (instead of {(small + full) / 2**20:.2f} MB with the videos)")
    else:
        check("./reels/r01.mp4" in m and not any(k.endswith(".handoff.json") for k in m) and not os.path.exists(os.path.join(O, "r01.full.tar.enc")),
              "[no_token] r01 package exactly as today: mp4 inside, no hand-off, no full file")
        check("JOIN SKIP y1 reason=no_token" in log and not os.path.exists(os.path.join(J, "y1.out.tar.enc")), "[no_token] join job does nothing (the PC joins the parts as before)")
    # encode settings handed to the renderers
    if tok:
        jl = [l for l in logs.splitlines() if l.startswith(("HANDOFF", "PREVIEWS", "JOIN", "MEDIA", "QA "))]
        print("INFO public lines: " + " | ".join(jl))

    # ---------------- PC side: decrypt what fetch would download ----------------
    CO = os.path.join(T, "cloud_out_" + mode); os.makedirs(CO)
    for f in os.listdir(O):
        if f.endswith(".out.tar.enc") and not (tok and f.startswith("y1-")): shutil.copy2(os.path.join(O, f), CO)   # joined -> parts skipped
    if os.path.isdir(J):
        for f in os.listdir(J):
            if f.endswith(".out.tar.enc"): shutil.copy2(os.path.join(J, f), CO)
    FIN = os.path.join(T, "final_" + mode, "reels"); YTO = os.path.join(T, "final_" + mode, "youtube")
    print(pc("decrypt", "--in", CO, "--out", FIN, "--yt-out", YTO, "--segs", SEG, "--scripts", PC).strip())
    if tok:
        check(not os.path.exists(os.path.join(FIN, "r01.mp4")) and json.load(open(os.path.join(FIN, "r01.handoff.json")))["media_id"] in STORE["media"]
              and os.path.isfile(os.path.join(FIN, "r01.qa.jpg")), "[handoff] PC: r01 media id + contact sheet, no video downloaded")
        h = json.load(open(os.path.join(YTO, "y1.handoff.json")))
        check(h["media_id"] in STORE["media"] and STORE["media"][h["media_id"]]["sha"] == h["sha256"] and h["secs"] > 5.9,
              "[handoff] PC: youtube/y1.handoff.json matches the bytes Social Lamma received")
        # --full: the videos on request
        for d in (O, J):
            for f in os.listdir(d):
                if f.endswith(".full.tar.enc"): shutil.copy2(os.path.join(d, f), CO)
        pc("decrypt", "--in", CO, "--out", FIN, "--yt-out", YTO, "--segs", SEG, "--scripts", PC)
        n = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                                os.path.join(YTO, "y1.mp4")], capture_output=True, text=True).stdout or 0)
        check(os.path.isfile(os.path.join(FIN, "r01.mp4")) and n == 180, f"[handoff] --full brings r01.mp4 + joined y1.mp4 ({n} frames of 180)")
        sha = hashlib.sha256(open(os.path.join(FIN, "r01.mp4"), "rb").read()).hexdigest()
        check(sha == json.load(open(os.path.join(FIN, "r01.handoff.json")))["sha256"], "[handoff] the full r01.mp4 is byte-identical to the uploaded one")
    else:
        n = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                                os.path.join(YTO, "y1.mp4")], capture_output=True, text=True).stdout or 0)
        check(os.path.isfile(os.path.join(FIN, "r01.mp4")) and n == 180, f"[no_token] PC: r01.mp4 + y1 joined on the PC from parts ({n} frames)")

srv.shutdown()
print("SPEED E2E " + ("PASSED" if ok_all else "FAILED")); sys.exit(0 if ok_all else 1)
