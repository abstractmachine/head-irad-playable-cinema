# Shotlist

![Shotlist Visualizer screenshot](./images/visualizers/visualizer-shotlist.png)

The `Shotlist` visualizer is the primary tool for reviewing, editing, and annotating shot lists. It combines a frame-accurate video player, an annotation panel, and a scene/shot table in one window.

Open it with:

```
crossing visualizer shotlist
```

## Layout

### Left — Video Player

Displays the current frame of the active shot. A timeline scrubber at the bottom lets you seek within the clip. The title bar shows the film name, scene number, and total shot count.

### Middle — Annotation Panel

Displays and edits the structured annotation fields for the selected shot:

| Field | Description |
|---|---|
| `type` | Shot category (e.g. establishing, close-up) |
| `title` | Optional shot title |
| `spatial` | Spatial context (e.g. indoor, outdoor) |
| `time_of_day` | Time of day label |
| `camera` | Camera movement or style |
| `shot` | Framing description (e.g. wide, medium) |
| `setting` | Location description |
| `description` | Free-text scene description |
| `humans` | People present in the shot |
| `wearing` | Clothing and accessories |
| `animals` | Animals in the shot |
| `objects` | Objects in the shot |
| `action` | Actions taking place |
| `text` | On-screen text |

Fields shown in bold have a value. Fields shown in muted text are empty. The panel can be collapsed to give more room to the video.

### Right — Scene & Shot Tables

**Top: film selector** — Choose a film (and media type) from the dropdown. The status line shows scene count, shot count, active/ignored/annotated totals.

**Scene table** — One row per scene. Click to jump to that scene.

**Shot table** — Lists shots within the selected scene with columns: `✓` (annotated), `Shot` number, `Start` timecode, `Best` frame, `Stop` timecode, `Ignore` flag. The active shot is highlighted in fuchsia.

**Shot info** — Below the tables shows the current shot's scene/shot number, frame number, timecodes, and media ID.

## Action Buttons

| Button | Action |
|---|---|
| **Auto-Annotate** | Run the AI annotation pipeline on the current shot |
| **Remove** | Delete the current shot from the shotlist |
| **Gremlins** | Inspect or fix annotation data issues |
| **New Shot** | Insert a new shot boundary at the current frame |
| **Merge Shot** | Merge the current shot with the next shot |
| **Ignore** | Toggle the current shot as ignored (excluded from analysis) |
| **New Scene** | Start a new scene at the current shot |
| **Merge Scene** | Merge the current scene into the previous scene |
| **Save** | Write annotation changes to disk |
| **Play** | Play/pause video |
| **Continue** | Advance to the next unannotated shot and play |
| **Loop** | Toggle looping playback of the current shot |

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `↑` / `↓` | Previous / next shot |
| `PgUp` / `PgDn` | Previous / next scene |
| `Space` | Play / pause |
| `←` / `→` | Step one frame back / forward |
| `Shift+←` / `Shift+→` | Jump 1 second back / forward |
| `Home` / `End` | First / last shot |
| `Escape` / `Ctrl+Q` / `Ctrl+W` | Close |

## Requirements

Video files must be present under `media/videos/<media_type>/`. Shotlist data is read from `data/shotlists/` and annotation data from `data/annotations/`.
