import os
import subprocess
import sys
import pytest
from unittest.mock import Mock

from visualizers import launcher


@pytest.mark.parametrize(
    "platform, session, desktop, display, override, expected",
    [
        ("linux", "wayland", "GNOME", ":0", None, "xcb"),
        ("linux", "wayland", "ubuntu:GNOME", ":1", None, "xcb"),
        ("linux", "WAYLAND", "gnome", ":0", None, "xcb"),
        ("linux", "wayland", "GNOME", ":0", "wayland", "wayland"),
        ("linux", "wayland", "GNOME", ":0", "offscreen", "offscreen"),
        ("linux", "wayland", "GNOME", ":0", "xcb;wayland", "xcb;wayland"),
        ("linux", "wayland", "GNOME", ":0", "", ""),
        ("linux", "wayland", "KDE", ":0", None, None),
        ("linux", "wayland", "", ":0", None, None),
        ("linux", "wayland", "GNOME", "", None, None),
        ("linux", "x11", "GNOME", ":0", None, None),
        ("linux", "tty", "", "", None, None),
        ("darwin", "wayland", "GNOME", ":0", None, None),
        ("win32", "wayland", "GNOME", ":0", None, None),
    ],
)
def test_configure_qt_platform(
    monkeypatch, platform, session, desktop, display, override, expected
):
    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    if override is not None:
        monkeypatch.setenv("QT_QPA_PLATFORM", override)

    launcher.configure_qt_platform()
    launcher.configure_qt_platform()

    assert os.environ.get("QT_QPA_PLATFORM") == expected


@pytest.mark.parametrize("entrypoint", ["shared", "illustration"])
def test_platform_is_selected_before_application_creation(monkeypatch, entrypoint):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    application = Mock()
    application.exec_.return_value = 0

    def create_application(arguments):
        assert os.environ["QT_QPA_PLATFORM"] == "xcb"
        assert "XDG_SESSION_TYPE" not in os.environ
        return application

    application_class = Mock(side_effect=create_application)
    application_class.instance.return_value = None
    monkeypatch.setattr(launcher, "QApplication", application_class)
    monkeypatch.setattr(launcher, "raise_existing_window", lambda name: False)
    monkeypatch.setattr(launcher.theme, "apply_theme", Mock())
    monkeypatch.setattr(launcher, "install_keyboard_manager", Mock())
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: default)
    monkeypatch.setattr("tool.prefs.set", Mock())
    monkeypatch.setattr(
        "visualizers.components.singleton_guard.claim_or_ping_and_bind",
        lambda *arguments: True,
    )
    window = Mock()
    if entrypoint == "illustration":
        monkeypatch.setattr(
            "visualizers.illustration_visualizer.IllustrationWindow",
            lambda *arguments, **options: window,
        )

    with pytest.raises(SystemExit) as exit_info:
        if entrypoint == "shared":
            launcher.run_visualizer_window("project", lambda: window)
        else:
            launcher.launch_visualizer("illustration", "/unused")

    assert exit_info.value.code == 0
    application_class.assert_called_once_with(launcher.sys.argv)
    window.show.assert_called_once_with()
    assert os.environ["XDG_SESSION_TYPE"] == "wayland"


def test_existing_application_does_not_reconfigure_platform(monkeypatch):
    application = Mock()
    application_class = Mock()
    application_class.instance.return_value = application
    monkeypatch.setattr(launcher, "QApplication", application_class)
    configure = Mock()
    monkeypatch.setattr(launcher, "configure_qt_platform", configure)
    monkeypatch.setattr(launcher, "install_keyboard_manager", Mock())
    monkeypatch.setattr("tool.prefs.get", lambda key, default=None: default)
    monkeypatch.setattr("tool.prefs.set", Mock())
    monkeypatch.setattr(
        "visualizers.components.singleton_guard.claim_or_ping_and_bind",
        lambda *arguments: True,
    )
    window = Mock()

    assert launcher.run_visualizer_window(
        "project", lambda: window, check_existing=False
    ) is window

    configure.assert_not_called()
    application_class.assert_not_called()


def test_session_hint_restored_when_application_creation_fails(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher, "QApplication", Mock(side_effect=RuntimeError("failed")))

    with pytest.raises(RuntimeError, match="failed"):
        launcher.create_qt_application([])

    assert os.environ["XDG_SESSION_TYPE"] == "wayland"
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"


@pytest.mark.parametrize("desktop", ["ubuntu:GNOME", "KDE"])
def test_real_qt_startup_preserves_override_without_gnome_warning(desktop):
    environment = dict(
        os.environ,
        XDG_SESSION_TYPE="wayland",
        XDG_CURRENT_DESKTOP=desktop,
        QT_QPA_PLATFORM="offscreen",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from visualizers.launcher import create_qt_application; "
            "import os; application = create_qt_application([]); "
            "assert application.platformName() == 'offscreen'; "
            "assert os.environ['QT_QPA_PLATFORM'] == 'offscreen'; "
            "assert os.environ['XDG_SESSION_TYPE'] == 'wayland'",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "Ignoring XDG_SESSION_TYPE=wayland" not in result.stderr