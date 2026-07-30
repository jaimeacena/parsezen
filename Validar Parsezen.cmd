@echo off
setlocal
cd /d "%~dp0"

set "PARSEZEN_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PARSEZEN_PYTHON%" (
    echo Parsezen todavia no esta instalado en esta carpeta.
    echo Pide a Codex que prepare el entorno de desarrollo.
    pause
    exit /b 1
)

echo Comprobando las funciones basicas de Parsezen...
echo.
"%PARSEZEN_PYTHON%" -m pytest -m acceptance
set "PARSEZEN_RESULT=%ERRORLEVEL%"
echo.

if "%PARSEZEN_RESULT%"=="0" (
    echo COMPROBACION SUPERADA
) else (
    echo LA COMPROBACION NECESITA ATENCION
)

echo.
pause
exit /b %PARSEZEN_RESULT%
