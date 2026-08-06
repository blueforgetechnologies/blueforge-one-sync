@echo off
setlocal EnableExtensions
title Blueforge Cloud - Remove from Sidebar
set "GUID={BF01C10D-5A7E-4B2C-9D31-0BF0E17C10D5}"

echo(
echo   Removing "Blueforge Cloud" from the File Explorer sidebar...
echo   (Your files are NOT touched - only the left-pane shortcut.)

reg delete "HKCU\Software\Classes\CLSID\%GUID%" /f >nul 2>&1
reg delete "HKCU\Software\Classes\Wow6432Node\CLSID\%GUID%" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\%GUID%" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel" /v "%GUID%" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\ClassicStartMenu" /v "%GUID%" /f >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -Namespace W -Name S -MemberDefinition '[DllImport(\"shell32.dll\")] public static extern void SHChangeNotify(int e, uint f, System.IntPtr a, System.IntPtr b);';[W.S]::SHChangeNotify(0x08000000,0,[IntPtr]::Zero,[IntPtr]::Zero)" >nul 2>&1

echo(
echo   Removed. Your files are safe.
echo(
pause
exit /b 0
