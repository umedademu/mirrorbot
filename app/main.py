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
CHART_BAR_COUNT = 30
SYMBOL_ROWS = (
    ("USDJPYm", "EURUSDm", "JP225m", "USOILm"),
    ("XAUUSDm", "XAGUSDm", "BTCUSDm", "ETHUSDm"),
)
ALL_SYMBOLS = tuple(symbol for row in SYMBOL_ROWS for symbol in row)


@dataclass(frozen=True)
class CandleBar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    bid: float
    ask: float
    digits: int
    bars: tuple[CandleBar, ...]


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


def fetch_snapshots() -> dict[str, SymbolSnapshot]:
    snapshots: dict[str, SymbolSnapshot] = {}

    for symbol in ALL_SYMBOLS:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, CHART_BAR_COUNT)

        if info is None or tick is None or rates is None or len(rates) == 0:
            code, message = mt5.last_error()
            raise RuntimeError(f"{symbol} の値段か足を取得できません: [{code}] {message}")

        bars = tuple(
            CandleBar(
                open=float(rate["open"]),
                high=float(rate["high"]),
                low=float(rate["low"]),
                close=float(rate["close"]),
            )
            for rate in rates
        )

        snapshots[symbol] = SymbolSnapshot(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            digits=info.digits,
            bars=bars,
        )

    return snapshots


def format_price(value: float, digits: int) -> str:
    normalized_digits = digits if digits >= 0 else 0
    return f"{value:,.{normalized_digits}f}"


