"""Unit tests for the New stream chain: state machine (new_stream.run_chain), the steps with fake tools + a fake GitHub, ns_plan, ns_post.
No Windows, no network, no ffmpeg, no PC files touched. Run: python tests_newstream.py"""
import json, os, shutil, sys, tempfile, time, unittest
_H = os.path.dirname(os.path.abspath(__file__))
for _d in (_H, os.path.join(_H, "..", "pc"), os.path.join(_H, "..")): sys.path.insert(0, _d)      # Scripts\\ on the PC, repo tests\\ in the repo
import new_stream as N, ns_plan as P, ns_post, prep_segments


def touch(p, data=b"x" * 2000):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f: f.write(data)


# ---------------------------------------------------------------- the engine alone
class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.out = []; self.calls = []
        self.st = N.State(os.path.join(self.tmp, "s.json"))
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)
    def mk(self, fail=(), pause=(), done=None):
        def step(n):
            def run(ctx):
                self.calls.append(n)
                if n in fail: raise N.StepFailed(f"{n} broke")
                if n in pause: raise N.Paused("look at it")
                return {"n": n}
            return N.Step(n, run, (done or {}).get(n))
        return [step(n) for n in N.ORDER]
    def go(self, steps, **k): return N.run_chain(steps, self.st, None, out=self.out.append, **k)

    def test_all_steps_run_in_order_and_state_is_saved(self):
        self.assertEqual(self.go(self.mk()), 0); self.assertEqual(self.calls, N.ORDER)
        self.assertTrue(all(self.st.status(n) == "done" for n in N.ORDER)); self.assertIn("STEP 3/10 Find clips and draft the plan", self.out)
        self.assertEqual(N.State(self.st.path).status("summary"), "done")        # persisted
    def test_done_steps_are_skipped_on_rerun(self):
        self.go(self.mk()); self.calls.clear(); self.assertEqual(self.go(self.mk()), 0); self.assertEqual(self.calls, [])
    def test_failure_stops_and_resume_continues_where_it_stopped(self):
        self.assertEqual(self.go(self.mk(fail=("cut",))), 1); self.assertEqual(self.calls, N.ORDER[:4])
        self.assertEqual((self.st.status("plan"), self.st.status("cut")), ("done", "failed")); self.assertEqual(self.st.step("cut")["error"], "cut broke")
        self.calls.clear(); self.assertEqual(self.go(self.mk()), 0); self.assertEqual(self.calls, N.ORDER[3:])
    def test_disk_truth_wins_over_state(self):
        alive = {"audio": True}
        self.go(self.mk(done={"audio": lambda c: alive["audio"]})); self.calls.clear()
        alive["audio"] = False; self.go(self.mk(done={"audio": lambda c: alive["audio"]}))
        self.assertEqual(self.calls[0], "audio")                                  # the file vanished: redo it
    def test_step_already_on_disk_is_marked_done_without_running(self):
        self.go(self.mk(done={"audio": lambda c: True, "transcribe": lambda c: True})); self.assertNotIn("audio", self.calls); self.assertNotIn("transcribe", self.calls)
        self.assertEqual(self.st.step("audio")["info"], {"skipped": "already on disk"})
    def test_crashed_running_step_is_retried(self):
        self.st.set("audio", "done"); self.st.set("transcribe", "running"); self.calls.clear()
        self.assertEqual(self.go(self.mk()), 0); self.assertEqual(self.calls[0], "transcribe")
    def test_unexpected_exception_becomes_failed_state_not_a_traceback(self):
        s = self.mk(); s[1] = N.Step("transcribe", lambda c: 1 / 0)
        self.assertEqual(self.go(s), 1); self.assertIn("ZeroDivisionError", self.st.step("transcribe")["error"])
    def test_pause_then_continue(self):
        self.assertEqual(self.go(self.mk(pause=("plan",))), 10); self.assertEqual(self.st.status("plan"), "paused"); self.assertEqual(self.calls[-1], "plan")
        self.calls.clear(); self.assertEqual(self.go(self.mk()), 0); self.assertEqual(self.calls[0], "plan")
    def test_real_run_resets_later_steps_and_content_change_drops_run_ids(self):
        self.go(self.mk()); self.st.data.update(run_id=5, tag="t", conclusion="success", fetched=True)
        self.calls.clear(); self.go(self.mk(), force=["cut"]); self.assertEqual(self.calls, N.ORDER[3:])
        self.assertNotIn("run_id", self.st.data); self.assertNotIn("conclusion", self.st.data)
    def test_non_content_rerun_keeps_the_cloud_run(self):
        self.go(self.mk()); self.st.data.update(run_id=5, tag="t"); self.go(self.mk(done={n: (lambda c: True) for n in N.ORDER[1:]}), force=["audio"])
        self.assertEqual(self.st.data.get("run_id"), 5)
    def test_start_from_step_and_force(self):
        self.go(self.mk()); self.calls.clear(); self.go(self.mk(), start="qa"); self.assertEqual(self.calls, [])
        self.go(self.mk(), start="qa", force=["qa"]); self.assertEqual(self.calls, ["qa", "summary"])
    def test_corrupt_state_file_starts_clean(self):
        with open(self.st.path, "w") as f: f.write("{not json")
        self.assertEqual(N.State(self.st.path).status("audio"), "pending")


