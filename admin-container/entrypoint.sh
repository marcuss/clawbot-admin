#!/usr/bin/env bash
# entrypoint.sh — ClawBot Admin Container Entrypoint
#
# Reads TASK_ID from environment and executes /tasks/${TASK_ID}.sh
# Secrets are available at:
#   /run/secrets/infisical_token
#   /run/secrets/aws_admin_key
#   /run/secrets/aws_admin_secret

set -euo pipefail

# Validate TASK_ID
if [ -z "${TASK_ID:-}" ]; then
    echo "ERROR: TASK_ID environment variable is not set." >&2
    exit 1
fi

TASK_FILE="/tasks/${TASK_ID}.sh"

if [ ! -f "$TASK_FILE" ]; then
    echo "ERROR: Task file not found: $TASK_FILE" >&2
    exit 1
fi

# Load AWS credentials from secrets (if available)
if [ -f /run/secrets/aws_admin_key ] && [ -f /run/secrets/aws_admin_secret ]; then
    export AWS_ACCESS_KEY_ID
    AWS_ACCESS_KEY_ID=$(cat /run/secrets/aws_admin_key)
    export AWS_SECRET_ACCESS_KEY
    AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/aws_admin_secret)
    export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
fi

# Load Infisical token (if available)
if [ -f /run/secrets/infisical_token ]; then
    export INFISICAL_TOKEN
    INFISICAL_TOKEN=$(cat /run/secrets/infisical_token)
fi

echo "=== ClawBot Admin: Executing task ${TASK_ID} ==="
echo "=== Start: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

# Execute the task
bash "$TASK_FILE"
EXIT_CODE=$?

echo ""
echo "=== End: $(date -u +%Y-%m-%dT%H:%M:%SZ) — Exit code: ${EXIT_CODE} ==="

exit $EXIT_CODE
