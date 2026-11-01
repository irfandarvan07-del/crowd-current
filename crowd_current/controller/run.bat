@echo off
title Crowd Current Controller
cd /d "%~dp0"
echo =======================================================
echo   Starting Crowd Current Controller...
echo   Web Dashboard: http://localhost:8000
echo =======================================================
echo.
python main.py
echo.
echo Process ended.
pause

