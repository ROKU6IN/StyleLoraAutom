param([string]$Python)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python.ps1')
$testPython = Resolve-StyleLoraPython -Requested $Python
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$savedPythonPath = $env:PYTHONPATH
$savedEncoding = $env:PYTHONIOENCODING
Push-Location -LiteralPath $repositoryRoot
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    & $testPython -m unittest discover -s tests -t . -v
    $testExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $savedPythonPath
    $env:PYTHONIOENCODING = $savedEncoding
    Pop-Location
}
exit $testExitCode
