[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ExpectedRepository = "story7077/adaptive-llm-quant-research-commander"
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Push-Location -LiteralPath $RepositoryRoot
try {
    uv run research-commander public-scan . --expected-repository $ExpectedRepository
    uv run pytest
    uv run ruff check .
    uv run pyright
}
finally {
    Pop-Location
}
