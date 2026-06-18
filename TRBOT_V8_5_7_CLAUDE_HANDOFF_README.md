# TRBOT V8.5.7 — Claude Handoff README

Bu README, V8.5.7 aşamasına kadar yapılan işleri, neden yapıldığını, son backtestte ne gördüğümüzü ve bundan sonra Claude’un hangi alanlara dokunması gerektiğini netleştirmek için hazırlanmıştır.

Amaç: sistemi baştan yazmak değil; mevcut Claude/President mimarisini bozmadan, sadece gerekli Python dosyalarında hedefli düzeltmeler yapmak.

---

## 1. Proje bağlamı

TRBOT artık sadece klasik long sinyal botu değil. Hedef mimari şu:

```text
Data Layer
  └─ Spot/Futures veri, sembol evreni, weekly universe

Signal Layer
  ├─ Core Long
  ├─ Short Surgeon
  ├─ Cascade Hunter / ileride futures-flow alpha
  ├─ Quality Score
  ├─ Adaptive Risk
  └─ Adaptive Exit

President Layer
  ├─ Branch vote toplar
  ├─ Aynı candle içi adayları sıralar
  ├─ Long/short kararını verir
  ├─ RiskGovernor sınırlarına uyar
  └─ Açılan/açılmayan kararları loglar

Audit Layer
  ├─ Decision audit
  ├─ BOA / block outcome
  ├─ ranking events
  ├─ score/label dağılımı
  └─ weekly universe denetimi
```

Temel kural:

```text
President tek nihai karar otoritesidir.
Motorlar sadece rapor/aday üretir.
RiskGovernor bypass edilmez.
BOA doğrudan trade açtırmaz; sadece küçük feedback feature verir.
```

---

## 2. V7 neden tekrar referans alındı?

Ömer’in geri bildirimi: V7’nin score/data tarafı daha sağlıklı görünüyordu.

Yapılan karşılaştırmada şu sonuç çıktı:

```text
V7 mimarisi daha zayıf ama bazı çekirdekleri daha doğruydu:
- strategy_core tarafında Supertrend kaynaklı score şişmesi daha iyi ele alınmıştı.
- EMA/ATR normalize momentum mantığı daha sağlıklıydı.
- symbols_builder daha gelişmişti.
- adaptive_exit.py ve block_outcome_analyzer.py değerliydi.

V8 mimarisi daha güçlüydü ama bazı V7 çekirdekleri tam taşınmamıştı.
```

Bu yüzden karar:

```text
V8 President mimarisi korunacak.
V7 score/data sağlamlığı geri alınacak.
```

---

## 3. V8.5.3 ile ne yapıldı?

Ana hedef: score/data integrity.

Yapılanlar:

```text
- strategy_core.py içinde Supertrend +20 kaynaklı score şişmesi kaldırıldı.
- EMA/ATR normalize momentum mantığı geri getirildi.
- raw_score, normalized_score, long_score, short_score alanları eklendi.
- symbols_builder.py V7’ye daha yakın hale getirildi.
- symbols_top70_meta.json / meta üretimi hedeflendi.
- decision_integrity_audit.py eklendi.
```

Yakalanan eski sorunlar:

```text
- Açılan trade’lerin %100’ü 97+ score oluyordu.
- Tüm açılan trade’ler STRONG label oluyordu.
- Short vote vardı ama short trade yoktu.
- MAX_POSITIONS çok sık ve kaba yazılıyordu.
```

---

## 4. V8.5.4 ile ne yapıldı?

Ana hedef: hotfix + GUI kontrol.

Yapılanlar:

```text
- backtest.py içindeki result/components runtime bug düzeltildi.
- duplicate trade kolonları temizlendi.
- validate_short_smoke.py eklendi.
- RUN_VALIDATE.bat zincirine short smoke eklendi.
- GUI’ye Validate, Decision Audit, Paper/Shadow Live, Module Status eklendi.
```

Not:

```text
Bu aşamada short PnL temel smoke test geçti.
Ama global same-candle ranking ve BOA feedback henüz tam bağlanmamıştı.
```

---

## 5. V8.5.5 ile ne yapıldı?

Ana hedef: President global same-candle ranking + BOA feedback.

Yapılanlar:

```text
- Aynı timestamp/candle içindeki adayların birlikte değerlendirilmesi hedeflendi.
- RANK_SELECTED / RANK_REJECTED_* mantığı eklendi.
- BOA feedback memory mantığı eklendi.
- BOA feedback’in President’a küçük feature olarak yansıması hedeflendi.
```

