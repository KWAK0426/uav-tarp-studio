param([switch]$CheckOnly, [switch]$NoDownload, [switch]$Headless, [switch]$Smoke)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:UAV_STUDIO_ROOT = $taskRoot
$env:UAV_STUDIO_DATA = Join-Path $taskRoot 'settings\workspace'
$taskState = Join-Path $taskRoot 'settings'
$taskCache = Join-Path $env:LOCALAPPDATA 'UAVTarpStudio'
$taskTools = Join-Path $taskCache 'tools\bin'
$env:UAV_STUDIO_TOOLS = $taskTools
New-Item -ItemType Directory -Path $taskState -Force | Out-Null
$taskLog = Join-Path $taskState 'startup.log'
$script:taskWindow = $null
$script:taskText = $null

function Write-Status([string]$message) {
    Add-Content -LiteralPath $taskLog -Value ((Get-Date -Format s)+' '+$message) -Encoding UTF8
    if ($script:taskText) { $script:taskText.AppendText($message+"`r`n"); $script:taskText.ScrollToCaret(); [System.Windows.Forms.Application]::DoEvents() }
    else { Write-Host $message }
}
function Run-Child([string]$exe, [string[]]$childArgs, [int]$timeout=1800, [string]$rawTail='') {
    $taskInfo = New-Object System.Diagnostics.ProcessStartInfo
    $taskInfo.FileName=$exe
    $taskInfo.Arguments=($childArgs | ForEach-Object { '"'+($_ -replace '(\\*)"','$1$1\"' -replace '(\\+)$','$1$1')+'"' }) -join ' '
    if ($rawTail) { $taskInfo.Arguments += ' '+$rawTail }
    $taskInfo.WorkingDirectory=$taskRoot
    $taskInfo.UseShellExecute=$false; $taskInfo.CreateNoWindow=$true
    $taskInfo.RedirectStandardOutput=$true; $taskInfo.RedirectStandardError=$true
    $taskProcess = New-Object System.Diagnostics.Process
    $taskProcess.StartInfo=$taskInfo
    [void]$taskProcess.Start()
    $taskOut=$taskProcess.StandardOutput.ReadToEndAsync(); $taskErr=$taskProcess.StandardError.ReadToEndAsync()
    $taskClock=[Diagnostics.Stopwatch]::StartNew()
    while (-not $taskProcess.WaitForExit(150)) {
        if ($script:taskWindow) { [System.Windows.Forms.Application]::DoEvents() }
        if ($taskClock.Elapsed.TotalSeconds -gt $timeout) { $taskProcess.Kill(); throw ('Timed out: '+$exe) }
    }
    $taskResult=@{code=$taskProcess.ExitCode;out=$taskOut.Result;err=$taskErr.Result}
    $taskProcess.Dispose()
    return $taskResult
}
function Get-VerifiedDownload([string]$name) {
    if ($NoDownload) { throw ('Download disabled: '+$name) }
    $taskAsset=(Get-Content -LiteralPath (Join-Path $taskRoot 'downloads.json') -Raw | ConvertFrom-Json).$name
    $taskDownloadDir=Join-Path $taskRoot 'updates\downloads'
    New-Item -ItemType Directory -Path $taskDownloadDir -Force | Out-Null
    $taskFile=Join-Path $taskDownloadDir ([IO.Path]::GetFileName(([uri]$taskAsset.url).AbsolutePath))
    if (-not (Test-Path -LiteralPath $taskFile) -or (Get-FileHash -LiteralPath $taskFile -Algorithm SHA256).Hash -ne $taskAsset.sha256) {
        Write-Status ('Downloading '+$name+' '+$taskAsset.version+' from its official release...')
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $taskAsset.url -OutFile ($taskFile+'.part') -UseBasicParsing
        if ((Get-FileHash -LiteralPath ($taskFile+'.part') -Algorithm SHA256).Hash -ne $taskAsset.sha256) { throw 'Download SHA256 mismatch. Nothing was installed.' }
        Move-Item -LiteralPath ($taskFile+'.part') -Destination $taskFile -Force
    }
    return $taskFile
}
function Find-Conda {
    $taskCandidates=@($env:CONDA_EXE,(Join-Path $taskCache 'miniforge\Scripts\conda.exe'))
    $taskCmd=Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($taskCmd) { $taskCandidates += $taskCmd.Source }
    foreach ($taskBase in @($env:USERPROFILE,$env:LOCALAPPDATA,'C:\ProgramData')) {
        foreach ($taskName in @('anaconda3','miniconda3','miniforge3')) { $taskCandidates += Join-Path $taskBase ($taskName+'\Scripts\conda.exe') }
    }
    foreach ($taskCandidate in $taskCandidates) { if ($taskCandidate -and (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) { return $taskCandidate } }
    $taskInstaller=Get-VerifiedDownload 'miniforge'
    $taskPrefix=Join-Path $taskCache 'miniforge'
    Write-Status 'Installing an app-owned package manager; system PATH and file associations stay unchanged.'
    # NSIS requires its final /D= argument without enclosing quotes, even with spaces.
    $taskInstall=Run-Child $taskInstaller @('/S','/InstallationType=JustMe','/RegisterPython=0','/AddToPath=0') 1800 ('/D='+$taskPrefix)
    if ($taskInstall.code -ne 0) { throw ('Miniforge installation failed: '+$taskInstall.err) }
    return Join-Path $taskPrefix 'Scripts\conda.exe'
}
function Ensure-ExifShim([string]$prefix) {
    $taskBin=Join-Path $prefix 'bin'
    if ((Test-Path -LiteralPath (Join-Path $taskBin 'exiftool')) -and -not (Test-Path -LiteralPath (Join-Path $taskBin 'exiftool.exe'))) {
        $taskCompiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        $taskBuild=Run-Child $taskCompiler @('/nologo','/target:exe',('/out:'+(Join-Path $taskBin 'exiftool.exe')),(Join-Path $taskRoot 'tools\ExifToolShim.cs')) 30
        if ($taskBuild.code -ne 0) { throw ('ExifTool launcher build failed: '+$taskBuild.out+$taskBuild.err) }
    }
}
function Probe-Python([string]$candidate) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $null }
    Write-Status ('Checking existing Python: '+$candidate)
    try {
        $taskProbe=Run-Child $candidate @('-B',(Join-Path $taskRoot 'tools\runtime_probe.py')) 100
        if ($taskProbe.code -eq 0) { return ($taskProbe.out | ConvertFrom-Json) }
    } catch { Write-Status $_.Exception.Message }
    return $null
}
function Find-Python {
    $taskCandidates=@($env:ELC_STUDIO_PYTHON,(Join-Path $taskCache 'runtime\python.exe'))
    $taskSaved=Join-Path $taskState 'runtime.json'
    if (Test-Path -LiteralPath $taskSaved) { try { $taskCandidates=@((Get-Content -LiteralPath $taskSaved -Raw | ConvertFrom-Json).python)+$taskCandidates } catch {} }
    $taskCmd=Get-Command python.exe -ErrorAction SilentlyContinue
    if ($taskCmd -and $taskCmd.Source -notmatch 'WindowsApps') { $taskCandidates += $taskCmd.Source }
    foreach ($taskBase in @($env:USERPROFILE,$env:LOCALAPPDATA,'C:\ProgramData')) {
        foreach ($taskName in @('anaconda3','miniconda3','miniforge3')) {
            $taskPrefix=Join-Path $taskBase $taskName
            $taskCandidates += Join-Path $taskPrefix 'python.exe'
            if (Test-Path -LiteralPath (Join-Path $taskPrefix 'envs')) { $taskCandidates += @(Get-ChildItem -LiteralPath (Join-Path $taskPrefix 'envs') -Directory | ForEach-Object { Join-Path $_.FullName 'python.exe' }) }
        }
    }
    if (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.conda\environments.txt')) {
        $taskCandidates += @(Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.conda\environments.txt') | ForEach-Object { Join-Path $_ 'python.exe' })
    }
    $taskUsable=$null
    $taskReports=@()
    foreach ($taskCandidate in ($taskCandidates | Where-Object { $_ } | Select-Object -Unique)) {
        $taskReport=Probe-Python $taskCandidate
        if ($taskReport) {
            $taskReports += $taskReport
            if ($taskReport.ok) { $taskReports | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskState 'environment-check.json') -Encoding UTF8; return $taskReport }
            if ($taskReport.python_ok -and -not $taskUsable) { $taskUsable=$taskReport }
        }
    }
    $taskReports | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskState 'environment-check.json') -Encoding UTF8
    if ($NoDownload) { throw 'No fully compatible runtime found. See settings/environment-check.json. Existing environments were not changed.' }
    $taskConda=Find-Conda
    if ($taskUsable) {
        Write-Status 'Python libraries passed. Installing only missing/outdated ExifTool in the app tools folder.'
        $taskToolPrefix=Join-Path $taskCache 'tools'
        $taskVerb=if (Test-Path -LiteralPath (Join-Path $taskToolPrefix 'conda-meta')) {'install'} else {'create'}
        $taskInstall=Run-Child $taskConda @($taskVerb,'--prefix',$taskToolPrefix,'--override-channels','-c','conda-forge','exiftool>=13.07','--yes')
        if ($taskInstall.code -ne 0) { throw ('ExifTool setup failed: '+$taskInstall.err) }
        Ensure-ExifShim $taskToolPrefix
        $taskReport=Probe-Python $taskUsable.python
    } else {
        Write-Status 'No compatible complete environment. Preparing a separate app runtime; existing environments remain intact.'
        $taskPrefix=Join-Path $taskCache 'runtime'
        $taskEnvFile=Join-Path $script:taskApp 'environment.yml'
        $taskVerb=if (Test-Path -LiteralPath (Join-Path $taskPrefix 'conda-meta')) {'update'} else {'create'}
        $taskInstall=Run-Child $taskConda @('env',$taskVerb,'--prefix',$taskPrefix,'--file',$taskEnvFile,'--yes')
        if ($taskInstall.code -ne 0) { throw ('Runtime setup failed: '+$taskInstall.err) }
        Ensure-ExifShim $taskPrefix
        $taskReport=Probe-Python (Join-Path $taskPrefix 'python.exe')
    }
    if (-not $taskReport -or -not $taskReport.ok) { throw 'Installed runtime did not pass verification. See settings/startup.log.' }
    return $taskReport
}

