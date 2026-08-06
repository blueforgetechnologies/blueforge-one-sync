@echo off
setlocal EnableExtensions
title Blueforge Cloud - Add to Sidebar
set "GUID={BF01C10D-5A7E-4B2C-9D31-0BF0E17C10D5}"
set "APPDIR=%USERPROFILE%\.blueforge-cloud-sync"
set "ICOURL=https://yzmfawkhpifmihtablha.supabase.co/storage/v1/object/public/downloads/desktop/blueforge.ico"
if not exist "%APPDIR%" mkdir "%APPDIR%" >nul 2>&1

echo(
echo   Blueforge Cloud - add to File Explorer sidebar
echo   ----------------------------------------------

rem --- 1) Work out which folder to show (from the app's config, else default) ---
set "FOLDER="
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='';try{if(Test-Path '%APPDIR%\config.json'){$j=Get-Content -Raw '%APPDIR%\config.json'|ConvertFrom-Json;if($j.local_folder){$f=$j.local_folder}}}catch{};if(-not $f){$f=Join-Path $env:USERPROFILE 'Blueforge Cloud'};$p=Split-Path $f -Parent;if((Split-Path $p -Leaf) -eq 'Blueforge Cloud'){$f=$p};if(-not(Test-Path $f)){New-Item -ItemType Directory -Force -Path $f|Out-Null};Set-Content -Path \"$env:TEMP\bffolder.txt\" -Value $f -NoNewline -Encoding ASCII"
if exist "%TEMP%\bffolder.txt" set /p FOLDER=<"%TEMP%\bffolder.txt"
del "%TEMP%\bffolder.txt" >nul 2>&1
if not defined FOLDER set "FOLDER=%USERPROFILE%\Blueforge Cloud"
echo   Folder : %FOLDER%

rem --- 2) Get the logo to a stable spot: use one beside this file if present
rem        (when extracted), else download it; else fall back to a Windows icon ---
set "ICO=%APPDIR%\blueforge.ico"
if exist "%~dp0blueforge.ico" copy /y "%~dp0blueforge.ico" "%ICO%" >nul 2>&1
if not exist "%ICO%" powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -UseBasicParsing '%ICOURL%' -OutFile '%ICO%'}catch{}"
set "ICONREF=%ICO%,0"
if not exist "%ICO%" set "ICONREF=%SystemRoot%\System32\imageres.dll,-1043"

rem --- 3) Register the navigation-pane entry (HKCU, no admin) ---
call :writeclsid "HKCU\Software\Classes\CLSID"
call :writeclsid "HKCU\Software\Classes\Wow6432Node\CLSID"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\%GUID%" /ve /d "Blueforge Cloud" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel" /v "%GUID%" /t REG_DWORD /d 1 /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\ClassicStartMenu" /v "%GUID%" /t REG_DWORD /d 1 /f >nul

rem --- 4) Tell Explorer to refresh so it appears now ---
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -Namespace W -Name S -MemberDefinition '[DllImport(\"shell32.dll\")] public static extern void SHChangeNotify(int e, uint f, System.IntPtr a, System.IntPtr b);';[W.S]::SHChangeNotify(0x08000000,0,[IntPtr]::Zero,[IntPtr]::Zero)" >nul 2>&1

echo(
echo   Done. Open a new File Explorer window ^(Windows key + E^).
echo   "Blueforge Cloud" is now on the left, with the cloud logo.
echo(
pause
exit /b 0

:writeclsid
set "R=%~1\%GUID%"
reg add "%R%" /ve /d "Blueforge Cloud" /f >nul
reg add "%R%" /v "InfoTip" /d "Your Blueforge Cloud files" /f >nul
reg add "%R%" /v "System.IsPinnedToNameSpaceTree" /t REG_DWORD /d 1 /f >nul
reg add "%R%" /v "SortOrderIndex" /t REG_DWORD /d 0x42 /f >nul
reg add "%R%\DefaultIcon" /ve /d "%ICONREF%" /f >nul
reg add "%R%\Instance" /v "CLSID" /d "{0E5AAE11-A475-4c5b-AB00-C66DE400274E}" /f >nul
reg add "%R%\Instance\InitPropertyBag" /v "Attributes" /t REG_DWORD /d 0x11 /f >nul
reg add "%R%\Instance\InitPropertyBag" /v "TargetFolderPath" /d "%FOLDER%" /f >nul
reg add "%R%\ShellFolder" /v "Attributes" /t REG_DWORD /d 0xF080004D /f >nul
reg add "%R%\ShellFolder" /v "FolderValueFlags" /t REG_DWORD /d 0x28 /f >nul
exit /b 0
