@echo off
chcp 65001 >nul
setlocal
rem YouTube cloud prep (PC side): one ENCRYPTED bundle per render part (y1-aud, y1-c01 ...) + code.tar.enc
rem -> scan\agent\segs\<vid>-<part>.tar.enc and scan\agent\segs\yt_parts.txt (the id list to give the agent).
rem Only these .enc files ever leave the PC. usage: YT_CLOUD_PREP.bat [plan.json] [y1,y2]
set PYTHONUTF8=1
cd /d "%~dp0"
set "PLAN=%~1"
if "%PLAN%"=="" set "PLAN=pro_plan_v5.json"
set "VIDS=%~2"
if "%VIDS%"=="" set "VIDS=y1,y2"
if not exist "%PLAN%" (echo missing %PLAN% - ask the agent for it & goto :fail)
if not exist "scan\agent\segs\pieces" (echo missing scan\agent\segs\pieces - run CUT_YT.bat first & goto :fail)
uv run --python-preference only-system --python 3.12 --with cryptography rf_pc.py check || (echo run SET_RENDER_KEY.bat first & goto :fail)
uv run --python-preference only-system --python 3.12 --with cryptography --with numpy --with pillow --with arabic-reshaper --with python-bidi --with fonttools rf_pc.py ytbundle --plan "%PLAN%" --vids %VIDS% || goto :fail
echo DONE: give the agent every scan\agent\segs\*.tar.enc and yt_parts.txt
type "scan\agent\segs\yt_parts.txt"
if not defined SLADE_HUB pause
exit /b 0
:fail
echo FAILED
if not defined SLADE_HUB pause
exit /b 1
