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

CPUだけで推論する場合は、専用スクリプトを使います。GPUは使用しません。

```bash
python traffic_light_cpu.py
```

モデルの初回準備にはRoboflow APIキーが必要です。次のコマンドでキーを入力して環境変数に設定します（入力内容は画面に表示されません）。

```bash
read -rsp "Roboflow API key: " ROBOFLOW_API_KEY; echo
export ROBOFLOW_API_KEY
python traffic_light_realtime_detect.py --prepare-model
```

CPU版でも同じ環境変数を設定して `traffic_light_cpu.py --prepare-model` を実行します。通常起動や準備済みモデルの検証ではAPIキーは不要です。APIキーをソースコードやGitに保存しないでください。

モデルの準備後はオフラインで起動できます。GPUが使えない環境では `--device cpu` を指定してください。

詳細な起動方法とモデル検証方法は [OFFLINE_USAGE.md](./OFFLINE_USAGE.md) を参照してください。

## 信号待ちノードへゴーサインを送る

`--ros-go` を付けると、カメラ推論プロセスが `/traffic_signal/heartbeat`
を発行し、ロボットの `/traffic_signal/waiting` を購読します。ロボットが
`waiting_for_go` を通知している間に、設定したクラスを信頼度以上で連続検出
した場合だけ `/traffic_signal/go` を発行します。PCは同じ要求IDのゴーサインを
再送し、`/traffic_signal/ack` で受理または拒否を確認します。

現在キャッシュされているモデルのクラスは `Trafficlight_blue` と
`Trafficlight_red` のみです。greenクラスがモデルに追加されるまでは
`Trafficlight_green` 検出が成立せず、ゴーサインは送信されません。
モデルと表示ラベルを確認してから、正確なクラス名を `--go-class` に指定してください。

まずロボット側の `play_waypoints_nav.launch.py` を、正しい
`signal_wait_indices` と同じ `robot_id` で起動します。PC側ではROS 2 Humbleと
ワークスペースをsourceし、次のように起動します。

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
.venv/bin/python traffic_light_realtime_detect.py \
  --ros-go \
  --robot-id orne_box \
  --go-class Trafficlight_green \
  --go-min-confidence 0.8 \
  --go-confirm-frames 3
```

画像を送るカメラプロセスは別ターミナルで起動します。

```bash
.venv/bin/python camera_image_sender.py --camera 2 --interval 0.1
```

`/traffic_signal/waiting`、`/traffic_signal/go`、`/traffic_signal/ack` を
`ros2 topic echo` で監視すると、待機要求・送信・ロボット受理を確認できます。
実カメラを使わないローカル接続試験では、PCのゴーサイン送信・再送・ACK確認を
検証できますが、カメラ認識そのものの確認にはgreenクラスを持つモデルと実画像が必要です。
通常起動ではROS連携は無効で、既存の表示動作は変わりません。

## ローカルデータ

モデルキャッシュは `model_cache/` に保存されます。サイズが大きく環境固有のため、Gitには含めません。モデルは各環境で `--prepare-model` を実行して取得してください。

`requirements.gpu-tested.txt` はGPU動作確認環境の依存パッケージ一覧です。GPU/CUDAやOSの構成に合わせて環境を用意してください。

## ブラウザなしで表示

デスクトップ上のターミナルから、OpenCVウィンドウ表示を指定します。

```bash
cd ~/traffic_light_detection
.venv/bin/python traffic_light_realtime_detect.py --display window
```

CPU版の場合は `.venv/bin/python traffic_light_cpu.py --display window` を使います。
別のターミナルからカメラ画像を送信します（既に送信中なら追加起動は不要）。

```bash
cd ~/traffic_light_detection
.venv/bin/python camera_image_sender.py --camera 2 --interval 0.05
```

`--interval` は秒単位で、0.05は最大20回/秒の送信指定です。実際の速度は推論時間などに依存します。
画像が届くまでウィンドウには待機メッセージが出ます。
検出枠・クラス/信頼度・FPS・フレーム数・検出数を画像に重ねて表示します。
`--ros-go` 使用時は、待機要求待ち、対象クラス待ち、連続検出確認中、
ゴーサインACK待ち、受理/拒否も画像上部に表示します。
ウィンドウにフォーカスを当てて Q / Esc、
閉じるボタン、またはターミナルの Ctrl+C で推論サーバーを終了できます。
送信プログラムは別途 Ctrl+C で停止してください。
モデル準備済みならネット接続は不要です。GUI対応OpenCVとデスクトップ表示環境が必要です。
`--display` を省略すると従来のブラウザ表示になります。

## 到着時サービスによる認識開始・停止（サービス連携版）

`--ros-services` は既存のトピック方式 `--ros-go` とは別モードです。
認識プロセスとカメラ送信プログラムをあらかじめ起動します。
モデル読込後は待機し、`std_srvs/srv/SetBool` の `data: true` で画像推論を開始、
`false` で停止します。プロセスは停止せず次の開始要求を待ちます。
停止中の画像送信にはHTTP 409を返すため、送信側のログにその旨が出ます。
実行中の1フレームの推論は中断しませんが、その結果からゴーサインは出しません。

GPU版・CPU版のどちらにも以下のオプションを指定できます。

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
cd ~/ros2_ws/src/traffic_light_detection
# add_service 派生版の専用サービスへ接続。
.venv/bin/python traffic_light_realtime_detect.py --display window \
  --ros-services \
  --control-service /mission/control_traffic_light \
  --go-service /waypoint_sender_node/signal_go \
  --service-go-class Trafficlight_blue \
  --service-go-confidence 0.8 \
  --service-go-frames 3
```

