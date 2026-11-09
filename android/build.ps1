$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $env:ANDROID_HOME) {
    $candidate = "C:\Users\$env:USERNAME\AppData\Local\Android\Sdk"
    if (Test-Path -LiteralPath $candidate) {
        $env:ANDROID_HOME = $candidate
    } else {
        throw "Set ANDROID_HOME to your Android SDK directory."
    }
}
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
Push-Location $project
try {
    & (Join-Path $project "gradlew.bat") --no-daemon :app:assembleRelease
    if ($LASTEXITCODE -ne 0) {
        throw "Gradle build failed with exit code $LASTEXITCODE"
    }
    $dist = Join-Path $project "dist"
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    Copy-Item -LiteralPath (Join-Path $project "app\build\outputs\apk\release\app-release.apk") `
        -Destination (Join-Path $dist "LinkDrop-Android-1.0.0.apk") -Force
} finally {
    Pop-Location
}