# ---------------------------------------------------------------- the real steps with fake tools
RANKED = [{"rank": 1, "start": 100.0, "dur": 20.0, "score": 9, "hook": "hook one two three four", "title": "t1", "category": "funny"},
          {"rank": 2, "start": 110.0, "dur": 20.0, "score": 8, "hook": "overlaps rank 1", "title": "t2"},
          {"rank": 3, "start": 300.0, "dur": 25.0, "score": 7, "hook": "", "title": "third clip title"},
          {"rank": 4, "start": 500.0, "dur": 5.0, "score": 6, "hook": "too short", "title": "t4"}]
WORDS = [("hello", 100.5, 100.9), ("world", 101.0, 101.4), ("احا", 101.5, 101.9), ("we", 102.0, 102.2), ("win.", 102.3, 102.6), ("later", 300.5, 300.9), ("clip", 301.0, 301.4)]
DIRECTOR = {"fx": [{"op": "punch_in", "t": 2, "d": 1, "zoom": 1.3},
                   {"op": "meme", "id": "m_bruh", "t": 3.0, "d": 1.2, "fallback": {"card": "bruh"}},
                   {"op": "meme", "id": "church_bells", "t": 8.0, "d": 1.0, "fallback": {}},
                   {"op": "meme", "id": "m_bruh", "t": 12.0, "d": 1.0, "fallback": {}},
                   {"op": "meme", "id": "m_okay", "t": 3.5, "d": 1.0, "fallback": {}},
                   {"op": "text_pop", "t": 5.0, "d": 1.5, "text": "label card", "source": "label"}],
            "sfx": [{"t": 3.0, "gen": "boom"}, {"t": 6.0, "gen": "sad_tone"}], "director": {"hook": "the director hook is here today"}}


class FakeGH:
    def __init__(self, drop=()): self.calls = []; self.drop = set(drop); self.fail = None
    def check(self): self.calls.append("check"); return {}
    def upload(self, d, tag, progress=None):
        self.calls.append("upload"); names = sorted(f for f in os.listdir(d) if f.endswith(".tar.enc")); return {"uploaded": names, "skipped": []}
    def dispatch(self, tag, reels):
        self.calls.append("dispatch"); self.reels = list(reels)
        if self.fail: raise type("GHError", (Exception,), {})(self.fail)
        return {"run_id": 777, "url": "https://github.com/x/actions/runs/777"}
    def wait(self, rid, timeout=0, interval=0, on_update=None):
        self.calls.append("wait"); s = {"status": "completed", "conclusion": "success", "jobs": {r: "success" for r in self.reels}}
        if on_update: on_update(s, "completed")
        return s
    def fetch(self, rid, outdir, tag=None, keep=False, want=None):
        self.calls.append("fetch"); os.makedirs(outdir, exist_ok=True); got = []
        for r in want:
            if r in self.drop: continue
            with open(os.path.join(outdir, r + ".out.tar.enc"), "wb") as f: f.write(b"Salted__" + b"z" * 300)
            got.append(r)
        return {"got": got, "missing": [r for r in want if r not in got]}


