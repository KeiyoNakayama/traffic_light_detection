"""USBカメラの最新画像を一定間隔で同じPCの推論プロセスに送る。"""
import argparse
import math
import threading
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera', type=int, default=2, help='USBカメラ番号（既定: 2）')
    parser.add_argument('--interval', type=float, default=2.0, help='送信間隔・秒（既定: 2）')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error('--interval は正の有限値を指定してください')
    import cv2
    cv2.setNumThreads(1)
    camera = cv2.VideoCapture(args.camera)
    if not camera.isOpened():
        camera.release()
        parser.exit(1, f'カメラ {args.camera} を開けません。\n')
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    condition = threading.Condition()
    stop = threading.Event()
    state = {'frame': None, 'sequence': 0, 'failed': False}

    def capture():
        try:
            while not stop.is_set():
                ok, frame = camera.read()
                with condition:
                    if not ok:
                        state['failed'] = True
                        condition.notify_all()
                        return
                    state['frame'] = frame
                    state['sequence'] += 1
                    condition.notify_all()
        finally:
            camera.release()

    worker = threading.Thread(target=capture, daemon=True)
    worker.start()
    # プロキシ設定に左右されず、同じPC内でのみ通信する。
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    next_send = time.monotonic()
    last_sequence = 0
    try:
        print(f'カメラ {args.camera}: {args.interval}秒ごとに最新画像を送信（終了: Ctrl+C）', flush=True)
        while True:
            time.sleep(max(0, next_send - time.monotonic()))
            with condition:
                ready = condition.wait_for(
                    lambda: state['failed'] or state['sequence'] > last_sequence, timeout=5
                )
                if state['failed'] or not ready:
                    raise RuntimeError('カメラから新しい画像を取得できません。')
                frame = state['frame'].copy()
                last_sequence = state['sequence']
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise RuntimeError('JPEG変換に失敗しました。')
            request = urllib.request.Request(
                'http://127.0.0.1:8765/infer', data=encoded.tobytes(),
                headers={'Content-Type': 'image/jpeg'}, method='POST',
            )
            try:
                with client.open(request, timeout=30) as response:
                    response.read()
                print('画像を送信し、推論が完了しました。', flush=True)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                print(f'送信失敗（推論側の起動を確認してください）: {error}', flush=True)
            next_send += args.interval
            # 遅れた分を連続送信せず、過去の画像もためない。
            now = time.monotonic()
            if next_send < now:
                next_send += (math.floor((now - next_send) / args.interval) + 1) * args.interval
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2)


if __name__ == '__main__':
    main()
