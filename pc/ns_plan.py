#!/usr/bin/env python3
"""ns_plan.py - draft pro plan for the New stream chain (pure stdlib, no AI call of its own).

  ranked.json (CLIP_FARMER)  +  edits\\farmNN.plan.json (DIRECTOR, local Ollama)  +  transcript words
      -> pro_plan.json in the farm_pro format (PLAN_SCHEMA.md), with every rule of knowledge\\SLADE_EDIT_RULES.md that a machine can
         check applied and the rest listed in "review" so the (optional) Claude plan review only reads a short list.

  ns_plan.py <video.mp4> [--scripts DIR] [--reels 6] [--game tft] [--out plan.json]

The rules file is REQUIRED: it is hashed into plan["rules"] and a missing file stops the chain (a plan must never be made without it).
Also here: scribe_to_ens() converts an ElevenLabs Scribe json to the whisper-style .ens.json the rest of the pipeline reads.
"""
import os, re, sys, json, glob, hashlib, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_REL = os.path.join("knowledge", "SLADE_EDIT_RULES.md")
GAME_NAMES = {"tft": "TFT", "league": "League", "eafc": "EAFC", "fc26": "EAFC", "overcooked": "Overcooked", "daysgone": "Days Gone"}
DEFAULT_LAYOUT = {"facecam": [130, 16, 300, 190], "board": [240, 60, 1020, 740], "src_w": 1920, "src_h": 1080, "hide": [[1440, 0, 480, 95]],
                  "reel_view": "wide", "face_mode": "none"}
SFX_MAP = {"boom": "boom", "scratch": "record_scratch", "whoosh": "whoosh", "riser": "riser", "ding": "ding", "horn": "boom", "drop": "boom"}
RELIGIOUS = re.compile(r"church|choir|hallelujah|gospel|amen\b|prayer|\bpray|bible|quran|jesus|allahu|priest|rosary", re.I)
CUSS_AR = re.compile(r"^(و|ف|يا)?(احا|كس|كسم\w*|يلعن\w*|شرموط\w*|متناك\w*|خول|خولات)$|^(?:و|ف|يا)?اح+ا+$")
DOTS = re.compile(r"[.…]+")
EMOJI = re.compile("[←-⯿☀-➿\U0001F000-\U0001FAFF‍️]")
MEME_GAP = 1.5            # one visual meme at a time, >= 1.5 s apart (rules: Memes and SFX)
MEME_MAX_VIDEOS = 2       # a meme clip may appear in at most 2 videos per batch
REEL_MAX = 58.0           # shorts limit with room for the end card
REEL_MIN = 10.0


def rules_info(scripts):
    p = os.path.join(scripts, RULES_REL)
    if not os.path.isfile(p): raise FileNotFoundError("missing " + p + " - the plan generator must follow the edit rules")
    with open(p, "rb") as f: b = f.read()
    return {"file": RULES_REL.replace("\\", "/"), "sha256": hashlib.sha256(b).hexdigest()[:16], "bytes": len(b)}


# ---------------- transcript ----------------
def scribe_to_ens(j, gap=0.8, maxw=14):
    """ElevenLabs Scribe json ({"words":[{"text","start","end","type","speaker_id"}]}) -> whisper-style {"transcription":[{"offsets","text","words"}]}"""
    ws = [w for w in (j.get("words") or []) if w.get("type", "word") == "word" and str(w.get("text", "")).strip()]
    segs, cur = [], []
    for w in ws:
        if cur and (w["start"] - cur[-1]["end"] > gap or len(cur) >= maxw or w.get("speaker_id") != cur[-1].get("speaker_id")): segs.append(cur); cur = []
        cur.append(w)
    if cur: segs.append(cur)
    out = []
    for s in segs:
        out.append({"offsets": {"from": int(s[0]["start"] * 1000), "to": int(s[-1]["end"] * 1000)}, "text": " " + " ".join(w["text"].strip() for w in s),
                    "words": [{"w": w["text"].strip(), "t0": round(float(w["start"]), 3), "t1": round(float(w["end"]), 3), "caption_ok": True} for w in s]})
    return {"source": "elevenlabs-scribe", "transcription": out}


def load_words(scan, stem):
    """-> [(t0, t1, word)] absolute seconds. Prefers Scribe (agent\\<stem>.scribe.json), then <stem>.ens.json, then other whisper json."""
    sc = os.path.join(scan, "agent", stem + ".scribe.json")
    if os.path.isfile(sc):
        try:
            with open(sc, encoding="utf-8") as f: j = scribe_to_ens(json.load(f))
            w = _words_of(j)
            if w: return w
        except Exception: pass
    for suf in (".ens.json", ".wav.json", ".json"):
        p = os.path.join(scan, stem + suf)
        if not os.path.isfile(p): continue
        try:
            with open(p, encoding="utf-8-sig") as f: w = _words_of(json.load(f))
        except Exception: continue
        if w: return w
    return []


