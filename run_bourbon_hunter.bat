@echo off
cd /d "C:\Users\tbene\Projects\bourbon-hunter"
call venv\Scripts\activate.bat
python pipeline.py
echo.
echo ============================================================
echo Run complete. Press any key to close.
pause >nul