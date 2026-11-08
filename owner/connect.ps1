# One-time setup for the ginfowell channel bot ("connect channel bot" on the Desktop).
# The owner only signs in (GitHub once, Google once); everything else is done here. Safe to run again:
# every step checks what's already there. Nothing is scheduled on this PC; the bot runs on GitHub.
# Note: `gh auth setup-git` makes git use the GitHub sign-in for pushes (global git credential helper).
$ErrorActionPreference = 'Stop'
$repoDir = Split-Path -Parent $PSScriptRoot
Set-Location $repoDir
$name = 'ginfowell-bot'
$gh = "$env:ProgramFiles\GitHub CLI\gh.exe"
if (-not (Test-Path $gh)) { $gh = (Get-Command gh).Source }
$py = (Get-Command python).Source

function Step($t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }
# PS 5.1: with 'Stop', a native command's redirected stderr becomes a terminating error, so the
# quiet checks run with 'Continue' and only look at the exit code.
function GhOk { $ErrorActionPreference = 'Continue'; & $gh @args *> $null; return ($LASTEXITCODE -eq 0) }
function Gh { & $gh @args; if ($LASTEXITCODE -ne 0) { throw "gh $($args[0]) $($args[1]) failed" } }
function Git-Do { & git @args; if ($LASTEXITCODE -ne 0) { throw "git $($args[0]) failed" } }
function Latest-RunId($workflow) {
    $ErrorActionPreference = 'Continue'
    $id = & $gh run list --repo $full --workflow $workflow --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId' 2> $null
    return "$id".Trim()
}
function Run-And-Wait($workflow, [string[]]$fields) {
    $before = Latest-RunId $workflow
    Gh workflow run $workflow --repo $full --ref main @fields
    $id = $before
    for ($i = 0; $i -lt 40 -and ($id -eq $before -or -not $id); $i++) { Start-Sleep 3; $id = Latest-RunId $workflow }
    if ($id -eq $before -or -not $id) { throw "the $workflow run never started" }
    Gh run watch $id --repo $full --exit-status --interval 15 | Out-Null
    return $id
}

try {
    Step 'GitHub sign-in'
    if (-not (GhOk auth status --hostname github.com)) {
        Write-Host 'A GitHub page opens: paste the code shown below, then press Authorize.'
        $null | & $gh auth login --hostname github.com --web --git-protocol https --scopes workflow --skip-ssh-key
        if ($LASTEXITCODE -ne 0) { throw "GitHub sign-in did not finish" }
    }
    Gh auth setup-git --hostname github.com
    $me = & $gh api user --jq .login
    $full = "$me/$name"
    Write-Host "signed in as $me"

    Step 'Code on GitHub (public: secrets stay in the youtube environment)'
    if ((git branch --show-current) -ne 'main') { throw 'the bot folder is not on the main branch' }
    if (GhOk repo view $full) {
        if (-not (git remote 2>$null | Select-String -SimpleMatch origin)) { Git-Do remote add origin "https://github.com/$full.git" }
        Git-Do push -u origin main
    } else {
        Gh repo create $full --public --source . --remote origin --push
    }

    Step 'Switches'
    Gh variable set YT_PRIVACY --body public --repo $full
    if (-not (GhOk variable get BOT_ENABLED --repo $full)) { Gh variable set BOT_ENABLED --body false --repo $full }

    Step 'YouTube key vault (only main may use it)'
    $useEnv = GhOk api "repos/$full/environments/youtube"
    if (-not $useEnv) {
        $envJson = '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
        $tmpJson = Join-Path $env:TEMP 'pixelhelm-env.json'
        Set-Content -Path $tmpJson -Value $envJson -Encoding ascii
        $useEnv = GhOk api -X PUT "repos/$full/environments/youtube" --input $tmpJson
        Remove-Item $tmpJson
        if ($useEnv) { GhOk api -X POST "repos/$full/environments/youtube/deployment-branch-policies" -f name=main -f type=branch | Out-Null }
    }
    if ($useEnv) { Write-Host 'environment "youtube" ready (only main can use the key)' }
    else { Write-Host 'this GitHub plan has no environments for private repos: the key goes in repository secrets (fine for a one-owner private repo)' }


    Step 'Fonts and packages (locks, ~5 min)'
    Git-Do pull --rebase -q origin main
    if ((Get-Content assets\fonts\fonts.lock.json -Raw) -match '"sha256": null') {
        Run-And-Wait pin-fonts.yml @() | Out-Null
        Git-Do pull --rebase -q origin main
    }
    if (-not (Test-Path requirements.lock.txt)) {
        $id = Run-And-Wait lock-deps.yml @()
        $tmp = Join-Path $env:TEMP "pixelhelm-lock-$id"
        Gh run download $id --repo $full -n requirements-lock -D $tmp
        Copy-Item "$tmp\requirements.lock.txt" requirements.lock.txt
        Git-Do add requirements.lock.txt
        Git-Do commit -q -m 'deps: hash lock from lock-deps run'
        Git-Do push -q origin main
    }
    Write-Host 'locked'

    Step 'YouTube sign-in'
    function Find-Client {
        Get-ChildItem "$repoDir\owner", "$env:USERPROFILE\Downloads" -Filter 'client_secret*.json' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    $client = Find-Client
    if (-not $client) {
        Write-Host 'A Google Cloud page opens. Next to "channel-bot", click the download icon, then "Download JSON".'
        Start-Process 'https://console.cloud.google.com/auth/clients?project=pixelhelm-channel-bot'
        for ($i = 0; $i -lt 150 -and -not $client; $i++) { Start-Sleep 2; $client = Find-Client }
    }
    if (-not $client) { throw 'No client_secret_....json arrived in Downloads.' }
    $where = if ($useEnv) { 'env' } else { 'repo' }
    # gh run from Python can't read the Windows credential store, so hand it the sign-in for this step only.
    $env:GH_TOKEN = (& $gh auth token)
    try { & $py -I "$PSScriptRoot\connect_youtube.py" $client.FullName $full $gh $where }
    finally { Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { throw 'YouTube sign-in did not finish' }

    Step 'Test video (dry run: renders, never posts, ~15 min)'
    $id = Run-And-Wait episode.yml @('-f', 'format=auto', '-f', 'dry_run=true')
    Write-Host "test video OK: https://github.com/$full/actions/runs/$id"

    Write-Host ''
    Write-Host 'All set. Press the green START channel bot button on the Desktop to go live.' -ForegroundColor Green
    Write-Host "You can delete $($client.Name) from Downloads now (the bot has its own copy on GitHub)."
} catch {
    Write-Host ''
    Write-Host "Stopped: $_" -ForegroundColor Red
    Write-Host 'Fix the line above (or tell Claude), then run connect again. Finished steps are skipped.'
}
Read-Host 'Press Enter to close'
