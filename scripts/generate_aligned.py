"""Generate provisional, causally aligned steps from one Piper X recording."""
import argparse
import bisect
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("j1", "j2", "j3", "j4", "j5", "j6", "gripper_width")
VIEWS = ("left_wrist", "right_wrist", "overview")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rows(path):
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig") as file:
        for number, line in enumerate(file, 1):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON") from exc


def relative(path, base):
    return os.path.relpath(path, base).replace(os.sep, "/")


def candidate(series, timestamp, max_age_ns):
    times, events = series
    index = bisect.bisect_right(times, timestamp) - 1
    if index < 0:
        return {"value": None, "valid": False, "reason": "no_prior_sample",
                "source_host_monotonic_ns": None, "age_ms": None, "source_index": None}
    event = events[index]
    age = timestamp - event["time"]
    valid = event["valid"] and age <= max_age_ns
    return {"value": event["value"] if valid else None, "valid": valid,
            "reason": None if valid else ("invalid_source" if not event["valid"] else "stale"),
            "source_host_monotonic_ns": event["time"], "age_ms": age / 1e6,
            "source_index": event["source_index"]}


def lookup_camera(series, timestamp, max_age_ns, output):
    times, events = series
    index = bisect.bisect_right(times, timestamp) - 1
    if index < 0:
        return {"path": None, "valid": False, "reason": "no_prior_frame",
                "source_host_monotonic_ns": None, "age_ms": None,
                "frame_number": None, "frame_index": None,
                "device_timestamp_ms": None, "timestamp_domain": None}
    event = events[index]
    age = timestamp - event["host_monotonic_ns"]
    valid = age <= max_age_ns
    return {"path": relative(event["path"], output) if valid else None,
            "valid": valid, "reason": None if valid else "stale",
            "source_host_monotonic_ns": event["host_monotonic_ns"], "age_ms": age / 1e6,
            "frame_number": event["frame_number"], "frame_index": event["index"],
            "device_timestamp_ms": event["device_timestamp_ms"],
            "timestamp_domain": event["timestamp_domain"]}


def ordered(events, key):
    events.sort(key=key)
    return [key(event) for event in events], events


def load_signals(episode, arms):
    result = {arm: {kind: {field: [] for field in FIELDS} for kind in ("state", "action")} for arm in arms}
    for arm in arms:
        path = episode / "can" / arm / "signals.jsonl"
        for row in rows(path):
            kind = row["kind"]
            if kind not in ("state", "action"):
                continue
            if len(row["fields"]) != len(row["values"]):
                raise ValueError(f"Field/value mismatch in {path}")
            for field, value in zip(row["fields"], row["values"]):
                if field not in FIELDS:
                    continue
                good = row["valid"] and isinstance(value, (int, float)) and math.isfinite(value)
                result[arm][kind][field].append({
                    "time": row["host_monotonic_ns"], "value": value if good else None,
                    "valid": good, "source_index": row["raw_index"]})
        for kind in ("state", "action"):
            for field in FIELDS:
                result[arm][kind][field] = ordered(result[arm][kind][field], lambda event: event["time"])
    return result


