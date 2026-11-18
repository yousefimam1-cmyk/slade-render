"""pro plan -> new plan: Slade's talk is never sped up or skipped (rule 2026-11-18), speed-ups only on silent stretches, max x4; cold open removed if it repeats footage"""
import json, re, sys
PAD, JOIN, KEEP, MIN_FAST, MAXK, MAXOUT = 0.35, 1.6, 2.5, 12.0, 4.0, 6.0
def words(scribe):
    W = [w for w in json.load(open(scribe, encoding="utf-8"))["words"] if w.get("type", "word") == "word" and str(w.get("text", "")).strip()]
    return sorted((float(w["start"]), float(w["end"])) for w in W)
def islands(W, a, b):
    out = []
    for s, e in W:
        if e < a or s > b: continue
        s, e = max(a, s - PAD), min(b, e + PAD)
        if out and s - out[-1][1] < JOIN: out[-1][1] = max(out[-1][1], e)
        else: out.append([s, e])
    return out
def kof(lab):
    m = re.match(r"\s*x(\d+(?:\.\d+)?)", lab or ""); return float(m.group(1)) if m else 1.0
def guard(y, W):
    segs, out = y["segments"], []
    for s in segs:
        a, b, lab = float(s[0]), float(s[1]), (s[2] if len(s) > 2 else "")
        k = kof(lab); rest = re.sub(r"^\s*x\d+(?:\.\d+)?\s*\|?\s*", "", lab)
        if k <= 1: out.append([a, b, lab]); continue
        t = a
        for s0, e0 in islands(W, a, b) + [[b, b]]:
            gap = s0 - t
            if gap > 0:
                tag = (f" | {rest}" if rest else "")
                if gap < KEEP: out.append([t, s0, "x1" + tag])                       # short pause: real time
                elif gap < MIN_FAST: out.append([t, t + 0.7, "x1" + tag]); out.append([s0 - 0.3, s0, "x1" + tag])   # silent pause: jump cut, no speed-up
                else:                                                                   # long silence: one x4 burst, at most MAXOUT s on screen
                    kk = min(k, MAXK); L = min(gap, MAXOUT * kk)
                    out.append([s0 - L, s0, f"x{int(kk)}" + tag])
            if e0 > s0: out.append([s0, e0, "x1" + (f" | {rest}" if rest else "")])
            t = max(t, e0)
    m = []   # merge neighbours with the same label speed
    for x in out:
        if m and abs(m[-1][1] - x[0]) < 1e-6 and kof(m[-1][2]) == kof(x[2]): m[-1][1] = x[1]
        else: m.append(x)
    y["segments"] = [x for x in m if x[1] - x[0] > 0.05]
    sh, keep = sorted(y.get("shop", []), key=lambda e: float(e["src_t"])), []   # auto "shop bar" SFX: one per roll-down at most, never spammed
    for e in sh:
        auto = str(e.get("why", "")).startswith("shop bar")
        if auto and any(abs(float(e["src_t"]) - float(k["src_t"])) < 25 for k in keep): continue
        if not auto and any(abs(float(e["src_t"]) - float(k["src_t"])) < 4 for k in keep): continue
        keep.append(e)
    y["shop"] = keep
    co = y.get("cold_open")
    if co and any(float(s[0]) < float(co[1]) and float(s[1]) > float(co[0]) for s in y["segments"]): y.pop("cold_open")
    return y
def dur(y): return sum((s[1] - s[0]) / kof(s[2]) for s in y["segments"]) + ((y["cold_open"][1] - y["cold_open"][0]) if y.get("cold_open") else 0)
if __name__ == "__main__":
    plan_in, scribe, plan_out = sys.argv[1:4]
    P = json.load(open(plan_in, encoding="utf-8-sig")); W = words(scribe)
    for y in P["youtube"]:
        d0 = dur(y); n0 = len(y.get("shop", [])); guard(y, W)
        print(f"{y['id']}: {d0/60:.1f} min -> {dur(y)/60:.1f} min, {sum(kof(s[2])>1 for s in y['segments'])} speed-ups, shop sfx {n0} -> {len(y['shop'])}")
    json.dump(P, open(plan_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
