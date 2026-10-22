"""Authentication, session cookies, login throttling and CSRF/Origin checks.

Design
------
* One shared password. It comes from THUMBMUX_PASSWORD (or
  THUMBMUX_PASSWORD_FILE). If neither is set, a random password is generated on
  first start, stored in ``<data dir>/password`` (mode 600) and printed ONCE.
  There is no built-in default and the server never starts without a password.
* Login sets a signed, expiring, HttpOnly, SameSite=Strict cookie
  (HMAC-SHA256 with a random per-install key in ``<data dir>/session.key``).
* Failed logins are throttled per client address.
* Every state-changing request and every WebSocket upgrade must come from the
  same origin (Origin / Referer / Sec-Fetch-Site check).
"""
import base64
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from urllib.parse import urlparse

import config

COOKIE = "thumbmux_session"
try:
    SESSION_TTL = max(1, int(os.environ.get("THUMBMUX_SESSION_DAYS", "30"))) * 86400
except ValueError:
    SESSION_TTL = 30 * 86400
MIN_PASSWORD_LEN = 8
_WEAK = {"password", "changeme", "change-me", "change_me", "12345678", "admin123",
         "thumbmux", "your-password-here"}

_lock = threading.Lock()
_fails = {}          # client ip -> [count, locked_until]
MAX_FAILS = 5
LOCK_SECONDS = 60


def _write_secret(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)


def load_password():
    """Return the shared password. Never returns an empty or weak value."""
    pw = os.environ.get("THUMBMUX_PASSWORD", "")
    src = "THUMBMUX_PASSWORD"
    pw_file = os.environ.get("THUMBMUX_PASSWORD_FILE", "")
    if not pw and pw_file:
        with open(pw_file, encoding="utf-8") as f:
            pw = f.read().strip()
        src = "THUMBMUX_PASSWORD_FILE"
    if pw:
        if len(pw) < MIN_PASSWORD_LEN or pw.lower() in _WEAK:
            sys.exit("thumbmux: refusing to start - %s is too weak (min %d chars, "
                     "not a placeholder). Pick a longer one or unset it to get a "
                     "random password." % (src, MIN_PASSWORD_LEN))
        return pw
    path = os.path.join(config.DATA_DIR, "password")
    try:
        with open(path, encoding="utf-8") as f:
            stored = f.read().strip()
        if len(stored) >= MIN_PASSWORD_LEN:
            print("thumbmux: using the generated password stored in %s" % path,
                  flush=True)
            return stored
    except FileNotFoundError:
        pass
    pw = secrets.token_urlsafe(18)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    _write_secret(path, pw + "\n")
    bar = "=" * 60
    print("\n%s\n  thumbmux: no THUMBMUX_PASSWORD set - generated one.\n"
          "  Password (shown only this once, also saved to %s):\n\n    %s\n%s\n"
          % (bar, path, pw, bar), flush=True)
    return pw


def _load_key():
    path = os.path.join(config.DATA_DIR, "session.key")
    try:
        with open(path, "rb") as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except FileNotFoundError:
        pass
    k = secrets.token_bytes(48)
    os.makedirs(config.DATA_DIR, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


class Auth:
    def __init__(self):
        os.makedirs(config.DATA_DIR, mode=0o700, exist_ok=True)
        self._pw = hashlib.sha256(load_password().encode()).digest()
        self._key = _load_key()

    # -- password ------------------------------------------------------
    def check_password(self, candidate):
        h = hashlib.sha256((candidate or "").encode()).digest()
        return hmac.compare_digest(h, self._pw)

    # -- cookie --------------------------------------------------------
    def _sig(self, payload):
        return base64.urlsafe_b64encode(
            hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")

    def make_cookie(self):
        payload = "%d.%s" % (int(time.time()) + SESSION_TTL, secrets.token_hex(8))
        return payload + "." + self._sig(payload)

    def valid_cookie(self, value):
        try:
            exp, nonce, sig = value.split(".")
            payload = exp + "." + nonce
            if not hmac.compare_digest(sig, self._sig(payload)):
                return False
            return int(exp) > time.time()
        except Exception:
            return False

    def from_headers(self, headers):
        raw = headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                return self.valid_cookie(v)
        return False

    # -- brute-force throttle -----------------------------------------
    def locked(self, ip):
        with _lock:
            rec = _fails.get(ip)
            return bool(rec and rec[1] > time.time())

    def failed(self, ip):
        with _lock:
            rec = _fails.setdefault(ip, [0, 0.0])
            rec[0] += 1
            if rec[0] >= MAX_FAILS:
                rec[0] = 0
                rec[1] = time.time() + LOCK_SECONDS
            if len(_fails) > 5000:       # keep memory bounded
                _fails.clear()

    def succeeded(self, ip):
        with _lock:
            _fails.pop(ip, None)


# -- same-origin check (CSRF + cross-site WebSocket hijacking) -----------
def _netloc(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def same_origin(headers):
    """True if the request provably comes from our own page.

    Browsers always send Origin on cross-origin POSTs and on WebSocket
    handshakes, and (modern ones) Sec-Fetch-Site. Requests with neither an
    Origin nor a Referer are rejected: our own page always has one.
    """
    site = headers.get("Sec-Fetch-Site", "")
    if site and site not in ("same-origin", "none"):
        return False
    host = (headers.get("Host") or "").lower()
    allowed = {o.split("://", 1)[-1].lower() for o in config.ALLOWED_ORIGINS}
    origin = headers.get("Origin")
    if origin:
        if origin == "null":
            return False
        loc = _netloc(origin)
        return bool(loc) and (loc == host or loc in allowed)
    ref = headers.get("Referer")
    if ref:
        loc = _netloc(ref)
        return bool(loc) and (loc == host or loc in allowed)
    return False
