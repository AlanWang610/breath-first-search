<#
.SYNOPSIS
    Start the GraphHopper server for one region.

.DESCRIPTION
    Serving never needs the LTS wrapper: GraphHopper.load() rebuilds the EncodingManager
    from the stored graph (EncodingManager.fromProperties) and never consults an
    ImportRegistry. So the stock graphhopper-web jar serves an LTS graph unmodified.

    The wrapper is only needed to BUILD a graph that contains `lts` -- see import-lts.ps1.
    If you point this at a config listing `lts` in graph.encoded_values and the graph
    folder does not exist yet, the stock server will try to import and fail loudly with
    "Unknown encoded value: lts". Run import-lts.ps1 first in that case.

    -Region resolves the config, the port and the heap from deploy/regions/routers.yaml,
    so five regions can run at once on five ports (ADR 0026). -Config still works and wins.

.EXAMPLE
    ./deploy/graphhopper/run.ps1 -Region ozarks
    ./deploy/graphhopper/run.ps1 -Region bayarea
    ./deploy/graphhopper/run.ps1 -Config deploy/graphhopper/config-bayarea-fallback.yml
#>
[CmdletBinding()]
param(
    [string]$Region = "",
    [string]$Config = "",
    [string]$Jar    = "data/graphhopper/graphhopper-web-11.0.jar",
    [string]$Xmx    = "",
    [string]$JavaHome = "C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

. (Join-Path $PSScriptRoot "registry.ps1")
$entry = Resolve-Region -Region $Region -Config $Config -Xmx $Xmx -DefaultXmx "6g"

$java = Join-Path $JavaHome "bin\java.exe"
if (-not (Test-Path $java))            { throw "No JDK at $JavaHome" }
if (-not (Test-Path $Jar))             { throw "No GraphHopper jar at $Jar" }
if (-not (Test-Path $entry.Config))    { throw "No config at $($entry.Config)" }

Write-Host "graphhopper: $Jar"
Write-Host "region:      $($entry.Region)"
Write-Host "config:      $($entry.Config)"
Write-Host "heap:        $($entry.Xmx)"
Write-Host "listening:   http://localhost:$($entry.Port)/  (admin http://localhost:$($entry.AdminPort)/)"

& $java "-Xmx$($entry.Xmx)" "-Xms$($entry.Xmx)" -jar $Jar server $entry.Config
