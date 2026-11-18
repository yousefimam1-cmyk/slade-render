#!/usr/bin/env python3
"""ns_plan.py - draft pro plan for the New stream chain (pure stdlib, no AI call of its own).

  ranked.json (CLIP_FARMER)  +  edits\\farmNN.plan.json (DIRECTOR, local Ollama)  +  transcript words
      -> pro_plan.json in the farm_pro format (PLAN_SCHEMA.md), with every rule of knowledge\\SLADE_EDIT_RULES.md that a machine can
         check applied and the rest listed in "review" so the (optional) Claude plan review only reads a short list.

  ns_plan.py <video.mp4> [--scripts DIR] [--reels 0] [--game tft] [--out plan.json]

The rules file is REQUIRED: it is hashed into plan["rules"] and a missing file stops the chain (a plan must never be made without it).
Also here: scribe_to_ens() converts an ElevenLabs Scribe json to the whisper-style .ens.json the rest of the pipeline reads.
"""
import os, re, sys, json, glob, hashlib, argparse

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
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


MIN_LLM, MIN_VIRALITY = 7.0, 60.0   # Slade 2026-11-18: no fixed reel count - only clips that are really reel-worthy


# "جيم في 30 ثانية" is a separate series: a stream reel / episode hook must never borrow its name (the AI copied it from the examples)
NOT_STREAM_HOOK = re.compile(r"(30|تلاتين|ثلاثين)\s*ثاني|جيم في 30|game in 30", re.I)
SILENT_MAX, MIN_WORDS = 10.0, 6      # a reel with 10 s+ of nobody talking (or under 6 spoken words) is not reel-worthy


def talk_ok(words, st, en):
    """-> (ok, why): enough talking inside [st, en] for a stream reel"""
    ts = sorted((float(a), float(b)) for a, b, w in words if st <= float(a) < en and clean_text(w))
    if len(ts) < MIN_WORDS: return False, f"only {len(ts)} spoken words"
    edges = [st] + [x for ab in ts for x in ab] + [en]
    gap = max((ts[i + 1][0] - ts[i][1] for i in range(len(ts) - 1)), default=0.0)
    gap = max(gap, ts[0][0] - st, en - ts[-1][1])
    return (gap <= SILENT_MAX, f"{gap:.0f} s with nobody talking")


def worthy(c):
    """the clip ranker's own judgement: AI rating >= 7/10 and virality >= 60/100 (clips without those fields pass on rank score alone)"""
    try: llm = float(c.get("llm")) if c.get("llm") is not None else None
    except (TypeError, ValueError): llm = None
    try: vir = float(c.get("virality")) if c.get("virality") is not None else None
    except (TypeError, ValueError): vir = None
    if llm is None and vir is None: return float(c.get("reel_score", c.get("score", 0)) or 0) >= 1.0
    return (llm is None or llm >= MIN_LLM) and (vir is None or vir >= MIN_VIRALITY)


def pick_clips(ranked, n=0):
    """best-first, reel-worthy only, no overlapping footage (the same footage never appears in two videos), 10-58 s. n=0: no cap"""
    out = []
    for c in sorted(ranked, key=lambda c: -float(c.get("reel_score", c.get("score", 0)))):
        if c.get("format") == "youtube" or not worthy(c): continue
        st = float(c["start"]); d = min(float(c.get("dur", 20)), REEL_MAX)
        if d < REEL_MIN: continue
        if any(st < o[1] - 1 and st + d > o[0] + 1 for o in [(float(x["start"]), float(x["start"]) + min(float(x.get("dur", 20)), REEL_MAX)) for x in out]): continue
        out.append(c)
        if n and len(out) >= n: break
    return out


def reel_from(c, rid, words, edits_dir, title_card):
    st = float(c["start"]); d = min(float(c.get("dur", 20)), REEL_MAX); en = round(st + d, 2); st = round(st, 2)
    ep = load_json(os.path.join(edits_dir, "farm%02d.plan.json" % int(c.get("rank", 0)))) if edits_dir else None
    dr = (ep or {}).get("director") or {}
    hook = next((h for h in (clean_text(dr.get("hook") or ""), clean_text(c.get("hook") or ""), clean_text(c.get("title") or "")) if h and not NOT_STREAM_HOOK.search(h)), "")
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


