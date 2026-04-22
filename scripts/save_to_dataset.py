#!/usr/bin/env python3
"""
Simple tar-gz backup of /opt/data → HF Dataset repo.
Keeps the last 5 backups to prevent data loss from corruption.

Env vars:
  HF_TOKEN              HF access token with write permission
  HERMES_DATASET_REPO   Target dataset repo, e.g. username/HermesFace-data
  HERMES_HOME           Source directory (default: /opt/data)
"""
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime

from huggingface_hub import HfApi


def main() -> None:
    repo_id = os.environ.get("HERMES_DATASET_REPO")
    token = os.environ.get("HF_TOKEN")
    state_dir = os.environ.get("HERMES_HOME", "/opt/data")

    if not repo_id or not token:
        print("[save_to_dataset] Missing HF_TOKEN or HERMES_DATASET_REPO.", file=sys.stderr)
        return
    if not os.path.isdir(state_dir):
        print(f"[save_to_dataset] No state directory to save: {state_dir}", file=sys.stderr)
        return

    api = HfApi(token=token)

    # Sync container logs into state dir for persistence
    try:
        sys_log_path = "/opt/data/logs"
        backup_log_path = os.path.join(state_dir, "logs/sys_logs")
        if os.path.exists(sys_log_path) and sys_log_path != backup_log_path:
            if os.path.exists(backup_log_path):
                shutil.rmtree(backup_log_path)
            shutil.copytree(sys_log_path, backup_log_path, ignore_dangling_symlinks=True)
    except Exception as e:
        print(f"[save_to_dataset] Warning: Failed to sync logs: {e}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"state/backup-{timestamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, "hermes.tar.gz")
        try:
            with tarfile.open(tar_path, "w:gz") as tf:
                def exclude(info: tarfile.TarInfo):
                    bad = (".lock", ".tmp", ".pid", ".socket")
                    if info.name.endswith(bad):
                        return None
                    if "__pycache__" in info.name:
                        return None
                    return info
                tf.add(state_dir, arcname=".", filter=exclude)
        except Exception as e:
            print(f"[save_to_dataset] Failed to compress: {e}", file=sys.stderr)
            return

        print(f"[save_to_dataset] Uploading backup: {filename}")
        try:
            api.upload_file(
                path_or_fileobj=tar_path,
                path_in_repo=filename,
                repo_id=repo_id,
                repo_type="dataset",
            )
        except Exception as e:
            print(f"[save_to_dataset] Upload failed: {e}", file=sys.stderr)
            return

    try:
        files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
        backups = sorted(
            f for f in files
            if f.startswith("state/backup-") and (f.endswith(".tar") or f.endswith(".tar.gz"))
        )
        if len(backups) > 5:
            to_delete = backups[:-5]
            print(f"[save_to_dataset] Rotating backups, deleting: {to_delete}")
            for old in to_delete:
                api.delete_file(path_in_repo=old, repo_id=repo_id, repo_type="dataset", token=token)
    except Exception as e:
        print(f"[save_to_dataset] Rotation failed (non-fatal): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
