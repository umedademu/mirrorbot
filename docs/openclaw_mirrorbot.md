OpenClaw と mirrorbot の連携メモ

目的

- OpenClaw が 20 分ごとに X を見て、上目線と下目線の発言だけを mirrorbot に渡す
- mirrorbot はその受け取りに反応して MT5 へ発注する
- 取引の出入りは SQLite に残し、OpenClaw からの受け取りは jsonl で受ける

構成

- OpenClaw 側
  - `~/.openclaw/cron/jobs.json` に `mirrorbot-x-watch` を登録
  - 20 分ごとに BrowserRelay を使って指定 4 アカウントを確認
  - 結果を `C:\Users\user\Desktop\mirrorbot\app\runtime\openclaw\signal_batch.json` に JSON 配列で保存
  - そのあと `app/openclaw_signal_ingest.py` を呼び、`signal_inbox.jsonl` に追記
  - 毎回の分析結果を Discord の `ミラトレbot` チャンネルへ送る
- mirrorbot 側
  - `app/main.py` の監視ループ内で `signal_inbox.jsonl` を読む
  - 重複を除いたうえで SQLite に記録する
  - 自動売買が有効なときだけ MT5 へ新規発注する
  - 同じユーザーと同じ銘柄で反対方向の建玉がある場合は先に決済する
  - 決済が確認できたら `out_at` と `out_price` を埋める

ファイル

- `app/trade_bridge.py`
  - 受け取り、重複防止、SQLite 記録、MT5 発注、決済同期
- `app/bridge_common.py`
  - 保存先、銘柄名の正規化、方向の正規化
- `app/openclaw_signal_ingest.py`
  - OpenClaw から渡された JSON 配列を jsonl inbox に追記
- `app/setup_openclaw_cron.py`
  - OpenClaw の cron 登録を同期
- `app/openclaw_cron_prompt.txt`
  - OpenClaw に 20 分ごとに読ませる本文
- `app/trade_settings.json`
  - 自動売買の開始停止、数量、許容ずれ

保存先

- 受け取り inbox
  - `app/runtime/openclaw/signal_inbox.jsonl`
- OpenClaw の一時 JSON
  - `app/runtime/openclaw/signal_batch.json`
- mirrorbot の SQLite
  - `app/runtime/mirrorbot.db`

SQLite の主な列

- `user_id`
- `symbol`
- `direction`
- `in_at`
- `in_price`
- `out_at`
- `out_price`

そのほかに、重複防止や追跡のために以下も持つ

- `source_post_id`
- `source_post_url`
- `source_user_url`
- `post_at`
- `reason`
- `state`
- `ticket`
- `last_error`

使い方

1. BrowserRelay が使える状態で OpenClaw Gateway を起動しておく
2. 必要なら `app/trade_settings.json` で数量などを調整する
3. `python app/setup_openclaw_cron.py` で cron を再同期する
4. `python app/main.py` で mirrorbot を起動する
5. 画面右上のボタンで自動売買を開始する
6. 画面右上の `数量設定` で共通数量と銘柄ごとの数量を保存する

注意

- 初期状態では自動売買は停止
- 数量設定画面では、各銘柄が空欄なら共通数量を使い、共通数量も空欄なら MT5 側の最小数量を使う
- OpenClaw 側の X 解析結果がゼロ件なら inbox には何も追記されない
- BrowserRelay の接続や X 側の表示状態によっては OpenClaw の解析が失敗することがある
