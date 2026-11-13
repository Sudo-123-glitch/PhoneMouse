# ABOUTME: Tests for the macOS input path of PhoneMouse and the routing of phone messages.
# ABOUTME: Uses a recording stand-in for the operating system, so no real cursor or key moves.

import unittest

import phone_mouse as pm

K = pm.MAC_KEYCODE
CMD = pm.MAC_FLAG["cmd"]


class RecordingOS:
    """Stands in for the macOS event calls and records what would be posted."""

    def __init__(self, cursor=(100.0, 100.0)):
        self.events = []
        self.pos = cursor

    def cursor(self):
        return self.pos

    def main_display(self):
        return (0.0, 0.0, 1000.0, 600.0)

    def desktop(self):
        return (0.0, 0.0, 2000.0, 600.0)   # a second screen to the right

    def post_mouse(self, kind, x, y, clicks=1):
        self.events.append(("mouse", kind, x, y, clicks))
        self.pos = (x, y)

    def post_scroll(self, pixels):
        self.events.append(("scroll", pixels))

    def post_key(self, code, down, flags):
        self.events.append(("key", code, down, flags))

    def post_text(self, ch):
        self.events.append(("text", ch))

    def post_media(self, name):
        self.events.append(("media", name))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def mac(cursor=(100.0, 100.0)):
    os_, clock = RecordingOS(cursor), Clock()
    return pm.MacInput(os_, clock), os_, clock


class MacShortcuts(unittest.TestCase):
    def test_ctrl_shortcuts_use_command(self):
        self.assertEqual(pm.mac_keys(["ctrl", "c"]), ["cmd", "c"])

    def test_alt_tab_switches_apps_with_command_tab(self):
        self.assertEqual(pm.mac_keys(["alt", "tab"]), ["cmd", "tab"])

    def test_win_key_opens_spotlight(self):
        self.assertEqual(pm.mac_keys(["win"]), ["cmd", "space"])

    def test_win_d_shows_desktop(self):
        self.assertEqual(pm.mac_keys(["win", "d"]), ["f11"])

    def test_f5_reloads_with_command_r(self):
        self.assertEqual(pm.mac_keys(["f5"]), ["cmd", "r"])

    def test_plain_keys_are_unchanged(self):
        self.assertEqual(pm.mac_keys(["enter"]), ["enter"])


