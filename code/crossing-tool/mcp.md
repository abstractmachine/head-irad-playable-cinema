### MCP Server (Claude Desktop integration)

`crossing_mcp.py` exposes crossing data to LLMs via the [Model Context Protocol](https://modelcontextprotocol.io). This lets you query your film library directly from Claude Desktop (or any MCP-compatible client) without leaving the chat.

#### Server setup (Ubuntu)

**1. Install the MCP library into the project environment:**

```bash
uv add "mcp[cli]"
```

**2. Set the project path** (if not already configured):

```bash
crossing tool path /path/to/your/project
```

The server reads this saved preference automatically — no environment variable needed.

**3. Make the launcher script executable** (one-time, after cloning):

```bash
chmod +x run_mcp.sh
```

`run_mcp.sh` is a thin wrapper that locates itself at runtime using `$SCRIPT_DIR` and `$HOME` — no hardcoded paths, safe to commit to git.

**4. Test the server locally:**

```bash
uv run python crossing_mcp.py
```

The server speaks stdio (no port, no HTTP). It hangs silently waiting for input — that means it is working. Press `Ctrl+C` to exit.

#### Client setup (Claude Desktop on macOS or Windows)

Edit `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add a `crossing` entry under `mcpServers`. The config file may already contain a `preferences` key — add `mcpServers` alongside it:

```json
{
  "preferences": { ... },
  "mcpServers": {
    "crossing": {
      "command": "ssh",
      "args": [
        "-T",
        "playable-cinema",
        "/path/to/crossing-tool/run_mcp.sh"
      ]
    }
  }
}
```

Replace `playable-cinema` with your SSH host alias (or `user@hostname`), and update the path to `run_mcp.sh`. The `-T` flag disables pseudo-TTY allocation, which prevents SSH from sending terminal control codes that would corrupt the JSON-RPC stream.

> **Tip — SSH host alias:** Define `playable-cinema` in `~/.ssh/config` on the Mac so you do not have to repeat connection details:
> ```
> Host playable-cinema
>     HostName <ip-or-hostname>
>     User <your-username>
>     IdentityFile ~/.ssh/id_ed25519
>     AddKeysToAgent yes
>     UseKeychain yes
> ```
>
> The `UseKeychain yes` / `AddKeysToAgent yes` lines are important: Claude Desktop is a GUI app and does not inherit your terminal's SSH agent. Without these, the key is not available to Claude Desktop and every connection attempt will fail with `Permission denied`.
>
> Add your key to the macOS keychain once:
> ```bash
> ssh-add --apple-use-keychain ~/.ssh/id_ed25519
> ```

**Test the connection before opening Claude Desktop:**

```bash
ssh -T playable-cinema /path/to/crossing-tool/run_mcp.sh
```

It should hang silently. Press `Ctrl+C`, then restart Claude Desktop.

**Using the tools in Claude Desktop:**

After restarting, click the `+` button in the chat input area, choose **Connectors**, and select **crossing** to attach it to your conversation. Then ask naturally — e.g. "List my movies" — and Claude will call the tool automatically.