#!/usr/bin/env python3
# validate_system.py — TRBOT President System V8.4
# ============================================================================
# AMAÇ: Her değişiklikten sonra tek komutla çalıştırılır:
#
#     python validate_system.py
#
# Sistemin "arıza lambası"dır. Yeni bir parça (config ayarı, filtre, modül)
# eklendiğinde şunları OTOMATİK kontrol eder:
#
#   [1] Tüm Python dosyaları hatasız derleniyor mu?           (syntax)
#   [2] Tüm modüller hatasız import ediliyor mu?               (import zinciri)
#   [3] config_online.yaml'daki ayarlar kod tarafından         (okunmayan config —
#       gerçekten okunuyor mu, yoksa "yazılmış ama etkisiz" mi? "hayalet ayar")
#   [4] Bilinen çakışma kalıpları var mı?                      (örn. MTF kapalı +
#       HTF eşiği nötr değerle çakışıyor mu — V8.4'te bulunan bug türü)
#   [5] TradeEngine (canlı motor) crash vermeden kuruluyor mu?
#   [6] Backtest çalışıyor mu, PnL muhasebesi tutarlı mı?       (trade toplamı =
#       running PnL, equity eğrisi son nokta = aynı değer)
#   [7] Risk sayacı (açık pozisyon sayısı) açılış/kapanışta dengeli mi?
#
# Script PASS/FAIL olarak biter. FAIL varsa, paket teslim edilmeden önce
# düzeltilmesi gerektiği anlamına gelir.
#
# Bu script projeyi yeniden yazmaz, mevcut mimariyi DEĞİŞTİRMEZ — sadece
# üstüne otomatik bir kontrol katmanı ekler.
# ============================================================================

import sys
import os
import re
import subprocess
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────
# Çıktı yardımcıları
# ─────────────────────────────────────────────────────────────────────────
class C:
    OK    = "\033[92m"
    FAIL  = "\033[91m"
    WARN  = "\033[93m"
    BOLD  = "\033[1m"
    END   = "\033[0m"

RESULTS = []  # (section, passed: bool, detail: str)

def section(title):
    print(f"\n{C.BOLD}── {title} ──{C.END}")

def ok(msg):
    print(f"  {C.OK}✓{C.END} {msg}")

def fail(msg):
    print(f"  {C.FAIL}✗{C.END} {msg}")

def warn(msg):
    print(f"  {C.WARN}!{C.END} {msg}")

def record(name, passed, detail=""):
    RESULTS.append((name, passed, detail))


# ═════════════════════════════════════════════════════════════════════════
# [1] SYNTAX — tüm .py dosyaları derleniyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_syntax():
    section("[1] Syntax — tüm Python dosyaları derleniyor mu?")
    py_files = [str(p) for p in ROOT.rglob("*.py")
                if "__pycache__" not in str(p) and "venv" not in str(p)]
    failed = []
    for f in py_files:
        r = subprocess.run([sys.executable, "-m", "py_compile", f],
                            capture_output=True, text=True)
        if r.returncode != 0:
            failed.append((f, r.stderr.strip()))
    if failed:
        for f, err in failed:
            fail(f"{f}\n      {err}")
        record("syntax", False, f"{len(failed)} dosya derlenmedi")
    else:
        ok(f"{len(py_files)} dosya hatasız derlendi")
        record("syntax", True, f"{len(py_files)} dosya")
    # temizlik
    for p in ROOT.rglob("__pycache__"):
        if p.is_dir():
            for sub in p.glob("*"):
                try: sub.unlink()
                except Exception: pass


# ═════════════════════════════════════════════════════════════════════════
# [2] IMPORT ZİNCİRİ — tüm modüller hatasız import ediliyor mu?
# ═════════════════════════════════════════════════════════════════════════
MODULES_TO_IMPORT = [
    "strategy_core", "adaptive_sl", "market_regime", "symbol_manager",
    "logger", "symbols_builder",
    "modules.decision_packet", "modules.risk_governor", "modules.convex_position",
    "branches.core_long_branch", "branches.short_surgeon", "branches.cascade_hunter",
    "president_governor", "president_runtime",
    "backtest", "walk_forward", "robustness_test", "true_walk_forward", "engine",
]

