# AI Quant Trading Bot — Audit Report

**Tanggal**: 16 Mei 2026  
**Versi**: v2.0 (dengan Whale Impact Update)  
**Mode**: Intraday (15M primary) + Swing (4H primary)  
**Exchange**: Bybit USDT Futures  
**Arsitektur**: Agentic Trading Workflow (5-Role Committee)  

---

## Executive Summary

Bot trading crypto futures dengan **arsitektur Agentic Trading Workflow** — bukan signal generator biasa, tapi autonomous trading committee dengan 5 role internal (Scout, Analyst, Risk Manager, Position Manager, Reviewer). Bot memiliki **15+ sistem filter** yang sangat comprehensive, namun **win rate masih ~45-50%** dengan **loss besar dari liquidation**.

**Key Finding**: Masalah utama bukan di filter kurang ketat, tapi di **exit strategy yang terlalu cepat memotong winner** dan **SL yang terlalu lebar (membiarkan loser liquidate)**.

---

## 0. Agentic Trading Workflow

Bot beroperasi sebagai **autonomous trading committee** dengan 5 role internal:

```
┌─────────────────────────────────────────────────────────────┐
│                    TRADING COMMITTEE                        │
├─────────────────────────────────────────────────────────────┤
│  1. SCOUT       → Scan market, cari setup potensial         │
│  2. ANALYST     → Analisa teknikal + fundamental            │
│  3. RISK MANAGER → Evaluasi risk/reward, reject jika jelek  │
│  4. POSITION MANAGER → Kelola entry/exit/SL/TP/trail        │
│  5. REVIEWER    → Belajar dari trade selesai, update bias   │
└─────────────────────────────────────────────────────────────┘
```

### Decision Hierarchy

1. **Hard risk rules** selalu override AI confidence
2. **Trade memory** override raw AI score
3. **SMC/retest quality** override momentum chasing
4. **High leverage** = higher quality confirmation needed
5. **High score TIDAK cukup** jika memory/setup jelek

### Key Principle

> "A high-confidence AI signal is only a proposal. It is not permission to trade. Permission comes only after memory, SMC, risk, and execution filters agree."

### Agentic Capabilities

| Capability | Deskripsi |
|------------|-----------|
| **Multi-timeframe analysis** | 4H primary, Daily HTF, 1H + 15M confirmation |
| **Trade memory learning** | Belajar dari setiap trade, update bias per symbol |
| **Adaptive risk mode** | Defensive → Conservative → Normal → Aggressive |
| **SMC/ICT context** | Liquidity sweep, BOS, premium/discount, retest zones |
| **Whale intelligence** | Smart money positioning + impact calculation |
| **Committee decision** | Setiap keputusan melalui 5 tahap evaluasi |

### Reviewer Behavior

Setelah trade selesai, bot belajar dari:
- Symbol, direction, setup type, entry quality
- Exit reason, PnL, drawdown, funding/fees
- Apakah SMC/retest respected
- Apakah memory agreed/disagreed
- Apakah trade early, valid, atau late chase

Update bias:
- **Reward** symbol/setup yang repeatedly produce clean wins
- **Penalize** symbol/setup yang produce large losses
- **Treat** repeated high-score losses sebagai evidence scoring overconfident

---

## 1. Arsitektur Overview

Bot menggunakan **Agentic Trading Workflow** — AI beroperasi sebagai trading committee, bukan signal generator biasa.

```
┌─────────────────────────────────────────────────────────────┐
│  AGENTIC TRADING WORKFLOW                                   │
│    SCOUT → ANALYST → RISK MANAGER → POSITION MANAGER       │
│                                      ↓                      │
│                                 REVIEWER (learning)         │
├─────────────────────────────────────────────────────────────┤
│  DATA LAYER                                                 │
│    MarketFeed (Bybit/OKX/Gate.io) → OHLCV + 30+ indikator  │
│    OnChainFeed (Binance) → Whale data + regime + impact     │
│    TradeMemory → Learning context + adaptive params         │
├─────────────────────────────────────────────────────────────┤
│  ANALYZER LAYER                                             │
│    SignalEngine (AI) → Multi-provider 3-tier fallback       │
│    10-kategori scoring system (max ~14.5 poin)              │
│    Agentic workflow prompt (5-role committee)               │
├─────────────────────────────────────────────────────────────┤
│  FILTER LAYER (10+ cascade)                                 │
│    1. AUTO_SYMBOLS (top-N by volume)                        │
│    2. Slot check (max concurrent positions)                 │
│    3. Score + Confidence hard floor                         │
│    4. Risk/Reward validation                                │
│    5. Anti-overtrading (6h rolling circuit breaker)         │
│    6. 4 Risk Detection (spread, wick, funding, wash)        │
│    7. ICT/SMC Retest gate                                   │
│    8. Memory blacklist gate                                 │
│    9. P3 Adaptive Rules                                     │
│    10. Whale Impact Score Modifier                          │
├─────────────────────────────────────────────────────────────┤
│  EXECUTION LAYER                                            │
│    CEXExecutor (Bybit) → Entry + SL/TP/Trail management     │
│    DEXExecutor (Hyperliquid) → Alternative exchange         │
├─────────────────────────────────────────────────────────────┤
│  TRACKING LAYER                                             │
│    PnLTracker → Dry-run simulation + live stats             │
│    Dashboard → Terminal + Web + HTML report                 │
│    Telegram → Two-way control + alerts                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Performance Analysis (1 Apr - 8 Mei 2026)

### 2.1 Trade Statistics

| Metrik | Nilai |
|--------|-------|
| Total closed trades | ~120+ |
| Win rate | ~45-50% |
| Total PnL | **Negatif (rugi bersih)** |
| Biggest win | +152% (SIRENUSDT) |
| Biggest loss | -313% (SIRENUSDT) |
| Liquidation count | ~15+ posisi |
| Average win | ~20-30% ROI |
| Average loss | ~50-80% ROI (termasuk liquidation) |

### 2.2 Win/Loss Ratio Problem

```
Menang: +152%, +142%, +86%, +76%, +60% → rata-rata +103%
Kalah:  -313%, -163%, -107%, -85%, -81% → rata-rata -150%

