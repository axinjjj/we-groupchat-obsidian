import unittest
from unittest.mock import Mock, patch

from ui import windows_rumps as rumps


class WindowsRumpsTests(unittest.TestCase):
    def test_menu_supports_existing_app_mutation_contract(self):
        menu = rumps.Menu([
            rumps.MenuItem("first"),
            rumps.separator,
            rumps.MenuItem("last"),
        ])

        menu.insert_after("first", rumps.MenuItem("middle"))
        menu.insert_before("last", rumps.MenuItem("before-last"))
        del menu["middle"]

        self.assertEqual(menu.keys(), ["first", "before-last", "last"])

    def test_menu_item_keeps_nested_titles_and_callbacks(self):
        callback = Mock()
        parent = rumps.MenuItem("parent")
        child = rumps.MenuItem("child", callback=callback)
        parent.add(child)

        self.assertEqual(parent.keys(), ["child"])
        self.assertIs(parent["child"].callback, callback)

    def test_timer_stop_is_idempotent(self):
        timer = rumps.Timer(lambda _timer: None, 1)

        timer.stop()
        timer.stop()

    def test_secure_window_records_masked_input_contract(self):
        window = rumps.Window("message", secure=True)

        self.assertTrue(window.secure)

    def test_notification_before_tray_start_is_queued(self):
        before = len(rumps._pending_notifications)

        rumps.notification("title", "subtitle", "message")

        self.assertEqual(len(rumps._pending_notifications), before + 1)
        rumps._pending_notifications.pop()

    def test_native_pystray_menu_builds_from_nested_rumps_shape(self):
        app = rumps.App("WGO", quit_button="退出")
        parent = rumps.MenuItem("parent")
        parent.add(rumps.MenuItem("child", callback=lambda _item: None))
        app.menu = [parent, rumps.separator, rumps.MenuItem("info")]

        native = app._build_native_menu()

        self.assertGreaterEqual(len(native.items), 5)

    def test_run_constructs_windows_tray_backend(self):
        app = rumps.App("WGO")
        app.menu = [rumps.MenuItem("action", callback=lambda _item: None)]

        with patch("pystray.Icon.run") as run:
            app.run()

        run.assert_called_once()
        self.assertTrue(callable(run.call_args.kwargs["setup"]))


if __name__ == "__main__":
    unittest.main()
