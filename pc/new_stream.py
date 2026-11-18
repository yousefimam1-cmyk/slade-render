#!/usr/bin/env python3
"""new_stream.py - one button "New stream -> content". Stdlib only. Run by NEW_STREAM.bat (Hub starts it server-side: it survives a closed window).

  new_stream.py <stream.mp4> [--game tft] [--scribe] [--reels 6] [--no-youtube] [--review-plan] [--approve-plan]
                [--from STEP] [--force STEP[,STEP]] [--status] [--no-post] [--full]

Chain (every step resumable; a step is skipped when its result already exists on disk, the disk is the truth):
  1 audio      EXTRACT_AUDIO.bat                         agent\\<stem>.a0.wav / a1.wav / proxy.mp4
  2 transcribe TRANSCRIBE.bat (local whisper, default)    or SCRIBE.bat (ElevenLabs credits: only with --scribe)
  3 plan       CLIP_FARMER.bat -> DIRECTOR.bat (local Ollama) -> ns_plan.py draft that follows knowledge\\SLADE_EDIT_RULES.md
               + one YouTube episode per game (ns_plan.build_youtube: talk_guard pacing, 70B titles/chapters; --no-youtube = reels only)
  4 cut        prep_segments.py + CUT_SEGMENTS.bat      stream-copy cuts of only the kept footage (YouTube: segs_yt\\CUT_SEGMENTS.bat, CUT_YT form)
  5 encrypt    rf_pc.py bundle (+ ytbundle)             RENDER_KEY, AES-256, nothing readable leaves the PC; reels + YouTube parts in ONE cloud run
  6 upload     rf_cloud_pc.py: release + assets + workflow dispatch (GH_RENDER_TOKEN)
  7 poll       rf_cloud_pc.py: wait for the Actions run
  8 fetch      rf_cloud_pc.py: artifacts, then rf_pc.py decrypt -> Videos\\Slade Clips\\<game> <date>\\ (YouTube: "<game> EPn.mp4" + description/chapters)
               hand-off mode (repo secret SOCIAL_LAMMA_TOKEN): QA-PASS videos went straight from GitHub to Social Lamma, so only small files come
               down (<id>.handoff.json = media id, qa.jpg, thumb.jpg, preview.mp4, srt, qa.json); --full also downloads the videos
  9 qa         QA_SHEET.bat per video (+ a small thumbnail)
 10 summary    summary.json in the output folder (what Hub's Review panel reads)
 11 post       social_post.py: per-platform captions (AI + Slade's rules + feedback, linted), upload, auto_slot schedule on Social Lamma
               ON only when SOCIAL_LAMMA_TOKEN is set (SET_SOCIAL_TOKEN.bat); --no-post skips it

State: scan\\<stem>.ns\\state.json. Prints "STEP i/11 label" lines (Slade Hub draws its progress bar from them). Exit codes: 0 ok, 1 failed, 10 paused (plan review).
"""
import os, re, sys, json, time, shlex, shutil, argparse, datetime as dt, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
NT = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
GAME_NAMES = {"tft": "TFT", "league": "League", "eafc": "EAFC", "fc26": "EAFC", "overcooked": "Overcooked", "daysgone": "Days Gone"}
ORDER = ["audio", "transcribe", "plan", "cut", "encrypt", "upload", "poll", "fetch", "qa", "summary", "post"]
LABELS = {"audio": "Extract audio", "transcribe": "Transcribe", "plan": "Find clips and draft the plan", "cut": "Cut segments", "encrypt": "Encrypt",
          "upload": "Upload and start the cloud render", "poll": "Cloud render", "fetch": "Download and decrypt", "qa": "QA contact sheets", "summary": "Write summary",
          "post": "Post to Social Lamma"}
OWNS = {"upload": ("tag", "run_id", "run_url"), "poll": ("conclusion", "jobs"), "fetch": ("missing", "fetched", "yt_missing", "handed_off")}   # data keys a step produces; cleared when an earlier step re-runs
CONTENT = ("plan", "cut", "encrypt")        # steps whose real re-run makes an earlier cloud run useless
EXIT_OK, EXIT_FAIL, EXIT_PAUSED = 0, 1, 10


class StepFailed(Exception): pass
class Paused(Exception): pass


def now_iso(): return dt.datetime.now().isoformat(timespec="seconds")
def safe(s): return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s)).strip("-")[:60] or "x"
def q(p): return '"' + str(p).replace('"', "") + '"'


