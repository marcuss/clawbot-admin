"""
approval_service.py — ClawBot Admin Approval Service

Listens for privileged task requests on a Unix socket, notifies Marcus via
WhatsApp, waits for TOTP approval, executes the task in the admin Docker
container, and logs everything to the audit log.

Run this as a background daemon (see com.clawbot.admin.plist for launchd).
"""

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pyotp

from audit_logger import log_event

# ─── Config ───────────────────────────────────────────────────────────────────
SOCK_PATH = "/tmp/clawbot-admin.sock"
RESPONSE_FILE = "/tmp/clawbot-admin-response.txt"
CLAWBOT_DIR = Path.home() / ".clawbot-admin"
SECRET_FILE = CLAWBOT_DIR / "totp.secret"
TASKS_DIR = Path(__file__).parent / "tasks"
DOCKER_COMPOSE_FILE = Path(__file__).parent / "docker-compose.admin.yml"

MARCUS_WHATSAPP = "+573013275073"
APPROVAL_TIMEOUT = 300  # 5 minutes
MAX_CONN_BACKLOG = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("approval_service")


# ─── TOTP ─────────────────────────────────────────────────────────────────────
def load_totp() -> pyotp.TOTP:
    if not SECRET_FILE.exists():
        log.error(f"TOTP secret not found at {SECRET_FILE}. Run setup.sh first.")
        sys.exit(1)
    secret = SECRET_FILE.read_text().strip()
    return pyotp.TOTP(secret)


def validate_totp(totp: pyotp.TOTP, code: str) -> bool:
    """Validate code with a ±2 window (±60s) to handle clock skew and response delays."""
    return totp.verify(code, valid_window=2)


# ─── WhatsApp notification ─────────────────────────────────────────────────────
def notify_marcus(request_id: str, description: str, command: str) -> None:
    msg = (
        f"🔐 *ClawBot Admin — Solicitud de tarea privilegiada*\n\n"
        f"*ID:* `{request_id}`\n"
        f"*Descripción:* {description}\n"
        f"*Comando:* `{command}`\n\n"
        f"Responde *OK <código-TOTP>* para aprobar (ej: `OK 847291`)\n"
        f"o *NO* para cancelar.\n\n"
        f"⏳ Tienes 5 minutos. Un solo intento."
    )
    subprocess.run(
        [
            "openclaw", "message", "send",
            "--target", MARCUS_WHATSAPP,
            "--message", msg,
            "--json",
        ],
        check=True,
    )
    log.info(f"[{request_id}] WhatsApp notification sent to Marcus.")


# ─── Response polling ─────────────────────────────────────────────────────────
def wait_for_response(request_id: str, timeout: int = APPROVAL_TIMEOUT) -> str | None:
    """
    Poll /tmp/clawbot-admin-response.txt for a response.

    File format (written by ClawBot main agent when Marcus replies):
        <request_id>|<response_text>

    Returns the response text (e.g. "OK 847291" or "NO"), or None on timeout.
    The response file is deleted after reading to prevent replay.
    """
    response_path = Path(RESPONSE_FILE)
    deadline = time.monotonic() + timeout
    poll_interval = 2  # seconds

    log.info(f"[{request_id}] Waiting for response (timeout={timeout}s) ...")

    while time.monotonic() < deadline:
        if response_path.exists():
            try:
                content = response_path.read_text().strip()
                if "|" in content:
                    file_req_id, response = content.split("|", 1)
                    if file_req_id.strip() == request_id:
                        response_path.unlink(missing_ok=True)
                        log.info(f"[{request_id}] Got response: {response[:20]}...")
                        return response.strip()
                    else:
                        log.debug(
                            f"[{request_id}] Response file has different request_id "
                            f"({file_req_id.strip()}), skipping."
                        )
            except Exception as e:
                log.warning(f"[{request_id}] Error reading response file: {e}")
        time.sleep(poll_interval)

    log.warning(f"[{request_id}] Timeout waiting for approval.")
    return None


# ─── Docker execution ─────────────────────────────────────────────────────────
def create_task_file(task_id: str, command: str) -> Path:
    """Write the task shell script to the tasks directory."""
    TASKS_DIR.mkdir(exist_ok=True)
    task_file = TASKS_DIR / f"{task_id}.sh"
    task_file.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{command}\n")
    task_file.chmod(0o755)
    return task_file


