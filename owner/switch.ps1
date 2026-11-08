# The green / red Desktop buttons: switch.ps1 on | off. Only flips the bot's switch on GitHub; nothing runs here.
param([ValidateSet('on', 'off')][string]$to)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$gh = "$env:ProgramFiles\GitHub CLI\gh.exe"
if (-not (Test-Path $gh)) { $gh = (Get-Command gh).Source }
$shell = New-Object -ComObject WScript.Shell

function Next-Run {
    # Shorts every day at 13:07, 17:37 and 21:13 UTC (.github/workflows/episode.yml)
    $now = [DateTime]::UtcNow
    $slots = foreach ($d in 0, 1) { foreach ($hm in @(@(13, 7), @(17, 37), @(21, 13))) { $now.Date.AddDays($d).AddHours($hm[0]).AddMinutes($hm[1]) } }
    $t = $slots | Where-Object { $_ -gt $now } | Select-Object -First 1
    return $t.ToLocalTime().ToString('dddd d MMM, h:mm tt')
}

try {
    if ($to -eq 'on') {
        & $gh workflow enable episode.yml; if ($LASTEXITCODE -ne 0) { throw 'could not turn the schedule on' }
        & $gh variable set BOT_ENABLED --body true; if ($LASTEXITCODE -ne 0) { throw 'could not flip the switch' }
        $msg = "Channel bot is ON.`n`nIt posts 3 Shorts a day, plus a long video on the LONG_DAYS days (Mon / Wed / Fri unless you change it), on GitHub's computers, not this PC.`nNext video: $(Next-Run)."
        $shell.Popup($msg, 0, 'PIXELHELM channel bot', 64) | Out-Null
    } else {
        & $gh variable set BOT_ENABLED --body false; if ($LASTEXITCODE -ne 0) { throw 'could not flip the switch' }
        & $gh workflow disable episode.yml; if ($LASTEXITCODE -ne 0) { throw 'could not turn the schedule off' }
        $shell.Popup("Channel bot is OFF.`n`nNo more videos until you press START.", 0, 'PIXELHELM channel bot', 64) | Out-Null
    }
} catch {
    $shell.Popup("Didn't work: $_`n`nIf you never ran 'connect channel bot', run it first.", 0, 'PIXELHELM channel bot', 16) | Out-Null
}
