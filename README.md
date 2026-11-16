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

## Test

`tests/local_test.sh` runs the whole chain on a synthetic 2-audio-track stream with a throwaway key:

1. the .bat's own ffmpeg lines;
2. Python-cipher bundling;
3. `cloud/job.sh` with openssl, as on the runner;
4. Python decrypt.

It then compares the result with a direct farm_pro render of the full source (duration and PSNR), which catches any time offset.
