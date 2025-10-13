# Player
This is a video player that has been optimized to run on a Raspberry Pi 5. It accepts `NDJSON` over `TCP` via a locally run server listening to port `6666`.

## Installation
```
% pyenv virtualenv 3.11.9 playable-player 
% pyenv activate playable-player
```

## Communication
The player starts a local server listening on port `6666`.

### Trusted
The calling IP must be in the list of trusted IPs.

```
% nc -w 1 127.0.0.1 6666 <<< '{}'
{"ok": false, "error": "unauthorized ip", "ip": "10.0.0.1", "port": 12345}
```

### Password
The password must match the current password file.

```
% nc -w 1 127.0.0.1 6666 <<< '{}'                         
{"ok": false, "error": "bad password", "ip": "127.0.0.1", "port": 12345}
```

### Type
```
% nc -w 1 127.0.0.1 6666 <<< '{"password":"pwd", "type": "ping"}'                         
{"ok": false, "password": "", "ip": "127.0.0.1", "port": 12345}
```

- `ping`
- `play`
    - `filename`, default = `null`
    - `index`, default = `null`
    - `start`, default = `0`
    - `stop`, default = `null`
- `list`

## Error
We had an error with one file:

