from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / "runtime"
OPENCLAW_RUNTIME_DIR = RUNTIME_DIR / "openclaw"
OPENCLAW_INBOX_PATH = OPENCLAW_RUNTIME_DIR / "signal_inbox.jsonl"
OPENCLAW_BATCH_PATH = OPENCLAW_RUNTIME_DIR / "signal_batch.json"
OPENCLAW_SOURCE_POSTS_PATH = OPENCLAW_RUNTIME_DIR / "discord_posts.json"
OPENCLAW_MONITOR_STATE_PATH = OPENCLAW_RUNTIME_DIR / "discord_monitor_state.json"
TRADE_DB_PATH = RUNTIME_DIR / "mirrorbot.db"
TRADE_SETTINGS_PATH = APP_DIR / "trade_settings.json"

SUPPORTED_SYMBOLS = (
    "USDJPYm",
    "EURUSDm",
    "JP225m",
    "USOILm",
    "XAUUSDm",
    "XAGUSDm",
    "BTCUSDm",
    "ETHUSDm",
)

SYMBOL_ALIASES = {
    "USDJPYM": "USDJPYm",
    "USDJPY": "USDJPYm",
    "ドル円": "USDJPYm",
    "ユードル": "EURUSDm",
    "EURUSDM": "EURUSDm",
    "EURUSD": "EURUSDm",
    "ユーロドル": "EURUSDm",
    "JP225M": "JP225m",
    "JP225": "JP225m",
    "NIKKEI": "JP225m",
    "NIKKEI225": "JP225m",
    "日経": "JP225m",
    "日経225": "JP225m",
    "USOILM": "USOILm",
    "USOIL": "USOILm",
    "OIL": "USOILm",
    "WTI": "USOILm",
    "原油": "USOILm",
    "XAUUSDM": "XAUUSDm",
    "XAUUSD": "XAUUSDm",
    "GOLD": "XAUUSDm",
    "金": "XAUUSDm",
    "ゴールド": "XAUUSDm",
    "XAGUSDM": "XAGUSDm",
    "XAGUSD": "XAGUSDm",
    "SILVER": "XAGUSDm",
    "銀": "XAGUSDm",
    "シルバー": "XAGUSDm",
    "BTCUSDM": "BTCUSDm",
    "BTCUSD": "BTCUSDm",
    "BTC": "BTCUSDm",
    "BITCOIN": "BTCUSDm",
    "ビットコイン": "BTCUSDm",
    "ETHUSDM": "ETHUSDm",
    "ETHUSD": "ETHUSDm",
    "ETH": "ETHUSDm",
    "ETHEREUM": "ETHUSDm",
    "イーサ": "ETHUSDm",
    "イーサリアム": "ETHUSDm",
}

DIRECTION_ALIASES = {
    "BULL": "bull",
    "LONG": "bull",
    "BUY": "bull",
    "UP": "bull",
    "STRONG": "bull",
    "強気": "bull",
    "上": "bull",
    "上目線": "bull",
    "買い": "bull",
    "BEAR": "bear",
    "SHORT": "bear",
    "SELL": "bear",
    "DOWN": "bear",
    "WEAK": "bear",
    "弱気": "bear",
    "下": "bear",
    "下目線": "bear",
    "売り": "bear",
}


def ensure_runtime_layout() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    OPENCLAW_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    OPENCLAW_INBOX_PATH.touch(exist_ok=True)


def now_local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw in SUPPORTED_SYMBOLS:
        return raw
    normalized = raw.replace("/", "").replace("-", "").replace("_", "").replace(" ", "").upper()
    return SYMBOL_ALIASES.get(normalized)


def normalize_direction(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("-", "_").replace(" ", "_").upper()
    return DIRECTION_ALIASES.get(normalized)


def load_json_file(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
