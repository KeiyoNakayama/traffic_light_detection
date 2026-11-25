# 信号機検出

カメラ画像から信号を認識し、OpenCVウィンドウまたはブラウザに表示します。
`waypoint_tools` の `add_service` 版と連携する場合は、到着時サービスで認識を開始し、
青信号の連続確認後に既存の `next_wp` サービスを呼びます。

## 構成と起動の考え方

| プログラム | 役割 |
|---|---|
| `camera_image_sender.py` | カメラの最新画像を同じPCの推論サーバーへHTTPで送信 |
| `traffic_light_realtime_detect.py` | GPU／CPUで推論し、検出結果を表示 |
| `traffic_light_cpu.py` | CPU専用の推論・表示 |
| `ros_service_control.py` | 開始・停止の受信、青の連続確認、進行要求の送信 |
| `launch/traffic_signal.launch.py` | 認識とカメラ送信の2プロセスを起動 |

**走行前にlaunchを手動で起動し、認識開始のタイミングをWaypointから制御します。**
到着時にプロセスを生成する方式ではありません。Nav2とWaypoint送信ノードは別途起動します。
`waypoint_tools` のC++・launchに信号専用の処理は追加せず、YAMLでサービスと開始地点を指定します。

以下は、このPCの配置 `~/ros2_ws/src/traffic_light_detection` と `.venv` を使用する手順です。
別のPCでは配置・Python環境・カメラ番号を合わせてください。
ROSサービス連携にはROS 2 Humbleと、`rclpy`・`std_srvs` を読み込めるPython環境が必要です。

## 1. カメラ認識だけ試す（走行要求なし）

ターミナル①：

```bash
cd ~/ros2_ws/src/traffic_light_detection
.venv/bin/python traffic_light_realtime_detect.py --device cuda --display window
```

CPUの場合は `.venv/bin/python traffic_light_cpu.py --display window` を使います。
ターミナル②：

```bash
cd ~/ros2_ws/src/traffic_light_detection
.venv/bin/python camera_image_sender.py --camera 2 --interval 0.1
```

この起動ではサービス待ちなしで認識し、Waypointへの進行要求は送りません。
`--display window` はデスクトップ表示環境とGUI対応OpenCVが必要です。
省略するとブラウザ表示になり、同じPCで <http://127.0.0.1:8765> を開きます。
終了は両ターミナルでCtrl+C。ウィンドウのQ／Escでも認識側を終了できますが、カメラ送信は別途停止してください。

## 2. Waypointと連携する

### Waypointの設定

現場の地図に合わせた座標・向きを設定してください。下の座標は説明用です。

```yaml
arrival_services:
  traffic_light: /mission/control_traffic_light
waypoints:
  # 信号手前の停止位置。到着すると認識開始サービスを呼び、進行要求を待つ。
  - x: 1.0
    y: 0.0
    yaw: 0.0
    on_arrival_task: traffic_light
    on_arrival_action: start
  # 青確認後に向かう地点
  - x: 3.0
    y: 0.0
    yaw: 0.0
```

`x`・`y`は地図座標のメートル、`yaw`はラジアンです。
到着時サービスを設定した点は停止して待つため、`stop: true` の併記は必須ではありません。
信号待ち点を最後のWaypointにすると走行終了になるため、その後に次の地点を置きます。
`on_arrival_action: stop` は認識停止の依頼であり、ロボットの停止指定とは別です。

### ターミナル①：認識とカメラを待機起動

Nav2は通常の方法で起動しておきます。

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
cd ~/ros2_ws/src/traffic_light_detection
ros2 launch ./launch/traffic_signal.launch.py \
  device:=cuda \
  display:=window \
  camera:=2