# ---------------- state ----------------
class State:
    """scan\\<stem>.ns\\state.json (atomic writes). {"steps": {name: {status, attempts, started, finished, error, info}}, "data": {...}}"""
    def __init__(self, path, init=None):
        self.path = path; self.d = None
        try:
            with open(path, encoding="utf-8") as f: self.d = json.load(f)
        except Exception: self.d = None
        if not isinstance(self.d, dict) or "steps" not in self.d:
            self.d = {"v": 1, "created": now_iso(), "steps": {}, "data": {}}
        for k, v in (init or {}).items(): self.d.setdefault(k, v)
        for n in ORDER: self.d["steps"].setdefault(n, {"status": "pending", "attempts": 0})
        self.d.setdefault("data", {})

    def save(self):
        self.d["updated"] = now_iso(); os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(self.d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def step(self, n): return self.d["steps"][n]
    def status(self, n): return self.d["steps"][n]["status"]
    @property
    def data(self): return self.d["data"]

    def set(self, n, status, **kw):
        s = self.d["steps"][n]; s["status"] = status; s.update(kw); self.save()

    def recover(self):
        """a previous process died (window kill, Stop, power cut) while a step was running: run it again"""
        for n in ORDER:
            if self.status(n) == "running": self.d["steps"][n]["status"] = "pending"
        self.save()


# ---------------- engine (pure; the part the unit tests cover) ----------------
class Step:
    def __init__(self, name, run, done=None, label=None):
        self.name, self.run, self.done, self.label = name, run, done, label or LABELS.get(name, name)


def run_chain(steps, st, ctx, force=(), start=None, out=print):
    """Runs the steps in order. -> EXIT_OK | EXIT_FAIL | EXIT_PAUSED.
    - a step is skipped when it is marked done AND its disk check (step.done) still passes; or when the disk check passes by itself
    - when a step really runs, every later step goes back to pending (re-checks its disk result); after plan/cut/encrypt also its run id/tag are cleared
    - a crashed 'running' step is retried; a failed step stops the chain and is retried on the next run"""
    st.recover(); names = [s.name for s in steps]; n = len(steps); force = set(force or ())
    begin = names.index(start) if start in names else 0
    for i, s in enumerate(steps):
        if i < begin: continue
        out(f"STEP {i + 1}/{n} {s.label}")
        forced = s.name in force
        if not forced:
            ok_disk = s.done(ctx) if s.done else None
            if (st.status(s.name) == "done" and ok_disk is not False) or ok_disk:
                if st.status(s.name) != "done": st.set(s.name, "done", finished=now_iso(), info={"skipped": "already on disk"})
                out(f"  skip: {s.label} already done"); continue
        for later in names[i + 1:]:                    # a real run: later steps re-check their disk result; when the CONTENT changed (plan/cut/encrypt) their run ids are dropped too
            if st.status(later) != "pending": st.d["steps"][later]["status"] = "pending"
            if s.name in CONTENT: [st.data.pop(k, None) for k in OWNS.get(later, ())]
        for k in OWNS.get(s.name, ()):
            if forced: st.data.pop(k, None)
        t0 = time.time(); at = st.step(s.name).get("attempts", 0) + 1
        st.set(s.name, "running", started=now_iso(), attempts=at, error=None)
        try:
            info = s.run(ctx) or {}
        except Paused as e:
            st.set(s.name, "paused", error=str(e)); out("PAUSED " + str(e)); return EXIT_PAUSED
        except StepFailed as e:
            st.set(s.name, "failed", error=str(e), finished=now_iso(), secs=round(time.time() - t0, 1)); out(f"FAILED {s.label}: {e}"); return EXIT_FAIL
        except Exception as e:                           # a bug or an odd file: still leave a readable state, never a traceback-only exit
            st.set(s.name, "failed", error=f"{type(e).__name__}: {e}"[:300], finished=now_iso(), secs=round(time.time() - t0, 1)); out(f"FAILED {s.label}: {type(e).__name__}: {e}"); return EXIT_FAIL
        st.set(s.name, "done", finished=now_iso(), info=info, secs=round(time.time() - t0, 1), error=None)
        out(f"  done: {s.label} ({time.time() - t0:.0f}s)")
    out("DONE all steps")
    return EXIT_OK


# ---------------- context (paths, tools, GitHub) ----------------
class Ctx:
    def __init__(self, video, scripts=HERE, vid=None, game="tft", scribe=False, reels=0, review_plan=False, log=print, gh_factory=None, runner=None, probe=None, youtube=True, full=False):
        self.video = os.path.abspath(video); self.stem = os.path.splitext(os.path.basename(video))[0]
        self.scripts = scripts; self.scan = os.path.join(scripts, "scan"); self.agent = os.path.join(self.scan, "agent")
        self.vid = vid or os.environ.get("SLADE_VIDEOS") or os.path.join(os.path.expanduser("~"), "Videos")
        self.game = (game or "tft").lower(); self.scribe = bool(scribe); self.n_reels = int(reels); self.review_plan = bool(review_plan)
        self.run_dir = os.path.join(self.scan, self.stem + ".ns"); self.farm = os.path.join(self.scan, self.stem + ".farm")
        self.youtube = bool(youtube); self.segs_yt = os.path.join(self.run_dir, "segs_yt"); self.yt_dir = os.path.join(self.run_dir, "yt")
        self.segs = os.path.join(self.run_dir, "segs"); self.cloud_out = os.path.join(self.run_dir, "cloud_out"); self.plan_path = os.path.join(self.run_dir, "plan.json")
        self.log = log; self._gh = gh_factory; self.runner = runner or self._run; self.probe = probe or self._probe
        self.st = None; self.out_dir = None; self.full = bool(full)

    # --- paths / names ---
    def date(self):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", self.stem)
        if m: return m.group(1)
        try: return dt.date.fromtimestamp(os.path.getmtime(self.video)).isoformat()
        except OSError: return dt.date.today().isoformat()

    def pick_out_dir(self):
        base = os.path.join(self.vid, "Slade Clips"); name = f"{GAME_NAMES.get(self.game, self.game)} {self.date()}"
        for suffix in ("", " 2", " 3", " 4", " 5"):
            d = os.path.join(base, name + suffix); mk = os.path.join(d, ".ns_stream")
            if not os.path.isdir(d): return d
            try:
                with open(mk, encoding="utf-8") as f:
                    if f.read().strip() == self.stem: return d
            except OSError: pass
            if not os.listdir(d): return d
        return os.path.join(base, name + " " + dt.datetime.now().strftime("%H%M"))

    def ids(self):
        p = load_json(self.plan_path) or {}
        return [r["id"] for r in p.get("reels", [])]

    def yts(self):
        """YouTube episodes of the plan: [{id, out_name, title, ...}]"""
        return list((load_json(self.plan_path) or {}).get("youtube") or [])

    def yids(self): return [y["id"] for y in self.yts()]

    def parts(self):
        """YouTube render part ids (y1-aud, y1-c01 ...) written by rf_pc.py ytbundle"""
        try:
            with open(os.path.join(self.segs_yt, "yt_parts.txt"), encoding="utf-8") as f: return [x.strip() for x in f.read().split(",") if x.strip()]
        except OSError: return []

    def cloud_ids(self): return self.ids() + (self.parts() if self.yids() else [])

    # --- running things ---
    def env(self, extra=None):
        e = dict(os.environ); e.update({"SLADE_HUB": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "CF_GAME": self.game})
        e.update(extra or {}); return e

    def run_tool(self, tool, args="", env=None, cwd=None):
        """one of the Scripts\\*.bat tools, same way Hub runs them. -> exit code"""
        path = tool if os.path.isabs(tool) else os.path.join(self.scripts, tool)
        if not os.path.isfile(path): raise StepFailed("missing " + path)
        cmd = f'cmd /c ""{path}" {args}"' if NT else ["sh", path] + shlex.split(args)
        return self.runner(cmd, env=self.env(env), cwd=cwd or self.scripts, label=os.path.basename(tool))

    def run_cmd(self, argv, env=None, cwd=None):
        return self.runner(argv, env=self.env(env), cwd=cwd or self.scripts, label=os.path.basename(argv[0]))

    def _run(self, cmd, env=None, cwd=None, label=""):
        kw = {"creationflags": NOWIN | BELOW} if NT else {}
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        last = ""; lastt = 0.0
        for raw in iter(p.stdout.readline, b""):
            for part in [x for x in raw.decode("utf-8", "replace").split("\r") if x.strip()][-1:]:
                line = part.rstrip("\n").strip(); key = re.sub(r"[\d.]+\s*(%|[KMG]B|MB/s|s\b)", "#", line)
                if key == last and time.time() - lastt < 1.5: continue
                last, lastt = key, time.time()
                if "RENDER_KEY" in line or "GH_RENDER_TOKEN" in line: line = re.sub(r"(=|: )\S{12,}", r"\1***", line)
                line = line.replace("STEP ", "sub-step ").replace("%", " pct")      # Hub's progress bar follows only this script's own "STEP i/10" lines
                self.log("    " + line[:400])
        return p.wait()

    @staticmethod
    def _probe(path):
        try:
            o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True, creationflags=NOWIN, timeout=60).stdout
            return float(o.strip())
        except Exception: return 0.0

    def gh(self):
        if self._gh: return self._gh()
        import rf_cloud_pc
        return rf_cloud_pc.GH(log=self.log)


