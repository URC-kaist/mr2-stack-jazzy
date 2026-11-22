#!/usr/bin/env node
// No serial ports are opened and no rover command topics are published.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

async function main() {
  const [gateway, output] = process.argv.slice(2);
  const requireGateway = createRequire(path.join(gateway, 'package.json'));
  const result = { status: 'running', node: process.version, checks: [] };
  let rcl;
  let node;
  const check = (name, detail) => result.checks.push({ name, status: 'passed', detail });
  try {
    requireGateway('dotenv');
    const { WebSocketServer } = requireGateway('ws');
    if (typeof WebSocketServer !== 'function') throw new Error('ws did not load');
    const { SerialPort } = requireGateway('serialport');
    const native = requireGateway('@serialport/bindings-cpp').autoDetect();
    if (typeof SerialPort !== 'function' || typeof native.open !== 'function') {
      throw new Error('serialport native binding did not load');
    }
    check('gateway_modules_and_serial_native_binding', { portOpened: false });
    rcl = requireGateway('rclnodejs');
    await rcl.init();
    node = new rcl.Node('mr2_setup_gateway_check');
    for (const type of ['std_msgs/msg/String', 'ublox_ubx_msgs/msg/UBXNavSvin', 'rtcm_msgs/msg/Message']) {
      if (!rcl.require(type)) throw new Error(`Message type did not load: ${type}`);
      // Resolve the native type support too; importing the generated JS alone
      // would miss a broken Jazzy custom-interface library search path.
      node.createPublisher(type, `/mr2_setup/${type.split('/')[0]}`);
    }
    check('jazzy_and_generated_message_types', { ros: process.env.ROS_DISTRO, nativePublishersCreated: 3, messagesPublished: 0 });
    const objects = process.report.getReport().sharedObjects;
    const rosObjects = objects.filter(name => name.includes('/opt/ros/'));
    if (!rosObjects.some(name => name.includes('/opt/ros/jazzy/') && name.includes('librcl.'))) {
      throw new Error('rclnodejs did not load Jazzy librcl');
    }
    if (objects.some(name => name.includes('/opt/ros/humble/'))) throw new Error('Humble library leaked into Jazzy');
    check('native_ros_library_paths', rosObjects);
    result.status = 'passed';
  } catch (error) {
    result.status = 'failed';
    result.error = error.stack || String(error);
  } finally {
    if (node) node.destroy();
    if (rcl && (!rcl.isInitialized || rcl.isInitialized())) await rcl.shutdown();
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
  }
  console.log(JSON.stringify({ status: result.status, checks: result.checks.length, result: output, error: result.error }));
  if (result.status !== 'passed') process.exitCode = 1;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
