#!/usr/bin/env python3
"""Preserve Humble's Twist-over-JointJog selection with Jazzy's explicit API."""

from copy import deepcopy
import math

import rclpy
from control_msgs.msg import JointJog
from geometry_msgs.msg import TwistStamped
from moveit_msgs.srv import ServoCommandType
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.time import Time


TWIST = ServoCommandType.Request.TWIST
JOINT = ServoCommandType.Request.JOINT_JOG


class ServoCommandRouter(Node):
    def __init__(self):
        super().__init__("servo_command_router")
        self.timeout = self.declare_parameter("incoming_command_timeout", 0.1).value
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("incoming_command_timeout must be positive and finite")
        self.latest = {}
        self.sequence = 0
        self.active = None
        self.pending = None
        self.sent = None
        self.moving = False
        self.publishers_by_type = {
            TWIST: self.create_publisher(TwistStamped, "servo_twist", 10),
            JOINT: self.create_publisher(JointJog, "servo_joint", 10),
        }
        self.create_subscription(TwistStamped, "twist_commands", lambda m: self.receive(TWIST, m), 10)
        self.create_subscription(JointJog, "joint_commands", lambda m: self.receive(JOINT, m), 10)
        self.switch = self.create_client(ServoCommandType, "switch_command_type")
        # ROS time also controls input expiry when a simulation is slowed or paused.
        self.create_timer(0.01, self.route)

    @staticmethod
    def values(kind, msg):
        if kind == JOINT:
            return msg.velocities
        return [getattr(vector, axis) for vector in (msg.twist.linear, msg.twist.angular)
                for axis in ("x", "y", "z")]

    def fresh(self, msg):
        stamp = Time.from_msg(msg.header.stamp)
        age = (self.get_clock().now() - stamp).nanoseconds * 1e-9
        return stamp.nanoseconds > 0 and age < self.timeout

    def receive(self, kind, msg):
        values = self.values(kind, msg)
        if not all(math.isfinite(v) for v in values):
            self.get_logger().warning("Discarding a non-finite Servo command")
            return
        if kind == JOINT and len(msg.joint_names) != len(values):
            self.get_logger().warning("Discarding a JointJog with mismatched names and velocities")
            return
        # Match Servo's header-based timeout without refreshing old motion.
        if not self.fresh(msg):
            return
        self.sequence += 1
        self.latest[kind] = (self.sequence, msg)
        self.route()

    def stop_active(self):
        if self.active is None or not self.moving:
            return
        msg = deepcopy(self.latest[self.active][1])
        msg.header.stamp = self.get_clock().now().to_msg()
        if self.active == JOINT:
            msg.velocities = [0.0] * len(msg.joint_names)
            msg.displacements = []
        else:
            for vector in (msg.twist.linear, msg.twist.angular):
                vector.x = vector.y = vector.z = 0.0
        self.publishers_by_type[self.active].publish(msg)
        self.moving = False

    def route(self):
        desired = None
        for kind in (TWIST, JOINT):
            item = self.latest.get(kind)
            if item and self.fresh(item[1]) and any(self.values(kind, item[1])):
                desired = kind
                break
        # Heartbeat zeroes stop the active type without switching it back and forth.
        if desired is None:
            self.stop_active()
        if self.pending is not None:
            return
        target = desired if desired is not None else self.active
        if target is None:
            target = TWIST
        if target != self.active:
            self.stop_active()
            if self.switch.service_is_ready():
                request = ServoCommandType.Request(command_type=target)
                self.pending = self.switch.call_async(request)
                self.pending.add_done_callback(lambda future: self.switched(target, future))
            return
        item = self.latest.get(target)
        if desired is not None and item and self.sent != item[0]:
            # Keep the original stamp; an acknowledgement must not revive old motion.
            self.publishers_by_type[target].publish(item[1])
            self.sent = item[0]
            self.moving = True

    def switched(self, target, future):
        self.pending = None
        try:
            if not future.result().success:
                self.get_logger().error("MoveIt Servo rejected command type selection", throttle_duration_sec=1.0)
                return
        except Exception as error:
            self.get_logger().error(f"MoveIt Servo command type selection failed: {error}")
            return
        self.active = target
        self.sent = None
        self.route()


def main():
    rclpy.init()
    node = ServoCommandRouter()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if rclpy.ok():
            node.stop_active()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
