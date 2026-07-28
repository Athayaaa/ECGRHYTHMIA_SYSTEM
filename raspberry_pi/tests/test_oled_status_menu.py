from __future__ import annotations

import json
import queue
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest import TestCase, mock

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import oled_status


class FakeRotary:
    def __init__(self, a, b, **kwargs):
        self.a = a
        self.b = b
        self.kwargs = kwargs
        self.when_rotated_clockwise = None
        self.when_rotated_counter_clockwise = None

    def close(self):
        pass


class FakeButton:
    def __init__(self, pin, **kwargs):
        self.pin = pin
        self.kwargs = kwargs
        self.when_pressed = None
        self.when_held = None
        self.when_released = None

    def close(self):
        pass


class FakeServices(oled_status.SystemServiceManager):
    def __init__(self):
        self.active: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.fail_on: str | None = None

    def exists(self, service: str) -> bool:
        return True

    def status(self, service: str) -> str:
        return "RUNNING" if service in self.active else "STOPPED"

    def is_active(self, service: str) -> bool:
        return service in self.active

    def start(self, service: str) -> oled_status.ServiceResult:
        self.calls.append(("start", service))
        if self.fail_on == service:
            return oled_status.ServiceResult(False, "ERROR", "boom")
        self.active.add(service)
        return oled_status.ServiceResult(True, "RUNNING", "OK")

    def stop(self, service: str) -> oled_status.ServiceResult:
        self.calls.append(("stop", service))
        self.active.discard(service)
        return oled_status.ServiceResult(True, "STOPPED", "OK")

    def restart(self, service: str) -> oled_status.ServiceResult:
        self.calls.append(("restart", service))
        if self.fail_on == service:
            return oled_status.ServiceResult(False, "ERROR", "boom")
        self.active.add(service)
        return oled_status.ServiceResult(True, "RUNNING", "OK")


class RotaryTests(TestCase):
    def test_gpio_mapping_and_pullups(self):
        events: queue.Queue[oled_status.InputEvent] = queue.Queue()
        rotary = oled_status.RotaryInput(
            events,
            rotary_cls=FakeRotary,
            button_cls=FakeButton,
            clock=lambda: 1.0,
        )

        self.assertEqual(rotary.encoder.a, 13)
        self.assertEqual(rotary.encoder.b, 19)
        self.assertEqual(rotary.button.pin, 26)
        self.assertTrue(rotary.button.kwargs["pull_up"])
        self.assertEqual(rotary.button.kwargs["bounce_time"], 0.05)
        self.assertEqual(rotary.button.kwargs["hold_time"], 1.5)

    def test_gpio_mapping_can_be_overridden(self):
        events: queue.Queue[oled_status.InputEvent] = queue.Queue()
        rotary = oled_status.RotaryInput(
            events,
            pin_a=13,
            pin_b=20,
            button_pin=26,
            rotary_cls=FakeRotary,
            button_cls=FakeButton,
            clock=lambda: 1.0,
        )

        self.assertEqual(rotary.encoder.a, 13)
        self.assertEqual(rotary.encoder.b, 20)
        self.assertEqual(rotary.button.pin, 26)

    def test_rotation_debounce_and_direction(self):
        events: queue.Queue[oled_status.InputEvent] = queue.Queue()
        now = [1.0]
        rotary = oled_status.RotaryInput(
            events,
            rotary_cls=FakeRotary,
            button_cls=FakeButton,
            clock=lambda: now[0],
        )

        rotary._clockwise()
        self.assertEqual(events.get_nowait().delta, 1)

        rotary._clockwise()
        self.assertTrue(events.empty())

        now[0] = 2.0
        rotary._counter_clockwise()
        self.assertEqual(events.get_nowait().delta, -1)

    def test_short_and_long_press_are_separate(self):
        events: queue.Queue[oled_status.InputEvent] = queue.Queue()
        rotary = oled_status.RotaryInput(
            events,
            rotary_cls=FakeRotary,
            button_cls=FakeButton,
            clock=lambda: 1.0,
        )

        rotary._button_pressed()
        rotary._button_released()
        self.assertEqual(events.get_nowait().kind, "select")

        rotary._button_pressed()
        rotary._button_held()
        rotary._button_released()
        self.assertEqual(events.get_nowait().kind, "back")
        self.assertTrue(events.empty())


