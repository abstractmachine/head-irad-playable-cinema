# Visualizers

A series of "visualizers" exist to better visualize the data created via the `crossing` tool CLI/MCP commands. A central `Project` visualizer contains core project settings as well as buttons to open all the other visualizers.

## Linux Display Backend

On GNOME Wayland sessions with an X display available, visualizers explicitly
select Qt's `xcb` backend (XWayland). This preserves Qt 5's existing GNOME
behavior without its implicit-backend warning; it does not force native Wayland
or filter warning output. Other desktops and sessions retain Qt's defaults.

Qt 5 emits the GNOME message before reading even an explicit platform setting.
When a backend is explicitly selected on GNOME Wayland, startup temporarily
omits `XDG_SESSION_TYPE` during Qt application construction to bypass that
redundant auto-detection, then immediately restores the original session value.

Any existing `QT_QPA_PLATFORM` setting is preserved. Native Wayland remains an
explicit opt-in, for installations with a working Qt Wayland plugin and rendering:

```bash
QT_QPA_PLATFORM=wayland crossing visualizer
```

## Core

- [Project](visualizer-project.md)
- [Metadata](visualizer-metadata.md)
- [Shotlist](visualizer-shotlist.md)
- [Mosaic](visualizer-mosaic.md)

## Secondary
- [Book](visualizer-book.md)
- [Cloud](visualizer-cloud.md)
- [Flipbook](visualizer-flipbook.md)
- [Palette](visualizer-palette.md)
- [Illustration](visualizer-illustration.md)

## Shared Inspector Layout

- [Inspector Grid Contract](inspector-grid.md)