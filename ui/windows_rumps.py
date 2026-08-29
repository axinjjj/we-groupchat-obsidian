"""Small rumps-compatible Windows tray adapter used by the canonical app.

The application keeps its existing menu construction and callbacks.  This
module only translates that UI surface to pystray/tkinter on Windows.
"""
from __future__ import annotations

import threading
import traceback
from types import SimpleNamespace


class _Separator:
    pass


separator = _Separator()
_active_app = None
_notification_handler = None
_pending_notifications = []
_notification_lock = threading.Lock()


class Menu:
    def __init__(self, items=None, *, owner=None):
        self._items = []
        self._owner = owner
        for item in items or []:
            self.add(item, notify=False)

    def _set_owner(self, owner):
        self._owner = owner
        for item in self._items:
            if isinstance(item, MenuItem):
                item._menu._set_owner(owner)

    def _changed(self):
        if self._owner is not None:
            self._owner._menu_changed()

    @staticmethod
    def _title(item):
        return item.title if isinstance(item, MenuItem) else None

    def _index(self, title):
        for index, item in enumerate(self._items):
            if self._title(item) == title:
                return index
        raise KeyError(title)

    def add(self, item, *, notify=True):
        if item is not separator and not isinstance(item, MenuItem):
            item = MenuItem(str(item))
        if isinstance(item, MenuItem):
            item._menu._set_owner(self._owner)
        self._items.append(item)
        if notify:
            self._changed()
        return item

    def insert_before(self, title, item):
        index = self._index(title)
        if item is not separator and not isinstance(item, MenuItem):
            item = MenuItem(str(item))
        if isinstance(item, MenuItem):
            item._menu._set_owner(self._owner)
        self._items.insert(index, item)
        self._changed()

    def insert_after(self, title, item):
        index = self._index(title) + 1
        if item is not separator and not isinstance(item, MenuItem):
            item = MenuItem(str(item))
        if isinstance(item, MenuItem):
            item._menu._set_owner(self._owner)
        self._items.insert(index, item)
        self._changed()

    def keys(self):
        return [
            item.title for item in self._items if isinstance(item, MenuItem)
        ]

    def __contains__(self, title):
        return title in self.keys()

    def __getitem__(self, title):
        return self._items[self._index(title)]

    def __delitem__(self, title):
        del self._items[self._index(title)]
        self._changed()

    def __iter__(self):
        return iter(self._items)


class MenuItem:
    def __init__(self, title, callback=None, **_kwargs):
        self.title = str(title)
        self.callback = callback
        self._menu = Menu()

    def add(self, item):
        return self._menu.add(item)

    def keys(self):
        return self._menu.keys()

    def __contains__(self, title):
        return title in self._menu

    def __getitem__(self, title):
        return self._menu[title]

    def __delitem__(self, title):
        del self._menu[title]


class Timer:
    def __init__(self, callback, interval):
        self.callback = callback
        self.interval = max(0.01, float(interval))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.callback(self)
            except Exception:
                traceback.print_exc()

    def stop(self):
        self._stop.set()


class Window:
    def __init__(
        self,
        message="",
        title="",
        default_text="",
        ok="OK",
        cancel="Cancel",
        dimensions=(300, 24),
        secure=False,
        **_kwargs,
    ):
        self.message = str(message)
        self.title = str(title)
        self.default_text = str(default_text)
        self.ok = str(ok)
        self.cancel = str(cancel)
        self.dimensions = dimensions
        self.secure = bool(secure)

    def run(self):
        import tkinter as tk
        from tkinter import messagebox, simpledialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if tuple(self.dimensions) == (0, 0):
                clicked = messagebox.askokcancel(
                    self.title,
                    self.message,
                    parent=root,
                )
                return SimpleNamespace(clicked=bool(clicked), text="")
            options = {
                "initialvalue": self.default_text,
                "parent": root,
            }
            if self.secure:
                options["show"] = "*"
            value = simpledialog.askstring(self.title, self.message, **options)
            return SimpleNamespace(clicked=value is not None, text=value or "")
        finally:
            root.destroy()


def clicked(_title):
    def decorate(function):
        return function

    return decorate


def notifications(function):
    global _notification_handler
    _notification_handler = function
    return function


def notification(title, subtitle, message, data=None):
    del data  # pystray notifications do not expose a portable click payload.
    body = f"{subtitle}\n{message}" if subtitle else str(message)
    if (
        _active_app is None
        or _active_app._icon is None
        or not _active_app._notification_ready.is_set()
    ):
        with _notification_lock:
            _pending_notifications.append((str(title), body))
            del _pending_notifications[:-20]
        return
    _active_app._icon.notify(body, str(title))


class App:
    def __init__(
        self,
        name,
        title=None,
        icon=None,
        template=False,
        quit_button="Quit",
        **_kwargs,
    ):
        del template
        self.name = str(name)
        self.title = str(title or name)
        self.icon = icon
        self.quit_button = str(quit_button or "Quit")
        self._menu = Menu(owner=self)
        self._icon = None
        self._menu_lock = threading.RLock()
        self._notification_ready = threading.Event()

    @property
    def menu(self):
        return self._menu

    @menu.setter
    def menu(self, items):
        menu = items if isinstance(items, Menu) else Menu(items)
        menu._set_owner(self)
        self._menu = menu
        self._menu_changed()

    def _menu_changed(self):
        with self._menu_lock:
            if self._icon is not None:
                try:
                    self._icon.menu = self._build_native_menu()
                    self._icon.update_menu()
                except Exception:
                    traceback.print_exc()

    def _native_callback(self, item):
        def invoke(_icon, _native_item):
            try:
                if callable(item.callback):
                    item.callback(item)
            except Exception:
                traceback.print_exc()
            finally:
                self._menu_changed()

        return invoke

    def _native_item(self, pystray, item):
        if item is separator:
            return pystray.Menu.SEPARATOR
        submenu = None
        if item._menu._items:
            submenu = pystray.Menu(*[
                self._native_item(pystray, child) for child in item._menu
            ])
        action = submenu if submenu is not None else self._native_callback(item)
        return pystray.MenuItem(item.title, action, enabled=bool(submenu or item.callback))

    def _build_native_menu(self):
        import pystray

        native = [self._native_item(pystray, item) for item in self._menu]
        native.extend((
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self.quit_button, lambda icon, _item: icon.stop()),
        ))
        return pystray.Menu(*native)

    def run(self):
        global _active_app
        import pystray
        from PIL import Image

        image_path = self.icon
        if not image_path:
            from pathlib import Path

            image_path = str(
                Path(__file__).resolve().parents[1] / "resources" / "app_icon.png"
            )
        with Image.open(image_path) as source_image:
            image = source_image.copy()
        self._icon = pystray.Icon(
            "we-groupchat-obsidian",
            image,
            self.title,
            self._build_native_menu(),
        )
        _active_app = self
        self._notification_ready.clear()

        def setup(icon):
            icon.visible = True
            self._notification_ready.set()
            with _notification_lock:
                pending = list(_pending_notifications)
                _pending_notifications.clear()
            for title, body in pending:
                try:
                    icon.notify(body, title)
                except Exception:
                    traceback.print_exc()
        try:
            self._icon.run(setup=setup)
        finally:
            self._notification_ready.clear()
            _active_app = None
            self._icon = None
