$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolsDir = Join-Path $root 'tools'
$tempDir = Join-Path $toolsDir 'ffmpeg-download'
$zipPath = Join-Path $toolsDir 'ffmpeg-release-win64-gpl.zip'
$binTarget = Join-Path $toolsDir 'ffmpeg\bin'
$url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-gpl-9.0.zip'

try {
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    if (Test-Path $tempDir) { Remove-Item -Recurse -Force $tempDir }
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

    Write-Host 'Downloading FFmpeg from the BtbN FFmpeg-Builds GitHub release...'
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -L --fail --retry 3 --output $zipPath $url
        if ($LASTEXITCODE -ne 0) { throw 'curl.exe could not download FFmpeg.' }
    } else {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    }
    Write-Host 'Extracting FFmpeg. The archive is large, so this may take a few minutes...'
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    $ffmpegExe = Get-ChildItem -Path $tempDir -Filter 'ffmpeg.exe' -Recurse | Select-Object -First 1
    if (-not $ffmpegExe) { throw 'Could not find ffmpeg.exe in the downloaded archive.' }
    $sourceBin = $ffmpegExe.Directory.FullName
    if (Test-Path $binTarget) { Remove-Item -Recurse -Force $binTarget }
    New-Item -ItemType Directory -Force -Path $binTarget | Out-Null
    Copy-Item -Path (Join-Path $sourceBin '*') -Destination $binTarget -Recurse -Force

    if (-not (Test-Path (Join-Path $binTarget 'ffmpeg.exe'))) { throw 'FFmpeg setup did not produce ffmpeg.exe.' }
    if (-not (Test-Path (Join-Path $binTarget 'ffprobe.exe'))) { throw 'FFmpeg setup did not produce ffprobe.exe.' }
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
    Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
    Write-Host 'FFmpeg installed locally in tools\ffmpeg\bin.'
    exit 0
}
catch {
    Write-Error $_
    Write-Host 'FFmpeg setup failed. Check your connection and run install.bat again.'
    exit 1
}
