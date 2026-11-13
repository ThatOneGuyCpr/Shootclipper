@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Shoot Clipper
if /i "%~1"=="--wait" timeout /t 3 /nobreak >nul

rem Shoot Clipper needs Python 3.11, 3.12 or 3.13. Newer versions (3.14, 3.15) have no
rem build of onnxruntime, which the speech-to-text library depends on.
rem Stop the Python install manager from trying to install versions while we probe for them.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "CHECK=import sys; sys.exit(sys.version_info[:2] not in [(3,11),(3,12),(3,13)])"

rem An environment left over from an unsupported Python gets rebuilt.
if not exist ".venv\Scripts\python.exe" goto findpy
".venv\Scripts\python.exe" -c "%CHECK%" >nul 2>&1
if not errorlevel 1 goto packages
echo The app's environment was built with an unsupported Python version. Rebuilding it...
rmdir /s /q ".venv" >nul 2>&1
if not exist ".venv" goto findpy
echo.
echo Couldn't remove the old .venv folder. Close Shoot Clipper if it's open, then try again.
pause
exit /b 1

:findpy
set "PY="
call :trycmd "py -3.12"
call :trycmd "py -3.13"
call :trycmd "py -3.11"
call :trypath "%LocalAppData%\Programs\Python\Python312\python.exe"
call :trypath "%ProgramFiles%\Python312\python.exe"
call :trypath "%LocalAppData%\Programs\Python\Python313\python.exe"
call :trypath "%LocalAppData%\Programs\Python\Python311\python.exe"
call :trycmd "python"
if defined PY goto makevenv

echo.
echo Shoot Clipper needs Python 3.12. The Python on this PC is too new for the
echo speech-to-text library, so 3.12 gets installed alongside it. Your other Python stays as it is.
echo.
choice /c YN /m "Install Python 3.12 now"
if errorlevel 2 goto manualpy
where pymanager >nul 2>&1
if errorlevel 1 goto wingetpy
rem This PC uses the Python install manager, so ask it for 3.12.
pymanager install 3.12
call :trycmd "py -3.12"
if defined PY goto makevenv

:wingetpy
where winget >nul 2>&1
if errorlevel 1 goto manualpy
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
call :trypath "%LocalAppData%\Programs\Python\Python312\python.exe"
call :trycmd "py -3.12"
if defined PY goto makevenv
echo.
echo Python 3.12 is installed. Close this window and double-click Launch Shoot Clipper.bat again.
pause
exit /b 1

:manualpy
echo Download the Windows installer from the page that's opening now (Windows installer, 64-bit),
echo run it, then double-click Launch Shoot Clipper.bat again.
start "" "https://www.python.org/downloads/release/python-31210/"
pause
exit /b 1

:makevenv
echo First run: setting things up. This takes a few minutes.
%PY% -m venv .venv
if errorlevel 1 goto venvfail
if not exist ".venv\Scripts\python.exe" goto venvfail
if exist ".venv\installed.txt" del ".venv\installed.txt"
goto packages

:venvfail
echo.
echo Couldn't create the app's Python environment with %PY%.
pause
exit /b 1

:packages
rem Install or update packages when requirements.txt changes.
fc /b requirements.txt ".venv\installed.txt" >nul 2>&1
if not errorlevel 1 goto ffmpeg
echo Installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 goto pipfail
copy /y requirements.txt ".venv\installed.txt" >nul
goto ffmpeg

:pipfail
echo.
echo Package install failed. Copy everything above and send it over.
pause
exit /b 1

:ffmpeg
where ffmpeg >nul 2>&1
if not errorlevel 1 goto run
echo Installing ffmpeg with winget...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
echo.
echo ffmpeg installed. Close this window and double-click Launch again so Windows sees it.
pause
exit /b 0

:run
start "" ".venv\Scripts\pythonw.exe" app.py
exit /b 0

rem ---- helpers: set PY only if it's still empty and this Python is a supported version ----
:trycmd
if defined PY goto :eof
%~1 -c "%CHECK%" <nul >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

:trypath
if defined PY goto :eof
if not exist "%~1" goto :eof
"%~1" -c "%CHECK%" <nul >nul 2>&1
if errorlevel 1 goto :eof
set PY="%~1"
goto :eof