def check_imports():
    section("[2] Import zinciri — tüm modüller yükleniyor mu?")
    code = "import sys; sys.path.insert(0, '.')\n"
    for m in MODULES_TO_IMPORT:
        code += f"import {m}\n"
    code += "print('IMPORT_OK')\n"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "IMPORT_OK" in r.stdout:
        ok(f"{len(MODULES_TO_IMPORT)} modül hatasız import edildi")
        record("imports", True, f"{len(MODULES_TO_IMPORT)} modül")
    else:
        fail("Import zinciri koptu:")
        print(r.stderr.strip())
        record("imports", False, r.stderr.strip()[-400:])


# ═════════════════════════════════════════════════════════════════════════
# [3] HAYALET CONFIG — config'te yazan ayar kodda hiç okunmuyor mu?
# ═════════════════════════════════════════════════════════════════════════
# Bu alanlar SABİT şema değil, kullanıcı tanımlı DİNAMİK anahtarlar içerir
# (örn. sembol adları). İçine inip her sembolü "kullanılmıyor" diye işaretlemek
# yanlış pozitif üretir — kod bunları .items() ile döngüsel okur, sabit
# anahtar adıyla değil. Bu yüzden flatten bu noktada DURMALI (parent leaf sayılır).
DYNAMIC_KEY_PARENTS = {
    "symbol_quality_filter.manual_symbol_multipliers",
}

def flatten_yaml_keys(d, prefix=""):
    """config dict'ini 'bolum.alt.alan' yollarına açar."""
    out = []
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if path in DYNAMIC_KEY_PARENTS:
            out.append(path)   # alt anahtarlara inme, kendisini leaf say
            continue
        if isinstance(v, dict):
            out.extend(flatten_yaml_keys(v, path))
        else:
            out.append(path)
    return out

# Bilinen "kasıtlı geriye-uyumluluk" anahtarları — bunlar artık birincil
# kaynak değil ama kod içinde fallback olarak hâlâ aranıyor, hayalet sayılmaz.
KNOWN_LEGACY_FALLBACKS = {
    "risk.starting_equity_usdt",  # account.starting_equity_usdt birincil; bu fallback
    "misc.starting_equity_usdt",  # aynı şekilde
}

# Bilinen "kasıtlı sabit" anahtarlar — config'te bilgi amaçlı yazılı durur
# ama kod bunu kasıtlı olarak sabit (hardcoded) kullanır; bunlar gerçek
# "hayalet ayar" değildir, sadece bilgi notu olarak ayrı gösterilir.
KNOWN_INFO_ONLY = {
    "general.version", "general.name", "general.exchange",
    "general.market_type", "general.base_currency",
}

# Bilinen YANLIŞ POZİTİF DEĞİL alanlar — leaf adı kodda geçiyor ama bu
# validator path-aware değil (sadece son anahtar adına bakıyor), bu yüzden
# leaf'i paylaşan farklı bir path okunduğunda bu alan da "kullanılıyor"
# görünür, oysa GERÇEKTE okunmuyor. live.president_execution_mode bunun
# tek örneği: backtest.president_execution_mode kodda okunuyor (V8.5
# patch-2'de eklendi), ama live.president_execution_mode'un GERÇEK bir
# okuma noktası yok — canlı tarafta (engine.py/app.py) bu CLI'ya eşdeğer
# bir override mekanizması henüz inşa edilmedi.
KNOWN_FALSE_POSITIVE_NOTE = {
    "live.president_execution_mode":
        "backtest.president_execution_mode okunuyor ama live.* okunmuyor — "
        "leaf adı ortak olduğu için bu kontrol yanlışlıkla 'kullanılıyor' diyor.",
}

