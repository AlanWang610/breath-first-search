<#
.SYNOPSIS
    Build a GraphHopper graph that carries the custom `lts` encoded value.

.DESCRIPTION
    Runs the lts-wrapper's import command instead of the stock jar's. The wrapper is a
    ~7 kB jar of four classes; the stock graphhopper-web jar supplies every other class,
    so the two are put on one classpath rather than shaded together.

    Build the wrapper first:
        mvn -f deploy/graphhopper/lts-wrapper/pom.xml package

    The input .pbf must already carry the `lts` tag, which comes from `osm_lts.way_lts`:
        uv run longrun build-region deploy/regions/<region>.yaml
        uv run python deploy/graphhopper/scripts/add_lts_tags.py `
            data/osm/<region>.osm.pbf data/osm/<region>-lts.osm.pbf

    -Region resolves the config through deploy/regions/routers.yaml, the same reader run.ps1
    uses -- importing with one config and serving with another is how you get GraphHopper's
    "profiles do not match the graph" refusal, and sharing the resolver is what prevents it.

.EXAMPLE
    ./deploy/graphhopper/import-lts.ps1 -Region ozarks
#>
[CmdletBinding()]
param(
    [string]$Region  = "",
    [string]$Config  = "",
    [string]$Jar     = "data/graphhopper/graphhopper-web-11.0.jar",
    [string]$Wrapper = "deploy/graphhopper/lts-wrapper/target/graphhopper-lts-wrapper-0.1.0.jar",
    [string]$Xmx     = "",
    [string]$JavaHome = "C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

. (Join-Path $PSScriptRoot "registry.ps1")
$entry = Resolve-Region -Region $Region -Config $Config -Xmx $Xmx -DefaultXmx "8g"

$java = Join-Path $JavaHome "bin\java.exe"
foreach ($p in @($java, $Jar, $Wrapper, $entry.Config)) {
    if (-not (Test-Path $p)) { throw "Missing: $p" }
}

Write-Host "region:  $($entry.Region)"
Write-Host "config:  $($entry.Config)"
Write-Host "heap:    $($entry.Xmx)"

# Wrapper first on the classpath so its classes win any name collision.
$cp = "$Wrapper;$Jar"

& $java "-Xmx$($entry.Xmx)" -cp $cp com.longrun.graphhopper.lts.LtsGraphHopperApplication import $entry.Config
