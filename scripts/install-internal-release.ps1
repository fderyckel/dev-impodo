[CmdletBinding()]
param(
    [string]$BundleDirectory = $PSScriptRoot,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Impodo\app")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$manifestPath = Join-Path $bundle "release-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "The internal release manifest is missing."
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1) {
    throw "The internal release manifest schema is not supported."
}
if ($manifest.platform -ne "windows" -or $manifest.python -notmatch '^3\.12\.') {
    throw "This bundle is not an approved Windows/Python 3.12 release."
}
if ($manifest.release_id -notmatch '^impodo-[0-9A-Za-z.+-]+-[0-9a-f]{12}$') {
    throw "The internal release identifier is invalid."
}
if (@($manifest.artifacts).Count -eq 0) {
    throw "The internal release manifest contains no artifacts."
}

$bundlePrefix = $bundle.TrimEnd('\') + '\'
$manifestArtifactNames = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
foreach ($artifact in $manifest.artifacts) {
    $name = [string]$artifact.name
    if (
        [IO.Path]::GetFileName($name) -ne $name -or
        $name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    ) {
        throw "The release manifest contains an unsafe artifact name."
    }
    if (-not $manifestArtifactNames.Add($name)) {
        throw "The release manifest contains a duplicate artifact: $name"
    }
    $path = [IO.Path]::GetFullPath((Join-Path $bundle $name))
    if (-not $path.StartsWith($bundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "A release artifact escapes the bundle directory."
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release artifact is missing: $name"
    }
    $file = Get-Item -LiteralPath $path
    if ($file.Length -ne [long]$artifact.bytes) {
        throw "Release artifact size mismatch: $name"
    }
    $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne ([string]$artifact.sha256).ToLowerInvariant()) {
        throw "Release artifact hash mismatch: $name"
    }
}

$requiredArtifacts = @(
    "requirements.windows-py312.lock",
    "tests.txt",
    "secret-scan.json",
    "dependency-audit.json",
    "sbom.cdx.json",
    "install-internal-release.ps1"
)
foreach ($requiredArtifact in $requiredArtifacts) {
    if (-not $manifestArtifactNames.Contains($requiredArtifact)) {
        throw "The release manifest omits required evidence: $requiredArtifact"
    }
}
foreach ($file in Get-ChildItem -LiteralPath $bundle -File) {
    if (
        $file.Name -ne "release-manifest.json" -and
        -not $manifestArtifactNames.Contains($file.Name)
    ) {
        throw "The bundle contains an unlisted artifact: $($file.Name)"
    }
}

$pythonVersion = py -3.12 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.12") {
    throw "An approved Python 3.12 runtime is required."
}

$resolvedInstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$target = Join-Path $resolvedInstallRoot ([string]$manifest.release_id)
if (Test-Path -LiteralPath $target) {
    throw "The versioned installation already exists: $target"
}

$lock = Join-Path $bundle "requirements.windows-py312.lock"
$wheels = @(Get-ChildItem -LiteralPath $bundle -Filter "impodo-*.whl" -File)
if ($wheels.Count -ne 1) {
    throw "The bundle must contain exactly one Impodo wheel."
}
if (-not $manifestArtifactNames.Contains($wheels[0].Name)) {
    throw "The Impodo wheel is not covered by the release manifest."
}

py -3.12 -m venv $target
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the versioned Impodo environment."
}
$targetPython = Join-Path $target "Scripts\python.exe"
& $targetPython -m pip install `
    --require-hashes `
    --only-binary=:all: `
    --requirement $lock
if ($LASTEXITCODE -ne 0) {
    throw "Locked dependency installation failed."
}
& $targetPython -m pip install --no-deps $wheels[0].FullName
if ($LASTEXITCODE -ne 0) {
    throw "Impodo wheel installation failed."
}
& $targetPython -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "The installed Impodo environment is inconsistent."
}

$launcher = Join-Path $target "Scripts\impodo.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "The installed environment did not create impodo.exe."
}

Write-Output "Impodo internal release installed successfully."
Write-Output "Start it with: $launcher"
