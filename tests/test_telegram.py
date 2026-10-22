"""Telegram integration tests against a FAKE Bot API (local http server).

No request ever leaves the machine and no real token is used. Each class starts
a real server (server/app.py) with a private tmux socket and points
THUMBMUX_TG_API_BASE at the fake API.

Run:  python3 -m unittest discover -s tests -v      (needs tmux + ttyd)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "100000001:FAKE-TOKEN-for-tests-only"
ME, STRANGER = 1001, 2002


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeTelegram:
    """Minimal Bot API: getMe, getUpdates (long-poll), sendMessage, getFile,
    answerCallbackQuery, plus /file/... downloads and an STT endpoint."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates, self.next_id = [], 1
        self.sent, self.calls, self.offsets, self.stt_hits = [], [], [], 0
        self.bad_token_hits = 0
        self.fail_send = 0
        self.msg_id = 100
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/file/bot%s/voice/v1.ogg" % TOKEN:
                    b = b"OggS-fake-audio"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                else:
                    self._json({"ok": False}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                if self.path == "/stt":
                    fake.stt_hits += 1
                    return self._json({"text": "voice words here"})
                parts = self.path.strip("/").split("/")
                if len(parts) != 2 or parts[0] != "bot" + TOKEN:
                    fake.bad_token_hits += 1
                    return self._json({"ok": False, "description": "Unauthorized"}, 401)
                method = parts[1]
                p = json.loads(body or b"{}")
                with fake.lock:
                    fake.calls.append(method)
                if method == "getMe":
                    return self._json({"ok": True, "result": {"username": "fake_bot"}})
                if method == "getUpdates":
                    off = int(p.get("offset") or 0)
                    with fake.lock:
                        fake.offsets.append(off)
                    end = time.time() + min(float(p.get("timeout") or 0), 1.0)
                    while True:
                        with fake.lock:
                            fake.updates = [u for u in fake.updates if u["update_id"] >= off]
                            out = list(fake.updates)
                        if out or time.time() >= end:
                            return self._json({"ok": True, "result": out})
                        time.sleep(0.05)
                if method == "sendMessage":
                    if fake.fail_send:
                        fake.fail_send -= 1
                        return self._json({"ok": False, "description":
                                           "boom for token " + TOKEN}, 500)
                    with fake.lock:
                        fake.msg_id += 1
                        p["message_id"] = fake.msg_id
                        fake.sent.append(p)
                    return self._json({"ok": True, "result": {"message_id": fake.msg_id}})
                if method == "getFile":
                    return self._json({"ok": True, "result": {"file_path": "voice/v1.ogg"}})
                return self._json({"ok": True, "result": True})

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass        # clients hang up mid long-poll on shutdown

        self.httpd = Quiet(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    # -- helpers for tests
    def push(self, **kw):
        with self.lock:
            u = {"update_id": self.next_id}
            u.update(kw)
            self.next_id += 1
            self.updates.append(u)
            return u["update_id"]

    def say(self, text, chat=ME, user=None, reply_to=None):
        m = {"message_id": 5000 + self.next_id, "chat": {"id": chat},
             "from": {"id": chat if user is None else user}, "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        return self.push(message=m)

    def voice(self, chat=ME):
        return self.push(message={"message_id": 7000 + self.next_id, "chat": {"id": chat},
                                  "from": {"id": chat}, "voice": {"file_id": "F1"}})

    def press(self, data, chat=ME):
        return self.push(callback_query={"id": "cb%d" % self.next_id, "data": data,
                                         "from": {"id": chat},
                                         "message": {"message_id": 1, "chat": {"id": chat}}})

    def texts(self, chat=ME):
        with self.lock:
            return [s for s in self.sent if s["chat_id"] == chat]


def wait_for(cond, timeout=25.0, step=0.15):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


class Harness(unittest.TestCase):
    ALLOWED = "%d" % ME
    EXTRA_ENV = {}
    with_server_wait = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="thumbmux-tg-")
        cls.sock = os.path.join(cls.tmp, "tmux.sock")
        cls.fake = FakeTelegram()
        cls.start_server()

    @classmethod
    def start_server(cls):
        cls.port = free_port()
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env.update({
            "THUMBMUX_TMUX_SOCKET": cls.sock,
            "TMUX_TMPDIR": cls.tmp,
            "THUMBMUX_SESSION": "tg-test",
            "THUMBMUX_DATA_DIR": os.path.join(cls.tmp, "data"),
            "THUMBMUX_WORKDIR": cls.tmp,
            "THUMBMUX_PORT": str(cls.port),
            "THUMBMUX_PASSWORD": "test-password-123",
            "THUMBMUX_TMUX_CONF": os.path.join(ROOT, "deploy", "tmux.conf"),
            "THUMBMUX_TG_BOT_TOKEN": TOKEN,
            "THUMBMUX_TG_ALLOWED_CHAT_IDS": cls.ALLOWED,
            "THUMBMUX_TG_API_BASE": cls.fake.base,
            "THUMBMUX_TG_POLL_TIMEOUT": "1",
            "THUMBMUX_TG_MIN_WORK_SECS": "0",
        })
        env.update(cls.EXTRA_ENV)
        cls.log_path = os.path.join(cls.tmp, "server-%d.log" % cls.port)
        cls.logf = open(cls.log_path, "w")
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "app.py")],
            env=env, stdout=cls.logf, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)

    @classmethod
    def stop_server(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.logf.close()

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        cls.tm("kill-server")           # our private socket only
        cls.fake.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def tm(cls, *args):
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        r = subprocess.run(["tmux", "-S", cls.sock, *args], env=env,
                           capture_output=True, text=True)
        return r.stdout

    @classmethod
    def log_text(cls):
        with open(cls.log_path) as f:
            return f.read()

    def pane(self, wid):
        return self.tm("capture-pane", "-p", "-t", wid)

    def new_cat_window(self, name):
        wid = self.tm("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "tg-test",
                      "-n", name, "cat").strip()
        self.assertTrue(wid.startswith("@"), wid)
        # agents fill the screen and their prompt sits at the bottom: the status
        # detector only looks at the last lines of the pane, so push text down
        self.tm("send-keys", "-t", wid, *(["Enter"] * 45))
        return wid


class TelegramEnabled(Harness):
    EXTRA_ENV = {"THUMBMUX_PUBLIC_URL": "https://term.example.com/",
                 "THUMBMUX_TG_RATE_PER_MIN": "6"}

    def test_01_flow(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls), "bot did not start")
        wid = self.new_cat_window("agent")
        other = self.new_cat_window("bystander")
        time.sleep(4.5)       # let the watcher take its baseline of both tabs

        # -- notification: the tab shows a question -> bot messages me, with buttons
        self.tm("send-keys", "-t", wid, "Do you want to proceed? (y/n)", "Enter")
        note = wait_for(lambda: next((s for s in fake.texts()
                                      if "agent" in s["text"] and "waiting" in s["text"]), None))
        self.assertTrue(note, "no notification; sent=%r" % fake.sent[-3:])
        self.assertIn("proceed", note["text"])            # last lines are included
        row = note["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(row[0]["callback_data"], "r:" + wid)
        self.assertIn("Reply", row[0]["text"])
        self.assertEqual(row[1]["url"], "https://term.example.com/m/")  # Open button
        nid = [s for s in fake.sent if s is note][0]["message_id"]

        # -- reply to the notification lands in THAT tab only
        fake.say("hello-from-telegram", reply_to=nid)
        self.assertTrue(wait_for(lambda: "hello-from-telegram" in self.pane(wid)),
                        self.pane(wid))
        self.assertNotIn("hello-from-telegram", self.pane(other))
        self.assertTrue(wait_for(lambda: any("Sent to" in s["text"] for s in fake.texts())))

        # -- "Reply" button -> force_reply prompt -> reply goes to the tab
        fake.press("r:" + wid)
        prompt = wait_for(lambda: next((s for s in fake.texts()
                                        if s["text"].startswith("↩️ Reply to")), None))
        self.assertTrue(prompt)
        self.assertTrue(prompt["reply_markup"].get("force_reply"))
        pid = prompt["message_id"]
        fake.say("via-button-text", reply_to=pid)
        self.assertTrue(wait_for(lambda: "via-button-text" in self.pane(wid)))

        # -- plain message goes to the selected tab
        fake.say("plain-selected")
        self.assertTrue(wait_for(lambda: "plain-selected" in self.pane(wid)))

        # -- /sessions lists tabs with select buttons; picking the other one
        fake.say("/sessions")
        lst = wait_for(lambda: next((s for s in fake.texts()
                                     if "bystander" in s["text"] and "agent" in s["text"]), None))
        self.assertTrue(lst)
        datas = [b["callback_data"] for r in lst["reply_markup"]["inline_keyboard"] for b in r]
        self.assertIn("s:" + other, datas)
        fake.press("s:" + other)
        wait_for(lambda: any("Selected" in s["text"] for s in fake.texts()))
        fake.say("to-bystander")
        self.assertTrue(wait_for(lambda: "to-bystander" in self.pane(other)))
        self.assertNotIn("to-bystander", self.pane(wid))

        # -- /new creates a tab
        fake.say("/new fresh")
        self.assertTrue(wait_for(lambda: "fresh" in self.tm(
            "list-windows", "-t", "tg-test", "-F", "#{window_name}").split()))
        # unknown agent is refused politely
        fake.say("/new x nonexistent-agent")
        self.assertTrue(wait_for(lambda: any("unavailable" in s["text"] for s in fake.texts())))

        # -- /help
        fake.say("/help")
        self.assertTrue(wait_for(lambda: any("/sessions" in s["text"] and "/new" in s["text"]
                                             for s in fake.texts())))

        # -- finished work: a tab prints for a few seconds, then goes quiet -> "finished"
        b = self.tm("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "tg-test",
                    "-n", "builder", "bash").strip()
        self.tm("send-keys", "-t", b, *(["Enter"] * 45))
        time.sleep(4.5)
        self.tm("send-keys", "-t", b,
                "for i in 1 2 3 4 5 6; do echo build step $i; sleep 0.8; done", "Enter")
        done = wait_for(lambda: next((s for s in fake.texts()
                                      if "builder" in s["text"] and "finished" in s["text"]),
                                     None))
        self.assertTrue(done, "no 'finished' message")
        self.assertIn("build step", done["text"])

    def test_02_stranger_is_ignored(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        wid = self.new_cat_window("guarded")
        time.sleep(1)
        before = len(fake.sent)
        fake.say("stranger-command", chat=STRANGER)               # foreign chat
        fake.say("stranger-in-my-chat", chat=ME, user=STRANGER)   # right chat, wrong sender
        fake.press("r:" + wid, chat=STRANGER)
        fake.say("/sessions", chat=STRANGER)
        fake.say("/new evil", chat=STRANGER)
        # a legit message after them proves the queue was processed
        fake.say("/help")
        self.assertTrue(wait_for(lambda: len(fake.sent) > before))
        time.sleep(1)
        self.assertEqual(fake.texts(STRANGER), [])
        self.assertNotIn("stranger", self.pane(wid))
        self.assertNotIn("evil", self.tm("list-windows", "-t", "tg-test",
                                         "-F", "#{window_name}"))
        self.assertTrue(wait_for(lambda: "ignored update from unauthorized chat_id=2002"
                                 in self.log_text()))

    def test_03_voice_via_stt(self):
        """Covered in TelegramVoice (needs a configured STT endpoint)."""

    def test_04_rate_limit(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        wid = self.new_cat_window("flood")
        time.sleep(1)
        fake.press("s:" + wid)
        time.sleep(1)
        for i in range(10):
            fake.say("flood-%d" % i)
        self.assertTrue(wait_for(lambda: any("slow down" in s["text"] for s in fake.texts())))
        typed = sum(1 for i in range(10) if ("flood-%d" % i) in self.pane(wid))
        self.assertLessEqual(typed, 6)

    def test_05_voice_not_configured(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        fake.voice()
        self.assertTrue(wait_for(lambda: any("not configured" in s["text"]
                                             for s in fake.texts())))

    def test_06_offset_on_disk_and_no_token_in_logs(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        fake.fail_send = 1                       # next sendMessage fails with the token in the body
        fake.say("/help")
        self.assertTrue(wait_for(lambda: "send failed" in self.log_text()))
        last = fake.say("/help")
        path = os.path.join(self.tmp, "data", "telegram.json")
        self.assertTrue(wait_for(lambda: os.path.exists(path)
                                 and json.load(open(path))["offset"] >= last + 1))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertNotIn(TOKEN, self.log_text())
        self.assertNotIn(TOKEN.split(":")[1], self.log_text())
        self.assertEqual(fake.bad_token_hits, 0)


class TelegramVoice(Harness):
    def stt_env():
        return {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="thumbmux-tg-")
        cls.sock = os.path.join(cls.tmp, "tmux.sock")
        cls.fake = FakeTelegram()
        cls.EXTRA_ENV = {"THUMBMUX_STT_URL": cls.fake.base + "/stt"}
        cls.start_server()

    def test_voice_is_transcribed_and_typed(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        wid = self.new_cat_window("dictation")
        time.sleep(1)
        fake.press("s:" + wid)
        time.sleep(1)
        fake.voice()
        self.assertTrue(wait_for(lambda: "voice words here" in self.pane(wid)), self.pane(wid))
        self.assertEqual(fake.stt_hits, 1)


class TelegramNoAllowlist(Harness):
    ALLOWED = ""

    def test_does_not_start_without_allowlist(self):
        self.assertTrue(wait_for(lambda: "NOT started" in self.log_text()),
                        self.log_text())
        time.sleep(2.5)
        self.assertEqual(self.fake.calls, [])        # not a single call to the API
        self.assertNotIn(TOKEN, self.log_text())


class TelegramBadAllowlist(Harness):
    ALLOWED = "123, *"

    def test_garbage_allowlist_fails_closed(self):
        self.assertTrue(wait_for(lambda: "NOT started" in self.log_text()))
        time.sleep(1.5)
        self.assertEqual(self.fake.calls, [])


class TelegramDisabledByDefault(Harness):
    EXTRA_ENV = {"THUMBMUX_TG_BOT_TOKEN": "", "THUMBMUX_TG_ALLOWED_CHAT_IDS": "1"}

    def test_no_token_no_bot(self):
        time.sleep(2)
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("Telegram", self.log_text())


class TelegramRestart(Harness):
    def test_offset_survives_restart(self):
        fake = self.fake
        self.assertTrue(wait_for(lambda: "getMe" in fake.calls))
        last = fake.say("/help")
        path = os.path.join(self.tmp, "data", "telegram.json")
        self.assertTrue(wait_for(lambda: os.path.exists(path)
                                 and json.load(open(path))["offset"] == last + 1))
        self.stop_server()
        fake.offsets.clear()
        type(self).start_server()
        self.assertTrue(wait_for(lambda: fake.offsets), "no getUpdates after restart")
        self.assertEqual(fake.offsets[0], last + 1)   # never re-reads old updates


class WatcherUnit(unittest.TestCase):
    """Status-transition rules, without tmux or network."""

    class B:
        def __init__(self):
            self.w = [{"id": "@1", "name": "t", "st": "idle"}]

        def windows(self, fresh=False):
            return [dict(x) for x in self.w]

        def tail(self, wid, n):
            return ["line"]

    def make(self, **kw):
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import tgbot
        d = tempfile.mkdtemp(prefix="thumbmux-tgu-")
        self.addCleanup(shutil.rmtree, d, True)
        b = tgbot.Bot(self.B(), TOKEN, {ME}, d, **kw)
        b.sent_notes = []
        b.notify_tab = lambda w, kind: b.sent_notes.append((w["id"], kind))
        return b

    def test_done_only_after_min_work(self):
        b = self.make(min_work=8)
        b.watch_once(now=100)                       # baseline
        b.b.w[0]["st"] = "work"
        b.watch_once(now=101)
        b.b.w[0]["st"] = "idle"
        b.watch_once(now=103)                       # 2 s of work: too short
        self.assertEqual(b.sent_notes, [])
        b.b.w[0]["st"] = "work"
        b.watch_once(now=200)
        b.b.w[0]["st"] = "done"
        b.watch_once(now=230)                       # 30 s of work -> finished
        self.assertEqual(b.sent_notes, [("@1", "done")])

    def test_ask_fires_once_and_baseline_is_silent(self):
        b = self.make()
        b.b.w[0]["st"] = "ask"
        b.watch_once(now=100)                       # first sight: silent
        self.assertEqual(b.sent_notes, [])
        b.b.w[0]["st"] = "work"
        b.watch_once(now=101)
        b.b.w[0]["st"] = "ask"
        b.watch_once(now=102)
        b.watch_once(now=103)                       # still ask: no repeat
        self.assertEqual(b.sent_notes, [("@1", "ask")])

    def test_notify_filter(self):
        b = self.make(notify=("ask",), min_work=0)
        b.watch_once(now=100)
        b.b.w[0]["st"] = "work"
        b.watch_once(now=101)
        b.b.w[0]["st"] = "done"
        b.watch_once(now=150)
        self.assertEqual(b.sent_notes, [])

    def test_parse_allowed(self):
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import tgbot
        self.assertEqual(tgbot.parse_allowed("1, -100200 ,"), {1, -100200})
        self.assertEqual(tgbot.parse_allowed(""), set())
        for bad in ("*", "abc", "1;2", "12.5"):
            with self.assertRaises(ValueError):
                tgbot.parse_allowed(bad)


if __name__ == "__main__":
    unittest.main()
