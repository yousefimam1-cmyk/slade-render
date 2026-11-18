# slade-render

A GitHub Actions render farm for Slade's reels. It runs the PC's own `farm_pro.py` (a snapshot shipped inside each encrypted bundle, not a fork) on one free `ubuntu-latest` runner per reel. Up to 20 reels render in parallel.

## Privacy model (this repo is PUBLIC)

- **What goes in git:** code only. Media, plans, transcripts, captions and logs never go in git (`.gitignore` blocks them).
- **Where the key lives:** `RENDER_KEY` exists only on the PC (a user env var set by `SET_RENDER_KEY.bat`) and in the repo secret. The agent sandbox never sees it.
- **Release assets and artifacts:** they hold only `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256` blobs (`Salted__…`). The artifacts are kept for 1 day, and `farm_cloud.py fetch` deletes them together with the release.
- **Public Actions log:** it shows progress lines only. The full log can contain subtitle text, so it travels inside the encrypted output.
- **PC-side encryption:** `rf_pc.py` uses `openssl.exe` (if it is version 1.1.1 or newer). Otherwise it uses Python `cryptography` with the byte-identical format. `tests/local_test.sh` checks both directions against Linux openssl.

## Files

| file | runs on | job |
|---|---|---|
| `prep_segments.py cut` | agent sandbox | plan → `CUT_SEGMENTS.bat` + `plan_segs.json` (rebased) + `needed_assets.json` |
| `pc/SET_RENDER_KEY.bat` (+ `set_render_key.ps1`) | PC, once | InputBox → HKCU env `RENDER_KEY` |
| `pc/CLOUD_PREP.bat` → `CUT_SEGMENTS.bat` + `rf_pc.py bundle` | PC | stream-copy cuts (`-map 0 -c copy`, both audio tracks) + memes/ProPack → `scan\agent\segs\<reel>.tar.enc` + `code.tar.enc` |
| `farm_cloud.py upload / run / wait / fetch` | agent sandbox (`gh`) | release upload → dispatch → poll → download `*.out.tar.enc`, then delete the artifacts and the release |
| `.github/workflows/render.yml` + `cloud/job.sh` | GitHub | decrypt → `assemble.py` → `farm_pro` (libx264 veryfast CRF 20) → encrypt → artifact |
| `pc/CLOUD_FETCH_DECRYPT.bat` | PC | `scan\agent\cloud_out\*.out.tar.enc` → `scan\<stream>.pro\reels\` (mp4, caption.txt, srt) |

Copy everything in `pc/` into the PC's `Scripts` folder, next to `farm_pro.py`.

## Segments

The pipeline cuts only the kept footage, not whole spans. Each reel, or each part of a compilation, is cut into pieces: its keep ranges padded by ±2 s, merged when the padded gap is under 24 s. In the cloud, `assemble.py` lays the pieces out on a compact timeline with 24 s of black and silence between them. The gap between keep ranges is then at least 28 s, so farm_pro's 20 s grouping and crossfade rules behave exactly as on the full stream. All plan times (`in/out/keep/subs/bleep/src_t`) are rebased to that timeline.

A cut made with input `-ss` and no `-avoid_negative_ts` keeps an mp4 edit list, so piece time 0 lands exactly on the requested second. `rf_pc.py bundle` warns if a piece's length disagrees. For `pro_plan_v2` this uploads about 14 min of footage instead of about 85 min.

## Two modes: `split_plan(reels, mode)` in `farm_cloud.py`

- **ASAP:** the PC renders the short reels (`r01 r03 r04 r05 r06`, via `PRO_RENDER.bat plan --only …`). At the same time the cloud renders the compilations (`rG1 rG1s rG2 rG2s`).
- **Streaming:** the PC is busy streaming, so the cloud renders all 9.

`python3 farm_cloud.py split pro_plan_v2.json asap|streaming` prints both lists.

## Run it

1. Once: set the repo secret (`gh secret set RENDER_KEY -R yousefimam1-cmyk/slade-render`, typing the value yourself). Then run `SET_RENDER_KEY.bat` on the PC with the **same** value.
2. Sandbox:
   ```
   prep_segments.py cut pro_plan_v2.json --mode asap --out gen
   ```
   Put `gen/CUT_SEGMENTS.bat` in `Scripts\` and `gen/plan_segs.json` in `Scripts\scan\agent\segs\`.
3. PC: run `CLOUD_PREP.bat`. In ASAP mode, also start `PRO_RENDER.bat pro_plan_v2.json --only r01,r03,r04,r05,r06`.
4. Sandbox: stage `scan\agent\segs\*.tar.enc`, then:
   ```
   farm_cloud.py upload <dir> b20261115
   farm_cloud.py run b20261115 rG1,rG1s,rG2,rG2s
   farm_cloud.py wait
   farm_cloud.py fetch <dir>
   ```
   Commit the `*.out.tar.enc` files to the PC's `scan\agent\cloud_out\`.
5. PC: run `CLOUD_FETCH_DECRYPT.bat`.

## YouTube videos (farm_yt4)

A long video is split into parts that run in parallel: `<vid>-aud` renders the whole audio track in one pass, plus the captions sidecars,
chapters, description and thumbnail. Each `<vid>-cNN` renders one frame-exact video chunk of about 150 s, cut on hard-cut boundaries. The PC
joins them by stream copy (`yt_join.py`), so there are no re-encodes and no audio seams.

| step | where | command |
|---|---|---|
| bundles | PC | `YT_CLOUD_PREP.bat [pro_plan_v5.json] [y1,y2]` → `scan\agent\segs\<vid>-<part>.tar.enc`, `code.tar.enc`, `yt_parts.txt` |
| part ids | sandbox | `farm_cloud.py ytparts <plan> <pieces dir> y1,y2` (the same list as yt_parts.txt) |
| render | sandbox | `farm_cloud.py upload <dir> <tag>` → `run <tag> <ids>` → `wait` → `fetch <outdir>` |
| decrypt + join | PC | `CLOUD_FETCH_DECRYPT.bat` (joins as soon as all parts of a video are there); `YT_JOIN.bat` by hand |

`cloud/job.sh` detects a YouTube part by `bundle/job.json`. An aud part whose plan has `discord_audio` installs CPU torch and demucs first;
their weights are cached. `tests/yt_local_test.sh <dir> y1,y2` runs the whole chain locally with a throwaway key.

## Test

`tests/local_test.sh` runs the whole chain on a synthetic 2-audio-track stream with a throwaway key:

1. the .bat's own ffmpeg lines;
2. Python-cipher bundling;
3. `cloud/job.sh` with openssl, as on the runner;
4. Python decrypt.

It then compares the result with a direct farm_pro render of the full source (duration and PSNR), which catches any time offset.

## New stream button (PC-side, no Claude, no `gh`)

Slade Hub's "New stream" tab runs `pc/NEW_STREAM.bat` (`new_stream.py`): extract audio, transcribe (local, Scribe opt-in), clip + plan draft (`ns_plan.py`, follows `knowledge/SLADE_EDIT_RULES.md`), cut, encrypt, then **`pc/rf_cloud_pc.py`** (stdlib, GitHub REST) does what `farm_cloud.py` does with `gh`: release, asset upload, workflow dispatch, poll, artifact download, cleanup. One-time on the PC: `SET_RENDER_KEY.bat`, and `SET_GH_TOKEN.bat` with a **fine-grained token for this repo only** (Actions: read+write, Contents: read+write), stored as user env var `GH_RENDER_TOKEN`. `prep_segments.py` now honours `SEGS_OUT` / `SCRIPTS` env vars in the generated `CUT_SEGMENTS.bat` (per-stream folders) and no longer needs `farm_cloud.py` unless `--mode` is used. Tests: `python tests/test_new_stream.py`, `python tests/test_rf_cloud_pc.py`. Full notes: Hub README ("NEW STREAM -> CONTENT").

## Speed (2026-11-18): less over the home connection

- **Upload source once:** `rf_pc.py bundle/ytbundle` moves every media file >= 1 MB into `<segs>\cache\f-<hmac>.enc` (encrypted once; name =
  HMAC(RENDER_KEY, content sha256), reveals nothing). `rf_cloud_pc.py upload` sends only missing ones to the long-lived release `src-cache`
  (unused assets > 21 days pruned; fetch/delete never touch it). The runner restores them with `cloud/fetch_media.py`. `--no-cache` = old way.
- **Quality-targeted encode:** x264 `-preset slow -crf 22` (was veryfast CRF 20): 1080p source SSIM 0.9908 vs 0.9890, PSNR 43.3 vs 42.3 dB, 12% smaller.
  Override: `RF_PRESET/RF_CRF` (reels), `YT_PRESET/YT_CRF` (YouTube chunks).
- **Direct hand-off (repo secret `SOCIAL_LAMMA_TOKEN`):** QA-PASS reels and each YouTube video (new `join` job, `yt_join.py` on GitHub) go straight
  from the runner to Social Lamma (`cloud/handoff.py`, media upload only). `render-<id>` then holds sidecars + qa/thumb/preview + `<id>.handoff.json`
  (media id), encrypted; the video is in `full-<id>` (1 day), fetched only with `--full`. No secret / QA FAIL / error -> the old package.
- Tests: `python3 -m unittest cloud/test_speed.py`, `python3 tests/test_speed_pc.py`, dry run vs fake services `python3 tests/speed_e2e.py /tmp/e2e pc`.
