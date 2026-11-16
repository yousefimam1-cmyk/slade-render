"""Unit tests for rf_cloud_pc.py (GitHub REST client). No network: a fake GitHub behind the Transport interface.
Run: python tests_rf_cloud_pc.py"""
import io, json, os, sys, tempfile, time, unittest, zipfile, shutil, urllib.error
_H = os.path.dirname(os.path.abspath(__file__))
for _d in (_H, os.path.join(_H, "..", "pc"), os.path.join(_H, "..")): sys.path.insert(0, _d)      # Scripts\\ on the PC, repo tests\\ in the repo
import rf_cloud_pc as R

TOKEN = "github_pat_TESTTOKEN_0123456789abcdefghij"
REPO = "yousefimam1-cmyk/slade-render"


class FakeGitHub:
    """just enough of the GitHub REST API; also checks that the token only goes to GitHub hosts"""
    def __init__(s):
        s.releases = {}; s.next_id = 100; s.runs = []; s.artifacts = {}; s.zips = {}; s.log = []; s.deleted_tags = []; s.fail_once = {}; s.leak = []
        s.dispatches = []; s.run_status = []; s.jobs = {}

    def _body(s, body):
        if body is None: return b""
        if isinstance(body, (bytes, bytearray)): return bytes(body)
        out = b""
        while True:
            b = body.read(1 << 16)
            if not b: break
            out += b
        return out

    def request(s, method, url, headers=None, body=None, timeout=300):
        headers = headers or {}; s.log.append((method, url))
        host = url.split("/")[2]; path = url.split(host, 1)[1]; p, _, qs = path.partition("?")
        if "Authorization" in headers and host not in ("api.github.com", "uploads.github.com"): s.leak.append(url)
        key = (method, p)
        if s.fail_once.get(key):
            s.fail_once[key] -= 1; return 503, {}, b'{"message":"busy"}'
        if headers.get("Authorization") != "Bearer " + TOKEN: return 401, {}, b'{"message":"Bad credentials"}'
        j = lambda o, st=200: (st, {}, json.dumps(o).encode())
        if (method, p) == ("GET", f"/repos/{REPO}"): return j({"full_name": REPO, "default_branch": "main", "private": False, "permissions": {"push": True}})
        if method == "GET" and p.startswith(f"/repos/{REPO}/releases/tags/"):
            r = s.releases.get(p.rsplit("/", 1)[1]); return j(r) if r else j({"message": "Not Found"}, 404)
        if (method, p) == ("POST", f"/repos/{REPO}/releases"):
            d = json.loads(s._body(body)); s.next_id += 1
            r = {"id": s.next_id, "tag_name": d["tag_name"], "assets": [], "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/{s.next_id}/assets{{?name,label}}"}
            s.releases[d["tag_name"]] = r; return j(r, 201)
        if method == "GET" and p.endswith("/assets") and "/releases/" in p:
            rid = int(p.split("/")[-2]); r = [x for x in s.releases.values() if x["id"] == rid][0]; return j(r["assets"])
        if method == "POST" and host == "uploads.github.com":
            rid = int(p.split("/")[-2]); r = [x for x in s.releases.values() if x["id"] == rid][0]; name = qs.split("name=")[1]
            data = s._body(body)
            if int(headers["Content-Length"]) != len(data): return 400, {}, b'{"message":"length"}'
            s.next_id += 1; r["assets"].append({"id": s.next_id, "name": name, "size": len(data), "state": "uploaded"}); return j({"id": s.next_id}, 201)
        if method == "DELETE" and "/releases/assets/" in p:
            aid = int(p.rsplit("/", 1)[1])
            for r in s.releases.values(): r["assets"] = [a for a in r["assets"] if a["id"] != aid]
            return 204, {}, b""
        if method == "DELETE" and "/releases/" in p:
            rid = int(p.rsplit("/", 1)[1])
            for t in [t for t, r in s.releases.items() if r["id"] == rid]: del s.releases[t]
            return 204, {}, b""
        if method == "DELETE" and "/git/refs/tags/" in p: s.deleted_tags.append(p.rsplit("/", 1)[1]); return 204, {}, b""
        if (method, p) == ("POST", f"/repos/{REPO}/actions/workflows/render.yml/dispatches"):
            d = json.loads(s._body(body)); s.dispatches.append(d)
            s.runs.append({"id": 555, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "html_url": f"https://github.com/{REPO}/actions/runs/555"}); return 204, {}, b""
        if method == "GET" and p.endswith("/runs") and "workflows" in p: return j({"workflow_runs": s.runs})
        if method == "GET" and p == f"/repos/{REPO}/actions/runs/555":
            st = s.run_status.pop(0) if len(s.run_status) > 1 else s.run_status[0]; return j({"status": st[0], "conclusion": st[1]})
        if method == "GET" and p == f"/repos/{REPO}/actions/runs/555/jobs": return j({"jobs": [{"name": f"render ({k})", "status": "completed" if v else "in_progress", "conclusion": v} for k, v in s.jobs.items()]})
        if method == "GET" and p == f"/repos/{REPO}/actions/runs/555/artifacts": return j({"artifacts": s.artifacts.get(555, [])})
        if method == "DELETE" and "/actions/artifacts/" in p:
            aid = int(p.rsplit("/", 1)[1]); s.artifacts[555] = [a for a in s.artifacts[555] if a["id"] != aid]; return 204, {}, b""
        return 404, {}, b'{"message":"no route ' + p.encode() + b'"}'

    def download(s, url, dest, headers=None, timeout=600):
        headers = headers or {}; host = url.split("/")[2]
        if "Authorization" in headers and host not in ("api.github.com", "uploads.github.com"): s.leak.append(url)
        if host == "api.github.com":
            if headers.get("Authorization") != "Bearer " + TOKEN: return 401, {}
            return 302, {"location": "https://blob.example.net/signed/" + url.split("/")[-2]}
        with open(dest, "wb") as f: f.write(s.zips[int(url.rsplit("/", 1)[1])])
        return 200, {}


