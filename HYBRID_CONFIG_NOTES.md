# TRBOT V8.4.1 Hybrid Config Patch

Bu patch sadece `config_online.yaml` dosyasını değiştirir. Amaç, President System'e uygun yeni config'i temel alıp eski config'teki değerli mantıkları kontrollü şekilde geri eklemektir.

## Ana karar
- Ana temel: yeni President/V8 config.
- Eski config'ten kontrollü eklenenler: `adaptive_exit`, `block_outcome_analysis`, `weekly_symbol_rotation`, `quality_score`, `adaptive_risk`.
- Güvenlik: canlı taraf `shadow`, backtest tarafı `simulated_active`.
- Rotation: fiziksel olarak kapalı, shadow modda.

## Sistem bu harman config'i anlayacak mı?
Evet, YAML olarak okur ve mevcut V8 kodu bilinmeyen/staged başlıkları kırmadan geçer. Ancak şu ayrımı unutmamak gerekir:

### Mevcut kodda doğrudan etkili olanlar
- `risk`, `limits`, `thresholds`, `mtf`, `adx_filter`, `rsi_filter`, `atr_filter`, `partial_tp`, `dynamic_trail`, `market_regime`, `president`, `core_long`, `short_surgeon`, `cascade_hunter`, `convex_position`, `position_rotation`, `account`, `backtest`, `live`, `symbol_quality_filter`, `symbol_blacklist`, `indicator_engine`.

### Config'te hazırlanan ama kod entegrasyonu gerekiyorsa davranışı değiştirmeyen/staged olanlar
- `adaptive_exit`: Policy mantığı config'e eklendi, ancak sistemde adaptive_exit modülü açıkça bağlanmadıysa sadece config'te durur.
- `weekly_symbol_rotation`: True historical weekly universe için robustness/symbol builder tarafı bunu okumalı.
- `quality_score`: Yeni President tarafında asıl aktif karşılığı `symbol_quality_filter`; eski quality_score kodu bağlanmadıysa no-op kalabilir.
- `adaptive_risk`: RiskGovernor bunu okumuyorsa no-op kalabilir.
- `block_outcome_analysis`: Backtest/BOA analyzer destekliyorsa kullanılır; destek yoksa no-op.

## Önemli tasarım seçimleri
- `mtf.htf_interval: 4h`, `htf_long_min: 60`: 1h ana strateji için gerçek HTF kontrolü.
- `rsi_filter.enabled: true`, `max_long: 72`: aşırı şişmiş longları azaltmak için.
- `atr_filter.enabled: true`, `min_atr_pct: 0.8`: ölü piyasayı azaltır ama aşırı sert değildir.
- `partial_tp.tp1_r_mult: 1.0`: eski 2.5R çok geç, yeni 0.75R çok erken olduğu için ara değer.
- `dynamic_trail.enabled: false`: erken trail riskini azaltmak ve temiz A/B test almak için.
- `market_regime.konsol_size_mult: 0.35`: KONSOL tamamen kapanmaz ama düşük size ile alınır.
- `position_rotation.enabled: false`, `shadow_mode: true`: test edilmemiş rotation fiziksel müdahale yapmaz.

## Kullanım
1. Mevcut V8.4.1 klasörünü yedekle.
2. Bu ZIP'i proje ana klasörüne çıkar.
3. `config_online.yaml` üzerine yazılsın.
4. `RUN_THIS_FIRST.bat` veya `.venv` terminalinden doğrula.
5. İlk test sırası:
   - Legacy 30 gün / 1h / top20
   - President simulated active 30 gün / 1h / top20
   - President shadow 30 gün / 1h / top20