class Pipeline(unittest.TestCase):
    STEM = "2026-11-13 23-20-41"
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.sc = os.path.join(self.tmp, "Videos", "Scripts"); self.vid = os.path.join(self.tmp, "Videos")
        os.makedirs(os.path.join(self.sc, "knowledge")); os.makedirs(os.path.join(self.sc, "scan"))
        with open(os.path.join(self.sc, "knowledge", "SLADE_EDIT_RULES.md"), "w") as f: f.write("# rules\n")
        for t in ("EXTRACT_AUDIO.bat", "TRANSCRIBE.bat", "SCRIBE.bat", "CLIP_FARMER.bat", "DIRECTOR.bat", "QA_SHEET.bat"): touch(os.path.join(self.sc, t), b"@echo off\n")
        self.video = os.path.join(self.vid, "OBS Streams", self.STEM + ".mp4"); touch(self.video, b"v" * 5000)
        self.ran = []; self.gh = FakeGH(); self.fail_label = None; self.logs = []; self.env_seen = {}
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, cmd, env=None, cwd=None, label=""):
        self.ran.append(label)
        if label == self.fail_label: return 3
        argv = cmd if isinstance(cmd, list) else cmd.split()
        stem, sc = self.STEM, self.sc; agent = os.path.join(sc, "scan", "agent"); farm = os.path.join(sc, "scan", stem + ".farm")
        if label == "EXTRACT_AUDIO.bat": touch(os.path.join(agent, stem + ".a0.wav")); touch(os.path.join(agent, stem + ".proxy.mp4"))
        elif label == "TRANSCRIBE.bat":
            j = {"transcription": [{"offsets": {"from": 0, "to": 1}, "text": "x", "words": [{"w": w, "t0": a, "t1": b, "caption_ok": True} for w, a, b in WORDS]}]}
            touch(os.path.join(sc, "scan", stem + ".ens.json"), json.dumps(j).encode())
        elif label == "SCRIBE.bat":
            ws = [{"text": w, "start": a, "end": b, "type": "word", "speaker_id": "s0"} for w, a, b in WORDS]; touch(os.path.join(agent, stem + ".scribe.json"), json.dumps({"words": ws}).encode())
        elif label == "CLIP_FARMER.bat": touch(os.path.join(farm, "ranked.json"), json.dumps(RANKED).encode())
        elif label == "DIRECTOR.bat": touch(os.path.join(farm, "edits", "farm01.plan.json"), json.dumps(DIRECTOR).encode())
        elif label == "CUT_SEGMENTS.bat": self.env_seen = dict(env); touch(os.path.join(env["SEGS_OUT"], "pieces", "p1.mp4"))
        elif label == "uv":
            sub = [a for a in argv if a in ("check", "bundle", "decrypt")][-1]
            self.ran[-1] = "rf_pc " + sub
            if sub == "bundle":
                segs = argv[argv.index("--segs") + 1]
                for r in argv[argv.index("--reels") + 1].split(",") + ["code"]: touch(os.path.join(segs, r + ".tar.enc"), b"Salted__" + b"q" * 300)
            if sub == "decrypt":
                i, o = argv[argv.index("--in") + 1], argv[argv.index("--out") + 1]
                for f in os.listdir(i):
                    if f.endswith(".out.tar.enc"):
                        r = f[:-len(".out.tar.enc")]; touch(os.path.join(o, r + ".mp4"), b"m" * 4000); touch(os.path.join(o, r + ".caption.txt"), ("caption of " + r).encode()); touch(os.path.join(o, r + ".srt"))
        elif label == "QA_SHEET.bat":
            mp4 = [a for a in argv if a.endswith(".mp4")][0].strip('"'); touch(os.path.splitext(mp4)[0] + ".qa.jpg")
        elif label == "ffmpeg": touch(argv[-1])
        return 0

    def ctx(self, **k):
        c = N.Ctx(self.video, self.sc, self.vid, k.pop("game", "tft"), log=self.logs.append, gh_factory=lambda: self.gh, runner=self.runner, probe=lambda p: 20.0, **k)
        sys.path.insert(0, self.sc) if self.sc not in sys.path else None
        return N.open_run(c)
    def go(self, c, **k): return N.run_chain(N.make_steps(), c.st, c, out=self.logs.append, **k)

    def test_full_chain_happy_path(self):
        c = self.ctx(); self.assertEqual(self.go(c), 0, "\n".join(self.logs[-15:]))
        out = os.path.join(self.vid, "Slade Clips", "TFT 2026-11-13"); self.assertEqual(c.out_dir, out)
        s = json.load(open(os.path.join(out, "summary.json"), encoding="utf-8"))
        self.assertEqual([r["id"] for r in s["reels"]], ["r01", "r02"]); self.assertTrue(all(r["status"] == "ok" and r["qa"] and r["thumb"] for r in s["reels"]))
        self.assertEqual(s["reels"][0]["caption"], "caption of r01"); self.assertEqual(s["cloud"]["run_id"], 777); self.assertEqual(s["options"]["transcript"], "local-whisper")
        self.assertEqual(s["rules"]["file"], "knowledge/SLADE_EDIT_RULES.md"); self.assertEqual(s["missing"], [])
        self.assertEqual(self.gh.calls, ["check", "upload", "dispatch", "wait", "fetch"]); self.assertEqual(self.gh.reels, ["r01", "r02"])
        self.assertNotIn("SCRIBE.bat", self.ran); self.assertEqual(self.ran.count("QA_SHEET.bat"), 2)
        self.assertEqual(self.env_seen["SEGS_OUT"], c.segs); self.assertEqual(self.env_seen["SRC"], c.video); self.assertTrue(self.env_seen["SCRIPTS"].endswith(os.sep))
        self.assertEqual(self.env_seen["SLADE_HUB"], "1")

    def test_second_run_does_nothing(self):
        c = self.ctx(); self.go(c); self.ran.clear(); self.gh.calls.clear(); c2 = self.ctx(); self.assertEqual(self.go(c2), 0)
        self.assertEqual(self.ran, []); self.assertEqual(self.gh.calls, [])

    def test_resume_after_a_failed_step_runs_only_what_is_left(self):
        c = self.ctx(); self.fail_label = "QA_SHEET.bat"; self.assertEqual(self.go(c), 1); self.assertEqual(c.st.status("qa"), "failed"); self.assertIn("QA_SHEET.bat failed", c.st.step("qa")["error"])
        self.fail_label = None; self.ran.clear(); self.gh.calls.clear(); c2 = self.ctx(); self.assertEqual(self.go(c2), 0)
        self.assertEqual(self.gh.calls, []); self.assertNotIn("EXTRACT_AUDIO.bat", self.ran); self.assertNotIn("CLIP_FARMER.bat", self.ran)

    def test_killed_during_upload_does_not_dispatch_twice(self):
        c = self.ctx(); self.go(c, start=None, force=[]); n = self.gh.calls.count("dispatch")
        for stp in ("poll", "fetch", "qa", "summary"): c.st.d["steps"][stp]["status"] = "running"
        c.st.save(); self.go(self.ctx()); self.assertEqual(self.gh.calls.count("dispatch"), n)

    def test_scribe_is_opt_in(self):
        c = self.ctx(scribe=True); self.assertEqual(self.go(c), 0); self.assertIn("SCRIBE.bat", self.ran); self.assertNotIn("TRANSCRIBE.bat", self.ran)
        self.assertTrue(os.path.isfile(os.path.join(self.sc, "scan", self.STEM + ".ens.json")))               # converted for the rest of the pipeline
        self.assertEqual(json.load(open(os.path.join(c.out_dir, "summary.json")))["options"]["transcript"], "elevenlabs-scribe")
    def test_scribe_failure_message_tells_how_to_go_free(self):
        c = self.ctx(scribe=True); self.fail_label = "SCRIBE.bat"; self.assertEqual(self.go(c), 1); self.assertIn("Untick Scribe", c.st.step("transcribe")["error"])

    def test_plan_follows_the_rules_and_pro_plan_format(self):
        c = self.ctx(); self.go(c, ); plan = json.load(open(c.plan_path, encoding="utf-8")); r1, r2 = plan["reels"]
        self.assertEqual((r1["in"], r1["out"], r2["in"]), (100.0, 120.0, 300.0)); self.assertEqual(plan["rules"]["file"], "knowledge/SLADE_EDIT_RULES.md"); self.assertEqual(len(plan["rules"]["sha256"]), 16)
        self.assertEqual([m["id"] for m in r1["memes"]], ["m_bruh"], "church meme, repeat and too-close meme must be dropped")
        self.assertNotIn("punch", r1); self.assertEqual(r1["hook"], "the director hook is here today"); self.assertEqual(r1["rel_timebase"], "source")
        self.assertEqual([s["kind"] for s in r1["sfx"]], ["boom"]); self.assertEqual(r1["bleep"], [[101.5, 101.9]]); self.assertIn("***", " ".join(x["text"] for x in r1["subs"]))
        self.assertFalse(any("." in x["text"] for x in r1["subs"])); self.assertEqual(r1["overlays"][0]["text"], "label card")
        rep = json.load(open(os.path.join(c.run_dir, "review.json"), encoding="utf-8")); msgs = " ".join(x["msg"] for x in rep)
        self.assertIn("religious", msgs); self.assertIn("repeats", msgs); self.assertIn("closer than", msgs); self.assertIn("caption is a placeholder", msgs)
        ps = prep_segments.build(plan, {"r01", "r02"}, 2.0); self.assertEqual(sorted(ps["reels"]), ["r01", "r02"])             # the segment prep accepts the draft

    def test_missing_rules_file_stops_before_any_plan(self):
        os.remove(os.path.join(self.sc, "knowledge", "SLADE_EDIT_RULES.md")); c = self.ctx(); self.assertEqual(self.go(c), 1)
        self.assertEqual(c.st.status("plan"), "failed"); self.assertIn("edit rules", c.st.step("plan")["error"]); self.assertFalse(os.path.exists(c.plan_path)); self.assertNotIn("CLIP_FARMER.bat", self.ran)

    def test_review_pause_keeps_manual_edits_and_continues_after_approval(self):
        c = self.ctx(review_plan=True); self.assertEqual(self.go(c), 10); self.assertEqual(c.st.status("plan"), "paused"); self.assertNotIn("CUT_SEGMENTS.bat", self.ran)
        plan = json.load(open(c.plan_path, encoding="utf-8")); plan["reels"][0]["hook"] = "EDITED BY CLAUDE"; json.dump(plan, open(c.plan_path, "w", encoding="utf-8"), ensure_ascii=False)
        self.assertEqual(self.go(self.ctx(review_plan=True)), 10)                                              # still waiting
        open(os.path.join(c.run_dir, "plan.approved"), "w").close(); c3 = self.ctx(review_plan=True); self.assertEqual(self.go(c3), 0)
        self.assertEqual(json.load(open(os.path.join(c3.out_dir, "summary.json"), encoding="utf-8"))["reels"][0]["hook"], "EDITED BY CLAUDE"); self.assertEqual(self.ran.count("CLIP_FARMER.bat"), 1)

    def test_plan_edit_after_cut_redoes_cut_and_a_new_cloud_run(self):
        c = self.ctx(); self.go(c); first = c.st.data["run_id"]; self.ran.clear(); self.gh.calls.clear()
        t = time.time() - 3600; os.utime(N.cut_marker(c), (t, t)); c2 = self.ctx(); self.assertEqual(self.go(c2), 0)      # the plan is now newer than the cut
        self.assertIn("CUT_SEGMENTS.bat", self.ran); self.assertEqual(self.gh.calls.count("dispatch"), 1); self.assertNotIn("EXTRACT_AUDIO.bat", self.ran); self.assertEqual(first, 777)

    def test_cloud_failure_of_one_reel_is_reported_not_fatal(self):
        self.gh = FakeGH(drop=["r02"]); c = self.ctx(); self.assertEqual(self.go(c), 0)
        s = json.load(open(os.path.join(c.out_dir, "summary.json"), encoding="utf-8")); self.assertEqual(s["missing"], ["r02"]); self.assertEqual(s["reels"][1]["status"], "missing")
        self.assertEqual(self.ran.count("QA_SHEET.bat"), 1)
    def test_github_error_is_a_readable_failed_step_and_resumable(self):
        self.gh.fail = "token rejected: run SET_GH_TOKEN.bat"; c = self.ctx(); self.assertEqual(self.go(c), 1); self.assertIn("SET_GH_TOKEN.bat", c.st.step("upload")["error"])
        self.gh.fail = None; self.ran.clear(); self.assertEqual(self.go(self.ctx()), 0); self.assertNotIn("rf_pc bundle", self.ran)               # encrypt is not redone
    def test_missing_render_key_has_a_clear_message(self):
        real = self.runner; self.runner = lambda cmd, env=None, cwd=None, label="": 1 if (label == "uv") else real(cmd, env, cwd, label)
        c = self.ctx(); self.assertEqual(self.go(c), 1); self.assertIn("SET_RENDER_KEY.bat", c.st.step("encrypt")["error"])
    def test_output_folder_is_per_game_and_date_and_not_shared_between_streams(self):
        c = self.ctx(); self.go(c); other = os.path.join(self.vid, "OBS Streams", "2026-11-13 20-00-00.mp4"); touch(other)
        c2 = N.Ctx(other, self.sc, self.vid, "tft"); self.assertEqual(os.path.basename(c2.pick_out_dir()), "TFT 2026-11-13 2")
        c3 = N.Ctx(other, self.sc, self.vid, "league"); self.assertEqual(os.path.basename(c3.pick_out_dir()), "League 2026-11-13")
    def test_hub_progress_lines_are_not_confused_by_tool_output(self):
        self.assertEqual(N.run_chain([N.Step("audio", lambda c: None)], self.ctx().st, None, out=self.logs.append), 0)
        self.assertTrue(any(l.startswith("STEP 1/1") for l in self.logs))


