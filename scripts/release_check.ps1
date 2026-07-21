param(
    [string]$MediaPath,
    [switch]$SkipMediaSmoke,
    [switch]$AllowDirty,
    [string]$WorkDir = "work/release-gate"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$allowedWorkRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "work"))
$resolvedWorkDir = if ([IO.Path]::IsPathRooted($WorkDir)) {
    [IO.Path]::GetFullPath($WorkDir)
} else {
    [IO.Path]::GetFullPath((Join-Path $repoRoot $WorkDir))
}
$requiredPrefix = $allowedWorkRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $resolvedWorkDir.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "WorkDir must be a child of $allowedWorkRoot"
}
if ($SkipMediaSmoke -and $MediaPath) {
    throw "-MediaPath and -SkipMediaSmoke cannot be used together"
}
if (-not $SkipMediaSmoke -and -not $MediaPath) {
    throw "-MediaPath is required unless -SkipMediaSmoke is set"
}

Set-Location $repoRoot
$steps = [System.Collections.Generic.List[object]]::new()
$reportPath = Join-Path $resolvedWorkDir "report.json"
$secretReportPath = Join-Path $resolvedWorkDir "secret-report.json"
$packageReportPath = Join-Path $resolvedWorkDir "package-report.json"
$mediaReportPath = Join-Path $resolvedWorkDir "media-smoke-report.json"
$tachReportPath = Join-Path $resolvedWorkDir "tach-report.txt"

function Invoke-NativeCommand {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-GateStep {
    param([string]$Name, [scriptblock]$Action)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $Action
        $timer.Stop()
        $steps.Add([ordered]@{ name = $Name; status = "passed"; duration_s = [Math]::Round($timer.Elapsed.TotalSeconds, 3) })
    } catch {
        $timer.Stop()
        $steps.Add([ordered]@{ name = $Name; status = "failed"; duration_s = [Math]::Round($timer.Elapsed.TotalSeconds, 3); error = $_.Exception.Message })
        throw
    }
}

