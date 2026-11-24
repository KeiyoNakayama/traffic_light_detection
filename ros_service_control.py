"""SetBool activation and one-shot Trigger go notification for waypoint tasks."""
import threading
import time



def add_arguments(parser):
    parser.add_argument('--ros-services', action='store_true', help='ROSサービスで認識を開始・停止')
    parser.add_argument('--control-service', default='/mission/control_traffic_light')
    parser.add_argument('--go-service', help='青確認後に一度呼ぶTriggerサービス（接続先の確認が必要）')
    parser.add_argument('--service-go-class', help='青信号の完全一致クラス名')
    parser.add_argument('--service-go-confidence', type=float, default=0.8)
    parser.add_argument('--service-go-frames', type=int, default=3)


def validate_arguments(parser, args):
    if not args.ros_services:
        return
    if args.prepare_model or args.check_model or getattr(args, 'ros_go', False):
        parser.error('--ros-services はモデル準備・検証・--ros-go と併用できません')
    if not args.go_service or not args.service_go_class:
        parser.error('--ros-services には --go-service と --service-go-class が必要です')
    if not 0 <= args.service_go_confidence <= 1 or args.service_go_frames < 1:
        parser.error('信頼度は0〜1、連続確認数は1以上にしてください')
    if args.control_service == args.go_service:
        parser.error('開始停止とゴーサインは別のサービス名にしてください')


class ServiceControl:
    def __init__(self, args):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from std_srvs.srv import SetBool, Trigger
        self.trigger_type = Trigger
        self.context = Context()
        rclpy.init(args=[], context=self.context)
        self.node = Node('traffic_light_service_control', context=self.context)
        self.lock = threading.Lock()
        self.active = False
        self.generation = 0
        self.streak = 0
        self.last_frame = 0.0
        self.pending = None
        self.sent_at = 0.0
        self.status = 'WAIT START'
        self.go_class = args.service_go_class
        self.confidence = args.service_go_confidence
        self.frames = args.service_go_frames
        self.client = self.node.create_client(Trigger, args.go_service)
        self.service = self.node.create_service(SetBool, args.control_service, self.control)
        self.timer = self.node.create_timer(0.05, self.tick)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        self.node.get_logger().info(f'待機: {args.control_service} (SetBool), go: {args.go_service} (Trigger)')

    def control(self, request, response):
        with self.lock:
            # A sent request cannot be recalled by SetBool(false).
            if self.pending is not None and request.data:
                response.success = False
                response.message = 'ゴーサイン応答待ちです。再開始できません。'
                return response
            if request.data and self.active:
                response.success = True
                response.message = '認識中（重複開始）'
                return response
            self.generation += 1
            self.active = bool(request.data)
            self.streak = 0
            self.last_frame = 0.0
            self.status = 'DETECTING' if self.active else 'STOPPED'
            response.success = True
            response.message = '認識開始' if self.active else '認識停止（送信済み要求は取り消せません）'
        return response

    def begin_frame(self):
        with self.lock:
            return (self.generation, time.monotonic()) if self.active else None

    def observe(self, token, predictions):
        now = time.monotonic()
        with self.lock:
            if token is None or not self.active or token[0] != self.generation:
                return
            if now - token[1] > 1.0:
                self.streak = 0
                return
            items = predictions.get('predictions', [])
            green = any(p.get('class') == self.go_class and
                        float(p.get('confidence', 0)) >= self.confidence for p in items)
            # Conflicting red detections inhibit release.
            red = any('red' in str(p.get('class', '')).lower() and
                      float(p.get('confidence', 0)) >= self.confidence for p in items)
            if now - self.last_frame > 1.0:
                self.streak = 0
            self.last_frame = now
            self.streak = self.streak + 1 if green and not red else 0
            self.status = f'CONFIRM {self.streak}/{self.frames}'

    def tick(self):
        with self.lock:
            if self.pending is not None:
                if self.pending.done():
                    try:
                        result = self.pending.result()
                        self.status = 'GO ACCEPTED' if result.success else 'GO REJECTED'
                        self.node.get_logger().info(f'{self.status}: {result.message}')
                    except Exception as error:
                        self.status = 'GO ERROR - RESTART REQUIRED'
                        self.node.get_logger().error(str(error))
                    self.pending = None
                elif time.monotonic() - self.sent_at > 5:
                    # Never retry a non-idempotent next-waypoint request.
                    self.status = 'GO RESPONSE UNKNOWN - NO RETRY'
                return
            if not self.active or self.streak < self.frames:
                return
            if time.monotonic() - self.last_frame > 1.0:
                self.streak = 0
                return
            if not self.client.service_is_ready():
                self.status = 'WAIT GO SERVICE'
                return
            self.active = False
            self.generation += 1
            self.streak = 0
            try:
                self.pending = self.client.call_async(self.trigger_type.Request())
                self.sent_at = time.monotonic()
                self.status = 'GO RESPONSE WAIT'
            except Exception as error:
                self.status = 'GO ERROR - RESTART REQUIRED'
                self.node.get_logger().error(str(error))

    def status_text(self):
        with self.lock:
            return self.status

    def close(self):
        with self.lock:
            self.active = False
            self.generation += 1
        self.executor.shutdown()
        self.thread.join(timeout=2)
        self.node.destroy_node()
        self.context.shutdown()