try {
    if (-not $Headless) {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $script:taskWindow=New-Object Windows.Forms.Form
        $script:taskWindow.Text='UAV Studio | Environment check'
        $script:taskWindow.Size=New-Object Drawing.Size(780,420)
        $script:taskWindow.StartPosition='CenterScreen'; $script:taskWindow.ControlBox=$false
        $script:taskText=New-Object Windows.Forms.TextBox
        $script:taskText.Multiline=$true; $script:taskText.ReadOnly=$true; $script:taskText.ScrollBars='Vertical'; $script:taskText.Dock='Fill'
        $script:taskText.Font=New-Object Drawing.Font('Segoe UI',10)
        $script:taskWindow.Controls.Add($script:taskText); $script:taskWindow.Show()
    }
    $taskVersion=(Get-Content -LiteralPath (Join-Path $taskState 'active.json') -Raw | ConvertFrom-Json).version
    if ($taskVersion -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') { throw 'Invalid active release version.' }
    $script:taskApp=Join-Path $taskRoot ('versions\'+$taskVersion)
    if (-not (Test-Path -LiteralPath (Join-Path $script:taskApp 'desktop.py'))) { throw 'Incomplete app package. Extract the entire ZIP.' }
    $taskRuntime=Find-Python
    $taskRuntime | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskState 'runtime.json') -Encoding UTF8
    Write-Status 'Environment verified. Reusing the selected libraries.'
    if ($CheckOnly) { exit 0 }
    $taskPrefix=Split-Path -Parent $taskRuntime.python
    $env:PATH="$taskTools;$taskPrefix\bin;$taskPrefix\Library\bin;$taskPrefix\Scripts;$taskPrefix;"+$env:PATH
    $taskArgs=@('-B',(Join-Path $script:taskApp 'desktop.py'))
    if ($Smoke) { $taskArgs += '--smoke' }
    if ($Smoke) {
        $taskRun=Run-Child $taskRuntime.python $taskArgs 60
        if ($taskRun.code -ne 0) { throw $taskRun.err }
    } else {
        $taskPythonw=Join-Path $taskPrefix 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $taskPythonw)) { throw 'pythonw.exe missing.' }
        $taskArguments=($taskArgs | ForEach-Object { '"'+$_+'"' }) -join ' '
        # This is the user-facing desktop window, not a background helper.
        Start-Process -FilePath $taskPythonw -ArgumentList $taskArguments -WorkingDirectory $script:taskApp -WindowStyle Normal
    }
} catch {
    Write-Status ('ERROR: '+$_.Exception.Message)
    if (-not $Headless) { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message+"`n`nDetails: settings/startup.log",'UAV Studio') | Out-Null }
    exit 1
} finally {
    if ($script:taskWindow) { $script:taskWindow.Dispose() }
}