def build_plan(video, scripts, scan=None, game="tft", n=6, farm=None, youtube=False, ai=True):
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
    talk_drop = []; picked = []
    for c in pick_clips(ranked, 0):
        ok, why = talk_ok(words, float(c["start"]), float(c["start"]) + min(float(c.get("dur", 20)), REEL_MAX)) if words else (True, "")
        if ok: picked.append(c)
        else: talk_drop.append(f"clip rank {c.get('rank')} dropped: {why}")
        if n and len(picked) >= n: break
    reels = [reel_from(c, "r%02d" % (i + 1), words, os.path.join(farm, "edits"), tc) for i, c in enumerate(picked)]
    rep = apply_rules(reels) + [{"id": "*", "level": "fix", "msg": m} for m in talk_drop]
    if not words: rep.append({"id": "*", "level": "warn", "msg": "no transcript words found: subtitles are empty"})
    yt = []
    if youtube:
        used = {}
        for r in reels:
            for m in r["memes"]: used[m["id"]] = used.get(m["id"], 0) + 1
        yt, yrep = build_youtube(words, ranked, os.path.join(farm, "edits"), game, st_set, scripts, os.path.join(scan, "yt_episodes.json"), stem, ai, used); rep += yrep
    plan = {"source": video, "set": st_set or gname, "layout": lay, "version": "ns-draft", "reels": reels, "youtube": yt,
            "post_order": [r["id"] for r in reels], "rules": rules, "draft": True, "game": str(game)}
    return plan, rep


# ---------------- YouTube episodes (one per game, no Claude) ----------------
YT_MIN_TALK = 180.0       # a game with < 3 min of speech is not an episode
YT_SPLIT_GAP = 150.0      # this much silence = between games (loading, queue, AFK)
YT_GG_GAP = 40.0          # a shorter silence ends a game when a GG / end cue comes just before it
YT_MAX_GAME = 55 * 60.0   # longer "games" (no cues) are split into ~40 min windows at the biggest silence
YT_WIN, YT_CH_WIN = 40 * 60.0, 270.0
YT_PIECE_JOIN, YT_PIECE_PAD = 20.0, 1.0
END_CUE = re.compile(r"^(gg|ggs|ggwp|جي ?جي)$", re.I)
SOCIALS = "Slade بيلعب {g} كل يوم live\nIG/FB: @sladewilsontv · Twitch: slaadewilson"
RIOT = {"TFT": "Not endorsed by Riot Games. TFT and Teamfight Tactics are trademarks of Riot Games, Inc.",
        "League": "Not endorsed by Riot Games. League of Legends is a trademark of Riot Games, Inc."}
HASHTAGS = {"TFT": "#TFT #TeamfightTactics #تي_اف_تي", "League": "#LeagueOfLegends #LoL #ليج"}
TAGS = {"TFT": ["TFT", "Teamfight Tactics", "TFT Set 18", "تي اف تي", "Slade", "SLADE PLAYS TFT", "TFT عربي", "Riot Games"]}
SHOP_CUE = [(re.compile(r"^(re)?roll(ing|down)?$|^رول$", re.I), "slot", "roll"), (re.compile(r"^hit$", re.I), "kaching3", "hit")]
CH_KEYS = [(re.compile(r"carousel|كاروسيل", re.I), "الـ Carousel"), (re.compile(r"augment", re.I), "Augment"),
           (re.compile(r"^(re)?roll|rolldown|رول", re.I), "Rolldown"), (re.compile(r"^level|لفل", re.I), "Level up"),
           (re.compile(r"streak", re.I), "الـ streak"), (re.compile(r"^(rod|bow|tear|sword|vest|cloak|belt|gloves|spat|pan)$", re.I), "الـ items"),
           (re.compile(r"^(gg|top)$", re.I), "آخر fight")]


