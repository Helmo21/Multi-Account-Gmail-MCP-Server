"""The MCP tool surface.

Tools validate arguments, delegate, and shape the reply. No Gmail API
logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from gmail_mcp.check import DEFAULT_MAX_PER_ACCOUNT, check_inboxes
from gmail_mcp.config import Account, Config, config_dir, load_config
from gmail_mcp.gmail import labels as labels_module
from gmail_mcp.gmail.client import ServiceCache
from gmail_mcp.gmail.compose import compose_and_deliver, send_existing_draft
from gmail_mcp.gmail.messages import read_message as _read_message
from gmail_mcp.gmail.messages import read_thread as _read_thread
from gmail_mcp.gmail.search import DEFAULT_MAX_RESULTS, search_messages
from gmail_mcp.storage import TokenStore, WatermarkStore

Recipients = str | list[str]


@dataclass
class Runtime:
    config: Config
    token_store: object
    watermarks: object
    services: object

    def account(self, alias: str) -> Account:
        return self.config.get(alias)

    def service(self, alias: str):
        return self.services.get(alias)


def build_runtime(directory: Path | None = None) -> Runtime:
    directory = directory or config_dir()
    config = load_config(directory / "config.toml")
    tokens = TokenStore(directory)
    return Runtime(
        config=config,
        token_store=tokens,
        watermarks=WatermarkStore(directory),
        services=ServiceCache(config, tokens),
    )


def create_server(runtime: Runtime) -> FastMCP:
    server = FastMCP("gmail-mcp")

    def list_accounts() -> list[dict]:
        """List every configured mailbox, its address, and its send policy.

        Call this first to learn the valid `account` aliases.
        """
        return [
            {
                "alias": a.alias,
                "email": a.email,
                "send_policy": a.send_policy,
            }
            for a in runtime.config.accounts
        ]

    def search_mail(
        account: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        include_spam_trash: bool = False,
    ) -> list[dict]:
        """Search one mailbox using Gmail query syntax.

        `query` accepts the same operators as the Gmail search box, for
        example `is:unread from:boss@example.com newer_than:2d`. Prefer a
        precise query over fetching broadly and filtering afterwards.
        Message IDs returned here are only valid for this account.
        """
        runtime.account(account)
        return search_messages(
            runtime.service(account),
            query,
            max_results=max_results,
            include_spam_trash=include_spam_trash,
        )

    def read_message(account: str, message_id: str) -> dict:
        """Read one message in full, including its text body.

        Attachment names and sizes are reported; contents are not
        downloaded. `message_id` is account-scoped: an ID from a
        different mailbox will not be found here.
        """
        runtime.account(account)
        return _read_message(runtime.service(account), message_id)

    def read_thread(account: str, thread_id: str) -> dict:
        """Read a whole conversation, oldest first, quoted text removed.

        `thread_id` is account-scoped, like message IDs.
        """
        runtime.account(account)
        return _read_thread(runtime.service(account), thread_id)

    def create_draft(
        account: str,
        to: Recipients,
        body: str,
        subject: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Save a draft. Never sends, whatever the account policy.

        Pass `reply_to_message_id` to reply in-thread; the subject is
        derived from the original when omitted.
        """
        target = runtime.account(account)
        return compose_and_deliver(
            runtime.service(account),
            target,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            force_draft=True,
        )

    def send_message(
        account: str,
        to: Recipients,
        body: str,
        subject: str | None = None,
        cc: Recipients | None = None,
        bcc: Recipients | None = None,
        reply_to_message_id: str | None = None,
        confirm: bool = False,
    ) -> dict:
        """Send a message, subject to the account's send policy.

        A `confirm` account requires `confirm=true` to transmit and saves
        a draft otherwise. A `draft_only` account never transmits. Check
        the `action` field of the result to see what actually happened:
        the message may have been drafted instead of sent. `confirm` must
        be a literal boolean; anything else is rejected rather than
        guessed at.
        """
        target = runtime.account(account)
        return compose_and_deliver(
            runtime.service(account),
            target,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            confirm=confirm,
        )

    def send_draft(account: str, draft_id: str, confirm: bool = False) -> dict:
        """Send an existing draft, subject to the same send policy.

        `draft_id` is account-scoped. `confirm` must be a literal
        boolean; see `send_message` for the policy semantics.
        """
        target = runtime.account(account)
        return send_existing_draft(
            runtime.service(account), target, draft_id, confirm=confirm
        )

    def list_labels(account: str) -> list[dict]:
        """List every label on one mailbox."""
        runtime.account(account)
        return labels_module.list_labels(runtime.service(account))

    def modify_labels(
        account: str,
        message_id: str | None = None,
        thread_id: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Add and remove labels by name on one message or thread.

        Use this to mark read or unread (the `UNREAD` label) and to
        archive (remove `INBOX`). Pass exactly one of `message_id` or
        `thread_id`.
        """
        runtime.account(account)
        return labels_module.modify_labels(
            runtime.service(account),
            message_id=message_id,
            thread_id=thread_id,
            add=add or [],
            remove=remove or [],
        )

    def check_inboxes_tool(
        accounts: list[str] | None = None,
        max_per_account: int = DEFAULT_MAX_PER_ACCOUNT,
    ) -> dict:
        """Summarise new unread inbox mail across mailboxes.

        Designed for scheduled use: returns counts plus a few compact
        items per account, never message bodies. Only mail arriving since
        the previous call is reported, so repeated runs do not repeat
        themselves. Accounts that fail are reported inline with an
        `error` field while the rest still return.
        """
        return check_inboxes(
            runtime.config,
            runtime.services,
            runtime.watermarks,
            aliases=accounts,
            max_per_account=max_per_account,
        )

    functions = {
        "list_accounts": list_accounts,
        "search_mail": search_mail,
        "read_message": read_message,
        "read_thread": read_thread,
        "create_draft": create_draft,
        "send_message": send_message,
        "send_draft": send_draft,
        "list_labels": list_labels,
        "modify_labels": modify_labels,
        "check_inboxes": check_inboxes_tool,
    }

    for name, function in functions.items():
        server.add_tool(function, name=name)

    server.tool_functions = functions
    return server


def main() -> None:
    create_server(build_runtime()).run(transport="stdio")
