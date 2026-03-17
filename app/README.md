# MT5 監視画面

MT5 と連携し、指定した 8 銘柄の売値と買値を 0.5 秒ごとに表示する画面です。

## 表示順

左列

- USDJPYm
- EURUSDm
- JP225m
- USOILm

右列

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
- 画面には売値と買値だけを表示し、差と時刻は出しません
