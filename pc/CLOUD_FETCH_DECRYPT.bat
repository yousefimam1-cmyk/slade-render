@echo off
chcp 65001 >nul
setlocal
rem Decrypts the cloud outputs scan\agent\cloud_out\*.out.tar.enc into scan\<stream>.pro\reels (mp4 + caption.txt + srt);
rem full render logs go to scan\agent\cloud_out\logs. usage: CLOUD_FETCH_DECRYPT.bat [--in DIR] [--out DIR]
set PYTHONUTF8=1
cd /d "%~dp0"
uv run --python-preference only-system --python 3.12 --with cryptography rf_pc.py decrypt %* || (echo FAILED & if not defined SLADE_HUB pause & exit /b 1)
if not defined SLADE_HUB pause
exit /b 0
