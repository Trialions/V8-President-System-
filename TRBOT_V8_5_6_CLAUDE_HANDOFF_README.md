# TRBOT President System V8.5.6 — Claude Handoff README

Bu README, mevcut TRBOT President System çalışmasının **nereden nereye geldiğini**, **hangi sorunların görüldüğünü**, **hangi patchlerin ne amaçla yapıldığını**, **şu an final pakette ne bulunduğunu** ve **bundan sonra Claude tarafında ne yapılması gerektiğini** netleştirmek için hazırlanmıştır.

---

## 1. Projenin ana hedefi

TRBOT artık sadece klasik bir long sinyal botu olarak düşünülmemelidir. Hedef sistem şudur:

```text
Spot/Futures gerçek piyasa verisini okuyan,
Long + Short adayları üreten,
Bütün motorların President'a rapor verdiği,
President'ın tek merkezi karar otoritesi olduğu,
RiskGovernor'ın mutlak risk sınırlarını koruduğu,
Backtest / Shadow / Paper Live / ileride Real Live akışı ayrılmış,
kararları GUI'den denetlenebilir bir trading karar platformu.
```

Başlangıçta hedeflenen uzun vadeli PnL beklentisi aylık %20-30 bandıydı. Ancak mevcut aşamada öncelik PnL değil; önce sistemin **doğru veriyle, doğru skorla, doğru karar akışıyla ve denetlenebilir şekilde** çalışmasıdır.

---

## 2. V7 neden önemliydi?

V7 sade ama bazı temel çekirdeklerde daha sağlıklıydı:

- `strategy_core.py` skor tarafında Supertrend kaynaklı score şişmesi fark edilmişti.
- EMA/ATR normalize momentum mantığı daha sağlıklıydı.
- `symbols_builder.py` sadece 24h volume sıralaması yapmıyor; daha dengeli sembol evreni seçiyordu.
- `adaptive_exit.py` ve `block_outcome_analyzer.py` değerliydi.
- Weekly symbol universe fikri V7’de daha doğal duruyordu.

V7’nin eksikleri:

- President gibi merkezi karar otoritesi yoktu.
- Long / short / cascade / exit / risk kararları tek hakemde birleşmiyordu.
- Audit zinciri, DecisionID, GUI kontrol, paper live ayrımı V8 kadar gelişmiş değildi.
- Aylık %20-30 hedef için gereken alpha motorları aktif değildi.

Özet:

```text
V7 = daha sade + bazı score/data çekirdekleri daha sağlıklı + mimari olarak sınırlı.
```

---

## 3. V8'e neden geçildi?

V8’e geçişin ana sebebi, sistemi “tek strateji scripti” olmaktan çıkarıp President merkezli bir karar mimarisine dönüştürmekti.

V8 ile amaçlananlar:

- President Governor
- Branch vote sistemi
- Core Long / Short Surgeon / Cascade Hunter ayrımı
- RiskGovernor
- DecisionID zinciri
- Shadow opportunity logging
- BOA raporları
- Adaptive Exit
- QualityScore
- AdaptiveRisk
- Weekly Universe
- Paper live hazırlığı
- GUI üzerinden kontrol ve inceleme

Ancak geçiş sırasında kritik bir sorun fark edildi:

```text
V8 mimarisi büyürken V7'nin iyi çalışan bazı score/data parçaları tam korunmadı.
```

Özellikle `strategy_core.py` ve `symbols_builder.py` tarafında V7 kalitesine geri dönmek gerekti.

---

## 4. Testlerde görülen temel problemler

Yapılan 30d/90d backtest incelemelerinde şu problemler görüldü:

### 4.1 Score saturation

Açılan trade’lerde CoreScore neredeyse hep 97-100 bandındaydı.

```text
CoreScore ≈ 100
Label = STRONG
```

Bu, skorun ayrıştırıcı olmadığını gösterdi. Gate yükseltmek çözüm değildi; çünkü sorun eşik değil, skor modelinin saturate olmasıydı.

### 4.2 Tüm trade’lerin STRONG olması

Açılan trade’lerin tamamı STRONG label alıyordu. Bu, President label sisteminin ayrışmadığını gösterdi.