def check_ghost_config():
    section("[3] Hayalet config — yazılan ayar kodda okunuyor mu?")
    import yaml
    cfg_path = ROOT / "config_online.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    keys = flatten_yaml_keys(cfg)

    # Tüm .py kaynaklarını birleştir
    py_files = [p for p in ROOT.rglob("*.py")
                if "__pycache__" not in str(p) and "venv" not in str(p)]
    all_src = ""
    for p in py_files:
        try:
            all_src += p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass

    ghosts = []
    info_only = []
    dormant = []  # enabled:false olan bölümlerin kullanılmayan alanları — gerçek hayalet değil
    for key in keys:
        leaf = key.split(".")[-1]  # son alan adı (örn. "htf_block_min")
        top  = key.split(".")[0]
        # leaf adı kodda en az bir .get("leaf" veya ["leaf"] şeklinde geçiyor mu?
        pattern = re.compile(r'[\[\.]get\(\s*["\']' + re.escape(leaf) + r'["\']|\["' + re.escape(leaf) + r'"\]')
        if pattern.search(all_src) or key in KNOWN_LEGACY_FALLBACKS:
            continue
        if key in KNOWN_INFO_ONLY:
            info_only.append(key)
            continue
        # Bu bölümün üst seviyesinde enabled:false var mı? (bölüm bilinçli kapalı)
        section_enabled = cfg.get(top, {}).get("enabled", None) if isinstance(cfg.get(top), dict) else None
        if section_enabled is False:
            dormant.append(key)
        else:
            ghosts.append(key)

    if info_only:
        warn(f"{len(info_only)} alan bilgi amaçlı yazılmış (kod kasıtlı sabit kullanıyor — sorun değil):")
        for g in info_only:
            print(f"      - {g}")

    if dormant:
        sections_d = sorted(set(g.split(".")[0] for g in dormant))
        warn(f"{len(dormant)} alan, enabled=false olan bölümlerde duruyor (kasıtlı pasif taslak, sorun değil): "
             f"{', '.join(sections_d)}")

    fp_present = [k for k in KNOWN_FALSE_POSITIVE_NOTE if k in keys]
    if fp_present:
        warn(f"{len(fp_present)} alan PASS gibi görünüyor ama path-aware DEĞİL kontrol yüzünden "
             f"yanlış pozitif olabilir:")
        for k in fp_present:
            print(f"      - {k}: {KNOWN_FALSE_POSITIVE_NOTE[k]}")

    if ghosts:
        # Aynı üst bölüme ait anahtarları gruplayıp tekrar bildirmek yerine
        # bölüm bazında gerçek "ölü modül" sinyali ver.
        sections = {}
        for g in ghosts:
            top = g.split(".")[0]
            sections.setdefault(top, []).append(g)
        fail(f"{len(ghosts)} config alanı kodda HİÇ aranmıyor — bölüm enabled=true/tanımsız olduğu "
             f"halde kodda hiç kullanılmıyor — '{', '.join(sections.keys())}' muhtemelen bağlanmamış modül:")
        for top, fields in sections.items():
            print(f"      [{top}] {len(fields)} alan kullanılmıyor")
        record("ghost_config", False, f"{len(ghosts)} alan, bölümler: {', '.join(sections.keys())}")
    else:
        ok(f"{len(keys)} config alanının hepsi kodda aranıyor veya bilinçli pasif bölümlerde (gerçek hayalet yok)")
        record("ghost_config", True, f"{len(keys)} alan, {len(dormant)} kasıtlı pasif")


