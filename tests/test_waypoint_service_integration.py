"""Mock Nav2 + actual C++ waypoint sender + detector service bridge (no robot)."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rclpy
from rclpy.action import ActionServer
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from nav2_msgs.action import NavigateToPose
from std_srvs.srv import Trigger
from ros_service_control import ServiceControl

ROOT = Path.home() / 'waypoint_service_ws'


class IntegrationTest(unittest.TestCase):
    def test_arrival_detection_resume(self):
        self.assertEqual(os.environ.get('ROS_DOMAIN_ID'), '179')
        self.assertEqual(os.environ.get('ROS_LOCALHOST_ONLY'), '1')
        rclpy.init()
        peer = Node('mock_nav2_signal_test')
        goals = []
        def execute(handle):
            goals.append(handle.request.pose.pose.position.x)
            handle.succeed()
            return NavigateToPose.Result()
        action = ActionServer(peer, NavigateToPose, '/navigate_to_pose', execute)
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(peer)
        thread = threading.Thread(target=executor.spin, daemon=True)
        thread.start()
        bridge = ServiceControl(SimpleNamespace(
            service_go_class='Trafficlight_blue', service_go_confidence=.8,
            service_go_frames=3, control_service='/mission/control_traffic_light',
            go_service='/waypoint_sender_node/next_wp'))
        log = open('/tmp/waypoint_signal_integration.log', 'w')
        process = subprocess.Popen([
            str(ROOT / 'install/waypoint_tools/lib/waypoint_tools/waypoint_sender_node'),
            '--ros-args', '-p', 'yaml_path:=' + str(ROOT / 'src/waypoint_tools/config/waypoints/traffic_signal_example.yaml'),
            '-p', 'frame_id:=map', '-p', 'robot_frame:=base_link', '-p', 'switch_radius:=0.1',
            '-p', 'max_retries:=0', '-p', 'skip_on_failure:=false'], stdout=log, stderr=log)
        def wait_for(condition, seconds=8):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if condition():
                    return
                time.sleep(.02)
            self.fail('Timed out; see /tmp/waypoint_signal_integration.log')
        def call(name):
            client = peer.create_client(Trigger, name)
            self.assertTrue(client.wait_for_service(timeout_sec=8))
            future = client.call_async(Trigger.Request())
            wait_for(future.done)
            result = future.result()
            peer.destroy_client(client)
            return result
        try:
            self.assertIsNone(bridge.begin_frame())
            self.assertEqual(goals, [])
            self.assertTrue(call('/waypoint_sender_node/send_all').success)
            wait_for(lambda: bridge.begin_frame() is not None)
            # Wait for the sender to consume the SetBool response and enter stopped state.
            wait_for(lambda: 'Stopped at waypoint 0' in Path('/tmp/waypoint_signal_integration.log').read_text())
            self.assertEqual(goals, [1.0])
            green = {'predictions': [{'class': 'Trafficlight_blue', 'confidence': .95}]}
            for _ in range(3):
                bridge.observe(bridge.begin_frame(), green)
            wait_for(lambda: bridge.status_text() == 'GO ACCEPTED')
            wait_for(lambda: len(goals) == 2)
            self.assertEqual(goals, [1.0, 3.0])
            self.assertIsNone(bridge.begin_frame())
            time.sleep(.2)
            self.assertEqual(len(goals), 2)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            log.close()
            bridge.close()
            executor.shutdown()
            thread.join(timeout=2)
            action.destroy()
            peer.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    unittest.main()
