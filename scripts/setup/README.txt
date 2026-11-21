MR2 Ubuntu 24.04 / ROS Jazzy environment setup
===========================================

Clone this repository with --recurse-submodules (or run git submodule update
--init --recursive afterwards). The pinned GNSS message source packages are
required for dev/base and rover with --with-rover-direct.

Public entrypoints (run from any working directory):

  bash scripts/setup_dev.bash
  bash scripts/setup_rover.bash
  bash scripts/setup_base.bash

dev:   CPU perception environment, ROS/control/navigation/simulation development,
       gateway and dashboard. Supports Ubuntu 24.04 amd64 and arm64.
rover: Jetson Orin NX, JetPack 7.2.1 / L4T 39.2.1, system GPU SDK and GPU
       perception. Optional --with-rover-direct adds local gateway/dashboard.
base:  Ubuntu 24.04 base computer, ROS message interfaces, Node gateway and
       dashboard; no perception/GPU Python environment.

Ubuntu installation and JetPack flashing are prerequisites. These scripts do not
perform OS release upgrades or flash a BSP. A mismatching platform fails before
installation. A rover run on Orin Nano requires --allow-nano-validation and is
reported as Nano validation, not NX hardware certification.

Options:
  --plan           Print the selected package/artifact plan without installation.
  --check          Repeat acceptance checks without installing packages. This
                   includes dashboard typecheck/build and writes result files.
  --prefix PATH    Toolchain, cache, venv, interface and report directory;
                   default: the invoking user's ~/.local/share/mr2.
  --config FILE    Optional device-config JSON. Exports ROS_DOMAIN_ID, MR2_BASE_IP,
                   MR2_ROVER_IP, MR2_CAN_INTERFACE and BASE_XBEE_DEVICE when set.
                   Without this option, state/PROFILE/device-config.json is read
                   if present. A template is generated; null values are omitted.
  --with-rover-direct
                   Add web dependencies to the rover profile.

Run as a normal user, allowing sudo for apt/system configuration. Invoking the
entrypoint with sudo is also supported: user-owned environments are created for
SUDO_USER, not root. Do not run under a root-only account. A shared prefix is
locked to prevent concurrent setup processes.

Package policy:
  - Official ROS Jazzy apt packages.
  - Ubuntu apt OpenCV 4.6 and NumPy 1.26; apt preferences prevent NVIDIA OpenCV
    from winning candidate selection. On Jetson, replacing NVIDIA OpenCV can
    remove NVIDIA umbrella metapackages, while explicitly retaining actual SDK
    components. Unrelated removals and BSP replacement are rejected.
  - JetPack 7.2.1's exact CUDA/cuDNN/TensorRT package versions remain pinned.
  - A perception venv uses system Python 3.12 with --system-site-packages.
    Fixed wheel files are SHA-256 checked and installed with --no-deps. Three
    provider exceptions (Ubuntu cv2 and system CUDA/cuDNN) are explicitly audited.
  - dev uses torch 2.13.0+cpu / torchvision 0.28.0+cpu; rover uses +cu132.
    Ultralytics stays at 8.4.46. The default runtime is PyTorch.
  - Node 24.15.0 and npm 11.12.1 live under the user prefix. The system Node is
    not replaced. npm ci preserves the reviewed gateway/dashboard locks.

The shared implementation is setup.py; versions.json holds the reviewed roots,
wheel and toolchain files/URLs/hashes. The small ROS apt-source bootstrap .deb in
assets/ is the unmodified official ros-apt-source 1.3.0 noble package. Its digest
and upstream release URL are in versions.json.

System SDK library selection (2026-11-22): retain the unmodified official
torchvision GPU wheel, including its bundled libcudart and libnvjpeg files.
Those bundled files are installed but must remain unused at runtime. The rover
installer creates two reviewed symlinks under PREFIX/runtime/rover-sdk-libs:
the wheel's hashed library names point to the apt-owned JetPack 7.2.1 SDK files.
perception.bash places this small directory first in LD_LIBRARY_PATH, ahead of
torchvision/image.so's DT_RUNPATH, without modifying wheel files or the global
linker configuration. Exact aliases/targets are recorded in versions.json;
resolved paths, package owners and hashes go to gpu-library-providers.json.

