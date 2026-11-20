#!/usr/bin/env python3
"""Google Drive -> runner, for the cloud stream pipeline (drive.yml).

  drive_fetch.py token                      print an access token (service account JSON in env GDRIVE_SA_KEY)
  drive_fetch.py meta <file_id>             print {"name","size","mimeType"} as JSON
  drive_fetch.py get <file_id> <out>        download (resumes on network errors, checks the size)
  drive_fetch.py seal <in> <out>            encrypt for the reviewer: random AES key wrapped with drive/review_pub.pem
  drive_fetch.py unseal <in> <out> <priv>   (reviewer side) decrypt
  drive_fetch.py words <tr_dir> <out.json>  merge faster-whisper parts -> whisper-style {"transcription":[...]} with echo/MSA cleanup

The service account only needs read access: share the Drive folder with its e-mail as Viewer.
"""
import base64, json, os, subprocess, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://www.googleapis.com/drive/v3/files/"


def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token():
    sa = json.loads(os.environ["GDRIVE_SA_KEY"])
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/drive.readonly",
                            "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    k = serialization.load_pem_private_key(sa["private_key"].encode(), None)
    sig = _b64(k.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{body}.{sig}"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=60) as r:
        return json.load(r)["access_token"]


def meta(fid, tok=None):
    tok = tok or token()
    q = urllib.parse.urlencode({"fields": "name,size,mimeType", "supportsAllDrives": "true"})
    req = urllib.request.Request(API + urllib.parse.quote(fid) + "?" + q, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)


def get(fid, out):
    tok = token(); m = meta(fid, tok); size = int(m.get("size") or 0)
    url = API + urllib.parse.quote(fid) + "?alt=media&supportsAllDrives=true"
    for attempt in range(8):
        have = os.path.getsize(out) if os.path.exists(out) else 0
        if size and have >= size: break
        if attempt and attempt % 3 == 0: tok = token()   # 1 h token: refresh on long downloads
        cmd = ["curl", "-fL", "--retry", "5", "--retry-delay", "5", "-sS", "-H", "Authorization: Bearer " + tok, "-C", "-", "-o", out, url]
        print(f"download {m.get('name')} {have // 1048576}/{size // 1048576} MB", flush=True)
        if subprocess.run(cmd).returncode == 0 and (not size or os.path.getsize(out) >= size): break
        time.sleep(10)
    got = os.path.getsize(out) if os.path.exists(out) else 0
    if size and got != size: sys.exit(f"download incomplete: {got} of {size} bytes")
    print(json.dumps({"name": m.get("name"), "size": got}))


def _ossl(*a, inp=None):
    r = subprocess.run(["openssl", *a], input=inp, capture_output=True)
    if r.returncode: sys.exit("openssl failed: " + r.stderr.decode()[:300])
    return r.stdout


def seal(src, out, pub=os.path.join(HERE, "review_pub.pem")):
    """format: 4-byte len + RSA-OAEP(aes key) + openssl aes-256-cbc pbkdf2 blob"""
    k = os.urandom(32).hex().encode()
    wk = _ossl("pkeyutl", "-encrypt", "-pubin", "-inkey", pub, "-pkeyopt", "rsa_padding_mode:oaep", inp=k)
    blob = _ossl("enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt", "-pass", "pass:" + k.decode(), "-in", src)
    with open(out, "wb") as f: f.write(len(wk).to_bytes(4, "big") + wk + blob)


def unseal(src, out, priv):
    d = open(src, "rb").read(); n = int.from_bytes(d[:4], "big")
    k = _ossl("pkeyutl", "-decrypt", "-inkey", priv, "-pkeyopt", "rsa_padding_mode:oaep", inp=d[4:4 + n]).decode()
    open(out, "wb").write(_ossl("enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-pass", "pass:" + k, inp=d[4 + n:]))


MSA = {"أنني", "انني", "إنني", "لقد", "سوف", "نعم", "هذا", "هذه", "الذي", "التي", "يسمونه", "سأموت", "أعتقد", "اعتقد", "لماذا", "ماذا", "ليس", "حسنا"}


def clean_words(ws, slack=.15):
    """drop engine echoes (same word again while overlapping in time) and formal-Arabic words Slade never says"""
    o = []
    for w in ws:
        t = w["w"].strip()
        if not t or t in MSA: continue
        if any(len(o) >= n and o[-n]["w"] == t and w["t0"] < o[-n]["t1"] + slack for n in (1, 2, 3, 4)): continue
        o.append(dict(w, w=t))
    return o


def words(tr_dir, out):
    segs = []
    for fn in sorted(os.listdir(tr_dir), key=lambda x: int("".join(c for c in x if c.isdigit()) or 0)):
        if fn.endswith(".json"): segs += json.load(open(os.path.join(tr_dir, fn), encoding="utf-8"))
    segs.sort(key=lambda s: s["s"]); outl = []
    for s in segs:
        ws = clean_words([{"t0": round(a, 2), "t1": round(b, 2), "w": w, "p": round(p, 3)} for a, b, w, p in s.get("w") or []])
        if ws: outl.append({"offsets": {"from": int(ws[0]["t0"] * 1000), "to": int(ws[-1]["t1"] * 1000)}, "text": " ".join(x["w"] for x in ws), "words": ws})
    json.dump({"source": "cloud-faster-whisper-large-v3", "transcription": outl}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{len(outl)} lines")


def selftest():
    ws = clean_words([{"t0": 0, "t1": .5, "w": "عدد"}, {"t0": .4, "t1": .9, "w": "عدد"}, {"t0": 1, "t1": 1.4, "w": "أنني"}, {"t0": 1.5, "t1": 2, "w": "هموت"},
                      {"t0": 2.6, "t1": 2.9, "w": "اه"}, {"t0": 3.2, "t1": 3.5, "w": "اه"}])
    assert [w["w"] for w in ws] == ["عدد", "هموت", "اه", "اه"], ws
    import tempfile
    d = tempfile.mkdtemp(); priv = os.path.join(d, "k.pem"); pub = os.path.join(d, "p.pem")
    _ossl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", priv); _ossl("pkey", "-in", priv, "-pubout", "-out", pub)
    src = os.path.join(d, "a.txt"); open(src, "w").write("سلام Slade")
    seal(src, src + ".enc", pub); unseal(src + ".enc", src + ".out", priv); assert open(src + ".out").read() == "سلام Slade"
    os.makedirs(os.path.join(d, "tr"))
    json.dump([{"s": 0, "e": 1, "t": "x", "w": [[0, .5, " يلا", .9], [.4, .8, "يلا", .9]]}], open(os.path.join(d, "tr", "tr_0.json"), "w"))
    words(os.path.join(d, "tr"), os.path.join(d, "o.json")); assert json.load(open(os.path.join(d, "o.json")))["transcription"][0]["text"] == "يلا"
    print("selftest OK")


if __name__ == "__main__":
    c, a = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if c == "token": print(token())
    elif c == "meta": print(json.dumps(meta(a[0]), ensure_ascii=False))
    elif c == "get": get(a[0], a[1])
    elif c == "seal": seal(a[0], a[1])
    elif c == "unseal": unseal(*a[:3])
    elif c == "words": words(a[0], a[1])
    elif c == "--selftest": selftest()
    else: sys.exit(__doc__)
