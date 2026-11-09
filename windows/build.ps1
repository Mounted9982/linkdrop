$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
$python = if (Test-Path -LiteralPath $candidate) { $candidate } else { (Get-Command python).Source }

Push-Location $project
try {
    & (Join-Path $project "scripts\download-croc.ps1")
    & $python tools\make_icon.py
    & $python -m pip install -r requirements-dev.txt
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name LinkDrop `
        --icon linkdrop\assets\app.ico `
        --version-file version_info.txt `
        --add-binary "vendor\croc\croc.exe;croc" `
        --add-data "vendor\croc\LICENSE;croc" `
        --add-data "vendor\croc\THIRD_PARTY_NOTICES.md;croc" `
        run.pyw
} finally {
    Pop-Location
}
