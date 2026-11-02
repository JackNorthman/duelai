@echo off
title DUELAI
cd /d "%~dp0"
rem Optional: set STBC_GAME_ROOT, OLLAMA_URL and ANTHROPIC_API_KEY here or in
rem your user environment (or put game_root / api_key in data\config.json).
rem set STBC_GAME_ROOT=C:\Games\Star Trek Bridge Commander
echo Starting DUELAI on http://localhost:8790 ...
start "" http://localhost:8790
python server.py
pause
