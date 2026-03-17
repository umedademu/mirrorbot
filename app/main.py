from __future__ import annotations

import os
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import MetaTrader5 as mt5


UPDATE_INTERVAL_MS = 500
CHART_BAR_COUNT = 30
PAGE_BG = "#0b1220"
TILE_BG = "#131c2e"
TILE_EDGE = "#21304a"
TEXT_MAIN = "#dbe7ff"
TEXT_SOFT = "#90a4c7"
GRID_LINE = "#2a3b58"
BUTTON_BG = "#162237"
BUTTON_ACTIVE_BG = "#28456e"
BUTTON_TEXT = "#d7e5ff"
BID_COLOR = "#39d98a"
ASK_COLOR = "#ff7a70"
BULL_LINE = "#3bd68c"
BEAR_LINE = "#ff6b5f"
BULL_FILL = "#1f8f66"
BEAR_FILL = "#9f3f39"
TIMEFRAME_OPTIONS = (
    ("1分", mt5.TIMEFRAME_M1, "1分足"),
    ("5分", mt5.TIMEFRAME_M5, "5分足"),
    ("15分", mt5.TIMEFRAME_M15, "15分足"),
    ("30分", mt5.TIMEFRAME_M30, "30分足"),
    ("1時間", mt5.TIMEFRAME_H1, "1時間足"),
    ("4時間", mt5.TIMEFRAME_H4, "4時間足"),
    ("日足", mt5.TIMEFRAME_D1, "日足"),
)
DEFAULT_TIMEFRAME_LABEL = "1分"
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


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    ticket: str
    time_text: str
    trade_type: str
    volume: str
    price_open: str
    sl: str
    tp: str
    price_current: str
    swap: str
    profit: str


@dataclass(frozen=True)
class AccountSnapshot:
    summary_text: str


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


def fetch_snapshots(timeframe_code: int) -> dict[str, SymbolSnapshot]:
    snapshots: dict[str, SymbolSnapshot] = {}

    for symbol in ALL_SYMBOLS:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        rates = mt5.copy_rates_from_pos(symbol, timeframe_code, 0, CHART_BAR_COUNT)

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


def fetch_positions_and_account() -> tuple[tuple[PositionSnapshot, ...], AccountSnapshot]:
    account_info = mt5.account_info()
    if account_info is None:
        code, message = mt5.last_error()
        raise RuntimeError(f"口座情報を取得できません: [{code}] {message}")

    positions_raw = mt5.positions_get()
    if positions_raw is None:
        code, message = mt5.last_error()
        if code != 1:
            raise RuntimeError(f"ポジション情報を取得できません: [{code}] {message}")
        positions_raw = ()

    positions: list[PositionSnapshot] = []
    for position in positions_raw:
        symbol_info = mt5.symbol_info(position.symbol)
        digits = symbol_info.digits if symbol_info is not None else 2
        time_text = datetime.fromtimestamp(position.time).strftime("%Y.%m.%d %H:%M:%S")
        trade_type = "buy" if position.type == mt5.POSITION_TYPE_BUY else "sell"
        positions.append(
            PositionSnapshot(
                symbol=position.symbol,
                ticket=str(position.ticket),
                time_text=time_text,
                trade_type=trade_type,
                volume=f"{position.volume:g}",
                price_open=format_price(position.price_open, digits),
                sl=format_price(position.sl, digits),
                tp=format_price(position.tp, digits),
                price_current=format_price(position.price_current, digits),
                swap=f"{position.swap:,.2f}",
                profit=f"{position.profit:,.2f}",
            )
        )

    summary_text = (
        f"残高: {account_info.balance:,.2f} {account_info.currency}    "
        f"有効証拠金: {account_info.equity:,.2f}    "
        f"必要証拠金: {account_info.margin:,.2f}    "
        f"余剰証拠金: {account_info.margin_free:,.2f}    "
        f"証拠金維持率: {account_info.margin_level:,.2f} %"
    )
    return tuple(positions), AccountSnapshot(summary_text=summary_text)


def format_price(value: float, digits: int) -> str:
    normalized_digits = digits if digits >= 0 else 0
    return f"{value:,.{normalized_digits}f}"


