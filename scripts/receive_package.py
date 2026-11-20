"""Install one verified package received on stdin into the configured shared project."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
from tempfile import TemporaryDirectory
from uuid import UUID


def relative_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != name or "\\" in name:
        raise ValueError(f"Invalid archive path: {name!r}")
    return path


def matches(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"]:
        return False
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest() == expected["sha256"]


def receive(root, stream):
    root = root.resolve()
    incoming = root / "incoming"
    packages = root / "data" / "training_packages"
    receipts = root / "receipts"
    for directory in (incoming, packages, receipts):
        if not directory.resolve().is_relative_to(root):
            raise ValueError(f"Receiver directory escapes project: {directory}")
        directory.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=incoming) as staged, tarfile.open(fileobj=stream, mode="r|") as tar:
        stage = Path(staged)
        first = tar.next()
        if first is None or first.name != "_manifest.json" or not first.isfile() or first.size > 16 * 1024 * 1024:
            raise ValueError("Invalid transfer manifest")
        with tar.extractfile(first) as file:
            manifest = json.load(file)
        job_id = str(UUID(manifest["job_id"]))
        [group] = manifest["groups"]
        group_path = relative_path(group)
        if len(group_path.parts) != 3 or group_path.parts[:2] != ("data", "training_packages"):
            raise ValueError("Expected a single training package.")
        expected = manifest["files"]
        prefix = group + "/"
        if prefix + "manifest.json" not in expected:
            raise ValueError("Package manifest is missing.")
        for name in expected:
            relative_path(name)
            if not name.startswith(prefix):
                raise ValueError(f"File outside package: {name}")

        seen = set()
        while (member := tar.next()) is not None:
            name = member.name
            if name not in expected or name in seen or not member.isfile() or member.size != expected[name]["size"]:
                raise ValueError(f"Unexpected archive member: {name}")
            path = stage / name.removeprefix(prefix)
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with tar.extractfile(member) as source, path.open("xb") as target:
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    target.write(block)
            if path.stat().st_size != member.size or digest.hexdigest() != expected[name]["sha256"]:
                raise ValueError(f"Transfer checksum mismatch: {name}")
            seen.add(name)
        if seen != set(expected):
            raise ValueError("Missing archive members")

        dest = packages / group_path.name
        if dest.is_symlink():
            raise ValueError(f"Package destination is a symbolic link: {dest}")
        if not dest.exists():
            try:
                stage.rename(dest)
            except OSError:
                if not dest.is_dir():
                    raise
        actual = {prefix + path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()}
        if actual != set(expected) or not all(matches(root / name, info) for name, info in expected.items()):
            raise ValueError(f"Existing package differs: {group}")
        receipt = {"status": "verified", "groups": [group], "file_count": len(expected), "files": expected}
        receipt_path = receipts / (job_id + ".json")
        with receipt_path.open("x", encoding="utf-8") as file:
            json.dump(receipt, file, ensure_ascii=False)
    print(json.dumps({"status": "verified", "group": group, "files": len(expected),
                      "receipt": str(receipt_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    receive(Path(sys.argv[1]), sys.stdin.buffer)
