from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from bridge_common import (
    OPENCLAW_POSTS_JSON_PATH,
    OPENCLAW_POSTS_TEXT_PATH,
    X_TIMELINE_COOKIE_PATH,
    ensure_runtime_layout,
    now_local_iso,
)


TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
DEFAULT_ACCOUNTS = (
    "Light_Yagami_a",
    "d_o_b46",
    "mutachan41",
    "btchakudaku",
    "cb_terminal",
)
NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


@dataclass(frozen=True)
class FetchSettings:
    accounts: tuple[str, ...]
    window_minutes: int
    json_output: Path
    text_output: Path
    cookie: str | None


@dataclass(frozen=True)
class TimelinePost:
    user_id: str
    user_url: str
    post_id: str
    post_url: str
    posted_at: str
    text: str
    is_reply: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="X の本文だけを集めて OpenClaw に渡す下準備をします。")
    parser.add_argument(
        "command",
        choices=("fetch",),
        help="本文取得を実行します。",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=20,
        help="何分前までの投稿を拾うか",
    )
    parser.add_argument(
        "--json-output",
        default=str(OPENCLAW_POSTS_JSON_PATH),
        help="取得結果 JSON の保存先",
    )
    parser.add_argument(
        "--text-output",
        default=str(OPENCLAW_POSTS_TEXT_PATH),
        help="OpenClaw に読ませる本文の保存先",
    )
    parser.add_argument(
        "--accounts",
        nargs="*",
        default=list(DEFAULT_ACCOUNTS),
        help="対象アカウント",
    )
    return parser


def load_cookie() -> str | None:
    env_value = os.environ.get("X_TIMELINE_COOKIE", "").strip()
    if env_value:
        return env_value

    if X_TIMELINE_COOKIE_PATH.exists():
        value = X_TIMELINE_COOKIE_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value

    return None


def fetch_html(opener, username: str, cookie: str | None) -> str:
    request = Request(
        TIMELINE_URL.format(username=username),
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": "https://x.com/",
            **({"Cookie": cookie} if cookie else {}),
        },
    )
    with opener.open(request, timeout=30) as response:
        return response.read().decode("utf-8")


def build_request_opener():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def extract_posts(html_text: str, username: str) -> list[TimelinePost]:
    match = NEXT_DATA_PATTERN.search(html_text)
    if match is None:
        raise ValueError(f"{username} のタイムライン本文が見つかりません。")

    payload = json.loads(match.group(1))
    entries = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("timeline", {})
        .get("entries", [])
    )

    posts: list[TimelinePost] = []
    seen_ids: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        tweet = content.get("tweet")
        if not isinstance(tweet, dict):
            continue

        post_id = str(tweet.get("id_str") or tweet.get("conversation_id_str") or "").strip()
        created_at = str(tweet.get("created_at") or "").strip()
        raw_text = tweet.get("full_text") or tweet.get("text") or ""

        if not post_id or not created_at or not isinstance(raw_text, str):
            continue
        if post_id in seen_ids:
            continue

        seen_ids.add(post_id)
        posted_at = parsedate_to_datetime(created_at).astimezone(timezone.utc).isoformat()
        posts.append(
            TimelinePost(
                user_id=username,
                user_url=f"https://x.com/{username}",
                post_id=post_id,
                post_url=f"https://x.com/{username}/status/{post_id}",
                posted_at=posted_at,
                text=html.unescape(raw_text).strip(),
                is_reply=bool(tweet.get("in_reply_to_status_id_str")),
            )
        )

    posts.sort(key=lambda item: item.posted_at, reverse=True)
    return posts


def filter_recent(posts: list[TimelinePost], window_minutes: int) -> list[TimelinePost]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    accepted: list[TimelinePost] = []
    for post in posts:
        posted_at = datetime.fromisoformat(post.posted_at)
        if posted_at >= cutoff:
            accepted.append(post)
    return accepted


def write_json(path: Path, posts: list[TimelinePost]) -> None:
    rows = [
        {
            "user_id": post.user_id,
            "user_url": post.user_url,
            "post_id": post.post_id,
            "post_url": post.post_url,
            "posted_at": post.posted_at,
            "text": post.text,
            "is_reply": post.is_reply,
        }
        for post in posts
    ]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, posts: list[TimelinePost], window_minutes: int, cookie_used: bool, errors: list[str]) -> None:
    lines: list[str] = []
    lines.append("OpenClaw に渡す X ポスト一覧")
    lines.append(f"取得時刻: {now_local_iso()}")
    lines.append(f"対象時間: 直近 {window_minutes} 分")
    lines.append(f"Cookie使用: {'yes' if cookie_used else 'no'}")
    lines.append("")

    if errors:
        lines.append("取得エラー:")
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    if not posts:
        lines.append(f"直近 {window_minutes} 分の対象ポストはありません。")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines.append(f"対象ポスト数: {len(posts)}")
    lines.append("")

    for index, post in enumerate(posts, start=1):
        lines.append(f"[{index}]")
        lines.append(f"ユーザーID: {post.user_id}")
        lines.append(f"ユーザーURL: {post.user_url}")
        lines.append(f"投稿ID: {post.post_id}")
        lines.append(f"投稿URL: {post.post_url}")
        lines.append(f"投稿日時: {post.posted_at}")
        lines.append(f"リプライ: {'yes' if post.is_reply else 'no'}")
        lines.append("本文:")
        lines.append(post.text if post.text else "(本文なし)")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_posts(settings: FetchSettings) -> tuple[list[TimelinePost], list[str]]:
    recent_posts: list[TimelinePost] = []
    errors: list[str] = []
    opener = build_request_opener()

    for username in settings.accounts:
        try:
            html_text = fetch_html(opener, username, settings.cookie)
            posts = extract_posts(html_text, username)
            recent_posts.extend(filter_recent(posts, settings.window_minutes))
            time.sleep(1)
        except HTTPError as exc:
            if exc.code == 429:
                time.sleep(3)
                try:
                    html_text = fetch_html(opener, username, settings.cookie)
                    posts = extract_posts(html_text, username)
                    recent_posts.extend(filter_recent(posts, settings.window_minutes))
                    time.sleep(1)
                    continue
                except Exception:
                    pass
            errors.append(f"{username}: HTTP {exc.code}")
        except URLError as exc:
            errors.append(f"{username}: 通信失敗 {exc.reason}")
        except Exception as exc:
            errors.append(f"{username}: {exc}")

    deduped: dict[str, TimelinePost] = {}
    for post in recent_posts:
        deduped.setdefault(post.post_id, post)

    posts = sorted(deduped.values(), key=lambda item: item.posted_at, reverse=True)
    return posts, errors


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command != "fetch":
        parser.error("未対応のコマンドです。")
        return 2

    ensure_runtime_layout()
    settings = FetchSettings(
        accounts=tuple(dict.fromkeys(str(account).strip() for account in args.accounts if str(account).strip())),
        window_minutes=max(1, args.window_minutes),
        json_output=Path(args.json_output),
        text_output=Path(args.text_output),
        cookie=load_cookie(),
    )

    try:
        posts, errors = fetch_posts(settings)
        settings.json_output.parent.mkdir(parents=True, exist_ok=True)
        settings.text_output.parent.mkdir(parents=True, exist_ok=True)
        write_json(settings.json_output, posts)
        write_text(settings.text_output, posts, settings.window_minutes, bool(settings.cookie), errors)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"posts={len(posts)} cookie={'yes' if settings.cookie else 'no'} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
