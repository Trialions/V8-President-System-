@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ============================================
echo TRBOT President System - VENV Kurulum + Baslat
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
  echo [HATA] Python bulunamadi. Python 3.10+ kurup PATH'e ekleyin.
  pause
  exit /b 1
)

if not exist .venv (
  echo [1/4] Sanal ortam olusturuluyor: .venv
  python -m venv .venv
  if errorlevel 1 (
    echo [HATA] Sanal ortam olusturulamadi.
    pause
    exit /b 1
  )
) else (
  echo [1/4] Sanal ortam mevcut: .venv
)

echo [2/4] Sanal ortam aktif ediliyor...
call .venv\Scripts\activate.bat

echo [3/4] Gereksinimler kuruluyor...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [HATA] requirements kurulumu basarisiz.
  pause
  exit /b 1
)

echo [4/4] Kod dogrulama...
python -m compileall -q .
if errorlevel 1 (
  echo [HATA] Python compile kontrolu basarisiz.
  pause
  exit /b 1
)

echo.
echo Kurulum ve kontrol tamam. GUI baslatiliyor...
python app.py
pause