class MacMouse(unittest.TestCase):
    def test_move_shifts_cursor_by_delta(self):
        dev, os_, _ = mac()
        dev.move(10, -5)
        self.assertEqual(os_.events, [("mouse", "move", 110.0, 95.0, 1)])

    def test_movement_smaller_than_a_point_still_moves_the_cursor(self):
        dev, os_, _ = mac()
        dev.move(0.25, 0)
        self.assertEqual(os_.events, [("mouse", "move", 100.25, 100.0, 1)])

    def test_quick_moves_build_on_each_other_when_the_system_cursor_lags(self):
        dev, os_, _ = mac()
        os_.post_mouse = lambda kind, x, y, clicks=1: os_.events.append(("mouse", kind, x, y, clicks))
        dev.move(10, 0)
        dev.move(10, 0)   # the system still reports x=100 here
        self.assertEqual(os_.events[-1], ("mouse", "move", 120.0, 100.0, 1))

    def test_after_a_pause_movement_starts_from_the_real_cursor(self):
        dev, os_, clock = mac()
        dev.move(10, 0)
        os_.pos = (500.0, 500.0)   # the user moved the real mouse meanwhile
        clock.t = 1.0
        dev.move(10, 0)
        self.assertEqual(os_.events[-1], ("mouse", "move", 510.0, 500.0, 1))

    def test_move_with_left_button_held_is_a_drag(self):
        dev, os_, _ = mac()
        dev.button("l", True)
        dev.move(10, 0)
        self.assertEqual(os_.events[-1], ("mouse", "ldrag", 110.0, 100.0, 1))

    def test_move_stops_at_the_edge_of_the_desktop(self):
        dev, os_, _ = mac(cursor=(1995.0, 5.0))
        dev.move(50, -50)
        self.assertEqual(os_.events, [("mouse", "move", 1999.0, 0.0, 1)])

    def test_laser_centre_is_the_middle_of_the_main_screen(self):
        dev, os_, _ = mac()
        dev.move_abs(0, 0)
        self.assertEqual(os_.events, [("mouse", "move", 500.0, 300.0, 1)])

    def test_laser_uses_the_same_scale_both_ways(self):
        dev, os_, _ = mac()
        dev.move_abs(0.5, 0.5)
        self.assertEqual(os_.events, [("mouse", "move", 750.0, 550.0, 1)])

    def test_click_presses_and_releases_at_the_cursor(self):
        dev, os_, _ = mac()
        dev.click("r")
        self.assertEqual(os_.events, [("mouse", "rdown", 100.0, 100.0, 1),
                                      ("mouse", "rup", 100.0, 100.0, 1)])

    def test_two_quick_clicks_count_as_a_double_click(self):
        dev, os_, clock = mac()
        dev.click("l")
        clock.t = 0.2
        dev.click("l")
        self.assertEqual([e[4] for e in os_.events], [1, 1, 2, 2])

    def test_two_slow_clicks_are_separate_clicks(self):
        dev, os_, clock = mac()
        dev.click("l")
        clock.t = 2.0
        dev.click("l")
        self.assertEqual([e[4] for e in os_.events], [1, 1, 1, 1])

    def test_wheel_scrolls_in_pixels_and_keeps_the_remainder(self):
        dev, os_, _ = mac()
        dev.wheel(1)
        self.assertEqual(os_.events, [])
        dev.wheel(119)
        self.assertEqual(os_.events, [("scroll", int(120 * pm.MAC_WHEEL_TO_PIXELS))])


class MacKeyboard(unittest.TestCase):
    def test_tap_presses_and_releases_the_key(self):
        dev, os_, _ = mac()
        dev.tap("enter")
        self.assertEqual(os_.events, [("key", K["enter"], True, 0), ("key", K["enter"], False, 0)])

    def test_combo_holds_the_modifier_around_the_key(self):
        dev, os_, _ = mac()
        dev.combo(["ctrl", "c"])
        self.assertEqual(os_.events, [("key", K["cmd"], True, CMD), ("key", K["c"], True, CMD),
                                      ("key", K["c"], False, CMD), ("key", K["cmd"], False, 0)])

    def test_volume_key_is_sent_as_a_media_key(self):
        dev, os_, _ = mac()
        dev.tap("volup")
        self.assertEqual(os_.events, [("media", "volup")])

    def test_text_types_characters_and_turns_newline_into_enter(self):
        dev, os_, _ = mac()
        dev.text("a\nb")
        self.assertEqual(os_.events, [("text", "a"), ("key", K["enter"], True, 0),
                                      ("key", K["enter"], False, 0), ("text", "b")])

    def test_every_key_the_phone_can_send_is_supported(self):
        for name in pm.VK:
            dev, os_, _ = mac()
            dev.tap(name)
            self.assertTrue(os_.events, "nothing sent for %r" % name)


class Routing(unittest.TestCase):
    def test_combo_message_reaches_the_device_as_one_combo(self):
        dev, os_, _ = mac()
        pm.apply_message(dev, {"ev": [["combo", ["ctrl", "v"]]]})
        self.assertEqual(os_.events[1], ("key", K["v"], True, CMD))

    def test_mac_is_chosen_on_macos_and_dry_run_wins(self):
        self.assertEqual(pm.device_kind("darwin", False), "mac")
        self.assertEqual(pm.device_kind("win32", False), "windows")
        self.assertEqual(pm.device_kind("linux", False), "dry")
        self.assertEqual(pm.device_kind("darwin", True), "dry")

    def test_page_tells_the_phone_when_the_computer_is_a_mac(self):
        self.assertIn('const HOST = "mac";', pm.page_html("mac"))
        self.assertIn('const HOST = "windows";', pm.page_html("windows"))


if __name__ == "__main__":
    unittest.main()
