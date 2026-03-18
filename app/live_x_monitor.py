from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from bridge_common import (
    OPENCLAW_BATCH_PATH,
    OPENCLAW_MONITOR_STATE_PATH,
    OPENCLAW_POSTS_JSON_PATH,
    OPENCLAW_POSTS_TEXT_PATH,
    ensure_runtime_layout,
    normalize_direction,
    normalize_symbol,
)
from openclaw_signal_ingest import ingest_file
from openclaw_x_text_fetch import (
    DEFAULT_ACCOUNTS,
    FetchSettings,
    TimelinePost,
    fetch_posts,
    load_cookie,
    write_json,
    write_text,
)


DISCORD_CHANNEL_ID = "1483783703174971442"
FETCH_WINDOW_MINUTES = 20
POLL_INTERVAL_SECONDS = 30
OPENCLAW_SESSION_ID = "mirrorbot-live-x-watch"
SEEN_RETENTION_HOURS = 72
SEEN_POST_LIMIT = 500


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    direction: str
    reason: str


@dataclass(frozen=True)
class AnalyzedPostDecision:
    post_id: str
    signals: tuple[SignalDecision, ...]
    ignore_reason: str


@dataclass(frozen=True)
class XMonitorRow:
    checked_at: str
    user_id: str
    posted_at: str
    text: str
    judgment: str
    reason: str
    post_url: str


