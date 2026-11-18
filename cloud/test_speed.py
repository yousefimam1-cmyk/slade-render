"""python3 -m unittest cloud/test_speed.py  - runner-side speed parts: hand-off uploader (fake Social Lamma), source cache fetch, previews.
No network, no real token. Needs ffmpeg + openssl."""
import hashlib, json, os, shutil, subprocess, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff as H, fetch_media as M, previews as PV

TOKEN = "sl_TEST_0123456789abcdefSECRET"
MEDIA_ID = "med_9f8e7d6c5b4a"


class FakeSocial:
    """the /mcp tools + presigned part URLs, like the worker. Checks the token goes only to /mcp."""
    def __init__(s, part=1000, kind="multipart", etag_in_body=True, fail=None):
        s.part, s.kind, s.etag_in_body, s.fail = part, kind, etag_in_body, dict(fail or {})
        s.parts = {}; s.completed = None; s.calls = []; s.leak = []; s.lock = threading.Lock(); s.put_body = None
    def __call__(s, method, url, headers, data=None, timeout=0):
        with s.lock: s.calls.append((method, url))
        if url.endswith("/mcp"):
            if headers.get("Authorization") != "Bearer " + TOKEN: return 401, {}, b"{}"
            j = json.loads(data); name = j["params"]["name"]; a = j["params"]["arguments"]
            if s.fail.get(name):
                s.fail[name] -= 1; return 503, {}, b"busy"
            if name == "get_media_upload_url":
                s.size = a["size_bytes"]; s.args = a
                up = {"kind": "put", "url": "https://r2.example/put"} if s.kind == "put" else \
                     {"kind": "multipart", "partSize": s.part, "partCount": -(-a["size_bytes"] // s.part), "partUrlTemplate": "https://r2.example/part/{partNumber}"}
                r = {"uploads": [{"media_id": MEDIA_ID, "upload": up}]}
            elif name == "complete_media_upload": s.completed = a; r = {"ok": True}
            else: return 200, {}, json.dumps({"jsonrpc": "2.0", "id": j["id"], "error": {"message": "no tool"}}).encode()
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": j["id"], "result": {"structuredContent": r}}).encode()
        if "Authorization" in headers: s.leak.append(url)
        if url == "https://r2.example/put": s.put_body = data; return 200, {}, b""
        n = int(url.rsplit("/", 1)[1])
        if s.fail.get(f"part{n}"):
            s.fail[f"part{n}"] -= 1; return 500, {}, b""
        assert int(headers["Content-Length"]) == len(data)
        with s.lock: s.parts[n] = data
        et = f'"etag{n}"'
        return (200, {}, json.dumps({"etag": et}).encode()) if s.etag_in_body else (200, {"etag": et}, b"")


def video(path, secs=2, size="320x568"):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30", "-f", "lavfi", "-i", "sine=f=440",
                    "-t", str(secs), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path], check=True)


class Handoff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(); cls.mp4 = os.path.join(cls.d, "r01.mp4"); video(cls.mp4)
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.d, ignore_errors=True)
    def client(self, fk): return H.Client(TOKEN, "https://social.example", transport=fk, sleep=lambda s: None)
    def run_(self, fk, **k):
        out = os.path.join(self.d, "r01.handoff.json")
        if os.path.exists(out): os.remove(out)
        rc, line = H.handoff(self.mp4, "r01", out, client=self.client(fk), **k)
        return rc, line, (json.load(open(out)) if os.path.exists(out) else None)

    def test_multipart_upload_parallel_parts_in_order(self):
        fk = FakeSocial(part=4096); rc, line, rec = self.run_(fk)
        self.assertEqual(rc, 0); n = -(-os.path.getsize(self.mp4) // 4096)
        self.assertEqual(line, f"HANDOFF OK r01 parts={n}")
        self.assertEqual(b"".join(fk.parts[i] for i in sorted(fk.parts)), open(self.mp4, "rb").read())
        self.assertEqual([p["partNumber"] for p in fk.completed["parts"]], list(range(1, n + 1)))
        self.assertEqual(fk.completed["parts"][0]["etag"], '"etag1"'); self.assertEqual(fk.completed["media_id"], MEDIA_ID)
        self.assertEqual((rec["media_id"], rec["w"], rec["h"], rec["bytes"]), (MEDIA_ID, 320, 568, os.path.getsize(self.mp4)))
        self.assertEqual(rec["sha256"], hashlib.sha256(open(self.mp4, "rb").read()).hexdigest()); self.assertAlmostEqual(rec["secs"], 2.0, delta=0.1)
        self.assertEqual(fk.args["brand"], "slade"); self.assertEqual(fk.args["mime"], "video/mp4"); self.assertEqual(fk.leak, [])

    def test_etag_from_header_and_single_put(self):
        rc, _, rec = self.run_(FakeSocial(part=8192, etag_in_body=False)); self.assertEqual(rc, 0)
        fk = FakeSocial(kind="put"); rc, line, rec = self.run_(fk)
        self.assertEqual((rc, line), (0, "HANDOFF OK r01 parts=1")); self.assertEqual(fk.put_body, open(self.mp4, "rb").read())

    def test_transient_errors_are_retried(self):
        fk = FakeSocial(part=8192, fail={"get_media_upload_url": 2, "part2": 2}); rc, line, rec = self.run_(fk)
        self.assertEqual(rc, 0, line); self.assertIsNotNone(fk.completed)

    def test_no_token_skips_and_bad_token_fails_without_leaking(self):
        out = os.path.join(self.d, "x.json")
        self.assertEqual(H.handoff(self.mp4, "r01", out, token=""), (3, "HANDOFF SKIP r01 reason=no_token")); self.assertFalse(os.path.exists(out))
        bad = H.Client("WRONG", "https://social.example", transport=FakeSocial(), sleep=lambda s: None)
        rc, line = H.handoff(self.mp4, "r01", out, client=bad)
        self.assertEqual((rc, line), (1, "HANDOFF FAIL r01 code=token_refused")); self.assertFalse(os.path.exists(out))

    def test_public_line_never_holds_secrets_or_ids(self):
        for fk in (FakeSocial(part=4096), FakeSocial(fail={"part1": 9})):
            rc, line, _ = self.run_(fk)
            for bad in (TOKEN, MEDIA_ID, "r2.example", "://"): self.assertNotIn(bad, line)
        p = subprocess.run([sys.executable, H.__file__, "--file", self.mp4, "--id", "r01", "--out", os.path.join(self.d, "y.json")],
                           capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "SOCIAL_LAMMA_TOKEN"})
        self.assertEqual((p.returncode, p.stdout.strip(), p.stderr), (3, "HANDOFF SKIP r01 reason=no_token", ""))

    def test_connector_link_in_the_secret_is_reduced_to_the_token(self):
        self.assertEqual(H.norm_token("https://social.example/mcp/" + "A" * 30 + "/"), "A" * 30)
        self.assertEqual(H.norm_token("  plainTOKEN_123  "), "plainTOKEN_123")

    def test_part_upload_failure_is_a_fail_code(self):
        rc, line, rec = self.run_(FakeSocial(part=4096, fail={"part3": 99}))
        self.assertEqual((rc, line, rec), (1, "HANDOFF FAIL r01 code=part_http_500", None))


