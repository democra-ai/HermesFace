#!/usr/bin/env python3
"""
Restore /opt/data from latest tar.gz backup on HF Dataset.

Env vars:
  HF_TOKEN              HF access token with read permission
  HERMES_DATASET_REPO   Source dataset repo
  HERMES_HOME           Target directory (default: /opt/data)
"""
import os
import sys
import tarfile

from huggingface_hub import HfApi, hf_hub_download


def main() -> None:
    repo_id = os.environ.get("HERMES_DATASET_REPO")
    token = os.environ.get("HF_TOKEN")
    if not repo_id or not token:
        return

    state_dir = os.environ.get("HERMES_HOME", "/opt/data")
    os.makedirs(state_dir, exist_ok=True)

    try:
        api = HfApi(token=token)
        files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
        backups = sorted(
            (f for f in files
             if f.startswith("state/backup-") and (f.endswith(".tar") or f.endswith(".tar.gz"))),
            reverse=True,
        )
        if not backups:
            if "state/hermes.tar" in files:
                backups = ["state/hermes.tar"]
            else:
                print("[restore_from_dataset] No backups found.", file=sys.stderr)
                return

        for backup_file in backups:
            print(f"[restore_from_dataset] Attempting to restore from: {backup_file}")
            try:
                tar_path = hf_hub_download(
                    repo_id=repo_id, repo_type="dataset", filename=backup_file, token=token
                )
                with tarfile.open(tar_path, "r:*") as tf:
                    tf.extractall(state_dir)
                print(f"[restore_from_dataset] Successfully restored from {backup_file}")
                return
            except Exception as e:
                print(f"[restore_from_dataset] Failed to restore {backup_file}: {e}", file=sys.stderr)

        print("[restore_from_dataset] All backup restore attempts failed.", file=sys.stderr)

    except Exception as e:
        print(f"[restore_from_dataset] Restore process failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
