# 信号機検出

USBカメラなどから送信した画像をローカルで推論し、検出結果をブラウザに表示するプログラムです。
推論サーバーは `127.0.0.1:8765` のみで待ち受けます。

## 起動

Python環境に必要な依存パッケージを用意し、プロジェクトのディレクトリで実行します。

```bash
python traffic_light_realtime_detect.py
```

ブラウザで <http://127.0.0.1:8765> を開きます。カメラ画像の送信は別のターミナルで行います。

```bash
python camera_image_sender.py --camera 2 --interval 2
```

初回はRoboflow APIキーを環境変数に設定してモデルを準備してください。APIキーはソースコードやGitに保存しないでください。

```bash
read -rsp "Roboflow API key: " ROBOFLOW_API_KEY; echo
export ROBOFLOW_API_KEY
python traffic_light_realtime_detect.py --prepare-model
```

モデルの準備後はオフラインで起動できます。GPUが使えない環境では `--device cpu` を指定してください。

詳細な起動方法とモデル検証方法は [OFFLINE_USAGE.md](./OFFLINE_USAGE.md) を参照してください。

## ローカルデータ

モデルキャッシュは `model_cache/` に保存されます。サイズが大きく環境固有のため、Gitには含めません。モデルは各環境で `--prepare-model` を実行して取得してください。

`requirements.gpu-tested.txt` はGPU動作確認環境の依存パッケージ一覧です。GPU/CUDAやOSの構成に合わせて環境を用意してください。
