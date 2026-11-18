"""PC-side tests of the fast render pipeline (no network, fakes only): rf_cloud_pc source cache + selective fetch, rf_pc cache_media,
new_stream with runner hand-off (no video download), social_post using the runner's media id. Run: python tests_speed.py
Needs the sibling test files (tests_newstream.py, tests_social_post.py on the PC; tests/ + pc/ in the repo)."""
import io, json, os, shutil, sys, tempfile, time, unittest, zipfile
_H = os.path.dirname(os.path.abspath(__file__))
for _d in (_H, os.path.join(_H, "..", "pc"), os.path.join(_H, "..")): sys.path.insert(0, _d)
os.environ["CF_AI_OFF"] = "1"
import rf_cloud_pc as R

TOKEN = "github_pat_TESTTOKEN_0123456789abcdefghij"; REPO = "yousefimam1-cmyk/slade-render"
try: from test_rf_cloud_pc import FakeGitHub, make_zip          # repo layout
except ImportError: FakeGitHub = None


def mkfake():
    """minimal fake GitHub (releases, assets with created_at, artifacts) when the repo's FakeGitHub is not next to us"""
    if FakeGitHub: return FakeGitHub()
    raise unittest.SkipTest("FakeGitHub (tests/test_rf_cloud_pc.py) not found")