def split_games(words, game="tft"):
    """[(t0, t1, w)] -> [(a, b)] game spans: long silences, or a GG cue just before a shorter silence; very long spans -> ~40 min windows"""
    W = sorted(words)
    if not W: return []
    spans, start = [], W[0][0]
    for i in range(len(W) - 1):
        gap = W[i + 1][0] - W[i][1]
        cue = any(END_CUE.match(re.sub(r"[^\w ]", "", w[2]).strip()) for w in W if W[i][1] - 90 <= w[0] <= W[i][1])
        if gap >= YT_SPLIT_GAP or (gap >= YT_GG_GAP and cue): spans.append((start, W[i][1])); start = W[i + 1][0]
    spans.append((start, W[-1][1])); out = []
    for a, b in spans:
        while b - a > YT_MAX_GAME:                       # no cues: cut at the biggest silence near a + 40 min
            lo, hi = a + YT_WIN - 300, a + YT_WIN + 300
            g = [(W[i + 1][0] - W[i][1], W[i][1], W[i + 1][0]) for i in range(len(W) - 1) if lo <= W[i][1] <= hi]
            cut = max(g, key=lambda x: (round(x[0], 1), -abs(x[1] - a - YT_WIN)))[1:] if g else (a + YT_WIN, a + YT_WIN)
            out.append((a, cut[0])); a = cut[1]
        out.append((a, b))
    return [(round(max(0.0, a - 2), 2), round(b + 2, 2)) for a, b in out]


def talk_secs(words, a, b):
    import talk_guard
    return sum(e - s for s, e in talk_guard.islands(sorted((w[0], w[1]) for w in words), a, b))


def kof(lab):
    m = re.match(r"\s*x(\d+(?:\.\d+)?)", lab or ""); return float(m.group(1)) if m else 1.0


def out_time(segs, t):
    """source time -> output time on the (speed-ramped) segment timeline; a time in a cut gap maps to the next segment start"""
    o = 0.0
    for a, b, lab in segs:
        k = kof(lab)
        if t < a: return o
        if t <= b: return o + (t - a) / k
        o += (b - a) / k
    return o


def yt_pieces(segs):
    """segments -> stream-copy cut spans (merged, padded): the CUT_YT pieces"""
    out = []
    for a, b, _ in sorted(segs):
        a, b = max(0.0, a - YT_PIECE_PAD), b + YT_PIECE_PAD
        if out and a - out[-1][1] < YT_PIECE_JOIN: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def yt_subs(words, lo, hi):
    subs, bleep = [], []
    pg, _ = pages(words, lo, hi)
    ws = [w for w in words if lo - 0.01 <= (w[0] + w[1]) / 2 < hi]; i = 0
    for p in pg:
        n = len(p["text"].split()); grp = []
        while i < len(ws) and len(grp) < n:
            a, b, w = ws[i]; i += 1; w = clean_text(w)
            if not w: continue
            cu = bool(CUSS_AR.match(re.sub(r"[^\w]", "", w)))
            if cu: bleep.append([round(a, 2), round(b, 2)])
            grp.append({"w": w, "s": round(a, 3), "e": round(b, 3), "cuss": cu, "src": w})
        if grp: subs.append({"s": grp[0]["s"], "e": grp[-1]["e"], "text": " ".join("***" if g["cuss"] else g["w"] for g in grp), "words": grp})
    return subs, bleep


def ai_rules_text(scripts):
    """few-shot examples (knowledge\\claude_examples_text.json: youtube_examples + lessons) + the rulebook (knowledge\\SLADE_AI_RULES.md)"""
    ex = load_json(os.path.join(scripts, "knowledge", "claude_examples_text.json"), {}) or {}
    try:
        with open(os.path.join(scripts, "knowledge", "SLADE_AI_RULES.md"), encoding="utf-8") as f: rb = f.read()
    except OSError: rb = ""
    return ex.get("youtube_examples") or [], ex.get("lessons") or [], rb