Beklenen yapı:

```text
SCOUT / NORMAL / STRONG / ATTACK
```

Ama görülen:

```text
Her açılan işlem = STRONG
```

### 4.3 Short motorlar oy veriyor ama short trade açılmıyor

Short Surgeon ve Cascade tarafında SHORT oyları vardı ama gerçek/paper trade tarafında SHORT pozisyon yoktu.

Bu nedenle sistem fiilen hâlâ long ağırlıklı test ediliyordu.

### 4.4 MAX_POSITIONS aşırı tetikleniyor

Aynı candle/timestamp içinde çok fazla coin aday oluyor, ilk birkaç pozisyon açılıyor, kalanlar `MAX_POSITIONS` ile bloklanıyordu.

Bu şu sorunu doğurdu:

```text
Sistem gerçekten en iyi adayları mı açtı, yoksa ilk gelenleri mi açtı?
```

### 4.5 BOA rapor üretiyor ama karara bağlı değildi

BOA block sonrası fiyat davranışını analiz ediyordu ama President’a öğrenme feature’ı olarak bağlanmamıştı.

### 4.6 GUI eksikleri

Bazı şeyler YAML’dan değiştirilebiliyordu ama GUI’den özel ve anlaşılır şekilde görülemiyordu:

- Validate
- Decision Audit
- Paper/Shadow Live seçimi
- Ranking / BOA paneli
- Module Status
- Weekly Universe durumu

---

## 5. Patch geçmişi ve ne yapıldı?

### V8.5.3 — Score/Data/Decision Integrity Patch

Amaç: Backtestleri strateji kararı için daha güvenilir hale getirmek.

Yapılanlar:

- Problemli Supertrend bonusu kaldırıldı.
- V7’ye yakın EMA/ATR normalize momentum mantığı geri getirildi.
- `raw_score`, `normalized_score`, `long_score`, `short_score`, `score_model` alanları eklendi.
- `symbols_builder.py` V7’ye daha yakın hale getirildi.
- 7d median volume, spike ratio, 30d momentum, win-days ratio, EMA uyumu gibi metrikler eklendi.
- `symbols_top70_meta.json` zorunlu çıktı mantığı eklendi.
- `decision_integrity_audit.py` eklendi.
- Score saturation, label dağılımı, short trade yokluğu, max-position patlaması, weekly universe output gibi kontroller eklendi.

Sonuç:

```text
Önceki backtestlerdeki mantıksızlıklar artık otomatik audit ile yakalanabilir hale geldi.
```

---

### V8.5.4 — Hotfix + Pro GUI Control

Amaç: V8.5.3’te görülen runtime/GUI eksiklerini düzeltmek.

Yapılanlar:

- `backtest.py` içinde `result/components` runtime hatası düzeltildi.
- Duplicate AE / Quality / AdaptiveRisk trade kolonları temizlendi.
- `validate_short_smoke.py` eklendi.
- SHORT pozisyon açma ve short PnL smoke testi eklendi.
- `RUN_VALIDATE.bat` short smoke testini de çalıştıracak şekilde güncellendi.
- GUI’ye Paper Live / Shadow Live seçimi eklendi.
- GUI’ye Validate butonu eklendi.
- GUI’ye Decision Audit butonu eklendi.
- GUI’ye Module Status paneli eklendi.
- `app.py` versiyon bilgisini config’ten okuyacak hale getirildi.

Geçen kontroller:

```text
compileall OK
validate_config OK
validate_hybrid_config OK
validate_short_smoke OK
import chain OK
```

---

### V8.5.5 — President Ranking + BOA Feedback

Amaç: Aynı candle içindeki adayları doğru sıralamak, MAX_POSITIONS yerine anlamlı rejection sebepleri üretmek ve BOA’yı President’a küçük bir öğrenme feature’ı olarak bağlamak.

Yapılanlar:

#### 1. President global same-candle candidate ranking

Eski mantık:

```text
Semboller sırayla işlenir.
İlk gelenler açılır.
Kalanlar MAX_POSITIONS olur.
```

Yeni mantık:

```text
Aynı timestamp/candle içindeki adaylar toplanır.
President kararları karşılaştırılır.
En iyi rank score alanlar seçilir.
Diğerleri rank rejection sebebiyle loglanır.
```

#### 2. MAX_POSITIONS yerine RANK_REJECTED ayrımı

Yeni rejection sebepleri:

```text
RANK_SELECTED
RANK_REJECTED_LOWER_SCORE
RANK_REJECTED_BAD_QUALITY
RANK_REJECTED_CHOP_RISK
RANK_REJECTED_SYMBOL_PENALTY
```

Amaç:

```text
Pozisyon açılmadı çünkü kapasite doluydu mı?
Yoksa aynı mumda daha iyi adaylar mı vardı?
Bunu ayırmak.
```

#### 3. BOA feedback memory

BOA artık sadece rapor üretmiyor. Backtest sonunda `boa_feedback_memory.json` oluşturuyor.

Bu hafıza sonraki koşularda President’a küçük bir feature olarak veriliyor.

Güvenlik ilkesi:

```text
BOA President'ı baypas etmez.
BOA tek başına OPEN/BLOCK kararı vermez.
RiskGovernor'ı baypas etmez.
Etkisi config ile sınırlıdır.
```

Config:

```yaml
president:
  boa_feedback:
    enabled: true
    memory_file: data/boa_feedback_memory.json
    min_count: 8
    weight: 1.0
    max_adjustment: 6.0
```

---

### V8.5.6 — Pro GUI Ranking / BOA Panel

Amaç: V8.5.5’te eklenen ranking ve BOA feedback sistemini GUI’den incelenebilir hale getirmek.

Önemli: Bu patch, Claude’ın yaptığı core karar mantığını bozmadan sadece app/GUI görünürlük katmanını genişletmek için tasarlandı.

Core dosyalara dokunulmaması hedeflendi:

```text
backtest.py
president_runtime.py
president_governor.py
block_outcome_analyzer.py
strategy_core.py
```

Eklenen app.py metodları:

```text
get_ranking_summary(folder="")
get_boa_feedback_status(folder="")
get_ranking_boa_dashboard(folder="")
```

GUI’ye eklenen sayfa:

```text
Ranking / BOA
```

GUI’de görülebilenler:

- RANK_SELECTED sayısı
- RANK_REJECTED toplamı
- RANK_REJECTED sebep dağılımı
- Ortalama aday / candle
- Maksimum aday / candle
- Son ranking candle adayları
- BOA feedback memory durumu
- Pozitif BOA edge tablosu
- Negatif BOA edge tablosu
- Decision Audit çalıştırma

Geçen kontroller:

```text
compileall OK
validate_config OK
validate_hybrid_config OK
validate_short_smoke OK
core import chain OK
```

Not:

```text
Bu ortamda pywebview olmadığı için gerçek GUI penceresi tıklamalı test edilemedi.
Kullanıcı bilgisayarında requirements kurulunca GUI açılıp manuel kontrol edilmeli.
```

---

## 6. Şu an final pakette ne var?

Final paket adı:

```text
TRBOT_President_System_V8_5_6_PRO_GUI_RANKING_BOA_FULL.zip
```

Final durum:

```text
V8.5.6 = V8.5.5 ranking/BOA feedback core + V8.5.6 GUI visibility patch
```

Şu an sistemde olması gereken ana yetenekler:

- President merkezi karar mimarisi
- Core Long
- Short Surgeon
- Cascade Hunter shadow/proto
- RiskGovernor
- Adaptive Exit
- QualityScore
- AdaptiveRisk
- Weekly Universe
- Score/Data integrity düzeltmeleri
- EMA/ATR momentum score
- raw/normalized/long/short score alanları
- Decision Integrity Audit
- Short Smoke Test
- Paper/Shadow Live GUI kontrolü
- Validate GUI kontrolü
- Module Status GUI paneli
- President same-candle candidate ranking
- RANK_REJECTED ayrımı
- BOA feedback memory
- Ranking / BOA GUI paneli

---

## 7. Şu an hâlâ yapılmamış veya bilerek ertelenmiş konular

