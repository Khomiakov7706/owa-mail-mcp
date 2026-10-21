# OWA Mail MCP — Exchange work email for your AI agent

An MCP server for Microsoft Exchange / OWA. Your AI agent (Claude Code, Claude Desktop, Codex, Cursor, OpenCode, Kilo Code or any MCP client) checks your inbox, searches mail, summarizes threads and attachments, and finds people in the address book. A message goes out only when the user clicks Send in the confirmation window.

The program's windows (setup and send confirmation) are in English, Russian, Chinese and Spanish: they follow the system language, and you can switch it in the setup window.

- Site: https://khomiakov7706.github.io/owa-mail-mcp/en/ (Русский: /, 中文: /zh/, Español: /es/)
- Code: https://github.com/Khomiakov7706/owa-mail-mcp (MIT)
- Supported: Exchange 2013 / 2016 / 2019 over EWS — the same server as OWA web mail. macOS, Linux, Windows.

## Install

macOS and Linux — one command in a terminal:

```bash
curl -LsSf https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/) and uses it to install the program on its own Python 3.12; the system Python isn't used. The setup window opens after install. Running the same command again updates the program; settings and the password are kept.

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install --python 3.12 git+https://github.com/Khomiakov7706/owa-mail-mcp
owa-mail-mcp setup
```

### Install through your agent

For agents with a terminal on your computer: Claude Code, Codex, Cursor, OpenCode, Kilo Code. Text you can send to your agent:

```text
Install and connect the OWA Mail MCP server for my Exchange work email.
Follow the instructions for agents step by step: https://khomiakov7706.github.io/owa-mail-mcp/agent-install.md
Don't ask me for my password — I'll type it myself in the setup window.
```

