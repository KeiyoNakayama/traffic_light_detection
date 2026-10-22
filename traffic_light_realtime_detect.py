import argparse
import json
import traceback
import os
import subprocess
import sys
from pathlib import Path
import signal
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Condition, Thread, Lock

# 準備時だけ通信を許可する。通常起動では保存済みモデルだけを使う。
parser = argparse.ArgumentParser(description="信号機検出（通常起動はオフライン）")
parser.add_argument("--prepare-model", action="store_true", help="通信してモデルを保存し、オフライン読込を検証する")
parser.add_argument("--check-model", action="store_true", help="カメラを使わずオフライン読込・推論を検証する")
parser.add_argument("--threads", type=int, default=2, help="CPU推論スレッド数（既定: 2）")
parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda", help="推論デバイス（既定: cuda）")
args = parser.parse_args()
if args.threads < 1:
  parser.error("--threads は1以上を指定してください")
if args.prepare_model and args.check_model:
  parser.error("--prepare-model と --check-model は同時に指定できません")
MODEL_CACHE = Path(__file__).resolve().parent / "model_cache"
os.environ["MODEL_CACHE_DIR"] = str(MODEL_CACHE)
os.environ["INFERENCE_HOME"] = str(MODEL_CACHE)
os.environ["OFFLINE_MODE"] = "False" if args.prepare_model else "True"
os.environ["OFFLINE_MODE_WARM_UP"] = "True" if args.prepare_model else "False"
# 子プロセスも選択したモードで起動する。
os.environ.pop("_ROBOFLOW_INFERENCE_OFFLINE_MODE_AT_PROCESS_START", None)
os.environ["HF_HOME"] = str(MODEL_CACHE / "hf_home")
os.environ["HF_HUB_CACHE"] = str(MODEL_CACHE / "hf_home" / "hub")
os.environ["HF_MODULES_CACHE"] = str(MODEL_CACHE / "hf_home" / "modules")
os.environ["HF_HUB_OFFLINE"] = "0" if args.prepare_model else "1"
os.environ["TRANSFORMERS_OFFLINE"] = "0" if args.prepare_model else "1"
os.environ["YOLO_OFFLINE"] = "False" if args.prepare_model else "True"

if not args.prepare_model and not MODEL_CACHE.is_dir():
  sys.exit("モデル未準備です。最初の一度だけ、通信できる状態で --prepare-model を実行してください。")

# CPUは明示指定時だけ使用する。GPUエラー時に黙ってCPUへ切り替えない。
if args.device == "cpu":
  os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["ONNXRUNTIME_EXECUTION_PROVIDERS"] = (
    "CUDAExecutionProvider,CPUExecutionProvider" if args.device == "cuda"
    else "CPUExecutionProvider"
)

# 小型モデルでは過剰な並列化が遅くなるため、ライブラリ読込前に制限する。
os.environ["OMP_NUM_THREADS"] = str(args.threads)
os.environ["MKL_NUM_THREADS"] = str(args.threads)

import cv2
import torch

torch.set_num_threads(args.threads)
if args.device == "cuda":
  if not torch.cuda.is_available():
    sys.exit("GPUを利用できません。ドライバとCUDA環境を確認してください。CPUで起動する場合は --device cpu を指定してください。")
  print(f"GPU: {torch.cuda.get_device_name(0)} / PyTorch CUDA {torch.version.cuda}", flush=True)
import onnxruntime as ort
if args.device == "cuda":
  ort.preload_dlls()
  if "CUDAExecutionProvider" not in ort.get_available_providers():
    sys.exit("ONNX Runtime GPU版が必要です。")
cv2.setNumThreads(1)
import supervision as sv
from inference import get_model
import numpy as np

# 設定
API_KEY = os.environ.get("ROBOFLOW_API_KEY")
MODEL_ID = "keiyos-workspace/traffic-light-detection2-ccd7n-7-yolo26n-t1"  # 例: プロジェクト名/バージョン番号