# ---------------------------------------------------------------- ns_plan / ns_post
class Plan(unittest.TestCase):
    def test_scribe_to_ens_groups_and_keeps_times(self):
        j = {"words": [{"text": "a", "start": 0, "end": 1, "type": "word"}, {"text": " ", "start": 1, "end": 1.1, "type": "spacing"}, {"text": "b", "start": 1.1, "end": 2, "type": "word"},
                       {"text": "c", "start": 5, "end": 6, "type": "word"}, {"text": "[laugh]", "start": 6, "end": 7, "type": "audio_event"}]}
        e = P.scribe_to_ens(j); self.assertEqual([len(s["words"]) for s in e["transcription"]], [2, 1]); self.assertEqual(e["transcription"][0]["offsets"], {"from": 0, "to": 2000})
        self.assertEqual(P._words_of(e), [(0.0, 1.0, "a"), (1.1, 2.0, "b"), (5.0, 6.0, "c")])
    def test_pages_are_short_dotless_and_cuss_bleeped_english_not(self):
        w = [(0, .4, "one"), (.5, .9, "two"), (1, 1.4, "three"), (1.5, 1.9, "four"), (2, 2.4, "five."), (2.5, 2.9, "fuck"), (3, 3.4, "احا")]
        subs, bleep = P.pages(w, 0, 10); self.assertEqual([len(s["text"].split()) for s in subs], [4, 3]); self.assertEqual(bleep, [[3.0, 3.4]])
        self.assertIn("fuck", subs[1]["text"]); self.assertNotIn(".", " ".join(s["text"] for s in subs))
    def test_apply_rules_unit(self):
        r = {"id": "r01", "in": 0, "out": 20, "hook": "a b c d e", "subs": [1], "bleep": [], "punch": [1], "memes": [
            {"t": 1, "dur": 1, "id": "a"}, {"t": 1.5, "dur": 1, "id": "b"}, {"t": 4.5, "dur": 1, "id": "b"}, {"t": 9, "dur": 1, "id": "choir_sound"}, {"t": 12, "dur": 1, "id": "c"}]}
        rep = P.apply_rules([r]); self.assertNotIn("punch", r)
        # a at 1-2 s kept; b at 1.5 s is closer than 1.5 s after a ends: dropped; b at 4.5 s kept; choir dropped; c kept
        self.assertEqual([m["id"] for m in r["memes"]], ["a", "b", "c"])
        self.assertTrue(any("zoom" in x["msg"] for x in rep)); self.assertTrue(any("religious" in x["msg"] for x in rep)); self.assertTrue(any("closer than" in x["msg"] for x in rep))
    def test_meme_may_be_used_in_two_videos_only(self):
        rs = [{"id": f"r0{i}", "in": 0, "out": 20, "hook": "a b c d", "subs": [1], "bleep": [], "memes": [{"t": 1, "dur": 1, "id": "m_bruh"}]} for i in (1, 2, 3)]
        P.apply_rules(rs); self.assertEqual([len(r["memes"]) for r in rs], [1, 1, 0])
    def test_pick_clips_no_overlap_and_length_limits(self):
        self.assertEqual([c["rank"] for c in P.pick_clips(RANKED, 6)], [1, 3]); self.assertEqual([c["rank"] for c in P.pick_clips(RANKED, 1)], [1])
        self.assertEqual(P.pick_clips([{"rank": 1, "start": 0, "dur": 300, "score": 1}], 3)[0]["dur"], 300)       # clamped later by reel_from
    def test_reel_length_is_capped(self):
        r = P.reel_from({"rank": 1, "start": 10, "dur": 300}, "r01", [], None, "TFT"); self.assertEqual(r["out"] - r["in"], P.REEL_MAX)
    def test_rules_info_requires_the_file(self):
        with self.assertRaises(FileNotFoundError): P.rules_info(tempfile.mkdtemp())


