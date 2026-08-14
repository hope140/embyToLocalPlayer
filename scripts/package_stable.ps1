[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$')]
    [string]$ReleaseVersion
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$arguments = @{
    Channel = 'stable'
    ReleaseVersion = $ReleaseVersion
}
if ($OutputDirectory) {
    $arguments.OutputDirectory = $OutputDirectory
}

& (Join-Path $PSScriptRoot 'package_release.ps1') @arguments
exit $LASTEXITCODE
