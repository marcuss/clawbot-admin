#!/usr/bin/env bash
# setup.sh — ClawBot Admin TOTP Setup
# Run ONCE manually. Never run again (it would overwrite your TOTP seed).
# SECURITY: The seed is NEVER sent over the network.

set -euo pipefail

CLAWBOT_DIR="$HOME/.clawbot-admin"
SECRET_FILE="$CLAWBOT_DIR/totp.secret"

echo "╔══════════════════════════════════════════════════╗"
echo "║        ClawBot Admin — TOTP Setup                ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check dependencies
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 required but not found."; exit 1; }
python3 -c "import pyotp, qrcode" 2>/dev/null || {
    echo "📦 Installing required Python packages..."
    pip3 install pyotp qrcode[pil] --quiet
}

# Warn if already setup
if [ -f "$SECRET_FILE" ]; then
    echo "⚠️  WARNING: TOTP secret already exists at $SECRET_FILE"
    echo "   Running setup again will REPLACE your existing secret."
    echo "   You will need to re-scan the QR code in Microsoft Authenticator."
    echo ""
    read -r -p "Continue and overwrite? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

# Create secure directory
mkdir -p "$CLAWBOT_DIR"
chmod 700 "$CLAWBOT_DIR"

# Generate TOTP seed and QR
python3 - <<'PYEOF'
import pyotp
import qrcode
import os
import sys

CLAWBOT_DIR = os.path.expanduser("~/.clawbot-admin")
SECRET_FILE = os.path.join(CLAWBOT_DIR, "totp.secret")

# Generate a random base32 secret
secret = pyotp.random_base32()

# Save to file with secure permissions (write first, then chmod)
with open(SECRET_FILE, "w") as f:
    f.write(secret)
os.chmod(SECRET_FILE, 0o600)

print(f"\n✅ TOTP secret generated and saved to {SECRET_FILE}")
print("   Permissions set to 600 (owner read/write only)")
print("")

# Generate TOTP URI for QR
totp = pyotp.TOTP(secret)
uri = totp.provisioning_uri(
    name="Marcus",
    issuer_name="ClawBot Admin"
)

# Generate ASCII QR code
qr = qrcode.QRCode(
    version=None,
    error_correction=qrcode.constants.ERROR_CORRECT_L,
    box_size=1,
    border=2,
)
qr.add_data(uri)
qr.make(fit=True)

print("══════════════════════════════════════════════════")
print("  Scan this QR code with Microsoft Authenticator  ")
print("══════════════════════════════════════════════════")
print("")
qr.print_ascii(invert=True)
print("")
print("══════════════════════════════════════════════════")
print("  Account: ClawBot Admin / Marcus")
print("══════════════════════════════════════════════════")
print("")
print("⚠️  IMPORTANT:")
print("   1. Scan the QR code NOW with Microsoft Authenticator")
print("   2. Verify a code works: run 'python3 -c \"import pyotp; t=pyotp.TOTP(open(os.path.expanduser(\\\"~/.clawbot-admin/totp.secret\\\")).read().strip()); print(t.now())\"'")
print("   3. The QR code is ONLY shown once (unless you re-run setup)")
print("   4. The secret is stored LOCALLY at ~/.clawbot-admin/totp.secret")
print("   5. It is NEVER transmitted over any network")
PYEOF

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Scan the QR code above with Microsoft Authenticator"
echo "  2. Add your admin credentials to ~/.clawbot-admin/"
echo "     See README.md for the required files"
echo "  3. Install the launchd service: see README.md"
echo ""
