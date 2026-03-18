OpenClaw と mirrorbot の連携メモ

目的

- `app/main.py` の起動中に軽い X 監視を回し、新着投稿だけを OpenClaw に渡す
- OpenClaw に渡す前に、X の本文だけを軽く取得する
- 本文取得は `https://syndication.twitter.com` の埋め込みタイムラインを使い、BrowserRelay は使わない
- OpenClaw の判定基準は、単なる感情や相場観ではなく、その投稿者が実際に行っている売買を mirrorbot が概ねなぞれるかどうかに置く
- mirrorbot はその受け取りに反応して MT5 へ発注する
- 取引の出入りは SQLite に残し、OpenClaw からの受け取りは jsonl で受ける

構成

- mirrorbot 側
  - `app/main.py` の起動中に `app/live_x_monitor.py` が約 30 秒ごとに本文取得を回す
  - 初回起動時の最初の 1 回は、直近投稿を既読扱いにして誤発注を避ける
  - 2 回目以降は新着投稿だけを OpenClaw に渡して解釈させる
  - OpenClaw には本文だけを渡し、銘柄と方向、見送り理由を JSON で返させる
  - `app/runtime/openclaw/signal_batch.json` に JSON 配列で保存し、そのあと `app/openclaw_signal_ingest.py` で `signal_inbox.jsonl` に追記する
  - 毎回の分析結果を Discord の `ミラトレbot` チャンネルへ送る
  - `app/main.py` の監視ループ内で `signal_inbox.jsonl` を読む
  - 重複を除いたうえで SQLite に記録する
  - 自動売買が有効なときだけ MT5 へ新規発注する
  - 同じユーザーと同じ銘柄で反対方向の建玉がある場合は先に決済する
  - 決済が確認できたら `out_at` と `out_price` を埋める
  - 画面下部に、最近読み込んだ投稿と判定結果、見送り理由を表示する
- 補助経路
  - 画面を閉じている間も定期実行したい場合だけ、`app/setup_openclaw_cron.py` で 20 分実行を登録できる

ファイル

- `app/trade_bridge.py`
  - 受け取り、重複防止、SQLite 記録、MT5 発注、決済同期
- `app/live_x_monitor.py`
  - `main.py` 起動中の本文監視、新着判定、OpenClaw 呼び出し、Discord 報告
- `app/bridge_common.py`
  - 保存先、銘柄名の正規化、方向の正規化
- `app/openclaw_signal_ingest.py`
  - OpenClaw から渡された JSON 配列を jsonl inbox に追記
- `app/openclaw_x_text_fetch.py`
  - X の本文だけを集めて OpenClaw に渡す一覧を作る
- `app/setup_openclaw_cron.py`
  - OpenClaw の cron 登録を同期
- `app/openclaw_cron_prompt.txt`
  - OpenClaw に 20 分ごとに読ませる本文
- `app/x_timeline_cookie.txt`
  - 任意。鮮度を上げたいときに使う Cookie 文字列の置き場
- `app/x_timeline_cookie.example.txt`
  - Cookie 文字列の書式見本
- `app/trade_settings.json`
  - 自動売買の開始停止、数量、許容ずれ

保存先

- 受け取り inbox
  - `app/runtime/openclaw/signal_inbox.jsonl`
- OpenClaw の一時 JSON
  - `app/runtime/openclaw/signal_batch.json`
- OpenClaw へ渡す本文一覧
  - `app/runtime/openclaw/x_posts.txt`
- X 本文の生データ JSON
  - `app/runtime/openclaw/x_posts.json`
- 既読管理
  - `app/runtime/openclaw/x_monitor_state.json`
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

1. OpenClaw Gateway を起動しておく
2. 必要なら `app/x_timeline_cookie.txt` に Cookie 文字列を置く
3. 必要なら `app/trade_settings.json` で数量などを調整する
4. `python app/main.py` で mirrorbot を起動する
5. 画面下部の X 一覧に、読み込んだ投稿と判定結果が出ることを確認する
6. 画面右上のボタンで自動売買を開始する
7. 画面右上の `数量設定` で共通数量と銘柄ごとの数量を保存する
8. 画面を閉じている間も定期監視したい場合だけ `python app/setup_openclaw_cron.py` を使う

注意

- 初期状態では自動売買は停止
- 数量設定画面では、各銘柄が空欄なら共通数量を使い、共通数量も空欄なら MT5 側の最小数量を使う
- OpenClaw 側の X 解析結果がゼロ件なら inbox には何も追記されない
- Cookie を置かずに動かすと、X 側の取得結果が古くなったり欠けたりすることがある
- 起動直後の最初の 1 回は、直近投稿を既読扱いにするため、新規発注は行わない