KEY = "test-only-key-0123456789abcdef"

class Media(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.b = os.path.join(self.d, "bundle"); self.src = os.path.join(self.d, "cache"); os.makedirs(self.b); os.makedirs(self.src)
        os.environ["RENDER_KEY"] = KEY; self.files = []
        for i, rel in enumerate(["pieces/s001.mp4", "pieces/s002.mp4", "propack/clip a.mp4"]):
            data = os.urandom(5000 + i); a = "f-" + hashlib.sha1(data).hexdigest() + ".enc"
            plain = os.path.join(self.d, f"p{i}")
            open(plain, "wb").write(data)
            subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt", "-pass", "env:RENDER_KEY",
                            "-in", plain, "-out", os.path.join(self.src, a)], check=True)
            self.files.append({"path": rel, "asset": a, "size": len(data), "data": data})
        json.dump({"v": 1, "files": [{k: f[k] for k in ("path", "asset", "size")} for f in self.files]}, open(os.path.join(self.b, "media.json"), "w"))
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)

    def test_assets_are_decrypted_into_place(self):
        self.assertEqual(M.fetch(self.b, src=self.src), 3)
        for f in self.files: self.assertEqual(open(os.path.join(self.b, *f["path"].split("/")), "rb").read(), f["data"])
    def test_no_media_json_is_a_no_op(self):
        os.remove(os.path.join(self.b, "media.json")); self.assertEqual(M.fetch(self.b, src=self.src), 0)
    def test_path_tricks_and_bad_names_refused(self):
        for bad in ({"path": "../evil", "asset": self.files[0]["asset"]}, {"path": "pieces/../../x", "asset": self.files[0]["asset"]},
                    {"path": "pieces/x.mp4", "asset": "../../etc/passwd"}, {"path": "plan.json", "asset": self.files[0]["asset"]}):
            json.dump({"files": [bad]}, open(os.path.join(self.b, "media.json"), "w"))
            with self.assertRaises(ValueError): M.fetch(self.b, src=self.src)
    def test_plaintext_asset_and_wrong_size_refused(self):
        a = self.files[0]["asset"]; open(os.path.join(self.src, a), "wb").write(b"not encrypted at all")
        with self.assertRaises(RuntimeError): M.fetch(self.b, src=self.src)
    def test_wrong_size_refused(self):
        m = json.load(open(os.path.join(self.b, "media.json"))); m["files"][1]["size"] += 1; json.dump(m, open(os.path.join(self.b, "media.json"), "w"))
        with self.assertRaises(RuntimeError): M.fetch(self.b, src=self.src)


class Previews(unittest.TestCase):
    def test_sheet_thumb_preview(self):
        d = tempfile.mkdtemp()
        try:
            v = os.path.join(d, "v.mp4"); video(v, secs=4, size="1080x1920")
            self.assertEqual(PV.make(v, d, "r01"), 3)
            for e in (".qa.jpg", ".thumb.jpg", ".preview.mp4"): self.assertGreater(os.path.getsize(os.path.join(d, "r01" + e)), 100)
            self.assertLess(os.path.getsize(os.path.join(d, "r01.preview.mp4")), os.path.getsize(v))
            h = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", os.path.join(d, "r01.preview.mp4")],
                               capture_output=True, text=True).stdout.strip()
            self.assertEqual(h, "640")
        finally: shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__": unittest.main()
