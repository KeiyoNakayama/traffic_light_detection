"""ROS 2 bridge from green-light detections to a pending robot wait request."""

import json
import threading
import time
import uuid
from typing import Any, Optional

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String


class SignalGoBridge:
    """Heartbeat the robot and acknowledge detector-triggered go commands."""

    def __init__(
            self,
            robot_id: str,
            go_class: str,
            min_confidence: float,
            confirm_frames: int,
            heartbeat_topic: str,
            waiting_topic: str,
            go_topic: str,
            ack_topic: str,
            heartbeat_period_sec: float = 1.0,
            retry_period_sec: float = 0.5) -> None:
        if not robot_id.strip():
            raise ValueError('robot_id must not be empty.')
        if not go_class.strip():
            raise ValueError('go_class must not be empty.')
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError('min_confidence must be between 0 and 1.')
        if confirm_frames < 1:
            raise ValueError('confirm_frames must be at least 1.')
        if heartbeat_period_sec <= 0.0 or retry_period_sec <= 0.0:
            raise ValueError('ROS heartbeat and retry periods must be positive.')

        if not rclpy.ok():
            rclpy.init(args=None)
            self._owns_rclpy_context = True
        else:
            self._owns_rclpy_context = False

        self.robot_id = robot_id.strip()
        self.go_class = go_class.strip()
        self.min_confidence = min_confidence
        self.confirm_frames = confirm_frames
        self.heartbeat_period_sec = heartbeat_period_sec
        self.retry_period_sec = retry_period_sec
        self._lock = threading.Lock()
        self._wait_key: Optional[tuple[str, int, str]] = None
        self._detection_streak = 0
        self._last_confidence = 0.0
        self._last_go_rejected = False
        self._pending_go: Optional[dict[str, Any]] = None
        self._last_publish_at = 0.0
        self._accepted_requests: set[tuple[str, int, str]] = set()
        self._session_id = str(uuid.uuid4())
        self._heartbeat_sequence = 0

        self._node = Node('traffic_signal_go_bridge')
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._heartbeat_pub = self._node.create_publisher(
            String, heartbeat_topic, qos)
        self._go_pub = self._node.create_publisher(String, go_topic, qos)
        self._waiting_sub = self._node.create_subscription(
            String, waiting_topic, self._waiting_callback, qos)
        self._ack_sub = self._node.create_subscription(
            String, ack_topic, self._ack_callback, qos)
        self._heartbeat_timer = self._node.create_timer(
            heartbeat_period_sec, self._heartbeat_callback)
        self._retry_timer = self._node.create_timer(
            retry_period_sec, self._retry_callback)

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._spin_thread = threading.Thread(
            target=self._executor.spin,
            name='traffic-signal-ros',
            daemon=True,
        )
        self._spin_thread.start()
        print(
            f'ROS 2ゴーサイン連携: robot_id={self.robot_id}, '
            f'go_class={self.go_class}, '
            f'min_confidence={self.min_confidence:.2f}, '
            f'confirm_frames={self.confirm_frames}',
            flush=True,
        )

    def process_predictions(self, predictions: dict[str, Any]) -> bool:
        """Publish go once when enough consecutive green detections are seen."""
        with self._lock:
            if self._wait_key is None:
                self._detection_streak = 0
                return False
            if self._wait_key in self._accepted_requests:
                self._detection_streak = 0
                return False
            if (self._pending_go is not None and
                    self._request_key(self._pending_go) == self._wait_key):
                return False

            confidence = self._best_confidence(predictions)
            self._last_confidence = confidence
            if confidence < self.min_confidence:
                self._detection_streak = 0
                return False

            self._detection_streak += 1
            if self._detection_streak < self.confirm_frames:
                return False

            route_id, waypoint_index, request_id = self._wait_key
            self._pending_go = {
                'robot_id': self.robot_id,
                'route_id': route_id,
                'waypoint_index': waypoint_index,
                'request_id': request_id,
                'command': 'go',
            }
            self._detection_streak = 0
            self._publish_pending_locked()
            print(
                f'ゴーサイン送信: class={self.go_class}, '
                f'confidence={confidence:.2f}, route={route_id}, '
                f'waypoint={waypoint_index}, request_id={request_id}',
                flush=True,
            )
            return True

    def status_text(self) -> str:
        """Return the current go-gate state for the camera preview."""
        with self._lock:
            if self._wait_key is None:
                return 'GO: waiting for robot'
            if self._wait_key in self._accepted_requests:
                return 'GO: accepted'
            if (self._pending_go is not None and
                    self._request_key(self._pending_go) == self._wait_key):
                return 'GO: sent; waiting for ACK'
            if self._last_go_rejected:
                return 'GO: rejected by robot'
            if self._last_confidence >= self.min_confidence:
                return (
                    f'GO: confirming {self._detection_streak}/'
                    f'{self.confirm_frames} ({self._last_confidence:.2f})'
                )
            if self._last_confidence > 0.0:
                return (
                    f'GO: below threshold '
                    f'({self._last_confidence:.2f}/{self.min_confidence:.2f})'
                )
            return f'GO: waiting for {self.go_class}'

    def _best_confidence(self, predictions: dict[str, Any]) -> float:
        detections = predictions.get('predictions')
        if not isinstance(detections, list):
            return 0.0

        confidence = 0.0
        for detection in detections:
            if not isinstance(detection, dict):
                continue
            if detection.get('class') != self.go_class:
                continue
            try:
                value = float(detection.get('confidence'))
            except (TypeError, ValueError):
                continue
            if 0.0 <= value <= 1.0:
                confidence = max(confidence, value)
        return confidence

    @staticmethod
    def _request_key(value: dict[str, Any]) -> tuple[str, int, str]:
        return (
            str(value.get('route_id', '')),
            value.get('waypoint_index', -1),
            str(value.get('request_id', '')),
        )

    def _decode_message(self, msg: String, kind: str) -> Optional[dict[str, Any]]:
        if len(msg.data) > 2048:
            print(f'ROS {kind} を無視: メッセージが大きすぎます。', flush=True)
            return None
        try:
            value = json.loads(msg.data)
        except json.JSONDecodeError as error:
            print(f'ROS {kind} を無視: JSON形式が不正です ({error})', flush=True)
            return None
        if not isinstance(value, dict):
            print(
                f'ROS {kind}: JSONルートがオブジェクトではありません。',
                flush=True,
            )
            return None
        return value

    def _waiting_callback(self, msg: String) -> None:
        value = self._decode_message(msg, 'waiting')
        if value is None:
            return
        if value.get('robot_id') != self.robot_id:
            return

        route_id = value.get('route_id')
        waypoint_index = value.get('waypoint_index')
        request_id = value.get('request_id')
        if (value.get('state') != 'waiting_for_go' or
                not isinstance(route_id, str) or not route_id or
                type(waypoint_index) is not int or waypoint_index < 0 or
                not isinstance(request_id, str)):
            print('ROS waitingを無視: 待機要求の形式が不正です。', flush=True)
            with self._lock:
                self._wait_key = None
                self._pending_go = None
                self._detection_streak = 0
                self._last_confidence = 0.0
                self._last_go_rejected = False
            return
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            print('ROS waitingを無視: request_idがUUIDではありません。', flush=True)
            with self._lock:
                self._wait_key = None
                self._pending_go = None
                self._detection_streak = 0
                self._last_confidence = 0.0
                self._last_go_rejected = False
            return

        new_key = (route_id, waypoint_index, request_id)
        with self._lock:
            if new_key != self._wait_key:
                self._wait_key = new_key
                self._pending_go = None
                self._detection_streak = 0
                self._last_confidence = 0.0
                self._last_go_rejected = False
                if new_key not in self._accepted_requests:
                    print(
                        f'信号待ちを受信: route={route_id}, '
                        f'waypoint={waypoint_index}, request_id={request_id}',
                        flush=True,
                    )

    def _heartbeat_callback(self) -> None:
        self._heartbeat_sequence += 1
        value = {
            'robot_id': self.robot_id,
            'session_id': self._session_id,
            'sequence': self._heartbeat_sequence,
        }
        self._heartbeat_pub.publish(
            String(data=json.dumps(value, separators=(',', ':'))))

    def _publish_pending_locked(self) -> None:
        if self._pending_go is None:
            return
        payload = json.dumps(self._pending_go, separators=(',', ':'))
        self._go_pub.publish(String(data=payload))
        self._last_publish_at = time.monotonic()

    def _retry_callback(self) -> None:
        with self._lock:
            if (self._pending_go is None or
                    time.monotonic() - self._last_publish_at <
                    self.retry_period_sec):
                return
            if self._request_key(self._pending_go) != self._wait_key:
                self._pending_go = None
                return
            self._publish_pending_locked()

    def _ack_callback(self, msg: String) -> None:
        value = self._decode_message(msg, 'ack')
        if value is None or value.get('robot_id') != self.robot_id:
            return
        if type(value.get('waypoint_index')) is not int:
            return
        if not isinstance(value.get('route_id'), str):
            return
        if not isinstance(value.get('request_id'), str):
            return

        key = self._request_key(value)
        with self._lock:
            if self._pending_go is None or key != self._request_key(
                    self._pending_go):
                return
            if value.get('accepted') is True:
                self._accepted_requests.add(key)
                self._pending_go = None
                print(
                    f'ロボットがゴーサインを受理: route={key[0]}, '
                    f'waypoint={key[1]}, request_id={key[2]}',
                    flush=True,
                )
            elif value.get('accepted') is False:
                self._pending_go = None
                self._last_go_rejected = True
                print(
                    f'ゴーサイン拒否: reason={value.get("reason", "unknown")}',
                    flush=True,
                )

    def close(self) -> None:
        """Stop the ROS executor and release its node."""
        self._executor.shutdown(timeout_sec=2.0)
        self._spin_thread.join(timeout=2.0)
        self._executor.remove_node(self._node)
        self._node.destroy_node()
        if self._owns_rclpy_context and rclpy.ok():
            rclpy.shutdown()
