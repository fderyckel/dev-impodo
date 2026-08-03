[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$toolRoot = Join-Path $repositoryRoot "var\lock-tools-py312"
$toolPython = Join-Path $toolRoot "Scripts\python.exe"
$toolCompile = Join-Path $toolRoot "Scripts\pip-compile.exe"
$lockName = "requirements.windows-py312.lock"
$pipTemp = Join-Path $repositoryRoot "var\pip-tmp-lock-tools"
$previousTemp = [Environment]::GetEnvironmentVariable("TEMP", "Process")
$previousTmp = [Environment]::GetEnvironmentVariable("TMP", "Process")

Push-Location $repositoryRoot
try {
    $pythonVersion = py -3.12 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.12") {
        throw "The internal lock must be generated with Python 3.12."
    }

    if (-not (Test-Path -LiteralPath $toolPython)) {
        py -3.12 -m venv $toolRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the isolated lock-tool environment."
        }
    }

    if (-not (Test-Path -LiteralPath $pipTemp)) {
        New-Item -ItemType Directory -Path $pipTemp | Out-Null
    }
    $env:TEMP = $pipTemp
    $env:TMP = $pipTemp

    & $toolPython -m pip install "pip==25.3" "pip-tools==7.6.0"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the isolated, pinned lock tooling."
    }

    & $toolCompile `
        --quiet `
        --generate-hashes `
        --strip-extras `
        --resolver=backtracking `
        --allow-unsafe `
        --pip-args="--only-binary=:all:" `
        --no-emit-index-url `
        --no-emit-trusted-host `
        --newline=lf `
        --output-file=$lockName `
        pyproject.toml
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency lock generation failed."
    }

    & $toolPython .\scripts\internal_release.py --validate-lock-only
    if ($LASTEXITCODE -ne 0) {
        throw "The generated dependency lock did not pass validation."
    }
}
finally {
    [Environment]::SetEnvironmentVariable("TEMP", $previousTemp, "Process")
    [Environment]::SetEnvironmentVariable("TMP", $previousTmp, "Process")
    Pop-Location
}
