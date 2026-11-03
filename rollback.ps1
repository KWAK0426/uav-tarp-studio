$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    $taskRecord=Join-Path $PSScriptRoot 'updates\backups\previous.json'
    $taskPrevious=Get-Content -LiteralPath $taskRecord -Raw | ConvertFrom-Json
    if ($taskPrevious.version -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') { throw 'Invalid previous version.' }
    $taskVersionRoot=Join-Path $PSScriptRoot ('versions\'+$taskPrevious.version)
    $taskManifest=Get-Content -LiteralPath (Join-Path $taskVersionRoot 'PACKAGE_MANIFEST.json') -Raw | ConvertFrom-Json
    foreach ($taskRow in $taskManifest.files) {
        $taskPath=[IO.Path]::GetFullPath((Join-Path $taskVersionRoot $taskRow.path))
        if (-not $taskPath.StartsWith($taskVersionRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path.' }
        if ((Get-FileHash -LiteralPath $taskPath -Algorithm SHA256).Hash -ne $taskRow.sha256) { throw ('Previous version file check failed: '+$taskRow.path) }
    }
    $taskChoice=[Windows.Forms.MessageBox]::Show(('Close all UAV Studio app windows first. Return to '+$taskPrevious.version+'? Projects and results will be kept.'),'UAV Studio rollback','YesNo','Question')
    if ($taskChoice -ne 'Yes') { exit 0 }
    $taskActive=Join-Path $PSScriptRoot 'settings\active.json'
    $taskCurrent=Get-Content -LiteralPath $taskActive -Raw
    $taskPrevious | ConvertTo-Json | Set-Content -LiteralPath ($taskActive+'.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($taskActive+'.tmp') -Destination $taskActive -Force
    $taskCurrent | Set-Content -LiteralPath $taskRecord -Encoding UTF8
    Start-Process -FilePath (Join-Path $PSScriptRoot 'UAV_Studio.exe') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
} catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'UAV Studio rollback') | Out-Null; exit 1 }
