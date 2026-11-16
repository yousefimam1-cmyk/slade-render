@echo off
chcp 65001 >nul
setlocal
rem Manual join of a YouTube render (CLOUD_FETCH_DECRYPT.bat already joins as soon as every part is there):
rem scan\<stream>.pro\youtube\<vid>\<vid>-cNN.mp4 + <vid>.m4a -> scan\<stream>.pro\youtube\<vid>.mp4  (stream copy, frame-checked)
rem usage: YT_JOIN.bat [stream stem] [y1,y2]
set PYTHONUTF8=1
cd /d "%~dp0"
set "STEM=%~1"
if "%STEM%"=="" set "STEM=2026-11-13 23-20-41"
set "VIDS=%~2"
if "%VIDS%"=="" set "VIDS=y1,y2"
for %%V in (%VIDS:,= %) do (
  uv run --python-preference only-system --python 3.12 yt_join.py "scan\%STEM%.pro\youtube\%%V" %%V --out "scan\%STEM%.pro\youtube\%%V.mp4" || goto :fail
)
echo DONE joined
if not defined SLADE_HUB pause
exit /b 0
:fail
echo FAILED
if not defined SLADE_HUB pause
exit /b 1
