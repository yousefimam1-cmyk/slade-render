#!/usr/bin/env python3
r"""Segment prep for the cloud render farm (runs in the sandbox, not on GitHub).

Step 1  plan -> CUT_SEGMENTS.bat + plan_segs.json + needed_assets.json
  prep_segments.py cut <plan.json> [--source "C:\\...\\stream.mp4"] [--reels r01,rG1 | --mode asap|streaming] [--out DIR] [--pad 2]
  The .bat (run on the PC from the Scripts folder) stream-copies only the kept spans (+pad s, -map 0 -c copy, both audio
  tracks) to scan\\agent\\segs\\pieces\\ and copies the needed meme wavs / ProPack clips to scan\\agent\\segs\\assets\\.

Step 2  on the PC (the key never leaves it): put plan_segs.json in Scripts\scan\agent\segs\ and CUT_SEGMENTS.bat in Scripts\,
  then CLOUD_PREP.bat = CUT_SEGMENTS.bat + rf_pc.py bundle -> scan\agent\segs\<reel>.tar.enc (+ code.tar.enc)

Time rebasing: every unit (a reel, or a part of a compilation) is cut into pieces = its keep ranges padded by --pad and merged
when the padded gap is < FILL s (so real footage is kept wherever farm_pro would read through a gap). Pieces are laid on a
compact timeline with FILL s of black/silence between them (FILL + 2*pad >= farm_pro's 20 s group / BIG_JUMP threshold, so
cut grouping and crossfades are unchanged). The cloud job (cloud/assemble.py) rebuilds that timeline as <unit>.mp4.
A stream-copy cut with input -ss and no -avoid_negative_ts keeps an mp4 edit list, so piece time 0 == the requested start exactly.
"""
import os, re, sys, json, copy, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try: import farm_cloud as FC          # only needed for --mode asap|streaming (sandbox); the PC passes --reels and does without it
except ImportError: FC = None

FILL = 24.0
SFX_MEME = {"bruh": "m_bruh"}          # sfx kinds that farm_pro takes from the meme manifest

# ---------------- timeline ----------------
def unit_pieces(o, r, pad):
    a0 = float(o.get("in", r.get("in", 0))); a1 = float(o.get("out", r.get("out", 0)))
    keep = [(float(a), float(b)) for a, b in (o.get("keep") or [[a0, a1]])]
    lo = min([a0] + [k[0] for k in keep]); hi = max([a1] + [k[1] for k in keep])
    spans = sorted((max(0.0, a - pad), b + pad) for a, b in keep)
    spans[0] = (max(0.0, min(spans[0][0], lo - pad)), spans[0][1]); spans[-1] = (spans[-1][0], max(spans[-1][1], hi + pad))
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] < FILL: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    out, off = [], 0.0
    for a, b in merged:
        a, b = round(a, 3), round(b, 3); out.append(dict(a=a, b=b, off=round(off, 3))); off += (b - a) + FILL
    return out

def mapper(pieces):
    def m(t):
        t = float(t)
        for i, p in enumerate(pieces):
            if t < p["a"]:   # before this piece: clamp to the nearer edge (only gap / pre-roll times land here)
                if i == 0: return p["off"]
                q = pieces[i - 1]; return round(q["off"] + q["b"] - q["a"] if t - q["b"] < p["a"] - t else p["off"], 3)
            if t <= p["b"]: return round(t - p["a"] + p["off"], 3)
        q = pieces[-1]; return round(q["off"] + q["b"] - q["a"], 3)
    return m

def rebase_unit(o, r, plan, m):
    in_old = float(o.get("in", r.get("in", 0)))
    for k in ("in", "out"):
        if k in o: o[k] = m(o[k])
    if o.get("keep"): o["keep"] = [[m(a), m(b)] for a, b in o["keep"]]
    for s in o.get("subs") or []: s["s"], s["e"] = m(s["s"]), m(s["e"])
    if o.get("bleep"): o["bleep"] = [[m(a), m(b)] for a, b in o["bleep"]]
    src_tb = (o.get("rel_timebase") or r.get("rel_timebase") or plan.get("rel_timebase") or "output") == "source"
    in_new = m(in_old)
    for key in ("memes", "sfx", "overlays", "punch", "tracker"):
        for e in o.get(key) or []:
            if "src_t" in e: e["src_t"] = m(e["src_t"])
            if src_tb and "t" in e: e["t"] = round(m(in_old + float(e["t"])) - in_new, 3)
    if isinstance(o.get("thumb"), dict) and "src_t" in o["thumb"]: o["thumb"]["src_t"] = m(o["thumb"]["src_t"])

