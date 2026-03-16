"""
audit_logger.py — Append-only audit log for ClawBot Admin.

Every privileged task execution is logged as a JSON line.
Also syncs to S3 for backup.
"""

import json
import os
import subprocess
import fcntl
from datetime import datetime, timezone
from pathlib import Path

CLAWBOT_DIR = Path.home() / ".clawbot-admin"
AUDIT_LOG = CLAWBOT_DIR / "audit.log"
S3_BUCKET = "s3://couplesapp-e2e-reports/admin-audit/audit.log"
AWS_PROFILE = "dev"


def log_event(
    request_id: str,
    task_description: str,
    task_command: str,
    approved_by: str | None,
    approval_channel: str | None,
    approval_method: str | None,
    execution_output: str | None,
    exit_code: int | None,
    duration_seconds: float | None,
    status: str = "executed",  # executed | rejected | timeout | totp_failed
) -> None:
    """Append a single audit event to the log file (thread-safe via flock)."""
    CLAWBOT_DIR.mkdir(mode=0o700, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "requester": "ClawBot",
        "task_description": task_description,
        "task_command": task_command,
        "approved_by": approved_by,
        "approval_channel": approval_channel,
        "approval_method": approval_method,
        "status": status,
        "execution_output": execution_output,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
    }

    line = json.dumps(entry, ensure_ascii=False) + "\n"

    # Append-only with file lock to prevent corruption in concurrent writes
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    # Async S3 backup (best-effort, don't block on failure)
    _backup_to_s3()


def _backup_to_s3() -> None:
    """Best-effort S3 backup. Errors are logged to stderr but not raised."""
    try:
        result = subprocess.run(
            [
                "aws", "s3", "cp",
                str(AUDIT_LOG),
                S3_BUCKET,
                "--profile", AWS_PROFILE,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            import sys
            print(
                f"[audit_logger] S3 backup warning: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as e:
        import sys
        print(f"[audit_logger] S3 backup failed (non-fatal): {e}", file=sys.stderr)
