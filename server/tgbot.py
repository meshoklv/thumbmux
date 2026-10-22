"""Optional Telegram bot: notifications from tabs + replies typed back into them.

Off unless THUMBMUX_TG_BOT_TOKEN is set. It refuses to start without
THUMBMUX_TG_ALLOWED_CHAT_IDS, because whoever can talk to the bot can type into
your shell. Updates from any other chat or sender are dropped silently (and
logged without their content). The token is never written to a log.

One poller thread is the only consumer of getUpdates (long-poll, offset kept
on disk); one watcher thread turns tab status changes (ask / work -> done)
into messages with inline buttons. Standard library only.
"""
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

MAP_KEEP = 500          # message_id -> tab entries remembered
MAX_INPUT = 4000        # characters per message typed into a tab
NOTIFY_GAP = 20         # seconds between two notifications for the same tab
ICON = {"ask": "🔴", "work": "🟡", "done": "🟢", "idle": "⚪"}
LABEL = {"ask": "waiting for you", "work": "working", "done": "done",
         "idle": "idle"}
NAME_RE = re.compile(r"[^\w \-.]", re.UNICODE)


def log(msg):
    print("[telegram] " + msg, flush=True)


def parse_allowed(raw):
    """'123, -100456' -> {123, -100456}. Any junk entry -> ValueError (fail closed)."""
    out = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not re.fullmatch(r"-?\d{1,20}", part):
            raise ValueError("bad entry in THUMBMUX_TG_ALLOWED_CHAT_IDS "
                             "(expected comma-separated numeric ids)")
        out.add(int(part))
    return out


def esc(s):
    return html.escape(s, quote=False)


class TgError(Exception):
    pass


class Api:
    """Thin Bot API client. Error messages never contain the token."""

    def __init__(self, token, base):
        self.token, self.base = token, base

    def clean(self, s):
        return str(s).replace(self.token, "<token>")

    def call(self, method, params=None, timeout=15):
        url = "%s/bot%s/%s" % (self.base, self.token, method)
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise TgError("%s -> HTTP %d" % (method, e.code))
        except Exception as e:
            raise TgError("%s failed: %s" % (method, self.clean(e)[:120]))
        try:
            j = json.loads(body)
        except ValueError:
            raise TgError("%s: bad JSON" % method)
        if not j.get("ok"):
            raise TgError("%s: %s" % (method, self.clean(j.get("description", ""))[:120]))
        return j.get("result")

    def download(self, file_path, limit=20 * 1024 * 1024):
        url = "%s/file/bot%s/%s" % (self.base, self.token, file_path)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read(limit + 1)[:limit]
        except Exception as e:
            raise TgError("download failed: %s" % self.clean(e)[:120])


