"""Start only traffic detection and camera transmission; navigation runs separately."""
import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from pathlib import Path


def setup(context):
    def arg(name):
        return LaunchConfiguration(name).perform(context)
    directory = os.path.expanduser(arg('detector_dir'))
    python = os.path.join(directory, '.venv', 'bin', 'python')
    return [
        ExecuteProcess(cmd=[python, os.path.join(directory, 'traffic_light_realtime_detect.py'),
            '--device', arg('device'), '--display', arg('display'), '--ros-services',
            '--control-service', '/mission/control_traffic_light',
            '--go-service', '/waypoint_sender_node/next_wp',
            '--service-go-class', arg('go_class'), '--service-go-confidence', arg('confidence'),
            '--service-go-frames', arg('confirm_frames')], output='screen'),
        ExecuteProcess(cmd=[python, os.path.join(directory, 'camera_image_sender.py'),
            '--camera', arg('camera'), '--interval', arg('interval')], output='screen'),

    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('detector_dir', default_value=str(Path(__file__).resolve().parents[1])),
        DeclareLaunchArgument('device', default_value='cuda'),
        DeclareLaunchArgument('display', default_value='window'),
        DeclareLaunchArgument('camera', default_value='2'),
        DeclareLaunchArgument('interval', default_value='0.1'),
        DeclareLaunchArgument('go_class', default_value='Trafficlight_blue'),
        DeclareLaunchArgument('confidence', default_value='0.8'),
        DeclareLaunchArgument('confirm_frames', default_value='3'),
        OpaqueFunction(function=setup),
    ])