def execute_in_container(task_id: str, command: str) -> tuple[str, int, float]:
    """
    Run the task in the admin Docker container.
    Returns (output, exit_code, duration_seconds).
    """
    task_file = create_task_file(task_id, command)
    start = time.monotonic()

    try:
        result = subprocess.run(
            [
                "docker", "compose",
                "-f", str(DOCKER_COMPOSE_FILE),
                "run", "--rm",
                "-e", f"TASK_ID={task_id}",
                "clawbot-admin",
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max for container execution
            cwd=str(Path(__file__).parent),
        )
        output = result.stdout + (("\nSTDERR:\n" + result.stderr) if result.stderr else "")
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        output = "ERROR: Container execution timed out (10 min limit)"
        exit_code = -1
    except Exception as e:
        output = f"ERROR: Failed to run container: {e}"
        exit_code = -1
    finally:
        # Clean up task file
        task_file.unlink(missing_ok=True)

    duration = time.monotonic() - start
    return output, exit_code, duration


# ─── Request handler ──────────────────────────────────────────────────────────
def handle_request(conn: socket.socket, totp: pyotp.TOTP) -> None:
    """Handle a single approval request from ClawBot client."""
    request_id = str(uuid.uuid4())[:8]

    try:
        raw = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 1_000_000:
                conn.sendall(b'{"error": "Request too large"}\n')
                return

        if not raw:
            return

        data = json.loads(raw.decode("utf-8"))
        description = data.get("description", "(no description)")
        command = data.get("command", "")
        timeout = int(data.get("timeout_seconds", APPROVAL_TIMEOUT))

        if not command:
            conn.sendall(b'{"error": "command is required"}\n')
            return

        log.info(f"[{request_id}] New request: {description[:80]}")

        # 1. Notify Marcus
        try:
            notify_marcus(request_id, description, command)
        except Exception as e:
            log.error(f"[{request_id}] Failed to send WhatsApp: {e}")
            conn.sendall(
                json.dumps({"error": f"Failed to notify Marcus: {e}"}).encode() + b"\n"
            )
            log_event(
                request_id=request_id,
                task_description=description,
                task_command=command,
                approved_by=None,
                approval_channel=None,
                approval_method=None,
                execution_output=f"Notification failed: {e}",
                exit_code=-1,
                duration_seconds=0,
                status="notification_failed",
            )
            return

        # 2. Wait for response
        response = wait_for_response(request_id, timeout=min(timeout, APPROVAL_TIMEOUT))

        if response is None:
            # Timeout
            conn.sendall(b'{"error": "Approval timeout (5 min). Request cancelled."}\n')
            log_event(
                request_id=request_id,
                task_description=description,
                task_command=command,
                approved_by=None,
                approval_channel="WhatsApp",
                approval_method="TOTP",
                execution_output=None,
                exit_code=None,
                duration_seconds=None,
                status="timeout",
            )
            return

        # 3. Parse response
        response_upper = response.upper().strip()

        if response_upper == "NO" or response_upper.startswith("NO "):
            conn.sendall(b'{"error": "Request rejected by Marcus."}\n')
            log_event(
                request_id=request_id,
                task_description=description,
                task_command=command,
                approved_by=MARCUS_WHATSAPP,
                approval_channel="WhatsApp",
                approval_method="TOTP",
                execution_output=None,
                exit_code=None,
                duration_seconds=None,
                status="rejected",
            )
            return

        # Expect "OK <6-digit-code>"
        if not response_upper.startswith("OK "):
            conn.sendall(
                b'{"error": "Invalid response format. Expected OK <code> or NO."}\n'
            )
            log_event(
                request_id=request_id,
                task_description=description,
                task_command=command,
                approved_by=MARCUS_WHATSAPP,
                approval_channel="WhatsApp",
                approval_method="TOTP",
                execution_output="Invalid response format",
                exit_code=None,
                duration_seconds=None,
                status="totp_failed",
            )
            return

        totp_code = response[3:].strip()  # everything after "OK "

        # 4. Validate TOTP (single attempt)
        if not validate_totp(totp, totp_code):
            log.warning(f"[{request_id}] Invalid TOTP code provided.")
            conn.sendall(
                b'{"error": "Invalid TOTP code. Request cancelled (1 attempt allowed)."}\n'
            )
            log_event(
                request_id=request_id,
                task_description=description,
                task_command=command,
                approved_by=MARCUS_WHATSAPP,
                approval_channel="WhatsApp",
                approval_method="TOTP",
                execution_output="Invalid TOTP code",
                exit_code=None,
                duration_seconds=None,
                status="totp_failed",
            )
            return

        log.info(f"[{request_id}] TOTP validated ✅. Executing task...")

        # 5. Execute in container
        task_id = f"task-{request_id}"
        output, exit_code, duration = execute_in_container(task_id, command)

        # 6. Log FIRST — before sending to client (so audit is never lost)
        log_event(
            request_id=request_id,
            task_description=description,
            task_command=command,
            approved_by=MARCUS_WHATSAPP,
            approval_channel="WhatsApp",
            approval_method="TOTP",
            execution_output=output[:4096],  # cap log size
            exit_code=exit_code,
            duration_seconds=round(duration, 2),
            status="executed",
        )
        log.info(
            f"[{request_id}] Done. exit_code={exit_code}, "
            f"duration={duration:.1f}s"
        )

        # 7. Return result to client (best-effort — pipe may already be closed)
        result_payload = json.dumps({
            "request_id": request_id,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 2),
            "output": output,
        })
        try:
            conn.sendall(result_payload.encode() + b"\n")
        except BrokenPipeError:
            log.warning(f"[{request_id}] Client disconnected before result was sent (audit log already written)")

    except json.JSONDecodeError:
        conn.sendall(b'{"error": "Invalid JSON request"}\n')
    except Exception as e:
        log.exception(f"[{request_id}] Unexpected error: {e}")
        try:
            conn.sendall(json.dumps({"error": str(e)}).encode() + b"\n")
        except Exception:
            pass


# ─── Main server loop ─────────────────────────────────────────────────────────
def run_server() -> None:
    totp = load_totp()
    log.info("ClawBot Admin Approval Service starting...")
    log.info(f"Listening on Unix socket: {SOCK_PATH}")
    log.info(f"Response file: {RESPONSE_FILE}")

    # Clean up stale socket
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    server.listen(MAX_CONN_BACKLOG)

    log.info("✅ Ready. Waiting for requests...")

    try:
        while True:
            conn, _ = server.accept()
            try:
                with conn:
                    handle_request(conn, totp)
            except Exception as e:
                log.exception(f"Connection handler error: {e}")
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        server.close()
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)


if __name__ == "__main__":
    run_server()
