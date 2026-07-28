[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CodexCliVersion,
    [string]$Image = 'adaptive-llm-quant-codex-runner:local',
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

docker build `
    --build-arg "CODEX_CLI_VERSION=$CodexCliVersion" `
    --tag $Image `
    --file (Join-Path $RepositoryRoot 'containers\Dockerfile') `
    $RepositoryRoot