class OpenClawLiveMonitor:
    def __init__(
        self,
        on_state_change: Callable[[str, tuple[XMonitorRow, ...]], None] | None = None,
    ) -> None:
        ensure_runtime_layout()
        self._on_state_change = on_state_change
        self._status_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._status_text = "X監視: 起動準備中"
        self._history: deque[XMonitorRow] = deque(maxlen=80)
        self._state_path = OPENCLAW_MONITOR_STATE_PATH
        self._openclaw_path = shutil.which("openclaw")
        self._initial_sync_needed = not self._state_path.exists()

    def get_snapshot(self) -> tuple[str, tuple[XMonitorRow, ...]]:
        with self._status_lock:
            status_text = self._status_text
        with self._history_lock:
            history = tuple(self._history)
        return status_text, history

    def run(self, stop_event: threading.Event) -> None:
        self._set_status("X監視: 監視を開始")
        while not stop_event.is_set():
            self._run_cycle()
            if stop_event.wait(POLL_INTERVAL_SECONDS):
                break
        self._set_status("X監視: 停止")

    def _run_cycle(self) -> None:
        try:
            posts, errors = self._fetch_recent_posts()
        except Exception as exc:
            self._append_system_row("取得失敗", str(exc))
            self._set_status(f"X監視: 取得失敗 {self._shorten(str(exc), 80)}")
            return

        seen_posts = self._load_seen_posts()
        ordered_posts = sorted(posts, key=lambda item: item.posted_at)

        if self._initial_sync_needed:
            if ordered_posts:
                self._remember_posts(ordered_posts, seen_posts)
                for post in ordered_posts:
                    self._append_post_row(
                        post,
                        "初回同期",
                        "起動直後のため既読扱いにしました。",
                        emit=False,
                    )
                self._emit_state()
                self._set_status(f"X監視: 初回同期完了 {len(ordered_posts)} 件")
            elif errors:
                self._append_system_row("取得警告", " / ".join(errors[:2]))
                self._set_status(f"X監視: 取得警告 {len(errors)} 件")
            else:
                self._set_status("X監視: 初回同期完了 0 件")
            self._initial_sync_needed = False
            return

        new_posts = [post for post in ordered_posts if post.post_id not in seen_posts]
        if not new_posts:
            if errors:
                self._append_system_row("取得警告", " / ".join(errors[:2]))
                self._set_status(f"X監視: 取得警告 {len(errors)} 件 / 新着なし")
            else:
                self._set_status(f"X監視: 新着なし {self._clock_text()}")
            return

        try:
            decisions = self._analyze_posts(new_posts)
            signal_rows = self._build_signal_rows(new_posts, decisions)
            self._write_signal_batch(signal_rows)
            self._remember_posts(new_posts, seen_posts)
        except Exception as exc:
            self._append_system_row("解釈失敗", str(exc))
            self._set_status(f"X監視: 解釈失敗 {self._shorten(str(exc), 80)}")
            return

        for post in new_posts:
            decision = decisions[post.post_id]
            self._append_post_row(
                post,
                self._judgment_text(decision),
                self._reason_text(decision),
                emit=False,
            )
        if errors:
            self._append_system_row("取得警告", " / ".join(errors[:2]), emit=False)
        self._emit_state()

        discord_error = self._send_discord_report(new_posts, decisions)
        if discord_error:
            self._append_system_row("Discord失敗", discord_error)

        adopted_count = sum(len(decision.signals) for decision in decisions.values())
        status_text = f"X監視: 新着{len(new_posts)}件 / 採用{adopted_count}件"
        if discord_error:
            status_text += " / Discord失敗"
        self._set_status(status_text)

    def _fetch_recent_posts(self) -> tuple[list[TimelinePost], list[str]]:
        settings = FetchSettings(
            accounts=DEFAULT_ACCOUNTS,
            window_minutes=FETCH_WINDOW_MINUTES,
            json_output=OPENCLAW_POSTS_JSON_PATH,
            text_output=OPENCLAW_POSTS_TEXT_PATH,
            cookie=load_cookie(),
        )
        posts, errors = fetch_posts(settings)
        settings.json_output.parent.mkdir(parents=True, exist_ok=True)
        settings.text_output.parent.mkdir(parents=True, exist_ok=True)
        write_json(settings.json_output, posts)
        write_text(settings.text_output, posts, settings.window_minutes, bool(settings.cookie), errors)
        return posts, errors

    def _analyze_posts(self, posts: list[TimelinePost]) -> dict[str, AnalyzedPostDecision]:
        raw_response = self._run_openclaw_agent(self._build_prompt(posts))
        payload = self._extract_json_payload(raw_response)
        decisions = self._normalize_decisions(payload, posts)
        return decisions

    def _run_openclaw_agent(self, prompt: str) -> str:
        if not self._openclaw_path:
            raise RuntimeError("openclaw コマンドが見つかりません。")

        result = subprocess.run(
            [
                self._openclaw_path,
                "agent",
                "--json",
                "--session-id",
                OPENCLAW_SESSION_ID,
                "--message",
                prompt,
                "--thinking",
                "minimal",
                "--timeout",
                "90",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "OpenClaw の呼び出しに失敗しました。"
            raise RuntimeError(message)

        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("OpenClaw から返答がありません。")

        try:
            wrapped = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout

        if not isinstance(wrapped, dict):
            return stdout

        payloads = wrapped.get("payloads")
        if not isinstance(payloads, list):
            return stdout

        texts = [
            payload.get("text", "").strip()
            for payload in payloads
            if isinstance(payload, dict) and str(payload.get("text", "")).strip()
        ]
        if not texts:
            raise RuntimeError("OpenClaw の返答本文が空です。")
        return "\n".join(texts).strip()

    def _build_prompt(self, posts: list[TimelinePost]) -> str:
        post_rows = [
            {
                "post_id": post.post_id,
                "user_id": post.user_id,
                "user_url": post.user_url,
                "post_url": post.post_url,
                "posted_at": post.posted_at,
                "is_reply": post.is_reply,
                "text": post.text,
            }
            for post in posts
        ]
        return (
            "あなたの役目は、次の X 本文一覧だけを読み、mirrorbot がミラトレすべき投稿だけを選ぶことです。\n"
            "目的は相場観の分類ではなく、投稿者が実際に行っている売買をできるだけなぞることです。\n"
            "単なる感想、煽り、事後コメント、実況、結果への喜怒哀楽だけなら見送りにしてください。\n"
            "買い、売り、継続保有、追加、明確な反転が本文から強く読める場合だけ採用してください。\n"
            "値動きや画像は見ないでください。ここに書かれた本文だけで判断してください。\n"
            "同じ投稿で複数銘柄が明確なら複数件採用して構いません。ただし同じ銘柄で矛盾した方向は出さないでください。\n"
            "対象銘柄は USDJPYm, EURUSDm, JP225m, USOILm, XAUUSDm, XAGUSDm, BTCUSDm, ETHUSDm の 8 つだけです。\n"
            "方向は bull か bear のどちらかだけです。\n"
            "たとえば ドル円 -> USDJPYm、ユーロドル -> EURUSDm、日経 -> JP225m、原油 -> USOILm、金やゴールド -> XAUUSDm、銀やシルバー -> XAGUSDm、BTCやビットコイン -> BTCUSDm、ETHやイーサリアム -> ETHUSDm です。\n"
            "返答は JSON だけにしてください。説明文や markdown は不要です。\n"
            "必ず入力投稿ごとに 1 件ずつ results に入れてください。\n"
            "signals が空配列のときは ignore_reason に見送り理由を書いてください。\n"
            "signals が 1 件以上あるときは ignore_reason を空文字にしてください。\n"
            "返答の形は次だけです。\n"
            "{\n"
            '  "results": [\n'
            '    {\n'
            '      "post_id": "入力の post_id",\n'
            '      "signals": [\n'
            '        {"symbol": "USDJPYm", "direction": "bull", "reason": "短い根拠"}\n'
            "      ],\n"
            '      "ignore_reason": ""\n'
            "    },\n"
            '    {\n'
            '      "post_id": "入力の post_id",\n'
            '      "signals": [],\n'
            '      "ignore_reason": "見送り理由"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "入力投稿一覧:\n"
            f"{json.dumps(post_rows, ensure_ascii=False, indent=2)}"
        )

    def _extract_json_payload(self, raw_text: str) -> object:
        candidates: list[str] = []
        stripped = raw_text.strip()
        if stripped:
            candidates.append(stripped)

        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            candidates.insert(0, fenced.group(1).strip())

        for start_token, end_token in (("{", "}"), ("[", "]")):
            start_index = stripped.find(start_token)
            end_index = stripped.rfind(end_token)
            if start_index != -1 and end_index > start_index:
                candidates.append(stripped[start_index : end_index + 1])

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen or not candidate:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

        raise RuntimeError(f"OpenClaw の返答を JSON として読めません: {self._shorten(raw_text, 140)}")

    def _normalize_decisions(
        self,
        payload: object,
        posts: list[TimelinePost],
    ) -> dict[str, AnalyzedPostDecision]:
        if isinstance(payload, dict):
            raw_results = payload.get("results")
        else:
            raw_results = payload

        if not isinstance(raw_results, list):
            raise RuntimeError("OpenClaw の返答に results 配列がありません。")

        post_map = {post.post_id: post for post in posts}
        decisions: dict[str, AnalyzedPostDecision] = {}

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            post_id = str(item.get("post_id", "")).strip()
            if not post_id or post_id not in post_map:
                continue

            raw_signals = item.get("signals", [])
            normalized_signals: list[SignalDecision] = []
            symbol_directions: dict[str, str] = {}
            conflicted_symbols: set[str] = set()

            if isinstance(raw_signals, list):
                for raw_signal in raw_signals:
                    if not isinstance(raw_signal, dict):
                        continue
                    symbol = normalize_symbol(raw_signal.get("symbol"))
                    direction = normalize_direction(raw_signal.get("direction"))
                    reason = str(raw_signal.get("reason", "")).strip() or "本文から売買意図が読めました。"
                    if not symbol or not direction:
                        continue
                    existing_direction = symbol_directions.get(symbol)
                    if existing_direction and existing_direction != direction:
                        conflicted_symbols.add(symbol)
                        continue
                    symbol_directions[symbol] = direction
                    normalized_signals.append(SignalDecision(symbol=symbol, direction=direction, reason=reason))

            if conflicted_symbols:
                normalized_signals = [
                    signal
                    for signal in normalized_signals
                    if signal.symbol not in conflicted_symbols
                ]

            deduped_signals: list[SignalDecision] = []
            seen_pairs: set[tuple[str, str]] = set()
            for signal in normalized_signals:
                pair = (signal.symbol, signal.direction)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                deduped_signals.append(signal)

            ignore_reason = str(item.get("ignore_reason", "")).strip()
            if not deduped_signals and not ignore_reason:
                ignore_reason = "本文だけではミラトレ可能な売買意図が読めません。"

            decisions[post_id] = AnalyzedPostDecision(
                post_id=post_id,
                signals=tuple(deduped_signals),
                ignore_reason=ignore_reason,
            )

        for post in posts:
            decisions.setdefault(
                post.post_id,
                AnalyzedPostDecision(
                    post_id=post.post_id,
                    signals=(),
                    ignore_reason="OpenClaw の返答にこの投稿の判定がありません。",
                ),
            )

        return decisions

    def _build_signal_rows(
        self,
        posts: list[TimelinePost],
        decisions: dict[str, AnalyzedPostDecision],
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for post in posts:
            decision = decisions[post.post_id]
            for signal in decision.signals:
                rows.append(
                    {
                        "user_id": post.user_id,
                        "user_url": post.user_url,
                        "symbol": signal.symbol,
                        "direction": signal.direction,
                        "post_id": post.post_id,
                        "post_url": post.post_url,
                        "posted_at": post.posted_at,
                        "reason": signal.reason,
                    }
                )
        return rows

    def _write_signal_batch(self, rows: list[dict[str, str]]) -> None:
        OPENCLAW_BATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPENCLAW_BATCH_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ingest_file(OPENCLAW_BATCH_PATH)

    def _send_discord_report(
        self,
        posts: list[TimelinePost],
        decisions: dict[str, AnalyzedPostDecision],
    ) -> str | None:
        if not self._openclaw_path:
            return "openclaw コマンドが見つかりません。"

        report_text = self._build_discord_report(posts, decisions)
        result = subprocess.run(
            [
                self._openclaw_path,
                "message",
                "send",
                "--channel",
                "discord",
                "--target",
                f"channel:{DISCORD_CHANNEL_ID}",
                "--message",
                report_text,
                "--silent",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0:
            return None
        return result.stderr.strip() or result.stdout.strip() or "Discord 送信に失敗しました。"

    def _build_discord_report(
        self,
        posts: list[TimelinePost],
        decisions: dict[str, AnalyzedPostDecision],
    ) -> str:
        lines = [
            f"分析時刻: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"新着ポスト数: {len(posts)}",
        ]

        signal_count = sum(len(decision.signals) for decision in decisions.values())
        if signal_count == 0:
            lines.append("直近の新着ではミラトレ可能な該当なし")
            return "\n".join(lines)

        for post in posts:
            decision = decisions[post.post_id]
            for signal in decision.signals:
                lines.append("")
                lines.append(f"- ユーザーID: {post.user_id}")
                lines.append(f"- 銘柄: {signal.symbol}")
                lines.append(f"- 方向: {signal.direction}")
                lines.append(f"- 投稿日時: {post.posted_at}")
                lines.append(f"- ポストURL: {post.post_url}")
                lines.append(f"- 判断理由: {signal.reason}")
        return "\n".join(lines)

    def _load_seen_posts(self) -> dict[str, str]:
        if not self._state_path.exists():
            return {}

        try:
            with self._state_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}

        raw_seen = payload.get("seen_posts", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_seen, dict):
            return {}

        cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_RETENTION_HOURS)
        kept: dict[str, str] = {}
        for post_id, posted_at in raw_seen.items():
            if not isinstance(post_id, str) or not isinstance(posted_at, str):
                continue
            try:
                posted_dt = datetime.fromisoformat(posted_at)
            except ValueError:
                continue
            if posted_dt.tzinfo is None:
                posted_dt = posted_dt.replace(tzinfo=timezone.utc)
            if posted_dt.astimezone(timezone.utc) < cutoff:
                continue
            kept[post_id] = posted_at
        return kept

    def _remember_posts(self, posts: list[TimelinePost], current_state: dict[str, str]) -> None:
        for post in posts:
            current_state[post.post_id] = post.posted_at

        ordered_items = sorted(
            current_state.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:SEEN_POST_LIMIT]
        payload = {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "seen_posts": {post_id: posted_at for post_id, posted_at in ordered_items},
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_post_row(
        self,
        post: TimelinePost,
        judgment: str,
        reason: str,
        emit: bool = True,
    ) -> None:
        row = XMonitorRow(
            checked_at=self._clock_text(),
            user_id=post.user_id,
            posted_at=self._format_posted_at(post.posted_at),
            text=self._shorten(post.text.replace("\n", " "), 90),
            judgment=judgment,
            reason=self._shorten(reason.replace("\n", " "), 110),
            post_url=post.post_url,
        )
        with self._history_lock:
            self._history.appendleft(row)
        if emit:
            self._emit_state()

    def _append_system_row(self, judgment: str, reason: str, emit: bool = True) -> None:
        row = XMonitorRow(
            checked_at=self._clock_text(),
            user_id="system",
            posted_at="",
            text="",
            judgment=judgment,
            reason=self._shorten(reason.replace("\n", " "), 110),
            post_url="",
        )
        with self._history_lock:
            self._history.appendleft(row)
        if emit:
            self._emit_state()

    def _judgment_text(self, decision: AnalyzedPostDecision) -> str:
        if not decision.signals:
            return "見送り"
        parts = [f"{signal.symbol} {signal.direction}" for signal in decision.signals]
        return "採用 " + " / ".join(parts)

    def _reason_text(self, decision: AnalyzedPostDecision) -> str:
        if not decision.signals:
            return decision.ignore_reason
        return " / ".join(signal.reason for signal in decision.signals)

    def _set_status(self, text: str) -> None:
        with self._status_lock:
            self._status_text = text
        self._emit_state()

    def _emit_state(self) -> None:
        if self._on_state_change is None:
            return
        status_text, history = self.get_snapshot()
        self._on_state_change(status_text, history)

    def _clock_text(self) -> str:
        return datetime.now().astimezone().strftime("%H:%M:%S")

    def _format_posted_at(self, iso_text: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_text)
        except ValueError:
            return iso_text
        return dt.astimezone().strftime("%m/%d %H:%M")

    def _shorten(self, text: str, length: int) -> str:
        if len(text) <= length:
            return text
        if length <= 3:
            return text[:length]
        return text[: length - 3].rstrip() + "..."
