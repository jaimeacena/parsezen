@echo off
setlocal
cd /d "%~dp0"

set "PARSEZEN_PYTHON=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PARSEZEN_PYTHON%" (
    echo Parsezen todavia no esta instalado en esta carpeta.
    echo Pide a Codex que prepare el entorno de desarrollo.
    pause
    exit /b 1
)

rem Always import Parsezen from this checkout, even if the virtual environment
rem still contains an older non-editable installation.
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
start "" "%PARSEZEN_PYTHON%" -m parsezen
if errorlevel 1 (
    echo Windows no pudo abrir Parsezen.
    pause
    exit /b 1
)

endlocal