Önemli kural:

```text
BOA feedback President’ı bypass etmez.
BOA tek başına OPEN/BLOCK kararı vermez.
Etkisi sınırlı olmalıdır. Örn. ±3 ila ±7 puan bandı.
```

---

## 6. V8.5.6 ile ne yapıldı?

Ana hedef: GUI’den ranking/BOA görünürlüğü.

Yapılanlar:

```text
- app.py içine ranking/BOA okuma metotları eklendi.
- GUI’ye Ranking / BOA sayfası eklendi.
- candidate ranking ve BOA feedback özetlerinin GUI’den okunması hedeflendi.
```

Bu aşamada core karar motoruna dokunulmadı; sadece okuma/görünürlük katmanı geliştirildi.

---

## 7. V8.5.7 ile ne yapıldı?

Ana hedef: Son backtestte çıkan mantık/loglama problemlerini düzeltmek.

Ömer’in kuralı:

```text
Tüm sistemi baştan yazma.
Değişecek alan çoksa sadece ilgili .py dosyalarını düzelt.
Kod kısa ise hangi dosyada hangi alanı değiştireceğimi tarif et.
```

V8.5.7’de yapılanlar:

```text
1. MAX_POSITIONS anlam hatası düzeltildi.
2. candidate_ranking_events.csv ayrı output dosyası olarak eklendi.
3. filter_events.csv kolon kaybı engellendi.
4. active_universe_symbols.json zorunlu output haline getirildi.
5. symbol_universe_history.csv, weekly_universe_log.csv, symbols_top70_meta.json görünür hale getirildi.
6. boa_feedback_memory.json boş bile olsa outputta görünür hale getirildi.
7. Trade kayıtlarına yeni score/ranking alanları eklendi:
   - EntryScore
   - PresidentScore
   - RankScore
   - RankPosition
   - RankCandidateCount
   - BOAFeedbackAdj
8. TP1 progress reduce analizi için ek alanlar eklendi:
   - TP1_Progress_ExitPrice
   - TP1_Progress_ReduceQty
9. app.py artık candidate_ranking_events.csv dosyasını öncelikli okuyor.
10. validate_ranking_output_smoke.py eklendi.
```

Validasyon sonucu:

```text
Compile: OK
Config: OK
Hybrid config: OK
Import chain: OK
SHORT smoke: OK
Ranking output smoke: OK
RUN_VALIDATE.bat: VALIDATION OK
```

Ömer’in lokal çıktısı:

```text
[1/6] Compile check... OK
[2/6] Config check... CONFIG_VALIDATION OK
[3/6] Hybrid config check... HYBRID_CONFIG_OK V8.5.2
[4/6] Import chain... IMPORT_OK
[5/6] SHORT smoke test... SHORT_SMOKE_OK
[6/6] Ranking output smoke test... RANKING_OUTPUT_SMOKE_OK
VALIDATION OK
```

Not: `HYBRID_CONFIG_OK V8.5.2` yazısı isimsel/sürüm etiketi kalıntısı olabilir. Fonksiyonel validation geçti.

---

## 8. Son analiz edilen backtest

Dosya:

```text
2026-06-18_04-56_1h_30d.zip
```

Analiz özeti:

```text
Trade: 97
Net PnL: -$8.8634
Max DD: ~%2.20
LONG: 89 trade / -$13.06
SHORT: 8 trade / +$4.20
Score >= 97 oranı: %18.6
RANK_SELECTED: 97
RANK_REJECTED_BAD_QUALITY: 9
MAX_POSITIONS problemi: görünmüyor
```

İyi gelişmeler:

```text
1. PnL/equity muhasebesi tutarlı.
2. candidate_ranking_events.csv oluşmuş.
3. RANK_SELECTED / RANK_REJECTED eventleri yazılmış.
4. MAX_POSITIONS yanlış loglama görünmüyor.
5. SHORT gerçekten açılmış ve pozitif katkı vermiş.
6. active_universe_symbols.json, weekly log, BOA memory dosyaları var.
7. Score artık tamamen 97-100’e yapışmıyor.
8. Label artık yalnızca STRONG değil.
```

Hâlâ düzeltilmesi gerekenler:

