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

    **An existing graph directory is refused, not overwritten.** GraphHopper's import loads an
    existing graph instead of rebuilding it and exits 0, so re-importing after the scores
    changed takes 1.4 seconds, reports success, and leaves the old graph in place. That is the
    stale-graph failure in its purest form: nothing is wrong anywhere except the answers. Found
    in M9 while rebuilding the Bay Area on real LTS -- the "rebuilt" graph was still the one
    built on 4 September with the placeholder scores. Use -Force to replace it.

.EXAMPLE
    ./deploy/graphhopper/import-lts.ps1 -Region ozarks
    ./deploy/graphhopper/import-lts.ps1 -Region bayarea -Force
#>
[CmdletBinding()]
param(
    [string]$Region  = "",
    [string]$Config  = "",
    [string]$Jar     = "data/graphhopper/graphhopper-web-11.0.jar",
    [string]$Wrapper = "deploy/graphhopper/lts-wrapper/target/graphhopper-lts-wrapper-0.1.0.jar",
    [string]$Xmx     = "",
    [switch]$Force,
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

# `graph.location` is the only thing here that knows where the graph lands, so read it back
# out of the config rather than reconstructing the convention in a second place.
$graphLocation = (Select-String -Path $entry.Config -Pattern '^\s*graph\.location:\s*(.+?)\s*$').Matches[0].Groups[1].Value
if (Test-Path $graphLocation) {
    if (-not $Force) {
        throw @"
A graph already exists at $graphLocation (built $((Get-Item (Join-Path $graphLocation 'properties')).LastWriteTime)).

GraphHopper's import would LOAD it rather than rebuild it, exit 0 in about a second, and
leave the old scores in place -- so a rebuild after add_lts_tags.py changed the `lts` tag
would report success and change nothing. Re-run with -Force to replace it.
"@
    }
    Write-Host "removing: $graphLocation (-Force)"
    Remove-Item -Recurse -Force $graphLocation
}

# Wrapper first on the classpath so its classes win any name collision.
$cp = "$Wrapper;$Jar"

& $java "-Xmx$($entry.Xmx)" -cp $cp com.longrun.graphhopper.lts.LtsGraphHopperApplication import $entry.Config