Use state/rover/perception-python or source state/rover/perception.bash. Merely
activating venv/bin/activate bypasses this SDK path selection. The generated
environment refuses missing/incorrect aliases; --check verifies the aliases and
actual mappings after GPU operations, including nvJPEG decode. It fails if a
core GPU library is loaded from the Python environment. This rule covers core
CUDA/cuDNN/nvJPEG; the separately selected cuSPARSELt/NCCL/NVSHMEM auxiliary
wheels remain permitted. Wheel upgrades require reviewing the hashed names and
repeating these checks.

The user accepted the existing PyTorch sm_87 compatibility warning on
2026-11-22 and chose to retain torch 2.13.0+cu132 after successful execution
tests. Record the warning and test coverage; the warning alone is not a failure.

The installer builds ublox_ubx_msgs and rtcm_msgs with --merge-install under
interfaces/jazzy-ARCH in the user prefix, not an old Humble install directory,
so rclnodejs can generate and load both custom Jazzy types from one prefix.
Custom JavaScript types are explicitly generated from their message sources;
this avoids rclnodejs 1.8.1's package-name parsing issue under ~/.local/share.
It reuses interface/node_modules outputs only when their provenance stamp
matches the source/lock/runtime/architecture.

Generated environment files:
  ~/.local/share/mr2/state/PROFILE/env.bash
  ~/.local/share/mr2/state/PROFILE/perception.bash       (dev/rover)
  ~/.local/share/mr2/state/PROFILE/perception-python     (dev/rover)

Source env.bash for system ROS builds and Node work. Use the generated
perception-python wrapper to run perception Python entrypoints; on rover this
also applies the required system SDK library selection. Activating a venv does not rewrite existing ROS executables'
shebangs. env.bash deactivates an active Python venv and clears old ROS workspace
paths; source a freshly built Jazzy application workspace afterwards when needed.
Wiring the migrated launch/service to this interpreter is still
required. This installer does not silently edit rover application launch files.

Acceptance checks:
  - platform, apt package ownership, selected Python interpreter and versions;
  - Jazzy rclpy/native node creation and standard message imports;
  - real cv_bridge image roundtrip and OpenCV/NumPy array operations;
  - an actual C++ OpenCV compile/link/run against Ubuntu headers and libraries;
  - venv wheel versions, declared dependencies, forbidden duplicate providers;
  - actual perception imports and native Polars operation;
  - CPU tensor operation and torchvision NMS (dev);
  - GPU tensor/NMS, FP32+FP16 convolution and nvJPEG image decode (rover),
    checking that core GPU mappings are apt-owned system SDK libraries;
  - system ONNX package/tiny graph checker and TensorRT import on rover;
  - Node/npm versions, serialport native binding, rclnodejs node/types and
    loaded Jazzy library paths, dashboard typecheck/production build (dev/base).

Reports live under state/PROFILE/: result.json, setup.log, system-health.json,
perception-health.json, gateway-health.json, apt-plan.txt, dpkg-installed.tsv,
apt-roots.lock.json, wheel-artifacts.lock.json and gpu-library-providers.json,
as applicable. Missing or
failed mandatory checks cause a nonzero exit; non-applicable and unverified
hardware/application tests are not reported as passed.

The scripts read application configuration or prepare an example, but do not
assign OS device IDs/IPs, bring up CAN, change clock/network policy, register a CI runner,
start rover services or actuate motors. Full model/robot/network end-to-end
tests, camera USB capture, NX encoder behavior and TensorRT engine inference
remain separate validation stages. No settings are appended to ~/.bashrc.

To run the installer regression checks without installing host packages:
  /usr/bin/python3 -m unittest discover -s scripts/setup/tests -v

Integration validation and the baseline dashboard type correction: see VALIDATION.txt.