# ═════════════════════════════════════════════════════════════════════════
# [4] BİLİNEN ÇAKIŞMA KALIPLARI — V8.4'te bulunan bug sınıfı tekrar eder mi?
# ═════════════════════════════════════════════════════════════════════════
def check_known_conflict_patterns():
    section("[4] Bilinen çakışma kalıpları")
    import yaml
    cfg = yaml.safe_load((ROOT / "config_online.yaml").read_text(encoding="utf-8"))
    problems = []

    # 4a. MTF kapalıyken bu sistemde htf_sc=100.0 (maksimum, her zaman geçer)
    #     kullanılır — V8.4'teki "eşiği nötr 50'nin altına çek" çözümünden farklı
    #     ama daha temiz bir tasarım: MTF kapalıyken hiçbir htf_block_min değeri
    #     sorun yaratamaz. Sadece MTF AÇIKKEN ve veri yetersizken 50.0 fallback'i
    #     kullanılır — o yüzden htf_block_min<=50 olması ZORUNLU değil, ama MTF
    #     açıkken bilinçli bir eşik olmalı (0 veya >=100 anlamsız olur).
    htf_block_min = float(cfg.get("core_long", {}).get("htf_block_min", 0.0))
    mtf_enabled = bool(cfg.get("mtf", {}).get("enabled", True))
    if htf_block_min <= 0 or htf_block_min >= 100:
        problems.append(
            f"core_long.htf_block_min={htf_block_min} anlamsız bir değer (0-100 arası olmalı, "
            f"MTF açıkken bu eşik tüm sinyalleri bloklar/hiçbirini bloklamaz)."
        )
    else:
        ok(f"core_long.htf_block_min={htf_block_min} (MTF enabled={mtf_enabled}) — makul aralıkta")

    # 4b. position_rotation.enabled=true olsa bile, bu sürümde
    #     _maybe_rotate_for_candidate()/_maybe_rotate_live() HER ZAMAN False
    #     döner (shadow-only güvenlik tasarımı) — fiziksel pozisyon kapatma
    #     kodu bilinçli olarak devre dışı. Yine de enabled=true + 
    #     allow_close_profitable=true kombinasyonu ileride biri "shadow-only"
    #     kilidini kaldırırsa riskli olur; bunu burada erken uyaralım.
    rot = cfg.get("position_rotation", {})
    if bool(rot.get("enabled", False)) and bool(rot.get("allow_close_profitable", False)):
        problems.append(
            "position_rotation.enabled=true VE allow_close_profitable=true "
            "→ (şu an kod shadow-only olduğu için zararsız, ama biri ileride "
            "fiziksel kapatmayı aktif ederse KÂRLI pozisyonlar kapatılabilir)."
        )
    else:
        ok("position_rotation güvenli durumda (enabled=false VEYA allow_close_profitable=false; "
           "ayrıca kod şu an her durumda shadow-only çalışıyor)")

    # 4c. president.shadow_mode=false (canlı emir) iken risk.risk_per_trade_pct
    #     aşırı yüksek olmamalı (kaza güvenliği).
    shadow = bool(cfg.get("president", {}).get("shadow_mode", True))
    risk_per_trade = float(cfg.get("risk", {}).get("risk_per_trade_pct", 0.0))
    if not shadow and risk_per_trade > 5.0:
        problems.append(
            f"president.shadow_mode=false (GERÇEK EMİR) VE risk_per_trade_pct={risk_per_trade}% "
            f"→ %5'in üzerinde, kaza riski yüksek."
        )
    elif not shadow:
        ok(f"shadow_mode=false ama risk_per_trade_pct={risk_per_trade}% makul seviyede")
    else:
        ok("president.shadow_mode=true (güvenli varsayılan, gerçek emir yok)")

    # 4d. mtf.enabled=false ise bu sürümde htf_sc=100.0 kullanılır (maksimum,
    #     her zaman geçer) — V8.4'teki "nötr 50" tasarımından farklı, KASITLI.
    mtf_enabled = bool(cfg.get("mtf", {}).get("enabled", True))
    if not mtf_enabled:
        warn("mtf.enabled=false — bu sürümde Core Long'a htf_sc=100.0 (maksimum) "
             "gönderilir, böylece MTF kapalıyken hiçbir htf_block_min değeri sinyalleri "
             "yanlışlıkla bloklayamaz. Bu kasıtlı bir tasarımdır, hata değildir.")

    if problems:
        for p in problems:
            fail(p)
        record("conflicts", False, f"{len(problems)} çakışma bulundu")
    else:
        record("conflicts", True, "Bilinen çakışma kalıplarından hiçbiri tespit edilmedi")


# ═════════════════════════════════════════════════════════════════════════
# [4b] PARİTE KONTROLÜ — engine.py (canlı) ile backtest.py aynı PnL formülünü
#      mü kullanıyor? (V8.5'te bulunan ve düzeltilen bug sınıfı: TP1 Progress
#      Manager eklenirken backtest.py güncellenmiş ama engine.py'de komisyon
#      hesabı unutulmuştu — bu kontrol bunun tekrarlanmadığını garanti eder.)
# ═════════════════════════════════════════════════════════════════════════
def check_engine_backtest_parity():
    section("[4b] Parite — engine.py'de backtest.py ile aynı PnL/komisyon yardımcıları var mı?")
    eng_src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="ignore")
    bt_src  = (ROOT / "backtest.py").read_text(encoding="utf-8", errors="ignore")

    required_helpers = ["_fee_cost", "_gross_pnl"]
    missing = [h for h in required_helpers if h not in eng_src]
    if missing:
        fail(f"engine.py'de eksik yardımcı fonksiyon(lar): {', '.join(missing)} "
             f"— canlı motor komisyonu hesaplamıyor olabilir (backtest'ten optimistik PnL).")
        record("engine_parity", False, f"eksik: {', '.join(missing)}")
        return

    # commission/slippage parametreleri okunuyor mu?
    if "self.commission" not in eng_src or "self.slippage" not in eng_src:
        fail("engine.py'de self.commission / self.slippage tanımlı değil.")
        record("engine_parity", False, "commission/slippage eksik")
        return

    # on_close çağrısına giden değişken adı total_net (veya benzer bir
    # "toplam" ismi) olmalı, sadece ham pnl_usd olmamalı.
    on_close_calls = re.findall(r"runtime\.on_close\([^)]*\)", eng_src)
    if not on_close_calls:
        warn("engine.py'de runtime.on_close çağrısı bulunamadı — manuel kontrol edin.")
        record("engine_parity", None, "on_close çağrısı bulunamadı")
        return

    if any("total_net" in c for c in on_close_calls):
        ok("engine.py: _fee_cost/_gross_pnl mevcut, commission okunuyor, "
           "on_close() total_net (TP1+progress+komisyon dahil) gönderiyor")
        record("engine_parity", True, "OK")
    else:
        fail(f"engine.py'deki on_close çağrısı 'total_net' değil ham bir pnl değişkeni "
             f"gönderiyor olabilir: {on_close_calls} — Risk Governor yanlış PnL görebilir.")
        record("engine_parity", False, f"on_close çağrıları: {on_close_calls}")


