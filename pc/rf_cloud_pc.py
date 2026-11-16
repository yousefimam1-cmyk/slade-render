#!/usr/bin/env python3
"""rf_cloud_pc.py - the PC talks to the GitHub render farm itself (no Claude, no gh CLI). Python stdlib only.
Port of farm_cloud.py upload / run / wait / fetch to the GitHub REST API.

  rf_cloud_pc.py check                       token + repo access test (prints the repo and the permissions, never the token)
  rf_cloud_pc.py upload <bundle_dir> <tag>   create the release <tag>, upload every *.tar.enc (resumable: same name+size is skipped)
  rf_cloud_pc.py run <tag> <reel,reel,...>   dispatch render.yml, print the run id
  rf_cloud_pc.py wait [--run-id N]           poll the run until it finishes (exit 0 success, 1 failed, 2 timeout)
  rf_cloud_pc.py fetch <outdir> [--run-id N] [--tag T] [--keep]
                                             download the encrypted *.out.tar.enc artifacts, then delete artifacts + release + tag
  rf_cloud_pc.py delete <tag>                delete a release and its tag

Auth: fine-grained token scoped to the slade-render repo only (Actions: read+write, Contents: read+write), saved by SET_GH_TOKEN.bat as
the user env var GH_RENDER_TOKEN. The token is only ever sent to api.github.com / uploads.github.com and is never printed or logged.
Everything uploaded is already encrypted by rf_pc.py (openssl "Salted__" blobs); this module refuses to upload anything else.
Repo: env SLADE_RENDER_REPO (default yousefimam1-cmyk/slade-render). State (run id, tag): %APPDATA%\\SladeHub\\rf_cloud_state.json
"""
import os, re, sys, json, time, glob, shutil, zipfile, argparse, tempfile, urllib.error, urllib.parse, urllib.request

REPO = os.environ.get("SLADE_RENDER_REPO", "yousefimam1-cmyk/slade-render")
WORKFLOW = "render.yml"
API = "https://api.github.com"
TOKEN_VAR = "GH_RENDER_TOKEN"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
MAX_ASSET = 2 * 1024 ** 3 - 1          # GitHub release asset limit
MAGIC = b"Salted__"


class GHError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg); self.status = status


def state_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "SladeHub", "rf_cloud_state.json")


def state(update=None, path=None):
    p = path or state_path()
    try:
        with open(p, encoding="utf-8") as f: s = json.load(f)
    except Exception: s = {}
    if update is not None:
        s.update(update); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f: json.dump(s, f, indent=1)
        os.replace(p + ".tmp", p)
    return s


def get_token(env=None):
    """user env var; falls back to the saved Windows user variable (set after Hub started). Returns None when missing."""
    env = os.environ if env is None else env
    t = (env.get(TOKEN_VAR) or "").strip()
    if not t and os.name == "nt" and env is os.environ:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k: t = str(winreg.QueryValueEx(k, TOKEN_VAR)[0]).strip()
        except OSError: t = ""
    return t or None


# ---------------- transport (injectable for tests) ----------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


class Transport:
    """request(method, url, headers, body, redirect) -> (status, headers(dict, lower-case keys), bytes).
    body: None | bytes | a binary file object (its size must be in the Content-Length header). Never follows redirects itself."""
    def request(self, method, url, headers=None, body=None, timeout=300):
        rq = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        op = urllib.request.build_opener(_NoRedirect)
        try:
            with op.open(rq, timeout=timeout) as r: return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            data = e.read() if e.fp else b""
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data

    def download(self, url, dest, headers=None, timeout=600):
        """stream a GET to a file. -> (status, headers). A 3xx is returned to the caller (Location header)."""
        rq = urllib.request.Request(url, headers=headers or {})
        op = urllib.request.build_opener(_NoRedirect)
        try:
            with op.open(rq, timeout=timeout) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20); return r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}


class _Progress:
    """file wrapper: reports MB uploaded to a callback while http streams it"""
    def __init__(self, path, cb=None):
        self.f = open(path, "rb"); self.n = os.path.getsize(path); self.done = 0; self.cb = cb; self.last = -1
    def read(self, k=-1):
        b = self.f.read(k)
        if b:
            self.done += len(b); pc = self.done * 100 // max(1, self.n)
            if self.cb and pc != self.last: self.last = pc; self.cb(pc)
        return b
    def __len__(self): return self.n
    def close(self): self.f.close()


