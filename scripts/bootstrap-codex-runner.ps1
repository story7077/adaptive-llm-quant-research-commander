[CmdletBinding()]
param(
    [string]$RunnerHome = (
        Join-Path $env:LOCALAPPDATA 'AdaptiveLlmQuant\codex-runner-home'
    )
)

$ErrorActionPreference = 'Stop'

$resolvedParent = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $RunnerHome)
)
if (-not (Test-Path -LiteralPath $resolvedParent)) {
    New-Item -ItemType Directory -Path $resolvedParent | Out-Null
}

$resolvedHome = [System.IO.Path]::GetFullPath($RunnerHome)
if (-not (Test-Path -LiteralPath $resolvedHome)) {
    New-Item -ItemType Directory -Path $resolvedHome | Out-Null
}

$forbiddenNames = @(
    'AGENTS.md',
    'AGENTS.override.md',
    'auth.json',
    'config.toml',
    'history.jsonl',
    'memories',
    'memories_extensions',
    'plugins',
    'rules',
    'sessions',
    'skills'
)
$presentForbidden = @(
    Get-ChildItem -LiteralPath $resolvedHome -Force |
        Where-Object {
            $_.Name -in $forbiddenNames -or
            $_.Name.EndsWith('.config.toml', [StringComparison]::OrdinalIgnoreCase)
        } |
        Select-Object -ExpandProperty Name
)
if ($presentForbidden.Count -gt 0) {
    throw (
        'Runner home contains credentials or context-bearing files: ' +
        ($presentForbidden -join ', ')
    )
}

$previousCodexHome = $env:CODEX_HOME
try {
    $env:CODEX_HOME = $resolvedHome
    & codex login --device-auth -c 'cli_auth_credentials_store="keyring"'
    if ($LASTEXITCODE -ne 0) {
        throw "Codex device login failed with exit code $LASTEXITCODE"
    }
    & codex login status -c 'cli_auth_credentials_store="keyring"'
    if ($LASTEXITCODE -ne 0) {
        throw "Codex login status failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:CODEX_HOME = $previousCodexHome
}

Write-Output 'Dedicated Codex runner login is ready.'
Write-Output "Runner home: $resolvedHome"
