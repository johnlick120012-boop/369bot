@echo off
title Discord Memecoin Bot
set PYTHONUTF8=1
echo Activating virtual environment...
call .\venv\Scripts\activate.bat
echo Starting Discord bot...
python bot.py
pause