### 7.1 Cascade / futures-flow gerçek alpha motoru

Henüz eklenmedi. Bilerek ertelendi.

Sebep:

```text
Önce President adayları doğru sıralayabilmeli.
RANK_REJECTED ayrımı çalışmalı.
BOA feedback denetlenmeli.
Sonra yeni alpha motoru eklenmeli.
```

Cascade/futures-flow için ileride gereken veriler:

- Funding rate
- Open interest
- Long/short ratio
- Top trader ratio
- Taker buy/sell volume
- Mark price / premium index
- Liquidation prints veya heatmap alternatifi
- Spot/futures volume farkı

### 7.2 Gerçek emir adapter’ı

Şu an hedef gerçek emir değil. Hedef:

```text
Gerçek veri + sanal pozisyon = Paper Live
```

### 7.3 Tam GUI tıklama testi

Bu ortamda yapılamadı. Kullanıcı bilgisayarında yapılmalı.

### 7.4 30d / 90d yeni backtest sonuçları

V8.5.6 final paketinden sonra henüz yeni 30d/90d test alınmadı.

Bu nedenle PnL değerlendirmesi yapılmamalı.

---

## 8. Claude için dikkat edilmesi gereken kritik ilkeler

### 8.1 President tek karar otoritesi kalmalı

Hiçbir motor doğrudan pozisyon açmamalı.

```text
CoreLong / ShortSurgeon / CascadeHunter / BOA / AE / QualityScore = rapor üretir.
President = karar verir.
RiskGovernor = mutlak risk sınırlarını uygular.
```

### 8.2 BOA feedback sadece feature olmalı

BOA kesinlikle President’ı bypass etmemeli.

Doğru:

```text
BOA → küçük edge adjustment
```

Yanlış:

```text
BOA → doğrudan OPEN/BLOCK
```

### 8.3 Short aktifliği sadece config değil, execution ile kanıtlanmalı

Short için kontrol edilmesi gerekenler:

- SHORT final decision var mı?
- SHORT pozisyon açılıyor mu?
- SHORT TP doğru hesaplanıyor mu?
- SHORT SL doğru hesaplanıyor mu?
- Trade dosyasında side=SHORT yazıyor mu?
- Net PnL short yön için doğru mu?

### 8.4 Score threshold yükseltmek çözüm değil

Sorun score 100’e yapışıyorsa gate’i 100 üstüne çekmek mantıksızdır.

Doğru çözüm:

- score normalize edilmeli
- raw / normalized ayrılmalı
- long_score / short_score ayrılmalı
- score saturation audit ile kontrol edilmeli

### 8.5 MAX_POSITIONS artık tek başına yeterli sebep değil

Aynı candle içinde aday sıralaması yapılmalı.

Açılmayan adaylar şu sebeplerle loglanmalı:

- lower score
- bad quality
- chop risk
- symbol penalty
- short conflict
- already full

### 8.6 GUI’den görülemeyen özellik eksik sayılmalı

Sadece YAML’da olan şey kullanıcı için yeterli değildir.

Özellikle:

- Validate
- Decision Audit
- Ranking Summary
- BOA Feedback
- Module Status
- Paper/Shadow Mode

GUI’den görülebilir olmalı.

---

## 9. Final paketten sonra önerilen test sırası

Claude veya kullanıcı şu sırayla ilerlemeli:

### Adım 1 — Teknik doğrulama

```bash
python -m compileall -q .
python validate_config.py
python validate_hybrid_config.py
python validate_short_smoke.py
```

Windows:

```bat
RUN_VALIDATE.bat
```

### Adım 2 — GUI açılış kontrolü

- GUI açılıyor mu?
- Paper Live / Shadow Live butonları görünüyor mu?
- Validate butonu çalışıyor mu?
- Decision Audit butonu çalışıyor mu?
- Module Status görünüyor mu?
- Ranking / BOA sayfası görünüyor mu?

### Adım 3 — 7 günlük küçük backtest

Amaç PnL değil, sistem akışı.

Kontrol:

- trade açıyor mu?
- short açıyor mu?
- RANK_SELECTED yazıyor mu?
- RANK_REJECTED yazıyor mu?
- BOA feedback memory oluşuyor mu?
- Decision audit ne diyor?

