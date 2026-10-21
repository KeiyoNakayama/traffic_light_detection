# 信号機検出のオフライン起動

モデルは、このスクリプトと同じフォルダの `model_cache/` に保存します。
再起動で消える `/tmp` は使用しません。このフォルダは削除しないでください。

## 通常の起動（ネット接続不要）

```bash
cd /path/to/traffic_light_detection
.venv/bin/python traffic_light_realtime_detect.py
```

ブラウザで http://127.0.0.1:8765 を開きます。同じPC内の表示なのでWi-Fiは不要です。
別のターミナルでカメラ側を起動します。

```bash
cd /path/to/traffic_light_detection
.venv/bin/python camera_image_sender.py --camera 2 --interval 2
```

カメラ側は動画を読み続けて最新フレームだけ保持し、2秒ごとにJPEG画像を送信します。
推論側はカメラを開かず、`POST http://127.0.0.1:8765/infer` の本文に画像バイト列を受け取ります。
応答は検出結果のJSONです。他のプログラムからもJPEG/PNGを送信できます。
通信は同じPC内で完結するためWi-Fiは不要です。
処理が2秒を超えた場合は送信回数を減らし、古い画像をためません。
終了は両方のターミナルで Ctrl+C。

## 初回準備・モデルを削除した場合

通信できる状態で一度だけ実行します。カメラは不要です。

```bash
cd /path/to/traffic_light_detection
read -rsp "Roboflow API key: " ROBOFLOW_API_KEY; echo
export ROBOFLOW_API_KEY
.venv/bin/python traffic_light_realtime_detect.py --prepare-model
```

ダウンロード後、別プロセスでオフライン読込とダミー画像の推論を確認します。
「準備完了」が出たら、以後は通常の起動を使ってください。
準備が失敗した場合は、通信・モデルへのアクセス権を確認して同じコマンドを再実行してください。

## カメラを使わず確認

```bash
cd /path/to/traffic_light_detection
.venv/bin/python traffic_light_realtime_detect.py --check-model
```

モデルの初回準備には `ROBOFLOW_API_KEY` 環境変数が必要です。キーをソースコードやGitに保存しないでください。準備後のオフライン起動と検証にはキーは不要です。

通常起動と検証は `OFFLINE_MODE=True`、準備だけ `OFFLINE_MODE_WARM_UP=True` で実行します。
モデルが不足していても、通常起動から自動的にオンラインへ切り替えません。

## フレームレート改善

CPU推論は既定で2スレッド、OpenCVは1スレッドに制限しています。
640×640のダミー画像で8回測定した参考値は、従来の16スレッドで約12回/秒、
2スレッドで約33回/秒でした。カメラ取得・描画・配信を含むFPSではありません。
モデルの入力サイズや認識のしきい値は変更していません。

画面の `FPS` は推論結果を処理する頻度を約1秒ごとに更新します。
JPEG品質は80にし、配信で次フレームを余分に待つ処理を修正しました。
別のCPUで調整するときは `--threads 4` などを指定できます。
GPUは引き続き無効です。カメラ接続時の実測は未確認です。
変更前のコードは `traffic_light_realtime_detect.py.before_fps` に保存しています。

依存関係チェックでは `inference` がCPU版のパッケージ名 `onnxruntime` を要求する警告が残りますが、実際のimportはGPU版で提供します。CPU版の追加インストールは避けてください。

GPU検証: オフラインモデル推論、HTTP画像送信3回、JPEGプレビュー更新、nvidia-smiによるGPU使用（380 MiB）を確認済み。実カメラは未検証です。