def zipb(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items(): z.writestr(n, d)
    return b.getvalue()


class Cloud(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.fk = mkfake(); os.environ["APPDATA"] = self.tmp
        self.gh = R.GH(REPO, TOKEN, transport=self.fk, sleep=lambda s: None, log=lambda *a: None)
        self.segs = os.path.join(self.tmp, "segs"); os.makedirs(os.path.join(self.segs, "cache", "index"))
        for n in ("r01", "y1-c01", "code"): open(os.path.join(self.segs, n + ".tar.enc"), "wb").write(b"Salted__" + b"x" * 100)
        self.assets = ["f-" + c * 40 + ".enc" for c in "abc"]
        for a in self.assets: open(os.path.join(self.segs, "cache", a), "wb").write(b"Salted__" + a.encode() * 10)
        open(os.path.join(self.segs, "cache", "index", "r01.txt"), "w").write(self.assets[0] + "\n" + self.assets[1] + "\n")
        open(os.path.join(self.segs, "cache", "index", "y1-c01.txt"), "w").write(self.assets[1] + "\n")
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)
    def cache_names(self): return sorted(a["name"] for a in self.fk.releases[R.CACHE_TAG]["assets"])

    def test_cache_assets_upload_once_to_the_long_lived_release(self):
        r = self.gh.upload(self.segs, "t1")
        self.assertEqual(sorted(r["cache_uploaded"]), self.assets[:2]); self.assertEqual(self.cache_names(), self.assets[:2])     # c is not needed
        self.assertNotIn("f-", " ".join(a["name"] for a in self.fk.releases["t1"]["assets"]))
        r2 = self.gh.upload(self.segs, "t2")                                          # a re-render: only the small bundles go up again
        self.assertEqual(r2["cache_uploaded"], []); self.assertEqual(sorted(r2["cache_skipped"]), self.assets[:2])
        self.assertEqual(sorted(r2["uploaded"]), ["code.tar.enc", "r01.tar.enc", "y1-c01.tar.enc"])
    def test_cache_release_is_never_deleted(self):
        self.gh.upload(self.segs, "t1")
        with self.assertRaises(R.GHError): self.gh.delete_release(R.CACHE_TAG)
        self.assertIn(R.CACHE_TAG, self.fk.releases)
    def test_plaintext_cache_asset_refused(self):
        open(os.path.join(self.segs, "cache", self.assets[0]), "wb").write(b"plain mp4 bytes")
        with self.assertRaises(R.GHError): self.gh.upload(self.segs, "t1")
    def test_old_unused_cache_assets_are_pruned_needed_ones_kept(self):
        self.gh.upload(self.segs, "t1")
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 40 * 86400))
        for a in self.fk.releases[R.CACHE_TAG]["assets"]: a["created_at"] = a["updated_at"] = old
        os.remove(os.path.join(self.segs, "cache", "index", "r01.txt"))                   # r01 no longer in this run: asset a unused
        self.gh.upload(self.segs, "t2"); self.assertEqual(self.cache_names(), [self.assets[1]])

    def arts(self, files_by_name):
        aid = 900
        for name, files in files_by_name.items():
            aid += 1; self.fk.artifacts.setdefault(555, []).append({"id": aid, "name": name, "expired": False}); self.fk.zips[aid] = zipb(files)
    def test_fetch_takes_small_outputs_skips_parts_of_a_joined_video_and_full_videos(self):
        blob = lambda: b"Salted__" + os.urandom(64)
        self.arts({"render-r01": {"r01.out.tar.enc": blob()}, "full-r01": {"r01.full.tar.enc": blob()},
                   "render-y1-aud": {"y1-aud.out.tar.enc": blob()}, "render-y1-c01": {"y1-c01.out.tar.enc": blob()},
                   "render-y1": {"y1.out.tar.enc": blob()}, "full-y1": {"y1.full.tar.enc": blob()}})
        self.gh.upload(self.segs, "t1"); out = os.path.join(self.tmp, "co")
        r = self.gh.fetch(555, out, "t1", want=["r01", "y1-aud", "y1-c01"])
        self.assertEqual((r["got"], r["missing"], r["full"], r["full_available"]), (["r01", "y1"], [], [], ["r01", "y1"]))
        self.assertEqual(sorted(os.listdir(out)), ["r01.out.tar.enc", "y1.out.tar.enc"])
        self.assertEqual(sorted(a["name"] for a in self.fk.artifacts[555]), ["full-r01", "full-y1"])          # kept 1 day for --full
        self.assertNotIn("t1", self.fk.releases); self.assertIn(R.CACHE_TAG, self.fk.releases)
        r = self.gh.fetch(555, out, None, want=["r01"], full=True)                       # later: the videos on request
        self.assertEqual(r["full"], ["r01", "y1"]); self.assertTrue(os.path.isfile(os.path.join(out, "y1.full.tar.enc"))); self.assertEqual(self.fk.artifacts[555], [])
    def test_fetch_without_hand_off_is_unchanged(self):
        self.arts({"render-r01": {"r01.out.tar.enc": b"Salted__" + b"1" * 50}, "render-y1-aud": {"y1-aud.out.tar.enc": b"Salted__" + b"2" * 50}})
        r = self.gh.fetch(555, os.path.join(self.tmp, "co"), None, want=["r01", "y1-aud", "y1-c01"])
        self.assertEqual((r["got"], r["missing"]), (["r01", "y1-aud"], ["y1-c01"]))


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["RENDER_KEY"] = "test-only-key-for-cache-0123456789"; os.environ["RF_BACKEND"] = "py"
        import rf_pc; self.P = rf_pc
        try: import cryptography  # noqa
        except ImportError: raise unittest.SkipTest("cryptography missing")
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)
    def stage(self, rid, data):
        b = os.path.join(self.tmp, "st", rid, "bundle"); os.makedirs(os.path.join(b, "pieces")); os.makedirs(os.path.join(b, "memes"))
        open(os.path.join(b, "pieces", "s1.mp4"), "wb").write(data); open(os.path.join(b, "memes", "small.wav"), "wb").write(b"w" * 100)
        return b
    def test_big_media_leave_the_bundle_once_and_names_are_key_bound(self):
        P = self.P; cache = os.path.join(self.tmp, "cache"); hc = P.HashCache(os.path.join(self.tmp, "h.json")); data = os.urandom(P.CACHE_MIN + 10)
        b1 = self.stage("r01", data); n, mb, new = P.cache_media(b1, cache, "r01", hc)
        self.assertEqual(n, 1); self.assertFalse(os.path.exists(os.path.join(b1, "pieces", "s1.mp4"))); self.assertTrue(os.path.exists(os.path.join(b1, "memes", "small.wav")))
        m = json.load(open(os.path.join(b1, "media.json"))); a = m["files"][0]["asset"]
        self.assertEqual((m["files"][0]["path"], m["files"][0]["size"]), ("pieces/s1.mp4", len(data)))
        self.assertTrue(open(os.path.join(cache, a), "rb").read(8) == b"Salted__"); self.assertGreater(new, 0)
        b2 = self.stage("r02", data); n2, _, new2 = P.cache_media(b2, cache, "r02", hc)
        self.assertEqual((json.load(open(os.path.join(b2, "media.json")))["files"][0]["asset"], new2), (a, 0))   # same footage: no new encryption
        self.assertEqual(open(os.path.join(cache, "index", "r02.txt")).read().split(), [a])
        os.environ["RENDER_KEY"] = "another-key-entirely-0123456789ab"; self.assertNotEqual(P.asset_name("00" * 32), a)
        out = os.path.join(self.tmp, "dec.bin"); os.environ["RENDER_KEY"] = "test-only-key-for-cache-0123456789"
        P.dec_file(os.path.join(cache, a), out); self.assertEqual(open(out, "rb").read(), data)


# ---------------- new_stream with hand-off ----------------
try: import tests_newstream as TN            # PC name
except ImportError: import test_new_stream as TN   # repo name
N = TN.N


class FakeGHHandoff(TN.FakeGH):
    def __init__(self, handed=("r01",), **k): super().__init__(**k); self.handed = set(handed); self.full_calls = 0
    def fetch(self, rid, outdir, tag=None, keep=False, want=None, full=False):
        self.calls.append("fetch" + ("_full" if full else "")); os.makedirs(outdir, exist_ok=True); got = []; gf = []
        for r in want:
            with open(os.path.join(outdir, r + ".out.tar.enc"), "wb") as f: f.write(b"Salted__" + b"z" * 300)
            got.append(r)
            if full and r in self.handed:
                with open(os.path.join(outdir, r + ".full.tar.enc"), "wb") as f: f.write(b"Salted__" + b"F" * 300)
                gf.append(r)
        return {"got": got, "missing": [], "full": gf, "full_available": [] if full else sorted(self.handed)}


