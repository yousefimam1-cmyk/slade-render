@echo off
rem Saves the GitHub token that lets this PC start and fetch the cloud render (user env var GH_RENDER_TOKEN). Never printed.
rem Make it at github.com/settings/personal-access-tokens/new : Repository access = Only select repositories > slade-render;
rem Repository permissions: Actions = Read and write, Contents = Read and write (Metadata read is automatic). Expiry: 90 days or more.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_gh_token.ps1"
if not defined SLADE_HUB pause