```text
1. PresidentScore trade kayıtlarında boş geliyor.
2. Label dağılımı bu kez NORMAL tarafına aşırı yığılmış: yaklaşık %94.8 NORMAL.
3. BOA memory oluşmuş ama bu koşuda BOAFeedbackAdj hâlâ 0.0.
4. DAILY_TRADE_LIMIT logları daha açıklayıcı olmalı.
5. decision_integrity_audit.md output içine otomatik yazılmalı.
```

Strateji tarafı ana bulgu:

```text
TP1 gören trade:     +$32.53
TP1 görmeyen trade:  -$41.39
```

TP1 Progress tarafı:

```text
TP1_Progress_Reduced = 1 olan trade’ler: -$24.36
```

Yorum:

```text
TP1 Progress kötü trade’i yakalıyor olabilir; ancak sadece azaltmak yetmiyor.
Bu durumda erken full-exit veya daha sert exit simülasyonu yapılmalı.
```

---

## 9. Şu an sistemin durumu

Artık sistem “komple bozuk” seviyesinde değil.

Mevcut durum:

```text
- Compile/import zinciri geçiyor.
- Short temel smoke test geçiyor.
- Ranking output smoke geçiyor.
- Backtest outputları önceye göre daha denetlenebilir.
- Short trade gerçekten açılıyor.
- Ranking eventleri oluşuyor.
- MAX_POSITIONS eski kaba/hatalı davranış bu son testte görünmüyor.
```

Ancak final optimize/paper-live kararına geçmeden önce birkaç anlam/mantık düzeltmesi gerekiyor.

---

## 10. Claude’un öncelikli düzeltmesi gerekenler

### 10.1 PresidentScore boşluğu

Sorun:

```text
DecisionID ile president_decisions.csv eşleşiyor.
President decision verisi var.
Ama backtest_trades.csv içindeki PresidentScore boş geliyor.
```

İstenen:

```text
Trade kapanış kaydına PresidentScore doğru taşınmalı.
```

Muhtemel kaynak:

```text
_open_from_decision() veya trade close record alanında decision score / result score yanlış isimle okunuyor.
```

Beklenen kolonlar:

```text
EntryScore          = strategy_core giriş skoru
PresidentScore      = President nihai karar skoru
RankScore           = same-candle ranking skoru
LongScore           = long feature skoru
ShortScore          = short branch/president skoru
ShortFeatureScore   = strategy_core short feature skoru
QualityScore        = quality feature
AdaptiveRiskMult    = risk multiplier
BOAFeedbackAdj      = BOA feedback adjustment
```

### 10.2 Label kalibrasyonu

Sorun:

```text
Eski testlerde herkes STRONG idi.
Son testte herkes NORMAL tarafına yığılmış durumda.
```

İstenen:

```text
Label sınıfları gerçekten ayrışmalı:
SCOUT / NORMAL / STRONG / ATTACK
```

Öneri:

```text
Label sadece final score’a bağlı kalmamalı.
QualityScore, AE_Class, RankScore, BOAFeedbackAdj, side ve regime de dikkate alınmalı.
```

Başlangıç mantığı:

```text
ATTACK: yüksek rank + yüksek quality + düşük chop risk + güçlü side uyumu
STRONG: iyi rank + yeterli quality + normal risk
NORMAL: orta sinyal
SCOUT: düşük confidence / küçük size / shadow-benzeri aday
```

### 10.3 BOAFeedbackAdj hep 0.0

Sorun:

```text
boa_feedback_memory.json oluşuyor ama BOAFeedbackAdj bu koşuda 0.0.
```

Bu ilk koşuda normal olabilir; çünkü BOA hafızası koşu sonunda oluşur ve sonraki koşuda kullanılmalıdır.

Claude kontrol etmeli:

```text
1. Eğer memory dosyası varsa bir sonraki backtest başında okunuyor mu?
2. min_count şartı çok yüksek mi?
3. key formatı write/read tarafında aynı mı?
4. BOA adjustment trade record içine taşınıyor mu?
```

Öneri:

```text
İkinci ardışık 30d koşuda BOAFeedbackAdj yine tamamen 0 ise bug kabul edilmeli.
```

### 10.4 DAILY_TRADE_LIMIT logları

Sorun:

```text
DAILY_TRADE_LIMIT eventleri var ama hangi sayaçtan, hangi limitten, hangi gün için oluştuğu yeterince açık değil.
```

İstenen kolonlar:

```text
date
symbol
side
daily_trade_count
daily_trade_limit
active_positions
reason
```

### 10.5 decision_integrity_audit.md otomatik outputa yazılsın