### Adım 4 — 30 günlük backtest

Kontrol:

- Score hâlâ 97-100’e yapışıyor mu?
- Her trade hâlâ STRONG mu?
- Long/short dağılımı var mı?
- Max positions azaldı mı?
- Ranking rejection dağılımı mantıklı mı?
- TP/SL sonrası fiyat davranışı ne?

### Adım 5 — 90 günlük backtest

Kontrol:

- Robust mu?
- Hangi aylar iyi/kötü?
- BOA feedback ikinci koşuda kararı iyileştiriyor mu?
- Short gerçekten katkı sağlıyor mu?
- Weekly universe output var mı?

### Adım 6 — Paper Live

Önce 24-48 saat, sonra 1-2 hafta.

Amaç:

```text
Backtest sinyali ile canlı/paper sinyal davranışı benzer mi?
```

---

## 10. Mevcut puanlama / gerçek durum

Dürüst durum:

```text
Mimari omurga: iyi seviyeye geldi.
Score/data tarafı V7’ye yaklaştırıldı.
Backtest audit katmanı eklendi.
GUI görünürlük iyileştirildi.
Short smoke testi eklendi.
Ranking/BOA feedback core eklendi.
```

Ama:

```text
Yeni final paketle henüz 30d/90d gerçek sonuç alınmadı.
Cascade/futures-flow alpha motoru yok.
Gerçek emir yok.
Tam GUI tıklama testi yapılmadı.
PnL hedefi henüz kanıtlanmadı.
```

Bu yüzden doğru ifade:

```text
V8.5.6 altyapı ve denetlenebilirlik açısından test aşamasına geçebilir.
Ama strateji kârlılığı henüz yeniden testlerle kanıtlanmalı.
```

---

## 11. Claude'a net görev

Claude bu paketi devralırsa ilk görevi yeni özellik eklemek olmamalı.

İlk görev:

```text
Final V8.5.6 paketinin gerçekten çalıştığını, GUI’den görüldüğünü ve 7d/30d backtest auditlerinden geçtiğini kanıtlamak.
```

Kontrol listesi:

```text
[ ] compileall OK
[ ] validate_config OK
[ ] validate_hybrid_config OK
[ ] validate_short_smoke OK
[ ] GUI açılıyor
[ ] Validate GUI’den çalışıyor
[ ] Decision Audit GUI’den çalışıyor
[ ] Ranking / BOA sayfası veri gösteriyor
[ ] 7d backtest trade üretiyor
[ ] RANK_SELECTED üretiyor
[ ] RANK_REJECTED üretiyor
[ ] boa_feedback_memory.json oluşuyor
[ ] Short trade en az smoke/backtestte doğrulanıyor
[ ] 30d decision audit kabul edilebilir
[ ] 90d decision audit kabul edilebilir
```

Bu checklist geçmeden Cascade/futures-flow veya yeni alpha eklenmemeli.

---

## 12. Özet

Ne yaptık?

```text
V8 President mimarisini koruduk.
V7 score/data kalitesini geri taşımaya başladık.
Score saturation ve label tekdüzeliği için audit ekledik.
Short smoke testi ekledik.
GUI kontrol katmanını genişlettik.
Same-candle global ranking ekledik.
MAX_POSITIONS yerine RANK_REJECTED ayrımı ekledik.
BOA feedback memory ekledik.
Ranking/BOA GUI paneli ekledik.
```

Ne henüz olmadı?

```text
Cascade/futures-flow gerçek alpha motoru yok.
Yeni final paketle 30d/90d test sonucu yok.
Tam GUI tıklama testi yapılmadı.
PnL hedefi kanıtlanmadı.
```

Şu an finalde ne yapılıyor?

```text
V8.5.6 artık test edilebilir, audit edilebilir, GUI’den daha iyi incelenebilir bir President System haline getirildi.
Bundan sonraki aşama yeni özellik değil; 7d → 30d → 90d → WF → Paper Live test zinciriyle sistemin gerçekten doğru çalıştığını kanıtlamaktır.
```