`Trafficlight_blue` が通行対象の信号に対応することを実画像で確認してください。
クラスは完全一致です。現状は画像中のすべての検出を対象とし、車線や信号機の選別はしません。
CPU版はスクリプト名を `traffic_light_cpu.py` に置き換えます。
カメラ送信は別ターミナルで `.venv/bin/python camera_image_sender.py --camera 2 --interval 0.1`。

手動で開始・停止を確認する場合:

```bash
ros2 service call /mission/control_traffic_light std_srvs/srv/SetBool '{data: true}'
ros2 service call /mission/control_traffic_light std_srvs/srv/SetBool '{data: false}'
```

青の連続確認後、指定先に `std_srvs/srv/Trigger` を一度だけ送信し認識を停止します。
連続判定は1秒を超える画像間隔、1秒を超える推論、対象未検出、信頼度以上のredクラス検出でリセットします。
再度の到着では `data: true` で再開します。動作中の重複開始は連続判定をリセットしません。
ゴーサイン応答待ちの再開始は拒否します。5秒以上応答がなければ応答不明とし、
二重進行を防ぐため自動再送しません。停止要求で送信済みゴーサインは取り消せません。
受信側も「信号待ち中のWaypointだけを再開する」検証が必要です。

画像の到着時サービス対応版で使うYAML設定例（座標は現場に合わせる）:

```yaml
arrival_services:
  traffic_light: /mission/control_traffic_light
waypoints:
  - {x: 1.0, y: 0.0, yaw: 0.0, on_arrival_task: traffic_light, on_arrival_action: start}
```

接続先は `~/waypoint_service_ws/src/waypoint_tools` の `add_service` 派生版です。
専用の `/waypoint_sender_node/signal_go` (`Trigger`) を追加しました。
`traffic_light` タスクの `start` 到着で停止中の場合だけ進行を受け付けます。
通常の `next_wp` と異なり、走行中・未開始・別タスク・編集停止では進行しません。
到着時の開始サービス応答より先にゴーサインが届いた場合は、開始成功後に進行します。
既存の `~/ros2_ws/src/waypoint_tools` は変更せず、別ワークスペースに配置しています。

Nav2起動後、以下で認識・カメラ送信・Waypoint送信ノードをまとめて待機起動します。
`send_waypoint_path` は現場の座標に変更したYAMLの絶対パスを指定してください。

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
source ~/waypoint_service_ws/install/setup.bash
ros2 launch waypoint_tools traffic_signal.launch.py \
  send_waypoint_path:=/absolute/path/to/your_waypoints.yaml \
  device:=cuda display:=window camera:=2 go_class:=Trafficlight_blue
```

CPUは `device:=cpu`。認識サーバーの「待機」表示を確認後、RVizのStartで走行開始します。
同じ名前の旧Waypointノード・既存の認識サーバー・カメラ送信は同時起動しないでください。
サンプルは `~/waypoint_service_ws/src/waypoint_tools/config/waypoints/traffic_signal_example.yaml`。
信号待ち点には `on_arrival_task: traffic_light` と `on_arrival_action: start` を指定します。
最後のWaypointにはせず、その後に横断後のWaypointを置いてください。
通常の認識ノードは常駐し、サービスで推論を有効化・無効化する方式です。

模擬ROSサービスでの検証（走行サービスを使いません）:

```bash
source /opt/ros/humble/setup.bash
ROS_DOMAIN_ID=179 ROS_LOCALHOST_ONLY=1 python3 tests/test_service_control.py
```
