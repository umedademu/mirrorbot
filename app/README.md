# MT5 Balance Viewer

MT5 と連携し、口座残高を表示するだけの最小 GUI アプリです。

## 起動方法

```powershell
cd C:\Users\user\Desktop\mirrorbot\app
python main.py
```

## 前提

- Windows 上に MT5 がインストールされていること
- `MetaTrader5` Python パッケージがインストールされていること
- 口座にログイン済みであること

## 補足

- 既定では `C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe` を優先して使用します
- 別の MT5 を使う場合は、`MT5_TERMINAL_PATH` 環境変数で `terminal64.exe` のパスを指定できます
