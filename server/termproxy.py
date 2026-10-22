"""ttyd supervisor + authenticated reverse proxy (HTTP and WebSocket).

ttyd gives us a real xterm.js terminal attached to the tmux session, but it
has no meaningful access control of its own. So thumbmux:

* starts ttyd itself, listening ONLY on a unix socket inside a private
  (0700) temp directory - there is no TCP listener to find or reach;
* exposes it at /m/term/ through the main server, after the login cookie and
  same-origin checks have passed.
"""
import http.client
import os
import select
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import config

PREFIX = "/m/term"
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade"}
# Never forward the user's session cookie or credentials to the upstream.
_DROP_REQ = _HOP | {"cookie", "authorization", "content-length"}

_proc = None
_sock_dir = None
SOCK = None


def _theme():
    return ('{"foreground":"#d6dbe5","background":"#0b0e14","cursor":"#e6b450",'
            '"cursorAccent":"#0b0e14","selectionBackground":"#2d4f7c",'
            '"black":"#1a1f29","red":"#f07178","green":"#7fd962","yellow":"#ffb454",'
            '"blue":"#59c2ff","magenta":"#d2a6ff","cyan":"#5ccfe6","white":"#c7c7c7",'
            '"brightBlack":"#686868","brightRed":"#ff8f93","brightGreen":"#a6e22e",'
            '"brightYellow":"#ffd173","brightBlue":"#73d0ff","brightMagenta":"#dfbfff",'
            '"brightCyan":"#95e6cb","brightWhite":"#ffffff"}')


def start():
    """Launch ttyd on a private unix socket. Returns the socket path."""
    global _proc, _sock_dir, SOCK
    ttyd = shutil.which(config.TTYD_BIN)
    if not ttyd:
        raise SystemExit("thumbmux: ttyd not found. Install it (apt install ttyd / "
                         "brew install ttyd) or set THUMBMUX_TTYD.")
    _sock_dir = tempfile.mkdtemp(prefix="thumbmux-")
    os.chmod(_sock_dir, 0o700)
    SOCK = os.path.join(_sock_dir, "ttyd.sock")
    sess = shlex.quote(config.SESSION)
    attach = ("tmux has-session -t %s 2>/dev/null || tmux new-session -d -s %s; "
              "exec tmux attach -t %s" % (sess, sess, sess))
    cmd = [ttyd, "-i", SOCK, "-W", "-O", "-T", "xterm-256color", "-m", "3",
           "-t", "fontSize=%d" % config.TTYD_FONT_SIZE,
           "-t", "scrollback=3000", "-t", "disableLeaveAlert=true",
           "-t", "rendererType=dom", "-t", "smoothScrollDuration=0",
           "-t", "disableResizeOverlay=true", "-t", "theme=" + _theme(),
           "sh", "-c", attach]
    env = dict(os.environ, COLORTERM="truecolor")
    # the login password must not leak into every shell the terminal spawns
    for k in list(env):
        if k.startswith("THUMBMUX_"):
            env.pop(k)
    _proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if os.path.exists(SOCK):
            break
        if _proc.poll() is not None:
            raise SystemExit("thumbmux: ttyd exited right after start")
        time.sleep(0.1)
    threading.Thread(target=_watch, daemon=True).start()
    return SOCK


def _watch():
    """Restart ttyd if it dies."""
    global _proc
    while True:
        time.sleep(3)
        if _proc and _proc.poll() is not None:
            try:
                os.unlink(SOCK)
            except OSError:
                pass
            cmd = _proc.args
            env = dict(os.environ, COLORTERM="truecolor")
            for k in list(env):
                if k.startswith("THUMBMUX_"):
                    env.pop(k)
            _proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)


def stop():
    if _proc and _proc.poll() is None:
        _proc.terminate()
    if _sock_dir:
        shutil.rmtree(_sock_dir, ignore_errors=True)


def _upstream_path(raw_path):
    p = raw_path[len(PREFIX):] or "/"
    return p if p.startswith("/") else "/" + p


class _UnixConn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=30)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(SOCK)


def proxy_http(h):
    """Forward a plain HTTP request to ttyd and relay the response."""
    conn = _UnixConn()
    try:
        headers = {k: v for k, v in h.headers.items() if k.lower() not in _DROP_REQ}
        body = None
        try:
            n = int(h.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n:
            body = h.rfile.read(min(n, 1 << 20))
        conn.request(h.command, _upstream_path(h.path), body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
    except Exception:
        h._send(502, {"error": "terminal backend unavailable"})
        return
    finally:
        conn.close()
    h.send_response(r.status)
    for k, v in r.getheaders():
        if k.lower() in _HOP or k.lower() == "content-length":
            continue
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)


def proxy_ws(h):
    """Tunnel a WebSocket upgrade to ttyd (auth + origin already checked)."""
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.settimeout(10)
        up.connect(SOCK)
    except OSError:
        up.close()
        h._send(502, {"error": "terminal backend unavailable"})
        return
    lines = ["GET %s HTTP/1.1" % _upstream_path(h.path)]
    for k, v in h.headers.items():
        if k.lower() in ("cookie", "authorization"):
            continue
        lines.append("%s: %s" % (k, v))
    up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))
    up.settimeout(None)
    cli = h.connection
    cli.settimeout(None)
    h.close_connection = True
    socks = [cli, up]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 60)
            if not r:
                continue
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (up if s is cli else cli).sendall(data)
    except OSError:
        pass
    finally:
        try:
            up.close()
        except OSError:
            pass
