#!/usr/bin/env python3
"""Slade render farm, cloud side driver (runs in the sandbox with `gh`).

  farm_cloud.py split <plan.json> asap|streaming print which reels the PC and the cloud render
  farm_cloud.py upload <bundle_dir> <tag>       create the release <tag>, upload *.tar.enc
  farm_cloud.py run <tag> <reels>               dispatch render.yml (reels = comma list)
  farm_cloud.py wait [--timeout S]              poll the dispatched run until it finishes
  farm_cloud.py fetch <outdir> [--keep]         download the encrypted *.out.tar.enc artifacts, then delete artifacts + release

This side never sees RENDER_KEY: it only moves encrypted blobs (openssl "Salted__" format) between the PC and GitHub.
Encryption/decryption happen on the PC (rf_pc.py) and in the workflow (repo secret).
Repo: env SLADE_RENDER_REPO (default yousefimam1-cmyk/slade-render).
"""
import os, re, sys, json, time, glob, shutil, argparse, tempfile, subprocess

REPO = os.environ.get("SLADE_RENDER_REPO", "yousefimam1-cmyk/slade-render")
WORKFLOW = "render.yml"
STATE = os.path.expanduser(os.environ.get("SLADE_RENDER_STATE", "~/.slade_render_state.json"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
MAX_ASSET = 2 * 1024 ** 3 - 1          # GitHub release asset limit

# ---------------- ASAP / streaming split ----------------
SHORT_DEFAULT = ("r01", "r03", "r04", "r05", "r06")
CLOUD_DEFAULT = ("rG1", "rG1s", "rG2", "rG2s")

def split_plan(reels, mode):
    """reels: list of reel dicts (from the plan) or ids. -> {"local": [...ids], "cloud": [...ids]}
    asap      : PC renders the short single-clip reels, cloud renders the compilations (rG*), all in parallel
    streaming : PC is busy streaming, cloud renders everything"""
    mode = (mode or "").lower()
    if mode not in ("asap", "streaming"): raise ValueError("mode must be asap or streaming")
    ids, comp = [], {}
    for r in reels:
        rid = r["id"] if isinstance(r, dict) else str(r)
        ids.append(rid)
        comp[rid] = bool(r.get("compilation")) if isinstance(r, dict) else (rid in CLOUD_DEFAULT or rid.startswith("rG"))
    if mode == "streaming": return {"local": [], "cloud": ids}
    return {"local": [i for i in ids if not comp[i]], "cloud": [i for i in ids if comp[i]]}

# ---------------- gh helpers ----------------
def gh(*args, check=True, capture=True):
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if check and r.returncode: sys.exit(f"gh {' '.join(args[:3])} failed: {(r.stderr or '').strip()[-400:]}")
    return r

def state(update=None):
    s = json.load(open(STATE)) if os.path.isfile(STATE) else {}
    if update is not None:
        s.update(update); json.dump(s, open(STATE, "w"), indent=1)
    return s

# ---------------- commands ----------------
def cmd_split(a):
    plan = json.load(open(a.plan, encoding="utf-8-sig"))
    s = split_plan(plan.get("reels", []), a.mode)
    print(json.dumps(s))
    if s["local"]: print(f'PC:    PRO_RENDER.bat "{os.path.basename(a.plan)}" --only {",".join(s["local"])}', file=sys.stderr)
    if s["cloud"]: print(f'cloud: farm_cloud.py run <tag> {",".join(s["cloud"])}', file=sys.stderr)

def cmd_upload(a):
    if not TAG_RE.match(a.tag): sys.exit("bad tag")
    files = sorted(glob.glob(os.path.join(a.bundle_dir, "*.tar.enc")))
    if not files: sys.exit(f"no *.tar.enc in {a.bundle_dir}")
    for f in files:
        if os.path.getsize(f) > MAX_ASSET: sys.exit(f"{os.path.basename(f)} > 2 GB (release asset limit): cut fewer reels per bundle")
        with open(f, "rb") as fh:
            if fh.read(8) != b"Salted__": sys.exit(f"{os.path.basename(f)} is not an openssl-encrypted file: refusing to upload")
    if gh("release", "view", a.tag, "-R", REPO, check=False).returncode:
        gh("release", "create", a.tag, "-R", REPO, "--prerelease", "--title", a.tag, "--notes", "encrypted render bundles (temporary)")
    gh("release", "upload", a.tag, "-R", REPO, "--clobber", *files, capture=False)
    state({"tag": a.tag, "repo": REPO})
    print(f"uploaded {len(files)} assets to release {a.tag}: " + ", ".join(os.path.basename(f) for f in files))

def cmd_run(a):
    reels = [x.strip() for x in a.reels.split(",") if x.strip()]
    if not reels or not all(ID_RE.match(x) for x in reels) or not TAG_RE.match(a.tag): sys.exit("bad reels/tag")
    t0 = time.time()
    gh("workflow", "run", WORKFLOW, "-R", REPO, "-f", f"reels={','.join(reels)}", "-f", f"bundle_tag={a.tag}")
    run_id = None
    for _ in range(30):                     # the run shows up a few seconds after dispatch
        time.sleep(3)
        r = gh("run", "list", "-R", REPO, "--workflow", WORKFLOW, "--event", "workflow_dispatch", "-L", "5", "--json", "databaseId,createdAt")
        for x in json.loads(r.stdout or "[]"):
            ts = time.mktime(time.strptime(x["createdAt"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
            if ts >= t0 - 10: run_id = x["databaseId"]; break
        if run_id: break
    if not run_id: sys.exit("dispatched, but the run did not appear; check: gh run list -R " + REPO)
    state({"tag": a.tag, "repo": REPO, "run_id": run_id, "reels": reels})
    print(f"run {run_id}: {len(reels)} reels -> https://github.com/{REPO}/actions/runs/{run_id}")

def cmd_wait(a):
    s = state(); rid = a.run_id or s.get("run_id")
    if not rid: sys.exit("no run id (use run first)")
    t_end = time.time() + a.timeout; last = ""
    while True:
        j = json.loads(gh("run", "view", str(rid), "-R", REPO, "--json", "status,conclusion,jobs").stdout)
        jobs = j.get("jobs", [])
        line = f"{j['status']}: " + " ".join(f"{x['name'].split('(')[-1].rstrip(')')}={x.get('conclusion') or x.get('status')}" for x in jobs)
        if line != last: print(time.strftime("%H:%M:%S"), line, flush=True); last = line
        if j["status"] == "completed":
            print("conclusion:", j.get("conclusion")); return 0 if j.get("conclusion") == "success" else 1
        if time.time() > t_end: print("timeout (run still going)"); return 2
        time.sleep(a.interval)

def cmd_fetch(a):
    """artifacts -> <outdir>/<reel>.out.tar.enc (still encrypted), then delete artifacts + release (+tag)"""
    s = state(); rid = a.run_id or s.get("run_id"); tag = a.tag or s.get("tag")
    if not rid: sys.exit("no run id")
    os.makedirs(a.outdir, exist_ok=True); tmp = tempfile.mkdtemp(prefix="rf_fetch_"); got = []
    try:
        gh("run", "download", str(rid), "-R", REPO, "-D", tmp, capture=False, check=False)
        for e in sorted(glob.glob(os.path.join(tmp, "**", "*.out.tar.enc"), recursive=True)):
            with open(e, "rb") as fh:
                if fh.read(8) != b"Salted__": print("skipping non-encrypted file", os.path.basename(e)); continue
            shutil.move(e, os.path.join(a.outdir, os.path.basename(e))); got.append(os.path.basename(e))
    finally: shutil.rmtree(tmp, ignore_errors=True)
    if not got: sys.exit("no artifacts downloaded (run failed early or artifacts expired)")
    want = set(s.get("reels") or []); have = {g[:-len(".out.tar.enc")] for g in got}
    for g in got: print(f"{g}  {os.path.getsize(os.path.join(a.outdir, g)) / 2 ** 20:.1f} MB")
    if want - have: print("MISSING (job failed before upload):", ",".join(sorted(want - have)))
    if not a.keep:
        arts = json.loads(gh("api", f"repos/{REPO}/actions/runs/{rid}/artifacts", check=False).stdout or "{}").get("artifacts", [])
        for x in arts: gh("api", "-X", "DELETE", f"repos/{REPO}/actions/artifacts/{x['id']}", check=False)
        if tag: gh("release", "delete", tag, "-R", REPO, "--yes", "--cleanup-tag", check=False); print(f"artifacts + release {tag} deleted")
    print(f"{len(got)} encrypted outputs in {a.outdir}: copy them to the PC scan\\agent\\cloud_out and run CLOUD_FETCH_DECRYPT.bat")

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("split"); p.add_argument("plan"); p.add_argument("mode", choices=["asap", "streaming"]); p.set_defaults(f=cmd_split)
    p = sp.add_parser("upload"); p.add_argument("bundle_dir"); p.add_argument("tag"); p.set_defaults(f=cmd_upload)
    p = sp.add_parser("run"); p.add_argument("tag"); p.add_argument("reels"); p.set_defaults(f=cmd_run)
    p = sp.add_parser("wait"); p.add_argument("--run-id"); p.add_argument("--timeout", type=int, default=3 * 3600); p.add_argument("--interval", type=int, default=30); p.set_defaults(f=cmd_wait)
    p = sp.add_parser("fetch"); p.add_argument("outdir"); p.add_argument("--run-id"); p.add_argument("--tag")
    p.add_argument("--keep", action="store_true", help="keep artifacts + release"); p.set_defaults(f=cmd_fetch)
    a = ap.parse_args(argv); return a.f(a) or 0

if __name__ == "__main__": sys.exit(main())
