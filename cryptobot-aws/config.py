"""
config.py — Central configuration loader.
Reads all settings from .env via python-dotenv.

Supported AI Providers:
  - anthropic : Claude models via Anthropic SDK
  - blink     : Claude models via Blink API (OpenAI-compatible)
  - openai    : GPT models via OpenAI API
  - gemini    : Google Gemini via OpenAI-compatible endpoint
  - glm       : GLM models via ZhipuAI (OpenAI-compatible)
  - minimax   : MiniMax models via OpenAI-compatible endpoint
  - custom    : Any OpenAI-compatible API
"""

import os
from typing import List
from dotenv import load_dotenv

load_dotenv()


# ── Provider presets (base_url defaults) ──────────────────────────
_PROVIDER_PRESETS = {
    "openai":     {"base_url": "https://api.openai.com/v1",            "default_model": "gpt-4.1-mini"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta", "default_model": "gemini-2.0-flash"},
    "glm":        {"base_url": "https://api.z.ai/api/paas/v4",         "default_model": "glm-5"},
    "minimax":    {"base_url": "https://api.minimaxi.chat/v1",         "default_model": "MiniMax-M1-80k"},
    "blink":      {"base_url": "https://core.blink.new/api/v1/ai",     "default_model": "anthropic/claude-sonnet-4.5"},
    "qwen":       {"base_url": "https://openrouter.ai/api/v1",         "default_model": "qwen/qwen3.5-flash-02-23"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",         "default_model": "deepseek/deepseek-v4-flash"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",          "default_model": "deepseek-v4-flash"},
}

# All providers that use OpenAI-compatible SDK
_OPENAI_COMPATIBLE = {"openai", "gemini", "glm", "minimax", "blink", "qwen", "openrouter", "deepseek", "custom"}


class Config:
    # ── AI Provider ───────────────────────────────────────────
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "blink").lower()

    # AI HTTP client guardrails. Some VPS/provider combinations need a longer
    # idle window for large multi-symbol prompts (30 symbols can request ~12k
    # output tokens). Keep configurable so timeout tuning does not require code
    # changes.
    AI_REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "240"))
    AI_MAX_RETRIES: int = int(os.getenv("AI_MAX_RETRIES", "1"))
    # Force OpenAI-compatible APIs to return a JSON object when supported.
    # Disable with AI_FORCE_JSON_RESPONSE=false if a proxy/model rejects
    # response_format.
    AI_FORCE_JSON_RESPONSE: bool = os.getenv("AI_FORCE_JSON_RESPONSE", "true").lower() == "true"
    AI_JSON_RETRY_ON_FAILURE: bool = os.getenv("AI_JSON_RETRY_ON_FAILURE", "true").lower() == "true"

    # Anthropic native SDK
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    # Blink
    BLINK_API_KEY: str = os.getenv("BLINK_API_KEY", "")
    BLINK_BASE_URL: str = os.getenv("BLINK_BASE_URL", _PROVIDER_PRESETS["blink"]["base_url"])
    BLINK_MODEL: str = os.getenv("BLINK_MODEL", _PROVIDER_PRESETS["blink"]["default_model"])

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "")

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_BASE_URL: str = os.getenv("GEMINI_BASE_URL", _PROVIDER_PRESETS["gemini"]["base_url"])
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", _PROVIDER_PRESETS["gemini"]["default_model"])

    # GLM (ZhipuAI)
    GLM_API_KEY: str = os.getenv("GLM_API_KEY", "")
    GLM_BASE_URL: str = os.getenv("GLM_BASE_URL", _PROVIDER_PRESETS["glm"]["base_url"])
    GLM_MODEL: str = os.getenv("GLM_MODEL", _PROVIDER_PRESETS["glm"]["default_model"])

    # MiniMax
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_BASE_URL: str = os.getenv("MINIMAX_BASE_URL", _PROVIDER_PRESETS["minimax"]["base_url"])
    MINIMAX_MODEL: str = os.getenv("MINIMAX_MODEL", _PROVIDER_PRESETS["minimax"]["default_model"])

    # Custom (any OpenAI-compatible)
    CUSTOM_API_KEY: str = os.getenv("CUSTOM_API_KEY", "")
    CUSTOM_BASE_URL: str = os.getenv("CUSTOM_BASE_URL", "")
    CUSTOM_MODEL: str = os.getenv("CUSTOM_MODEL", "")

    # Qwen via OpenRouter (OpenAI-compatible)
    QWEN_API_KEY: str = os.getenv("QWEN_API_KEY", "")
    QWEN_BASE_URL: str = os.getenv("QWEN_BASE_URL", _PROVIDER_PRESETS["qwen"]["base_url"])
    QWEN_MODEL: str = os.getenv("QWEN_MODEL", _PROVIDER_PRESETS["qwen"]["default_model"])

    # OpenRouter (proper slot — generic alias untuk semua model di OpenRouter,
    # mis. deepseek/deepseek-v4-flash. Sebelumnya ditumpangkan di slot qwen)
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", _PROVIDER_PRESETS["openrouter"]["base_url"])
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", _PROVIDER_PRESETS["openrouter"]["default_model"])

    # DeepSeek native API (api.deepseek.com) — punya prompt caching $0.0028/M
    # input untuk cache hit. Auto cache sisi server, tidak perlu code change.
    # Model: deepseek-chat (= V4-Flash), deepseek-reasoner (= V4-Pro reasoning).
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", _PROVIDER_PRESETS["deepseek"]["base_url"])
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", _PROVIDER_PRESETS["deepseek"]["default_model"])

    # ── AI Fallback (Tier 2) ──────────────────────────────────
    # When primary AI fails (timeout / API error / parse failure / empty response),
    # bot retries the same scan with this fallback provider. Empty = no fallback.
    AI_PROVIDER_FALLBACK: str = os.getenv("AI_PROVIDER_FALLBACK", "").lower()

    # ── AI Tier-1 Override (optional) ─────────────────────────
    # Provides a uniform tier-1 override interface that matches tier-2/3.
    # When set, these values override whatever AI_PROVIDER's default slot
    # supplies. Lets the .env stay clean (only tier-1/2/3 blocks, no need
    # for per-provider slots).
    AI_TIER1_MODEL: str = os.getenv("AI_TIER1_MODEL", "")
    AI_TIER1_API_KEY: str = os.getenv("AI_TIER1_API_KEY", "")
    AI_TIER1_BASE_URL: str = os.getenv("AI_TIER1_BASE_URL", "")

    # ── AI Tier-2 Override (optional) ─────────────────────────
    # If set, Tier-2 uses this instead of pulling defaults from
    # AI_PROVIDER_FALLBACK's standard slot. Useful when Tier-2 reuses the
    # same provider as Tier-1 (e.g. both via OpenRouter) but with a different model.
    AI_TIER2_MODEL: str = os.getenv("AI_TIER2_MODEL", "")
    AI_TIER2_API_KEY: str = os.getenv("AI_TIER2_API_KEY", "")
    AI_TIER2_BASE_URL: str = os.getenv("AI_TIER2_BASE_URL", "")

    # ── AI Tier-3 (optional 2nd fallback) ─────────────────────
    # Triggered when both Tier-1 and Tier-2 fail.
    AI_PROVIDER_TIER3: str = os.getenv("AI_PROVIDER_TIER3", "").lower()
    AI_TIER3_MODEL: str = os.getenv("AI_TIER3_MODEL", "")
    AI_TIER3_API_KEY: str = os.getenv("AI_TIER3_API_KEY", "")
    AI_TIER3_BASE_URL: str = os.getenv("AI_TIER3_BASE_URL", "")

    # ── Exchange ─────────────────────────────────────────────
    EXCHANGE_MODE: str = os.getenv("EXCHANGE_MODE", "CEX").upper()
    # Which CEX executes orders when EXCHANGE_MODE=CEX. Bybit tetap default —
    # menambah OKX tidak mengubah jalur yang sudah jalan.
    # Sengaja TIDAK dijadikan EXCHANGE_MODE=OKX: gate CEX di
    # risk/decision_engine.py (materialize_effective_stop) dan sizing RR
    # berlaku untuk semua CEX perp, jadi OKX harus tetap terbaca "CEX".
    CEX_PROVIDER: str = os.getenv("CEX_PROVIDER", "bybit").lower()
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # Dry-run execution costs.  Historical dry-run PnL was gross because fills
    # were booked exactly at the planned price with no fees.  Keep the model
    # explicit so new samples are comparable to a taker-order live path.
    DRY_RUN_INCLUDE_COSTS: bool = os.getenv("DRY_RUN_INCLUDE_COSTS", "true").lower() == "true"
    TAKER_FEE_PCT: float = float(os.getenv("TAKER_FEE_PCT", "0.055"))
    DRY_RUN_SLIPPAGE_PCT: float = float(os.getenv("DRY_RUN_SLIPPAGE_PCT", "0.02"))

    # CEX — Bybit
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
    BYBIT_SECRET: str = os.getenv("BYBIT_SECRET", "")
    BYBIT_TESTNET: bool = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

    # CEX — OKX (USDT-margined SWAP / perpetual)
    # Kredensial trading memakai trio yang SAMA dengan feed Web3 di bawah
    # (OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE) — satu API key OKX
    # melayani REST v5 trading maupun data. Tidak ada secret baru.
    # OKX_DEMO=true memakai header simulated-trading (x-simulated-trading: 1),
    # bukan host terpisah seperti Bybit testnet.
    OKX_DEMO: bool = os.getenv("OKX_DEMO", "true").lower() == "true"
    OKX_BASE_URL: str = os.getenv("OKX_BASE_URL", "https://www.okx.com")
    # OKX perp dihitung dalam KONTRAK, bukan koin. Order size harus dibulatkan
    # ke lotSz dan >= minSz; konversi koin->kontrak pakai ctVal per instrumen.
    OKX_MARGIN_MODE: str = os.getenv("OKX_MARGIN_MODE", "isolated").lower()

    # DEX — Hyperliquid
    HL_WALLET_ADDRESS: str = os.getenv("HL_WALLET_ADDRESS", "")
    HL_PRIVATE_KEY: str = os.getenv("HL_PRIVATE_KEY", "")
    HL_TESTNET: bool = os.getenv("HL_TESTNET", "true").lower() == "true"

    # Binance (on-chain/regime data — public endpoints only, key optional)
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")

    # OKX Web3 (optional enrichment only; execution remains Bybit/Hyperliquid)
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", os.getenv("OK_ACCESS_KEY", ""))
    OKX_SECRET_KEY: str = os.getenv("OKX_SECRET_KEY", os.getenv("OK_ACCESS_SECRET", ""))
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", os.getenv("OK_ACCESS_PASSPHRASE", ""))
    OKX_PROJECT_ID: str = os.getenv("OKX_PROJECT_ID", os.getenv("OK_ACCESS_PROJECT", ""))
    OKX_WEB3_ENABLED: bool = os.getenv("OKX_WEB3_ENABLED", "false").lower() == "true"
    OKX_WEB3_BASE_URL: str = os.getenv("OKX_WEB3_BASE_URL", "https://web3.okx.com")
    OKX_WEB3_DEFAULT_CHAIN: str = os.getenv("OKX_WEB3_DEFAULT_CHAIN", "501")  # Solana
    OKX_WEB3_TOKEN_MAP: str = os.getenv("OKX_WEB3_TOKEN_MAP", "")
    OKX_WEB3_AUTO_RESOLVE: bool = os.getenv("OKX_WEB3_AUTO_RESOLVE", "false").lower() == "true"
    OKX_WEB3_RESOLVE_MIN_LIQUIDITY_USD: float = float(os.getenv("OKX_WEB3_RESOLVE_MIN_LIQUIDITY_USD", "100000"))
    OKX_WEB3_MAX_SYMBOLS: int = int(os.getenv("OKX_WEB3_MAX_SYMBOLS", "30"))
    OKX_WEB3_CACHE_TTL_SECONDS: int = int(os.getenv("OKX_WEB3_CACHE_TTL_SECONDS", "300"))
    OKX_WEB3_TIMEOUT_SECONDS: float = float(os.getenv("OKX_WEB3_TIMEOUT_SECONDS", "10"))
    OKX_WEB3_MIN_INTERVAL_MS: int = int(os.getenv("OKX_WEB3_MIN_INTERVAL_MS", "250"))
    OKX_WEB3_429_MAX_RETRIES: int = int(os.getenv("OKX_WEB3_429_MAX_RETRIES", "2"))
    OKX_WEB3_429_BASE_DELAY_MS: int = int(os.getenv("OKX_WEB3_429_BASE_DELAY_MS", "1000"))
    # How many actually fetched market-data symbols are passed into regime/on-chain
    # context. 0 = all fetched symbols. OKX_WEB3_MAX_SYMBOLS still caps OKX calls.
    ONCHAIN_CONTEXT_MAX_SYMBOLS: int = int(os.getenv("ONCHAIN_CONTEXT_MAX_SYMBOLS", "0"))

    # ── Telegram ──────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Trading ───────────────────────────────────────────────
    _raw_symbols: str = os.getenv("SYMBOLS", "BTC/USDT:USDT")
    SYMBOLS: List[str] = [s.strip() for s in _raw_symbols.split(",") if s.strip()]
    ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))
    SCAN_INTERVAL: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "2"))

    # ── Symbol blacklist (HARD reject) ────────────────────────
    # Bot REFUSE entry pada simbol di list ini, meski:
    #   - lolos quality score
    #   - di-pick AUTO_SYMBOLS auto-discovery
    #   - manual ada di SYMBOLS list
    # Tujuan: kunci simbol yang historis rugi konsisten supaya tidak nyangkut lagi.
    _raw_blacklist: str = os.getenv("SYMBOL_BLACKLIST", "")
    SYMBOL_BLACKLIST: List[str] = [
        s.strip().upper() for s in _raw_blacklist.split(",") if s.strip()
    ]

    # ── Data-only symbols (fetch but don't trade) ─────────────
    # Simbol yang di-fetch market datanya untuk konteks AI (mis. BTC sebagai
    # global bias anchor di prompt, lihat signal_engine.py BTC GLOBAL BIAS),
    # TAPI bot tidak boleh masuk posisi di simbol ini.
    # Default: BTCUSDT — dipakai sebagai "leader" untuk menentukan
    # alt LONG/SHORT bias, tapi bot punya WR rendah trading BTC sendiri.
    _raw_data_only: str = os.getenv("DATA_ONLY_SYMBOLS", "BTCUSDT")
    DATA_ONLY_SYMBOLS: List[str] = [
        s.strip().upper() for s in _raw_data_only.split(",") if s.strip()
    ]

    # ── Detection thresholds (4 risk filters) ─────────────────
    # Reject signal kalau spread > MAX_SPREAD_PCT (orderbook ask-bid / mid)
    # Default 0.15% — coin mainstream tipikal <0.05%, low-cap >0.2%.
    MAX_SPREAD_PCT: float = float(os.getenv("MAX_SPREAD_PCT", "0.15"))

    # Reject signal kalau wick (high-close atau close-low) > N × ATR pada
    # 1 candle terakhir. Indikasi stop-hunt / manipulation.
    MAX_WICK_ATR_RATIO: float = float(os.getenv("MAX_WICK_ATR_RATIO", "2.5"))

    # Reject signal kalau funding rate berubah > N × stdev dari 24h history.
    # 3.0 = 3-sigma anomaly, indikasi funding spike / manipulation.
    FUNDING_ANOMALY_SIGMA: float = float(os.getenv("FUNDING_ANOMALY_SIGMA", "3.0"))

    # The latest cost-adjusted cohort showed falling-volume entries losing in
    # every chronological slice.  This is a deterministic confirmation gate,
    # independent from the AI score/confidence self-rating.
    REJECT_FALLING_VOLUME_ENTRIES: bool = (
        os.getenv("REJECT_FALLING_VOLUME_ENTRIES", "true").lower() == "true"
    )

    # ── Auto symbol discovery ──────────────────────────────────
    # If AUTO_SYMBOLS=true, SYMBOLS list above is ignored at startup.
    # Bot fetches top AUTO_SYMBOLS_COUNT USDT-perps by 24h volume from Bybit.
    # Symbol list is refreshed every AUTO_SYMBOLS_REFRESH_H hours.
    AUTO_SYMBOLS: bool = os.getenv("AUTO_SYMBOLS", "false").lower() == "true"
    AUTO_SYMBOLS_COUNT: int = int(os.getenv("AUTO_SYMBOLS_COUNT", "30"))
    AUTO_SYMBOLS_REFRESH_H: float = float(os.getenv("AUTO_SYMBOLS_REFRESH_H", "4"))
    # Min 24h USD turnover utk masuk auto pool — filter wash + low-cap
    AUTO_SYMBOLS_MIN_TURNOVER_USD: float = float(
        os.getenv("AUTO_SYMBOLS_MIN_TURNOVER_USD", "30000000")
    )

    # Position sizing limits (% of balance)
    MIN_POSITION_SIZE_PCT: float = float(os.getenv("MIN_POSITION_SIZE_PCT", "1.0"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "5.0"))

    # ── SL mode ───────────────────────────────────────────────
    # "atr"  : SL mengikuti level volatilitas/ATR yang sudah melewati risk gate
    # "roi"  : mode legacy, SL berbasis ROI dengan price-move cap
    # "none" : tidak place SL sama sekali
    SL_MODE: str = os.getenv("SL_MODE", "atr").lower()

    # ── DCA (Dollar Cost Averaging) ──────────────────────────
    # Disabled by default: the historical sample does not demonstrate a DCA
    # edge, while adding size after invalidation bypasses the original risk cap.
    DCA_ENABLED: bool = os.getenv("DCA_ENABLED", "false").lower() == "true"
    DCA_SIZE_PCT: float = float(os.getenv("DCA_SIZE_PCT", "1.0"))  # % of balance per DCA
    DCA_TIMEOUT: int = int(os.getenv("DCA_TIMEOUT", "120"))  # seconds to wait for user reply
    # ROI threshold to trigger DCA alert (leveraged %, negative = losing)
    # e.g. -75 = ask DCA when position is down 75% of margin
    DCA_ROI_TRIGGER: float = float(os.getenv("DCA_ROI_TRIGGER", "-75.0"))

    # ── Risk/Reward filter ────────────────────────────────────
    # Reject signal if weighted TP reward / SL risk < MIN_RR.
    # Weighted reward follows the planned exits: TP1, TP2, then TP3 remainder.
    MIN_RR: float = float(os.getenv("MIN_RR", "1.2"))
    # Audit 2026-07-26: floor kedua yang menilai runner di TP2 (bukan TP3 yang
    # jarang tercapai), net of round-trip cost. Menolak setup dengan TP1/TP2
    # mikroskopis vs SL — sumber winner 0.5R di Juli. 0 = nonaktif.
    RR_TP2_FLOOR: float = float(os.getenv("RR_TP2_FLOOR", "1.0"))

    # ── Hard quality floor (overrides adaptive min) ───────────
    # AI-rated score and confidence are subjective — bot's own filter floor
    # provides backstop. Adaptive min_score/min_conf may go lower in normal
    # mode but bot will still reject anything below MIN_SCORE_HARD/MIN_CONF_HARD.
    MIN_SCORE_HARD: float = float(os.getenv("MIN_SCORE_HARD", "7.0"))
    MIN_CONFIDENCE_HARD: float = float(os.getenv("MIN_CONFIDENCE_HARD", "0.65"))

    # Minimum SL distance from entry (% price). Tighter SL = noise stops out.
    # Bybit fee × 2 = 0.11%, plus need buffer for normal wick.
    # 0.4% minimum = SL fires only on real movement, not random tick.
    MIN_SL_DISTANCE_PCT: float = float(os.getenv("MIN_SL_DISTANCE_PCT", "0.40"))

    # Adaptive leverage guardrail (applied in Phase-3 post-filter)
    LEVERAGE_FLOOR: int = int(os.getenv("LEVERAGE_FLOOR", "1"))
    LEVERAGE_CAP_DEFENSIVE: int = int(os.getenv("LEVERAGE_CAP_DEFENSIVE", "5"))
    LEVERAGE_CAP_CONSERVATIVE: int = int(os.getenv("LEVERAGE_CAP_CONSERVATIVE", "8"))
    LEVERAGE_CAP_NORMAL: int = int(os.getenv("LEVERAGE_CAP_NORMAL", "10"))
    LEVERAGE_CAP_AGGRESSIVE: int = int(os.getenv("LEVERAGE_CAP_AGGRESSIVE", "10"))
    LEVERAGE_CAP_HIGH_RISK: int = int(os.getenv("LEVERAGE_CAP_HIGH_RISK", "5"))

    # ── High-conviction size multiplier ───────────────────────
    # Attribution only by default. The audited high-score cohort did not show
    # a reliable sizing edge, so score-only size-up remains disabled.
    HIGH_CONVICTION_ENABLED: bool = os.getenv("HIGH_CONVICTION_ENABLED", "false").lower() == "true"
    HIGH_CONVICTION_SCORE: float = float(os.getenv("HIGH_CONVICTION_SCORE", "9.5"))
    HIGH_CONVICTION_SIZE_MULT: float = float(os.getenv("HIGH_CONVICTION_SIZE_MULT", "1.0"))

    # Expectancy filters from the 2026-06-18 trade audit.
    EXPECTANCY_FILTER_ENABLED: bool = os.getenv("EXPECTANCY_FILTER_ENABLED", "true").lower() == "true"
    BLOCK_HIGH_RISK_TRADES: bool = os.getenv("BLOCK_HIGH_RISK_TRADES", "true").lower() == "true"
    HIGH_RISK_SIZE_MULT: float = float(os.getenv("HIGH_RISK_SIZE_MULT", "0.25"))
    SCORE_85_95_MEDIUM_SIZE_MULT: float = float(os.getenv("SCORE_85_95_MEDIUM_SIZE_MULT", "1.0"))
    SCORE_GE_95_SIZE_MULT: float = float(os.getenv("SCORE_GE_95_SIZE_MULT", "0.75"))
    SCORE_LT_75_SIZE_MULT: float = float(os.getenv("SCORE_LT_75_SIZE_MULT", "0.50"))
    SCORE_HIGH_THRESHOLD: float = float(os.getenv("SCORE_HIGH_THRESHOLD", "9.0"))
    SCORE_HIGH_SIZE_MULT: float = float(
        os.getenv("SCORE_HIGH_SIZE_MULT", os.getenv("SCORE_GE_95_SIZE_MULT", "0.75"))
    )
    SHORT_DOWNTREND_SIZE_MULT: float = float(os.getenv("SHORT_DOWNTREND_SIZE_MULT", "1.0"))
    LONG_SIDEWAYS_SIZE_MULT: float = float(os.getenv("LONG_SIDEWAYS_SIZE_MULT", "1.0"))
    LATE_CHASE_SIZE_MULT: float = float(os.getenv("LATE_CHASE_SIZE_MULT", "0.25"))
    MAX_LOSS_USDT_PER_TRADE: float = float(os.getenv("MAX_LOSS_USDT_PER_TRADE", "10.0"))
    # Effective cap is min(absolute cap, this percentage of account equity).
    # Set 0 to disable the percentage cap.
    MAX_RISK_PCT_PER_TRADE: float = float(os.getenv("MAX_RISK_PCT_PER_TRADE", "0.5"))

    # ICT/SMC retest gate:
    # strict = legacy hard gate, soft = reject obvious chase only,
    # off = disable this gate entirely.
    SMC_FILTER_MODE: str = os.getenv("SMC_FILTER_MODE", "soft").lower()
    SMC_SOFT_BYPASS_SCORE: float = float(os.getenv("SMC_SOFT_BYPASS_SCORE", "8.2"))
    SMC_SOFT_BYPASS_CONFIDENCE: float = float(os.getenv("SMC_SOFT_BYPASS_CONFIDENCE", "0.72"))
    # A wrong premium/discount location without sweep/BOS/retest is never
    # bypassed by a high subjective AI score when this guard is enabled.
    SMC_HARD_REJECT_LATE_CHASE: bool = (
        os.getenv("SMC_HARD_REJECT_LATE_CHASE", "false").lower() == "true"
    )

    # ── Regime quality gate (forward-looking, complements circuit breaker) ──
    # Block scan / per-signal entry pas BTC + market keseluruhan clearly choppy.
    # Calibrasi dari incident 24 Mei 2026 (BTC range 0.3% / 5h, vol=weak):
    # gate harus trigger SEBELUM loss streak terjadi, bukan setelah.
    # Liberal defaults — set ke 0 untuk disable.
    REGIME_BTC_ATR_THRESHOLD: float = float(os.getenv("REGIME_BTC_ATR_THRESHOLD", "0.50"))
    REGIME_WEAK_SYMBOL_PCT: float = float(os.getenv("REGIME_WEAK_SYMBOL_PCT", "0.60"))
    REGIME_RSI_NEUTRAL_BAND: float = float(os.getenv("REGIME_RSI_NEUTRAL_BAND", "8.0"))
    REGIME_MARGINAL_SCORE_BUMP: float = float(os.getenv("REGIME_MARGINAL_SCORE_BUMP", "1.0"))
    REGIME_MARGINAL_CONF_BUMP: float = float(os.getenv("REGIME_MARGINAL_CONF_BUMP", "0.1"))

    # ── Anti-overtrading ──────────────────────────────────────
    # Max number of trades (open+closed today) per symbol per day
    MAX_TRADES_PER_SYMBOL_DAY: int = int(os.getenv("MAX_TRADES_PER_SYMBOL_DAY", "3"))
    # Hours to wait before re-entering a symbol after a losing trade
    SYMBOL_LOSS_COOLDOWN_H: float = float(os.getenv("SYMBOL_LOSS_COOLDOWN_H", "4.0"))
    REPEATED_LOSS_STREAK: int = int(os.getenv("REPEATED_LOSS_STREAK", "2"))
    REPEATED_LOSS_COOLDOWN_H: float = float(
        os.getenv("REPEATED_LOSS_COOLDOWN_H", "72.0")
    )

    # ── Daily circuit breaker ─────────────────────────────────
    # Stop opening NEW positions if daily realized loss exceeds this (USDT)
    # 0 = disabled. e.g. 3.0 = halt new entries if today lost > $3
    DAILY_LOSS_LIMIT_USDT: float = float(os.getenv("DAILY_LOSS_LIMIT_USDT", "8.0"))

    # ── SL price cap ─────────────────────────────────────────
    # Max % price move allowed from entry before SL fires (regardless of leverage)
    # Prevents "SL_MODE=roi" from allowing -20% move on 5x positions
    # e.g. 6.0 = SL placed at most 6% from entry price
    SL_MAX_PRICE_MOVE_PCT: float = float(os.getenv("SL_MAX_PRICE_MOVE_PCT", "15.0"))

    # ── AI Learning ───────────────────────────────────────────
    # Legacy memory stored only the final residual leg for multi-TP positions,
    # so it must not drive prompts/sizing until rebuilt from lifecycle totals.
    AI_LEARNING: bool = os.getenv("AI_LEARNING", "false").lower() == "true"
    MEMORY_SYMBOL_SIZING_ENABLED: bool = (
        os.getenv("MEMORY_SYMBOL_SIZING_ENABLED", "false").lower() == "true"
    )

    # ── Storage backend ───────────────────────────────────────
    # "sqlite" : data/trading_bot.db (default — atomic writes, SQL-auditable, shared with MCP)
    # "json"   : pnl_store.json + trade_memory.json (legacy fallback)
    # Migrate first with `python admin.py storage migrate` and confirm
    # `python admin.py storage verify` reports EQUIVALENT before switching.
    # Use `python admin.py storage export` to dump sqlite back to JSON if needed.
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "sqlite").lower()

    # ── Trading Mode ──────────────────────────────────────────
    # "swing"    : 4H primary, Daily HTF, 1H + 15M confirmation
    # "intraday" : 15M primary, 1H HTF, 4H super-HTF, 5M entry timing
    TRADING_MODE: str = os.getenv("TRADING_MODE", "swing").lower()
    STRATEGY_VERSION: str = os.getenv(
        "STRATEGY_VERSION", "lifecycle-cost-v1-20260714"
    )

    # ── Time-based Auto-Close (P1 — prevent intraday → swing drift) ───
    # Force close if intraday position has been held too long with no progress.
    # Stagnant exit: > STAGNANT_HOLD_HOURS AND |ROI| < STAGNANT_ROI_PCT
    # Hard exit  : > MAX_HOLD_HOURS_INTRADAY (regardless of state)
    STAGNANT_HOLD_HOURS: float = float(os.getenv("STAGNANT_HOLD_HOURS", "6.0"))
    STAGNANT_ROI_PCT: float = float(os.getenv("STAGNANT_ROI_PCT", "5.0"))
    MAX_HOLD_HOURS_INTRADAY: float = float(os.getenv("MAX_HOLD_HOURS_INTRADAY", "12.0"))
    MAX_HOLD_HOURS_SWING: float = float(os.getenv("MAX_HOLD_HOURS_SWING", "24.0"))

    # ── Partial Close at TP (P2) ──────────────────────────────
    # Close X% of position when TP1/TP2 hit (instead of just moving SL)
    # TP3 closes the remainder.
    PARTIAL_CLOSE_ENABLED: bool = os.getenv("PARTIAL_CLOSE_ENABLED", "true").lower() == "true"
    TP1_CLOSE_PCT: float = float(os.getenv("TP1_CLOSE_PCT", "15.0"))
    # TP2 is a percentage of the remaining position.  15% then 24% produces
    # an initial allocation of about 15% / 20.4% / 64.6% to TP1/TP2/runner.
    TP2_CLOSE_PCT: float = float(os.getenv("TP2_CLOSE_PCT", "24.0"))
    # The tracker owns TP/partial execution.  Placing another set of exchange
    # conditional TPs creates a double-close race, so it is opt-in only.
    EXCHANGE_TP_ORDERS_ENABLED: bool = (
        os.getenv("EXCHANGE_TP_ORDERS_ENABLED", "false").lower() == "true"
    )
    # TP3 closes whatever remains (about 64.6% with the defaults).

    # Minimum % price move from entry required to actually do a partial close.
    # Bybit taker fee = 0.055% × 2 sides = 0.11% breakeven. If TP is set tighter
    # than this, partial close becomes net negative after fees. Default 0.2% =
    # break fees + small net profit margin.
    PARTIAL_CLOSE_MIN_MOVE_PCT: float = float(os.getenv("PARTIAL_CLOSE_MIN_MOVE_PCT", "0.20"))

    # Audit 2026-07-26: partial TP1 hanya dieksekusi kalau jarak TP1 membayar
    # minimal N × risiko awal (entry→SL). Juli: 126 partial TP1 di ~0.3-0.5R
    # cuma rata-rata +$0.55/leg — generator fee, bukan asuransi (BE-lock sudah
    # meng-cover downside). 0 = nonaktifkan gate (perilaku lama).
    TP1_PARTIAL_MIN_R: float = float(os.getenv("TP1_PARTIAL_MIN_R", "0.5"))

    # After TP1 hit, SL moves to a profit lock. ROI mode is leverage-aware:
    # The ROI lock is leverage-aware. Legacy price-percent lock
    # remains available as a fallback when BE_PROFIT_LOCK_ROI_PCT is disabled.
    BE_PROFIT_LOCK_ROI_PCT: float = float(os.getenv("BE_PROFIT_LOCK_ROI_PCT", "10.0"))
    BE_PROFIT_LOCK_PCT: float = float(os.getenv("BE_PROFIT_LOCK_PCT", "0.30"))
    TP2_TRAIL_TIGHTEN_MULT: float = float(os.getenv("TP2_TRAIL_TIGHTEN_MULT", "0.75"))

    # ── Trailing Take-Profit ──────────────────────────────────
    # Dulu TP3 = titik exit statis: runner (≈64.6% posisi dengan default
    # TP1/TP2) SELALU dipotong di harga yang ditebak AI berjam-jam sebelumnya,
    # walau tren masih jalan. Sekarang TP3 = titik ARMING trailing TP:
    # target ikut bergeser naik mengikuti peak, exit terjadi saat harga
    # memberi balik (giveback) sekian ATR dari peak.
    #
    # false = perilaku lama (TP3 full close di harga tetap).
    TRAILING_TP_ENABLED: bool = os.getenv("TRAILING_TP_ENABLED", "true").lower() == "true"
    # Giveback dari peak = N × ATR%. Lebih ketat dari trail pra-TP3 karena
    # di titik ini profit sudah besar dan yang dilindungi adalah runner.
    TRAILING_TP_GIVEBACK_ATR_MULT: float = float(os.getenv("TRAILING_TP_GIVEBACK_ATR_MULT", "1.5"))
    TRAILING_TP_MIN_GIVEBACK_PCT: float = float(os.getenv("TRAILING_TP_MIN_GIVEBACK_PCT", "0.60"))
    TRAILING_TP_MAX_GIVEBACK_PCT: float = float(os.getenv("TRAILING_TP_MAX_GIVEBACK_PCT", "5.0"))
    # Target berikutnya diletakkan N × ATR% di atas peak (LONG) / di bawah
    # (SHORT). Murni informasional + basis cap ekstensi — bukan limit order.
    TRAILING_TP_STEP_ATR_MULT: float = float(os.getenv("TRAILING_TP_STEP_ATR_MULT", "1.0"))
    # Batas waras: berhenti menggeser dan ambil untung saat harga sudah
    # melewati TP3 sejauh N × risiko awal (entry→original_sl). 0 = tanpa batas.
    TRAILING_TP_MAX_EXTENSION_R: float = float(os.getenv("TRAILING_TP_MAX_EXTENSION_R", "4.0"))
    # Runner yang mandek tidak tertangkap time-exit (time-exit hanya untuk
    # posisi RUGI). Tanpa guard ini, posisi profit bisa menggantung selamanya.
    # Jam tanpa peak baru sejak arming → close di market. 0 = nonaktif.
    TRAILING_TP_STALL_HOURS: float = float(os.getenv("TRAILING_TP_STALL_HOURS", "8.0"))

    # ── Quick-Exit on Reversal (P3) ────────────────────────────
    # Force close if technical signals reverse before SL/TP hit.
    # Triggers: structure flip + RSI extreme + momentum reverse
    QUICK_EXIT_ENABLED: bool = os.getenv("QUICK_EXIT_ENABLED", "true").lower() == "true"
    # RSI threshold: LONG exits if rsi < this; SHORT exits if rsi > (100-this)
    QUICK_EXIT_RSI_LONG: float = float(os.getenv("QUICK_EXIT_RSI_LONG", "35.0"))
    QUICK_EXIT_RSI_SHORT: float = float(os.getenv("QUICK_EXIT_RSI_SHORT", "65.0"))
    # Only trigger if position is at least N hours old (avoid premature exits)
    QUICK_EXIT_MIN_HOURS: float = float(os.getenv("QUICK_EXIT_MIN_HOURS", "1.0"))

    # Fast failed-setup exit:
    # If a new position quickly moves against entry and the market structure no
    # longer supports the trade direction, close before waiting for full SL.
    FAILED_SETUP_EXIT_ENABLED: bool = os.getenv("FAILED_SETUP_EXIT_ENABLED", "false").lower() == "true"
    FAILED_SETUP_MIN_MINUTES: float = float(os.getenv("FAILED_SETUP_MIN_MINUTES", "5.0"))
    FAILED_SETUP_MAX_MINUTES: float = float(os.getenv("FAILED_SETUP_MAX_MINUTES", "30.0"))
    FAILED_SETUP_MAX_ROI_LOSS_PCT: float = float(os.getenv("FAILED_SETUP_MAX_ROI_LOSS_PCT", "-10.0"))

    # ── Per-Mode Auto-Tuning ─────────────────────────────────
    # Intraday mode needs different defaults than swing for many shared
    # parameters. Instead of requiring manual .env edits on every mode
    # switch, we auto-adjust here.
    #
    # Priority: INTRADAY_<PARAM> env var > intraday default > swing value
    # User can set e.g. INTRADAY_SCAN_INTERVAL_SECONDS=180 to customize.
    #
    # Swing defaults are the class-level defaults above — no override needed.
    # fmt: off
    # ┌─────────────────────────────┬─────────┬─────────┬──────────────────────────────────┐
    # │ Parameter                   │ Swing   │Intraday │ Why                              │
    # ├─────────────────────────────┼─────────┼─────────┼──────────────────────────────────┤
    # │ SCAN_INTERVAL               │ 900s    │ 300s    │ 15M candle, need faster scan     │
    # │ MAX_OPEN_POSITIONS          │ 4       │ 3       │ Less concurrent, small account   │
    # │ SYMBOL_LOSS_COOLDOWN_H      │ 4.0h    │ 1.5h   │ Setups change fast in intraday   │
    # │ MAX_TRADES_PER_SYMBOL_DAY   │ 3       │ 5       │ More opportunities intraday      │
    # │ BE_PROFIT_LOCK_PCT          │ 0.50%   │ 0.15%  │ TP1=0.3%, lock must be < TP1     │
    # │ DCA_ROI_TRIGGER             │ -75%    │ -40%   │ Intraday SL cap = -50% ROI       │
    # │ SL_MAX_PRICE_MOVE_PCT       │ 15%     │ 5%     │ Tighter for faster timeframe     │
    # │ DAILY_LOSS_LIMIT_USDT       │ 8.0     │ 5.0    │ Tighter daily limit intraday     │
    # │ STAGNANT_HOLD_HOURS         │ 6.0h    │ 3.0h   │ Faster stagnant detection        │
    # │ TRAILING_TP_GIVEBACK_ATR    │ 1.5×    │ 1.2×   │ 15M noise kecil, giveback ketat  │
    # │ TRAILING_TP_STALL_HOURS     │ 8.0h    │ 3.0h   │ Runner intraday tak boleh nganggur│
    # └─────────────────────────────┴─────────┴─────────┴──────────────────────────────────┘
    # fmt: on
    if TRADING_MODE == "intraday":
        SCAN_INTERVAL           = int(os.getenv("INTRADAY_SCAN_INTERVAL_SECONDS", "300"))
        MAX_OPEN_POSITIONS      = int(os.getenv("INTRADAY_MAX_OPEN_POSITIONS", "3"))
        SYMBOL_LOSS_COOLDOWN_H  = float(os.getenv("INTRADAY_SYMBOL_LOSS_COOLDOWN_H", "1.5"))
        MAX_TRADES_PER_SYMBOL_DAY = int(os.getenv("INTRADAY_MAX_TRADES_PER_SYMBOL_DAY", "5"))
        BE_PROFIT_LOCK_ROI_PCT  = float(os.getenv("INTRADAY_BE_PROFIT_LOCK_ROI_PCT", "5.0"))
        BE_PROFIT_LOCK_PCT      = float(os.getenv("INTRADAY_BE_PROFIT_LOCK_PCT", "0.15"))
        DCA_ROI_TRIGGER         = float(os.getenv("INTRADAY_DCA_ROI_TRIGGER", "-40.0"))
        SL_MAX_PRICE_MOVE_PCT   = float(os.getenv("INTRADAY_SL_MAX_PRICE_MOVE_PCT", "5.0"))
        DAILY_LOSS_LIMIT_USDT   = float(os.getenv("INTRADAY_DAILY_LOSS_LIMIT_USDT", "5.0"))
        STAGNANT_HOLD_HOURS     = float(os.getenv("INTRADAY_STAGNANT_HOLD_HOURS", "3.0"))
        REGIME_BTC_ATR_THRESHOLD = float(os.getenv("INTRADAY_REGIME_BTC_ATR_THRESHOLD", "0.40"))
        TRAILING_TP_GIVEBACK_ATR_MULT = float(os.getenv("INTRADAY_TRAILING_TP_GIVEBACK_ATR_MULT", "1.2"))
        TRAILING_TP_MIN_GIVEBACK_PCT  = float(os.getenv("INTRADAY_TRAILING_TP_MIN_GIVEBACK_PCT", "0.35"))
        TRAILING_TP_MAX_GIVEBACK_PCT  = float(os.getenv("INTRADAY_TRAILING_TP_MAX_GIVEBACK_PCT", "3.0"))
        TRAILING_TP_STALL_HOURS       = float(os.getenv("INTRADAY_TRAILING_TP_STALL_HOURS", "3.0"))

    # ── Helper: resolve provider config ───────────────────────

    @classmethod
    def get_ai_config(cls, provider: str = None) -> dict:
        """Return {api_key, model, base_url, provider} for the given provider
        (defaults to AI_PROVIDER). Used by SignalEngine for both tier-1 and tier-2.

        When called with no `provider` arg (tier-1 path), AI_TIER1_* overrides
        are applied on top of the provider's default slot — letting .env stay
        clean with just tier blocks instead of per-provider slots.
        """
        is_primary = provider is None
        p = (provider or cls.AI_PROVIDER).lower()

        if p == "anthropic":
            cfg = {
                "provider": "anthropic",
                "api_key": cls.ANTHROPIC_API_KEY,
                "model": cls.CLAUDE_MODEL,
                "base_url": "",
            }
        elif p == "blink":
            cfg = {
                "provider": "blink",
                "api_key": cls.BLINK_API_KEY,
                "model": cls.BLINK_MODEL,
                "base_url": cls.BLINK_BASE_URL,
            }
        elif p == "openai":
            cfg = {
                "provider": "openai",
                "api_key": cls.OPENAI_API_KEY,
                "model": cls.OPENAI_MODEL or _PROVIDER_PRESETS["openai"]["default_model"],
                "base_url": cls.OPENAI_BASE_URL or _PROVIDER_PRESETS["openai"]["base_url"],
            }
        elif p == "gemini":
            cfg = {
                "provider": "gemini",
                "api_key": cls.GEMINI_API_KEY,
                "model": cls.GEMINI_MODEL,
                "base_url": cls.GEMINI_BASE_URL,
            }
        elif p == "glm":
            cfg = {
                "provider": "glm",
                "api_key": cls.GLM_API_KEY,
                "model": cls.GLM_MODEL,
                "base_url": cls.GLM_BASE_URL,
            }
        elif p == "minimax":
            cfg = {
                "provider": "minimax",
                "api_key": cls.MINIMAX_API_KEY,
                "model": cls.MINIMAX_MODEL,
                "base_url": cls.MINIMAX_BASE_URL,
            }
        elif p == "custom":
            cfg = {
                "provider": "custom",
                "api_key": cls.CUSTOM_API_KEY,
                "model": cls.CUSTOM_MODEL,
                "base_url": cls.CUSTOM_BASE_URL,
            }
        elif p == "qwen":
            cfg = {
                "provider": "qwen",
                "api_key": cls.QWEN_API_KEY,
                "model": cls.QWEN_MODEL,
                "base_url": cls.QWEN_BASE_URL,
            }
        elif p == "openrouter":
            cfg = {
                "provider": "openrouter",
                "api_key": cls.OPENROUTER_API_KEY,
                "model": cls.OPENROUTER_MODEL,
                "base_url": cls.OPENROUTER_BASE_URL,
            }
        elif p == "deepseek":
            cfg = {
                "provider": "deepseek",
                "api_key": cls.DEEPSEEK_API_KEY,
                "model": cls.DEEPSEEK_MODEL,
                "base_url": cls.DEEPSEEK_BASE_URL,
            }
        else:
            raise ValueError(f"Unknown AI_PROVIDER: {p}")

        # ── Apply tier-1 overrides (only on primary path) ──
        if is_primary:
            if cls.AI_TIER1_MODEL:
                cfg["model"] = cls.AI_TIER1_MODEL
            if cls.AI_TIER1_API_KEY:
                cfg["api_key"] = cls.AI_TIER1_API_KEY
            if cls.AI_TIER1_BASE_URL:
                cfg["base_url"] = cls.AI_TIER1_BASE_URL

        return cfg

    @classmethod
    def get_tier_chain(cls) -> List[dict]:
        """
        Build the ordered fallback chain AFTER tier-1.
        Returns a list of {provider, model, api_key, base_url} dicts.
        Empty list = no fallback. Each tier may apply per-tier overrides.
        """
        chain: List[dict] = []

        # ── TIER 2 ────────────────────────────────────────────
        if cls.AI_PROVIDER_FALLBACK:
            try:
                t2 = cls.get_ai_config(cls.AI_PROVIDER_FALLBACK)
                # Per-tier overrides (model/key/url) — useful when reusing
                # the same provider with a different model
                if cls.AI_TIER2_MODEL:
                    t2["model"] = cls.AI_TIER2_MODEL
                if cls.AI_TIER2_API_KEY:
                    t2["api_key"] = cls.AI_TIER2_API_KEY
                if cls.AI_TIER2_BASE_URL:
                    t2["base_url"] = cls.AI_TIER2_BASE_URL
                if t2.get("api_key"):
                    chain.append(t2)
            except Exception:
                pass

        # ── TIER 3 ────────────────────────────────────────────
        if cls.AI_PROVIDER_TIER3:
            try:
                t3 = cls.get_ai_config(cls.AI_PROVIDER_TIER3)
                if cls.AI_TIER3_MODEL:
                    t3["model"] = cls.AI_TIER3_MODEL
                if cls.AI_TIER3_API_KEY:
                    t3["api_key"] = cls.AI_TIER3_API_KEY
                if cls.AI_TIER3_BASE_URL:
                    t3["base_url"] = cls.AI_TIER3_BASE_URL
                if t3.get("api_key"):
                    chain.append(t3)
            except Exception:
                pass

        return chain

    @classmethod
    def validate(cls) -> None:
        errors: List[str] = []
        valid_providers = {"anthropic", "blink", "openai", "gemini", "glm", "minimax", "custom", "qwen", "openrouter", "deepseek"}

        if cls.AI_PROVIDER not in valid_providers:
            errors.append(
                f"Unknown AI_PROVIDER '{cls.AI_PROVIDER}' "
                f"(use: {' | '.join(sorted(valid_providers))})"
            )
        else:
            try:
                cfg = cls.get_ai_config()
                if not cfg["api_key"]:
                    key_name = {
                        "anthropic": "ANTHROPIC_API_KEY",
                        "blink": "BLINK_API_KEY",
                        "openai": "OPENAI_API_KEY",
                        "gemini": "GEMINI_API_KEY",
                        "glm": "GLM_API_KEY",
                        "minimax": "MINIMAX_API_KEY",
                        "custom": "CUSTOM_API_KEY",
                        "qwen": "QWEN_API_KEY",
                        "openrouter": "OPENROUTER_API_KEY",
                        "deepseek": "DEEPSEEK_API_KEY",
                    }.get(cls.AI_PROVIDER, "API_KEY")
                    errors.append(f"{key_name} is required for provider={cls.AI_PROVIDER}")
                if not cfg["model"]:
                    errors.append(f"Model is required for provider={cls.AI_PROVIDER}")
                if cls.AI_PROVIDER == "custom" and not cfg["base_url"]:
                    errors.append("CUSTOM_BASE_URL is required for provider=custom")
            except ValueError as e:
                errors.append(str(e))

        # Validate fallback provider (optional). Same provider is allowed
        # as long as a per-tier model override (AI_TIER2_MODEL) is set.
        if cls.AI_PROVIDER_FALLBACK:
            if cls.AI_PROVIDER_FALLBACK not in valid_providers:
                errors.append(
                    f"Unknown AI_PROVIDER_FALLBACK '{cls.AI_PROVIDER_FALLBACK}' "
                    f"(use: {' | '.join(sorted(valid_providers))})"
                )
            else:
                try:
                    fb_cfg = cls.get_ai_config(cls.AI_PROVIDER_FALLBACK)
                    fb_model = cls.AI_TIER2_MODEL or fb_cfg["model"]
                    fb_key = cls.AI_TIER2_API_KEY or fb_cfg["api_key"]
                    if not fb_key:
                        errors.append(
                            f"API key missing for fallback provider "
                            f"'{cls.AI_PROVIDER_FALLBACK}'"
                        )
                    # Reject only if same provider AND same model (no actual override)
                    if (
                        cls.AI_PROVIDER_FALLBACK == cls.AI_PROVIDER
                        and fb_model == cls.get_ai_config()["model"]
                    ):
                        errors.append(
                            "Tier-2 must differ from Tier-1: same provider AND same model. "
                            "Set AI_TIER2_MODEL to override the model."
                        )
                except Exception as e:
                    errors.append(f"Fallback provider error: {e}")

        # Validate Tier-3 (optional, similar rules)
        if cls.AI_PROVIDER_TIER3:
            if cls.AI_PROVIDER_TIER3 not in valid_providers:
                errors.append(
                    f"Unknown AI_PROVIDER_TIER3 '{cls.AI_PROVIDER_TIER3}' "
                    f"(use: {' | '.join(sorted(valid_providers))})"
                )
            else:
                try:
                    t3_cfg = cls.get_ai_config(cls.AI_PROVIDER_TIER3)
                    t3_key = cls.AI_TIER3_API_KEY or t3_cfg["api_key"]
                    if not t3_key:
                        errors.append(
                            f"API key missing for tier-3 provider "
                            f"'{cls.AI_PROVIDER_TIER3}'"
                        )
                except Exception as e:
                    errors.append(f"Tier-3 provider error: {e}")

        if not cls.DRY_RUN:
            if cls.EXCHANGE_MODE == "CEX" and cls.CEX_PROVIDER == "bybit":
                if not cls.BYBIT_API_KEY:
                    errors.append("BYBIT_API_KEY required for live CEX trading")
                if not cls.BYBIT_SECRET:
                    errors.append("BYBIT_SECRET required for live CEX trading")
            elif cls.EXCHANGE_MODE == "CEX" and cls.CEX_PROVIDER == "okx":
                # Trio yang sama dengan feed Web3 — satu API key OKX melayani
                # REST v5 trading dan data. Passphrase WAJIB (beda dari Bybit).
                if not cls.OKX_API_KEY:
                    errors.append("OKX_API_KEY required for live OKX trading")
                if not cls.OKX_SECRET_KEY:
                    errors.append("OKX_SECRET_KEY required for live OKX trading")
                if not cls.OKX_PASSPHRASE:
                    errors.append("OKX_PASSPHRASE required for live OKX trading")
            elif cls.EXCHANGE_MODE == "DEX":
                if not cls.HL_PRIVATE_KEY:
                    errors.append("HL_PRIVATE_KEY required for live DEX trading")
                if not cls.HL_WALLET_ADDRESS:
                    errors.append("HL_WALLET_ADDRESS required for live DEX trading")

        if cls.EXCHANGE_MODE not in {"CEX", "DEX"}:
            errors.append("EXCHANGE_MODE must be CEX or DEX")
        # Fail loudly: a typo here would silently fall through to Bybit and
        # trade on the wrong exchange.
        if cls.CEX_PROVIDER not in {"bybit", "okx"}:
            errors.append("CEX_PROVIDER must be bybit or okx")
        if cls.EXCHANGE_MODE == "CEX" and cls.CEX_PROVIDER == "okx":
            if cls.OKX_MARGIN_MODE not in {"isolated", "cross"}:
                errors.append("OKX_MARGIN_MODE must be isolated or cross")
        if cls.TRADING_MODE not in {"swing", "intraday"}:
            errors.append("TRADING_MODE must be swing or intraday")
        if not cls.STRATEGY_VERSION.strip():
            errors.append("STRATEGY_VERSION cannot be empty")
        if cls.STORAGE_BACKEND not in {"sqlite", "json"}:
            errors.append("STORAGE_BACKEND must be sqlite or json")
        if cls.SL_MODE not in {"atr", "roi", "none"}:
            errors.append("SL_MODE must be atr, roi, or none")
        if not cls.DRY_RUN and cls.SL_MODE == "none":
            errors.append("SL_MODE=none is not allowed for live trading")
        if cls.SMC_FILTER_MODE not in {"strict", "soft", "off"}:
            errors.append("SMC_FILTER_MODE must be strict, soft, or off")

        if cls.SCAN_INTERVAL <= 0:
            errors.append("SCAN_INTERVAL_SECONDS must be > 0")
        if cls.MAX_OPEN_POSITIONS <= 0:
            errors.append("MAX_OPEN_POSITIONS must be > 0")
        if cls.MAX_TRADES_PER_SYMBOL_DAY <= 0:
            errors.append("MAX_TRADES_PER_SYMBOL_DAY must be > 0")
        if not (0 < cls.MIN_POSITION_SIZE_PCT <= cls.MAX_POSITION_SIZE_PCT <= 100):
            errors.append(
                "position size limits must satisfy 0 < MIN <= MAX <= 100"
            )
        if not (0 <= cls.TP1_CLOSE_PCT <= 100):
            errors.append("TP1_CLOSE_PCT must be between 0 and 100")
        if not (0 <= cls.TP2_CLOSE_PCT <= 100):
            errors.append("TP2_CLOSE_PCT must be between 0 and 100")
        if cls.TRAILING_TP_ENABLED:
            if cls.TRAILING_TP_GIVEBACK_ATR_MULT <= 0:
                errors.append("TRAILING_TP_GIVEBACK_ATR_MULT must be > 0")
            if cls.TRAILING_TP_STEP_ATR_MULT <= 0:
                errors.append("TRAILING_TP_STEP_ATR_MULT must be > 0")
            if not (0 < cls.TRAILING_TP_MIN_GIVEBACK_PCT <= cls.TRAILING_TP_MAX_GIVEBACK_PCT):
                errors.append(
                    "trailing TP giveback bounds must satisfy "
                    "0 < TRAILING_TP_MIN_GIVEBACK_PCT <= TRAILING_TP_MAX_GIVEBACK_PCT"
                )
            if cls.TRAILING_TP_MAX_EXTENSION_R < 0:
                errors.append("TRAILING_TP_MAX_EXTENSION_R cannot be negative")
            if cls.TRAILING_TP_STALL_HOURS < 0:
                errors.append("TRAILING_TP_STALL_HOURS cannot be negative")

        if not (0 <= cls.TAKER_FEE_PCT < 1):
            errors.append("TAKER_FEE_PCT must be between 0 and 1")
        if not (0 <= cls.DRY_RUN_SLIPPAGE_PCT < 1):
            errors.append("DRY_RUN_SLIPPAGE_PCT must be between 0 and 1")
        if not (0 <= cls.MAX_RISK_PCT_PER_TRADE <= 100):
            errors.append("MAX_RISK_PCT_PER_TRADE must be between 0 and 100")
        if cls.MAX_LOSS_USDT_PER_TRADE < 0:
            errors.append("MAX_LOSS_USDT_PER_TRADE cannot be negative")
        if not (0 <= cls.LATE_CHASE_SIZE_MULT <= 1):
            errors.append("LATE_CHASE_SIZE_MULT must be between 0 and 1")
        if not (0 <= cls.SCORE_HIGH_SIZE_MULT <= 1):
            errors.append("SCORE_HIGH_SIZE_MULT must be between 0 and 1")
        if cls.REPEATED_LOSS_STREAK < 1:
            errors.append("REPEATED_LOSS_STREAK must be >= 1")
        if cls.REPEATED_LOSS_COOLDOWN_H < 0:
            errors.append("REPEATED_LOSS_COOLDOWN_H cannot be negative")

        leverage_caps = (
            cls.LEVERAGE_CAP_DEFENSIVE,
            cls.LEVERAGE_CAP_CONSERVATIVE,
            cls.LEVERAGE_CAP_NORMAL,
            cls.LEVERAGE_CAP_AGGRESSIVE,
            cls.LEVERAGE_CAP_HIGH_RISK,
        )
        if cls.LEVERAGE_FLOOR < 1 or any(cap < cls.LEVERAGE_FLOOR for cap in leverage_caps):
            errors.append("leverage floor must be >= 1 and not exceed any leverage cap")
        if any(cap > 10 for cap in leverage_caps):
            errors.append("all leverage caps must be <= 10")
        if cls.EXCHANGE_TP_ORDERS_ENABLED:
            errors.append(
                "EXCHANGE_TP_ORDERS_ENABLED is unsafe until exchange fill reconciliation "
                "can update local partial state; keep the local tracker as TP owner"
            )

        if cls.ACCOUNT_BALANCE <= 0:
            errors.append("ACCOUNT_BALANCE must be > 0")

        if not cls.SYMBOLS:
            errors.append("SYMBOLS list cannot be empty")

        if errors:
            raise ValueError("Config validation failed:\n" + "\n".join(f"  x {e}" for e in errors))

    @classmethod
    def strategy_fingerprint(cls) -> str:
        """Stable, non-secret hash of parameters that change trade outcomes."""
        import hashlib
        import json

        payload = {
            "strategy_version": cls.STRATEGY_VERSION,
            "trading_mode": cls.TRADING_MODE,
            "sl_mode": cls.SL_MODE,
            "dca_enabled": cls.DCA_ENABLED,
            "fee_pct": cls.TAKER_FEE_PCT,
            "slippage_pct": cls.DRY_RUN_SLIPPAGE_PCT,
            "min_rr": cls.MIN_RR,
            "score_floor": cls.MIN_SCORE_HARD,
            "confidence_floor": cls.MIN_CONFIDENCE_HARD,
            "leverage_caps": [
                cls.LEVERAGE_CAP_DEFENSIVE,
                cls.LEVERAGE_CAP_CONSERVATIVE,
                cls.LEVERAGE_CAP_NORMAL,
                cls.LEVERAGE_CAP_AGGRESSIVE,
            ],
            "max_loss_usdt": cls.MAX_LOSS_USDT_PER_TRADE,
            "max_risk_pct": cls.MAX_RISK_PCT_PER_TRADE,
            "reject_falling_volume": cls.REJECT_FALLING_VOLUME_ENTRIES,
            "late_chase_mult": cls.LATE_CHASE_SIZE_MULT,
            "score_high_threshold": cls.SCORE_HIGH_THRESHOLD,
            "score_high_mult": cls.SCORE_HIGH_SIZE_MULT,
            "tp1_pct": cls.TP1_CLOSE_PCT,
            "tp2_remaining_pct": cls.TP2_CLOSE_PCT,
            "trailing_tp": cls.TRAILING_TP_ENABLED,
            "trailing_tp_giveback_atr": cls.TRAILING_TP_GIVEBACK_ATR_MULT,
            "trailing_tp_max_ext_r": cls.TRAILING_TP_MAX_EXTENSION_R,
            "repeated_loss_streak": cls.REPEATED_LOSS_STREAK,
            "repeated_loss_cooldown_h": cls.REPEATED_LOSS_COOLDOWN_H,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def summary(cls) -> str:
        try:
            cfg = cls.get_ai_config()
            model_info = cfg["model"]
            if cfg["base_url"]:
                model_info += f" (via {cfg['base_url']})"
        except Exception:
            model_info = "unknown"

        lines = [
            f"  Exchange Mode : {cls.EXCHANGE_MODE}",
            f"  Trading Mode  : {cls.TRADING_MODE.upper()}",
            f"  Dry Run       : {cls.DRY_RUN}",
            f"  Symbols       : {cls.SYMBOLS}",
            f"  Balance       : {cls.ACCOUNT_BALANCE} USDT",
            f"  Scan Interval : {cls.SCAN_INTERVAL}s",
            f"  Max Positions : {cls.MAX_OPEN_POSITIONS}",
            f"  Position Size : {cls.MIN_POSITION_SIZE_PCT}-{cls.MAX_POSITION_SIZE_PCT}% of balance",
            f"  DCA           : {'ON' if cls.DCA_ENABLED else 'OFF'} ({cls.DCA_SIZE_PCT}% per DCA)",
            f"  AI Provider   : {cls.AI_PROVIDER}"
            + (f"  -> t2: {cls.AI_PROVIDER_FALLBACK}" if cls.AI_PROVIDER_FALLBACK else "")
            + (f"  -> t3: {cls.AI_PROVIDER_TIER3}" if cls.AI_PROVIDER_TIER3 else ""),
            f"  AI Model      : {model_info}",
            f"  AI Learning   : {cls.AI_LEARNING}",
        ]
        if cls.EXCHANGE_MODE == "CEX":
            lines.append(f"  CEX Provider  : {cls.CEX_PROVIDER}")
            if cls.CEX_PROVIDER == "okx":
                lines.append(f"  OKX Demo      : {cls.OKX_DEMO}")
                lines.append(f"  OKX Margin    : {cls.OKX_MARGIN_MODE}")
            else:
                lines.append(f"  Testnet       : {cls.BYBIT_TESTNET}")
        elif cls.EXCHANGE_MODE == "DEX":
            lines.append(f"  HL Testnet    : {cls.HL_TESTNET}")
        return "\n".join(lines)
