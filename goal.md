I need a local MCP (Model Context Protocol) server that connects 
5 Gmail accounts (1 personal, 4 business) to Claude Desktop, so I 
can search, read, draft, and send email across all of them without 
manually switching accounts.

This is an MCP (Model Context Protocol) server for Claude 
Desktop — not a chatbot or model-training project.

Requirements:
- Local MCP server (runs on my machine, no hosting/cloud needed)
- OAuth 2.0 setup under one Google Cloud project, authenticating 
  all 5 Gmail accounts
- Secure local token storage with automatic refresh
- MCP tools for: searching mail, reading a message/thread, sending, 
  drafting, and applying labels
- Every tool call should let Claude specify WHICH account to act 
  on (e.g. "personal", "work-main", "work-sales", etc.)
- A summary/check tool designed for periodic automated use (e.g. 
  "check all 5 inboxes and return anything unread/urgent") — this 
  will be called on a schedule by Claude directly, so it should be 
  fast and return a concise summary rather than full message dumps
- One-time browser-based login per account during setup; hands-off 
  after that
- Compatible with Claude Desktop's MCP configuration

Deliverables:
- Working, tested MCP server (Python preferred)
- Setup documentation
- A short screen-share call to walk through authenticating my 
  5 accounts together

Milestones:
1. OAuth working end-to-end on 1-2 accounts (proof of concept)
2. All 5 accounts authenticated + full toolset, including the summary/check tool