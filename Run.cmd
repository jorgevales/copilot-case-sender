@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Copilot Case Sender
rem Double-click to send the queue, or run:  Run.cmd setup | check | plan | run
rem Keep this file and copilot_case_sender.py together in a folder on S:.
rem The only thing asked here is where python.exe is (saved once in local_python.cmd, never committed).
rem Everything else is asked by "setup" and saved in sender_config.json.

set "SENDER_PY="
if exist "%~dp0local_python.cmd" call "%~dp0local_python.cmd"
rem Quick fallbacks if nothing is saved yet: a python folder beside this one, or one level up.
if not defined SENDER_PY if exist "%~dp0python\python.exe" set "SENDER_PY=%~dp0python\python.exe"
if not defined SENDER_PY if exist "%~dp0..\python\python.exe" set "SENDER_PY=%~dp0..\python\python.exe"

:askpython
if defined SENDER_PY if exist "%SENDER_PY%" goto havepython
echo.
echo Where is the embedded Python? Paste the full path of python.exe (quotes are fine).
echo   Tip: Shift + right-click python.exe, then "Copy as path".
set "SENDER_PY="
set /p "SENDER_PY=python.exe: "
if not defined SENDER_PY goto askpython
set "SENDER_PY=%SENDER_PY:"=%"
if not exist "%SENDER_PY%" (echo That file was not found. & set "SENDER_PY=" & goto askpython)
>"%~dp0local_python.cmd" echo set "SENDER_PY=%SENDER_PY%"

:havepython
rem -B no bytecode files, -E ignore PYTHON* variables, -s no user site packages (embedded Python rules).
"%SENDER_PY%" -B -E -s "%~dp0copilot_case_sender.py" %*
set "SENDER_EXIT=%ERRORLEVEL%"
echo.
if "%SENDER_EXIT%"=="0" (echo Finished.) else (echo Ended with code %SENDER_EXIT% - read the message above.)
if not defined SENDER_NOPAUSE pause
exit /b %SENDER_EXIT%
