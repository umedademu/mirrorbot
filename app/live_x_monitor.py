from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from bridge_common import (
    OPENCLAW_BATCH_PATH,
    OPENCLAW_MONITOR_STATE_PATH,
    OPENCLAW_SOURCE_POSTS_PATH,
    ensure_runtime_layout,
    normalize_direction,
    normalize_symbol,
)
from openclaw_signal_ingest import ingest_file


DISCORD_CHANNEL_ID = "1483783703174971442"
DISCORD_APP_ID_TWEETSHIFT = "713026372142104687"
POLL_INTERVAL_SECONDS = 15
FETCH_MESSAGE_LIMIT = 50
TEXT_CONTEXT_LIMIT = 4
RECENT_CACHE_LIMIT = 120
TEXT_CONTEXT_LOOKBACK_HOURS = 24
OPENCLAW_TEXT_SESSION_ID = "mirrorbot-discord-watch"
OPENCLAW_BROWSER_SESSION_ID = "mirrorbot-discord-browser-review"
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
STATUS_URL_RE = re.compile(r"https?://(?:x|twitter)\.com/(?P<user>[^/\s]+)/status/(?P<post_id>\d+)", flags=re.IGNORECASE)
AUTHOR_HANDLE_RE = re.compile(r"\(@(?P<user>[^)]+)\)")
IMAGE_URL_RE = re.compile(r"^https?://(?:pbs\.twimg\.com|images-ext-1\.discordapp\.net)/", flags=re.IGNORECASE)


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
    needs_browser_review: bool = False
    browser_review_reason: str = ""
    used_browser_review: bool = False


@dataclass(frozen=True)
class XMonitorRow:
    checked_at: str
    user_id: str
    posted_at: str
    text: str
    judgment: str
    reason: str
    post_url: str


@dataclass(frozen=True)
class SourcePost:
    discord_message_id: str
    discord_message_time: str
    user_id: str
    user_url: str
    post_id: str
    post_url: str
    posted_at: str
    text: str
    image_urls: tuple[str, ...]


