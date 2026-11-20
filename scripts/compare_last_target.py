"""Generate last-target actions with separately marked first-target backfill."""
import argparse
import bisect
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
from uuid import uuid4

from home_reset import HOME, JOINTS, SCHEMA as HOME_SCHEMA
from record_episode import FOLLOWER_STATUS, follower_status_ok

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("j1", "j2", "j3", "j4", "j5", "j6", "gripper_width")
SIDES = ("left", "right")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read_frames(path):
    statuses, faults = [], []
    for line in path.open(encoding="utf-8-sig"):
        frame = json.loads(line)
        time = frame["host_monotonic_ns"]
        if frame["error"]:
            faults.append(time)
        elif frame["id"] == FOLLOWER_STATUS and not frame["extended"] and not frame["remote"]:
            data = bytes.fromhex(frame["data"])
            if len(data) == 8:
                ok = follower_status_ok(frame)
                statuses.append((time, ok))
                if not ok:
                    faults.append(time)
    statuses.sort()
    faults.sort()
    return [x[0] for x in statuses], statuses, faults


def targets(path):
    data, first = {}, {}
    with path.open(encoding="utf-8-sig") as file:
        for line in file:
            row = json.loads(line)
            if row["kind"] != "action":
                continue
            for field, value in zip(row["fields"], row["values"]):
                time, index = row["host_monotonic_ns"], row["raw_index"]
                sample = (value, row["valid"], time)
                data[(index, field)] = sample
                current = first.get(field)
                if current is None or (time, index) < (current[0][2], current[1]):
                    first[field] = (sample, index)
    return data, first


def feedback_health(row, side, health):
    time = row["host_monotonic_ns"]
    status_times, statuses, _ = health[side]
    index = bisect.bisect_right(status_times, time) - 1
    status = statuses[index] if index >= 0 else None
    status_valid = status is not None and status[1] and time - status[0] <= 50_000_000
    state_valid = all(row["state"][side][field]["valid"] for field in FIELDS)
    return status, status_valid, state_valid


def initial_health_limits(aligned, health):
    """Find the first observed fault or missing feedback on each side."""
    limits = {side: frames[2][0] for side, frames in health.items() if frames[2]}
    with aligned.open(encoding="utf-8-sig") as source:
        for line in source:
            row = json.loads(line)
            time = row["host_monotonic_ns"]
            for side in health:
                if side in limits and limits[side] <= time:
                    continue
                _, status_valid, state_valid = feedback_health(row, side, health)
                if not (status_valid and state_valid):
                    limits[side] = time
            if len(limits) == len(health) and max(limits.values()) <= time:
                break
    return limits


def percentile(values, p):
    values = sorted(values)
    return values[math.ceil(p*len(values))-1] if values else None