def load_json(p, d=None):
    try:
        with open(p, encoding="utf-8-sig") as f: return json.load(f)
    except Exception: return d


def exists(p, min_bytes=1):
    try: return os.path.getsize(p) >= min_bytes
    except OSError: return False


def mt(p):
    try: return os.path.getmtime(p)
    except OSError: return 0.0


# ---------------- steps ----------------
def transcript_files(c): return [os.path.join(c.scan, c.stem + s) for s in (".ens.json", ".wav.json", ".json")]

def audio_done(c): return exists(os.path.join(c.agent, c.stem + ".a0.wav"), 1000) and exists(os.path.join(c.agent, c.stem + ".proxy.mp4"), 1000)

def s_audio(c):
    rc = c.run_tool("EXTRACT_AUDIO.bat", q(c.video))
    if rc != 0 or not audio_done(c): raise StepFailed(f"EXTRACT_AUDIO.bat failed (code {rc})")


def transcribe_done(c):
    if c.scribe: return exists(os.path.join(c.agent, c.stem + ".scribe.json"), 100) and any(exists(p, 100) for p in transcript_files(c))
    return any(exists(p, 100) for p in transcript_files(c))

def s_transcribe(c):
    if c.scribe:
        scribe = os.path.join(c.agent, c.stem + ".scribe.json")
        if not exists(scribe, 100):
            c.log("  Scribe is ON: this spends ElevenLabs credits (once per stream)")
            rc = c.run_tool("SCRIBE.bat", q(c.stem))
            if rc != 0 or not exists(scribe, 100): raise StepFailed(f"SCRIBE.bat failed (code {rc}); ELEVENLABS_API_KEY set? Untick Scribe to use the free local transcript")
        if not any(exists(p, 100) for p in transcript_files(c)):          # the rest of the pipeline reads whisper-style json
            import ns_plan
            ens = ns_plan.scribe_to_ens(load_json(scribe, {}))
            with open(transcript_files(c)[0], "w", encoding="utf-8") as f: json.dump(ens, f, ensure_ascii=False)
        return {"engine": "elevenlabs-scribe"}
    rc = c.run_tool("TRANSCRIBE.bat", q(c.video) + " --game " + c.game)
    if rc != 0 or not any(exists(p, 100) for p in transcript_files(c)): raise StepFailed(f"TRANSCRIBE.bat failed (code {rc})")
    return {"engine": "local-whisper"}


def plan_ready(c):
    if not exists(c.plan_path, 10): return False
    return (not c.review_plan) or os.path.isfile(os.path.join(c.run_dir, "plan.approved"))

def s_plan(c):
    import ns_plan
    ns_plan.rules_info(c.scripts)                                    # no rules file, no plan
    ranked = os.path.join(c.farm, "ranked.json")
    if not exists(ranked, 3):
        rc = c.run_tool("CLIP_FARMER.bat", q(c.video) + f" --top {max(12, (c.n_reels or 0) * 2)} --game {c.game}")
        if rc != 0 or not exists(ranked, 3): raise StepFailed(f"CLIP_FARMER.bat failed (code {rc})")
    if not exists(c.plan_path, 10):                                   # never overwrite a plan that someone edited
        picks = ns_plan.pick_clips(load_json(ranked, []), c.n_reels)
        have = [f for f in os.listdir(os.path.join(c.farm, "edits")) if f.endswith(".plan.json")] if os.path.isdir(os.path.join(c.farm, "edits")) else []
        if picks and not have:
            ranks = ",".join(str(int(x["rank"])) for x in picks)
            rc = c.run_tool("DIRECTOR.bat", f"--farm {q(c.farm)} --video {q(c.video)} --ranks {ranks}")
            if rc != 0: c.log(f"  DIRECTOR.bat code {rc}: continuing with a plan without AI edit beats")
        plan, rep = ns_plan.build_plan(c.video, c.scripts, scan=c.scan, game=c.game, n=c.n_reels, youtube=c.youtube)
        if not plan["reels"] and not plan["youtube"]: raise Paused("no reel-worthy clips in this stream (AI rating >= 7 and virality >= 60) and no game with 3+ min of talk: nothing made")
        os.makedirs(c.run_dir, exist_ok=True)
        with open(c.plan_path, "w", encoding="utf-8") as f: json.dump(plan, f, ensure_ascii=False, indent=1)
        with open(os.path.join(c.run_dir, "review.json"), "w", encoding="utf-8") as f: json.dump(rep, f, ensure_ascii=False, indent=1)
        c.log(f"  plan: {len(plan['reels'])} reels, {len(plan['youtube'])} YouTube episodes, rules {plan['rules']['file']} {plan['rules']['sha256']}")
    if c.review_plan and not os.path.isfile(os.path.join(c.run_dir, "plan.approved")):
        raise Paused(f"plan ready for review: {c.plan_path} . Edit it if needed, then press Continue (NEW_STREAM.bat ... --approve-plan)")
    return {"reels": len(c.ids()), "youtube": len(c.yids())}


