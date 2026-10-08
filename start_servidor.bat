@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Falha ao instalar dependencias. Verifique sua Internet/Python.
  pause
  exit /b 1
)
python servidor_precos.py
pause
