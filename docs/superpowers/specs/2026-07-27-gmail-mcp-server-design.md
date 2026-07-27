# Local Gmail MCP Server — Design

**Date:** 2026-07-27
**Status:** Approved for planning

## Purpose

A local MCP server that connects five Gmail mailboxes on one Google Workspace
domain to Claude Desktop, so mail can be searched, read, drafted, sent, and
labelled across all five without switching accounts. A separate summary tool
sweeps all five inboxes on a schedule and returns a concise digest.

The server runs on the user's own machine over stdio. There is no hosting, no
network service, and no component that outlives the Claude Desktop process
except the token store and the check watermarks on disk.

## Decisions

These were settled during brainstorming and are not open questions.

| Decision | Choice |
| --- | --- |
| Account type | Five mailboxes, one Google Workspace domain |
| Google Cloud | One project, one OAuth client, shared by all five accounts |
| Consent screen | Internal — no verification, no unverified-app warning, no 7-day refresh-token expiry |
| Auth model | Per-account OAuth, one browser login each, run from a setup CLI |
| Send autonomy | Per-mailbox policy, enforced server-side |
| Check tool state | Per-account watermark persisted on disk |
| Target platforms | macOS, Windows, and Linux |
| Language | Python 3.12, `uv` for dependency management |

The Internal consent screen is what makes hands-off operation possible. On an
External consent screen in Testing status Google expires refresh tokens after
seven days, which would force a weekly re-login across all five accounts.

Service-account domain-wide delegation was considered and rejected. It would
remove browser logins entirely and never need re-auth, but it requires Admin
console cooperation and its credential can reach every mailbox on the domain
rather than the intended five. No abstraction is being built to keep that door
open; if it is ever wanted, it replaces `auth.py` and nothing else.

## Architecture

Two entry points share one core. `gmail-mcp serve` is the stdio MCP server that
Claude Desktop launches. `gmail-mcp auth` is the setup CLI used during initial
authentication. Both load the same config and the same token store, which is
what makes "authenticate once, then hands-off" work.

```
gmail_mcp/
  server.py      MCP tool definitions — validate args, delegate, format replies
  config.py      load and validate config.toml; resolve alias to account
  auth.py        OAuth installed-app flow; credential load and refresh
  storage.py     token store (keyring, file fallback) and check watermarks
  gmail/
    client.py    per-account Gmail service factory with process-lifetime cache
    search.py    query pass-through, result normalisation
    messages.py  message and thread reads, MIME body extraction
    compose.py   MIME construction, drafts, sending
    labels.py    label listing and modification
  check.py       periodic summary logic and watermark handling
  cli.py         auth add / list / remove, and doctor
tests/
```

`server.py` stays thin on purpose: tool functions validate arguments, call into
`gmail/`, and shape the response. No Gmail API logic lives inline there. This
keeps each module independently testable without an MCP client in the loop, and
keeps files small enough to reason about whole.

Every request follows one path: Claude Desktop → stdio → tool → resolve alias to
account → credentials from store, refreshing if stale → Gmail API → normalise to
a compact shape → return. No tool returns raw Gmail API JSON. Response envelope
fields cost tokens that are better spent on actual mail content.

## Configuration and account identity

Config lives in the per-OS user config directory, resolved with `platformdirs`
(`~/.config/gmail-mcp` on Linux, `~/Library/Application Support/gmail-mcp` on
macOS, `%LOCALAPPDATA%` on Windows). It is plain TOML, meant to be read and
edited by hand.

```toml
[[accounts]]
alias       = "personal"
email       = "you@yourdomain.com"
send_policy = "confirm"

[[accounts]]
alias       = "work-sales"
email       = "sales@yourdomain.com"
send_policy = "draft_only"
```

`alias` is the handle Claude uses in every tool call. `email` is a safety check,
not decoration.

**The alias-to-mailbox guard.** When five accounts are authenticated
back-to-back in one browser, Google reuses whichever session is already signed
in. Without a check, two aliases silently end up pointing at the same mailbox
and the mistake is not visible until someone sends mail from the wrong address.
After consent, the setup CLI reads back the authenticated profile address and
**refuses to save the token if it does not match the alias's configured email**,
naming both addresses in the error. This single check is what makes a
five-account setup session survivable.

## OAuth and token storage

