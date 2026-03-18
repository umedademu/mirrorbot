from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bridge_common import (
    OPENCLAW_INBOX_PATH,
    ensure_runtime_layout,
    normalize_direction,
    normalize_symbol,
    now_local_iso,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw からのシグナルを inbox に追記します。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="JSON 配列を読み込んで inbox に追記します。")
    ingest.add_argument("--input", required=True, help="入力 JSON ファイルのパス")
    return parser


def normalize_record(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None

    user_id = str(raw.get("user_id", "")).strip()
    symbol = normalize_symbol(raw.get("symbol"))
    direction = normalize_direction(raw.get("direction"))
    post_id = str(raw.get("post_id", "")).strip()
    post_url = str(raw.get("post_url", "")).strip()

    if not user_id or not symbol or not direction or not post_id or not post_url:
        return None

    return {
        "user_id": user_id,
        "user_url": str(raw.get("user_url", "")).strip(),
        "symbol": symbol,
        "direction": direction,
        "post_id": post_id,
        "post_url": post_url,
        "posted_at": str(raw.get("posted_at", "")).strip(),
        "reason": str(raw.get("reason", "")).strip(),
        "ingested_at": now_local_iso(),
        "source": "openclaw",
    }


def ingest_file(input_path: Path) -> int:
    ensure_runtime_layout()
    with input_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, list):
        raise ValueError("入力 JSON は配列である必要があります。")

    accepted: list[dict[str, object]] = []
    for item in payload:
        normalized = normalize_record(item)
        if normalized is not None:
            accepted.append(normalized)

    with OPENCLAW_INBOX_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        for row in accepted:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(accepted)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "ingest":
            count = ingest_file(Path(args.input))
        else:
            parser.error("未対応のコマンドです。")
            return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"ingested={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
