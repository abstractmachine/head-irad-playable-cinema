# TODO
- Add :
    - Subtitles of all dialogue spoken in between `Start` and `End` when sending to `OpenAI` `API`. Include somehow in System Prompt

- Add Movies :
    - The Naked Spur (Higher Res)
    - Track Of The Cat (Higher Res)
    - Cf. /PLAYABLE/Work/todo

# Bugs

- For the long `playthroughs` (approx 25h+ of footage), we should:
    - Use the standard detection method `threshold-adaptive`
    - Add a `max_length` text field in the `shotlist` window
    - After detections, if `max_length` field is not 0, cut any row in the shotlist whose length (`End` - `Begin`) is longer than than `max_length` into parts no longer than `max_length`

# Fixed
- ~~When not playing, the shotlist index does not advance. This keeps the auto-bot from being able to press the `Next` button~~
- ~~Play for a second before shotlist jumping. There is a weird hack I have to do in tool where you have to play/pause video after it has loaded, in order to get the timecode to align correctly in the shotlist. Otherwise the timings are off.~~
- ~~Sometimes the shotlist can't find a closest shot~~