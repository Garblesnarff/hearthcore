#!/usr/bin/env python3
"""Azeroth Watch — lightweight GTK3 + WebKit2 fullscreen kiosk viewer for the Acer monitor."""

import argparse
import os
import sys

# Keep WebKit compositor lean on Intel iGPU / Wayland
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "0")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gtk, WebKit2  # noqa: E402


class AzerothWatchWindow(Gtk.Window):
    def __init__(self, url: str, fullscreen: bool = True):
        super().__init__(title="Azeroth Watch — Hearthguard")
        self.set_default_size(1440, 900)
        self._is_fullscreen = fullscreen

        # Dark background immediately before first paint (zero white flash)
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background-color: #0a0d12; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Ephemeral web context -> zero disk cache growth, in-RAM image cache prevents icon/map flicker
        ctx = WebKit2.WebContext.new_ephemeral()
        ctx.set_cache_model(WebKit2.CacheModel.WEB_BROWSER)

        self.webview = WebKit2.WebView.new_with_context(ctx)
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(False)
        settings.set_enable_webgl(False)
        settings.set_enable_page_cache(False)

        bg = Gdk.RGBA()
        bg.parse("#0a0d12")
        self.webview.set_background_color(bg)
        self.webview.load_uri(url)

        self.add(self.webview)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_key)

        self.show_all()
        if fullscreen:
            self.fullscreen()

    def _on_key(self, _widget, event):
        key = Gdk.keyval_name(event.keyval)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if key == "F11":
            if self._is_fullscreen:
                self.unfullscreen()
                self._is_fullscreen = False
            else:
                self.fullscreen()
                self._is_fullscreen = True
            return True
        if key == "Escape" and self._is_fullscreen:
            self.unfullscreen()
            self._is_fullscreen = False
            return True
        if ctrl and key in ("q", "Q"):
            Gtk.main_quit()
            return True
        if key in ("F5",) or (ctrl and key in ("r", "R")):
            self.webview.reload_bypass_cache()
            return True
        return False


def main():
    parser = argparse.ArgumentParser(description="Azeroth Watch Kiosk")
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--windowed", action="store_true", help="Start windowed instead of fullscreen")
    args = parser.parse_args()

    AzerothWatchWindow(args.url, fullscreen=not args.windowed)
    Gtk.main()


if __name__ == "__main__":
    main()