def yt_ai(scripts, gname, ep, windows, result_hint=""):
    """one 70B call per game -> {hook, word, story, chapters[len(windows)]} or None (no token / bad reply)"""
    try:
        import farm_cfai
        if not farm_cfai.available(): return None
        exs, lessons, rb = ai_rules_text(scripts)
        tx = "\n".join(f"[{i}] {w['text'][:700]}" for i, w in enumerate(windows))
        prompt = (f"Rulebook (hard rules):\n{rb[:7000]}\n\nLessons: {json.dumps(lessons, ensure_ascii=False)}\n\n"
                  f"Examples of finished episodes (match this format exactly):\n{json.dumps(exs, ensure_ascii=False)}\n\n"
                  f"Now write SLADE PLAYS {gname} EP{ep}. Transcript of the game in {len(windows)} windows (Egyptian Arabic, English game terms):\n{tx}\n\n"
                  "Reply JSON: {\"hook\": story hook for the title in Egyptian Arabic with English game terms, 4-9 words, true to the transcript, "
                  "\"word\": one English word for the episode and the thumbnail (like Tournament / Comebackable), "
                  "\"story\": two lines (\\n between) with real facts from the transcript only, never invent a placement, "
                  f"\"chapters\": exactly {len(windows)} short chapter names (2-5 words, one per window, all different, no dots, no emojis, no HP/stage numbers, nothing religious)}}")
        try: j = json.loads(farm_cfai.generate(prompt, max_tokens=900))
        except Exception as e:                       # too long / not JSON: one compact retry, and say why (it used to fail silently)
            print(f"  YouTube text AI: {type(e).__name__}: {str(e)[:160]} -> retrying with a shorter prompt", flush=True)
            prompt = (f"Rules (short): {rb[:2500]}\n\nExample: {json.dumps(exs[:1], ensure_ascii=False)[:2500]}\n\n" + prompt[prompt.index("Now write"):])
            j = json.loads(farm_cfai.generate(prompt, max_tokens=900))
        ch = [clean_text(x)[:45] for x in (j.get("chapters") or [])]
        if len(ch) != len(windows) or not all(ch) or len(set(ch)) != len(ch): ch = None
        hook = clean_text(j.get("hook") or ""); word = clean_text(j.get("word") or "").split(" ")[0][:20]
        story = "\n".join(clean_text(x) for x in str(j.get("story") or "").split("\n") if clean_text(x))[:400]
        if not hook or NOT_STREAM_HOOK.search(hook) or RELIGIOUS.search(hook + " " + story + " " + " ".join(ch or [])) or ".exe" in (hook + story).lower():
            print("  YouTube text AI: reply broke a rule (hook/religion/series name) -> rule-based text", flush=True); return None
        return {"hook": hook, "word": word or "Episode", "story": story, "chapters": ch}
    except Exception as e:
        print(f"  YouTube text AI failed ({type(e).__name__}: {str(e)[:160]}) -> rule-based text", flush=True); return None


def rule_chapters(windows):
    """no AI: a TFT keyword heard in the window, else its place in the game; first = البداية, the last window may be آخر fight"""
    names, used, n = [], set(), len(windows)
    for i, w in enumerate(windows):
        nm = "البداية" if i == 0 else ("آخر fight" if i == n - 1 else None)
        if not nm:
            for rx, lab in CH_KEYS[:-1]:
                if lab not in used and any(rx.search(t) for t in w["words"]): nm = lab; break
        if not nm: nm = "Early game" if i < n * 0.34 else ("Mid game" if i < n * 0.67 else "Late game")
        if nm in used: nm = f"{nm} {sum(1 for x in names if x.startswith(nm)) + 1}"
        used.add(nm); names.append(nm)
    return names


