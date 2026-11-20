"""Return selected Piper X leader–follower pairs to Home before passive capture."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from uuid import uuid4

from can_channel import open_can_channel
from record_episode import (
    FEEDBACK, FOLLOWER_STATUS, ROOT, TARGET, decode, dump, find_can_adapters, follower_status_ok, load, load_devices,
)

SCHEMA = "piperx-home-reset/0.1"
JOINTS = tuple(f"j{i}" for i in range(1, 7))
HOME = [0.0] * len(JOINTS)
MAX_AGE_NS = 50_000_000
HOME_COMMAND_ID = 0x191


def open_bus(raw, settings, *, close_device=True):
    return open_can_channel(raw, settings, listen_only=False, close_device=close_device)


def read_feedback(raw, hardware_time):
    import usb.core
    from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID, GsUsbFrame

    try:
        data = raw.read(0x81, GsUsbFrame().__sizeof__(hardware_time), timeout=50)
    except usb.core.USBTimeoutError:
        return None
    now = time.perf_counter_ns()
    frame = GsUsbFrame()
    GsUsbFrame.unpack_into(frame, data, hardware_time)
    if frame.echo_id != GS_USB_NONE_ECHO_ID:
        return None
    return {"id": frame.arbitration_id, "error": frame.is_error_frame, "extended": frame.is_extended_id,
            "remote": frame.is_remote_frame, "data": bytes(frame.data[:frame.can_dlc]).hex(),
            "host_monotonic_ns": now, "index": -1}


def send_mode(adapter, mode):
    from gs_usb.gs_usb_frame import GsUsbFrame
    from pyAgxArm.protocols.can_protocol.drivers.piper.default.parser import Codec
    from pyAgxArm.protocols.can_protocol.msgs.piper.default import ArmMsgLeaderArmMoveToHome

    if mode not in (0, 2):
        raise ValueError("Only pair Home and restore-drag are allowed")
    payload = Codec().encode_191_leader_arm_move_to_home(ArmMsgLeaderArmMoveToHome(mode=mode))
    if not adapter.send(GsUsbFrame(can_id=HOME_COMMAND_ID, data=payload)):
        raise RuntimeError("Could not send the Home/drag command")


def snapshot(frames, now, since, require_follow):
    needed = (*FEEDBACK, FOLLOWER_STATUS)
    if any(key not in frames or not since < frames[key]["host_monotonic_ns"] <= now
           or now - frames[key]["host_monotonic_ns"] > MAX_AGE_NS for key in needed):
        return None
    status = frames[FOLLOWER_STATUS]
    status_data = bytes.fromhex(status["data"])
    if len(status_data) != 8 or status_data[1] or any(status_data[6:8]):
        raise RuntimeError("Follower reported abnormal status during Home verification")
    if require_follow and not follower_status_ok(status):
        return None
    signals = [decode(dict(frames[key], index=-1)) for key in FEEDBACK]
    if any(signal is None or not signal["valid"] for signal in signals):
        return None
    joints = [value for signal in signals[:3] for value in signal["values"]]
    return {"verified_host_monotonic_ns": now, "follower_joints_rad": joints,
            "follower_gripper_width_m": signals[-1]["values"][0],
            "status_data_hex": status["data"], "motion_reached": status_data[4] == 0,
            "feedback_host_monotonic_ns": {str(key): frames[key]["host_monotonic_ns"] for key in needed}}


def near_home(evidence, tolerance):
    return all(math.isfinite(value) and abs(value) <= tolerance for value in evidence["follower_joints_rad"])


def wait_feedback(read, settings, *, since, require_follow, check_home, check_stable=False, diagnostics=None):
    frames, counts = {}, Counter()
    deadline = time.perf_counter() + settings["timeout_s"]
    stable_from = None
    latest = None
    max_stable_ns = 0
    settling = check_home or check_stable

    def save_progress(now):
        if diagnostics is not None:
            diagnostics.update(
                counts={hex(key): count for key, count in counts.items()},
                latest_fresh_position=latest, longest_stable_ms=max_stable_ns / 1e6,
                latest_frame_age_ms={hex(key): (now-frame["host_monotonic_ns"]) / 1e6
                                     for key, frame in frames.items()},
            )

    while time.perf_counter() < deadline:
        frame = read()
        now = time.perf_counter_ns()
        if frame is not None:
            if frame["error"]:
                raise RuntimeError("CAN error during Home sequence")
            if not frame["extended"] and not frame["remote"]:
                counts[frame["id"]] += 1
            if not frame["extended"] and not frame["remote"] and frame["id"] in (*FEEDBACK, FOLLOWER_STATUS):
                if frame["id"] == FOLLOWER_STATUS:
                    data = bytes.fromhex(frame["data"])
                    if len(data) != 8 or data[1] or any(data[6:8]):
                        raise RuntimeError("Follower reported an error during Home sequence")
                frames[frame["id"]] = frame
        evidence = snapshot(frames, now, since, require_follow)
        if evidence is not None:
            latest = evidence
        if evidence is None or (check_home and not near_home(evidence, settings["joint_tolerance_rad"])):
            stable_from = None
            continue
        if not settling:
            save_progress(now)
            return evidence
        joints = evidence["follower_joints_rad"]
        if stable_from is not None:
            lows = [min(low, value) for low, value in zip(lows, joints)]
            highs = [max(high, value) for high, value in zip(highs, joints)]
            if any(high - low > settings["joint_stability_rad"] for low, high in zip(lows, highs)):
                stable_from = None
        if stable_from is None:
            stable_from = now
            lows, highs = joints[:], joints[:]
        max_stable_ns = max(max_stable_ns, now - stable_from)
        if now - stable_from >= settings["settle_s"] * 1e9:
            save_progress(now)
            return evidence
    save_progress(time.perf_counter_ns())
    if latest is None:
        detail = "no complete, fresh follower feedback in the expected mode"
    elif check_home and not near_home(latest, settings["joint_tolerance_rad"]):
        detail = "follower joints outside Home tolerance"
    else:
        detail = "follower position did not stay stable long enough"
    raise RuntimeError(f"Home verification timed out: {detail}")


def reset_selected(arms, simulated, *, adapters=None):
    if not arms or len(set(arms)) != len(arms) or any(side not in {"left", "right"} for side in arms):
        raise ValueError("Select a left, right or both Home pairs")
    settings = load(ROOT / "configs" / "home_reset.json")
    report = {"schema": SCHEMA, "simulated": simulated, "settings": settings, "sides": {},
              "requested_utc": datetime.now(timezone.utc).isoformat()}
    if simulated:
        report["sides"] = {side: {"joint_target_rad": HOME[:], "restored_at_ns": 0} for side in arms}
        return report

    config = load_devices()
    firmware_path = ROOT / "configs" / "robot_firmware.json"
    if not firmware_path.is_file():
        raise ValueError("Copy configs/robot_firmware.example.json and fill in the verified firmware versions.")
    firmware = load(firmware_path)
    selected_devices = {side: config["can"][side] for side in arms}
    owns_devices = adapters is None
    if owns_devices:
        adapters = find_can_adapters(device["serial"] for device in selected_devices.values())
    selected = {}
    for side in arms:
        for role in ("leader", "follower"):
            if firmware.get(f"{side}_{role}", {}).get("software_version") not in settings["approved_firmware"]:
                raise ValueError(f"{side} {role} firmware is outside the configured Home profile")
        device = config["can"][side]
        if device["serial"] not in adapters:
            raise RuntimeError(f"Missing {side} Home adapter")
        selected[side] = (adapters[device["serial"]], device)

    try:
        response = input(f"Move the {' and '.join(arms)} leader–follower pair(s) to Home? Clear their paths, then type HOME: ")
    except EOFError:
        raise KeyboardInterrupt from None
    if response.strip() != "HOME":
        raise KeyboardInterrupt

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    report_path = log_dir / f"home-{uuid4()}.json"
    report["log_file"] = report_path.name
    report["state"] = "running"
    dump(report_path, report)
    try:
        for side, (raw, device) in selected.items():
            print(f"HOMING: {side} pair", flush=True)
            checks = {}
            evidence = {"joint_target_rad": HOME[:], "adapter_serial": device["serial"], "waits": checks}
            report["sides"][side] = evidence
            with open_bus(raw, device, close_device=owns_devices) as (adapter, hardware_time):
                def read():
                    return read_feedback(raw, hardware_time)

                wait_feedback(read, settings, since=time.perf_counter_ns(), require_follow=True, check_home=False,
                              diagnostics=checks.setdefault("before_home", {}))
                evidence["home_requested_at_ns"] = time.perf_counter_ns()
                dump(report_path, report)
                send_mode(adapter, 2)
                evidence["home_write_returned"] = True
                evidence["home_follower_feedback"] = wait_feedback(
                    read, settings, since=evidence["home_requested_at_ns"], require_follow=False, check_home=True,
                    diagnostics=checks.setdefault("waiting_for_home", {}),
                )
            # Follower CAN feedback cannot independently verify the leader's actual joint angles.
            try:
                confirmed = input(f"Confirm the {side} leader is also at Home and both arms are stationary [y/N]: ")
            except EOFError:
                raise KeyboardInterrupt from None
            if confirmed.strip().lower() != "y":
                raise KeyboardInterrupt
            evidence["operator_verified_leader_home"] = True
            with open_bus(raw, device, close_device=owns_devices) as (adapter, hardware_time):
                wait_feedback(read, settings, since=time.perf_counter_ns(), require_follow=False, check_home=True,
                              diagnostics=checks.setdefault("before_restore", {}))
                evidence["restored_at_ns"] = time.perf_counter_ns()
                dump(report_path, report)
                send_mode(adapter, 0)
                evidence["restore_write_returned"] = True
                evidence["after_restore"] = wait_feedback(
                    read, settings, since=evidence["restored_at_ns"], require_follow=True, check_home=True,
                    diagnostics=checks.setdefault("after_restore", {}),
                )
        report["state"] = "ready_for_passive_verification"
        return report
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
        report.update(state="interrupted", error=repr(exc))
        print(f"Home sequence stopped. Check devices on site. Log: {report_path}", flush=True)
        raise
    finally:
        dump(report_path, report)


def resume_from_log(path, arms):
    path = path.resolve(strict=True)
    if path.parent != (ROOT / "logs").resolve():
        raise ValueError("Choose a completed Home report from this project's logs folder")
    report = load(path)
    sides = report.get("sides", {})
    devices = load_devices()["can"]
    if (report.get("schema") != SCHEMA or report.get("simulated") is not False
            or report.get("state") != "ready_for_passive_verification" or set(sides) != set(arms)
            or any(sides[side].get("adapter_serial") != devices[side]["serial"]
                   or not sides[side].get("operator_verified_leader_home")
                   or not sides[side].get("restore_write_returned")
                   or not sides[side].get("after_restore")
                   for side in arms)):
        raise ValueError("The report does not show a completed Home and restore for the selected pair(s)")
    latest = max(item["restored_at_ns"] for item in sides.values())
    if latest >= time.perf_counter_ns():
        raise ValueError("The Home report uses an earlier host clock. Run a fresh Home sequence.")
    try:
        answer = input("Are the selected leader and follower arms still stationary at Home, with drag restored? [y/N]: ")
    except EOFError:
        raise KeyboardInterrupt from None
    if answer.strip().lower() != "y":
        raise KeyboardInterrupt
    report["resumed_from_log"] = path.name
    return report


def verify_start(streams, report):
    contexts = {stream.name: (stream.can_context.copy(), stream.last_context_fault_ns) for stream in streams
                if stream.kind == "can" and stream.name in report["sides"]}
    now = time.perf_counter_ns()
    tolerance = report["settings"]["joint_tolerance_rad"]
    report["verified_at_start"] = False
    report["recording_boundary_check"] = {"host_monotonic_ns": now, "sides": {}}
    for side, item in report["sides"].items():
        context, last_fault_ns = contexts[side]
        checks = {"last_fault_host_monotonic_ns": last_fault_ns or None,
                  "feedback_age_ms": {hex(key): (now - frame["host_monotonic_ns"]) / 1e6
                                      for key, frame in context.items()
                                      if key in (*FEEDBACK, FOLLOWER_STATUS)}}
        report["recording_boundary_check"]["sides"][side] = checks
        position = snapshot(context, now, max(item["restored_at_ns"], last_fault_ns), True)
        checks["follower_position"] = position
        if position is None:
            raise RuntimeError(f"{side} follower has no complete fresh feedback in follow mode at start")
        if not near_home(position, tolerance):
            outside = {joint: value for joint, value in zip(JOINTS, position["follower_joints_rad"])
                       if abs(value) > tolerance or not math.isfinite(value)}
            checks["outside_home_tolerance_rad"] = outside
            detail = ", ".join(f"{joint.upper()}={math.degrees(value):+.2f} deg" for joint, value in outside.items())
            raise RuntimeError(f"{side} follower outside Home tolerance at start: "
                               f"{detail} (limit {math.degrees(tolerance):.2f} deg)")
        for key in TARGET[:3]:
            cached = context.get(key)
            if cached is not None:
                signal = decode(dict(cached, index=-1))
                if signal is None or not signal["valid"] or any(abs(value) > tolerance for value in signal["values"]):
                    raise RuntimeError(f"{side} leader indicated a non-Home target before start")
        item["at_recording_start"] = position
    report["verified_at_start"] = True
    report["start_host_monotonic_ns"] = now
    return now


def restore_from_log(path, side):
    path = path.resolve(strict=True)
    log_dir = (ROOT / "logs").resolve()
    if path.parent != log_dir:
        raise ValueError("Select an interrupted Home log from this project's logs folder")
    previous = load(path)
    attempted = previous.get("sides", {}).get(side, {})
    device = load_devices()["can"][side]
    if (previous.get("schema") != SCHEMA or previous.get("simulated") is not False
            or previous.get("state") != "interrupted" or not attempted.get("home_requested_at_ns")
            or attempted.get("adapter_serial") != device["serial"]):
        raise ValueError(f"This log does not show an interrupted Home request on the {side} adapter")

    settings = load(ROOT / "configs" / "home_reset.json")
    adapters = find_can_adapters([device["serial"]])
    if device["serial"] not in adapters:
        raise RuntimeError(f"Missing {side} adapter")
    try:
        response = input(f"Restore drag on {side} only? Verify both arms have stopped, clear the area, then type RESTORE: ")
    except EOFError:
        raise KeyboardInterrupt from None
    if response.strip() != "RESTORE":
        raise KeyboardInterrupt

    raw = adapters[device["serial"]]
    trace = {"schema": SCHEMA, "operation": "restore_drag_only", "side": side, "source_log": path.name,
             "adapter_serial": device["serial"], "state": "running", "waits": {},
             "requested_utc": datetime.now(timezone.utc).isoformat()}
    report_path = log_dir / f"restore-drag-{uuid4()}.json"
    dump(report_path, trace)
    print(f"Recovery log: {report_path}", flush=True)
    try:
        with open_bus(raw, device) as (adapter, hardware_time):
            def read():
                return read_feedback(raw, hardware_time)

            wait_feedback(read, settings, since=time.perf_counter_ns(), require_follow=False, check_home=False,
                          check_stable=True, diagnostics=trace["waits"].setdefault("before_restore", {}))
            trace["restore_requested_at_ns"] = time.perf_counter_ns()
            dump(report_path, trace)
            send_mode(adapter, 0)
            trace["restore_write_returned"] = True
            wait_feedback(read, settings, since=trace["restore_requested_at_ns"], require_follow=True,
                          check_home=False, check_stable=True, diagnostics=trace["waits"].setdefault("after_restore", {}))
            trace["state"] = "drag_restore_sent_follower_stable"
        print("Restore sent and follower feedback is stable. Verify leader drag and following on site.", flush=True)
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
        trace.update(state="interrupted", error=repr(exc))
        raise
    finally:
        dump(report_path, trace)


def main():
    parser = argparse.ArgumentParser(description="Restore drag after an interrupted Home sequence.")
    parser.add_argument("--restore-drag", choices=("left", "right"), required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        restore_from_log(args.report, args.restore_drag)
    except KeyboardInterrupt:
        parser.exit(130, "Stopped. Check the arms on site.\n")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
