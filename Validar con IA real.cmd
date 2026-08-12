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

echo Comprobando un flujo breve con tu modelo local de IA...
echo Esta prueba puede tardar varios minutos, pero no envia documentos a Internet.
echo.
"%PARSEZEN_PYTHON%" scripts\validate_real_workflows.py %*
set "PARSEZEN_RESULT=%ERRORLEVEL%"
echo.

if "%PARSEZEN_RESULT%"=="0" (
    echo COMPROBACION REAL SUPERADA
) else (
    echo LA COMPROBACION REAL NECESITA ATENCION
)

echo.
pause
exit /b %PARSEZEN_RESULT%