def episode_no(ep_file, key, game):
    """persistent per-game episode counter (scan\\yt_episodes.json): the same game of the same stream keeps its number on re-runs"""
    d = load_json(ep_file, {}) or {}; g = d.setdefault(game, {"next": 1, "keys": {}})
    if key not in g["keys"]: g["keys"][key] = g["next"]; g["next"] += 1
    try:
        with open(ep_file, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError: pass
    return g["keys"][key]


def protect_spans(memes):
    return [(float(m["src_t"]) - 0.4, float(m["src_t"]) + max(1.0, float(m.get("dur") or 1.5))) for m in memes]


def yt_memes(ranked, edits_dir, a, b, used):
    """AI-edit beats (farmNN.plan.json, clip-relative t) that fall inside [a, b) -> pro-plan YouTube memes (source time), edit rules applied"""
    cand = []
    for c in ranked or []:
        ep = load_json(os.path.join(edits_dir, "farm%02d.plan.json" % int(c.get("rank", 0)))) if edits_dir else None
        if not ep: continue
        st = float((ep.get("clip") or {}).get("start", c.get("start", 0)))
        for o in ep.get("fx", []):
            if o.get("op") != "meme" or not o.get("id"): continue
            t = st + float(o.get("t", o.get("t0", 0)))
            if not a <= t < b: continue
            fb = o.get("fallback") or {}; m = {"id": o["id"], "src_t": round(t, 2), "dur": round(float(o.get("d", 1.2)), 2), "want": clean_text(fb.get("card") or o["id"])}
            if fb.get("fx"): m["fx"] = fb["fx"][0]
            cand.append(m)
    keep, last, seen, rep = [], -9.0, set(), []
    for m in sorted(cand, key=lambda m: m["src_t"]):
        k = m["id"]
        if RELIGIOUS.search(k + " " + m["want"]): rep.append(f"meme {k} dropped: religious"); continue
        if k in seen or m["src_t"] - last < MEME_GAP or used.get(k, 0) >= MEME_MAX_VIDEOS: rep.append(f"meme {k} dropped: repeat / too close / used in {MEME_MAX_VIDEOS} videos"); continue
        seen.add(k); last = m["src_t"] + m["dur"]; used[k] = used.get(k, 0) + 1; keep.append(m)
    return keep, rep


def yt_shop(words, a, b):
    out = []
    for t0, t1, w in words:
        if not a <= t0 < b: continue
        ww = re.sub(r"[^\w]", "", w)
        for rx, kind, ev in SHOP_CUE:
            if rx.match(ww): out.append({"src_t": round(t1 + 0.05, 2), "kind": kind, "ev": ev, "why": f"said '{ww}'", "gain_db": -8}); break
    return out


def build_youtube(words, ranked, edits_dir, game="tft", set_name=None, scripts=HERE, ep_file=None, stem="", ai=True, used=None):
    """-> (youtube entries in the pro-plan format, report). One episode per game; talk_guard pacing; 70B text with a rules-only fallback."""
    import talk_guard
    gname = GAME_NAMES.get(str(game).lower(), str(game)); rep = []; out = []; used = used if used is not None else {}
    W2 = [(w[0], w[1]) for w in words]
    for gi, (a, b) in enumerate(split_games(words, game)):
        talk = talk_secs(words, a, b)
        if talk < YT_MIN_TALK: rep.append({"id": "*", "level": "fix", "msg": f"game {a:.0f}-{b:.0f} s skipped: only {talk:.0f} s of speech (< {YT_MIN_TALK:.0f})"}); continue
        memes, mrep = yt_memes(ranked, edits_dir, a, b, used)
        y = {"id": f"y{len(out) + 1}", "segments": [[a, b, "x4"]], "cold_open": None, "memes": memes, "shop": yt_shop(words, a, b),
             "music": False, "hud": False, "phase_cards": False, "speed_badge": True, "discord_audio": False, "overlays": [], "tracker": []}
        talk_guard.guard(y, sorted(W2 + protect_spans(memes)))           # talk 1x, short pauses kept, 2.5-12 s jump cuts, long silence one x4 burst; memes never cut
        segs = y["segments"]
        y["subs"], y["bleep"] = yt_subs(words, a, b)
        wins, t = [], a                                                   # chapter windows (~4.5 min of source each)
        while t < b - 30:
            e = min(b, t + YT_CH_WIN) if b - (t + YT_CH_WIN) > 90 else b
            ww = [w for w in words if t <= w[0] < e]
            wins.append({"a": t, "b": e, "text": " ".join(clean_text(w[2]) for w in ww), "words": [re.sub(r"[^\w ]", "", w[2]).strip() for w in ww]}); t = e
        key = f"{stem}#{int(a)}"; ep = episode_no(ep_file, key, gname) if ep_file else len(out) + 1
        txt = yt_ai(scripts, gname, ep, wins) if ai else None
        names = (txt or {}).get("chapters") or rule_chapters(wins)
        chs, last = [], -99.0
        for w, nm in zip(wins, names):
            s0 = next((s[0] for s in segs if s[1] > w["a"] and kof(s[2]) == 1), w["a"]); s0 = max(s0, w["a"])
            to = 0.0 if not chs else round(out_time(segs, s0), 1)
            T = out_time(segs, segs[-1][1])
            if chs and (to - last < 10 or to > T - 10 or nm in {c["name"] for c in chs}): continue
            chs.append({"t": to, "name": nm, "src_t": round(s0, 2)}); last = to
        top = [c for c in (ranked or []) if a <= float(c["start"]) < b]
        hook = (txt or {}).get("hook") or clean_text(next((c.get("title") for c in sorted(top, key=lambda c: -float(c.get("reel_score", c.get("score", 0)))) if c.get("title")), "") or "") or f"game {gi + 1} من الـ stream"
        word = (txt or {}).get("word") or "Full Game"
        story = (txt or {}).get("story") or f"{gname} game كاملة من الـ stream بدون ولا skip\nكل الـ fights والـ rolldowns من البداية للآخر"
        sn = set_name if set_name and gname.lower() in str(set_name).lower() else gname
        y["title"] = f"{sn} | {hook} | SLADE PLAYS {gname.upper()} EP{ep}"[:100]
        y["description"] = (HASHTAGS.get(gname, f"#{re.sub(r'[^A-Za-z0-9]', '', gname)} #gaming #Slade") + f"\n\nSLADE PLAYS {gname.upper()} | EP {ep}: {word}\n{story}\n\n"
                            + SOCIALS.format(g=gname) + ("\n\n" + RIOT[gname] if gname in RIOT else ""))
        y["tags"] = TAGS.get(gname, [gname, "Slade", f"SLADE PLAYS {gname.upper()}"])
        mid = max(segs, key=lambda s: (kof(s[2]) == 1, s[1] - s[0]))
        tt = next((float(c["start"]) + float(c.get("dur", 10)) / 2 for c in top), (mid[0] + mid[1]) / 2)
        if not any(s[0] <= tt <= s[1] for s in segs): tt = (mid[0] + mid[1]) / 2
        y["thumb"] = {"src_t": round(tt, 2), "text": f"SLADE PLAYS {gname.upper()} EP{ep}", "big": word}
        y["chapters"] = chs; y["ep"] = ep; y["out_name"] = f"{gname} EP{ep}"; y["game_span"] = [a, b]; y["text_source"] = "70b" if txt else "rules"
        y["pieces"] = yt_pieces(segs)
        rep += [{"id": y["id"], "level": "fix", "msg": m} for m in mrep]
        rep.append({"id": y["id"], "level": "warn" if not txt else "todo", "msg": f"EP{ep} {a:.0f}-{b:.0f} s, talk {talk / 60:.1f} min, {len(memes)} memes, text by {y['text_source']}"})
        out.append(y)
    return out, rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video"); ap.add_argument("--scripts", default=HERE); ap.add_argument("--reels", type=int, default=0, help="max reels (0 = every reel-worthy clip)")
    ap.add_argument("--game", default="tft"); ap.add_argument("--out"); ap.add_argument("--youtube", action="store_true", help="also plan one YouTube episode per game")
    a = ap.parse_args(argv)
    try: plan, rep = build_plan(a.video, a.scripts, game=a.game, n=a.reels, youtube=a.youtube)
    except FileNotFoundError as e: print("FAILED", e); return 1
    out = a.out or os.path.join(a.scripts, "scan", os.path.splitext(os.path.basename(a.video))[0] + ".ns", "plan.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f: json.dump(plan, f, ensure_ascii=False, indent=1)
    with open(os.path.splitext(out)[0] + ".review.json", "w", encoding="utf-8") as f: json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"plan: {len(plan['reels'])} reels, {len(plan['youtube'])} YouTube episodes -> {out} (rules {plan['rules']['file']} {plan['rules']['sha256']}); {sum(1 for x in rep if x['level'] == 'fix')} auto-fixes, {sum(1 for x in rep if x['level'] == 'warn')} warnings")
    return 0


if __name__ == "__main__": sys.exit(main())