class Post(unittest.TestCase):
    S = {"stream": "s1", "game_name": "TFT", "date": "2026-11-13", "out_dir": "/o", "reels": [
        {"id": "r01", "status": "ok", "file": "r01.mp4", "hook": "h1", "caption": "real caption", "secs": 20}, {"id": "r02", "status": "ok", "file": "r02.mp4", "hook": "h2", "caption": "h2"},
        {"id": "r03", "status": "missing"}]}
    def test_build_plan(self):
        p = ns_post.build_plan(self.S, ["r01", "r02", "r03"], ["tiktok", "bogus"], {"r02": "mine"})
        self.assertEqual([i["id"] for i in p["items"]], ["r01", "r02"]); self.assertEqual(p["platforms"], {"tiktok": ["r01", "r02"]}); self.assertEqual(p["items"][1]["caption"], "mine")
        self.assertFalse(p["items"][0]["caption_is_placeholder"]); self.assertEqual(ns_post.build_plan(self.S)["items"][1]["caption_is_placeholder"], True)
    def test_no_token_or_endpoint_means_no_network(self):
        p = ns_post.build_plan(self.S); calls = []
        for cfg in ({}, {"token": "t"}, {"schedule_endpoint": "/x"}):
            ok, msg = ns_post.schedule(p, cfg, opener=lambda *a, **k: calls.append(1)); self.assertFalse(ok); self.assertIn("posting_plan.json", msg)
        self.assertEqual(calls, [])
    def test_placeholder_captions_are_not_sent(self):
        ok, msg = ns_post.schedule(ns_post.build_plan(self.S), {"token": "t", "schedule_endpoint": "/api/x"}, opener=lambda *a, **k: 1 / 0); self.assertFalse(ok); self.assertIn("placeholders", msg)
    def test_schedule_posts_with_bearer(self):
        seen = {}
        class R:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): pass
        def op(rq, timeout=0): seen.update(url=rq.full_url, auth=rq.get_header("Authorization"), body=json.loads(rq.data)); return R()
        p = ns_post.build_plan(self.S, ["r01"]); ok, msg = ns_post.schedule(p, {"token": "T", "base": "https://x.example/", "schedule_endpoint": "/api/bulk"}, opener=op)
        self.assertTrue(ok); self.assertEqual((seen["url"], seen["auth"]), ("https://x.example/api/bulk", "Bearer T")); self.assertEqual(seen["body"]["plan"]["items"][0]["id"], "r01")
    def test_http_error_is_reported(self):
        import urllib.error
        def op(rq, timeout=0): raise urllib.error.HTTPError(rq.full_url, 401, "x", {}, None)
        ok, msg = ns_post.schedule(ns_post.build_plan(self.S, ["r01"]), {"token": "T", "schedule_endpoint": "/a"}, opener=op); self.assertFalse(ok); self.assertIn("401", msg)