class GH:
    def __init__(self, repo=None, token=None, transport=None, sleep=time.sleep, log=print, api=API):
        self.repo = repo or REPO; self.token = token or get_token(); self.t = transport or Transport()
        self.sleep = sleep; self.log = log; self.api = api.rstrip("/")
        if not self.token: raise GHError("GH_RENDER_TOKEN is not set: run SET_GH_TOKEN.bat once")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo): raise GHError("bad repo name")
        self._branch = None

    # --- low level ---
    def _h(self, extra=None):
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "slade-hub-rf-cloud"}
        h.update(extra or {}); return h

    def _fail(self, st, data, what):
        try: msg = json.loads(data.decode("utf-8", "replace")).get("message", "")
        except Exception: msg = ""
        msg = (msg or "")[:200].replace(self.token, "***")
        hint = {401: "token rejected or expired: make a new one and run SET_GH_TOKEN.bat",
                403: "token lacks permission (needs Actions + Contents read/write on the repo) or rate limit hit",
                404: "not found: wrong repo name, or the token is not allowed to see the repo",
                422: "GitHub refused the request"}.get(st, "")
        raise GHError(f"{what} failed (HTTP {st}) {msg} {hint}".strip(), st)

    def call(self, method, path, body=None, ok=(200, 201, 204), what=None, retries=3, base=None):
        url = path if path.startswith("http") else (base or self.api) + path
        data = None; h = self._h()
        if body is not None: data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        last = None
        for i in range(retries):
            try: st, hd, out = self.t.request(method, url, h, data)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = GHError(f"{what or method + ' ' + path}: network error {type(e).__name__}"); self.sleep(2 * (i + 1)); continue
            if st in (500, 502, 503, 504): last = GHError(f"{what or path}: GitHub busy (HTTP {st})", st); self.sleep(2 * (i + 1)); continue
            if st in ok: return st, (json.loads(out.decode("utf-8", "replace")) if out and out[:1] in (b"{", b"[") else None)
            self._fail(st, out, what or f"{method} {path}")
        raise last or GHError("request failed")

    def get(self, path, what=None, **k): return self.call("GET", path, what=what, ok=(200,), **k)[1]

    # --- repo ---
    def check(self):
        j = self.get(f"/repos/{self.repo}", "repo check")
        self._branch = j.get("default_branch") or "main"
        return {"repo": j.get("full_name"), "private": j.get("private"), "default_branch": self._branch, "permissions": j.get("permissions") or {}}

    def branch(self):
        if not self._branch: self.check()
        return self._branch

    # --- releases ---
    def release_by_tag(self, tag):
        st, j = self.call("GET", f"/repos/{self.repo}/releases/tags/{tag}", ok=(200, 404), what="release lookup")
        return j if st == 200 else None

    def ensure_release(self, tag):
        r = self.release_by_tag(tag)
        if r: return r
        return self.call("POST", f"/repos/{self.repo}/releases", {"tag_name": tag, "name": tag, "prerelease": True, "target_commitish": self.branch(),
                         "body": "encrypted render bundles (temporary)"}, what="release create")[1]

    def delete_release(self, tag):
        r = self.release_by_tag(tag)
        if r: self.call("DELETE", f"/repos/{self.repo}/releases/{r['id']}", ok=(204, 404), what="release delete")
        self.call("DELETE", f"/repos/{self.repo}/git/refs/tags/{tag}", ok=(204, 404, 422), what="tag delete")
        return bool(r)

    def upload(self, bundle_dir, tag, progress=None):
        """-> {"release_id", "uploaded": [names], "skipped": [names]}. Resumable: an asset with the same name and size is not sent again."""
        if not TAG_RE.match(tag or ""): raise GHError("bad tag")
        files = sorted(glob.glob(os.path.join(bundle_dir, "*.tar.enc")))
        if not files: raise GHError(f"no *.tar.enc in {bundle_dir}")
        for f in files:
            if os.path.getsize(f) > MAX_ASSET: raise GHError(f"{os.path.basename(f)} is over 2 GB (release asset limit): render fewer reels per bundle")
            with open(f, "rb") as fh:
                if fh.read(8) != MAGIC: raise GHError(f"{os.path.basename(f)} is not an encrypted bundle: refusing to upload")
        rel = self.ensure_release(tag)
        have = {a["name"]: a for a in (rel.get("assets") or [])}
        if rel.get("id") and not have:
            have = {a["name"]: a for a in (self.get(f"/repos/{self.repo}/releases/{rel['id']}/assets?per_page=100", "asset list") or [])}
        up_url = (rel.get("upload_url") or "").split("{")[0] or f"https://uploads.github.com/repos/{self.repo}/releases/{rel['id']}/assets"
        sent, skipped = [], []
        for f in files:
            nm = os.path.basename(f); sz = os.path.getsize(f); a = have.get(nm)
            if a and a.get("size") == sz and a.get("state", "uploaded") == "uploaded": skipped.append(nm); continue
            if a: self.call("DELETE", f"/repos/{self.repo}/releases/assets/{a['id']}", ok=(204, 404), what="stale asset delete")
            self._put_asset(up_url, f, nm, sz, (lambda pc, n=nm: progress(n, pc)) if progress else None)
            sent.append(nm)
        state({"tag": tag, "repo": self.repo})
        return {"release_id": rel.get("id"), "uploaded": sent, "skipped": skipped}

    def _put_asset(self, up_url, path, name, size, cb):
        url = up_url + "?" + urllib.parse.urlencode({"name": name})
        last = None
        for i in range(3):
            body = _Progress(path, cb)
            try:
                st, hd, out = self.t.request("POST", url, self._h({"Content-Type": "application/octet-stream", "Content-Length": str(size)}), body, 3600)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = GHError(f"upload of {name}: network error {type(e).__name__}"); self.sleep(5 * (i + 1)); continue
            finally: body.close()
            if st in (200, 201): return
            if st in (500, 502, 503, 504): last = GHError(f"upload of {name}: GitHub busy (HTTP {st})", st); self.sleep(5 * (i + 1)); continue
            self._fail(st, out, f"upload of {name}")
        raise last or GHError("upload failed")

    # --- workflow ---
    def dispatch(self, tag, reels, wait_for_run=90):
        """-> {"run_id", "url"}"""
        reels = [x.strip() for x in (reels.split(",") if isinstance(reels, str) else reels) if str(x).strip()]
        if not reels or not all(ID_RE.match(x) for x in reels) or not TAG_RE.match(tag or ""): raise GHError("bad reels/tag")
        t0 = time.time()
        self.call("POST", f"/repos/{self.repo}/actions/workflows/{WORKFLOW}/dispatches",
                  {"ref": self.branch(), "inputs": {"reels": ",".join(reels), "bundle_tag": tag}}, ok=(204,), what="workflow dispatch")
        deadline = time.time() + wait_for_run
        while True:
            j = self.get(f"/repos/{self.repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=10", "run list") or {}
            for x in j.get("workflow_runs", []):
                ts = _ts(x.get("created_at"))
                if ts is not None and ts >= t0 - 15:
                    state({"tag": tag, "run_id": x["id"], "reels": reels, "repo": self.repo})
                    return {"run_id": x["id"], "url": x.get("html_url") or f"https://github.com/{self.repo}/actions/runs/{x['id']}"}
            if time.time() > deadline: raise GHError("dispatched, but the run did not appear (check the Actions tab of the repo)")
            self.sleep(3)

    def status(self, run_id):
        j = self.get(f"/repos/{self.repo}/actions/runs/{run_id}", "run status")
        jobs = (self.get(f"/repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100", "run jobs") or {}).get("jobs", [])
        return {"status": j.get("status"), "conclusion": j.get("conclusion"),
                "jobs": {x["name"].split("(")[-1].rstrip(")"): (x.get("conclusion") or x.get("status")) for x in jobs}}

    def wait(self, run_id, timeout=3 * 3600, interval=30, on_update=None, clock=time.time):
        """-> final status dict (+ "timeout": True when the run was still going)"""
        end = clock() + timeout; last = None
        while True:
            s = self.status(run_id)
            line = f"{s['status']}: " + " ".join(f"{k}={v}" for k, v in sorted(s["jobs"].items()))
            if line != last:
                last = line
                if on_update: on_update(s, line)
            if s["status"] == "completed": return s
            if clock() > end: s["timeout"] = True; return s
            self.sleep(interval)

    # --- results ---
    def fetch(self, run_id, outdir, tag=None, keep=False, want=None):
        """artifacts -> <outdir>/<reel>.out.tar.enc (still encrypted); then delete artifacts + release (+tag) unless keep.
        -> {"got": [reel ids], "missing": [reel ids]}"""
        os.makedirs(outdir, exist_ok=True)
        arts = (self.get(f"/repos/{self.repo}/actions/runs/{run_id}/artifacts?per_page=100", "artifact list") or {}).get("artifacts", [])
        arts = [a for a in arts if not a.get("expired")]
        got = []; tmp = tempfile.mkdtemp(prefix="rf_art_")
        try:
            for a in arts:
                z = os.path.join(tmp, f"a{a['id']}.zip")
                self._download_zip(a["id"], z)
                with zipfile.ZipFile(z) as zf:
                    for n in zf.namelist():
                        b = os.path.basename(n)
                        if not b.endswith(".out.tar.enc"): continue                  # basename only: no path tricks from the zip
                        with zf.open(n) as src:
                            head = src.read(8)
                            if head != MAGIC: self.log("skipping non-encrypted file " + b); continue
                            dst = os.path.join(outdir, b)
                            with open(dst + ".part", "wb") as f:
                                f.write(head); shutil.copyfileobj(src, f, 1 << 20)
                            os.replace(dst + ".part", dst); got.append(b[:-len(".out.tar.enc")])
                os.remove(z)
        finally: shutil.rmtree(tmp, ignore_errors=True)
        want = set(want if want is not None else (state().get("reels") or []))
        out = {"got": sorted(set(got)), "missing": sorted(want - set(got))}
        if got and not keep:
            for a in arts: self.call("DELETE", f"/repos/{self.repo}/actions/artifacts/{a['id']}", ok=(204, 404), what="artifact delete")
            tag = tag or state().get("tag")
            if tag: self.delete_release(tag)
            out["cleaned"] = True
        return out

    def _download_zip(self, art_id, dest):
        url = f"{self.api}/repos/{self.repo}/actions/artifacts/{art_id}/zip"
        for i in range(3):
            try:
                st, hd = self.t.download(url, dest, self._h())
                if st in (301, 302, 303, 307, 308) and hd.get("location"):      # signed blob URL: must NOT get the GitHub token
                    st, hd = self.t.download(hd["location"], dest, {"User-Agent": "slade-hub-rf-cloud"})
            except (urllib.error.URLError, OSError, TimeoutError):
                self.sleep(3 * (i + 1)); continue
            if st == 200: return
            if st in (500, 502, 503, 504): self.sleep(3 * (i + 1)); continue
            self._fail(st, b"", f"artifact {art_id} download")
        raise GHError(f"artifact {art_id} download failed")


