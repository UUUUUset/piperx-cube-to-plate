"""Write verified Piper X episode packages as an official LeRobot v3 dataset."""
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
import json
import math
from pathlib import Path
import re
import sys
from uuid import uuid4

from generate_aligned import FIELDS, VIEWS
from package_aligned import ALIGNED_SCHEMA, ARCHIVE_SCHEMA
from record_episode import TRAJECTORY_USABILITIES
from sync_server import digest, verify_episode

LEROBOT_VERSION = "0.4.4"
INDEX_COLUMNS = {"index", "episode_index", "frame_index", "timestamp", "task_index"}
ARMS_TO_MODE = {("left",): "left", ("right",): "right", ("left", "right"): "both"}
ACTION_SOURCES = {"observed_target", "held_last_target", "verified_home_initial"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_package(path, args):
    path = path.resolve(strict=True)
    package_root = (args.data_root / "training_packages").resolve()
    if not path.is_relative_to(package_root) or path.name.endswith(".inprogress"):
        raise ValueError(f"Expected a completed package under {package_root}: {path}")
    verify_episode(path)
    meta = read(path / "metadata.json")
    schema = meta.get("schema")
    if schema != ARCHIVE_SCHEMA and (schema != ALIGNED_SCHEMA or "last_target_policy" not in meta):
        raise ValueError(f"Expected an aligned Piper X package or raw archive: {path}")
    if meta.get("source_simulated") is not args.allow_simulated:
        raise ValueError("Real and simulated inputs must be exported separately. Use --allow-simulated for tests.")
    usability = meta.setdefault("trajectory_usability", "unreviewed")
    if usability not in TRAJECTORY_USABILITIES:
        raise ValueError(f"Invalid trajectory usability label in {path.name}")
    if schema == ARCHIVE_SCHEMA or usability != "usable":
        return path, meta, None
    if meta["result"] not in (args.include_result or ["success"]):
        raise ValueError(f"Result {meta['result']!r} excluded for {path.name}. Use --include-result to opt in.")
    quality = {"passed", "warning"} if args.allow_warning else {"passed"}
    if meta["source_quality_status"] not in quality:
        raise ValueError(f"Recording quality {meta['source_quality_status']!r} excluded for {path.name}.")
    task_id = meta.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", task_id):
        raise ValueError(f"Package has no valid task ID. Rebuild with the prepare command: {path.name}")
    if not isinstance(meta.get("task"), str) or not meta["task"].strip():
        raise ValueError(f"Package has no task instruction: {path.name}")
    config = read(path / "config_snapshot" / "capture_v0_1.json")
    for key in ("width", "height"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"Invalid image {key} in {path.name}")
    return path, meta, config


def step_rows(path):
    with (path / "steps.jsonl").open(encoding="utf-8-sig") as file:
        for line in file:
            yield json.loads(line)


def contiguous_rows(path, meta, steps):
    arms, views = meta["arms"], meta["views"]
    start, end, count = None, None, 0
    for i, row in enumerate(steps):
        count += 1
        if row["step_index"] != i:
            raise ValueError(f"Out of order source step in {path.name} at {i}")
        if (not row["candidate_valid"] or row.get("action_uses_future_target", False)
                or any(row["action"][side][field]["action_source"] not in ACTION_SOURCES
                       for side in arms for field in FIELDS)):
            continue
        if start is None:
            start = i
        elif i != end:
            raise ValueError(f"Internal gap before step {i} in {path.name}; review it before export.")
        end = i + 1
        if abs(row["timestamp_s"] - i / meta["fps"]) > 1e-6:
            raise ValueError(f"Unexpected step spacing in {path.name} at {i}")
        if any(not row["images"][view]["valid"] for view in views):
            raise ValueError(f"Invalid image in {path.name} at {i}")
        for kind in ("state", "action"):
            for side in arms:
                for field in FIELDS:
                    item = row[kind][side][field]
                    if not item["valid"] or not isinstance(item["value"], (int, float)) or not math.isfinite(item["value"]):
                        raise ValueError(f"Invalid {kind} {side}.{field} in {path.name} at {i}")
    if count != meta["stats"]["steps"]:
        raise ValueError(f"Source step count differs in {path.name}")
    if start is None:
        raise ValueError(f"No usable training steps in {path.name}")
    return start, end


def vector(row, kind, arms, numpy):
    return numpy.array([row[kind][side][field]["value"] for side in arms for field in FIELDS],
                       dtype=numpy.float32)


def camera_image(path, view, row, size, image, numpy):
    file = (path / row["images"][view]["path"]).resolve(strict=True)
    if not file.is_relative_to(path / "images" / view):
        raise ValueError(f"Image escapes its package view: {file}")
    with image.open(file) as decoded:
        if decoded.size != size or decoded.mode != "RGB":
            raise ValueError(f"Unexpected image size or mode: {file}")
        return numpy.array(decoded)


def export_dataset(args):
    try:
        installed = version("lerobot")
    except PackageNotFoundError as exc:
        raise ValueError(f"Install lerobot=={LEROBOT_VERSION} in a dedicated export environment.") from exc
    if installed != LEROBOT_VERSION:
        raise ValueError(f"Use lerobot=={LEROBOT_VERSION}; found {installed}.")
    import numpy
    from PIL import Image
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    packages, skipped = [], []
    seen = set()
    for entry in args.packages:
        path, meta, config = load_package(entry, args)
        episode_id = meta["source_episode_id"]
        if episode_id in seen:
            raise ValueError(f"Duplicate source episode: {episode_id}. Choose one reviewed package for each episode.")
        seen.add(episode_id)
        if config is None:
            reason = meta.get("archive_reason") if meta["schema"] == ARCHIVE_SCHEMA else "trajectory_usability"
            skipped.append({
                "episode_id": episode_id, "package": path.relative_to(args.data_root.resolve()).as_posix(),
                "trajectory_usability": meta["trajectory_usability"], "reason": reason,
            })
            print(f"SKIPPED: {path.name} | {reason} | trajectory_usability={meta['trajectory_usability']}", flush=True)
        else:
            packages.append((path, meta, config))
    if not packages:
        raise ValueError("No aligned trajectories marked usable. Review the source recordings before exporting.")
    _, first, config = packages[0]
    task_id, task = first["task_id"], first["task"]
    arms, views, fps = first["arms"], first["views"], first["fps"]
    size = config["width"], config["height"]
    mode = ARMS_TO_MODE.get(tuple(arms))
    if (mode is None or type(fps) is not int or fps <= 0 or not views
            or len(set(views)) != len(views) or any(view not in VIEWS for view in views)
            or first["fields_per_arm"] != list(FIELDS)
            or first["units"] != {"joints": "rad", "gripper_width": "m"}):
        raise ValueError("Invalid arm, view, timing or vector schema.")
    spans = []
    for path, meta, config in packages:
        if any(meta[key] != first[key] for key in (
            "task_id", "task", "arms", "views", "fps", "fields_per_arm", "units", "threshold_ms"
        )) or (config["width"], config["height"]) != size or meta.get("host_clock") != first.get("host_clock"):
            raise ValueError(f"Task or capture schema differs: {path.name}. Export it as a separate dataset.")
        spans.append(contiguous_rows(path, meta, step_rows(path)))

    vector_names = [f"{side}_{field}" for side in arms for field in FIELDS]
    features = {
        name: {"dtype": "float32", "shape": (len(vector_names),), "names": vector_names}
        for name in ("observation.state", "action")
    }
    features.update({
        f"observation.images.{view}": {"dtype": "image", "shape": (size[1], size[0], 3),
                                       "names": ["height", "width", "channels"]}
        for view in views
    })

    base = args.data_root.resolve() / ("lerobot_selftest" if args.allow_simulated else "lerobot")
    base.mkdir(parents=True, exist_ok=True)
    name = f"{task_id}--{mode}--{args.name}"
    final = base / name
    if final.exists():
        raise ValueError(f"Dataset already exists: {final}. Choose a new --name.")
    stage = base / f".{name}.{uuid4().hex[:8]}.inprogress"
    repo_id = "local/" + re.sub(r"-+", "-", name).strip("-")
    dataset = LeRobotDataset.create(repo_id=repo_id, root=stage, fps=fps, robot_type="piperx",
                                    features=features, use_videos=False)
    sidecar = stage / "piperx"
    sidecar.mkdir()
    episode_records = []
    frame_count = 0
    try:
        with (sidecar / "rows.jsonl").open("w", encoding="utf-8") as trace:
            for ep_index, ((path, meta, _), (start, end)) in enumerate(zip(packages, spans, strict=True)):
                episode_records.append({
                    "episode_index": ep_index, "episode_id": meta["source_episode_id"],
                    "session": meta["source_session_id"], "result": meta["result"],
                    "recording_quality": meta["source_quality_status"],
                    "trajectory_usability": meta["trajectory_usability"],
                    "trajectory_usability_source": meta.get("trajectory_usability_source"),
                    "source_trajectory_usability": meta.get("source_trajectory_usability"),
                    "package": path.relative_to(args.data_root.resolve()).as_posix(),
                    "package_manifest_sha256": digest(path / "manifest.json"),
                    "source_manifest_sha256": meta.get("source_manifest_sha256"),
                    "first_source_step": start, "end_source_step_exclusive": end,
                    "trimmed_prefix": start, "trimmed_suffix": meta["stats"]["steps"] - end,
                    "length": end - start,
                })
                for row in islice(step_rows(path), start, end):
                    index = row["step_index"]
                    frame = {
                        "task": task, "observation.state": vector(row, "state", arms, numpy),
                        "action": vector(row, "action", arms, numpy),
                    }
                    for view in views:
                        frame[f"observation.images.{view}"] = camera_image(path, view, row, size, Image, numpy)
                    dataset.add_frame(frame)
                    trace.write(json.dumps({
                        "index": frame_count, "episode_index": ep_index, "frame_index": index - start,
                        "source_step": index, "source_timestamp_s": row["timestamp_s"],
                        "source_host_monotonic_ns": row["host_monotonic_ns"],
                        "action_source": {side: {f: row["action"][side][f]["action_source"] for f in FIELDS}
                                          for side in arms},
                    }, allow_nan=False) + "\n")
                    frame_count += 1
                dataset.save_episode()
    finally:
        dataset.finalize()

    reopened = LeRobotDataset(repo_id, root=stage)
    expected = INDEX_COLUMNS | set(features)
    if (len(reopened) != frame_count or reopened.meta.total_episodes != len(packages)
            or reopened.meta.info["codebase_version"] != "v3.0"
            or not expected.issubset(reopened.meta.features)):
        raise ValueError(f"LeRobot validation failed: {stage}")
    offset = 0
    for episode in episode_records:
        length = episode["length"]
        for frame_index in (0, length - 1):
            row = reopened[offset + frame_index]
            if (row["task"] != task or int(row["index"]) != offset + frame_index
                    or int(row["episode_index"]) != episode["episode_index"]
                    or int(row["frame_index"]) != frame_index or int(row["task_index"]) != 0
                    or abs(float(row["timestamp"]) - frame_index / fps) > 1e-4
                    or tuple(row["observation.state"].shape) != (len(vector_names),)
                    or tuple(row["action"].shape) != (len(vector_names),)
                    or any(tuple(row[f"observation.images.{view}"].shape) != (3, size[1], size[0])
                           for view in views)):
                raise ValueError(f"LeRobot round-trip inspection failed: {stage}")
        offset += length
    del reopened

    (sidecar / "provenance.json").write_text(json.dumps({
        "format": "lerobot/v3.0", "lerobot_version": installed, "exporter_sha256": digest(Path(__file__)),
        "task_id": task_id, "task": task, "fps": fps, "arms": arms, "views": views,
        "units": first["units"], "threshold_ms": first["threshold_ms"], "vector_fields": vector_names,
        "host_clock": first.get("host_clock"),
        "simulated": args.allow_simulated, "episodes": episode_records, "skipped_episodes": skipped,
        "python_version": sys.version.split()[0],
        "dependencies": {name: version(name) for name in ("numpy", "torch", "torchvision", "pyarrow", "datasets", "Pillow")},
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    stage.rename(final)
    print(f"LEROBOT: {final} | {len(packages)} episodes | {frame_count} frames", flush=True)
    return final