def needed_assets(r):
    memes, clips = [], []
    for o in [r] + list(r.get("parts") or []):
        for mm in o.get("memes") or []:
            if mm.get("id"): memes.append(mm["id"])
            if mm.get("clip"): clips.append(mm["clip"])
        for s in o.get("sfx") or []:
            if s.get("kind") in SFX_MEME: memes.append(SFX_MEME[s["kind"]])
    return dict(memes=list(dict.fromkeys(memes)), clips=list(dict.fromkeys(clips)))

def build(plan, reels, pad):
    """-> plan_segs dict: per reel {plan: rebased one-reel plan, units: [{file, pieces:[{name,a,b,off}]}], assets}"""
    base = {k: v for k, v in plan.items() if k not in ("reels", "youtube", "post_order")}
    out = dict(version=1, source=plan.get("source"), pad=pad, fill=FILL, reels={})
    cuts = {}                                            # piece name -> (a, b) (shared between rG1 / rG1s etc)
    for r0 in plan.get("reels", []):
        if r0["id"] not in reels: continue
        r = copy.deepcopy(r0); units = []
        targets = [(p, f"{r['id']}_p{i + 1}") for i, p in enumerate(r["parts"])] if r.get("compilation") and r.get("parts") else [(r, r["id"])]
        for o, name in targets:
            ps = unit_pieces(o, r, pad)
            for p in ps:
                key = f"s{int(round(p['a'] * 1000)):08d}_{int(round(p['b'] * 1000)):08d}"; p["name"] = key + ".mp4"; cuts[key] = (p["a"], p["b"])
            rebase_unit(o, r, plan, mapper(ps))
            o["source"] = name + ".mp4"; o["seg_base"] = ps[0]["a"]
            units.append(dict(file=name + ".mp4", pieces=ps))
        if r.get("compilation"):    # reel-level in/out only serve as part defaults; parts carry their own now
            r["in"], r["out"] = 0.0, max(u["pieces"][-1]["off"] + u["pieces"][-1]["b"] - u["pieces"][-1]["a"] for u in units)
        rp = dict(copy.deepcopy(base), reels=[r], youtube=[], source=units[0]["file"], meme_manifest="memes/manifest.json")
        out["reels"][r["id"]] = dict(plan=rp, units=units, assets=needed_assets(r))
    out["cuts"] = {k: list(v) for k, v in sorted(cuts.items(), key=lambda kv: kv[1])}
    return out

