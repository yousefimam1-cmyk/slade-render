# Asks for the render-farm key (the SAME value as the GitHub secret RENDER_KEY) and saves it as a user environment variable.
# Nothing is sent anywhere and the key is never printed.
Add-Type -AssemblyName Microsoft.VisualBasic
$k = [Microsoft.VisualBasic.Interaction]::InputBox("Paste the render-farm key (same value as the GitHub secret RENDER_KEY of slade-render):", "Slade - render farm key", "")
$k = "$k".Trim()
if ($k.Length -lt 16) { Write-Output "No key entered (or shorter than 16 chars) - nothing changed"; exit 1 }
[Environment]::SetEnvironmentVariable("RENDER_KEY", $k, "User")
Write-Output "DONE render key saved for this Windows user (open a new window for it to apply)"
