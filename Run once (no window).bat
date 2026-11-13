@echo off
cd /d "%~dp0"
rem Same as pressing Make clips, without opening the app. Pass a number to run several interviews.
if exist ".venv\installed.txt" goto run
echo Double-click Launch Shoot Clipper.bat once first, so it can install everything.
pause
exit /b 1
:run
".venv\Scripts\python.exe" autopilot.py %*
pause
