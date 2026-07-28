#!/usr/bin/env bash
# Build MQTT publisher arguments from systemd EnvironmentFile values.

set -euo pipefail

: "${CALIBRATED_DIR:?current session is missing CALIBRATED_DIR}"
: "${PREDICTION_DIR:?current session is missing PREDICTION_DIR}"
: "${MQTT_BROKER:?MQTT_BROKER is required}"
: "${MQTT_PORT:?MQTT_PORT is required}"
: "${MQTT_TOPIC:?MQTT_TOPIC is required}"
: "${MQTT_DEVICE_ID:?MQTT_DEVICE_ID is required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
args=(
  --ecg-dir "$CALIBRATED_DIR"
  --prediction-dir "$PREDICTION_DIR"
  --broker "$MQTT_BROKER"
  --port "$MQTT_PORT"
  --topic "$MQTT_TOPIC"
  --device-id "$MQTT_DEVICE_ID"
  --follow
)

if [[ -n "${MQTT_USERNAME:-}" ]]; then
  : "${MQTT_PASSWORD:?MQTT_PASSWORD is required when MQTT_USERNAME is set}"
  args+=(--username "$MQTT_USERNAME" --password-env MQTT_PASSWORD)
fi

case "${MQTT_TLS:-0}" in
  1|true|TRUE|yes|YES) args+=(--tls) ;;
esac

exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/mqtt_publisher.py" "${args[@]}"
