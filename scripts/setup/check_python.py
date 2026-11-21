#!/usr/bin/env python3
"""Exercise the installed Python/ROS/native stack, without running rover nodes."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback


CORE_GPU_LIBRARY = re.compile(
    r"^lib(cuda|cudart|cublasLt|cublas|cudnn[^/]*|cusparse|cufft|curand|cusolver|"
    r"nvrtc[^/]*|nvJitLink|nvjpeg|cupti|cufile)(?:[.-][0-9a-f]+)?\.so(?:\.|$)"
)


def is_core_gpu_library(path):
    # auditwheel gives vendored libraries names such as
    # libcudart.3eda8113.so.13, which must not evade the SDK-provider audit.
    return bool(CORE_GPU_LIBRARY.search(Path(path).name))


def gpu_alias_paths(manifest, directory):
    directory = Path(directory)
    expected = manifest["gpu_library_aliases"]
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != set(expected):
        raise RuntimeError("Missing or unexpected SDK library aliases: " + str(directory))
    if os.environ.get("LD_LIBRARY_PATH", "").split(":")[0] != str(directory):
        raise RuntimeError("The system SDK alias directory is not first in LD_LIBRARY_PATH")
    result = {}
    for name, spec in expected.items():
        link = directory / name
        target = Path(spec["system_path"])
        if not link.is_symlink() or not target.is_file() or link.resolve() != target.resolve():
            raise RuntimeError("Incorrect system SDK library alias: " + str(link))
        result[name] = str(target.resolve())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["dev", "rover", "base"], required=True)
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--wheel-lock", type=Path)
    parser.add_argument("--wheel-set")
    parser.add_argument("--gpu-library-directory", type=Path)
    args = parser.parse_args()
    results = {"status": "running", "python": sys.executable, "prefix": sys.prefix,
               "profile": args.profile, "checks": []}

    def check(name, function):
        start = time.monotonic()
        try:
            value = function()
            results["checks"].append(dict(name=name, status="passed", detail=value,
                                          seconds=round(time.monotonic() - start, 3)))
            return value
        except Exception as exc:
            results["checks"].append(dict(name=name, status="failed", detail=str(exc)))
            raise

    def apt_module(name, expected_owner, version_prefix=None):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if not str(path).startswith("/usr/lib/python3/dist-packages/"):
            raise RuntimeError(f"{name} is shadowing Ubuntu apt: {path}")
        p = subprocess.run(["dpkg-query", "-S", str(path)], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode or expected_owner not in p.stdout:
            raise RuntimeError(f"Unexpected dpkg owner for {name}: {p.stdout} {p.stderr}")
        version = getattr(module, "__version__", None)
        if version_prefix and not str(version).startswith(version_prefix):
            raise RuntimeError(f"Unexpected {name} version: {version}")
        return {"path": str(path), "version": version, "owner": p.stdout.strip()}

    def base_interpreter():
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("Python 3.12 required")
        if Path(sys.base_prefix).resolve() != Path("/usr").resolve():
            raise RuntimeError("The venv must use Ubuntu's system interpreter")
        if args.venv:
            if Path(sys.prefix).resolve() != args.venv.resolve():
                raise RuntimeError("Wrong perception interpreter: " + sys.prefix)
            if "include-system-site-packages = true" not in (args.venv / "pyvenv.cfg").read_text():
                raise RuntimeError("system-site-packages is not enabled")
        elif sys.prefix != sys.base_prefix:
            raise RuntimeError("ROS build check unexpectedly uses a venv")
        return {"version": sys.version, "base_prefix": sys.base_prefix,
                "venv": str(args.venv) if args.venv else None}

    def ros():
        if os.environ.get("ROS_DISTRO") != "jazzy":
            raise RuntimeError("ROS_DISTRO is not jazzy")
        import rclpy
        from std_msgs.msg import String
        from sensor_msgs.msg import Image
        from std_srvs.srv import SetBool
        if "/opt/ros/jazzy/" not in rclpy.__file__:
            raise RuntimeError("rclpy is not provided by Jazzy: " + rclpy.__file__)
        rclpy.init()
        try:
            node = rclpy.create_node("mr2_setup_python_check")
            node.destroy_node()
        finally:
            rclpy.shutdown()
        return {"rclpy": rclpy.__file__, "messages": [String.__name__, Image.__name__, SetBool.__name__],
                "node_create_destroy": "passed"}

    def image_bridge():
        import numpy as np
        import cv2
        from cv_bridge import CvBridge
        bridge = CvBridge()
        image = np.arange(4 * 5 * 3, dtype=np.uint8).reshape((4, 5, 3))
        msg = bridge.cv2_to_imgmsg(image, encoding="bgr8")
        actual = bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        if not np.array_equal(image, actual):
            raise RuntimeError("cv_bridge did not preserve the image")
        gray = cv2.cvtColor(actual, cv2.COLOR_BGR2GRAY)
        if gray.shape != (4, 5):
            raise RuntimeError("OpenCV conversion failed")
        return {"shape": list(image.shape), "cv_bridge_roundtrip": "passed", "opencv_color_conversion": "passed"}

    def shared_modules():
        names = ["yaml", "psutil", "scipy", "matplotlib", "PIL", "requests", "typing_extensions",
                 "filelock", "networkx", "jinja2", "fsspec"]
        found = {}
        for name in names:
            module = importlib.import_module(name)
            found[name] = str(Path(module.__file__).resolve())
            if not found[name].startswith("/usr/lib/python3/dist-packages/"):
                raise RuntimeError(name + " does not use the system apt copy: " + found[name])
        return found

    def onnx_runtime_package():
        import onnx
        import numpy as np
        from onnx import TensorProto, helper, numpy_helper
        if not str(onnx.__version__).startswith("1.14.1"):
            raise RuntimeError("Wrong ONNX version")
        tensor = numpy_helper.from_array(np.array([1.0], dtype=np.float32), name="x")
        graph = helper.make_graph([helper.make_node("Identity", ["x"], ["y"])], "mr2_setup",
                                  [], [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1])], [tensor])
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
        onnx.checker.check_model(model)
        return {"version": onnx.__version__, "path": onnx.__file__, "tiny_graph_checker": "passed",
                "scope": "Package health only; not a YOLO model export test"}

    def wheel_metadata():
        lock = json.loads(args.wheel_lock.read_text())
        expected = lock["wheels"][args.wheel_set]
        local = args.venv / "lib/python3.12/site-packages"
        from packaging.utils import canonicalize_name
        from packaging.requirements import Requirement
        from packaging.markers import default_environment
        actual = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions(path=[str(local)])}
        forbidden = {"numpy", "opencv-python", "opencv-python-headless", "opencv-contrib-python",
                     "opencv-contrib-python-headless", "cuda-toolkit", "nvidia-cudnn-cu13", "tensorrt", "tensorrt-cu13"}
        bad = forbidden & set(actual)
        if bad:
            raise RuntimeError("Forbidden duplicate providers in the venv: " + ", ".join(sorted(bad)))
        exceptions = {("ultralytics", "opencv-python"): "Ubuntu python3-opencv",
                      ("torch", "cuda-toolkit"): "JetPack system CUDA 13.2.2",
                      ("torch", "nvidia-cudnn-cu13"): "JetPack system cuDNN 9.20.0.46"}
        env = default_environment()
        env["extra"] = ""
        checks, accepted = [], []
        for item in expected:
            name = canonicalize_name(item["package"])
            dist = actual.get(name)
            if dist is None or dist.version != item["version"]:
                raise RuntimeError(f"Incorrect local wheel {name}: {dist.version if dist else 'missing'}; expected {item['version']}")
            for text in dist.requires or []:
                req = Requirement(text)
                if req.marker and not req.marker.evaluate(env):
                    continue
                dependency = canonicalize_name(req.name)
                if (name, dependency) in exceptions:
                    accepted.append({"parent": name, "requires": text, "provider": exceptions[(name, dependency)]})
                    continue
                try:
                    version = metadata.version(req.name)
                except metadata.PackageNotFoundError as exc:
                    raise RuntimeError(f"Missing dependency for {name}: {text}") from exc
                if version not in req.specifier:
                    raise RuntimeError(f"Unsatisfied dependency for {name}: {text}; actual {version}")
                checks.append({"parent": name, "requires": text, "version": version})
        return {"local_distributions": {n: d.version for n, d in actual.items()},
                "dependency_checks": checks, "documented_provider_exceptions": accepted}

    def perception_imports():
        import torch
        import torchvision
        import ultralytics
        import sympy
        import mpmath
        import polars
        import setuptools
        modules = [torch, torchvision, ultralytics, sympy, mpmath, polars, setuptools]
        for module in modules:
            if not Path(module.__file__).resolve().is_relative_to(args.venv.resolve()):
                raise RuntimeError("Perception module does not use the venv: " + module.__name__)
        if polars.DataFrame({"x": [1, 2]}).select(polars.col("x").sum()).item() != 3:
            raise RuntimeError("Polars native runtime operation failed")
        return {module.__name__: {"path": module.__file__, "version": str(getattr(module, "__version__", "unknown"))}
                for module in modules}

    def compute():
        import torch
        import torchvision
        gpu = args.profile == "rover"
        device = "cuda:0" if gpu else "cpu"
        if gpu and not torch.cuda.is_available():
            raise RuntimeError("The rover GPU is not available")
        expected = torch.arange(16, dtype=torch.float32).reshape(4, 4)
        actual = expected.to(device) @ torch.eye(4, device=device)
        if not torch.equal(actual.cpu(), expected):
            raise RuntimeError("Tensor operation failed")
        boxes = torch.tensor([[0, 0, 10, 10], [1, 1, 9, 9]], dtype=torch.float32, device=device)
        kept = torchvision.ops.nms(boxes, torch.tensor([0.9, 0.8], device=device), 0.5)
        if kept.cpu().tolist() != [0]:
            raise RuntimeError("torchvision native NMS failed")
        detail = {"device": device, "tensor_operation": "passed", "torchvision_nms": "passed"}
        if gpu:
            if args.gpu_library_directory is None:
                raise RuntimeError("The rover SDK library directory was not supplied")
            detail["sdk_library_aliases"] = gpu_alias_paths(
                json.loads(args.wheel_lock.read_text()), args.gpu_library_directory)
            if not torch.backends.cudnn.is_available():
                raise RuntimeError("cuDNN is not available")
            for dtype in [torch.float32, torch.float16]:
                layer = torch.nn.Conv2d(3, 4, 3, padding=1).to(device=device, dtype=dtype)
                with torch.inference_mode():
                    result = layer(torch.ones((1, 3, 16, 16), device=device, dtype=dtype))
                torch.cuda.synchronize()
                if result.shape != (1, 4, 16, 16) or not torch.isfinite(result).all().item():
                    raise RuntimeError("cuDNN convolution failed")
            detail.update(gpu_name=torch.cuda.get_device_name(0), cudnn_version=torch.backends.cudnn.version(),
                          fp32_fp16_convolution="passed")
            # Exercise nvJPEG as well as NMS/cuDNN: importing image.so alone
            # does not prove that the replacement SDK decoder actually runs.
            image = torch.arange(32 * 32, dtype=torch.int32).remainder(256).to(torch.uint8).reshape(1, 32, 32).repeat(3, 1, 1)
            encoded = torchvision.io.encode_jpeg(image)
            decoded = torchvision.io.decode_jpeg(encoded, device="cuda")
            reference = torchvision.io.decode_jpeg(encoded, device="cpu")
            error = (decoded.cpu().float() - reference.float()).abs().mean().item()
            if not decoded.is_cuda or decoded.shape != image.shape or error > 3:
                raise RuntimeError("System nvJPEG GPU decoding failed")
            detail["nvjpeg_gpu_decode"] = {"status": "passed", "mean_abs_error_vs_cpu": error}
            paths = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                            if "/" in line.split()[-1]})
            selected = [p for p in paths if is_core_gpu_library(p)]
            if not any("libcudart.so" in p for p in selected) or not any("libcudnn.so" in p for p in selected):
                raise RuntimeError("CUDA/cuDNN core mappings were not found after compute")
            duplicates = [p for p in selected if str(args.venv) in p or "/site-packages/" in p]
            if duplicates:
                raise RuntimeError("Core GPU libraries are also loaded from Python packages: " + ", ".join(duplicates))
            ownership = {}
            for path in selected:
                real = str(Path(path).resolve())
                p = subprocess.run(["dpkg-query", "-S", real], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if p.returncode:
                    raise RuntimeError("GPU core library is not owned by an apt package: " + real)
                ownership[real] = p.stdout.strip()
            detail["system_sdk_mappings"] = ownership
            detail["auxiliary_gpu_libraries"] = [p for p in paths if any(s in p for s in ["libnccl", "libnvshmem", "libcusparseLt"])]
        return detail

    def tensorrt_import():
        import tensorrt
        if not tensorrt.__version__.startswith("10.16.2"):
            raise RuntimeError("TensorRT is not the JetPack 7.2.1 version: " + tensorrt.__version__)
        if args.venv and Path(tensorrt.__file__).resolve().is_relative_to(args.venv.resolve()):
            raise RuntimeError("TensorRT unexpectedly uses a pip/venv copy")
        return {"version": tensorrt.__version__, "path": tensorrt.__file__,
                "engine_inference": "not verified; PyTorch remains the default"}

    try:
        check("interpreter", base_interpreter)
        check("apt_numpy", lambda: apt_module("numpy", "python3-numpy", "1.26.4"))
        check("apt_opencv", lambda: apt_module("cv2", "python3-opencv", "4.6.0"))
        check("jazzy_python_and_messages", ros)
        check("opencv_numpy_cv_bridge", image_bridge)
        if args.venv:
            check("shared_apt_python_modules", shared_modules)
            check("wheel_versions_and_metadata", wheel_metadata)
            check("perception_imports_and_native_polars", perception_imports)
            check("cpu_or_gpu_native_compute", compute)
            check("apt_onnx_package", onnx_runtime_package)
            if args.profile == "rover":
                check("system_tensorrt_import", tensorrt_import)
        else:
            check("apt_setuptools", lambda: apt_module("setuptools", "python3-setuptools", "68.1.2"))
        results["status"] = "passed"
    except Exception as exc:
        results["status"] = "failed"
        results["error"] = str(exc)
        results["traceback"] = traceback.format_exc()
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": results["status"], "checks": len(results["checks"]),
                      "error": results.get("error"), "result": str(args.output)}, ensure_ascii=False))
    return 0 if results["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