class Bat(unittest.TestCase):
    def test_generated_cut_bat_honours_env_overrides_and_is_crlf(self):
        d = tempfile.mkdtemp(); plan = {"source": "C:\\x\\s.mp4", "reels": [{"id": "r01", "in": 10, "out": 30, "keep": [[10, 30]], "memes": [{"t": 1, "id": "m_bruh"}], "sfx": [], "overlays": [], "subs": []}]}
        pp = os.path.join(d, "plan.json"); json.dump(plan, open(pp, "w")); self.assertEqual(prep_segments.main(["cut", pp, "--reels", "r01", "--out", d]), 0)
        b = open(os.path.join(d, "CUT_SEGMENTS.bat"), "rb").read().decode()
        self.assertIn("\r\n", b); self.assertNotIn("\n\n", b.replace("\r\n", "")); self.assertIn('if defined SEGS_OUT (set "OUT=%SEGS_OUT%")', b); self.assertIn('if not defined SCRIPTS set "SCRIPTS=%~dp0"', b)
        self.assertNotIn("%~dp0memes", b); shutil.rmtree(d)


class Bats(unittest.TestCase):
    def test_pc_scripts_are_crlf_and_never_print_the_token(self):
        here = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isfile(os.path.join(here, "NEW_STREAM.bat")): here = os.path.join(here, "..", "pc")            # repo layout
        for f in ("NEW_STREAM.bat", "SET_GH_TOKEN.bat", "set_gh_token.ps1"):
            b = open(os.path.join(here, f), "rb").read(); self.assertTrue(b.count(b"\r\n") == b.count(b"\n"), f + " must be CRLF")
        ps = open(os.path.join(here, "set_gh_token.ps1"), encoding="utf-8").read()
        self.assertNotRegex(ps, r"Write-(Output|Host)[^\n]*\$k\b"); self.assertIn("GH_RENDER_TOKEN", ps)


if __name__ == "__main__": unittest.main(verbosity=1)
