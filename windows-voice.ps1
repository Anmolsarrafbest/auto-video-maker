param(
    [Parameter(Mandatory)][string]$RequestPath,
    [Parameter(Mandatory)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
[void][Windows.Media.SpeechSynthesis.SpeechSynthesisStream,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices |
    Where-Object { $_.DisplayName -eq $request.voice -and $_.Language -eq 'en-IN' } |
    Select-Object -First 1
if ($null -eq $voice) { throw 'Install the English (India) text-to-speech language pack in Windows Settings.' }

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Length -eq 1 -and $_.GetGenericArguments().Length -eq 1 } |
    Select-Object -First 1
$synth = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::new()
try {
    $synth.Voice = $voice
    $synth.Options.SpeakingRate = [double]$request.speed
    $operation = $synth.SynthesizeTextToStreamAsync([string]$request.text)
    $task = $asTask.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult() | Out-Null
    $audio = $task.Result
    $reader = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($audio.GetInputStreamAt(0))
    try {
        $file = [System.IO.File]::Create($OutputPath)
        try { $reader.CopyTo($file) } finally { $file.Dispose() }
    } finally {
        $reader.Dispose()
        $audio.Dispose()
    }
} finally {
    $synth.Dispose()
}
