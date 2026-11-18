#!/usr/bin/env python3
"""Upload-once source cache, runner side: put the content-addressed media a bundle points to back in place.

  fetch_media.py <bundle dir> [--src DIR]

<bundle>/media.json (inside the encrypted bundle): {"v": 1, "files": [{"path": "pieces/x.mp4", "asset": "f-<40 hex>.enc", "size": N}, ...]}
Assets live in the long-lived release "src-cache" (env SRC_CACHE_TAG); each one is a single openssl "Salted__" file (RENDER_KEY), its name an
HMAC of the content made on the PC (it reveals nothing). Downloaded with ONE `gh release download` call (gh fetches in parallel), then decrypted
in parallel straight into <bundle>/<path>. --src DIR: take the assets from a folder instead (local tests).
No media.json (old bundle) -> nothing to do. Public stdout: "MEDIA <n> files from cache" / "MEDIA FAIL code=<x>".
"""
import json, os, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

ASSET_RE = re.compile(r"^f-[0-9a-f]{40}\.enc$")
PATH_RE = re.compile(r"^(pieces|memes|propack)/[^/\\]{1,200}$")
OSSL = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-pass", "env:RENDER_KEY"]


def plan(bundle):
    p = os.path.join(bundle, "media.json")
    if not os.path.isfile(p): return []
    with open(p, encoding="utf-8") as f: m = json.load(f)
    out = []
    for x in m.get("files") or []:
        a, rel = str(x.get("asset", "")), str(x.get("path", "")).replace("\\", "/")
        if not ASSET_RE.match(a) or not PATH_RE.match(rel) or rel.split("/", 1)[1] in (".", ".."): raise ValueError("bad media.json entry")
        out.append((a, rel, int(x.get("size") or -1)))
    return out


def fetch(bundle, src=None, repo=None, tag=None, par=None):
    items = plan(bundle)
    if not items: return 0
    names = sorted({a for a, _, _ in items}); tmp = tempfile.mkdtemp(prefix="media_")
    try:
        if src:
            for n in names: shutil.copy2(os.path.join(src, n), os.path.join(tmp, n))
        else:
            repo = repo or os.environ["GITHUB_REPOSITORY"]; tag = tag or os.environ.get("SRC_CACHE_TAG", "src-cache")
            cmd = ["gh", "release", "download", tag, "-R", repo, "-D", tmp, "--clobber"] + sum((["-p", n] for n in names), [])
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode: raise RuntimeError("download")
        for n in names:
            with open(os.path.join(tmp, n), "rb") as f:
                if f.read(8) != b"Salted__": raise RuntimeError("not_encrypted")

        def dec(it):
            a, rel, size = it; dst = os.path.join(bundle, *rel.split("/")); os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(os.path.join(tmp, a), "rb") as fi, open(dst + ".part", "wb") as fo:
                if subprocess.run(OSSL, stdin=fi, stdout=fo, stderr=subprocess.DEVNULL).returncode: raise RuntimeError("decrypt")
            if size >= 0 and os.path.getsize(dst + ".part") != size: raise RuntimeError("size")
            os.replace(dst + ".part", dst); return rel

        with ThreadPoolExecutor(par or max(2, os.cpu_count() or 2)) as ex: list(ex.map(dec, items))
        return len(items)
    finally: shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    args = sys.argv[1:]; src = None
    if "--src" in args: i = args.index("--src"); src = args[i + 1]; del args[i:i + 2]
    try: n = fetch(args[0], src); print(f"MEDIA {n} files from cache")
    except RuntimeError as e: print(f"MEDIA FAIL code={re.sub(r'[^a-z_]', '_', str(e).lower())[:30]}"); sys.exit(1)
    except Exception as e: print(f"MEDIA FAIL code={type(e).__name__.lower()[:30]}"); sys.exit(1)
