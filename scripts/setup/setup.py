#!/usr/bin/env python3
"""Install the agreed MR2 dev/rover/base environments and run acceptance checks.

Only the standard library is needed to bootstrap this program. OS installation,
JetPack flashing and starting rover hardware are deliberately separate operations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
REMOVABLE = {
    "nvidia-jetpack", "nvidia-jetpack-dev", "nvidia-jetpack-runtime",
    "nvidia-opencv", "nvidia-opencv-dev", "libopencv", "libopencv-python",
    "libopencv-samples", "opencv-licenses", "opencv-samples-data",
}


class SetupError(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def os_release():
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    return values


class Setup:
    def __init__(self, args):
        self.args = args
        self.profile = args.profile
        self.lock = json.loads((HERE / "versions.json").read_text())
        self.user = pwd.getpwnam(os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name)
        if self.user.pw_uid == 0:
            raise SetupError("Run as a normal user (sudo is supported); do not provision a root-owned development environment.")
        self.arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
        self.prefix = Path(args.prefix or Path(self.user.pw_dir) / ".local/share/mr2").expanduser().resolve()
        if self.prefix == Path("/") or self.prefix == REPO:
            raise SetupError("Choose a dedicated --prefix directory, outside the source checkout.")
        self.state = self.prefix / "state" / self.profile
        self.cache = self.prefix / "cache"
        self.node = self.prefix / "toolchains" / ("node-" + self.lock["node"]["version"] + "-" + str(self.arch))
        self.venv = self.prefix / "venvs" / ("perception-" + self.profile)
        # rclnodejs discovers our custom interfaces reliably when they share one
        # install prefix, matching the original gateway's type-generation layout.
        self.interfaces = self.prefix / "interfaces" / ("jazzy-" + str(self.arch))
        self.has_web = self.profile in {"dev", "base"} or args.with_rover_direct
        self.has_perception = self.profile in {"dev", "rover"}
        self.results = {"profile": self.profile, "hostname": platform.node(), "architecture": platform.machine(),
                        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": [],
                        "mode": "check" if args.check else "install", "source": str(REPO),
                        "manifest_sha256": sha256(HERE / "versions.json"), "status": "running"}
        self.env = dict(os.environ)
        for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "AMENT_PREFIX_PATH",
                     "CMAKE_PREFIX_PATH", "COLCON_PREFIX_PATH", "ROS_DISTRO", "ROS_VERSION", "ROS_PYTHON_VERSION",
                     "LD_LIBRARY_PATH", "LD_PRELOAD", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"]:
            self.env.pop(name, None)
        self.env.update(HOME=self.user.pw_dir, USER=self.user.pw_name, LOGNAME=self.user.pw_name,
                        PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", YOLO_AUTOINSTALL="False",
                        PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PIP_CONFIG_FILE="/dev/null",
                        DEBIAN_FRONTEND="noninteractive")
        # Select the system interpreter for ROS builds, regardless of the caller's active venv.
        self.env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.log = None
        self.lock_file = None
        self.device_config = {}

    def directory(self, path):
        path = Path(path)
        created = []
        parent = path
        while not parent.exists():
            created.append(parent)
            parent = parent.parent
        path.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            managed = [path]
            parent = path.parent
            while parent.is_relative_to(self.prefix):
                managed.append(parent)
                parent = parent.parent
            for directory in set(created + managed):
                os.chown(directory, self.user.pw_uid, self.user.pw_gid)
        return path

    def write(self, path, content, mode=0o644):
        path = Path(path)
        self.directory(path.parent)
        temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        temp.write_text(content)
        temp.chmod(mode)
        if os.geteuid() == 0:
            os.chown(temp, self.user.pw_uid, self.user.pw_gid)
        temp.replace(path)

    def save_json(self, path, value):
        self.write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def message(self, text):
        print(text, flush=True)
        if self.log:
            self.log.write(text + "\n")
            self.log.flush()

    def as_user(self):
        if os.geteuid() == 0:
            os.initgroups(self.user.pw_name, self.user.pw_gid)
            os.setgid(self.user.pw_gid)
            os.setuid(self.user.pw_uid)

    def run(self, argv, *, root=False, cwd=None, env=None, timeout=1800, capture=False):
        argv = [str(x) for x in argv]
        if root and os.geteuid() != 0:
            argv = ["sudo", "--", "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", *argv]
        self.message("$ " + shlex.join(argv))
        proc = subprocess.Popen(argv, cwd=cwd, env=env or self.env,
                                preexec_fn=self.as_user if os.geteuid() == 0 and not root else None,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        output = []
        import threading
        timed_out = []

        def stop():
            import signal
            if proc.poll() is None:
                timed_out.append(True)
                os.killpg(proc.pid, signal.SIGTERM)

        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                output.append(line)
                if not capture:
                    self.message(line.rstrip("\n"))
            status = proc.wait(timeout=15)
        finally:
            timer.cancel()
        data = "".join(output)
        if capture and self.log:
            self.log.write(data)
            self.log.flush()
        if timed_out:
            raise SetupError("Command exceeded its timeout: " + shlex.join(argv))
        if status:
            if capture:
                self.message(data[-8000:])
            raise SetupError(f"Command failed ({status}): " + shlex.join(argv))
        return data

    def check(self, name, function):
        self.message("\n[CHECK] " + name)
        start = time.monotonic()
        try:
            detail = function()
        except Exception as exc:
            self.results["checks"].append({"name": name, "status": "failed", "detail": str(exc),
                                            "seconds": round(time.monotonic() - start, 2)})
            raise
        self.results["checks"].append({"name": name, "status": "passed", "detail": detail,
                                        "seconds": round(time.monotonic() - start, 2)})

    def preflight(self):
        release = os_release()
        if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
            raise SetupError("Ubuntu 24.04 is required. OS migration/JetPack flashing is a separate step.")
        if not self.arch or sys.version_info[:2] != (3, 12):
            raise SetupError("Requires Ubuntu system Python 3.12 on amd64 or arm64.")
        if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
            raise SetupError("Use the system /usr/bin/python3, not a venv or Conda interpreter.")
        model = ""
        model_file = Path("/proc/device-tree/model")
        if model_file.exists():
            model = model_file.read_text().replace("\x00", "").strip()
        if self.profile == "rover":
            if self.arch != "arm64":
                raise SetupError("Rover requires a Jetson arm64 device.")
            nano = "orin nano" in model.lower()
            if "orin nx" not in model.lower() and not (nano and self.args.allow_nano_validation):
                raise SetupError("Rover targets Orin NX. On Orin Nano use --allow-nano-validation for explicitly limited software/GPU validation.")
            core = self.installed("nvidia-l4t-core")
            if not core or not core.startswith("39.2.1-"):
                raise SetupError("Requires JetPack 7.2.1 / L4T 39.2.1; refusing a BSP upgrade or downgrade.")
            self.results["device_model"] = model
            self.results["validation_target"] = "Orin Nano (NX-specific hardware not verified)" if nano else "Orin NX"
        space = shutil.disk_usage(self.prefix.parent if self.prefix.parent.exists() else self.user.pw_dir)
        if not self.args.check and space.free < 3 * 1024**3:
            raise SetupError("At least 3 GiB free space is required for toolchains, wheels and build outputs.")
        self.load_device_config()
        return {"os": release["PRETTY_NAME"], "architecture": self.arch, "python": sys.version,
                "device_model": model, "prefix": str(self.prefix)}

    def load_device_config(self):
        supplied = getattr(self.args, "config", None)
        path = Path(supplied).expanduser() if supplied else self.state / "device-config.json"
        if not path.exists():
            if supplied:
                raise SetupError("Device configuration does not exist: " + str(path))
            self.device_config = {}
            return
        data = json.loads(path.read_text())
        allowed = {"profile", "note", "ROS_DOMAIN_ID", "base_ip", "rover_ip", "can_interface", "serial_device"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise SetupError("Unsupported device configuration fields: " + str(path))
        if data.get("profile") not in {None, self.profile}:
            raise SetupError("Device configuration belongs to a different profile")
        domain = data.get("ROS_DOMAIN_ID")
        if domain is not None and (type(domain) is not int or not 0 <= domain <= 232):
            raise SetupError("ROS_DOMAIN_ID must be an integer in 0..232")
        for name in ["base_ip", "rover_ip"]:
            if data.get(name) is not None:
                try:
                    if not isinstance(data[name], str):
                        raise ValueError("IP address must be a string")
                    ipaddress.ip_address(data[name])
                except ValueError as exc:
                    raise SetupError("Invalid " + name) from exc
        interface = data.get("can_interface")
        if interface is not None and (not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface)):
            raise SetupError("Invalid can_interface")
        serial = data.get("serial_device")
        if serial is not None and (not isinstance(serial, str) or not serial.startswith("/dev/")
                                   or "\n" in serial or "\x00" in serial
                                   or not Path(serial).resolve().is_relative_to(Path("/dev"))):
            raise SetupError("serial_device must be a path under /dev")
        self.device_config = data

    @staticmethod
    def installed(package):
        p = subprocess.run(["dpkg-query", "-W", "-f=${Status}\t${Version}", package],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if p.returncode == 0 and p.stdout.startswith("install ok installed\t"):
            return p.stdout.split("\t", 1)[1]
        return None

    def artifact(self, item):
        self.directory(self.cache)
        target = self.cache / item["filename"]
        if target.exists() and sha256(target) == item["sha256"]:
            return target
        bundled = HERE / "assets" / item["filename"]
        if bundled.exists() and sha256(bundled) == item["sha256"]:
            shutil.copyfile(bundled, target)
        else:
            partial = target.with_name(target.name + ".part")
            last_error = None
            for attempt in range(3):
                try:
                    self.message("Downloading " + item["filename"])
                    request = urllib.request.Request(item["url"], headers={"User-Agent": "MR2-setup/1"})
                    with urllib.request.urlopen(request, timeout=90) as source, partial.open("wb") as dest:
                        shutil.copyfileobj(source, dest, 4 * 1024 * 1024)
                    if sha256(partial) != item["sha256"]:
                        raise SetupError("SHA-256 mismatch: " + item["filename"])
                    partial.replace(target)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    partial.unlink(missing_ok=True)
                    time.sleep(attempt + 1)
            if last_error:
                raise SetupError("Could not fetch verified artifact: " + str(last_error))
        if sha256(target) != item["sha256"]:
            raise SetupError("SHA-256 mismatch: " + str(target))
        if os.geteuid() == 0:
            os.chown(target, self.user.pw_uid, self.user.pw_gid)
        return target

    def system_write(self, path, content):
        path = Path(path)
        if path.exists() and path.read_text() == content:
            return
        local = self.state / (path.name + ".proposed")
        self.write(local, content)
        self.run(["install", "-m", "0644", local, path], root=True)

    def repositories(self):
        if not Path("/opt/ros/jazzy/setup.bash").exists() or not self.installed("ros2-apt-source"):
            bootstrap = self.artifact(self.lock["ros_apt_source"])
            self.run(["dpkg", "-i", bootstrap], root=True)
        # Ubuntu and ROS deliberately share the Ubuntu OpenCV ABI. Jetson's
        # umbrella OpenCV packages must not silently win candidate selection.
        pref = ('Package: libopencv* opencv* python3-opencv\n'
                'Pin: release o=Ubuntu\nPin-Priority: 1001\n')
        if (self.installed("nvidia-l4t-core") or "").startswith("39.2.1-"):
            for package, version in self.lock["gpu_sdk"]["pins"].items():
                pref += f"\nPackage: {package}\nPin: version {version}\nPin-Priority: 1001\n"
                pref += f"\nPackage: {package}\nPin: version *\nPin-Priority: -1\n"
        self.system_write("/etc/apt/preferences.d/mr2-environment", pref)
        self.run(["apt-get", "update"], root=True, timeout=600)

    def packages(self):
        apt = self.lock["apt"]
        names = list(apt["common"])
        if self.has_perception:
            names += apt["perception"]
        names += apt[self.profile]
        if self.profile == "rover":
            names += [key + "=" + value for key, value in self.lock["gpu_sdk"]["roots"].items()]
        names = sorted(set(names))
        return names

    def install_apt(self):
        roots = self.packages()
        remove = [p + "-" for p in sorted(REMOVABLE) if self.installed(p)] if self.has_perception else []
        cache = self.directory(self.cache / "apt")
        self.directory(cache / "archives")
        command = ["apt-get", "-o", "Dir::Cache=" + str(cache), "-o", "APT::Keep-Downloaded-Packages=true",
                   "install", "--no-install-recommends", "--allow-downgrades", *roots, *remove]
        plan = self.run(["apt-get", "-s", *command[1:]], root=True, capture=True)
        self.write(self.state / "apt-plan.txt", plan)
        removals = [line.split()[1] for line in plan.splitlines() if line.startswith("Remv ")]
        unexpected = set(removals) - REMOVABLE
        if unexpected:
            raise SetupError("APT would remove unrelated packages: " + ", ".join(sorted(unexpected)))
        if self.profile == "rover":
            # The BSP precondition is immutable; APT must not replace it.
            for line in plan.splitlines():
                if line.startswith("Inst nvidia-l4t-"):
                    raise SetupError("APT plans a BSP change; refusing: " + line)
        requested_space = self.apt_space_check(plan)
        self.message("APT plan accepted; expected OpenCV umbrella removals: " + ", ".join(removals))
        self.run([*command[:1], "-y", *command[1:]], root=True, timeout=3600)
        # Keep selected SDK components from becoming autoremove candidates after
        # removing NVIDIA's umbrella package to use Ubuntu OpenCV.
        if (self.installed("nvidia-l4t-core") or "").startswith("39.2.1-"):
            sdk = [p for p in self.lock["gpu_sdk"]["pins"] if not p.startswith("nvidia-jetpack") and self.installed(p)]
            self.run(["apt-mark", "manual", *sdk], root=True)
        snapshot = self.run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${Status}\n"], capture=True)
        self.write(self.state / "dpkg-installed.tsv", snapshot)
        self.save_json(self.state / "apt-roots.lock.json", {p.split("=", 1)[0]: self.installed(p.split("=", 1)[0]) for p in roots})
        return {"requested": roots, "removed_umbrella_packages": removals,
                "snapshot": str(self.state / "dpkg-installed.tsv"), "disk_preflight": requested_space}

    def apt_space_check(self, plan):
        changes = []
        for line in plan.splitlines():
            match = re.match(r"Inst (\S+)(?: \[[^\]]+\])? \((\S+)", line)
            if match:
                changes.append(match.group(1) + "=" + match.group(2))
        if not changes:
            return {"additional_installed_bytes": 0, "changed_packages": 0}
        raw = self.run(["apt-cache", "show", *changes], capture=True)
        new_sizes = {}
        for stanza in raw.split("\n\n"):
            fields = dict(line.split(": ", 1) for line in stanza.splitlines()
                          if ": " in line and not line.startswith((" ", "\t")))
            if "Package" in fields:
                new_sizes[fields["Package"]] = int(fields.get("Installed-Size", "0")) * 1024
        old = self.run(["dpkg-query", "-W", "-f=${Package}\t${Installed-Size}\n"], capture=True)
        old_sizes = {}
        for line in old.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                old_sizes[parts[0]] = int(parts[1]) * 1024
        extra = max(0, sum(value - old_sizes.get(name, 0) for name, value in new_sizes.items()))
        free = shutil.disk_usage("/usr").free
        if free < extra + 512 * 1024**2:
            raise SetupError(f"Not enough space on /usr: need about {extra / 1024**3:.1f} GiB plus 512 MiB reserve; free {free / 1024**3:.1f} GiB")
        return {"additional_installed_bytes": extra, "free_bytes": free, "changed_packages": len(changes)}

    @staticmethod
    def extract(archive, target, strip=1):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            for member in tar:
                parts = Path(member.name).parts[strip:]
                if not parts:
                    continue
                if any(p == ".." for p in parts) or Path(*parts).is_absolute():
                    raise SetupError("Unsafe archive path")
                relative = Path(*parts)
                dest = target / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    dest.mkdir(exist_ok=True)
                elif member.issym():
                    resolved = (dest.parent / member.linkname).resolve()
                    if not resolved.is_relative_to(target.resolve()):
                        raise SetupError("Unsafe archive symlink")
                    dest.unlink(missing_ok=True)
                    dest.symlink_to(member.linkname)
                elif member.isfile():
                    source = tar.extractfile(member)
                    with dest.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    dest.chmod(member.mode & 0o777)
                else:
                    raise SetupError("Unexpected archive entry: " + member.name)

    def own_tree(self, path):
        if os.geteuid() == 0:
            for root, dirs, files in os.walk(path):
                os.chown(root, self.user.pw_uid, self.user.pw_gid)
                for name in dirs + files:
                    os.lchown(Path(root) / name, self.user.pw_uid, self.user.pw_gid)

    def install_node(self):
        marker = self.node / ".mr2-node.json"
        wanted = {"node": self.lock["node"]["version"], "npm": self.lock["npm"]["version"], "architecture": self.arch}
        ready = marker.exists() and json.loads(marker.read_text()) == wanted
        if not ready:
            archive = self.artifact(self.lock["node"]["artifacts"][self.arch])
            self.directory(self.node)
            self.extract(archive, self.node)
            npm = self.artifact(self.lock["npm"]["artifact"])
            npm_dir = self.node / "lib/node_modules/npm"
            if npm_dir.exists():
                shutil.rmtree(npm_dir)
            self.extract(npm, npm_dir)
            self.own_tree(self.node)
            self.save_json(marker, wanted)
        self.env["PATH"] = str(self.node / "bin") + ":" + self.env["PATH"]
        return wanted

    def ros_env(self):
        # The caller may have sourced Humble. All paths above are reset before
        # sourcing Jazzy; never let old workspace install directories leak in.
        command = 'set -e; source /opt/ros/jazzy/setup.bash; '
        if (self.interfaces / "setup.bash").exists():
            command += "source " + shlex.quote(str(self.interfaces / "setup.bash")) + "; "
        command += 'exec /usr/bin/python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
        data = self.run(["/bin/bash", "--noprofile", "--norc", "-c", command], capture=True)
        env = json.loads(data.splitlines()[-1])
        if env.get("ROS_DISTRO") != "jazzy":
            raise SetupError("Failed to select ROS Jazzy")
        env["PYTHONNOUSERSITE"] = "1"
        env["YOLO_AUTOINSTALL"] = "False"
        return env

    def build_interfaces(self):
        packages = [REPO / "rover/ros2_ws/src/ublox_dgnss/ublox_ubx_msgs", REPO / "rover/ros2_ws/src/rtcm_msgs"]
        h = hashlib.sha256()
        for package in packages:
            if not package.is_dir():
                raise SetupError("Missing message source package: " + str(package))
            for file in sorted(package.rglob("*")):
                if file.is_file():
                    h.update(str(file.relative_to(package)).encode())
                    h.update(file.read_bytes())
        wanted = {"sources_sha256": h.hexdigest(), "ros": "jazzy", "python": "3.12",
                  "architecture": self.arch, "install_layout": "merged-v1"}
        stamp = self.interfaces / ".mr2-interfaces.json"
        if stamp.exists() and json.loads(stamp.read_text()) == wanted and (self.interfaces / "setup.bash").exists():
            return wanted
        build = self.directory(self.prefix / "build" / ("interfaces-jazzy-" + self.arch + "-merged"))
        logs = self.directory(self.prefix / "logs" / ("interfaces-jazzy-" + self.arch))
        self.directory(self.interfaces)
        self.run(["/usr/bin/colcon", "--log-base", logs, "build", "--base-paths", *packages,
                  "--build-base", build, "--install-base", self.interfaces,
                  "--merge-install",
                  "--parallel-workers", "2", "--cmake-args", "-DBUILD_TESTING=OFF",
                  "-DPython3_EXECUTABLE=/usr/bin/python3", "-DCMAKE_BUILD_TYPE=Release"],
                 env=self.ros_env(), cwd=REPO, timeout=1800)
        self.save_json(stamp, wanted)
        return wanted

    def wheel_set(self):
        key = "gpu-arm64" if self.profile == "rover" else "cpu-" + self.arch
        return self.lock["wheels"][key]

    def gpu_library_directory(self):
        return self.prefix / "runtime/rover-sdk-libs"

    def configure_gpu_library_paths(self):
        # Keep the upstream wheel intact. Its image extension requests hashed
        # library names; alias those names to the reviewed, apt-owned SDK files.
        # LD_LIBRARY_PATH precedes this extension's DT_RUNPATH at process start.
        directory = self.directory(self.gpu_library_directory())
        aliases = self.lock["gpu_library_aliases"]
        if set(p.name for p in directory.iterdir()) - set(aliases):
            raise SetupError("Unexpected files in the managed SDK library directory: " + str(directory))
        providers = {}
        for name, spec in aliases.items():
            target = Path(spec["system_path"])
            if not target.is_file():
                raise SetupError("System SDK library is missing: " + str(target))
            real = target.resolve()
            package = spec["apt_package"]
            if self.installed(package) != self.lock["gpu_sdk"]["pins"][package]:
                raise SetupError("Unexpected system SDK provider version: " + package)
            owner = self.run(["dpkg-query", "-S", real], capture=True).strip()
            if not any(line.split(": ", 1)[0].split(":", 1)[0] == package for line in owner.splitlines()):
                raise SetupError("Unexpected system SDK library owner: " + owner)
            link = directory / name
            if link.exists() and not link.is_symlink():
                raise SetupError("Refusing to overwrite a regular file: " + str(link))
            if not link.is_symlink() or link.readlink() != target:
                temporary = directory / (name + ".tmp-" + str(os.getpid()))
                temporary.symlink_to(target)
                temporary.replace(link)
            if os.geteuid() == 0:
                os.lchown(link, self.user.pw_uid, self.user.pw_gid)
            providers[name] = {"system_path": str(target), "resolved_path": str(real),
                               "sha256": sha256(real), "owner": owner}
        self.save_json(self.state / "gpu-library-providers.json", providers)
        return {"directory": str(directory), "providers": providers,
                "wheel_policy": "Unmodified upstream wheel; bundled GPU libraries remain installed but unused"}

    def install_perception(self):
        self.directory(self.venv.parent)
        if not (self.venv / "bin/python").exists():
            self.run(["/usr/bin/python3", "-m", "venv", "--system-site-packages", self.venv])
        config = (self.venv / "pyvenv.cfg").read_text()
        if "include-system-site-packages = true" not in config:
            raise SetupError("Existing perception venv does not share system site-packages: " + str(self.venv))
        artifacts = self.wheel_set()
        files = [self.artifact(row) for row in artifacts]
        # Local paths + hashes + --no-deps make the provider exceptions explicit.
        lockfile = self.state / "perception-requirements.lock"
        self.write(lockfile, "\n".join(str(path) + " --hash=sha256:" + row["sha256"] for row, path in zip(artifacts, files)) + "\n")
        env = self.ros_env()
        self.run([self.venv / "bin/python", "-m", "pip", "install", "--no-deps", "--require-hashes",
                  "--no-index", "--no-cache-dir", "--progress-bar", "off", "-r", lockfile], env=env, timeout=1800)
        self.save_json(self.state / "wheel-artifacts.lock.json", artifacts)
        detail = {"venv": str(self.venv), "artifacts": len(files), "system_site_packages": True}
        if self.profile == "rover":
            detail["gpu_library_paths"] = self.configure_gpu_library_paths()
        return detail

    def install_web(self):
        env = self.ros_env()
        env["npm_config_audit"] = "false"
        env["npm_config_fund"] = "false"
        env["npm_config_cache"] = str(self.cache / "npm")
        npm = self.node / "bin/npm"
        for relative in ["base/gateway", "dashboard"]:
            path = REPO / relative
            lock = path / "package-lock.json"
            expected = self.lock["npm_locks"][relative + "/package-lock.json"]
            if sha256(lock) != expected:
                raise SetupError("npm lock changed; update the reviewed manifest first: " + relative)
            stamp = path / "node_modules/.mr2-environment.json"
            wanted = {"lock_sha256": expected, "node": self.lock["node"]["version"],
                      "npm": self.lock["npm"]["version"], "architecture": self.arch,
                      "ros": "jazzy", "interfaces": sha256(self.interfaces / ".mr2-interfaces.json"),
                      "message_generation": "source-path-v2"}
            if not stamp.exists() or json.loads(stamp.read_text()) != wanted:
                self.run([npm, "ci", "--no-audit", "--no-fund"], cwd=path, env=env, timeout=1800)
                if relative == "base/gateway":
                    # npm can reuse prebuilt native modules without producing our
                    # workspace's types. The upstream CLI also catches generation
                    # errors without a failing exit code, so await the API directly.
                    generator = """const g=require('./node_modules/rclnodejs/rosidl_gen');
