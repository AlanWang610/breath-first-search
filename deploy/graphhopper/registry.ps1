<#
.SYNOPSIS
    Resolve a region name to its config, port and heap from deploy/regions/routers.yaml.

.DESCRIPTION
    Dot-sourced by run.ps1 and import-lts.ps1 so the two cannot disagree about which config
    belongs to which region -- that disagreement is exactly what ADR 0001 warns about, since
    GraphHopper.load() refuses a graph whose stored `profiles` differ from the config's, and
    importing with one config and serving with another is the way to produce it.

    Deliberately a small hand-parser rather than a YAML dependency: PowerShell 5.1 ships no
    YAML reader, the file is a flat two-level map, and adding a module install to a script
    whose whole job is to start a JVM would be a worse trade than twenty lines here. The
    authoritative reader is `longrun.regions.routers.load_registry`; this mirrors it, and
    `tests/unit/test_region_routers.py` pins the two against the same file.
#>

function Get-RouterRegistry {
    param([string]$Path = "deploy/regions/routers.yaml")

    if (-not (Test-Path $Path)) { throw "No router registry at $Path" }

    $registry = @{}
    $current = $null
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -match '^([A-Za-z0-9_-]+):\s*$') {
            $current = $Matches[1]
            $registry[$current] = @{ Region = $current; Port = 8989; Xmx = "4g"; Host = "localhost" }
            continue
        }
        if ($null -ne $current -and $line -match '^\s+([A-Za-z_]+):\s*(.+?)\s*$') {
            $key = $Matches[1]
            $value = $Matches[2]
            switch ($key) {
                "port" { $registry[$current].Port = [int]$value }
                "xmx"  { $registry[$current].Xmx = $value }
                "host" { $registry[$current].Host = $value }
            }
        }
    }
    return $registry
}

function Resolve-Region {
    param(
        [string]$Region = "",
        [string]$Config = "",
        [string]$Xmx = "",
        [string]$DefaultXmx = "4g"
    )

    # An explicit -Config wins, so the fallback graph (decision 0001's escape hatch) and any
    # one-off config stay runnable without an entry in the registry.
    if ($Config) {
        $name = if ($Region) { $Region } else { "(from -Config)" }
        $port = 8989
        if ($Config -match 'config-([A-Za-z0-9_-]+)-lts\.yml$') {
            $registry = Get-RouterRegistry
            if ($registry.ContainsKey($Matches[1])) {
                $name = $Matches[1]
                $port = $registry[$name].Port
                if (-not $Xmx) { $Xmx = $registry[$name].Xmx }
            }
        }
        return [pscustomobject]@{
            Region    = $name
            Config    = $Config
            Port      = $port
            AdminPort = $port + 1
            Xmx       = if ($Xmx) { $Xmx } else { $DefaultXmx }
        }
    }

    if (-not $Region) {
        throw "Give -Region (one of: $((Get-RouterRegistry).Keys -join ', ')) or -Config."
    }

    $registry = Get-RouterRegistry
    if (-not $registry.ContainsKey($Region)) {
        throw "Region '$Region' is not in deploy/regions/routers.yaml. Known: $($registry.Keys -join ', ')"
    }

    $entry = $registry[$Region]
    return [pscustomobject]@{
        Region    = $Region
        Config    = "deploy/graphhopper/config-$Region-lts.yml"
        Port      = $entry.Port
        AdminPort = $entry.Port + 1
        Xmx       = if ($Xmx) { $Xmx } else { $entry.Xmx }
    }
}
