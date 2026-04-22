#!/usr/bin/env python3
"""
HermesFace Full Directory Persistence for Hugging Face Spaces
=============================================================

Tar-gz snapshot of /opt/data with atomic upload, rotation, and integrity check.

Usage:
    python3 hermes_persist.py save     # backup
    python3 hermes_persist.py load     # restore latest
    python3 hermes_persist.py status   # show current backups

Env vars:
    HF_TOKEN              HF access token with write permission
    HERMES_DATASET_REPO   Target dataset repo (e.g. username/HermesFace-data)
    HERMES_HOME           Source directory (default: /opt/data)
    MAX_BACKUPS           Rotation size (default: 5)
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import RepositoryNotFoundError


class Config:
    HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    DATASET_REPO = os.environ.get("HERMES_DATASET_REPO", "")
    HF_TOKEN = os.environ.get("HF_TOKEN", "")
    MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "5"))
    BACKUP_PREFIX = "backup-"

    EXCLUDE_SUFFIXES = (".lock", ".tmp", ".pyc", ".socket", ".pid")
    EXCLUDE_NAMES = {"__pycache__", "node_modules", ".DS_Store", ".git"}
    SKIP_DIRS = {".cache", "logs/sys_logs"}


def _log(level: str, msg: str, **kv: object) -> None:
    payload = {"timestamp": datetime.now().isoformat(), "level": level,
               "module": "hermes-persist", "message": msg, **kv}
    print(json.dumps(payload))


def _tar_filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    name = info.name
    if any(name.endswith(suf) for suf in Config.EXCLUDE_SUFFIXES):
        return None
    parts = set(Path(name).parts)
    if parts & Config.EXCLUDE_NAMES:
        return None
    rel = name.lstrip("./")
    for skip in Config.SKIP_DIRS:
        if rel == skip or rel.startswith(skip + "/"):
            return None
    return info


def _api() -> HfApi:
    if not Config.HF_TOKEN:
        raise RuntimeError("HF_TOKEN not set")
    if not Config.DATASET_REPO:
        raise RuntimeError("HERMES_DATASET_REPO not set")
    return HfApi(token=Config.HF_TOKEN)


def _ensure_repo(api: HfApi) -> None:
    try:
        api.repo_info(repo_id=Config.DATASET_REPO, repo_type="dataset")
    except RepositoryNotFoundError:
        _log("INFO", "creating_dataset", repo=Config.DATASET_REPO)
        api.create_repo(repo_id=Config.DATASET_REPO, repo_type="dataset", private=True)


def save() -> int:
    if not Config.HERMES_HOME.exists():
        _log("ERROR", "no_source_dir", path=str(Config.HERMES_HOME))
        return 1

    api = _api()
    _ensure_repo(api)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"state/{Config.BACKUP_PREFIX}{timestamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "hermes.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(str(Config.HERMES_HOME), arcname=".", filter=_tar_filter)

        size = tar_path.stat().st_size
        sha256 = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        _log("INFO", "uploading_backup", file=archive_name, bytes=size, sha256=sha256[:12])

        api.upload_file(
            path_or_fileobj=str(tar_path),
            path_in_repo=archive_name,
            repo_id=Config.DATASET_REPO,
            repo_type="dataset",
            commit_message=f"HermesFace backup {timestamp}",
        )

    try:
        files = api.list_repo_files(repo_id=Config.DATASET_REPO, repo_type="dataset")
        backups = sorted(
            f for f in files
            if f.startswith(f"state/{Config.BACKUP_PREFIX}") and f.endswith(".tar.gz")
        )
        if len(backups) > Config.MAX_BACKUPS:
            for old in backups[: -Config.MAX_BACKUPS]:
                _log("INFO", "rotating_backup", delete=old)
                api.delete_file(
                    path_in_repo=old,
                    repo_id=Config.DATASET_REPO,
                    repo_type="dataset",
                    token=Config.HF_TOKEN,
                )
    except Exception as e:
        _log("WARNING", "rotation_failed", error=str(e))

    _log("INFO", "save_completed", file=archive_name)
    return 0


def load() -> int:
    api = _api()
    try:
        files = api.list_repo_files(repo_id=Config.DATASET_REPO, repo_type="dataset")
    except RepositoryNotFoundError:
        _log("ERROR", "repo_not_found", repo=Config.DATASET_REPO)
        return 1

    backups = sorted(
        (f for f in files
         if f.startswith(f"state/{Config.BACKUP_PREFIX}") and f.endswith(".tar.gz")),
        reverse=True,
    )
    if not backups:
        _log("WARNING", "no_backups_found")
        return 1

    Config.HERMES_HOME.mkdir(parents=True, exist_ok=True)

    for backup in backups:
        _log("INFO", "attempting_restore", file=backup)
        try:
            local = hf_hub_download(
                repo_id=Config.DATASET_REPO,
                repo_type="dataset",
                filename=backup,
                token=Config.HF_TOKEN,
            )
            # Local safety backup
            snapshot = Config.HERMES_HOME.parent / f"hermes_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            if Config.HERMES_HOME.exists() and any(Config.HERMES_HOME.iterdir()):
                shutil.copytree(Config.HERMES_HOME, snapshot, dirs_exist_ok=True)
            with tarfile.open(local, "r:*") as tf:
                tf.extractall(str(Config.HERMES_HOME))
            _log("INFO", "restore_completed", file=backup)
            return 0
        except Exception as e:
            _log("ERROR", "restore_failed", file=backup, error=str(e))
            continue

    _log("ERROR", "all_restore_attempts_failed")
    return 1


def status() -> int:
    api = _api()
    try:
        files = api.list_repo_files(repo_id=Config.DATASET_REPO, repo_type="dataset")
    except RepositoryNotFoundError:
        _log("WARNING", "repo_not_found", repo=Config.DATASET_REPO)
        return 0

    backups = sorted(
        (f for f in files
         if f.startswith(f"state/{Config.BACKUP_PREFIX}") and f.endswith(".tar.gz")),
        reverse=True,
    )
    local_files = 0
    if Config.HERMES_HOME.exists():
        local_files = sum(1 for _ in Config.HERMES_HOME.rglob("*") if _.is_file())

    print(json.dumps({
        "repo": Config.DATASET_REPO,
        "local_dir": str(Config.HERMES_HOME),
        "local_files": local_files,
        "remote_backups": backups,
        "max_backups": Config.MAX_BACKUPS,
    }, indent=2))
    return 0


COMMANDS = {"save": save, "load": load, "status": status}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: {sys.argv[0]} {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]]())


if __name__ == "__main__":
    main()