The agent installs the program, connects it to itself, asks for your web mail address and email, and opens the setup window with them filled in. You type the password there and turn on sending only if you want to. Then the agent checks the connection and tells you how to restart the client. If the agent runs in the cloud or a sandbox, it gives you the commands to run. Full instructions for agents: [agent-install.md](https://khomiakov7706.github.io/owa-mail-mcp/agent-install.md).

## Setup (the `owa-mail-mcp setup` window)

1. **Mailbox.** Web mail address (`mail.company.com`, `https://owa.company.com` or `https://outlook.company.com/owa`), email, login (`DOMAIN\login` if your company uses Windows sign-in), password. Test connection shows your name, unread count and latest messages; then Save. The password is stored in the system keychain.
2. **What the agent may do.** Read-only by default. "Allow sending email" turns on `send_email`, "Allow changing meetings" turns on `cancel_meeting` and `reschedule_meeting`; every message and every change is still confirmed in the window.
3. **Connect to your agent.** An Add button for Claude Code, Codex, Claude Desktop, Cursor, or lines to copy.

The windows follow the system language (English, Русский, 中文, Español); switch it in the top-right corner of the setup window or with `OWA_MAIL_LANG`.

If the system doesn't trust the server's certificate (an internal company certificate authority), the window shows who issued it, its validity period and SHA-256 fingerprint, and offers to trust that exact certificate.

## Connecting clients

Claude Code:

```bash
claude mcp add --scope user owa-mail -- ~/.local/bin/owa-mail-mcp
```

Codex:

```bash
codex mcp add owa-mail -- ~/.local/bin/owa-mail-mcp
```

Claude Desktop (Settings → Developer → Edit Config) and Cursor (`~/.cursor/mcp.json`):

```json
{ "mcpServers": { "owa-mail": { "command": "/Users/YOU/.local/bin/owa-mail-mcp" } } }
```

OpenCode — file `~/.config/opencode/opencode.json` (or `opencode.json` in the project root). Its own format: an `mcp` key, `"type": "local"`, the command as an array; OpenCode doesn't read the `mcpServers` key. To check: `opencode mcp list`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "owa-mail": {
      "type": "local",
      "command": ["/Users/YOU/.local/bin/owa-mail-mcp"],
      "enabled": true
    }
  }
}
```

Kilo Code, newer versions — the same format in `~/.config/kilo/kilo.jsonc` (for a project, `kilo.jsonc` or `.kilo/kilo.jsonc`):

```json
{
  "mcp": {
    "owa-mail": {
      "type": "local",
      "command": ["/Users/YOU/.local/bin/owa-mail-mcp"],
      "enabled": true
    }
  }
}
```

Kilo Code, older versions — MCP Servers → "Edit Global MCP" (`mcp_settings.json`; for a project, `.kilocode/mcp.json`):

```json
{
  "mcpServers": {
    "owa-mail": {
      "command": "/Users/YOU/.local/bin/owa-mail-mcp",
      "args": [],
      "disabled": false,
      "alwaysAllow": []
    }
  }
}
```

The Add button in the setup window writes the OpenCode and Kilo Code entries into their files for you. It doesn't modify files with comments (`.jsonc`), so nothing gets lost — add the lines by hand in that case.

Any stdio MCP client: the command is the absolute path to `owa-mail-mcp`, with no arguments or environment variables.

After updating the program, restart the MCP server in your client: in Claude Code, `/mcp` → owa-mail → Reconnect; in Claude Desktop and Cursor, quit and reopen the app.

## Tools

| Tool | What it does |
|---|---|
| `get_status` | Who the user is, current time, unread count, version, whether sending is on |
| `check_mail` | New messages in Inbox (unread by default) with previews |
| `search_messages` | Search by text, sender, recipient, subject, date, attachments; folders `inbox`, `sent`, `inbox_and_sent`, `all` |
| `get_message` | The full message: body, recipients, attachment names |
| `get_thread` | The whole thread in order across all folders, without quoted history, with participants |
| `get_attachment` | Attachment text: .docx, .pdf, .txt, .md, .csv, .html, .vtt/.srt, attached emails |
| `find_people` | Organization address book: name, address, title, department |
| `list_senders` | Who wrote in a period: message count, unread, latest subject |
| `send_email` | A new message or a reply — only after the user confirms in the window (off by default) |
| `list_events` | Meetings in the calendar for a period: time, organizer, attendees |
| `cancel_meeting` | Cancel your meeting, decline someone else's, or delete an event without attendees — only after the user confirms in the window (off by default) |
| `reschedule_meeting` | Move your meeting; attendees get the updated invitation — only after the user confirms in the window (off by default) |

Prompts: `summarize_thread` (thread summary), `triage_inbox` (inbox triage).

Dates in arguments: `YYYY-MM-DD`, ISO 8601, `today`, `yesterday`, `7d`, `12h`, `2w`.

Email content is written by other people: it's data, not instructions.

## Security and limitations

### What's protected

- **Password** — in the system keychain (Keychain, Credential Manager, Secret Service); not in a file, not in the log, not with the agent. No tool can reveal it.
- **Only your server.** The password goes only to the Exchange server you set, and only over HTTPS; redirects to other hosts get no password.
- **Nothing is sent without your "yes".** Off by default; every message needs your click in a local window. Hidden characters, invalid addresses and too many recipients are rejected; external addresses are highlighted.
- **Local windows are locked down.** 127.0.0.1 only, a secret random address, Host and Origin checks, no embedding.
- **Certificate pinning.** Standard checks stay on; an internal company certificate is accepted only if it's that exact one, by SHA-256.
- **No account lockouts.** After a rejected password, requests to Exchange stop, and request rates are limited. The log keeps only tool names and timestamps.

### What it doesn't solve

1. **Your mail goes to an AI service.** Everything the agent reads (messages, attachments, transcripts) is sent to the company that runs the agent. A bank or another regulated company needs sign-off from its security team; policy may forbid it.
2. **Emails with "commands" for the agent.** The server asks the agent to treat mail as data, but that's a request, not a lock. The real safeguard is the confirmation window. The agent can read everything; you can't limit it to specific folders yet.
3. **How it installs.** `curl … | sh` installs whatever is on the main branch right now; there are no signed releases or pinned versions yet.
4. **Settings that weaken security** (off by default): "legacy encryption settings" for the server, and a password in `OWA_MAIL_PASSWORD` instead of the keychain.
5. **Access to your computer.** Anyone already signed in as you can use the saved password and the running program.

### Before you connect work email

- Make sure you trust your network.
- Make sure you trust the provider of the model your agent uses: the emails the agent reads are sent to it.

## Commands

| Command | What it does |
|---|---|
| `owa-mail-mcp` | In a terminal, the setup window; when launched by a client, the MCP server over stdio |
| `owa-mail-mcp setup` | Setup window |
| `owa-mail-mcp status` | Version, settings location, whether the password is saved (exit code 0 — configured, 2 — not) |
| `owa-mail-mcp check` | Test the connection |
| `owa-mail-mcp diagnose` | What the server returns at the EWS and OWA addresses, no passwords |

## Troubleshooting

- **"Server unreachable", but web mail opens fine.** The window tells you why. DNS: reconnect your VPN. Connection: set a proxy under Advanced → Proxy. TLS: allow legacy settings for this server.
- **"The server doesn't respond like EWS".** There's a gateway with a web login in front of EWS. The program signs in through OWA on its own and continues on the same connection. If that doesn't help, run `owa-mail-mcp diagnose`.
- **I updated, but the errors are the same.** Restart the MCP server in your client.
- **Where settings live.** `config.json` in `~/Library/Application Support/owa-mail-mcp` (macOS), `%APPDATA%\owa-mail-mcp` (Windows), `~/.config/owa-mail-mcp` (Linux); the password is in the keychain.
- **Exchange Online (Microsoft 365).** Not supported: password sign-in for EWS is disabled there.
