"""Record, prepare, upload, and export Piper X demonstration datasets."""
import argparse
from contextlib import nullcontext
from datetime import datetime
import math
from pathlib import Path
import re
import sys

import compare_last_target
import generate_aligned
import home_reset
import package_aligned
import record_episode
import review_episode
import sync_server

ROOT = record_episode.ROOT
MODES = {"left": ["left"], "right": ["right"], "both": ["left", "right"]}


def slug(value):
    if "--" in value or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value):
        raise argparse.ArgumentTypeError("Use 1–32 lowercase ASCII letters, digits, underscores or single hyphens.")
    return value


def selected_views(value):
    views = value.split(",")
    if len(set(views)) != len(views) or any(view not in record_episode.VIEWS for view in views):
        raise argparse.ArgumentTypeError("Select from left_wrist,right_wrist,overview (comma-separated).")
    return [view for view in record_episode.VIEWS if view in views]


def within(path, *parents):
    path = path.resolve(strict=True)
    if not any(path.is_relative_to(parent.resolve()) for parent in parents):
        raise ValueError(f"Input is outside project data directories: {path}")
    return path


def upload(package, destination):
    base = ROOT / "data" / "training_packages"
    package = within(package, base)
    info = record_episode.load(package / "metadata.json")
    schema = info.get("schema")
    supported = schema == package_aligned.ARCHIVE_SCHEMA or (
        schema == package_aligned.ALIGNED_SCHEMA and "last_target_policy" in info
    )
    if package.name.endswith(".inprogress") or info.get("source_simulated") is not False or not supported:
        raise ValueError("Upload requires a completed, real aligned or raw archive package.")
    sync_server.verify_episode(package)
    group = "data/training_packages/" + package.relative_to(base).as_posix()
    files = {}
    sync_server.add_tree(files, package, group)
    sync_server.send(files, group, destination)


def prepare(episode, args):
    raw = record_episode.load(episode / "metadata.json")
    quality = record_episode.load(episode / "quality.json")
    if raw["status"] != "closed" or raw["result"] == "aborted":
        raise ValueError(f"Recording is incomplete or aborted; raw files are available at {episode}")
    if quality["status"] not in {"passed", "warning", "failed"}:
        raise ValueError(f"Unknown recording quality status: {episode}")
    task_id = raw.get("task_id") or getattr(args, "task_id", None)
    if task_id is None:
        raise ValueError("Specify --task-id for a recording made with the legacy recorder.")
    if getattr(args, "task_id", None) not in (None, task_id):
        raise ValueError("Requested task ID conflicts with the recording metadata.")
    mode = next((key for key, arms in MODES.items() if raw["active_arms"] == arms), None)
    if mode is None:
        raise ValueError("An active arm selection is required.")
    session = raw.get("collection_session") or raw["session_id"].lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", session):
        raise ValueError("Invalid source session identifier.")
    views = args.views
    sync_server.verify_episode(episode)
    name_prefix = f"{task_id}--{mode}--{session}"
    usability = getattr(args, "usability", None)
    archive_reason = None
    if quality["status"] == "failed":
        archive_reason = "recording_quality_failed"
    elif not (raw.get("operator_confirmed_follow") or getattr(args, "confirm_follow", False)):
        archive_reason = "follow_mode_unconfirmed"
    else:
        if not set(views).issubset(raw["recorded_views"]):
            raise ValueError("One or more requested export cameras were not recorded.")
        config = record_episode.load(episode / "config_snapshot" / "capture_v0_1.json")
        aligned = generate_aligned.generate(
            episode, ROOT / "data" / "aligned_candidates", MODES[mode], views, config["fps"], 50, 50, 100
        )
        if "task_id" not in raw:
            meta_path = aligned / "metadata.json"
            aligned_info = record_episode.load(meta_path)
            aligned_info.update(task_id=task_id, collection_session=session)
            record_episode.dump(meta_path, aligned_info)
        candidate = compare_last_target.run(aligned, None, None, True)
        if not record_episode.load(candidate / "metadata.json")["stats"]["candidate_valid_steps"]:
            archive_reason = "no_usable_causal_training_steps"
    if archive_reason:
        package = package_aligned.archive_raw(
            episode, raw, quality, name_prefix=name_prefix, task_id=task_id,
            arms=MODES[mode], reason=archive_reason, usability=usability,
        )
    else:
        package = package_aligned.generate(candidate, name_prefix=name_prefix, usability=usability)
    print(f"PACKAGE: {package}", flush=True)
    if args.review:
        review = ROOT / "data" / "reviews" / (raw["episode_id"] + ".html")
        try:
            review_episode.build(episode, review)
            print(f"REVIEW: {review}", flush=True)
        except (OSError, ValueError) as exc:
            print(f"REVIEW WARNING: {exc}", file=sys.stderr, flush=True)
    if args.destination and not raw.get("simulated"):
        upload(package, args.destination)
    return package