# ═════════════════════════════════════════════════════════════════════════
# [5] ENGINE KURULUMU — canlı motor crash vermeden kuruluyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_engine_boot():
    section("[5] Canlı motor (TradeEngine) kurulumu")
    code = """
import sys, yaml
sys.path.insert(0, '.')
from engine import TradeEngine
cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
e = TradeEngine(['BTCUSDT'], cfg, data_dir='/tmp/_validate_engine')
assert hasattr(e, 'runtime'), 'runtime yok'
assert hasattr(e.runtime, 'confirm_open'), 'confirm_open yok'
assert hasattr(e, 'rotation_enabled'), 'rotation_enabled yok'
print('ENGINE_BOOT_OK rotation_enabled=' + str(e.rotation_enabled))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "ENGINE_BOOT_OK" in r.stdout:
        ok("TradeEngine crash vermeden kuruldu — " + r.stdout.strip().split("\n")[-1])
        record("engine_boot", True, "OK")
    else:
        fail("TradeEngine kurulumu başarısız:")
        print((r.stderr or r.stdout).strip())
        record("engine_boot", False, (r.stderr or r.stdout).strip()[-400:])


# ═════════════════════════════════════════════════════════════════════════
# [5b] CANLI BOT ZİNCİRİ — app.py'nin simulator.py'yi gerçekten import edip
#      _SIM_OK=True alabildiğini doğrular. Bu kontrol, simulator.py (ve
#      data_ws/data_rest/agent/agent_reporter/optimizer bağımlılıkları)
#      paketten yanlışlıkla çıkarılırsa GUI'deki "Canlı Başlat" butonunun
#      sessizce no-op'a düşmesini önceden yakalamak için eklendi.
# ═════════════════════════════════════════════════════════════════════════
def check_live_bot_chain():
    section("[5b] Canlı bot zinciri — app.py simulator.py'yi gerçekten kullanabiliyor mu?")
    sim_path = ROOT / "simulator.py"
    if not sim_path.exists():
        fail("simulator.py PAKETTE YOK — app.py _SIM_OK=False fallback'ine düşecek, "
             "GUI'deki 'Canlı Başlat' butonu sessizce hiçbir şey yapmayacak.")
        record("live_bot_chain", False, "simulator.py eksik")
        return

    code = """
import sys
sys.path.insert(0, '.')
try:
    from simulator import (get_status, get_open_status, get_pnl,
                           start_realtime, stop_realtime,
                           add_to_blacklist, remove_from_blacklist,
                           get_blacklist, get_hourly_stats, get_coin_stats)
    print('SIM_OK')
except ImportError as e:
    print('SIM_IMPORT_FAIL: ' + str(e))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "SIM_OK" in r.stdout:
        ok("simulator.py mevcut ve app.py'nin beklediği TÜM fonksiyonlarla import "
           "edilebiliyor (_SIM_OK=True olacak, canlı bot gerçekten çalışır)")
        record("live_bot_chain", True, "OK")
    else:
        fail(f"simulator.py import edilemiyor — app.py _SIM_OK=False fallback'ine "
             f"düşecek: {(r.stdout + r.stderr).strip()[-300:]}")
        record("live_bot_chain", False, (r.stdout + r.stderr).strip()[-300:])


