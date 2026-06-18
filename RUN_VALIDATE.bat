@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat (
  call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
)
echo [1/6] Compile check...
python -m compileall -q . || goto :err
echo [2/6] Config check...
python validate_config.py || goto :err
echo [3/6] Hybrid config check...
python validate_hybrid_config.py || goto :err
echo [4/6] Import chain...
python -c "import engine, backtest, president_runtime, adaptive_exit, block_outcome_analyzer, weekly_symbol_universe, simulator; print('IMPORT_OK')" || goto :err
echo [5/6] SHORT smoke test...
python validate_short_smoke.py || goto :err
echo [6/6] Ranking output smoke test...
python validate_ranking_output_smoke.py || goto :err
echo.
echo VALIDATION OK
pause
exit /b 0
:err
echo.
echo VALIDATION FAILED
pause
exit /b 1