class MT5RateMonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MT5 Rate Monitor")
        self.root.geometry("1260x640")
        self.root.minsize(1080, 560)
        self.root.configure(bg=PAGE_BG)

        self.quote_vars = {
            symbol: {
                "bid": tk.StringVar(value="--"),
                "ask": tk.StringVar(value="--"),
            }
            for symbol in ALL_SYMBOLS
        }
        self.header_quote_vars = {
            symbol: {
                "bid": tk.StringVar(value="BID --"),
                "ask": tk.StringVar(value="ASK --"),
            }
            for symbol in ALL_SYMBOLS
        }
        self.timeframe_label_var = tk.StringVar(value="1分足")
        self.selected_timeframe_label = DEFAULT_TIMEFRAME_LABEL
        self.selected_timeframe_code = self._get_timeframe_code(DEFAULT_TIMEFRAME_LABEL)
        self.account_summary_var = tk.StringVar(value="残高: --    有効証拠金: --    必要証拠金: --    余剰証拠金: --    証拠金維持率: --")
        self.timeframe_buttons: dict[str, ttk.Button] = {}
        self.chart_canvases: dict[str, tk.Canvas] = {}
        self.positions_tree: ttk.Treeview | None = None
        self.stop_event = threading.Event()
        self.closing = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_monitor()

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Page.TFrame", background=PAGE_BG)
        style.configure("Tile.TFrame", background=TILE_BG, relief="flat")
        style.configure("TileSymbol.TLabel", background=TILE_BG, foreground=TEXT_SOFT)
        style.configure("TileSub.TLabel", background=TILE_BG, foreground=TEXT_SOFT)
        style.configure("BidHero.TLabel", background=TILE_BG, foreground=BID_COLOR)
        style.configure("AskHero.TLabel", background=TILE_BG, foreground=ASK_COLOR)
        style.configure("Bottom.TFrame", background=TILE_BG)
        style.configure("Summary.TLabel", background=TILE_BG, foreground=TEXT_MAIN)
        style.configure("Toolbar.TFrame", background=PAGE_BG)
        style.configure(
            "Positions.Treeview",
            background=TILE_BG,
            fieldbackground=TILE_BG,
            foreground=TEXT_MAIN,
            rowheight=24,
            bordercolor=TILE_EDGE,
            lightcolor=TILE_EDGE,
            darkcolor=TILE_EDGE,
        )
        style.configure(
            "Positions.Treeview.Heading",
            background=BUTTON_BG,
            foreground=TEXT_MAIN,
            bordercolor=TILE_EDGE,
            lightcolor=BUTTON_BG,
            darkcolor=BUTTON_BG,
            font=("Yu Gothic UI Semibold", 9),
        )
        style.map(
            "Positions.Treeview",
            background=[("selected", "#22334f")],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "Positions.Treeview.Heading",
            background=[("active", "#1d2d46")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "TimeButton.TButton",
            padding=(10, 5),
            font=("Yu Gothic UI", 9),
            background=BUTTON_BG,
            foreground=BUTTON_TEXT,
            bordercolor=TILE_EDGE,
            lightcolor=BUTTON_BG,
            darkcolor=BUTTON_BG,
        )
        style.map(
            "TimeButton.TButton",
            background=[("active", "#1d2d46"), ("pressed", "#20324f")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure(
            "TimeButtonActive.TButton",
            padding=(10, 5),
            font=("Yu Gothic UI Semibold", 9),
            background=BUTTON_ACTIVE_BG,
            foreground="#ffffff",
            bordercolor="#4678b8",
            lightcolor=BUTTON_ACTIVE_BG,
            darkcolor=BUTTON_ACTIVE_BG,
        )
        style.map(
            "TimeButtonActive.TButton",
            background=[("active", "#31588d"), ("pressed", "#2b4e7e")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )

        outer = ttk.Frame(self.root, style="Page.TFrame", padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(2, weight=0)

        toolbar = ttk.Frame(outer, style="Toolbar.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for index, (label, _, __) in enumerate(TIMEFRAME_OPTIONS):
            button = ttk.Button(
                toolbar,
                text=label,
                command=lambda value=label: self._change_timeframe(value),
                style="TimeButton.TButton",
            )
            button.grid(row=0, column=index, padx=(0, 6))
            self.timeframe_buttons[label] = button
        self._refresh_timeframe_buttons()

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
                tile.columnconfigure(0, weight=1)
                tile.columnconfigure(1, weight=1)
                tile.rowconfigure(1, weight=1)

                ttk.Label(
                    tile,
                    text=symbol,
                    style="TileSymbol.TLabel",
                    font=("Yu Gothic UI", 10),
                ).grid(row=0, column=0, sticky="w", pady=(0, 6))

                header_right = ttk.Frame(tile, style="Tile.TFrame")
                header_right.grid(row=0, column=1, sticky="e", pady=(0, 6))
                ttk.Label(
                    header_right,
                    textvariable=self.header_quote_vars[symbol]["bid"],
                    style="BidHero.TLabel",
                    font=("Consolas", 11, "bold"),
                ).grid(row=0, column=0, sticky="e", padx=(0, 10))
                ttk.Label(
                    header_right,
                    textvariable=self.header_quote_vars[symbol]["ask"],
                    style="AskHero.TLabel",
                    font=("Consolas", 11, "bold"),
                ).grid(row=0, column=1, sticky="e", padx=(0, 10))
                ttk.Label(
                    header_right,
                    textvariable=self.timeframe_label_var,
                    style="TileSub.TLabel",
                    font=("Yu Gothic UI", 7),
                ).grid(row=0, column=2, sticky="e")

                canvas = tk.Canvas(
                    tile,
                    width=210,
                    height=136,
                    bg=TILE_BG,
                    bd=0,
                    highlightthickness=0,
                    relief="flat",
                )
                canvas.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 6))
                self.chart_canvases[symbol] = canvas

        bottom = ttk.Frame(outer, style="Bottom.TFrame", padding=10)
        bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)

        columns = (
            "symbol",
            "ticket",
            "time",
            "type",
            "volume",
            "price_open",
            "sl",
            "tp",
            "price_current",
            "swap",
            "profit",
        )
        tree = ttk.Treeview(
            bottom,
            columns=columns,
            show="headings",
            height=5,
            style="Positions.Treeview",
        )
        tree.grid(row=0, column=0, sticky="ew")
        self.positions_tree = tree

        headings = (
            ("symbol", "銘柄", 110, "w"),
            ("ticket", "チケット", 110, "w"),
            ("time", "時間", 170, "w"),
            ("type", "タイプ", 70, "center"),
            ("volume", "数量", 60, "e"),
            ("price_open", "価格", 95, "e"),
            ("sl", "決済逆指値(S/L)", 120, "e"),
            ("tp", "決済指値(T/P)", 120, "e"),
            ("price_current", "価格", 95, "e"),
            ("swap", "スワップ", 80, "e"),
            ("profit", "損益", 80, "e"),
        )
        for column_id, heading_text, width, anchor in headings:
            tree.heading(column_id, text=heading_text, anchor=anchor)
            tree.column(column_id, width=width, minwidth=width, anchor=anchor, stretch=True)

        tree_scroll = ttk.Scrollbar(bottom, orient="horizontal", command=tree.xview)
        tree_scroll.grid(row=1, column=0, sticky="ew")
        tree.configure(xscrollcommand=tree_scroll.set)

        ttk.Label(
            bottom,
            textvariable=self.account_summary_var,
            style="Summary.TLabel",
            font=("Yu Gothic UI Semibold", 10),
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _start_monitor(self) -> None:
        self._set_window_title("接続中")
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()

    def _monitor_loop(self) -> None:
        try:
            initialize_mt5()
            self._call_on_main_thread(lambda: self._set_window_title("接続済み"))

            while not self.stop_event.is_set():
                timeframe_code = self.selected_timeframe_code
                snapshots = fetch_snapshots(timeframe_code)
                positions, account = fetch_positions_and_account()
                self._call_on_main_thread(
                    lambda data=snapshots, current_positions=positions, current_account=account: self._apply_terminal_state(
                        data,
                        current_positions,
                        current_account,
                    )
                )
                self.stop_event.wait(UPDATE_INTERVAL_MS / 1000)
        except Exception as exc:  # pragma: no cover
            self._call_on_main_thread(lambda message=str(exc): self._apply_error(message))
        finally:
            mt5.shutdown()

    def _apply_terminal_state(
        self,
        snapshots: dict[str, SymbolSnapshot],
        positions: tuple[PositionSnapshot, ...],
        account: AccountSnapshot,
    ) -> None:
        for symbol, snapshot in snapshots.items():
            bid_text = format_price(snapshot.bid, snapshot.digits)
            ask_text = format_price(snapshot.ask, snapshot.digits)
            self.quote_vars[symbol]["bid"].set(bid_text)
            self.quote_vars[symbol]["ask"].set(ask_text)
            self.header_quote_vars[symbol]["bid"].set(f"BID {bid_text}")
            self.header_quote_vars[symbol]["ask"].set(f"ASK {ask_text}")
            self._draw_chart(self.chart_canvases[symbol], snapshot.bars)
        self._refresh_positions(positions)
        self.account_summary_var.set(account.summary_text)
        self._set_window_title("接続済み")

    def _apply_error(self, message: str) -> None:
        for symbol in ALL_SYMBOLS:
            self.quote_vars[symbol]["bid"].set("--")
            self.quote_vars[symbol]["ask"].set("--")
            self.header_quote_vars[symbol]["bid"].set("BID --")
            self.header_quote_vars[symbol]["ask"].set("ASK --")
            self.chart_canvases[symbol].delete("all")
        self._refresh_positions(())
        self.account_summary_var.set("残高: --    有効証拠金: --    必要証拠金: --    余剰証拠金: --    証拠金維持率: --")
        self._set_window_title(message)

    def _draw_chart(self, canvas: tk.Canvas, bars: tuple[CandleBar, ...]) -> None:
        width = max(canvas.winfo_width(), 210)
        height = max(canvas.winfo_height(), 136)
        top_padding = 2
        bottom_padding = 2
        left_padding = 4
        right_padding = 4

        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=TILE_BG, outline="")

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
            canvas.create_line(left_padding, y, width - right_padding, y, fill=GRID_LINE)

        for index, bar in enumerate(bars):
            center_x = left_padding + (index + 0.5) * candle_space
            high_y = to_y(bar.high)
            low_y = to_y(bar.low)
            open_y = to_y(bar.open)
            close_y = to_y(bar.close)

            is_up = bar.close >= bar.open
            line_color = BULL_LINE if is_up else BEAR_LINE
            body_color = BULL_FILL if is_up else BEAR_FILL

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

    def _set_window_title(self, message: str) -> None:
        self.root.title(f"MT5 Rate Monitor - {self.selected_timeframe_label} - {message}")

    def _refresh_positions(self, positions: tuple[PositionSnapshot, ...]) -> None:
        if self.positions_tree is None:
            return

        self.positions_tree.delete(*self.positions_tree.get_children())
        for position in positions:
            self.positions_tree.insert(
                "",
                "end",
                values=(
                    position.symbol,
                    position.ticket,
                    position.time_text,
                    position.trade_type,
                    position.volume,
                    position.price_open,
                    position.sl,
                    position.tp,
                    position.price_current,
                    position.swap,
                    position.profit,
                ),
            )

    def _get_timeframe_code(self, label: str) -> int:
        for timeframe_label, timeframe_code, _ in TIMEFRAME_OPTIONS:
            if timeframe_label == label:
                return timeframe_code
        raise ValueError(f"未対応の時間足です: {label}")

    def _get_timeframe_display(self, label: str) -> str:
        for timeframe_label, _, timeframe_display in TIMEFRAME_OPTIONS:
            if timeframe_label == label:
                return timeframe_display
        raise ValueError(f"未対応の時間足です: {label}")

    def _change_timeframe(self, label: str) -> None:
        self.selected_timeframe_label = label
        self.selected_timeframe_code = self._get_timeframe_code(label)
        self.timeframe_label_var.set(self._get_timeframe_display(label))
        self._refresh_timeframe_buttons()
        self._set_window_title("更新中")

    def _refresh_timeframe_buttons(self) -> None:
        for label, button in self.timeframe_buttons.items():
            style_name = "TimeButtonActive.TButton" if label == self.selected_timeframe_label else "TimeButton.TButton"
            button.configure(style=style_name)

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
