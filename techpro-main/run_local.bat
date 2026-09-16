@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 從現有資料建立網站...
python scripts\build_web_data.py --skip-collect || goto :error
echo [2/2] 本機網站已啟動：http://localhost:8000
start "" http://localhost:8000
python -m http.server 8000 --directory web
goto :eof
:error
echo 執行失敗，請確認已安裝 Python 3.10 或以上版本。
pause
