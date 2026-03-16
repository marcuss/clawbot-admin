"""
clawbot_client.py — ClawBot Admin Client

Use this to request privileged task execution with approval from Marcus.

Usage:
    from clawbot_client import request_privileged_task

    result = request_privileged_task(
        description="Revisar tabla profiles de producción",
        command="psql $PROD_DB_URL -c 'SELECT id, email FROM profiles LIMIT 20'",
        timeout_seconds=300
    )
    print(result.output)
    print(f"Exit code: {result.exit_code}")
"""

import json
import socket
import os
from dataclasses import dataclass
from typing import Optional

SOCK_PATH = "/tmp/clawbot-admin.sock"
RESPONSE_FILE = "/tmp/clawbot-admin-response.txt"


@dataclass
class TaskResult:
    request_id: str
    output: str
    exit_code: int
    duration_seconds: float
    approved: bool = True


class ApprovalError(Exception):
    """Raised when a task is rejected, times out, or TOTP fails."""
    pass


def request_privileged_task(
    description: str,
    command: str,
    timeout_seconds: int = 300,
) -> TaskResult:
    """
    Request execution of a privileged task.

    Sends the request to the approval service, which will:
    1. Ask Marcus to approve via WhatsApp with a TOTP code
    2. Execute the task in an isolated Docker container if approved
    3. Return the output

    Raises:
        ApprovalError: if rejected, timed out, or TOTP validation fails
        ConnectionError: if the approval service is not running
    """
    if not os.path.exists(SOCK_PATH):
        raise ConnectionError(
            f"Approval service not running. Socket not found: {SOCK_PATH}\n"
            "Start it with: launchctl load ~/Library/LaunchAgents/com.clawbot.admin.plist"
        )

    payload = json.dumps({
        "description": description,
        "command": command,
        "timeout_seconds": timeout_seconds,
    }).encode()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Allow extra time for human approval + execution
    sock.settimeout(timeout_seconds + 120)

    try:
        sock.connect(SOCK_PATH)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)  # signal end of request

        # Read response
        raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()

    if not raw:
        raise ConnectionError("Empty response from approval service.")

    response = json.loads(raw.decode("utf-8"))

    if "error" in response:
        raise ApprovalError(response["error"])

    return TaskResult(
        request_id=response.get("request_id", "unknown"),
        output=response.get("output", ""),
        exit_code=response.get("exit_code", -1),
        duration_seconds=response.get("duration_seconds", 0.0),
        approved=True,
    )


def write_approval_response(request_id: str, response_text: str) -> None:
    """
    Write Marcus's approval response to the response file.

    This is called by ClawBot (main agent) when Marcus replies via WhatsApp.
    Format: "<request_id>|<response_text>"

    Example:
        write_approval_response("abc12345", "OK 847291")
        write_approval_response("abc12345", "NO")
    """
    content = f"{request_id}|{response_text}\n"
    with open(RESPONSE_FILE, "w") as f:
        f.write(content)


# ─── CLI helper (for testing) ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "respond":
        # Usage: python3 clawbot_client.py respond <request_id> <response>
        # Example: python3 clawbot_client.py respond abc12345 "OK 847291"
        if len(sys.argv) < 4:
            print("Usage: python3 clawbot_client.py respond <request_id> <response>")
            sys.exit(1)
        req_id = sys.argv[2]
        resp = sys.argv[3]
        write_approval_response(req_id, resp)
        print(f"✅ Response written: {req_id}|{resp}")
    else:
        # Quick test request
        print("Testing ClawBot Admin client...")
        try:
            result = request_privileged_task(
                description="Test: echo hello",
                command="echo 'Hello from ClawBot Admin!'",
                timeout_seconds=300,
            )
            print(f"✅ Success!")
            print(f"Output: {result.output}")
            print(f"Exit code: {result.exit_code}")
            print(f"Duration: {result.duration_seconds}s")
        except ApprovalError as e:
            print(f"❌ Approval error: {e}")
        except ConnectionError as e:
            print(f"❌ Connection error: {e}")
