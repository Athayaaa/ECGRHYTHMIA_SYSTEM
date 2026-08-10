#!/usr/bin/env bash
# Build MQTT publisher arguments for the session created from the OLED menu.

set -euo pipefail

: "${MQTT_BROKER:?MQTT_BROKER is required}"
: "${MQTT_PORT:?MQTT_PORT is required}"
: "${MQTT_TOPIC:?MQTT_TOPIC is required}"
: "${MQTT_DEVICE_ID:?MQTT_DEVICE_ID is required}"
: "${CALIBRATED_DIR:?CALIBRATED_DIR is required; start a session from the OLED menu}"
: "${PREDICTION_DIR:?PREDICTION_DIR is required; start a session from the OLED menu}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A filtered directory exists only when the receiver notch export is enabled.
ECG_DIR="${FILTERED_DIR:-$CALIBRATED_DIR}"
args=(
  --ecg-dir "$ECG_DIR"
  --prediction-dir "$PREDICTION_DIR"
  --broker "$MQTT_BROKER"
  --port "$MQTT_PORT"
  --publish-topic "$MQTT_TOPIC"
  --device-id "$MQTT_DEVICE_ID"
  --username-env "${MQTT_USERNAME_ENV:-HIVEMQ_USERNAME}"
  --password-env "${MQTT_PASSWORD_ENV:-HIVEMQ_PASSWORD}"
  --client-id "${MQTT_CLIENT_ID:-ecgrhythmia-device01-publisher}"
  --state-file "$PREDICTION_DIR/mqtt_publish_state.json"
  --follow
)

case "${MQTT_TLS:-0}" in
  1|true|TRUE|yes|YES) args+=(--tls) ;;
esac

case "${MQTT_JSON_TOPIC_EXACT:-0}" in
  1|true|TRUE|yes|YES) args+=(--json-topic-exact) ;;
esac

exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/mqtt_publisher.py" "${args[@]}"
