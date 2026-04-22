#!/usr/bin/env python3
"""
Atomic Dataset Persistence for HermesFace
Save state to Hugging Face Dataset with atomic commit, checksum, and backup.

Usage:
    python3 save_to_dataset_atomic.py <repo_id> <source_path1> [source_path2...]

Env vars:
    HF_TOKEN              HF access token (read from env by huggingface_hub)
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
from huggingface_hub.utils import RepositoryNotFoundError

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "atomic-save", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)


class AtomicDatasetSaver:
    def __init__(self, repo_id: str, dataset_path: str = "state"):
        self.repo_id = repo_id
        self.dataset_path = Path(dataset_path)
        self.api = HfApi()
        self.max_retries = 3
        self.base_delay = 1.0
        self.max_backups = 3
        logger.info(f"init repo_id={repo_id} dataset_path={dataset_path}")

    def calculate_checksum(self, file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        return h.hexdigest()

    def create_backup(self, current_commit: Optional[str]) -> Optional[str]:
        if not current_commit:
            return None
        try:
            files = self.api.list_repo_files(
                repo_id=self.repo_id, repo_type="dataset", revision=current_commit
            )
            state_files = [f for f in files if f.startswith(str(self.dataset_path))]
            if not state_files:
                return None

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"backups/state_{timestamp}"
            logger.info(f"creating_backup path={backup_path} files={len(state_files)}")

            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                ops = []
                for file_path in state_files:
                    local = hf_hub_download(
                        repo_id=self.repo_id,
                        repo_type="dataset",
                        filename=file_path,
                        revision=current_commit,
                    )
                    if local:
                        dst = tmpdir_path / Path(file_path).name
                        shutil.copy2(local, dst)
                        ops.append(CommitOperationAdd(
                            path_in_repo=f"{backup_path}/{Path(file_path).name}",
                            path_or_fileobj=str(dst),
                        ))

                if ops:
                    info = self.api.create_commit(
                        repo_id=self.repo_id,
                        repo_type="dataset",
                        operations=ops,
                        commit_message=f"Backup state before update - {timestamp}",
                        parent_commit=current_commit,
                    )
                    logger.info(f"backup_created commit={info.oid}")
                    return info.oid
        except Exception as e:
            logger.error(f"backup_failed error={e}")
        return None

    def save_state_atomic(
        self, state_data: Dict[str, Any], source_paths: List[str]
    ) -> Dict[str, Any]:
        operation_id = f"save_{int(time.time())}"
        logger.info(f"starting_atomic_save op={operation_id} sources={source_paths}")

        try:
            try:
                repo_info = self.api.repo_info(repo_id=self.repo_id, repo_type="dataset")
                current_commit = repo_info.sha
            except RepositoryNotFoundError:
                current_commit = None

            backup_commit = self.create_backup(current_commit)

            with tempfile.TemporaryDirectory() as tmpdir:
                state_dir = Path(tmpdir) / self.dataset_path
                state_dir.mkdir(parents=True, exist_ok=True)

                metadata = {
                    "timestamp": datetime.now().isoformat(),
                    "operation_id": operation_id,
                    "checksum": None,
                    "backup_commit": backup_commit,
                    "state_data": state_data,
                }
                metadata_path = state_dir / "metadata.json"
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=2)

                ops = [CommitOperationAdd(
                    path_in_repo="state/metadata.json",
                    path_or_fileobj=str(metadata_path),
                )]

                for source_path in source_paths:
                    src = Path(source_path)
                    if src.exists():
                        dst = state_dir / src.name
                        shutil.copy2(src, dst)
                        checksum = self.calculate_checksum(dst)
                        ops.append(CommitOperationAdd(
                            path_in_repo=f"state/{src.name}",
                            path_or_fileobj=str(dst),
                        ))
                        logger.info(f"file_added source={source_path} sha256={checksum[:12]}")

                metadata["checksum"] = hashlib.sha256(
                    json.dumps(state_data, sort_keys=True).encode()
                ).hexdigest()
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=2)

                info = self.api.create_commit(
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    operations=ops,
                    commit_message=f"Atomic state update - {operation_id}",
                    parent_commit=current_commit,
                )

                result = {
                    "success": True,
                    "operation_id": operation_id,
                    "commit_id": info.oid,
                    "backup_commit": backup_commit,
                    "timestamp": datetime.now().isoformat(),
                    "files_count": len(source_paths),
                }
                logger.info(f"atomic_save_completed {result}")
                return result

        except Exception as e:
            logger.error(f"atomic_save_failed error={e}")
            raise


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: python save_to_dataset_atomic.py <repo_id> <source_path1> [source_path2...]",
            "status": "error",
        }, indent=2))
        sys.exit(1)

    repo_id = sys.argv[1]
    source_paths = sys.argv[2:]
    for p in source_paths:
        if not os.path.exists(p):
            print(json.dumps({"error": f"Source path does not exist: {p}", "status": "error"}, indent=2))
            sys.exit(1)

    state_data = {
        "environment": "production",
        "version": "1.0.0",
        "platform": "huggingface-spaces",
        "app": "hermesface",
        "timestamp": datetime.now().isoformat(),
    }

    try:
        saver = AtomicDatasetSaver(repo_id)
        result = saver.save_state_atomic(state_data, source_paths)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "status": "error"}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
