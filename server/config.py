"""Runtime configuration for thumbmux.

Everything is read from environment variables (see ``.env.example``).
Nothing in here is a secret and nothing here has a default password:
authentication is configured in ``auth.py`` (THUMBMUX_PASSWORD).
"""
import os

# Never act on the tmux server of whoever launched us (a nested $TMUX would
# silently redirect every tmux command there). Use TMUX_TMPDIR to choose a socket.
os.environ.pop("TMUX", None)


def _env(name, default=""):
    return os.environ.get(name, default)


def _bool(name, default=False):
    v = _env(name, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --- network -----------------------------------------------------------
# Loopback by default. Only change this if something in front of the server
# (TLS reverse proxy, SSH tunnel, Tailscale ...) is doing the exposing.
HOST = _env("THUMBMUX_HOST", "127.0.0.1")
PORT = _int("THUMBMUX_PORT", 7682)
# Honour X-Forwarded-For / X-Forwarded-Proto from a reverse proxy you control.
TRUST_PROXY = _bool("THUMBMUX_TRUST_PROXY")
# Mark the session cookie Secure even when the proxy header is not trusted.
COOKIE_SECURE = _bool("THUMBMUX_COOKIE_SECURE")
# Extra allowed Origins for POST / WebSocket (comma separated, full origins).
# The page's own Host is always allowed.
ALLOWED_ORIGINS = [s.strip().rstrip("/") for s in
                   _env("THUMBMUX_ALLOWED_ORIGINS").split(",") if s.strip()]

# --- tmux --------------------------------------------------------------
SESSION = _env("THUMBMUX_SESSION", "phone")
WORKDIR = _env("THUMBMUX_WORKDIR") or os.path.expanduser("~")
TMUX_CONF = _env("THUMBMUX_TMUX_CONF") or os.path.expanduser("~/.tmux.conf")
# Command names offered in the "new tab" menu. "shell" is always allowed.
AGENTS = [s.strip() for s in
          _env("THUMBMUX_AGENTS", "claude,codex,opencode,gemini,aider").split(",")
          if s.strip()]

# --- storage -----------------------------------------------------------
DATA_DIR = _env("THUMBMUX_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".thumbmux")
UPLOAD_DIR = _env("THUMBMUX_UPLOAD_DIR") or os.path.join(WORKDIR, ".uploads")
CLAUDE_PROJECTS_DIR = _env("THUMBMUX_CLAUDE_PROJECTS") or os.path.expanduser(
    "~/.claude/projects")

# --- terminal (ttyd) ---------------------------------------------------
# thumbmux starts ttyd itself on a private unix socket and reverse-proxies it
# behind the login. ttyd never listens on a TCP port.
TTYD_BIN = _env("THUMBMUX_TTYD", "ttyd")
TTYD_FONT_SIZE = _int("THUMBMUX_FONT_SIZE", 13)

# --- voice input (optional, off by default) ---------------------------
# Any OpenAI-compatible /audio/transcriptions endpoint: OpenAI, Groq, or a
# local server (faster-whisper-server, whisper.cpp server, ...).
STT_URL = _env("THUMBMUX_STT_URL")
STT_KEY = _env("THUMBMUX_STT_KEY")
STT_MODEL = _env("THUMBMUX_STT_MODEL", "whisper-1")
STT_LANGUAGE = _env("THUMBMUX_STT_LANGUAGE")
STT_TIMEOUT = _int("THUMBMUX_STT_TIMEOUT", 120)

# --- limits ------------------------------------------------------------
MAX_UPLOAD = 15 * 1024 * 1024        # photos
MAX_FILE = 100 * 1024 * 1024         # arbitrary files into the project dir
MAX_AUDIO = 30 * 1024 * 1024
