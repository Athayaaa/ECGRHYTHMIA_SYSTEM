#!/usr/bin/env bash
# Keep the remote START/STOP command listener alive independently of OLED sessions.

set -euo pipefail

: "${MQTT_BROKER:?MQTT_BROKER is required}"
: "${MQTT_PORT:?MQTT_PORT is required}"
: "${MQTT_TOPIC:?MQTT_TOPIC is required}"
: "${MQTT_DEVICE_ID:?MQTT_DEVICE_ID is required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
args=(
  --broker "$MQTT_BROKER"
  --port "$MQTT_PORT"
  --publish-topic "$MQTT_TOPIC"
  --command-topic "${MQTT_COMMAND_TOPIC:-${MQTT_TOPIC%/}/command}"
  --device-id "$MQTT_DEVICE_ID"
  --username-env "${MQTT_USERNAME_ENV:-HIVEMQ_USERNAME}"
  --password-env "${MQTT_PASSWORD_ENV:-HIVEMQ_PASSWORD}"
  --client-id "${MQTT_CLIENT_ID:-ecgrhythmia-device01-controller}"
  --controller
)

case "${MQTT_TLS:-0}" in
  1|true|TRUE|yes|YES) args+=(--tls) ;;
esac

case "${MQTT_JSON_TOPIC_EXACT:-0}" in
  1|true|TRUE|yes|YES) args+=(--json-topic-exact) ;;
esac

exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/mqtt_publisher.py" "${args[@]}"
