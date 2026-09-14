<#
.SYNOPSIS
    Start the GraphHopper server for a Bay Area region build.

.DESCRIPTION
    Serving never needs the LTS wrapper: GraphHopper.load() rebuilds the EncodingManager
    from the stored graph (EncodingManager.fromProperties) and never consults an
    ImportRegistry. So the stock graphhopper-web jar serves an LTS graph unmodified.

    The wrapper is only needed to BUILD a graph that contains `lts` -- see import-lts.ps1.
    If you point this at a config listing `lts` in graph.encoded_values and the graph
    folder does not exist yet, the stock server will try to import and fail loudly with
    "Unknown encoded value: lts". Run import-lts.ps1 first in that case.

.EXAMPLE
    ./deploy/graphhopper/run.ps1
    ./deploy/graphhopper/run.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
#>
[CmdletBinding()]
param(
    [string]$Config = "deploy/graphhopper/config-bayarea.yml",
    [string]$Jar    = "data/graphhopper/graphhopper-web-11.0.jar",
    [string]$Xmx    = "6g",
    [string]$JavaHome = "C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

$java = Join-Path $JavaHome "bin\java.exe"
if (-not (Test-Path $java))   { throw "No JDK at $JavaHome" }
if (-not (Test-Path $Jar))    { throw "No GraphHopper jar at $Jar" }
if (-not (Test-Path $Config)) { throw "No config at $Config" }

Write-Host "graphhopper: $Jar"
Write-Host "config:      $Config"
Write-Host "heap:        $Xmx"
Write-Host "listening:   http://localhost:8989/  (admin http://localhost:8990/)"

& $java "-Xmx$Xmx" "-Xms$Xmx" -jar $Jar server $Config
