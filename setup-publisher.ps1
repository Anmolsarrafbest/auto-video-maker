[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '18:30',
    [ValidateSet(1, 2)]
    [int]$Count = 1,
    [ValidateSet(12, 15, 20)]
    [int]$Seconds = 15,
    [ValidateSet('private', 'unlisted', 'public')]
    [string]$Visibility = 'private',
    [ValidatePattern('^[A-Z]{2}$')]
    [string]$Region = 'IN',
    [switch]$Queue,
    [switch]$Instagram,
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
if ($Instagram -and $Queue) {
    throw 'Register the YouTube queue and Instagram as separate tasks.'
}
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Instagram) {
        $destination = @'
import instagram_publish
import shorts
from local_secrets import secret
path = instagram_publish.CONFIG
config = shorts.read_json(path) if path.is_file() else {}
if not config.get("username") or not (secret("INSTAGRAM_PAGE_ACCESS_TOKEN") or config.get("token")):
    raise SystemExit("Run instagram_publish.py connect before scheduling Reels.")
print("@" + config["username"])
'@ | & $python -
    } else {
        $channelId = @'
import shorts
if not shorts.has_token(shorts.TOKEN):
    raise SystemExit("Run shorts.py connect-youtube before scheduling uploads.")
print(shorts.read_token(shorts.TOKEN)["channel"]["id"])
'@ | & $python -
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the publishing account configuration.' }
} finally {
    Pop-Location
}

if ($Instagram) {
    $taskName = 'Auto Video Maker - Instagram Reels'
    $script = Join-Path $PSScriptRoot 'instagram_publish.py'
    $arguments = '"{0}" publish-next' -f $script
    $description = 'Publish one approved Instagram Reel'
} else {
    if ($channelId -notmatch '^UC[A-Za-z0-9_-]{22}$') {
        throw 'The connected channel ID is missing or invalid. Reconnect your YouTube channel.'
    }
    $destination = $channelId
    $taskName = 'Auto Video Maker - YouTube Shorts'
    $description = "Publish up to $Count $Visibility YouTube Short(s)"
    if ($Queue) {
        $script = Join-Path $PSScriptRoot 'queue_publish.py'
        $arguments = '"{0}" publish --count {1} --visibility {2} --channel-id {3}' -f $script, $Count, $Visibility, $channelId
    } else {
        $script = Join-Path $PSScriptRoot 'shorts.py'
        $arguments = '"{0}" daily --count {1} --seconds {2} --region {3} --upload {4} --channel-id {5}' -f $script, $Count, $Seconds, $Region, $Visibility, $channelId
        if ($Offline) { $arguments += ' --offline' }
    }
}
$time = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
if ($PSCmdlet.ShouldProcess($destination, "$description daily at $At local PC time")) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output "Registered '$taskName' for $destination, daily at $At local PC time."
}