async function generate(){
  await g.generateAll(true);
  for(const source of process.argv.slice(1)) await g.generateInPath(source);
}
generate().catch(e=>{console.error(e.stack);process.exitCode=1});"""
                    # rclnodejs 1.8.1 parses the first '/share/PACKAGE/' in an
                    # installed path. ~/.local/share/mr2 would become package
                    # 'mr2'. Generate custom types from their reviewed sources
                    # while standard types come from the Jazzy apt prefix.
                    generation_env = dict(env, AMENT_PREFIX_PATH="/opt/ros/jazzy")
                    self.run([self.node / "bin/node", "-e", generator,
                              REPO / "rover/ros2_ws/src/ublox_dgnss/ublox_ubx_msgs",
                              REPO / "rover/ros2_ws/src/rtcm_msgs"],
                             cwd=path, env=generation_env, timeout=180)
                self.save_json(stamp, wanted)
            if sha256(lock) != expected:
                raise SetupError("npm ci unexpectedly modified its lock file: " + relative)
        return {"node": self.lock["node"]["version"], "npm": self.lock["npm"]["version"], "locks_preserved": True}

    def environments(self):
        node_line = "export PATH=" + shlex.quote(str(self.node / "bin")) + ':"$PATH"\n' if self.has_web else ""
        interface_line = "source " + shlex.quote(str(self.interfaces / "setup.bash")) + "\n" if self.has_web else ""
        common = ("# Generated by MR2 setup. Source this file in a new shell.\n"
                  "# Return ROS builds to system Python even after perception activation.\n"
                  "if declare -F deactivate >/dev/null; then deactivate; fi\n"
                  "unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH\n"
                  "unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION LD_LIBRARY_PATH LD_PRELOAD\n"
                  'export PATH="/usr/bin:$PATH"\n'
                  "export PYTHONNOUSERSITE=1 YOLO_AUTOINSTALL=False\n"
                  # ROS/colcon setup files legitimately inspect unset variables.
                  # Preserve the caller's nounset setting around those external files.
                  "case $- in *u*) _mr2_restore_nounset=1; set +u ;; *) _mr2_restore_nounset=0 ;; esac\n"
                  "source /opt/ros/jazzy/setup.bash\n" + interface_line
                  + 'if [[ "$_mr2_restore_nounset" == 1 ]]; then set -u; fi\n'
                  + "unset _mr2_restore_nounset\n" + node_line)
        names = {"ROS_DOMAIN_ID": "ROS_DOMAIN_ID", "base_ip": "MR2_BASE_IP", "rover_ip": "MR2_ROVER_IP",
                 "can_interface": "MR2_CAN_INTERFACE", "serial_device": "BASE_XBEE_DEVICE"}
        for key, variable in names.items():
            if self.device_config.get(key) is not None:
                common += "export " + variable + "=" + shlex.quote(str(self.device_config[key])) + "\n"
        self.write(self.state / "env.bash", common)
        if self.has_perception:
            perception = common + "source " + shlex.quote(str(self.venv / "bin/activate")) + "\n"
            if self.profile == "rover":
                # Fail if an alias is missing instead of silently falling back
                # to the bundled wheel library. Do not export this into base/dev.
                for name, spec in self.lock["gpu_library_aliases"].items():
                    link = shlex.quote(str(self.gpu_library_directory() / name))
                    target = shlex.quote(spec["system_path"])
                    perception += (f"if [[ ! -L {link} || ! {link} -ef {target} ]]; then\n"
                                   "  echo 'MR2 system SDK library alias is missing or incorrect; rerun rover setup.' >&2\n"
                                   "  return 1\nfi\n")
                perception += ("export LD_LIBRARY_PATH=" + shlex.quote(str(self.gpu_library_directory()))
                               + '${LD_LIBRARY_PATH:+:"$LD_LIBRARY_PATH"}\n')
            self.write(self.state / "perception.bash", perception)
            launcher = ("#!/usr/bin/env bash\nset -Eeuo pipefail\nsource "
                        + shlex.quote(str(self.state / "perception.bash")) + "\nexec "
                        + shlex.quote(str(self.venv / "bin/python")) + ' "$@"\n')
            self.write(self.state / "perception-python", launcher, 0o755)
        device = self.state / "device-config.example.json"
        if not device.exists():
            self.save_json(device, {"profile": self.profile, "ROS_DOMAIN_ID": None,
                                   "base_ip": None, "rover_ip": None, "can_interface": None,
                                   "serial_device": None, "note": "Fill from the team's device/network agreement. Not applied to the OS."})
        return {"system_env": str(self.state / "env.bash"),
                "perception_env": str(self.state / "perception.bash") if self.has_perception else None,
                "device_config": self.device_config,
                "host_settings": "application environment only; network/udev/clock changes and service startup not applied"}

    def python_health(self, perception=False):
        python = self.venv / "bin/python" if perception else Path("/usr/bin/python3")
        if perception:
            python = self.state / "perception-python"
            if not python.exists():
                raise SetupError("The generated perception launcher is missing")
        output = self.state / ("perception-health.json" if perception else "system-health.json")
        command = [python, HERE / "check_python.py", "--output", output, "--profile", self.profile]
        if perception:
            command += ["--venv", self.venv, "--wheel-lock", HERE / "versions.json",
                        "--wheel-set", "gpu-arm64" if self.profile == "rover" else "cpu-" + self.arch]
            if self.profile == "rover":
                command += ["--gpu-library-directory", self.gpu_library_directory()]
        env = self.ros_env()
        env.update(ROS_DOMAIN_ID="219", ROS_LOCALHOST_ONLY="1")
        yolo = self.directory(self.state / "ultralytics-config")
        env.update(YOLO_CONFIG_DIR=str(yolo),
                   ROS_LOG_DIR=str(self.state / "ros-check-logs"),
                   MPLBACKEND="Agg", MPLCONFIGDIR=str(self.state / "matplotlib-config"))
        data = self.run(command, env=env, timeout=180, capture=True)
        self.message(data.strip())
        if not output.exists():
            raise SetupError("Health check did not write its JSON result")
        result = json.loads(output.read_text())
        if result["status"] != "passed":
            raise SetupError("Python health check failed: " + str(output))
        return {"result_file": str(output), "checks": len(result["checks"])}

    def native_health(self):
        version = self.run(["pkg-config", "--modversion", "opencv4"], capture=True).strip()
        if version != "4.6.0":
            raise SetupError("C++ OpenCV pkg-config does not select Ubuntu 4.6.0: " + version)
        flags = shlex.split(self.run(["pkg-config", "--cflags", "--libs", "opencv4"], capture=True))
        work = self.directory(self.state / "native-health")
        source = work / "opencv.cpp"
        binary = work / "opencv-check"
        self.write(source, '#include <opencv2/core.hpp>\n#include <iostream>\n'
                   'int main() { cv::Mat m(2, 2, CV_8UC1, cv::Scalar(3)); '
                   'if (cv::sum(m)[0] != 12) return 1; '
                   'std::cout << CV_VERSION << " " << cv::getVersionString() << "\\n"; }\n')
        self.run(["/usr/bin/c++", "-std=c++17", source, "-o", binary, *flags], timeout=90)
        actual = self.run([binary], capture=True).strip()
        if actual != "4.6.0 4.6.0":
            raise SetupError("C++ OpenCV headers/runtime differ: " + actual)
        linked = self.run(["ldd", binary], capture=True)
        if "libopencv_core.so.406" not in linked:
            raise SetupError("The C++ executable is not linked to Ubuntu OpenCV 406")
        detail = {"headers_and_runtime": actual, "linked_libraries": linked}
        if self.profile == "rover":
            versions = {p: self.installed(p) for p in self.lock["gpu_sdk"]["roots"]}
            if versions != self.lock["gpu_sdk"]["roots"]:
                raise SetupError("Installed SDK package versions differ from JetPack 7.2.1 policy: " + str(versions))
            detail["gpu_sdk_package_versions"] = versions
        return detail

    def web_health(self):
        node = self.node / "bin/node"
        npm = self.node / "bin/npm"
        node_version = self.run([node, "--version"], capture=True).strip()
        npm_version = self.run([npm, "--version"], capture=True).strip()
        if node_version != "v" + self.lock["node"]["version"] or npm_version != self.lock["npm"]["version"]:
            raise SetupError("Node/npm do not match the pinned runtime")
        env = self.ros_env()
        env.update(ROS_DOMAIN_ID="219", ROS_LOCALHOST_ONLY="1")
        output = self.state / "gateway-health.json"
        self.run([node, HERE / "check_gateway.cjs", REPO / "base/gateway", output], env=env, timeout=90)
        result = json.loads(output.read_text())
        if result.get("status") != "passed":
            raise SetupError("Gateway native module/ROS check failed")
        self.run([npm, "run", "typecheck"], cwd=REPO / "dashboard", env=env, timeout=180)
        self.run([npm, "run", "build"], cwd=REPO / "dashboard", env=env, timeout=300)
        for relative in ["base/gateway", "dashboard"]:
            if sha256(REPO / relative / "package-lock.json") != self.lock["npm_locks"][relative + "/package-lock.json"]:
                raise SetupError("npm lock changed during verification")
        return {"node": node_version, "npm": npm_version, "gateway": str(output),
                "dashboard_typecheck_and_build": "passed", "locks_preserved": True}

    def execute(self):
        self.check("platform", self.preflight)
        if self.args.plan:
            print(json.dumps({"profile": self.profile, "apt_roots": self.packages(),
                              "perception_wheels": self.wheel_set() if self.has_perception else [],
                              "web": self.has_web, "prefix": str(self.prefix)}, ensure_ascii=False, indent=2))
            return
        self.directory(self.prefix)
        self.directory(self.state)
        self.directory(self.cache)
        self.lock_file = (self.prefix / ".setup.lock").open("a+")
        if os.geteuid() == 0:
            os.chown(self.prefix / ".setup.lock", self.user.pw_uid, self.user.pw_gid)
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError("Another MR2 setup is running for this prefix") from exc
        self.log = (self.state / "setup.log").open("a", buffering=1)
        if os.geteuid() == 0:
            os.chown(self.state / "setup.log", self.user.pw_uid, self.user.pw_gid)
        self.message(f"MR2 {self.profile}: {self.results['mode']} on {platform.node()} ({self.arch})")
        try:
            if not self.args.check:
                self.check("repositories_and_pins", self.repositories)
                self.check("apt_installation", self.install_apt)
                if self.has_web:
                    self.check("node_installation", self.install_node)
                    self.check("ros_interface_build", self.build_interfaces)
                if self.has_perception:
                    self.check("perception_installation", self.install_perception)
                if self.has_web:
                    self.check("npm_installation", self.install_web)
                self.check("environment_files", self.environments)
            elif self.has_web:
                self.env["PATH"] = str(self.node / "bin") + ":" + self.env["PATH"]
            self.check("system_python_and_ros", self.python_health)
            self.check("cpp_opencv_and_sdk_versions", self.native_health)
            if self.has_perception:
                self.check("perception_loading_and_compute", lambda: self.python_health(True))
            if self.has_web:
                self.check("node_gateway_and_dashboard", self.web_health)
            self.results["status"] = "passed"
            self.results["not_verified"] = [
                "Application end-to-end rover behavior and model accuracy/performance",
                "Physical motor actuation, device IDs, network/clock deployment and service startup",
                "RealSense USB capture and NX-specific hardware encoder",
                "ONNX model export and TensorRT engine inference (PyTorch remains the default)"]
        except Exception as exc:
            self.results["status"] = "failed"
            self.results["error"] = str(exc)
            raise
        finally:
            self.results["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.save_json(self.state / "result.json", self.results)
            self.message("Result: " + str(self.state / "result.json") + " (" + self.results["status"] + ")")
            if self.has_perception:
                self.message("Perception Python: " + str(self.state / "perception-python"))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--profile", choices=["dev", "rover", "base"], required=True)
    parser.add_argument("--prefix", help="Toolchains, venvs, caches and reports (default: ~/.local/share/mr2)")
    parser.add_argument("--config", help="Optional device JSON; exports app settings without changing host networking or clock services")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Re-run loading, compute, Node and build checks without package installation")
    mode.add_argument("--plan", action="store_true", help="Show the selected roots and artifacts without installing or configuring anything")
    parser.add_argument("--allow-nano-validation", action="store_true", help="Permit rover software/GPU validation on Orin Nano; does not certify NX hardware")
    parser.add_argument("--with-rover-direct", action="store_true", help="Also provision local gateway/dashboard on the rover")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        Setup(parse_args()).execute()
    except (SetupError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("MR2 setup failed: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