# ---------------- .bat ----------------
def write_bat(ps, path, source, extra=""):
    """extra: more ffmpeg output options for every cut (YouTube pieces: -avoid_negative_ts make_zero, the CUT_YT form rf_pc.py ytbundle expects)"""
    L = ["@echo off", "chcp 65001 >nul", "setlocal EnableExtensions",
         "rem generated by prep_segments.py: stream-copy cuts for the cloud render farm (no re-encode, both audio tracks)",
         "if not defined SCRIPTS set \"SCRIPTS=%~dp0\"",
         "cd /d \"%SCRIPTS%\"",
         f"if not defined SRC set \"SRC={source}\"",
         "if not defined MEMES set \"MEMES=%USERPROFILE%\\Videos\\Viral_Meme_SFX\"",
         "if not defined PROPACK set \"PROPACK=%USERPROFILE%\\Videos\\Slade OBS\\Meme Voices\\ProPack\"",
         "if defined SEGS_OUT (set \"OUT=%SEGS_OUT%\") else (set \"OUT=%SCRIPTS%scan\\agent\\segs\")",
         "if not exist \"%SRC%\" (echo source not found: %SRC% & echo set SRC=full path and run again & goto :fail)",
         "mkdir \"%OUT%\\pieces\" \"%OUT%\\assets\\memes\" \"%OUT%\\assets\\propack\" 2>nul",
         "set MISSING=0"]
    n = len(ps["cuts"])
    for i, (k, (a, b)) in enumerate(ps["cuts"].items()):
        L.append(f"echo [{i + 1}/{n}] cut {a:.2f}-{b:.2f}")
        L.append(f"if not exist \"%OUT%\\pieces\\{k}.mp4\" ffmpeg -nostdin -hide_banner -v error -y -ss {a:.3f} -to {b:.3f} -i \"%SRC%\" -map 0 -c copy{(' ' + extra) if extra else ''} \"%OUT%\\pieces\\{k}.mp4\" || goto :fail")
    memes = sorted({x for r in ps["reels"].values() for x in r["assets"]["memes"]})
    clips = sorted({x for r in ps["reels"].values() for x in r["assets"]["clips"]})
    L.append("echo copying %s meme wavs and %s ProPack clips" % (len(memes), len(clips)))
    L.append("if exist \"%SCRIPTS%memes\\manifest.json\" (copy /y \"%SCRIPTS%memes\\manifest.json\" \"%OUT%\\assets\\memes\\\" >nul) else (echo WARNING: memes\\manifest.json not found & set MISSING=1)")
    for mid in memes:
        L.append(f"if exist \"%MEMES%\\{mid}.wav\" (copy /y \"%MEMES%\\{mid}.wav\" \"%OUT%\\assets\\memes\\\" >nul) else (echo MISSING meme {mid} & set MISSING=1)")
    for c in clips:
        safe = re.sub(r"[%^&|<>\"]", "", c)
        L.append('set "F=0"')
        L.append(f"for /r \"%PROPACK%\" %%G in (*{safe}*) do (copy /y \"%%~fG\" \"%OUT%\\assets\\propack\\\" >nul & set \"F=1\")")
        L.append(f"if \"%F%\"==\"0\" (echo MISSING clip {safe} & set MISSING=1)")
    L += ["echo.", "echo done: %OUT%", "if \"%MISSING%\"==\"1\" echo some assets are missing (see above): those memes will be skipped in the cloud render",
          "echo next: CLOUD_PREP.bat bundles + encrypts these (or it already called this file)",
          "if not defined SLADE_HUB pause", "exit /b 0",
          ":fail", "echo FAILED", "if not defined SLADE_HUB pause", "exit /b 1"]
    open(path, "w", encoding="utf-8", newline="\r\n").write("\n".join(L) + "\n")

def cmd_cut(a):
    plan = json.load(open(a.plan, encoding="utf-8-sig"))
    ids = [r["id"] for r in plan.get("reels", [])]
    if a.reels: want = [x.strip() for x in a.reels.split(",") if x.strip()]
    elif a.mode:
        if FC is None: sys.exit("--mode needs farm_cloud.py next to this file; pass --reels instead")
        want = FC.split_plan(plan["reels"], a.mode)["cloud"]
    else: want = ids
    bad = [x for x in want if x not in ids]
    if bad: sys.exit(f"not in plan: {bad}")
    ps = build(plan, set(want), a.pad); src = a.source or plan.get("source")
    os.makedirs(a.out, exist_ok=True)
    json.dump(ps, open(os.path.join(a.out, "plan_segs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({k: v["assets"] for k, v in ps["reels"].items()}, open(os.path.join(a.out, "needed_assets.json"), "w"), indent=1)
    write_bat(ps, os.path.join(a.out, "CUT_SEGMENTS.bat"), src)
    tot = sum(b - a_ for a_, b in ps["cuts"].values())
    print(f"{len(want)} reels, {len(ps['cuts'])} pieces, {tot / 60:.1f} min of stream-copied footage -> {a.out}")
    for k, v in ps["reels"].items():
        print(f"  {k}: {len(v['units'])} unit(s), {sum(len(u['pieces']) for u in v['units'])} pieces, memes {len(v['assets']['memes'])}, clips {len(v['assets']['clips'])}")

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("cut"); p.add_argument("plan"); p.add_argument("--source"); p.add_argument("--reels"); p.add_argument("--mode", choices=["asap", "streaming"])
    p.add_argument("--out", default=os.path.join(HERE, "generated")); p.add_argument("--pad", type=float, default=2.0); p.set_defaults(f=cmd_cut)
    a = ap.parse_args(argv); return a.f(a) or 0

if __name__ == "__main__": sys.exit(main())