def cut_marker(c): return os.path.join(c.segs, "cut.ok")
def cut_done(c): return exists(cut_marker(c), 0) and exists(os.path.join(c.segs, "plan_segs.json")) and mt(cut_marker(c)) >= mt(c.plan_path)

def yt_cut_bat(c):
    """YouTube pieces (plan youtube[].pieces, merged spans of the kept segments) + their meme assets -> segs_yt\\CUT_SEGMENTS.bat (CUT_YT cut form)"""
    import prep_segments
    cuts, reels = {}, {}
    for y in c.yts():
        for a, b in y.get("pieces") or [[s[0], s[1]] for s in y["segments"]]:
            cuts[f"s{int(round(a * 1000)):08d}_{int(round(b * 1000)):08d}"] = (float(a), float(b))
        reels[y["id"]] = {"assets": prep_segments.needed_assets(y)}
    os.makedirs(c.segs_yt, exist_ok=True)
    ps = {"cuts": dict(sorted(cuts.items(), key=lambda kv: kv[1])), "reels": reels}
    with open(os.path.join(c.segs_yt, "yt_cuts.json"), "w", encoding="utf-8") as f: json.dump(ps, f, ensure_ascii=False, indent=1)
    prep_segments.write_bat(ps, os.path.join(c.segs_yt, "CUT_SEGMENTS.bat"), c.video, extra="-avoid_negative_ts make_zero")
    return len(cuts)

def s_cut(c):
    import prep_segments
    ids = c.ids(); yids = c.yids()
    if not ids and not yids: raise StepFailed("plan has no reels and no YouTube episodes")
    sc = c.scripts if c.scripts.endswith(("\\", "/")) else c.scripts + os.sep
    if ids:
        os.makedirs(c.segs, exist_ok=True)
        if prep_segments.main(["cut", c.plan_path, "--source", c.video, "--reels", ",".join(ids), "--out", c.segs]) not in (0, None): raise StepFailed("prep_segments failed")
        rc = c.run_tool(os.path.join(c.segs, "CUT_SEGMENTS.bat"), "", env={"SRC": c.video, "SEGS_OUT": c.segs, "SCRIPTS": sc})
        if rc != 0: raise StepFailed(f"CUT_SEGMENTS.bat failed (code {rc})")
    yp = 0
    if yids:
        yp = yt_cut_bat(c)
        rc = c.run_tool(os.path.join(c.segs_yt, "CUT_SEGMENTS.bat"), "", env={"SRC": c.video, "SEGS_OUT": c.segs_yt, "SCRIPTS": sc})
        if rc != 0: raise StepFailed(f"YouTube CUT_SEGMENTS.bat failed (code {rc})")
    os.makedirs(c.segs, exist_ok=True)
    with open(cut_marker(c), "w") as f: f.write(now_iso())
    return {"pieces": len(os.listdir(os.path.join(c.segs, "pieces"))) if os.path.isdir(os.path.join(c.segs, "pieces")) else 0, "yt_pieces": yp}


def uv(c, script, *args):
    return ["uv", "run", "--python-preference", "only-system", "--python", "3.12", "--with", "cryptography", os.path.join(c.scripts, script), *args]

def encrypt_done(c):
    ids = c.ids(); yids = c.yids()
    if (not ids and not yids) or not cut_done(c): return False
    if yids and not c.parts(): return False
    fs = [os.path.join(c.segs, i + ".tar.enc") for i in c.cloud_ids()] + [os.path.join(c.segs, "code.tar.enc")]
    return all(exists(f, 100) and mt(f) >= mt(cut_marker(c)) for f in fs)

YT_DEPS = ["--with", "numpy", "--with", "pillow", "--with", "arabic-reshaper", "--with", "python-bidi", "--with", "fonttools"]

def s_encrypt(c):
    if c.run_cmd(uv(c, "rf_pc.py", "check")) != 0: raise StepFailed("RENDER_KEY is not set on this PC: run SET_RENDER_KEY.bat once (same value as the GitHub secret)")
    if c.ids():
        rc = c.run_cmd(uv(c, "rf_pc.py", "bundle", "--segs", c.segs, "--reels", ",".join(c.ids()), "--scripts", c.scripts))
        if rc != 0: raise StepFailed(f"rf_pc.py bundle failed (code {rc})")
    if c.yids():                                    # same YouTube path as YT_CLOUD_PREP.bat; the parts go to the same release + run as the reels
        try: os.remove(os.path.join(c.segs_yt, "yt_parts.txt"))
        except OSError: pass
        a = uv(c, "rf_pc.py", "ytbundle", "--plan", c.plan_path, "--vids", ",".join(c.yids()), "--segs", c.segs_yt, "--scripts", c.scripts,
               "--cache", os.path.join(c.segs, "cache"))
        a[a.index("cryptography") + 1:a.index("cryptography") + 1] = YT_DEPS
        rc = c.run_cmd(a)
        if rc != 0 or not c.parts(): raise StepFailed(f"rf_pc.py ytbundle failed (code {rc})")
        os.makedirs(c.segs, exist_ok=True)
        for n in c.parts() + ([] if c.ids() else ["code"]):
            src, dst = os.path.join(c.segs_yt, n + ".tar.enc"), os.path.join(c.segs, n + ".tar.enc")
            if not exists(src, 100): raise StepFailed(f"ytbundle made no {n}.tar.enc")
            try: os.remove(dst)
            except OSError: pass
            try: os.link(src, dst)
            except OSError: shutil.copy2(src, dst)
    if not encrypt_done(c): raise StepFailed("encrypted bundles missing after rf_pc.py")


