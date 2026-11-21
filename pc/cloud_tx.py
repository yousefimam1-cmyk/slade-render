#!/usr/bin/env python3
"""Transcribe a PC stream in the cloud (GitHub drive.yml: faster-whisper large-v3, 10-min parts in parallel), no GPU on the PC.
  python cloud_tx.py "<video.mp4>" [--game tft] [--wait 5400]
1. mic track (a:1 when OBS/SteelSeries split it, else a:0) -> 16 kHz mono FLAC -> RENDER_KEY-encrypted -> release tx-<stem>
2. commits jobs/drive/<stem>.json {"stem", "audio_tag", "game"} -> drive.yml runs
3. waits for <stem>.wav.json.enc on the release -> decrypts to scan\\<stem>.wav.json (NEW_STREAM then skips transcribing)
Needs GH_RENDER_TOKEN + RENDER_KEY (same as the render farm)."""
import argparse, base64, json, os, re, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import rf_pc, rf_cloud_pc
REPO = os.environ.get("SLADE_RENDER_REPO", "yousefimam1-cmyk/slade-render")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def n_audio(v):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", v], capture_output=True, text=True, creationflags=NOWIN)
    return len([x for x in r.stdout.split() if x.strip()])

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("video"); ap.add_argument("--game", default=""); ap.add_argument("--wait", type=int, default=5400); a = ap.parse_args()
    stem = os.path.splitext(os.path.basename(a.video))[0]; safe = re.sub(r"[^A-Za-z0-9._+-]", "_", stem); tag = "tx-" + safe
    tmp = tempfile.mkdtemp(prefix="ctx_"); flac = os.path.join(tmp, "audio.flac"); enc = flac + ".enc"
    tr = 1 if n_audio(a.video) >= 2 else 0
    print(f"STEP 1/3 mic audio (track a:{tr})", flush=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-map", f"0:a:{tr}", "-ac", "1", "-ar", "16000", "-c:a", "flac", flac], check=True, creationflags=NOWIN)
    rf_pc.enc_file(flac, enc)
    gh = rf_cloud_pc.GH(repo=REPO, log=print)
    gh.delete_release(tag); rel = gh.ensure_release(tag)
    up = rel["upload_url"].split("{")[0]; gh._put_asset(up, enc, "audio.flac.enc", os.path.getsize(enc), None)
    print(f"STEP 2/3 cloud job ({os.path.getsize(enc) // 1048576} MB uploaded)", flush=True)
    path = f"jobs/drive/{safe}.json"; body = json.dumps({"stem": safe, "audio_tag": tag, "game": a.game, "t": int(time.time())}).encode()
    st, cur = gh.call("GET", f"/repos/{REPO}/contents/{path}", ok=(200, 404), what="job lookup")
    put = {"message": f"cloud_tx: {safe}", "content": base64.b64encode(body).decode()}
    if st == 200 and cur: put["sha"] = cur["sha"]
    gh.call("PUT", f"/repos/{REPO}/contents/{path}", put, ok=(200, 201), what="job commit")
    print("STEP 3/3 waiting for the transcript", flush=True); t0 = time.time()
    want = safe + ".wav.json.enc"
    while time.time() - t0 < a.wait:
        time.sleep(60)
        assets = gh._assets(gh.release_by_tag(tag) or {"id": rel["id"]})
        hit = next((x for x in assets if x["name"] == want), None)
        print(f"  waiting {int((time.time() - t0) // 60)} min", flush=True)
        if not hit: continue
        dst = os.path.join(tmp, want); url = f"{gh.api}/repos/{REPO}/releases/assets/{hit['id']}"
        for hop in range(3):
            s_, hd = gh.t.download(url, dst, gh._h({"Accept": "application/octet-stream"}) if hop == 0 else {"User-Agent": "slade-hub-rf-cloud"}, timeout=600)
            if s_ in (301, 302, 303, 307, 308) and hd.get("location"): url = hd["location"]; continue
            break
        scan = os.path.join(HERE, "scan"); os.makedirs(scan, exist_ok=True); out = os.path.join(scan, stem + ".wav.json")
        rf_pc.dec_file(dst, out); gh.delete_release(tag)
        n = len(json.load(open(out, encoding="utf-8")).get("transcription", []))
        print(f"DONE cloud transcript: {n} lines -> {out}", flush=True); return 0
    print("FAILED: no transcript after the wait (see the drive.yml run on GitHub)"); return 1

if __name__ == "__main__": sys.exit(main())