**Scope: `gmail.modify`, alone.** It covers search, read, send, draft, and
label — everything required — and it cannot permanently delete mail. That
limitation is taken deliberately: whatever else goes wrong, there is no API path
by which Claude can destroy a message. As a restricted scope on an Internal
consent screen it needs no Google verification.

The OAuth client is of type **Desktop app**, using the loopback redirect flow via
`InstalledAppFlow.run_local_server(port=0)`. A throwaway localhost listener
receives the authorization code. The deprecated out-of-band copy-paste flow is
not used. The flow requests `access_type=offline` with `prompt=consent` so a
refresh token is issued rather than silently omitted on repeat authorizations.

**Token storage is `keyring`** — macOS Keychain, Windows Credential Manager,
Linux Secret Service — keyed by service `gmail-mcp` with the alias as username.
Where no keyring backend exists, realistically a headless Linux install, the
fallback is a JSON file at mode `0600` in the config directory, and `doctor`
reports which mode is active.

The fallback is deliberately not an encrypted file. The server starts
unattended and can never prompt for a passphrase, so the decryption key would
have to sit beside the ciphertext. That is ceremony rather than security. A
`0600` file is the honest equivalent, and the keyring is the real answer
wherever one exists.

Refresh is automatic: on each call, an expired access token is refreshed and the
updated credential written back to the store. **A dead credential fails one
account, never the server.** A revoked refresh token produces a structured error
naming the alias and the exact command to re-run, while the other four accounts
continue working.

## Tools

Ten tools. Every mail-touching tool takes `account` as its first argument, so
the mailbox is always named explicitly and never inherited from earlier in the
conversation.

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Aliases, addresses, and send policies, so Claude discovers valid names rather than guessing |
| `search_mail` | Gmail query syntax passed through; returns compact hits |
| `read_message` | One message: headers, text body, attachment names and sizes |
| `read_thread` | Whole thread in order, quoted text collapsed |
| `create_draft` | Compose a draft, optionally as a reply |
| `send_message` | Compose and send, gated by the account's send policy |
| `send_draft` | Send an existing draft, gated by the same policy |
| `list_labels` | Label names and IDs for the account |
| `modify_labels` | Add and remove labels on a message or thread |
| `check_inboxes` | Scheduled sweep across all accounts |

`search_mail` passes Gmail query syntax straight through
(`is:unread from:someone newer_than:2d`) and documents that in its tool
description so Claude writes precise queries rather than fetching broadly and
filtering afterwards. It returns 20 hits by default, capped at 100, and
excludes spam and trash unless asked otherwise.

`modify_labels` covers more than labels. Marking read and unread is adding or
removing the `UNREAD` system label; archiving is removing `INBOX`. One tool
handles all of it rather than three near-duplicate tools.

Attachment contents are out of scope. `read_message` reports attachment names
and sizes but does not download them; nothing in this server writes to the
filesystem outside its own config directory.

### Reply threading

Passing `reply_to_message_id` to `create_draft` or `send_message` makes the
server fetch that message's `Message-ID` header and set `In-Reply-To` and
`References` on the outgoing message, alongside the Gmail thread ID. Without
this, replies arrive as orphan messages in the recipient's client. It is the
most common way integrations of this kind look broken to everyone except their
author.

### Send policy

Each account declares one of three policies in config, enforced in code:

- **`send`** — `send_message` transmits immediately. The `confirm` argument is
  accepted and ignored, so a caller that always passes it behaves identically.
- **`confirm`** — transmitting requires an explicit `confirm=true` argument.
  Without it, the server creates a draft and returns it along with a note that
  re-calling with confirmation will send it.
- **`draft_only`** — never transmits, even with `confirm=true`. Creates a draft
  and returns it with the policy named in the response.

There is no default policy: every account must declare one, and config
validation fails on a missing or unrecognised value rather than guessing.

`send_draft` is gated identically, so the confirm workflow does not require
recomposing the message.

The policy lives in configuration rather than in prompt instructions, so it
holds regardless of what Claude has been told in conversation. This matters
because **email content is untrusted input**. A message can contain text
crafted to steer the model — instructions to forward mail elsewhere, for
example — and the check tool reads mail from the open internet on a schedule.
Per-account send policy is the actual control on that risk, which is why it is
enforced in code.

## The check tool

`check_inboxes` sweeps every configured account by default, or a named subset
via an optional `accounts` argument. It returns facts, not judgments. Per
account it queries
`in:inbox is:unread after:<watermark>`, fetches **metadata only** — `From`,
`Subject`, `Date`, no bodies — and returns:

