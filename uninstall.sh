#!/usr/bin/env bash
#
# Remove the LeChacal -> Home Assistant MQTT bridge service.
# Run as root:  sudo ./uninstall.sh
#
set -euo pipefail

SERVICE_NAME="lechacal-mqtt"
SERVICE_USER="lechacal"
INSTALL_DIR="/opt/lechacal-mqtt"
CONFIG_DIR="/etc/lechacal-mqtt"
STATE_DIR="/var/lib/lechacal-mqtt"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This must be run as root. Try: sudo ./uninstall.sh" >&2
    exit 1
fi

echo "==> Stopping and disabling service..."
systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
rm -f "${UNIT_PATH}"
systemctl daemon-reload

echo "==> Removing ${INSTALL_DIR} and ${STATE_DIR}..."
rm -rf "${INSTALL_DIR}" "${STATE_DIR}"

read -r -p "Also remove config at ${CONFIG_DIR}? [y/N] " reply
if [[ "${reply}" =~ ^[Yy]$ ]]; then
    rm -rf "${CONFIG_DIR}"
    echo "    Removed ${CONFIG_DIR}."
fi

read -r -p "Also remove the '${SERVICE_USER}' system user? [y/N] " reply
if [[ "${reply}" =~ ^[Yy]$ ]]; then
    userdel "${SERVICE_USER}" 2>/dev/null || true
    echo "    Removed user ${SERVICE_USER}."
fi

echo "Done."
