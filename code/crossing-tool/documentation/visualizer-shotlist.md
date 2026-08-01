# Shotlist

![Shotlist Visualizer screenshot](./images/visualizers/visualizer-shotlist.png)

The `Shotlist` visualizer is the primary tool for reviewing, editing, and annotating shot lists. It combines a frame-accurate video player, a scene/shot index, and an annotation panel in one window, following the shared Visualizer Framework layout (Browser / side panels / Inspector).

Open it with:

```
crossing visualizer shotlist
```

## Layout

### Browser — Video Playback

The full-window playback area. Displays the current frame of the active shot with the subtitle overlay on top. A timeline scrubber at the bottom lets you scrub the whole film — the scrubber's handle length reflects how much of the film the current shot spans. The Browser is for viewing, scrubbing, and navigating playback only; it holds no annotation or inspector-style controls.

### Scene panel

A collapsible, content-width side panel listing one row per scene. Click a row to jump to that scene. Collapse or expand it via its splitter grip handle (it is not manually resizable).

### Shot panel

A collapsible, content-width side panel listing shots in the film with columns: `✓` (annotated), `Shot` number, `Start` timecode, `Best` frame, `Stop` timecode, `Ignore` flag. The active shot is highlighted in yellow. Collapse or expand it via its splitter grip handle (it is not manually resizable).

### Inspector

A fixed-width right-side panel built from collapsible sections:

**Filter** — Media type (movie / gameplay) and film selector.

**Info** — Aggregate stats (scene count, shot count, active/ignored/annotated totals) and the current shot's detail (scene, shot #, frame, start/end timecode, confidence, shot ID).

**Annotation** — Displays and edits the structured annotation for the selected shot (fields/json/txt/vector/mapping representations), plus **Auto-Annotate** and **Remove** actions:

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

Fields shown in bold have a value. Fields shown in muted text are empty.

**Playback** — Play/Pause, Continue, Loop, and Gremlins controls.

**Tools** — Shot/scene structural edits: New Shot, Merge Shot, Ignore, New Scene, Merge Scene, and Save.

Any section can be collapsed independently to give more room to the ones you're using.

## Action Buttons

| Button | Section | Action |
|---|---|---|
| **Auto-Annotate** | Annotation | Run the AI annotation pipeline on the current shot |
| **Remove** | Annotation | Delete the annotation for the current shot |
| **New Shot** | Tools | Insert a new shot boundary at the current frame |
| **Merge Shot** | Tools | Merge the current shot with the previous shot |
| **Ignore** | Tools | Toggle the current shot as ignored (excluded from analysis) |
| **New Scene** | Tools | Start a new scene at the current shot |
| **Merge Scene** | Tools | Merge the current scene into the previous scene |
| **Save** | Tools | Write shotlist changes to disk |
| **Play** | Playback | Play/pause video |
| **Continue** | Playback | Advance to the next unannotated shot and play |
| **Loop** | Playback | Toggle looping playback of the current shot |
| **Gremlins** | Playback | Randomly jump movies/timecodes every 5s |

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
