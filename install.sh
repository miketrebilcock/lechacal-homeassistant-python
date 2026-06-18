#!/usr/bin/env bash
#
# Install the LeChacal -> Home Assistant MQTT bridge as a systemd service.
# Run on the Raspberry Pi (or any Debian/Ubuntu host) as root:
#
#     sudo ./install.sh
#
set -euo pipefail

SERVICE_NAME="lechacal-mqtt"
SERVICE_USER="lechacal"
INSTALL_DIR="/opt/lechacal-mqtt"
CONFIG_DIR="/etc/lechacal-mqtt"
STATE_DIR="/var/lib/lechacal-mqtt"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer must be run as root. Try: sudo ./install.sh" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer expects a Debian/Ubuntu system with apt-get." >&2
    echo "Install python3 + python3-venv manually, then adapt this script." >&2
    exit 1
fi

echo "==> Installing system dependencies..."
apt-get update
apt-get install -y python3 python3-venv python3-pip

echo "==> Creating service user '${SERVICE_USER}'..."
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
# Needs dialout for serial port access.
usermod -aG dialout "${SERVICE_USER}"

echo "==> Installing application to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
install -m 0644 "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
rm -rf "${INSTALL_DIR}/device-mapping"
cp -r "${SCRIPT_DIR}/device-mapping" "${INSTALL_DIR}/device-mapping"

echo "==> Creating Python virtualenv..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Creating state directory ${STATE_DIR}..."
mkdir -p "${STATE_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}"

echo "==> Installing configuration to ${CONFIG_DIR}..."
mkdir -p "${CONFIG_DIR}"
if [[ -f "${CONFIG_DIR}/config.yml" ]]; then
    echo "    Existing ${CONFIG_DIR}/config.yml left untouched."
else
    install -m 0644 "${SCRIPT_DIR}/config.yml.example" "${CONFIG_DIR}/config.yml"
    echo "    Created ${CONFIG_DIR}/config.yml from the example - EDIT THIS."
fi

echo "==> Installing systemd service..."
install -m 0644 "${SCRIPT_DIR}/${SERVICE_NAME}.service" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Done. The ${SERVICE_NAME} service is installed and running."
echo
echo "Next steps:"
echo "  1. Edit your settings:   sudo nano ${CONFIG_DIR}/config.yml"
echo "  2. Apply changes:        sudo systemctl restart ${SERVICE_NAME}"
echo "  3. Watch the logs:       journalctl -u ${SERVICE_NAME} -f"
echo
systemctl --no-pager status "${SERVICE_NAME}.service" || true
