# TODO List
- [ ]   Fix occasional audio unsnycing when jumping to new shot or sequence or movie; rare occurence that can quickly be resynced by pressing Arrow Up then Down again, suggesting this is an easy fix
- [ ]   Start work on Sync Visualizer
- [ ]   Add media type selector (Movie/Gameplay) in Shotlist Visualizer. Question: should Movie Shotlist Visualizer be a separate instance from Gameplay Shotlist Visualizer?
- [ ]   Finish setting more/all Scene breaks in database
- [ ]   Start Scene detections with scene-specific system & user prompts
- [ ]   Add Scene field panel in Shotlist Visualizer
- [ ]   Test/Vertify/Confirm previous Index system is still working with new shot_id system
- [ ]   Figure out why some movies (Cowboys & Aliens, Days of Heaven, others?) seem to use the wrong audio channel in the current sync-audio-to-OpenCV-frame system in Shotlist Visualizer
- [ ]   Normalize *all* the audio levels across all the films (via a new `crossing media movies normalize` command)