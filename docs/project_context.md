# Project Context

## Project

Path: `C:\Users\kejar\Trading Bot - switch`

Bot: Marvel AI Quant Trading Bot

Mode runtime utama: `INTRADAY`, `DRY_RUN=true`, Bybit Futures CEX.

## MCP Setup

MCP server dibuat untuk monitoring dan integrasi AI tools.

Files terkait:

- `mcp_server.py`
- `mcp_config.json`
- `.codex/mcp.json`
- `MCP_README.md`

Codex CLI sudah punya MCP server global:

```text
trading-bot -> python mcp_server.py
```

MCP server bersifat monitoring/read-only bridge. Ia tidak otomatis menjalankan bot trading.

## Dashboard

Dashboard lokal:

```text
http://localhost:5050
http://127.0.0.1:5050
```

Run:

```bash
python dashboard.py
```

Dashboard sempat tidak update open-position PnL karena `pnl_store.json` tidak menyimpan `current_price`. Fix sudah dibuat di `dashboard.py`: endpoint `/api/data` sekarang fetch harga live Bybit untuk open positions, lalu menghitung unrealized PnL dari harga live.

Test hasil setelah fix:

```text
ZECUSDT  entry=581.93  current=585.8    PnL=+19.15 USDT  ROI=+7.98%
VVVUSDT  entry=16.82   current=17.159   PnL=+58.05 USDT  ROI=+24.19%
```

Jika dashboard ingin dishare ke orang lain, jangan share direct tanpa auth. Rekomendasi cepat: `ngrok http 5050 --basic-auth="viewer:PasswordKuat123"`. Lebih aman jangka panjang: Cloudflare Tunnel + Cloudflare Access.

## Circuit Breaker

STORJUSDT loss `-32.6360 USDT` pernah trigger circuit breaker karena limit lama terlalu kecil.

Config sudah diubah di `.env`:

```env
DAILY_LOSS_LIMIT_USDT=100.0
INTRADAY_DAILY_LOSS_LIMIT_USDT=100.0
```

Restart bot diperlukan agar config baru terbaca.

## STORJUSDT Incident

Trade:

```text
STORJUSDT SHORT
entry=0.1195
SL=0.1208
leverage=12x
PnL=-32.6360 USDT
reason=stop_loss
```

Analisa:

- Bot entry pada `struct=breakdown`, `RSI=39.5`, `vol=strong`.
- Harga sempat naik kena SL, lalu turun sesuai arah prediksi.
- Masalah utama bukan direction, tapi whipsaw/false breakdown dan SL yang terlalu sensitif.
- `short_retest_zone` terlalu luas karena `near_ema20` dianggap retest valid tanpa candle confirmation.

## SMC/ICT Retest Fix

File: `data/market_feed.py`

Sebelum:

```python
"long_retest_zone": near_support or near_ema20
"short_retest_zone": near_resistance or near_ema20
```

Masalah: near EMA20 langsung dianggap retest valid.

Sesudah:

- Long retest butuh near support/EMA20 + bullish candle atau lower rejection wick.
- Short retest butuh near resistance/EMA20 + bearish candle atau upper rejection wick.
- EMA retest juga butuh posisi harga sesuai arah (`cur > ema20` untuk long, `cur < ema20` untuk short).

Bug setelah fix:

- `_smc_metrics` adalah `@staticmethod`, tapi sempat salah ditambah parameter `self`.
- Ini menyebabkan argument shift dan `closes` menerima float `ema20`, lalu `len(closes)` error.
- Sudah diperbaiki dengan menghapus `self` dari signature dan passing `opens` secara eksplisit.

Validasi:

```text
ETHUSDT fetch OK
short_retest_zone: True
long_retest_zone: False
near_ema20: True
```

## LLM Tiering

TIER-1 sempat memakai:

```env
AI_PROVIDER=deepseek
DEEPSEEK_MODEL=mimo-v2.5-pro
DEEPSEEK_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1
```

Masalah:

- `mimo-v2.5-pro` reasoning-heavy.
- Untuk prompt multi-symbol besar, sering menghasilkan `content` kosong/truncated dan hanya `reasoning_content`.
- Bot butuh final JSON di `content`, bukan reasoning text.
- Fallback parsing `reasoning_content` sempat dicoba, tapi salah karena reasoning text bukan JSON valid.

Fix:

- Jangan parse `reasoning_content` sebagai final JSON.
- Jika TIER-1 hanya mengembalikan reasoning tanpa content, biarkan gagal bersih dan fallback ke TIER-2.
- Tambah `mimo` ke reasoning-disable keywords agar extra body mencoba disable reasoning:

```python
reasoning_keywords = [..., "mimo"]
```

Rekomendasi model MiMo untuk bot:

```env
DEEPSEEK_MODEL=mimo-v2.5
```

Bukan `mimo-v2.5-pro`, karena bot butuh structured JSON cepat dan stabil, bukan reasoning panjang.

Recommended tier:

```env
AI_PROVIDER=deepseek
DEEPSEEK_MODEL=mimo-v2.5

AI_PROVIDER_FALLBACK=openrouter
AI_TIER2_MODEL=deepseek-v4-flash

AI_PROVIDER_TIER3=qwen
AI_TIER3_MODEL=qwen/qwen3-coder:free
```

## Scoring Documentation

File baru:

- `scoring.md`

Scoring juga sudah diringkas dan dimasukkan ke:

- `docs/bot_capabilities.md`
- `docs/bot_capabilities_indo.md`

Inti scoring:

- Score ditentukan oleh AI, bukan dihitung deterministik oleh bot.
- Swing mode max sekitar `14.5`.
- Intraday mode max sekitar `14`.
- Intraday entry threshold `score >= 8.0`, confidence minimal `0.70`.
- Swing confidence minimal `0.65`.

Known issue: AI bisa overconfident, sehingga score tinggi tetap harus ditahan oleh memory, SMC/ICT, risk filters, RR, dan execution filters.

## Trade History Snapshot

Ringkasan saat dicek:

```text
Total trades: 136
Wins: 65
Losses: 71
Win rate: ~47.8%
Total realized PnL: -40.42 USDT
Best trade: +20.31 USDT (BILLUSDT)
Worst trade: -32.64 USDT (STORJUSDT)
```

Recent notable:

- BILLUSDT SHORT profitable.
- STORJUSDT SHORT caused major loss and circuit breaker.

## Public Access / Sharing

Dashboard default hanya lokal. URL `http://100.64.100.6:5050/` berada di private/CGNAT range. Tidak aman dishare sembarangan karena orang dalam jaringan/VPN yang sama bisa akses.

Untuk share aman:

- Prefer screenshot jika hanya konsultasi.
- Jika live access diperlukan, gunakan ngrok dengan basic auth atau Cloudflare Access.
- Jangan expose endpoint yang bisa open/close/modify posisi.
- Jangan expose `.env`, API key, raw logs, atau config sensitif.

## Important Operational Notes

- Setelah perubahan `.env`, bot harus restart.
- Setelah perubahan `dashboard.py`, dashboard harus restart.
- MCP server tidak menjalankan bot otomatis.
- Jika bot `No market data this cycle`, cek exception di `data/market_feed.py` atau function `_smc_metrics`.
- Jika TIER-1 kosong/truncated, cek apakah model reasoning-heavy dan apakah output final JSON ada di `content`.
