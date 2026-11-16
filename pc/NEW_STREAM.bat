@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem New stream -> content in one go (Slade Hub button "New stream" runs this; it also works by hand).
rem usage: NEW_STREAM.bat "stream.mp4" [--game tft] [--scribe] [--reels 6] [--review-plan] [--approve-plan] [--from STEP] [--force STEP]
rem Steps: audio, transcribe, plan, cut, encrypt, upload, poll, fetch, qa, summary. Run it again after any stop: finished steps are skipped.
rem --scribe spends ElevenLabs credits (off by default: local whisper). Needs once: SET_RENDER_KEY.bat and SET_GH_TOKEN.bat.
cd /d "%~dp0"
if "%~1"=="" (
  echo usage: NEW_STREAM.bat "stream.mp4" [--game tft] [--scribe] [--reels 6] [--review-plan]
  if not defined SLADE_HUB pause
  exit /b 2
)
uv run --python-preference only-system --python 3.12 new_stream.py %*
set rc=%errorlevel%
rem exit code 10 = paused for plan review: not an error for Hub (the step list shows it)
if "%rc%"=="10" set rc=0
if not defined SLADE_HUB pause
exit /b %rc%