def load_detection_model():
  if args.prepare_model and not API_KEY:
    raise RuntimeError("モデル準備前に環境変数 ROBOFLOW_API_KEY にRoboflow APIキーを設定してください。")
  loaded = get_model(
      model_id=MODEL_ID, api_key=API_KEY,
      device=torch.device("cuda:0" if args.device == "cuda" else "cpu"),
      onnx_execution_providers=(
          [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}), "CPUExecutionProvider"] if args.device == "cuda"
          else ["CPUExecutionProvider"]
      ),
  )
  session = getattr(getattr(loaded, "_model", None), "_session", None)
  if session is None:
    raise RuntimeError("ONNXモデルの実行デバイスを確認できません。")
  providers = session.get_providers()
  if args.device == "cuda" and providers[0] != "CUDAExecutionProvider":
    raise RuntimeError(f"GPU推論の初期化に失敗しました: {providers}")
  print(f"推論実行: {providers[0]}", flush=True)
  return loaded


if args.prepare_model or args.check_model:
  from inference import get_model
  import numpy as np

  print("モデルを保存しています。" if args.prepare_model else "オフラインでモデルを検証しています。", flush=True)
  try:
    model = load_detection_model()
    model.infer(np.zeros((640, 640, 3), dtype=np.uint8))
  except Exception as error:
    print(f"モデルの準備・検証に失敗しました ({type(error).__name__})。", file=sys.stderr)
    print("モデルの保存状態と、指定デバイスの実行環境を確認してください。", file=sys.stderr)
    sys.exit(1)
  if args.prepare_model:
    # 新しいプロセスで、オンライン時のメモリに依存せず再読込できるか確認。
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--check-model", "--threads", str(args.threads), "--device", args.device])
    if result.returncode:
      sys.exit(result.returncode)
    print(f"準備完了。モデル保存先: {MODEL_CACHE}")
    print("以後は引数なしで、ネット接続なしで起動できます。")
  else:
    print("オフラインでのモデル読込・推論に成功しました。")
  sys.exit(0)

# バウンディングボックスを描画するためのアノテーターを初期化
box_annotator = sv.BoxAnnotator()
label_annotator = sv.LabelAnnotator()
frame_count = 0
latest_jpeg = None
latest_sequence = 0
fps_started = None
fps_frames = 0
processing_fps = 0.0
frame_condition = Condition()
inference_lock = Lock()


class PreviewHandler(BaseHTTPRequestHandler):
  def do_POST(self):
    if self.path != "/infer":
      self.send_error(404)
      return
    try:
      length = int(self.headers.get("Content-Length", "0"))
    except ValueError:
      self.send_error(400, "Invalid Content-Length")
      return
    if not 0 < length <= 10 * 1024 * 1024:
      self.send_error(413, "Expected an image of at most 10 MiB")
      return
    self.connection.settimeout(10)
    try:
      payload = self.rfile.read(length)
    except OSError:
      self.send_error(408)
      return
    if len(payload) != length:
      self.send_error(400, "Incomplete image")
      return
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
      self.send_error(400, "Invalid image")
      return
    if not inference_lock.acquire(blocking=False):
      self.send_error(429, "Inference busy")
      return
    try:
      result = model.infer(image)[0]
      # 未使用の tracker_id=None を描画側へ渡すと整数変換で失敗する。
      predictions = result.model_dump(by_alias=True, mode="json", exclude_none=True)
      my_custom_sink(predictions, image)
      body = json.dumps(predictions).encode("utf-8")
    except Exception as error:
      print(f"推論・描画処理失敗: {type(error).__name__}", flush=True)
      traceback.print_exc()
      self.send_error(500, "Inference failed")
      return
    finally:
      inference_lock.release()
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    try:
      self.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
      pass

  def do_GET(self):
    if self.path in ("/", "/index.html"):
      page = b"""<!doctype html><html lang='ja'><head><meta charset='utf-8'>
      <title>Traffic Light Realtime Detection</title></head>
      <body style='margin:0;background:#111;color:#fff;font-family:sans-serif'>
      <h2 style='padding:0 16px'>Traffic Light Realtime Detection</h2>
      <img src='/stream.mjpg' style='width:100%;max-width:1280px'>
      </body></html>"""
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(page)))
      self.end_headers()
      self.wfile.write(page)
      return

    if self.path != "/stream.mjpg":
      self.send_error(404)
      return

    self.send_response(200)
    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    self.end_headers()
    try:
      last_sequence = -1
      while True:
        with frame_condition:
          frame_condition.wait_for(
              lambda: latest_jpeg is not None and latest_sequence != last_sequence
          )
          jpeg = latest_jpeg
          last_sequence = latest_sequence
        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
        self.wfile.write(jpeg + b"\r\n")
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
      pass

  def log_message(self, format, *args):
    pass


