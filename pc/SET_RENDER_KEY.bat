@echo off
rem Saves RENDER_KEY (same value as the GitHub repo secret) as a user env var. The key is never shown again.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_render_key.ps1"
if not defined SLADE_HUB pause
