"""Piper X passive episode recorder. See docs/DATA_SPEC.md."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from queue import Full, Queue
import shutil
import struct
from threading import Event, Thread
import time
from uuid import uuid4

from can_channel import open_can_channel

ROOT = Path(__file__).resolve().parents[1]
SIDES = ("left", "right")
VIEWS = ("left_wrist", "right_wrist", "overview")
FEEDBACK = (0x2A5, 0x2A6, 0x2A7, 0x2A8)
TARGET = (0x155, 0x156, 0x157, 0x159)
FOLLOWER_STATUS = 0x2A1
CONTEXT_FEEDBACK_MAX_AGE_NS = 50_000_000
TRAJECTORY_USABILITIES = ("usable", "unusable", "unreviewed")
FIELDS = (("j1", "j2"), ("j3", "j4"), ("j5", "j6"), ("gripper_width",))


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def log(file, data):
    file.write(json.dumps(data, separators=(",", ":")) + "\n")


def decode(row):
    """Decode only the known standard, eight-byte position messages."""
    if row["error"] or row["extended"] or row["remote"]:
        return None
    key = row["id"]
    if key not in FEEDBACK and key not in TARGET:
        return None
    data = bytes.fromhex(row["data"])
    if len(data) != 8:
        return None
    kind, ids = ("state", FEEDBACK) if key in FEEDBACK else ("action", TARGET)
    group = ids.index(key)
    valid = True
    if group == 3:
        valid = data[7] == 0
        values = [struct.unpack_from(">i", data)[0] * 1e-6] if valid else [None]
    else:
        values = [math.radians(n / 1000) for n in struct.unpack(">ii", data)]
    return {
        "kind": kind, "fields": FIELDS[group], "values": values, "valid": valid,
        "raw_index": row["index"], "host_monotonic_ns": row["host_monotonic_ns"],
    }


def follower_status_ok(row):
    data = bytes.fromhex(row["data"])
    return len(data) == 8 and data[0] == 1 and data[1] == 0 and data[6:8] == b"\0\0"


def can_samples(raw, settings, *, close_device=True):
    import usb.core
    from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID, GsUsbFrame

    with open_can_channel(raw, settings, listen_only=True, close_device=close_device) as (_, hardware_time):
        size = GsUsbFrame().__sizeof__(hardware_time)
        while True:
            try:
                packet = raw.read(0x81, size, timeout=100)
            except usb.core.USBTimeoutError:
                yield None
                continue
            now = time.perf_counter_ns()
            frame = GsUsbFrame()
            GsUsbFrame.unpack_into(frame, packet, hardware_time)
            if frame.echo_id == GS_USB_NONE_ECHO_ID:
                yield {
                    "host_monotonic_ns": now,
                    "adapter_timestamp_us": frame.timestamp_us if hardware_time else None,
                    "id": frame.arbitration_id, "error": frame.is_error_frame,
                    "extended": frame.is_extended_id, "remote": frame.is_remote_frame,
                    "flags": frame.flags, "dlc": frame.can_dlc,
                    "data": bytes(frame.data[:frame.can_dlc]).hex(),
                }
            else:
                yield None


def camera_samples(serial, config, depth):
    import numpy as np
    import pyrealsense2 as rs

    pipeline = rs.pipeline()
    settings = rs.config()
    settings.enable_device(serial)
    size = config["width"], config["height"]
    settings.enable_stream(rs.stream.color, *size, rs.format.rgb8, config["fps"])
    if depth:
        settings.enable_stream(rs.stream.depth, *size, rs.format.z16, config["fps"])
    profile = pipeline.start(settings)
    try:
        scale = profile.get_device().first_depth_sensor().get_depth_scale() if depth else None
        while True:
            frames = pipeline.wait_for_frames(1200)
            now = time.perf_counter_ns()
            packet = []
            for kind, frame in (("rgb", frames.get_color_frame()), ("depth", frames.get_depth_frame())):
                if kind == "depth" and not depth:
                    continue
                if not frame:
                    raise RuntimeError(f"Missing {kind} in frameset")
                row = {
                    "stream": kind, "host_monotonic_ns": now,
                    "frame_number": frame.get_frame_number(),
                    "device_timestamp_ms": frame.get_timestamp(),
                    "timestamp_domain": str(frame.get_frame_timestamp_domain()),
                }
                if kind == "depth":
                    row["depth_scale_m"] = scale
                packet.append((row, np.asanyarray(frame.get_data())))
            # Copy only packets admitted to the recording window.
            yield now, packet
    finally:
        pipeline.stop()


def synthetic(kind, config, depth, active=True, home_window=None):
    import numpy as np

    index = 0
    image = np.zeros((config["height"], config["width"], 3), dtype=np.uint8)
    depth_image = image[:, :, 0].astype(np.uint16)
    keys = (FOLLOWER_STATUS,) + FEEDBACK + (TARGET if active else ())
    while True:
        time.sleep(1 / config["fps"] if kind == "camera" else 0.002)
        now = time.perf_counter_ns()
        index += 1
        if kind == "can":
            key = keys[index % len(keys)]
            at_home = home_window is not None and home_window["start"] is None
            if at_home and key in TARGET[:3]:
                continue
            position = 0 if at_home and key in FEEDBACK[:3] else index
            payload = b"\x01" + bytes(7) if key == FOLLOWER_STATUS else struct.pack(">ii", position, 0)
            yield {"host_monotonic_ns": now, "adapter_timestamp_us": None, "id": key,
                   "error": False, "extended": False, "remote": False, "flags": 0,
                   "dlc": 8, "data": payload.hex()}
        else:
            row = {"host_monotonic_ns": now, "frame_number": index,
                   "device_timestamp_ms": now / 1e6, "timestamp_domain": "synthetic"}
            packet = [(dict(row, stream="rgb"), image)]
            if depth:
                packet.append((dict(row, stream="depth", depth_scale_m=0.001), depth_image))
            yield now, packet


class Stream:
    def __init__(self, name, kind, source, folder, config, stop, window):
        self.name = name
        self.kind = kind
        self.source = source
        self.folder = folder / kind / name
        self.config = config
        self.stop = stop
        self.window = window
        self.ready = Event()
        self.done = Event()
        limit = config["camera_queue_frames"] if kind == "camera" else config["can_queue_frames"]
        self.queue = Queue(limit)
        self.counts = Counter()
        self.errors = []
        self.last_frame = {}
        self.max_gap_ns = {}
        self.feedback_times = {}
        self.decoded_counts = Counter()
        self.timelines = {}
        self.domains = {}
        self.context_counts = Counter()
        self.live_target_count = 0
        self.can_context = {}
        self.last_context_fault_ns = 0

    def enqueue(self, sample):
        try:
            self.queue.put_nowait(sample)
        except Full:
            self.counts["queue_overflow"] += 1
            raise RuntimeError("Recording queue saturated")

    def cache_can_context(self, context, sample):
        now = time.perf_counter_ns() if sample is None else sample["host_monotonic_ns"]
        fault = False
        if sample is not None:
            fault = sample["error"]
            if not fault and not sample["extended"] and not sample["remote"]:
                key = sample["id"]
                if key in (*FEEDBACK, *TARGET, FOLLOWER_STATUS):
                    context[key] = sample
        status = context.get(FOLLOWER_STATUS)
        if fault or (sample is not None and sample["id"] == FOLLOWER_STATUS
                     and not sample["extended"] and not sample["remote"]
                     and not follower_status_ok(sample)):
            self.last_context_fault_ns = now
        healthy = (not fault and status is not None and follower_status_ok(status)
                   and now - status["host_monotonic_ns"] <= CONTEXT_FEEDBACK_MAX_AGE_NS
                   and len(self.feedback_times) == len(FEEDBACK)
                   and now - min(self.feedback_times.values()) <= CONTEXT_FEEDBACK_MAX_AGE_NS)
        if not healthy:
            for key in TARGET:
                context.pop(key, None)

    @staticmethod
    def copy_packet(packet, context=False):
        return [(dict(row, preroll=context), pixels.copy()) for row, pixels in packet]

    def capture(self):
        can_context = self.can_context
        camera_context = None
        context_flushed = False
        try:
            for sample in self.source:
                if self.stop.is_set():
                    break
                if sample is not None:
                    now = sample[0] if self.kind == "camera" else sample["host_monotonic_ns"]
                    if self.kind == "can" and sample["id"] in FEEDBACK:
                        signal = decode(dict(sample, index=-1))
                        if signal and signal["valid"]:
                            self.feedback_times[sample["id"]] = now
                    if self.kind == "camera" or len(self.feedback_times) == len(FEEDBACK):
                        self.ready.set()
                    start = self.window["start"]
                    if start is None or now < start:
                        if self.kind == "can":
                            self.cache_can_context(can_context, sample)
                        else:
                            camera_context = self.copy_packet(sample[1], context=True)
                    else:
                        if not context_flushed:
                            if self.kind == "can":
                                for frame in sorted(can_context.values(), key=lambda item: item["host_monotonic_ns"]):
                                    self.enqueue(dict(frame, preroll=True))
                                can_context.clear()
                            elif camera_context is not None:
                                self.enqueue(camera_context)
                                camera_context = None
                            context_flushed = True
                        self.enqueue(self.copy_packet(sample[1]) if self.kind == "camera" else sample)
                elif self.kind == "can" and not context_flushed:
                    self.cache_can_context(can_context, None)
                if self.kind == "can" and self.window["start"] is not None:
                    last = min(self.feedback_times.values(), default=None)
                    if last is None or time.perf_counter_ns() - last > self.config["can_silence_ms"] * 1e6:
                        raise RuntimeError("CAN feedback group timeout")
        except Exception as exc:
            self.errors.append(f"capture: {exc!r}")
            self.stop.set()
        finally:
            try:
                self.source.close()
            except Exception as exc:
                self.errors.append(f"close: {exc!r}")
                self.stop.set()
            finally:
                self.done.set()

    def write(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / "frames.jsonl").open("w", encoding="utf-8", buffering=65536) as raw:
                if self.kind == "can":
                    with (self.folder / "signals.jsonl").open("w", encoding="utf-8", buffering=65536) as signals:
                        self.drain(lambda row: self.write_can(row, raw, signals))
                else:
                    self.drain(lambda packet: self.write_images(packet, raw))
        except Exception as exc:
            self.errors.append(f"write: {exc!r}")
            self.stop.set()

    def drain(self, write):
        from queue import Empty

        while not self.done.is_set() or not self.queue.empty():
            try:
                sample = self.queue.get(timeout=0.1)
            except Empty:
                continue
            write(sample)

    def write_can(self, row, raw, signals):
        row["index"] = self.counts["frames"]
        log(raw, row)
        self.counts["frames"] += 1
        self.counts["error_frames"] += int(row["error"])
        if row.get("preroll"):
            self.context_counts["frames"] += 1
        else:
            self.track_time("can", row["host_monotonic_ns"])
        signal = decode(row)
        if signal is not None:
            log(signals, signal)
            if signal["valid"] and not row.get("preroll"):
                self.track_time(hex(row["id"]), row["host_monotonic_ns"])
                self.live_target_count += int(signal["kind"] == "action")
            self.decoded_counts[str(row["id"])] += int(signal["valid"])
            self.counts["invalid_signal"] += int(not signal["valid"])

    def track_time(self, kind, now):
        first, last = self.timelines.get(kind, (now, now))
        self.timelines[kind] = first, now
        self.max_gap_ns[kind] = max(self.max_gap_ns.get(kind, 0), now - last)

    def write_images(self, packet, index):
        import numpy as np
        from PIL import Image

        for row, pixels in packet:
            kind = row["stream"]
            number = row["frame_number"]
            previous = self.last_frame.get(kind)
            if previous is not None:
                old_number, old_ts, old_domain = previous
                self.counts[f"{kind}_skips"] += max(0, number - old_number - 1)
                self.counts[f"{kind}_duplicates"] += int(number == old_number)
                self.counts[f"{kind}_reset"] += int(number < old_number)
                self.counts[f"{kind}_clock_changes"] += int(
                    row["timestamp_domain"] != old_domain or row["device_timestamp_ms"] <= old_ts
                )
            self.last_frame[kind] = number, row["device_timestamp_ms"], row["timestamp_domain"]
            self.domains.setdefault(kind, set()).add(row["timestamp_domain"])
            if row.get("preroll"):
                self.context_counts[kind] += 1
            else:
                self.track_time(kind, row["host_monotonic_ns"])
            row["index"] = self.counts[kind]
            extension = "jpg" if kind == "rgb" else "npy"
            row["file"] = f'{kind}_{row["index"]:07d}.{extension}'
            path = self.folder / row["file"]
            if kind == "rgb":
                Image.fromarray(pixels).save(path, quality=self.config["jpeg_quality"], subsampling=0)
            else:
                np.save(path, pixels, allow_pickle=False)
            log(index, row)
            self.counts[kind] += 1

    def report(self, active, depth):
        issues = list(self.errors)
        required = ("frames",) if self.kind == "can" else (("rgb", "depth") if depth else ("rgb",))
        issues.extend(f"{key}: no frames after start" for key in required
                      if self.counts[key] <= self.context_counts[key])
        for key, value in self.counts.items():
            if value and key not in {"rgb", "depth", "frames"}:
                issues.append(f"{key}: {value}")
        warnings = []
        if self.kind == "can":
            missing_state = [hex(key) for key in FEEDBACK if not self.decoded_counts[str(key)]]
            if missing_state:
                issues.append(f"missing valid feedback IDs: {missing_state}")
            missing_target = [hex(key) for key in TARGET if not self.decoded_counts[str(key)]]
            if active and missing_target:
                warnings.append(f"active arm missing target IDs: {missing_target}")
            if not active and self.live_target_count:
                warnings.append("target frames on arm outside active_arms")
        return {"counts": dict(self.counts), "preroll_counts": dict(self.context_counts),
                "valid_signal_counts_by_id": dict(self.decoded_counts),
                "max_host_gap_ns": self.max_gap_ns, "host_range_ns": self.timelines,
                "timestamp_domains": {kind: sorted(values) for kind, values in self.domains.items()},
                "issues": issues, "warnings": warnings}


def load_devices():
    path = ROOT / "configs" / "devices.json"
    if not path.is_file():
        raise ValueError("Copy configs/devices.example.json to configs/devices.json and set the device serials.")
    config = load(path)
    for group, names in (("can", SIDES), ("cameras", VIEWS)):
        settings = config.get(group, {})
        keys = [settings.get(name, {}).get("serial") for name in names]
        if any(not isinstance(key, str) or not key or key != key.strip() for key in keys):
            raise ValueError(f"Set all {group} serial numbers in configs/devices.json.")
        if len(set(keys)) != len(keys):
            raise ValueError(f"Duplicate {group} serial numbers in configs/devices.json.")
    return config


def find_can_adapters(required_serials=(), *, attempts=8, retry_delay=0.35):
    import libusb_package
    import usb.core
    import usb.util

    required = set(required_serials)
    backend = libusb_package.get_libusb1_backend()
    if backend is None:
        raise RuntimeError("libusb backend unavailable")
    for attempt in range(attempts):
        adapters, unreadable = {}, []
        devices = usb.core.find(find_all=True, idVendor=0x1D50, idProduct=0x606F, backend=backend)
        for device in devices:
            try:
                serial = device.serial_number
                if not serial:
                    raise ValueError("empty USB serial")
            except (usb.core.USBError, ValueError) as exc:
                unreadable.append(f"USB bus {device.bus}, address {device.address}: {exc}")
                try:
                    usb.util.dispose_resources(device)
                except usb.core.USBError:
                    pass
                continue
            if serial in adapters:
                raise RuntimeError(f"Multiple USB-CAN adapters report serial {serial}")
            adapters[serial] = device
        missing = required - adapters.keys()
        if not missing and (required or not unreadable):
            return adapters
        if attempt + 1 == attempts:
            detail = " | ".join(unreadable) if unreadable else "Required adapters were not visible"
            raise RuntimeError(f"USB-CAN identification failed after {attempts} scans: {detail}")
        for device in adapters.values():
            try:
                usb.util.dispose_resources(device)
            except usb.core.USBError:
                pass
        # A new PyUSB Device on each scan avoids reusing a failed language descriptor cache.
        time.sleep(retry_delay)
    raise ValueError("USB discovery needs at least one scan")


def inventory(required_serials=()):
    import pyrealsense2 as rs

    adapters = find_can_adapters(required_serials)
    cameras = {device.get_info(rs.camera_info.serial_number) for device in rs.context().query_devices()}
    return adapters, cameras


@contextmanager
def connected_capture_devices():
    import usb.core
    import usb.util

    config = load_devices()
    adapters, camera_ids = inventory(config["can"][side]["serial"] for side in SIDES)
    try:
        missing = [view for view in VIEWS if config["cameras"][view]["serial"] not in camera_ids]
        if missing:
            raise RuntimeError(f"Missing required cameras before Home: {missing}")
        yield adapters, camera_ids
    finally:
        for device in adapters.values():
            try:
                usb.util.dispose_resources(device)
            except usb.core.USBError:
                pass


def manifest(folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            files[path.relative_to(folder).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest}
    return files


def wait_for_input(stop, seconds=None, check=None):
    if seconds is not None:
        deadline = time.monotonic() + seconds
        while not stop.wait(max(0, min(1, deadline - time.monotonic()))):
            if check:
                check()
            if time.monotonic() >= deadline:
                break
        return
    import msvcrt

    next_check = time.monotonic()
    while not stop.wait(0.02):
        if check and time.monotonic() >= next_check:
            check()
            next_check = time.monotonic() + 1
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key == "\r":
                return
            if key == "\x1b":
                raise KeyboardInterrupt


def read_trajectory_usability():
    while True:
        answer = input("Trajectory usability [usable/unusable/unreviewed; Enter=unreviewed]: ").strip().lower()
        if not answer:
            return "unreviewed"
        if answer in TRAJECTORY_USABILITIES:
            return answer
        print("Choose usable, unusable or unreviewed.", flush=True)


def validate_images(stream, config):
    import numpy as np
    from PIL import Image

    counts = Counter()
    with (stream.folder / "frames.jsonl").open(encoding="utf-8") as index:
        for line in index:
            row = json.loads(line)
            kind = row["stream"]
            if row["index"] != counts[kind]:
                raise ValueError(f"{kind}: inconsistent frame index")
            path = stream.folder / row["file"]
            shape = config["height"], config["width"]
            if kind == "rgb":
                with Image.open(path) as image:
                    image.load()
                    if image.mode != "RGB" or image.size != shape[::-1]:
                        raise ValueError(f"Invalid RGB dimensions or mode: {path}")
            else:
                pixels = np.load(path, allow_pickle=False)
                if pixels.dtype != np.uint16 or pixels.shape != shape:
                    raise ValueError(f"Invalid depth dimensions or type: {path}")
            counts[kind] += 1
    if any(counts[kind] != stream.counts[kind] for kind in ("rgb", "depth")):
        raise ValueError("Frame index count differs from writer count")


def record(args, *, connected=None):
    config = load(ROOT / "configs" / "capture_v0_1.json")
    devices = None if args.simulate else load_devices()
    active = args.arms.split(",") if args.arms != "none" else []
    required_cameras = {"overview", *(f"{side}_wrist" for side in active)}
    required_can = set(SIDES if not active else active)
    if not args.allow_missing_optional:
        required_can, required_cameras = set(SIDES), set(VIEWS)
    if args.simulate:
        adapters, camera_ids = {}, set()
    else:
        required_serials = [devices["can"][side]["serial"] for side in sorted(required_can)]
        adapters, camera_ids = connected if connected is not None else inventory(required_serials)
    enabled_can = [side for side in SIDES if args.simulate or devices["can"][side]["serial"] in adapters]
    enabled_views = [view for view in VIEWS if args.simulate or devices["cameras"][view]["serial"] in camera_ids]
    missing = (required_can - set(enabled_can)) | (required_cameras - set(enabled_views))
    if missing:
        raise RuntimeError(f"Missing required devices: {sorted(missing)}")

    output = ROOT / "data" / ("recorder_selftest" if args.simulate else "raw") / args.session
    output.mkdir(parents=True, exist_ok=True)
    def check_disk():
        if shutil.disk_usage(output).free < config["min_free_bytes"]:
            raise RuntimeError("Insufficient free disk space")

    check_disk()
    episode_id = str(uuid4())
    folder = output / (episode_id + ".inprogress")
    folder.mkdir()
    snapshot = folder / "config_snapshot"
    snapshot.mkdir()
    config_root = ROOT / "configs"
    shutil.copyfile(config_root / "capture_v0_1.json", snapshot / "capture_v0_1.json")
    if not args.simulate:
        shutil.copyfile(config_root / "devices.json", snapshot / "devices.json")
        firmware = config_root / "robot_firmware.json"
        if firmware.is_file():
            shutil.copyfile(firmware, snapshot / firmware.name)
        calibration = config_root / "camera_calibration"
        if calibration.is_dir():
            shutil.copytree(calibration, snapshot / calibration.name)
    metadata = {"schema": "piperx-episode/0.1", "session_id": args.session, "episode_id": episode_id,
                "created_utc": datetime.now(timezone.utc).isoformat(), "status": "recording",
                "task": args.task, "active_arms": active, "action_source": "synthetic" if args.simulate else "teleop",
                "simulated": args.simulate, "depth": args.depth,
                "host_clock": "time.perf_counter_ns",
                "required_can": sorted(required_can), "required_views": sorted(required_cameras),
                "recorded_can": enabled_can, "recorded_views": enabled_views,
                "missing_can": sorted(set(SIDES) - set(enabled_can)),
                "missing_views": sorted(set(VIEWS) - set(enabled_views)),
                "recorder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    metadata["preroll"] = "last CAN status, feedback and healthy targets, plus one frameset per camera"
    if getattr(args, "task_id", None):
        metadata.update(task_id=args.task_id, collection_session=args.collection_session)
    dump(folder / "metadata.json", metadata)
    print(f"Episode: {folder}", flush=True)
    stop = Event()
    window = {"start": None}
    home = getattr(args, "home_reset", None)
    streams = []
    for kind, names in (("camera", enabled_views), ("can", enabled_can)):
        for name in names:
            if args.simulate:
                source = synthetic(kind, config, args.depth, active=name in active,
                                   home_window=window if home and name in active else None)
            elif kind == "camera":
                source = camera_samples(devices["cameras"][name]["serial"], config, args.depth)
            else:
                settings = devices["can"][name]
                source = can_samples(adapters[settings["serial"]], settings, close_device=connected is None)
            streams.append(Stream(name, kind, source, folder, config, stop, window))
    workers = []
    end = None
    aborted = None
    result, reason = None, None
    annotation_interrupted = False
    try:
        for stream in streams:
            for target in (stream.capture, stream.write):
                thread = Thread(target=target)
                thread.start()
                workers.append(thread)
        deadline = time.monotonic() + config["startup_timeout_s"]
        while not all(stream.ready.is_set() for stream in streams):
            if stop.wait(0.02) or time.monotonic() >= deadline:
                waiting = [stream.name for stream in streams if not stream.ready.is_set()]
                raise RuntimeError(f"Startup failed or streams not ready: {waiting}")
        if stop.wait(config["warmup_s"]):
            raise RuntimeError("Stream failed during warmup")
        if args.seconds is None and home:
            print("READY. Keep selected pairs at Home. Verify drag mode and a clear area, then press Enter.", flush=True)
        else:
            print("READY. Prepare the task, let both arms settle, then press Enter."
                  if args.seconds is None else "READY. Starting timed capture.", flush=True)
        if args.seconds is None:
            wait_for_input(stop)
        if stop.is_set():
            raise RuntimeError("Stream failed before recording")
        if home:
            from home_reset import verify_start

            window["start"] = verify_start(streams, home)
        else:
            window["start"] = time.perf_counter_ns()
        print("RECORDING. Enter stops; Esc/Ctrl+C aborts." if args.seconds is None else "RECORDING", flush=True)
        wait_for_input(stop, args.seconds, check_disk)
        if stop.is_set():
            raise RuntimeError("Stream failed during recording")
    except KeyboardInterrupt:
        aborted = "operator_abort"
    except Exception as exc:
        aborted = str(exc)
    finally:
        end = time.perf_counter_ns()
        stop.set()
        for thread in workers:
            thread.join()

    if aborted is None and args.seconds is None:
        try:
            while result not in {"success", "failure", "aborted", "unlabeled"}:
                result = input("Result [success/failure/aborted/unlabeled]: ").strip()
            reason = input("Termination note (optional): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            annotation_interrupted = True
            result = result if result in {"success", "failure", "aborted"} else "unlabeled"
    elif aborted:
        result, reason = "aborted", aborted
    else:
        result = args.result

    if getattr(args, "confirm_follow", False):
        confirmed = args.simulate and not (aborted or annotation_interrupted)
        if args.seconds is None and not (aborted or annotation_interrupted) and result != "aborted":
            try:
                confirmed = input("Was leader-follower mode maintained throughout? [y/N]: ").strip().lower() == "y"
            except (EOFError, KeyboardInterrupt):
                annotation_interrupted = True
                confirmed = False
        metadata["operator_confirmed_follow"] = confirmed

    usability = "unreviewed"
    if getattr(args, "label_usability", False) and not (aborted or annotation_interrupted) and result != "aborted":
        if args.seconds is None:
            try:
                usability = read_trajectory_usability()
            except (EOFError, KeyboardInterrupt):
                annotation_interrupted = True
        elif args.simulate:
            usability = "usable"
    metadata["trajectory_usability"] = usability

    for stream in streams:
        if stream.kind == "camera":
            try:
                validate_images(stream, config)
            except Exception as exc:
                stream.errors.append(f"readback: {exc!r}")
    reports = {f"{stream.kind}/{stream.name}": stream.report(stream.name in active, args.depth)
               for stream in streams}
    status = "incomplete" if aborted or any(stream.errors for stream in streams) else (
        "failed" if any(report["issues"] for report in reports.values()) else
        "warning" if any(report["warnings"] for report in reports.values()) else "passed")
    if home:
        metadata["home_reset"] = home
    metadata.update(status="incomplete" if status == "incomplete" else "closed",
                    start_host_monotonic_ns=window["start"], stop_request_host_monotonic_ns=end,
                    result=result, termination_note=reason)
    dump(folder / "metadata.json", metadata)
    dump(folder / "quality.json", {"status": status, "capture_error": aborted, "streams": reports,
                                   "training_readiness": "not_evaluated"})
    if status != "incomplete":
        dump(folder / "manifest.json", {"algorithm": "sha256", "files": manifest(folder)})
    suffix = {"incomplete": ".incomplete", "failed": ".quality_failed"}.get(status, "")
    final = folder.with_name(episode_id + suffix)
    folder.rename(final)
    print(f"{status.upper()}: {final}", flush=True)
    if aborted == "operator_abort" or annotation_interrupted:
        raise KeyboardInterrupt
    return final, status


def main(args):
    _, status = record(args)
    return int(status in {"incomplete", "failed"})


def cli():
    import re
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--arms", choices=["left", "right", "left,right", "none"], required=True)
    parser.add_argument("--task", default="validation")
    parser.add_argument("--depth", action="store_true")
    parser.add_argument("--seconds", type=float, help="Timed capture; otherwise use Enter to start/stop")
    parser.add_argument("--result", choices=["success", "failure", "aborted", "unlabeled"], default="unlabeled")
    parser.add_argument("--allow-missing-optional", action="store_true")
    parser.add_argument("--simulate", action="store_true", help="Generate synthetic frames without hardware")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.session):
        parser.error("Invalid session name")
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("--seconds must be a finite positive number")
    if args.seconds is None and (sys.platform != "win32" or not sys.stdin.isatty()):
        parser.error("Interactive capture requires a Windows console; use --seconds")
    try:
        raise SystemExit(main(args))
    except KeyboardInterrupt:
        parser.exit(130, "\nStopped.\n")
    except Exception as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    cli()
