from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5

from bridge_common import (
    OPENCLAW_INBOX_PATH,
    TRADE_DB_PATH,
    TRADE_SETTINGS_PATH,
    ensure_runtime_layout,
    normalize_direction,
    normalize_symbol,
    now_local_iso,
)


SUCCESS_RETCODES = {
    getattr(mt5, "TRADE_RETCODE_DONE", 10009),
    getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
}


@dataclass
class TradeSettings:
    auto_trade_enabled: bool
    default_volume: float | None
    symbol_volumes: dict[str, float]
    slippage_points: int
    magic_number: int


def load_trade_settings(path: Path) -> TradeSettings:
    default_payload = {
        "auto_trade_enabled": False,
        "default_volume": None,
        "symbol_volumes": {},
        "slippage_points": 30,
        "magic_number": 42004201,
    }
    if not path.exists():
        path.write_text(json.dumps(default_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    symbol_volumes = {
        str(symbol).strip(): float(volume)
        for symbol, volume in dict(raw.get("symbol_volumes", {})).items()
        if volume is not None
    }
    default_volume = raw.get("default_volume")
    return TradeSettings(
        auto_trade_enabled=bool(raw.get("auto_trade_enabled", False)),
        default_volume=float(default_volume) if default_volume is not None else None,
        symbol_volumes=symbol_volumes,
        slippage_points=max(1, int(raw.get("slippage_points", 30))),
        magic_number=max(1, int(raw.get("magic_number", 42004201))),
    )


def save_trade_settings(path: Path, settings: TradeSettings) -> None:
    payload = {
        "auto_trade_enabled": settings.auto_trade_enabled,
        "default_volume": settings.default_volume,
        "symbol_volumes": settings.symbol_volumes,
        "slippage_points": settings.slippage_points,
        "magic_number": settings.magic_number,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AutoTradeBridge:
    def __init__(self) -> None:
        ensure_runtime_layout()
        self.settings_path = TRADE_SETTINGS_PATH
        self.settings = load_trade_settings(self.settings_path)
        self.conn = sqlite3.connect(TRADE_DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._status_lock = threading.Lock()
        self._status_text = ""
        self._inbox_offset = 0
        self._inbox_partial = ""
        self._set_status("自動売買: 停止中" if not self.settings.auto_trade_enabled else "自動売買: 稼働中")

    def close(self) -> None:
        self.conn.close()

    def get_status_text(self) -> str:
        with self._status_lock:
            return self._status_text

    def toggle_enabled(self) -> bool:
        self.settings.auto_trade_enabled = not self.settings.auto_trade_enabled
        save_trade_settings(self.settings_path, self.settings)
        self._set_status("自動売買: 稼働中" if self.settings.auto_trade_enabled else "自動売買: 停止中")
        return self.settings.auto_trade_enabled

    def process_cycle(self, snapshots: dict[str, Any], positions: tuple[Any, ...]) -> bool:
        changed = self._sync_closed_rows(positions, snapshots)
        new_signals = self._read_new_signals()
        if not new_signals:
            return changed

        for signal in new_signals:
            if self._handle_signal(signal, snapshots):
                changed = True
        return changed

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS signal_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                in_at TEXT NOT NULL,
                in_price REAL NOT NULL,
                out_at TEXT,
                out_price REAL,
                source_post_id TEXT NOT NULL,
                source_post_url TEXT NOT NULL,
                source_user_url TEXT,
                post_at TEXT,
                reason TEXT,
                state TEXT NOT NULL,
                ticket TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_trades_unique_post
            ON signal_trades (source_post_id, symbol, direction);
            CREATE INDEX IF NOT EXISTS idx_signal_trades_open_lookup
            ON signal_trades (user_id, symbol, state);
            CREATE INDEX IF NOT EXISTS idx_signal_trades_ticket
            ON signal_trades (ticket);
            """
        )
        self.conn.commit()

    def _set_status(self, text: str) -> None:
        with self._status_lock:
            self._status_text = text

    def _read_new_signals(self) -> list[dict[str, Any]]:
        if not OPENCLAW_INBOX_PATH.exists():
            return []

        with OPENCLAW_INBOX_PATH.open("r", encoding="utf-8") as handle:
            handle.seek(self._inbox_offset)
            chunk = handle.read()
            self._inbox_offset = handle.tell()

        if not chunk:
            return []

        text = self._inbox_partial + chunk
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            self._inbox_partial = lines.pop()
        else:
            self._inbox_partial = ""

        signals: list[dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                signals.append(payload)
        return signals

    def _handle_signal(self, raw: dict[str, Any], snapshots: dict[str, Any]) -> bool:
        signal = self._normalize_signal(raw)
        if signal is None:
            return False

        if self._signal_exists(signal["source_post_id"], signal["symbol"], signal["direction"]):
            return False

        snapshot = snapshots.get(signal["symbol"])
        if snapshot is None:
            self._insert_signal_row(signal, 0.0, "error", last_error="値段が取得できません")
            self._set_status(f"自動売買: {signal['symbol']} の値段取得に失敗")
            return False

        entry_price = self._entry_price(signal["direction"], snapshot)
        open_rows = self._fetch_open_rows(signal["user_id"], signal["symbol"])
        if any(row["direction"] == signal["direction"] for row in open_rows):
            self._insert_signal_row(signal, entry_price, "ignored", last_error="同方向の建玉が残っています")
            self._set_status(f"自動売買: {signal['user_id']} {signal['symbol']} は同方向のため見送り")
            return False

        if not self.settings.auto_trade_enabled:
            self._insert_signal_row(signal, entry_price, "ignored", last_error="自動売買が停止中です")
            self._set_status(f"自動売買: 停止中のため {signal['symbol']} を記録のみ")
            return False

        for row in open_rows:
            if not row["ticket"]:
                continue
            if not self._close_row(row, snapshots):
                self._insert_signal_row(signal, entry_price, "error", last_error="反対建玉の決済に失敗しました")
                self._set_status(f"自動売買: {signal['symbol']} の反対建玉を閉じられません")
                return False

        row_id = self._insert_signal_row(signal, entry_price, "pending")
        success, message, ticket, actual_price = self._open_position(signal["symbol"], signal["direction"], row_id, snapshots)
        if not success:
            self._update_row_state(row_id, "error", actual_price, last_error=message)
            self._set_status(f"自動売買: {signal['symbol']} の新規発注に失敗")
            return False

        self.conn.execute(
            """
            UPDATE signal_trades
            SET state = ?, ticket = ?, in_price = ?, updated_at = ?, last_error = NULL
            WHERE id = ?
            """,
            ("open", ticket, actual_price, now_local_iso(), row_id),
        )
        self.conn.commit()
        self._set_status(f"自動売買: {signal['user_id']} {signal['symbol']} {signal['direction']} を発注")
        return True

    def _normalize_signal(self, raw: dict[str, Any]) -> dict[str, str] | None:
        user_id = str(raw.get("user_id", "")).strip()
        symbol = normalize_symbol(raw.get("symbol"))
        direction = normalize_direction(raw.get("direction"))
        source_post_id = str(raw.get("post_id", "")).strip()
        source_post_url = str(raw.get("post_url", "")).strip()
        if not user_id or not symbol or not direction or not source_post_id or not source_post_url:
            return None

        return {
            "user_id": user_id,
            "symbol": symbol,
            "direction": direction,
            "source_post_id": source_post_id,
            "source_post_url": source_post_url,
            "source_user_url": str(raw.get("user_url", "")).strip(),
            "post_at": str(raw.get("posted_at", "")).strip(),
            "reason": str(raw.get("reason", "")).strip(),
        }

    def _signal_exists(self, post_id: str, symbol: str, direction: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM signal_trades
            WHERE source_post_id = ? AND symbol = ? AND direction = ?
            LIMIT 1
            """,
            (post_id, symbol, direction),
        ).fetchone()
        return row is not None

    def _fetch_open_rows(self, user_id: str, symbol: str) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM signal_trades
            WHERE user_id = ? AND symbol = ? AND state = 'open' AND out_at IS NULL
            ORDER BY id ASC
            """,
            (user_id, symbol),
        ).fetchall()
        return list(rows)

    def _insert_signal_row(
        self,
        signal: dict[str, str],
        in_price: float,
        state: str,
        *,
        last_error: str | None = None,
    ) -> int:
        now_text = now_local_iso()
        cursor = self.conn.execute(
            """
            INSERT INTO signal_trades (
                user_id,
                symbol,
                direction,
                in_at,
                in_price,
                out_at,
                out_price,
                source_post_id,
                source_post_url,
                source_user_url,
                post_at,
                reason,
                state,
                ticket,
                last_error,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                signal["user_id"],
                signal["symbol"],
                signal["direction"],
                now_text,
                in_price,
                signal["source_post_id"],
                signal["source_post_url"],
                signal["source_user_url"],
                signal["post_at"],
                signal["reason"],
                state,
                last_error,
                now_text,
                now_text,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _update_row_state(
        self,
        row_id: int,
        state: str,
        in_price: float | None = None,
        *,
        last_error: str | None = None,
    ) -> None:
        current = self.conn.execute(
            "SELECT in_price FROM signal_trades WHERE id = ?",
            (row_id,),
        ).fetchone()
        resolved_in_price = in_price if in_price is not None else float(current["in_price"])
        self.conn.execute(
            """
            UPDATE signal_trades
            SET state = ?, in_price = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (state, resolved_in_price, last_error, now_local_iso(), row_id),
        )
        self.conn.commit()

    def _entry_price(self, direction: str, snapshot: Any) -> float:
        return float(snapshot.ask) if direction == "bull" else float(snapshot.bid)

    def _resolve_volume(self, symbol: str) -> float:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"{symbol} の数量設定を取得できません")
        raw_volume = self.settings.symbol_volumes.get(symbol)
        if raw_volume is None:
            raw_volume = self.settings.default_volume
        if raw_volume is None:
            raw_volume = float(info.volume_min)
        step = float(info.volume_step or info.volume_min or 0.01)
        minimum = float(info.volume_min or step)
        maximum = float(info.volume_max or raw_volume)
        volume = max(minimum, min(maximum, float(raw_volume)))
        if step > 0:
            steps = round((volume - minimum) / step)
            volume = minimum + (steps * step)
        decimals = 0
        if step < 1:
            step_text = f"{step:.8f}".rstrip("0")
            decimals = len(step_text.split(".")[1]) if "." in step_text else 0
        return round(volume, decimals)

    def _resolve_filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return getattr(mt5, "ORDER_FILLING_RETURN", 2)
        preferred = getattr(info, "filling_mode", None)
        candidates = [
            preferred,
            getattr(mt5, "ORDER_FILLING_IOC", None),
            getattr(mt5, "ORDER_FILLING_FOK", None),
            getattr(mt5, "ORDER_FILLING_RETURN", None),
        ]
        for value in candidates:
            if isinstance(value, int):
                return value
        return 0

    def _open_position(
        self,
        symbol: str,
        direction: str,
        row_id: int,
        snapshots: dict[str, Any],
    ) -> tuple[bool, str, str | None, float]:
        snapshot = snapshots[symbol]
        if not mt5.symbol_select(symbol, True):
            return False, "銘柄を有効化できません", None, self._entry_price(direction, snapshot)

        order_type = mt5.ORDER_TYPE_BUY if direction == "bull" else mt5.ORDER_TYPE_SELL
        price = float(snapshot.ask) if direction == "bull" else float(snapshot.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": self._resolve_volume(symbol),
            "type": order_type,
            "price": price,
            "deviation": self.settings.slippage_points,
            "magic": self.settings.magic_number,
            "comment": f"mb:{row_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._resolve_filling_mode(symbol),
        }
        result = mt5.order_send(request)
        if result is None:
            code, message = mt5.last_error()
            return False, f"MT5 発注失敗 [{code}] {message}", None, price

        actual_price = float(getattr(result, "price", 0.0) or price)
        if int(result.retcode) not in SUCCESS_RETCODES:
            return False, f"MT5 発注失敗 [{result.retcode}] {result.comment}", None, actual_price

        ticket = getattr(result, "order", 0) or getattr(result, "deal", 0)
        return True, "", str(ticket), actual_price

    def _close_row(self, row: sqlite3.Row, snapshots: dict[str, Any]) -> bool:
        symbol = str(row["symbol"])
        ticket = str(row["ticket"])
        if not ticket:
            return False

        snapshot = snapshots.get(symbol)
        if snapshot is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                self._mark_row_closed(row["id"], None, "値段が取得できません")
                return False
            class TickSnapshot:
                bid = tick.bid
                ask = tick.ask
            snapshot = TickSnapshot()

        if row["direction"] == "bull":
            order_type = mt5.ORDER_TYPE_SELL
            price = float(snapshot.bid)
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = float(snapshot.ask)

        live_position = mt5.positions_get(ticket=int(ticket))
        live_volume = None
        if live_position:
            live_volume = float(live_position[0].volume)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": live_volume if live_volume is not None else self._resolve_volume(symbol),
            "type": order_type,
            "position": int(ticket),
            "price": price,
            "deviation": self.settings.slippage_points,
            "magic": self.settings.magic_number,
            "comment": f"mb-close:{row['id']}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._resolve_filling_mode(symbol),
        }
        result = mt5.order_send(request)
        if result is None:
            code, message = mt5.last_error()
            self._mark_row_error(row["id"], f"決済失敗 [{code}] {message}")
            return False

        actual_price = float(getattr(result, "price", 0.0) or price)
        if int(result.retcode) not in SUCCESS_RETCODES:
            self._mark_row_error(row["id"], f"決済失敗 [{result.retcode}] {result.comment}")
            return False

        self._mark_row_closed(row["id"], actual_price, None)
        return True

    def _mark_row_closed(
        self,
        row_id: int,
        out_price: float | None,
        last_error: str | None,
        *,
        out_at: str | None = None,
    ) -> None:
        resolved_out_at = out_at or now_local_iso()
        self.conn.execute(
            """
            UPDATE signal_trades
            SET state = 'closed',
                out_at = COALESCE(out_at, ?),
                out_price = COALESCE(out_price, ?),
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (resolved_out_at, out_price, last_error, now_local_iso(), row_id),
        )
        self.conn.commit()

    def _mark_row_error(self, row_id: int, message: str) -> None:
        self.conn.execute(
            """
            UPDATE signal_trades
            SET last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (message, now_local_iso(), row_id),
        )
        self.conn.commit()

    def _sync_closed_rows(self, positions: tuple[Any, ...], snapshots: dict[str, Any]) -> bool:
        live_tickets = {str(position.ticket) for position in positions}
        rows = self.conn.execute(
            """
            SELECT *
            FROM signal_trades
            WHERE state = 'open' AND ticket IS NOT NULL AND out_at IS NULL
            """
        ).fetchall()

        changed = False
        for row in rows:
            ticket = str(row["ticket"])
            if ticket in live_tickets:
                continue

            out_price, out_at = self._find_closed_fill(str(row["symbol"]), ticket)
            if out_price is None:
                snapshot = snapshots.get(str(row["symbol"]))
                if snapshot is not None:
                    out_price = float(snapshot.bid if row["direction"] == "bull" else snapshot.ask)
            self._mark_row_closed(int(row["id"]), out_price, row["last_error"], out_at=out_at)
            changed = True
        return changed

    def _find_closed_fill(self, symbol: str, ticket: str) -> tuple[float | None, str | None]:
        end = datetime.now()
        start = end - timedelta(days=7)
        deals = mt5.history_deals_get(start, end, group=symbol)
        if deals is None:
            return None, None

        matching = []
        close_entries = {
            getattr(mt5, "DEAL_ENTRY_OUT", 1),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
        }
        for deal in deals:
            position_id = str(getattr(deal, "position_id", ""))
            if position_id != ticket:
                continue
            if getattr(deal, "entry", None) not in close_entries:
                continue
            matching.append(deal)

        if not matching:
            return None, None
        latest = max(matching, key=lambda item: getattr(item, "time_msc", 0) or getattr(item, "time", 0))
        closed_at = datetime.fromtimestamp(getattr(latest, "time", int(end.timestamp()))).astimezone().isoformat(timespec="seconds")
        return float(getattr(latest, "price", 0.0) or 0.0), closed_at