def capture(args):
    if not args.instruction.strip():
        raise ValueError("Task instruction must not be blank.")
    if not args.simulate and (sys.platform != "win32" or not sys.stdin.isatty()):
        raise ValueError("Real capture requires an interactive Windows terminal.")
    if args.simulate and args.resume_home:
        raise ValueError("Cannot resume a real Home log in simulation.")
    if not args.simulate and not args.enable_homing and not args.resume_home:
        raise ValueError("Real capture moves the selected arms. Pass --enable-homing to arm this workflow.")
    if args.simulate:
        args.seconds = 2.0 if args.seconds is None else args.seconds
        if not math.isfinite(args.seconds) or args.seconds <= 0:
            raise ValueError("--seconds must be finite and positive.")
    elif args.seconds is not None:
        raise ValueError("--seconds is only for --simulate; start real recordings manually.")
    settings = argparse.Namespace(
        session=f"{args.task_id}--{args.arms}--{args.session}",
        collection_session=args.session, task_id=args.task_id, arms=",".join(MODES[args.arms]),
        task=args.instruction.strip(), depth=args.depth, seconds=args.seconds,
        result="success" if args.simulate else "unlabeled", simulate=args.simulate,
        allow_missing_optional=False, confirm_follow=True, label_usability=True,
    )
    while True:
        connection = nullcontext(None) if args.simulate else record_episode.connected_capture_devices()
        with connection as connected:
            if args.resume_home:
                settings.home_reset = home_reset.resume_from_log(args.resume_home, MODES[args.arms])
            else:
                adapters = connected[0] if connected is not None else None
                settings.home_reset = home_reset.reset_selected(MODES[args.arms], simulated=args.simulate,
                                                                adapters=adapters)
            try:
                episode, quality = record_episode.record(settings, connected=connected)
            except (OSError, RuntimeError, ValueError) as exc:
                home_log = settings.home_reset.get("log_file")
                if home_log:
                    path = ROOT / "logs" / home_log
                    report = record_episode.load(path)
                    report["last_capture_setup_error"] = repr(exc)
                    record_episode.dump(path, report)
                    print(f"Capture setup stopped after Home. Report: {path}", flush=True)
                raise
        if quality == "incomplete":
            cause = record_episode.load(episode / "quality.json").get("capture_error") or "see quality.json"
            raise ValueError(f"Recording incomplete: {cause}. Raw files: {episode}")
        package = prepare(episode, args)
        if quality == "failed":
            raise ValueError(f"Recording quality failed. Review the archive before continuing: {package}")
        if args.once or args.resume_home or not sys.stdin.isatty() or not collect_another():
            return


def collect_another():
    while True:
        try:
            answer = input("Collect another episode with the same settings? [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Enter y to continue or n to finish.", flush=True)


def prepare_options(parser):
    parser.add_argument("--views", type=selected_views, default=list(record_episode.VIEWS),
                        help="RGB views included in the aligned package; raw capture keeps all three.")
    parser.add_argument("--review", action="store_true", help="Also build the offline review page.")
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument("--upload", action="store_true", help="Upload using local configs/upload.json.")
    transfer.add_argument("--no-upload", action="store_true", help="Finish after local packaging (default).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="Collect episodes with the same settings.")
    record.add_argument("--task-id", type=slug, default="cube_to_plate")
    record.add_argument("--instruction", default="把方块放到盘子里",
                        help="Natural language instruction stored in LeRobot.")
    record.add_argument("--arms", choices=MODES, required=True)
    record.add_argument("--session", type=slug, default=datetime.now().strftime("%Y%m%d"))
    record.add_argument("--depth", action="store_true", help="Also save raw depth on the laptop.")
    record.add_argument("--enable-homing", action="store_true", help="Enable selective Home motion on real hardware.")
    record.add_argument("--resume-home", type=Path,
                        help="Record one episode from a completed Home report without issuing a motion command.")
    record.add_argument("--simulate", action="store_true", help="Test locally without hardware or upload.")
    record.add_argument("--seconds", type=float, help="Duration for simulation only.")
    record.add_argument("--once", action="store_true", help="Finish after one episode without asking to continue.")
    prepare_options(record)

    offline = commands.add_parser("prepare", help="Align or archive a completed raw episode.")
    offline.add_argument("episode", type=Path)
    offline.add_argument("--task-id", type=slug, help="Required for a legacy recording without a task ID.")
    offline.add_argument("--confirm-follow", action="store_true", help="Attest that this episode maintained follow mode.")
    offline.add_argument("--usability", choices=record_episode.TRAJECTORY_USABILITIES,
                         help="Set the reviewed trajectory label in the new package; preserve the raw recording.")
    prepare_options(offline)

    retry = commands.add_parser("upload", help="Retry uploading an existing completed episode package.")
    retry.add_argument("package", type=Path)

    export = commands.add_parser("export-lerobot", help="Export compatible packages marked usable using the official SDK.")
    export.add_argument("packages", nargs="+", type=Path)
    export.add_argument("--name", type=slug, required=True, help="Version label for this dataset, e.g. v01.")
    export.add_argument("--include-result", action="append", choices=("success", "failure", "unlabeled"),
                        help="Repeat to select task outcomes. Default: success only.")
    export.add_argument("--allow-warning", action="store_true", help="Include integrity warnings after reviewing them.")
    export.add_argument("--allow-simulated", action="store_true", help="Export a separate synthetic-only test dataset.")
    export.add_argument("--data-root", type=Path, default=ROOT / "data",
                        help="Data directory containing training_packages and receiving the exported dataset.")
    args = parser.parse_args()
    try:
        if getattr(args, "upload", False) and getattr(args, "simulate", False):
            raise ValueError("Simulated recording cannot be uploaded.")
        args.destination = (sync_server.load_destination() if args.command == "upload"
                            or getattr(args, "upload", False) else None)
        if args.command == "record":
            capture(args)
        elif args.command == "prepare":
            episode = within(args.episode, ROOT / "data" / "raw", ROOT / "data" / "recorder_selftest")
            prepare(episode, args)
        elif args.command == "upload":
            upload(args.package, args.destination)
        else:
            from export_lerobot import export_dataset

            export_dataset(args)
    except KeyboardInterrupt:
        parser.exit(130, "\nStopped. Review the paths printed above for any unfinished work.\n")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
