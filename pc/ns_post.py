#!/usr/bin/env python3
"""ns_post.py - posting plan for the Hub Review panel. Stdlib only.

build_plan(summary, ids, platforms, captions) -> posting_plan dict (same {platform: [reel ids]} shape as farm\\posting_plan.json + per-item detail)
schedule(plan, cfg)  -> (ok, message). Calls the Social Lamma API ONLY when scan\\lamma.json has a token AND an explicit "schedule_endpoint"
                        (no endpoint is known to be stable from the PC, so none is guessed). Otherwise the caller writes posting_plan.json for Claude
                        (Social_Lamma MCP: get_media_upload_url -> post) and nothing is sent anywhere.
"""
import os, re, json, urllib.request, urllib.error, datetime as dt

PLATFORMS = ("tiktok", "facebook", "youtube_shorts", "instagram")
RULE = "same source footage never twice on one platform; singles 15-30 s, stories 45-58 s (Shorts/Reels) or 61-90 s (TikTok/FB); every platform needs its own caption"


def build_plan(summary, ids=None, platforms=None, captions=None, brand="slade"):
    plats = [p for p in (platforms or PLATFORMS) if p in PLATFORMS] or list(PLATFORMS)
    caps = captions or {}; want = set(ids) if ids else None; items = []
    for r in summary.get("reels", []):
        if r.get("status") != "ok" or (want is not None and r["id"] not in want): continue
        cap = (caps.get(r["id"]) if isinstance(caps.get(r["id"]), str) else None) or r.get("caption") or ""
        items.append({"id": r["id"], "file": os.path.join(summary.get("out_dir", ""), r.get("file") or r["id"] + ".mp4"), "secs": r.get("secs"),
                      "hook": r.get("hook", ""), "caption": cap.strip(), "caption_is_placeholder": (not cap.strip()) or cap.strip() == (r.get("hook") or "").strip(),
                      "platforms": plats, "schedule": "auto_slot"})
    by = {p: [i["id"] for i in items] for p in plats}
    return {"v": 1, "brand": brand, "stream": summary.get("stream"), "game": summary.get("game_name"), "date": summary.get("date"), "folder": summary.get("out_dir"),
            "created": dt.datetime.now().isoformat(timespec="seconds"), "platforms": by, "items": items, "_rule": RULE,
            "_for_claude": "Per platform: lint_caption, then get_media_upload_url + upload each file, then post with schedule_at or auto_slot. Captions marked placeholder need real captions + hashtags first."}


def load_cfg(scan):
    try:
        with open(os.path.join(scan, "lamma.json"), encoding="utf-8-sig") as f: return json.load(f)
    except Exception: return {}


def can_schedule(cfg):
    return bool(cfg.get("token") and str(cfg.get("schedule_endpoint") or "").startswith("/"))


def schedule(plan, cfg, opener=urllib.request.urlopen, timeout=30):
    if not can_schedule(cfg): return False, "no Social Lamma token/endpoint on this PC: posting_plan.json written for Claude"
    if any(i.get("caption_is_placeholder") for i in plan["items"]): return False, "some captions are still placeholders: write them first"
    base = str(cfg.get("base") or "https://social.lammaonline.website").rstrip("/")
    rq = urllib.request.Request(base + cfg["schedule_endpoint"], json.dumps({"brandId": cfg.get("brandId") or None, "plan": plan}).encode("utf-8"),
                                {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["token"], "User-Agent": "slade-hub"}, method="POST")
    try:
        with opener(rq, timeout=timeout) as r: code = getattr(r, "status", 200)
    except urllib.error.HTTPError as e: return False, f"Social Lamma answered HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e: return False, "Social Lamma not reachable: " + type(e).__name__
    return (200 <= code < 300), f"Social Lamma accepted {len(plan['items'])} post(s) (HTTP {code})" if 200 <= code < 300 else f"Social Lamma answered HTTP {code}"