def run(original, baseline, report_dir, follow_confirmed):
    if not follow_confirmed:
        raise ValueError("Follow-mode confirmation for this episode is required")
    meta = load(original / "metadata.json")
    if meta.get("schema") != "piperx-aligned-candidate/0.1" or any(
        flag in meta for flag in ("last_target_policy", "hold_augmentation")
    ):
        raise ValueError("Expected original aligned candidate")
    arms = meta["arms"]
    if not arms or len(set(arms)) != len(arms) or any(side not in SIDES for side in arms):
        raise ValueError("Invalid arm selection")
    baseline = baseline or original
    old = load(baseline / "metadata.json")
    if baseline != original and old.get("hold_augmentation", {}).get("source_aligned_id") != meta["export_id"]:
        raise ValueError("Baseline does not represent the source episode")
    if report_dir:
        report = load(report_dir / "summary.json")
        if report["aligned_id"] != meta["export_id"]:
            raise ValueError("Hold analysis differs from aligned input")
    episode = (original / meta["source_episode_relative_path"]).resolve()
    source = load(episode / "metadata.json")
    if source["episode_id"] != meta["source_episode_id"] or source["status"] != "closed":
        raise ValueError("Source mismatch or not closed")
    home = source.get("home_reset")
    start = source["start_host_monotonic_ns"]
    home_sides = {}
    if home and home.get("verified_at_start"):
        home_sides = home["sides"]
        if (home.get("schema") != HOME_SCHEMA or home.get("start_host_monotonic_ns") != start
                or not set(arms).issubset(home_sides)
                or any(home_sides[side]["joint_target_rad"] != HOME or
                       home_sides[side]["at_recording_start"]["verified_host_monotonic_ns"] != start or
                       (not source.get("simulated") and not home_sides[side].get("operator_verified_leader_home"))
                       for side in arms)):
            raise ValueError("Verified Home metadata disagrees with the recording")
    health = {side: read_frames(episode / "can" / side / "frames.jsonl") for side in arms}
    observed = {side: targets(episode / "can" / side / "signals.jsonl") for side in arms}
    health_limits = initial_health_limits(original / "steps.jsonl", health)
    communication_breaks = {side: None for side in arms}
    counts = Counter()
    joint_differences = {side: [] for side in arms}
    gripper_differences = {side: [] for side in arms}
    uid = str(uuid4())
    root = ROOT / "data" / "aligned_candidates"
    stage, final = root / (uid + ".inprogress"), root / uid
    stage.mkdir()
    with (original/"steps.jsonl").open(encoding="utf-8-sig") as a, \
         (baseline/"steps.jsonl").open(encoding="utf-8-sig") as b, \
         (stage/"steps.jsonl").open("w", encoding="utf-8", buffering=1024*1024) as out:
        for aline, bline in zip(a,b,strict=True):
            row, before = json.loads(aline), json.loads(bline)
            step, now = row["step_index"], row["host_monotonic_ns"]
            if step != counts["steps"] or before["step_index"] != step or before["host_monotonic_ns"] != now:
                raise ValueError("Input step mismatch")
            uses_future = False
            for side in arms:
                faults = health[side][2]
                status, status_valid, state_valid = feedback_health(row, side, health)
                fi = bisect.bisect_right(faults, now)-1
                fault_time = faults[fi] if fi >= 0 else None
                healthy = status_valid and state_valid
                if not healthy:
                    communication_breaks[side] = now
                if communication_breaks[side] is not None:
                    fault_time = max(fault_time or 0, communication_breaks[side])
                row.setdefault("health_evidence", {})[side] = {
                    "status_host_monotonic_ns": status[0] if status is not None else None,
                    "status_age_ms": (now - status[0]) / 1e6 if status is not None else None,
                    "follower_status_normal": status_valid, "state_fresh": state_valid,
                    "last_fault_host_monotonic_ns": fault_time,
                    "leader_follow_mode": "operator_confirmed_episode_not_live",
                }
                for field in FIELDS:
                    act = row["action"][side][field]
                    source_info = dict(act)
                    idx = act["source_index"]
                    samples, first_targets = observed[side]
                    target = samples.get((idx, field))
                    valid_target = (target is not None and target[1] and target[0] is not None
                                    and math.isfinite(target[0]) and target[2] <= now
                                    and (fault_time is None or target[2] > fault_time))
                    label = "unknown"
                    value = None
                    initial_home = (side in home_sides and field in JOINTS and
                                    (act["source_host_monotonic_ns"] is None
                                     or act["source_host_monotonic_ns"] < start))
                    if initial_home:
                        if healthy and fault_time is None:
                            label, value = "verified_home_initial", HOME[JOINTS.index(field)]
                            act.update(source_index=None, source_host_monotonic_ns=start,
                                       age_ms=(now - start) / 1e6)
                    elif healthy and act["valid"] and valid_target:
                        label, value = "observed_target", target[0]
                    elif healthy and act["reason"] == "stale" and valid_target:
                        label, value = "held_last_target", target[0]
                    elif healthy and act["reason"] == "no_prior_sample" and fault_time is None:
                        first = first_targets.get(field)
                        if first is not None:
                            (first_value, valid, first_time), first_index = first
                            limit = health_limits.get(side)
                            if (valid and first_value is not None and math.isfinite(first_value)
                                    and now < first_time and (limit is None or first_time < limit)):
                                label, value = "backfilled_first_target", first_value
                                uses_future = True
                                act.update(source_index=first_index, source_host_monotonic_ns=first_time,
                                           lead_ms=(first_time - now) / 1e6)
                    act["original_action"] = source_info
                    act["action_source"] = label
                    act["value"] = value
                    act["valid"] = label != "unknown"
                    act["reason"] = None if act["valid"] else "unknown"
                    counts[f"{side}.{label}_fields"] += 1
                    previous_action = before["action"][side][field]
                    if act["valid"] and previous_action["valid"]:
                        diff = abs(act["value"]-previous_action["value"])
                        (gripper_differences if field=="gripper_width" else joint_differences)[side].append(diff)
                for label in ("observed_target", "held_last_target", "verified_home_initial", "backfilled_first_target", "unknown"):
                    counts[f"{side}.{label}_steps"] += int(any(
                        row["action"][side][f]["action_source"]==label for f in FIELDS))
            row["action_valid"] = all(row["action"][side][f]["valid"] for side in arms for f in FIELDS)
            row["action_uses_future_target"] = uses_future
            complete = row["action_valid"] and row["state_valid"] and row["images_valid"]
            row["candidate_valid"] = complete and not uses_future
            counts["complete_steps_including_backfill"] += int(complete)
            counts["steps_using_future_target"] += int(uses_future)
            counts["steps"] += 1
            counts["candidate_valid_steps"] += int(row["candidate_valid"])
            out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)+"\n")
    if counts["steps"] != meta["stats"]["steps"]:
        raise ValueError("Incorrect step count")
    comparisons = {}
    for side in arms:
        comparisons[side] = {}
        for name, values in (("joint_abs_diff_rad",joint_differences[side]),("gripper_abs_diff_m",gripper_differences[side])):
            comparisons[side][name] = {"count":len(values),"median":median(values) if values else None,
                                       "p95":percentile(values,.95),"max":max(values) if values else None}
    result = dict(meta)
    if home_sides:
        result["home_reset"] = home
    result.update({"export_id":uid,"created_utc":datetime.now(timezone.utc).isoformat(),
                   "source_episode_relative_path":os.path.relpath(episode,final).replace("\\","/"),
                   "stats":dict(counts),"training_ready":False,
                   "action_semantics":"Per-group latest received target; offline startup backfill uses the first future target and is excluded from candidate_valid.",
                   "last_target_policy":{
                       "source_aligned_id":meta["export_id"],"baseline_id":old["export_id"],
                       "source_steps_sha256":digest(original/"steps.jsonl"),
                       "baseline_steps_sha256":digest(baseline/"steps.jsonl"),
                       "analysis_sha256":digest(report_dir/"summary.json") if report_dir else None,
                       "operator_confirmed_follow_entire_episode":True,
                       "follower_health_not_leader_mode_proof":True,
                       "initial_policy":"verified_home_then_observed_target" if home_sides else "backfilled_first_target",
                       "initial_health_limits_host_monotonic_ns":health_limits,
                       "last_target_timeout_ms":None,
                       "on_detected_fault":"unknown until a fresh group target after fault",
                       "caution":"Initial backfill contains future information; held targets depend on continuity of mode and reception.",
                       "generator_sha256":digest(Path(__file__)),
                       "comparisons":comparisons,
                   }})
    (stage/"metadata.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    stage.rename(final)
    print(json.dumps({"output":str(final),"stats":dict(counts),"comparisons":comparisons,
                      "training_ready":False},ensure_ascii=False))
    return final


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("aligned",type=Path)
    p.add_argument("baseline",type=Path,nargs="?")
    p.add_argument("analysis",type=Path,nargs="?")
    p.add_argument("--operator-confirmed-follow",action="store_true")
    args=p.parse_args()
    for path,sub in ((args.aligned,"aligned_candidates"),(args.baseline,"aligned_candidates"),(args.analysis,"hold_analysis")):
        if path is not None and not path.resolve().is_relative_to((ROOT/"data"/sub).resolve()):
            p.error(f"Input must be under data/{sub}")
    run(args.aligned.resolve(),args.baseline.resolve() if args.baseline else None,
        args.analysis.resolve() if args.analysis else None,args.operator_confirmed_follow)


if __name__=="__main__":
    main()
