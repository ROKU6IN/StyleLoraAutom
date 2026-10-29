param(
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python.ps1')
$demoPython = Resolve-StyleLoraPython -Requested $Python
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$savedPythonPath = $env:PYTHONPATH
$savedEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    & $demoPython (Join-Path $PSScriptRoot 'http_demo.py') --output $Output
    $demoExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $savedPythonPath
    $env:PYTHONIOENCODING = $savedEncoding
}
exit $demoExitCode