def load_cameras(episode, views):
    result = {}
    for view in views:
        folder = episode / "camera" / view
        events = []
        for row in rows(folder / "frames.jsonl"):
            if row.get("stream") != "rgb":
                continue
            name = row["file"]
            if Path(name).name != name:
                raise ValueError(f"Unsafe frame name: {name}")
            path = folder / name
            if not path.is_file():
                raise FileNotFoundError(path)
            row["path"] = path
            events.append(row)
        result[view] = ordered(events, lambda event: event["host_monotonic_ns"])
    return result


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def generate(episode, output_root, arms, views, fps, state_ms, action_ms, image_ms):
    metadata = read_json(episode / "metadata.json")
    if metadata.get("schema") != "piperx-episode/0.1":
        raise ValueError("Unsupported source schema")
    if metadata.get("status") != "closed":
        raise ValueError("Source episode is not closed")
    start, end = metadata["start_host_monotonic_ns"], metadata["stop_request_host_monotonic_ns"]
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        raise ValueError("Invalid episode time bounds")
    signals = load_signals(episode, arms)
    cameras = load_cameras(episode, views)
    quality_path = episode / "quality.json"
    quality = read_json(quality_path) if quality_path.exists() else {}
    export_id = str(uuid4())
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / export_id
    staging = output_root / (export_id + ".inprogress")
    staging.mkdir()
    stats = Counter()
    try:
        with (staging / "steps.jsonl").open("w", encoding="utf-8", buffering=1024*1024) as file:
            index = 0
            while True:
                timestamp = start + index * 1_000_000_000 // fps
                if timestamp >= end:
                    break
                image = {view: lookup_camera(cameras[view], timestamp, image_ms * 1e6, output)
                         for view in views}
                data = {kind: {arm: {
                    field: candidate(signals[arm][kind][field], timestamp,
                                     (state_ms if kind == "state" else action_ms) * 1e6)
                    for field in FIELDS} for arm in arms} for kind in ("state", "action")}
                state_valid = all(item["valid"] for arm in arms for item in data["state"][arm].values())
                action_valid = all(item["valid"] for arm in arms for item in data["action"][arm].values())
                image_valid = all(item["valid"] for item in image.values())
                valid = state_valid and action_valid and image_valid
                row = {"step_index": index, "timestamp_s": (timestamp-start)/1e9,
                       "host_monotonic_ns": timestamp, "images": image,
                       "state": data["state"], "action": data["action"],
                       "state_valid": state_valid, "action_valid": action_valid,
                       "images_valid": image_valid, "candidate_valid": valid}
                file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                stats.update({"steps": 1, "state_valid_steps": int(state_valid),
                              "action_valid_steps": int(action_valid),
                              "images_valid_steps": int(image_valid),
                              "candidate_valid_steps": int(valid)})
                for kind in ("state", "action"):
                    for arm in arms:
                        for field, item in data[kind][arm].items():
                            if not item["valid"]:
                                stats[f"{kind}.{arm}.{field}.{item['reason']}"] += 1
                for view, item in image.items():
                    if not item["valid"]:
                        stats[f"images.{view}.{item['reason']}"] += 1
                index += 1
        info = {
            "schema": "piperx-aligned-candidate/0.1", "export_id": export_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_episode_id": metadata["episode_id"], "source_session_id": metadata["session_id"],
            "source_episode_relative_path": relative(episode, output),
            "source_manifest_sha256": sha256(episode/"manifest.json") if (episode/"manifest.json").exists() else None,
            "source_quality_status": quality.get("status"), "source_simulated": metadata.get("simulated"),
            "task": metadata.get("task"), "result": metadata.get("result"),
            "trajectory_usability": metadata.get("trajectory_usability", "unreviewed"),
            "arms": arms, "fields_per_arm": list(FIELDS), "views": views,
            "units": {"joints": "rad", "gripper_width": "m"}, "fps": fps,
            "threshold_ms": {"state": state_ms, "action": action_ms, "image": image_ms},
            "sampling": "latest host arrival at or before step; invalid events override previous values",
            "action_semantics": "latest observed teleoperation target; temporal alignment and hold behavior unverified",
            "warning": "PROVISIONAL: host arrival is not exposure or execution time; thresholds require validation",
            "training_ready": False,
            "generator_sha256": sha256(Path(__file__)), "stats": dict(stats),
            "images": "relative references to original JPEG files, preserve directory structure when transferring",
        }
        info.update({key: metadata[key] for key in ("task_id", "collection_session", "depth", "host_clock")
                     if key in metadata})
        (staging / "metadata.json").write_text(json.dumps(info, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        staging.rename(output)
    except Exception:
        # Retain the staging directory for diagnosis.
        raise
    print(json.dumps({"output": str(output), "steps": stats["steps"],
                      "valid": stats["candidate_valid_steps"], "training_ready": False}, ensure_ascii=False))
    return output


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--arms", choices=("left", "right", "left,right"), required=True)
    parser.add_argument("--views", default="left_wrist,right_wrist,overview")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--state-max-age-ms", type=positive, default=50)
    parser.add_argument("--action-max-age-ms", type=positive, default=50)
    parser.add_argument("--image-max-age-ms", type=positive, default=100)
    parser.add_argument("--output-root", type=Path, default=ROOT / "data" / "aligned_candidates")
    args = parser.parse_args()
    views = args.views.split(",")
    if not views or len(set(views)) != len(views) or any(view not in VIEWS for view in views):
        parser.error("views must be a comma-separated selection of left_wrist,right_wrist,overview")
    if not 1 <= args.fps <= 200:
        parser.error("fps must be between 1 and 200")
    episode, root = args.episode.resolve(), args.output_root.resolve()
    if root == episode or root.is_relative_to(episode):
        parser.error("Output must be outside source episode")
    generate(episode, root, args.arms.split(","), views, args.fps,
             args.state_max_age_ms, args.action_max_age_ms, args.image_max_age_ms)


if __name__ == "__main__":
    main()
