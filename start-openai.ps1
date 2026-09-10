$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $repo "agent.py"

Write-Host ""
Write-Host "SECURITY FIRST" -ForegroundColor Yellow
Write-Host "The API key pasted into chat must be revoked before continuing."
Write-Host "Opening the OpenAI API keys page now. Revoke that key and create a new one."
Write-Host "Never paste the replacement key into chat."
Write-Host ""

Start-Process "https://platform.openai.com/api-keys"
Read-Host "Press Enter after you have revoked the exposed key and created a replacement"

$secureKey = Read-Host "Paste the NEW OpenAI API key here (input is hidden)" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
        throw "No API key was entered."
    }

    Set-Location -LiteralPath $repo
    python $runner --provider openai "set me up"
}
finally {
    Remove-Item Env:\OPENAI_API_KEY -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $secureKey = $null
}