def upload_done(c): return bool(c.st.data.get("run_id"))

def s_upload(c):
    gh = c.gh(); ids = c.cloud_ids(); d = c.st.data
    try:
        gh.check()
        tag = d.get("tag") or "ns-" + safe(c.stem)[:40] + "-" + dt.datetime.now().strftime("%m%d%H%M")
        d["tag"] = tag; c.st.save()
        r = gh.upload(c.segs, tag, progress=lambda n, pc: c.log(f"    upload {n}: {pc} pct") if pc % 25 == 0 else None)
        c.log(f"  uploaded {len(r['uploaded'])}, already there {len(r['skipped'])}"
              + (f"; source cache: {len(r.get('cache_uploaded') or [])} new, {len(r.get('cache_skipped') or [])} already on GitHub" if "cache_uploaded" in r else ""))
        r = gh.dispatch(tag, ids)
    except Exception as e:
        if type(e).__name__ == "GHError": raise StepFailed(str(e))
        raise
    d["run_id"] = r["run_id"]; d["run_url"] = r["url"]; c.st.save()
    c.log(f"  render started: {r['url']}")
    return {"run_id": r["run_id"], "tag": tag}


def poll_done(c): return bool(c.st.data.get("conclusion"))

def s_poll(c, timeout=3 * 3600, interval=30):
    gh = c.gh(); d = c.st.data
    if not d.get("run_id"): raise StepFailed("no run id: the upload step has not run")
    def upd(s, line):
        jobs = s.get("jobs") or {}; fin = sum(1 for v in jobs.values() if v in ("success", "failure", "cancelled", "skipped"))
        c.log(f"  {line}")
        if jobs: c.log(f"  {fin} of {len(jobs)} reels finished in the cloud")
    try: s = gh.wait(d["run_id"], timeout=timeout, interval=interval, on_update=upd)
    except Exception as e:
        if type(e).__name__ == "GHError": raise StepFailed(str(e))
        raise
    if s.get("timeout"): raise StepFailed("the cloud run is still going: press the button again to keep waiting")
    d["conclusion"] = s.get("conclusion"); d["jobs"] = s.get("jobs"); c.st.save()
    if s.get("conclusion") in ("cancelled", "timed_out", "startup_failure"): raise StepFailed("cloud run " + str(s.get("conclusion")))
    if s.get("conclusion") != "success": c.log("  some reels failed in the cloud: the good ones are still downloaded")
    return {"conclusion": s.get("conclusion")}


def out_mp4(c, i): return os.path.join(c.out_dir, i + ".mp4")
def handoff_path(c, i): return os.path.join(c.out_dir, i + ".handoff.json")
def yt_name(c, y): return y.get("out_name") or f"{GAME_NAMES.get(c.game, c.game)} {y['id']}"
def yt_mp4(c, y): return os.path.join(c.out_dir, yt_name(c, y) + ".mp4")
def yt_handoff(c, y): return os.path.join(c.out_dir, yt_name(c, y) + ".handoff.json")
def have(c, i): return exists(out_mp4(c, i), 1000) or exists(handoff_path(c, i), 10)            # video here, or already on Social Lamma
def yt_have(c, y): return exists(yt_mp4(c, y), 1000) or exists(yt_handoff(c, y), 10)
def cloud_enc(c, i, full=False): return os.path.join(c.cloud_out, i + (".full.tar.enc" if full else ".out.tar.enc"))
def want_full(c, i): return c.full and not exists(out_mp4(c, i), 1000)
YT_SIDE = (".description.txt", ".chapters.txt", ".srt", ".thumbnail.png", ".layout.json", ".handoff.json", ".qa.json", ".qa.jpg", ".thumb.jpg", ".preview.mp4")

def fetch_done(c):
    ids = c.ids(); yts = c.yts(); miss = set(c.st.data.get("missing") or []); ymiss = set(c.st.data.get("yt_missing") or [])
    return bool(ids or yts) and bool(c.st.data.get("fetched")) and all((have(c, i) and not want_full(c, i)) or i in miss for i in ids) \
        and all((yt_have(c, y) and not (c.full and not exists(yt_mp4(c, y), 1000))) or y["id"] in ymiss for y in yts)

def yt_copy(c, y):
    """run_dir\\yt\\<vid>.mp4 (+ sidecars, joined by rf_pc decrypt) -> out_dir\\<game> EPn.mp4 / .description.txt / .chapters.txt ..."""
    src = os.path.join(c.yt_dir, y["id"] + ".mp4")
    if not exists(src, 1000) and not exists(os.path.join(c.yt_dir, y["id"] + ".handoff.json"), 10): return False
    os.makedirs(c.out_dir, exist_ok=True)
    for ext in (".mp4",) + YT_SIDE:
        f = os.path.join(c.yt_dir, y["id"] + ext)
        if exists(f, 1): shutil.copy2(f, os.path.join(c.out_dir, yt_name(c, y) + ext))
    if not exists(os.path.join(c.out_dir, yt_name(c, y) + ".description.txt"), 1):
        with open(os.path.join(c.out_dir, yt_name(c, y) + ".description.txt"), "w", encoding="utf-8") as f: f.write(y.get("title", "") + "\n\n" + y.get("description", "") + "\n")
    with open(os.path.join(c.out_dir, yt_name(c, y) + ".title.txt"), "w", encoding="utf-8") as f: f.write(y.get("title", "") + "\n")
    return True

