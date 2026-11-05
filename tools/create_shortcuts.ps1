$ErrorActionPreference = 'Stop'
$studioRoot = Split-Path $PSScriptRoot -Parent
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shell.CreateShortcut((Join-Path $folder 'UAV Tarp Studio.lnk'))
    $shortcut.TargetPath = Join-Path $studioRoot 'UAV_Studio.exe'
    $shortcut.WorkingDirectory = $studioRoot
    $shortcut.IconLocation = (Join-Path $studioRoot 'assets\studio.ico') + ',0'
    $shortcut.Description = 'UAV Tarp Studio'
    $shortcut.Save()
}
