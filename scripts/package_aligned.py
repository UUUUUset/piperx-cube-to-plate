"""Package aligned JPEG observations and robot signals into a portable provisional episode."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from record_episode import TRAJECTORY_USABILITIES
from sync_server import verify_episode

ROOT = Path(__file__).resolve().parents[1]
ALIGNED_SCHEMA = "piperx-self-contained-candidate/0.1"
ARCHIVE_SCHEMA = "piperx-raw-archive/0.1"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def usability_metadata(raw, reviewed_label):
    source = raw.get("trajectory_usability", "unreviewed")
    chosen = source if reviewed_label is None else reviewed_label
    if source not in TRAJECTORY_USABILITIES or chosen not in TRAJECTORY_USABILITIES:
        raise ValueError("Invalid trajectory usability label")
    return {
        "trajectory_usability": chosen, "source_trajectory_usability": source,
        "trajectory_usability_source": "source_recording" if reviewed_label is None else "prepare_review",
    }


def publish(stage, final, metadata):
    (stage / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    files = {}
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Package cannot contain a symbolic link: {path}")
        if path.is_file():
            files[path.relative_to(stage).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    manifest = {"algorithm": "sha256", "files": files}
    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stage.rename(final)


def archive_raw(episode, raw, quality, *, name_prefix, task_id, arms, reason, usability=None):
    uid = str(uuid4())
    final = ROOT / "data" / "training_packages" / f"{name_prefix}--archive--{uid}"
    stage = final.with_name(final.name + ".inprogress")
    stage.mkdir(parents=True)
    shutil.copytree(episode, stage / "raw", symlinks=True)
    verify_episode(stage / "raw")
    source_signature = digest(episode / "manifest.json")
    if digest(stage / "raw" / "manifest.json") != source_signature:
        raise ValueError("Source manifest differs from archived manifest")
    metadata = {
        "schema": ARCHIVE_SCHEMA, "package_id": uid, "archive_reason": reason,
        "created_utc": datetime.now(timezone.utc).isoformat(), "training_ready": False,
        "source_episode_id": raw["episode_id"], "source_session_id": raw["session_id"],
        "source_manifest_sha256": source_signature, "source_quality_status": quality["status"],
        "source_simulated": raw["simulated"], "task_id": task_id, "task": raw.get("task"),
        "arms": arms, "result": raw.get("result"), "raw_episode_path": "raw",
        **usability_metadata(raw, usability),
    }
    publish(stage, final, metadata)
    print(f"ARCHIVE: {reason} | trajectory_usability={metadata['trajectory_usability']}", flush=True)
    return final


def generate(aligned, result=None, name_prefix=None, usability=None):
    begin = time.perf_counter()
    metadata = json.loads((aligned / "metadata.json").read_text(encoding="utf-8-sig"))
    if metadata["schema"] != "piperx-aligned-candidate/0.1":
        raise ValueError("Unsupported aligned data schema")
    episode = (aligned / metadata["source_episode_relative_path"]).resolve()
    raw = json.loads((episode / "metadata.json").read_text(encoding="utf-8-sig"))
    if raw["episode_id"] != metadata["source_episode_id"]:
        raise ValueError("Source episode ID mismatch")
    usability_fields = usability_metadata(raw, usability)
    original_manifest = json.loads((episode / "manifest.json").read_text(encoding="utf-8-sig"))
    if digest(episode / "manifest.json") != metadata["source_manifest_sha256"]:
        raise ValueError("Source episode manifest differs from aligned metadata")
    uid = str(uuid4())
    base = ROOT / "data" / "training_packages"
    base.mkdir(parents=True, exist_ok=True)
    name = f"{name_prefix}--{uid}" if name_prefix else uid
    stage = base / (name + ".inprogress")
    stage.mkdir()
    copies = {}
    counts = {"steps": 0, "candidate_valid_steps": 0, "image_references": 0}
    try:
        with (aligned / "steps.jsonl").open(encoding="utf-8-sig") as source, \
                (stage / "steps.jsonl").open("w", encoding="utf-8", buffering=1024*1024) as dest:
            for line in source:
                row = json.loads(line)
                if row["step_index"] != counts["steps"]:
                    raise ValueError("Step indices are not contiguous")
                for view, item in row["images"].items():
                    if not item["valid"]:
                        if item["path"] is not None:
                            raise ValueError("Invalid image unexpectedly has a path")
                        continue
                    path = (aligned / item["path"]).resolve()
                    allowed = (episode / "camera" / view).resolve()
                    if not path.is_relative_to(allowed) or path.suffix.lower() != ".jpg":
                        raise ValueError(f"Image outside source view: {path}")
                    if path not in copies:
                        source_key = path.relative_to(episode).as_posix()
                        expected = original_manifest["files"].get(source_key)
                        if not expected or path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
                            raise ValueError(f"Source image checksum mismatch: {path}")
                        target = Path("images") / view / path.name
                        destination = stage / target
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
                        if digest(destination) != expected["sha256"]:
                            raise ValueError(f"Copy checksum mismatch: {path}")
                        copies[path] = target.as_posix()
                    item["path"] = copies[path]
                    counts["image_references"] += 1
                dest.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                counts["steps"] += 1
                counts["candidate_valid_steps"] += int(row["candidate_valid"])
        if counts["steps"] != metadata["stats"]["steps"]:
            raise ValueError("Step count differs from aligned metadata")
        shutil.copytree(episode / "config_snapshot", stage / "config_snapshot")
        shutil.copyfile(episode / "quality.json", stage / "source_quality.json")
        counts["unique_jpeg_files"] = len(copies)
        package_meta = dict(metadata)
        package_meta.update({
            "schema": ALIGNED_SCHEMA, "package_id": uid,
            "source_aligned_id": metadata["export_id"], "source_aligned_metadata_sha256": digest(aligned / "metadata.json"),
            "source_steps_sha256": digest(aligned / "steps.jsonl"),
            "source_result": raw.get("result"), "result": result if result else metadata.get("result"),
            **usability_fields,
            "result_override_reason": "operator clarified result is not a task outcome" if result == "unlabeled" and raw.get("result") != "unlabeled" else None,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "images": "JPEG byte copies within this directory; image paths in steps.jsonl are package-relative",
            "image_recompression": False, "training_ready": False, "package_stats": counts,
            "packager_sha256": digest(Path(__file__)),
        })
        package_meta.pop("source_episode_relative_path", None)
        final = base / name
        publish(stage, final, package_meta)
        elapsed = time.perf_counter() - begin
        size = sum(path.stat().st_size for path in final.rglob("*") if path.is_file())
        print(json.dumps({"output": str(final), "processing_seconds": elapsed, "bytes": size,
                          **counts, "trajectory_usability": usability_fields["trajectory_usability"],
                          "training_ready": False}, ensure_ascii=False), flush=True)
        return final
    except Exception:
        # Keep incomplete data for inspection.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aligned", type=Path)
    parser.add_argument("--result", choices=("success", "failure", "aborted", "unlabeled"),
                        help="Explicit result correction; source result is preserved in metadata")
    args = parser.parse_args()
    aligned = args.aligned.resolve()
    aligned_root = (ROOT / "data" / "aligned_candidates").resolve()
    if not aligned.is_relative_to(aligned_root):
        parser.error("Aligned candidate must be under data/aligned_candidates")
    generate(aligned, args.result)


if __name__ == "__main__":
    main()
