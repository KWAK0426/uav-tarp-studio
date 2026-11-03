param(
    [ValidateSet('Setup', 'Run', 'Check', 'New')][string]$Action = 'Run',
    [switch]$Smoke,
    [string]$Project,
    [string]$Screenshot
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$prefix = if ($env:ELC_STUDIO_ENV) { $env:ELC_STUDIO_ENV } else { Join-Path $env:LOCALAPPDATA 'ELCStudioShare\runtime-20261026' }
$python = if ($env:ELC_STUDIO_PYTHON) { $env:ELC_STUDIO_PYTHON } else { Join-Path $prefix 'python.exe' }

function Find-Conda {
    if ($env:ELC_CONDA_EXE -and (Test-Path -LiteralPath $env:ELC_CONDA_EXE)) { return $env:ELC_CONDA_EXE }
    if ($env:CONDA_EXE -and (Test-Path -LiteralPath $env:CONDA_EXE)) { return $env:CONDA_EXE }
    $found = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($base in @($env:USERPROFILE, $env:LOCALAPPDATA)) {
        foreach ($name in @('miniforge3', 'miniconda3', 'anaconda3')) {
            $candidate = Join-Path $base "$name\Scripts\conda.exe"
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    throw 'Conda not found. Install Miniforge (see README.md), or set ELC_CONDA_EXE to conda.exe.'
}

try {
    if ($Action -eq 'Setup') {
        if ($env:ELC_STUDIO_PYTHON) { throw 'Unset ELC_STUDIO_PYTHON before installation. Setup only creates its dedicated environment.' }
        $conda = Find-Conda
        if (Test-Path -LiteralPath $python) {
            Write-Host "Existing dedicated environment kept: $prefix"
            Write-Host 'Running checks only. No base-environment changes.'
        } else {
            Write-Host "Installing dedicated environment: $prefix"
            Write-Host 'Allow several GB of free disk space plus a Conda package cache. No imagery is included.'
            & $conda env create --prefix $prefix --file (Join-Path $root 'environment.yml') --yes
            if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed. Read the Conda error; do not modify base Python.' }
        }
    }
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python runtime missing. Run 00_INSTALL.bat first. See README.md.' }
    $envPrefix = Split-Path -Parent $python
    $env:PATH = "$envPrefix\Library\bin;$envPrefix\Scripts;$envPrefix;" + $env:PATH
    if ($Action -in @('Setup', 'Check')) {
        & $python (Join-Path $root 'distribution.py') check
        exit $LASTEXITCODE
    }
    if ($Action -eq 'New') {
        & $python (Join-Path $root 'distribution.py') new
        exit $LASTEXITCODE
    }
    $arguments = @((Join-Path $root 'distribution.py'), 'start')
    if ($Project) { $arguments += @('--project', $Project) }
    if ($Smoke) { $arguments += '--smoke' }
    if ($Screenshot) { $arguments += @('--screenshot', $Screenshot) }
    # Smoke is synchronous for verifiable exit codes; normal UI runs without a console.
    if ($Smoke) {
        & $python @arguments
        exit $LASTEXITCODE
    }
    $pythonw = Join-Path $envPrefix 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe missing from the selected runtime.' }
    $quoted = ($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    Start-Process -FilePath $pythonw -ArgumentList $quoted -WorkingDirectory $root -WindowStyle Hidden
} catch {
    Write-Host $_ -ForegroundColor Red
    exit 1
}
