from __future__ import annotations

import importlib
from pathlib import Path
import sys
import tempfile
import types
from unittest import TestCase, mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

serial_stub = types.ModuleType("serial")
serial_stub.Serial = object
serial_stub.SerialException = OSError
with mock.patch.dict(sys.modules, {"serial": serial_stub}):
    receiver = importlib.import_module("receiver")


class ReceiverConfigTests(TestCase):
    def test_local_config_overrides_nested_values_without_losing_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.yaml"
            config_path.write_text(
                "serial:\n  port: /dev/ttyUSB0\n  baudrate: 115200\n"
                "device:\n  device_id: base\n  state: stored\n",
                encoding="utf-8",
            )
            receiver.local_config_path(config_path).write_text(
                "serial:\n  port: /dev/ttyACM0\n"
                "device:\n  state: testing\n",
                encoding="utf-8",
            )

            config = receiver.load_config(config_path)

            self.assertEqual(config["serial"]["port"], "/dev/ttyACM0")
            self.assertEqual(config["serial"]["baudrate"], 115200)
            self.assertEqual(config["device"]["device_id"], "base")
            self.assertEqual(config["device"]["state"], "testing")

    def test_invalid_local_config_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.yaml"
            config_path.write_text("serial: {}\n", encoding="utf-8")
            receiver.local_config_path(config_path).write_text("- invalid\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Configuration root"):
                receiver.load_config(config_path)
