@echo off
title Skill-check macro (HUD)
cd /d "%~dp0"

REM ===== Administrator check (same method as Herring) =====
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

py -3 -m pip install -q pydirectinput keyboard mss numpy opencv-python pillow

REM no console window - HUD only
where pyw >nul 2>&1
if %errorlevel%==0 (set PYW=pyw) else (set PYW=pythonw)
start "" %PYW% goldpan_macro.py
exit