def _words_of(j):
    out = []
    for s in j.get("transcription", []):
        ws = s.get("words") or []
        if ws:
            for w in ws:
                if isinstance(w, dict) and w.get("t0") is not None and ("caption_ok" not in w or w["caption_ok"]): out.append((float(w["t0"]), float(w["t1"]), str(w["w"]).strip()))
        elif s.get("offsets") and str(s.get("text", "")).strip():      # plain whisper.cpp segment: spread the words evenly
            a, b = s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000; tk = s["text"].split()
            for i, t in enumerate(tk): out.append((a + (b - a) * i / len(tk), a + (b - a) * (i + 1) / len(tk), t))
    return [w for w in out if w[2]]


# ---------------- subs ----------------
def clean_text(t):
    t = EMOJI.sub("", DOTS.sub("", str(t))); return re.sub(r"\s+", " ", t).strip()


def pages(words, lo, hi, per=4, gap=0.5):
    """words in [lo, hi) -> subs pages (2-4 words, no dots) + bleep ranges for Arabic cusses (English swear words are never bleeped: rules)"""
    ws = [w for w in words if lo - 0.01 <= (w[0] + w[1]) / 2 < hi]
    subs, bleep, cur = [], [], []
    def flush():
        if cur: subs.append({"s": round(cur[0][0], 2), "e": round(cur[-1][1], 2), "text": " ".join(x[2] for x in cur)})
    for a, b, w in ws:
        w = clean_text(w)
        if not w: continue
        if CUSS_AR.match(re.sub(r"[^\w]", "", w)):
            bleep.append([round(a, 2), round(b, 2)]); w = "***"
        if cur and (len(cur) >= per or a - cur[-1][1] > gap or re.search(r"[?!؟]$", cur[-1][2])): flush(); cur.clear()
        cur.append((a, b, w))
    flush()
    return subs, bleep


# ---------------- plan ----------------
def newest_layout(scripts):
    for p in sorted(glob.glob(os.path.join(scripts, "pro_plan_*.json")), key=os.path.getmtime, reverse=True):
        try:
            with open(p, encoding="utf-8-sig") as f: j = json.load(f)
            if j.get("layout"): return j["layout"], j.get("set")
        except Exception: continue
    return None, None


def load_json(p, d=None):
    try:
        with open(p, encoding="utf-8-sig") as f: return json.load(f)
    except Exception: return d


def pick_clips(ranked, n):
    """best-first, no overlapping footage (the same footage never appears in two videos), 10-58 s"""
    out = []
    for c in sorted(ranked, key=lambda c: -float(c.get("reel_score", c.get("score", 0)))):
        if c.get("format") == "youtube": continue
        st = float(c["start"]); d = min(float(c.get("dur", 20)), REEL_MAX)
        if d < REEL_MIN: continue
        if any(st < o[1] - 1 and st + d > o[0] + 1 for o in [(float(x["start"]), float(x["start"]) + min(float(x.get("dur", 20)), REEL_MAX)) for x in out]): continue
        out.append(c)
        if len(out) >= n: break
    return out


def reel_from(c, rid, words, edits_dir, title_card):
    st = float(c["start"]); d = min(float(c.get("dur", 20)), REEL_MAX); en = round(st + d, 2); st = round(st, 2)
    ep = load_json(os.path.join(edits_dir, "farm%02d.plan.json" % int(c.get("rank", 0)))) if edits_dir else None
    dr = (ep or {}).get("director") or {}
    hook = clean_text(dr.get("hook") or c.get("hook") or c.get("title") or "")
    subs, bleep = pages(words, st, en)
    memes, sfx, overlays = [], [], []
    for o in (ep or {}).get("fx", []):
        t = float(o.get("t", o.get("t0", 0)))
        if o.get("op") == "meme" and o.get("id"):
            memes.append({"t": round(t, 2), "dur": round(float(o.get("d", 1.2)), 2), "id": o["id"], "src_t": round(st + t, 2), "mode": o.get("mode", "overlay"),
                          "want": clean_text((o.get("fallback") or {}).get("card") or ""), "duck": True})
        elif o.get("op") == "text_pop" and o.get("source") == "label" and t > 0.1:      # t=0 is the hook card
            overlays.append({"t": round(t, 2), "dur": round(float(o.get("d", 1.5)), 2), "text": clean_text(o.get("text", "")), "style": "label"})
    for s in (ep or {}).get("sfx", []):
        k = SFX_MAP.get(s.get("gen"))
        if k: sfx.append({"t": round(float(s.get("t", 0)), 2), "kind": k})
    r = {"id": rid, "in": st, "out": en, "keep": [[st, en]], "hook": hook, "title_card": title_card, "subs": subs, "highlight": [], "bleep": bleep,
         "memes": memes, "sfx": sfx, "overlays": overlays, "loop_end": True, "end_card": True, "rel_timebase": "source",
         "caption": hook, "_src": {"rank": c.get("rank"), "score": c.get("reel_score", c.get("score")), "category": c.get("category"), "director": bool(ep)}}
    return r


