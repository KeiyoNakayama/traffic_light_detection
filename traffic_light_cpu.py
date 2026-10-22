import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Lock, Thread

parser = argparse.ArgumentParser(description="信号機検出（CPU専用・通常起動はオフライン）")
parser.add_argument("--prepare-model", action="store_true", help="通信してモデルを保存し、オフライン読込を検証する")
parser.add_argument("--check-model", action="store_true", help="カメラを使わずオフライン読込・推論を検証する")
parser.add_argument("--threads", type=int, default=2, help="CPU推論スレッド数（既定: 2）")
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
os.environ.pop("_ROBOFLOW_INFERENCE_OFFLINE_MODE_AT_PROCESS_START", None)
os.environ["HF_HOME"] = str(MODEL_CACHE / "hf_home")
os.environ["HF_HUB_CACHE"] = str(MODEL_CACHE / "hf_home" / "hub")
os.environ["HF_MODULES_CACHE"] = str(MODEL_CACHE / "hf_home" / "modules")
os.environ["HF_HUB_OFFLINE"] = "0" if args.prepare_model else "1"
os.environ["TRANSFORMERS_OFFLINE"] = "0" if args.prepare_model else "1"
os.environ["YOLO_OFFLINE"] = "False" if args.prepare_model else "True"

if not args.prepare_model and not MODEL_CACHE.is_dir():
  sys.exit("モデル未準備です。最初の一度だけ、通信できる状態で --prepare-model を実行してください。")

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["ONNXRUNTIME_EXECUTION_PROVIDERS"] = "CPUExecutionProvider"
os.environ["OMP_NUM_THREADS"] = str(args.threads)
os.environ["MKL_NUM_THREADS"] = str(args.threads)

import cv2
import numpy as np
import onnxruntime as ort
import supervision as sv
import torch
from inference import get_model

torch.set_num_threads(args.threads)
cv2.setNumThreads(1)

MODEL_ID = "keiyos-workspace/traffic-light-detection2-ccd7n-7-yolo26n-t1"
API_KEY = os.environ.get("ROBOFLOW_API_KEY")


def load_detection_model():
  if args.prepare_model and not API_KEY:
    raise RuntimeError("モデル準備前に環境変数 ROBOFLOW_API_KEY にRoboflow APIキーを設定してください。")
  if "CPUExecutionProvider" not in ort.get_available_providers():
    raise RuntimeError("ONNX RuntimeのCPUExecutionProviderが利用できません。")
  loaded = get_model(
      model_id=MODEL_ID,
      api_key=API_KEY,
      device=torch.device("cpu"),
      onnx_execution_providers=["CPUExecutionProvider"],
  )
  session = getattr(getattr(loaded, "_model", None), "_session", None)
  if session is None:
    raise RuntimeError("ONNXモデルの実行デバイスを確認できません。")
  providers = session.get_providers()
  if not providers or providers[0] != "CPUExecutionProvider":
    raise RuntimeError(f"CPU推論の初期化に失敗しました: {providers}")
  print(f"推論実行: {providers[0]}", flush=True)
  return loaded


if args.prepare_model or args.check_model:
  print("モデルを保存しています。" if args.prepare_model else "オフラインでモデルを検証しています。", flush=True)
  try:
    model = load_detection_model()
    model.infer(np.zeros((640, 640, 3), dtype=np.uint8))
  except Exception as error:
    print(f"モデルの準備・検証に失敗しました ({type(error).__name__})。", file=sys.stderr)
    print("モデルの保存状態とCPU実行環境を確認してください。", file=sys.stderr)
    sys.exit(1)
  if args.prepare_model:
    result = subprocess.run([
        sys.executable, str(Path(__file__).resolve()), "--check-model",
        "--threads", str(args.threads),
    ])
    if result.returncode:
      sys.exit(result.returncode)
    print(f"準備完了。モデル保存先: {MODEL_CACHE}")
    print("以後は引数なしで、ネット接続なしで起動できます。")
  else:
    print("オフラインでのモデル読込・CPU推論に成功しました。")
  sys.exit(0)

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
      <title>Traffic Light Realtime Detection (CPU)</title></head>
      <body style='margin:0;background:#111;color:#fff;font-family:sans-serif'>
      <h2 style='padding:0 16px'>Traffic Light Realtime Detection (CPU)</h2>
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

  labels = [
      f"{item['class']} {item['confidence']:.2f}"
      for item in predictions["predictions"]
  ]
  detections = sv.Detections.from_inference(predictions)
  annotated_image = box_annotator.annotate(scene=image.copy(), detections=detections)
  annotated_image = label_annotator.annotate(
      scene=annotated_image, detections=detections, labels=labels
  )

  status = f"CPU | FPS: {processing_fps:.1f} | Frame: {frame_count} | Detections: {len(detections)}"
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
    print("画像を受信し、CPU推論を開始しました。")

  success, encoded_image = cv2.imencode(
      ".jpg", annotated_image, [cv2.IMWRITE_JPEG_QUALITY, 80]
  )
  if success:
    global latest_jpeg, latest_sequence
    with frame_condition:
      latest_jpeg = encoded_image.tobytes()
      latest_sequence += 1
      frame_condition.notify_all()


print("保存済みモデルをオフラインで初期化しています。", flush=True)
model = load_detection_model()
print("試し推論でCPUの準備をしています。", flush=True)
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