class SessionControllerTests(TestCase):
    def test_start_creates_unique_session_and_orders_services(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            services = FakeServices()
            controller = oled_status.SessionController(
                services,
                session_root=root / "sessions",
                env_path=root / "runtime" / "current_session.env",
                now_func=lambda: datetime(2026, 7, 24, 10, 30, 5),
            )

            result = controller.start_new_session()

            self.assertTrue(result.ok)
            self.assertTrue((root / "runtime" / "current_session.env").is_file())
            session_dir = root / "sessions" / "session_24072026_103005"
            self.assertTrue((session_dir / "raw_adc").is_dir())
            self.assertTrue((session_dir / "model_ready").is_dir())
            self.assertIn("RAW_ADC_DIR", (root / "runtime" / "current_session.env").read_text())
            self.assertEqual(
                services.calls,
                [
                    ("start", oled_status.RECEIVER_SERVICE),
                    ("start", oled_status.AI_SERVICE),
                    ("start", oled_status.MQTT_SERVICE),
                ],
            )

    def test_start_does_not_create_double_receiver(self):
        with tempfile.TemporaryDirectory() as td:
            services = FakeServices()
            services.active.add(oled_status.RECEIVER_SERVICE)
            controller = oled_status.SessionController(
                services,
                session_root=Path(td) / "sessions",
                env_path=Path(td) / "current.env",
            )

            result = controller.start_new_session()

            self.assertFalse(result.ok)
            self.assertEqual(services.calls, [])

    def test_stop_order_and_session_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            services = FakeServices()
            controller = oled_status.SessionController(
                services,
                session_root=root / "sessions",
                env_path=root / "current.env",
            )
            controller.start_new_session()
            services.calls.clear()

            result = controller.stop_recording()

            self.assertTrue(result.ok)
            self.assertEqual(
                services.calls,
                [
                    ("stop", oled_status.MQTT_SERVICE),
                    ("stop", oled_status.AI_SERVICE),
                    ("stop", oled_status.RECEIVER_SERVICE),
                ],
            )
            self.assertTrue(any((root / "sessions").iterdir()))
            session_dir = next((root / "sessions").iterdir())
            metadata = json.loads((session_dir / "session.json").read_text())
            self.assertEqual(metadata["status"], "STOPPED")

    def test_service_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            services = FakeServices()
            services.fail_on = oled_status.AI_SERVICE
            controller = oled_status.SessionController(
                services,
                session_root=Path(td) / "sessions",
                env_path=Path(td) / "current.env",
            )

            result = controller.start_new_session()

            self.assertFalse(result.ok)
            self.assertEqual(result.title, "SERVICE ERROR")


class MenuTests(TestCase):
    def make_menu(self):
        services = FakeServices()
        controller = oled_status.SessionController(services)
        settings = oled_status.OledSettings()
        return oled_status.MenuController(controller, settings, services), controller

    def test_short_press_opens_menu_and_selects(self):
        menu, _controller = self.make_menu()
        menu.handle(oled_status.InputEvent("select"))
        self.assertEqual(menu.mode, "menu")
        menu.handle(oled_status.InputEvent("rotate", 1))
        self.assertEqual(menu.selected, 1)

    def test_long_press_goes_back(self):
        menu, _controller = self.make_menu()
        menu.open_menu()
        menu.menu_stack.append("services")
        menu.handle(oled_status.InputEvent("back"))
        self.assertEqual(menu.current_menu, "main")

    def test_reboot_requires_yes_and_long_press(self):
        menu, controller = self.make_menu()
        called = {"count": 0}

        def fake_reboot():
            called["count"] += 1
            return oled_status.ActionResult(True, "REBOOT", ["REBOOTING"])

        controller.reboot = fake_reboot
        menu.confirm("REBOOT?", "reboot")
        menu.handle(oled_status.InputEvent("rotate", 1))
        menu.handle(oled_status.InputEvent("select"))
        self.assertEqual(menu.mode, "hold_confirm")
        self.assertEqual(called["count"], 0)
        menu.handle(oled_status.InputEvent("select"))
        self.assertEqual(called["count"], 0)
        menu.handle(oled_status.InputEvent("back"))
        self.assertEqual(called["count"], 1)


class SnapshotSafetyTests(TestCase):
    def test_session_raw_adc_frames_update_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_adc = root / "raw_adc"
            raw_adc.mkdir()
            for frame_index in (1, 2):
                (raw_adc / f"frame_{frame_index:06d}.json").write_text(
                    json.dumps(
                        {
                            "frame_index": frame_index,
                            "sample_rate_hz": 250,
                            "parser_stats": {"crc_errors": 0},
                        }
                    ),
                    encoding="utf-8",
                )

            class Args:
                session_dir = str(root)
                data_root = str(root)

            with mock.patch.object(
                oled_status,
                "get_receiver_status",
                return_value={"running": True, "start_time": time.time()},
            ):
                snap = oled_status.build_snapshot(Args(), None, True)

            self.assertEqual(snap.frame_index, 2)
            self.assertEqual(snap.frame_count, 2)
            self.assertEqual(snap.state, "RECORDING")

    def test_build_snapshot_does_not_read_latest_prediction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "predictions").mkdir()
            (root / "predictions" / "latest_prediction.json").write_text(
                json.dumps({"prediction": "Normal"}),
                encoding="utf-8",
            )
            (root / "frame_000001.json").write_text(
                json.dumps({"frame_index": 1, "sample_rate_hz": 250}),
                encoding="utf-8",
            )

            class Args:
                session_dir = str(root)
                data_root = str(root)

            read_paths: list[str] = []
            real_read_json = oled_status._read_json

            def spy(path):
                read_paths.append(str(path))
                return real_read_json(path)

            with mock.patch.object(oled_status, "_read_json", side_effect=spy):
                snap = oled_status.build_snapshot(Args(), None, True)

            self.assertEqual(snap.frame_index, 1)
            self.assertFalse(any("latest_prediction.json" in path for path in read_paths))

    def test_safe_source_guard(self):
        oled_status.assert_safe_source()