- the account alias
- a count of what is new since the last run
- the total unread count
- up to `max_per_account` (default 10) compact items, each with sender,
  subject, a snippet capped at roughly 140 characters, received time, and
  message ID, plus a count of any remainder

It does not classify urgency. Keyword heuristics for "urgent" go stale and
misfire, and Claude judges better from sender and subject with conversational
context in hand. If summaries prove noisy in practice, restricting the query to
Gmail's own `is:important` signal is a one-line change.

The watermark is the most recent reported message time per account, persisted
in the config directory, and **advances to the newest reported message once
everything new has actually been returned**. With no watermark — first run, or
a newly added account — the tool looks back 24 hours rather than dumping years
of accumulated unread mail. A failed, empty, or truncated *first* run still
persists that frozen 24-hour boundary rather than leaving the watermark unset,
so the lookback window does not silently slide forward with the clock and skip
mail that fell inside the original window.

Accounts are queried in parallel. Metadata-only fetches should keep a full
five-account sweep to a few seconds, which matters for a tool that fires on a
schedule.

One accepted limitation: the watermark advances when the tool returns, so if a
scheduled run's output is never acted upon, that mail is not repeated on the
next run. The alternative — an explicit acknowledgement step — adds a
stateful handshake disproportionate to the problem. Anything missed remains
reachable through `search_mail`.

## Error handling

The governing rule is that **one account's problem is never all five accounts'
problem**. Any multi-account operation returns per-account errors inline
alongside the successful results.

| Condition | Behaviour |
| --- | --- |
| Unknown alias | Immediate structured error listing the valid aliases |
| Revoked or invalid refresh token | Per-account error naming the alias and the exact re-auth command |
| Gmail 429 or 5xx | Retry with exponential backoff and jitter, three attempts, then a structured error |
| Message ID not found | Error noting the ID may belong to a different mailbox |

That last case earns its own message. **Gmail message IDs are scoped to the
account they came from**, so an ID from `work-sales` simply 404s against
`personal`. Reported raw, it reads as "message deleted" and sends the user
looking in the wrong place.

All failures surface as structured tool errors with actionable text. No stack
traces reach the model.

## Testing

Unit tests run against a mocked Gmail API, so the suite is offline and fast.
Implementation is test-first.

- **Send policy gate** — table-driven across all three policies with and
  without confirmation. This is the code most capable of causing real-world
  damage if subtly wrong, and it gets the most thorough coverage.
- **MIME construction** — reply headers, unicode subjects, cc and bcc.
- **Body extraction** — multipart alternatives, HTML-only mail, quoted-text
  collapsing.
- **Watermark logic** — advance on success, hold on failure, first-run
  look-back.
- **Error mapping** — each condition in the table above produces its intended
  structured error.
- **Config and alias resolution** — including the email-mismatch guard, tested
  with a fake credential store.

One integration test covers the OAuth round trip against a real test mailbox.
It is opt-in behind an environment variable, because that flow cannot be
meaningfully faked and should not run in ordinary test invocations.

## Setup and delivery

`gmail-mcp doctor` verifies that config parses, that every account's token
loads and refreshes, which storage backend is in use, and that Gmail responds.
It is the safety net for the walkthrough session: run it after the five logins
and it either reports all five green or names exactly which account is wrong.

The package installs a `gmail-mcp` console script. Claude Desktop launches the
server through an absolute-path invocation in `claude_desktop_config.json`
under `mcpServers`, because Claude Desktop does not inherit a login shell's
`PATH` and a bare command name is a routine cause of a server that silently
fails to start.

Documentation covers Google Cloud project setup (enabling the Gmail API,
creating the Internal consent screen, creating the Desktop OAuth client),
installation via `uv`, the Claude Desktop MCP configuration block, per-account
authentication, and troubleshooting keyed to what `doctor` reports.

## Success criteria

1. All five accounts authenticate through one browser login each, and the
   alias guard rejects a mismatched login.
2. Search, read, draft, send, and label work against every account, addressed
   by alias.
3. Replies thread correctly in the recipient's mail client.
4. Send policy is honoured per account, verified for all three policies.
5. `check_inboxes` returns a five-account digest in a few seconds and does not
   repeat previously reported mail on a subsequent run.
6. A revoked credential on one account degrades that account only.
7. Server survives a Claude Desktop restart with no re-authentication.