def apply_rules(reels):
    """mechanical rules from SLADE_EDIT_RULES.md. Fixes in place, returns the report [{id, level, msg}]"""
    rep = []
    used = {}
    for r in reels:
        rid = r["id"]; keep = []; seen = set(); last = -9.0
        for m in sorted(r["memes"], key=lambda m: m["t"]):
            key = m.get("id") or m.get("clip") or ""
            if RELIGIOUS.search(key + " " + m.get("want", "")): rep.append({"id": rid, "level": "fix", "msg": f"meme {key} dropped: religious (rules: culturally appropriate)"}); continue
            if key in seen: rep.append({"id": rid, "level": "fix", "msg": f"meme {key} dropped: repeats inside the video"}); continue
            if m["t"] - last < MEME_GAP: rep.append({"id": rid, "level": "fix", "msg": f"meme {key} dropped: closer than {MEME_GAP}s after the previous one ends"}); continue
            if used.get(key, 0) >= MEME_MAX_VIDEOS: rep.append({"id": rid, "level": "fix", "msg": f"meme {key} dropped: already in {MEME_MAX_VIDEOS} videos of this batch"}); continue
            seen.add(key); last = m["t"] + m["dur"]; used[key] = used.get(key, 0) + 1; keep.append(m)
        r["memes"] = keep
        if r.get("punch"): r.pop("punch"); rep.append({"id": rid, "level": "fix", "msg": "punch-ins removed (rules: no zoom-ins, show the full gameplay)"})
        dur = r["out"] - r["in"]
        if not 15 <= dur <= 30 and not 45 <= dur <= 58: rep.append({"id": rid, "level": "warn", "msg": f"length {dur:.0f}s: singles are 15-30 s, stories 45-58 s (Shorts/Reels) or 61-90 s (TikTok/FB)"})
        if not r["hook"]: rep.append({"id": rid, "level": "warn", "msg": "no hook text"})
        elif not 4 <= len(r["hook"].split()) <= 9: rep.append({"id": rid, "level": "warn", "msg": f"hook has {len(r['hook'].split())} words (4-9)"})
        if not r["subs"]: rep.append({"id": rid, "level": "warn", "msg": "no subtitles (no transcript words in this range): every spoken word must appear"})
        if r["bleep"]: rep.append({"id": rid, "level": "warn", "msg": f"{len(r['bleep'])} Arabic cuss word(s) bleeped: check them"})
        rep.append({"id": rid, "level": "todo", "msg": "caption is a placeholder (hook): Claude writes the real caption + hashtags"})
    return rep


def build_plan(video, scripts, scan=None, game="tft", n=6, farm=None):
    """-> (plan dict, report list). Raises FileNotFoundError when the rules file or ranked.json is missing."""
    scan = scan or os.path.join(scripts, "scan"); stem = os.path.splitext(os.path.basename(video))[0]
    rules = rules_info(scripts)
    farm = farm or os.path.join(scan, stem + ".farm")
    ranked = load_json(os.path.join(farm, "ranked.json"))
    if not ranked: raise FileNotFoundError("no ranked.json in " + farm + " (clip step not done)")
    words = load_words(scan, stem)
    lay = load_json(os.path.join(scan, "ns_layout.json")); st_set = None
    if not lay: lay, st_set = newest_layout(scripts)
    lay = lay or dict(DEFAULT_LAYOUT)
    gname = GAME_NAMES.get(str(game).lower(), str(game))
    tc = (f"{gname} · {st_set}" if st_set and gname == "TFT" else gname)
    reels = [reel_from(c, "r%02d" % (i + 1), words, os.path.join(farm, "edits"), tc) for i, c in enumerate(pick_clips(ranked, n))]
    rep = apply_rules(reels)
    if not words: rep.append({"id": "*", "level": "warn", "msg": "no transcript words found: subtitles are empty"})
    plan = {"source": video, "set": st_set or gname, "layout": lay, "version": "ns-draft", "reels": reels, "youtube": [],
            "post_order": [r["id"] for r in reels], "rules": rules, "draft": True, "game": str(game)}
    return plan, rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video"); ap.add_argument("--scripts", default=HERE); ap.add_argument("--reels", type=int, default=6)
    ap.add_argument("--game", default="tft"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    try: plan, rep = build_plan(a.video, a.scripts, game=a.game, n=a.reels)
    except FileNotFoundError as e: print("FAILED", e); return 1
    out = a.out or os.path.join(a.scripts, "scan", os.path.splitext(os.path.basename(a.video))[0] + ".ns", "plan.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f: json.dump(plan, f, ensure_ascii=False, indent=1)
    with open(os.path.splitext(out)[0] + ".review.json", "w", encoding="utf-8") as f: json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"plan: {len(plan['reels'])} reels -> {out} (rules {plan['rules']['file']} {plan['rules']['sha256']}); {sum(1 for x in rep if x['level'] == 'fix')} auto-fixes, {sum(1 for x in rep if x['level'] == 'warn')} warnings")
    return 0


if __name__ == "__main__": sys.exit(main())
