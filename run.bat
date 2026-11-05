@echo off
setlocal
title Household Finance
cd /d "%~dp0"

echo.
echo  Household Finance - starting...
echo.

REM ---- find a real Python (py launcher first; skip the Microsoft Store stub)
set "PY="
where py >nul 2>nul && (py -3 -c "import sys" >nul 2>nul && set "PY=py -3")
if not defined PY (
  where python >nul 2>nul && (python -c "import sys" >nul 2>nul && set "PY=python")
)
if not defined PY (
  echo  [!] Python was not found.
  echo.
  echo      Install Python 3.10 or newer from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH" on the first screen of the installer.
  echo      Then double-click run.bat again.
  echo.
  echo      If you already installed it: Settings ^> Apps ^> Advanced app settings ^>
  echo      App execution aliases, and turn OFF the two "python" entries.
  goto :fail
)
for /f "delims=" %%v in ('%PY% -c "import sys;print('%%d.%%d'%%sys.version_info[:2])"') do set "PYVER=%%v"
echo  Using Python %PYVER%
%PY% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" || (
  echo  [!] Python %PYVER% is too old. Install 3.10 or newer from python.org.
  goto :fail
)

REM ---- (re)create the virtual environment if it is missing or broken
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import streamlit, pandas, plotly, requests, openpyxl, pypdf, keyring, defusedxml" >nul 2>nul && goto :run
  echo  Installing new or missing packages...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  ".venv\Scripts\python.exe" -c "import streamlit, pandas, plotly, requests, openpyxl, pypdf, keyring, defusedxml" >nul 2>nul && goto :run
  echo  Previous setup was incomplete - rebuilding...
  rmdir /s /q .venv
)
echo  First run: setting up (this takes a minute or two)...
%PY% -m venv .venv || (echo  [!] Could not create the virtual environment. & goto :fail)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (
  echo  [!] Installing packages failed. Check your internet connection and try again.
  goto :fail
)

:run
echo.
echo  Opening http://localhost:8501 in your browser.
echo  Keep this window open while you use the app; close it to stop the app.
echo.
REM open the browser once the server is up (headless mode skips Streamlit's email prompt)
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8501"
".venv\Scripts\python.exe" -m streamlit run app.py --server.headless true
if errorlevel 1 goto :fail
goto :eof

:fail
echo.
echo  Something went wrong. Copy the messages above and send them to Claude.
echo.
pause
exit /b 1
