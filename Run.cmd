@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Daily review
rem Double-click = the MENU: full run, System 1 only, System 2 only, System 1 + 2, System 3 only (Mission Control,
rem or Copilot / Azure when setup says so), setup, check.  Every run asks for the BAM Report (.xlsx) and which cases.
rem   Run.cmd run         the same menu (add --mode ... --report ... --scope ... to skip the questions)
rem   Run.cmd setup       paste/edit your paths, URLs, system names and options (saved in sender_config.json)
rem   Run.cmd check       validate everything without sending (add --browser to also test Edge); with the Azure
rem                       reviewer it also checks that Azure answers
rem   Run.cmd plan        dry run of what is in CASES_ROOT now
rem   Run.cmd send-only   Part 3 alone: send the case folders already in CASES_ROOT (general review guidance)
rem   Run.cmd azure-test  LAPTOP ONLY: the Azure reviewer on made-up cases + accuracy report (--case-model, --limit)
rem Keep this folder together on S:.  The only thing asked here is where python.exe is (saved once in local_python.cmd,
rem never committed).

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
rem Shown at once: loading Python and the script from S: takes a few seconds before the script can print anything.
echo Starting...
rem -B no bytecode files, -E ignore PYTHON* variables, -s no user site packages (embedded Python rules).
set "TARGET=%~dp0orchestrator\run_pipeline.py"
if /i "%~1"=="setup" set "TARGET=%~dp03_copilot_sender\copilot_case_sender.py"
if /i "%~1"=="check" set "TARGET=%~dp03_copilot_sender\copilot_case_sender.py"
if /i "%~1"=="plan" set "TARGET=%~dp03_copilot_sender\copilot_case_sender.py"
if /i "%~1"=="send-only" set "TARGET=%~dp03_copilot_sender\copilot_case_sender.py"
if /i "%~1"=="azure-test" set "TARGET=%~dp0orchestrator\azure_test.py"
"%SENDER_PY%" -B -E -s "%TARGET%" %*
set "SENDER_EXIT=%ERRORLEVEL%"
echo.
if "%SENDER_EXIT%"=="0" (echo Finished.) else (echo Ended with code %SENDER_EXIT% - read the message above.)
if not defined SENDER_NOPAUSE pause
exit /b %SENDER_EXIT%
