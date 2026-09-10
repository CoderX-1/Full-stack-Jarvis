$ErrorActionPreference = "Stop"

$secretDir = Join-Path $env:APPDATA "fullstack-agent"
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

Write-Host "Replace the API keys that were exposed in chat." -ForegroundColor Yellow
Write-Host "The new values are hidden while you type and encrypted for this Windows account."
Write-Host ""

Start-Process "https://platform.openai.com/api-keys"
$openAI = Read-Host "Paste your NEW OpenAI key" -AsSecureString
if ($openAI.Length -eq 0) { throw "OpenAI key was empty." }

Start-Process "https://elevenlabs.io/app/settings/api-keys"
$elevenLabs = Read-Host "Paste your NEW ElevenLabs key" -AsSecureString
if ($elevenLabs.Length -eq 0) { throw "ElevenLabs key was empty." }

$openAI | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $secretDir "openai.key") -Encoding UTF8
$elevenLabs | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $secretDir "elevenlabs.key") -Encoding UTF8

$openAI = $null
$elevenLabs = $null
Write-Host ""
Write-Host "Encrypted replacement keys saved. You can now use START JARVIS." -ForegroundColor Green
