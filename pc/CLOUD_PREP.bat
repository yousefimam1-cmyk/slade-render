@echo off
chcp 65001 >nul
setlocal
rem Cloud render prep (PC side): stream-copy cuts + assets (CUT_SEGMENTS.bat), then bundle + ENCRYPT each reel
rem -> scan\agent\segs\<reel>.tar.enc + code.tar.enc. Only these .enc files ever leave the PC.
rem usage: CLOUD_PREP.bat [--reels rG1,rG2]
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "scan\agent\segs\plan_segs.json" (echo missing scan\agent\segs\plan_segs.json - ask the agent for it & goto :fail)
if not exist "CUT_SEGMENTS.bat" (echo missing CUT_SEGMENTS.bat - ask the agent for it & goto :fail)
uv run --python-preference only-system --python 3.12 --with cryptography rf_pc.py check || (echo run SET_RENDER_KEY.bat first & goto :fail)
set "HUB_WAS=%SLADE_HUB%"
set SLADE_HUB=1
call CUT_SEGMENTS.bat || goto :fail
set "SLADE_HUB=%HUB_WAS%"
uv run --python-preference only-system --python 3.12 --with cryptography rf_pc.py bundle %* || goto :fail
echo DONE encrypted bundles in %~dp0scan\agent\segs
if not defined SLADE_HUB pause
exit /b 0
:fail
echo FAILED
if not defined SLADE_HUB pause
exit /b 1