```

CPUは `device:=cpu`。このlaunchがカメラ送信も起動するため、別途送信プログラムを起動する必要はありません。
初期化完了後、次の表示を確認してください。

```text
待機: /mission/control_traffic_light (SetBool), go: /waypoint_sender_node/next_wp (Trigger)
画像受付: http://127.0.0.1:8765/infer
```

カメラ送信側に「Waypointからの認識開始サービス待ち」と出るのは正常です。
開始サービスを受けるまで、検出結果の映像は更新されません。

### ターミナル②：Waypoint送信を起動

このPCでは `add_service` 版を `~/waypoint_service_ws` に配置しています。
別のPCでは、その版をビルドしたワークスペースをsourceしてください。

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
source ~/waypoint_service_ws/install/setup.bash
ros2 launch waypoint_tools send.launch.py \
  send_waypoint_path:=/absolute/path/to/your_waypoints.yaml
```

パスを実際のWaypoint YAMLに置き換え、認識側が待機状態になってからRVizのStartを押します。
到着 → `SetBool(true)` → 青の連続確認 → `next_wp` → 次の地点への移動、の順に動作します。
同名のWaypointノードや、既存の認識サーバー・カメラ送信を重複起動しないでください。
停止時は認識launchのターミナルでCtrl+Cを押します。**認識launchの終了はNav2の走行停止ではありません。**

### 青判定の条件

| 項目 | 既定値・動作 |
|---|---|
| 開始条件 | `/mission/control_traffic_light` に `SetBool(true)` を受信済み |
| 対象クラス | `Trafficlight_blue` と完全一致 |
| 信頼度 | 0.8以上 |
| 連続確認 | 推論結果3フレーム |
| 赤の同時検出 | クラス名に `red` を含み信頼度0.8以上なら連続数をリセット |
| 未検出・青の信頼度不足 | 連続数をリセット |
| 遅延 | 1秒を超えるフレーム処理・結果間隔で連続判定をリセット |
| 送信 | サービスが利用可能で、最新の判定から1秒以内の場合に一度だけ呼ぶ |

例えば信頼度0.9以上、5フレーム連続に変更する場合：

```bash
ros2 launch ./launch/traffic_signal.launch.py \
  device:=cuda display:=window camera:=2 \
  confidence:=0.9 confirm_frames:=5 interval:=0.1
```

`interval` は画像送信間隔（秒）です。実際の処理速度は推論時間にも依存し、3フレームは固定の時間条件ではありません。
`confidence` は赤の判定にも使います。対象クラスは `go_class:=Trafficlight_blue` で変更できます。
画像全体を判定し、同じ信号の追跡や車線・対象信号の選別は行っていません。

進行要求を送った後は認識を停止し、次の開始要求を待ちます。
応答成功時は `GO ACCEPTED`、拒否時は `GO REJECTED` と表示します。
5秒以上応答がなければ応答不明として自動再送しません。応答待ちの再開始も拒否します。
`SetBool(false)` で進行中の1フレーム処理自体は中断しませんが、その結果から進行要求は出しません。
既に送信した進行要求は取り消せません。

既存の `next_wp` は信号待ち専用ではなく、走行中でも次へ進めるサービスです。
認識中にRVizのNext WPやStartで手動操作する場合は、先に認識を停止してください。

```bash
# ROS環境をsourceしたターミナルで実行
ros2 service call /mission/control_traffic_light std_srvs/srv/SetBool '{data: false}'
```

手動で認識を開始する場合は上の `false` を `true` にします。
開始後は青判定で実際に `next_wp` が呼ばれるため、単にカメラ認識を見る場合は「1」の手順を使ってください。

## 3. 模擬Nav2でサービス連携を確認する

このPCでは `~/waypoint_service_ws` のビルド済み送信ノードと設定例を使用します。
実機・カメラ・本物のNav2・認識launchは起動不要です。

```bash
source /opt/ros/humble/setup.bash
source ~/waypoint_service_ws/install/setup.bash
cd ~/ros2_ws/src/traffic_light_detection
ROS_DOMAIN_ID=179 ROS_LOCALHOST_ONLY=1 \
  python3 tests/test_waypoint_service_integration.py
```

