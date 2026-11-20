"""python3 -m unittest cloud/test_qa_media.py  (needs ffmpeg + numpy; whisper is faked through the qa() seam)"""
import json, os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_media as Q

F = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
def ff(*a): subprocess.run(F + list(a), check=True)

class Media(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = d = tempfile.mkdtemp(); j = lambda n: os.path.join(d, n)
        ff("-f", "lavfi", "-i", "mandelbrot=s=320x180:r=25", "-t", "9", "-pix_fmt", "yuv420p", j("src.mp4"))
        ff("-i", j("src.mp4"), "-t", "3", j("a.mp4")); ff("-ss", "3", "-i", j("src.mp4"), "-t", "3", j("b.mp4"))
        ff("-f", "lavfi", "-i", "color=black:s=320x180:r=25", "-t", "1", "-pix_fmt", "yuv420p", j("k.mp4"))
        ff("-f", "lavfi", "-i", "color=gray:s=320x180:r=25", "-t", "6", "-pix_fmt", "yuv420p", j("still.mp4"))
        for name, parts in (("loop", "aba"), ("black", "akb")):
            open(j(name + ".txt"), "w").write("".join(f"file {p}.mp4\n" for p in parts))
            ff("-f", "concat", "-i", j(name + ".txt"), "-c:v", "libx264", j(name + ".mp4"))
        ff("-i", j("src.mp4"), "-f", "lavfi", "-i", "sine=f=300:d=9", "-shortest", "-c:v", "copy", j("sound.mp4"))
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.d, ignore_errors=True)
    def p(self, n): return os.path.join(self.d, n + ".mp4")

    def test_clean_clip_passes(self):
        self.assertEqual(Q.check_loop(self.p("src")), []); self.assertEqual(Q.check_black(self.p("src"), Q.duration(self.p("src"))), [])
    def test_looped_clip_is_found_with_times(self):
        lo = Q.check_loop(self.p("loop")); self.assertEqual(len(lo), 1)
        self.assertAlmostEqual(lo[0]["t"], 6.0, delta=0.3); self.assertAlmostEqual(lo[0]["src"], 0.0, delta=0.3); self.assertGreaterEqual(lo[0]["len"], 1.5)
    def test_still_frame_is_not_a_loop(self):
        self.assertEqual(Q.check_loop(self.p("still")), [])
    def test_black_gap_is_found(self):
        bl = Q.check_black(self.p("black"), Q.duration(self.p("black"))); self.assertEqual(len(bl), 1); self.assertAlmostEqual(bl[0][0], 3.0, delta=0.1)
    def test_black_only_at_the_edges_is_ok(self):
        self.assertEqual(Q.parse_black("black_start:0 black_end:0.55", 10.0), [])
        self.assertEqual(Q.parse_black("black_start:9.45 black_end:10", 10.0), [])
        self.assertEqual(Q.parse_black("black_start:0 black_end:0.7", 10.0), [[0.0, 0.7]])

    def test_reel_end_to_end_sync_is_a_warning_and_public_line_has_no_text(self):
        pkg = tempfile.mkdtemp(dir=self.d); os.makedirs(os.path.join(pkg, "reels"))
        shutil.copy(self.p("sound"), os.path.join(pkg, "reels", "rG1.mp4"))
        words = ["كلمة%d" % i for i in range(12)]
        srt = "".join(f"{i+1}\n00:00:0{i//2},{(i%2)*500:03d} --> 00:00:0{i//2},{(i%2)*500+400:03d}\n{w}\n\n" for i, w in enumerate(words))
        open(os.path.join(pkg, "reels", "rG1.srt"), "w", encoding="utf-8").write(srt)
        heard = [{"w": w, "s": i * 0.5 - 1.5} for i, w in enumerate(words)]   # speech 1.5 s earlier -> captions late
        res = Q.qa("rG1", pkg, whisper=lambda m, t: (heard, 9.0)); Q.write(res, pkg)
        self.assertEqual((res["codes"], res["warn"], res["status"]), ([], ["sync"], "PASS")); self.assertAlmostEqual(res["checks"]["sync"]["median"], 1.5, delta=0.01)
        line = open(os.path.join(pkg, "qa.line")).read().strip(); self.assertEqual(line, "QA PASS rG1 warn=sync")
        self.assertTrue(os.path.isfile(os.path.join(pkg, "reels", "rG1.qa.json")))
        res2 = Q.qa("rG1", pkg, whisper=lambda m, t: ([{"w": w, "s": i * 0.5 + 0.05} for i, w in enumerate(words)], 9.0))
        self.assertEqual(res2["status"], "PASS"); self.assertEqual(Q.public_line(res2), "QA PASS rG1")

    def test_hard_failures_no_audio_and_no_video(self):
        pkg = tempfile.mkdtemp(dir=self.d); os.makedirs(os.path.join(pkg, "reels"))
        ff("-f", "lavfi", "-i", "testsrc2=s=160x284:r=10:d=3", "-an", os.path.join(pkg, "reels", "r9.mp4"))
        res = Q.qa("r9", pkg); self.assertIn("no_audio", res["codes"]); self.assertEqual(res["status"], "FAIL")
        self.assertEqual(Q.final_codes(os.path.join(pkg, "nope.mp4")), ["no_video"])
        self.assertEqual(Q.final_codes(self.p("sound"), expect_secs=99), ["duration"])

    def test_whisper_crash_never_fails_the_render(self):
        pkg = tempfile.mkdtemp(dir=self.d); os.makedirs(os.path.join(pkg, "reels"))
        shutil.copy(self.p("sound"), os.path.join(pkg, "reels", "r2.mp4")); open(os.path.join(pkg, "reels", "r2.srt"), "w").write("1\n00:00:00,000 --> 00:00:01,000\nاهلا\n")
        def boom(m, t): raise RuntimeError("secret transcript text")
        res = Q.qa("r2", pkg, whisper=boom); self.assertEqual(Q.public_line(res), "QA PASS r2 warn=qa_error")

    def test_youtube_aud_layout_caps_and_meme_warn(self):
        pkg = tempfile.mkdtemp(dir=self.d); os.makedirs(os.path.join(pkg, "yt")); os.makedirs(os.path.join(pkg, "reels"))
        ff("-f", "lavfi", "-i", "sine=f=300:d=5", os.path.join(pkg, "yt", "y1.m4a"))
        caps = [{"w": "مَرْحَبًا%d" % i, "s": i + 0.1, "e": i + 0.5} for i in range(10)]
        lay = {"caps": caps, "memes": [{"t": 2, "vis": 1.0, "dur": 2.5, "keys": ["x"]}, {"t": 5, "vis": [5, 7], "dur": 2.0}], "cuts": []}
        with open(os.path.join(pkg, "yt", "y1.layout.json"), "w", encoding="utf-8") as f: json.dump(lay, f, ensure_ascii=False)
        heard = [{"w": "مرحبا%d" % i, "s": i} for i in range(10)]
        res = Q.qa("y1-aud", pkg, whisper=lambda m, t: (heard, 300.0))
        self.assertEqual(res["status"], "PASS"); self.assertEqual(res["checks"]["sync"]["matched"], 10)
        self.assertEqual([m["i"] for m in res["checks"]["memes"]], [0]); self.assertEqual(Q.public_line(res), "QA PASS y1-aud warn=meme_cut")

class Sync(unittest.TestCase):
    def test_normalise_arabic(self):
        self.assertEqual(Q.norm_ar("إِسْتَراحَة"), Q.norm_ar("استراحه")); self.assertEqual(Q.norm_ar("مشى"), Q.norm_ar("مشي"))
    def test_too_few_matches_never_fail(self):
        st = Q.sync_stats([{"w": "كلمة", "s": 5}] * 5, [{"w": "كلمة", "s": 3}]); self.assertFalse(Q.sync_fail(st))
    def test_short_words_and_far_words_are_ignored(self):
        st = Q.sync_stats([{"w": "في", "s": 1}, {"w": "بعيد", "s": 10}], [{"w": "في", "s": 1}, {"w": "بعيد", "s": 1}]); self.assertEqual(st["matched"], 0)
    def test_spread_fails_on_p75(self):
        caps = [{"w": "كلمة%d" % i, "s": float(i)} for i in range(10)]
        heard = [{"w": "كلمة%d" % i, "s": i + (0.5 if i % 2 else -0.5)} for i in range(10)]
        st = Q.sync_stats(caps, heard); self.assertLess(abs(st["median"]), 0.15); self.assertTrue(Q.sync_fail(st))

if __name__ == "__main__": unittest.main()