def s_fetch(c):
    gh = c.gh(); d = c.st.data; ids = c.ids(); yts = c.yts(); allw = c.cloud_ids()
    def part_done(p):
        v = p.split("-")[0]
        return exists(cloud_enc(c, p), 100) or exists(cloud_enc(c, v), 100) or any(yt_have(c, y) for y in yts if y["id"] == v)
    need = [i for i in ids if not exists(cloud_enc(c, i), 100) and not have(c, i)]
    need += [p for p in c.parts() if not part_done(p)]
    need += [i for i in ids + [y["id"] for y in yts] if c.full and exists(cloud_enc(c, i), 100) and not exists(cloud_enc(c, i, True), 100)
             and not exists(out_mp4(c, i), 1000) and not any(exists(yt_mp4(c, y), 1000) for y in yts if y["id"] == i)]
    if need:
        try: r = gh.fetch(d["run_id"], c.cloud_out, d.get("tag"), want=allw, **({"full": True} if c.full else {}))
        except Exception as e:
            if type(e).__name__ == "GHError": raise StepFailed(str(e))
            raise
        c.log(f"  downloaded {len(r['got'])} encrypted output(s)" + (f" + {len(r.get('full') or [])} full video(s)" if r.get("full") else "")
              + (f", missing from the cloud: {','.join(r['missing'])}" if r["missing"] else ""))
        if r.get("full_available"): c.log(f"  already on Social Lamma, not downloaded: {','.join(r['full_available'])} (run again with --full to get the videos too, within 1 day)")
        if not r["got"] and not r.get("full") and not any(exists(cloud_enc(c, i), 100) for i in allw + [y["id"] for y in yts]):
            raise StepFailed("the cloud run produced no outputs (open the run page, see the log)")
    os.makedirs(c.out_dir, exist_ok=True)
    todo = [i for i in ids if (exists(cloud_enc(c, i), 100) and not have(c, i)) or (want_full(c, i) and exists(cloud_enc(c, i, True), 100))]
    todo += [y["id"] for y in yts if not yt_have(c, y) and not exists(os.path.join(c.yt_dir, y["id"] + ".mp4"), 1000)
             and any(exists(cloud_enc(c, p), 100) for p in c.parts() + [y["id"]] if p == y["id"] or p.startswith(y["id"] + "-"))]
    todo += [y["id"] for y in yts if c.full and not exists(yt_mp4(c, y), 1000) and exists(cloud_enc(c, y["id"], True), 100)]
    if todo:
        rc = c.run_cmd(uv(c, "rf_pc.py", "decrypt", "--in", c.cloud_out, "--out", c.out_dir, "--segs", c.segs, "--scripts", c.scripts, "--yt-out", c.yt_dir))
        if rc != 0: raise StepFailed(f"rf_pc.py decrypt failed (code {rc}): is RENDER_KEY the same value as the GitHub secret?")
    for y in yts:
        if not exists(yt_mp4(c, y), 1000): yt_copy(c, y)
    d["missing"] = [i for i in ids if not have(c, i)]; d["yt_missing"] = [y["id"] for y in yts if not yt_have(c, y)]
    d["handed_off"] = [i for i in ids if exists(handoff_path(c, i), 10)] + [y["id"] for y in yts if exists(yt_handoff(c, y), 10)]
    d["fetched"] = True; c.st.save()
    if len(d["missing"]) == len(ids) and len(d["yt_missing"]) == len(yts): raise StepFailed("no video came out of the decrypt step")
    if d["yt_missing"]: c.log(f"  YouTube not complete: {','.join(d['yt_missing'])} (a part failed in the cloud; see cloud_out\\logs)")
    if d["handed_off"]: c.log(f"  already uploaded to Social Lamma from GitHub (no video download): {','.join(d['handed_off'])}")
    return {"videos": len(ids) - len(d["missing"]), "missing": d["missing"], "youtube": len(yts) - len(d["yt_missing"]), "yt_missing": d["yt_missing"],
            "handed_off": len(d["handed_off"])}


def videos(c): return [i for i in c.ids() if have(c, i)]
def qa_path(c, i): return os.path.join(c.out_dir, i + ".qa.jpg")
def thumb_path(c, i): return os.path.join(c.out_dir, i + ".thumb.jpg")
def yt_videos(c): return [y for y in c.yts() if yt_have(c, y)]
def qa_done(c): return bool(videos(c) or yt_videos(c)) and all(exists(qa_path(c, i), 100) and exists(thumb_path(c, i), 100) for i in videos(c) if exists(out_mp4(c, i), 1000))

def s_qa(c):
    for i in videos(c):
        mp4 = out_mp4(c, i)
        if not exists(mp4, 1000):                  # handed off: the runner made the sheet + thumbnail (previews.py)
            if not exists(qa_path(c, i), 100): c.log(f"  {i}: on Social Lamma, no contact sheet came with it (run with --full to make one)")
            continue
        if not exists(qa_path(c, i), 100):
            d = c.probe(mp4) or 30.0; sec = max(1, int(round(d / 25.0)))        # 25 frames per sheet (5x5), one sheet per video
            rc = c.run_tool("QA_SHEET.bat", f"{q(mp4)} {sec}")
            if rc != 0 or not exists(qa_path(c, i), 100): raise StepFailed(f"QA_SHEET.bat failed for {i} (code {rc})")
        if not exists(thumb_path(c, i), 100):
            c.run_cmd(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "1", "-i", mp4, "-frames:v", "1", "-vf", "scale=360:-2", thumb_path(c, i)])
            if not exists(thumb_path(c, i), 100): c.log(f"  no thumbnail for {i} (ffmpeg)")
    return {"sheets": len(videos(c))}


