# gmail-mcp

A local MCP server that gives Claude Desktop access to several Gmail
mailboxes at once — searching, reading, drafting, sending, labelling, and
a scheduled inbox summary — without switching accounts.

Runs entirely on your machine over stdio. Nothing is hosted, and no mail
leaves your machine except to Google.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Aliases, addresses, and send policies |
| `search_mail` | Gmail query syntax against one account |
| `read_message` | One message with its body |
| `read_thread` | A whole conversation, quoted text removed |
| `create_draft` | Save a draft, never send |
| `send_message` | Send, subject to the account's send policy |
| `send_draft` | Send an existing draft |
| `list_labels` | Labels on one account |
| `modify_labels` | Add and remove labels; also marks read and archives |
| `check_inboxes` | New unread mail across all accounts, for scheduled use |

Every mail tool takes an `account` alias, so the mailbox is always
explicit.

## Send safety

Each mailbox declares a `send_policy` in config, enforced in code rather
than in prompt text:

- `send` — transmits when asked.
- `confirm` — saves a draft unless the call explicitly confirms.
- `draft_only` — never transmits.

This matters because mail is untrusted input: a message can contain text
crafted to steer the model, and `check_inboxes` reads mail from the open
internet on a schedule. Configuration is the control on that, which is
why it is not a prompt instruction.

The server requests exactly one OAuth scope, `gmail.modify`, which cannot
permanently delete mail.

## Setup

See [docs/SETUP.md](docs/SETUP.md).

## Development

```bash
uv sync
uv run pytest
```

The suite runs entirely offline against a fake Gmail API. The OAuth
integration test is opt-in:

```bash
GMAIL_MCP_INTEGRATION=1 GMAIL_MCP_CONFIG_DIR=~/.config/gmail-mcp \
  uv run pytest tests/test_integration_oauth.py -v
```
