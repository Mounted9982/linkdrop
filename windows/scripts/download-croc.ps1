$ErrorActionPreference = "Stop"

$version = "11.6.5"
$url = "https://github.com/schollz/croc/releases/download/v$version/croc_v${version}_Windows-64bit.zip"
$expectedSha256 = "AE5B9D249099FF159F6A4B91824A2BF7D2B32F0DD77342AEF51B34154AA4FFFB"
$project = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$target = Join-Path $project "vendor\croc"
$archive = Join-Path $target "croc.zip"
$executable = Join-Path $target "croc.exe"

if (Test-Path -LiteralPath $executable) {
    exit 0
}

New-Item -ItemType Directory -Force -Path $target | Out-Null
Invoke-WebRequest -Uri $url -OutFile $archive
$actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "croc archive checksum mismatch: expected $expectedSha256, got $actualSha256"
}

$extract = Join-Path $target "extract"
Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
Copy-Item -LiteralPath (Join-Path $extract "croc.exe") -Destination $executable -Force
Copy-Item -LiteralPath (Join-Path $extract "LICENSE") -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $extract "THIRD_PARTY_NOTICES.md") -Destination $target -Force
Remove-Item -LiteralPath $extract -Recurse -Force

