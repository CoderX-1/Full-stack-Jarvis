$ErrorActionPreference = "Stop"

$projects = "C:\Projects"
$backtalk = Join-Path $projects "backtalk"
$barehands = Join-Path $projects "barehands"
$visualizer = Join-Path $projects "ai-visualizer"
$vault = Join-Path $projects "Ayaan AI Vault"
$secretDir = Join-Path $env:APPDATA "fullstack-agent"
$started = @()

function Import-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing private environment file: $Path"
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $name, $value = $trimmed.Split("=", 2)
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name -match '^[A-Za-z_][A-Za-z0-9_]*$') {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Get-EncryptedSecretText {
    param([Parameter(Mandatory = $true)][string]$FileName)

    $path = Join-Path $secretDir $FileName
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing encrypted key: $path. Run START-EASY.bat once to save replacement keys."
    }

    $encrypted = (Get-Content -Raw -LiteralPath $path).Trim()
    $secure = ConvertTo-SecureString $encrypted
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$Port)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $client.Connect("127.0.0.1", $Port)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Wait-LocalPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$Seconds = 15
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $Seconds) {
        if (Test-LocalPort $Port) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

if (-not (Test-Path -LiteralPath $backtalk)) {
    throw "Backtalk is missing from $backtalk"
}

try {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        $ffmpeg = Get-ChildItem -Path $wingetPackages -Filter "ffmpeg.exe" `
            -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ffmpeg) {
            $env:Path = $ffmpeg.DirectoryName + ";" + $env:Path
        }
    }
    Import-DotEnv (Join-Path $PSScriptRoot ".env")

    Write-Host ""
    Write-Host "Starting Jarvis..." -ForegroundColor Cyan

    if (-not (Test-LocalPort 8790)) {
        $started += Start-Process cmd.exe -ArgumentList "/c", "run.bat" `
            -WorkingDirectory $visualizer -WindowStyle Hidden -PassThru
    }
    if (-not (Test-LocalPort 8794)) {
        $started += Start-Process cmd.exe -ArgumentList "/c", "run.bat" `
            -WorkingDirectory $barehands -WindowStyle Hidden -PassThru
    }

    if (Wait-LocalPort 8790) {
        Start-Process "http://127.0.0.1:8790/"
    }
    else {
        Write-Warning "Face did not start on port 8790."
    }
    if (Wait-LocalPort 8794) {
        Start-Process "http://127.0.0.1:8794/stage.html"
    }
    else {
        Write-Warning "Hands board did not start on port 8794."
    }

    $obsidian = "C:\Users\kuchi\AppData\Local\Programs\Obsidian\Obsidian.exe"
    if (Test-Path -LiteralPath $obsidian) {
        Start-Process $obsidian
    }

    Write-Host ""
    Write-Host "Jarvis is ready." -ForegroundColor Green
    Write-Host "Hold the HOME key, speak, then release it."
    Write-Host "Close this window to stop the voice."
    Write-Host ""

    Set-Location -LiteralPath $backtalk
    uv run --no-sync python -m backtalk.main
}
finally {
    Remove-Item Env:\OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\ELEVENLABS_API_KEY -ErrorAction SilentlyContinue
    foreach ($process in $started) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        }
    }
}