def summary_path(c): return os.path.join(c.out_dir, "summary.json")
def summary_done(c): return exists(summary_path(c), 10) and all(mt(summary_path(c)) >= mt(f) for f in [out_mp4(c, i) for i in videos(c)] + [yt_mp4(c, y) for y in yt_videos(c)]
                                                                     + [handoff_path(c, i) for i in videos(c)] + [yt_handoff(c, y) for y in yt_videos(c)])

def read_text(p):
    try:
        with open(p, encoding="utf-8-sig", errors="replace") as f: return f.read().strip()
    except OSError: return ""

def read_cloud_qa(path):
    """<id>.qa.json from the render farm (qa_media.py) -> (status, codes, warn); no file / bad file -> ("none", [], [])"""
    d = load_json(path, None)
    if not isinstance(d, dict) or d.get("status") not in ("PASS", "FAIL"): return "none", [], []
    codes = [str(x) for x in d.get("codes") or []]
    return ("FAIL" if d["status"] == "FAIL" and codes else "PASS"), codes, [str(x) for x in d.get("warn") or []]

def handoff_info(p):
    """<id>.handoff.json from the runner -> {media_id, bytes, secs, w, h} or None"""
    h = load_json(p, None)
    return h if isinstance(h, dict) and h.get("media_id") else None

def build_summary(c):
    plan = load_json(c.plan_path, {}); rev = load_json(os.path.join(c.run_dir, "review.json"), [])
    pr = {r["id"]: r for r in plan.get("reels", [])}; reels = []
    for i in c.ids():
        mp4 = exists(out_mp4(c, i), 1000); ho = handoff_info(handoff_path(c, i)); ok = mp4 or bool(ho)
        qs, qcodes, qwarn = read_cloud_qa(os.path.join(c.out_dir, i + ".qa.json"))
        reels.append({"id": i, "status": ("qa_fail" if qs == "FAIL" else "ok") if ok else "missing", "cloud_qa": qs, "qa_codes": qcodes, "qa_warn": qwarn,
                      "file": i + ".mp4" if ok else None, "local": mp4,
                      "mb": round((os.path.getsize(out_mp4(c, i)) if mp4 else (ho or {}).get("bytes", 0)) / 1048576, 1) if ok else 0,
                      "secs": round(c.probe(out_mp4(c, i)) if mp4 else float((ho or {}).get("secs") or 0), 1) if ok else 0, "hook": (pr.get(i) or {}).get("hook", ""),
                      "caption": read_text(os.path.join(c.out_dir, i + ".caption.txt")) or (pr.get(i) or {}).get("caption", ""),
                      "srt": i + ".srt" if exists(os.path.join(c.out_dir, i + ".srt")) else None,
                      "qa": i + ".qa.jpg" if exists(qa_path(c, i)) else None, "thumb": i + ".thumb.jpg" if exists(thumb_path(c, i)) else None,
                      "preview": i + ".preview.mp4" if exists(os.path.join(c.out_dir, i + ".preview.mp4")) else None,
                      "media_id": (ho or {}).get("media_id"), "handoff": i + ".handoff.json" if ho else None})
    yt = []
    for y in c.yts():
        nm = yt_name(c, y); mp4 = exists(yt_mp4(c, y), 1000); ho = handoff_info(yt_handoff(c, y)); ok = mp4 or bool(ho)
        yt.append({"id": y["id"], "ep": y.get("ep"), "status": "ok" if ok else "missing", "file": nm + ".mp4" if ok else None, "local": mp4, "title": y.get("title", ""),
                   "mb": round((os.path.getsize(yt_mp4(c, y)) if mp4 else (ho or {}).get("bytes", 0)) / 1048576, 1) if ok else 0,
                   "secs": round(c.probe(yt_mp4(c, y)) if mp4 else float((ho or {}).get("secs") or 0), 1) if ok else 0,
                   "description": nm + ".description.txt" if exists(os.path.join(c.out_dir, nm + ".description.txt")) else None,
                   "chapters": nm + ".chapters.txt" if exists(os.path.join(c.out_dir, nm + ".chapters.txt")) else None,
                   "thumb": nm + ".thumbnail.png" if exists(os.path.join(c.out_dir, nm + ".thumbnail.png")) else None, "text_by": y.get("text_source"),
                   "qa": nm + ".qa.jpg" if exists(os.path.join(c.out_dir, nm + ".qa.jpg")) else None,
                   "preview": nm + ".preview.mp4" if exists(os.path.join(c.out_dir, nm + ".preview.mp4")) else None,
                   "media_id": (ho or {}).get("media_id"), "handoff": nm + ".handoff.json" if ho else None})
    d = c.st.data; steps = {n: {"status": c.st.status(n), "secs": c.st.step(n).get("secs")} for n in ORDER}
    return {"v": 1, "stream": c.stem, "video": c.video, "game": c.game, "game_name": GAME_NAMES.get(c.game, c.game), "date": c.date(), "out_dir": c.out_dir,
            "created": now_iso(), "reels": reels, "missing": d.get("missing") or [], "youtube": yt, "yt_missing": d.get("yt_missing") or [],
            "cloud": {"repo": os.environ.get("SLADE_RENDER_REPO", "yousefimam1-cmyk/slade-render"), "tag": d.get("tag"), "run_id": d.get("run_id"), "url": d.get("run_url"), "conclusion": d.get("conclusion")},
            "options": {"scribe": c.scribe, "transcript": (c.st.step("transcribe").get("info") or {}).get("engine", "local-whisper"), "review_plan": c.review_plan, "youtube": c.youtube},
            "rules": plan.get("rules"), "plan": c.plan_path,
            "review": {"fix": sum(1 for x in rev if x["level"] == "fix"), "warn": sum(1 for x in rev if x["level"] == "warn"), "todo": sum(1 for x in rev if x["level"] == "todo"),
                       "items": [x for x in rev if x["level"] in ("warn", "fix")][:40]},
            "steps": steps, "total_secs": round(sum((v["secs"] or 0) for v in steps.values()), 1),
            "next": "Claude: read this file + one contact sheet per video, write captions. Then Hub > Review > Approve."}

