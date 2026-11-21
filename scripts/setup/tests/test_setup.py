"""Regression tests for the installer's boundaries; no host packages are changed."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mr2_setup", ROOT / "setup.py")
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)
HEALTH_SPEC = importlib.util.spec_from_file_location("mr2_health", ROOT / "check_python.py")
HEALTH = importlib.util.module_from_spec(HEALTH_SPEC)
HEALTH_SPEC.loader.exec_module(HEALTH)


class InstallerBoundaries(unittest.TestCase):
    def test_sdk_alias_audit_rejects_missing_wrong_and_unexpected_libraries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root / "aliases"
            directory.mkdir()
            sdk = root / "sdk.so"
            sdk.write_bytes(b"SDK fixture")
            manifest = {"gpu_library_aliases": {"libcudart.hash.so.13": {"system_path": str(sdk)}}}
            link = directory / "libcudart.hash.so.13"
            with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": str(directory)}):
                with self.assertRaises(RuntimeError):
                    HEALTH.gpu_alias_paths(manifest, directory)
                wrong = root / "wheel.so"
                wrong.write_bytes(b"wheel fixture")
                link.symlink_to(wrong)
                with self.assertRaises(RuntimeError):
                    HEALTH.gpu_alias_paths(manifest, directory)
                link.unlink()
                link.symlink_to(sdk)
                self.assertEqual(HEALTH.gpu_alias_paths(manifest, directory), {link.name: str(sdk)})
                (directory / "unreviewed.so").write_bytes(b"unexpected")
                with self.assertRaises(RuntimeError):
                    HEALTH.gpu_alias_paths(manifest, directory)

    def test_rover_launcher_refuses_missing_sdk_aliases_and_restores_ros_build_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = self.installer(root / "prefix", "rover")
            sdk = root / "sdk.so"
            sdk.write_bytes(b"fixture")
            name = "libcudart.hash.so.13"
            s.lock["gpu_library_aliases"] = {name: {"system_path": str(sdk)}}
            s.venv.joinpath("bin").mkdir(parents=True)
            s.venv.joinpath("bin/activate").write_text(":\n")
            s.environments()
            fixture = root / "ros-setup.bash"
            fixture.write_text('export LD_LIBRARY_PATH=/opt/ros/jazzy/lib\n')
            for filename in ["env.bash", "perception.bash"]:
                target = s.state / filename
                target.write_text(target.read_text().replace('/opt/ros/jazzy/setup.bash', str(fixture)))
            args = ['bash', '-euo', 'pipefail', '-c', 'source "$1"', 'test', str(s.state / "perception.bash")]
            p = subprocess.run(args, text=True, capture_output=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("SDK library alias", p.stderr)
            directory = s.gpu_library_directory()
            directory.mkdir(parents=True)
            (directory / name).symlink_to(sdk)
            p = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                                'source "$1"; test "$LD_LIBRARY_PATH" = "$3:/opt/ros/jazzy/lib"; '
                                'source "$2"; test "$LD_LIBRARY_PATH" = /opt/ros/jazzy/lib',
                                'test', str(s.state / "perception.bash"), str(s.state / "env.bash"), str(directory)],
                               text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_hashed_cuda_runtime_cannot_escape_provider_audit(self):
        for name in ["libcudart.so.13.2.86", "libcudart.3eda8113.so.13", "libcudart-3eda8113.so.13",
                     "libcublasLt.01234567.so.13", "libnvjpeg.4a0b5c81.so.13",
                     "libcuda.so.1.1", "libcudnn_ops.so.9"]:
            with self.subTest(name=name):
                self.assertTrue(HEALTH.is_core_gpu_library(name))
        for name in ["libnccl.so.2", "libnvshmem_host.so.3", "libcusparseLt.so.0", "libtorch_cuda.so"]:
            with self.subTest(name=name):
                self.assertFalse(HEALTH.is_core_gpu_library(name))

    def installer(self, directory, profile="dev"):
        return SETUP.Setup(argparse.Namespace(profile=profile, prefix=str(directory), check=False,
                                             plan=False, allow_nano_validation=False,
                                             with_rover_direct=False))

    def test_cpu_and_gpu_artifacts_do_not_cross(self):
        lock = json.loads((ROOT / "versions.json").read_text())
        for name, artifacts in lock["wheels"].items():
            by_name = {a["package"]: a for a in artifacts}
            self.assertEqual(len(by_name), len(artifacts))
            self.assertNotIn("numpy", by_name)
            self.assertNotIn("opencv-python", by_name)
            self.assertNotIn("cuda-toolkit", by_name)
            self.assertNotIn("nvidia-cudnn-cu13", by_name)
            for a in artifacts:
                self.assertRegex(a["sha256"], r"^[a-f0-9]{64}$")
            if name.startswith("cpu-"):
                self.assertTrue(by_name["torch"]["version"].endswith("+cpu"))
                self.assertFalse(any(n.startswith("nvidia-") for n in by_name))
                arch = "x86_64" if name.endswith("amd64") else "aarch64"
                self.assertIn(arch, by_name["torch"]["filename"])
                self.assertIn(arch, by_name["polars-runtime-32"]["filename"])
            else:
                self.assertEqual(by_name["torch"]["version"], "2.13.0+cu132")

    def test_bootstrap_bytes_match_manifest(self):
        info = json.loads((ROOT / "versions.json").read_text())["ros_apt_source"]
        self.assertEqual(SETUP.sha256(ROOT / "assets" / info["filename"]), info["sha256"])

    def test_entrypoint_cannot_be_changed_with_an_abbreviated_profile(self):
        p = subprocess.run(['bash', str(ROOT.parent / 'setup_dev.bash'), '--prof=base', '--plan'],
                           text=True, capture_output=True)
        self.assertEqual(p.returncode, 2, p.stderr)

    def test_archive_cannot_escape_destination(self):
        for name, link in [("package/../../escape", None), ("package/link", "../../escape")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                archive = Path(td) / "input.tar"
                with tarfile.open(archive, "w") as tar:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                        tar.addfile(member)
                    else:
                        member.size = 1
                        tar.addfile(member, io.BytesIO(b"x"))
                with self.assertRaises(SETUP.SetupError):
                    SETUP.Setup.extract(archive, Path(td) / "output")
                self.assertFalse((Path(td) / "escape").exists())

    def test_unrelated_apt_removal_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "state")
            s.run = mock.Mock(return_value="Remv ubuntu-desktop [1.0]\n")
            with mock.patch.object(s, "installed", return_value=None), self.assertRaises(SETUP.SetupError):
                s.install_apt()
            self.assertEqual(s.run.call_count, 1)
            self.assertIn("-s", s.run.call_args.args[0])

    def test_bsp_replacement_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "state", "rover")
            s.run = mock.Mock(return_value="Inst nvidia-l4t-core [39.2.1] (39.2.2 NVIDIA [arm64])\n")
            with mock.patch.object(s, "installed", return_value=None), self.assertRaises(SETUP.SetupError):
                s.install_apt()
            self.assertEqual(s.run.call_count, 1)

    def test_bad_cached_artifact_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix")
            s.directory(s.cache)
            cached = s.cache / "fixture.bin"
            cached.write_bytes(b"wrong")
            good = b"verified replacement"
            item = dict(filename=cached.name, url="https://example.invalid/fixture.bin",
                        sha256=hashlib.sha256(good).hexdigest())
            with mock.patch.object(SETUP.urllib.request, "urlopen", return_value=io.BytesIO(good)):
                self.assertEqual(s.artifact(item).read_bytes(), good)

    def test_sudo_created_parent_directories_remain_user_owned(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix")
            leaf = s.prefix / "state/dev/native-health"
            with mock.patch.object(SETUP.os, "geteuid", return_value=0), mock.patch.object(SETUP.os, "chown") as chown:
                s.directory(leaf)
            changed = {call.args[0] for call in chown.call_args_list}
            self.assertEqual(changed, {s.prefix, s.prefix / "state", s.prefix / "state/dev", leaf})
            self.assertNotIn(Path(td), changed)

    def test_apt_install_stops_when_system_partition_is_too_small(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix")
            s.run = mock.Mock(side_effect=["Package: fixture\nInstalled-Size: 2048\n", ""])
            with mock.patch.object(SETUP.shutil, "disk_usage", return_value=mock.Mock(free=512 * 1024**2)):
                with self.assertRaisesRegex(SETUP.SetupError, "Not enough space on /usr"):
                    s.apt_space_check("Inst fixture (1.0 Ubuntu [amd64])\n")

    def test_invalid_device_config_is_rejected(self):
        for config in [{"ROS_DOMAIN_ID": -1}, {"ROS_DOMAIN_ID": True},
                       {"base_ip": 12345}, {"base_ip": "$(touch /tmp/bad)"},
                       {"serial_device": "/dev/../../etc/passwd"}, {"profile": "rover"}]:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "config.json"
                path.write_text(json.dumps(config))
                s = self.installer(Path(td) / "prefix")
                s.args.config = str(path)
                with self.assertRaises(SETUP.SetupError):
                    s.load_device_config()

    def test_device_settings_are_generated_without_network_commands(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix", "base")
            s.device_config = {"ROS_DOMAIN_ID": 15, "base_ip": "192.0.2.1", "rover_ip": "192.0.2.2",
                               "can_interface": "can0", "serial_device": "/dev/ttyUSB0"}
            s.environments()
            generated = (s.state / "env.bash").read_text()
            self.assertIn("export ROS_DOMAIN_ID=15", generated)
            self.assertIn("export BASE_XBEE_DEVICE=/dev/ttyUSB0", generated)
            self.assertNotIn("ip link", generated)
            self.assertNotIn("systemctl", generated)

    def test_generated_environment_supports_strict_shell_and_restores_nounset(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix", "rover")
            s.environments()
            fixture = Path(td) / "ros-setup.bash"
            fixture.write_text('if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then :; fi\n')
            target = s.state / "env.bash"
            target.write_text(target.read_text().replace('/opt/ros/jazzy/setup.bash', str(fixture)))
            p = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                                'source "$1"; case $- in *u*) exit 0;; *) exit 9;; esac',
                                'test', str(target)], text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_system_environment_deactivates_perception_and_removes_old_ros(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.installer(Path(td) / "prefix", "rover")
            s.environments()
            venv = Path(td) / "venv"
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
            ros = Path(td) / "ros-setup.bash"
            ros.write_text('export ROS_DISTRO=jazzy\n')
            target = s.state / "env.bash"
            target.write_text(target.read_text().replace('/opt/ros/jazzy/setup.bash', str(ros)))
            p = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                                'source "$1/bin/activate"; export AMENT_PREFIX_PATH=/opt/ros/humble; '
                                'source "$2"; test -z "${VIRTUAL_ENV:-}"; test -z "${AMENT_PREFIX_PATH:-}"; '
                                'test "$ROS_DISTRO" = jazzy; '
                                'python3 -c "import sys; assert sys.prefix == sys.base_prefix"',
                                'test', str(venv), str(target)], text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
