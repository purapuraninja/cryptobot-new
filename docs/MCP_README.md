# MCP Server Setup Guide

## Overview

MCP (Model Context Protocol) server memungkinkan AI agents (Cursor, OpenAI Codex CLI) untuk mengontrol dan memantau trading bot.

## 4 MCP Server Terintegrasi

| MCP Server | Fungsi | Tools |
|------------|--------|-------|
| **Crypto Exchange** | Akses Bybit, Binance | `get_ticker`, `get_ohlcv`, `get_positions`, `get_balance`, `get_funding_rate`, `get_orderbook` |
| **Database (SQLite)** | Trade history & analytics | `get_trade_history`, `get_performance_stats`, `get_signal_history`, `get_whale_history` |
| **Bot Control** | Monitoring & config | `get_bot_config`, `get_bot_health`, `read_pnl_store`, `read_trade_memory` |
| **Analytics** | Performance metrics | Via database tools + resources |

## Resources yang Tersedia

| Resource | URI | Deskripsi |
|----------|-----|-----------|
| PnL History | `pnl://history` | Full PnL history dari pnl_store.json |
| Trade Memory | `memory://trades` | Trade memory dan learning data |
| Current Config | `config://current` | Konfigurasi bot saat ini |
| Trade Database | `database://trades` | Trade history dari SQLite |
| Performance Stats | `database://performance` | Statistik performa |

---

## Installation

### 1. Install Dependencies

```bash
cd "C:/Users/kejar/Trading Bot - switch"
pip install -r requirements.txt
```

### 2. Test MCP Server

```bash
# Test stdio mode
python mcp_server.py
```

---

## Configuration untuk Cursor IDE

### Method 1: Cursor Settings UI

1. Buka **Cursor Settings** (Ctrl+,)
2. Cari **"MCP"** di search bar
3. Klik **"Add MCP Server"**
4. Isi:
   - **Name**: `trading-bot`
   - **Type**: `stdio`
   - **Command**: `python mcp_server.py`
   - **Working Directory**: `C:/Users/kejar/Trading Bot - switch`

### Method 2: Manual Config File

1. Buka Cursor Settings
2. Klik **"Open Settings JSON"** (ikon gear di pojok kanan atas)
3. Tambahkan konfigurasi berikut:

```json
{
  "mcpServers": {
    "trading-bot": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "C:/Users/kejar/Trading Bot - switch",
      "env": {
        "PYTHONPATH": "C:/Users/kejar/Trading Bot - switch"
      }
    }
  }
}
```

4. Restart Cursor

---

## Configuration untuk OpenAI Codex CLI

### Method 1: Environment Variable

```bash
# Set MCP config path
export OPENAI_MCP_CONFIG="C:/Users/kejar/Trading Bot - switch/mcp_config.json"

# Run Codex CLI
codex
```

### Method 2: Project Config

1. Buat file `.codex/mcp.json` di root project:

```json
{
  "mcpServers": {
    "trading-bot": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "C:/Users/kejar/Trading Bot - switch"
    }
  }
}
```

2. Jalankan Codex CLI dari project directory:

```bash
cd "C:/Users/kejar/Trading Bot - switch"
codex
```

---

## Usage Examples

### Dari Cursor IDE

```
User: "Apa posisi terbuka di Bybit?"
Cursor: [memanggil get_exchange_positions(exchange="bybit")]
Cursor: "Ada 2 posisi terbuka: LONG ETHUSDT (+5.2%), SHORT SOLUSDT (-2.1%)"
```

```
User: "Berapa win rate bot bulan ini?"
Cursor: [memanggil get_performance_stats(days=30)]
Cursor: "Win rate bulan ini: 48.5% dengan total 62 trades"
```

```
User: "Tunjukkan data whale untuk BTCUSDT"
Cursor: [memanggil get_whale_history(symbol="BTCUSDT", hours=24)]
Cursor: "Data whale 24h terakhir: OI +2.3%, Taker Buy 58%, Whale Signal: accumulation"
```

### Dari OpenAI Codex CLI

```bash
codex> "Apa konfigurasi bot sekarang?"
# Codex akan memanggil get_bot_config()

codex> "Show me recent trades"
# Codex akan memanggil get_trade_history(days=7)

codex> "Get BTC/USDT ticker from Bybit"
# Codex akan memanggil get_ticker(exchange="bybit", symbol="BTC/USDT:USDT")
```

---

## Troubleshooting

### "mcp package not installed"

```bash
pip install mcp
```

### "Module not found: config"

Pastikan working directory benar:
```bash
cd "C:/Users/kejar/Trading Bot - switch"
python mcp_server.py
```

### "Exchange not configured"

Pastikan API keys ada di `.env`:
```env
BYBIT_API_KEY=your_key
BYBIT_SECRET=your_secret
```

### Cursor tidak mendeteksi MCP server

1. Pastikan format JSON benar
2. Restart Cursor
3. Cek Cursor logs: `Help > Toggle Developer Tools > Console`

### OpenAI Codex CLI tidak mendeteksi MCP server

1. Pastikan file `mcp_config.json` ada
2. Set environment variable: `OPENAI_MCP_CONFIG`
3. Atau buat `.codex/mcp.json` di project directory

---

## Security Notes

- MCP server hanya bisa diakses secara lokal (localhost)
- Tidak ada autentikasi (untuk local use only)
- Untuk remote access, gunakan SSH tunnel atau VPN
- API keys tidak di-log atau di-expose melalui MCP

---

## File Structure

```
Trading Bot - switch/
├── mcp_server.py          # MCP server utama
├── mcp_config.json        # Config untuk Cursor/Codex
├── MCP_README.md          # File ini
├── data/
│   └── trading_bot.db     # SQLite database (auto-created)
├── config.py              # Bot configuration
├── requirements.txt       # Dependencies (termasuk mcp)
└── .env                   # API keys (jangan commit)
```
