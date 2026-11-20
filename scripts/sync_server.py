"""Verify local packages and optionally send them over an operator-configured SSH connection."""
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]




def digest(path):
    with path.open("rb") as file:
        sha = hashlib.sha256()
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(block)
        return sha.hexdigest()


def details(path):
    return {"size": path.stat().st_size, "sha256": digest(path)}


def add_tree(files, folder, prefix):
    if not folder.is_dir():
        raise ValueError(f"Missing directory: {folder}")
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported: {path}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[f"{prefix}/{path.relative_to(folder).as_posix()}"] = path


def verify_episode(episode):
    manifest_path = episode / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest["algorithm"] != "sha256":
        raise ValueError("Unsupported episode manifest")
    actual = {path.relative_to(episode).as_posix() for path in episode.rglob("*")
              if path.is_file() and path != manifest_path}
    expected = set(manifest["files"])
    if actual != expected:
        raise ValueError("Episode files differ from recorder manifest")
    for name, info in manifest["files"].items():
        path = episode / name
        if path.stat().st_size != info["bytes"] or digest(path) != info["sha256"]:
            raise ValueError(f"Source episode checksum mismatch: {name}")


def load_destination():
    path = ROOT / "configs" / "upload.json"
    if not path.is_file():
        raise ValueError("Copy configs/upload.example.json to configs/upload.json and set the lab destination.")
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    host = settings.get("host")
    root = settings.get("project_root")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("Upload host must be a host alias defined in SSH config.")
    if not isinstance(root, str) or not root.startswith("/") or ".." in PurePosixPath(root).parts:
        raise ValueError("Upload project_root must be an absolute server path.")
    root = PurePosixPath(root)
    if len(root.parts) < 2:
        raise ValueError("Choose a dedicated project directory on the server.")
    return host, str(root)


def send(files, group, destination):
    host, root = destination
    descriptions = {name: details(path) for name, path in files.items()}
    manifest = {"job_id": str(uuid4()), "groups": [group], "files": descriptions}
    receiver = Path(__file__).with_name("receive_package.py").read_text(encoding="utf-8")
    code = f"import sys\nsys.argv = ['receive_package.py', {root!r}]\n" + receiver
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    with subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           host, command], stdin=subprocess.PIPE) as process:
        with process.stdin:
            with tarfile.open(fileobj=process.stdin, mode="w|") as tar:
                payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                header = tarfile.TarInfo("_manifest.json")
                header.size = len(payload)
                tar.addfile(header, io.BytesIO(payload))
                for name, path in sorted(files.items()):
                    header = tarfile.TarInfo(name)
                    header.size = descriptions[name]["size"]
                    with path.open("rb") as file:
                        tar.addfile(header, file)
        status = process.wait()
        if status:
            raise RuntimeError(f"Remote transfer failed (ssh exit {status})")
