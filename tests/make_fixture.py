#!/usr/bin/env python3
"""Synthetic test fixture: <dir>/src.mp4 (1920x1080 30 fps, 2 s GOP like OBS, 2 audio tracks), plan.json (one single reel built
from pro_plan_v2's r01 shifted to the fixture clock + a 2-part compilation whose 2nd part has a > 28 s keep gap, so the cloud
path has to assemble pieces with filler), memes/manifest.json with Windows paths, fake meme wavs, a green-screen ProPack clip."""
import os, sys, json, copy, subprocess

D = os.path.abspath(sys.argv[1]); REAL = sys.argv[2] if len(sys.argv) > 2 else "/mnt/user-data/outputs/Scripts/pro_plan_v2.json"
os.makedirs(D, exist_ok=True)
def ff(*a): subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *a], check=True)

LEN = 130
if not os.path.isfile(os.path.join(D, "src.mp4")):
    ff("-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={LEN}", "-f", "lavfi", "-i", f"sine=f=330:d={LEN}:sample_rate=48000",
       "-f", "lavfi", "-i", f"sine=f=660:d={LEN}:sample_rate=48000,volume=0.5",
       "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
       "-c:a", "aac", "-ac", "2", os.path.join(D, "src.mp4"))

real = json.load(open(REAL, encoding="utf-8")); r01 = next(r for r in real["reels"] if r["id"] == "r01")
SH = 3689.62 - 12.0                                   # r01 'in' -> 12 s on the fixture clock
def shift(o, d):
    o = copy.deepcopy(o)
    for k in ("in", "out"):
        if k in o: o[k] = round(o[k] - d, 3)
    if o.get("keep"): o["keep"] = [[round(a - d, 3), round(b - d, 3)] for a, b in o["keep"]]
    for s in o.get("subs", []): s["s"], s["e"] = round(s["s"] - d, 3), round(s["e"] - d, 3)
    for k in ("memes", "sfx", "overlays"):
        for e in o.get(k, []):
            if "src_t" in e: e["src_t"] = round(e["src_t"] - d, 3)
    return o

t01 = shift(r01, SH); t01["id"] = "t01"
p1 = {k: copy.deepcopy(t01[k]) for k in ("in", "out", "keep", "subs", "memes", "sfx", "overlays")}; p1["label"] = "part 1"
p2 = shift({k: copy.deepcopy(r01[k]) for k in ("in", "out", "keep", "subs", "memes", "sfx", "overlays")}, 3689.62 - 70.0)   # same content at 70 s
# split part 2: first 2 keep ranges stay, the rest jump +35 s (forces 2 pieces + filler)
k = p2["keep"]; p2["keep"] = k[:2] + [[round(a + 35, 3), round(b + 35, 3)] for a, b in k[2:]]
cut = k[2][0]
for s in p2["subs"]:
    if s["s"] >= cut: s["s"], s["e"] = round(s["s"] + 35, 3), round(s["e"] + 35, 3)
for e in p2["memes"] + p2["sfx"] + p2["overlays"]:
    if e.get("src_t", 0) >= cut: e["src_t"] = round(e["src_t"] + 35, 3)
p2["out"] = round(p2["out"] + 35, 3); p2["label"] = "part 2"
tG = dict(id="tG", compilation=True, rel_timebase="part", transitions=["whip"], transition_dur=0.3, title_card=r01.get("title_card"),
          hook=r01.get("hook"), caption="test compilation caption", parts=[p1, p2], **{"in": p1["in"], "out": p2["out"]})
plan = {k: v for k, v in real.items() if k not in ("reels", "youtube", "post_order", "platforms")}
plan.update(source=os.path.join(D, "src.mp4"), reels=[t01, tG], youtube=[], meme_manifest=os.path.join(D, "memes", "manifest.json"))
json.dump(plan, open(os.path.join(D, "plan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# assets as they would sit on the PC
mem = os.path.join(D, "pc_memes"); pp = os.path.join(D, "pc_propack"); os.makedirs(os.path.join(D, "memes"), exist_ok=True); os.makedirs(mem, exist_ok=True); os.makedirs(pp, exist_ok=True)
ids = sorted({m["id"] for o in (t01, p1, p2) for m in o["memes"] if m.get("id")} | {"m_bruh"})
clips = sorted({m["clip"] for o in (t01, p1, p2) for m in o["memes"] if m.get("clip")})
for i, mid in enumerate(ids): ff("-f", "lavfi", "-i", f"sine=f={500 + 40 * i}:d=1.2:sample_rate=48000", "-ac", "2", os.path.join(mem, mid + ".wav"))
for c in clips: ff("-f", "lavfi", "-i", "color=c=0x00FF00:s=640x360:r=30:d=2,drawbox=x=220:y=100:w=200:h=160:color=red:t=fill", "-f", "lavfi", "-i", "sine=f=900:d=2",
                   "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", os.path.join(pp, c + ".mp4"))
man = {"v": 1, "items": [{"id": m, "kb": m, "file": f"C:\\Users\\user\\Videos\\Viral_Meme_SFX\\{m}.wav", "kind": "audio", "tags": m.split("_")[1:], "max_per_reel": 1} for m in ids]}
json.dump(man, open(os.path.join(D, "memes", "manifest.json"), "w"), indent=1)
print(f"fixture in {D}: {len(ids)} memes, {len(clips)} clips")