class MT5RateMonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MT5 Rate Monitor")
        self.root.geometry("1260x640")
        self.root.minsize(1080, 560)
        self.root.configure(bg="#eef2f6")

        self.status_var = tk.StringVar(value="接続待機中")
        self.quote_vars = {
            symbol: {
                "bid": tk.StringVar(value="--"),
                "ask": tk.StringVar(value="--"),
            }
            for symbol in ALL_SYMBOLS
        }
        self.chart_canvases: dict[str, tk.Canvas] = {}
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
        style.configure("TileSub.TLabel", background="#f8fafc", foreground="#64748b")

        outer = ttk.Frame(self.root, style="Page.TFrame", padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer, style="Card.TFrame", padding=16)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="MT5 監視銘柄",
            style="Headline.TLabel",
            font=("Yu Gothic UI Semibold", 18),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="売値と買値を更新し、各銘柄に１分足を表示",
            style="Muted.TLabel",
            font=("Yu Gothic UI", 9),
            wraplength=520,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(
            header,
            textvariable=self.status_var,
            style="Muted.TLabel",
            font=("Yu Gothic UI", 9),
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        content = ttk.Frame(outer, style="Page.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        for column_index in range(4):
            content.columnconfigure(column_index, weight=1)
        for row_index in range(2):
            content.rowconfigure(row_index, weight=1)

        for row_index, symbols in enumerate(SYMBOL_ROWS):
            for column_index, symbol in enumerate(symbols):
                tile = ttk.Frame(content, style="Tile.TFrame", padding=12)
                tile.grid(
                    row=row_index,
                    column=column_index,
                    sticky="nsew",
                    padx=5,
                    pady=5,
                )
                tile.columnconfigure(1, weight=1)
                tile.rowconfigure(1, weight=1)

                ttk.Label(
                    tile,
                    text=symbol,
                    style="TileSymbol.TLabel",
                    font=("Yu Gothic UI Semibold", 12),
                ).grid(row=0, column=0, sticky="w", pady=(0, 6))
                ttk.Label(
                    tile,
                    text="１分足",
                    style="TileSub.TLabel",
                    font=("Yu Gothic UI", 8),
                ).grid(row=0, column=1, sticky="e", pady=(0, 6))

                canvas = tk.Canvas(
                    tile,
                    width=210,
                    height=64,
                    bg="#f8fafc",
                    bd=0,
                    highlightthickness=0,
                    relief="flat",
                )
                canvas.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
                self.chart_canvases[symbol] = canvas

                ttk.Label(
                    tile,
                    text="売値",
                    style="TileKey.TLabel",
                    font=("Yu Gothic UI", 8),
                ).grid(row=2, column=0, sticky="w")
                ttk.Label(
                    tile,
                    textvariable=self.quote_vars[symbol]["bid"],
                    style="TileValue.TLabel",
                    font=("Consolas", 12),
                ).grid(row=2, column=1, sticky="e")
                ttk.Label(
                    tile,
                    text="買値",
                    style="TileKey.TLabel",
                    font=("Yu Gothic UI", 8),
                ).grid(row=3, column=0, sticky="w", pady=(4, 0))
                ttk.Label(
                    tile,
                    textvariable=self.quote_vars[symbol]["ask"],
                    style="TileValue.TLabel",
                    font=("Consolas", 12),
                ).grid(row=3, column=1, sticky="e", pady=(4, 0))

    def _start_monitor(self) -> None:
        self.status_var.set("MT5 に接続中...")
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()

    def _monitor_loop(self) -> None:
        try:
            initialize_mt5()
            self._call_on_main_thread(lambda: self.status_var.set("接続済み"))

            while not self.stop_event.is_set():
                snapshots = fetch_snapshots()
                self._call_on_main_thread(lambda data=snapshots: self._apply_rates(data))
                self.stop_event.wait(UPDATE_INTERVAL_MS / 1000)
        except Exception as exc:  # pragma: no cover
            self._call_on_main_thread(lambda message=str(exc): self._apply_error(message))
        finally:
            mt5.shutdown()

    def _apply_rates(self, snapshots: dict[str, SymbolSnapshot]) -> None:
        for symbol, snapshot in snapshots.items():
            self.quote_vars[symbol]["bid"].set(format_price(snapshot.bid, snapshot.digits))
            self.quote_vars[symbol]["ask"].set(format_price(snapshot.ask, snapshot.digits))
            self._draw_chart(self.chart_canvases[symbol], snapshot.bars)

    def _apply_error(self, message: str) -> None:
        for symbol in ALL_SYMBOLS:
            self.quote_vars[symbol]["bid"].set("--")
            self.quote_vars[symbol]["ask"].set("--")
            self.chart_canvases[symbol].delete("all")
        self.status_var.set(message)

    def _draw_chart(self, canvas: tk.Canvas, bars: tuple[CandleBar, ...]) -> None:
        width = max(canvas.winfo_width(), 210)
        height = max(canvas.winfo_height(), 64)
        top_padding = 6
        bottom_padding = 6
        left_padding = 6
        right_padding = 6

        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#f8fafc", outline="")

        if not bars:
            return

        low_price = min(bar.low for bar in bars)
        high_price = max(bar.high for bar in bars)

        if high_price == low_price:
            high_price += 1
            low_price -= 1

        chart_height = height - top_padding - bottom_padding
        chart_width = width - left_padding - right_padding
        candle_space = chart_width / max(len(bars), 1)
        candle_body_width = max(min(candle_space * 0.55, 10), 3)

        def to_y(price: float) -> float:
            ratio = (price - low_price) / (high_price - low_price)
            return top_padding + chart_height - (ratio * chart_height)

        for guide_ratio in (0.25, 0.5, 0.75):
            y = top_padding + chart_height * guide_ratio
            canvas.create_line(left_padding, y, width - right_padding, y, fill="#d7dee7")

        for index, bar in enumerate(bars):
            center_x = left_padding + (index + 0.5) * candle_space
            high_y = to_y(bar.high)
            low_y = to_y(bar.low)
            open_y = to_y(bar.open)
            close_y = to_y(bar.close)

            is_up = bar.close >= bar.open
            line_color = "#0f9d58" if is_up else "#d93025"
            body_color = "#8ad2aa" if is_up else "#f2a8a0"

            canvas.create_line(center_x, high_y, center_x, low_y, fill=line_color, width=1)

            top_y = min(open_y, close_y)
            bottom_y = max(open_y, close_y)

            if abs(bottom_y - top_y) < 2:
                mid_y = (top_y + bottom_y) / 2
                canvas.create_line(
                    center_x - candle_body_width / 2,
                    mid_y,
                    center_x + candle_body_width / 2,
                    mid_y,
                    fill=line_color,
                    width=2,
                )
                continue

            canvas.create_rectangle(
                center_x - candle_body_width / 2,
                top_y,
                center_x + candle_body_width / 2,
                bottom_y,
                fill=body_color,
                outline=line_color,
            )

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
