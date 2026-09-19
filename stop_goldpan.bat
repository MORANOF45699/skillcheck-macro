@echo off
title Skill-check macro - Stop
cd /d "%~dp0"

REM ===== Administrator check (macro runs elevated, so killing it needs admin too) =====
powershell -NoProfile -Command "exit ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
if %errorlevel%==1 goto :is_admin
if "%~1"=="--elevated" (
    echo [!] Could not get Administrator rights - running as normal user.
    timeout /t 3 >nul
    goto :is_admin
)
echo Requesting Administrator rights...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
exit /b
:is_admin

echo =======================================================
echo    Stopping skill-check macro
echo =======================================================
echo.

REM Kill only python processes running goldpan_macro, not other python apps
powershell -NoProfile -Command ^
  "$f = \"Name='pythonw.exe' or Name='python.exe' or Name='py.exe' or Name='pyw.exe'\";" ^
  "$p = Get-CimInstance Win32_Process -Filter $f | Where-Object { $_.CommandLine -match 'goldpan_macro' };" ^
  "if ($p) { $p | ForEach-Object { Write-Host ('  killing PID ' + $_.ProcessId + '  ' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };" ^
  "  Start-Sleep -Milliseconds 600;" ^
  "  $left = Get-CimInstance Win32_Process -Filter $f | Where-Object { $_.CommandLine -match 'goldpan_macro' };" ^
  "  if ($left) { Write-Host ''; Write-Host ('  WARNING: ' + @($left).Count + ' still running') } else { Write-Host ''; Write-Host '  Macro stopped.' } }" ^
  "else { Write-Host '  No macro process found (nothing to stop).' }"

echo.
pause
