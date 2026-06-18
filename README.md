# V8-President-System-
#Çok Katmanlı Trade -Bot Uygulaması
# TRBOT — Algoritmik Kripto Trading Botu (V8.5.9)

Binance Spot piyasasında, çoklu-sembol evreninde **LONG-only** çalışan, kural
tabanlı + skor tabanlı hibrit bir algoritmik trading sistemi. Karar verme
mimarisinin merkezinde **President/Governor modeli** var: birden fazla bağımsız
"dal" (branch) oy veriyor, bir "Başkan" (President) bu oyları birleştirip nihai
kararı veriyor, bir "Risk Governor" da bu kararın üstüne son güvenlik katmanını
ekliyor. Aynı karar pipeline'ı (`PresidentRuntime.evaluate()`) backtest, walk-
forward, robustness testi ve canlı trading motorunun **dördünde de birebir
aynı şekilde** çalışıyor — bu, sistemin en temel tasarım ilkesi (parity).

Bu doküman; sistemin ne yaptığını, nasıl karar verdiğini, hangi dosyanın ne işe
yaradığını ve V8.5.8 / V8.5.9 yamalarında neyin neden değiştiğini özetler.

---

## 1. Genel Mimari

```
                         ┌─────────────────────┐
   1h candle  ──────────▶│   strategy_core.py   │  0-100 skor üretir
                         │   score_symbol()      │  (RSI, MACD, BB, EMA,
                         └──────────┬────────────┘   Supertrend, OBV,
                                    │                 divergence, pattern)
                                    ▼
                  ┌─────────────────────────────────┐
                  │   Sert Filtreler (hard block)     │  ADX / BTC korelasyon /
                  │   ADX_TOO_LOW, MTF_NO_CONFIRM,    │  MTF / blacklist /
                  │   blacklist, max_trades_day...    │  günlük limit
                  └──────────────┬────────────────────┘
                                 │ geçen adaylar
                                 ▼
                  ┌─────────────────────────────────┐
                  │   Pump/Manipülasyon Filtresi       │  SOFT: skor -puan,
                  │   pump_filter.py (V8.5.8)          │  boyut küçültme
                  └──────────────┬────────────────────┘  (engelleme YOK)
                                 ▼
        ┌────────────────────────────────────────────────────┐
        │              PresidentRuntime.evaluate()             │
        │  ┌──────────┐ ┌──────────────┐ ┌──────────────────┐ │
        │  │Core Long │ │Short Surgeon │ │ Cascade Hunter    │ │  3 bağımsız
        │  │ branch   │ │ branch       │ │ branch (shadow)   │ │  dal oyu
        │  └────┬─────┘ └──────┬───────┘ └────────┬──────────┘ │
        │       └──────────────┴──────────────────┘            │
        │                      ▼                                │
        │            PresidentGovernor.decide()                 │
        │   • Quality Score (quality_score.py)                  │
        │   • Adaptive Risk Hint                                │
        │   • Multi-faktörlü Label (V8.5.8): score+kalite+rejim  │
        │   • BOA Feedback (geçmiş blok analizinden küçük edge)  │
        │                      ▼                                │
        │              RiskGovernor (son onay)                  │
        └──────────────────────┬─────────────────────────────────┘
                                ▼
                  Action.OPEN / Action.SHADOW / Action.BLOCK
                                ▼
        ┌─────────────────────────────────────────────────────┐
        │  Aynı mumda birden fazla aday varsa: RANKING          │
        │  (candidate_ranking_events.csv) — en iyi aday(lar)    │
        │  seçilir, diğerleri RANK_REJECTED_* ile loglanır      │
        └───────────────────────┬─────────────────────────────┘
                                ▼
                  Position açılır: Adaptive SL/Trail,
                  Adaptive Exit policy, Symbol/Regime size mult
```

Bu pipeline'ın **tamamı** dört farklı "motor" tarafından paylaşılıyor:

| Motor | Dosya | Amaç |
|---|---|---|
| Backtest | `backtest.py` | Tek bir tarih aralığında tam simülasyon |
| Walk-Forward | `walk_forward.py` | Aylık segmentlere bölünmüş ardışık backtest |
| Robustness | `robustness_test.py` | Haftalık segmentlere bölünmüş ardışık backtest |
| Gerçek WF (OOS) | `true_walk_forward.py` | Train→optimize→test→roll, gerçek out-of-sample |
| Canlı | `engine.py` + `simulator.py` | Binance'ten gerçek zamanlı veri, gerçek/paper emir |

Bu beşi de **aynı** `PresidentRuntime` / `PresidentGovernor` / branch kodunu
çağırır — hiçbiri kendi kopyasını tutmaz. Bu "parity" ilkesi, geçmişte birçok
hatanın (komisyon hesaplama, MTF filtresi, PresidentScore boşluğu) kaynağı
olduğu için **kasıtlı ve kritik** bir mimari karardır.

---

## 2. Karar Mekanizması — Detay

### 2.1 Skor Motoru (`strategy_core.py`)
Her sembol/mum için 0-100 arası bir skor üretir. Girdiler:
RSI, MACD, Bollinger Bands, EMA trend yapısı, Supertrend, hacim/OBV,
RSI divergence, mum formasyonu (pattern) tespiti. Bu skor, branch'lerin
oy vermesi için temel girdidir — branch'ler bu ham skoru kendi mantıklarıyla
yorumlayıp (HTF teyit, hacim onayı, cascade/momentum onayı vb.) bir "oy"a
çevirir.

### 2.2 Dallar (Branches)
- **Core Long** (`branches/core_long_branch.py`) — ana, her zaman aktif dal.
  Tek başına oy verdiğinde ("core-only") sinyal daha az teyitli sayılır.
- **Short Surgeon** (`branches/short_surgeon.py`) — SHORT tarafı için var
  ama geçmiş testlerde tutarlı şekilde sorunlu çıktı (bkz. §6), şu an
  düşük öncelikli.