def _ts(s):
    try: return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception: return None


# ---------------- CLI ----------------
def main(argv=None, gh=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check")
    p = sp.add_parser("upload"); p.add_argument("bundle_dir"); p.add_argument("tag")
    p = sp.add_parser("run"); p.add_argument("tag"); p.add_argument("reels")
    p = sp.add_parser("wait"); p.add_argument("--run-id"); p.add_argument("--timeout", type=int, default=3 * 3600); p.add_argument("--interval", type=int, default=30)
    p = sp.add_parser("fetch"); p.add_argument("outdir"); p.add_argument("--run-id"); p.add_argument("--tag"); p.add_argument("--keep", action="store_true")
    p = sp.add_parser("delete"); p.add_argument("tag")
    a = ap.parse_args(argv)
    try:
        g = gh or GH()
        if a.cmd == "check":
            r = g.check(); print(f"OK repo {r['repo']} (branch {r['default_branch']}), permissions: {r['permissions']}"); return 0
        if a.cmd == "upload":
            r = g.upload(a.bundle_dir, a.tag, progress=lambda n, pc: print(f"upload {n} {pc}%", flush=True) if pc % 10 == 0 else None)
            print(f"uploaded {len(r['uploaded'])}, already there {len(r['skipped'])} -> release {a.tag}"); return 0
        if a.cmd == "run":
            r = g.dispatch(a.tag, a.reels); print(f"run {r['run_id']}: {r['url']}"); return 0
        rid = getattr(a, "run_id", None) or state().get("run_id")
        if a.cmd == "wait":
            if not rid: print("no run id (use run first)"); return 1
            s = g.wait(rid, a.timeout, a.interval, on_update=lambda s, line: print(time.strftime("%H:%M:%S"), line, flush=True))
            if s.get("timeout"): print("timeout (run still going)"); return 2
            print("conclusion:", s["conclusion"]); return 0 if s["conclusion"] == "success" else 1
        if a.cmd == "fetch":
            if not rid: print("no run id"); return 1
            r = g.fetch(rid, a.outdir, a.tag, a.keep)
            print(f"{len(r['got'])} encrypted outputs in {a.outdir}" + (f"; MISSING: {','.join(r['missing'])}" if r["missing"] else "")); return 0 if r["got"] else 1
        if a.cmd == "delete":
            print("deleted" if g.delete_release(a.tag) else "no such release"); return 0
    except GHError as e:
        print("FAILED " + str(e)); return 1


if __name__ == "__main__": sys.exit(main())