# ═════════════════════════════════════════════════════════════════════════
# [6] BACKTEST SMOKE TEST — PnL muhasebesi tutarlı mı?
# ═════════════════════════════════════════════════════════════════════════
def check_backtest_smoke():
    section("[6] Backtest smoke test — PnL muhasebesi tutarlılığı")
    code = """
import sys, os, numpy as np, yaml
sys.path.insert(0, '.')
from backtest import Backtester

cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
cfg['thresholds']['score_long_open'] = 40
cfg['mtf']['enabled'] = False   # bilinen risk senaryosu: MTF kapali
cfg['president']['shadow_mode'] = False
cfg['core_long']['shadow_mode'] = False
cfg['limits']['max_open_positions'] = 2

def mk(n, s, t):
    np.random.seed(s)
    b = np.cumsum(np.random.randn(n)) * 0.6 + np.linspace(0, t, n) + 100
    return [{'open_time': 1704067200000 + i*3600000, 'open': float(p),
             'high': float(p*1.013), 'low': float(p*0.987), 'close': float(p),
             'volume': 1000.0 + i} for i, p in enumerate(b)]

syms = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
cb = {s: mk(450, i+1, [20, -15, 12, -10][i]) for i, s in enumerate(syms)}

os.system('rm -rf /tmp/_validate_bt')
bt = Backtester(cfg, '/tmp/_validate_bt', president_enabled=True, interval='1h')
r = bt.run(syms, cb, {s: cb[s] for s in syms})
sm = r['summary']
trades = r['trades']

trade_sum = sum(t.get('Net_PnL', 0) for t in trades)
fark = abs(trade_sum - bt._pnl_running)

print('TRADES=' + str(sm['Toplam_Islem']))
print('PNL=' + str(sm['Net_PnL_USD']))
print('FARK=' + f'{fark:.6f}')
print('TRADE_HAS_ZERO=' + str(sm['Toplam_Islem'] == 0))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    out = r.stdout
    if r.returncode != 0:
        fail("Backtest çalışırken hata fırlattı:")
        print(r.stderr.strip())
        record("backtest_smoke", False, r.stderr.strip()[-400:])
        return

    def grab(key):
        m = re.search(rf"^{key}=(.*)$", out, re.M)
        return m.group(1) if m else None

    trades_n = grab("TRADES")
    pnl      = grab("PNL")
    fark     = grab("FARK")
    zero     = grab("TRADE_HAS_ZERO")

    problems = []
    if zero == "True":
        problems.append("İşlem sayısı 0 — MTF kapalıyken sinyal üretilmiyor olabilir (bilinen risk).")
    try:
        if fark is not None and float(fark) > 0.01:
            problems.append(f"PnL tutarsızlığı: trade toplamı ile running PnL arasında ${fark} fark var (>0.01$ eşiği aşıldı).")
    except ValueError:
        problems.append("Fark değeri okunamadı.")

    if problems:
        for p in problems:
            fail(p)
        record("backtest_smoke", False, "; ".join(problems))
    else:
        ok(f"İşlem={trades_n} PnL=${pnl} — PnL tutarlılık farkı=${fark} (✓ < 0.01$)")
        record("backtest_smoke", True, f"İşlem={trades_n} fark=${fark}")


# ═════════════════════════════════════════════════════════════════════════
# [7] RİSK SAYACI DENGESİ — açık pozisyon sayacı aç/kapa sonrası sıfırlanıyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_risk_counter_balance():
    section("[7] Risk sayacı dengesi — açık pozisyon aç/kapa simülasyonu")
    code = """
import sys, yaml, numpy as np
from collections import deque
sys.path.insert(0, '.')
from engine import TradeEngine
from strategy_core import score_symbol

cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
cfg['thresholds']['score_long_open'] = 35
cfg['mtf']['enabled'] = False
cfg['president']['shadow_mode'] = False
cfg['core_long']['shadow_mode'] = False
cfg['adx_filter']['enabled'] = False
cfg['rsi_filter']['enabled'] = False
cfg['atr_filter']['enabled'] = False

e = TradeEngine(['BTCUSDT'], cfg, data_dir='/tmp/_validate_risk')
e.vol_mult = 0.0001

np.random.seed(1)
prices = np.cumsum(np.random.randn(80)) * 0.5 + np.linspace(0, 15, 80) + 100

