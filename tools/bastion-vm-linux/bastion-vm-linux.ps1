$ErrorActionPreference = "Stop"

$python = $null
$pythonPrefix = @()
foreach ($candidate in @(
    @{ Name = "python3"; Prefix = @() },
    @{ Name = "python"; Prefix = @() },
    @{ Name = "py"; Prefix = @("-3") }
)) {
    $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        continue
    }
    & $command.Source @($candidate.Prefix) -c `
        "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $python = $command.Source
        $pythonPrefix = $candidate.Prefix
        break
    }
}

if ($null -eq $python) {
    [Console]::Error.WriteLine("Python 3.10 or newer is required.")
    exit 1
}

$separator = [IO.Path]::PathSeparator
if ([string]::IsNullOrEmpty($env:PYTHONPATH)) {
    $env:PYTHONPATH = $PSScriptRoot
} else {
    $env:PYTHONPATH = "$PSScriptRoot$separator$($env:PYTHONPATH)"
}

& $python @pythonPrefix -m bastion_vm_linux @args
exit $LASTEXITCODE
