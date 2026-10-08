#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

& uv run --project $PSScriptRoot agents-status @args
exit $LASTEXITCODE
