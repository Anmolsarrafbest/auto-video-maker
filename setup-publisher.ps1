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
    [string]$Region = 'IN'
)

$ErrorActionPreference = 'Stop'
$tokenPath = Join-Path $PSScriptRoot 'secrets\youtube-token.json'
if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw 'Connect your channel with python shorts.py connect-youtube before scheduling uploads.'
}
$channel = (Get-Content -LiteralPath $tokenPath -Raw | ConvertFrom-Json).channel
$channelId = $channel.id
if ($channelId -notmatch '^UC[A-Za-z0-9_-]{22}$') {
    throw 'The connected channel ID is missing or invalid. Reconnect your YouTube channel.'
}
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$script = Join-Path $PSScriptRoot 'shorts.py'
$arguments = '"{0}" daily --count {1} --seconds {2} --region {3} --upload {4} --channel-id {5}' -f $script, $Count, $Seconds, $Region, $Visibility, $channelId
$time = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$taskName = 'Auto Video Maker - YouTube Shorts'
if ($PSCmdlet.ShouldProcess("$($channel.title) ($channelId)", "Schedule $Count $Visibility YouTube Short(s) daily at $At local PC time")) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output "Registered '$taskName' for $($channel.title), daily at $At local PC time."
}
