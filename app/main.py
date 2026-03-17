from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

import MetaTrader5 as mt5


@dataclass(frozen=True)
class AccountSnapshot:
    login: int
    server: str
    balance: float
    equity: float
    profit: float
    currency: str
    terminal_path: str


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


def fetch_account_snapshot() -> AccountSnapshot:
    terminal_path = find_terminal_path()

    if terminal_path:
        initialized = mt5.initialize(path=terminal_path)
    else:
        initialized = mt5.initialize()

    if not initialized:
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: [{code}] {message}")

    try:
        info = mt5.account_info()
        if info is None:
            code, message = mt5.last_error()
            raise RuntimeError(f"MT5 account_info failed: [{code}] {message}")

        return AccountSnapshot(
            login=info.login,
            server=info.server,
            balance=info.balance,
            equity=info.equity,
            profit=info.profit,
            currency=info.currency,
            terminal_path=terminal_path or "auto-detect",
        )
    finally:
        mt5.shutdown()


class MT5BalanceApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MT5 Balance Viewer")
        self.root.geometry("560x320")
        self.root.minsize(520, 300)

        self.balance_var = tk.StringVar(value="--")
        self.currency_var = tk.StringVar(value="")
        self.login_var = tk.StringVar(value="--")
        self.server_var = tk.StringVar(value="--")
        self.equity_var = tk.StringVar(value="--")
        self.profit_var = tk.StringVar(value="--")
        self.path_var = tk.StringVar(value=find_terminal_path() or "auto-detect")
        self.status_var = tk.StringVar(value="接続待機中")

        self._build_ui()
        self.refresh_balance()

    def _build_ui(self) -> None:
        self.root.configure(bg="#f3f5f7")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Body.TLabel", background="#ffffff", foreground="#1f2937")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6b7280")
        style.configure("Balance.TLabel", background="#ffffff", foreground="#0f172a")
        style.configure("Refresh.TButton", padding=(14, 8))

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        card = ttk.Frame(outer, style="Card.TFrame", padding=24)
        card.pack(fill="both", expand=True)

        ttk.Label(card, text="MT5 口座残高", style="Muted.TLabel", font=("Yu Gothic UI", 12)).pack(anchor="w")

        balance_row = ttk.Frame(card, style="Card.TFrame")
        balance_row.pack(fill="x", pady=(8, 20))

        ttk.Label(
            balance_row,
            textvariable=self.balance_var,
            style="Balance.TLabel",
            font=("Yu Gothic UI Semibold", 30),
        ).pack(side="left")
        ttk.Label(
            balance_row,
            textvariable=self.currency_var,
            style="Muted.TLabel",
            font=("Yu Gothic UI", 14),
            padding=(12, 10, 0, 0),
        ).pack(side="left")

        details = ttk.Frame(card, style="Card.TFrame")
        details.pack(fill="x")
        details.columnconfigure(1, weight=1)

        rows = (
            ("ログインID", self.login_var),
            ("サーバー", self.server_var),
            ("有効証拠金", self.equity_var),
            ("評価損益", self.profit_var),
            ("ターミナル", self.path_var),
        )

        for row_index, (label, variable) in enumerate(rows):
            ttk.Label(details, text=label, style="Muted.TLabel", font=("Yu Gothic UI", 10)).grid(
                row=row_index,
                column=0,
                sticky="nw",
                padx=(0, 16),
                pady=4,
            )
            ttk.Label(
                details,
                textvariable=variable,
                style="Body.TLabel",
                font=("Consolas", 10) if label == "ターミナル" else ("Yu Gothic UI", 10),
                wraplength=360,
                justify="left",
            ).grid(row=row_index, column=1, sticky="w", pady=4)

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.pack(fill="x", side="bottom", pady=(24, 0))
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel", font=("Yu Gothic UI", 10)).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Button(
            footer,
            text="再読み込み",
            command=self.refresh_balance,
            style="Refresh.TButton",
        ).grid(row=0, column=1, sticky="e")

    def refresh_balance(self) -> None:
        self.status_var.set("MT5 に接続中...")
        thread = threading.Thread(target=self._refresh_worker, daemon=True)
        thread.start()

    def _refresh_worker(self) -> None:
        try:
            snapshot = fetch_account_snapshot()
            self.root.after(0, lambda: self._apply_snapshot(snapshot))
        except Exception as exc:  # pragma: no cover
            self.root.after(0, lambda: self._apply_error(str(exc)))

    def _apply_snapshot(self, snapshot: AccountSnapshot) -> None:
        self.balance_var.set(f"{snapshot.balance:,.2f}")
        self.currency_var.set(snapshot.currency)
        self.login_var.set(str(snapshot.login))
        self.server_var.set(snapshot.server)
        self.equity_var.set(f"{snapshot.equity:,.2f} {snapshot.currency}")
        self.profit_var.set(f"{snapshot.profit:,.2f} {snapshot.currency}")
        self.path_var.set(snapshot.terminal_path)
        self.status_var.set("接続済み")

    def _apply_error(self, message: str) -> None:
        self.balance_var.set("--")
        self.currency_var.set("")
        self.login_var.set("--")
        self.server_var.set("--")
        self.equity_var.set("--")
        self.profit_var.set("--")
        self.status_var.set(message)


def main() -> None:
    root = tk.Tk()
    MT5BalanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
