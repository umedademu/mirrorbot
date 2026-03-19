OpenClaw と mirrorbot の連携メモ

目的

- `app/main.py` の起動中に Discord の `#ミラトレ` を監視し、新着通知だけを OpenClaw に渡す
- `main.py` の単独起動だけで監視から売買まで進める
- 通常は本文と同じ投稿者の直近通知だけで軽く解釈し、曖昧なときだけ BrowserRelay を使う
- Discord 通知に画像が付いている場合は、その画像 URL も追加精査で使えるようにする
- OpenClaw の判定基準は、単なる感情や相場観ではなく、その投稿者が実際に行っている売買を mirrorbot が概ねなぞれるかどうかに置く
- mirrorbot はその受け取りに反応して MT5 へ発注する
- 取引の出入りは SQLite に残し、OpenClaw からの受け取りは jsonl で受ける

構成

- mirrorbot 側
  - `app/main.py` の起動中に `app/live_x_monitor.py` が約 15 秒ごとに Discord の `#ミラトレ` を確認する
  - TweetShift 由来の通知だけを対象にし、自分たちの分析報告は読み直さない
  - 初回起動時の最初の 1 回は、直近通知を既読扱いにして誤発注を避ける
  - 2 回目以降は新着通知だけを OpenClaw に渡して解釈させる
  - 1 段目では本文と同じ投稿者の直近通知だけを使い、銘柄と方向、見送り理由、追加精査の要否を JSON で返させる
  - 2 段目では、曖昧な通知だけ BrowserRelay で元ポストや画像、必要最小限の過去投稿を確認させる
  - `app/runtime/openclaw/signal_batch.json` に JSON 配列で保存し、そのあと `app/openclaw_signal_ingest.py` で `signal_inbox.jsonl` に追記する
  - 毎回の分析結果を Discord の同じチャンネルへ送る
  - `app/main.py` の監視ループ内で `signal_inbox.jsonl` を読む
  - 重複を除いたうえで SQLite に記録する
  - 自動売買が有効なときだけ MT5 へ新規発注する
  - 同じユーザーと同じ銘柄で反対方向の建玉がある場合は先に決済する
  - 決済が確認できたら `out_at` と `out_price` を埋める
  - 画面下部は `口座情報` と `Discord` の 2 タブに分かれ、Discord 側に最近読み込んだ通知と判定結果、見送り理由を表示する

ファイル

- `app/trade_bridge.py`
  - 受け取り、重複防止、SQLite 記録、MT5 発注、決済同期
- `app/live_x_monitor.py`
  - `main.py` 起動中の Discord 監視、新着判定、OpenClaw 呼び出し、追加精査、Discord 報告
- `app/bridge_common.py`
  - 保存先、銘柄名の正規化、方向の正規化
- `app/openclaw_signal_ingest.py`
  - OpenClaw から渡された JSON 配列を jsonl inbox に追記
- `app/trade_settings.json`
  - 自動売買の開始停止、数量、許容ずれ

保存先

- 受け取り inbox
  - `app/runtime/openclaw/signal_inbox.jsonl`
- OpenClaw の一時 JSON
  - `app/runtime/openclaw/signal_batch.json`
- Discord 通知の整理結果
  - `app/runtime/openclaw/discord_posts.json`
- 既読管理
  - `app/runtime/openclaw/discord_monitor_state.json`
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
2. OpenClaw の Discord 連携が有効で、`#ミラトレ` を読めることを確認する
3. `python -m pip install -r app/requirements.txt` を実行する
4. 必要なら `app/trade_settings.json` で数量などを調整する
5. `python app/main.py` で mirrorbot を起動する
6. 画面下部の `Discord` タブに、Discord から読んだ通知と判定結果が出ることを確認する
7. 画面右上のボタンで自動売買を開始する
8. 画面右上の `数量設定` で共通数量と銘柄ごとの数量を保存する

注意

- 初期状態では自動売買は停止
- 数量設定画面では、各銘柄が空欄なら共通数量を使い、共通数量も空欄なら MT5 側の最小数量を使う
- OpenClaw 側の X 解析結果がゼロ件なら inbox には何も追記されない
- 起動直後の最初の 1 回は、直近通知を既読扱いにするため、新規発注は行わない
- BrowserRelay は、曖昧な通知を追加精査するときだけ使う
