"""Keep HighGUI calls on the main thread and display only the latest result."""
import os
import sys
from threading import Lock

import cv2
import numpy as np


class WindowPreview:
    def __init__(self):
        if sys.platform.startswith('linux') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
        ):
            raise RuntimeError('画面に接続できません。デスクトップ上のターミナルで起動してください。')
        gui = next((line for line in cv2.getBuildInformation().splitlines()
                    if 'GUI:' in line), '')
        if not gui or 'NONE' in gui:
            raise RuntimeError('GUI対応のOpenCVが必要です（現在はheadless版です）。')
        self.title = 'Traffic Light Detection - Q / Esc to quit'
        self.lock = Lock()
        self.pending = None
        cv2.namedWindow(self.title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.title, 960, 540)
        waiting = np.zeros((540, 960, 3), dtype=np.uint8)
        cv2.putText(waiting, 'Waiting for camera images...', (40, 270),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        cv2.imshow(self.title, waiting)
        cv2.waitKey(1)

    def publish(self, frame):
        with self.lock:
            self.pending = frame.copy()

    def tick(self):
        with self.lock:
            frame, self.pending = self.pending, None
        if frame is not None:
            cv2.imshow(self.title, frame)
        key = cv2.waitKey(10) & 0xff
        return key not in (ord('q'), ord('Q'), 27) and cv2.getWindowProperty(
            self.title, cv2.WND_PROP_VISIBLE) >= 1

    def close(self):
        cv2.destroyAllWindows()
