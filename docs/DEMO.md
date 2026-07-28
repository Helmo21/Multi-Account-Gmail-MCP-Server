# Demo script

A ten-minute walkthrough. Each act is one thing to say to Claude and one
point to make. Runs against two accounts (`personal`, `work-main`); the
same script works with any number.

The arc is deliberate: capability first, then the safety property. For a
business audience the safety beat lands harder than the search, because
it answers the question they are actually holding — *what stops this
thing emailing my clients?*

---

## Before the call — seed the inboxes (2 minutes)

The demo is much better with known mail in it. Send yourself three
messages from webmail or your phone, to whichever address you'll be
demoing, with subjects like:

- `Invoice #1042 — payment overdue`
- `Team lunch Friday?`
- `Contract renewal — need your signature by Thursday`

Leave them unread. One urgent, one trivial, one time-boxed gives Claude
something to actually reason about in Act 1.

**To re-run the demo from scratch**, delete the watermark file so the
check tool forgets what it already reported:

```bash
rm ~/.config/gmail-mcp/watermarks.json
```

Sanity check before you start:

```bash
uv run gmail-mcp doctor
```

---

## Act 1 — "What needs my attention?"

> **Say:** "Check all my inboxes and tell me what needs my attention."

Claude calls `check_inboxes` once and gets a compact digest from every
account in parallel.

**The point:** one question, every mailbox, no switching. And note *how*
it answers — the server returns only sender, subject and a snippet. It
does not score urgency or match keywords. Claude decides what matters,
using everything it knows about you. Keyword rules for "urgent" go stale;
judgment doesn't.

---

## Act 2 — "Anything new?"

> **Say:** "Check again."

It reports nothing new.

**The point:** this is what makes it safe to run on a timer. Each account
remembers where it got to, so a scheduled check every 30 minutes tells
you about genuinely new mail instead of re-reading you the same list all
day. Worth dwelling on — it's the difference between a useful alert and
notification noise.

---

## Act 3 — One question, both mailboxes

> **Say:** "Search both accounts for anything about invoices or payments
> in the last week, and summarise what's outstanding."

**The point:** the mailbox is always named explicitly in every call, so
there's no ambiguity about which account an answer came from — but you
never have to say it. Ask about "both", or name one directly: *"search
work-main for..."*.

---

## Act 4 — The safety beat

> **Say:** "Reply to the invoice email from my personal account saying
> I'll pay it tomorrow. Send it."

You asked it to send. **It creates a draft instead** and tells you the
account's policy is `draft_only`.

**The point, and this is the one that matters to a business:** the policy
lives in the configuration file and is enforced in code, not in an
instruction Claude has been asked to follow. It cannot be talked out of
it, and neither can anything that arrives in the mail it reads. That last
part is not hypothetical — email is untrusted input, and a message can be
written to steer a model into forwarding things. Configuration is the
control on that.

Then show the config:

```toml
[[accounts]]
alias       = "personal"
send_policy = "draft_only"    # never sends, ever

[[accounts]]
alias       = "work-main"
send_policy = "confirm"       # sends only on explicit confirmation
```

Three settings per mailbox: `send` transmits when asked, `confirm`
requires explicit confirmation, `draft_only` never transmits. Set per
account, so a personal address can be free while client-facing ones stay
locked.

---

## Act 5 — Confirmed send, threaded correctly

> **Say:** "Draft a reply from work-main to the contract email saying I'll
> review and sign by Wednesday."

It drafts, because `work-main` is `confirm`.

> **Then say:** "That looks right — send it."

Now it transmits.

**The point:** two steps, and the second one is yours. Open the sent
message in Gmail and show it sitting in the original thread — replies
carry the threading headers, so they land as replies in Outlook and Apple
Mail too, not as orphan messages.

---

## Act 6 — Triage

> **Say:** "Archive the team lunch email and mark the invoice one as
> read."

**The point:** archiving and marking read are label operations, so the
same tool that organises mail also clears the inbox. You can dictate a
triage pass instead of clicking through it.

---

## If someone asks

**"Where does my mail go?"** Nowhere. The server runs on this machine and
talks to Google directly. No third-party service, nothing hosted.

**"What can it actually do to my mail?"** It holds one Google permission,
`gmail.modify`. That covers reading, sending, drafting and labelling —
and it *cannot* permanently delete a message. That limit was chosen
deliberately, not inherited.

**"What if it gets a token wrong?"** Each account is independent. A
revoked or expired credential fails that one mailbox and names it, while
the others keep working. `gmail-mcp doctor` says which one needs
re-authenticating and the exact command to fix it.

**"How many accounts?"** As many as you want. Add a block to the config,
run `auth add`, done.
