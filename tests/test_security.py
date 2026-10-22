"""End-to-end security smoke test: starts a real server on a free loopback port
(with a private tmux socket) and checks that nothing works without login.

Run:  python3 -m unittest discover -s tests -v      (needs tmux + ttyd)
"""
import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "test-password-123"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="thumbmux-test-")
        cls.port = free_port()
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env.update({
            "TMUX_TMPDIR": cls.tmp,                 # private tmux server
            "THUMBMUX_SESSION": "ci-test",
            "THUMBMUX_DATA_DIR": os.path.join(cls.tmp, "data"),
            "THUMBMUX_WORKDIR": cls.tmp,
            "THUMBMUX_PORT": str(cls.port),
            "THUMBMUX_PASSWORD": PASSWORD,
            "THUMBMUX_TMUX_CONF": os.path.join(ROOT, "deploy", "tmux.conf"),
        })
        cls.env = env
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "app.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        subprocess.run(["tmux", "kill-server"], env=cls.env, capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data

    def login(self):
        origin = "http://127.0.0.1:%d" % self.port
        st, h, _ = self.req("POST", "/login",
                            urllib.parse.urlencode({"password": PASSWORD, "next": "/m/"}),
                            {"Origin": origin,
                             "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 302)
        return h["Set-Cookie"].split(";")[0], origin

    def test_everything_requires_login(self):
        for path in ("/m/", "/m/api/windows", "/m/api/fs/ls?path=/",
                     "/m/api/settings", "/m/term/", "/m/term/token"):
            st, _, _ = self.req("GET", path)
            self.assertIn(st, (302, 401), path)
        st, _, _ = self.req("POST", "/m/api/type", "id", {"Origin": "http://127.0.0.1"})
        self.assertEqual(st, 401)

    def test_public_endpoints(self):
        self.assertEqual(self.req("GET", "/healthz")[0], 200)
        self.assertEqual(self.req("GET", "/login")[0], 200)

    def test_wrong_password(self):
        st, _, _ = self.req("POST", "/login", "password=nope-nope-nope",
                            {"Origin": "http://127.0.0.1:%d" % self.port,
                             "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 401)

    def test_forged_cookie(self):
        st, _, _ = self.req("GET", "/m/api/windows",
                            headers={"Cookie": "thumbmux_session=9999999999.aa.bb"})
        self.assertEqual(st, 401)

    def test_login_and_csrf(self):
        cookie, origin = self.login()
        self.assertIn("HttpOnly", self.req("POST", "/login",
                      urllib.parse.urlencode({"password": PASSWORD}),
                      {"Origin": origin, "Content-Type":
                       "application/x-www-form-urlencoded"})[1]["Set-Cookie"])
        self.assertEqual(self.req("GET", "/m/api/windows", headers={"Cookie": cookie})[0], 200)
        # state-changing request: needs same origin
        self.assertEqual(self.req("POST", "/m/api/key?k=Enter",
                                  headers={"Cookie": cookie})[0], 403)
        self.assertEqual(self.req("POST", "/m/api/key?k=Enter",
                                  headers={"Cookie": cookie, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/m/api/key?k=Enter",
                                  headers={"Cookie": cookie, "Origin": origin})[0], 200)

    def test_open_redirect_blocked(self):
        origin = "http://127.0.0.1:%d" % self.port
        st, h, _ = self.req("POST", "/login",
                            urllib.parse.urlencode({"password": PASSWORD, "next": "//evil.example"}),
                            {"Origin": origin,
                             "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/m/")

    def test_websocket_needs_auth_and_origin(self):
        cookie, origin = self.login()
        base = ("GET /m/term/ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: tty\r\n" % self.port)

        def status(extra):
            s = socket.create_connection(("127.0.0.1", self.port), 5)
            s.sendall((base + extra + "\r\n").encode())
            line = s.recv(200).split(b"\r\n")[0].decode()
            s.close()
            return line

        self.assertIn(" 401 ", status("Origin: %s\r\n" % origin))
        self.assertIn(" 403 ", status("Cookie: %s\r\nOrigin: https://evil.example\r\n" % cookie))
        self.assertIn(" 403 ", status("Cookie: %s\r\n" % cookie))
        self.assertIn(" 101 ", status("Cookie: %s\r\nOrigin: %s\r\n" % (cookie, origin)))

    def test_weak_password_refused(self):
        env = dict(self.env, THUMBMUX_PASSWORD="password", THUMBMUX_PORT=str(free_port()))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "server", "app.py")],
                           env=env, capture_output=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