- **Cascade Hunter** (`branches/cascade_hunter.py`) — momentum/cascade
  onayı, çoğunlukla **shadow modda** (gerçek emir vermez, sadece "ne
  olurdu" diye loglar) çalışır.

### 2.3 President — Karar ve Etiketleme (`president_governor.py`)
Branch oyları toplanır, `final_score` hesaplanır, sonra **label** atanır:
`ATTACK > STRONG > NORMAL > SCOUT > WEAK`. Label, pozisyon boyutunu
(`label_size_mults`) doğrudan etkiler.

**V8.5.8 öncesi davranış:** core-only bir sinyal (sadece Core Long oy
verdiğinde), skoru ne olursa olsun **körü körüne** `core_only_max_label`
değerine (NORMAL) sabitleniyordu. Bu, trade'lerin ~%95'inin NORMAL etiketine
yığılmasına yol açıyordu.

**V8.5.8 sonrası (çok faktörlü kalibrasyon):** core-only bir sinyal artık
STRONG'a çıkabilir **EĞER**:
- Quality Score ≥ `core_only_quality_min` (varsayılan 65) **VE**
- rejim "choppy" değilse (`core_only_choppy_regimes`: KONSOL/CHOP/RANGE/BEARISH)

Aksi halde eski korumacı davranış (NORMAL'e düş) korunur. Ayrıca NORMAL
bandındaki sinyaller Quality Score < `low_quality_demote_below` (35) ise
SCOUT'a düşürülür — düşük kaliteli "orta" sinyal artık NORMAL boyutuyla
açılmıyor.

### 2.4 Quality Score (`quality_score.py`)
Skordan bağımsız, "bu sinyal ne kadar güvenilir" diye ayrı bir 0-100 değer.
Hem label kalibrasyonunda (yukarıda) hem pozisyon boyutunda kullanılıyor.

### 2.5 BOA Feedback (`block_outcome_analyzer.py`)
"Blocked Outcome Analysis": geçmişte filtreler tarafından bloklanan
sinyallerin sonradan **gerçekte kazanıp kazanmadığı** analiz edilir. Belirli
sembol/rejim/sebep kombinasyonları sürekli "gereksiz engel" çıkıyorsa,
gelecekteki benzer sinyallere küçük bir pozitif puan eklenir (ya da gerçekten
doğru engelse küçük bir negatif puan). Bu **President kararını asla bypass
etmez** — sadece final_score üzerinde küçük bir ayar yapar.

### 2.6 Pump/Manipülasyon Filtresi — `pump_filter.py` (YENİ, V8.5.8)
Son `price_lookback_bars` (varsayılan 3) mumda **hem** anormal hacim patlaması
(`vol_spike_mult`, 4x) **hem de** sert fiyat sıçraması (`price_spike_pct`, %8)
birlikte görülürse "pump riski" işaretlenir. Davranış **kasıtlı olarak sert
blok değil**:
- Skor `score_penalty` (varsayılan 8 puan) kadar düşürülür,
- Pozisyon açılırsa boyutu `size_mult` (varsayılan 0.5) ile küçültülür.

`backtest.py` ve `engine.py` **aynı** `compute_pump_risk()` fonksiyonunu
çağırır — parity ilkesi burada da korunmuş.

### 2.7 Risk Governor (`modules/risk_governor.py`)
President'ın OPEN dediği bir karar, son kez burada günlük işlem limiti,
eşzamanlı pozisyon limiti gibi portföy-seviyeli kurallardan geçer.

### 2.8 Ranking Sistemi (V8.5.5'ten beri)
Aynı mum içinde **birden fazla sembol** eşzamanlı OPEN sinyali verirse, hepsi
açılmaz — `candidate_ranking_events.csv`'ye loglanan bir sıralama yapılır
(rank skoru = final_score + BOA edge), en iyi aday(lar) açılır, diğerleri
`RANK_REJECTED_*` sebebiyle (BAD_QUALITY, CHOP_RISK, SYMBOL_PENALTY,
LOWER_SCORE) loglanır.

### 2.9 Pozisyon Yönetimi
- **Adaptive SL/Trailing** (`adaptive_sl.py`) — rejime ve ATR%'ye göre stop ve
  trailing step ayarlanır.
- **Adaptive Exit** (`adaptive_exit.py`) — trade'i bir "sınıfa" (`ae_class`)
  ayırıp TP1 close yüzdesi, trail step, max hold süresi gibi politikaları
  belirler. President kararını bypass etmez, sadece yönetim parametrelerini
  ayarlar.
- **TP1 Progress Manager** — TP1 sonrası kademeli azaltma.
- **Symbol Size Multiplier** (`symbol_manager.py`) — sembolün geçmiş
  performansına göre boyut çarpanı; statik blacklist yerine dinamik ceza
  tercih edilir (bkz. §6).
- **Regime Size Multiplier** — NEUTRAL/KONSOL rejimlerde boyut otomatik
  küçültülür.

---

## 3. V8.5.8 Yaması — Neler Eklendi/Düzeltildi

| # | Konu | Ne yapıldı |
|---|---|---|
| 1 | **PresidentScore boşluğu** | `president_runtime.py`, `final_score`'u `pkt.extra["president_score"]`'a hiç yazmıyordu; `backtest_trades.csv`'de bu kolon hep boştu. Tek satır fix. |
| 2 | **Label aşırı yığılması** | Yukarıda §2.3'te açıklanan çok faktörlü kalibrasyon. |
| 3 | **Pump/Manipülasyon Filtresi** | Yeni `pump_filter.py`, soft penalty (engelleme yok), backtest+canlı parity. |
| 4 | **Explainability Paneli** | GUI'de "President Tepkisi" sekmesine "Açıklamalı Görünüm" alt-sekmesi: her trade için karar zincirini düz Türkçe cümleye çeviren kartlar (dal oyları → President skoru/label → kalite → rank → BOA → pump uyarısı → sonuç). Yeni hesaplama yapmaz, var olan veriyi anlatıya çevirir. |
| 5 | **GUI — Analiz paneli inline** | "Analiz Aç" artık popup/slide-over değil, sonucun tam altında sayfa içinde açılıyor. |
| 6 | **GUI — WF/ROB/TWF segment drill-down** | Aylık/haftalık/fold tablosundaki her satır artık tıklanabilir → o segmentin (zaten diskte tam bir Backtester çıkışı olan) kendi detaylı analizi aynı panelde açılıyor. |
| 7 | **GUI — "Klasörü Aç" düğmesi** | Hiç bağlı değildi; `app.py`'de gerçek `open_folder()` (Win/Mac/Linux) eklendi ve tüm sayfalara bağlandı. |
| 8 | **GUI — Ranking/BOA sayfası** | Açıklama barları + Label Dağılımı grafiği eklendi (`label_rows` verisi zaten dönüyordu, gösterilmiyordu). |
| 9 | **Özet sekmesi otomatik yorum** | PnL/winrate/maxDD/Sharpe'a göre deterministik (AI değil, kural tabanlı) bir özet cümle üretiliyor. |

## 4. V8.5.9 Yaması — Kritik Test Hatası Düzeltmesi

**Semptom:** Walk-Forward ve Robustness testlerinde **her segment** (her ay,
her hafta) `Toplam_Islem=0` döndü — strateji hiç trade açmadı. Buna karşılık
aynı dönem için tekli backtest normal çalıştı (96 trade).

**Kök neden:** `config_online.yaml`'da `president.shadow_mode: true` statik
olarak yazılıydı. Bu, President'ı **her kararı SHADOW'a düşürmeye** zorluyor
(`president_governor.py`: `is_shadow = self.shadow or all_shadow`) — yani
strateji mantığı, skorlar, branch oyları tamamen sağlıklı çalışıyor
(`shadow_opportunities.csv`'de yüzlerce geçerli "OPEN olurdu" kararı kanıt
olarak duruyor), ama gerçek pozisyon hiç açılmıyor.

Tekli backtest'in bundan etkilenmemesinin sebebi: GUI, `backtest.py`'yi CLI
olarak çalıştırırken `cfg.backtest.president_execution_mode` (`simulated_active`)
okuyup `shadow_mode`'u **runtime'da** `False`'a çeviren bir override
içeriyordu — ama bu override **sadece** `backtest.py`'nin CLI bloğunda vardı.
`walk_forward.py` / `robustness_test.py` / `true_walk_forward.py`, `Backtester`'ı
doğrudan Python içinde çağırdığı için bu override'dan **hiç geçmiyordu** ve
config'teki statik `true` değerini olduğu gibi devralıyordu.

**Düzeltme:**
1. Override mantığı `backtest.py` içinde `resolve_president_execution_mode()`
   adında paylaşılan bir fonksiyona çıkarıldı.
2. `walk_forward.py`, `robustness_test.py`, `true_walk_forward.py`'nin
   `main()` fonksiyonları artık config'i yükledikten hemen sonra bu
   fonksiyonu çağırıyor — dördü de artık aynı mantığı uyguluyor.
3. Savunma katmanı olarak `config_online.yaml`'daki statik varsayılan da
   `false`'a çekildi — `backtest.president_execution_mode` alanı silinir/
   bozulursa/tanınmazsa diye (bu durumda resolver hiçbir şeye dokunmaz,
   ne yazıyorsa o kalır).

---

## 5. Dosya Haritası

```
president_runtime.py     PresidentRuntime — Quality/Risk hesapla, decide() çağır
president_governor.py    PresidentGovernor — oy birleştirme, label, ATTACK kalibrasyonu
modules/decision_packet.py  BranchVote, DecisionPacket, Action, Side veri sınıfları
modules/risk_governor.py    Portföy seviyeli son onay
modules/convex_position.py  Pozisyon veri modeli
branches/core_long_branch.py / short_surgeon.py / cascade_hunter.py  Oy veren dallar
quality_score.py         Quality Score hesaplama
adaptive_sl.py            Rejime göre SL/trailing
adaptive_exit.py          AE class/policy (TP1, trail, max hold)
market_regime.py          TREND/BULL/BEARISH/KONSOL/CHOP/RANGE/NEUTRAL tespiti
symbol_manager.py         Sembol bazlı boyut çarpanı, blacklist
block_outcome_analyzer.py BOA — bloklanan sinyallerin sonradan analiz edilmesi
pump_filter.py            (V8.5.8) Pump/manipülasyon soft filtresi
strategy_core.py          0-100 skor motoru (score_symbol)
backtest.py               Ana backtest motoru + ranking + CSV yazımı + CLI
engine.py                 Canlı trading motoru (komisyon dahil, parity)
simulator.py              Canlı bot orkestrasyonu (GUI "Start Live" bunu kullanır)
data_ws.py / data_rest.py Binance WebSocket/REST veri katmanı
data_macro.py             Fear&Greed Index + BTC funding rate
walk_forward.py           Aylık segment walk-forward
robustness_test.py        Haftalık segment robustness
true_walk_forward.py      Train→test→roll gerçek OOS walk-forward
validate_system.py        Parity/bütünlük kontrolleri (backtest vs engine vs config)
app.py                    PyWebView API köprüsü (GUI ↔ Python)
gui.html                  PyWebView arayüzü (tek dosya HTML/CSS/JS)
config_online.yaml        Ana yapılandırma dosyası (46 üst seviye anahtar)
```

### Her test koşusunun ürettiği klasör yapısı
```
<run_klasörü>/
  backtest_summary.csv        Özet metrikler (PnL, winrate, maxDD, Sharpe...)
  backtest_trades.csv         Her trade'in tam kaydı (skor, label, komisyon, pump bilgisi...)
  equity_curve.csv            Equity eğrisi
  filter_events.csv           Tüm blok/uyarı olayları (ADX, MTF, PUMP_RISK_SOFT, RANK_*...)
  candidate_ranking_events.csv  Aynı-mum aday sıralama olayları
  ghost_signal_analysis.csv   "Açılmasaydı ne olurdu" analizleri
  boa_*.csv                   BOA feedback raporları (sebep/sembol/rejim bazlı)
  active_universe_symbols.json  O koşuda kullanılan sembol evreni
  config_snapshot.json        O koşunun TAM config kopyası (denetim için)
  _president/
    president_decisions.csv   President'ın verdiği TÜM kararlar
    shadow_opportunities.csv  Shadow modda loglanan "açılırdı" kararları
    branch_votes.csv          Her dalın her kararda verdiği oy
```
WF/ROB/TWF'nin her ay/hafta/fold alt klasörü, **kendi başına tam bir
Backtester çıktısıdır** — yukarıdaki yapının birebir aynısı. Bu sayede GUI'deki
"Analiz" paneli, tekli backtest ile segment drill-down için **aynı kodu**
kullanabiliyor.

---

## 6. Bilinen Kısıtlamalar ve Çıkarılan Dersler

- **Rejim bağımlılığı yapısaldır.** Sistem temelde bir trend-takip
  stratejisi; KONSOL/BEARISH dönemlerde sistematik olarak kaybediyor. Bunu
  "düzeltmeye" çalışmak yerine kabul edip boyut/etiket kalibrasyonuyla
  (§2.3, §2.9) etkisini azaltmak daha sağlıklı sonuç verdi.
- **Backtest/canlı parity kritik.** Komisyon hesaplama, MTF filtresi,
  symbol manager importu gibi birden fazla geçmiş hata, bir motora eklenip
  diğerine eklenmeyen kod yüzünden çıktı. `validate_system.py` artık bunu
  otomatik kontrol ediyor.
- **Look-ahead bias riski sürekli izlenmeli.** Geçmişte SL-sonrası yön
  tahmininde, yüklenmiş veri setinde mevcut olan ama canlıda olmayan
  gelecek mum verisi kullanıldığı tespit edildi.
- **Filtre eklemek çoğu zaman zarar veriyor.** `block_outcome_analyzer.py`
  verileri, bloklanan sinyallerin sadece %40.4 kazanma oranına sahip olduğunu
  (sistem ortalaması %60.7) gösterdi — yani mevcut filtreler genelde doğru
  çalışıyor, "daha fazla filtre" çözüm değil.
- **Partial TP boyutu önemli.** `partial_tp.close_pct`'i 0.10'dan 0.40'a
  çekmek, ek risk almadan anlamlı PnL artışı sağladı.
- **SHORT pozisyonlar tutarlı şekilde sorunlu.** Tüm versiyonlarda SHORT
  entegrasyonu ya hiç tetiklenmedi ya da performansı bozdu; şu an
  düşük öncelikli.
- **Sembol bazlı performans kararsız.** Bir dönemin en iyi sembolü diğer
  dönemde en kötüsü olabiliyor — statik blacklist güvenilmez, rejim/ceza
  bazlı yaklaşım (symbol_manager.py) tercih ediliyor.

## 7. Yapılandırma — Önemli Alanlar (`config_online.yaml`)

```yaml
backtest:
  president_execution_mode: simulated_active   # live/shadow/legacy/simulated_active
president:
  enabled: true
  shadow_mode: false        # V8.5.9: artık statik varsayılan da "false"
  decision_calibration:
    core_only_max_label: NORMAL
    core_only_quality_min: 65.0       # V8.5.8
    core_only_choppy_regimes: [KONSOL, CHOP, RANGE, BEARISH]   # V8.5.8
    low_quality_demote_below: 35.0    # V8.5.8
pump_filter:                 # V8.5.8 — yeni blok
  enabled: true
  price_lookback_bars: 3
  vol_spike_mult: 4.0
  price_spike_pct: 8.0
  score_penalty: 8.0
  size_mult: 0.5
```

## 8. Testleri Çalıştırma

- **Tekli backtest:** GUI → Backtest sayfası → "Yeni Test" (arka planda
  `backtest.py` CLI olarak çalışır).
- **Walk-Forward:** GUI → Walk-Forward sayfası → her ay kendi tam backtest
  çıktısını üretir, `wf_summary.json`'da toplanır.
- **Robustness:** GUI → Robustness sayfası → haftalık segmentler.
- **Gerçek WF (OOS):** GUI → Gerçek WF sayfası → train/test/roll fold'ları,
  gerçek out-of-sample performansı verir (en güvenilir gösterge).
- **Doğrulama:** `RUN_VALIDATE.bat` → `validate_system.py` → import zinciri,
  config parity, commission parity, simulator.py varlığı gibi kontrolleri
  çalıştırır. **Her kod değişikliğinden sonra önce bu çalıştırılmalı.**

## 9. Yol Haritası

- Config'teki kalan ~7 alan için yeni algoritma geliştirme (henüz bağlı değil)
- `data_macro.py`'daki `_fetch_btc_funding`'in `engine.py`'ye entegrasyonu
- MEXC/Bybit'e geçiş değerlendirmesi (komisyon + bölgesel erişim)
- Mart 2026'nın "pozitif rejim" doğrulama dönemi olarak walk-forward'da
  öncelikli incelenmesi

---

*Bu README, V8.5.9 itibarıyla sistemin anlık durumunu yansıtır. Kod
değiştikçe (özellikle §3/§4'teki davranışlar) güncellenmesi gerekir.*
