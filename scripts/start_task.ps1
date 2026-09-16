[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Spec,
    [Parameter(Mandatory=$true)][string]$OutDir,
    [Parameter(Mandatory=$true)][string]$SmokePass,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath $ProjectRoot).Path
$specPath = (Resolve-Path -LiteralPath $Spec).Path
$smokePath = (Resolve-Path -LiteralPath $SmokePass).Path
$jobPath = Join-Path $PSScriptRoot 'job.py'
$evidence = Get-Content -Raw -LiteralPath $smokePath -Encoding UTF8 | ConvertFrom-Json
$fingerprint = (Get-FileHash -LiteralPath $jobPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($evidence.status -ne 'passed' -or $evidence.command_fingerprint -ne $fingerprint) { throw 'Runner smoke evidence is incompatible.' }
$toolDir = Join-Path $env:USERPROFILE '.codex\tools'
$environment = & (Join-Path $toolDir 'check_env.ps1') -ProjectRoot $projectPath -AsJson | ConvertFrom-Json
if ($environment.runner -ne 'python') { throw 'Sync the locked project .venv before starting the task.' }
$parts = @($environment.executable,'-X','utf8','-B',$jobPath,'--spec',$specPath,'--run-dir',[IO.Path]::GetFullPath($OutDir),'--smoke-pass',$smokePath)
foreach ($part in $parts) { if ($part.Contains('"')) { throw 'Quote characters are not supported in command paths.' } }
$command = ($parts | ForEach-Object { '"' + $_ + '"' }) -join ' '
& (Join-Path $toolDir 'start_job.ps1') -WorkDir $projectPath -OutDir $OutDir -Command $command -ResumeCommand $command -Stage 'translate-book'