class PipelineHandoff(TN.Pipeline):
    """the fake decrypt makes small outputs (handoff.json + sheets, no mp4) for handed-off reels, like rf_pc decrypt of the runner's package"""
    def setUp(self):
        super().setUp(); self.gh = FakeGHHandoff()
        real = self.runner
        def runner(cmd, env=None, cwd=None, label=""):
            argv = cmd if isinstance(cmd, list) else cmd.split()
            if label == "uv" and "decrypt" in argv:
                self.ran.append("rf_pc decrypt"); i, o = argv[argv.index("--in") + 1], argv[argv.index("--out") + 1]
                for f in os.listdir(i):
                    full = f.endswith(".full.tar.enc"); r = f.split(".")[0]
                    if full: TN.touch(os.path.join(o, r + ".mp4"), b"m" * 4000); continue
                    if r in self.gh.handed:
                        TN.touch(os.path.join(o, r + ".handoff.json"), json.dumps({"media_id": "med_" + r, "bytes": 5 * 2 ** 20, "secs": 21.5, "w": 1080, "h": 1920}).encode())
                        for e in (".qa.jpg", ".thumb.jpg", ".preview.mp4"): TN.touch(os.path.join(o, r + e))
                    else: TN.touch(os.path.join(o, r + ".mp4"), b"m" * 4000)
                    TN.touch(os.path.join(o, r + ".caption.txt"), ("caption of " + r).encode()); TN.touch(os.path.join(o, r + ".srt"))
                return 0
            return real(cmd, env, cwd, label)
        self.runner = runner

    def test_handed_off_reel_needs_no_video_download(self):
        c = self.ctx(); self.assertEqual(self.go(c), 0, "\n".join(self.logs[-15:]))
        s = json.load(open(os.path.join(c.out_dir, "summary.json"), encoding="utf-8")); r1, r2 = s["reels"]
        self.assertEqual((r1["status"], r1["local"], r1["media_id"], r1["secs"], r1["mb"], r1["qa"], r1["preview"]), ("ok", False, "med_r01", 21.5, 5.0, "r01.qa.jpg", "r01.preview.mp4"))
        self.assertEqual((r2["status"], r2["local"], r2["media_id"]), ("ok", True, None))
        self.assertFalse(os.path.exists(os.path.join(c.out_dir, "r01.mp4"))); self.assertEqual(self.ran.count("QA_SHEET.bat"), 1)   # only r02 (local)
        self.assertEqual(c.st.data["handed_off"], ["r01"]); self.assertTrue(any("not downloaded: r01" in str(x) for x in self.logs))
        self.ran.clear(); self.gh.calls.clear(); self.assertEqual(self.go(self.ctx()), 0); self.assertEqual(self.gh.calls, [])   # rerun: nothing to do

    def test_full_flag_downloads_the_handed_off_video(self):
        c = self.ctx(); self.go(c); self.gh.calls.clear()
        c2 = self.ctx(full=True); self.assertEqual(self.go(c2, force=["fetch"]), 0, "\n".join(self.logs[-15:]))
        self.assertIn("fetch_full", self.gh.calls); self.assertTrue(os.path.exists(os.path.join(c2.out_dir, "r01.mp4")))


# ---------------- social_post with hand-off ----------------
try: import tests_social_post as TS; _Base = TS.T; SP = TS.SP
except ImportError: TS = None; _Base = unittest.TestCase           # social_post.py lives on the PC only


class PostHandoff(_Base):
    def test_handed_off_reel_is_posted_by_media_id_without_upload(self):
        if TS is None: self.skipTest("social_post.py is PC-only")
        SP.youtube_short_ok = self._yt                    # the real check: it must use the runner's w/h/secs (no local file)
        os.remove(os.path.join(self.out, "r01.mp4"))
        json.dump({"media_id": "med_from_runner", "w": 1080, "h": 1920, "secs": 20.0, "bytes": 123}, open(os.path.join(self.out, "r01.handoff.json"), "w"))
        r = SP.run(self.out, ids=["r01"], client=self.c, scan=self.scan, log=self.log.append)
        self.assertEqual(r["failed"], [], r); self.assertEqual(len(r["posted"]), 1)
        self.assertEqual(self.w.posts[-1]["media_id"], "med_from_runner")
        self.assertNotIn("get_media_upload_url", self.w.calls); self.assertNotIn("complete_media_upload", self.w.calls)
        self.assertIn("youtube", self.w.posts[-1]["platforms"])                    # vertical 20 s from handoff.json -> still a Short
        self.assertTrue(any("already on Social Lamma" in l for l in self.log))


if __name__ == "__main__":
    loader = unittest.TestLoader(); suite = unittest.TestSuite()
    for c in (Cloud, Cache, PipelineHandoff, PostHandoff):
        names = [n for n in loader.getTestCaseNames(c) if c not in (PipelineHandoff, PostHandoff) or "handed" in n or "full_flag" in n]
        suite.addTests(c(n) for n in names)
    sys.exit(0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1)
