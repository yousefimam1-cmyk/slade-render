# Asks for the fine-grained GitHub token (slade-render only: Actions + Contents write) and saves it as the user env var GH_RENDER_TOKEN.
# The token is never printed, logged or sent anywhere except api.github.com (one test call to the repo).
Add-Type -AssemblyName Microsoft.VisualBasic
$repo = if ($env:SLADE_RENDER_REPO) { $env:SLADE_RENDER_REPO } else { 'yousefimam1-cmyk/slade-render' }
$k = [Microsoft.VisualBasic.Interaction]::InputBox("Paste the GitHub token for $repo (github.com/settings/personal-access-tokens/new, this repo only, Actions + Contents = Read and write):", "Slade - GitHub render token", "")
$k = "$k".Trim()
if ($k.Length -lt 30 -or $k -notmatch '^(github_pat_|ghp_)') { Write-Output "No valid token entered (it starts with github_pat_) - nothing changed"; exit 1 }
[Environment]::SetEnvironmentVariable("GH_RENDER_TOKEN", $k, "User")
try {
  $r = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo" -Headers @{ Authorization = "Bearer $k"; 'User-Agent' = 'slade-hub'; Accept = 'application/vnd.github+json' } -TimeoutSec 20
  if ($r.permissions -and -not $r.permissions.push) { Write-Output "WARNING the token can read $repo but not write: give it Actions + Contents = Read and write" }
  else { Write-Output "DONE token saved and works for $($r.full_name)" }
} catch {
  $c = ''; try { $c = [int]$_.Exception.Response.StatusCode } catch {}
  Write-Output "Token saved, but GitHub answered $c for $repo (401 = wrong or expired token, 404 = token is not allowed to see the repo). Make a new one and run this again."
}
Write-Output "Open Slade Hub again (or Refresh) so it picks the token up."
