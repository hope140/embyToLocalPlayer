[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$PythonEmbedDirectory,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$')]
    [string]$ReleaseVersion
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$arguments = @{
    Channel = 'beta'
    ReleaseVersion = $ReleaseVersion
}
if ($OutputDirectory) {
    $arguments.OutputDirectory = $OutputDirectory
}
if ($PythonEmbedDirectory) {
    $arguments.PythonEmbedDirectory = $PythonEmbedDirectory
}

& (Join-Path $PSScriptRoot 'package_release.ps1') @arguments
exit $LASTEXITCODE
