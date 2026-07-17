@echo off
REM GaIA - Script para ejecutar la aplicación Papers_Asistente
REM Abre el navegador y el servidor en el puerto 8901

REM Establece el título de la ventana
title GaIA

REM Sitúate en el directorio del script
cd /d "%~dp0"

REM Intenta activar el entorno virtual si existe
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

REM Abre el navegador
start http://127.0.0.1:8901/app

REM Levanta el servidor en primer plano (mostrará los logs en esta ventana)
python -m uvicorn backend:app --port 8901
