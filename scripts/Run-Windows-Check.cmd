@echo off
setlocal
cd /d "%~dp0"
if exist "scripts\windows_workflow_diagnostic.py" goto start
cd ..
:start
echo Impodo Windows diagnostic - synthetic data only
where py >nul 2>&1
if not errorlevel 1 (
    py -3 scripts\windows_workflow_diagnostic.py
    goto done
)
if exist "%USERPROFILE%\Applications\dev-impodo\.tools\uv.exe" (
    "%USERPROFILE%\Applications\dev-impodo\.tools\uv.exe" run --no-project --python 3.12.14 python scripts\windows_workflow_diagnostic.py
    goto done
)
where uv >nul 2>&1
if not errorlevel 1 (
    uv run --no-project --python 3.12.14 python scripts\windows_workflow_diagnostic.py
    goto done
)
where python >nul 2>&1
if not errorlevel 1 (
    python scripts\windows_workflow_diagnostic.py
    goto done
)
echo Python or uv is required. Use the approved Python setup for this laptop.
pause
exit /b 1
:done
set "checkresult=%errorlevel%"
pause
exit /b %checkresult%
