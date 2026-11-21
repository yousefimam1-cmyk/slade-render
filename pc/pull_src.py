#!/usr/bin/env python3
"""PC side of drive-src.yml: pull a Drive stream that GitHub re-packed as encrypted release pieces, join + verify.
  python pull_src.py <stem> [--out "C:\\Users\\youse\\Videos\\Content We Can Generate\\Drive"] [--keep-release]
Needs GH_RENDER_TOKEN (SET_GH_TOKEN.bat, same as the render farm; no gh CLI) and RENDER_KEY (SET_RENDER_KEY.bat). Puts <stem>.mp4 in --out and, if the cloud
transcript is there, <stem>.wav.json in Scripts\\scan\\ (NEW_STREAM then skips transcribing). Deletes the release after."""
import argparse, glob, hashlib, os, shutil, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import rf_pc, rf_cloud_pc
REPO = os.environ.get("SLADE_RENDER_REPO", "yousefimam1-cmyk/slade-render")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("stem"); ap.add_argument("--keep-release", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "Videos", "Content We Can Generate", "Drive")); a = ap.parse_args()
    tag = "src-" + a.stem; tmp = tempfile.mkdtemp(prefix="src_", dir=a.out if os.path.isdir(a.out) else None); os.makedirs(a.out, exist_ok=True)
    if shutil.disk_usage(a.out).free < 40 * 1024 ** 3: sys.exit("less than 40 GB free: stopping")
    gh = rf_cloud_pc.GH(repo=REPO, log=print)
    rel = gh.release_by_tag(tag)
    if not rel: sys.exit(f"no release {tag} (is drive-src done for this stream?)")
    for a_ in gh._assets(rel):
        dest = os.path.join(tmp, a_["name"])
        url = f"{gh.api}/repos/{REPO}/releases/assets/{a_['id']}"
        for hop in range(3):   # API answers 302 -> signed storage URL (must be fetched WITHOUT the token)
            st, hd = gh.t.download(url, dest, gh._h({"Accept": "application/octet-stream"}) if hop == 0 else {"User-Agent": "slade-hub-rf-cloud"}, timeout=3600)
            if st in (301, 302, 303, 307, 308) and hd.get("location"): url = hd["location"]; continue
            break
        if st != 200 or os.path.getsize(dest) != a_["size"]: sys.exit(f"download failed: {a_['name']} (HTTP {st})")
        print("got", a_["name"], a_["size"] // 1048576, "MB", flush=True)
    parts = sorted(glob.glob(os.path.join(tmp, a.stem + ".part*.enc")))
    if not parts: sys.exit("no pieces in the release")
    dst = os.path.join(a.out, a.stem + ".mp4"); h = hashlib.sha256()
    with open(dst, "wb") as fo:
        for p in parts:
            plain = p[:-4]; rf_pc.dec_file(p, plain); os.remove(p)
            with open(plain, "rb") as fi:
                for b in iter(lambda: fi.read(8 << 20), b""): h.update(b); fo.write(b)
            os.remove(plain); print("joined", os.path.basename(plain), flush=True)
    want = open(os.path.join(tmp, a.stem + ".sha256")).read().strip()
    if h.hexdigest() != want: sys.exit(f"CHECKSUM MISMATCH for {dst}: delete it and run again")
    tx = os.path.join(tmp, a.stem + ".wav.json.enc")
    if os.path.isfile(tx):
        scan = os.path.join(HERE, "scan"); os.makedirs(scan, exist_ok=True); rf_pc.dec_file(tx, os.path.join(scan, a.stem + ".wav.json")); print("cloud transcript ->", scan)
    shutil.rmtree(tmp, ignore_errors=True)
    if not a.keep_release: gh.delete_release(tag)
    print("DONE", dst)

if __name__ == "__main__": main()
