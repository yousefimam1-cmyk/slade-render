#!/usr/bin/env python3
"""Rebuild each unit's compact timeline (<unit>.mp4) from its stream-copied pieces.

units.json: {"fill": 24, "units": [{"file": "r04.mp4", "pieces": [{"name", "a", "b", "off"}, ...]}]}
Piece i is placed at `off` and lasts b-a s; between pieces `fill` s of black + silence (one silent track per audio track).
One piece -> the piece file is used as is (no re-encode). Several -> one mezzanine encode (libx264 veryfast crf 14, every
audio track kept in order) so farm_pro sees a normal source whose times match plan.json.
"""
import os, sys, json, subprocess

def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate", "-of", "json", p],
                       capture_output=True, text=True, check=True)
    ss = json.loads(r.stdout)["streams"]; v = next(s for s in ss if s["codec_type"] == "video")
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    if fr in ("0/0", ""): fr = v.get("r_frame_rate", "30/1")
    n, d = (fr.split("/") + ["1"])[:2]; fps = round(float(n) / float(d or 1), 3) if float(d or 1) else 30.0
    return dict(w=v["width"], h=v["height"], fps=fps if 1 < fps < 241 else 30.0, na=sum(s["codec_type"] == "audio" for s in ss))

def assemble(bdir, threads="0"):
    U = json.load(open(os.path.join(bdir, "units.json"))); fill = float(U.get("fill", 24))
    for u in U["units"]:
        out = os.path.join(bdir, u["file"]); ps = u["pieces"]
        if os.path.exists(out): continue
        files = [os.path.join(bdir, "pieces", p["name"]) for p in ps]
        if len(ps) == 1:
            os.link(files[0], out) if hasattr(os, "link") else os.replace(files[0], out)
            print(f"assemble: {u['file']} 1 piece (stream copy kept)", flush=True); continue
        P = probe(files[0]); na = P["na"]; fps = P["fps"]; fc = []; seq = []
        for i, (p, f) in enumerate(zip(ps, files)):
            d = p["b"] - p["a"]
            fc.append(f"[{i}:v:0]fps={fps},scale={P['w']}:{P['h']},setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=2,"
                      f"trim=duration={d:.3f},setpts=PTS-STARTPTS[v{i}]")
            seq.append(f"[v{i}]")
            for j in range(na):
                fc.append(f"[{i}:a:{j}]aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=duration={d:.3f},asetpts=PTS-STARTPTS[a{i}_{j}]")
                seq.append(f"[a{i}_{j}]")
            if i < len(ps) - 1:
                fc.append(f"color=c=black:s={P['w']}x{P['h']}:r={fps}:d={fill:.3f},format=yuv420p,setsar=1[fv{i}]"); seq.append(f"[fv{i}]")
                for j in range(na):
                    fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={fill:.3f}[fa{i}_{j}]"); seq.append(f"[fa{i}_{j}]")
        n = 2 * len(ps) - 1
        fc.append("".join(seq) + f"concat=n={n}:v=1:a={na}[v]" + "".join(f"[a{j}]" for j in range(na)))
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y"]
        for f in files: cmd += ["-i", f]
        cmd += ["-filter_complex", ";".join(fc), "-map", "[v]"] + sum((["-map", f"[a{j}]"] for j in range(na)), []) + \
               ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-g", str(int(round(fps))), "-pix_fmt", "yuv420p", "-threads", threads,
                "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", out + ".tmp.mp4"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode: print(f"assemble: {u['file']} FAILED\n{r.stderr[-1500:]}", flush=True); sys.exit(1)
        os.replace(out + ".tmp.mp4", out)
        tot = ps[-1]["off"] + ps[-1]["b"] - ps[-1]["a"]
        print(f"assemble: {u['file']} {len(ps)} pieces -> {tot:.1f}s @ {fps}fps, {na} audio tracks", flush=True)

if __name__ == "__main__": assemble(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "0")