def enc_blob(n=300): return b"Salted__" + os.urandom(n)
def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items(): z.writestr(n, d)
    return b.getvalue()


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.fk = FakeGitHub(); self.sl = []
        os.environ["APPDATA"] = self.tmp
        self.gh = R.GH(REPO, TOKEN, transport=self.fk, sleep=self.sl.append, log=lambda *a: None)
        self.segs = os.path.join(self.tmp, "segs"); os.makedirs(self.segs)
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True)
    def bundle(self, **sizes):
        for n, sz in (sizes or {"r01": 300, "r02": 500, "code": 200}).items():
            with open(os.path.join(self.segs, n + ".tar.enc"), "wb") as f: f.write(b"Salted__" + b"x" * sz)

    # ---- token / config ----
    def test_token_missing_is_a_clear_error(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_RENDER_TOKEN", None)
            with self.assertRaises(R.GHError) as e: R.GH(REPO, None, transport=self.fk)
        self.assertIn("SET_GH_TOKEN.bat", str(e.exception))
    def test_get_token_reads_env_only_and_strips(self):
        self.assertEqual(R.get_token({"GH_RENDER_TOKEN": "  abc  "}), "abc"); self.assertIsNone(R.get_token({}))
    def test_bad_repo_name_refused(self):
        with self.assertRaises(R.GHError): R.GH("not a repo", TOKEN, transport=self.fk)

    # ---- check ----
    def test_check_reports_branch_and_permissions(self):
        r = self.gh.check(); self.assertEqual((r["repo"], r["default_branch"], r["permissions"]["push"]), (REPO, "main", True))
    def test_401_message_has_hint_and_no_token(self):
        gh = R.GH(REPO, "github_pat_WRONGWRONGWRONGWRONGWRONGWRONG", transport=self.fk, sleep=self.sl.append)
        with self.assertRaises(R.GHError) as e: gh.check()
        self.assertEqual(e.exception.status, 401); self.assertIn("SET_GH_TOKEN.bat", str(e.exception)); self.assertNotIn("WRONGWRONG", str(e.exception))
    def test_transient_503_is_retried(self):
        self.fk.fail_once[("GET", f"/repos/{REPO}")] = 2; self.assertEqual(self.gh.check()["repo"], REPO); self.assertEqual(len(self.sl), 2)
    def test_network_error_is_retried_then_reported(self):
        class Down:
            def request(s, *a, **k): raise urllib.error.URLError("down")
        gh = R.GH(REPO, TOKEN, transport=Down(), sleep=self.sl.append)
        with self.assertRaises(R.GHError) as e: gh.check()
        self.assertIn("network error", str(e.exception)); self.assertEqual(len(self.sl), 3)

    # ---- upload ----
    def test_upload_creates_release_and_assets(self):
        self.bundle(); r = self.gh.upload(self.segs, "ns-test")
        self.assertEqual(sorted(r["uploaded"]), ["code.tar.enc", "r01.tar.enc", "r02.tar.enc"]); self.assertEqual(r["skipped"], [])
        self.assertEqual(sorted(a["name"] for a in self.fk.releases["ns-test"]["assets"]), ["code.tar.enc", "r01.tar.enc", "r02.tar.enc"])
        self.assertEqual(self.fk.leak, [])
    def test_upload_is_resumable_same_size_skipped_changed_replaced(self):
        self.bundle(); self.gh.upload(self.segs, "ns-test")
        with open(os.path.join(self.segs, "r02.tar.enc"), "wb") as f: f.write(b"Salted__" + b"y" * 999)       # a re-made bundle of another size
        n_before = len([c for c in self.fk.log if c[0] == "POST" and "uploads.github.com" in c[1]])
        r = self.gh.upload(self.segs, "ns-test")
        self.assertEqual(r["uploaded"], ["r02.tar.enc"]); self.assertEqual(sorted(r["skipped"]), ["code.tar.enc", "r01.tar.enc"])
        self.assertEqual(len([c for c in self.fk.log if c[0] == "POST" and "uploads.github.com" in c[1]]) - n_before, 1)
        self.assertEqual([a["size"] for a in self.fk.releases["ns-test"]["assets"] if a["name"] == "r02.tar.enc"], [8 + 999])
    def test_upload_refuses_plaintext_and_bad_tag_and_empty_dir(self):
        with open(os.path.join(self.segs, "x.tar.enc"), "wb") as f: f.write(b"plain text, not encrypted")
        with self.assertRaises(R.GHError) as e: self.gh.upload(self.segs, "t")
        self.assertIn("not an encrypted bundle", str(e.exception)); self.assertEqual(self.fk.releases, {})
        with self.assertRaises(R.GHError): self.gh.upload(self.segs, "bad tag!")
        with self.assertRaises(R.GHError): self.gh.upload(self.tmp, "t")
    def test_upload_progress_callback(self):
        self.bundle(r01=50000, code=10); seen = []
        self.gh.upload(self.segs, "t", progress=lambda n, pc: seen.append((n, pc)))
        self.assertTrue(any(n == "r01.tar.enc" and pc == 100 for n, pc in seen))
    def test_upload_retries_transient_failure(self):
        self.bundle(r01=10, code=10); self.fk.fail_once[("POST", f"/repos/{REPO}/releases/101/assets")] = 0
        calls = {"n": 0}; real = self.fk.request
        def flaky(method, url, headers=None, body=None, timeout=300):
            if method == "POST" and "uploads.github.com" in url and calls["n"] == 0: calls["n"] += 1; self.fk._body(body); return 502, {}, b"{}"
            return real(method, url, headers, body, timeout)
        self.fk.request = flaky; r = self.gh.upload(self.segs, "t"); self.assertEqual(len(r["uploaded"]), 2)

    # ---- dispatch ----
    def test_dispatch_returns_run_id_and_sends_inputs(self):
        r = self.gh.dispatch("ns-test", "r01, r02")
        self.assertEqual(r["run_id"], 555); self.assertIn("/actions/runs/555", r["url"])
        self.assertEqual(self.fk.dispatches[0], {"ref": "main", "inputs": {"reels": "r01,r02", "bundle_tag": "ns-test"}})
        self.assertEqual(R.state()["run_id"], 555)
    def test_dispatch_rejects_bad_input_without_calling(self):
        for reels, tag in (("", "t"), ("r01;rm", "t"), ("r01", "bad tag")):
            with self.assertRaises(R.GHError): self.gh.dispatch(tag, reels)
        self.assertEqual(self.fk.dispatches, [])
    def test_dispatch_ignores_old_runs_and_times_out(self):
        self.fk.runs.append({"id": 1, "created_at": "2020-01-01T00:00:00Z"})
        real = self.fk.request
        self.fk.request = lambda m, u, h=None, b=None, t=300: (204, {}, b"") if m == "POST" and u.endswith("dispatches") else real(m, u, h, b, t)
        with self.assertRaises(R.GHError) as e: self.gh.dispatch("t", "r01", wait_for_run=0)
        self.assertIn("did not appear", str(e.exception))

    # ---- wait ----
    def test_wait_polls_until_completed_and_reports_jobs(self):
        self.fk.run_status = [("queued", None), ("in_progress", None), ("completed", "success")]; self.fk.jobs = {"r01": "success", "r02": "success"}; lines = []
        s = self.gh.wait(555, interval=1, on_update=lambda s, line: lines.append(line))
        self.assertEqual((s["status"], s["conclusion"]), ("completed", "success")); self.assertEqual(s["jobs"], {"r01": "success", "r02": "success"})
        self.assertEqual(len(lines), 3); self.assertEqual(self.sl, [1, 1])
    def test_wait_timeout(self):
        self.fk.run_status = [("in_progress", None)]; t = {"v": 0}
        def clock(): t["v"] += 100; return t["v"]
        s = self.gh.wait(555, timeout=250, interval=1, clock=clock); self.assertTrue(s.get("timeout"))
    def test_wait_failure_conclusion_returned(self):
        self.fk.run_status = [("completed", "failure")]; self.assertEqual(self.gh.wait(555)["conclusion"], "failure")

    # ---- fetch ----
    def art(self, files, aid=9001, expired=False):
        self.fk.artifacts.setdefault(555, []).append({"id": aid, "name": "x", "expired": expired}); self.fk.zips[aid] = make_zip(files)
    def test_fetch_downloads_without_leaking_token_and_cleans_up(self):
        self.bundle(); self.gh.upload(self.segs, "ns-test")
        b1, b2 = enc_blob(), enc_blob()
        self.art({"r01.out.tar.enc": b1}, 9001); self.art({"r02.out.tar.enc": b2}, 9002)
        out = os.path.join(self.tmp, "out"); r = self.gh.fetch(555, out, "ns-test", want=["r01", "r02"])
        self.assertEqual(r["got"], ["r01", "r02"]); self.assertEqual(r["missing"], [])
        with open(os.path.join(out, "r01.out.tar.enc"), "rb") as fh: self.assertEqual(fh.read(), b1)
        self.assertEqual(self.fk.leak, [], "the GitHub token must never be sent to the signed blob URL")
        self.assertEqual(self.fk.artifacts[555], []); self.assertNotIn("ns-test", self.fk.releases); self.assertEqual(self.fk.deleted_tags, ["ns-test"])
    def test_fetch_reports_missing_and_skips_plain_files_and_zip_paths(self):
        self.art({"../../evil/r01.out.tar.enc": enc_blob(), "r02.out.tar.enc": b"not encrypted at all", "notes.txt": b"x"})
        out = os.path.join(self.tmp, "out"); r = self.gh.fetch(555, out, None, keep=True, want=["r01", "r02"])
        self.assertEqual(r["got"], ["r01"]); self.assertEqual(r["missing"], ["r02"]); self.assertEqual(os.listdir(out), ["r01.out.tar.enc"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))
    def test_fetch_keeps_everything_when_nothing_downloaded(self):
        self.bundle(); self.gh.upload(self.segs, "ns-test"); self.art({"a.txt": b"x"})
        r = self.gh.fetch(555, os.path.join(self.tmp, "o"), "ns-test", want=["r01"])
        self.assertEqual(r["got"], []); self.assertIn("ns-test", self.fk.releases); self.assertEqual(len(self.fk.artifacts[555]), 1)
    def test_fetch_ignores_expired_artifacts(self):
        self.art({"r01.out.tar.enc": enc_blob()}, expired=True); r = self.gh.fetch(555, os.path.join(self.tmp, "o"), None, want=["r01"]); self.assertEqual(r["got"], [])
    def test_delete_release_removes_tag_even_without_release(self):
        self.assertFalse(self.gh.delete_release("nope")); self.assertEqual(self.fk.deleted_tags, ["nope"])

    # ---- cli ----
    def test_cli_check_and_run_output_never_contains_token(self):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = R.main(["check"], gh=self.gh); rc2 = R.main(["run", "t1", "r01"], gh=self.gh)
        self.assertEqual((rc, rc2), (0, 0)); self.assertNotIn(TOKEN, buf.getvalue()); self.assertIn("run 555", buf.getvalue())
    def test_cli_wait_exit_codes(self):
        import contextlib
        for conc, code in (("success", 0), ("failure", 1)):
            self.fk.run_status = [("completed", conc)]; R.state({"run_id": 555}); self.fk.jobs = {}
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(R.main(["wait", "--interval", "1"], gh=self.gh), code)


class RealTransport(unittest.TestCase):
    """the real urllib Transport against a local HTTP server: streamed upload with Content-Length, redirect not followed, no token on the redirect target"""
    def test_upload_stream_and_redirected_download(self):
        import http.server, threading
        seen = {"up": None, "blob_auth": "unset", "api_auth": None}; zbytes = make_zip({"r01.out.tar.enc": enc_blob()})
        class Hd(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                n = int(self.headers["Content-Length"]); data = self.rfile.read(n)
                seen["up"] = (self.path, n, len(data), self.headers.get("Content-Type"), self.headers.get("Authorization")); self.send_response(201); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
            def do_GET(self):
                if self.path.endswith("/zip"):
                    seen["api_auth"] = self.headers.get("Authorization"); self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/blob/x"); self.send_header("Content-Length", "0"); self.end_headers()
                else:
                    seen["blob_auth"] = self.headers.get("Authorization"); self.send_response(200); self.send_header("Content-Length", str(len(zbytes))); self.end_headers(); self.wfile.write(zbytes)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hd); threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"; tmp = tempfile.mkdtemp()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"; gh = R.GH(REPO, TOKEN, api=base, log=lambda *a: None)
            f = os.path.join(tmp, "r01.tar.enc")
            with open(f, "wb") as fh: fh.write(b"Salted__" + os.urandom(3 << 20))
            gh._put_asset(base + "/up", f, "r01.tar.enc", os.path.getsize(f), None)
            self.assertEqual(seen["up"][:3], ("/up?name=r01.tar.enc", os.path.getsize(f), os.path.getsize(f))); self.assertEqual(seen["up"][3], "application/octet-stream"); self.assertEqual(seen["up"][4], "Bearer " + TOKEN)
            dest = os.path.join(tmp, "a.zip"); gh._download_zip(9, dest)
            self.assertEqual(open(dest, "rb").read(), zbytes); self.assertEqual(seen["api_auth"], "Bearer " + TOKEN); self.assertIsNone(seen["blob_auth"])
        finally: srv.shutdown(); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__": unittest.main(verbosity=1)
