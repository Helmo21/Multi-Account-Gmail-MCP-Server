# Setup

## 1. Google Cloud project

One project serves all five accounts.

1. Create a project at <https://console.cloud.google.com/>.
2. **APIs & Services → Library →** enable **Gmail API**.
3. **APIs & Services → OAuth consent screen →** choose **Internal**.
   Internal is available because every mailbox is on one Workspace
   domain. It needs no Google verification, shows no "unverified app"
   warning, and — unlike an External screen in Testing status — does not
   expire refresh tokens after seven days.
4. Add the scope `https://www.googleapis.com/auth/gmail.modify`. Add
   nothing else.
5. **Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app.**
6. Download the JSON and save it as `client_secret.json` in your config
   directory (see below).

## 2. Install

```bash
git clone <repo> && cd local_mcp_gmail
uv sync
```

## 3. Configure

The config directory does not exist yet — create it, then copy
`config.example.toml` into it as `config.toml`:

| Platform | Config directory |
| --- | --- |
| Linux | `~/.config/gmail-mcp/` |
| macOS | `~/Library/Application Support/gmail-mcp/` |
| Windows | `%LOCALAPPDATA%\gmail-mcp\` |

```bash
# Linux
mkdir -p ~/.config/gmail-mcp
cp config.example.toml ~/.config/gmail-mcp/config.toml

# macOS
mkdir -p ~/Library/Application\ Support/gmail-mcp
cp config.example.toml ~/Library/Application\ Support/gmail-mcp/config.toml
```

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\gmail-mcp"
Copy-Item config.example.toml "$env:LOCALAPPDATA\gmail-mcp\config.toml"
```

Edit the aliases, addresses, and send policies in the copy. `send_policy`
is required on every account and must be `send`, `confirm`, or
`draft_only`. There is no default.

This is also where `client_secret.json` from step 1 goes, so create the
directory before saving that file if you have not already.

## 4. Authenticate each account

Run once per alias:

```bash
uv run gmail-mcp auth add personal
uv run gmail-mcp auth add work-main
uv run gmail-mcp auth add work-sales
uv run gmail-mcp auth add work-support
uv run gmail-mcp auth add work-billing
```

Each opens a browser. **Sign in as the exact address configured for that
alias.** Google reuses whichever session is already signed in, so when
authenticating several accounts in a row, either sign out between them or
use a private window. If the wrong mailbox is authenticated the command
refuses to save anything and tells you which address it saw.

Check the result:

```bash
uv run gmail-mcp auth list
uv run gmail-mcp doctor
```

`doctor` verifies every account's token refreshes and reaches the
intended mailbox. All five should report `ok`.

### Where tokens are stored

Tokens are saved in your OS keyring (macOS Keychain, GNOME Keyring/KWallet
on Linux, Windows Credential Manager) whenever one is available. If none
is reachable — common on a headless Linux box or inside a container —
the server falls back automatically to a plain JSON file in your config
directory, created with `0600` permissions so only your user account can
read it. Both `auth list` and `doctor` print a `storage:` line showing
which mode is in effect: `keyring:<backend name>` or `file:<path>`. This
is decided once, automatically, and needs no configuration from you.

## 5. Connect Claude Desktop

Add to `claude_desktop_config.json`:

| Platform | Path |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "gmail": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/local_mcp_gmail",
               "run", "gmail-mcp", "serve"]
    }
  }
}
```

Use absolute paths for both. Claude Desktop does not inherit a login
shell's `PATH`, and a bare command name is the usual reason a server
silently fails to start. Find your `uv` with `which uv`.

Restart Claude Desktop, then ask it to list your accounts.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `has not been authenticated` | Run `gmail-mcp auth add <alias>`. |
| `was revoked or expired` | The password changed or access was removed. Run `gmail-mcp auth add <alias>` again. |
| `authenticated <other address>` | The browser was signed in as the wrong account. Sign out or use a private window and retry. |
| Server missing in Claude Desktop | Non-absolute paths in the config block. Check `which uv`. |
| `Not found in this mailbox` | The message ID came from a different account. Gmail IDs are account-scoped. |
| `storage: file:` in `doctor` | No OS keyring available. Tokens are in a `0600` file; expected on headless Linux. |
