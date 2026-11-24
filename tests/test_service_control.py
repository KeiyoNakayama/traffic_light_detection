"""Local ROS service integration test; never connects to navigation services."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rclpy
from rclpy.node import Node
from std_srvs.srv import SetBool, Trigger
from ros_service_control import ServiceControl


class ServiceTest(unittest.TestCase):
    def test_activation_stale_frames_conflict_and_one_shot(self):
        rclpy.init()
        node = Node('test_signal_service_peer')
        calls = []
        def go(request, response):
            calls.append(request)
            response.success = True
            response.message = 'test accepted'
            return response
        node.create_service(Trigger, '/test_signal_only/go', go)
        bridge = ServiceControl(SimpleNamespace(
            service_go_class='Trafficlight_blue', service_go_confidence=0.8,
            service_go_frames=3, control_service='/test_signal_only/control',
            go_service='/test_signal_only/go'))
        client = node.create_client(SetBool, '/test_signal_only/control')
        def control(value):
            self.assertTrue(client.wait_for_service(timeout_sec=3))
            future = client.call_async(SetBool.Request(data=value))
            rclpy.spin_until_future_complete(node, future, timeout_sec=3)
            self.assertTrue(future.done())
            self.assertTrue(future.result().success)
        green = {'predictions': [{'class': 'Trafficlight_blue', 'confidence': .95}]}
        try:
            self.assertIsNone(bridge.begin_frame())
            control(True)
            old = bridge.begin_frame()
            control(False)
            control(True)
            bridge.observe(old, green)
            self.assertEqual(bridge.streak, 0)
            bridge.observe(bridge.begin_frame(), green)
            control(True)  # Duplicate start must not reset the streak.
            self.assertEqual(bridge.streak, 1)
            bridge.observe(bridge.begin_frame(), {'predictions': green['predictions'] +
                           [{'class': 'Trafficlight_red', 'confidence': .99}]})
            self.assertEqual(bridge.streak, 0)
            bridge.observe((bridge.generation, time.monotonic() - 2), green)
            self.assertEqual(bridge.streak, 0)
            for _ in range(3):
                bridge.observe(bridge.begin_frame(), green)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and bridge.status_text() != 'GO ACCEPTED':
                rclpy.spin_once(node, timeout_sec=.05)
            self.assertEqual(bridge.status_text(), 'GO ACCEPTED')
            self.assertEqual(len(calls), 1)
            self.assertIsNone(bridge.begin_frame())
            for _ in range(5):
                bridge.observe(old, green)
                rclpy.spin_once(node, timeout_sec=.05)
            self.assertEqual(len(calls), 1)
        finally:
            bridge.close()
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    unittest.main()
