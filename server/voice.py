"""Optional voice input: speech-to-text through ANY OpenAI-compatible endpoint.

Disabled unless THUMBMUX_STT_URL is set. Works with OpenAI, Groq, or a local
server such as faster-whisper-server / whisper.cpp. Audio is sent only to the
URL you configured - nothing else is contacted.
"""
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

import config

JOBS_KEEP = 12
_jobs = {}
_lock = threading.Lock()
_last = [""]
LABELS = {"stt": "Speech-to-text"}


def enabled():
    return bool(config.STT_URL)


def _multipart(fields, fname, ctype, data):
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (b, k, v)).encode())
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (b, fname, ctype)).encode())
    out.append(data)
    out.append(("\r\n--%s--\r\n" % b).encode())
    return b"".join(out), "multipart/form-data; boundary=" + b


def transcribe(raw, ext):
    """Send audio to the configured endpoint, return the text."""
    if not enabled():
        raise RuntimeError("voice input is not configured (THUMBMUX_STT_URL)")
    ext = (ext or ".webm").lstrip(".") or "webm"
    ctype = {"mp4": "audio/mp4", "m4a": "audio/mp4", "ogg": "audio/ogg",
             "mp3": "audio/mpeg", "wav": "audio/wav"}.get(ext, "audio/webm")
    fields = {"model": config.STT_MODEL, "response_format": "json"}
    if config.STT_LANGUAGE:
        fields["language"] = config.STT_LANGUAGE
    body, ct = _multipart(fields, "audio." + ext, ctype, raw)
    req = urllib.request.Request(config.STT_URL, data=body, method="POST")
    req.add_header("Content-Type", ct)
    if config.STT_KEY:
        req.add_header("Authorization", "Bearer " + config.STT_KEY)
    try:
        with urllib.request.urlopen(req, timeout=config.STT_TIMEOUT) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError("STT endpoint answered %d %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("STT endpoint unreachable: %s" % str(e)[:160])
    try:
        text = json.loads(payload).get("text", "")
    except ValueError:
        text = payload.decode("utf-8", "replace")
    return (text or "").strip()


def start_job(raw, ext):
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {"state": "run", "stage": "stt", "tried": [], "text": "",
                      "engine": "", "error": "", "started": time.time(),
                      "chain": ["stt"]}
        for old in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-JOBS_KEEP]:
            _jobs.pop(old, None)
    _last[0] = jid

    def worker():
        t0 = time.time()
        try:
            text = transcribe(raw, ext)
            upd = {"state": "done", "text": text, "engine": "stt", "stage": "",
                   "tried": [{"id": "stt", "ok": True, "why": "",
                              "secs": round(time.time() - t0, 1)}]}
        except Exception as e:
            upd = {"state": "error", "error": str(e)[:300], "stage": "",
                   "tried": [{"id": "stt", "ok": False, "why": str(e)[:120],
                              "secs": round(time.time() - t0, 1)}]}
        upd["took"] = round(time.time() - t0, 1)
        with _lock:
            if jid in _jobs:
                _jobs[jid].update(upd)

    threading.Thread(target=worker, daemon=True).start()
    return jid


def view(jid=""):
    jid = jid or _last[0]
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return {"job": "", "state": "none"}
        v = dict(j)
    v["job"] = jid
    v["labels"] = LABELS
    return v
