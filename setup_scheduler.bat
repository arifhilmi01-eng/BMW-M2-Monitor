@echo off
REM ============================================================
REM  Car Monitor - Windows Task Scheduler Setup
REM  Right-click this file and choose "Run as administrator"
REM ============================================================

SET TASK_NAME=CarMonitor
SET SCRIPT=%~dp0car_monitor.ps1

echo.
echo ============================================================
echo  Car Monitor - Task Scheduler Setup
echo ============================================================
echo.
echo Task name : %TASK_NAME%
echo Script    : %SCRIPT%
echo Schedule  : Every 2 hours
echo.

schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "powershell.exe -ExecutionPolicy Bypass -NonInteractive -File \"%SCRIPT%\"" ^
    /SC HOURLY ^
    /MO 2 ^
    /RU "%USERNAME%" ^
    /RL HIGHEST ^
    /F

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo  SUCCESS! Task "%TASK_NAME%" registered.
    echo  The scraper runs every 2 hours automatically.
    echo  dashboard.html opens in your browser after each run.
    echo.
    echo  Run it right now:  schtasks /Run /TN "%TASK_NAME%"
    echo  Remove the task:   schtasks /Delete /TN "%TASK_NAME%" /F
    echo.
) ELSE (
    echo.
    echo  ERROR: Could not create the task.
    echo  Right-click this .bat file and choose "Run as administrator".
    echo.
)

pause
