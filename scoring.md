# Scoring System Documentation

## Overview

Score ditentukan oleh AI (DeepSeek/GPT/Gemini), bukan dihitung oleh bot. Bot mengirim data market ke AI, AI return score 0-14, bot filter berdasarkan threshold.

---

## SWING MODE (4H Primary)

Max Score: ~14.5

### Scoring Categories

| # | Kategori | Poin | Keterangan |
|---|----------|------|------------|
| 1 | EMA trend alignment (4H) | +2 / +1 / 0 | full / partial / misaligned |
| 2 | HTF alignment | +1.5 / 0 / -2 | aligned / neutral / against |
| 3 | Structure (4H) | +2 / +2.5 / 0 | aligned / breakout+breakdown / sideways |
| 4 | RSI (4H) | +1 / +0.5 / +0.5 / -1 | ideal / ok / daily confirms / extreme |
| 5 | Volume | +2 / +1.5 / +0.5 / -1 | strong+rising / strong+flat / normal / weak |
| 6 | OI | +1 / 0 / -0.5 / -0.5 | rising aligned / flat / rising against / falling |
| 7 | Funding | +1 / 0 / -1 | healthy / warm / extreme against |
| 8 | Entry location | +1.5 / 0 / -1.5 | good pullback / neutral / near opposing level |
| 9 | Volatility | +1 / -1 | suitable / too high |
| 10 | MTF alignment | +2 / +1 / 0 / -1 | 4H+1H+15M / 4H+1H / 1H neutral / 1H against |

### Combo Penalty

- structure=uptrend AND htf_bias=neutral → -1.5

### Bonus

- 1H pullback entry (h1_rsi oversold for LONG / overbought for SHORT) → +0.5
- 15M momentum confirms direction → +0.5

### Decision Threshold (Swing)

| Score | Decision | Risk % |
|-------|----------|--------|
| < 5.5 | HOLD | - |
| >= 5.5 | Entry (ok) | 1.0% |
| >= 6.5 | Entry (good) | 1.5% |
| >= 8.0 | Entry (strong) | 2.0% |

### Setup Override

- structure=uptrend AND htf_bias=neutral → min score 7.5 (not 5.5)

### Confidence Requirement (Swing)

- Minimum: 0.65

---

## INTRADAY MODE (15M Primary)

Max Score: ~14

### Scoring Categories

| # | Kategori | Poin | Keterangan |
|---|----------|------|------------|
| 1 | 4H alignment (h1_ema_trend) | +3 / 0 / -3 | aligned / neutral / against (reject) |
| 2 | 1H HTF (htf_bias) | +2 / 0 / -2 | aligned / neutral / against |
| 3 | 15M structure (market_structure) | +2.5 / +2 / 0 | breakout/breakdown / trend / sideways |
| 4 | 15M RSI (rsi) | +1 / -1 / +0.5 | ideal / extreme / divergence bonus |
| 5 | Volume (15M) | +2 / +0.5 / reject | spike+rising / normal / weak |
| 6 | 5M momentum (m15_momentum) | +1 / 0 / -1 | confirms / neutral / against |
| 7 | 5M structure (m15_structure) | +1 / 0 | aligned breakout / neutral |
| 8 | Entry location | +1.5 / +1 / -1.5 | at S/R bounce / clean air / near resistance |
| 9 | Funding | +0.5 / -1 | neutral / extreme against |

### Decision Threshold (Intraday)

| Score | Decision | Risk % |
|-------|----------|--------|
| < 8.0 | HOLD | - |
| >= 8.0 | Entry (ok) | 1.0% |
| >= 9.0 | Entry (good) | 1.5% |
| >= 10.0 | Entry (strong) | 2.0% |

### Confidence Requirement (Intraday)

- Minimum: 0.70

---

## Hard Reject Rules

### Both Modes

- BTC global bias OPPOSITE to direction (LONG when BTC bearish, SHORT when BTC bullish) UNLESS score>=9.5 AND volume spike strong
- late chase entry without sweep/BOS/retest confirmation
- funding_anomaly=true (z>=3, cascade risk)
- wick_warning=true (recent >=2xATR wick = stop-hunt active)
- wash_severity=high (Volume>$50M + OI<1%)

### Intraday Additional

- 4H trend (h1_ema_trend) opposite to trade direction
- 1H (htf_bias) opposite to trade direction without strong 15M breakout
- weak volume on 15M
- 15M structure sideways (no clear setup)
- entry near strong S/R on 15M
- RR < 2.0
- atr_pct (15M) > 3%
- spread_pct > 0.15

---

## Signal Continuity Rules

### Anti-Flip (Swing)

If prev.action=HOLD and prev.score<6 less than 60 min ago:
- MUST justify any LONG/SHORT with NEW evidence
- Score change >3 in <60 min on same 4H candle = AI uncertainty → HOLD

### Anti-Flip (Intraday)

If prev.action=HOLD and prev.score<5 less than 15 min ago:
- MUST justify any LONG/SHORT with NEW evidence
- Score volatility >5 in <15 min without clear catalyst = HOLD

### Continuation

If prev.action was LONG/SHORT same direction recently:
- Prefer continuation over abrupt reversal
- Reversing within same candle = high-fail

---

## AI Learning Integration

### Adaptive Modes

| Mode | Trigger | Min Score | Leverage Mult |
|------|---------|-----------|---------------|
| Defensive | 5+ loss streak OR weekly DD > 25% | 7.0 | 0.6x |
| Conservative | 3+ loss streak OR WR < 35% | 6.5 | 0.8x |
| Normal | Default | 5.5 (swing) / 8.0 (intraday) | 1.0x |
| Aggressive | 3+ win streak AND WR > 65% | 5.0 | 1.3x |

### Memory Penalty

- Symbol with WR < 40% (>=3 trades) → size reduced 30%
- Symbol with repeated high-score losses → score penalty

---

## Bot-Side Filters (After AI Score)

### SMC/ICT Filter

| Check | Condition | Action |
|-------|-----------|--------|
| Retest zone | near_resistance AND (wick OR bearish) | Allow |
| Retest zone | near_ema20 AND (wick OR bearish) AND price<ema20 | Allow |
| Breakdown | structure=breakdown AND volume_trend!=rising | Reject (hard) |
| Premium zone | LONG in premium without sweep/retest | Reject |
| Discount zone | SHORT in discount without sweep/retest | Reject |
| RSI extreme | LONG RSI>68 without swept_low | Reject |
| RSI extreme | SHORT RSI<32 without swept_high | Reject |
| EMA extension | distance > soft_extension_pct | Reject |

### Score Threshold (Bot-side)

| Mode | Hard Floor |
|------|------------|
| Swing | 7.0 |
| Intraday | 8.0 |

### Confidence Threshold (Bot-side)

| Mode | Hard Floor |
|------|------------|
| Swing | 0.65 |
| Intraday | 0.70 |

---

## Risk/Reward Filter

- Minimum RR: 1.2 (swing) / 2.0 (intraday)
- Formula: weighted TP reward / SL risk

---

## Known Issues

1. **AI Overconfidence**: AI can give score 9.5 for bad setups (e.g., STORJUSDT false breakdown)
2. **No Hard Calculation**: Score is subjective from AI, not formula-based
3. **Soft Bypass**: score>=8.6 AND conf>=0.78 can bypass SMC filter in soft mode
4. **Trend Alignment Bypass**: trend_aligned_short can skip SMC check in soft mode