def s_summary(c):
    s = build_summary(c); os.makedirs(c.out_dir, exist_ok=True)
    tmp = summary_path(c) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, summary_path(c))
    with open(os.path.join(c.out_dir, ".ns_stream"), "w", encoding="utf-8") as f: f.write(c.stem)
    c.log(f"  summary: {summary_path(c)}")
    bad = [r["id"] + ":" + ",".join(r["qa_codes"]) for r in s["reels"] if r["status"] == "qa_fail"]
    if bad: c.log("  cloud QA failed (not auto-posted): " + " ".join(bad))
    return {"qa_fail": len(bad), "videos": sum(1 for r in s["reels"] if r["status"] in ("ok", "qa_fail")), "youtube": sum(1 for y in s["youtube"] if y["status"] == "ok")}


def post_off(c):
    if getattr(c, "no_post", False): return "--no-post"
    try:
        import social_post
        return None if social_post.available() else "no Social Lamma token (SET_SOCIAL_TOKEN.bat)"
    except Exception as e: return "social_post unavailable: " + type(e).__name__

def post_done(c):
    if post_off(c): return True                                            # posting is off: nothing to do, the chain still ends OK
    st = load_json(os.path.join(c.out_dir, "social_post.json"), {})
    return bool(videos(c)) and all((st.get(i) or {}).get("post_id") for i in videos(c))

def s_post(c):
    why = post_off(c)
    if why: c.log("  posting skipped: " + why); return {"skipped": why}
    import social_post
    try: r = social_post.run(c.out_dir, log=lambda t: c.log("  " + t))
    except social_post.SocialError as e: raise StepFailed(str(e))
    if r["failed"]: raise StepFailed(f"{len(r['failed'])} reel(s) not posted: " + "; ".join(f"{x['id']}: {x['why']}" for x in r["failed"])[:300])
    return {"posted": len(r["posted"]), "skipped": len(r["skipped"])}


def make_steps():
    return [Step("audio", s_audio, audio_done), Step("transcribe", s_transcribe, transcribe_done), Step("plan", s_plan, plan_ready),
            Step("cut", s_cut, cut_done), Step("encrypt", s_encrypt, encrypt_done), Step("upload", s_upload, upload_done),
            Step("poll", s_poll, poll_done), Step("fetch", s_fetch, fetch_done), Step("qa", s_qa, qa_done), Step("summary", s_summary, summary_done),
            Step("post", s_post, post_done)]


def open_run(c):
    os.makedirs(c.run_dir, exist_ok=True)
    c.st = State(os.path.join(c.run_dir, "state.json"), {"video": c.video, "stem": c.stem, "game": c.game})
    c.out_dir = c.st.d.get("out_dir") or c.pick_out_dir()
    c.st.d.update(out_dir=c.out_dir, game=c.game, scribe=c.scribe, review_plan=c.review_plan, reels=c.n_reels, youtube=c.youtube); c.st.save()
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video"); ap.add_argument("--game", default=os.environ.get("CF_GAME") or "tft"); ap.add_argument("--scribe", action="store_true")
    ap.add_argument("--reels", type=int, default=0, help="max reels, 0 = every reel-worthy clip"); ap.add_argument("--review-plan", action="store_true"); ap.add_argument("--approve-plan", action="store_true")
    ap.add_argument("--youtube", dest="youtube", action="store_true", default=True, help="one YouTube episode per game (default on)"); ap.add_argument("--no-youtube", dest="youtube", action="store_false")
    ap.add_argument("--from", dest="start", choices=ORDER); ap.add_argument("--force", default=""); ap.add_argument("--status", action="store_true")
    ap.add_argument("--scripts", default=HERE); ap.add_argument("--videos"); ap.add_argument("--no-post", action="store_true", help="do not post to Social Lamma")
    ap.add_argument("--full", action="store_true", help="also download the full videos that went straight from GitHub to Social Lamma")
    a = ap.parse_args(argv)
    if not a.status and not os.path.isfile(a.video): print("FAILED video not found: " + a.video); return EXIT_FAIL
    sys.path.insert(0, a.scripts)
    logf = None
    def log(*x):
        t = " ".join(str(v) for v in x); print(t, flush=True)
        if logf:
            try: logf.write(t + "\n"); logf.flush()
            except Exception: pass
    c = Ctx(a.video, a.scripts, a.videos, a.game, a.scribe, a.reels, a.review_plan, log=log, youtube=a.youtube, full=a.full); c.no_post = a.no_post; open_run(c)
    if a.status: print(json.dumps(c.st.d, ensure_ascii=False, indent=1)); return 0
    try:
        if os.path.isfile(os.path.join(c.run_dir, "chain.log")) and os.path.getsize(os.path.join(c.run_dir, "chain.log")) > 1 << 20: os.remove(os.path.join(c.run_dir, "chain.log"))
        logf = open(os.path.join(c.run_dir, "chain.log"), "a", encoding="utf-8")
    except OSError: logf = None
    log(f"== New stream {c.stem} ({c.game}) -> {c.out_dir} {'[Scribe ON]' if c.scribe else '[local transcript]'} {'[YouTube ON]' if c.youtube else '[reels only]'} {time.strftime('%H:%M:%S')}")
    if a.approve_plan:
        with open(os.path.join(c.run_dir, "plan.approved"), "w") as f: f.write(now_iso())
        log("plan approved")
    force = [x for x in a.force.split(",") if x in ORDER]
    rc = run_chain(make_steps(), c.st, c, force=force, start=a.start, out=log)
    if rc == EXIT_OK: log("DONE " + c.out_dir)
    return rc


if __name__ == "__main__": sys.exit(main())