class Bot:
    """backend must provide: windows() -> [{id,name,st,...}], tail(wid, n) -> [str],
    send(wid, text) -> bool, new_window(name, agent) -> wid."""

    def __init__(self, backend, token, allowed, data_dir, public_url="",
                 api_base="https://api.telegram.org", stt=None,
                 poll_timeout=25, rate_per_min=20, min_work=8,
                 notify=("ask", "done"), tail_lines=10):
        if not token:
            raise ValueError("no token")
        if not allowed:
            raise ValueError("allowlist is empty")
        self.b = backend
        self.api = Api(token, api_base)
        self.allowed = set(allowed)
        self.public_url = public_url
        self.stt = stt                      # object with enabled() / transcribe(raw, ext)
        self.poll_timeout = poll_timeout
        self.rate = max(1, rate_per_min)
        self.min_work = min_work
        self.notify = set(notify)
        self.tail_lines = tail_lines
        self.state_path = os.path.join(data_dir, "telegram.json")
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        st = self._load()
        self.offset = int(st.get("offset", 0))
        self.msgmap = st.get("msgmap", {})          # "chat:msg" -> window id
        self.selected = st.get("selected", {})      # str(chat) -> window id
        self.sent_times = {}                        # chat -> [ts]
        self.rate_warned = {}
        self.prev = {}                              # wid -> {"st","work_since","note"}

    # -- persistence ------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        with self.lock:
            snap = {"offset": self.offset, "selected": self.selected,
                    "msgmap": dict(list(self.msgmap.items())[-MAP_KEEP:])}
            os.makedirs(os.path.dirname(self.state_path), mode=0o700, exist_ok=True)
            tmp = self.state_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, self.state_path)

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        try:
            me = self.api.call("getMe") or {}
            log("bot @%s ready, %d allowed chat id(s)" % (me.get("username", "?"),
                                                           len(self.allowed)))
        except TgError as e:
            log("getMe failed (%s) - the poller keeps retrying" % e)
        for fn in (self._poll_loop, self._watch_loop):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    # -- polling ------------------------------------------------------------
    def _poll_loop(self):
        backoff = 1
        while not self.stop_ev.is_set():
            try:
                ups = self.api.call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": self.poll_timeout,
                     "allowed_updates": ["message", "callback_query"]},
                    timeout=self.poll_timeout + 10) or []
                backoff = 1
            except TgError as e:
                log("poll error: %s (retry in %ds)" % (e, backoff))
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in ups:
                try:
                    self.handle_update(u)
                except Exception as e:      # one bad update must not kill the loop
                    log("update failed: %s" % type(e).__name__)
                self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            if ups:
                self._save()
            elif self.poll_timeout == 0:
                self.stop_ev.wait(0.5)

    def authorized(self, chat_id, user_id):
        """Chat AND sender must be on the allowlist (matters in group chats)."""
        return chat_id in self.allowed and user_id in self.allowed

    def handle_update(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            uid = (cq.get("from") or {}).get("id")
            if not self.authorized(chat, uid):
                log("ignored callback from unauthorized chat_id=%s" % chat)
                return
            return self.on_callback(cq, chat)
        m = u.get("message")
        if not m:
            return
        chat = (m.get("chat") or {}).get("id")
        uid = (m.get("from") or {}).get("id")
        if not self.authorized(chat, uid):
            log("ignored update from unauthorized chat_id=%s" % chat)
            return
        if m.get("voice") or m.get("audio"):
            return self.on_voice(m, chat)
        text = (m.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            return self.on_command(text, chat)
        return self.on_text(m, text, chat)

    # -- sending ------------------------------------------------------------
    def send(self, chat, text, markup=None, wid=None):
        p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        try:
            r = self.api.call("sendMessage", p)
        except TgError as e:
            log("send failed: %s" % e)
            return None
        if wid and r and "message_id" in r:
            with self.lock:
                self.msgmap["%s:%s" % (chat, r["message_id"])] = wid
            self._save()
        return r

    def buttons(self, wid):
        row = [{"text": "↩️ Reply", "callback_data": "r:" + wid}]
        if self.public_url:
            row.append({"text": "📺 Open", "url": self.public_url + "/m/"})
        return {"inline_keyboard": [row]}

    # -- tab helpers --------------------------------------------------------
    def window(self, wid):
        for fresh in (False, True):        # second look bypasses the status cache
            for w in self.b.windows(fresh=fresh):
                if w["id"] == wid:
                    return w
        return None

    def tail_html(self, wid):
        lines = self.b.tail(wid, self.tail_lines)
        body = "\n".join(l.rstrip() for l in lines)[-1500:]
        return "<pre>%s</pre>" % esc(body) if body.strip() else ""

    def allow_input(self, chat):
        now = time.time()
        with self.lock:
            q = [t for t in self.sent_times.get(chat, []) if now - t < 60]
            ok = len(q) < self.rate
            if ok:
                q.append(now)
            self.sent_times[chat] = q
            return ok

    def deliver(self, chat, wid, text):
        """Type text into tab wid (after the rate limit); tell the chat what happened."""
        if not self.allow_input(chat):
            if time.time() - self.rate_warned.get(chat, 0) > 30:
                self.rate_warned[chat] = time.time()
                self.send(chat, "⏳ Too many messages - slow down (limit %d/min)." % self.rate)
            return False
        if len(text) > MAX_INPUT:
            self.send(chat, "Message too long (max %d characters)." % MAX_INPUT)
            return False
        w = self.window(wid)
        if not w:
            self.send(chat, "That tab no longer exists. Pick one with /sessions.")
            return False
        if not self.b.send(wid, text):
            self.send(chat, "⚠️ Could not type into <b>%s</b>." % esc(w["name"]))
            return False
        self.send(chat, "✅ Sent to <b>%s</b>" % esc(w["name"]), wid=wid)
        return True

    def target_for(self, m, chat):
        rt = m.get("reply_to_message")
        if rt:
            wid = self.msgmap.get("%s:%s" % (chat, rt.get("message_id")))
            if wid:
                return wid
        return self.selected.get(str(chat))

    # -- handlers -----------------------------------------------------------
    def on_text(self, m, text, chat):
        wid = self.target_for(m, chat)
        if not wid:
            return self.send(chat, "No tab selected. Reply to a notification or use /sessions.")
        self.deliver(chat, wid, text)

    def on_voice(self, m, chat):
        if not (self.stt and self.stt.enabled()):
            return self.send(chat, "🎙 Voice is not configured on this server "
                                   "(set THUMBMUX_STT_URL). Send text instead.")
        wid = self.target_for(m, chat)
        if not wid:
            return self.send(chat, "No tab selected. Reply to a notification or use /sessions.")
        media = m.get("voice") or m.get("audio")
        try:
            info = self.api.call("getFile", {"file_id": media["file_id"]})
            raw = self.api.download(info["file_path"])
            ext = os.path.splitext(info["file_path"])[1] or ".ogg"
            text = self.stt.transcribe(raw, ext)
        except Exception as e:
            return self.send(chat, "🎙 Could not transcribe: %s" % esc(
                self.api.clean(e)[:150]))
        if not text:
            return self.send(chat, "🎙 Nothing recognised.")
        self.send(chat, "🎙 <i>%s</i>" % esc(text))
        self.deliver(chat, wid, text)

    def on_callback(self, cq, chat):
        kind, _, wid = (cq.get("data") or "").partition(":")
        try:
            self.api.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        except TgError:
            pass
        w = self.window(wid) if wid else None
        if kind not in ("r", "s") or not w:
            return self.send(chat, "That tab no longer exists. Use /sessions.")
        with self.lock:
            self.selected[str(chat)] = wid
        self._save()
        if kind == "r":
            self.send_prompt(chat, w)
        else:
            self.send(chat, "Selected <b>%s</b> %s. Your next messages go there."
                      % (esc(w["name"]), ICON.get(w["st"], "")), wid=wid)

    def send_prompt(self, chat, w):
        self.send(chat, "↩️ Reply to <b>%s</b>:" % esc(w["name"]),
                  {"force_reply": True, "selective": True,
                   "input_field_placeholder": "Type into this tab"}, wid=w["id"])

    def on_command(self, text, chat):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            return self.send(chat, (
                "<b>thumbmux bot</b>\n"
                "/sessions - list tabs, pick one\n"
                "/new &lt;name&gt; [agent] - open a new tab\n"
                "/help - this text\n\n"
                "Reply to a notification (or tap ↩️ Reply) to type into that tab. "
                "Plain messages go to the tab you picked last. "
                "Voice messages are transcribed if voice input is configured."))
        if cmd == "/sessions":
            return self.cmd_sessions(chat)
        if cmd == "/new":
            return self.cmd_new(chat, arg)
        self.send(chat, "Unknown command. /help")

    def cmd_sessions(self, chat):
        wins = self.b.windows()
        if not wins:
            return self.send(chat, "No tabs.")
        sel = self.selected.get(str(chat))
        lines, rows = [], []
        for w in wins:
            mark = " ◀" if w["id"] == sel else ""
            lines.append("%s <b>%s</b> - %s%s" % (ICON.get(w["st"], ""), esc(w["name"]),
                                                  LABEL.get(w["st"], w["st"]), mark))
            rows.append([{"text": "%s %s" % (ICON.get(w["st"], ""), w["name"][:30]),
                          "callback_data": "s:" + w["id"]}])
        self.send(chat, "\n".join(lines), {"inline_keyboard": rows})

    def cmd_new(self, chat, arg):
        parts = arg.split()
        if not parts:
            return self.send(chat, "Usage: /new &lt;name&gt; [agent]")
        name = NAME_RE.sub("", parts[0])[:40].strip()
        agent = parts[1] if len(parts) > 1 else None
        if not name:
            return self.send(chat, "Bad name.")
        if not self.allow_input(chat):
            return self.send(chat, "⏳ Too many requests - slow down.")
        try:
            wid = self.b.new_window(name, agent)
        except ValueError as e:
            return self.send(chat, "⚠️ %s" % esc(str(e)))
        except Exception:
            return self.send(chat, "⚠️ Could not create the tab.")
        with self.lock:
            self.selected[str(chat)] = wid
        self._save()
        self.send(chat, "🆕 Tab <b>%s</b> created and selected." % esc(name),
                  self.buttons(wid), wid=wid)

    # -- notifications ------------------------------------------------------
    def _watch_loop(self):
        while not self.stop_ev.is_set():
            try:
                self.watch_once()
            except Exception as e:
                log("watcher error: %s" % type(e).__name__)
            self.stop_ev.wait(2.0)

    def watch_once(self, now=None):
        """Compare each tab's status with the previous tick; notify on
        -> ask, and on work -> done/idle after at least min_work seconds."""
        now = time.time() if now is None else now
        seen = set()
        for w in self.b.windows():
            wid, st = w["id"], w["st"]
            seen.add(wid)
            p = self.prev.get(wid)
            if p is None:                       # first sight: baseline only
                self.prev[wid] = {"st": st, "work_since": now if st == "work" else 0,
                                  "note": 0.0}
                continue
            old = p["st"]
            if st == old:
                continue
            p["st"] = st
            fire = None
            if st == "work":
                p["work_since"] = now
            elif st == "ask":
                if "ask" in self.notify:
                    fire = "ask"
            elif old == "work" and "done" in self.notify \
                    and now - p["work_since"] >= self.min_work:
                fire = "done"
            if fire and now - p["note"] >= NOTIFY_GAP:
                p["note"] = now
                self.notify_tab(w, fire)
        for gone in [k for k in self.prev if k not in seen]:
            del self.prev[gone]

    def notify_tab(self, w, kind):
        head = ("🔴 <b>%s</b> is waiting for you" if kind == "ask"
                else "🟢 <b>%s</b> finished") % esc(w["name"])
        text = head + "\n" + self.tail_html(w["id"])
        for chat in sorted(self.allowed):
            self.send(chat, text, self.buttons(w["id"]), wid=w["id"])
