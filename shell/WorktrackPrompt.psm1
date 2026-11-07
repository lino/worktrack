<#
.SYNOPSIS
    Non-blocking Worktrack (`wt`) prompt integration module for PowerShell.
.DESCRIPTION
    Queries `wt status --porcelain` over the local `worktrackd` IPC socket
    (`$env:XDG_RUNTIME_DIR` on POSIX or loopback TCP via `$env:LOCALAPPDATA` /
    `$env:WORKTRACK_IPC_PORT` on Windows) and returns an empty string whenever
    `wt` is absent, idle, or `worktrackd` is offline.
#>

function Get-WorktrackPromptSegment {
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory = $false)]
        [string]$Prefix = "[wt: ",

        [Parameter(Mandatory = $false)]
        [string]$Suffix = "]"
    )

    $wtCommand = Get-Command -Name "wt" -ErrorAction SilentlyContinue
    if ($null -eq $wtCommand) {
        return ""
    }

    try {
        $rawOutput = & $wtCommand status --porcelain 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($rawOutput)) {
            return ""
        }
        $trimmed = ([string]$rawOutput).Trim()
        if ([string]::IsNullOrEmpty($trimmed)) {
            return ""
        }
        return "$Prefix$trimmed$Suffix"
    }
    catch {
        return ""
    }
}

Export-ModuleMember -Function Get-WorktrackPromptSegment
