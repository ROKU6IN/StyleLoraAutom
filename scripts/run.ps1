[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Python,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python.ps1')
$runPython = Resolve-StyleLoraPython -Requested $Python
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$savedPythonPath = $env:PYTHONPATH
$savedEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    & $runPython -m stylelora @Arguments
    $runExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $savedPythonPath
    $env:PYTHONIOENCODING = $savedEncoding
}
exit $runExitCode
