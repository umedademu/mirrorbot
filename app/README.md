# MT5 監視画面

MT5 と連携し、指定した 8 銘柄の売値と買値を 0.5 秒ごとに表示し、それぞれに 1 分足チャートを出す画面です。

## 表示順

上段

- USDJPYm
- EURUSDm
- JP225m
- USOILm

下段

- XAUUSDm
- XAGUSDm
- BTCUSDm
- ETHUSDm

## 起動方法

```powershell
cd C:\Users\user\Desktop\mirrorbot\app
python main.py
```

## 前提

- Windows 上に MT5 がインストールされていること
- `MetaTrader5` Python パッケージがインストールされていること
- 口座にログイン済みであること
- 上の 8 銘柄がその口座で使えること

## 補足

- 既定では `C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe` を優先して使用します
- 別の MT5 を使う場合は、`MT5_TERMINAL_PATH` 環境変数で `terminal64.exe` のパスを指定できます
- 画面には売値と買値を表示し、各銘柄の直近 30 本の 1 分足チャートを出します
- 一般的な大きさのウインドウでも、上段4つ・下段4つの並びで収まりやすいように、各チャートの横幅を抑えています
- 値段表示は各枠の上部で銘柄名と同じ行に並べ、`BID` と `ASK` を出します
- `BID` と `ASK` は銘柄名より目立つように、文字を大きくして色も分けています
- 画面上部の見出しは置かず、そのぶんの高さを各チャートに回しています
- チャートは枠の高さをより使うようにして、上下の空きを減らしています
- 差と時刻は出しません