模擬Nav2が到着成功を返し、人工的な青判定を3回入力して、次の地点が送信されることを検証します。
`GO ACCEPTED` と最後の `OK` が成功の目印です。起動したテスト用ノードは終了時に停止します。
ログは `/tmp/waypoint_signal_integration.log`。同じテストを同時に複数起動しないでください。
画像認識精度・実際の走行や停止位置はこのテストでは確認しません。

開始・停止、古い結果の除外、赤の同時検出、重複送信防止のテスト：

```bash
ROS_DOMAIN_ID=179 ROS_LOCALHOST_ONLY=1 python3 tests/test_service_control.py
```

## 4. 起動ログとトラブル確認

| 表示 | 意味・確認すること |
|---|---|
| 推論ライブラリを読み込んでいます | 初期化中。読み込み完了とモデル初期化を待つ |
| 認識サーバーへの接続待ち | まだ画像受付に接続できていない。認識側のログを確認 |
| Waypointからの認識開始サービス待ち | 接続済み、認識停止中。到着時の開始要求を待っている |
| 画像送信・推論中 | 画像を送信し、推論処理が成功している |
| HTTP応答エラー | 認識側の例外や状態を確認 |

カメラ送信側の同じ状態のログは5秒ごとに表示します。
SAM・YoloWorldなどの未使用モデルに関する警告だけでは、信号認識の失敗とは判断できません。
`CUDAExecutionProvider`、試し推論、その後のサービス待機表示まで進んだかを確認してください。

ウィンドウ作成で停止する問題に対応し、現在は推論ライブラリ読込前にメインスレッドでOpenCVウィンドウを作成します。
GPUでのモデル初期化・試し推論・サービス待機開始まで確認済みです。
起動ログを保存する場合：

```bash
ros2 launch ./launch/traffic_signal.launch.py \
  device:=cuda display:=window camera:=2 2>&1 | tee /tmp/traffic_launch_latest.log
```

## 5. モデル準備とローカル環境

モデル保存済みなら通常起動時のインターネット接続は不要です。
`model_cache/` と `.venv/` はGitに含まれないため、別のPCでは環境とモデルを準備してください。
`requirements.gpu-tested.txt` はGPU動作確認環境の依存パッケージ一覧です。

モデルの初回準備時だけRoboflow APIキーとネット接続が必要です。

```bash
cd ~/ros2_ws/src/traffic_light_detection
read -rsp "Roboflow API key: " ROBOFLOW_API_KEY; echo
export ROBOFLOW_API_KEY
.venv/bin/python traffic_light_realtime_detect.py --prepare-model
```

CPUでは `.venv/bin/python traffic_light_cpu.py --prepare-model` を使います。
APIキーをコードやGitに保存しないでください。詳細は [OFFLINE_USAGE.md](./OFFLINE_USAGE.md) を参照してください。

## 6. コードを読む順番

1. `launch/traffic_signal.launch.py`：起動するプログラムと設定値。
2. `ros_service_control.py`：`control()` で開始・停止、`observe()` で青判定、`tick()` で進行要求。
3. `traffic_light_realtime_detect.py`：`do_POST()` で `begin_frame()` → `model.infer()` → `observe()` を接続。
4. `tests/test_waypoint_service_integration.py`：模擬到着から次Waypointまでの流れ。

## 参考：既存のトピック方式

`--ros-go` と `ros_signal_go_bridge.py` は別の信号待ちノード向けのトピック方式です。
`/traffic_signal/heartbeat`・`waiting`・`go`・`ack` を使い、`--ros-services` とは併用できません。
今回の `waypoint_tools` 連携は `--ros-services` を使用します。
トピック方式の既定クラス `Trafficlight_green` は、現在のblue／redモデルと一致しません。
利用時は対象モデルのクラス名を確認し、`--go-class` に完全一致する名前を指定してください。