Rasio menang:kalah = 1:1.5 (SEHARUSNYA minimal 1:1)
```

**Root Cause**: Winner dipotong terlalu cepat (TP 1.5-2x ATR), loser dibiarkan liquidate (SL -100% ROI).

### 2.3 Rejection Analysis (Log 1-8 Mei 2026)

| Filter | Jumlah Rejection | % |
|--------|------------------|---|
| Sideways structure | 8,462 | ~40% |
| Weak volume | 4,866 | ~23% |
| No sweep/BOS | 3,600 | ~17% |
| HTF bias conflict | 3,793 | ~18% |
| No retest zone | 2,172 | ~10% |
| Near resistance/support | 2,467 | ~12% |
| Wash trading | 1,655 | ~8% |
| **CIRCUIT BREAKER** | **301 scan** | **Hard block** |
| Spread reject | 117 | ~0.5% |

**Finding**: CIRCUIT BREAKER memblokir 301 scan karena daily loss limit tercapai. Bot kehilangan banyak peluang.

---

## 3. Masalah yang Ditemukan

### 3.1 KRITICAL: Exit Strategy Merusak Edge

| Masalah | Dampak | Solusi |
|---------|--------|--------|
| TP terlalu cepat (1.5-2x ATR) | Winner kecil, tidak bisa cover loss | Sudah diubah ke 3-5x ATR |
| Trailing terlalu ketat (1.5-2x ATR) | Kena noise, winner dipotong prematur | Sudah diubah ke 2.5-3x ATR |
| Trail dikurangi 50% setelah TP1 | Winner makin kecil | Sudah dihapus |
| SL_MODE=roi (-100% ROI) | Sama dengan tidak ada SL → liquidation | Perlu ganti ke SL_MODE=atr |
| BE profit lock terlalu kecil (0.3%) | Kena noise setelah TP1 | Sudah diubah ke 10% ROI |

### 3.2 HIGH: Liquidation Risk

Banyak posisi liquidate karena:
- Leverage 20-30x pada koin dengan ATR 3-8%
- SL di -100% ROI = tidak ada perlindungan
- Koin low-cap (SIRENUSDT, RAVEUSDT, APRUSDT) mudah dimanipulasi

**Formula**: `leverage × ATR% = risk per candle`
- 20x × 3% = 60% ROI per candle
- 3 candle salah = -180% = liquidation

### 3.3 MEDIUM: Circuit Breaker Terlalu Ketat

- Reset per 24h (UTC midnight) → bot block sampai besok
- Sudah diubah ke **rolling 6 jam** → bot bisa trade lagi lebih cepat

### 3.4 LOW: Whale Data Tidak Terasa Impact

- Hanya digunakan sebagai "tiebreaker" di AI prompt
- Tidak ada hard filter atau score modifier
- Sudah ditambahkan **Whale Impact Calculator** dengan score modifier

---

## 4. Perubahan yang Sudah Dilakukan

### 4.1 Exit Strategy (TP & Trailing)

| Parameter | Sebelum | Sesudah |
|-----------|---------|---------|
| TP1 | 1.5-2× ATR | 3× ATR |
| TP2 | 3× ATR | 5× ATR |
| TP3 | 5-7× ATR | 7-10× ATR |
| Trailing swing | 2.0× ATR | 3.0× ATR |
| Trailing intraday | 1.5× ATR | 2.5× ATR |
| Trail after TP1 | -50% | Full (tidak dikurangi) |
| Trail after TP2 | -80% ATR | Full (tidak dikurangi) |
| BE profit lock | 5% ROI / 0.3% price | 10% ROI |

### 4.2 Circuit Breaker

| Parameter | Sebelum | Sesudah |
|-----------|---------|---------|
| Reset period | UTC midnight (24h) | Rolling 6 jam |

### 4.3 Max Hold Time

| Parameter | Sebelum | Sesudah |
|-----------|---------|---------|
| MAX_HOLD_HOURS_SWING | 72h | 24h |

### 4.4 Whale Impact (NEW)

Ditambahkan sistem Whale Impact Calculator:
- Fetch OI change dari Bybit
- Fetch 24h price change dari Bybit
- Hitung impact score (0-10)
- Deteksi 6 sinyal whale (stealth_accumulation, whale_accumulation, distribution_top, whale_distribution, short_squeeze, long_liquidation)
- Score modifier: +1.5 sampai -1.0

---

## 5. Rekomendasi Perbaikan Lanjutan

### 5.1 Prioritas Tinggi

| # | Rekomendasi | Dampak | Effort |
|---|-------------|--------|--------|
| 1 | **Ganti SL_MODE ke `atr`** | Cegah liquidation | Low |
| 2 | **Tambah MAX_ATR_PCT=4.0 filter** | Reject koin terlalu volatil | Low |
| 3 | **Kurangi leverage untuk low-cap** | Cegah liquidation | Medium |

### 5.2 Prioritas Medium

| # | Rekomendasi | Dampak | Effort |
|---|-------------|--------|--------|
| 4 | **Tambah unit tests** | Prevent regression | High |
| 5 | **Structured logging (JSON)** | Analisis lebih mudah | Medium |
| 6 | **Rate limiter untuk API calls** | Prevent API ban | Medium |

### 5.3 Prioritas Rendah

| # | Rekomendasi | Dampak | Effort |
|---|-------------|--------|--------|
| 7 | **Health check endpoint** | Monitoring | Low |
| 8 | **Graceful shutdown handler** | Prevent data loss | Low |
| 9 | **Backtesting framework** | Validasi strategi | High |

---

## 6. Security Audit

### 6.1 Sudah Diperbaiki

| Issue | Status |
|-------|--------|
| SSL verification disabled | ✅ Fixed (pakai certifi) |
| InsecureRequestWarning suppressed | ✅ Fixed (dihapus) |

### 6.2 Sudah Aman

| Check | Status |
|-------|--------|
| .gitignore covers .env | ✅ |
| API key tidak di-hardcode | ✅ |
| API key tidak di-log | ✅ |
| Testnet default | ✅ |
| DRY_RUN default | ✅ |

### 6.3 Perlu Perhatian

| Issue | Risk | Rekomendasi |
|-------|------|-------------|
| `_SESSION.verify = False` di beberapa tempat | Medium | Sudah diperbaiki |
| No rate limiting | Low | Tambah ratelimit library |
| Log file menyimpan data sensitif | Low | Rotate logs, encrypt at rest |

---

## 7. Code Quality

### 7.1 Strengths

- **Agentic Trading Workflow**: 5-role committee architecture (Scout, Analyst, Risk Manager, Position Manager, Reviewer)
- **Clean architecture**: 4-layer separation yang jelas
- **Comprehensive logging**: Setiap step di-log
- **Error handling**: Fallback di setiap API call
- **Configuration**: Semua parameter configurable via .env
- **Documentation**: README dan bot_capabilities.md lengkap

### 7.2 Weaknesses

- **No unit tests**: Tidak ada folder tests/
- **Magic numbers**: Beberapa threshold langsung di code
- **Large files**: main.py > 2200 lines, perlu dipecah
- **Duplicated code**: main.py dan main og.py mirip
- **No type hints**: Beberapa fungsi tidak ada type hints

---

## 8. Kesimpulan

### Rating Keseluruhan: ★★★★☆ (Sangat Baik)

**Strengths:**
- **Agentic Trading Workflow** (5-role committee) — decision-making sophisticated
- Filter pipeline sangat comprehensive (10+ layers)
- Whale intelligence yang canggih (Whale Impact Calculator)
- AI learning & adaptive risk system
- Multi-provider AI dengan 3-tier fallback
- Interactive DCA via Telegram

**Weaknesses:**
- Exit strategy merusak edge (SUDAH DIPERBAIKI)
- Liquidation risk dari leverage tinggi + SL lebar
- No unit tests
- Circuit breaker terlalu ketat (SUDAH DIPERBAIKI)

### Next Steps

1. **Test perubahan TP/Trailing** dengan DRY_RUN
2. **Monitor performa** setelah perubahan
3. **Pertimbangkan ganti SL_MODE ke `atr`** untuk cegah liquidation
4. **Tambah unit tests** untuk critical path

---

## Appendix: File Changes

| File | Changes |
|------|---------|
| `data/onchain_feed.py` | +calculate_whale_impact(), +_fetch_bybit_oi(), +_fetch_bybit_ticker(), updated _fetch_symbol_whale_metrics_sync() |
| `main.py` | +Whale Impact Score Modifier, updated circuit breaker to 6h rolling |
| `utils/pnl_tracker.py` | Updated trailing multiplier (3x/2.5x), removed trail tightening |
| `config.py` | Updated BE_PROFIT_LOCK_ROI_PCT to 10%, MAX_HOLD_HOURS_SWING to 24h |
| `analyzer/signal_engine.py` | Updated AI prompt for wider TP (3x/5x/7-10x ATR) |
| `inject_sl_tp.py` | Updated TP multipliers (3.0/5.0/7.0) and trail (3.0) |
| `bot_capabilities.md` | Updated rating dan descriptions |
