function Resolve-StyleLoraPython {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Python executable does not exist: $Requested"
        }
        return $Requested
    }
    $repositoryRoot = Split-Path -Parent $PSScriptRoot
    $localPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { return $localPython }
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) { return $bundledPython }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') { return $command.Source }
    throw 'Python 3.11+ is required. Pass -Python with its executable path.'
}
