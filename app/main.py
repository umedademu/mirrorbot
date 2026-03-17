from __future__ import annotations

import os
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

import MetaTrader5 as mt5


UPDATE_INTERVAL_MS = 500
SYMBOL_COLUMNS = (
    ("USDJPYm", "EURUSDm", "JP225m", "USOILm"),
    ("XAUUSDm", "XAGUSDm", "BTCUSDm", "ETHUSDm"),
)
ALL_SYMBOLS = tuple(symbol for column in SYMBOL_COLUMNS for symbol in column)


@dataclass(frozen=True)
class RateSnapshot:
    symbol: str
    bid: float
    ask: float
    digits: int


DEFAULT_TERMINAL_CANDIDATES = (
    Path(r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"),
    Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    Path(r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"),
)


def find_terminal_path() -> str | None:
    env_path = os.environ.get("MT5_TERMINAL_PATH")
    if env_path:
        expanded = Path(env_path).expanduser()
        if expanded.is_file():
            return str(expanded)

    for candidate in DEFAULT_TERMINAL_CANDIDATES:
        if candidate.is_file():
            return str(candidate)

    return None


def initialize_mt5() -> None:
    terminal_path = find_terminal_path()
    initialized = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()

    if not initialized:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 に接続できません: [{code}] {message}")

    for symbol in ALL_SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            code, message = mt5.last_error()
            raise RuntimeError(f"{symbol} を表示対象にできません: [{code}] {message}")


def fetch_rates() -> dict[str, RateSnapshot]:
    snapshots: dict[str, RateSnapshot] = {}

    for symbol in ALL_SYMBOLS:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)

        if info is None or tick is None:
            code, message = mt5.last_error()
            raise RuntimeError(f"{symbol} の値段を取得できません: [{code}] {message}")

        snapshots[symbol] = RateSnapshot(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            digits=info.digits,
        )

    return snapshots


def format_price(value: float, digits: int) -> str:
    normalized_digits = digits if digits >= 0 else 0
    return f"{value:,.{normalized_digits}f}"


class MT5RateMonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MT5 Rate Monitor")
        self.root.geometry("860x620")
        self.root.minsize(760, 560)
        self.root.configure(bg="#eef2f6")

        self.status_var = tk.StringVar(value="接続待機中")
        self.quote_vars = {
            symbol: {
                "bid": tk.StringVar(value="--"),
                "ask": tk.StringVar(value="--"),
            }
            for symbol in ALL_SYMBOLS
        }
        self.stop_event = threading.Event()
        self.closing = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_monitor()

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Page.TFrame", background="#eef2f6")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Tile.TFrame", background="#f8fafc")
        style.configure("Headline.TLabel", background="#ffffff", foreground="#0f172a")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b")
        style.configure("TileSymbol.TLabel", background="#f8fafc", foreground="#0f172a")
        style.configure("TileKey.TLabel", background="#f8fafc", foreground="#64748b")
        style.configure("TileValue.TLabel", background="#f8fafc", foreground="#111827")

        outer = ttk.Frame(self.root, style="Page.TFrame", padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer, style="Card.TFrame", padding=20)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="MT5 監視銘柄",
            style="Headline.TLabel",
            font=("Yu Gothic UI Semibold", 20),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="0.5秒ごとに売値と買値を更新",
            style="Muted.TLabel",
            font=("Yu Gothic UI", 10),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(
            header,
            textvariable=self.status_var,
            style="Muted.TLabel",
            font=("Yu Gothic UI", 10),
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        content = ttk.Frame(outer, style="Page.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)

        for column_index, symbols in enumerate(SYMBOL_COLUMNS):
            column_frame = ttk.Frame(content, style="Page.TFrame")
            column_frame.grid(
                row=0,
                column=column_index,
                sticky="nsew",
                padx=(0, 10) if column_index == 0 else (10, 0),
            )
            column_frame.columnconfigure(0, weight=1)

            for row_index, symbol in enumerate(symbols):
                tile = ttk.Frame(column_frame, style="Tile.TFrame", padding=18)
                tile.grid(row=row_index, column=0, sticky="ew", pady=(0, 14))
                tile.columnconfigure(1, weight=1)

                ttk.Label(
                    tile,
                    text=symbol,
                    style="TileSymbol.TLabel",
                    font=("Yu Gothic UI Semibold", 14),
                ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
                ttk.Label(
                    tile,
                    text="売値",
                    style="TileKey.TLabel",
                    font=("Yu Gothic UI", 10),
                ).grid(row=1, column=0, sticky="w")
                ttk.Label(
                    tile,
                    textvariable=self.quote_vars[symbol]["bid"],
                    style="TileValue.TLabel",
                    font=("Consolas", 16),
                ).grid(row=1, column=1, sticky="e")
                ttk.Label(
                    tile,
                    text="買値",
                    style="TileKey.TLabel",
                    font=("Yu Gothic UI", 10),
                ).grid(row=2, column=0, sticky="w", pady=(8, 0))
                ttk.Label(
                    tile,
                    textvariable=self.quote_vars[symbol]["ask"],
                    style="TileValue.TLabel",
                    font=("Consolas", 16),
                ).grid(row=2, column=1, sticky="e", pady=(8, 0))

    def _start_monitor(self) -> None:
        self.status_var.set("MT5 に接続中...")
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()

    def _monitor_loop(self) -> None:
        try:
            initialize_mt5()
            self._call_on_main_thread(lambda: self.status_var.set("接続済み"))

            while not self.stop_event.is_set():
                snapshots = fetch_rates()
                self._call_on_main_thread(lambda data=snapshots: self._apply_rates(data))
                self.stop_event.wait(UPDATE_INTERVAL_MS / 1000)
        except Exception as exc:  # pragma: no cover
            self._call_on_main_thread(lambda message=str(exc): self._apply_error(message))
        finally:
            mt5.shutdown()

    def _apply_rates(self, snapshots: dict[str, RateSnapshot]) -> None:
        for symbol, snapshot in snapshots.items():
            self.quote_vars[symbol]["bid"].set(format_price(snapshot.bid, snapshot.digits))
            self.quote_vars[symbol]["ask"].set(format_price(snapshot.ask, snapshot.digits))

    def _apply_error(self, message: str) -> None:
        for symbol in ALL_SYMBOLS:
            self.quote_vars[symbol]["bid"].set("--")
            self.quote_vars[symbol]["ask"].set("--")
        self.status_var.set(message)

    def _call_on_main_thread(self, callback: Callable[[], None]) -> None:
        if self.closing:
            return

        try:
            self.root.after(0, callback)
        except RuntimeError:
            pass

    def _on_close(self) -> None:
        self.closing = True
        self.stop_event.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MT5RateMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
