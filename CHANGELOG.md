# Changelog

## Unreleased

- Telegram integration (optional, off by default): a built-in bot sends a message when a tab waits for input or finishes, with "Reply" and "Open" buttons; replies and voice messages are typed into the right tab; `/sessions`, `/new`, `/help`. Enabled by `THUMBMUX_TG_BOT_TOKEN` + a mandatory `THUMBMUX_TG_ALLOWED_CHAT_IDS`; long-poll offset persisted, input rate-limited, token never logged.
- Tests against a fake Telegram Bot API (notification, reply routing, foreign chat ignored, no allowlist = no start, voice, rate limit, restart offset).
- Fix: starting the server when the tmux session already exists crashed (`_ready` was undefined).

## v0.1.0 — first public release

- Mobile web UI for tmux: tabs, session list with live status, swipe scroll, input box, special keys.
- Uploads (photos, files), file manager with trash/undo, favourite folders, split panes.
- Resume Claude Code chats from transcripts.
- Optional voice input through any OpenAI-compatible transcription endpoint.
- Secure by default: loopback bind, mandatory password, signed session cookies, login throttling, same-origin (CSRF/WebSocket) checks, ttyd on a private unix socket behind the login.
- Docker, docker compose, optional VPN kill-switch compose file.
