@echo off
REM StockPilot daily run — invoked by Windows Task Scheduler.
REM Refreshes data, pushes to PythonAnywhere, reloads the site, emails digest.

cd /d "%~dp0"
if not exist logs mkdir logs

REM Dated log file: logs\daily_YYYY-MM-DD.log
for /f "tokens=2 delims==." %%I in ('wmic os get LocalDateTime /value') do set DT=%%I
set LOGDATE=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%
set LOGFILE=logs\daily_%LOGDATE%.log

echo Starting StockPilot daily run, logging to %LOGFILE%
"C:\Python310\python.exe" daily_update.py >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo Daily run finished with exit code %RC% (see %LOGFILE%)
exit /b %RC%