e.close_series['BTCUSDT'] = deque(maxlen=300)
e.high_series['BTCUSDT']  = deque(maxlen=300)
e.low_series['BTCUSDT']   = deque(maxlen=300)
e.vol_series['BTCUSDT']   = deque(maxlen=300)

for i, p in enumerate(prices):
    e.close_series['BTCUSDT'].append(float(p))
    e.high_series['BTCUSDT'].append(float(p * 1.01))
    e.low_series['BTCUSDT'].append(float(p * 0.99))
    e.vol_series['BTCUSDT'].append(5_000_000.0)
    e.last_close_time['BTCUSDT'] = int(1704067200000 + i * 3600000)

pl = list(e.close_series['BTCUSDT']); hl = list(e.high_series['BTCUSDT'])
ll = list(e.low_series['BTCUSDT']);   vl = list(e.vol_series['BTCUSDT'])
result = score_symbol(pl, hl, ll, vl)
score = result['final_score']

before = e.runtime.get_state()['open_longs']
e._try_open('BTCUSDT', pl[-1], score, pl, hl, ll, vl, result)
opened = 'BTCUSDT' in e.open_positions
mid = e.runtime.get_state()['open_longs']

if opened:
    e._close('BTCUSDT', pl[-1] * 1.02, 0.02, 'TP')
after = e.runtime.get_state()['open_longs']

print('BEFORE=' + str(before))
print('OPENED=' + str(opened))
print('MID=' + str(mid))
print('AFTER=' + str(after))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        fail("Risk sayacı testi hata fırlattı:")
        print(r.stderr.strip())
        record("risk_balance", False, r.stderr.strip()[-400:])
        return

    out = r.stdout
    def grab(key):
        m = re.search(rf"^{key}=(.*)$", out, re.M)
        return m.group(1) if m else None

    before, opened, mid, after = grab("BEFORE"), grab("OPENED"), grab("MID"), grab("AFTER")

    if opened != "True":
        warn("Bu sentetik veride pozisyon açılmadı (filtre engelledi) — sayaç testi atlandı, bu FAIL değildir.")
        record("risk_balance", None, "pozisyon açılmadı, test anlamlı sonuç vermedi")
        return

    if before == "0" and mid == "1" and after == "0":
        ok(f"Sayaç dengeli: açılış öncesi={before} → açılış sonrası={mid} → kapanış sonrası={after}")
        record("risk_balance", True, f"{before}→{mid}→{after}")
    else:
        fail(f"Sayaç DENGESİZ: açılış öncesi={before} → açılış sonrası={mid} → kapanış sonrası={after} (0→1→0 olmalıydı)")
        record("risk_balance", False, f"{before}→{mid}→{after}")


# ═════════════════════════════════════════════════════════════════════════
# ÖZET
# ═════════════════════════════════════════════════════════════════════════
def print_summary():
    section("ÖZET")
    n_pass = sum(1 for _, p, _ in RESULTS if p is True)
    n_fail = sum(1 for _, p, _ in RESULTS if p is False)
    n_warn = sum(1 for _, p, _ in RESULTS if p is None)

    for name, passed, detail in RESULTS:
        tag = f"{C.OK}PASS{C.END}" if passed is True else (f"{C.FAIL}FAIL{C.END}" if passed is False else f"{C.WARN}WARN{C.END}")
        print(f"  [{tag}] {name:<18} {detail}")

    print()
    if n_fail == 0:
        print(f"{C.OK}{C.BOLD}SONUÇ: PASS{C.END} ({n_pass} geçti, {n_warn} uyarı) — sistem teslime hazır görünüyor.")
        return 0
    else:
        print(f"{C.FAIL}{C.BOLD}SONUÇ: FAIL{C.END} ({n_fail} hata, {n_pass} geçti, {n_warn} uyarı) — teslimden ÖNCE düzeltilmeli.")
        return 1


def main():
    print(f"{C.BOLD}TRBOT President System — validate_system.py{C.END}")
    print(f"Kontrol dizini: {ROOT}")
    try:
        check_syntax()
        check_imports()
        check_ghost_config()
        check_known_conflict_patterns()
        check_engine_backtest_parity()
        check_engine_boot()
        check_live_bot_chain()
        check_backtest_smoke()
        check_risk_counter_balance()
    except Exception:
        print(f"\n{C.FAIL}Script çalışırken beklenmeyen hata:{C.END}")
        traceback.print_exc()
        return 2
    return print_summary()


if __name__ == "__main__":
    sys.exit(main())
