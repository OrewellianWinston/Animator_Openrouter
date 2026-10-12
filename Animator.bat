@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m animator.gui
) else (
  where pythonw >nul 2>nul && (start "" pythonw -m animator.gui) || (python -m animator.gui)
)
