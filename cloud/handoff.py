#!/usr/bin/env python3
"""Direct hand-off: upload a finished video from the runner straight to Social Lamma (media upload only, never a post).

  handoff.py --file <mp4> --id <reel or vid> --out <handoff.json>

Token: env SOCIAL_LAMMA_TOKEN (repo secret). Empty -> "HANDOFF SKIP <id> reason=no_token", exit 3 (the caller keeps today's path).
Flow (same tools as the PC's social_post.py): get_media_upload_url -> PUT the parts (exact partSize, several at once) -> complete_media_upload.
<out> (travels ENCRYPTED with the outputs): {id, media_id, bytes, sha256, w, h, secs, filename}.
Public stdout: ONE line, ids/codes only: "HANDOFF OK <id> parts=N" | "HANDOFF FAIL <id> code=<x>" | "HANDOFF SKIP <id> reason=<x>".
Never prints the token, the media id, upload URLs or any caption/transcript text.
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

TOKEN_VAR = "SOCIAL_LAMMA_TOKEN"
BASE = (os.environ.get("SOCIAL_LAMMA_BASE") or "https://social.lammaonline.website").rstrip("/")
BRAND = os.environ.get("SOCIAL_LAMMA_BRAND") or "slade"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PAR = int(os.environ.get("HANDOFF_PARALLEL", "4"))


def norm_token(v):
    """the secret may hold the whole connector link (https://.../mcp/<token>): use the token part"""
    v = (v or "").strip(); m = re.search(r"/mcp/([A-Za-z0-9_-]{20,200})/?$", v)
    return m.group(1) if m else v


class HandoffError(Exception):
    def __init__(self, code): super().__init__(code); self.code = code


def http(method, url, headers, data=None, timeout=300):
    """-> (status, headers lower-case, body). HTTP errors are returned; network errors raise OSError/URLError."""
    rq = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r: return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


class Client:
    def __init__(self, tok, base=BASE, transport=None, sleep=time.sleep):
        self.tok, self.base, self.t, self.sleep, self.n = tok, base.rstrip("/"), transport or http, sleep, 0

    def call(self, tool, args):
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": tool, "arguments": args}}).encode()
        hd = {"Authorization": "Bearer " + self.tok, "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "slade-render"}
        code, raw = 0, b""
        for tr in range(4):
            try: code, _, raw = self.t("POST", self.base + "/mcp", hd, body, 120)
            except (urllib.error.URLError, OSError): code = -1
            if code in (-1, 429, 500, 502, 503, 504) and tr < 3: self.sleep(3 * (tr + 1)); continue
            break
        if code in (401, 403): raise HandoffError("token_refused")
        if code != 200: raise HandoffError(f"{tool}_http_{code}")
        try: j = json.loads(raw.decode("utf-8", "replace"))
        except ValueError: raise HandoffError(f"{tool}_not_json")
        if j.get("error"): raise HandoffError(f"{tool}_rpc_error")
        r = j.get("result") or {}; out = r.get("structuredContent")
        if out is None:
            try: out = json.loads(((r.get("content") or [{}])[0]).get("text") or "{}")
            except ValueError: out = {}
        if r.get("isError"): raise HandoffError(f"{tool}_refused")
        return out or {}


def probe(path):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", path],
                           capture_output=True, timeout=60)
        d = json.loads(p.stdout or b"{}"); st = (d.get("streams") or [{}])[0]
        return int(st.get("width") or 0), int(st.get("height") or 0), round(float((d.get("format") or {}).get("duration") or 0), 3)
    except Exception: return 0, 0, 0.0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def _etag(hd, raw):
    et = hd.get("etag")
    if not et:
        try: et = json.loads(raw.decode("utf-8", "replace")).get("etag")
        except Exception: et = None
    return et


def upload(client, path, filename=None, mime="video/mp4", parallel=PAR):
    """-> (media_id, n_parts). Raises HandoffError(code)."""
    size = os.path.getsize(path); t = client.t
    r = client.call("get_media_upload_url", {"brand": BRAND, "filename": filename or os.path.basename(path), "mime": mime, "size_bytes": size})
    try: up = r["uploads"][0]; mid = up["media_id"]; u = up["upload"]
    except (KeyError, IndexError, TypeError): raise HandoffError("bad_upload_url_reply")
    if u.get("kind") == "put":
        with open(path, "rb") as f: data = f.read()
        for tr in range(5):
            try: code, _, _ = t("PUT", u["url"], {"Content-Type": mime, "Content-Length": str(size)}, data, 1800)
            except (urllib.error.URLError, OSError): code = -1
            if 200 <= code < 300: return mid, 1
            client.sleep(2 * (tr + 1))
        raise HandoffError(f"put_http_{code}")
    ps = int(u["partSize"]); n_parts = int(u.get("partCount") or -(-size // ps))
    if n_parts < 1 or ps < 1: raise HandoffError("bad_part_size")

    def put(n):
        with open(path, "rb") as f: f.seek((n - 1) * ps); data = f.read(ps)
        url = u["partUrlTemplate"].replace("{partNumber}", str(n)).replace("{n}", str(n)); code = 0
        for tr in range(5):
            try: code, hd, raw = t("PUT", url, {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}, data, 900)
            except (urllib.error.URLError, OSError): code, hd, raw = -1, {}, b""
            et = _etag(hd, raw)
            if 200 <= code < 300 and et: return n, et
            if code == 409: raise HandoffError("upload_expired")
            client.sleep(2 * (tr + 1))
        raise HandoffError(f"part_http_{code}")

    with ThreadPoolExecutor(max(1, min(parallel, n_parts))) as ex: done = dict(ex.map(put, range(1, n_parts + 1)))
    client.call("complete_media_upload", {"media_id": mid, "parts": [{"partNumber": n, "etag": done[n]} for n in sorted(done)]})
    return mid, n_parts


def handoff(path, rid, out, token=None, client=None, filename=None):
    """-> (exit code, public line). 0 ok, 1 fail, 3 skipped (no token / no file)."""
    rid = rid if ID_RE.match(rid or "") else "x"
    tok = norm_token(token if token is not None else os.environ.get(TOKEN_VAR))
    if not tok and client is None: return 3, f"HANDOFF SKIP {rid} reason=no_token"
    if not os.path.isfile(path) or os.path.getsize(path) < 1000: return 3, f"HANDOFF SKIP {rid} reason=no_video"
    try:
        c = client or Client(tok)
        mid, n = upload(c, path, filename)
        w, h, secs = probe(path)
        rec = {"v": 1, "id": rid, "media_id": mid, "bytes": os.path.getsize(path), "sha256": sha256(path), "w": w, "h": h, "secs": secs,
               "filename": filename or os.path.basename(path), "brand": BRAND, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(out + ".tmp", "w", encoding="utf-8") as f: json.dump(rec, f, indent=1)
        os.replace(out + ".tmp", out)
        return 0, f"HANDOFF OK {rid} parts={n}"
    except HandoffError as e:
        code = re.sub(r"[^a-z0-9_]", "_", str(e.code).lower())[:40]
        return 1, f"HANDOFF FAIL {rid} code={code}"
    except Exception as e:                                   # never leak details (they could hold URLs); the type is enough
        return 1, f"HANDOFF FAIL {rid} code={type(e).__name__.lower()[:40]}"


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--file", required=True); ap.add_argument("--id", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--filename"); a = ap.parse_args(argv)
    rc, line = handoff(a.file, a.id, a.out, filename=a.filename)
    print(line, flush=True); return rc


if __name__ == "__main__": sys.exit(main())