function Write-GateReport {
    param([string]$Status, [string]$FailureMessage = "")
    $package = if (Test-Path -LiteralPath $packageReportPath) { Get-Content -LiteralPath $packageReportPath -Raw | ConvertFrom-Json } else { $null }
    $media = if (Test-Path -LiteralPath $mediaReportPath) { Get-Content -LiteralPath $mediaReportPath -Raw | ConvertFrom-Json } else { $null }
    $payload = [ordered]@{
        status = $Status
        git_commit = (git rev-parse HEAD).Trim()
        python_version = (& python -c "import platform; print(platform.python_version())").Trim()
        platform = (& python -c "import platform; print(platform.platform())").Trim()
        media_smoke = -not $SkipMediaSmoke
        allow_dirty = [bool]$AllowDirty
        steps = $steps
        package = $package
        media = $media
        failure = if ($FailureMessage) { $FailureMessage } else { $null }
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding utf8
}

try {
    if (-not $AllowDirty) {
        $dirty = git status --porcelain --untracked-files=all
        if ($dirty) {
            throw "Release gate requires a clean worktree. Use -AllowDirty only while developing the gate."
        }
    }
    if (Test-Path -LiteralPath $resolvedWorkDir) {
        Remove-Item -LiteralPath $resolvedWorkDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolvedWorkDir | Out-Null

    Invoke-GateStep "secret_scan" {
        Invoke-NativeCommand "python" @("-m", "scripts.check_secrets", "--history", "--report", $secretReportPath)
    }
    Invoke-GateStep "git_diff_check" {
        Invoke-NativeCommand "git" @("diff", "--check", "HEAD", "--")
    }
    Invoke-GateStep "ruff_check" {
        Invoke-NativeCommand "python" @("-m", "ruff", "check", ".")
    }
    Invoke-GateStep "tach_check" {
        $hasNativePreference = Test-Path Variable:\PSNativeCommandUseErrorActionPreference
        $previousNativePreference = $null
        if ($hasNativePreference) {
            $previousNativePreference = $PSNativeCommandUseErrorActionPreference
            $PSNativeCommandUseErrorActionPreference = $false
        }
        try {
            & cmd.exe /c "python -m tach check --dependencies --output text > `"$tachReportPath`" 2>&1"
            $tachExitCode = $LASTEXITCODE
        } finally {
            if ($hasNativePreference) {
                $PSNativeCommandUseErrorActionPreference = $previousNativePreference
            }
        }
        $output = if (Test-Path -LiteralPath $tachReportPath) { Get-Content -LiteralPath $tachReportPath } else { @() }
        if ($output) {
            $output | Write-Host
        }
        if ($tachExitCode -ne 0) {
            throw "Tach boundary issues found. See $tachReportPath."
        }
    }
    Invoke-GateStep "pytest" {
        Invoke-NativeCommand "python" @("-m", "pytest", "-q")
    }
    Invoke-GateStep "compileall" {
        Invoke-NativeCommand "python" @("-m", "compileall", "-q", "common", "episode_planner", "ingest", "match", "orchestrator", "preflight", "render", "review", "series_composer", "series_match", "series_recap", "shots", "storymap", "tts", "visual_index", "scripts", "tests", "run.py")
    }
    Invoke-GateStep "editable_install_dry_run" {
        Invoke-NativeCommand "python" @("-m", "pip", "install", "--dry-run", "--no-deps", "-e", ".")
    }

    $wheelDir = Join-Path $resolvedWorkDir "wheel"
    New-Item -ItemType Directory -Path $wheelDir | Out-Null
    Invoke-GateStep "wheel_build" {
        Invoke-NativeCommand "python" @("-m", "pip", "wheel", "--no-deps", "-w", $wheelDir, ".")
    }
    $wheel = Get-ChildItem -LiteralPath $wheelDir -Filter "*.whl" | Sort-Object LastWriteTimeUtc | Select-Object -Last 1
    if (-not $wheel) {
        throw "Wheel build produced no .whl file"
    }
    Invoke-GateStep "wheel_content" {
        Invoke-NativeCommand "python" @("-m", "scripts.check_wheel", "--wheel", $wheel.FullName, "--report", $packageReportPath)
    }

    $venvDir = Join-Path $resolvedWorkDir "wheel-venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    Invoke-GateStep "wheel_install_import" {
        Invoke-NativeCommand "python" @("-m", "venv", "--system-site-packages", $venvDir)
        Invoke-NativeCommand $venvPython @("-m", "pip", "install", "--no-deps", "--force-reinstall", $wheel.FullName)
        $smokeDir = Join-Path $resolvedWorkDir "outside-repo"
        New-Item -ItemType Directory -Path $smokeDir | Out-Null
        Push-Location $smokeDir
        try {
            Invoke-NativeCommand $venvPython @("-c", "import pathlib, run, common, episode_planner, ingest, match, orchestrator, preflight, render, review, series_composer, series_match, series_recap, shots, storymap, tts, visual_index; modules=(run,common,episode_planner,ingest,match,orchestrator,preflight,render,review,series_composer,series_match,series_recap,shots,storymap,tts,visual_index); paths=[pathlib.Path(m.__file__).resolve() for m in modules]; assert all('site-packages' in str(p).lower() for p in paths), paths; print(*paths, sep='\n')")
        } finally {
            Pop-Location
        }
    }
    Invoke-GateStep "cli_help" {
        $smokeDir = Join-Path $resolvedWorkDir "outside-repo"
        Push-Location $smokeDir
        try {
            foreach ($module in @("episode_planner", "ingest", "match", "series_composer", "series_match", "series_recap", "visual_index")) {
                Invoke-NativeCommand $venvPython @("-m", $module, "--help")
            }
        } finally {
            Pop-Location
        }
    }

    $dryRunFilm = if ($SkipMediaSmoke) {
        $placeholder = Join-Path $resolvedWorkDir "dry-run-film.mp4"
        [IO.File]::WriteAllBytes($placeholder, [byte[]](0))
        $placeholder
    } else {
        $resolvedMedia = [IO.Path]::GetFullPath($MediaPath)
        if (-not (Test-Path -LiteralPath $resolvedMedia -PathType Leaf)) {
            throw "MediaPath does not exist: $resolvedMedia"
        }
        $resolvedMedia
    }
    Invoke-GateStep "production_dry_run" {
        Invoke-NativeCommand "python" @("run.py", "--input", $dryRunFilm, "--run-dir", (Join-Path $resolvedWorkDir "production-dry-run"), "--config", "config.movie.production.yaml", "--dry-run")
    }

    if (-not $SkipMediaSmoke) {
        Invoke-GateStep "real_media_cache_smoke" {
            Invoke-NativeCommand "python" @("-m", "scripts.cache_integrity_smoke", "--media", $dryRunFilm, "--work-dir", (Join-Path $resolvedWorkDir "cache-smoke"), "--report", $mediaReportPath)
        }
    }

    Write-GateReport "passed"
    Write-Host "Release candidate gate passed. Report: $reportPath"
} catch {
    if (-not (Test-Path -LiteralPath $resolvedWorkDir)) {
        New-Item -ItemType Directory -Path $resolvedWorkDir | Out-Null
    }
    Write-GateReport "failed" $_.Exception.Message
    throw
}
