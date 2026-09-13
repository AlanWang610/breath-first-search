<#
.SYNOPSIS
    Build a GraphHopper graph that carries the custom `lts` encoded value.

.DESCRIPTION
    Runs the lts-wrapper's import command instead of the stock jar's. The wrapper is a
    ~7 kB jar of four classes; the stock graphhopper-web jar supplies every other class,
    so the two are put on one classpath rather than shaded together.

    Build the wrapper first:
        mvn -f deploy/graphhopper/lts-wrapper/pom.xml package

.EXAMPLE
    ./deploy/graphhopper/import-lts.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
#>
[CmdletBinding()]
param(
    [string]$Config  = "deploy/graphhopper/config-bayarea-lts.yml",
    [string]$Jar     = "data/graphhopper/graphhopper-web-11.0.jar",
    [string]$Wrapper = "deploy/graphhopper/lts-wrapper/target/graphhopper-lts-wrapper-0.1.0.jar",
    [string]$Xmx     = "8g",
    [string]$JavaHome = "C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repo

$java = Join-Path $JavaHome "bin\java.exe"
foreach ($p in @($java, $Jar, $Wrapper, $Config)) {
    if (-not (Test-Path $p)) { throw "Missing: $p" }
}

# Wrapper first on the classpath so its classes win any name collision.
$cp = "$Wrapper;$Jar"

& $java "-Xmx$Xmx" -cp $cp com.longrun.graphhopper.lts.LtsGraphHopperApplication import $Config