Sorun:

```text
decision_integrity_audit.py var ama backtest output içinde otomatik decision_integrity_audit.md görünmüyor.
```

İstenen:

```text
Backtest tamamlandıktan sonra audit otomatik çalışsın veya RUN_BACKTEST sonrası script output klasörüne audit dosyası yazsın.
```

Audit minimum kontrol listesi:

```text
score_saturation
label_distribution
side_distribution
short_votes_vs_short_trades
rank_selected_vs_rejected
max_positions_integrity
weekly_universe_files
boa_feedback_file
pnl_equity_reconciliation
```

---

## 11. Strateji tarafında hâlâ çözülmesi gereken ana konu

Ana zarar kaynağı:

```text
TP1 görmeyen trade’ler.
```

Son test:

```text
TP1 gören trade:     +$32.53
TP1 görmeyen trade:  -$41.39
```

TP1 Progress Reduced trade’ler:

```text
31 civarı trade, toplam yaklaşık -$24.36
```

Yorum:

```text
TP1 Progress Manager kötü trade’i tespit ediyor gibi.
Ama sadece reduce etmek yeterli değil.
```

İstenen sonraki simülasyon:

```text
TP1 progress reduced olduğunda:
A) Mevcut davranış
B) %50 reduce
C) %100 full exit
D) daha kısa timeout + exit
E) side reversal shadow
```

Bu simülasyon gerçek strategy değişikliği yapılmadan, önce rapor/simülasyon olarak çalıştırılmalı.

---

## 12. Dokunulmaması gerekenler

Claude aşağıdakileri gereksiz yere baştan yazmamalı:

```text
- President mimarisi
- RiskGovernor bypass mantığı
- Engine/backtest tüm akışı
- GUI genel yapısı
- strategy_core tamamı
- symbols_builder tamamı
```

Sadece hedefli dosya değiştirilmeli.

Ömer’in çalışma kuralı:

```text
Tüm sistemi baştan yazma.
Değişecek alan çoksa sadece ilgili .py dosyalarını güncelle.
Kısa düzeltmelerde hangi dosyada hangi alanı değişeceğini tarif et.
```

---

## 13. Önerilen sonraki mini patch

İsim:

```text
V8.5.8 PresidentScore + Audit Output + Label Calibration Patch
```

Kapsam:

```text
1. backtest.py
   - PresidentScore trade record fix
   - BOAFeedbackAdj trade record fix
   - decision_integrity_audit auto-write hook
   - DAILY_TRADE_LIMIT detay kolonları

2. president_governor.py veya president_runtime.py
   - Label kalibrasyonu
   - NORMAL yığılmasını azaltma
   - SCOUT/STRONG/ATTACK ayrımı

3. decision_integrity_audit.py
   - son output klasörüne otomatik rapor yazma desteği

4. app.py / gui.html gerekirse sadece okuma tarafı
   - audit dosyasını gösterme
```

Bu patch PnL artırma patch’i olmamalı. Amaç:

```text
Karar kayıtlarının anlamını düzeltmek ve audit’i otomatikleştirmek.
```

---

## 14. Test sırası

Claude düzeltmeden sonra şu sıra izlenmeli:

```text
1. RUN_VALIDATE.bat
2. 7 günlük küçük 1h President Active backtest
3. Output kontrol:
   - backtest_trades.csv
   - president_decisions.csv
   - candidate_ranking_events.csv
   - filter_events.csv
   - boa_feedback_memory.json
   - decision_integrity_audit.md
4. 30 günlük 1h President Active backtest
5. Aynı audit kontrolü
6. Aynı 30d testi ikinci kez çalıştırılarak BOAFeedbackAdj etkisi kontrolü
7. 90 günlük test
8. Long/short dağılım analizi
9. TP1 progress full-exit simülasyonu
```

---

## 15. Son hüküm

V8.5.7 sonrası durum:

```text
Altyapı toparlandı.
Backtest outputu artık önceye göre anlamlı.
Short açılıyor.
Ranking eventleri oluşuyor.
MAX_POSITIONS eski mantık hatası görünmüyor.
```

Fakat final test/optimizasyon öncesi:

```text
PresidentScore boşluğu,
label kalibrasyonu,
BOAFeedbackAdj doğrulaması,
decision_integrity_audit otomatik output,
TP1 progress exit simülasyonu
```

çözülmeli.

Bu README Claude’a verilecek nihai bağlamdır.