class OpenClawLiveMonitor:
    def __init__(
        self,
        on_state_change: Callable[[str, tuple[XMonitorRow, ...]], None] | None = None,
    ) -> None:
        ensure_runtime_layout()
        self._on_state_change = on_state_change
        self._status_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._status_text = "Discord監視: 起動準備中"
        self._history: deque[XMonitorRow] = deque(maxlen=80)
        self._state_path = OPENCLAW_MONITOR_STATE_PATH
        self._openclaw_path = shutil.which("openclaw")
        self._initial_sync_needed = not self._state_path.exists()
        self._last_message_id = self._load_last_message_id()
        self._recent_post_map: dict[str, SourcePost] = {}
        self._discord_token = ""
        self._setup_error = ""
        try:
            self._discord_token = self._load_discord_token()
        except Exception as exc:
            self._setup_error = str(exc)

    def get_snapshot(self) -> tuple[str, tuple[XMonitorRow, ...]]:
        with self._status_lock:
            status_text = self._status_text
        with self._history_lock:
            history = tuple(self._history)
        return status_text, history

    def run(self, stop_event: threading.Event) -> None:
        self._set_status("Discord監視: 監視を開始")
        while not stop_event.is_set():
            self._run_cycle()
            if stop_event.wait(POLL_INTERVAL_SECONDS):
                break
        self._set_status("Discord監視: 停止")

    def _run_cycle(self) -> None:
        try:
            messages = self._fetch_channel_messages()
            newest_message_id = self._max_message_id(messages)
            source_posts = self._extract_source_posts(messages)
            self._merge_recent_posts(source_posts)
            self._write_source_posts_debug(source_posts)
        except Exception as exc:
            self._append_system_row("取得失敗", str(exc))
            self._set_status(f"Discord監視: 取得失敗 {self._shorten(str(exc), 80)}")
            return

        ordered_posts = sorted(source_posts, key=self._post_sort_key)

        if self._initial_sync_needed:
            if ordered_posts:
                self._remember_message_id(newest_message_id)
                initial_rows = ordered_posts[-min(len(ordered_posts), 12) :]
                for post in initial_rows:
                    self._append_post_row(
                        post,
                        "初回同期",
                        "起動直後のため既読扱いにしました。",
                        emit=False,
                    )
                self._emit_state()
                self._set_status(f"Discord監視: 初回同期完了 {len(ordered_posts)} 件")
            else:
                self._remember_message_id(newest_message_id)
                self._set_status("Discord監視: 初回同期完了 0 件")
            self._initial_sync_needed = False
            return

        new_posts = [post for post in ordered_posts if self._is_newer_message(post.discord_message_id)]
        if newest_message_id:
            self._remember_message_id(newest_message_id)

        if not new_posts:
            self._set_status(f"Discord監視: 新着なし {self._clock_text()}")
            return

        try:
            decisions = self._analyze_posts(new_posts)
            signal_rows = self._build_signal_rows(new_posts, decisions)
            self._write_signal_batch(signal_rows)
        except Exception as exc:
            self._append_system_row("解釈失敗", str(exc))
            self._set_status(f"Discord監視: 解釈失敗 {self._shorten(str(exc), 80)}")
            return

        for post in new_posts:
            decision = decisions[post.post_id]
            self._append_post_row(
                post,
                self._judgment_text(decision),
                self._reason_text(decision),
                emit=False,
            )
        self._emit_state()

        discord_error = self._send_discord_report(new_posts, decisions)
        if discord_error:
            self._append_system_row("Discord失敗", discord_error)

        adopted_count = sum(len(decision.signals) for decision in decisions.values())
        reviewed_count = sum(1 for decision in decisions.values() if decision.used_browser_review)
        status_text = f"Discord監視: 新着{len(new_posts)}件 / 採用{adopted_count}件"
        if reviewed_count:
            status_text += f" / 精査{reviewed_count}件"
        if discord_error:
            status_text += " / Discord失敗"
        self._set_status(status_text)

    def _load_discord_token(self) -> str:
        if not OPENCLAW_CONFIG_PATH.exists():
            raise RuntimeError(f"OpenClaw の設定が見つかりません: {OPENCLAW_CONFIG_PATH}")
        try:
            payload = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"OpenClaw の設定を読めません: {exc}") from exc

        token = payload.get("channels", {}).get("discord", {}).get("token")
        token_text = str(token).strip() if token is not None else ""
        if not token_text:
            raise RuntimeError("OpenClaw の Discord トークンが設定されていません。")
        return token_text

    def _fetch_channel_messages(self) -> list[dict[str, Any]]:
        if not self._discord_token:
            try:
                self._discord_token = self._load_discord_token()
                self._setup_error = ""
            except Exception as exc:
                self._setup_error = str(exc)
                raise RuntimeError(self._setup_error) from exc

        query = urllib.parse.urlencode({"limit": FETCH_MESSAGE_LIMIT})
        url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {self._discord_token}",
                "User-Agent": "mirrorbot-discord-monitor",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body, dict):
                    retry_after = body.get("retry_after")
                    message = body.get("message")
                    if retry_after is not None:
                        detail = f" {retry_after} 秒待って再試行が必要です。"
                    elif message:
                        detail = f" {message}"
            except Exception:
                detail = ""
            raise RuntimeError(f"Discord の取得に失敗しました: HTTP {exc.code}.{detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"Discord の取得に失敗しました: {exc}") from exc

        if not isinstance(payload, list):
            raise RuntimeError("Discord の返答が配列ではありません。")

        return [item for item in payload if isinstance(item, dict)]

    def _extract_source_posts(self, messages: list[dict[str, Any]]) -> list[SourcePost]:
        posts: list[SourcePost] = []
        for message in messages:
            post = self._parse_source_post(message)
            if post is not None:
                posts.append(post)
        return posts

    def _parse_source_post(self, message: dict[str, Any]) -> SourcePost | None:
        if not self._is_tweetshift_message(message):
            return None

        message_id = str(message.get("id", "")).strip()
        message_time = str(message.get("timestamp", "")).strip()
        if not message_id or not message_time:
            return None

        content = str(message.get("content", "")).strip()
        embeds = [embed for embed in message.get("embeds", []) if isinstance(embed, dict)]
        primary_embed = self._find_primary_embed(embeds)

        post_url = self._extract_post_url(content, embeds)
        if not post_url:
            return None

        status_match = STATUS_URL_RE.search(post_url)
        if not status_match:
            return None
        post_id = status_match.group("post_id")

        user_url = self._extract_user_url(primary_embed, status_match.group("user"))
        user_id = self._extract_user_id(primary_embed, user_url, status_match.group("user"))
        if not user_id:
            return None

        posted_at = self._extract_posted_at(primary_embed, message_time)
        text = self._extract_text(content, primary_embed)
        image_urls = self._extract_image_urls(message, embeds)

        return SourcePost(
            discord_message_id=message_id,
            discord_message_time=self._normalize_iso(message_time),
            user_id=user_id,
            user_url=user_url,
            post_id=post_id,
            post_url=post_url,
            posted_at=posted_at,
            text=text,
            image_urls=image_urls,
        )

    def _is_tweetshift_message(self, message: dict[str, Any]) -> bool:
        application_id = str(message.get("application_id", "")).strip()
        author_name = str(message.get("author", {}).get("username", "")).strip()
        return application_id == DISCORD_APP_ID_TWEETSHIFT or "TweetShift" in author_name

    def _find_primary_embed(self, embeds: list[dict[str, Any]]) -> dict[str, Any] | None:
        for embed in embeds:
            url = str(embed.get("url", "")).strip()
            if STATUS_URL_RE.search(url):
                return embed
        return embeds[0] if embeds else None

    def _extract_post_url(self, content: str, embeds: list[dict[str, Any]]) -> str:
        for embed in embeds:
            url = str(embed.get("url", "")).strip()
            if STATUS_URL_RE.search(url):
                return url.replace("twitter.com/", "x.com/")

        match = STATUS_URL_RE.search(content)
        if not match:
            return ""
        return match.group(0).replace("twitter.com/", "x.com/")

    def _extract_user_url(self, primary_embed: dict[str, Any] | None, fallback_user_id: str) -> str:
        if primary_embed is not None:
            author_url = str(primary_embed.get("author", {}).get("url", "")).strip()
            if author_url:
                return author_url.replace("twitter.com/", "x.com/")
        return f"https://x.com/{fallback_user_id}"

    def _extract_user_id(self, primary_embed: dict[str, Any] | None, user_url: str, fallback_user_id: str) -> str:
        if primary_embed is not None:
            author_name = str(primary_embed.get("author", {}).get("name", "")).strip()
            match = AUTHOR_HANDLE_RE.search(author_name)
            if match:
                return match.group("user")

        cleaned = user_url.rstrip("/").split("/")[-1].strip()
        if cleaned:
            return cleaned
        return fallback_user_id

    def _extract_posted_at(self, primary_embed: dict[str, Any] | None, fallback_time: str) -> str:
        if primary_embed is not None:
            timestamp = str(primary_embed.get("timestamp", "")).strip()
            if timestamp:
                return self._normalize_iso(timestamp)
        return self._normalize_iso(fallback_time)

    def _extract_text(self, content: str, primary_embed: dict[str, Any] | None) -> str:
        if primary_embed is not None:
            description = str(primary_embed.get("description", "")).strip()
            if description:
                return description

        text = STATUS_URL_RE.sub("", content)
        text = re.sub(r"\[[^\]]+\]\((https?://[^)]+)\)", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_image_urls(self, message: dict[str, Any], embeds: list[dict[str, Any]]) -> tuple[str, ...]:
        urls: list[str] = []

        for attachment in message.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            url = str(attachment.get("url", "")).strip()
            if url:
                urls.append(url)

        for embed in embeds:
            for key in ("image", "thumbnail", "video"):
                nested = embed.get(key)
                if not isinstance(nested, dict):
                    continue
                url = str(nested.get("url", "")).strip()
                if url and IMAGE_URL_RE.search(url):
                    urls.append(url)

        unique: list[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = url.replace(":large", "")
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(url)
        return tuple(unique)

    def _analyze_posts(self, posts: list[SourcePost]) -> dict[str, AnalyzedPostDecision]:
        raw_response = self._run_openclaw_agent(
            prompt=self._build_text_prompt(posts),
            session_id=OPENCLAW_TEXT_SESSION_ID,
            timeout_seconds=90,
            thinking_level="minimal",
        )
        payload = self._extract_json_payload(raw_response)
        decisions = self._normalize_decisions(payload, posts, allow_browser_review=True)

        review_posts = [post for post in posts if decisions[post.post_id].needs_browser_review]
        if not review_posts:
            return decisions

        self._set_status(f"Discord監視: 追加精査 {len(review_posts)} 件")
        try:
            review_response = self._run_openclaw_agent(
                prompt=self._build_browser_review_prompt(review_posts, decisions),
                session_id=OPENCLAW_BROWSER_SESSION_ID,
                timeout_seconds=180,
                thinking_level="low",
            )
            review_payload = self._extract_json_payload(review_response)
            reviewed = self._normalize_decisions(review_payload, review_posts, allow_browser_review=False)
        except Exception as exc:
            for post in review_posts:
                previous = decisions[post.post_id]
                decisions[post.post_id] = AnalyzedPostDecision(
                    post_id=post.post_id,
                    signals=previous.signals,
                    ignore_reason=f"追加精査に失敗しました: {self._shorten(str(exc), 80)}",
                    needs_browser_review=False,
                    browser_review_reason="",
                    used_browser_review=True,
                )
            return decisions

        for post in review_posts:
            reviewed_decision = reviewed.get(post.post_id)
            if reviewed_decision is None:
                continue
            decisions[post.post_id] = AnalyzedPostDecision(
                post_id=post.post_id,
                signals=reviewed_decision.signals,
                ignore_reason=reviewed_decision.ignore_reason,
                needs_browser_review=False,
                browser_review_reason="",
                used_browser_review=True,
            )
        return decisions

    def _build_text_prompt(self, posts: list[SourcePost]) -> str:
        input_rows = []
        for post in posts:
            input_rows.append(
                {
                    "post_id": post.post_id,
                    "user_id": post.user_id,
                    "user_url": post.user_url,
                    "post_url": post.post_url,
                    "posted_at": post.posted_at,
                    "text": post.text,
                    "image_urls": list(post.image_urls),
                    "recent_same_user_posts": [
                        {
                            "post_id": context.post_id,
                            "post_url": context.post_url,
                            "posted_at": context.posted_at,
                            "text": context.text,
                        }
                        for context in self._find_context_posts(post)
                    ],
                }
            )

        return (
            "あなたの役目は、Discord に届いた X の新着通知だけを読んで、mirrorbot がミラトレすべき投稿だけを選ぶことです。\n"
            "目的は相場観の分類ではなく、投稿者が実際に行っている売買をできるだけなぞることです。\n"
            "ここでは BrowserRelay は使わず、本文と recent_same_user_posts だけで判断してください。\n"
            "image_urls は画像があることを示すだけで、画像そのものは見えません。\n"
            "次の 3 種類だけ、追加精査が必要なら needs_browser_review を true にしてください。\n"
            "1. 銘柄が分からない\n"
            "2. 上下どちらかが分からない\n"
            "3. 指値、利確、追加、反転などの意味が本文だけでは曖昧で、画像や過去投稿を見れば判定できそう\n"
            "ただし、単なる感想、煽り、実況、事後コメントでしかなく、精査してもミラトレ対象にならないものは needs_browser_review を false にしてください。\n"
            "同じ投稿で複数銘柄が明確なら複数件採用して構いません。ただし同じ銘柄で矛盾した方向は出さないでください。\n"
            "対象銘柄は USDJPYm, EURUSDm, JP225m, USOILm, XAUUSDm, XAGUSDm, BTCUSDm, ETHUSDm の 8 つだけです。\n"
            "方向は bull か bear のどちらかだけです。\n"
            "たとえば ドル円 -> USDJPYm、ユーロドル -> EURUSDm、日経 -> JP225m、原油 -> USOILm、金やゴールド -> XAUUSDm、銀やシルバー -> XAGUSDm、BTCやビットコイン -> BTCUSDm、ETHやイーサリアム -> ETHUSDm です。\n"
            "返答は JSON だけにしてください。説明文や markdown は不要です。\n"
            "必ず入力投稿ごとに 1 件ずつ results に入れてください。\n"
            "signals が空配列のときは ignore_reason に見送り理由を書いてください。\n"
            "signals が 1 件以上あるときは ignore_reason を空文字にしてください。\n"
            "needs_browser_review が true のときは browser_review_reason を短く具体的に書いてください。\n"
            "返答の形は次だけです。\n"
            "{\n"
            '  "results": [\n'
            '    {\n'
            '      "post_id": "入力の post_id",\n'
            '      "signals": [\n'
            '        {"symbol": "USDJPYm", "direction": "bull", "reason": "短い根拠"}\n'
            "      ],\n"
            '      "ignore_reason": "",\n'
            '      "needs_browser_review": false,\n'
            '      "browser_review_reason": ""\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "入力投稿一覧:\n"
            f"{json.dumps(input_rows, ensure_ascii=False, indent=2)}"
        )

    def _build_browser_review_prompt(
        self,
        posts: list[SourcePost],
        decisions: dict[str, AnalyzedPostDecision],
    ) -> str:
        review_rows = []
        for post in posts:
            review_rows.append(
                {
                    "post_id": post.post_id,
                    "user_id": post.user_id,
                    "post_url": post.post_url,
                    "posted_at": post.posted_at,
                    "text_from_discord": post.text,
                    "image_urls": list(post.image_urls),
                    "browser_review_reason": decisions[post.post_id].browser_review_reason,
                    "recent_same_user_posts": [
                        {
                            "post_id": context.post_id,
                            "post_url": context.post_url,
                            "posted_at": context.posted_at,
                            "text": context.text,
                        }
                        for context in self._find_context_posts(post)
                    ],
                }
            )

        return (
            "BrowserRelay を使って、次の投稿だけを追加精査してください。\n"
            "ブラウザ操作は必ず 1 つのタブだけを使い、順番に処理してください。\n"
            "まず Discord から渡された text_from_discord と recent_same_user_posts を見てください。\n"
            "それでも曖昧な場合だけ、post_url を開いて現在の投稿を確認してください。\n"
            "image_urls がある場合は、必要ならその画像 URL を直接開いて確認して構いません。\n"
            "それでも足りない場合だけ、同じ投稿者の少し前の投稿を必要最小限だけさかのぼってください。\n"
            "目的は相場観の分類ではなく、投稿者が実際に行っている売買を mirrorbot がなぞれるかを判断することです。\n"
            "単なる感想、実況、事後コメントなら見送りにしてください。\n"
            "対象銘柄は USDJPYm, EURUSDm, JP225m, USOILm, XAUUSDm, XAGUSDm, BTCUSDm, ETHUSDm の 8 つだけです。\n"
            "方向は bull か bear のどちらかだけです。\n"
            "返答は JSON だけにしてください。説明文や markdown は不要です。\n"
            "必ず入力投稿ごとに 1 件ずつ results に入れてください。\n"
            "signals が空配列のときは ignore_reason に見送り理由を書いてください。\n"
            "返答の形は次だけです。\n"
            "{\n"
            '  "results": [\n'
            '    {\n'
            '      "post_id": "入力の post_id",\n'
            '      "signals": [\n'
            '        {"symbol": "USDJPYm", "direction": "bull", "reason": "短い根拠"}\n'
            "      ],\n"
            '      "ignore_reason": ""\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "追加精査対象:\n"
            f"{json.dumps(review_rows, ensure_ascii=False, indent=2)}"
        )

    def _find_context_posts(self, target: SourcePost) -> list[SourcePost]:
        target_dt = self._post_datetime(target)
        cutoff = target_dt - timedelta(hours=TEXT_CONTEXT_LOOKBACK_HOURS)
        candidates = [
            post
            for post in self._recent_post_map.values()
            if post.user_id == target.user_id
            and post.post_id != target.post_id
            and cutoff <= self._post_datetime(post) <= target_dt
        ]
        ordered = sorted(candidates, key=self._post_sort_key)
        return ordered[-TEXT_CONTEXT_LIMIT:]

    def _run_openclaw_agent(
        self,
        prompt: str,
        session_id: str,
        timeout_seconds: int,
        thinking_level: str,
    ) -> str:
        if not self._openclaw_path:
            raise RuntimeError("openclaw コマンドが見つかりません。")

        result = subprocess.run(
            [
                self._openclaw_path,
                "agent",
                "--json",
                "--session-id",
                session_id,
                "--message",
                prompt,
                "--thinking",
                thinking_level,
                "--timeout",
                str(timeout_seconds),
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
        posts: list[SourcePost],
        allow_browser_review: bool,
    ) -> dict[str, AnalyzedPostDecision]:
        raw_results = payload.get("results") if isinstance(payload, dict) else payload
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
            needs_browser_review = False
            browser_review_reason = ""
            if allow_browser_review:
                needs_browser_review = bool(item.get("needs_browser_review", False))
                browser_review_reason = str(item.get("browser_review_reason", "")).strip()

            if deduped_signals:
                ignore_reason = ""
                needs_browser_review = False
                browser_review_reason = ""
            elif not ignore_reason:
                ignore_reason = "本文だけではミラトレ可能な売買意図が読めません。"

            decisions[post_id] = AnalyzedPostDecision(
                post_id=post_id,
                signals=tuple(deduped_signals),
                ignore_reason=ignore_reason,
                needs_browser_review=needs_browser_review,
                browser_review_reason=browser_review_reason,
                used_browser_review=not allow_browser_review,
            )

        for post in posts:
            decisions.setdefault(
                post.post_id,
                AnalyzedPostDecision(
                    post_id=post.post_id,
                    signals=(),
                    ignore_reason="OpenClaw の返答にこの投稿の判定がありません。",
                    needs_browser_review=False,
                    browser_review_reason="",
                    used_browser_review=not allow_browser_review,
                ),
            )

        return decisions

    def _build_signal_rows(
        self,
        posts: list[SourcePost],
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

    def _write_source_posts_debug(self, posts: list[SourcePost]) -> None:
        payload = [
            {
                "discord_message_id": post.discord_message_id,
                "user_id": post.user_id,
                "post_id": post.post_id,
                "post_url": post.post_url,
                "posted_at": post.posted_at,
                "text": post.text,
                "image_urls": list(post.image_urls),
            }
            for post in sorted(posts, key=self._post_sort_key, reverse=True)
        ]
        OPENCLAW_SOURCE_POSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPENCLAW_SOURCE_POSTS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _send_discord_report(
        self,
        posts: list[SourcePost],
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
        posts: list[SourcePost],
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

    def _load_last_message_id(self) -> str:
        if not self._state_path.exists():
            return ""

        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return ""

        if not isinstance(payload, dict):
            return ""

        return str(payload.get("last_message_id", "")).strip()

    def _remember_message_id(self, message_id: str) -> None:
        if not message_id:
            return
        self._last_message_id = message_id
        payload = {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_message_id": message_id,
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _merge_recent_posts(self, posts: list[SourcePost]) -> None:
        for post in posts:
            self._recent_post_map[post.post_id] = post

        ordered = sorted(self._recent_post_map.values(), key=self._post_sort_key, reverse=True)[:RECENT_CACHE_LIMIT]
        self._recent_post_map = {post.post_id: post for post in ordered}

    def _append_post_row(
        self,
        post: SourcePost,
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
        prefix = "精査後 " if decision.used_browser_review else ""
        if not decision.signals:
            return prefix + "見送り"
        parts = [f"{signal.symbol} {signal.direction}" for signal in decision.signals]
        return prefix + "採用 " + " / ".join(parts)

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

    def _normalize_iso(self, iso_text: str) -> str:
        normalized = iso_text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()

    def _post_datetime(self, post: SourcePost) -> datetime:
        return datetime.fromisoformat(post.posted_at).astimezone(timezone.utc)

    def _post_sort_key(self, post: SourcePost) -> tuple[str, int]:
        return (post.posted_at, self._safe_int(post.discord_message_id))

    def _max_message_id(self, messages: list[dict[str, Any]]) -> str:
        max_id = 0
        for message in messages:
            max_id = max(max_id, self._safe_int(message.get("id")))
        return str(max_id) if max_id else ""

    def _is_newer_message(self, message_id: str) -> bool:
        if not self._last_message_id:
            return True
        return self._safe_int(message_id) > self._safe_int(self._last_message_id)

    def _safe_int(self, value: object) -> int:
        try:
            return int(str(value).strip())
        except Exception:
            return 0

    def _shorten(self, text: str, length: int) -> str:
        if len(text) <= length:
            return text
        if length <= 3:
            return text[:length]
        return text[: length - 3].rstrip() + "..."
