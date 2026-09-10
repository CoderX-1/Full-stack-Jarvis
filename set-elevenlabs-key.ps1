$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "SECURITY FIRST" -ForegroundColor Yellow
Write-Host "The ElevenLabs key pasted into chat must be rotated before it is stored."
Write-Host "Opening the ElevenLabs API-key settings page now."
Write-Host "Create a replacement key, switch to it, and delete the exposed key."
Write-Host "Never paste the replacement key into chat."
Write-Host ""

Start-Process "https://elevenlabs.io/app/settings/api-keys"
Read-Host "Press Enter after you have created a replacement and deleted the exposed key"

$secureKey = Read-Host "Paste the NEW ElevenLabs API key here (input is hidden)" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw "No API key was entered."
    }

    [Environment]::SetEnvironmentVariable("ELEVENLABS_API_KEY", $plainKey, "User")
    Write-Host ""
    Write-Host "ElevenLabs is configured for future Chat/Talk windows." -ForegroundColor Green
    Write-Host "Restart any agent or Backtalk window that is already open."
}
finally {
    $plainKey = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $secureKey = $null
}
