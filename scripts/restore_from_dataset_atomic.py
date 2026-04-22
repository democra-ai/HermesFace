#!/usr/bin/env python3
"""
Atomic Dataset Restore for HermesFace
Restore state from HF Dataset with integrity validation and local backup.

Usage:
    python3 restore_from_dataset_atomic.py <repo_id> <target_dir> [--force]
"""
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from huggingface_hub import HfApi, hf_hub_download

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "atomic-restore", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)


class AtomicDatasetRestorer:
    def __init__(self, repo_id: str, dataset_path: str = "state"):
        self.repo_id = repo_id
        self.dataset_path = Path(dataset_path)
        self.api = HfApi()

    def calculate_checksum(self, file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        return h.hexdigest()

    def validate_integrity(self, metadata: Dict[str, Any], state_files: List[Path]) -> bool:
        try:
            if "checksum" not in metadata:
                logger.warning("no_checksum_in_metadata")
                return True
            calculated = hashlib.sha256(
                json.dumps(metadata.get("state_data", {}), sort_keys=True).encode()
            ).hexdigest()
            expected = metadata["checksum"]
            valid = calculated == expected
            logger.info(f"integrity_check expected={expected} calculated={calculated} valid={valid}")
            return valid
        except Exception as e:
            logger.error(f"integrity_validation_failed error={e}")
            return False

    def create_backup_before_restore(self, target_dir: Path) -> Optional[Path]:
        try:
            if not target_dir.exists():
                return None
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = target_dir.parent / f"state_backup_{timestamp}"
            logger.info(f"creating_local_backup source={target_dir} backup={backup_dir}")
            shutil.copytree(target_dir, backup_dir)
            return backup_dir
        except Exception as e:
            logger.error(f"local_backup_failed error={e}")
            return None

    def restore_from_commit(
        self, commit_sha: str, target_dir: Path, force: bool = False
    ) -> Dict[str, Any]:
        operation_id = f"restore_{int(time.time())}"
        logger.info(f"starting_atomic_restore op={operation_id} commit={commit_sha}")

        try:
            self.api.repo_info(repo_id=self.repo_id, repo_type="dataset", revision=commit_sha)
        except Exception as e:
            return {"success": False, "operation_id": operation_id,
                    "error": f"Invalid commit: {e}", "timestamp": datetime.now().isoformat()}

        backup_dir = self.create_backup_before_restore(target_dir)

        try:
            with tempfile.TemporaryDirectory() as _tmpdir:
                files = self.api.list_repo_files(
                    repo_id=self.repo_id, repo_type="dataset", revision=commit_sha
                )
                state_files = [f for f in files if f.startswith(str(self.dataset_path))]
                if not state_files:
                    return {"success": False, "operation_id": operation_id,
                            "error": "No state files found in commit",
                            "timestamp": datetime.now().isoformat()}

                downloaded_files: List[Path] = []
                metadata = None
                for file_path in state_files:
                    try:
                        local = hf_hub_download(
                            repo_id=self.repo_id,
                            repo_type="dataset",
                            filename=file_path,
                            revision=commit_sha,
                        )
                        if local:
                            downloaded_files.append(Path(local))
                            if file_path.endswith("metadata.json"):
                                with open(local, "r") as f:
                                    metadata = json.load(f)
                    except Exception as e:
                        logger.error(f"file_download_failed file={file_path} error={e}")

                if not metadata:
                    return {"success": False, "operation_id": operation_id,
                            "error": "Metadata not found in state files",
                            "timestamp": datetime.now().isoformat()}

                if not self.validate_integrity(metadata, downloaded_files):
                    return {"success": False, "operation_id": operation_id,
                            "error": "Data integrity validation failed",
                            "timestamp": datetime.now().isoformat()}

                target_dir.mkdir(parents=True, exist_ok=True)
                restored = []
                for f in downloaded_files:
                    if f.name != "metadata.json":
                        dst = target_dir / f.name
                        shutil.copy2(f, dst)
                        restored.append(str(dst))

                result = {
                    "success": True,
                    "operation_id": operation_id,
                    "commit_sha": commit_sha,
                    "backup_dir": str(backup_dir) if backup_dir else None,
                    "timestamp": datetime.now().isoformat(),
                    "restored_files": restored,
                    "metadata": metadata,
                }
                logger.info(f"atomic_restore_completed {result}")
                return result
        except Exception as e:
            logger.error(f"atomic_restore_failed error={e}")
            return {"success": False, "operation_id": operation_id,
                    "error": str(e), "timestamp": datetime.now().isoformat()}

    def restore_latest(self, target_dir: Path, force: bool = False) -> Dict[str, Any]:
        try:
            repo_info = self.api.repo_info(repo_id=self.repo_id, repo_type="dataset")
            if not repo_info.sha:
                return {"success": False, "error": "No commit found in repository",
                        "timestamp": datetime.now().isoformat()}
            return self.restore_from_commit(repo_info.sha, target_dir, force)
        except Exception as e:
            return {"success": False, "error": f"Failed to get latest commit: {e}",
                    "timestamp": datetime.now().isoformat()}


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: python restore_from_dataset_atomic.py <repo_id> <target_dir> [--force]",
            "status": "error",
        }, indent=2))
        sys.exit(1)

    repo_id = sys.argv[1]
    target_dir = sys.argv[2]
    force = "--force" in sys.argv

    try:
        restorer = AtomicDatasetRestorer(repo_id)
        result = restorer.restore_latest(Path(target_dir), force)
        print(json.dumps(result, indent=2))
        if not result.get("success", False):
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e), "status": "error"}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