def start_preview_server():
  server = ThreadingHTTPServer(("127.0.0.1", 8765), PreviewHandler)
  Thread(target=server.serve_forever, daemon=True).start()
  return server


# フレームごとに呼ばれる推論結果の処理関数
def my_custom_sink(predictions, image):
  global frame_count, fps_started, fps_frames, processing_fps
  now = time.perf_counter()
  if fps_started is None:
    fps_started = now
  else:
    fps_frames += 1
    elapsed = now - fps_started
    if elapsed >= 1.0:
      processing_fps = fps_frames / elapsed
      fps_frames = 0
      fps_started = now
  frame_count += 1

  # 予測結果からラベルとバウンディングボックスのデータを抽出
  labels = [
      f"{item['class']} {item['confidence']:.2f}"
      for item in predictions["predictions"]
  ]
  detections = sv.Detections.from_inference(predictions)

  # 画像に枠とラベルを描画
  annotated_image = box_annotator.annotate(scene=image.copy(), detections=detections)
  annotated_image = label_annotator.annotate(
      scene=annotated_image, detections=detections, labels=labels
  )

  # 枠が出ないフレームでも、推論が動いていることを画面上で確認できるようにする。
  status = f"{args.device.upper()} | FPS: {processing_fps:.1f} | Frame: {frame_count} | Detections: {len(detections)}"
  cv2.putText(
      annotated_image,
      status,
      (10, 30),
      cv2.FONT_HERSHEY_SIMPLEX,
      0.8,
      (0, 255, 0),
      2,
      cv2.LINE_AA,
  )
  if frame_count == 1:
    print("画像を受信し、推論を開始しました。")

  # OpenCVのGUIを使わず、ブラウザ表示用のJPEGを更新する。
  success, encoded_image = cv2.imencode(".jpg", annotated_image, [cv2.IMWRITE_JPEG_QUALITY, 80])
  if success:
    global latest_jpeg, latest_sequence
    with frame_condition:
      latest_jpeg = encoded_image.tobytes()
      latest_sequence += 1
      frame_condition.notify_all()


# カメラにはアクセスせず、別プログラムから送られた画像を処理する。
print("保存済みモデルをオフラインで初期化しています。", flush=True)
model = load_detection_model()
print("試し推論でGPUの準備をしています。", flush=True)
model.infer(np.zeros((640, 640, 3), dtype=np.uint8))
preview_server = start_preview_server()


def stop_server(signum, frame):
  raise KeyboardInterrupt


signal.signal(signal.SIGINT, stop_server)
signal.signal(signal.SIGTERM, stop_server)
try:
  print("画像受付: http://127.0.0.1:8765/infer", flush=True)
  print("結果表示: http://127.0.0.1:8765 （終了: Ctrl+C）", flush=True)
  while True:
    time.sleep(0.2)
except KeyboardInterrupt:
  pass
finally:
  preview_server.shutdown()
  preview_server.server_close()
  print("画像推論を終了しました。")
