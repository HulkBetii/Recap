param(
    [int]$Port = 8765,
    [switch]$NoOpen,
    [switch]$Dev
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $repoRoot

if ($Dev) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "web\package.json") -PathType Leaf)) {
        throw "Frontend package is missing: web\package.json"
    }

    $backendArgs = @("-m", "recap_ui", "--host", "127.0.0.1", "--port", $Port, "--no-open")
    $backend = Start-Process -FilePath "python" -ArgumentList $backendArgs -PassThru -WindowStyle Hidden
    try {
        $viteArgs = @("--prefix", "web", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173")
        if (-not $NoOpen) {
            $viteArgs += "--open"
        }
        & npm @viteArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Vite exited with code $LASTEXITCODE"
        }
    } finally {
        if (-not $backend.HasExited) {
            Stop-Process -Id $backend.Id
        }
    }
    exit 0
}

$staticIndex = Join-Path $repoRoot "recap_ui\static\index.html"
if (-not (Test-Path -LiteralPath $staticIndex -PathType Leaf)) {
    throw "Production UI assets are missing. Run: npm --prefix web ci; npm --prefix web run build"
}

$uiArgs = @("-m", "recap_ui", "--host", "127.0.0.1", "--port", $Port)
if ($NoOpen) {
    $uiArgs += "--no-open"
} else {
    $uiArgs += "--open"
}
& python @uiArgs
exit $LASTEXITCODE
