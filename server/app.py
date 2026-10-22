#!/usr/bin/env python3
"""thumbmux - mobile web UI for tmux sessions (and the AI agents inside them).

Serves the mobile page at /m/ plus a small JSON API that drives tmux windows,
and reverse-proxies the real terminal (ttyd) at /m/term/. EVERYTHING except
/login and /healthz requires the login cookie - see auth.py.
"""

import base64
import datetime
import html as htmllib
import json
import mimetypes
import os
import contextlib
import posixpath
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import config
import auth as authmod
import termproxy
import voice
import tgbot

SESSION = config.SESSION
# Version of the code = fingerprint of this file at startup. Exposed through
# /m/api/health; the page compares it with its own and reloads itself after an
# update, so a phone tab never lives for weeks on stale JS.
try:
    import hashlib as _hl
    with open(os.path.abspath(__file__), "rb") as _f:
        APP_VER = _hl.md5(_f.read()).hexdigest()[:12]
except Exception:
    APP_VER = str(int(time.time()))
WORKDIR = config.WORKDIR
LISTEN = (config.HOST, config.PORT)
LOCAL_MODE = True      # tmux runs on the same machine / container as this server
CONTAINER = ""


def _wrap(cmd, interactive=False):
    """Kept as a seam: commands run directly (tmux lives next to this server)."""
    return cmd


UPLOAD_DIR_HOST = UPLOAD_DIR_CTR = config.UPLOAD_DIR
MAX_UPLOAD = config.MAX_UPLOAD
MAX_FILE = config.MAX_FILE
DATA_DIR = config.DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)
FAVS_FILE = os.path.join(DATA_DIR, "favorites.json")


def _norm_path(p):
    p = (p or "").strip()
    return p.rstrip("/") or "/"


_favs_lock = threading.Lock()  # threaded server: read-modify-write under a lock


def _read_favs(path):
    with open(path, encoding="utf-8") as f:
        return [x for x in json.load(f) if isinstance(x, str)]


def load_favs():
    # main file; if corrupt/missing, fall back to the backup (never lose data for good)
    for p in (FAVS_FILE, FAVS_FILE + ".bak"):
        try:
            return _read_favs(p)
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return []


def save_favs(lst):
    # atomic (temp + rename) + mirrored .bak as a safety net
    fd, tmp = tempfile.mkstemp(prefix="favs_", dir=os.path.dirname(FAVS_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False)
        os.replace(tmp, FAVS_FILE)
        try:
            with open(FAVS_FILE + ".bak", "w", encoding="utf-8") as f:
                json.dump(lst, f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def update_fav(path, op):
    """Atomically add/remove a favorite under the lock; otherwise two fast
    star taps race on the file and one favorite is lost."""
    with _favs_lock:
        favs = load_favs()
        if op == "add" and path not in favs:
            favs.append(path)
        elif op == "remove":
            favs = [x for x in favs if x != path]
        save_favs(favs)
        return favs


def reorder_favs(order):
    """Reorder favorites by `order` (existing paths only;
    omitted ones are appended at the end in their previous relative order)."""
    with _favs_lock:
        cur = load_favs()
        seen, new = set(), []
        for p in order:
            if p in cur and p not in seen:
                new.append(p); seen.add(p)
        for p in cur:
            if p not in seen:
                new.append(p); seen.add(p)
        save_favs(new)
        return new


# ── chat folders (window grouping) ─────────────────────────────────────
# Storage modeled on favorites.json: atomic temp+os.replace write under a
# separate lock (threaded server). Window->folder binding uses the STABLE
# window_id (the tmux window index changes on reorder/kill, the id does not).
# folders.json format:
#   {"folders": [{"id": "<uuid8>", "name": "Work", "order": 0}, ...],
#    "members": {"@12": ["<folder_id>", ...], "@7": [...]}}
# folders is the ordered folder list; members maps window_id -> list of folder
# ids the window belongs to (a window may be in several folders).
FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
_folders_lock = threading.Lock()


def _empty_folders():
    return {"folders": [], "members": {}}


def load_folders():
    for p in (FOLDERS_FILE, FOLDERS_FILE + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            folders = [x for x in data.get("folders", [])
                       if isinstance(x, dict) and x.get("id") and x.get("name")]
            members = data.get("members", {})
            if not isinstance(members, dict):
                members = {}
            # clean up membership: string folder ids only
            valid_ids = {f["id"] for f in folders}
            clean_members = {}
            for win, fids in members.items():
                if isinstance(fids, list):
                    keep = [fid for fid in fids
                            if isinstance(fid, str) and fid in valid_ids]
                    if keep:
                        clean_members[win] = keep
            return {"folders": folders, "members": clean_members}
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return _empty_folders()


def save_folders(data):
    fd, tmp = tempfile.mkstemp(prefix="folders_", dir=os.path.dirname(FOLDERS_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, FOLDERS_FILE)
        try:
            with open(FOLDERS_FILE + ".bak", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def folders_op(op, args):
    """folders.json mutations under the lock (atomic). Returns the fresh object.
    args is a dict of string request parameters (name/id/win/folder/ids)."""
    with _folders_lock:
        data = load_folders()
        folders = data["folders"]
        members = data["members"]
        by_id = {f["id"]: f for f in folders}
        if op == "create":
            name = (args.get("name") or "").strip()[:40]
            if name:
                fid = uuid.uuid4().hex[:8]
                order = (max((f.get("order", 0) for f in folders), default=-1) + 1)
                folders.append({"id": fid, "name": name, "order": order})
        elif op == "rename":
            fid = args.get("id")
            name = (args.get("name") or "").strip()[:40]
            if fid in by_id and name:
                by_id[fid]["name"] = name
        elif op == "delete":
            fid = args.get("id")
            if fid in by_id:
                data["folders"] = [f for f in folders if f["id"] != fid]
                # drop from membership + remove emptied entries
                new_members = {}
                for win, fids in members.items():
                    keep = [x for x in fids if x != fid]
                    if keep:
                        new_members[win] = keep
                data["members"] = new_members
        elif op == "assign":
            win = args.get("win")
            fid = args.get("folder")
            if win and fid in by_id:
                cur = members.setdefault(win, [])
                if fid not in cur:
                    cur.append(fid)
        elif op == "unassign":
            win = args.get("win")
            fid = args.get("folder")
            if win and win in members:
                keep = [x for x in members[win] if x != fid]
                if keep:
                    members[win] = keep
                else:
                    del members[win]
        elif op == "reorder":
            raw = args.get("ids") or ""
            wanted = [x for x in raw.split(",") if x]
            pos = {fid: i for i, fid in enumerate(wanted)}
            for f in folders:
                if f["id"] in pos:
                    f["order"] = pos[f["id"]]
            data["folders"] = sorted(folders, key=lambda f: f.get("order", 0))
        save_folders(data)
        return data


# -- server-side settings (survive switching device) ----------------------
# settings.json: JSON object, POST merges a patch (shallow). Keys: default_cwd
# (folder for new tabs), default_cmd (agent started by the "+" button).
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
_settings_lock = threading.Lock()
SETTINGS_DEFAULTS = {"default_cwd": WORKDIR, "default_cmd": "shell"}
_AGENT_CMDS = [c for c in config.AGENTS if re.match(r"^[A-Za-z0-9._-]{1,32}$", c)]
_ALLOWED_CMDS = {"shell"} | set(_AGENT_CMDS)
# model= in /m/api/new: start Claude with an explicit model. The value ends up
# on a command line, so only a safe alphabet is accepted (and shlex.quote'd).
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,64}$")
_NICE = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode",
         "gemini": "Gemini", "aider": "Aider"}
_AGENT_LABELS = [("shell", "Terminal")] + [(c, _NICE.get(c, c)) for c in _AGENT_CMDS]


def load_settings():
    for p in (SETTINGS_FILE, SETTINGS_FILE + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged = dict(SETTINGS_DEFAULTS)
                merged.update(data)
                return merged
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return dict(SETTINGS_DEFAULTS)


def save_settings(data):
    fd, tmp = tempfile.mkstemp(prefix="settings_", dir=os.path.dirname(SETTINGS_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
        try:
            with open(SETTINGS_FILE + ".bak", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def patch_settings(patch):
    """Merge a patch (dict) into settings.json atomically under the lock. Known
    keys are validated; unknown ones are kept too (extensible for the frontend)."""
    with _settings_lock:
        data = load_settings()
        if isinstance(patch, dict):
            for k, v in patch.items():
                if k == "default_cmd":
                    if isinstance(v, str) and v in _ALLOWED_CMDS:
                        data[k] = v
                elif k == "default_cwd":
                    if isinstance(v, str) and v.strip():
                        data[k] = v.strip()
                else:
                    data[k] = v
        save_settings(data)
        return data


# ── file manager: trash, undo journal, sandbox ────────────────────────────
# Storage modeled on folders.json: atomic temp+os.replace write under a
# separate lock (threaded server). fs-undo.json format:
#   {"ops": [{"id": "<uuid8>", "op": "delete", "ts": 1690000000,
#             "label": "delete 'foo.txt'", ...paths...}, ...]}
# The top of the stack is the LAST list element. Each entry is self-contained:
#   mkdir  → {"path": <created directory>}
#   rename → {"src": <before>, "dst": <after>}
#   move   → {"src": <before>, "dst": <after>}
#   delete → {"src": <original path>, "trash": <path in trash>}
FS_UNDO_FILE = os.path.join(DATA_DIR, "fs-undo.json")
FS_UNDO_MAX = 50            # undo stack depth
FS_LS_LIMIT = 5000          # listing cap: a giant directory must not hang the portal
FS_DL_MAX = 2 * 1024 * 1024 * 1024   # download cap (2 GB)
FS_TRASH_DIR = os.path.join(DATA_DIR, "trash")
# Where deleted items really go. In non-local mode DATA_DIR is a path on the
# HOST while mv runs INSIDE the container: if the trash is outside its home,
# the move cannot cross the namespace boundary -> use a fallback inside WORKDIR.
FS_TRASH = FS_TRASH_DIR if (
    LOCAL_MODE or FS_TRASH_DIR.startswith(WORKDIR.rstrip("/") + "/")
) else posixpath.join(WORKDIR, ".thumbmux-trash")

_fs_lock = threading.Lock()  # FS mutations + journal strictly one at a time
_fs_root_cache = None


class FsError(Exception):
    """File operation error carrying a ready HTTP code (400/403/404/409/500)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _fs_sh(script, *args, timeout=25):
    """bash script in the right namespace; paths are passed POSITIONALLY ($1, $2...),
    as in list_dirs: no injection possible, shell=True is not used."""
    p = subprocess.run(
        _wrap(["bash", "-c", script, "_", *args]),
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


_FS_PROBE_SH = r'''
p=$1
if [ -d "$p" ]; then printf 'dir\n'
elif [ -e "$p" ]; then printf 'file\n'
elif [ -L "$p" ]; then printf 'link\n'
else printf 'none\n'; fi
'''

_FS_REAL_SH = r'''
readlink -f -- "$1"
'''

_FS_LS_SH = r'''
d=$1
[ -e "$d" ] || [ -L "$d" ] || exit 44
[ -d "$d" ] || exit 40
cd -P -- "$d" 2>/dev/null || exit 43
pwd -P
if find . -maxdepth 0 -printf '' 2>/dev/null; then
  printf 'F\n'
  find . -mindepth 1 -maxdepth 1 -printf '%y\t%Y\t%s\t%T@\t%f\0' 2>/dev/null
else
  printf 'L\n'
  ls -1Ap 2>/dev/null
fi
'''


def _fs_realpath(path):
    """realpath in the right namespace (locally via os, otherwise readlink -f inside
    the container). In docker mode a missing intermediate directory = 404:
    a lexical fallback would be dangerous here, it could hide a symlink escaping outside."""
    if LOCAL_MODE:
        return os.path.realpath(path)
    rc, out, _ = _fs_sh(_FS_REAL_SH, path)
    real = out.strip()
    if rc != 0 or not real:
        raise FsError(404, "path not found")
    return real


def _fs_kind(path):
    """'dir' | 'file' | 'link' (broken symlink) | 'none'. A symlink to a directory
    counts as a directory: handy to enter, but we do not descend into it."""
    if LOCAL_MODE:
        if os.path.isdir(path):
            return "dir"
        if os.path.exists(path):
            return "file"
        return "link" if os.path.islink(path) else "none"
    rc, out, _ = _fs_sh(_FS_PROBE_SH, path)
    kind = out.strip()
    return kind if (rc == 0 and kind in ("dir", "file", "link")) else "none"


def _fs_exists(path):
    """Whether something exists at the path (a broken symlink counts too)."""
    return _fs_kind(path) != "none"


def _fs_root():
    """Real sandbox root (WORKDIR after realpath). Cached: in
    docker mode it would otherwise cost an extra exec per path check."""
    global _fs_root_cache
    if _fs_root_cache is None:
        try:
            _fs_root_cache = _fs_realpath(WORKDIR).rstrip("/") or WORKDIR
        except Exception:
            _fs_root_cache = WORKDIR.rstrip("/") or WORKDIR
    return _fs_root_cache


def _fs_under(p, root):
    root = (root or "").rstrip("/")
    return bool(root) and (p == root or p.startswith(root + "/"))


def _fs_guard(path, follow=False, roots=None):
    """The ONLY door for any incoming path. Normalizes and checks that
    the result lies inside the WORKDIR subtree; the check is AFTER realpath so that
    neither `..` nor a symlink can lead outside. Returns the canonical path.

    follow=False (default): resolve only the PARENT, keep the name as is;
    otherwise rename/delete of a symlink would touch its target, not the link itself.
    follow=True: resolve fully (for listing: entering a symlink-to-folder
    is allowed, but only if the target is also inside the sandbox).
    roots: extra allowed roots (needed for trash paths if
    THUMBMUX_DATA_DIR lives outside WORKDIR)."""
    p = (path or "").strip()
    if not p:
        raise FsError(400, "path not set")
    if not p.startswith("/"):
        raise FsError(400, "absolute path required")
    if "\x00" in p:
        raise FsError(400, "invalid path")
    p = posixpath.normpath(p)
    parent, base = posixpath.split(p)
    if follow or not base:
        real = _fs_realpath(p)
    else:
        real = posixpath.join(_fs_realpath(parent), base)
    real = real.rstrip("/") or "/"
    allowed = list(roots) if roots else [_fs_root()]
    if not any(_fs_under(real, r) for r in allowed):
        raise FsError(403, "path outside the working folder")
    return real


def _fs_state_dir():
    """Portal service-state directory (if inside the sandbox);
    the file manager never moves it."""
    try:
        d = _fs_realpath(DATA_DIR).rstrip("/")
    except Exception:
        return None
    return d if _fs_under(d, _fs_root()) else None


def _fs_protected(p, allow_root=False):
    """Paths the manager will not rename/move/delete.
    Otherwise one move could wipe out the trash together with the undo journal,
    and "reversible delete" would stop being reversible."""
    if not allow_root and p == _fs_root():
        return "cannot touch the root of the working folder"
    for guarded in (_fs_state_dir(), FS_TRASH.rstrip("/")):
        if guarded and _fs_under(p, guarded):
            return "this is a portal service folder (trash/state)"
    return None


def safe_fs_name(name):
    """File/folder name typed by the user. Cyrillic and spaces are NOT touched
    (like safe_filename() for uploads); otherwise a name with spaces and non-ASCII letters
    would turn into underscores. Only `/`, control characters (including \\0)
    and the names `.`/`..` are forbidden. Unlike safe_filename(): here the forbidden input
    is a 400 ERROR (the user sees and fixes it), there it is silent cleaning."""
    name = (name or "").strip()
    if not name or name in (".", ".."):
        raise FsError(400, "invalid name")
    if "/" in name:
        raise FsError(400, "the / character is not allowed in a name")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise FsError(400, "control characters are not allowed in a name")
    if len(name.encode("utf-8")) > 255:
        raise FsError(400, "name too long (>255 bytes)")
    return name


# ── FS primitives (work the same in both modes) ───────────────────────────

def _fs_mkdir(path):
    if LOCAL_MODE:
        try:
            os.mkdir(path)
        except FileExistsError:
            raise FsError(409, "already exists")
        except PermissionError:
            raise FsError(403, "no write permission")
        except OSError as e:
            raise FsError(500, str(e)[:200])
        return
    rc, _, err = _fs_sh('mkdir -- "$1"', path)
    if rc != 0:
        raise FsError(500, (err or "mkdir failed").strip()[:200])


def _fs_move(src, dst):
    """Move src -> dst. dst is ALWAYS the full destination path, which does not exist yet
    (checked by the caller under the lock): mv/shutil.move must never overwrite anything."""
    if LOCAL_MODE:
        try:
            shutil.move(src, dst)
        except PermissionError:
            raise FsError(403, "no permission to move")
        except OSError as e:
            raise FsError(500, str(e)[:200])
        return
    # -n = do not overwrite: a safety net against a race with something outside the portal
    rc, _, err = _fs_sh('mv -n -- "$1" "$2"', src, dst)
    if rc != 0:
        raise FsError(500, (err or "mv failed").strip()[:200])


def _fs_rmdir(path):
    """Remove an EMPTY directory (only for rolling back mkdir). Never -r."""
    if LOCAL_MODE:
        try:
            os.rmdir(path)
        except OSError as e:
            raise FsError(409, "could not remove the directory: " + str(e)[:120])
        return
    rc, _, err = _fs_sh('rmdir -- "$1"', path)
    if rc != 0:
        raise FsError(409, (err or "rmdir failed").strip()[:160])


def _fs_is_empty_dir(path):
    if LOCAL_MODE:
        try:
            with os.scandir(path) as it:
                return next(it, None) is None
        except OSError:
            return False
    rc, out, _ = _fs_sh('ls -A1 -- "$1"', path)
    return rc == 0 and not out.strip()


def _fs_makedirs(path):
    if LOCAL_MODE:
        os.makedirs(path, exist_ok=True)
        return
    rc, _, err = _fs_sh('mkdir -p -- "$1"', path)
    if rc != 0:
        raise FsError(500, (err or "mkdir -p failed").strip()[:200])


# ── listing ──────────────────────────────────────────────────────────────

def _fs_entry(name, kind, link, size, mtime):
    return {
        "name": name,
        "type": "dir" if kind == "dir" else "file",
        "size": int(size or 0),
        "mtime": int(mtime or 0),
        "hidden": name.startswith("."),
        "link": bool(link),
    }


def _fs_list_local(real):
    entries, truncated = [], False
    try:
        it = os.scandir(real)
    except FileNotFoundError:
        raise FsError(404, "path not found")
    except NotADirectoryError:
        raise FsError(400, "not a directory")
    except PermissionError:
        raise FsError(403, "no access to the folder")
    except OSError as e:
        raise FsError(500, str(e)[:200])
    with it:
        for e in it:
            if len(entries) >= FS_LS_LIMIT:
                truncated = True
                break
            link = False
            try:
                link = e.is_symlink()
            except OSError:
                pass
            # lstat: take size/date from the entry itself, do not follow symlinks
            try:
                st = e.stat(follow_symlinks=False)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            try:
                kind = "dir" if e.is_dir() else "file"
            except OSError:
                kind = "file"
            entries.append(_fs_entry(e.name, kind, link, size, mtime))
    return entries, truncated


def _fs_list_remote(real):
    rc, out, err = _fs_sh(_FS_LS_SH, real)
    if rc == 44:
        raise FsError(404, "path not found")
    if rc == 40:
        raise FsError(400, "not a directory")
    if rc == 43:
        raise FsError(403, "no access to the folder")
    if rc != 0:
        raise FsError(500, (err or "listing failed").strip()[:200])
    _norm, _, rest = out.partition("\n")   # first line is pwd -P
    mode, _, body = rest.partition("\n")
    entries, truncated = [], False
    if mode.strip() == "F":
        for rec in body.split("\0"):
            if not rec:
                continue
            if len(entries) >= FS_LS_LIMIT:
                truncated = True
                break
            parts = rec.split("\t", 4)
            if len(parts) < 5:
                continue
            y, target, size, mtime, name = parts
            link = y == "l"
            kind = "dir" if (y == "d" or (link and target == "d")) else "file"
            try:
                size = int(size)
            except ValueError:
                size = 0
            try:
                mtime = int(float(mtime))
            except ValueError:
                mtime = 0
            entries.append(_fs_entry(name, kind, link, size, mtime))
    else:
        # fallback without GNU find -printf: names and type only (ls -p), like list_dirs.
        # Size/date unavailable, return zeros so the UI does not break.
        for line in body.splitlines():
            if not line:
                continue
            if len(entries) >= FS_LS_LIMIT:
                truncated = True
                break
            isdir = line.endswith("/")
            entries.append(_fs_entry(line[:-1] if isdir else line,
                                     "dir" if isdir else "file", False, 0, 0))
    return entries, truncated


def fs_ls(path):
    """Directory listing inside the sandbox. Directories first, then by name,
    case-insensitive (like list_dirs). Hidden entries are returned but flagged."""
    real = _fs_guard(path or WORKDIR, follow=True)
    entries, truncated = (_fs_list_local(real) if LOCAL_MODE
                          else _fs_list_remote(real))
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
    root = _fs_root()
    parent = posixpath.dirname(real) or "/"
    return {
        "path": real,
        # nothing above the sandbox root to climb to: the frontend hides "up"
        "parent": None if (real == root or not _fs_under(parent, root)) else parent,
        "entries": entries,
        "truncated": truncated,
        "count": len(entries),
    }


# ── file download ────────────────────────────────────────────────────────

_FS_STAT_SH = r'''
p=$1
[ -f "$p" ] || exit 40
stat -c '%s' -- "$p" 2>/dev/null || stat -f '%z' -- "$p"
'''


def _fs_size(real):
    """Size of a regular file (in the right namespace). Needed BEFORE serving: without it
    there is nothing to put in Content-Length, and without that the browser shows no progress."""
    if LOCAL_MODE:
        try:
            return os.path.getsize(real)
        except FileNotFoundError:
            raise FsError(404, "file not found")
        except PermissionError:
            raise FsError(403, "no access to the file")
        except OSError as e:
            raise FsError(500, str(e)[:200])
    rc, out, err = _fs_sh(_FS_STAT_SH, real)
    if rc == 40:
        raise FsError(400, "not a regular file")
    if rc != 0:
        raise FsError(500, (err or "stat failed").strip()[:200])
    try:
        return int(out.strip())
    except ValueError:
        raise FsError(500, "could not determine file size")


def fs_download(path):
    """Validate the path and return (real path, size) for serving the file.
    Same door as the other operations, _fs_guard: neither `..` nor a symlink
    can lead outside the sandbox. follow=True: clicking a symlink is allowed, but
    only if the TARGET is also inside the sandbox."""
    real = _fs_guard(path or "", follow=True)
    kind = _fs_kind(real)
    if kind == "dir":
        raise FsError(400, "cannot download a folder, pick a file")
    if kind != "file":
        raise FsError(404, "file not found")
    # ONLY regular files. A named pipe (mkfifo) is a "file" for _fs_kind, but its
    # size is 0: the user would silently get an empty download, and reading a pipe
    # without a writer hangs the thread forever. In docker mode this is already
    # filtered by `[ -f ]` inside _FS_STAT_SH.
    if LOCAL_MODE and not os.path.isfile(real):
        raise FsError(400, "not a regular file (pipe or device)")
    # check permissions HERE, not at read time: otherwise the pre-flight fs/stat
    # would say "will serve" (stat needs no read permission), while the download itself
    # would fail with 403 and the error would land on disk as a file instead of readable text
    if LOCAL_MODE and not os.access(real, os.R_OK):
        raise FsError(403, "no access to the file")
    size = _fs_size(real)
    if size > FS_DL_MAX:
        raise FsError(413, "file is larger than 2 GB, fetch it via the terminal")
    return real, size


# Types the browser can EXECUTE on our own domain. Serve them as a
# byte stream: more reliable even with Content-Disposition preserved.
_FS_DL_NEUTRAL = {
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "application/xml", "text/xml", "application/javascript", "text/javascript",
}


def fs_download_type(name):
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return "application/octet-stream" if ctype in _FS_DL_NEUTRAL else ctype


def fs_download_disposition(name):
    """Content-Disposition that survives non-ASCII names. Headers allow
    only ASCII, so the name goes out twice: sanitized in filename= (for very old
    clients) and the real one in filename*= per RFC 5987."""
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', '_', name).strip() or "file"
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
        ascii_name.replace('"', "_"), quote(name, safe=""))


# ── undo journal ─────────────────────────────────────────────────────────

def load_fs_undo():
    for p in (FS_UNDO_FILE, FS_UNDO_FILE + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            ops = [x for x in data.get("ops", [])
                   if isinstance(x, dict) and x.get("op")]
            return {"ops": ops[-FS_UNDO_MAX:]}
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return {"ops": []}


def save_fs_undo(data):
    # atomic (temp + os.replace) + mirrored .bak, like favorites/folders
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass
    fd, tmp = tempfile.mkstemp(prefix="fsundo_", dir=os.path.dirname(FS_UNDO_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, FS_UNDO_FILE)
        try:
            with open(FS_UNDO_FILE + ".bak", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _fs_undo_push(entry):
    """Push an operation onto the top of the stack (call ONLY under _fs_lock)."""
    data = load_fs_undo()
    entry["id"] = uuid.uuid4().hex[:8]
    entry["ts"] = int(time.time())
    data["ops"] = (data.get("ops") or [])[-(FS_UNDO_MAX - 1):] + [entry]
    save_fs_undo(data)
    return entry


def _fs_undo_pop(entry_id):
    """Pop an entry off the stack after a SUCCESSFUL rollback (also under _fs_lock)."""
    data = load_fs_undo()
    data["ops"] = [x for x in (data.get("ops") or []) if x.get("id") != entry_id]
    save_fs_undo(data)


def _fs_label(op, src=None, dst=None):
    """Human-readable description for the "Undo" button."""
    sname = posixpath.basename((src or "").rstrip("/")) or src or ""
    dname = posixpath.basename((dst or "").rstrip("/")) or dst or ""
    if op == "mkdir":
        return "create folder '%s'" % sname
    if op == "rename":
        return "rename '%s' -> '%s'" % (sname, dname)
    if op == "move":
        return "move '%s' to '%s'" % (sname, posixpath.basename(
            posixpath.dirname((dst or "").rstrip("/"))) or "/")
    if op == "delete":
        return "delete '%s'" % sname
    return op


def fs_undoable():
    """What would be rolled back right now, for highlighting the button in the UI."""
    ops = load_fs_undo().get("ops") or []
    if not ops:
        return {"can": False, "label": ""}
    top = ops[-1]
    return {
        "can": True,
        "label": top.get("label") or _fs_label(top.get("op"),
                                               top.get("src") or top.get("path"),
                                               top.get("dst")),
        "op": top.get("op"),
        "ts": top.get("ts", 0),
        "depth": len(ops),
    }


# ── mutations ────────────────────────────────────────────────────────────

def _fs_free_trash_path(base):
    """<unixts>-<basename> in the trash; on collision, with a short suffix."""
    stamp = str(int(time.time()))
    cand = posixpath.join(FS_TRASH, "%s-%s" % (stamp, base))
    if not _fs_exists(cand):
        return cand
    return posixpath.join(FS_TRASH, "%s-%s-%s" % (stamp, uuid.uuid4().hex[:4], base))


def fs_op(op, args):
    """FS mutations + undo journal under a shared lock (modeled on folders_op).
    args is a dict of string request parameters (path/name/src/dst).
    Returns {"ok": True, ...} or raises FsError with an HTTP code."""
    with _fs_lock:
        if op == "mkdir":
            parent = _fs_guard(args.get("path") or WORKDIR)
            if _fs_kind(parent) != "dir":
                raise FsError(404, "folder not found")
            bad = _fs_protected(parent, allow_root=True)
            if bad:
                raise FsError(400, bad)
            name = safe_fs_name(args.get("name"))
            target = _fs_guard(posixpath.join(parent, name))
            if _fs_exists(target):
                raise FsError(409, "'%s' already exists" % name)
            _fs_mkdir(target)
            _fs_undo_push({"op": "mkdir", "path": target,
                           "label": _fs_label("mkdir", target)})
            return {"ok": True, "path": target, "undo": fs_undoable()}

        if op == "rename":
            src = _fs_guard(args.get("path"))
            if not _fs_exists(src):
                raise FsError(404, "object not found")
            bad = _fs_protected(src)
            if bad:
                raise FsError(400, bad)
            name = safe_fs_name(args.get("name"))
            dst = _fs_guard(posixpath.join(posixpath.dirname(src), name))
            if dst == src:
                raise FsError(400, "name unchanged")
            if _fs_exists(dst):
                raise FsError(409, "'%s' already exists" % name)
            _fs_move(src, dst)
            _fs_undo_push({"op": "rename", "src": src, "dst": dst,
                           "label": _fs_label("rename", src, dst)})
            return {"ok": True, "path": dst, "undo": fs_undoable()}

        if op == "move":
            src = _fs_guard(args.get("src"))
            dst_dir = _fs_guard(args.get("dst"), follow=True)
            if not _fs_exists(src):
                raise FsError(404, "object not found")
            if _fs_kind(dst_dir) != "dir":
                raise FsError(400, "destination is not a directory")
            bad = _fs_protected(src) or _fs_protected(dst_dir, allow_root=True)
            if bad:
                raise FsError(400, bad)
            # CLASSIC TREE SUICIDE: /a/b -> /a/b/c. mv in this case
            # either errors out or (on some filesystems) buries the directory inside itself.
            # Compare FULLY resolved paths so a symlink cannot fool the check.
            src_real = _fs_realpath(src).rstrip("/")
            if dst_dir == src_real or _fs_under(dst_dir, src_real):
                raise FsError(400, "cannot move a directory into itself")
            if posixpath.dirname(src) == dst_dir:
                raise FsError(400, "object is already in this folder")
            base = posixpath.basename(src)
            target = posixpath.join(dst_dir, base)
            if _fs_exists(target):
                raise FsError(409, "destination folder already contains '%s'" % base)
            _fs_move(src, target)
            _fs_undo_push({"op": "move", "src": src, "dst": target,
                           "label": _fs_label("move", src, target)})
            return {"ok": True, "path": target, "undo": fs_undoable()}

        if op == "delete":
            # NO rm: only a move to the trash + a journal entry, so
            # every delete is reversible and a slipped finger costs no data.
            src = _fs_guard(args.get("path"))
            if not _fs_exists(src):
                raise FsError(404, "object not found")
            bad = _fs_protected(src)
            if bad:
                raise FsError(400, bad)
            _fs_makedirs(FS_TRASH)
            base = posixpath.basename(src.rstrip("/")) or "item"
            trash = _fs_free_trash_path(base)
            _fs_move(src, trash)
            _fs_undo_push({"op": "delete", "src": src, "trash": trash,
                           "label": _fs_label("delete", src)})
            return {"ok": True, "trash": trash, "undo": fs_undoable()}

        raise FsError(400, "bad op")


def fs_undo():
    """Roll back the last operation. Before each step check that the world has not
    changed: object in place, target path free. If it changed, return 409 with an
    explanation and touch NOTHING (silently breaking data is worse than refusing).
    A successful rollback pops the entry off the stack."""
    with _fs_lock:
        ops = load_fs_undo().get("ops") or []
        if not ops:
            raise FsError(409, "nothing to undo")
        ent = ops[-1]
        op = ent.get("op")
        trash_roots = (_fs_root(), FS_TRASH.rstrip("/"))

        if op == "mkdir":
            path = _fs_guard(ent.get("path"))
            kind = _fs_kind(path)
            if kind == "none":
                raise FsError(409, "directory already removed, nothing to undo")
            if kind != "dir":
                raise FsError(409, "no longer a directory at this path")
            if not _fs_is_empty_dir(path):
                raise FsError(409, "directory is not empty, remove its contents manually")
            _fs_rmdir(path)

        elif op in ("rename", "move"):
            dst = _fs_guard(ent.get("dst"))
            src = _fs_guard(ent.get("src"))
            if not _fs_exists(dst):
                raise FsError(409, "object not found at the new path, already moved?")
            if _fs_exists(src):
                raise FsError(409, "original path is occupied, free up '%s'"
                              % posixpath.basename(src))
            if _fs_kind(posixpath.dirname(src)) != "dir":
                raise FsError(409, "original folder is gone, nowhere to restore to")
            _fs_move(dst, src)

        elif op == "delete":
            trash = _fs_guard(ent.get("trash"), roots=trash_roots)
            src = _fs_guard(ent.get("src"))
            if not _fs_exists(trash):
                raise FsError(409, "object vanished from the trash")
            if _fs_exists(src):
                raise FsError(409, "original path is occupied, free up '%s'"
                              % posixpath.basename(src))
            if _fs_kind(posixpath.dirname(src)) != "dir":
                raise FsError(409, "original folder is gone, nowhere to restore to")
            _fs_move(trash, src)

        else:
            raise FsError(409, "unknown operation in the journal")

        _fs_undo_pop(ent.get("id"))
        return {"ok": True, "undone": ent.get("label") or op,
                "op": op, "undo": fs_undoable()}



NAME_RE = re.compile(r"[^A-Za-z0-9 _.\-]")
# Control characters (including NUL) are the only thing replaced in an
# uploaded file name. Cyrillic, spaces and ordinary punctuation are legal,
# exactly as in safe_fs_name(): there must be one truth.
FILE_RE = re.compile(r"[\x00-\x1f\x7f]")
# Length limit is in UTF-8 BYTES (filesystems cap names at 255 bytes, a Cyrillic
# letter takes 2 bytes). 240 leaves room for the uuid prefix "xxxxxxxx_" (9 bytes)
# added by the caller in /m/api/upload.
FILE_MAX_BYTES = 240


def safe_filename(name):
    """Uploaded file name. Keep non-ASCII letters/spaces/punctuation, cut
    the dangerous: path separators (`/`, `\\`), control characters and NUL, leading
    dots (hidden files and `..`), length beyond FILE_MAX_BYTES bytes."""
    name = str(name or "")
    name = name.replace("\\", "/")        # windows paths too, before basename
    name = name.rsplit("/", 1)[-1]
    name = FILE_RE.sub("_", name)
    name = name.strip().lstrip(". ").rstrip()
    if not name:
        return "image.jpg"
    if len(name.encode("utf-8")) > FILE_MAX_BYTES:
        stem, ext = os.path.splitext(name)
        ext_b = ext.encode("utf-8")
        if len(ext_b) > 20:               # do not save an absurd "extension"
            ext_b = b""
        keep = max(1, FILE_MAX_BYTES - len(ext_b))
        stem = stem.encode("utf-8")[:keep].decode("utf-8", "ignore").rstrip(". ")
        name = (stem or "file") + ext_b.decode("utf-8")
    return name


def is_heic(b):
    """iPhone HEIC/HEIF sniff (ISO-BMFF ftyp box with a heic-family brand)."""
    head = b[:32]
    return b"ftyp" in head and any(
        brand in head for brand in (b"heic", b"heix", b"heif", b"hevc", b"mif1", b"msf1")
    )


def list_dirs(path):
    """List subfolders of `path` inside the container; return normalized path,
    parent, and sorted subdir names. Path is passed as data (no shell injection)."""
    script = 'cd "$1" 2>/dev/null && pwd -P && ls -1Ap 2>/dev/null'
    p = subprocess.run(
        _wrap(["bash", "-c", script, "_", path]),
        capture_output=True, text=True, timeout=15,
    )
    lines = p.stdout.splitlines()
    if not lines:
        raise RuntimeError("not a directory")
    norm = lines[0]
    dirs = sorted((e[:-1] for e in lines[1:] if e.endswith("/")), key=str.lower)
    parent = posixpath.dirname(norm.rstrip("/")) or "/"
    return {"path": norm, "parent": parent, "dirs": dirs}


def tmux(*args, check=False):
    """Run a tmux command inside the container; return (rc, stdout)."""
    cmd = _wrap(["tmux", *args])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.returncode, p.stdout


TMUX_CONF = config.TMUX_CONF


_ready = False


def ensure_session():
    """Cheap on the hot path: one `has-session` check. The authoritative
    options live in ~/.tmux.conf (container); it is re-sourced once per
    backend start, because the tmux server may predate the conf file
    (e.g. session created by ttyd's `tmux new -A` — that's how the live
    session ended up with `mouse off` despite the options below)."""
    global _ready
    cleanup_zombie_clients()  # periodic, throttled to once per 5 min
    rc, _ = tmux("has-session", "-t", SESSION)
    if rc == 0:
        if not _ready:
            tmux("source-file", "-q", TMUX_CONF)
        _ready = True
        return
    _ready = False
    tmux("new-session", "-d", "-s", SESSION, "-c", WORKDIR, "-x", "100", "-y", "40")
    tmux("rename-window", "-t", f"{SESSION}:0", "main")
    # fallback in case the conf file is missing; the source-file below wins
    for opt, val in (
        ("status", "off"),
        ("mouse", "on"),
        ("window-size", "latest"),
        ("default-terminal", "tmux-256color"),
    ):
        tmux("set-option", "-t", SESSION, "-g", opt, val)
    tmux("set-option", "-t", SESSION, "-g", "terminal-overrides", ",*:RGB")
    tmux("set-window-option", "-t", SESSION, "-g", "aggressive-resize", "on")
    tmux("set-environment", "-g", "COLORTERM", "truecolor")
    tmux("source-file", "-q", TMUX_CONF)
    _ready = True


def tmux_live(*args):
    """Hot-path tmux call: run directly (1 docker exec); if the session
    vanished, recreate it once and retry. Avoids ensure_session overhead."""
    rc, out = tmux(*args)
    if rc != 0:
        ensure_session()
        rc, out = tmux(*args)
    return rc, out


# text cap for /m/api/type (characters). Beyond it an EXPLICIT 413 error,
# no silent truncation. 200K goes into the tmux buffer via stdin in under 1 s.
TYPE_MAX_CHARS = 200_000


def tmux_stdin(data, *args):
    """tmux command with the payload on STDIN (docker exec -i). Needed for
    large texts: passing text as an argv argument (set-buffer -- <text>)
    dies at ~16K bytes: the tmux client sends the command to the server in one imsg and
    gets "command too long", and the backend silently swallowed it (text lost,
    the frontend saw "ok"). load-buffer - reads stdin and has no cap.
    Retry semantics as in tmux_live."""
    def run():
        p = subprocess.run(_wrap(["tmux", *args], interactive=True),
                           input=data, capture_output=True, text=True,
                           timeout=30)
        return p.returncode, p.stdout
    rc, out = run()
    if rc != 0:
        ensure_session()
        rc, out = run()
    return rc, out


# ── periodic zombie client cleanup ──────────────────────────────────────
# Without -D, tmux clients can accumulate when ttyd child processes don't
# exit cleanly (e.g., browser crash, network drop). This runs at most once
# every 5 minutes and kills stale clients if count exceeds ZOMBIE_THRESHOLD.
_last_cleanup = 0
ZOMBIE_THRESHOLD = 4   # more clients than this -> detach the OLDEST extras. Live viewers
                       # usually 1 (phone); duplicate frames = a war of zombie attach processes
                       # piling up from iframe reconnects (the docker boundary swallows
                       # SIGHUP). Keep only the FRESHEST.
ZOMBIE_INTERVAL = 30   # seconds between cleanup runs
CLEAN_LOOP_INTERVAL = 90  # background timer cadence (independent of HTTP traffic)


def cleanup_zombie_clients():
    """Kill excess tmux clients to prevent accumulation. Called from
    ensure_session (hot path) but throttled to once per ZOMBIE_INTERVAL."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < ZOMBIE_INTERVAL:
        return
    _last_cleanup = now
    rc, out = tmux("list-clients", "-t", SESSION, "-F",
                   "#{client_created}\t#{client_tty}")
    if rc != 0:
        return
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) <= ZOMBIE_THRESHOLD:
        return
    # client_created is sorted ascending (OLD first). The live client
    # is the FRESHEST (last page reconnect); zombies from old
    # reconnects are old. Keep the last ZOMBIE_THRESHOLD (newest),
    # kill the OLD extra clients. (It used to be the other way round - it cut the fresh client.)
    lines.sort()
    excess = lines[:-ZOMBIE_THRESHOLD]
    for _, tty in (l.split("\t") for l in excess):
        tty = tty.strip()
        if tty:
            # WARNING: tmux has NO kill-client command (it was used here and failed
            # silently with "unknown command", so cleanup NEVER worked and
            # zombie clients piled up). The right command is detach-client.
            tmux("detach-client", "-t", tty)


def _cleanup_loop():
    """Background daemon: detach surplus tmux clients on a fixed cadence.
    Reconnect churn (phone sleep/wake, network drops) can leave stale attach
    clients behind. Only excess CLIENTS are detached; panes and the session
    are never touched, so anything running inside keeps running."""
    while True:
        time.sleep(CLEAN_LOOP_INTERVAL)
        try:
            cleanup_zombie_clients()
        except Exception:
            pass


# special keys allowed via /m/api/key — friendly name -> tmux key
KEYMAP = {
    "Enter": "Enter", "Escape": "Escape", "Tab": "Tab", "BSpace": "BSpace",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "Home": "Home", "End": "End", "PageUp": "PageUp", "PageDown": "PageDown",
    "C-c": "C-c", "C-d": "C-d", "C-l": "C-l", "C-z": "C-z", "C-r": "C-r",
    "C-a": "C-a", "C-e": "C-e", "C-u": "C-u", "C-w": "C-w", "Space": "Space",
    "C-o": "C-o",
}


def exit_copy_mode():
    """Typed input must reach the app, not tmux copy-mode (swipe-scroll can
    leave the pane in copy-mode). One exec: conditional cancel."""
    tmux("if-shell", "-F", "-t", SESSION, "#{pane_in_mode}",
         f"send-keys -t {SESSION} -X cancel")


# -- window names ------------------------------------------------------------
# Names may contain letters of any script, digits, space and _ . -
WINNAME_RE = re.compile(r"[^\w \-.]", re.UNICODE)


def _mark_manual(idx):
    """Remember in tmux itself (window option @manual) that a human named it."""
    tmux("set-option", "-w", "-t", f"{SESSION}:{idx}", "@manual", "1")


def read_mem():
    """Host RAM snapshot from /proc/meminfo (in MB)."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.strip().split()[0]) // 1024  # kB -> MB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {
        "total_mb": total,
        "avail_mb": avail,
        "used_pct": round((total - avail) * 100 / total) if total else 0,
    }


# -- window activity indicator: work / done / idle ----------------------
# One light docker exec = window list + a short tail (tail -4) of each.
# The "esc/ctrl+c to interrupt" marker lives in the 1-2 bottom lines only while
# Claude is WORKING; a tail diff catches output streaming. The spinner is NOT a
# signal (it shows at idle too: "✻ Crunched for 5m"). Registry keyed by stable window_id.
_STATE_TTL = 2.8
_state_lock = threading.Lock()
_state_cache = {"t": 0.0, "data": None}  # data = list[{index,name,active,id,st}]
# Cache generation: grows on every _state_invalidate(). A tick started BEFORE
# the reset (second client: phone + PC) must not put its stale snapshot into
# the cache after the reset - otherwise a closed/moved window would live on for _STATE_TTL.
_state_gen = 0
_win_state = {}   # win_id -> {"hash":str,"changed":float,"was_work":bool,"done":bool,"ask":bool}
DONE_QUIET = 1.5  # seconds of quiet after work -> "done"
STATE_TAIL = 4    # tail lines per window (markers at the bottom; less data than preview)

# -- "ask" state detection: the agent waits for an answer/confirmation ---
# Red status: Claude/Codex stopped at a "continue?" question or a menu choice
# (Yes/No, "❯ 1.", "(y/n)"). The marker constants list - extend it
# here. Matched against the bottom lines of the active pane (STATE_TAIL). Priority
# ask > work: even if a spinner still spins and "to interrupt" is shown below,
# a pending question matters more - show red.
# Substrings are lower-case (compared with tail.lower()) so that
# both "Do you want..." and "do you want..." match.
ASK_MARKERS = (
    "do you want to proceed",
    "do you want to continue",
    "do you want to make this edit",
    "do you want to create",
    "would you like to proceed",
    "yes, and don't ask again",
    "yes, allow always",
    "no, and tell claude",
    "waiting for your input",
    "press enter to continue",
    "1. yes",
    "2. no",
    "❯ 1.",
    "❯ 2.",
    "(y/n)",
    "[y/n]",
    "y/n)?",
    "allow this tool call",
    "approve this",
    "apply this change",
)
# Regexes for Claude/Codex choice prompts that a constant cannot catch:
#   "❯ 1. Yes", "> 2. No", a numbered choice menu in a frame.
_ASK_RE = re.compile(r"(?m)^\s*[❯>▶]\s*\d+\.\s+\S")


def _looks_ask(tail):
    """True if the tail of the active pane contains a confirmation/
    choice prompt (the agent waits for an answer). tail is the raw multi-line pane text."""
    low = tail.lower()
    for m in ASK_MARKERS:
        if m in low:
            return True
    return bool(_ASK_RE.search(tail))

# Marker format: '@@S <index> <active> <window_id> <name>@@' + up to 4 tail lines.
# The name is LAST (may contain spaces) -> parse the remainder as name.
_STATE_SCRIPT = (
    "tmux list-windows -t " + SESSION + " -F "
    "'#{window_index}\t#{window_active}\t#{window_id}\t#{window_name}' 2>/dev/null | "
    "while IFS=$'\\t' read -r i a id n; do "
    "printf '@@S %s %s %s %s@@\\n' \"$i\" \"$a\" \"$id\" \"$n\"; "
    "tmux capture-pane -t " + SESSION + ":\"$i\" -p 2>/dev/null "
    "| tail -n " + str(STATE_TAIL) + " || true; "
    "done"
)


def _fetch_state_raw():
    p = subprocess.run(
        _wrap(["bash", "-c", _STATE_SCRIPT]),
        capture_output=True, text=True, errors="replace", timeout=15)
    return p.stdout


def _state_tick():
    """One exec -> parse windows+tails -> update the state machine under
    _state_lock, return a list of {index,name,active,id,st}. Tails are NOT
    returned (detection only). A missing session/empty output = idle, no crash."""
    out = _fetch_state_raw()
    if not out.strip():  # the session may be missing - create it and retry once
        ensure_session()
        out = _fetch_state_raw()
    wins, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@S ") and line.endswith("@@"):
            parts = line[4:-2].split(" ", 3)
            if len(parts) == 4 and parts[0].isdigit():
                cur = {"index": int(parts[0]), "active": parts[1] == "1",
                       "id": parts[2], "name": parts[3], "_tail": []}
                wins.append(cur)
                continue
        if cur is not None:
            cur["_tail"].append(line)

    now = time.time()
    with _state_lock:
        seen_ids = set()
        for w in wins:
            wid = w["id"]
            seen_ids.add(wid)
            tail = "\n".join(w["_tail"])
            h = str(hash(tail))
            marker = "to interrupt" in tail
            ask = _looks_ask(tail)
            first_seen = wid not in _win_state
            changed = (not first_seen) and h != _win_state[wid]["hash"]
            # first display of a window is the baseline, NOT work (otherwise after start/unhide
            # all windows would falsely "work" for one tick). ask is allowed on the first
            # display too - a question is static, a tail diff cannot catch it.
            work = (marker or changed) and not first_seen
            rec = _win_state.setdefault(
                wid, {"hash": h, "changed": now, "was_work": False,
                      "done": False, "ask": False, "act_ts": 0.0})
            rec["ask"] = ask
            if ask:
                # priority ask > work: a pending question matters more than the spinner.
                # clear work/done so that after the answer the window goes
                # through the normal work->done branch.
                rec["done"] = False
                rec["was_work"] = False
                rec["act_ts"] = now
                st = "ask"
            elif work:
                st = "work"
                rec["done"] = False
                rec["was_work"] = True
                rec["changed"] = now
                rec["act_ts"] = now
            else:
                if rec["was_work"] and now - rec["changed"] >= DONE_QUIET:
                    rec["done"] = True
                    rec["was_work"] = False
                st = "done" if rec["done"] else "idle"
            # window opened -> green clears IMMEDIATELY (ask is NOT cleared: the question waits
            # for an answer even in the active window)
            if w["active"] and rec["done"] and not ask:
                rec["done"] = False
                st = "idle"
            rec["hash"] = h
            w["st"] = st
            del w["_tail"]
        # clean up leaked ids (windows closed)
        for dead in [k for k in _win_state if k not in seen_ids]:
            del _win_state[dead]
    return wins


def list_windows():
    """Window list + indicator state. One exec, cached for _STATE_TTL under
    the lock (a crowd of clients does not spawn execs). Returns
    list[{index,name,active,id,st}] - extra fields do not break old tests."""
    global _state_cache
    with _state_lock:
        c = _state_cache
        if c["data"] is not None and time.time() - c["t"] < _STATE_TTL:
            return c["data"]
    for _ in range(2):
        with _state_lock:
            gen = _state_gen
        data = _state_tick()  # exec outside _state_lock (_state_tick takes its own lock)
        with _state_lock:
            if gen == _state_gen:
                _state_cache = {"t": time.time(), "data": data}
                return data
        # while the exec ran, the cache was reset (kill/move-window from another request) -
        # the snapshot is already stale: do not cache it, take it again
    return data  # two resets in a row - return as is, do not cache


def _state_invalidate():
    """Reset the window list and preview caches. Required after operations that change
    the set/order of windows (reorder, join-pane, break-pane): otherwise the reply of
    `list_windows()` in the same handler returns a snapshot up to _STATE_TTL old -
    the frontend drew the OLD order and the tab "jumped back" until the next
    poll (~5 s). A real bug, not a guess: reorder called
    list_windows() before the reorder (fresh cache) and after (same cache)."""
    global _state_cache, _state_gen, _prev_gen
    with _state_lock:
        _state_cache = {"t": 0.0, "data": None}
        _state_gen += 1
        # previews: do NOT take _prev_lock - it is held for the duration of the preview
        # docker exec (up to 15 s), and a POST kill/reorder would wait for it. Just bump
        # the generation; previews_cached will see its cache is stale.
        _prev_gen += 1


# -- previews of all windows in one docker exec (mobile tile mode) -------
PREVIEW_TTL = 1.5     # sec: cache shared by a crowd of clients (ThreadingHTTPServer)
PREVIEW_LINES = 16    # tail for tile previews (tiles)
PREVIEW_SCROLLBACK = 60  # how many history lines to capture (needed for the "last message" in the chat list)
PREVIEW_COLS = 100    # truncate each line

# Claude/Codex interface chrome that is NOT shown as the last message in the chat list
_CHROME_MARKERS = (
    "shift+tab", "accept edits", "for agents", "esc to interrupt", "to interrupt",
    "? for shortcuts", "for shortcuts", "for newline", "auto-accept", "auto accept",
    "bypass permissions", "bypassing permissions", "plan mode on", "plan mode off",
    "context left", "auto-compact", "approaching", "tokens left", "ctrl+c",
    "⏵", "▶▶", "esc to ", "? for ", "/help for",
    "new task", "/clear to save", "to save ", "esc to clear", "/clear",
    "✻", "0: dismiss", "1: bad", "esc to undo", "shortcuts",
    "how is claude doing", "how is claude", "(optional)",
)

def _clean_chat_preview(lines):
    """Keep only meaningful output lines: drop frames (box-drawing),
    empty lines, the bare input prompt and Claude/Codex interface hints."""
    out = []
    for ln in lines:
        s = "".join(c for c in ln if not ("─" <= c <= "▟")).strip()
        s = s.lstrip("│|>❯›▶◀  ").rstrip("│| ").strip()
        if not s or s in (">", ">>", "›"):
            continue
        low = s.lower()
        if any(m in low for m in _CHROME_MARKERS):
            continue
        if not any(c.isalnum() for c in s):
            continue
        out.append(s)
    return out
_prev_lock = threading.Lock()
_prev_cache = {"t": 0.0, "data": None, "gen": 0}
_prev_gen = 0   # preview cache generation (under _state_lock, see _state_invalidate)

# One bash script = one docker exec: window list + capture-pane of each,
# sections separated by markers '@@WIN <idx> <active> <name>@@'. The name is the last
# field (may contain spaces). A window may vanish between list and capture -
# the error is swallowed (2>/dev/null + || true) and the preview is simply skipped.
# Truncation to PREVIEW_COLS is done in Python (cut -c in the container cuts by
# bytes and splits a multibyte character in half); lines are bounded by the pane width anyway
_PREVIEW_SCRIPT = (
    "tmux list-windows -t " + SESSION + " -F "
    "'#{window_index}\t#{window_active}\t#{window_id}\t#{pane_current_command}\t#{window_name}' 2>/dev/null | "
    "while IFS=$'\\t' read -r i a id c n; do "
    "printf '@@WIN %s %s %s %s %s@@\\n' \"$i\" \"$a\" \"$id\" \"$c\" \"$n\"; "
    "tmux capture-pane -t " + SESSION + ":\"$i\" -p -S -" + str(PREVIEW_SCROLLBACK) + " 2>/dev/null "
    "| tail -n " + str(PREVIEW_SCROLLBACK) + " || true; "
    "done"
)


def _fetch_previews_raw():
    p = subprocess.run(
        _wrap(["bash", "-c", _PREVIEW_SCRIPT]),
        capture_output=True, text=True, errors="replace", timeout=15)
    return p.stdout


def window_previews():
    out = _fetch_previews_raw()
    if not out.strip():  # the session may be missing - create it and retry once
        ensure_session()
        out = _fetch_previews_raw()
    wins, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@WIN ") and line.endswith("@@"):
            parts = line[6:-2].split(" ", 4)
            if len(parts) == 5 and parts[0].isdigit():
                # parts[3] = pane_current_command of the window's active pane (claude/
                # bash/...) - for the frontend's virtual "Running" folder
                cur = {"index": int(parts[0]), "id": parts[2], "name": parts[4],
                       "cmd": parts[3], "active": parts[1] == "1", "lines": []}
                wins.append(cur)
                continue
        if cur is not None:
            cur["lines"].append(line[:PREVIEW_COLS])
    for w in wins:  # do not send an empty preview tail over the network
        while w["lines"] and not w["lines"][-1].strip():
            w["lines"].pop()
    # mix in the indicator state by id from _win_state - WITHOUT a new exec
    # (for the tile header). No entry -> idle.
    now = time.time()
    with _state_lock:
        for w in wins:
            rec = _win_state.get(w["id"])
            if rec and rec.get("ask"):
                w["st"] = "ask"
            elif rec and rec.get("done") and not w["active"]:
                w["st"] = "done"
            elif rec and rec.get("was_work"):
                w["st"] = "work"
            else:
                w["st"] = "idle"
            # act_ago: seconds since the agent's last activity (work/ask) - for the
            # "Running" folder: the window stays there after the status goes out
            # (for the active window done clears instantly). Reset by an
            # app.py restart (in-memory registry) - streaming windows return
            # to the folder within 1-2 ticks, ones that finished quietly before the restart do not.
            act = rec.get("act_ts") if rec else None
            w["act_ago"] = int(now - act) if act else None
    return {"windows": wins}


def previews_cached():
    """Cache for PREVIEW_TTL under the lock: several open clients (the server
    is multithreaded) do not spawn parallel docker execs - the second one waits for the lock and
    gets the first one's fresh cache."""
    global _prev_cache
    with _prev_lock:
        with _state_lock:
            gen = _prev_gen
        if _prev_cache["data"] is not None and _prev_cache.get("gen") == gen and \
                time.time() - _prev_cache["t"] < PREVIEW_TTL:
            return _prev_cache["data"]
        data = window_previews()
        with _state_lock:
            fresh = gen == _prev_gen
        # the exec ran across a reset (kill/reorder in a neighbouring request) - the snapshot
        # is stale: return it but do not cache it
        if fresh:
            _prev_cache = {"t": time.time(), "data": data, "gen": gen}
        return data


# -- Claude Code chat list: transcripts under the projects dir ------------
# Layout: <PROJECTS>/<slug>/<sessionId>.jsonl, where slug is the session cwd with
# / . _ replaced by "-". We do NOT decode the slug back (ambiguous: "-" can be
# "/", "." or "_"); the real cwd is taken from the first transcript line
# that has a cwd field.
# Subdirectories inside a project (subagents/, tool-results/) are service ones, NOT
# sessions, and are not scanned (only first-level *.jsonl).
CHAT_PROJECTS_DIR = os.path.realpath(config.CLAUDE_PROJECTS_DIR)
CHATS_CACHE_FILE = os.path.join(DATA_DIR, "chats-cache.json")
CHAT_NAMES_FILE = os.path.join(DATA_DIR, "chat-names.json")

CHATS_TTL = 4.0            # sec: cache of the ready index (like _STATE_TTL for windows)
CHAT_HEAD_BYTES = 2 * 1024 * 1024  # max bytes of the file "head" to read
CHAT_HEAD_LINES = 400              # ...and no more than this many lines
CHAT_CHUNK = 64 * 1024             # read chunk size
CHAT_LINE_CAP = 1024 * 1024    # a longer line (fat tool_result) is dropped,
                               # without accumulating in memory. Cannot be smaller: live
                               # messages with pasted data reach 400 KB
                               # (verified on real transcripts)
CHAT_RESCUE_BYTES = 32 * 1024 * 1024  # ceiling of the "rescue" pass (see _chat_rescue)
# file tail where Claude Code APPENDS service records with titles
# (custom-title / ai-title / summary). Measured on all real transcripts
# (~145 files, up to 70 MB): the last such record is always within 26 KB of the end
# for custom-title and is covered by head+tail for ai-title. 512 KB leaves a margin.
CHAT_TAIL_BYTES = 512 * 1024
# cache record version: bump when the format changes so an old cache without new
# fields (tsrc, titles from the tail, ts/started) is rescanned instead of served
# as is
CHAT_CACHE_VER = 3
CHAT_TITLE_MAX = 80            # title length
CHAT_NAME_MAX = 60             # custom name length
CHAT_AI_CTX = 3500             # context for auto-naming (characters)
CHAT_LIMIT_DEFAULT = 200
CHAT_LIMIT_MAX = 1000
# line count estimate: files up to this size are counted exactly, larger ones by samples
CHAT_COUNT_EXACT = 512 * 1024
CHAT_SAMPLES = 6

CHAT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# control characters in a custom name (including from the query string);
#  /  - "invisible" line breaks, they break JS on the frontend
CHAT_CTL_RE = re.compile(r"[\x00-\x1f\x7f  ]")
# Claude Code service wrappers inside message text
CHAT_TAG_RE = re.compile(
    r"<(system-reminder|local-command-caveat|command-name|command-message|"
    r"command-args|command-contents|user-prompt-submit-hook|ide_[a-z_]+)"
    r"\b.*?</\1>", re.S | re.I)
# attachments that Claude Code inserts into message text
CHAT_ATTACH_RE = re.compile(
    r"\[(Image|Images?|Pasted text|Screenshot|Attachment|File)[^\]\n]{0,200}\]",
    re.I)
# path to an uploaded file that the portal prepends to the text
# (~/.uploads/20fe8d5a_IMG_0715.png Hello ...)
CHAT_UPLOAD_RE = re.compile(r"^(?:\S*/\.uploads/\S+\s+)+")
# /clear, /resume, /model ... (but NOT a path like ~/.uploads/...: it has another "/" after)
CHAT_SLASH_RE = re.compile(r"^/[A-Za-z][\w:-]*(\s|$)")

# canned phrases: technically "user messages", but not written
# by a human, and meaningless as a chat title
CHAT_JUNK_PREFIX = (
    "caveat: the messages below",
    "continue from where you left off",
    "this session is being continued",
    "please continue the conversation",
    "[request interrupted",
    "api error",
    "your task is to create a detailed summary",
    "analyze this codebase and generate",
)

# model refusal instead of a name ("Unfortunately, I cannot...") - do not save

_chats_lock = threading.Lock()   # index + cache file
_chat_names_lock = threading.Lock()
# rows - the ready sorted list; cache - path -> record map (the same one
# that goes into chats-cache.json)
_chats_state = {"t": 0.0, "rows": None, "cache": None}


def _chat_head_lines(path, stats=None):
    """Generator of lines from the HEAD of a .jsonl. The file is NOT read whole into memory:
    read in CHAT_CHUNK chunks, split on \\n ourselves; a line longer than
    CHAT_LINE_CAP (a fat tool_result of tens of megabytes) is dropped on the fly,
    without accumulating. Stops at CHAT_HEAD_BYTES/CHAT_HEAD_LINES; the caller
    usually stops earlier (found the first message - done).
    stats (dict) is filled: bytes/lines/dropped."""
    read = lines = dropped = 0
    buf = ""
    skipping = False   # inside a too-long line: waiting for its end
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            while read < CHAT_HEAD_BYTES and lines < CHAT_HEAD_LINES:
                chunk = f.read(CHAT_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                buf += chunk
                while True:
                    i = buf.find("\n")
                    if i < 0:
                        if len(buf) > CHAT_LINE_CAP:
                            buf = ""
                            if not skipping:
                                dropped += 1
                            skipping = True
                        break
                    line, buf = buf[:i], buf[i + 1:]
                    lines += 1
                    if skipping:
                        skipping = False   # tail of a dropped line
                    else:
                        yield line
                    if lines >= CHAT_HEAD_LINES:
                        break
            if buf and not skipping and len(buf) <= CHAT_LINE_CAP \
                    and lines < CHAT_HEAD_LINES:
                lines += 1
                yield buf
    except OSError:
        pass
    finally:
        if stats is not None:
            stats["bytes"] = read
            stats["lines"] = lines
            # how many lines were dropped as too long (see _chat_rescue)
            stats["dropped"] = dropped + (1 if skipping else 0)


# "rescue" pass over a file with giant lines: look for the first
# text block and cwd directly in the bytes
_CHAT_RESCUE_TEXT = re.compile(rb'"type":"text","text":"((?:[^"\\]|\\.){2,600})')
_CHAT_RESCUE_CWD = re.compile(rb'"cwd":"(/[^"]{0,200})"')


def _chat_unescape(raw):
    """JSON string (raw bytes from the transcript) -> text."""
    s = raw.decode("utf-8", "replace").rstrip("\\")
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\")


def _chat_rescue(path):
    """Rare case: the FIRST record of a session is a giant line (an embedded
    PDF/photo in base64: a 15 MB user message occurred, with the text itself at
    the very end of the line). Line-by-line parsing drops such a line and the chat
    would stay without a title. Here we walk byte chunks (memory holds only
    the chunk + overlap) and use a regex to find the first text block and cwd.
    Expensive in I/O, so called ONLY when the head gave nothing, and
    the result lives in the cache until the file changes next."""
    text = cwd = ""
    tail = b""
    read = 0
    try:
        with open(path, "rb") as f:
            while read < CHAT_RESCUE_BYTES and not (text and cwd):
                chunk = f.read(CHAT_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                window = tail + chunk
                if not text:
                    m = _CHAT_RESCUE_TEXT.search(window)
                    # a match that hit the window edge may have truncated the text -
                    # wait for the next chunk (the overlap returns us to the marker)
                    if m and (len(m.group(1)) >= 600
                              or window[m.end():m.end() + 1] == b'"'):
                        text = _chat_unescape(m.group(1))
                if not cwd:
                    m = _CHAT_RESCUE_CWD.search(window)
                    if m:
                        cwd = m.group(1).decode("utf-8", "replace")
                tail = window[-4096:]
    except OSError:
        pass
    return text, cwd


def _chat_count_lines(path, size):
    """Approximate number of records in a transcript. Only small files are counted
    exactly; large ones by CHAT_SAMPLES pieces of CHAT_CHUNK, evenly across
    the file (average line length jumps from 200 B to 100 KB, so a single
    "head" is not enough, and reading 70 MB whole is not an option)."""
    if size <= 0:
        return 0
    try:
        with open(path, "rb") as f:
            if size <= CHAT_COUNT_EXACT:
                n = 0
                while True:
                    chunk = f.read(CHAT_CHUNK)
                    if not chunk:
                        break
                    n += chunk.count(b"\n")
                return n
            got = nl = 0
            step = size // CHAT_SAMPLES
            for k in range(CHAT_SAMPLES):
                f.seek(min(k * step, max(0, size - CHAT_CHUNK)))
                chunk = f.read(CHAT_CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                nl += chunk.count(b"\n")
            if not nl or not got:
                return 1
            return max(1, int(size / (got / nl)))
    except OSError:
        return 0


def _chat_user_text(rec):
    """Text of a HUMAN message from a transcript record, or None.
    Excluded: non-user, service (isMeta), subagent branches (isSidechain),
    tool results (content=[{type:tool_result}] / toolUseResult)."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    if rec.get("toolUseResult") is not None:
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("role") not in (None, "user"):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                return None      # this is a tool reply, not a human
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return None


def _chat_clean_text(text):
    """Message text -> a one-line title candidate, or "" if it is
    service junk. Same idea as _clean_chat_preview (line 1224), but
    a different source: that one handles a tmux SCREEN snapshot (frames, Claude hints from
    _CHROME_MARKERS), this one a transcript with its own junk: xml wrappers
    of commands, caveat preambles, attachment placeholders, slash commands."""
    s = text or ""
    s = CHAT_TAG_RE.sub(" ", s)        # <system-reminder>...</system-reminder> etc.
    s = CHAT_ATTACH_RE.sub(" ", s)     # [Image: original 4032x3024 …]
    s = CHAT_UPLOAD_RE.sub("", s.lstrip())   # path to an attached file
    s = s.strip()
    if not s or s.startswith("<"):     # unclosed/unknown service wrapper
        return ""
    if CHAT_SLASH_RE.match(s):         # /clear, /resume, /model …
        return ""
    low = s.lower()
    if any(low.startswith(p) for p in CHAT_JUNK_PREFIX):
        return ""
    s = " ".join(s.split())            # collapse line breaks and spaces
    if sum(1 for c in s if c.isalnum()) < 2:
        return ""                      # "===", ">>>", a smiley and the like
    return s


def _chat_shorten(s, limit=CHAT_TITLE_MAX):
    """Truncate at a word boundary with an ellipsis."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp >= limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-") + "…"


# sane range for a transcript timestamp: guard against junk (wrong clock,
# a quoted piece of someone else's JSON) that would otherwise drag a chat in sorting
# into the far past/future
_CHAT_TS_MIN = 1262304000            # 2010-01-01
_CHAT_TS_SLACK = 7 * 86400           # the future no further than a week from "now"


def _chat_parse_ts(val):
    """ISO-8601 from a timestamp field -> unix seconds (int) or None.
    Tolerates Z/milliseconds/offset; without tz we assume UTC (as Claude Code writes it).
    Anything unclear -> None: the caller falls back to mtime, the scan does not crash."""
    if not isinstance(val, str) or len(val) < 10:
        return None
    s = val.strip()
    if s.endswith(("Z", "z")):       # before 3.11 fromisoformat does not understand Z
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ts = int(dt.timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    if not (_CHAT_TS_MIN <= ts <= time.time() + _CHAT_TS_SLACK):
        return None
    return ts


# raw timestamp - fallback when the tail has no WHOLE line at all
# (the last record is a giant longer than CHAT_TAIL_BYTES)
_CHAT_TS_RAW_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]{10,40})"')

# service transcript records with titles: (type, value field, slot)
_CHAT_TAIL_MARKS = (
    ("custom-title", "customTitle", "own"),   # the owner named it in Claude Code
    ("ai-title", "aiTitle", "ai"),            # auto title from Claude Code
    ("summary", "summary", "sum"),            # session summary
)


# Which model the chat ran on: message.model of assistant replies. Read
# only the file TAIL (transcripts reach 70 MB) - the last model is enough.
CHAT_MODEL_TAIL_BYTES = 256 * 1024
CHAT_TITLE_MIN = 6   # shorter first messages ("ok", "go") make poor titles


def _chat_model_in_lines(lines):
    """Last assistant model in a list of raw jsonl lines (bytes), from the end.
    `<synthetic>` (service replies of Claude Code itself) is skipped. The rule
    "last model" is deliberate: a chat already continued on Opus
    opens on Opus next time - to return it to GLM use the "Open in..." menu."""
    for line in reversed(lines):
        if b'"model"' not in line or b'"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        m = msg.get("model") if isinstance(msg, dict) else None
        if isinstance(m, str) and m and m != "<synthetic>":
            return m[:64]
    return ""


def chat_last_model(path, size=None):
    """Model of the last assistant reply from the transcript tail ("" - not found)."""
    try:
        if size is None:
            size = os.path.getsize(path)
        off = max(0, size - CHAT_MODEL_TAIL_BYTES)
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(CHAT_MODEL_TAIL_BYTES + CHAT_CHUNK)
    except OSError:
        return ""
    lines = data.split(b"\n")
    if off and len(lines) > 1:
        lines = lines[1:]      # the first tail line is truncated
    return _chat_model_in_lines(lines)


def _chat_tail_titles(path, size):
    """Titles from service transcript records -> {"own":..., "ai":..., "sum":...}
    plus "ts" - unix time of the LAST record with a timestamp (= last
    chat activity; file mtime lies after a bulk copy).
    These records are APPENDED to the end of the file (renamed - another one appeared),
    so we do not read the whole file: only the CHAT_TAIL_BYTES tail, one pass
    for everything. The last record of each type wins. A marker accidentally
    quoted inside someone else's line (a human pasted a piece of a transcript)
    is filtered out by JSON parsing and a type check; for timestamp - by checking that it is a
    top-level field (in file-history-snapshot it is nested, not activity)."""
    out = {}
    off = max(0, size - CHAT_TAIL_BYTES)
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(CHAT_TAIL_BYTES + CHAT_CHUNK)
    except OSError:
        return out
    lines = data.split(b"\n")
    if off and len(lines) > 1:
        lines = lines[1:]      # the first tail line is almost certainly truncated
    marks = [(b'"type":"' + typ.encode() + b'"', typ, key, slot)
             for typ, key, slot in _CHAT_TAIL_MARKS]
    ts_lines = []              # candidate lines for the last timestamp
    for line in lines:
        if b'"timestamp":"' in line:
            ts_lines.append(line)
        hits = [m for m in marks if m[0] in line]
        if not hits:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        for _mark, typ, key, slot in hits:
            if rec.get("type") != typ:
                continue
            val = rec.get(key)
            if isinstance(val, str) and val.strip():
                out[slot] = " ".join(val.split())[:CHAT_TITLE_MAX * 4]
    # model of the last reply - for the GLM badge in the list (same tail, no
    # second file read)
    out["model"] = _chat_model_in_lines(lines)
    # walk from the end: the first line that parses into a record with a valid
    # TOP-level timestamp is the last activity. Attempt ceiling -
    # so a pathological tail of snapshots only does not make us parse everything
    for line in reversed(ts_lines[-50:]):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            ts = _chat_parse_ts(rec.get("timestamp"))
            if ts:
                out["ts"] = ts
                break
    if "ts" not in out:
        # no whole line with a timestamp: the file ends with a giant record
        # longer than the tail (fat tool_result). Take the last timestamp straight
        # from the raw bytes - it may be nested here (snapshot), but it is
        # still real activity time, not the file copy time
        for m in reversed(_CHAT_TS_RAW_RE.findall(data)[-20:]):
            try:
                ts = _chat_parse_ts(m.decode("utf-8", "replace"))
            except Exception:
                ts = None
            if ts:
                out["ts"] = ts
                break
    return out


def _chat_scan_file(path, mtime, size):
    """Parse the transcript head -> cache record.
    Read in STREAM mode and stop as soon as both cwd and a title are found."""
    stats = {"bytes": 0, "lines": 0}
    sid = os.path.basename(path)[:-6]   # file name = sessionId (fallback)
    cwd = ""
    title = ""
    weak = ""        # short ("ok", "6") - usable only if nothing else is available
    ai_title = ""    # type=ai-title: Claude Code's own name, if any
    got_sid = False
    started = None   # first timestamp of the head = chat start
    head_ts = None   # last timestamp seen in the head (fallback)
    head = _chat_head_lines(path, stats)   # keep a reference: close explicitly
    for line in head:
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if not cwd:
            val = rec.get("cwd")
            if isinstance(val, str) and val.startswith("/"):
                cwd = val
        val = _chat_parse_ts(rec.get("timestamp"))
        if val:
            head_ts = val
            if started is None:
                started = val
        if not got_sid:
            val = rec.get("sessionId")
            if isinstance(val, str) and CHAT_UUID_RE.match(val):
                sid, got_sid = val, True
        if not title:
            if not ai_title and rec.get("type") == "ai-title":
                val = rec.get("aiTitle")
                if isinstance(val, str) and val.strip():
                    ai_title = " ".join(val.split())[:CHAT_TITLE_MAX]
            cand = _chat_clean_text(_chat_user_text(rec))
            if cand:
                if len(cand) >= CHAT_TITLE_MIN:
                    title = cand
                elif not weak:
                    weak = cand
        if title and cwd:
            break
    head.close()      # close the file and finish filling stats without waiting for GC
    # titles from the file tail: owner name (custom-title) and auto (ai-title)
    tail = _chat_tail_titles(path, size)
    # chat time - from the CONTENT (last timestamp = last activity),
    # not from mtime: for files copied in bulk during a move mtime is the same
    # for all. If the tail has no timestamp (the file ends with a truncated
    # giant record) - take the last one seen in the head; mtime - only
    # when there are no timestamps at all
    ts = tail.get("ts") or head_ts or mtime
    # jsonl records are not strictly ordered (attachment/queue-operation may
    # sit "in the future" relative to neighbours) - start no later than end
    started = min(started or ts, ts)
    own = tail.get("own", "")
    ai_title = tail.get("ai", "") or ai_title   # the tail is fresher than the head
    summ = tail.get("sum", "")
    msg_title = title or weak
    if not msg_title and not (own or ai_title or summ) and stats.get("dropped"):
        # the head consisted of dropped megabyte lines - go rescue
        rescued, rcwd = _chat_rescue(path)
        msg_title = _chat_clean_text(rescued)
        cwd = cwd or rcwd
    # title priority: owner name -> Claude Code auto name -> summary ->
    # first message -> date. tsrc tells the frontend where the title came from:
    # own - set by the owner, ai/sum - machine, msg - first message, date - nothing found
    if own:
        title, tsrc = own, "own"
    elif ai_title:
        title, tsrc = ai_title, "ai"
    elif summ:
        title, tsrc = summ, "sum"
    elif msg_title:
        title, tsrc = msg_title, "msg"
    else:
        # no meaningful message at all (session aborted, only commands) -
        # show the date/time as agreed
        title = "Chat " + time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        tsrc = "date"
    # mtime stays as is - chats_index uses it (and size) to decide whether to
    # rescan the file; ts goes out as the chat time
    return {"v": CHAT_CACHE_VER, "id": sid, "cwd": cwd,
            "title": _chat_shorten(title), "tsrc": tsrc,
            "mtime": mtime, "ts": ts, "started": started, "size": size,
            "model": tail.get("model", ""),
            # APPROXIMATE: an estimate of the number of file RECORDS (messages +
            # service lines) obtained by sampling, not an exact count
            "msgs": _chat_count_lines(path, size)}


def load_chats_cache():
    """Transcript parse cache: {path: {mtime,size,title,cwd,id,msgs}}.
    A broken/missing file is not an error, just a cold start."""
    try:
        with open(CHATS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, dict)
                    and isinstance(v.get("mtime"), int)}
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def save_chats_cache(cache):
    """Atomic (temp + os.replace), like save_folders. No .bak: the cache
    can be rebuilt by a scan, nothing to lose."""
    try:
        os.makedirs(os.path.dirname(CHATS_CACHE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="chats_",
                                   dir=os.path.dirname(CHATS_CACHE_FILE))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CHATS_CACHE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _chat_iter_files():
    """Cheap walk: scandir over projects + stat. Not a single open()."""
    try:
        dirs = list(os.scandir(CHAT_PROJECTS_DIR))
    except OSError:
        return
    for d in dirs:
        try:
            if not d.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(d.path))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.name.endswith(".jsonl") or \
                        not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat()
            except OSError:
                continue
            yield d.name, e.path, int(st.st_mtime), st.st_size


def chats_index():
    """Index of all chats, no staler than CHATS_TTL (in the manner of previews_cached:
    cache under a lock, a crowd of clients does not spawn work).
    Expensive parsing - ONLY for files with changed mtime/size."""
    global _chats_state
    with _chats_lock:
        now = time.time()
        st = _chats_state
        if st["rows"] is not None and now - st["t"] < CHATS_TTL:
            return st["rows"]
        cache = st["cache"]
        if cache is None:
            cache = load_chats_cache()
        rows, alive, dirty = [], set(), False
        for slug, path, mtime, size in _chat_iter_files():
            alive.add(path)
            rec = cache.get(path)
            if not rec or rec.get("v") != CHAT_CACHE_VER \
                    or rec.get("mtime") != mtime or rec.get("size") != size:
                rec = _chat_scan_file(path, mtime, size)
                cache[path] = rec
                dirty = True
            row = dict(rec)
            row["slug"] = slug
            row["path"] = path      # internal, not exposed
            rows.append(row)
        if len(cache) != len(alive):        # files were deleted - clean the cache
            for dead in [p for p in cache if p not in alive]:
                del cache[dead]
                dirty = True
        # sort by last-activity time from the content; mtime is a
        # fallback for records without a single timestamp
        rows.sort(key=lambda r: r.get("ts") or r["mtime"], reverse=True)
        if dirty:
            save_chats_cache(cache)
        _chats_state = {"t": now, "rows": rows, "cache": cache}
        return rows


def load_chat_names():
    """Custom chat names: {sessionId: name}."""
    for p in (CHAT_NAMES_FILE, CHAT_NAMES_FILE + ".bak"):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if isinstance(k, str) and isinstance(v, str)}
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return {}


def save_chat_names(data):
    """Atomic write + .bak - exactly like save_folders (line 190)."""
    try:
        os.makedirs(os.path.dirname(CHAT_NAMES_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="chatnames_",
                                   dir=os.path.dirname(CHAT_NAMES_FILE))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CHAT_NAMES_FILE)
        try:
            with open(CHAT_NAMES_FILE + ".bak", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def chat_name_clean(raw):
    """Chat name: no control characters, collapsed spaces, <=60.
    Any letters and punctuation are allowed (it is a human-facing title, not a tmux
    window name) - only what breaks JSON/terminal is stripped."""
    s = CHAT_CTL_RE.sub("", raw or "")
    return " ".join(s.split())[:CHAT_NAME_MAX].strip()


def chat_name_set(sid, name):
    """Set/clear the name under the lock. Empty name = delete (return to
    the auto title). Returns the name or None."""
    with _chat_names_lock:
        names = load_chat_names()
        if name:
            names[sid] = name
        else:
            names.pop(sid, None)
        save_chat_names(names)
    return name or None


def chat_find(sid):
    """Chat record by sessionId (or None). Looked up in the fresh index; the id
    comes from the transcript, so it matches even if the file was renamed."""
    for row in chats_index():
        if row["id"] == sid:
            return row
    return None


def chat_remap_cwd(cwd):
    """Transcripts store the cwd of the machine they were recorded on; if that
    folder is gone, the caller falls back to WORKDIR."""
    return cwd


def chats_list(limit=CHAT_LIMIT_DEFAULT, project="all", q=""):
    """Ready reply of /m/api/chats: filters + sorting + slice."""
    rows = chats_index()
    names = load_chat_names()
    if project and project != "all":
        rows = [r for r in rows if r["slug"] == project]
    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        name = names.get(r["id"])
        if needle:
            hay = " ".join((r["title"], name or "", r["cwd"], r["slug"], r["id"]))
            if needle not in hay.lower():
                continue
        ts = r.get("ts") or r["mtime"]
        out.append({"id": r["id"], "slug": r["slug"],
                    "cwd": chat_remap_cwd(r["cwd"]),
                    "title": r["title"], "tsrc": r.get("tsrc") or "msg",
                    # the field is still called mtime (the frontend formats it),
                    # but it carries the last-activity time from the transcript
                    "name": name, "mtime": ts,
                    "started": r.get("started") or ts,
                    "size": r["size"], "msgs": r["msgs"]})
    return {"chats": out[:limit], "total": len(out)}


def chat_project_check(project):
    """Validate ?project=: only a direct subdirectory of CHAT_PROJECTS_DIR.
    Any separator/.. /symlink outward -> False (the handler returns 403)."""
    if not project or project == "all":
        return True
    if "/" in project or "\\" in project or project in (".", ".."):
        return False
    real = os.path.realpath(os.path.join(CHAT_PROJECTS_DIR, project))
    return (real == CHAT_PROJECTS_DIR or
            real.startswith(CHAT_PROJECTS_DIR + os.sep)) and os.path.isdir(real)


def chat_path_check(path):
    """The same check for paths from the cache: the file must lie inside
    the Claude projects dir (the cache could be stale/tampered)."""
    real = os.path.realpath(path or "")
    return real.startswith(CHAT_PROJECTS_DIR + os.sep)


def chat_windows_after(idx):
    """Switch to window idx and return the window list - one tmux call,
    format exactly like /m/api/select (line 4881), so the frontend redraws
    the tabs without a second request."""
    fmt = "#{window_index}\t#{window_name}\t#{window_active}\t#{window_id}"
    rc, out = tmux_live("select-window", "-t", f"{SESSION}:{idx}", ";",
                        "refresh-client", ";",
                        "list-windows", "-t", SESSION, "-F", fmt)
    if rc != 0:
        return []
    with _state_lock:
        st_by_id = {x["id"]: x["st"] for x in (_state_cache.get("data") or [])}
    wins = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 4:
            active = p[2] == "1"
            st = st_by_id.get(p[3], "idle")
            if active and st == "done":
                st = "idle"
            wins.append({"index": int(p[0]), "name": p[1], "active": active,
                         "id": p[3], "st": st})
    return wins


def chat_open(sid):
    """Resume a Claude Code chat: new tmux window in its cwd running
    `claude --resume <uuid>`. Returns (index, windows, warn). `sid` must have
    been validated against CHAT_UUID_RE by the caller."""
    row = chat_find(sid)
    if not row:
        raise KeyError("chat not found")
    if not chat_path_check(row.get("path", "")):
        raise PermissionError("transcript is outside the Claude projects folder")
    warn = ""
    cwd = chat_remap_cwd(row.get("cwd") or "")
    if not cwd or not os.path.isdir(cwd):
        warn = f"folder {cwd or '?'} no longer exists, opened in {WORKDIR}"
        cwd = WORKDIR
    ensure_session()
    rc, out = tmux("new-window", "-P", "-t", SESSION, "-c", cwd, "-F", "#{window_index}")
    if rc != 0:
        raise RuntimeError("could not create a window")
    idx = int(out.strip() or "0")
    name = load_chat_names().get(sid) or row.get("title") or "chat"
    name = WINNAME_RE.sub("", name)[:24].strip() or "chat"
    tmux("rename-window", "-t", f"{SESSION}:{idx}", name)
    _mark_manual(idx)
    # the uuid already passed CHAT_UUID_RE; quote anyway - it goes to a shell
    tmux("send-keys", "-t", f"{SESSION}:{idx}", "-l", "--", "claude --resume " + shlex.quote(sid))
    tmux("send-keys", "-t", f"{SESSION}:{idx}", "Enter")
    return idx, chat_windows_after(idx), warn


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="app-ver" content="__APP_VER__">
<title>Terminals</title>
<style>
/* ============================================================
   OLED BLACK — monochrome hairline glass terminal shell
   iOS Safari / iPhone. Pure CSS.
   ============================================================ */
:root{
  --bg:#000;
  --panel:rgba(14,14,15,.66);
  --field:#0c0c0d;
  --hair:rgba(255,255,255,.12);
  --hair-soft:rgba(255,255,255,.08);
  --hair-bright:rgba(255,255,255,.25);
  --ink:#f4f4f5;
  --ink-dim:rgba(255,255,255,.72);
  --ink-mute:rgba(255,255,255,.38);
  --sel:rgba(255,255,255,.10);
  --sel-strong:rgba(255,255,255,.16);
  --glow:rgba(255,255,255,.16);
  --ok:#30d158;   /* green 'done' dot of the activity indicator */
  --tap:38px;   /* round buttons of the top bar (secondary) */
  --row:46px;   /* bottom bar buttons, input field, list rows */
  --tab-h:32px; /* tab pills */
  --key-h:38px; /* special-key pills */
  --r:13px;
  --ease:cubic-bezier(.2,.7,.2,1);
}
*,*::before,*::after{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html,body{
  position:fixed; inset:0; margin:0; padding:0;
  overflow:hidden; touch-action:none; overscroll-behavior:none;
  background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  font-size:15px; -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility; -webkit-text-size-adjust:100%;
}
::-webkit-scrollbar{ width:0; height:0; display:none; }
*{ scrollbar-width:none; }
#app{ display:flex; flex-direction:column; overflow:hidden; background:var(--bg); width:100%; }

/* shared glass for chrome bars */
#bar,#inputbar{
  position:relative; background:var(--panel);
  -webkit-backdrop-filter:blur(18px) saturate(120%); backdrop-filter:blur(18px) saturate(120%);
}
#bar::after,#inputbar::before{
  content:""; position:absolute; left:0; right:0; height:1px; pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.14) 18%,rgba(255,255,255,.14) 82%,transparent);
}
#bar::after{ bottom:0; }
#inputbar::before{ top:0; }

/* ===== TOP BAR ===== */
#bar{
  flex:0 0 auto; display:flex; align-items:center; gap:8px;
  /* at least 26px on top: strip reserved for the .tgv-pill pill; with safe-area=0
   (landscape / no notch) its tap zone would otherwise overlap bar buttons and tabs */
  padding:7px 10px; padding-top:calc(7px + max(env(safe-area-inset-top, 0px), 26px));
  padding-left:calc(10px + env(safe-area-inset-left));
  padding-right:calc(10px + env(safe-area-inset-right));
  min-width:0; border-bottom:.5px solid var(--hair);
}
.ctl{
  flex:0 0 auto; width:var(--tap); height:var(--tap);
  display:flex; align-items:center; justify-content:center;
  line-height:1; color:var(--ink-dim);
  background:rgba(255,255,255,.04); border:.5px solid var(--hair); border-radius:50%;
  cursor:pointer; transition:background .18s var(--ease),color .18s var(--ease),transform .12s var(--ease);
}
.ctl:active{ transform:scale(.95); color:var(--ink); background:var(--sel); }
#tabs{
  flex:1 1 auto; display:flex; align-items:center; gap:6px; min-width:0;
  overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; padding:1px 2px;
  touch-action:pan-x;
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 6px,#000 calc(100% - 6px),transparent);
  mask-image:linear-gradient(90deg,transparent,#000 6px,#000 calc(100% - 6px),transparent);
}
.tab{
  flex:0 0 auto; display:inline-flex; align-items:center; gap:6px; max-width:160px; height:var(--tab-h);
  padding:0 12px; font-size:13px; letter-spacing:.01em; color:var(--ink-dim);
  background:rgba(255,255,255,.03); border:.5px solid var(--hair-soft); border-radius:999px;
  cursor:pointer; white-space:nowrap; touch-action:none;
  transition:background .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease),box-shadow .18s var(--ease),transform .12s var(--ease);
}
.tab>span:first-child{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tab:active{ transform:scale(.95); }
.tab.active{
  color:var(--ink); background:rgba(255,255,255,.14); border-color:var(--hair-bright);
  box-shadow:0 0 0 .5px rgba(255,255,255,.25),0 0 16px -3px rgba(255,255,255,.38);
}
.tab .x{
  flex:0 0 auto; display:inline-flex; align-items:center; justify-content:center;
  width:28px; height:28px; margin:-3px -9px -3px -5px;
  font-size:14px; line-height:1; color:var(--ink-mute); border-radius:50%;
  transition:color .15s var(--ease),background .15s var(--ease),transform .15s var(--ease);
}
.tab .x:active{ color:var(--ink); background:rgba(255,255,255,.12); }
/* full tab name on hover/touch: a pill above the tab, in the empty
   strip at the top (same place as the window-panel handle). Names get cut
   at 160px. position:fixed -
   works when #tabs moves to the bottom; pointer-events:none does not block
   taps; above the handle pill (higher z-index) while the cursor is on a tab. */
#tabhint{
  position:fixed; z-index:7710; top:0; left:0; height:22px; line-height:22px;
  padding:0 12px; font-size:12.5px; letter-spacing:.01em; color:var(--ink);
  background:var(--panel); border:.5px solid var(--hair-bright); border-radius:999px;
  box-shadow:0 2px 10px -2px rgba(0,0,0,.6); white-space:nowrap;
  max-width:calc(100vw - 16px); overflow:hidden; text-overflow:ellipsis;
  pointer-events:none; opacity:0; transition:opacity .12s var(--ease);
}
#tabhint.on{ opacity:1; }
/* window activity indicator: dot between label and close button / in the tile header */
.tind{ flex:0 0 auto; width:7px; height:7px; border-radius:50%; background:transparent; pointer-events:none; }
.tind-done{ background:var(--ok); box-shadow:0 0 6px -1px var(--ok); }
.tind-work{ background:var(--ink-dim); animation:tind-pulse 3s ease-in-out infinite; }
@keyframes tind-pulse{ 0%,100%{opacity:.55;transform:scale(.96)} 50%{opacity:.95;transform:scale(1.04)} }
#mem{
  flex:0 0 auto; margin-left:auto; padding-left:4px; font-size:11px;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1; letter-spacing:.02em;
  color:var(--ink-mute); white-space:nowrap;
}
#mem.low{ color:var(--ink); }

/* ===== TERMINAL ===== */
#wrap{ flex:1 1 auto; position:relative; overflow:hidden; background:#000; }
#term{ position:absolute; inset:0; width:100%; height:100%; border:0; display:block; background:#000; }

/* drop zones for dragging a tab into a split (shown while dragging) */
.dz{ position:absolute; inset:0; z-index:200; padding:8px;
  display:grid; gap:6px;
  grid-template-columns:1fr 1.2fr 1fr; grid-template-rows:1fr 1.2fr 1fr;
  grid-template-areas:". top ." "left mid right" ". bot ."; }
.dz.fp-hidden{ display:none; }
.dz-zone{ display:flex; align-items:center; justify-content:center;
  border:1px dashed rgba(255,255,255,.35); border-radius:12px;
  background:rgba(255,255,255,.05); color:var(--ink-dim); font-size:13px; font-weight:600;
  -webkit-backdrop-filter:none; backdrop-filter:none; transition:background .1s,color .1s,border-color .1s; }
.dz-zone.hot{ background:rgba(89,194,255,.28); border-color:#59c2ff; color:#fff; }
.dragghost{ position:fixed; z-index:9700; pointer-events:none;
  padding:6px 12px; border-radius:999px; background:rgba(89,194,255,.9); color:#001;
  font-size:13px; font-weight:700; box-shadow:0 8px 24px -6px rgba(0,0,0,.6);
  transform:translate(-50%,-140%); white-space:nowrap; }
.dragghost.fp-hidden{ display:none; }
.tab.dragging{ opacity:.45; }
.tab.reorder{ position:relative; z-index:20; opacity:.94; transition:none;
  box-shadow:0 10px 26px -8px rgba(0,0,0,.7); }

/* ===== QUICK KEYS ===== */
#keys{
  flex:0 0 auto; display:flex; align-items:center; gap:8px; padding:8px 10px;
  padding-left:calc(10px + env(safe-area-inset-left));
  padding-right:calc(10px + env(safe-area-inset-right));
  overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; touch-action:pan-x;
  background:var(--panel); -webkit-backdrop-filter:blur(18px) saturate(120%); backdrop-filter:blur(18px) saturate(120%);
  border-top:.5px solid var(--hair);
}
#keys.hidden{ display:none; }
.key{
  flex:0 0 auto; height:var(--key-h); min-width:40px; padding:0 12px;
  display:inline-flex; align-items:center; justify-content:center; font-size:13px; line-height:1;
  color:var(--ink-dim); background:rgba(255,255,255,.04); border:.5px solid var(--hair-soft); border-radius:9px;
  cursor:pointer; white-space:nowrap;
  transition:background .15s var(--ease),color .15s var(--ease),border-color .15s var(--ease),transform .1s var(--ease);
}
.key:active{ transform:scale(.95); color:var(--ink); background:var(--sel); border-color:var(--hair); }
.key.cmd{
  color:var(--ink); background:rgba(255,255,255,.07); border-color:var(--hair-bright);
  font-weight:700; letter-spacing:.02em;
}
.key.cmd:active{ background:var(--sel-strong); }
.sep{ flex:0 0 auto; width:.5px; align-self:stretch; margin:5px 4px; background:var(--hair); }
#keys-cmds{ display:contents; } /* transparent to flexbox: command buttons become direct flex items of #keys (horizontal row) */

/* ===== INPUT BAR ===== */
#inputbar{
  flex:0 0 auto; display:flex; align-items:flex-end; gap:8px;
  padding:8px 10px; padding-bottom:calc(8px + env(safe-area-inset-bottom));
  padding-left:calc(10px + env(safe-area-inset-left));
  padding-right:calc(10px + env(safe-area-inset-right));
  border-top:.5px solid var(--hair);
}
/* --- Strip of attached photo thumbnails (above the composer) --- */
#attachbar{
  flex:0 0 auto; display:none; gap:8px; align-items:center;
  padding:8px 10px 2px; overflow-x:auto; overflow-y:hidden;
  -webkit-overflow-scrolling:touch; scrollbar-width:none;
  padding-left:calc(10px + env(safe-area-inset-left));
  padding-right:calc(10px + env(safe-area-inset-right));
}
#attachbar::-webkit-scrollbar{ display:none; }
#attachbar.on{ display:flex; }
.thumb{
  position:relative; flex:0 0 auto; width:56px; height:56px; border-radius:12px;
  overflow:hidden; background:var(--field); border:.5px solid var(--hair);
  cursor:pointer; -webkit-tap-highlight-color:transparent;
  animation:thumbIn .22s var(--ease); transition:transform .12s var(--ease);
}
.thumb:active{ transform:scale(.94); }
.thumb img{ width:100%; height:100%; object-fit:cover; display:block; pointer-events:none; }
@keyframes thumbIn{ from{ opacity:0; transform:scale(.7); } to{ opacity:1; transform:scale(1); } }
.thumb-x{
  position:absolute; top:-1px; right:-1px; width:20px; height:20px;
  display:flex; align-items:center; justify-content:center; line-height:1;
  color:#fff; font-size:13px; font-weight:600;
  background:rgba(0,0,0,.72); border:.5px solid rgba(255,255,255,.35);
  border-radius:0 12px 0 10px; cursor:pointer; z-index:2;
}
.thumb-x::before{ content:''; position:absolute; inset:-12px; }  /* tap target >=40px */
.thumb-x:active{ background:#000; }
/* --- Lightbox for fullscreen photo preview --- */
#lightbox{
  position:fixed; inset:0; z-index:120; display:none;
  align-items:center; justify-content:center;
  background:#000;               /* solid dark background, NO blur (iOS) */
  opacity:0; transition:opacity .2s var(--ease);
  padding:env(safe-area-inset-top) env(safe-area-inset-right)
          env(safe-area-inset-bottom) env(safe-area-inset-left);
}
#lightbox.on{ display:flex; }
#lightbox.show{ opacity:1; }
#lightbox img{
  max-width:100%; max-height:100%; object-fit:contain;
  border-radius:6px; transform:scale(.92);
  transition:transform .26s cubic-bezier(.2,.9,.25,1.2);   /* light spring */
}
#lightbox.show img{ transform:scale(1); }
#lightbox .lb-x{
  position:absolute; top:calc(10px + env(safe-area-inset-top));
  right:calc(12px + env(safe-area-inset-right));
  width:40px; height:40px; display:flex; align-items:center; justify-content:center;
  color:#fff; font-size:22px; line-height:1; border-radius:50%;
  background:rgba(255,255,255,.10); border:.5px solid rgba(255,255,255,.22);
  cursor:pointer; z-index:2;
}
#lightbox .lb-x:active{ background:rgba(255,255,255,.22); }
#keystoggle{
  flex:0 0 auto; width:var(--row); height:var(--row);
  display:flex; align-items:center; justify-content:center; line-height:1;
  color:var(--ink-dim); background:rgba(255,255,255,.04); border:.5px solid var(--hair); border-radius:50%;
  cursor:pointer; transition:background .18s var(--ease),color .18s var(--ease),transform .12s var(--ease);
}
#keystoggle:active{ transform:scale(.95); color:var(--ink); background:var(--sel); }
#keystoggle.on{ color:var(--ink); background:var(--sel-strong); box-shadow:0 0 0 .5px rgba(255,255,255,.20),0 0 14px -4px rgba(255,255,255,.30); }
#tin{
  flex:1 1 auto; min-width:0; min-height:var(--row); max-height:160px; padding:12px 14px;
  font-family:inherit; font-size:16px; line-height:1.3; color:var(--ink); caret-color:#fff;
  background:var(--field); border:.5px solid var(--hair); border-radius:var(--r);
  resize:none; outline:none; overflow-y:auto; touch-action:pan-y; -webkit-appearance:none; appearance:none;
  transition:border-color .2s var(--ease),box-shadow .2s var(--ease),background .2s var(--ease);
}
#tin::placeholder{ color:var(--ink-mute); font-weight:300; letter-spacing:.005em; }
#tin:focus{ border-color:var(--hair-bright); background:#0e0e10; box-shadow:0 0 0 .5px rgba(255,255,255,.18),0 0 0 4px var(--glow); }
#send{
  flex:0 0 auto; width:var(--row); height:var(--row);
  display:flex; align-items:center; justify-content:center; line-height:1;
  color:#000; background:#fff; border:.5px solid rgba(255,255,255,.85); border-radius:50%;
  cursor:pointer; box-shadow:0 0 18px -5px rgba(255,255,255,.6);
  transition:transform .12s var(--ease),box-shadow .2s var(--ease),opacity .2s var(--ease);
}
#send svg{ transform:translateX(1px); }  /* optical center of the triangle */
#send:active{ transform:scale(.9); opacity:.92; }
#send:disabled{ opacity:.45; }

/* busy: spinning ring over the button (feedback for sending/loading) */
.busy{ position:relative; pointer-events:none; }
.busy>*{ opacity:.25; }
.busy::after{
  content:""; position:absolute; left:50%; top:50%; width:16px; height:16px;
  margin:-8px 0 0 -8px; border-radius:50%;
  border:2px solid rgba(255,255,255,.16); border-top-color:rgba(255,255,255,.6);
  -webkit-animation:mtspin .8s linear infinite; animation:mtspin .8s linear infinite;
}
#send.busy::after{ border-color:rgba(0,0,0,.12); border-top-color:rgba(0,0,0,.55); }
@-webkit-keyframes mtspin{ to{ transform:rotate(360deg); } }
@keyframes mtspin{ to{ transform:rotate(360deg); } }

/* ===== TOAST ===== */
#toast{
  position:fixed; left:50%; bottom:calc(78px + env(safe-area-inset-bottom));
  transform:translateX(-50%) translateY(8px); max-width:80vw; padding:9px 16px;
  font-size:13px; letter-spacing:.01em; color:var(--ink); text-align:center;
  background:rgba(10,10,11,.82); -webkit-backdrop-filter:blur(20px) saturate(140%); backdrop-filter:blur(20px) saturate(140%);
  border:.5px solid var(--hair-bright); border-radius:999px;
  box-shadow:0 8px 32px -12px rgba(0,0,0,.9),0 0 0 .5px rgba(255,255,255,.06) inset;
  opacity:0; pointer-events:none; z-index:9999;
  transition:opacity .25s var(--ease),transform .25s var(--ease);
}
#toast.show,#toast.visible{ opacity:1; transform:translateX(-50%) translateY(0); }

/* control-button icons */
.ctl svg,#keystoggle svg,#micbtn svg{ display:block; }
/* mic (voice input) button — matches #keystoggle metrics */
#micbtn{
  flex:0 0 auto; width:var(--row); height:var(--row);
  display:flex; align-items:center; justify-content:center;
  color:var(--ink-dim); background:rgba(255,255,255,.04);
  border:.5px solid var(--hair); border-radius:50%; cursor:pointer;
  transition:background .18s var(--ease),color .18s var(--ease),transform .12s var(--ease);
}
#micbtn:active{ transform:scale(.93); color:var(--ink); background:var(--sel); }
/* screen recording is desktop only: getDisplayMedia does not work on mobile,
   so the button is hidden in mobile mode 2.0 (JS also hides it where the API is missing) */
/* preview of the captured clip in the screen-recording widget */

/* ===== Voice input modal ===== */
.vc-banner{ margin:10px 12px 0; padding:9px 12px; font-size:12.5px; line-height:1.35;
  color:var(--ink-dim); background:rgba(255,255,255,.04); border:.5px solid var(--hair-soft); border-radius:10px; flex:0 0 auto; }
.vc-body{ flex:1 1 auto; display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:14px 12px; gap:10px; min-height:120px; overflow-y:auto; touch-action:pan-y; }
.vc-rec{ width:72px; height:72px; border-radius:50%; border:1px solid var(--hair-bright);
  background:rgba(255,255,255,.05); color:var(--ink); display:flex; align-items:center; justify-content:center;
  cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),background .18s var(--ease),color .18s var(--ease); }
.vc-rec:active{ transform:scale(.95); }
.vc-rec svg{ width:30px; height:30px; display:block; }
.vc-rec.rec{ background:#fff; color:#000; -webkit-animation:vcPulse 1.5s var(--ease) infinite; animation:vcPulse 1.5s var(--ease) infinite; }
@-webkit-keyframes vcPulse{ 0%{box-shadow:0 0 0 0 rgba(255,255,255,.30)} 70%{box-shadow:0 0 0 20px rgba(255,255,255,0)} 100%{box-shadow:0 0 0 0 rgba(255,255,255,0)} }
@keyframes vcPulse{ 0%{box-shadow:0 0 0 0 rgba(255,255,255,.30)} 70%{box-shadow:0 0 0 20px rgba(255,255,255,0)} 100%{box-shadow:0 0 0 0 rgba(255,255,255,0)} }
.vc-time{ font-size:22px; font-variant-numeric:tabular-nums; letter-spacing:.04em; color:var(--ink); }
.vc-hint{ font-size:13px; color:var(--ink-mute); text-align:center; max-width:280px; }
.vc-text{ width:100%; min-height:130px; max-height:40vh; resize:none; background:var(--field); color:var(--ink);
  border:.5px solid var(--hair); border-radius:var(--r); padding:12px 14px; font-size:16px; line-height:1.4;
  outline:none; -webkit-appearance:none; appearance:none; }
.vc-text:focus{ border-color:var(--hair-bright); background:#0e0e10; box-shadow:0 0 0 3px var(--glow); }
.vc-actions{ display:flex; flex-wrap:wrap; gap:8px; padding:10px 12px; padding-bottom:max(10px,env(safe-area-inset-bottom));
  border-top:.5px solid var(--hair-soft); flex:0 0 auto; }
.vc-actions .vc-btn{ flex:1 1 calc(50% - 4px); min-width:calc(50% - 4px); }
.vc-hint.err{ color:#ff8f93; }

/* transparent catcher backdrop: a tap outside the menu (incl. on the terminal iframe) closes it */
.nm-ovl{ position:fixed; inset:0; z-index:9490; background:transparent; }
/* choice menu on '+': what to launch in a new window */
.nm{ position:fixed; z-index:9500; display:flex; flex-direction:column; min-width:160px;
  padding:6px; gap:2px; border-radius:14px; border:.5px solid var(--hair);
  background:rgba(20,20,22,.92); -webkit-backdrop-filter:blur(16px) saturate(120%);
  backdrop-filter:blur(16px) saturate(120%); box-shadow:0 12px 40px -8px rgba(0,0,0,.7); }
.nm.fp-hidden{ display:none; }
.nm-item{ height:44px; padding:0 14px; text-align:left; border:0; border-radius:10px;
  background:transparent; color:var(--ink); font:600 15px/1 inherit; cursor:pointer;
  -webkit-appearance:none; appearance:none; transition:background .12s var(--ease); }
.nm-item:active{ background:var(--sel-strong); transform:scale(.98); }
.nm-item.nm-primary{ background:var(--sel); box-shadow:inset 0 0 0 .5px var(--hair-bright); }
.nm-item.nm-primary::after{ content:'default'; float:right; font:600 11px/1 inherit;
  opacity:.55; margin-left:10px; align-self:center; }
.vc-btn{ flex:1 1 auto; height:var(--row); border-radius:var(--r); border:.5px solid var(--hair);
  background:rgba(255,255,255,.05); color:var(--ink); font-size:15px; font-weight:600; cursor:pointer;
  -webkit-appearance:none; appearance:none; transition:transform .12s var(--ease),opacity .15s var(--ease); }
.vc-btn:active{ transform:scale(.97); }
.vc-btn:disabled{ opacity:.4; }
.vc-primary{ background:#fff; color:#000; border-color:#fff; }
[hidden]{ display:none !important; }

/* ===== HISTORY (composer): button in the corner of the field + compact popup.
   hist- classes only (not IDs - project pitfall); solid background, NO backdrop-filter. */
.hist-hidden{ display:none !important; }

/* ===== KILL CONFIRM: explicit confirmation for closing a window (tab close button).
   Same style as hist-pop/set-pop - solid background, NO backdrop-filter. kc- classes only. */
.kc-ovl{ position:fixed; inset:0; z-index:9600; background:transparent; }
.kc-pop{
  position:fixed; z-index:9700; left:50%; top:50%; transform:translate(-50%,-50%);
  width:88%; max-width:340px;
  background:linear-gradient(var(--panel),var(--panel)) #000;
  border:.5px solid var(--hair-bright); border-radius:var(--r);
  box-shadow:0 12px 40px -8px rgba(0,0,0,.8);
  display:flex; flex-direction:column; gap:14px; padding:16px 16px 14px;
}
.kc-msg{ color:var(--ink); font-size:15px; line-height:1.4; word-break:break-word; }
.kc-msg b{ color:var(--ink); }
.kc-row{ display:flex; gap:10px; }
.kc-btn{
  flex:1 1 auto; min-height:44px; padding:0 12px;
  display:flex; align-items:center; justify-content:center;
  border-radius:10px; border:.5px solid var(--hair);
  background:rgba(255,255,255,.05); color:var(--ink);
  font-size:15px; font-weight:600; line-height:1; cursor:pointer;
  -webkit-appearance:none; appearance:none; transition:transform .12s var(--ease),background .12s var(--ease);
}
.kc-btn:active{ transform:scale(.97); background:var(--sel); }
.kc-cancel{ flex:1.3 1 auto; }
.kc-danger{ flex:1 1 auto; color:#ff6b6b; border-color:rgba(255,107,107,.55); background:rgba(255,107,107,.08); }
.kc-danger:active{ background:rgba(255,107,107,.18); }
.hist-btn{
  position:absolute; z-index:5; top:14px;
  right:calc(23px + env(safe-area-inset-right,0px) + var(--row));
  width:22px; height:22px; padding:0;
  display:flex; align-items:center; justify-content:center;
  color:var(--ink-dim); opacity:.5; background:transparent; border:none; border-radius:6px;
  cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:opacity .15s var(--ease),color .15s var(--ease);
}
.hist-btn:active{ opacity:1; color:var(--ink); background:var(--sel); }
.hist-ovl{ position:fixed; inset:0; z-index:9400; background:transparent; }
.hist-pop{
  position:fixed; z-index:9500; left:50%; transform:translateX(-50%);
  width:92%; max-width:600px;
  background:linear-gradient(var(--panel),var(--panel)) #000;
  border:.5px solid var(--hair-bright); border-radius:var(--r);
  box-shadow:0 12px 40px -8px rgba(0,0,0,.8);
  display:flex; flex-direction:column; gap:8px; padding:10px;
}
.hist-nav{ display:flex; align-items:center; gap:6px; }
.hist-arr{
  flex:0 0 auto; width:44px; height:40px;
  display:flex; align-items:center; justify-content:center;
  color:var(--ink); background:rgba(255,255,255,.05); border:.5px solid var(--hair);
  border-radius:10px; font-size:17px; line-height:1; cursor:pointer;
  -webkit-appearance:none; appearance:none;
}
.hist-arr:active{ background:var(--sel); }
.hist-arr:disabled{ opacity:.3; }
.hist-pos{
  min-width:58px; text-align:center; color:var(--ink-dim); font-size:13px;
  font-variant-numeric:tabular-nums;
}
.hist-sp{ flex:1 1 auto; }
.hist-x{
  flex:0 0 auto; width:40px; height:40px;
  display:flex; align-items:center; justify-content:center;
  color:var(--ink-dim); background:transparent; border:.5px solid var(--hair);
  border-radius:10px; font-size:15px; line-height:1; cursor:pointer;
  -webkit-appearance:none; appearance:none;
}
.hist-x:active{ background:var(--sel); color:var(--ink); }
.hist-text{
  margin:0; padding:8px 10px; min-height:42px; max-height:106px; overflow-y:auto;
  font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--ink);
  white-space:pre-wrap; word-break:break-word;
  background:var(--field); border:.5px solid var(--hair-soft); border-radius:10px;
}
.hist-insert{
  height:40px; border-radius:10px; border:.5px solid var(--hair);
  background:rgba(255,255,255,.08); color:var(--ink); font-size:14px; font-weight:600;
  cursor:pointer; -webkit-appearance:none; appearance:none;
}
.hist-insert:active{ background:var(--sel-strong); }
.hist-insert:disabled{ opacity:.4; }

/* ===== Open Folder modal ===== */
#fp-modal{
  position:fixed; inset:0; z-index:9000;
  display:flex; align-items:flex-end; justify-content:center;
  font:16px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; color:var(--ink);
  -webkit-tap-highlight-color:transparent;
}
@media (min-width:540px){ #fp-modal{ align-items:center; } }
#vc-modal{ font:16px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; color:var(--ink);
  -webkit-tap-highlight-color:transparent; }
#fp-modal.fp-hidden,#vc-modal.fp-hidden,.fp-hidden{ display:none; }
.fp-backdrop{
  /* SOLID background, NO backdrop-filter: blur over a live WebGL terminal
   froze iOS Safari (the compositor resampled the canvas every frame) */
  position:absolute; inset:0; background:rgba(0,0,0,.72);
  -webkit-animation:fpFade .22s var(--ease) both; animation:fpFade .22s var(--ease) both;
}
/* while the folder modal is open, hide the terminal (do not paint WebGL under the overlay).
   voice is a floating widget, the terminal STAYS live (can scroll). */
body:has(#fp-modal:not(.fp-hidden)) #wrap{ visibility:hidden; }

/* voice: narrow floating DRAGGABLE widget (not a modal, no dimming) */
.vc-float{ position:fixed; z-index:9600; left:50%; bottom:96px; transform:translateX(-50%);
  width:min(320px,92vw); display:flex; flex-direction:column; overflow:hidden;
  background:rgba(22,22,24,.97); border:.5px solid var(--hair); border-radius:16px;
  box-shadow:0 18px 50px -10px rgba(0,0,0,.8); }
.vc-float.dragged{ transform:none; }   /* after dragging, disable centering */
.vc-float.fp-hidden{ display:none; }
.vc-drag{ position:relative;   /* otherwise the close button measures its top from the WHOLE window
     and hangs in the middle of it instead of the header (old layout pitfall) */
  display:flex; align-items:center; gap:8px; padding:9px 8px 9px 12px;
  cursor:grab; touch-action:none; border-bottom:.5px solid var(--hair-soft);
  -webkit-user-select:none; user-select:none; }
.vc-drag:active{ cursor:grabbing; }
.vc-drag .fp-title{ flex:1 1 auto; font-weight:700; font-size:13px; color:var(--ink); }
.vc-dot{ flex:0 0 auto; width:8px; height:8px; border-radius:50%; background:var(--ink-mute);
  transition:background .2s var(--ease); }
.vc-float.recording .vc-dot{ background:#f07178;
  -webkit-animation:vcpulse 1s ease-in-out infinite; animation:vcpulse 1s ease-in-out infinite; }
@-webkit-keyframes vcpulse{ 0%,100%{opacity:1} 50%{opacity:.3} }
@keyframes vcpulse{ 0%,100%{opacity:1} 50%{opacity:.3} }
.fp-sheet{
  position:relative; width:100%; max-width:520px; max-height:min(80vh,680px);
  display:flex; flex-direction:column; background:var(--panel);
  -webkit-backdrop-filter:blur(18px) saturate(120%); backdrop-filter:blur(18px) saturate(120%);
  border:.5px solid var(--hair); border-radius:16px 16px 0 0;
  box-shadow:0 -1px 0 var(--hair-soft) inset,0 24px 64px rgba(0,0,0,.72);
  padding-bottom:env(safe-area-inset-bottom); overflow:hidden;
  -webkit-animation:fpSlide .26s var(--ease) both; animation:fpSlide .26s var(--ease) both;
}
@media (min-width:540px){ .fp-sheet{ border-radius:16px; margin:0 16px;
  -webkit-animation:fpPop .24s var(--ease) both; animation:fpPop .24s var(--ease) both; } }
.fp-head{ position:relative; display:flex; align-items:center; justify-content:center;
  height:52px; flex:0 0 auto; border-bottom:.5px solid var(--hair-soft); }
.fp-title{ font-size:16px; font-weight:600; letter-spacing:.01em; color:var(--ink); }
.fp-close{ position:absolute; right:8px; top:50%; transform:translateY(-50%);
  width:44px; height:44px; display:flex; align-items:center; justify-content:center;
  background:transparent; border:0; padding:0; color:var(--ink-dim); cursor:pointer; border-radius:50%;
  transition:color .15s var(--ease),background .15s var(--ease); }
.fp-close:active{ color:var(--ink); background:var(--sel); }
.fp-pathrow{ display:flex; align-items:center; gap:8px; padding:10px 12px; flex:0 0 auto;
  border-bottom:.5px solid var(--hair-soft); }
.fp-input{ flex:1 1 auto; min-width:0; height:var(--row); padding:0 12px; font-size:16px;
  font-family:ui-monospace,"SF Mono",Menlo,monospace; color:var(--ink); background:var(--field);
  border:.5px solid var(--hair); border-radius:var(--r); outline:none; -webkit-appearance:none; appearance:none;
  transition:border-color .15s var(--ease),box-shadow .15s var(--ease); }
.fp-input::placeholder{ color:var(--ink-mute); }
.fp-input:focus{ border-color:var(--hair-bright); box-shadow:0 0 0 3px var(--glow); }
.fp-ok{ flex:0 0 auto; height:var(--row); min-width:56px; padding:0 18px; font-size:16px; font-weight:600;
  letter-spacing:.02em; color:#000; background:#fff; border:.5px solid #fff; border-radius:var(--r);
  cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),opacity .15s var(--ease); }
.fp-ok:active{ transform:scale(.96); opacity:.85; }
.fp-ok:disabled{ opacity:.4; }
.fp-favs{ display:flex; gap:6px; margin:0 12px 10px; padding-bottom:2px; flex:0 0 auto;
  overflow-x:auto; -webkit-overflow-scrolling:touch; touch-action:pan-x; }
.fp-chip{ flex:0 0 auto; display:inline-flex; align-items:center; gap:6px; height:32px;
  padding:0 6px 0 12px; border-radius:999px; border:.5px solid var(--hair);
  background:rgba(255,255,255,.05); color:var(--ink); font:600 13px/1 inherit; cursor:pointer;
  -webkit-appearance:none; appearance:none; white-space:nowrap; }
.fp-chip:active{ transform:scale(.96); }
.fp-chip.fp-chip-drag{ transform:scale(1.1); box-shadow:0 6px 18px rgba(0,0,0,.5); opacity:.92; z-index:5; transition:transform .16s cubic-bezier(.22,1,.36,1); }
.fp-chip .x{ display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px;
  border-radius:50%; color:var(--ink-mute); font-size:14px; }
.fp-chip .x:active{ color:var(--ink); background:rgba(255,255,255,.12); }
.fp-star{ flex:0 0 auto; display:inline-flex; align-items:center; justify-content:center;
  width:38px; height:38px; margin:-9px -6px -9px 0; font-size:18px; line-height:1;
  color:var(--ink-mute); }
.fp-star.on{ color:#ffd173; }
.fp-star:active{ transform:scale(.88); }
.fp-launch{ display:flex; gap:4px; margin:0 12px 10px; padding:3px;
  background:rgba(255,255,255,.04); border:.5px solid var(--hair-soft); border-radius:11px; flex:0 0 auto; }
.fp-seg{ flex:1 1 0; height:34px; border:0; border-radius:8px; background:transparent;
  color:var(--ink-dim); font:600 13px/1 inherit; letter-spacing:.01em; cursor:pointer;
  -webkit-appearance:none; appearance:none; transition:background .15s var(--ease),color .15s var(--ease); }
.fp-seg.active{ color:var(--ink); background:var(--sel-strong); box-shadow:0 0 0 .5px var(--hair); }
.fp-seg:active{ transform:scale(.97); }
.fp-list{ flex:1 1 auto; overflow-y:auto; -webkit-overflow-scrolling:touch;
  overscroll-behavior:contain; padding:4px 0 6px; touch-action:pan-y; }
.fp-row{ display:flex; align-items:center; gap:12px; width:100%; min-height:var(--row);
  padding:0 16px; background:transparent; border:0; text-align:left; color:var(--ink); cursor:pointer;
  border-bottom:.5px solid var(--hair-soft); -webkit-appearance:none; appearance:none; font:inherit;
  transition:background .12s var(--ease); }
.fp-row:last-child{ border-bottom:0; }
.fp-row:active{ background:var(--sel-strong); }
.fp-ico{ flex:0 0 auto; width:18px; height:18px; display:block; color:var(--ink-dim); }
.fp-row:active .fp-ico{ color:var(--ink); }
.fp-name{ flex:1 1 auto; min-width:0; font-size:16px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.fp-row--up .fp-name{ color:var(--ink-dim); font-family:ui-monospace,Menlo,monospace; }
.fp-state{ padding:22px 16px; text-align:center; font-size:14px; color:var(--ink-mute); }
@-webkit-keyframes fpFade{ from{opacity:0} to{opacity:1} }
@keyframes fpFade{ from{opacity:0} to{opacity:1} }
@-webkit-keyframes fpSlide{ from{transform:translateY(100%)} to{transform:translateY(0)} }
@keyframes fpSlide{ from{transform:translateY(100%)} to{transform:translateY(0)} }
@-webkit-keyframes fpPop{ from{transform:scale(.96);opacity:0} to{transform:scale(1);opacity:1} }
@keyframes fpPop{ from{transform:scale(.96);opacity:0} to{transform:scale(1);opacity:1} }
@media (prefers-reduced-motion:reduce){ .fp-backdrop,.fp-sheet{ -webkit-animation:none; animation:none; } }

/* ===== TGV: mobile tile mode (overview of all windows) =====
   Only classes with the tgv- prefix (not ID-scoped). SOLID backgrounds, no
   backdrop-filter: blur - iOS Safari freezes with a canvas-rendered terminal. */
body.tgv-open #wrap{ visibility:hidden; }  /* grid is open - hide the terminal */
.tgv-grid{ position:fixed; inset:0; z-index:8000; display:none; background:#000;
  overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain; touch-action:pan-y;
  padding:calc(12px + env(safe-area-inset-top)) calc(12px + env(safe-area-inset-right))
          calc(12px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-left)); }
.tgv-grid.tgv-on{ display:block; }
.tgv-cells{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.tgv-tile{ position:relative; display:flex; flex-direction:column; overflow:hidden;
  height:38vh; background:#0c0c0d; border:.5px solid var(--hair); border-radius:16px;
  cursor:pointer; -webkit-user-select:none; user-select:none;
  transition:transform .12s var(--ease),border-color .18s var(--ease),box-shadow .18s var(--ease); }
.tgv-tile:active{ transform:scale(.96); }
.tgv-tile.tgv-active{ border-color:var(--hair-bright);
  box-shadow:0 0 0 .5px rgba(255,255,255,.25),0 0 18px -4px rgba(255,255,255,.35);
  background:#111114; }
/* tile drag: the lifted one sticks to the finger (transition:none - otherwise lag behind the finger),
   the target tile under the finger gets a bright hairline */
.tgv-tile.tgv-drag{ z-index:8050; transition:none; border-color:var(--hair-bright);
  box-shadow:0 16px 36px -10px rgba(0,0,0,.85),0 0 0 .5px rgba(255,255,255,.2); }
.tgv-tile.tgv-drophint{ border-color:rgba(255,255,255,.6);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.35); }
.tgv-title{ flex:0 0 auto; display:flex; align-items:center; gap:6px;
  padding:9px 12px 7px; font-size:13px; font-weight:600;
  color:var(--ink-dim); border-bottom:.5px solid var(--hair-soft); }
.tgv-title>span:first-child{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tgv-tile.tgv-active .tgv-title{ color:var(--ink); }
.tgv-prev{ flex:1 1 auto; margin:0; padding:8px 10px; overflow:hidden; white-space:pre;
  font:9px/1.25 ui-monospace,"SF Mono",Menlo,monospace; color:var(--ink-mute); }
.tgv-close{ position:fixed; z-index:8100;
  top:calc(8px + env(safe-area-inset-top)); right:calc(10px + env(safe-area-inset-right));
  width:38px; height:38px; display:flex; align-items:center; justify-content:center;
  color:var(--ink-dim); background:rgba(20,20,22,.95); border:.5px solid var(--hair);
  border-radius:50%; padding:0; cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),background .18s var(--ease),color .18s var(--ease); }
.tgv-close:active{ transform:scale(.92); color:var(--ink); background:var(--sel); }
/* handle pill: in the empty safe-area zone above the bar buttons, centered */
.tgv-pill{ position:fixed; left:50%; transform:translateX(-50%); z-index:7700;
  top:max(4px, calc(env(safe-area-inset-top, 0px)/2 - 9px));
  width:64px; height:18px; padding:0; background:var(--panel);
  border:.5px solid var(--hair); border-radius:999px; cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),background .18s var(--ease); }
.tgv-pill::before{ content:""; position:absolute; inset:-10px -18px; }  /* wider tap zone */
.tgv-pill::after{ content:""; position:absolute; left:50%; top:50%;
  transform:translate(-50%,-50%); width:28px; height:4px; border-radius:999px;
  background:var(--ink-mute); }
.tgv-pill:active{ transform:translateX(-50%) scale(.94); background:var(--sel); }
/* half-screen panel on top: tiles in a ruler row, horizontal scroll.
   Terminal is NOT hidden - solid background right at the panel. */
.tgv-strip{ position:fixed; left:0; right:0; top:0; z-index:7600; height:50vh;
  display:flex; background:rgba(0,0,0,.9); border-bottom:.5px solid var(--hair-bright);
  border-radius:0 0 20px 20px; overflow:hidden;
  transform:translateY(-103%); transition:transform .28s var(--ease); }
.tgv-strip.tgv-on{ transform:translateY(0); }
.tgv-row{ flex:1 1 auto; display:flex; align-items:stretch; gap:10px;
  overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; touch-action:pan-x;
  padding:calc(10px + env(safe-area-inset-top)) 14px 14px; }
.tgv-row .tgv-tile{ flex:0 0 auto; width:70vw; height:auto; }
/* transparent tap catcher below the panel (the terminal iframe does not bubble clicks) */
.tgv-strip-bg{ position:fixed; inset:0; z-index:7550; display:none; background:transparent; }
.tgv-strip-bg.tgv-on{ display:block; }
/* tile ruler at the bottom (mterm_strip_bottom toggle): slides out from the bottom */
.tgv-strip.tgv-strip-bottom{ top:auto; bottom:0;
  border-top:.5px solid var(--hair-bright); border-bottom:none;
  border-radius:20px 20px 0 0; transform:translateY(103%); }
.tgv-strip.tgv-strip-bottom.tgv-on{ transform:translateY(0); }
.tgv-strip.tgv-strip-bottom .tgv-row{ padding:14px 14px calc(14px + env(safe-area-inset-bottom)); }

/* ===== Collapsing bar buttons (feature 1) ===== */
#barcollapse .bc-chev{ transition:transform .22s var(--ease); }
/* arrow rotation is tied to #barcollapse itself (class on the button), not to #bar -
   otherwise when the arrow moved to the bottom panel (#set-tabsbar) the selector did not match
   and the arrow did not rotate. Now works in any container. */
#barcollapse.bc-collapsed .bc-chev{ transform:rotate(180deg); }  /* ‹ → › */
.bar-actions{ display:flex; align-items:center; gap:8px; }
/* button collapsing works in ANY container (class on the node itself, not on the #bar ancestor) */
.bar-actions.actions-hidden{ display:none; }

/* ===== Bottom tab strip (feature 3, mterm_tabs_bottom toggle) ===== */
.set-tabsbar{ flex:0 0 auto; position:relative; display:flex; align-items:center;
  background:var(--panel);
  -webkit-backdrop-filter:blur(18px) saturate(120%); backdrop-filter:blur(18px) saturate(120%);
  padding:6px 10px; padding-left:calc(10px + env(safe-area-inset-left));
  padding-right:calc(10px + env(safe-area-inset-right));
  border-top:.5px solid var(--hair); min-width:0; }
.set-tabsbar[hidden]{ display:none; }
.set-tabsbar::after{ content:""; position:absolute; left:0; right:0; top:0; height:1px; pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.14) 18%,rgba(255,255,255,.14) 82%,transparent); }

/* ===== Settings panel - iOS Settings (grouped list) =====
   Opens ONLY from the session list screen (DOM, not the live ttyd terminal),
   so backdrop-filter on the overlay is safe (the terminal is not under the panel). */
#mem{ cursor:pointer; border-radius:8px; padding:2px 6px; margin-left:auto;
  transition:background .18s var(--ease),color .18s var(--ease); }
#mem:active{ background:var(--sel); color:var(--ink); }

/* Dimmed + BLURRED backdrop (the blurred session list is visible, not the terminal) */
.set-ovl{ position:fixed; inset:0; z-index:9400; background:rgba(0,0,0,.42);
  opacity:0; transition:opacity .32s var(--ease); }
/* backdrop blur ONLY in mobile 2.0 (settings over the list DOM). In classic mode the panel
   floats over the LIVE ttyd terminal -> blur freezes iOS (README pitfall) -> solid background. */
body.tt-2 .set-ovl{ -webkit-backdrop-filter:blur(20px) saturate(160%); backdrop-filter:blur(20px) saturate(160%); }
body:not(.tt-2) .set-ovl{ background:rgba(0,0,0,.82); }
.set-ovl.set-open{ opacity:1; }

/* Panel - large bottom sheet with rounded top corners (iOS list) */
.set-pop{
  position:fixed; z-index:9500; left:0; right:0; bottom:0;
  top:calc(env(safe-area-inset-top, 0px) + 32px);
  background:#101012;
  border-top:.5px solid var(--hair-bright);
  border-radius:22px 22px 0 0;
  box-shadow:0 -14px 48px -8px rgba(0,0,0,.85);
  display:flex; flex-direction:column; overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",system-ui,sans-serif;
  letter-spacing:-.014em;
  transform:translateY(100%); transition:transform .44s var(--tt-spring); will-change:transform;
}
.set-pop.set-open{ transform:translateY(0); }
/* grab bar at the top of the sheet */
.set-pop::before{ content:""; position:absolute; top:8px; left:50%; transform:translateX(-50%);
  width:38px; height:5px; border-radius:3px; background:rgba(255,255,255,.22); z-index:2; }

.set-head{ display:flex; align-items:center; gap:8px; padding:20px 16px 12px;
  border-bottom:.5px solid var(--hair-soft); }
.set-title{ flex:1 1 auto; font-size:22px; font-weight:700; color:var(--ink); letter-spacing:-.02em; }
.set-x{ flex:0 0 auto; height:34px; min-width:34px; padding:0 14px; display:flex; align-items:center; justify-content:center;
  color:var(--tt-accent); background:transparent; border:0; border-radius:10px;
  font-size:17px; font-weight:600; line-height:1; cursor:pointer; -webkit-appearance:none; appearance:none; }
.set-x:active{ opacity:.5; }

/* scrollable body with grouped cards */
.set-list{ display:flex; flex-direction:column; overflow-y:auto; -webkit-overflow-scrolling:touch;
  padding:8px 16px calc(28px + env(safe-area-inset-bottom,0px)); }

/* ---- iOS section: caption on top + rounded dark card ---- */
.set-sec{ margin-top:22px; }
.set-sec:first-child{ margin-top:8px; }
.set-sechd{ padding:0 16px 7px; font-size:12.5px; font-weight:600; letter-spacing:.03em;
  text-transform:uppercase; color:var(--tt-sub-dim); }
.set-card{ background:#1c1c1e; border-radius:14px; overflow:hidden;
  box-shadow:inset 0 0 0 .5px var(--hair-soft); }
.set-secnote{ padding:7px 16px 0; font-size:12.5px; line-height:1.35; color:var(--tt-sub-dim);
  letter-spacing:-.005em; }

/* ---- row inside a card: label on the left + control on the right + hairline ---- */
.set-row{ position:relative; display:flex; align-items:center; gap:12px; min-height:48px;
  padding:9px 16px; cursor:pointer; -webkit-user-select:none; user-select:none;
  transition:background .12s ease; }
.set-row:active{ background:rgba(255,255,255,.05); }
/* hairline separator between rows (not under the last one) - left inset like in iOS */
.set-row::after{ content:""; position:absolute; left:16px; right:0; bottom:0; height:.5px;
  background:rgba(255,255,255,.09); transform:scaleY(.5); transform-origin:bottom; }
.set-row:last-child::after{ display:none; }
.set-label{ flex:1 1 auto; font-size:16px; color:var(--ink); letter-spacing:-.01em;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.set-sub{ display:block; font-size:12.5px; color:var(--tt-sub-dim); margin-top:2px;
  white-space:normal; letter-spacing:-.005em; }

/* iOS toggle with a sliding knob */
.set-switch{ flex:0 0 auto; width:51px; height:31px; border-radius:999px; position:relative;
  background:rgba(120,120,128,.32); border:0;
  transition:background .28s cubic-bezier(.4,.2,.2,1); }
.set-switch::after{ content:""; position:absolute; top:2px; left:2px; width:27px; height:27px;
  border-radius:50%; background:#fff; box-shadow:0 2px 5px rgba(0,0,0,.35),0 1px 1px rgba(0,0,0,.25);
  transition:transform .28s cubic-bezier(.4,.2,.2,1); }
.set-row.set-on .set-switch{ background:var(--tt-done); }
.set-row.set-on .set-switch::after{ transform:translateX(20px); }

/* ---- transcription engine list: bulb + queue number + arrows ---- */

/* ---- transcription progress in the voice window: who is processing now, who already failed ---- */
.vc-steps{ width:100%; display:flex; flex-direction:column; gap:6px; margin-top:10px; }
.vc-step{ display:grid; grid-template-columns:14px minmax(0,1fr) auto; column-gap:8px; row-gap:1px;
  align-items:baseline; font-size:12.5px; line-height:1.25; color:var(--tt-sub-dim);
  letter-spacing:-.005em; }
.vc-step .vc-si{ text-align:center; font-size:11px; }
.vc-step .vc-sname{ font-weight:600; color:var(--tt-sub); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.vc-step .vc-secs{ font-size:11.5px; color:var(--tt-sub-dim); font-variant-numeric:tabular-nums;
  white-space:nowrap; }
.vc-step .vc-why{ grid-column:2 / -1; font-size:11.5px; opacity:.9; white-space:normal; }
.vc-step.now .vc-sname{ color:var(--ink); }
@keyframes sttPulse{ 0%,100%{ opacity:1; transform:scale(1); } 50%{ opacity:.45; transform:scale(.82); } }
.vc-step.now .vc-si{ color:var(--tt-work); animation:sttPulse 1s ease-in-out infinite; }
.vc-step.ok .vc-si, .vc-step.ok .vc-sname{ color:var(--tt-done); }
.vc-step.bad .vc-sname{ color:var(--tt-sub-dim); text-decoration:line-through; }
.vc-done{ width:100%; margin-top:8px; font-size:12.5px; color:var(--tt-done);
  letter-spacing:-.005em; text-align:center; }

/* picker row: value in secondary color + chevron (leads to a choice) */
.set-val{ flex:0 0 auto; max-width:56%; font-size:16px; color:var(--tt-sub);
  text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; letter-spacing:-.01em; }
.set-chev{ flex:0 0 auto; width:9px; height:15px; margin-left:2px; color:var(--tt-sub-dim);
  opacity:.75; }
.set-chev svg{ width:9px; height:15px; display:block; }

/* ============================================================
   tmux tabs 2.0 — Telegram-iOS chat list (screen 1) + slide.
   All classes/ids prefixed tt-. Ported from _refs/mockup-chatlist.html.
   ============================================================ */
:root{
  --tt-accent:#3a8dff;
  --tt-sub:#8b8b90; --tt-sub-dim:#6a6a70; --tt-time:#79797f;
  --tt-work:#ff9f0a; --tt-wait:#ff453a; --tt-done:#30d158; --tt-idle:#48484d;
  --tt-spring:cubic-bezier(.22,1,.36,1);
  --tt-spring2:cubic-bezier(.34,1.56,.64,1);
  --tt-safe-top:env(safe-area-inset-top,0px);
  --tt-safe-bot:env(safe-area-inset-bottom,0px);
}
/* ===== slide between the two screens ===== */
/* #app is the TERMINAL screen; it slides in from the right over the list.
   Gated on body.tt-2 so that if JS fails to boot, the classic terminal stays. */
body.tt-2 #app{ position:relative; z-index:10; transform:translateX(100%);
  transition:transform .42s var(--tt-spring); box-shadow:-14px 0 40px rgba(0,0,0,.55); will-change:transform; }
body.tt-2.tt-term #app{ transform:translateX(0); }
/* list is hidden entirely unless the two-screen mode booted */
#tt-list-screen{ display:none; }
/* disable iOS text selection and the callout menu (Copy/Look Up/Translate) on the list and action sheet.
   Terminal/composer (#app,#inputbar,#tin) are NOT affected - they are in another container. */
#tt-list-screen, #tt-list-screen *, #tt-sheet, #tt-sheet *, #tt-scrim, .tt-back, .tt-back * {
  -webkit-user-select:none; -moz-user-select:none; user-select:none; -webkit-touch-callout:none; }
body.tt-2 #tt-list-screen{ display:flex;
  transition:transform .42s var(--tt-spring),opacity .42s var(--tt-spring); will-change:transform; }
/* list gets a subtle parallax push while term opens */
body.tt-2.tt-term #tt-list-screen{ transform:translateX(-24%); opacity:.5; pointer-events:none; }

#tt-list-screen{
  position:fixed; inset:0; z-index:5; flex-direction:column;
  background:var(--bg); overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",system-ui,sans-serif;
  letter-spacing:-.014em;
}

/* ===== nav header ===== */
.tt-nav{
  position:relative; z-index:6; flex:0 0 auto;
  padding:calc(6px + max(var(--tt-safe-top),22px)) 16px 8px;
  display:flex; align-items:flex-end; justify-content:space-between;
  background:linear-gradient(to bottom,rgba(0,0,0,.92),rgba(0,0,0,.7));
}
.tt-nav .tt-title{ font-size:26px; font-weight:700; letter-spacing:-.03em; line-height:1.04; }
.tt-nav .tt-title small{ display:block; font-size:12px; font-weight:500; color:var(--tt-work);
  margin-top:3px; letter-spacing:-.006em; }
.tt-nav-actions{ display:flex; gap:10px; }
.tt-ibtn{ width:36px; height:36px; border-radius:50%; background:rgba(120,120,128,.18);
  display:flex; align-items:center; justify-content:center; color:var(--tt-accent);
  transition:transform .18s var(--tt-spring),background .18s ease; -webkit-appearance:none; appearance:none; border:0; padding:0; }
.tt-ibtn:active{ transform:scale(.86); background:rgba(120,120,128,.34); }
.tt-ibtn svg{ width:20px; height:20px; fill:none; stroke:var(--tt-accent); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }

/* ===== chat list ===== */
.tt-list{ flex:1 1 auto; overflow-y:auto; -webkit-overflow-scrolling:touch;
  overscroll-behavior:contain; touch-action:pan-y; padding-bottom:118px; }
.tt-row{ position:relative; display:flex; align-items:center; gap:12px;
  padding:8px 16px 8px 14px; transition:background .14s ease,transform .2s var(--tt-spring); will-change:transform; }
.tt-row.tt-press{ background:#1a1a1d; transform:scale(.985); }
.tt-row::after{ content:""; position:absolute; left:80px; right:0; bottom:0; height:.5px;
  background:rgba(255,255,255,.08); transform:scaleY(.5); transform-origin:bottom; }
.tt-row:last-child::after{ display:none; }

/* pan-y, NOT none: a swipe started on the avatar must scroll the list (with none
   this was a dead zone - confirmed in Chromium). Drag-to-folder after a
   long press kills scrolling itself via preventDefault in touchmove. */
.tt-avawrap{ position:relative; flex:0 0 auto; touch-action:pan-y; will-change:transform; }
.tt-ava{ width:54px; height:54px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  font-size:22px; font-weight:600; color:#fff; background:linear-gradient(145deg,#4a4a52,#2c2c31); letter-spacing:0; }
.tt-ava.g0{ background:linear-gradient(145deg,#6ac6ff,#2f8bff); }
.tt-ava.g1{ background:linear-gradient(145deg,#ff9f6b,#ff7a45); }
.tt-ava.g2{ background:linear-gradient(145deg,#8be08f,#35b84a); }
.tt-ava.g3{ background:linear-gradient(145deg,#c58bff,#8b4bff); }
.tt-ava.g4{ background:linear-gradient(145deg,#ff8bd0,#ff5c9a); }
.tt-ava.g5{ background:linear-gradient(145deg,#ffd66b,#ffab2e); color:#3a2a00; }
.tt-ava.g6{ background:linear-gradient(145deg,#6be0d4,#25b0a3); }
.tt-dot{ position:absolute; right:-1px; bottom:-1px; width:16px; height:16px; border-radius:50%;
  border:2.5px solid var(--bg); background:var(--tt-idle); z-index:2; }
.tt-st-work .tt-dot{ background:var(--tt-work); animation:ttPulseWork 1.6s ease-in-out infinite; }
.tt-st-wait .tt-dot{ background:var(--tt-wait); animation:ttPulseWait 1.05s ease-in-out infinite; }
.tt-st-done .tt-dot{ background:var(--tt-done); }
.tt-st-idle .tt-dot{ background:var(--tt-idle); }
.tt-avawrap::before{ content:""; position:absolute; inset:-3px; border-radius:50%;
  border:2px solid transparent; transition:border-color .3s ease; pointer-events:none; }
.tt-st-work .tt-avawrap::before{ border-color:rgba(255,159,10,.55); animation:ttRingWork 1.6s ease-in-out infinite; }
.tt-st-wait .tt-avawrap::before{ border-color:rgba(255,69,58,.65); animation:ttRingWait 1.05s ease-in-out infinite; }
.tt-st-done .tt-avawrap::before{ border-color:rgba(48,209,88,.45); }
@keyframes ttPulseWork{ 0%,100%{transform:scale(1);opacity:1} 50%{transform:scale(1.18);opacity:.75} }
@keyframes ttPulseWait{ 0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(255,69,58,.55)} 50%{transform:scale(1.16);box-shadow:0 0 0 5px rgba(255,69,58,0)} }
@keyframes ttRingWork{ 0%,100%{opacity:.5} 50%{opacity:.95} }
@keyframes ttRingWait{ 0%,100%{opacity:.55;box-shadow:0 0 0 0 rgba(255,69,58,.35)} 50%{opacity:1;box-shadow:0 0 14px 2px rgba(255,69,58,.35)} }

.tt-body{ flex:1; min-width:0; }
.tt-top{ display:flex; align-items:baseline; gap:8px; }
.tt-name{ font-size:17px; font-weight:600; color:var(--ink); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; flex:1; min-width:0; letter-spacing:-.02em; }
.tt-idx{ font-size:11px; font-weight:600; padding:1px 6px; border-radius:5px; color:var(--tt-sub);
  background:rgba(120,120,128,.2); margin-left:6px; letter-spacing:-.005em; }
/* right label: short status (secondary color) or an 'active' badge */
.tt-rtime{ font-size:13px; color:var(--tt-time); flex:0 0 auto; font-weight:400; letter-spacing:-.01em; }
.tt-rtime.tt-st-wait{ color:var(--tt-wait); font-weight:600; }
.tt-rtime.tt-st-work{ color:var(--tt-work); }
.tt-rtime.tt-st-done{ color:var(--tt-done); }
.tt-rtime.tt-live{ font-size:11px; font-weight:600; color:#cfcfd4; padding:1px 7px;
  border-radius:6px; background:rgba(120,120,128,.24); letter-spacing:-.01em; }
.tt-bottom{ display:flex; align-items:flex-start; gap:8px; margin-top:2px; }
.tt-prev{ flex:1; min-width:0; font-size:14.5px; line-height:1.32; color:var(--tt-sub);
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
  font-family:"SF Mono",ui-monospace,Menlo,monospace; white-space:pre-wrap; word-break:break-word;
  letter-spacing:-.008em; }
/* empty output - muted placeholder, NO strokes/underlines */
.tt-prev-empty{ color:var(--tt-sub-dim); font-style:normal; font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif; }
.tt-badge{ flex:0 0 auto; min-width:22px; height:22px; padding:0 7px; border-radius:11px;
  background:#2a2a2e; color:#cfcfd4; font-size:13px; font-weight:600;
  display:flex; align-items:center; justify-content:center; margin-top:2px; letter-spacing:-.01em; }
.tt-badge.hot{ background:var(--tt-wait); color:#fff; }
.tt-badge.go{ background:var(--tt-done); color:#04270f; }
.tt-badge.run{ background:var(--tt-work); color:#2a1a00; }
.tt-empty{ padding:40px 20px; text-align:center; color:var(--tt-sub-dim); font-size:14px; }

/* ===== List settings (controlled by the settings panel) ===== */
/* 'Show message previews' OFF -> hide the preview row in the chat list */
body.tt-nopreview .tt-bottom{ display:none; }
body.tt-nopreview .tt-top{ margin-top:2px; }
/* 'Compact list' -> denser rows: less padding, smaller avatar, preview on 1 line */
body.tt-compact .tt-row{ padding-top:5px; padding-bottom:5px; }
body.tt-compact .tt-ava{ width:44px; height:44px; font-size:19px; }
body.tt-compact .tt-row::after{ left:70px; }
body.tt-compact .tt-name{ font-size:16px; }
body.tt-compact .tt-prev{ -webkit-line-clamp:1; font-size:13.5px; }

/* ===== bottom: folder pills + fab ===== */
.tt-bottombar{ position:fixed; left:0; right:0; bottom:0; z-index:8;
  padding-bottom:var(--tt-safe-bot);
  background:linear-gradient(to top,rgba(0,0,0,.96) 55%,rgba(0,0,0,0));
  transition:opacity .28s var(--tt-spring),transform .28s var(--tt-spring); }
body.tt-term .tt-bottombar,body.tt-term .tt-fab{ opacity:0; pointer-events:none; transform:translateY(14px); }
.tt-folders{ display:flex; gap:8px; padding:8px 14px calc(10px + var(--tt-safe-bot)); overflow-x:auto;
  -webkit-overflow-scrolling:touch; scrollbar-width:none; touch-action:pan-x; }
.tt-folders::-webkit-scrollbar{ display:none; }
.tt-folder{ flex:0 0 auto; display:flex; align-items:center; gap:6px; height:34px; padding:0 15px;
  border-radius:17px; background:rgba(120,120,128,.16); color:var(--tt-sub); font-size:15px; font-weight:500;
  letter-spacing:-.01em; white-space:nowrap; -webkit-appearance:none; appearance:none; border:0;
  transition:transform .2s var(--tt-spring2),background .22s ease,color .22s ease; }
.tt-folder:active{ transform:scale(.92); }
.tt-folder.tt-fon{ background:var(--tt-accent); color:#fff; font-weight:600; box-shadow:0 4px 14px rgba(58,141,255,.35); }
.tt-folder .tt-cnt{ font-size:12px; font-weight:600; min-width:18px; height:18px; padding:0 5px; border-radius:9px;
  background:rgba(255,255,255,.16); color:inherit; display:flex; align-items:center; justify-content:center; }
.tt-folder.tt-fon .tt-cnt{ background:rgba(255,255,255,.28); }
.tt-folder.tt-fadd{ color:var(--tt-accent); font-weight:600; }
/* drop target under the finger grows noticeably with a spring (iOS style).
   overflow:visible on the container during the drag - otherwise the scroll container (overflow-x:auto)
   clips the enlarged pill and the growth is invisible. High specificity (.tt-dragover ...)
   overrides .tt-folder:active (scale .92), which kicks in when the pill is touched. */
.tt-folder.tt-drop-hot{ background:var(--tt-accent); color:#fff; z-index:3;
  box-shadow:0 12px 28px rgba(58,141,255,.6);
  transition:transform .24s cubic-bezier(.34,1.56,.64,1),background .18s ease,color .18s ease; }
.tt-folders.tt-dragover .tt-folder.tt-drop-hot{ transform:scale(1.35) translateY(-5px) !important; }
.tt-folder.tt-drop-hot .tt-cnt{ background:rgba(255,255,255,.3); }
.tt-folders.tt-dragover{ background:rgba(58,141,255,.05); overflow:visible; }
/* donor avatar during the drag - muted in place */
.tt-row.tt-drag-src .tt-avawrap{ opacity:.28; transition:opacity .2s ease; }
/* 'ghost' - clone of the avatar with the name, flies after the finger */
/* ghost during drag = ONLY the avatar circle (no name), so it does not cover folders below */
.tt-drag-ghost{ position:fixed; left:0; top:0; z-index:9500; pointer-events:none;
  display:flex; align-items:center; justify-content:center; will-change:transform,opacity;
  transform:translate(-9999px,-9999px) scale(1); opacity:0; }
.tt-drag-ghost.tt-lift{ opacity:1; }
.tt-drag-ghost .tt-ava{ width:50px; height:50px; font-size:21px;
  box-shadow:0 10px 26px rgba(0,0,0,.55), 0 0 0 3px rgba(255,255,255,.12); }
/* spring-back into place / fly into a folder - enable smooth transform on transitionend */
.tt-drag-ghost.tt-anim{ transition:transform .34s var(--tt-spring),opacity .3s ease; }
.tt-drag-ghost.tt-anim.tt-fly{ transition:transform .3s cubic-bezier(.5,0,.75,0),opacity .3s ease; }

.tt-fab{ position:fixed; right:16px; bottom:calc(66px + var(--tt-safe-bot)); z-index:9;
  width:56px; height:56px; border-radius:50%; background:linear-gradient(145deg,#4a9cff,#2f7bff);
  display:flex; align-items:center; justify-content:center; box-shadow:0 8px 22px rgba(47,123,255,.5);
  transition:opacity .28s var(--tt-spring),transform .28s var(--tt-spring2); -webkit-appearance:none; appearance:none; border:0; }
.tt-fab:active{ transform:scale(.88) rotate(90deg); }
.tt-fab svg{ width:26px; height:26px; stroke:#fff; stroke-width:2.4; fill:none; stroke-linecap:round; }

/* ===== long-press action sheet ===== */
/* ===== 'Claude chats' screen (phone) ===== */
#ttc-screen{ display:none; position:fixed; inset:0; z-index:9300; flex-direction:column; background:var(--bg);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif; letter-spacing:-.014em; }
body.ttc-open #ttc-screen{ display:flex; }
body:not(.tt-2) #ttc-btn{ display:none; }
.ttc-nav{ flex:0 0 auto; padding:calc(6px + max(var(--tt-safe-top),22px)) 16px 8px; display:flex; flex-direction:column; gap:6px;
  background:linear-gradient(to bottom,rgba(0,0,0,.92),rgba(0,0,0,.7)); }
.ttc-back{ align-self:flex-start; background:none; border:0; padding:4px 0; color:var(--tt-accent); font-size:17px; -webkit-appearance:none; }
.ttc-title{ font-size:26px; font-weight:700; letter-spacing:-.03em; line-height:1.04; }
.ttc-title small{ display:block; font-size:12px; font-weight:500; color:#8e8e93; margin-top:3px; }
.ttc-srow{ flex:0 0 auto; padding:4px 16px 10px; }
.ttc-srow input{ width:100%; box-sizing:border-box; height:36px; border-radius:10px; border:0; padding:0 12px;
  background:rgba(120,120,128,.24); color:inherit; font-size:16px; -webkit-appearance:none; outline:none; }
.ttc-list{ flex:1 1 auto; overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain; padding-bottom:40px; }
.ttc-row{ display:block; width:100%; text-align:left; background:none; border:0; color:inherit; padding:11px 16px;
  border-bottom:.5px solid rgba(255,255,255,.08); font:inherit; -webkit-appearance:none; }
.ttc-row:active,.ttc-row.ttc-busy{ background:#1a1a1d; }
.ttc-r1{ display:flex; gap:8px; align-items:baseline; }
.ttc-name{ flex:1 1 auto; font-size:16px; font-weight:600; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.ttc-time{ flex:0 0 auto; font-size:13px; color:#8e8e93; }
.ttc-sub{ font-size:13px; color:#8e8e93; margin-top:3px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.ttc-more,.ttc-empty{ display:block; margin:14px auto; background:none; border:0; color:var(--tt-accent); font-size:15px; text-align:center; }
.ttc-empty{ color:#8e8e93; }
.ttc-row{ -webkit-touch-callout:none; -webkit-user-select:none; user-select:none; }
.tt-scrim{ position:fixed; inset:0; z-index:9440; background:rgba(0,0,0,.5);
  opacity:0; pointer-events:none; transition:opacity .3s ease; }
.tt-scrim.tt-show{ opacity:1; pointer-events:auto; }
.tt-sheet{ position:fixed; left:8px; right:8px; bottom:calc(8px + var(--tt-safe-bot)); z-index:9441;
  transform:translateY(130%); transition:transform .44s var(--tt-spring); will-change:transform; }
.tt-sheet.tt-show{ transform:translateY(0); }
.tt-sgroup{ background:#1c1c1e; border-radius:16px; overflow:hidden; margin-bottom:8px; }
.tt-shead{ padding:14px 16px 10px; text-align:center; border-bottom:.5px solid rgba(255,255,255,.08); }
.tt-shead .tt-hn{ font-size:15px; font-weight:600; letter-spacing:-.015em; }
.tt-shead .tt-hs{ font-size:12px; color:var(--tt-sub-dim); margin-top:2px; }
.tt-sitem{ padding:15px 18px; font-size:18px; letter-spacing:-.022em; display:flex; align-items:center;
  justify-content:space-between; border-bottom:.5px solid rgba(255,255,255,.08);
  transition:background .12s ease; color:#fff; -webkit-appearance:none; appearance:none; background:transparent; border-left:0; border-right:0; border-top:0; width:100%; text-align:left; }
.tt-sitem:last-child{ border-bottom:none; }
.tt-sitem:active{ background:#2c2c2e; }
.tt-sitem.tt-danger{ color:var(--tt-wait); }
.tt-sitem svg{ width:20px; height:20px; stroke:currentColor; fill:none; stroke-width:1.8; opacity:.9; }
.tt-scancel{ background:#1c1c1e; border-radius:16px; padding:16px; text-align:center; font-size:18px;
  font-weight:600; color:var(--tt-accent); letter-spacing:-.022em; transition:background .12s ease;
  -webkit-appearance:none; appearance:none; border:0; width:100%; }
.tt-scancel:active{ background:#2c2c2e; }
/* folder-choice sublist inside the sheet */
.tt-fchoice{ max-height:46vh; overflow-y:auto; -webkit-overflow-scrolling:touch; touch-action:pan-y; }
.tt-fchoice .tt-sitem .tt-chk{ color:var(--tt-accent); font-weight:700; }

/* ===== terminal-screen back nav (thin bar prepended into #bar area) ===== */
/* 'back to list' button - at the BOTTOM, first item of the composer row (easy for a thumb),
   shown only in terminal mode (body.tt-term). */
.tt-back{ flex:0 0 auto; display:none; align-items:center; justify-content:center; color:var(--tt-accent);
  padding:4px 4px 4px 2px; transition:transform .16s var(--tt-spring),opacity .16s ease;
  -webkit-appearance:none; appearance:none; background:transparent; border:0; }
body.tt-term .tt-back{ display:inline-flex; }
.tt-back:active{ transform:scale(.9); opacity:.6; }
.tt-back svg{ width:24px; height:24px; stroke:var(--tt-accent); stroke-width:2.4; fill:none; stroke-linecap:round; stroke-linejoin:round; }
.tt-termname{ flex:0 1 auto; min-width:0; display:flex; align-items:center; gap:6px; overflow:hidden; }
.tt-termname .tt-tn{ font-size:15px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; letter-spacing:-.02em; color:var(--ink); cursor:text; -webkit-user-select:none; user-select:none; }
.tt-termname .tt-tn:active{ opacity:.6; }
/* inline editing of the session name (contenteditable) */
.tt-termname .tt-tn.tt-edit{ -webkit-user-select:text; user-select:text; cursor:text; outline:none; border-bottom:1.5px solid var(--tt-work,#0a84ff); text-overflow:clip; overflow:visible; min-width:2ch; }
/* auto-title */
@keyframes ttAiPulse{ 0%,100%{ opacity:.35; transform:scale(.9);} 50%{ opacity:1; transform:scale(1.08);} }
.tt-termname .tt-td{ flex:0 0 auto; width:8px; height:8px; border-radius:50%; background:var(--tt-idle); }
.tt-termname.tt-st-work .tt-td{ background:var(--tt-work); }
.tt-termname.tt-st-wait .tt-td{ background:var(--tt-wait); animation:ttPulseWait 1.05s ease-in-out infinite; }
.tt-termname.tt-st-done .tt-td{ background:var(--tt-done); }
/* hide the old horizontal tab strip; kept in DOM so its drag/reorder JS stays intact */
body.tt-2 #tabs{ display:none; }
/* back button + term-name belong to the two-screen mode only */
.tt-back,.tt-termname{ display:none; }
body.tt-2 .tt-back{ display:flex; }
body.tt-2 .tt-termname{ display:flex; }

/* select (default agent) - native select, pushed to the right, secondary color */
.set-select{ flex:0 0 auto; max-width:56%; height:34px; padding:0 4px 0 8px; border:0;
  background:transparent; color:var(--tt-sub); font:400 16px/1 inherit; letter-spacing:-.01em;
  -webkit-appearance:none; appearance:none; text-align:right; text-align-last:right; }
/* folder picker row: value in a monospace font (path) + chevron */
.set-selrow .set-cwd{ flex:0 0 auto; max-width:60%; font:400 15px/1.2 ui-monospace,Menlo,monospace; color:var(--tt-sub);
  text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  background:transparent; border:0; padding:0; -webkit-appearance:none; appearance:none; letter-spacing:-.02em; }
.set-selrow .set-cwd:active{ opacity:.6; }
/* ============================================================
   Interactive interface guide (prefix gd-).
   Backdrop 9800, card 9810 - above all overlays, below the toast (9999).
   IMPORTANT: the body:has(...) #wrap{visibility:hidden} rule is NOT copied here -
   the guide must show the real elements beneath it.
   ============================================================ */
#gd-ovl{ position:fixed; inset:0; z-index:9800; background:transparent;
  -webkit-tap-highlight-color:transparent; }
#gd-ovl.gd-hidden,#gd-card.gd-hidden{ display:none; }
/* 'Spotlight' as ONE element: frame around the target + a huge solid shadow
   that acts as the dimming. SOLID background, NO backdrop-filter: blur over
   a live terminal canvas freezes iOS Safari (see .fp-backdrop above). */
.gd-ring{
  position:fixed; left:0; top:0; width:0; height:0; pointer-events:none;
  border:2px solid var(--tt-accent); border-radius:12px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.72);
  -webkit-animation:fpFade .22s var(--ease) both; animation:fpFade .22s var(--ease) both;
  transition:left .24s var(--ease),top .24s var(--ease),width .24s var(--ease),height .24s var(--ease);
}
#gd-card{
  position:fixed; left:0; top:0; z-index:9810; box-sizing:border-box;
  width:min(340px,calc(100vw - 24px)); padding:14px 14px 12px;
  font:16px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  color:var(--ink); background:#1c1c1e;
  border:.5px solid var(--hair); border-radius:16px;
  box-shadow:0 24px 64px rgba(0,0,0,.72);
  -webkit-tap-highlight-color:transparent;
  -webkit-animation:fpPop .24s var(--ease) both; animation:fpPop .24s var(--ease) both;
}
.gd-title{ font-size:16px; font-weight:700; letter-spacing:-.01em; color:var(--ink);
  padding-right:30px; }
.gd-text{ margin-top:7px; font-size:14px; line-height:1.42; color:var(--tt-sub); }
.gd-close{ position:absolute; right:6px; top:6px; width:32px; height:32px;
  display:flex; align-items:center; justify-content:center; font-size:14px; line-height:1;
  color:var(--ink-dim); background:transparent; border:0; padding:0; border-radius:50%;
  cursor:pointer; -webkit-appearance:none; appearance:none; }
.gd-close:active{ color:var(--ink); background:var(--sel); }
.gd-foot{ display:flex; align-items:center; gap:8px; margin-top:13px; }
.gd-dots{ flex:1 1 auto; display:flex; align-items:center; gap:4px; flex-wrap:wrap; min-width:0; }
.gd-dot{ flex:0 0 auto; width:6px; height:6px; border-radius:50%; background:var(--ink-mute); }
.gd-dot.gd-on{ background:var(--tt-accent); }
.gd-btn{ flex:0 0 auto; height:34px; padding:0 14px; font-size:14px; font-weight:600;
  font-family:inherit; border-radius:10px; cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),background .18s var(--ease); }
.gd-btn:active{ transform:scale(.96); }
.gd-back{ color:var(--ink-dim); background:transparent; border:.5px solid var(--hair); }
.gd-next{ color:#fff; background:var(--tt-accent); border:.5px solid var(--tt-accent); }
.gd-skip{ margin-top:9px; text-align:center; font-size:13px; color:var(--tt-sub-dim);
  cursor:pointer; -webkit-user-select:none; user-select:none; }
.gd-skip:active{ color:var(--ink-dim); }
/* ============================================================
   SIDEBAR - side panel with a file tree 'like in VS Code'.
   DESKTOP only: shown gated by the body.sb-avail class, which
   JS sets via ttIsClassic(). All classes/variables use the sb- prefix.
   Palette/font/icons are shared with the rest of the shell (see :root above).
   ============================================================ */
:root{
  --sb-w:260px;        /* current panel width (JS sets it on documentElement) */
  --sb-rh:22px;        /* tree row height - like VS Code */
  --sb-ind:13px;       /* indent per nesting level */
  --sb-guide:rgba(255,255,255,.09);        /* vertical level guides */
  --sb-guide-hi:rgba(255,255,255,.17);
}

/* --- buttons in the top bar: visible ONLY in classic (desktop) mode --- */
#sidebtn,#undobtn{ display:none; }
body.sb-avail #sidebtn,body.sb-avail #undobtn{ display:flex; }
body.sb-on #sidebtn{ color:var(--ink); background:var(--sel); border-color:var(--hair-bright); }
#sidebtn .sb-btn-fill{ opacity:0; transition:opacity .18s var(--ease); }
body.sb-on #sidebtn .sb-btn-fill{ opacity:.38; }
#undobtn:disabled{ opacity:.32; cursor:default; }
#undobtn:disabled:active{ transform:none; background:rgba(255,255,255,.04); color:var(--ink-dim); }

/* --- horizontal row 'sidebar | resizer | terminal' ---
   CRITICAL: the sidebar SHRINKS #wrap (flex row), not overlays the iframe -
   otherwise xterm will not recalculate its size and the terminal breaks. */
#sb-row{ flex:1 1 auto; display:flex; align-items:stretch; min-width:0; min-height:0; position:relative; }
#sb-row > #wrap{ min-width:0; }

#sidebar{
  flex:0 0 auto; position:relative; width:0; min-width:0; overflow:hidden;
  background:linear-gradient(var(--panel),var(--panel)) #000;
  border-right:0 solid var(--hair);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  letter-spacing:-.014em;
  transition:width .22s var(--ease);
  -webkit-user-select:none; user-select:none;
}
body.sb-on #sidebar{ width:var(--sb-w); border-right-width:.5px; }
body.sb-resizing #sidebar{ transition:none; }
/* inner fixed-width wrapper: while the panel collapses,
   the content is NOT reflowed (otherwise the tree jitters) */
.sb-inner{ width:var(--sb-w); height:100%; display:flex; flex-direction:column; min-height:0; }

/* --- width resizer (drag) --- */
#sb-resizer{ display:none; flex:0 0 auto; width:4px; position:relative; z-index:6; cursor:col-resize; }
body.sb-on #sb-resizer{ display:block; }
#sb-resizer::after{ content:""; position:absolute; top:0; bottom:0; left:1px; width:2px;
  background:transparent; transition:background .15s var(--ease); }
#sb-resizer:hover::after,body.sb-resizing #sb-resizer::after{ background:var(--hair-bright); }
/* during any drag the iframe must not swallow pointer events */
body.sb-resizing #term,body.sb-dragging #term{ pointer-events:none; }
body.sb-resizing,body.sb-dragging{ cursor:col-resize; }
body.sb-dragging{ cursor:grabbing; }

/* --- TOP: place for the chats panel (written separately) --- */
.sb-top{ flex:0 0 auto; height:40%; min-height:0; position:relative; display:flex; overflow:hidden; }
#sb-chats{ flex:1 1 auto; min-width:0; min-height:0; overflow:auto; }
/* placeholder hint while the chats container is empty. Disappears once content appears. */
.sb-top:has(> #sb-chats:empty)::after{
  content:"Chats panel"; position:absolute; inset:0; display:flex;
  align-items:center; justify-content:center; pointer-events:none;
  font-size:11.5px; letter-spacing:.02em; color:var(--ink-mute); opacity:.5;
}

/* --- 'chats / files' divider (vertical drag) --- */
.sb-hsplit{ flex:0 0 auto; height:7px; position:relative; cursor:row-resize; }
.sb-hsplit::before{ content:""; position:absolute; left:0; right:0; top:3px; height:.5px;
  background:var(--hair); transition:background .15s var(--ease); }
.sb-hsplit::after{ content:""; position:absolute; left:50%; top:50%; width:22px; height:2px;
  transform:translate(-50%,-50%); border-radius:2px; background:var(--hair-soft); }
.sb-hsplit:hover::before,body.sb-vresizing .sb-hsplit::before{ background:var(--hair-bright); }

/* --- BOTTOM: files section --- */
.sb-files{ flex:1 1 auto; min-height:0; display:flex; flex-direction:column; }
.sb-head{ flex:0 0 auto; display:flex; align-items:center; gap:1px; height:28px; padding:0 4px 0 10px; }
.sb-cap{ flex:1 1 auto; min-width:0; font-size:11px; font-weight:700; letter-spacing:.05em;
  text-transform:uppercase; color:var(--ink-mute); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sb-tbtn{ flex:0 0 auto; width:24px; height:24px; display:flex; align-items:center; justify-content:center;
  padding:0; border:0; border-radius:6px; background:transparent; color:var(--ink-mute); cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:background .14s var(--ease),color .14s var(--ease),transform .12s var(--ease); }
.sb-tbtn:hover{ color:var(--ink); background:var(--sel); }
.sb-tbtn:active{ transform:scale(.92); background:var(--sel-strong); }
.sb-tbtn.sb-tbtn-on{ color:var(--ink); background:var(--sel); }

/* --- breadcrumbs of the current root --- */
.sb-crumbs{ flex:0 0 auto; display:flex; align-items:center; gap:0; padding:0 8px 5px 8px;
  overflow-x:auto; white-space:nowrap; font-size:11.5px; }
.sb-crumb{ flex:0 0 auto; padding:2px 4px; border:0; border-radius:5px; background:transparent;
  color:var(--ink-mute); font:inherit; cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:background .14s var(--ease),color .14s var(--ease); }
.sb-crumb:hover{ color:var(--ink); background:var(--sel); }
.sb-crumb.sb-crumb-last{ color:var(--ink-dim); font-weight:600; }
.sb-crumb-sep{ flex:0 0 auto; color:var(--ink-mute); opacity:.45; }

/* --- tree --- */
.sb-tree{ flex:1 1 auto; min-height:0; overflow-y:auto; overflow-x:hidden;
  padding:1px 0 14px; outline:none; overscroll-behavior:contain; }
.sb-row{
  position:relative; display:flex; align-items:center; gap:5px;
  height:var(--sb-rh); padding-right:8px;
  padding-left:calc(4px + var(--d,0) * var(--sb-ind));
  font-size:13px; line-height:1; color:var(--ink-dim); white-space:nowrap; cursor:pointer;
  transition:background .1s var(--ease),color .1s var(--ease);
}
/* indent guides: one vertical line per level passed.
   Drawn with a repeating gradient - cheap and adds no DOM. */
.sb-row::before{
  content:""; position:absolute; left:4px; top:0; bottom:0; pointer-events:none;
  width:calc(var(--d,0) * var(--sb-ind));
  background-image:repeating-linear-gradient(to right,
    transparent 0 6px, var(--sb-guide) 6px 7px, transparent 7px var(--sb-ind));
}
.sb-tree:hover .sb-row::before{ background-image:repeating-linear-gradient(to right,
    transparent 0 6px, var(--sb-guide-hi) 6px 7px, transparent 7px var(--sb-ind)); }
.sb-row:hover{ background:var(--sel); color:var(--ink); }
.sb-row.sb-sel{ background:var(--sel-strong); color:var(--ink); }
.sb-tree:focus .sb-row.sb-sel{ box-shadow:inset 0 0 0 1px var(--hair-bright); }
.sb-row.sb-hid{ opacity:.45; }
.sb-row.sb-link .sb-name{ font-style:italic; }

.sb-chev{ flex:0 0 auto; width:14px; height:14px; display:flex; align-items:center; justify-content:center;
  color:var(--ink-mute); transition:transform .16s var(--ease); }
.sb-row.sb-open > .sb-chev{ transform:rotate(90deg); }
.sb-chev.sb-nochev{ visibility:hidden; }
.sb-chev svg{ width:13px; height:13px; display:block; }
.sb-ico{ flex:0 0 auto; width:15px; height:15px; display:block; color:var(--ink-mute); }
.sb-row[data-t="dir"] .sb-ico{ color:var(--ink-dim); }
.sb-row[data-t="dir"].sb-open .sb-ico{ color:var(--ink); }
.sb-name{ flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; }

/* color accents for extensions - NO icon fonts, only SVG currentColor */
.sb-row[data-x="py"]   .sb-ico{ color:#7fb3e0; }
.sb-row[data-x="js"]   .sb-ico{ color:#e6c95c; }
.sb-row[data-x="ts"]   .sb-ico{ color:#5aa9e6; }
.sb-row[data-x="json"] .sb-ico{ color:#d6a15c; }
.sb-row[data-x="md"]   .sb-ico{ color:#6fa8dc; }
.sb-row[data-x="sh"]   .sb-ico{ color:#79c46a; }
.sb-row[data-x="txt"]  .sb-ico{ color:#b9b9c0; }
.sb-row[data-x="img"]  .sb-ico{ color:#c58bff; }
.sb-row[data-x="html"] .sb-ico{ color:#e8845c; }
.sb-row[data-x="css"]  .sb-ico{ color:#6fc3e0; }
.sb-row[data-x="cfg"]  .sb-ico{ color:#c0b0e0; }
.sb-row[data-x="log"]  .sb-ico{ color:#8a8a90; }

/* status rows: loading / empty / error / truncated */
.sb-note{ display:flex; align-items:center; gap:6px; height:var(--sb-rh);
  padding-left:calc(23px + var(--d,0) * var(--sb-ind)); padding-right:8px;
  font-size:11.5px; color:var(--ink-mute); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sb-note.sb-err{ color:#ff8f93; font-style:normal; }
.sb-note{ font-style:italic; }

/* root-level state (error): heading + reason + exit buttons */
.sb-state{ display:flex; flex-direction:column; align-items:flex-start; gap:4px; margin:10px 12px; }
.sb-state-t{ font-size:12.5px; font-weight:600; color:var(--ink-dim); }
.sb-state-m{ font-size:11.5px; line-height:1.4; color:var(--ink-mute); word-break:break-word; }
.sb-state-r{ display:flex; gap:6px; margin-top:5px; }
.sb-state-btn{ height:24px; padding:0 10px; font:600 11.5px/1 inherit; color:var(--ink-dim);
  background:rgba(255,255,255,.05); border:.5px solid var(--hair); border-radius:7px; cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:background .14s var(--ease),color .14s var(--ease); }
.sb-state-btn:hover{ color:var(--ink); background:var(--sel); }
.sb-spin{ flex:0 0 auto; width:11px; height:11px; border-radius:50%;
  border:1.5px solid var(--hair); border-top-color:var(--ink-dim);
  -webkit-animation:sbSpin .7s linear infinite; animation:sbSpin .7s linear infinite; }
@-webkit-keyframes sbSpin{ to{ -webkit-transform:rotate(360deg); } }
@keyframes sbSpin{ to{ transform:rotate(360deg); } }

/* inline rename */
.sb-rn{ flex:1 1 auto; min-width:0; height:calc(var(--sb-rh) - 3px); padding:0 4px;
  font:inherit; font-size:12.5px; color:var(--ink); background:var(--field);
  border:1px solid var(--tt-accent); border-radius:4px; outline:none;
  -webkit-appearance:none; appearance:none; }

/* --- drag-and-drop --- */
.sb-row.sb-drag-src{ opacity:.4; }
.sb-row.sb-drop{ background:var(--sel-strong); color:var(--ink);
  box-shadow:inset 0 0 0 1px var(--hair-bright); }
.sb-tree.sb-drop-root{ box-shadow:inset 0 0 0 1px var(--hair-bright); border-radius:6px; }
.sb-ghost{
  position:fixed; left:0; top:0; z-index:9700; pointer-events:none;
  display:flex; align-items:center; gap:6px; height:24px; max-width:280px; padding:0 10px 0 8px;
  font:13px/1 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  letter-spacing:-.014em; color:var(--ink);
  background:rgba(24,24,26,.96); border:.5px solid var(--hair-bright); border-radius:8px;
  box-shadow:0 12px 30px -8px rgba(0,0,0,.85);
  white-space:nowrap; overflow:hidden;
}
.sb-ghost.sb-anim{ transition:transform .26s var(--tt-spring),opacity .26s var(--ease); }
.sb-ghost.sb-bad{ border-color:rgba(255,107,107,.65); color:#ff8f93; }
.sb-ghost .sb-ico{ opacity:.85; }
.sb-ghost-why{ font-size:11px; color:#ff8f93; }

/* --- context menu --- */
.sb-movl{ position:fixed; inset:0; z-index:9490; background:transparent; }
.sb-menu{ position:fixed; z-index:9500; display:flex; flex-direction:column; min-width:190px;
  padding:5px; gap:1px; border-radius:12px; border:.5px solid var(--hair);
  background:rgba(20,20,22,.96); box-shadow:0 12px 40px -8px rgba(0,0,0,.7); }
.sb-menu.fp-hidden,.sb-movl.fp-hidden{ display:none; }
.sb-mi{ display:flex; align-items:center; gap:8px; height:32px; padding:0 10px;
  text-align:left; border:0; border-radius:8px; background:transparent; color:var(--ink);
  font:500 13.5px/1 inherit; cursor:pointer; -webkit-appearance:none; appearance:none;
  transition:background .12s var(--ease); }
.sb-mi:hover{ background:var(--sel); }
.sb-mi:active{ background:var(--sel-strong); }
.sb-mi.sb-mi-danger{ color:#ff8f93; }
.sb-mi.sb-mi-danger:hover{ background:rgba(255,107,107,.14); }
.sb-mi .sb-ico{ width:14px; height:14px; color:currentColor; opacity:.8; }
.sb-mi-key{ margin-left:auto; padding-left:14px; font-size:11px; color:var(--ink-mute); }
.sb-msep{ height:.5px; margin:4px 6px; background:var(--hair-soft); }
/* --- 'Open in terminal' submenu: agent picker, accordion inside the menu --- */
.sb-mterm{ display:flex; flex-direction:column; }
.sb-mterm > .sb-mi{ width:100%; }
.sb-mi-chev{ flex:0 0 auto; margin-left:2px; width:12px; height:12px; color:var(--ink-mute);
  transition:transform .16s var(--ease); }
.sb-mi-chev .sb-chevsvg,.sb-mi-chev svg{ display:block; width:11px; height:11px; }
.sb-mi.sb-mi-open .sb-mi-chev{ transform:rotate(90deg); }
.sb-msub{ display:flex; flex-direction:column; gap:1px; margin:1px 0 3px 15px;
  padding-left:5px; border-left:1.5px solid var(--hair-soft); }
.sb-msub[hidden]{ display:none; }
.sb-mi-agent{ height:28px; font-size:12.5px; font-weight:500; color:var(--ink-dim); }
.sb-mi-agent:hover{ color:var(--ink); }
.sb-mi-dot{ flex:0 0 auto; width:5px; height:5px; border-radius:50%;
  background:transparent; box-shadow:inset 0 0 0 1px var(--hair); }
.sb-mi-agent.sb-mi-cur{ color:var(--ink); font-weight:600; }
.sb-mi-agent.sb-mi-cur .sb-mi-dot{ background:var(--tt-accent); box-shadow:0 0 6px -1px var(--tt-accent); }

/* --- delete confirmation: .fp-sheet primitive, own container (centered) --- */
#sb-confirm{ position:fixed; inset:0; z-index:9550; display:flex; align-items:center; justify-content:center;
  font:16px/1.3 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; color:var(--ink); }
#sb-confirm.fp-hidden{ display:none; }
#sb-confirm .fp-sheet{ max-width:420px; }
.sb-cf-body{ padding:16px 18px 4px; }
.sb-cf-name{ display:flex; align-items:center; gap:8px; font-size:15px; font-weight:600; color:var(--ink);
  word-break:break-all; }
.sb-cf-name .sb-ico{ width:16px; height:16px; }
.sb-cf-path{ margin-top:4px; font:11.5px/1.4 ui-monospace,"SF Mono",Menlo,monospace;
  color:var(--ink-mute); word-break:break-all; }
.sb-cf-note{ margin-top:10px; font-size:13.5px; line-height:1.45; color:var(--ink-dim); }
.sb-cf-warn{ margin-top:8px; padding:8px 10px; border-radius:10px; font-size:13px; line-height:1.4;
  color:#ffb4a0; background:rgba(255,107,107,.09); border:.5px solid rgba(255,107,107,.28); }
.sb-cf-trash{ margin-top:10px; font-size:12.5px; line-height:1.45; color:var(--ink-mute); }
.sb-cf-actions{ display:flex; gap:10px; padding:14px 18px 18px; }
.sb-cf-btn{ flex:1 1 auto; min-height:42px; padding:0 12px;
  display:flex; align-items:center; justify-content:center;
  border-radius:10px; border:.5px solid var(--hair); background:rgba(255,255,255,.05);
  color:var(--ink); font:600 15px/1 inherit; cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:transform .12s var(--ease),background .12s var(--ease),box-shadow .15s var(--ease); }
.sb-cf-btn:hover{ background:var(--sel); }
.sb-cf-btn:active{ transform:scale(.97); }
.sb-cf-btn:focus-visible{ outline:none; box-shadow:0 0 0 3px var(--glow); }
.sb-cf-cancel{ flex:1.25 1 auto; }
.sb-cf-danger{ color:#ff6b6b; border-color:rgba(255,107,107,.55); background:rgba(255,107,107,.08); }
.sb-cf-danger:hover{ background:rgba(255,107,107,.16); }

@media (prefers-reduced-motion:reduce){
  #sidebar,.sb-chev,.sb-ghost{ transition:none; }
  .sb-spin{ -webkit-animation:none; animation:none; }
}

/* ============================================================

      CLAUDE CODE CHATS PANEL - contents of the #sb-chats container.
      DESKTOP only (panel is built only if ttIsClassic()).
      All classes use the ch- prefix (to avoid clashing with sb-/fp-/tt-).
      Visual language is the same as the .tt-* session list (Telegram):
      row = name + time on the right, muted path on the second line.
      Palette: existing variables (--ink/--hair/--sel/--tt-*).
   ============================================================ */

/* The sidebar author creates the container; only safe defaults are set here,
      so the panel works even if #sb-chats is just an empty <div>.
      The sidebar rules come LATER in the file and win at equal
      specificity. */
#sb-chats{ display:flex; flex-direction:column; min-width:0; min-height:0; }
#sb-chats[hidden]{ display:none; }

.ch-panel{
  display:flex; flex-direction:column; min-width:0; min-height:0; max-height:100%;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  letter-spacing:-.01em; color:var(--ink);
  border-bottom:.5px solid var(--hair-soft);
}
.ch-panel[hidden]{ display:none; }

/* ===== header: 'Chats' + counter + refresh =====
      Same spec as the explorer .sb-head/.sb-cap: two panels in one
      sidebar should read as one system. */
.ch-head{ flex:0 0 auto; display:flex; align-items:center; gap:6px; height:28px; padding:0 4px 0 10px; }
.ch-h-title{ font-size:11px; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
  color:var(--ink-mute); white-space:nowrap; }
.ch-count{ font-size:10.5px; font-weight:500; color:var(--ink-mute);
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1; white-space:nowrap; }
.ch-sp{ flex:1 1 auto; min-width:0; }
.ch-ibtn{
  flex:0 0 auto; width:24px; height:24px; display:flex; align-items:center; justify-content:center;
  padding:0; color:var(--ink-mute); background:transparent; border:0; border-radius:6px; cursor:pointer;
  -webkit-appearance:none; appearance:none;
  transition:color .16s var(--ease),background .16s var(--ease),transform .12s var(--ease);
}
.ch-ibtn:hover{ color:var(--ink); background:var(--sel); }
.ch-ibtn:active{ transform:scale(.92); }
.ch-ibtn svg{ display:block; width:15px; height:15px; }
.ch-ibtn.ch-spin svg{ -webkit-animation:mtspin .9s linear infinite; animation:mtspin .9s linear infinite; }

/* ===== search ===== */
.ch-searchrow{ flex:0 0 auto; position:relative; display:flex; align-items:center; margin:0 8px 6px; }
.ch-sicon{ position:absolute; left:8px; top:50%; margin-top:-6.5px; width:13px; height:13px;
  color:var(--ink-mute); pointer-events:none; }
.ch-sicon svg{ display:block; width:13px; height:13px; }
.ch-search{
  width:100%; min-width:0; height:26px; padding:0 24px 0 26px;
  font:400 12.5px/1 inherit; color:var(--ink); background:var(--field);
  border:.5px solid var(--hair); border-radius:7px; outline:none;
  -webkit-appearance:none; appearance:none;
  transition:border-color .15s var(--ease),box-shadow .15s var(--ease);
}
.ch-search::placeholder{ color:var(--ink-mute); }
.ch-search:focus{ border-color:var(--hair-bright); box-shadow:0 0 0 3px var(--glow); }
.ch-sclear{
  position:absolute; right:3px; top:50%; margin-top:-10px; width:20px; height:20px;
  display:none; align-items:center; justify-content:center; padding:0;
  font-size:11px; line-height:1; color:var(--ink-mute);
  background:transparent; border:0; border-radius:50%; cursor:pointer;
  -webkit-appearance:none; appearance:none;
}
.ch-searchrow.ch-has .ch-sclear{ display:flex; }
.ch-sclear:hover{ color:var(--ink); background:var(--sel); }

/* ===== list ===== */
/* max-height is a safeguard in case the sidebar gives the panel no height via
      flex: without it hundreds of rows would stretch the sidebar. If the parent is a flex column
      and provides height, flex:1 works. */
.ch-list{
  position:relative; flex:1 1 auto; min-height:110px; max-height:100%;
  overflow-y:auto; overflow-x:hidden; overscroll-behavior:contain; padding-bottom:4px;
  scrollbar-width:thin; scrollbar-color:rgba(255,255,255,.18) transparent;
}
/* scrollbars are hidden globally (::-webkit-scrollbar{width:0}); here
      a thin one is restored: on desktop with hundreds of chats it serves as a landmark */
.ch-list::-webkit-scrollbar{ display:block; width:8px; height:8px; }
.ch-list::-webkit-scrollbar-track{ background:transparent; }
.ch-list::-webkit-scrollbar-thumb{ background:rgba(255,255,255,.13); border-radius:4px;
  border:2px solid transparent; background-clip:padding-box; }
.ch-list:hover::-webkit-scrollbar-thumb{ background:rgba(255,255,255,.22); background-clip:padding-box; }

/* ===== project section header (sticky) =====
      Shows what DISTINGUISHES chats - the project folder. The common prefix (~/)
      is not repeated in rows: it lives only here. */
.ch-ghead{
  position:sticky; top:0; z-index:2; display:flex; align-items:center; gap:3px;
  width:100%; height:23px; padding:0 10px 0 5px; text-align:left; cursor:pointer;
  color:var(--tt-sub); background:var(--bg); border:0; border-bottom:.5px solid var(--hair-soft);
  font:600 10.5px/1 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  letter-spacing:.015em;
  -webkit-appearance:none; appearance:none;
  transition:color .14s var(--ease),background .14s var(--ease);
}
.ch-ghead:hover{ color:var(--ink-dim); background:#0a0a0b; }
.ch-chev{ flex:0 0 auto; width:12px; height:12px; color:var(--ink-mute);
  transition:transform .18s var(--ease); }
.ch-chev svg{ display:block; width:11px; height:11px; }
.ch-ghead.ch-gcol .ch-chev{ transform:rotate(-90deg); }
.ch-gname{ flex:0 1 auto; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ch-gcnt{ flex:0 0 auto; margin-left:5px; font:500 10px/1 inherit; color:var(--tt-sub-dim);
  font-variant-numeric:tabular-nums; }
.ch-gsp{ flex:1 1 auto; min-width:6px; }
/* section last-activity date - on the right, like the time on a chat row */
.ch-gtime{ flex:0 0 auto; font:400 10px/1 inherit; color:var(--tt-time); white-space:nowrap;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1; }

/* ===== time divider inside an expanded section ('Today', 'Yesterday'...) ===== */
.ch-dsep{
  padding:7px 10px 3px; font:600 9.5px/1 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  letter-spacing:.06em; text-transform:uppercase; color:var(--ink-mute);
  -webkit-user-select:none; user-select:none;
}
/* ===== chat row = separate card 'pill' =====
      Each chat is its own rounded rectangle: background slightly lighter than the base,
      thin border, 3px gap between cards - chats do not merge into a sheet.
      Density first: one 'name + time' line, no path (it is the same
      for the whole section) and no message counter (it is in the tooltip). A long name
      wraps to a second line (clamp 2) instead of being cut off. */
.ch-row{
  position:relative; display:block; margin:0 6px 3px; padding:5px 8px 6px 9px;
  background:rgba(255,255,255,.045); border:.5px solid rgba(255,255,255,.055);
  border-radius:9px;
  cursor:pointer; -webkit-user-select:none; user-select:none;
  transition:background .12s var(--ease),border-color .12s var(--ease);
}
/* first card after a sticky section header / date divider - breathing room */
.ch-ghead + .ch-row{ margin-top:4px; }
.ch-dsep + .ch-row{ margin-top:1px; }
.ch-row:hover{ background:var(--sel); border-color:var(--hair); }
.ch-row:focus-visible{ outline:none; background:var(--sel); box-shadow:inset 0 0 0 1px var(--hair-bright); }
/* active / last opened card: stronger background + accent stripe
      on the left (inset shadow is clipped by the rounding itself) + bluish border */
.ch-row.ch-on{ background:var(--sel-strong); border-color:rgba(58,141,255,.45);
  box-shadow:inset 2px 0 0 var(--tt-accent); }
.ch-row.ch-on .ch-name{ color:var(--ink); }

.ch-top{ display:flex; align-items:flex-start; gap:7px; }
.ch-name{ flex:1 1 auto; min-width:0; font-size:12.5px; font-weight:400; line-height:1.32;
  color:var(--ink-dim); overflow:hidden;
  display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2;
  overflow-wrap:anywhere; }
.ch-name[hidden]{ display:none; }
.ch-row:hover .ch-name{ color:var(--ink); }
/* chat with a name FROM THE OWNER (renamed in the panel or named in Claude Code) -
      accent dot before the title + bolder text: 'I named this myself' */
.ch-row.ch-named .ch-name{ color:var(--ink); font-weight:600; }
.ch-row.ch-named .ch-name::before{
  content:""; display:inline-block; vertical-align:middle;
  width:5px; height:5px; margin:-1px 5px 0 0; border-radius:50%;
  background:var(--tt-accent); box-shadow:0 0 6px -1px var(--tt-accent);
}
/* chat with a MACHINE title (ai-title from the transcript) - normal weight, gray
      DIAMOND before the title (owner-named has a blue dot): shows where the name
      was auto-generated. Drawn as a CSS square, not a glyph: font-independent */
.ch-row.ch-auto .ch-name::before{
  content:""; display:inline-block; vertical-align:middle;
  width:4.5px; height:4.5px; margin:-1px 6px 0 1px;
  background:var(--tt-sub-dim); border-radius:1px;
  transform:rotate(45deg);
}
.ch-hl{ background:rgba(58,141,255,.30); color:var(--ink); border-radius:3px; padding:0 1px; }
.ch-time{ flex:0 0 auto; margin-top:1.5px; font-size:10px; color:var(--tt-time); white-space:nowrap;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
  transition:opacity .14s var(--ease); }

/* auto-title: appears on hover in place of the time (as in the bar, .tt-ainame) */
.ch-row:hover .ch-time{ opacity:0; }

/* row while a chat is opening - spinner on the right, clicks blocked */
.ch-row.ch-loading{ pointer-events:none; background:var(--sel); }
.ch-row.ch-loading::before{
  content:""; position:absolute; right:8px; top:6px; width:12px; height:12px; border-radius:50%;
  border:2px solid rgba(255,255,255,.16); border-top-color:rgba(255,255,255,.62);
  -webkit-animation:mtspin .8s linear infinite; animation:mtspin .8s linear infinite;
}

/* inline rename (like .tt-tn.tt-edit) */
.ch-input{
  flex:1 1 auto; min-width:0; height:18px; padding:0 4px; margin:0;
  font:600 12.5px/18px inherit; color:var(--ink); background:#000;
  border:0; border-bottom:1.5px solid var(--tt-accent); border-radius:3px 3px 0 0;
  outline:none; -webkit-appearance:none; appearance:none;
}
.ch-input::placeholder{ color:var(--ink-mute); font-weight:400; }

/* ===== list states / loading more ===== */
.ch-state{ padding:18px 14px 20px; text-align:center; font-size:12.5px; line-height:1.5; color:var(--ink-mute); }
.ch-state b{ display:block; margin-bottom:3px; font-size:13px; font-weight:600; color:var(--ink-dim); }
.ch-state .ch-q{ color:var(--ink-dim); }
.ch-link{ margin-top:9px; padding:4px 10px; font:600 12px/1 inherit; color:var(--tt-accent);
  background:transparent; border:.5px solid rgba(58,141,255,.3); border-radius:8px; cursor:pointer;
  -webkit-appearance:none; appearance:none; transition:background .15s var(--ease); }
.ch-link:hover{ background:rgba(58,141,255,.14); }
.ch-more{ display:block; width:calc(100% - 20px); margin:6px 10px 4px; height:28px;
  font:600 12px/1 inherit; color:var(--tt-accent); background:rgba(58,141,255,.10);
  border:.5px solid rgba(58,141,255,.26); border-radius:8px; cursor:pointer;
  -webkit-appearance:none; appearance:none; }
.ch-more:hover{ background:rgba(58,141,255,.18); }
.ch-sent{ height:1px; }

/* ===== row context menu (lives in <body>, position:fixed) ===== */
.ch-menu{
  position:fixed; left:0; top:0; z-index:9700; min-width:190px; padding:5px;
  background:#1c1c1e; border:.5px solid var(--hair); border-radius:12px;
  box-shadow:0 18px 44px rgba(0,0,0,.72);
  font:400 13px/1.2 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif; color:var(--ink);
  -webkit-animation:fpPop .16s var(--ease) both; animation:fpPop .16s var(--ease) both;
}
.ch-menu.ch-hidden{ display:none; }
.ch-mhead{ max-width:240px; padding:6px 9px 8px; margin-bottom:4px; font-size:11px; color:var(--ink-mute);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-bottom:.5px solid var(--hair-soft); }
.ch-mitem{ display:flex; align-items:center; gap:9px; width:100%; padding:8px 9px; text-align:left;
  font:400 13px/1.2 inherit; color:var(--ink); background:transparent; border:0; border-radius:8px;
  cursor:pointer; -webkit-appearance:none; appearance:none; }
.ch-mitem:hover{ background:var(--sel); }
.ch-mitem svg{ flex:0 0 auto; width:15px; height:15px; opacity:.72; }
</style>
</head>
<body>

<!-- ============ SCREEN 1: CHAT LIST (tmux tabs 2.0) ============ -->
<div id="tt-list-screen">
  <div class="tt-nav">
    <div class="tt-title">Sessions<small id="tt-subtitle"></small></div>
    <div class="tt-nav-actions">
      <button class="tt-ibtn" id="tt-set-btn" aria-label="Settings">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7H1a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 2.6 7a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 7 2.6h.1A1.6 1.6 0 0 0 8.9 1V1a2 2 0 1 1 4 0v.1A1.6 1.6 0 0 0 17 2.6a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7H23a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg>
      </button>
      <button class="tt-ibtn" id="gd-btn-list" aria-label="Interface guide">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.8-.9 1.4v.4"/><path d="M12 16.9h.01"/></svg>
      </button>
      <button class="tt-ibtn" id="ttc-btn" aria-label="Claude chats">
        <svg viewBox="0 0 24 24"><path d="M20 12a7.5 7.5 0 0 1-11.2 6.5L4 19.5l1.1-4.3A7.5 7.5 0 1 1 20 12z"/><path d="M9 10.5h6M9 13.5h4"/></svg>
      </button>
      <button class="tt-ibtn" id="tt-new-btn" aria-label="New session">
        <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
      </button>
    </div>
  </div>
  <div class="tt-list" id="tt-list"><!-- rows injected by JS --></div>
  <div class="tt-bottombar">
    <div class="tt-folders" id="tt-folders"><!-- folder pills injected by JS --></div>
  </div>
  <button type="button" class="tt-fab" id="tt-fab" aria-label="New session">
    <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
  </button>
</div>

<!-- "Claude chats" screen for phones (ttc-btn button in the list header) -->
<div id="ttc-screen" aria-hidden="true">
  <div class="ttc-nav">
    <button type="button" class="ttc-back" id="ttc-back" aria-label="Back to sessions">‹ Sessions</button>
    <div class="ttc-title">Chats<small id="ttc-count"></small></div>
  </div>
  <div class="ttc-srow"><input type="search" id="ttc-q" placeholder="Search chats" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" aria-label="Search chats"></div>
  <div class="ttc-list" id="ttc-list"></div>
</div>

<!-- long-press action sheet -->
<div class="tt-scrim" id="tt-scrim"></div>
<div class="tt-sheet" id="tt-sheet">
  <div class="tt-sgroup" id="tt-sheet-body"></div>
  <button type="button" class="tt-scancel" id="tt-sheet-cancel">Cancel</button>
</div>

<div id="app">
  <div id="bar">
    <span class="tt-termname" id="tt-termname"><span class="tt-td"></span><span class="tt-tn" id="tt-tn" title="Tap to rename session">—</span></button></span>
    <button class="ctl" id="barcollapse" title="Collapse / expand buttons" aria-label="Collapse buttons">
      <svg class="bc-chev" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>
    </button>
    <span class="bar-actions">
    <button class="ctl" id="add" title="New tab">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
    </button>
    <button class="ctl" id="reload" title="Reconnect terminal (if frozen or garbled)">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>
    </button>
    <button class="ctl" id="gridbtn" title="All windows as tiles">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M10.5 18.5h3"/></svg>
    </button>
    <button class="ctl" id="openfolder" title="Open folder">
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
    </button>
    <button class="ctl" id="imgbtn" title="Send photo to Claude">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10" r="1.6"/><path d="M21 16l-4.5-4.5L7 21"/></svg>
    </button>
    <button class="ctl" id="filebtn" title="Upload file to the current folder">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.05 12.2 20.2a5 5 0 0 1-7.1-7.1l9.2-9.2a3.2 3.2 0 0 1 4.6 4.6l-9.2 9.2a1.4 1.4 0 0 1-2-2l8.5-8.5"/></svg>
    </button>
    <button class="ctl" id="gd-btn-bar" title="Interface guide" aria-label="Interface guide">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.8-.9 1.4v.4"/><path d="M12 16.9h.01"/></svg>
    </button>
    <button class="ctl" id="sidebtn" title="Show files panel" aria-label="Files panel" aria-pressed="false">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="4.5" width="17.6" height="15" rx="2.4"/><path d="M9.4 4.5v15"/><rect class="sb-btn-fill" x="4.5" y="5.8" width="3.7" height="12.4" rx="1.1" fill="currentColor" stroke="none"/></svg>
    </button>
    <button class="ctl" id="undobtn" title="Nothing to undo" aria-label="Undo file operation" disabled>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9h10.6a4.6 4.6 0 1 1 0 9.2H9"/><path d="M7.6 5.4L4 9l3.6 3.6"/></svg>
    </button>
    </span>
    <!-- no image/*: then iOS converts HEIC to JPEG itself on selection -->
    <input type="file" id="imgfile" accept="image/jpeg,image/png" multiple style="display:none">
    <input type="file" id="ufile" multiple style="display:none">
    <span id="tabs"></span>
    <span id="tabhint" aria-hidden="true"></span>
    <span id="mem" role="button" tabindex="0" title="Free RAM · tap for settings">—</span>
  </div>
  <div id="sb-row">
  <aside id="sidebar" aria-label="Files and chats">
    <div class="sb-inner">
      <!-- TOP: chats panel. The contents of #sb-chats are written by separate code. -->
      <div class="sb-top" id="sb-top"><div id="sb-chats"></div></div>
      <div class="sb-hsplit" id="sb-hsplit" role="separator" aria-orientation="horizontal"
           aria-label="Chats panel border" title="Drag to resize the chats panel"></div>
      <!-- BOTTOM: file tree -->
      <section class="sb-files">
        <div class="sb-head">
          <span class="sb-cap">Explorer</span>
          <button type="button" class="sb-tbtn" id="sb-newdir" title="New folder" aria-label="New folder">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l1.8 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/><path d="M12 11.5v5M9.5 14h5"/></svg>
          </button>
          <button type="button" class="sb-tbtn" id="sb-dot" title="Show hidden files" aria-label="Show hidden files" aria-pressed="false">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.6 12S6 5.9 12 5.9 21.4 12 21.4 12 18 18.1 12 18.1 2.6 12 2.6 12z"/><circle cx="12" cy="12" r="2.9"/></svg>
          </button>
          <button type="button" class="sb-tbtn" id="sb-collapse" title="Collapse all folders" aria-label="Collapse all folders">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 9.5l5-5 5 5"/><path d="M7 14.5l5 5 5-5"/></svg>
          </button>
          <button type="button" class="sb-tbtn" id="sb-refresh" title="Refresh tree" aria-label="Refresh tree">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>
          </button>
        </div>
        <div class="sb-crumbs" id="sb-crumbs" aria-label="Current root"></div>
        <div class="sb-tree" id="sb-tree" tabindex="0" role="tree" aria-label="File tree"></div>
      </section>
    </div>
  </aside>
  <div id="sb-resizer" role="separator" aria-orientation="vertical" aria-label="Panel width"></div>
  <div id="wrap"><iframe id="term" src="term/"></iframe>
  <div id="dropzones" class="dz fp-hidden" aria-hidden="true">
    <div class="dz-zone" data-dir="v" data-before="1" style="grid-area:top">top</div>
    <div class="dz-zone" data-dir="h" data-before="1" style="grid-area:left">left</div>
    <div class="dz-zone dz-center" style="grid-area:mid">switch</div>
    <div class="dz-zone" data-dir="h" data-before="0" style="grid-area:right">right</div>
    <div class="dz-zone" data-dir="v" data-before="0" style="grid-area:bot">bottom</div>
  </div>
</div>
</div><!-- /#sb-row -->
<div id="dragghost" class="dragghost fp-hidden"></div>
  <div id="keys" class="hidden">
    <span id="keys-cmds"></span><!-- quick commands: rendered from localStorage (configurable) -->
    <span class="sep"></span>
    <button class="key" data-k="Escape">esc</button>
    <button class="key" data-k="Tab">tab</button>
    <button class="key" data-k="C-c">^C</button>
    <button class="key" data-k="C-d">^D</button>
    <button class="key" data-k="C-l">^L</button>
    <button class="key" data-k="Up">&#8593;</button>
    <button class="key" data-k="Down">&#8595;</button>
    <button class="key" data-k="Left">&#8592;</button>
    <button class="key" data-k="Right">&#8594;</button>
    <button class="key" data-k="Enter">&#9166;</button>
    <span class="sep"></span>
    <button class="key" data-k="PageUp">PgUp</button>
    <button class="key" data-k="PageDown">PgDn</button>
    <button class="key" data-k="C-o" title="Transcript (Ctrl+O)">^O</button>
    <span class="sep"></span>
    <button class="key" data-pane="splitv" title="Split: top/bottom">⬓ split</button>
    <button class="key" data-pane="splith" title="Split: left/right">◧ split</button>
    <button class="key" data-pane="next" title="Next pane">⇄ part</button>
    <button class="key" data-pane="break" title="Detach pane back into a tab">⇱ part</button>
    <button class="key" data-pane="kill" title="Close pane">✕ part</button>
  </div>
  <div id="set-tabsbar" class="set-tabsbar" hidden></div>
  <div id="attachbar" aria-label="Attached photos"></div>
  <div id="inputbar" style="position:relative">
    <button class="tt-back" id="tt-back" aria-label="To session list" title="To session list">
      <svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>
    </button>
    <button id="keystoggle" title="Keys and commands">
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
    </button>
    <button id="micbtn" title="Voice input">
      <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
    </button>
    <textarea id="tin" rows="1" placeholder="Message Claude…" style="padding-right:36px"
      autocapitalize="off" autocomplete="off" autocorrect="on" spellcheck="true"></textarea>
    <button type="button" class="hist-btn" title="Input history" aria-label="Input history">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 3.8V8h4.2"/><path d="M12 7.6V12l3 1.8"/></svg>
    </button>
    <button id="send" title="Send (Ctrl/Cmd+Enter)">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7.4 4.6 19 11.3a.8.8 0 0 1 0 1.4L7.4 19.4a.8.8 0 0 1-1.2-.7V5.3a.8.8 0 0 1 1.2-.7z"/></svg>
    </button>
  </div>
</div>
<div class="hist-ovl hist-hidden"></div>
<div class="hist-pop hist-hidden" role="dialog" aria-label="Input history">
  <div class="hist-nav">
    <button type="button" class="hist-arr hist-prev" aria-label="Newer">←</button>
    <span class="hist-pos">0/0</span>
    <button type="button" class="hist-arr hist-next" aria-label="Older">→</button>
    <span class="hist-sp"></span>
    <button type="button" class="hist-x" aria-label="Close">✕</button>
  </div>
  <pre class="hist-text"></pre>
  <button type="button" class="hist-insert">Insert</button>
</div>
<!-- Close-window confirmation: explicit dialog instead of double-tap on the cross -->
<div class="kc-ovl hist-hidden" id="kc-ovl">
  <div class="kc-pop" role="dialog" aria-modal="true" aria-label="Close window" onclick="event.stopPropagation()">
    <div class="kc-msg" id="kc-msg"></div>
    <div class="kc-row">
      <button type="button" class="kc-btn kc-cancel" id="kc-cancel">Cancel</button>
      <button type="button" class="kc-btn kc-danger" id="kc-confirm">Close</button>
    </div>
  </div>
</div>
<!-- Settings: popup in the project style (solid background, no backdrop-filter) -->
<div class="set-ovl hist-hidden"></div>
<div id="set-pop" class="set-pop hist-hidden" role="dialog" aria-label="Settings">
  <div class="set-head">
    <span class="set-title">Settings</span>
    <button type="button" class="set-x" aria-label="Done">Done</button>
  </div>
  <div class="set-list"></div>
</div>
<div id="nm-ovl" class="nm-ovl fp-hidden"></div>
<div id="newmenu" class="nm fp-hidden" role="menu" aria-label="New window">
  __NM_AGENTS__
<button type="button" class="nm-item" data-cmd="shell">Terminal</button>
</div>
<!-- Interactive interface guide: 'spotlight' backdrop + step card.
          The card is a SIBLING of the backdrop (not a child), so its z-index 9810 is counted
          from body and is guaranteed to sit above all overlays. -->
<div id="gd-ovl" class="gd-hidden" aria-hidden="true"><div class="gd-ring" id="gd-ring"></div></div>
<div id="gd-card" class="gd-hidden" role="dialog" aria-modal="true" aria-label="Interface guide">
  <button type="button" class="gd-close" id="gd-close" aria-label="Close guide">✕</button>
  <div class="gd-title" id="gd-title"></div>
  <div class="gd-text" id="gd-text"></div>
  <div class="gd-foot">
    <div class="gd-dots" id="gd-dots"></div>
    <button type="button" class="gd-btn gd-back" id="gd-back">Back</button>
    <button type="button" class="gd-btn gd-next" id="gd-next">Next</button>
  </div>
  <div class="gd-skip" id="gd-skip">Skip</div>
</div>
<div id="toast"></div>
<div id="lightbox" aria-hidden="true">
  <button type="button" class="lb-x" aria-label="Close">✕</button>
  <img alt="Photo preview">
</div>

<!-- TGV: tile mode - fullscreen grid, pill and half-screen panel -->
<div id="tgv-grid" class="tgv-grid" role="dialog" aria-label="All windows as tiles">
  <button type="button" id="tgv-close" class="tgv-close" aria-label="Close">
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
  <div id="tgv-cells" class="tgv-cells"></div>
</div>
<button type="button" id="tgv-pill" class="tgv-pill" aria-label="Windows panel"></button>
<div id="tgv-strip-bg" class="tgv-strip-bg" aria-hidden="true"></div>
<div id="tgv-strip" class="tgv-strip" role="dialog" aria-label="Windows in a row">
  <div id="tgv-row" class="tgv-row"></div>
</div>

<div id="fp-modal" class="fp-hidden" role="dialog" aria-modal="true" aria-label="Open folder">
  <div class="fp-backdrop" data-fp-close></div>
  <div class="fp-sheet" role="document">
    <header class="fp-head">
      <span class="fp-title">Open folder</span>
      <button class="fp-close" type="button" data-fp-close aria-label="Close">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <div class="fp-pathrow">
      <input id="fp-path" class="fp-input" type="text" inputmode="text"
             autocapitalize="off" autocorrect="off" autocomplete="off" spellcheck="false"
             enterkeyhint="go" placeholder="~/projects/" aria-label="Folder path">
      <button id="fp-ok" class="fp-ok" type="button">OK</button>
    </div>
    <div id="fp-favs" class="fp-favs" hidden aria-label="Favorite folders"></div>
    <div class="fp-launch" role="group" aria-label="What to launch in the folder">
      <button type="button" class="fp-seg" data-cmd="shell">Terminal</button>
      __FP_AGENTS__
</div>
    <div id="fp-list" class="fp-list" aria-busy="false"></div>
  </div>
</div>

<div id="vc-modal" class="vc-float fp-hidden" role="dialog" aria-label="Voice input">
  <div class="vc-drag" id="vc-drag">
    <span class="vc-dot"></span>
    <span class="fp-title">Voice</span>
    <button class="fp-close" type="button" data-vc-close aria-label="Close">
      <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
    </button>
  </div>
  <div class="vc-body">
    <button id="vc-rec" class="vc-rec" type="button" aria-label="Record">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
    </button>
    <div id="vc-time" class="vc-time" hidden>00:00</div>
    <div id="vc-hint" class="vc-hint">Tap to record</div>
    <div id="vc-steps" class="vc-steps" hidden></div>
    <textarea id="vc-text" class="vc-text" hidden placeholder="Transcript…"
      autocapitalize="sentences" autocorrect="on" spellcheck="true"></textarea>
  </div>
  <div class="vc-actions">
    <button id="vc-cancel" class="vc-btn" type="button">Close</button>
    <button id="vc-again" class="vc-btn" type="button" hidden>Redo</button>
    <button id="vc-insert" class="vc-btn vc-primary" type="button" hidden>Insert</button>
    <button id="vc-retry" class="vc-btn vc-primary" type="button" hidden>Retry</button>
    <button id="vc-renew" class="vc-btn" type="button" hidden>Record again</button>
    <button id="vc-download" class="vc-btn" type="button" hidden>Download</button>
  </div>
</div>

<!-- tree context menu (items rendered by JS) -->
<div id="sb-movl" class="sb-movl fp-hidden"></div>
<div id="sb-menu" class="sb-menu fp-hidden" role="menu" aria-label="File actions"></div>

<!-- delete confirmation: same .fp-sheet primitive as the folder picker -->
<div id="sb-confirm" class="fp-hidden" role="dialog" aria-modal="true" aria-label="Delete">
  <div class="fp-backdrop" data-sb-close></div>
  <div class="fp-sheet" role="document">
    <header class="fp-head">
      <span class="fp-title">Delete?</span>
      <button class="fp-close" type="button" data-sb-close aria-label="Close">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <div class="sb-cf-body">
      <div class="sb-cf-name" id="sb-cf-name"></div>
      <div class="sb-cf-path" id="sb-cf-path"></div>
      <div class="sb-cf-note" id="sb-cf-note"></div>
      <div class="sb-cf-warn" id="sb-cf-warn" hidden></div>
      <div class="sb-cf-trash">Deleted items go to the trash - restore them with the "↺ Undo" button in the top bar.</div>
    </div>
    <div class="sb-cf-actions">
      <button type="button" class="sb-cf-btn sb-cf-cancel" id="sb-cf-cancel">Cancel</button>
      <button type="button" class="sb-cf-btn sb-cf-danger" id="sb-cf-ok">Delete</button>
    </div>
  </div>
</div>
<script>
// The page lives at .../m/ (possibly behind a stripped URL prefix, e.g. /tt/user/m/).
// All requests are RELATIVE to the page, so any prefix works by itself.
// The backend redirects /m -> m/ (without the slash relative paths would break).
window.VOICE_ON = __VOICE_ON__;
window.HOME_DIR = __HOME_DIR__;
const API = 'api/';
let lastActive = null, editingClose = false;

function toast(t){ const e=document.getElementById('toast'); e.textContent=t; e.classList.add('show');
  clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('show'),1400); }

// Interface mode: '1' = classic (desktop), '0' = mobile 2.0,
// no key = auto by device (wide screen without touch = desktop).
function ttIsClassic(){
  var v=localStorage.getItem('mterm_classic');
  if(v==='1') return true;
  if(v==='0') return false;
  try{ return window.matchMedia('(min-width:900px) and (pointer:fine)').matches; }catch(e){ return false; }
}
function ttModeIsAuto(){ return localStorage.getItem('mterm_classic')===null; }

async function api(path, opts){ const r = await fetch(API+path, opts||{}); if(!r.ok) throw new Error(r.status);
  const ct=r.headers.get('content-type')||''; return ct.includes('json')? r.json(): r.text(); }

// indicator dot class by window state (work/done/idle)
function tindClass(st){ return 'tind'+(st==='work'?' tind-work':(st==='done'?' tind-done':'')); }
// Targeted update of indicator dots WITHOUT rebuilding the DOM: st is NOT in _winsSig,
// so render() does not touch the window set, and here we find by data-id/data-idx
// .tab (and open tiles) and change ONLY className of .tind - strip scroll
// stays intact, taps are not swallowed (a known pain point).
function updateIndicators(wins){
  const tabs=document.getElementById('tabs');
  const cells=document.getElementById('tgv-cells'), row=document.getElementById('tgv-row');
  for(const w of wins){
    const cls=tindClass(w.st);
    const sel='[data-id="'+(w.id||'')+'"]';
    if(tabs){
      const tab=w.id ? tabs.querySelector('.tab'+sel) : tabs.querySelector('.tab[data-idx="'+w.index+'"]');
      if(tab){ const d=tab.querySelector('.tind'); if(d && d.className!==cls) d.className=cls; }
    }
    for(const host of [cells,row]){
      if(!host) continue;
      const tl=host.querySelector('.tgv-tile[data-idx="'+w.index+'"]');
      if(tl){ const d=tl.querySelector('.tind'); if(d && d.className!==cls) d.className=cls; }
    }
  }
}

// tooltip with the full tab name (see #tabhint in CSS): above the tab,
// centered on it, clamped to the screen. On touch pointerenter/leave come together with
// the touch - on a phone the name is visible while the finger is on the tab.
function showTabHint(btn, text){
  const h=document.getElementById('tabhint'); if(!h) return;
  h.textContent=text;
  const r=btn.getBoundingClientRect(), vw=window.innerWidth;
  const w=h.offsetWidth, hh=h.offsetHeight;
  let left=r.left+r.width/2-w/2;
  left=Math.max(8, Math.min(vw-w-8, left));
  let top=r.top-hh-4;
  if(top<2) top=2;
  h.style.left=left+'px'; h.style.top=top+'px';
  h.classList.add('on');
}
function hideTabHint(){ const h=document.getElementById('tabhint'); if(h) h.classList.remove('on'); }

function render(wins, scrollToActive){
  hideTabHint();
  const c = document.getElementById('tabs'); c.innerHTML='';
  for(const w of wins){
    const b=document.createElement('button'); b.className='tab'+(w.active?' active':'');
    b.dataset.idx=w.index;
    if(w.id) b.dataset.id=w.id;
    const label=document.createElement('span'); label.textContent=(w.index)+': '+w.name; b.appendChild(label);
    const dot=document.createElement('span'); dot.className=tindClass(w.st); b.appendChild(dot);
    const x=document.createElement('span'); x.className='x'; x.textContent='×';
    x.onclick=(e)=>{ e.stopPropagation(); askClose(w.index, w.name); };
    b.appendChild(x);
    b.onclick=()=>{ if(!b._dragged) select(w.index); };
    attachTabDrag(b, w.index, w.name);
    b.addEventListener('pointerenter', ()=>showTabHint(b, w.index+': '+w.name));
    b.addEventListener('pointerleave', hideTabHint);
    c.appendChild(b);
    if(w.active && scrollToActive) b.scrollIntoView({inline:'nearest',block:'nearest'});
  }
}

// drag a tab into a split: drag the tab -> zones above the terminal ->
// the window joins the current view as a pane (left/right/top/bottom)
const dz=document.getElementById('dropzones'), ghost=document.getElementById('dragghost');
// While windows are being reordered/merged on the backend, windows polling does NOT redraw the strip
// (otherwise an intermediate/old server reply would put tabs back in the previous order),
// and a polling reply that started BEFORE the operation is discarded after it (_winsGen).
let _winsBusy=0, _winsGen=0, _tabDragging=false, _tabDragT=0;
async function winsMutate(fn){
  _winsBusy++; _winsGen++;
    // safeguard: an aborted request (network/VPN/hung docker exec) must not
    // keep the strip 'busy' forever - otherwise auto-refresh and windows polling
    // stay silent until the page reloads. Release after 15 s.
  let tm=null;
  const guard=new Promise(res=>{ tm=setTimeout(res, 15000); });
  try{ return await Promise.race([fn(), guard]); }
  finally{ clearTimeout(tm); _winsBusy--; _winsGen++; }
}
async function joinWin(src, dir, before){
  await winsMutate(async()=>{
  try{ const r=await api('join?src='+src+'&dir='+dir+'&before='+(before?1:0), {method:'POST'});
    if(r && r.error){ toast(r.error); return; }
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, true); }
    toast('Session '+src+' in split');
  }catch(e){ toast('Drag failed'); }
  });
}
// persist the new tab order on the backend (actually moves tmux windows)
async function commitReorder(){
  const c=document.getElementById('tabs');
  const order=[...c.querySelectorAll('.tab')].map(t=>+t.dataset.idx);
  await winsMutate(async()=>{
  try{
    const r=await api('reorder?order='+order.join(','), {method:'POST'});
    if(r && r.error){ toast(r.error); _winsSig=''; return; }
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, false); }
  }catch(e){ toast('Reorder failed'); _winsSig=''; }
  });
  if(!_winsSig) refresh();   // error -> roll back to the server order
}
// Three gestures on a tab (tabs use touch-action:none - gestures are handled manually):
//   - hold in place (long-press) -> 'lift' and drag SIDEWAYS = reorder;
//   - quick swipe sideways -> scroll the strip (scrollLeft driven manually);
//   - pull DOWN toward the terminal -> split (as before).
function attachTabDrag(btn, idx, name){
  btn.addEventListener('pointerdown',(e)=>{
    if(e.target.closest('.x')) return;                 // do not drag the close button
    const c=btn.parentNode;
    const sx=e.clientX, sy=e.clientY;
    let mode=null;                                     // 'reorder' | 'scroll' | 'split'
    let lastX=e.clientX, prevX=e.clientX, baseX=e.clientX, roInit=null;
    const isMouse=(e.pointerType==='mouse');
    btn._dragged=false;
        // -- auto-scroll the strip at the edges while reordering --
        // A strip with 15 tabs is scrolled; without this a tab cannot be dragged
        // past the visible left/right - it 'hit' the edge.
    let asRaf=null;
    function autoScroll(){
      asRaf=null;
      if(mode!=='reorder') return;
      const cr=c.getBoundingClientRect(), EDGE=48, SPEED=10;
      let d=0;
      if(lastX<cr.left+EDGE) d=-SPEED*Math.min(1,(cr.left+EDGE-lastX)/EDGE);
      else if(lastX>cr.right-EDGE) d=SPEED*Math.min(1,(lastX-(cr.right-EDGE))/EDGE);
      if(d){
        const before=c.scrollLeft; c.scrollLeft+=d;
        const moved=c.scrollLeft-before;
        if(moved){ baseX-=moved; placeOver(lastX); }   // tab stays under the cursor
      }
      asRaf=requestAnimationFrame(autoScroll);
    }
    function enterReorder(){
      mode='reorder'; btn._dragged=true; _tabDragging=true; _tabDragT=Date.now();
      btn.classList.add('reorder');
      baseX=lastX; btn.style.transform='translateX(0px) scale(1.06)';
      roInit=[...c.querySelectorAll('.tab')].map(t=>t.dataset.idx).join(',');
      if(navigator.vibrate) navigator.vibrate(18);
      if(!asRaf) asRaf=requestAnimationFrame(autoScroll);
    }
        // insert the lifted tab before/after the neighbor under point x
    function placeOver(x){
            // render() in the middle of a drag (select/addWin/join) rebuilt the strip: btn is no longer
            // in the DOM, insertBefore would return a 'detached' button -> two tabs with
            // the same idx. Cancel the gesture and redraw from the server.
      if(!btn.isConnected){ cancelDrag(); return; }
      btn.style.transform='translateX('+(x-baseX)+'px) scale(1.06)';
            // barY - from the current strip position: keyboard/panel may have shifted it
      const cr=c.getBoundingClientRect(), barY=cr.top+cr.height/2;
      const over=document.elementsFromPoint(x, barY)
        .map(el=>el.closest && el.closest('.tab')).find(t=>t && t!==btn && t.parentNode===c);
      if(over){
        const r=over.getBoundingClientRect();
        const b0=btn.getBoundingClientRect().left;
        c.insertBefore(btn, (x < r.left+r.width/2) ? over : over.nextSibling);
        baseX += btn.getBoundingClientRect().left - b0;   // stick to the cursor without a jump
        btn.style.transform='translateX('+(x-baseX)+'px) scale(1.06)';
      }
    }
        // pointer capture: without it, as soon as the cursor/finger moves onto the terminal iframe,
        // pointermove/pointerup go to the iframe, not to us - the drag
        // hung in the lifted state, up never arrived, listeners leaked (both split and reorder).
    try{ btn.setPointerCapture(e.pointerId); }catch(_){}
        // long-press in place -> reorder mode. Finger only: a mouse held
        // without moving (>300 ms) must SELECT the tab on release, not
        // lift it (with a mouse we reorder by a horizontal gesture).
    const lp=isMouse ? null : setTimeout(()=>{ if(!mode) enterReorder(); }, 300);
    let ended=false;
        // remove everything attached for the duration of the gesture
    function teardown(){
      ended=true;
      clearTimeout(lp);
      if(asRaf){ cancelAnimationFrame(asRaf); asRaf=null; }
      document.removeEventListener('pointermove',move,true);
      document.removeEventListener('pointerup',up,true);
      document.removeEventListener('pointercancel',up,true);
      _tabDragging=false;
    }
        // gesture cancelled (button detached from DOM / system aborted the pointer):
        // commit nothing, roll the view back to the server order
    function cancelDrag(){
      teardown(); mode=null;   // autoScroll sees via mode that the gesture is over
      btn.classList.remove('reorder','dragging'); btn.style.transform='';
      dz.classList.add('fp-hidden'); ghost.classList.add('fp-hidden');
      dz.querySelectorAll('.dz-zone').forEach(z=>z.classList.remove('hot'));
      _winsSig=''; refresh();
      setTimeout(()=>{ btn._dragged=false; }, 60);
    }
    function move(ev){
      if(ev.pointerId!==e.pointerId) return;   // a second finger does not drive the first one's gesture
      lastX=ev.clientX;
      const dx=ev.clientX-sx, dy=ev.clientY-sy;
      if(mode==='reorder'){
        ev.preventDefault();
        placeOver(ev.clientX);
        return;
      }
      if(mode==='scroll'){ c.scrollLeft -= (ev.clientX-prevX); prevX=ev.clientX; return; }
      if(mode==='split'){
        ev.preventDefault();
        ghost.style.left=ev.clientX+'px'; ghost.style.top=ev.clientY+'px';
        const el=document.elementFromPoint(ev.clientX, ev.clientY);
        const zone=el && el.closest('.dz-zone');
        dz.querySelectorAll('.dz-zone').forEach(z=>z.classList.toggle('hot', z===zone));
        return;
      }
      // mode not chosen yet - decide by the gesture
      if(Math.abs(dx)>8 || Math.abs(dy)>8) clearTimeout(lp);    // finger moved -> not a long-press
      if(dy>=14 && Math.abs(dy)>Math.abs(dx)){
        mode='split'; btn._dragged=true; _tabDragging=true; _tabDragT=Date.now(); btn.classList.add('dragging');
        dz.classList.remove('fp-hidden'); ghost.classList.remove('fp-hidden'); ghost.textContent=idx+': '+name;
        ev.preventDefault();
      } else if(Math.abs(dx)>=10 && Math.abs(dx)>=Math.abs(dy)){
        // mouse: horizontal gesture = reorder at once, no hold
        // (the strip is scrolled with the wheel); finger: this is strip scrolling
        if(isMouse){ clearTimeout(lp); enterReorder(); }
        else { mode='scroll'; btn._dragged=true; prevX=ev.clientX; }
      }
    }
    function up(ev){
      if(ended) return;                          // pointerup + lostpointercapture - one finish
      if(ev.pointerId!==e.pointerId) return;   // a DIFFERENT finger was released - gesture continues
      if(!btn.isConnected){ cancelDrag(); return; }   // strip was redrawn under the finger
      teardown();
      try{ btn.releasePointerCapture(ev.pointerId); }catch(_){}
      if(mode==='reorder'){
        btn.classList.remove('reorder'); btn.style.transform='';
        if(ev.type==='pointercancel'){
          // system aborted the gesture (call, edge swipe) - partial order
          // NOT committed, strip returns to the server order
          _winsSig=''; refresh();
        } else {
          const now=[...c.querySelectorAll('.tab')].map(t=>t.dataset.idx).join(',');
          if(now!==roInit) commitReorder();
        }
      } else if(mode==='split'){
        const el=document.elementFromPoint(ev.clientX, ev.clientY);
        const zone=el && el.closest('.dz-zone');
        dz.classList.add('fp-hidden'); ghost.classList.add('fp-hidden');
        dz.querySelectorAll('.dz-zone').forEach(z=>z.classList.remove('hot'));
        btn.classList.remove('dragging');
        if(zone){
          if(zone.classList.contains('dz-center')) select(idx);
          else joinWin(idx, zone.dataset.dir, zone.dataset.before==='1');
        }
      }
      setTimeout(()=>{ btn._dragged=false; }, 60);     // suppress click after drag
    }
    document.addEventListener('pointermove',move,true);
    document.addEventListener('pointerup',up,true);
    document.addEventListener('pointercancel',up,true);
        // Capture is lost BY ITSELF when the tab is moved in the DOM (insertBefore) or
        // gets pointer-events:none on lift - Chrome sends lostpointercapture
        // although the gesture continues. The gesture must NOT be finished on it (it
        // killed mouse reordering altogether). While the gesture is alive and the button is in the DOM, capture
        // again (so events do not go to the terminal iframe); button removed - cancel.
    btn.addEventListener('lostpointercapture', ()=>{
      if(ended) return;
      if(!btn.isConnected){ cancelDrag(); return; }
      try{ btn.setPointerCapture(e.pointerId); }catch(_){}
    });
  });
}

// mouse wheel over the tab strip scrolls it horizontally (the strip has no vertical
// scroll, and dragging with the mouse now = reordering)
document.getElementById('tabs').addEventListener('wheel', (e)=>{
  if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){
    const d=(e.deltaMode===1) ? e.deltaY*16 : e.deltaY;   // Firefox sends lines, not pixels
    e.currentTarget.scrollLeft+=d; e.preventDefault();
  }
}, {passive:false});
// redraw only when windows really change - otherwise innerHTML every
// 2.5s resets manual strip scroll and eats taps
let _winsSig='';
// redraw dedup signature: ONLY window set/names/order/active.
// WARNING: do NOT include st AND id - otherwise a state change rebuilds the strip DOM and
// resets manual scroll/swallows taps. State is applied by updateIndicators.
function winsSig(wins){ return JSON.stringify(wins.map(w=>[w.index,w.name,w.active])); }
async function refresh(){
  if(document.hidden) return;  // a hidden tab does not spin docker exec for nothing
  if(_tabDragging && Date.now()-_tabDragT>15000) _tabDragging=false;   // safeguard against an 'eternal' drag
  if(_winsBusy || _tabDragging) return;  // reorder/split in progress - leave the strip alone
  const gen=_winsGen;
  try{ const wins = await api('windows');
    if(gen!==_winsGen || _tabDragging) return;   // reply is stale: windows were reordered meanwhile
    const a = wins.find(w=>w.active), ai = a? a.index : null;
    const sig = winsSig(wins);
    if(sig!==_winsSig){ _winsSig=sig; render(wins, ai!==lastActive); }
    updateIndicators(wins);   // dots - every poll, AFTER render dedup
    lastActive = ai;
  }catch(e){ /* keep last UI */ }
}

async function select(i){
  // instant highlight - do not wait for the server (feels fast)
  document.querySelectorAll('#tabs .tab').forEach(b=>b.classList.toggle('active', +b.dataset.idx===i));
  lastActive=i;
  try{
    const r=await api('select?w='+i, {method:'POST'});  // reply already carries the window list
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, true); }
  }catch(e){ toast('Could not switch'); }
}
async function addWin(cmd, model){
  let q = (cmd && cmd!=='shell') ? ('?cmd='+cmd) : '';
  if(model) q += (q?'&':'?')+'model='+encodeURIComponent(model);
  const label = cmd;
  try{ const r= await api('new'+q, {method:'POST'});
    toast(cmd && cmd!=='shell' ? ('Launching '+label) : ('Window '+r.index));
        // the reply already carries a fresh list - draw immediately (refresh may have returned cache)
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, true); }
    else { _winsSig=''; await refresh(); } }
  catch(e){ toast('Creation error'); }
}
// menu on '+': choose what to launch (without typing claude/codex manually)
const newmenu=document.getElementById('newmenu');
const nmOvl=document.getElementById('nm-ovl');
let _nmFromTT=false;   // menu opened from the list screen (FAB/+) -> enter the terminal after creation
function openNewMenu(){
  _nmFromTT=false;
  const r=document.getElementById('add').getBoundingClientRect();
  newmenu.style.top=(r.bottom+6)+'px'; newmenu.style.left=r.left+'px';
  newmenu.classList.remove('fp-hidden'); nmOvl.classList.remove('fp-hidden');
}
// open the same menu from the list screen, anchored to an arbitrary element (FAB/header)
window.ttOpenNewMenu=function(anchor){
  _nmFromTT=true;
  const r=(anchor||document.getElementById('add')).getBoundingClientRect();
  const w=170;
  let left=Math.min(r.left, window.innerWidth-w-10);
  let top=r.bottom+6;
  if(top+320>window.innerHeight) top=Math.max(10, r.top-320);   // FAB at the bottom -> menu goes up
  newmenu.style.top=top+'px'; newmenu.style.left=Math.max(10,left)+'px';
  newmenu.classList.remove('fp-hidden'); nmOvl.classList.remove('fp-hidden');
};
function closeNewMenu(){ newmenu.classList.add('fp-hidden'); nmOvl.classList.add('fp-hidden'); }
nmOvl.onclick=closeNewMenu;
newmenu.querySelectorAll('.nm-item').forEach(b=>{
  b.onclick=()=>{ const fromTT=_nmFromTT; closeNewMenu();
    addWin(b.dataset.cmd, b.dataset.model);
    if(fromTT && window.ttEnterLastNew) window.ttEnterLastNew();   // enter the new terminal
  };
});
document.addEventListener('click',(e)=>{
  if(newmenu.classList.contains('fp-hidden')) return;
    // exclude ALL menu-opener buttons (classic #add and 2.0 buttons #tt-new-btn/#tt-fab),
    // otherwise a click on '+' opened the menu and the same click closed it at once - '+' seemed broken.
  if(!newmenu.contains(e.target) && !e.target.closest('#add,#tt-new-btn,#tt-fab')) closeNewMenu();
});
// cross: explicit confirmation dialog 'Close window N: name?' -
// instead of double-tap/native confirm(), which are easy to hit blindly on iOS
const kcOvl=document.getElementById('kc-ovl'), kcMsg=document.getElementById('kc-msg');
let kcTarget=null;
function askClose(i, name){
  kcTarget=i;
  kcMsg.textContent='Close window '+i+': '+name+'?';   // textContent - no innerHTML
  kcOvl.classList.remove('hist-hidden');
}
function kcClose(){ kcOvl.classList.add('hist-hidden'); kcTarget=null; }
document.getElementById('kc-cancel').onclick=kcClose;
kcOvl.onclick=kcClose;   // tap on the overlay outside the popup cancels (the popup stops propagation itself)
document.getElementById('kc-confirm').onclick=()=>{
  const i=kcTarget; kcClose();
  if(i!==null && i!==undefined) closeWin(i);
};
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape' && !kcOvl.classList.contains('hist-hidden')){ e.preventDefault(); kcClose(); }
});
async function closeWin(i){
    // Before: kill -> refresh() -> server returned a CACHE still with the closed window,
    // signature did not change, the tab vanished on the next poll (5-10 s).
    // Now the kill reply carries a fresh list - draw it at once; inside
    // winsMutate, so parallel polling does not bring back the old strip.
  await winsMutate(async()=>{
  try{ const r=await api('kill?w='+i, {method:'POST'});
    if(r && r.error){ toast(r.error); _winsSig=''; return; }
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, true); }
    else _winsSig='';
  }catch(e){ toast('Could not close'); _winsSig=''; }
  });
  if(!_winsSig) refresh();
  if(window.ttRefreshList) window.ttRefreshList();   // chat list - immediately
}

async function refreshMem(){
  try{ const m = await api('stat');
    const el=document.getElementById('mem');
    const g=(m.avail_mb/1024).toFixed(1);
    el.textContent='RAM '+g+'G';
    el.classList.toggle('low', m.avail_mb < 600);
  }catch(e){}
}

// --- text input and special keys (working around xterm.js IME limits) ---
async function sendKey(k){
  try{ await api('key?k='+encodeURIComponent(k), {method:'POST'}); }
  catch(e){ toast('key failed'); }
}
async function sendCmd(c){
    // slash command: literal input + Enter (Claude recognizes it as a command)
  try{ await api('type?enter=1&lit=1', {method:'POST',
        headers:{'Content-Type':'text/plain; charset=utf-8'}, body:c}); }
  catch(e){ toast('not sent'); }
}
let _paneBusy=false, _paneLastT=0;
async function sendPane(act){
    // split-screen / switch / close / detach a pane (tmux panes)
  if(_paneBusy) return;              // previous operation still running - do not spawn a second split
  if(act==='splitv' || act==='splith'){
        // _paneBusy is held ~40 ms - a double tap managed to make two splits.
        // Minimum interval between splits is 400 ms.
    const n=Date.now(); if(n-_paneLastT<400) return; _paneLastT=n;
  }
  _paneBusy=true;
  const run=async()=>{
  try{ const r=await api('pane?act='+encodeURIComponent(act), {method:'POST'});
    if(r && r.error){ toast(r.error); return; }
    if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows, true); }  // break returned the tab
    if(act==='break') toast('Pane detached into a tab');
  }catch(e){ toast('failed'); }
  finally{ _paneBusy=false; }
  };
    // break changes the tab set - like join, under winsMutate, so windows polling
    // that started before the operation does not redraw the strip with the old list
  if(act==='break') await winsMutate(run); else await run();
}
document.querySelectorAll('#keys button').forEach(b=>{
    // preventDefault on pointer/mouse-down: tapping a button does NOT move focus,
    // so iOS does not open the on-screen keyboard. Special keys go to the
    // backend via /m/api/key (sendKey) - they do not need text field focus.
  b.addEventListener('pointerdown', e=>e.preventDefault());
  b.addEventListener('mousedown', e=>e.preventDefault());
  b.onclick=()=>{
    if(b.dataset.cmd) sendCmd(b.dataset.cmd);
    else if(b.dataset.pane) sendPane(b.dataset.pane);
    else sendKey(b.dataset.k);
        // do NOT focus tin: focus in the textarea opened the on-screen keyboard, which
        // shrank the viewport and pushed the button row down - they 'stopped
        // responding'. To type, tap the input field manually.
  };
});

// #8: configurable quick commands (slash buttons under the menu). Stored in localStorage.
const QCMD_KEY='mterm_quickcmds', QCMD_DEF=['/resume','/model','/usage'];
function getQuickCmds(){ try{ const a=JSON.parse(localStorage.getItem(QCMD_KEY)||'null');
  if(Array.isArray(a)) return a.filter(x=>typeof x==='string'&&x.trim()); }catch(e){} return QCMD_DEF.slice(); }
function setQuickCmds(a){ localStorage.setItem(QCMD_KEY, JSON.stringify(a)); renderQuickCmds(); }
function renderQuickCmds(){ const el=document.getElementById('keys-cmds'); if(!el) return; el.innerHTML='';
  for(const c of getQuickCmds()){ const b=document.createElement('button'); b.type='button';
    b.className='key cmd'; b.dataset.cmd=c; b.textContent=c;
    b.addEventListener('pointerdown', e=>e.preventDefault());
    b.addEventListener('mousedown', e=>e.preventDefault());
    b.onclick=()=>sendCmd(c); el.appendChild(b); } }
window.getQuickCmds=getQuickCmds; window.setQuickCmds=setQuickCmds; window.renderQuickCmds=renderQuickCmds;
renderQuickCmds();

// one button hides/shows the whole row of keys and hotkeys
const keys=document.getElementById('keys'), ktoggle=document.getElementById('keystoggle');
ktoggle.onclick=()=>{
  const open=keys.classList.toggle('hidden')===false;
  ktoggle.classList.toggle('on', open);
};

const tin=document.getElementById('tin');
const sendBtn=document.getElementById('send');
function autoGrow(){ tin.style.height='auto'; tin.style.height=Math.min(tin.scrollHeight,160)+'px'; }
tin.addEventListener('input', autoGrow);
let sending=false;  // double-tap protection on a slow network
async function sendText(){
  if(sending) return;
  const t=tin.value;
    // attached photos (thumbnail strip) - their paths are injected into the window before the text
  const hasPhotos = (typeof photoAttach!=='undefined') && photoAttach.length>0;
  if(!t && !hasPhotos){ toast('Empty. Enter is in the ☰ panel'); return; }
  if(t.length>200000){ toast('Text too long: '+t.length+' (max 200 000)'); return; }
  sending=true; sendBtn.disabled=true; sendBtn.classList.add('busy');
  try{
        // 1) first the attached photo paths -> into the window (Claude picks them up as [Image]).
        //    w is fixed at attach time (photoW) so it does not go to another tab.
    if(hasPhotos){
      const paths=photoAttach.map(a=>a.path).filter(Boolean);
      if(paths.length){
        const q = (photoW!=null? '?w='+photoW : '');
        await api('attach-inject'+q, {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({paths})});
      }
      clearAttach();                     // photos sent - clear the thumbnail strip
    }
        // 2) then the question text (if any); photo-without-text - Enter was already sent by the injection
    if(t){
      await api('type?enter=1', {method:'POST',
        headers:{'Content-Type':'text/plain; charset=utf-8'}, body:t});
      histPush(t);                       // success: push to history
    } else if(hasPhotos){
      // photo without text: an empty type with enter=1 sends only Enter to the window,
      // so Claude accepts the attachment (same goal as with regular text)
      await api('type?enter=1', {method:'POST',
        headers:{'Content-Type':'text/plain; charset=utf-8'}, body:''});
    }
    tin.value=''; autoGrow(); tin.focus();
    clearTimeout(_draftT); saveDraft();  // success: draft cleared
  }catch(e){
    // error/limit: the draft stays in the field and in localStorage
    toast(e && e.message==='413'
      ? 'Text too long: '+t.length+' (max 200,000)'
      : 'not sent');
  }
  finally{ sending=false; sendBtn.disabled=false; sendBtn.classList.remove('busy'); }
}
sendBtn.onclick=sendText;
// Enter = new line (composer). Send with the button or Ctrl/Cmd+Enter (desktop).
tin.addEventListener('keydown', e=>{
  if(e.key==='Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); sendText(); }
});

// --- Draft (mterm_draft): field autosave with debounce, restored on load ---
let _draftT=null;
function saveDraft(){ try{
  if(tin.value) localStorage.setItem('mterm_draft', tin.value);
  else localStorage.removeItem('mterm_draft');
}catch(e){} } // Safari private mode throws on setItem: skip silently
tin.addEventListener('input', ()=>{ clearTimeout(_draftT); _draftT=setTimeout(saveDraft,300); });
try{
  const _d=localStorage.getItem('mterm_draft');
  if(_d && !tin.value){ tin.value=_d; autoGrow(); }
}catch(e){}

// --- Sent history (mterm_hist): newest first, consecutive dedup, cap 50x4000 ---
const HIST_MAX=50, HIST_ITEM_MAX=4000;
function histLoad(){ try{
  const a=JSON.parse(localStorage.getItem('mterm_hist')||'[]');
  return Array.isArray(a)?a.filter(x=>typeof x==='string'):[];
}catch(e){ return []; } }
function histPush(t){ try{
  if(!t || !t.trim()) return;
  if(t.length>HIST_ITEM_MAX) t=t.slice(0,HIST_ITEM_MAX)+'…';
  const a=histLoad();
  if(a[0]===t) return;               // skip consecutive duplicates
  a.unshift(t);
  localStorage.setItem('mterm_hist', JSON.stringify(a.slice(0,HIST_MAX)));
}catch(e){} }

// --- History popup: button in the field corner, pager, Insert ---
const histBtn=document.querySelector('.hist-btn'), histPop=document.querySelector('.hist-pop'),
      histOvl=document.querySelector('.hist-ovl'), histTxtEl=document.querySelector('.hist-text'),
      histPosEl=document.querySelector('.hist-pos'), histPrev=document.querySelector('.hist-prev'),
      histNext=document.querySelector('.hist-next'), histIns=document.querySelector('.hist-insert'),
      histX=document.querySelector('.hist-x');
let histIdx=0, histArr=[];
function histRender(){
  if(!histArr.length){
    histTxtEl.textContent='empty'; histPosEl.textContent='0/0';
    histPrev.disabled=histNext.disabled=histIns.disabled=true; return;
  }
  if(histIdx<0) histIdx=0;
  if(histIdx>histArr.length-1) histIdx=histArr.length-1;
  histTxtEl.textContent=histArr[histIdx];   // textContent: data never goes into innerHTML
  histTxtEl.scrollTop=0;
  histPosEl.textContent=(histIdx+1)+'/'+histArr.length;
  histPrev.disabled=(histIdx===0);
  histNext.disabled=(histIdx===histArr.length-1);
  histIns.disabled=false;
}
function histOpen(){
  histArr=histLoad(); histIdx=0; histRender();
  const r=document.getElementById('inputbar').getBoundingClientRect();
  histPop.style.bottom=Math.max(8, window.innerHeight-r.top+8)+'px'; // above the inputbar
  histPop.classList.remove('hist-hidden'); histOvl.classList.remove('hist-hidden');
}
function histClose(){ histPop.classList.add('hist-hidden'); histOvl.classList.add('hist-hidden'); }
histBtn.addEventListener('pointerdown', e=>e.preventDefault()); // do not dismiss the keyboard
histBtn.onclick=()=>{ histPop.classList.contains('hist-hidden') ? histOpen() : histClose(); };
histPrev.onclick=()=>{ histIdx--; histRender(); };
histNext.onclick=()=>{ histIdx++; histRender(); };
histX.onclick=histClose;
histOvl.onclick=histClose;
histIns.onclick=()=>{
  const t=histArr[histIdx]; if(t==null) return;
  const cur=tin.value;
  if(cur && cur.trim() && cur!==t) histPush(cur); // a non-empty draft also goes to history
  tin.value=t; autoGrow(); saveDraft();           // no focus here (keyboard must not pop up)
  histClose();
};

document.getElementById('add').onclick=()=>{
  newmenu.classList.contains('fp-hidden') ? openNewMenu() : closeNewMenu();
};
function reloadTerm(){ document.getElementById('term').src='term/?_='+Date.now(); }
document.getElementById('reload').onclick=reloadTerm;

// --- rename session by tapping the bar title + auto-name ---
(function(){
  const tnEl=document.getElementById('tt-tn');
  const NAME_SANI=/[^\p{L}\p{N} _.\-]/gu;
  // index of the ACTIVE window (the bar always shows the active session). lastActive is
  // a global from refresh()/select(); fallback: the last windows snapshot.
  function activeIdx(){
    // lastActive is a global from refresh()/select(): index of the active window.
    return (typeof lastActive==='number' && lastActive!=null) ? lastActive : null;
  }
  function afterRename(){ if(typeof refresh==='function') refresh();
    if(window.ttRefreshList) window.ttRefreshList(); }

  // inline name editing (contenteditable). Enter/blur saves, Esc cancels.
  let editing=false;
  function startEdit(){
    if(editing) return; const idx=activeIdx(); if(idx==null) return;
    editing=true; const orig=tnEl.textContent;
    tnEl.classList.add('tt-edit'); tnEl.contentEditable='true';
    tnEl.focus();
    try{ const r=document.createRange(); r.selectNodeContents(tnEl);
      const s=getSelection(); s.removeAllRanges(); s.addRange(r); }catch(e){}
    function finish(save){
      if(!editing) return; editing=false;
      tnEl.contentEditable='false'; tnEl.classList.remove('tt-edit');
      tnEl.removeEventListener('keydown',onKey); tnEl.removeEventListener('blur',onBlur);
      let nn=(tnEl.textContent||'').replace(NAME_SANI,'').trim().slice(0,24);
      if(!save || !nn || nn===orig){ tnEl.textContent=orig; return; }
      tnEl.textContent=nn;
      api('rename?w='+activeIdx()+'&name='+encodeURIComponent(nn), {method:'POST'})
        .then(afterRename).catch(()=>{ tnEl.textContent=orig; toast('Rename failed'); });
    }
    function onKey(e){ if(e.key==='Enter'){ e.preventDefault(); finish(true); }
      else if(e.key==='Escape'){ e.preventDefault(); finish(false); } }
    function onBlur(){ finish(true); }
    tnEl.addEventListener('keydown',onKey); tnEl.addEventListener('blur',onBlur);
  }
  tnEl.addEventListener('click', startEdit);
})();

// --- auto-reconnect: remove the ttyd 'Press Enter to Reconnect' overlay ---
// the terminal lives in a same-origin iframe; its text is drawn on canvas, so any
// DOM text '...to Reconnect'/'Connection Closed' means a stuck ttyd overlay.
let _lastReload=0;
// reconnect ONLY for a visible page: a background tab must not
// hijack the session and start a client war
function autoReload(){ if(document.hidden) return;
  const n=Date.now(); if(n-_lastReload>8000){ _lastReload=n; reloadTerm(); } }
// reload the iframe ONLY when the connection is really dead (stuck ttyd
// '...to Reconnect'/'Connection Closed' overlay). Otherwise every reload = a new
// tmux attach, and the old one does not die across the docker boundary -> zombie clients
// pile up and fight for the screen = duplicated Claude/Codex frames. So
// visibilitychange/online no longer trigger a blind reload - they only check
// whether the terminal is alive and fix it only if it is dead.
function termIsDead(){
  try{
    const d=document.getElementById('term').contentDocument;
    return !!(d && /to Reconnect|Connection Closed/i.test(d.body.innerText||''));
  }catch(e){ return false; }
}
function reloadIfDead(){ if(!document.hidden && termIsDead()) autoReload(); }
setInterval(reloadIfDead, 2000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) reloadIfDead(); });
window.addEventListener('online', reloadIfDead);

// --- terminal swipe scroll: touch -> synthetic WheelEvent in xterm.js ---
// xterm.js 5.3 (inside ttyd) has no touch support (xtermjs#1007/#5377), but it handles
// wheel without an isTrusted check. ttyd exports window.term.
// A swipe is converted to wheel on term.element: xterm sends tmux (mouse on)
// SGR codes - tmux scrolls copy-mode, or passes them on to Claude (fullscreen).
(function(){
  const ifr=document.getElementById('term');
  const STEP=16;   // finger px per wheel tick; a tick = 1 line (bind in
                   // .tmux.conf), line height ~16px -> the finger moves ~1:1
  const TAP=12;    // less than this is a tap (focus/click), not a swipe
  const dbg=window._swdbg={hooks:0,start:0,move:0,tick:0,noterm:0};  // for uitest
  function hook(){
    let idoc, iwin;
    try{ idoc=ifr.contentDocument; iwin=ifr.contentWindow; }catch(e){ return; }
    if(!idoc || !iwin || idoc._swipeHooked) return;
    idoc._swipeHooked=true; dbg.hooks++;
    let y0=0, lastY=0, lastT=0, acc=0, armed=false, vel=0, mom=null;
    function tick(dir){
      const term=iwin.term;
      if(!term || !term.element){ dbg.noterm++; return; }
      const scr=idoc.querySelector('.xterm-screen');
      if(!scr){ dbg.noterm++; return; }
      const r=scr.getBoundingClientRect();
      term.element.dispatchEvent(new iwin.WheelEvent('wheel',{
        deltaY:dir*STEP, deltaMode:0,
        clientX:r.left+r.width/2, clientY:r.top+r.height/2,
        bubbles:true, cancelable:true}));
      dbg.tick++;
    }
    function stopMom(){ if(mom){ cancelAnimationFrame(mom); mom=null; } }
    idoc.addEventListener('touchstart', e=>{
      dbg.start++;
      stopMom(); y0=lastY=e.touches[0].clientY; lastT=performance.now();
      acc=0; vel=0; armed=false;
    }, {passive:true});
    idoc.addEventListener('touchmove', e=>{
      dbg.move++;
      const y=e.touches[0].clientY, t=performance.now();
      if(!armed){ if(Math.abs(y0-y)<TAP) return; armed=true; }
      const dy=lastY-y;
      if(t>lastT) vel=0.8*vel+0.2*(dy/(t-lastT));
      lastY=y; lastT=t; acc+=dy;
      while(Math.abs(acc)>=STEP){ const d=Math.sign(acc); acc-=d*STEP; tick(d); }
      e.preventDefault();   // suppress Safari rubber-band scroll/zoom
    }, {passive:false});
    idoc.addEventListener('touchend', ()=>{
      if(!armed || Math.abs(vel)<0.35) return;       // inertia only after a fling
      let v=Math.max(-28, Math.min(28, vel*16)), carry=0;  // px per frame (~16ms), soft cap
      (function step(){
        carry+=v; v*=0.92;
        while(Math.abs(carry)>=STEP){ const d=Math.sign(carry); carry-=d*STEP; tick(d); }
        mom = Math.abs(v)>1 ? requestAnimationFrame(step) : null;
      })();
    }, {passive:true});
  }
  ifr.addEventListener('load', ()=>setTimeout(hook, 300));
  setInterval(hook, 2000);  // safety net: first pass and iframe auto-reload
})();

// ===== OSC 52: copy from the terminal to the browser clipboard =====
// Claude Code -> tmux (set-clipboard on) -> ttyd -> xterm.js: ESC ] 52 ; c ; base64.
// By default xterm.js does NOT handle OSC 52 (xtermjs#3260) and silently
// drops it; ttyd 1.7.4 has no handler either (clipboard-addon appeared
// in newer ttyd). ttyd exposes window.term in a same-origin iframe, so
// we register the handler from here, the same way as the swipe bridge above.
// Clipboard writes need a secure context and usually a fresh gesture, while OSC 52
// arrives over websocket asynchronously. Cascade: navigator.clipboard.writeText
// -> execCommand('copy') in the iframe document -> 'arming': the text waits for
// the next click/key and the user sees a toast. We never fail silently.
(function(){
  const ifr=document.getElementById('term');
  const dbg=window._cbdbg={hooks:0,osc:0,ok:0,legacy:0,armed:0,fail:0,last:''};  // for uitest
  let pending=null, disarm=null;

  function b64utf8(s){
    // atob returns latin1 bytes; a naive atob corrupts UTF-8 (non-Latin text)
    const bin=atob(s), bytes=new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    return new TextDecoder('utf-8').decode(bytes);
  }
  function cbToast(msg, ms){
    const e=document.getElementById('toast');
    e.textContent=msg; e.classList.add('show');
    clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('show'), ms||1400);
  }
  function legacyCopy(text){
    // hidden textarea in the IFRAME document: focus stays in the terminal
    try{
      const d=ifr.contentDocument||document;
      const ta=d.createElement('textarea');
      ta.value=text;
      ta.style.cssText='position:fixed;top:0;left:0;width:2px;height:2px;opacity:0';
      d.body.appendChild(ta);
      const prev=d.activeElement;
      ta.focus({preventScroll:true}); ta.select();
      const ok=d.execCommand('copy');
      ta.remove();
      if(prev&&prev.focus) try{ prev.focus({preventScroll:true}); }catch(e){}
      return ok;
    }catch(e){ return false; }
  }
  function armGesture(text){
    // the browser rejected the write outside a gesture: catch the next click/key
    pending=text; dbg.armed++;
    cbToast('Text ready: click or press a key to copy', 8000);
    if(disarm) disarm();
    const targets=[document];
    try{ if(ifr.contentDocument) targets.push(ifr.contentDocument); }catch(e){}
    function off(){ targets.forEach(d=>{ d.removeEventListener('pointerdown',h,true); d.removeEventListener('keydown',h,true); }); disarm=null; }
    function done(){ pending=null; off(); dbg.ok++; cbToast('Copied'); }
    function h(){
      const t=pending; if(t==null){ off(); return; }
      // we are inside a gesture: modern API first, then execCommand
      navigator.clipboard.writeText(t).then(done, ()=>{
        if(legacyCopy(t)){ pending=null; off(); dbg.legacy++; cbToast('Copied'); }
        else { pending=null; off(); dbg.fail++; cbToast('Clipboard unavailable in this browser'); }
      });
    }
    targets.forEach(d=>{ d.addEventListener('pointerdown',h,true); d.addEventListener('keydown',h,true); });
    disarm=off;
  }
  function deliver(text){
    dbg.last=text.slice(0,80);
    if(navigator.clipboard&&window.isSecureContext){
      navigator.clipboard.writeText(text).then(
        ()=>{ dbg.ok++; cbToast('Copied'); },
        ()=>{ if(legacyCopy(text)){ dbg.legacy++; cbToast('Copied'); } else armGesture(text); });
    } else if(legacyCopy(text)){ dbg.legacy++; cbToast('Copied'); }
    else armGesture(text);
  }
  function onOsc52(data){
    // payload after '52;': '<Pc>;<base64>', Pc = targets (c,p,s,0-7)
    dbg.osc++;
    const i=data.indexOf(';');
    if(i<0) return true;
    const b64=data.slice(i+1);
    if(b64==='?') return true;          // clipboard read request: not supported
    let text='';
    try{ text=b64utf8(b64.replace(/\s/g,'')); }catch(e){ return true; }
    if(text) deliver(text);
    return true;                        // sequence handled
  }
  function hook(){
    let iwin;
    try{ iwin=ifr.contentWindow; }catch(e){ return; }
    if(!iwin||iwin._osc52Hooked) return;
    const term=iwin.term;
    if(!term||!term.parser) return;     // ttyd is still loading: the next tick will retry
    iwin._osc52Hooked=true;             // the flag lives in the iframe window: reload = re-hook
    term.parser.registerOscHandler(52, onOsc52);
    dbg.hooks++;
  }
  ifr.addEventListener('load', ()=>setTimeout(hook, 300));
  setInterval(hook, 2000);
})();

// Pin the app to the visible viewport (area above the soft keyboard) so iOS
// Safari can't scroll-to-focus and fling the input bar off the top.
const app=document.getElementById('app');
function fitViewport(){
  const vv=window.visualViewport;
  app.style.height=(vv?vv.height:window.innerHeight)+'px';
  // toast above the keyboard (fixed positions by the layout viewport)
  const kb=vv? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
  document.getElementById('toast').style.bottom='calc('+(78+kb)+'px + env(safe-area-inset-bottom))';
  window.scrollTo(0,0);
}
if(window.visualViewport){
  visualViewport.addEventListener('resize', fitViewport);
  visualViewport.addEventListener('scroll', fitViewport);
}
window.addEventListener('resize', fitViewport);
window.addEventListener('orientationchange', ()=>setTimeout(fitViewport,200));
tin.addEventListener('focus', ()=>setTimeout(fitViewport,250));
fitViewport();

// ===== TGV: mobile tile mode =====
// One shared interval for both views (grid/strip); polling ONLY while open;
// skip the tick when document.hidden. <pre> preview strictly via textContent.
(function(){
  const grid=document.getElementById('tgv-grid'),
        cells=document.getElementById('tgv-cells'),
        strip=document.getElementById('tgv-strip'),
        stripBg=document.getElementById('tgv-strip-bg'),
        row=document.getElementById('tgv-row'),
        pill=document.getElementById('tgv-pill'),
        gbtn=document.getElementById('gridbtn');
  let mode=null;            // null | 'grid' | 'strip'
  let timer=null, sig='';
  let dragging=false;       // a tile drag is in progress: polling does not redraw the grid

  function tile(w){
    const t=document.createElement('div');
    t.className='tgv-tile'+(w.active?' tgv-active':'');
    t.dataset.idx=w.index;
    if(w.id) t.dataset.id=w.id;
    const h=document.createElement('div'); h.className='tgv-title';
    const ht=document.createElement('span'); ht.textContent=w.index+': '+w.name; h.appendChild(ht);  // indexes are NOT contiguous
    const dot=document.createElement('span'); dot.className=tindClass(w.st); dot.style.marginLeft='auto'; h.appendChild(dot);
    const p=document.createElement('pre'); p.className='tgv-prev';
    p.textContent=(w.lines||[]).join('\n');            // textContent only!
    t.appendChild(h); t.appendChild(p);
    t.onclick=()=>{ if(t._dragged) return;             // tap after a drag: do not switch
      tgvClose(); select(w.index); };                  // tap = switch + exit
    attachTileDrag(t);
    return t;
  }
  // commit the new tile order: same reorder POST as for the tabs.
  // After a reorder tmux RENUMBERS windows, so old indexes are invalid:
  // from the response we update the tab strip, reset the signature and force fresh previews.
  async function commitTileOrder(order){
    await winsMutate(async()=>{
    try{
      const r=await api('reorder?order='+order.join(','), {method:'POST'});
      if(r && r.error) toast(r.error);
      else if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows,false); }
    }catch(e){ toast('Reorder failed'); }
    });
    sig=''; poll();
  }
  // long-press 300ms on a tile -> drag: the tile lifts and sticks to the finger,
  // the target under the finger is highlighted, drop = insert BEFORE the target (below
  // the last = to the end). Movement >10px BEFORE 300ms = normal grid scroll.
  function attachTileDrag(t){
    t.addEventListener('contextmenu',(e)=>{ e.preventDefault(); }); // long-press without a menu
    t.addEventListener('pointerdown',(e)=>{
      if(mode!=='grid') return;                        // drag only in the grid (not in the strip)
      const sx=e.clientX, sy=e.clientY;
      let drag=false, hint=null;
      t._dragged=false;
      const lp=setTimeout(()=>{
        if(!t.isConnected) return;                     // the tile was redrawn before the long-press
        drag=true; dragging=true; t._dragged=true;
        t.classList.add('tgv-drag'); t.style.pointerEvents='none'; // elementFromPoint sees the neighbours
        t.style.transform='translate(0px,0px) scale(1.04)';
        if(navigator.vibrate) navigator.vibrate(18);
      },300);
      function setHint(el){
        if(hint===el) return;
        if(hint) hint.classList.remove('tgv-drophint');
        hint=el; if(hint) hint.classList.add('tgv-drophint');
      }
      // .tgv-grid has touch-action:pan-y: if the browser starts a pan, a
      // pointercancel arrives and the drag dies. While the tile is on the finger, block the pan manually.
      function tm(ev){ if(drag) ev.preventDefault(); }
      function cleanup(){
        clearTimeout(lp); setHint(null); dragging=false;
        t.classList.remove('tgv-drag'); t.style.pointerEvents=''; t.style.transform='';
        document.removeEventListener('pointermove',move,true);
        document.removeEventListener('pointerup',up,true);
        document.removeEventListener('pointercancel',cancel,true);
        document.removeEventListener('touchmove',tm,true);
        setTimeout(()=>{ t._dragged=false; },60);      // suppress the click after a drag
      }
      function move(ev){
        if(!drag){                                     // still waiting for the long-press:
          if(Math.abs(ev.clientX-sx)>10 || Math.abs(ev.clientY-sy)>10) clearTimeout(lp);
          return;                                      // finger moved = scroll as before
        }
        ev.preventDefault();
        t.style.transform='translate('+(ev.clientX-sx)+'px,'+(ev.clientY-sy)+'px) scale(1.04)';
        const g=grid.getBoundingClientRect();
        if(ev.clientX<g.left||ev.clientX>g.right||ev.clientY<g.top||ev.clientY>g.bottom)
          return cleanup();                            // finger left the overlay = cancel
        const el=document.elementFromPoint(ev.clientX, ev.clientY);
        const over=el && el.closest('.tgv-tile');
        setHint((over && over!==t && over.parentNode===cells)? over : null);
      }
      function cancel(){ cleanup(); }                  // pointercancel = restore as it was
      function up(ev){
        if(!drag){ cleanup(); return; }                // plain tap: onclick will fire
        const el=document.elementFromPoint(ev.clientX, ev.clientY);
        const over=el && el.closest('.tgv-tile');
        const target=(over && over!==t && over.parentNode===cells)? over : null;
        const tiles=[...cells.querySelectorAll('.tgv-tile')];
        const cur=tiles.map(x=>+x.dataset.idx);
        const from=+t.dataset.idx;
        let order=null;
        if(target){                                    // insert BEFORE the target
          order=cur.filter(i=>i!==from);
          order.splice(order.indexOf(+target.dataset.idx),0,from);
        } else {
          const last=tiles[tiles.length-1];
          if(last && last!==t && ev.clientY>last.getBoundingClientRect().bottom){
            order=cur.filter(i=>i!==from); order.push(from);   // below the last = to the end
          }
        }
        cleanup();
        if(order && order.join(',')!==cur.join(',')) commitTileOrder(order);
      }
      document.addEventListener('pointermove',move,true);
      document.addEventListener('pointerup',up,true);
      document.addEventListener('pointercancel',cancel,true);
      document.addEventListener('touchmove',tm,{passive:false,capture:true});
    });
  }
  function renderTiles(wins){
    const host = (mode==='grid') ? cells : row;
    host.innerHTML='';
    for(const w of wins) host.appendChild(tile(w));
  }
  async function poll(){
    if(document.hidden || !mode || dragging) return;   // skip the tick during a drag
    try{
      const d=await api('previews');
      const wins=(d && d.windows)||[];
      // st is NOT in the signature: a state-only change does not rebuild tiles
      // (grid scroll stays), dots are patched by updateIndicators.
      const s=JSON.stringify(wins.map(w=>[w.index,w.name,w.active,w.lines]));
      if(s!==sig){ sig=s; renderTiles(wins); }         // on change: scroll stays intact
      updateIndicators(wins);
    }catch(e){ /* keep last tiles */ }
  }
  function stopPoll(){ if(timer){ clearInterval(timer); timer=null; } }
  function startPoll(){ stopPoll(); sig=''; poll(); timer=setInterval(poll, 3000); }
  function tgvClose(){
    mode=null; stopPoll();
    grid.classList.remove('tgv-on'); document.body.classList.remove('tgv-open');
    strip.classList.remove('tgv-on'); stripBg.classList.remove('tgv-on');
  }
  function openGrid(){                                 // the grid closes the strip
    tgvClose(); mode='grid';
    grid.classList.add('tgv-on'); document.body.classList.add('tgv-open');
    startPoll();
  }
  function openStrip(){                                // the strip closes the grid
    tgvClose(); mode='strip';
    strip.style.height=Math.round(app.clientHeight/2)+'px';  // 50% of #app height
    strip.classList.add('tgv-on'); stripBg.classList.add('tgv-on');
    startPoll();
  }
  gbtn.onclick=()=>{ (mode==='grid') ? tgvClose() : openGrid(); };
  document.getElementById('tgv-close').onclick=tgvClose;
  pill.onclick=()=>{ (mode==='strip') ? tgvClose() : openStrip(); };
  stripBg.onclick=tgvClose;                            // tap below the strip = close
})();

// ===== Settings + bar collapse + tabs/ruler at the bottom =====
(function(){
  const bar=document.getElementById('bar');
  const bcBtn=document.getElementById('barcollapse');
  const tabsbar=document.getElementById('set-tabsbar');
  const memEl=document.getElementById('mem');
  const pop=document.getElementById('set-pop');
  const ovl=document.querySelector('.set-ovl');
  const listEl=pop.querySelector('.set-list');
  const KEY_COLLAPSE='mterm_barcollapsed';
  const KEY_TABS_BOTTOM='mterm_tabs_bottom';
  const KEY_ACTIONS_BOTTOM='mterm_actions_bottom';
  // new chat-list settings
  const KEY_NOPREVIEW='mterm_hide_preview';   // OFF = hide the preview line
  const KEY_COMPACT='mterm_compact_list';     // compact row density

  // applying the new list settings: class on body + CSS above
  function applyNoPreview(on){ document.body.classList.toggle('tt-nopreview', on); }
  function applyCompact(on){ document.body.classList.toggle('tt-compact', on); }

  // -- FLAT registry of all toggles (for applyAll: restore on load) --
  const SETTINGS=[
    { key:KEY_NOPREVIEW, label:'Show preview', apply:applyNoPreview, invert:true },
    { key:KEY_COMPACT, label:'Compact list', apply:applyCompact },
    { key:KEY_TABS_BOTTOM, label:'Tabs at bottom', apply:applyTabsBottom },
    { key:KEY_ACTIONS_BOTTOM, label:'Buttons at bottom', apply:applyActionsBottom },
    { key:'mterm_strip_bottom', label:'Tile strip at bottom', apply:applyStripBottom },
    { key:KEY_COLLAPSE, label:'Buttons collapsed', apply:applyCollapsed },
  ];
  const get=(k)=> localStorage.getItem(k)==='1';
  const set=(k,v)=>{ v ? localStorage.setItem(k,'1') : localStorage.removeItem(k); };

  // -- SECTIONS (iOS Settings layout). Easy to extend: add a row to items.
  //    Row types: 'srv-agent' | 'srv-folder' (server) | 'toggle' (localStorage).
  //    invert:true -> toggle ON = key CLEARED (for 'Show preview': on by default).
  //    More could be added later: 'Mobile 2.0 / Classic', quick commands,
  //    favorite folder order - just new items/sections, the engine renders them. --
  const SECTIONS=[
    { title:'Design', note:'Mobile session list (2.0) or the classic tab bar (for PC).', items:[
      { type:'design', label:'Design' },
    ]},
    { title:'Tutorial', note:'Step-by-step UI tour highlighting real elements.', items:[
      { type:'guide', label:'Interface guide' },
    ]},
    { title:'Defaults', note:'What to open in a new session.', items:[
      { type:'srv-agent',  label:'Default agent' },
      { type:'srv-folder', label:'Default folder' },
    ]},
    { title:'Appearance', items:[
      { type:'toggle', key:KEY_NOPREVIEW, label:'Show preview',
        sub:'Last output line under the session name', invert:true },
      { type:'toggle', key:KEY_COMPACT, label:'Compact list',
        sub:'Tighter rows, smaller avatars' },
    ]},
    { title:'Quick commands', note:'Command buttons under the menu in the input field. Use the cross to remove, plus to add.', items:[
      { type:'quickcmds' },
    ]},
    { title:'Classic mode', note:'Layout of the old (PC) terminal interface.', items:[
      { type:'toggle', key:KEY_TABS_BOTTOM,       label:'Tabs at bottom' },
      { type:'toggle', key:KEY_ACTIONS_BOTTOM,    label:'Buttons at bottom' },
      { type:'toggle', key:'mterm_strip_bottom',  label:'Tile strip at bottom' },
      { type:'toggle', key:KEY_COLLAPSE,          label:'Buttons collapsed' },
    ]},
  ];

  const actions=document.querySelector('.bar-actions');

  // Show/hide the bottom bar: visible if there is ANYTHING at the bottom (tabs OR buttons)
  function syncTabsbarVisible(){
    tabsbar.hidden = !(get(KEY_TABS_BOTTOM) || get(KEY_ACTIONS_BOTTOM));
  }
  // Stable order of the bottom bar children: arrow, then buttons, then tabs (RIGHT),
  // as at the top. Appending in turn those actually in the bar sets the order.
  function normalizeTabsbarOrder(){
    const tabs=document.getElementById('tabs');
    if(bcBtn && bcBtn.parentNode===tabsbar) tabsbar.appendChild(bcBtn);         // 1) arrow on the left
    if(actions && actions.parentNode===tabsbar) tabsbar.appendChild(actions);   // 2) buttons
    if(tabs && tabs.parentNode===tabsbar) tabsbar.appendChild(tabs);            // 3) tabs on the right
  }

  // --- feature 1/3-collapse: collapse buttons (class on the .bar-actions node itself - works in any container) ---
  function applyCollapsed(on){
    // arrow rotation: by the class on the #barcollapse button itself (works at the top and in the bottom bar)
    bcBtn.classList.toggle('bc-collapsed', on);
    if(actions) actions.classList.toggle('actions-hidden', on);
    bcBtn.setAttribute('aria-label', on ? 'Expand buttons' : 'Collapse buttons');
  }
  // --- feature 3: move the #tabs node down/up (the node itself, not a copy) ---
  function applyTabsBottom(on){
    const tabs=document.getElementById('tabs');
    if(on){ syncTabsbarVisible(); if(tabs.parentNode!==tabsbar) tabsbar.appendChild(tabs); }
    else {
      // return #tabs to #bar at its previous place (before #mem)
      if(tabs.parentNode!==bar) bar.insertBefore(tabs, memEl);
      syncTabsbarVisible();
    }
    normalizeTabsbarOrder();
    // scroll to the active tab after the move
    const act=(tabs.querySelector('.tab.active'));
    if(act) act.scrollIntoView({inline:'nearest',block:'nearest'});
  }
  // --- feature 'Buttons at bottom': move the .bar-actions node down/up (the node itself, handlers untouched) ---
  function applyActionsBottom(on){
    if(!actions) return;
    if(on){
      syncTabsbarVisible();
      // the arrow moves WITH the buttons (but stays a SEPARATE sibling, not inside .bar-actions,
      // otherwise collapsing would hide the arrow itself and there would be nothing to expand with)
      if(bcBtn.parentNode!==tabsbar) tabsbar.appendChild(bcBtn);
      if(actions.parentNode!==tabsbar) tabsbar.appendChild(actions);
    } else {
      // return the arrow to the very START of #bar, then the buttons right after it
      if(bcBtn.parentNode!==bar) bar.insertBefore(bcBtn, bar.firstChild);
      if(actions.parentNode!==bar) bar.insertBefore(actions, bcBtn.nextSibling);
      syncTabsbarVisible();
    }
    normalizeTabsbarOrder();
  }
  // --- feature 3: tile ruler at the bottom (class on #tgv-strip) ---
  function applyStripBottom(on){
    const strip=document.getElementById('tgv-strip');
    if(strip) strip.classList.toggle('tgv-strip-bottom', on);
  }

  function applyAll(){ for(const s of SETTINGS) s.apply(get(s.key)); }

  // --- server settings (default_cmd/default_cwd), persist across devices ---
  const AGENT_OPTS=__AGENT_OPTS__;
  let _srvSet={default_cmd:'shell', default_cwd:HOME_DIR};
  async function loadSrvSet(){ try{ _srvSet=await api('settings'); }catch(e){} }
  async function patchSrvSet(patch){
    try{ _srvSet=await api('settings', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)}); }
    catch(e){ toast('Setting was not saved'); }
  }
  // pick the default folder via the existing 'Open folder' modal
  // (reuse /m/api/ls/favs). The hook intercepts confirmOpen while open.
  window.ttPickDefaultFolder=function(){
    if(typeof window.openFolderPicker!=='function'){ toast('Picker unavailable'); return; }
    window.__ttFolderTarget='default_cwd';
    window.openFolderPicker(_srvSet.default_cwd||HOME_DIR);
  };

  // chevron for picker rows (iOS)
  const CHEV='<svg viewBox="0 0 9 15" fill="none"><path d="M1.5 1.5L7 7.5L1.5 13.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  // row with a title (+ caption) on the left
  function mkLabel(item){
    const lab=document.createElement('span'); lab.className='set-label'; lab.textContent=item.label;
    if(item.sub){ const s=document.createElement('span'); s.className='set-sub'; s.textContent=item.sub; lab.appendChild(s); }
    return lab;
  }

  // --- render the grouped list (iOS Settings sections) ---
  function renderList(){
    listEl.innerHTML='';
    for(const sec of SECTIONS){
      const secEl=document.createElement('div'); secEl.className='set-sec';
      if(sec.title){ const h=document.createElement('div'); h.className='set-sechd'; h.textContent=sec.title; secEl.appendChild(h); }
      const card=document.createElement('div'); card.className='set-card';

      for(const item of sec.items){
        if(item.type==='srv-agent'){
          const row=document.createElement('div'); row.className='set-row';
          const sel=document.createElement('select'); sel.className='set-select';
          for(const [v,l] of AGENT_OPTS){ const o=document.createElement('option'); o.value=v; o.textContent=l;
            if(v===_srvSet.default_cmd) o.selected=true; sel.appendChild(o); }
          sel.onchange=()=>patchSrvSet({default_cmd:sel.value});
          row.appendChild(mkLabel(item)); row.appendChild(sel); card.appendChild(row);

        } else if(item.type==='srv-folder'){
          const row=document.createElement('div'); row.className='set-row';
          const val=document.createElement('span'); val.className='set-val';
          const shortCwd=(p=>p.indexOf(HOME_DIR)===0?'~'+p.slice(HOME_DIR.length):p)(_srvSet.default_cwd||HOME_DIR);
          val.textContent=shortCwd; row.title=_srvSet.default_cwd||'';
          const chev=document.createElement('span'); chev.className='set-chev'; chev.innerHTML=CHEV;
          row.appendChild(mkLabel(item)); row.appendChild(val); row.appendChild(chev);
          row.onclick=()=>{ closeSet(); window.ttPickDefaultFolder(); };
          card.appendChild(row);

        } else if(item.type==='guide'){
          const row=document.createElement('div'); row.className='set-row';
          const chev=document.createElement('span'); chev.className='set-chev'; chev.innerHTML=CHEV;
          row.appendChild(mkLabel(item)); row.appendChild(chev);
          row.onclick=()=>{ closeSet(); if(window.gdStart) window.gdStart(0); };
          card.appendChild(row);

        } else if(item.type==='design'){
          const row=document.createElement('div'); row.className='set-row';
          const val=document.createElement('span'); val.className='set-val';
          const classic=ttIsClassic();   // tri-state: '1' classic, '0' mobile, no key = auto
          val.textContent = (classic ? 'Classic (PC)' : 'Mobile 2.0') + (ttModeIsAuto() ? ' · auto' : '');
          const chev=document.createElement('span'); chev.className='set-chev'; chev.innerHTML=CHEV;
          row.appendChild(mkLabel(item)); row.appendChild(val); row.appendChild(chev);
          row.onclick=()=>{
            // always write an EXPLICIT value opposite to the current effective one: manual choice beats auto
            localStorage.setItem('mterm_classic', ttIsClassic() ? '0' : '1');
            location.reload();   // clean mode switch
          };
          card.appendChild(row);

        } else if(item.type==='quickcmds'){        } else if(item.type==='quickcmds'){
          const cmds=(window.getQuickCmds?window.getQuickCmds():[]);
          cmds.forEach((c,i)=>{
            const row=document.createElement('div'); row.className='set-row';
            const lab=document.createElement('span'); lab.className='set-label';
            lab.textContent=c; lab.style.fontFamily='ui-monospace,Menlo,monospace';
            const del=document.createElement('span'); del.className='set-chev';
            del.textContent='✕'; del.style.color='var(--tt-wait)';
            del.onclick=(e)=>{ e.stopPropagation();
              window.setQuickCmds((window.getQuickCmds()||[]).filter((_,j)=>j!==i)); renderList(); };
            row.appendChild(lab); row.appendChild(del); card.appendChild(row);
          });
          const add=document.createElement('div'); add.className='set-row';
          const al=document.createElement('span'); al.className='set-label';
          al.textContent='+ Add command'; al.style.color='var(--tt-accent)';
          add.appendChild(al);
          add.onclick=()=>{ let v=prompt('Command (e.g. /compact):'); if(v && v.trim()){
            v=v.trim(); if(!v.startsWith('/')) v='/'+v;
            const a=(window.getQuickCmds()||[]); a.push(v); window.setQuickCmds(a); renderList(); } };
          card.appendChild(add);

        } else { // toggle
          const on=item.invert ? !get(item.key) : get(item.key);   // toggle display
          const row=document.createElement('div');
          row.className='set-row'+(on?' set-on':'');
          row.setAttribute('role','switch'); row.setAttribute('aria-checked', on?'true':'false');
          const sw=document.createElement('span'); sw.className='set-switch';
          row.appendChild(mkLabel(item)); row.appendChild(sw);
          row.onclick=()=>{
            const nowOn=!row.classList.contains('set-on');   // new toggle state
            const stored=item.invert ? !nowOn : nowOn;       // what to write to the key
            set(item.key, stored);
            row.classList.toggle('set-on', nowOn); row.setAttribute('aria-checked', nowOn?'true':'false');
            // apply gets the RAW key value (as applyAll on load)
            (SETTINGS.find(s=>s.key===item.key)||item).apply(stored);
          };
          card.appendChild(row);
        }
      }
      secEl.appendChild(card);
      if(sec.note){ const n=document.createElement('div'); n.className='set-secnote'; n.textContent=sec.note; secEl.appendChild(n); }
      listEl.appendChild(secEl);
    }
  }
  let _setCloseT=null;
  function openSet(){
    if(_setCloseT){ clearTimeout(_setCloseT); _setCloseT=null; }
    loadSrvSet().then(()=>{ if(!pop.classList.contains('hist-hidden')) renderList(); });
    renderList();
    // remove display:none, then in the NEXT frame remove transform -> the spring works
    pop.classList.remove('hist-hidden'); ovl.classList.remove('hist-hidden');
    void pop.offsetHeight;                 // force reflow: start from translateY(100%)/opacity 0
    requestAnimationFrame(()=>{ pop.classList.add('set-open'); ovl.classList.add('set-open'); });
  }
  function closeSet(){
    pop.classList.remove('set-open');       // sheet slides down (transition on transform)
    ovl.classList.remove('set-open');       // blur fades (opacity transition)
    // hide completely (display:none) only AFTER the animation
    if(_setCloseT) clearTimeout(_setCloseT);
    _setCloseT=setTimeout(()=>{ pop.classList.add('hist-hidden'); ovl.classList.add('hist-hidden'); _setCloseT=null; }, 460);
  }
  window.ttOpenSettings=openSet;   // gear button on the list screen
  window.ttSrvSet=()=>_srvSet;     // current server settings (for the FAB menu)

  memEl.addEventListener('click', openSet);
  memEl.addEventListener('keydown', (e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openSet(); } });
  ovl.addEventListener('click', closeSet);
  pop.querySelector('.set-x').addEventListener('click', closeSet);

  // arrow <-> 'Buttons collapsed' toggle are synced through one key
  bcBtn.addEventListener('click', ()=>{
    const nv=!get(KEY_COLLAPSE); set(KEY_COLLAPSE, nv); applyCollapsed(nv);
    if(!pop.classList.contains('hist-hidden')) renderList();  // if the panel is open: refresh the row
  });

  applyAll();  // restore state on load
})();

// ============================================================
// tmux tabs 2.0 - session list screen (Telegram-iOS style) <-> terminal.
// #app = TERMINAL screen (existing, internals untouched).
// #tt-list-screen = LIST screen. Switched by the body.tt-term class.
// The list takes data from /m/api/previews?n=2 (windows + status + last lines).
// 'Enter chat' = select(index) (make active) + show the terminal;
// the iframe already shows the active window, no reload needed.
// ============================================================
(function(){
  if(!ttIsClassic())                                // 2.0 unless classic is chosen explicitly and this is not 'PC' by auto
    document.body.classList.add('tt-2');            // enable the two-screen mode
  const listEl=document.getElementById('tt-list');
  const foldersEl=document.getElementById('tt-folders');
  const subEl=document.getElementById('tt-subtitle');
  const termNameEl=document.getElementById('tt-termname');
  const AGENT_LABEL=__AGENT_LABEL__;

  // ---- state ----
  // WORK_FID is the virtual 'Working' folder: its membership is computed on the fly
  // (agent windows with an active status), it is NOT stored in folders.json.
  // Opened by default so working Claudes are visible from the phone at once.
  const WORK_FID='__work';
  let curFolder=WORK_FID;                         // folder filter (WORK_FID | 'all' | folderId)
  let winsCache=[];                               // latest windows from previews
  let foldersData={folders:[],members:{}};
  let _listSig='', _folSig='', _pollTimer=null;

  // ---- status: backend st = ask|work|done|idle -> UI wait|work|done|idle ----
  function uiSt(st){ return st==='ask' ? 'wait' : (st||'idle'); }
  const ST_TEXT={work:'working',wait:'waiting',done:'done',idle:'idle'};

  // ---- avatar: letter + color by name ----
  function avaInitial(name){
    const s=(name||'').trim();
    if(!s) return '•';
    // if it starts with an emoji/non-letter, take the first character as is
    const cp=Array.from(s)[0];
    return /[\p{L}\p{N}]/u.test(cp) ? cp.toUpperCase() : cp;
  }
  function avaColor(name){
    let h=0; const s=name||''; for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))>>>0;
    return 'g'+(h%7);
  }
  // ---- status badge ----
  function badge(st, idx){
    if(st==='wait') return '<div class="tt-badge hot">!</div>';
    if(st==='done') return '<div class="tt-badge go">✓</div>';
    if(st==='work') return '<div class="tt-badge run">▶</div>';
    return '<div class="tt-badge">'+idx+'</div>';
  }

  // ---- folder membership key: window_id like '@5' (as the backend stores it) ----
  function membersKey(w){ if(!w.id) return ''; return w.id[0]==='@' ? w.id : '@'+w.id; }
  // ---- 'Working': an agent runs in the window pane (w.cmd from pane_current_command;
  // claude matches both regular windows and Claude GLM) and it is working / waiting for
  // an answer / just finished - OR worked recently (act_ago from the backend). Freshness
  // is needed because for the ACTIVE window tmux clears 'done' instantly:
  // left the session -> the window stays active -> without freshness Claude would drop out
  // of the folder as soon as it goes quiet. Bare shells are excluded. ----
  const AGENT_CMDS=__AGENT_CMDS__;
  const FRESH_SECS=30*60;   // 'fresh claude': worked within the last 30 minutes
  function isWorking(w){
    if(!AGENT_CMDS[w.cmd]) return false;
    const st=uiSt(w.st);
    if(st==='work'||st==='wait'||st==='done') return true;
    return w.act_ago!=null && w.act_ago<FRESH_SECS;
  }
  // ---- list of windows in the current folder ----
  function inFolder(w){
    if(curFolder==='all') return true;
    if(curFolder===WORK_FID) return isWorking(w);
    const fids=foldersData.members[membersKey(w)] || [];
    return fids.indexOf(curFolder)>=0;
  }

  // ---- list render (dedup by signature: set/name/status/preview) ----
  function listSig(wins){
    return JSON.stringify(wins.map(w=>[w.index,w.name,w.active,uiSt(w.st),w.cmd,(w.lines||[]).join('\n')]))
      +'|'+curFolder+'|'+JSON.stringify(foldersData.members);
  }
  function render(){
    const wins=winsCache.filter(inFolder);
    listEl.innerHTML='';
    if(!wins.length){
      const e=document.createElement('div'); e.className='tt-empty';
      e.textContent = !winsCache.length ? 'No sessions'
        : (curFolder===WORK_FID ? 'Nothing is working now - see All' : 'This folder is empty');
      listEl.appendChild(e); return;
    }
    const hasReal=(s)=>{ for(const ch of s){ const c=ch.codePointAt(0);
      if((c>=48&&c<=57)||(c>=65&&c<=90)||(c>=97&&c<=122)||(c>=0x400&&c<=0x4FF)) return true; } return false; };
    for(const w of wins){
      const st=uiSt(w.st);
      const row=document.createElement('div');
      row.className='tt-row tt-st-'+st; row.dataset.idx=w.index; if(w.id) row.dataset.id=w.id;
      // preview: only lines with real text (letters/digits), no box-drawing separators from Claude/Codex; otherwise a placeholder
      const clean=(w.lines||[]).map(x=>String(x).trim()).filter(x=>x&&hasReal(x));
      const prev=clean.length ? clean.slice(-2).join('\n') : '';
      const emptyPrev=!prev;
      // right side: ONE label: active window -> compact 'active' badge, otherwise a short status
      const rtimeHtml = w.active
        ? '<span class="tt-rtime tt-live">active</span>'
        : '<span class="tt-rtime tt-st-'+st+'"></span>';
      row.innerHTML=
        '<div class="tt-avawrap">'+
          '<div class="tt-ava '+avaColor(w.name)+'"></div>'+
          '<div class="tt-dot"></div>'+
        '</div>'+
        '<div class="tt-body">'+
          '<div class="tt-top"><div class="tt-name"></div>'+rtimeHtml+'</div>'+
          '<div class="tt-bottom"><div class="tt-prev'+(emptyPrev?' tt-prev-empty':'')+'"></div>'+badge(st,w.index)+'</div>'+
        '</div>';
      row.querySelector('.tt-ava').textContent=avaInitial(w.name);
      row.querySelector('.tt-name').textContent=w.name;
      if(!w.active && st!=='idle') row.querySelector('.tt-rtime').textContent=ST_TEXT[st]||'';
      row.querySelector('.tt-prev').textContent = emptyPrev ? 'no output' : prev;
      attachRow(row, w);
      attachAvaDrag(row, w);
      listEl.appendChild(row);
    }
  }

  // ---- subtitle 'N active / M waiting' ----
  function subtitle(){
    const total=winsCache.length;
    const waiting=winsCache.filter(w=>uiSt(w.st)==='wait').length;
    let t=total+' '+plural(total,'session','sessions','sessions');
    if(waiting) t+=' · '+waiting+' waiting';
    subEl.textContent=t;
  }
  function plural(n,a,b,c){ return Math.abs(n)===1 ? a : b; }

  // ---- folder pills ----
  function folSig(){ return JSON.stringify(foldersData.folders)+'|'+JSON.stringify(foldersData.members)+'|'+curFolder+'|'+winsCache.length+'|'+winsCache.filter(isWorking).length; }
  function countIn(fid){
    let n=0; for(const w of winsCache){ const k=membersKey(w);
      if((foldersData.members[k]||[]).indexOf(fid)>=0) n++; } return n;
  }
  function renderFolders(){
    foldersEl.innerHTML='';
    // virtual 'Working' goes first; it has NO dataset.fid, so dropping
    // an avatar onto it is impossible (membership is by status, not assigned)
    const wk=document.createElement('button'); wk.type='button';
    wk.className='tt-folder tt-fvirt'+(curFolder===WORK_FID?' tt-fon':'');
    wk.innerHTML='Working <span class="tt-cnt">'+winsCache.filter(isWorking).length+'</span>';
    wk.onclick=()=>{ curFolder=WORK_FID; _listSig=''; _folSig=''; render(); renderFolders(); };
    foldersEl.appendChild(wk);
    const all=document.createElement('button'); all.type='button';
    all.className='tt-folder'+(curFolder==='all'?' tt-fon':'');
    all.innerHTML='All <span class="tt-cnt">'+winsCache.length+'</span>';
    all.onclick=()=>{ curFolder='all'; _listSig=''; _folSig=''; render(); renderFolders(); };
    foldersEl.appendChild(all);
    for(const f of foldersData.folders){
      const b=document.createElement('button'); b.type='button';
      b.className='tt-folder'+(curFolder===f.id?' tt-fon':'');
      b.dataset.fid=f.id;                          // drag-drop target for the avatar
      const cnt=countIn(f.id);
      const nm=document.createElement('span'); nm.textContent=f.name; b.appendChild(nm);
      const c=document.createElement('span'); c.className='tt-cnt'; c.textContent=cnt; b.appendChild(c);
      b.onclick=()=>{ curFolder=f.id; _listSig=''; _folSig=''; render(); renderFolders(); };
      foldersEl.appendChild(b);
    }
    const add=document.createElement('button'); add.type='button'; add.className='tt-folder tt-fadd';
    add.textContent='+ folder';
    add.onclick=newFolder;
    foldersEl.appendChild(add);
  }
  async function newFolder(){
    const name=(prompt('Folder name')||'').trim(); if(!name) return;
    try{ foldersData=await api('folders?op=create&name='+encodeURIComponent(name), {method:'POST'});
      _folSig=''; renderFolders(); }
    catch(e){ toast('Folder was not created'); }
  }
  async function loadFolders(){
    try{ const d=await api('folders'); if(d && d.folders){ foldersData=d;
      // the folder may have vanished: reset the filter ('all' and WORK_FID are not from folders.json)
      if(curFolder!=='all' && curFolder!==WORK_FID
         && !foldersData.folders.some(f=>f.id===curFolder)) curFolder='all';
    } }catch(e){}
  }

  // ---- row: tap = enter chat; long-press = action sheet ----
  // moved = finger moved >10px (list scroll): release does NOT open the chat.
  // lastTouch: after touch the browser synthesizes mousedown/mouseup: we suppress them.
  let pressTimer=null, longFired=false, moved=false, startY=0, startX=0, lastTouch=0;
  function attachRow(row, w){
    const down=(x,y,tgt)=>{ longFired=false; moved=false; startY=y; startX=x;
      // press ON THE AVATAR is the drag-to-folder zone: do NOT start the long-press timer,
      // otherwise holding to drag pops up the menu and interferes with the drag.
      if(tgt && tgt.closest && tgt.closest('.tt-avawrap')) return;
      row.classList.add('tt-press');
      pressTimer=setTimeout(()=>{ longFired=true; row.classList.remove('tt-press');
        if(navigator.vibrate) navigator.vibrate(12); openSheet(w); }, 480); };
    const up=()=>{ clearTimeout(pressTimer); row.classList.remove('tt-press');
      if(!longFired && !moved) enterChat(w); };      // clean tap without movement
    const cancel=()=>{ clearTimeout(pressTimer); row.classList.remove('tt-press'); };
    const isMouseAfterTouch=()=>Date.now()-lastTouch<700;
    row.addEventListener('touchstart', e=>down(e.touches[0].clientX,e.touches[0].clientY, e.target), {passive:true});
    row.addEventListener('touchmove', e=>{ if(Math.abs(e.touches[0].clientY-startY)>10||Math.abs(e.touches[0].clientX-startX)>10){ moved=true; cancel(); } }, {passive:true});
    row.addEventListener('touchend', ()=>{ lastTouch=Date.now(); up(); });
    row.addEventListener('touchcancel', ()=>{ lastTouch=Date.now(); moved=true; cancel(); });
    row.addEventListener('mousedown', e=>{ if(isMouseAfterTouch()) return; down(e.clientX,e.clientY, e.target); });
    row.addEventListener('mouseup', ()=>{ if(isMouseAfterTouch()){ moved=true; return; } up(); });
    row.addEventListener('mouseleave', cancel);
    row.addEventListener('contextmenu', e=>e.preventDefault());
  }

  // ---- drag a row avatar into a folder (move a chat to a folder) ----
  // The gesture starts ONLY from the avatar. Mouse: shift >8px = drag. Finger:
  // first HOLD_MS of holding still (the avatar 'lifts', vibration), then
  // drag; moving >8px earlier is a list scroll, the drag is cancelled and the finger
  // is handed to the browser (folders sit ABOVE the list, so the gesture direction
  // cannot be told from a scroll - only by holding). While no drag is active,
  // preventDefault is not called, scrolling is free.
  const DRAG_THRESH=8, HOLD_MS=260;
  function attachAvaDrag(row, w){
    const ava=row.querySelector('.tt-avawrap'); if(!ava) return;
    ava.addEventListener('touchstart', e=>startAvaDrag(row, w, e.touches[0], e, true), {passive:false});
    ava.addEventListener('mousedown',  e=>startAvaDrag(row, w, e, e, false));
  }
  function startAvaDrag(row, w, pt, oev, isTouch){
    if(oev.button===2) return;
    const winId=row.dataset.id || (w && w.id) || ''; if(!winId) return;
    const sx=pt.clientX, sy=pt.clientY;
    let active=false, ghost=null, gw=0, gh=0, hotFolder=null, cx=sx, cy=sy;
    let armed=!isTouch, holdT=null;   // finger: drag allowed only after the hold

    function makeGhost(){
      const g=document.createElement('div'); g.className='tt-drag-ghost';
      const av=row.querySelector('.tt-ava');
      const avC=av? av.cloneNode(true) : document.createElement('div');
      g.appendChild(avC);   // only the avatar circle - does not cover the folders
      document.body.appendChild(g);
      const gr=g.getBoundingClientRect(); gw=gr.width; gh=gr.height;
      return g;
    }
    function place(x,y,scale){
      if(!ghost) return;
      ghost.style.transform='translate('+(x-gw/2)+'px,'+(y-gh/2)+'px) scale('+(scale||1)+')';
    }
    function begin(){
      active=true; row.classList.add('tt-drag-src');
      ghost=makeGhost();
      // start the ghost at the avatar position, then 'lift' it to the finger
      const ar=row.querySelector('.tt-avawrap').getBoundingClientRect();
      place(ar.left+ar.width/2, ar.top+ar.height/2, 1);
      // force reflow so the start position applies before the lift animation
      ghost.getBoundingClientRect();
      ghost.classList.add('tt-anim','tt-lift');
      place(cx, cy, 1.14);
      // after the lift remove anim so further moves are instant (60fps)
      setTimeout(()=>{ if(ghost) ghost.classList.remove('tt-anim'); }, 200);
      if(navigator.vibrate) navigator.vibrate(14);
    }
    function updateHot(){
      // ghost has pointer-events:none -> elementFromPoint sees the folder under the finger
      const el=document.elementFromPoint(cx, cy);
      const f=el && el.closest && el.closest('.tt-folder');
      const overBar=el && el.closest && el.closest('#tt-folders');
      foldersEl.classList.toggle('tt-dragover', !!overBar);
      // drop target: real folders only (the '+ folder', 'All' and virtual
      // 'Working' have no dataset.fid - a window cannot be assigned there)
      const target=(f && f.dataset.fid) ? f : null;
      if(target!==hotFolder){
        if(hotFolder) hotFolder.classList.remove('tt-drop-hot');
        hotFolder=target;
        if(hotFolder){ hotFolder.classList.add('tt-drop-hot');
          if(navigator.vibrate) navigator.vibrate(8); }
      }
    }
    function move(ev){
      const p=isTouch ? (ev.touches&&ev.touches[0]) : ev; if(!p) return;
      cx=p.clientX; cy=p.clientY;
      const dx=cx-sx, dy=cy-sy;
      if(!active){
        if(Math.abs(dx)<DRAG_THRESH && Math.abs(dy)<DRAG_THRESH) return;
        if(!armed){ clearTimeout(holdT); cleanup(); return; }   // finger moved before the hold = scroll
        lift();
      }
      if(ev.cancelable) ev.preventDefault();   // do not scroll the page during the drag
      place(cx, cy, 1.14);
      updateHot();
    }
    function finish(){
      const dropOn=hotFolder;
      if(hotFolder){ hotFolder.classList.remove('tt-drop-hot'); hotFolder=null; }
      foldersEl.classList.remove('tt-dragover');
      row.classList.remove('tt-drag-src');
      if(!ghost){ cleanup(); return; }
      if(dropOn){
        // the ghost flies into the pill center: scale->0 + fade
        const fr=dropOn.getBoundingClientRect();
        if(navigator.vibrate) navigator.vibrate([12,30,14]);
        ghost.classList.add('tt-anim','tt-fly');
        place(fr.left+fr.width/2, fr.top+fr.height/2, 0.1);
        ghost.style.opacity='0';
        assign(dropOn.dataset.fid);
        endGhost(300);
      } else {
        // missed the folders: the ghost springs back to the avatar place, then fades
        const ar=row.querySelector('.tt-avawrap'), g=ghost;
        ghost.classList.add('tt-anim');
        if(ar){ const r=ar.getBoundingClientRect();
          place(r.left+r.width/2, r.top+r.height/2, 1); }
        // slight fade toward the end of the return so the disappearance is unnoticeable
        setTimeout(()=>{ if(g&&g.parentNode) g.style.opacity='0'; }, 240);
        endGhost(360);
      }
    }
    function endGhost(ms){ const g=ghost; ghost=null;
      setTimeout(()=>{ if(g&&g.parentNode) g.parentNode.removeChild(g); }, ms);
      cleanup(); }
    async function assign(fid){
      if(!fid) return;
      try{
        foldersData=await api('folders?op=assign&win='+encodeURIComponent(winId)+
          '&folder='+encodeURIComponent(fid), {method:'POST'});
        _listSig=''; _folSig=''; render(); renderFolders();
        ttPoll();
      }catch(e){ toast('Could not add to folder'); }
    }
    function cleanup(){
      document.removeEventListener('touchmove', move, {capture:true});
      document.removeEventListener('touchend', end, true);
      document.removeEventListener('touchcancel', end, true);
      document.removeEventListener('mousemove', move, true);
      document.removeEventListener('mouseup', end, true);
    }
    function lift(){
      // suppress long-press/tap on the row - the drag takes over the gesture
      if(pressTimer) clearTimeout(pressTimer); longFired=true;
      row.classList.remove('tt-press');
      begin();
    }
    function end(){ clearTimeout(holdT); finish(); }
    if(isTouch) holdT=setTimeout(()=>{ armed=true; lift(); }, HOLD_MS);
    document.addEventListener('touchmove', move, {passive:false, capture:true});
    document.addEventListener('touchend', end, true);
    document.addEventListener('touchcancel', end, true);
    document.addEventListener('mousemove', move, true);
    document.addEventListener('mouseup', end, true);
  }

  // ---- enter chat = select(index) + show the terminal ----
  function enterChat(w){
    const st=uiSt(w.st);
    termNameEl.className='tt-termname tt-st-'+st;
    termNameEl.querySelector('.tt-tn').textContent=w.name;
    if(typeof select==='function') select(w.index);   // make the window active (POST select)
    showTerm();
  }
  window.ttEnterActive=function(){                    // enter the active window (after new)
    const a=winsCache.find(x=>x.active)||winsCache[winsCache.length-1];
    if(a){ termNameEl.querySelector('.tt-tn').textContent=a.name; }
    showTerm();
  };
  window.ttEnterLastNew=function(){                   // after creating a session: refresh and enter
    setTimeout(()=>{ ttPoll().then(()=>window.ttEnterActive()); }, 350);
  };

  function blurKbd(){ try{
      var ae=document.activeElement; if(ae && ae.blur) ae.blur();
      if(typeof tin!=='undefined' && tin && tin.blur) tin.blur();
      var ifr=document.querySelector('#wrap iframe'); if(ifr && ifr.blur) ifr.blur();
    }catch(e){} }
  function showTerm(){ document.body.classList.add('tt-term'); stopPoll();
    if(typeof fitViewport==='function') fitViewport();
    // do NOT open the keyboard automatically on entering a session: drop focus (terminal/composer)
    blurKbd(); setTimeout(blurKbd,60); setTimeout(blurKbd,220); }
  function showList(){ document.body.classList.remove('tt-term'); startPoll(); ttPoll(); }
  window.ttShowList=showList; window.ttShowTerm=showTerm;
  window.ttRefreshList=ttPoll;   // force a list refresh from outside (after kill/rename)

  // ---- action sheet (long-press) ----
  const scrim=document.getElementById('tt-scrim'), sheet=document.getElementById('tt-sheet'),
        sheetBody=document.getElementById('tt-sheet-body');
  let sheetWin=null;
  function openSheet(w){ sheetWin=w; buildSheetMain(w);
    scrim.classList.add('tt-show'); sheet.classList.add('tt-show'); }
  function closeSheet(){ scrim.classList.remove('tt-show'); sheet.classList.remove('tt-show'); }
  document.getElementById('tt-sheet-cancel').onclick=closeSheet;
  // the menu opened by long-press, the finger is still on screen; on release the browser
  // synthesizes a click - it hit the scrim and closed the menu at once
  scrim.onclick=()=>{ if(Date.now()-lastTouch<700) return; closeSheet(); };

  function sheetHead(w){
    const st=uiSt(w.st);
    return '<div class="tt-shead"><div class="tt-hn">'+esc(w.name)+'</div>'+
      '<div class="tt-hs">'+(ST_TEXT[st]||'')+' · window '+w.index+'</div></div>';
  }
  function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function buildSheetMain(w){
    const myF=foldersData.members[membersKey(w)]||[];
    const folderLabel=myF.length?'Folders: add / remove':'Add to folder';
    let unassignBtn='';
    if(curFolder!=='all' && myF.indexOf(curFolder)>=0){
      const fn=(foldersData.folders.find(f=>f.id===curFolder)||{}).name||'folder';
      unassignBtn='<button class="tt-sitem" data-a="unassign-cur">Remove from "'+esc(fn)+'"'+
        '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9.5 12h5"/></svg></button>';
    }
    sheetBody.innerHTML=sheetHead(w)+
      '<button class="tt-sitem" data-a="folder">'+folderLabel+
        '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></button>'+
      unassignBtn+
      '<button class="tt-sitem" data-a="rename">Rename'+
        '<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>'+
      '<button class="tt-sitem" data-a="clear">Clear output'+
        '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>'+
      '<button class="tt-sitem tt-danger" data-a="kill">Close session'+
        '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>';
    sheetBody.querySelectorAll('.tt-sitem').forEach(b=>b.onclick=()=>sheetAct(b.dataset.a));
  }
  function buildSheetFolders(w){
    const key=membersKey(w);
    const cur=foldersData.members[key]||[];
    let html=sheetHead(w)+'<div class="tt-fchoice">';
    if(!foldersData.folders.length){
      html+='<div class="tt-shead"><div class="tt-hs">No folders yet - create one on the list screen</div></div>';
    }
    for(const f of foldersData.folders){
      const on=cur.indexOf(f.id)>=0;
      html+='<button class="tt-sitem" data-f="'+f.id+'">'+esc(f.name)+
        (on?'<span class="tt-chk">✓</span>':'')+'</button>';
    }
    html+='</div>';
    html+='<button class="tt-sitem" data-back="1">‹ Back</button>';
    sheetBody.innerHTML=html;
    sheetBody.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>toggleAssign(w, b.dataset.f));
    const bk=sheetBody.querySelector('[data-back]'); if(bk) bk.onclick=()=>buildSheetMain(w);
  }
  async function toggleAssign(w, fid){
    const key=membersKey(w); const cur=foldersData.members[key]||[];
    const on=cur.indexOf(fid)>=0;
    const op=on?'unassign':'assign';
    try{ foldersData=await api('folders?op='+op+'&win='+encodeURIComponent(w.id)+'&folder='+fid, {method:'POST'});
      buildSheetFolders(w); _folSig=''; _listSig=''; render(); renderFolders(); }
    catch(e){ toast('Not saved'); }
  }
  function sheetAct(a){
    const w=sheetWin; if(!w) return;
    if(a==='folder'){ buildSheetFolders(w); return; }
    if(a==='unassign-cur'){   // remove from the CURRENT folder (the one open in the filter)
      closeSheet();
      api('folders?op=unassign&win='+encodeURIComponent(w.id)+'&folder='+curFolder, {method:'POST'})
        .then(r=>{ foldersData=r; _folSig=''; _listSig=''; render(); renderFolders(); toast('Removed from folder'); })
        .catch(()=>toast('Not saved'));
      return;
    }
    closeSheet();
    if(a==='rename'){
      const nn=(prompt('New window name', w.name)||'').trim(); if(!nn) return;
      api('rename?w='+w.index+'&name='+encodeURIComponent(nn), {method:'POST'})
        .then(()=>{ _listSig=''; ttPoll(); if(typeof refresh==='function') refresh(); })
        .catch(()=>toast('Rename failed'));
    } else if(a==='clear'){
      // Ctrl+L clears the active window's screen; make the window active first
      api('select?w='+w.index, {method:'POST'})
        .then(()=>api('key?k='+encodeURIComponent('C-l'), {method:'POST'}))
        .then(()=>{ toast('Screen cleared'); _listSig=''; setTimeout(ttPoll,300); })
        .catch(()=>toast('Failed'));
    } else if(a==='kill'){
      if(typeof askClose==='function') askClose(w.index, w.name);  // existing confirmation dialog
    }
  }

  // ---- list polling (only while the list is open) ----
  async function ttPoll(){
    if(document.hidden || document.body.classList.contains('tt-term')) return;
    try{
      await loadFolders();
      const d=await api('previews?n=2');
      winsCache=(d && d.windows)||[];
      const ls=listSig(winsCache);
      if(ls!==_listSig){ _listSig=ls; render(); }
      const fs=folSig();
      if(fs!==_folSig){ _folSig=fs; renderFolders(); }
      subtitle();
    }catch(e){ /* keep last UI */ }
  }
  function stopPoll(){ if(_pollTimer){ clearInterval(_pollTimer); _pollTimer=null; } }
  function startPoll(){ stopPoll(); _pollTimer=setInterval(ttPoll, 3000); }

  // ---- header / FAB buttons ----
  document.getElementById('tt-back').onclick=showList;
  document.getElementById('tt-set-btn').onclick=()=>{ if(window.ttOpenSettings) window.ttOpenSettings(); };
  // FAB / '+': reuse the existing agent picker menu (#newmenu),
  // after creating a window, jump into its terminal.
  function openNew(anchor){
    if(typeof window.ttOpenNewMenu==='function'){ window.ttOpenNewMenu(anchor); return; }
    // fallback: use the default agent from server settings right away
    const cmd=(window.ttSrvSet && window.ttSrvSet().default_cmd) || 'shell';
    if(typeof addWin==='function'){ addWin(cmd); window.ttEnterLastNew(); }
  }
  document.getElementById('tt-fab').onclick=()=>openNew(document.getElementById('tt-fab'));
  document.getElementById('tt-new-btn').onclick=()=>openNew(document.getElementById('tt-new-btn'));

  // refresh the list immediately when returning to the tab
  document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) ttPoll(); });

  // startup: the list is open by default
  ttPoll(); startPoll();
})();

refresh(); refreshMem();
setInterval(refresh, 4000);
// ── auto-reload the page after a deploy ──
// The server exposes a code fingerprint in health. If it differs from the one
// we loaded with, reload, but ONLY at a calm moment: no drag in progress,
// input field empty and unfocused; for a hidden tab, on return to it.
(function(){
  // version we loaded with comes from the page's own meta (injected by the
  // server when serving). Empty (not injected) -> fallback: first health response.
  const meta=document.querySelector('meta[name="app-ver"]');
  let mine=(meta && meta.content && meta.content!=='__APP_VER__') ? meta.content : null, want=false;
  function calm(){
    const t=document.getElementById('tin');
    const typing=t && (t.value.trim() || document.activeElement===t);
    const drag=(typeof _tabDragging!=='undefined' && _tabDragging), busy=(typeof _winsBusy!=='undefined' && _winsBusy);
    // voice/screen recording in progress (modal open) or a split operation:
    // a reload would kill the recording / interrupt the action
    const modal=['vc-modal'].some(id=>{ const m=document.getElementById(id); return m && !m.classList.contains('fp-hidden'); });
    const pane=(typeof _paneBusy!=='undefined' && _paneBusy);
    return !typing && !drag && !busy && !modal && !pane;
  }
  function tryReload(){ if(want && !document.hidden && calm()) location.reload(); }
  async function check(){
    if(document.hidden) return;
    try{ const h=await api('health'); if(!h || !h.ver) return;
      if(mine===null){ mine=h.ver; return; }
      if(h.ver!==mine){ want=true; tryReload(); }
    }catch(e){}
  }
  setTimeout(check, 3000);
  setInterval(check, 20000);
  setInterval(tryReload, 5000);
  document.addEventListener('visibilitychange', ()=>{ if(!document.hidden){ tryReload(); check(); } });
})();
setInterval(refreshMem, 4000);
// back on the tab: refresh at once, do not wait for the interval
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden){ refresh(); refreshMem(); } });

// --- photo/file upload: the path is injected into the window (photo as [Image]) ---
const imgbtn=document.getElementById('imgbtn'), imgfile=document.getElementById('imgfile');
const filebtn=document.getElementById('filebtn'), ufile=document.getElementById('ufile');

// --- Attached photos: array of attachments + thumbnail strip above the composer ---
// Each attachment: {path (path in the container, returned by the server), url (local
// objectURL for thumbnail/lightbox), name}. The path is injected into the window NOT
// on upload but at send time (sendText), so the X can remove a photo before
// anything reaches the agent. The target window w is fixed at attach time.
let photoAttach = [];        // currently attached photos
let photoW = null;           // window that will receive the paths on send
const attachbar=document.getElementById('attachbar');
const lightbox=document.getElementById('lightbox');
const lbImg=lightbox.querySelector('img');

function renderAttach(){
  attachbar.innerHTML='';
  if(!photoAttach.length){ attachbar.classList.remove('on'); return; }
  attachbar.classList.add('on');
  photoAttach.forEach((a, i)=>{
    const t=document.createElement('div'); t.className='thumb';
    const img=document.createElement('img'); img.src=a.url; img.alt=a.name||'photo'; t.appendChild(img);
    const x=document.createElement('div'); x.className='thumb-x'; x.textContent='✕';
    x.setAttribute('role','button'); x.setAttribute('aria-label','Remove photo');
    x.onclick=(e)=>{ e.stopPropagation(); removeAttach(i); };
    t.appendChild(x);
    t.onclick=()=>openLightbox(a.url);
    attachbar.appendChild(t);
  });
}
function removeAttach(i){
  const a=photoAttach[i]; if(!a) return;
  try{ URL.revokeObjectURL(a.url); }catch(e){}
  photoAttach.splice(i,1); renderAttach();
}
function clearAttach(){
  for(const a of photoAttach){ try{ URL.revokeObjectURL(a.url); }catch(e){} }
  photoAttach=[]; photoW=null; renderAttach();
}
function openLightbox(url){
  lbImg.src=url; lightbox.classList.add('on'); lightbox.setAttribute('aria-hidden','false');
  requestAnimationFrame(()=>lightbox.classList.add('show'));
}
function closeLightbox(){
  lightbox.classList.remove('show'); lightbox.setAttribute('aria-hidden','true');
  setTimeout(()=>{ lightbox.classList.remove('on'); lbImg.src=''; }, 200);
}
lightbox.onclick=closeLightbox;
lightbox.querySelector('.lb-x').onclick=(e)=>{ e.stopPropagation(); closeLightbox(); };
lbImg.onclick=(e)=>e.stopPropagation();   // tap on the photo itself does not close
window.addEventListener('keydown', e=>{ if(e.key==='Escape' && lightbox.classList.contains('on')) closeLightbox(); });

async function sendImage(f, w){
  if(imgbtn.classList.contains('busy')) return;
  imgbtn.classList.add('busy'); toast('Uploading photo…');
  try{
    // defer=1 -> the server saves the file and returns the path but does NOT inject it yet;
    // the path goes to the window on send (sendText -> /m/api/attach-inject).
    const r=await fetch(API+'upload?defer=1&filename='+encodeURIComponent(f.name||'image.jpg')
        +(w!=null?'&w='+w:''),
      {method:'POST', headers:{'Content-Type':f.type||'application/octet-stream'}, body:f});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){ throw new Error(d.error||('error '+r.status)); }
    if(!d.path){ throw new Error('no path'); }
    photoW = (w!=null? w : photoW);
    photoAttach.push({path:d.path, url:URL.createObjectURL(f), name:f.name||'photo'});
    renderAttach();
    toast('Photo attached: add your question and press ▶'); tin.focus();
  }catch(e){ toast('Photo: '+e.message); }
  imgbtn.classList.remove('busy');
}
async function sendFile(f, w){
  if(filebtn.classList.contains('busy')) return;
  filebtn.classList.add('busy'); toast('Uploading "'+(f.name||'file')+'"…');
  try{
    const r=await fetch(API+'file?filename='+encodeURIComponent(f.name||'file.bin')
        +(w!=null?'&w='+w:''),
      {method:'POST', headers:{'Content-Type':f.type||'application/octet-stream'}, body:f});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){ throw new Error(d.error||('error '+r.status)); }
    toast('File added: add your question and press ▶'); tin.focus();
  }catch(e){ toast('File: '+e.message); }
  filebtn.classList.remove('busy');
}
function dispatchUpload(f, w){
  if(!f) return;
  if(/^image\//.test(f.type||'')) return sendImage(f, w);
  return sendFile(f, w);
}
// Upload several files at once: snapshot the list synchronously (before .value=''),
// then strictly SEQUENTIALLY via await: parallel is not allowed, the busy guard
// in sendImage/sendFile would drop concurrent ones. w is fixed at selection time.
async function uploadAll(fileList, kind){
  const files=Array.from(fileList||[]);
  if(!files.length) return;
  const w=lastActive;
  if(files.length>1) toast('Uploading '+files.length+(kind==='img'?' photos…':' files…'));
  let ok=0;
  for(const f of files){
    try{ await (kind==='img'? sendImage(f, w) : sendFile(f, w)); ok++; }
    catch(e){ /* the individual error was already shown by sendImage/sendFile */ }
  }
  if(files.length>1) toast('Added '+ok+' of '+files.length+': add your question and press ▶');
}
imgbtn.onclick=()=>imgfile.click();
imgfile.onchange=()=>{ uploadAll(imgfile.files, 'img'); imgfile.value=''; };
filebtn.onclick=()=>ufile.click();
ufile.onchange=()=>{ uploadAll(ufile.files, 'file'); ufile.value=''; };

// paste photos/files straight into the composer (clipboard), like in a chat
tin.addEventListener('paste', (e)=>{
  const dt=e.clipboardData; if(!dt) return;
  const files=[];
  if(dt.files && dt.files.length){ for(const f of dt.files) files.push(f); }
  else if(dt.items){ for(const it of dt.items){ if(it.kind==='file'){ const f=it.getAsFile(); if(f) files.push(f); } } }
  if(!files.length) return;            // plain text: do not interfere with the paste
  e.preventDefault();
  const w=lastActive;
  (async()=>{ for(const f of files){ await dispatchUpload(f, w); } })();
});
document.getElementById('openfolder').onclick=()=>openFolderPicker();

// --- 'Open folder' modal ---
(function(){
  const START=HOME_DIR;
  const modal=document.getElementById('fp-modal');
  const sheet=modal.querySelector('.fp-sheet');
  const pathInput=document.getElementById('fp-path');
  const listEl=document.getElementById('fp-list');
  const okBtn=document.getElementById('fp-ok');
  const favsEl=document.getElementById('fp-favs');
  let curParent=null, loadSeq=0, favs=[];
  const norm=p=>(p||'').replace(/\/+$/,'')||'/';
  const baseName=p=>{ p=norm(p); const i=p.lastIndexOf('/'); return i>=0?p.slice(i+1)||p:p; };

  async function loadFavs(){
    try{ const r=await api('favs'); favs=r.favs||[]; }catch(e){ favs=[]; }
    renderFavs();
  }
  function renderFavs(){
    favsEl.innerHTML='';
    if(!favs.length){ favsEl.hidden=true; return; }
    favsEl.hidden=false;
    for(const p of favs){
      const chip=document.createElement('button'); chip.type='button'; chip.className='fp-chip'; chip.dataset.path=p;
      const t=document.createElement('span'); t.textContent='★ '+baseName(p); chip.appendChild(t);
      const x=document.createElement('span'); x.className='x'; x.textContent='×';
      x.addEventListener('click',(e)=>{ e.stopPropagation(); toggleFav(p); });
      chip.appendChild(x);
      chip.addEventListener('click',()=>{ if(chip._moved){ chip._moved=false; return; } loadDir(p); });
      attachFavReorder(chip);
      favsEl.appendChild(chip);
    }
  }
  // #9: drag-reorder of favorite chips (hold -> drag left/right -> new order).
  // move/up listeners are on DOCUMENT so the gesture is not lost when the finger leaves a neighbor chip.
  function attachFavReorder(chip){
    let t=null, dragging=false, sx=0, sy=0;
    const move=(e)=>{
      if(!dragging){
        if(Math.abs(e.clientX-sx)>10 || Math.abs(e.clientY-sy)>10) clearTimeout(t);  // moved before the hold -> scroll, not drag
        return;
      }
      e.preventDefault(); chip._moved=true;
      const el=document.elementFromPoint(e.clientX, e.clientY);
      const over=el && el.closest && el.closest('.fp-chip');
      if(over && over!==chip && over.parentNode===favsEl){
        const r=over.getBoundingClientRect();
        const before=e.clientX < r.left+r.width/2;
        favsEl.insertBefore(chip, before?over:over.nextSibling);
      }
    };
    const up=()=>{
      clearTimeout(t);
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', up, true);
      document.removeEventListener('pointercancel', up, true);
      if(dragging){ dragging=false; chip.classList.remove('fp-chip-drag');
        const order=[...favsEl.querySelectorAll('.fp-chip')].map(c=>c.dataset.path);
        favs=order;
        if(navigator.vibrate) navigator.vibrate(8);
        api('favs?op=reorder&paths='+encodeURIComponent(JSON.stringify(order)), {method:'POST'}).catch(()=>{});
      }
    };
    chip.addEventListener('pointerdown', (e)=>{
      if(e.target.classList && e.target.classList.contains('x')) return;  // close button: not a drag
      sx=e.clientX; sy=e.clientY; dragging=false;
      document.addEventListener('pointermove', move, true);
      document.addEventListener('pointerup', up, true);
      document.addEventListener('pointercancel', up, true);
      t=setTimeout(()=>{ dragging=true; chip.classList.add('fp-chip-drag');
        if(navigator.vibrate) navigator.vibrate(12); }, 280);
    });
  }
  async function toggleFav(path, starEl){
    const p=norm(path); const on=favs.includes(p);
    if(starEl) starEl.classList.toggle('on', !on);    // optimistic
    try{ const r=await api('favs?op='+(on?'remove':'add')+'&path='+encodeURIComponent(p), {method:'POST'});
      favs=r.favs||[]; renderFavs(); }
    catch(e){ if(starEl) starEl.classList.toggle('on', on); toast('failed'); }
  }
  const FOLDER_SVG='<svg class="fp-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  const UP_SVG='<svg class="fp-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 16v-5M9.5 12.5L12 10l2.5 2.5"/></svg>';
  function setState(msg){ listEl.innerHTML='<div class="fp-state">'+msg+'</div>'; }
  function makeRow(cls,html,onTap){ const b=document.createElement('button'); b.type='button';
    b.className='fp-row'+(cls?' '+cls:''); b.innerHTML=html; b.addEventListener('click',onTap); return b; }
  async function loadDir(path){
    const seq=++loadSeq; listEl.setAttribute('aria-busy','true'); setState('Loading…');
    let data;
    try{ data=await api('ls?path='+encodeURIComponent(path)); }
    catch(e){ if(seq!==loadSeq) return; setState('Could not open path'); toast('No access: '+path); return; }
    if(seq!==loadSeq) return;
    listEl.setAttribute('aria-busy','false');
    const norm=data.path||path; curParent=data.parent||null;
    pathInput.value=norm.endsWith('/')?norm:norm+'/';
    const dirs=Array.isArray(data.dirs)?data.dirs:[];
    listEl.innerHTML='';
    if(curParent && curParent!==norm){
      listEl.appendChild(makeRow('fp-row--up', UP_SVG+'<span class="fp-name">..</span>', ()=>loadDir(curParent)));
    }
    if(!dirs.length){ const d=document.createElement('div'); d.className='fp-state'; d.textContent='No subfolders'; listEl.appendChild(d); return; }
    const base=norm.endsWith('/')?norm:norm+'/'; const frag=document.createDocumentFragment();
    for(const name of dirs){ const child=base+name; const cp=child.replace(/\/+$/,'');
      const row=makeRow('', FOLDER_SVG+'<span class="fp-name"></span>', ()=>loadDir(child));
      row.querySelector('.fp-name').textContent=name;
      const star=document.createElement('span'); star.className='fp-star'+(favs.includes(cp)?' on':'');
      star.textContent='★'; star.title='Add to favorites';
      star.addEventListener('click',(e)=>{ e.stopPropagation(); toggleFav(cp, star); });
      row.appendChild(star);
      frag.appendChild(row); }
    listEl.appendChild(frag); listEl.scrollTop=0;
  }
  // segment "what to launch": shell or one of the configured agents.
  // Default is CLAUDE; the last choice is remembered in localStorage (key mterm_defcmd).
  const CMD_KEY='mterm_defcmd', CMDS=__CMDS__;
  let launchCmd='__DEFAULT_AGENT__';
  try{ const s=localStorage.getItem(CMD_KEY); if(s && CMDS.includes(s)) launchCmd=s; }catch(e){}
  function applyLaunchCmd(cmd){
    launchCmd=cmd;
    modal.querySelectorAll('.fp-seg').forEach(x=>x.classList.toggle('active', x.dataset.cmd===cmd));
    try{ localStorage.setItem(CMD_KEY, cmd); }catch(e){}
  }
  applyLaunchCmd(launchCmd);  // highlight the saved/default segment on load
  modal.querySelectorAll('.fp-seg').forEach(s=>{ s.onclick=()=>applyLaunchCmd(s.dataset.cmd); });
  async function confirmOpen(){
    let p=(pathInput.value||'').trim(); if(!p){ toast('Enter a path'); return; }
    // 'choose default folder' mode (from tmux tabs 2.0 settings):
    // do not open a window, save default_cwd on the server and return.
    if(window.__ttFolderTarget==='default_cwd'){
      window.__ttFolderTarget=null; okBtn.disabled=true;
      try{ await api('settings', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({default_cwd:p})}); toast('Default folder: '+p); }
      catch(e){ toast('Not saved'); }
      finally{ okBtn.disabled=false; closeFolderPicker(); }
      return;
    }
    okBtn.disabled=true;
    try{ await api('open?path='+encodeURIComponent(p)+'&cmd='+launchCmd, {method:'POST'});
      closeFolderPicker();
      const label=launchCmd;
      toast(launchCmd==='shell'?('Opened: '+p):('Starting '+label+' in '+p));
      if(window.ttEnterLastNew) window.ttEnterLastNew(); else refresh(); }
    catch(e){ toast('Could not open: '+p); }
    finally{ okBtn.disabled=false; }
  }
  window.openFolderPicker=function(start){ applyLaunchCmd(launchCmd); modal.classList.remove('fp-hidden'); loadFavs(); loadDir(start||START); };
  window.closeFolderPicker=function(){ modal.classList.add('fp-hidden'); window.__ttFolderTarget=null; };
  modal.addEventListener('click',(e)=>{ if(e.target.closest('[data-fp-close]')) closeFolderPicker(); });
  // the close button is INSIDE .fp-sheet; handle its click here directly,
  // otherwise stopPropagation below swallows the bubbling and the button 'does not work'.
  sheet.addEventListener('click',(e)=>{ if(e.target.closest('[data-fp-close]')){ closeFolderPicker(); return; } e.stopPropagation(); });
  okBtn.addEventListener('click',confirmOpen);
  pathInput.addEventListener('keydown',(e)=>{ if(e.key==='Enter'){ e.preventDefault(); const p=(pathInput.value||'').trim(); if(p) loadDir(p); } });
})();

// --- voice input: recording -> transcription -> insert into the field ---
(function(){
  if(!window.VOICE_ON){ const mb=document.getElementById('micbtn'); if(mb) mb.style.display='none'; return; }
  const modal=document.getElementById('vc-modal');
  const dragEl=document.getElementById('vc-drag');
  const recBtn=document.getElementById('vc-rec');
  const timeEl=document.getElementById('vc-time');
  const hintEl=document.getElementById('vc-hint');
  const textEl=document.getElementById('vc-text');
  const cancelBtn=document.getElementById('vc-cancel');
  const againBtn=document.getElementById('vc-again');
  const insertBtn=document.getElementById('vc-insert');
  const retryBtn=document.getElementById('vc-retry');
  const renewBtn=document.getElementById('vc-renew');
  const stepsEl=document.getElementById('vc-steps');
  const dlBtn=document.getElementById('vc-download');
  const MIC='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
  const STOP='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2.5"/></svg>';
  let rec=null, chunks=[], stream=null, timer=null, t0=0, state='idle';
  let lastBlob=null, lastExt='webm', lastErr='', curEngine='flash', wakeLock=null;
  // transcription runs ON THE SERVER as a separate job: close the window or minimize the portal,
  // it keeps going and on return we see its progress again, not an empty screen
  let jobId=null, pollT=null, lastJob=null;
  const JOB_KEY='mterm_vc_job';

  async function acquireWake(){ try{ if('wakeLock' in navigator) wakeLock=await navigator.wakeLock.request('screen'); }catch(e){} }
  function releaseWake(){ try{ wakeLock&&wakeLock.release(); }catch(e){} wakeLock=null; }
  // if the system released the wake lock (minimized) and we record again, re-acquire it
  document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='visible'&&state==='rec'&&!wakeLock) acquireWake(); });

  function fmt(s){ s=Math.floor(s); return ('0'+Math.floor(s/60)).slice(-2)+':'+('0'+(s%60)).slice(-2); }
  function tick(){ timeEl.textContent=fmt((Date.now()-t0)/1000); }
  function stopTracks(){ if(stream){ stream.getTracks().forEach(t=>t.stop()); stream=null; } }
  function clearTimer(){ if(timer){ clearInterval(timer); timer=null; } }

  function setState(s){
    state=s;
    const err=(s==='error');
    recBtn.hidden = !(s==='idle'||s==='rec'||err);   // after an error one can re-record at once
    timeEl.hidden = (s!=='rec');
    textEl.hidden = (s!=='done');
    againBtn.hidden = (s!=='done');
    insertBtn.hidden = (s!=='done');
    stepsEl.hidden = !(s==='load'||s==='done'||err);
    renewBtn.hidden = !err;
    retryBtn.hidden = !err; dlBtn.hidden = !err;
    recBtn.classList.toggle('rec', s==='rec');
    recBtn.innerHTML = (s==='rec') ? STOP : MIC;
    modal.classList.toggle('recording', s==='rec');   // red dot in the header
    hintEl.classList.toggle('err', err);
    if(s==='idle'){ hintEl.hidden=false; hintEl.textContent='Press to record'; }
    else if(s==='rec'){ hintEl.hidden=false; hintEl.textContent='Recording… press stop'; }
    else if(s==='load'){ hintEl.hidden=false; hintEl.textContent='Transcribing…'; }
    else if(s==='done'){ hintEl.hidden=true; }
    else if(err){ hintEl.hidden=false; hintEl.textContent='Error: '+lastErr; }
  }

  // Human-readable progress: what has already failed, who is working now,
  // who is still queued. Without this it is unclear whether it fell back to the spare engine.
  function renderSteps(j){
    stepsEl.innerHTML='';
    if(!j || !j.chain) return;
    const labels=j.labels||{};
    const tried={}; (j.tried||[]).forEach(t=>{ tried[t.id]=t; });
    j.chain.forEach(id=>{
      const t=tried[id], now=(j.stage===id);
      const row=document.createElement('div');
      row.className='vc-step'+(t? (t.ok?' ok':' bad') : (now?' now':''));
      const ic=document.createElement('span'); ic.className='vc-si';
      ic.textContent = t ? (t.ok?'✓':'✕') : (now?'◌':'·');
      const nm=document.createElement('span'); nm.className='vc-sname'; nm.textContent=labels[id]||id;
      const sec=document.createElement('span'); sec.className='vc-secs';
      sec.textContent = t&&t.secs ? (t.secs+' s') : (now?'working…':'');
      row.appendChild(ic); row.appendChild(nm); row.appendChild(sec);
      if(t && !t.ok){
        const w=document.createElement('span'); w.className='vc-why';
        w.textContent=String(t.why||'no response').replace(/\s+/g,' ').slice(0,70);
        row.appendChild(w);
      }
      stepsEl.appendChild(row);
    });
    if(j.state==='done' && j.engine){
      const d=document.createElement('div'); d.className='vc-done';
      d.textContent='Transcribed by '+(labels[j.engine]||j.engine)+(j.took?(' · '+j.took+' s'):'');
      stepsEl.appendChild(d);
    }
  }

  function stopPoll(){ if(pollT){ clearTimeout(pollT); pollT=null; } }

  function poll(){
    stopPoll();
    pollT=setTimeout(async ()=>{
      if(!jobId) return;
      let j=null;
      try{ j=await api('stt-job?id='+encodeURIComponent(jobId)); }catch(e){ poll(); return; }
      lastJob=j; renderSteps(j);
      if(j.state==='run'){ poll(); return; }
      if(j.state==='done'){
        const t=(j.text||'').trim();
        jobId=null; try{ sessionStorage.removeItem(JOB_KEY); }catch(e){}
        if(!t){ lastErr='empty transcript'; setState('error'); return; }
        textEl.value=t; setState('done');
        return;
      }
      // error / none: show the reason and keep the recording at hand
      jobId=null; try{ sessionStorage.removeItem(JOB_KEY); }catch(e){}
      lastErr=(j.error||'transcription failed'); setState('error');
    }, 600);
  }

  async function doTranscribe(engine){
    if(!lastBlob){ toast('Recording lost: record again'); setState('idle'); return; }
    curEngine=engine; setState('load'); renderSteps(null);
    hintEl.textContent='Sending recording…';
    try{
      const r=await fetch(API+'transcribe?job=1&ext='+lastExt, {method:'POST',
        headers:{'Content-Type':lastBlob.type||'application/octet-stream'}, body:lastBlob});
      const d=await r.json().catch(()=>({}));
      if(!r.ok || !d.job) throw new Error(d.error||('error '+r.status));
      jobId=d.job; try{ sessionStorage.setItem(JOB_KEY, jobId); }catch(e){}
      hintEl.textContent='Transcribing…';
      poll();
    }catch(e){ lastErr=e.message; setState('error'); }
  }

  async function startRec(){
    try{ stream=await navigator.mediaDevices.getUserMedia({audio:true}); }
    catch(e){ toast('No microphone access'); return; }
    chunks=[];
    try{ rec=new MediaRecorder(stream); }
    catch(e){ rec=new MediaRecorder(stream, {mimeType:'audio/mp4'}); }
    rec.ondataavailable=(e)=>{ if(e.data && e.data.size) chunks.push(e.data); };
    rec.onstop=onStop;
    rec.start();
    t0=Date.now(); timeEl.textContent='00:00'; clearTimer(); timer=setInterval(tick,250);
    setState('rec'); acquireWake();   // keep the screen on while recording
  }
  function stopRec(){ clearTimer(); releaseWake(); try{ rec && rec.state!=='inactive' && rec.stop(); }catch(e){} }

  async function onStop(){
    stopTracks();
    const type=(rec && rec.mimeType) || 'audio/webm';
    const blob=new Blob(chunks, {type});
    if(!blob.size){ toast('Empty recording'); setState('idle'); return; }
    lastBlob=blob;                                       // keep it so it is not lost on error
    lastExt = /mp4|m4a|aac/.test(type)?'mp4' : /ogg/.test(type)?'ogg' : 'webm';
    doTranscribe('flash');                               // Gemini auto-retry inside the backend
  }

  function open(){
    modal.classList.remove('fp-hidden');
    // If transcription is still running (window was closed, portal minimized), keep
    // showing its progress. Otherwise the user would see an empty screen and
    // think the recording was lost.
    let saved=''; try{ saved=sessionStorage.getItem(JOB_KEY)||''; }catch(e){}
    if(jobId || saved){
      jobId=jobId||saved; setState('load'); renderSteps(lastJob); poll(); return;
    }
    chunks=[]; lastBlob=null; textEl.value=''; stepsEl.innerHTML=''; setState('idle');
  }
  function close(){ stopRec(); stopTracks(); clearTimer(); releaseWake(); modal.classList.add('fp-hidden'); }

  recBtn.onclick=()=>{ if(state==='rec') stopRec(); else if(state==='idle'||state==='error') startRec(); };
  function reRecord(){ stopPoll(); jobId=null; try{ sessionStorage.removeItem(JOB_KEY); }catch(e){}
    chunks=[]; lastBlob=null; textEl.value=''; stepsEl.innerHTML=''; setState('idle'); }
  againBtn.onclick=reRecord;
  renewBtn.onclick=reRecord;
  insertBtn.onclick=()=>{
    const t=textEl.value.trim(); close();
    if(t){ tin.value = tin.value ? (tin.value.replace(/\s*$/,'')+' '+t) : t; autoGrow(); tin.focus(); }
  };
  // after an error: try again
  retryBtn.onclick=()=>doTranscribe('flash');
  // download the recording to the phone so it is not lost (blob -> file)
  dlBtn.onclick=()=>{
    if(!lastBlob) return;
    const u=URL.createObjectURL(lastBlob);
    const a=document.createElement('a'); a.href=u; a.download='voice-'+Date.now()+'.'+lastExt;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(u), 10000);
    toast('Recording saved');
  };
  cancelBtn.onclick=close;
  modal.addEventListener('click',(e)=>{ if(e.target.closest('[data-vc-close]')) close(); });

  // --- drag the widget by its header (touch/pointer) ---
  let drag=null;
  function clampPos(x,y){
    const r=modal.getBoundingClientRect(), m=6;
    x=Math.max(m, Math.min(x, window.innerWidth - r.width - m));
    y=Math.max(m, Math.min(y, window.innerHeight - r.height - m));
    return [x,y];
  }
  dragEl.addEventListener('pointerdown',(e)=>{
    if(e.target.closest('[data-vc-close]')) return;     // close button does not drag
    const r=modal.getBoundingClientRect();
    drag={dx:e.clientX-r.left, dy:e.clientY-r.top};
    modal.classList.add('dragged');                     // remove auto-centering
    modal.style.left=r.left+'px'; modal.style.top=r.top+'px'; modal.style.bottom='auto';
    try{ dragEl.setPointerCapture(e.pointerId); }catch(_){}
  });
  dragEl.addEventListener('pointermove',(e)=>{
    if(!drag) return;
    const [x,y]=clampPos(e.clientX-drag.dx, e.clientY-drag.dy);
    modal.style.left=x+'px'; modal.style.top=y+'px';
  });
  const endDrag=()=>{ drag=null; };
  dragEl.addEventListener('pointerup', endDrag);
  dragEl.addEventListener('pointercancel', endDrag);

  document.getElementById('micbtn').onclick=open;
  // external: open the widget and start recording AT ONCE, for the hotkey link ?voice=1
  window.ttVoiceStart=function(){ open(); startRec(); };
})();

// --- hotkey link: ?go=claude&cwd=...&model=...&voice=1 ----------------------
// A double tap of the right Option key on a Mac opens the portal with such a link: a
// tab with the agent is created in the given folder and the microphone starts.
// The params are stripped from the URL so reload/back do not spawn extra tabs.
(function(){
  var p; try{ p=new URLSearchParams(location.search); }catch(e){ return; }
  var go=(p.get('go')||'').trim(), voice=p.get('voice')==='1';
  if(!go && !voice) return;
  try{
    var keep=new URLSearchParams(location.search);
    ['go','cwd','model','voice'].forEach(function(k){ keep.delete(k); });
    var qs=keep.toString();
    history.replaceState(null,'',location.pathname+(qs?('?'+qs):'')+location.hash);
  }catch(e){}
  function startVoice(){ if(voice && window.ttVoiceStart){ try{ window.ttVoiceStart(); }catch(e){} } }
  setTimeout(function(){
    if(!go){ startVoice(); return; }
    var q='?cmd='+encodeURIComponent(go);
    var cwd=(p.get('cwd')||'').trim(); if(cwd) q+='&cwd='+encodeURIComponent(cwd);
    var model=(p.get('model')||'').trim(); if(model) q+='&model='+encodeURIComponent(model);
    api('new'+q,{method:'POST'}).then(function(r){
      if(r && r.windows){ _winsSig=winsSig(r.windows); render(r.windows,true); }
      else { _winsSig=''; refresh(); }
      if(window.ttEnterLastNew) window.ttEnterLastNew();
      setTimeout(startVoice, 600);
    }).catch(function(){ toast('Could not open tab'); startVoice(); });
  }, 250);
})();


// ============================================================
// Interactive UI guide (prefix gd-).
// Highlights REAL on-screen elements with a spotlight and explains them.
// A step whose element is missing or hidden is silently skipped, since half of the
// elements exist only in one mode (2.0 / classic).
// Public entry point: window.gdStart([step number]).
// ============================================================
(function(){
  var ovl=document.getElementById('gd-ovl'), ring=document.getElementById('gd-ring'),
      card=document.getElementById('gd-card'),
      titleEl=document.getElementById('gd-title'), textEl=document.getElementById('gd-text'),
      dotsEl=document.getElementById('gd-dots'),
      backBtn=document.getElementById('gd-back'), nextBtn=document.getElementById('gd-next'),
      closeBtn=document.getElementById('gd-close'), skipEl=document.getElementById('gd-skip');
  if(!ovl || !ring || !card) return;

  var SEEN='mterm_guide_seen';

  // sel: comma-separated selectors; the FIRST visible one is used (modes have different buttons).
  // screen: which screen of mode 2.0 hosts the element ('list' | 'term').
  // only2: the step exists only in mobile 2.0.
  var STEPS=[
    { sel:'#tt-list', screen:'list', only2:true, title:'Your sessions',
      text:'List screen: sessions look like chats, with an avatar, name and the last output lines. '+
           'Tap a row to go inside. Each session is a separate tmux window with its own agent.' },
    { sel:'#tt-subtitle', screen:'list', only2:true, title:'Statuses',
      text:'On the list screen, a red ! (waiting for a reply) means the agent asked a question. '+
           '▶ means working right now. A green ✓ (done) means it finished while you were away; it clears when you open the session. '+
           'There is also idle. The header shows how many sessions are waiting for a reply.' },
    { sel:'#tt-fab,#tt-new-btn,#add', screen:'list', title:'New session',
      text:'Pick an agent here: Claude, Codex, OpenCode or just a Terminal. '+
           'After creation you jump straight into the new session.' },
    { sel:'#tin', screen:'term', title:'Type here, not in the terminal',
      text:'This field supports swipe typing, autocorrect and dictation; the terminal itself does not '+
           '(xterm.js has no IME support). Enter adds a new line; send with the ▶ button or Ctrl+Enter. '+
           'The draft is saved automatically.' },
    { sel:'#term', screen:'term', title:'Swipe to scroll the conversation',
      text:'Swiping up or down on the terminal scrolls the history, with inertia. '+
           'A short tap does not count as a scroll, so you can tap the terminal freely.' },
    { sel:'#keystoggle', screen:'term', title:'Keys and commands',
      text:'Here are esc, tab, ^C, ^L, arrows, PgUp/PgDn and ^O (Claude Code transcript mode). '+
           'Next to them are quick commands /resume, /model, /usage (configurable). '+
           'Pressing these buttons deliberately does not open the keyboard.' },
    { sel:'#micbtn', screen:'term', title:'Voice input',
      text:'Record, transcribe, then Insert into the input field. '+
           'If transcription failed, the recording is not lost: you can retry or '+
           'download the audio.' },
    { sel:'#imgbtn', screen:'term', title:'Photos and files',
      text:'A photo (JPEG/PNG, up to 15 MB) is attached as a thumbnail; you can remove it before sending. '+
           'The paperclip next to it adds any file up to 100 MB straight into the project working folder. '+
           'You can also paste a file or image from the clipboard into the input field.' },
    { sel:'#openfolder', screen:'term', title:'Open folder',
      text:'Start an agent right in the folder you need. '+
           '★ adds the folder to favorites, and favorites then show as chips at the top.' },
    { sel:'#tt-list', screen:'list', only2:true, title:'Long press on a session',
      text:'On the list screen, a long press on a row opens a menu: folders, rename, '+
           'clear output, close. You can drag a session by its avatar onto a folder pill at the bottom, '+
           'and tapping a pill filters the list.' },
    { sel:'#gridbtn', screen:'term', title:'All windows at once',
      text:'A grid of previews of all windows: tap to switch, long press to reorder. '+
           'The narrow pill above the bar opens the same panel at half screen.' },
    { sel:'#keystoggle', screen:'term', title:'Split screen',
      text:'In the ☰ panel, the ⬓ split and ◧ split buttons divide the window into parts, '+
           '⇄ part switches between them, ✕ part closes. Tapping a part selects it.' },
    { sel:'#mem,#tt-set-btn', screen:'term', title:'Settings',
      text:'Default agent and folder (stored on the server and shared across devices), '+
           'your own quick commands, list appearance and the Mobile 2.0 / Classic (desktop) switch. '+
           'Tapping RAM …G in the bar also opens settings.' },
    { sel:'#reload', screen:'term', title:'If the terminal hangs',
      text:'The ↻ button reconnects the terminal, which fixes hangs and garbled characters. '+
           'Your work in the session is not lost.' }
  ];

  var order=[], idx=0, curEl=null;

  // Visibility: a hidden element means the step is skipped. Note on position:fixed:
  // such elements always have offsetParent null (#tt-fab), so judge by styles and the box.
  function gdVisible(el){
    if(!el) return false;
    var st=window.getComputedStyle(el);
    if(st.display==='none' || st.visibility==='hidden' || parseFloat(st.opacity)===0) return false;
    if(el.offsetParent===null && st.position!=='fixed') return false;
    var r=el.getBoundingClientRect();
    return r.width>0 && r.height>0;
  }
  function gdTarget(step){
    var sels=step.sel.split(',');
    for(var i=0;i<sels.length;i++){
      var el=document.querySelector(sels[i].replace(/^\s+|\s+$/g,''));
      if(gdVisible(el)) return el;
    }
    return null;
  }

  // Switch 2.0 screens before the step; classic has nothing to switch, so call back at once.
  function gdScreen(scr, cb){
    if(!scr || !document.body.classList.contains('tt-2') ||
       typeof window.ttShowList!=='function' || typeof window.ttShowTerm!=='function'){ cb(); return; }
    var isTerm=document.body.classList.contains('tt-term');
    if(scr==='list' && isTerm){ window.ttShowList(); setTimeout(cb,350); return; }
    if(scr==='term' && !isTerm){ window.ttShowTerm(); setTimeout(cb,350); return; }
    cb();
  }

  function gdBuildOrder(){
    var tt2=document.body.classList.contains('tt-2');
    order=[];
    for(var i=0;i<STEPS.length;i++){
      if(STEPS[i].only2 && !tt2) continue;
      order.push(STEPS[i]);
    }
  }

  // Position the spotlight and card. Also called on resize/orientationchange.
  function gdPlace(){
    if(!curEl || ovl.classList.contains('gd-hidden')) return;
    var r=curEl.getBoundingClientRect();
    var vw=window.innerWidth, vh=window.innerHeight, pad=6, m=10;
    var x=Math.max(2, r.left-pad), y=Math.max(2, r.top-pad);
    var w=Math.max(8, Math.min(r.width+pad*2, vw-4)), h=Math.max(8, Math.min(r.height+pad*2, vh-4));
    if(x+w>vw-2) x=Math.max(2, vw-2-w);
    if(y+h>vh-2) y=Math.max(2, vh-2-h);
    var cs=window.getComputedStyle(curEl).borderRadius||'';
    var round=(cs.indexOf('%')>=0 || cs.indexOf('999')>=0);
    ring.style.left=x+'px'; ring.style.top=y+'px';
    ring.style.width=w+'px'; ring.style.height=h+'px';
    ring.style.borderRadius = round ? '999px' : Math.min(20,(parseFloat(cs)||8)+pad)+'px';
    // card goes below the target if the target is in the upper half of the screen, else above; clamped
    // offsetWidth/Height, NOT getBoundingClientRect: during the fpPop animation the card
    // is scaled, so the rect would return the scale(.96) size and the card would drift
    var cw=card.offsetWidth, ch=card.offsetHeight;
    var below=((r.top+r.height/2) < vh/2);
    var cx=r.left + r.width/2 - cw/2;
    var cy=below ? (y+h+12) : (y-ch-12);
    cx=Math.max(m, Math.min(cx, vw-cw-m));
    cy=Math.max(m, Math.min(cy, vh-ch-m));
    card.style.left=cx+'px'; card.style.top=cy+'px';
  }

  function gdShow(step, el){
    curEl=el;
    titleEl.textContent=step.title;
    textEl.textContent=step.text;
    var last=(idx>=order.length-1);
    nextBtn.textContent = last ? 'Got it' : 'Next';
    backBtn.style.display = (idx>0) ? '' : 'none';
    dotsEl.innerHTML='';
    for(var i=0;i<order.length;i++){
      var d=document.createElement('span');
      d.className='gd-dot'+(i===idx?' gd-on':'');
      dotsEl.appendChild(d);
    }
    ovl.classList.remove('gd-hidden'); card.classList.remove('gd-hidden');
    ovl.setAttribute('aria-hidden','false');
    gdPlace();
    // the screen slide lasts .42s and we read coordinates after 350 ms, so settle the frame
    // in place once the transition has surely finished (and after keyboard/font layout)
    setTimeout(gdPlace, 420);
    // restart the card appear animation on every step
    card.style.animation='none'; void card.offsetWidth; card.style.animation='';
  }

  function gdGo(i, dir){
    if(i<0 || !order.length) return;
    if(i>=order.length){ gdClose(); return; }
    var step=order[i];
    gdScreen(step.screen, function(){
      var el=gdTarget(step);
      if(!el){ gdGo(i+(dir<0?-1:1), dir); return; }   // element missing/hidden: silently move on
      idx=i; gdShow(step, el);
    });
  }

  function gdClose(){
    ovl.classList.add('gd-hidden'); card.classList.add('gd-hidden');
    ovl.setAttribute('aria-hidden','true');
    curEl=null;
    try{ localStorage.setItem(SEEN,'1'); }catch(e){}   // set the key on ANY exit
  }

  nextBtn.onclick=function(){ if(idx>=order.length-1) gdClose(); else gdGo(idx+1,1); };
  backBtn.onclick=function(){ gdGo(idx-1,-1); };
  closeBtn.onclick=gdClose;
  skipEl.onclick=gdClose;
  ovl.onclick=gdClose;                 // tap on the backdrop (the card is a sibling; its clicks do not bubble here)
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape' && !ovl.classList.contains('gd-hidden')){ e.preventDefault(); gdClose(); }
  });
  window.addEventListener('resize', gdPlace);
  window.addEventListener('orientationchange', function(){ setTimeout(gdPlace,250); });

  window.gdStart=function(from){
    gdBuildOrder();
    if(!order.length) return;
    idx=0;
    gdGo((typeof from==='number' && from>0) ? from : 0, 1);
  };

  var gb1=document.getElementById('gd-btn-list'), gb2=document.getElementById('gd-btn-bar');
  if(gb1) gb1.onclick=function(){ window.gdStart(0); };   // manual start does not check the key
  if(gb2) gb2.onclick=function(){ window.gdStart(0); };

  // show it once automatically to a newcomer
  try{
    if(localStorage.getItem(SEEN)===null) setTimeout(function(){ window.gdStart(0); }, 1200);
  }catch(e){}
})();
// ============================================================
// SIDEBAR + FILE TREE like in VS Code. Classic (desktop) mode only.
// All in one IIFE; exposed outward as window.sbToggleSidebar / window.sbRefreshTree.
// File names come from disk -> into the DOM ONLY via textContent.
// ============================================================
(function(){
  var sidebar=document.getElementById('sidebar');
  if(!sidebar) return;
  var ROOT_DEF=HOME_DIR;
  // HOME is the sandbox root FROM THE SERVER (fs/ls without path returns its own WORKDIR).
  // ROOT_DEF is only a fallback until the server answers / when it is unavailable.
  var HOME=ROOT_DEF;
  var K_OPEN='mterm_sb_open', K_W='mterm_sb_w', K_SPLIT='mterm_sb_split',
      K_DOT='mterm_sb_dot', K_ROOT='mterm_sb_root', K_EXP='mterm_sb_exp';
  var W_MIN=180, W_MAX=520, W_DEF=260;

  var rowEl=document.getElementById('sb-row');
  var resizer=document.getElementById('sb-resizer');
  var topEl=document.getElementById('sb-top');
  var hsplit=document.getElementById('sb-hsplit');
  var treeEl=document.getElementById('sb-tree');
  var crumbsEl=document.getElementById('sb-crumbs');
  var menuEl=document.getElementById('sb-menu'), menuOvl=document.getElementById('sb-movl');
  var cfEl=document.getElementById('sb-confirm');
  var sideBtn=document.getElementById('sidebtn'), undoBtn=document.getElementById('undobtn');
  var dotBtn=document.getElementById('sb-dot');

  // ---------- small path utilities ----------
  function norm(p){ p=(p||'').replace(/\/+$/,''); return p||'/'; }
  function join(dir,name){ dir=norm(dir); return (dir==='/'?'':dir)+'/'+name; }
  function parentOf(p){ p=norm(p); var i=p.lastIndexOf('/'); return i<=0?'/':p.slice(0,i); }
  function baseName(p){ p=norm(p); var i=p.lastIndexOf('/'); return i>=0?(p.slice(i+1)||'/'):p; }
  function isInside(dir,p){ dir=norm(dir); return p===dir || p.indexOf(dir==='/'?'/':dir+'/')===0; }

  // ---------- network: like the rest of the code (relative paths), but with the error text ----------
  // The backend returns {"error":"text"} with code 400/403/404/409; show THAT, not a bare 'Error 409'.
  async function fsApi(path, opts){
    var r=await fetch(API+path, opts||{});
    var data=null;
    try{ data=await r.json(); }catch(e){}
    if(!r.ok){ var er=new Error((data&&data.error)||('Error '+r.status)); er.status=r.status; throw er; }
    if(data && data.error) throw new Error(data.error);
    return data||{};
  }

  // ---------- icons (inline SVG, shell style: 24x24, stroke=currentColor) ----------
  function svg(inner,cls){
    return '<svg class="'+(cls||'sb-ico')+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
           'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+inner+'</svg>';
  }
  var I_CHEV   = svg('<path d="M9.5 5.5l6.5 6.5-6.5 6.5"/>','sb-chevsvg');
  var I_DIR    = svg('<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l1.8 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>');
  var I_DIROPEN= svg('<path d="M3 17.5V7.5A1.5 1.5 0 0 1 4.5 6h4.2l1.8 2h8A1.5 1.5 0 0 1 20 9.5v1.1"/>'+
                     '<path d="M3.3 18.1l2.3-6.5a1.5 1.5 0 0 1 1.42-1h13.1a1 1 0 0 1 .94 1.34l-2.05 5.6a1.5 1.5 0 0 1-1.41.99H4.7a1.5 1.5 0 0 1-1.4-2.03z"/>');
  var I_FILE   = svg('<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>');
  var I_CODE   = svg('<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>'+
                     '<path d="M10.4 12.8L9 14.2l1.4 1.4M13.6 12.8L15 14.2l-1.4 1.4"/>');
  var I_TEXT   = svg('<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>'+
                     '<path d="M8.4 12.2h7M8.4 15h5"/>');
  var I_IMG    = svg('<rect x="3.5" y="5" width="17" height="14" rx="2.2"/><circle cx="8.6" cy="10" r="1.4"/>'+
                     '<path d="M20.5 15.4L16 11l-8.5 8"/>');
  var I_TRASH  = svg('<path d="M4.5 6.5h15M9.5 6.5V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v1.5"/>'+
                     '<path d="M6.5 6.5l.8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12"/>');
  var I_PEN    = svg('<path d="M16.5 3.9l3.6 3.6L8.6 19h-3.6v-3.6z"/>');
  var I_TERM   = svg('<rect x="3" y="4.5" width="18" height="15" rx="2.4"/><path d="M7.5 9.5l3 2.5-3 2.5M13 15h4"/>');
  var I_NEWDIR = svg('<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l1.8 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/><path d="M12 11.5v5M9.5 14h5"/>');
  var I_SYNC   = svg('<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/>');
  var I_DL     = svg('<path d="M12 3.5v11M8 11l4 4 4-4"/><path d="M4.5 16.5v2A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-2"/>');

  // extension -> group (color in CSS) and icon shape
  var EXTMAP={ py:'py', js:'js', mjs:'js', cjs:'js', ts:'ts', tsx:'ts', jsx:'ts',
    json:'json', md:'md', markdown:'md', sh:'sh', bash:'sh', zsh:'sh', txt:'txt', log:'log',
    png:'img', jpg:'img', jpeg:'img', gif:'img', svg:'img', webp:'img', ico:'img', bmp:'img',
    html:'html', htm:'html', css:'css', scss:'css',
    yml:'cfg', yaml:'cfg', toml:'cfg', ini:'cfg', conf:'cfg', cfg:'cfg', env:'cfg' };
  function extOf(name){
    var i=name.lastIndexOf('.');
    if(i<=0) return '';
    return EXTMAP[name.slice(i+1).toLowerCase()]||'';
  }
  function fileIco(x){
    if(x==='img') return I_IMG;
    if(x==='py'||x==='js'||x==='ts'||x==='html'||x==='css'||x==='json') return I_CODE;
    if(x==='md'||x==='txt'||x==='log'||x==='cfg'||x==='sh') return I_TEXT;
    return I_FILE;
  }

  // ---------- tree model ----------
  // nodes: path -> {path,name,type,hidden,link,size,mtime,children,expanded,loaded,loading,err,truncated}
  var nodes=new Map();
  var root=null, selPath=null, flat=[], showHidden=false, inited=false;
  try{ showHidden=localStorage.getItem(K_DOT)==='1'; }catch(e){}

  function getNode(path, e){
    var n=nodes.get(path);
    if(!n){ n={ path:path, name:e?e.name:baseName(path), type:e?e.type:'dir',
      children:null, expanded:false, loaded:false, loading:false, err:null, truncated:false };
      nodes.set(path,n); }
    if(e){ n.name=e.name; n.type=e.type; n.hidden=!!e.hidden; n.link=e.link||null;
      n.size=e.size; n.mtime=e.mtime; }
    return n;
  }
  function dropSubtree(n){
    if(n.children) for(var i=0;i<n.children.length;i++) dropSubtree(n.children[i]);
    nodes.delete(n.path);
  }
  // node path change (rename/move), together with all descendants; re-key the entries in nodes
  function rekey(n, newPath){
    nodes.delete(n.path); n.path=newPath; n.name=baseName(newPath); nodes.set(newPath,n);
    if(n.children) for(var i=0;i<n.children.length;i++) rekey(n.children[i], join(newPath, n.children[i].name));
  }
  function cmpEntries(a,b){
    if((a.type==='dir')!==(b.type==='dir')) return a.type==='dir'?-1:1;
    return a.name.localeCompare(b.name,'ru',{sensitivity:'base',numeric:true});
  }

  // ---------- persisting expanded folders ----------
  function saveExp(){
    var out=[];
    nodes.forEach(function(n){ if(n.type==='dir' && n.expanded && n!==root) out.push(n.path); });
    try{ localStorage.setItem(K_EXP, JSON.stringify(out.slice(0,300))); }catch(e){}
  }
  function loadExp(){
    try{ var a=JSON.parse(localStorage.getItem(K_EXP)||'[]'); return Array.isArray(a)?a:[]; }catch(e){ return []; }
  }

  // ---------- loading a directory ----------
  async function loadDir(n, quiet){
    if(!n || n.type!=='dir' || n.loading) return;
    n.loading=true; n.err=null; if(!quiet) render();
    try{
      var d=await fsApi('fs/ls?path='+encodeURIComponent(n.path));
      var list=(d.entries||[]).slice();
      list.sort(cmpEntries);
      var kids=[], seen={};
      for(var i=0;i<list.length;i++){ var k=getNode(join(n.path,list[i].name), list[i]); kids.push(k); seen[k.path]=1; }
      // drop nodes that disappeared from disk from the index
      if(n.children) for(var j=0;j<n.children.length;j++) if(!seen[n.children[j].path]) dropSubtree(n.children[j]);
      n.children=kids; n.truncated=!!d.truncated; n.loaded=true;
    }catch(e){
      n.err=e.message||'could not read'; n.children=n.children||[]; n.loaded=true;
    }finally{
      n.loading=false; render();
    }
  }
  // re-read everything expanded (after undo / 'Refresh')
  async function reloadAll(){
    var list=[];
    nodes.forEach(function(n){ if(n.type==='dir' && n.loaded && (n===root || n.expanded)) list.push(n); });
    await Promise.all(list.map(function(n){ return loadDir(n,true); }));
    render();
  }
  window.sbRefreshTree=reloadAll;

  // ---------- rendering ----------
  function visible(n){ return showHidden || !n.hidden; }
  function note(txt, depth, cls, spin){
    var d=document.createElement('div');
    d.className='sb-note'+(cls?' '+cls:''); d.style.setProperty('--d', depth);
    if(spin){ var s=document.createElement('span'); s.className='sb-spin'; d.appendChild(s); }
    var t=document.createElement('span'); t.textContent=txt; d.appendChild(t);
    return d;
  }
  function makeRow(n, depth){
    var r=document.createElement('div');
    r.className='sb-row'+(n.expanded?' sb-open':'')+(n.hidden?' sb-hid':'')+
                (n.link?' sb-link':'')+(n.path===selPath?' sb-sel':'');
    r.style.setProperty('--d', depth);
    r.setAttribute('role','treeitem');
    r.dataset.t=n.type;
    var x=n.type==='file'?extOf(n.name):'';
    if(x) r.dataset.x=x;
    if(n.type==='dir') r.setAttribute('aria-expanded', n.expanded?'true':'false');

    var chev=document.createElement('span');
    chev.className='sb-chev'+(n.type==='dir'?'':' sb-nochev');
    chev.innerHTML=I_CHEV;                                  // static markup, not user-supplied
    r.appendChild(chev);

    var ico=document.createElement('span');
    ico.innerHTML=(n.type==='dir') ? (n.expanded?I_DIROPEN:I_DIR) : fileIco(x);
    var icoSvg=ico.firstChild; r.appendChild(icoSvg);

    var nm=document.createElement('span');
    nm.className='sb-name'; nm.textContent=n.name;          // NAME FROM DISK: textContent only
    r.appendChild(nm);
    r.title=n.link ? (n.name+' → '+n.link) : n.path;

    n._el=r; r._n=n;
    return r;
  }
  function walk(n, depth, frag){
    var kids=(n.children||[]).filter(visible);
    for(var i=0;i<kids.length;i++){
      var k=kids[i];
      frag.appendChild(makeRow(k, depth));
      flat.push(k);
      if(k.type==='dir' && k.expanded){
        if(k.loading && !k.loaded) frag.appendChild(note('Loading…', depth+1, '', true));
        else if(k.err) frag.appendChild(note(k.err, depth+1, 'sb-err'));
        else if(!(k.children||[]).filter(visible).length) frag.appendChild(note('empty', depth+1));
        else { walk(k, depth+1, frag); if(k.truncated) frag.appendChild(note('list truncated…', depth+1)); }
      }
    }
  }
  // error at ROOT LEVEL: not a red line in an empty void but a clear
  // state with a way out: retry or return to the home folder
  function rootErrState(){
    var d=document.createElement('div'); d.className='sb-state';
    var t=document.createElement('div'); t.className='sb-state-t';
    t.textContent='Folder could not be opened';
    var m=document.createElement('div'); m.className='sb-state-m'; m.textContent=root.err||'';
    d.appendChild(t); d.appendChild(m);
    var row=document.createElement('div'); row.className='sb-state-r';
    var b1=document.createElement('button'); b1.type='button'; b1.className='sb-state-btn';
    b1.textContent='Retry'; b1.onclick=function(){ loadDir(root); };
    row.appendChild(b1);
    if(norm(root.path)!==norm(HOME)){
      var b2=document.createElement('button'); b2.type='button'; b2.className='sb-state-btn';
      b2.textContent='Home (~)'; b2.onclick=function(){ setRoot(HOME); };
      row.appendChild(b2);
    }
    d.appendChild(row);
    return d;
  }
  function render(){
    if(!root) return;
    var st=treeEl.scrollTop;
    flat=[];
    var frag=document.createDocumentFragment();
    if(root.loading && !root.loaded) frag.appendChild(note('Loading…',0,'',true));
    else if(root.err) frag.appendChild(rootErrState());
    else{
      walk(root,0,frag);
      if(!flat.length) frag.appendChild(note('folder is empty',0));
      if(root.truncated) frag.appendChild(note('list truncated…',0));
    }
    treeEl.innerHTML='';
    treeEl.appendChild(frag);
    treeEl.scrollTop=st;
    renderCrumbs();
  }
  function renderCrumbs(){
    crumbsEl.innerHTML='';
    var home=norm(HOME), rp=norm(root.path);
    var mk=function(label,path,last){
      var b=document.createElement('button');
      b.type='button'; b.className='sb-crumb'+(last?' sb-crumb-last':'');
      b.textContent=label; b.title=path;
      b.onclick=function(){ if(!last) setRoot(path); };
      return b;
    };
    // There is nowhere to go above the home folder (the backend will not allow it), so
    // the breadcrumbs start at '~', not at a pointless and dangerous '/'.
    crumbsEl.appendChild(mk('~', home, rp===home));
    if(rp!==home && isInside(home,rp)){
      var rest=rp.slice(home.length).split('/').filter(Boolean), acc=home;
      for(var i=0;i<rest.length;i++){
        acc+='/'+rest[i];
        var s=document.createElement('span'); s.className='sb-crumb-sep'; s.textContent='/';
        crumbsEl.appendChild(s);
        crumbsEl.appendChild(mk(rest[i], acc, i===rest.length-1));
      }
    }
    crumbsEl.scrollLeft=crumbsEl.scrollWidth;               // the tail of the path matters more than the start
  }

  // ---------- root ----------
  function setRoot(path){
    path=norm(path||HOME);
    if(!isInside(norm(HOME), path)) path=norm(HOME);        // outside the sandbox: go home only
    nodes.clear(); flat=[]; selPath=null;
    root=getNode(path); root.type='dir'; root.expanded=true;
    try{ localStorage.setItem(K_ROOT, path); }catch(e){}
    render();
    loadDir(root).then(restoreExpanded);
  }
  // restore previously expanded folders (one level at a time, top down)
  async function restoreExpanded(){
    var want=loadExp().filter(function(p){ return isInside(root.path,p) && p!==root.path; });
    want.sort(function(a,b){ return a.split('/').length-b.split('/').length; });
    for(var i=0;i<want.length;i++){
      var n=nodes.get(want[i]);
      if(!n || n.type!=='dir' || n.loaded) continue;
      n.expanded=true;
      await loadDir(n,true);
    }
    render();
  }

  // ---------- select / expand ----------
  function selectNode(n){ selPath=n?n.path:null; render(); if(n&&n._el) n._el.scrollIntoView({block:'nearest'}); }
  function toggle(n, force){
    if(!n || n.type!=='dir') return;
    n.expanded = (force==null) ? !n.expanded : !!force;
    if(n.expanded && !n.loaded) loadDir(n); else render();
    saveExp();
  }
  function collapseAll(){
    nodes.forEach(function(n){ if(n!==root) n.expanded=false; });
    saveExp(); render();
  }

  // ---------- open a folder as the working directory (existing POST api/open) ----------
  // The list of agents and their labels has a SINGLE source of truth: the segments
  // of the 'Open folder' modal (#fp-modal). No second list in the code.
  function agentList(){
    var out=[], segs=document.querySelectorAll('#fp-modal .fp-seg');
    for(var i=0;i<segs.length;i++){
      var c=segs[i].getAttribute('data-cmd');
      if(c) out.push({cmd:c, label:(segs[i].textContent||c).trim()});
    }
    if(!out.length) out=[{cmd:'claude',label:'Claude'},{cmd:'shell',label:'Terminal'}];
    return out;
  }
  // remembered agent, but only if it IS in the list; otherwise default to Claude
  // (a stale localStorage value no longer launches silently)
  function savedAgent(){
    try{
      var s=localStorage.getItem('mterm_defcmd');
      if(s && agentList().some(function(a){ return a.cmd===s; })) return s;
    }catch(e){}
    return 'claude';
  }
  function agentLabel(cmd){
    var a=agentList().filter(function(x){ return x.cmd===cmd; })[0];
    return a ? a.label : cmd;
  }
  function openDir(path, cmd){
    cmd=cmd||savedAgent();
    api('open?path='+encodeURIComponent(path)+'&cmd='+encodeURIComponent(cmd), {method:'POST'})
      .then(function(){
        toast(cmd==='shell' ? ('Opened: '+path) : ('Starting '+cmd+' in '+path));
        if(window.ttEnterLastNew) window.ttEnterLastNew(); else refresh();
      })
      .catch(function(){ toast('Could not open: '+path); });
  }

  // ---------- undo ----------
  async function refreshUndo(){
    if(!undoBtn) return;
    try{
      var d=await fsApi('fs/undoable');
      undoBtn.disabled=!d.can;
      undoBtn.title=d.can ? ('Undo: '+(d.label||'last action')) : 'Nothing to undo';
      undoBtn.setAttribute('aria-label', undoBtn.title);
    }catch(e){ undoBtn.disabled=true; undoBtn.title='Nothing to undo'; }
  }
  async function doUndo(){
    if(!undoBtn || undoBtn.disabled) return;
    undoBtn.disabled=true;
    try{ var r=await fsApi('fs/undo',{method:'POST'}); toast(r.label?('Undone: '+r.label):'Undone'); }
    catch(e){ toast(e.message||'Could not undo'); }
    finally{ await reloadAll(); refreshUndo(); }
  }

  // ---------- file operations ----------
  async function opMkdir(dir, name){
    await fsApi('fs/op?op=mkdir&path='+encodeURIComponent(dir)+'&name='+encodeURIComponent(name), {method:'POST'});
  }
  async function opRename(path, name){
    await fsApi('fs/op?op=rename&path='+encodeURIComponent(path)+'&name='+encodeURIComponent(name), {method:'POST'});
  }
  async function opMove(src, dst){
    await fsApi('fs/op?op=move&src='+encodeURIComponent(src)+'&dst='+encodeURIComponent(dst), {method:'POST'});
  }
  async function opDelete(path){
    await fsApi('fs/op?op=delete&path='+encodeURIComponent(path), {method:'POST'});
  }

  // ===== DOWNLOAD FILE =====
  // Click our own link: the URL is relative (like the rest of the API),
  // so it also works under an external prefix /tt/<user>/. The download attribute
  // makes the browser SAVE the response instead of navigating the portal page to it,
  // so even an error response does not hijack the open session.
  // Deliberately no fetch+blob: the whole file would sit in the phone memory,
  // whereas here the stream goes straight to disk.
  function downloadNode(n){
    if(!n || n===root) return;
    if(n.type!=='file'){ toast('Only a file can be downloaded'); return; }
    // First ask the server whether it will serve the file, and only then click the
    // link. Otherwise a refusal ("no access", "file gone") would be saved as a file
    // with the error text, while the user saw a cheerful "Downloading" and nothing.
    fsApi('fs/stat?path='+encodeURIComponent(n.path)).then(function(){
      var a=document.createElement('a');
      a.href=API+'fs/get?path='+encodeURIComponent(n.path);
      a.download=n.name;                                      // name as in the tree
      a.rel='noopener';
      a.style.display='none';
      document.body.appendChild(a);
      a.click();
      setTimeout(function(){ if(a.parentNode) a.parentNode.removeChild(a); }, 0);
      toast('Downloading: '+n.name);
    }, function(e){ toast(e.message||'Could not download'); });
  }

  // ===== INLINE RENAME (F2) - same approach as startEdit() of the session title =====
  var renaming=false;
  function startRename(n){
    if(renaming || !n || n===root) return;
    var r=n._el; if(!r) return;
    renaming=true;
    var nameEl=r.querySelector('.sb-name'); if(!nameEl){ renaming=false; return; }
    var orig=n.name;
    var inp=document.createElement('input');
    inp.type='text'; inp.className='sb-rn'; inp.value=orig;
    inp.spellcheck=false; inp.autocapitalize='off'; inp.autocomplete='off';
    r.replaceChild(inp, nameEl);
    inp.focus();
    // select the name WITHOUT the extension (like VS Code)
    var dot=orig.lastIndexOf('.');
    try{ inp.setSelectionRange(0, (n.type==='file'&&dot>0)?dot:orig.length); }catch(e){}

    var done=false;
    function finish(save){
      if(done) return; done=true; renaming=false;
      inp.removeEventListener('keydown',onKey); inp.removeEventListener('blur',onBlur);
      var nn=(inp.value||'').trim();
      if(!save || !nn || nn===orig || nn.indexOf('/')>=0){ render(); if(nn.indexOf('/')>=0) toast('Slash is not allowed in a name'); return; }
      var parent=nodes.get(parentOf(n.path));
      var oldPath=n.path;
      rekey(n, join(parentOf(oldPath), nn));               // optimistic
      if(parent && parent.children) parent.children.sort(cmpEntries);
      selPath=n.path; render();
      opRename(oldPath, nn)
        .then(function(){ saveExp(); refreshUndo(); })
        .catch(function(e){
          rekey(n, oldPath);                                // rollback
          if(parent && parent.children) parent.children.sort(cmpEntries);
          selPath=oldPath; render(); toast(e.message||'Rename failed');
        });
    }
    function onKey(e){
      e.stopPropagation();
      if(e.key==='Enter'){ e.preventDefault(); finish(true); }
      else if(e.key==='Escape'){ e.preventDefault(); finish(false); }
    }
    function onBlur(){ finish(true); }
    inp.addEventListener('keydown',onKey); inp.addEventListener('blur',onBlur);
  }

  // ===== NEW FOLDER =====
  function uniqueName(dirNode, base){
    var taken={}; (dirNode.children||[]).forEach(function(c){ taken[c.name]=1; });
    if(!taken[base]) return base;
    for(var i=2;i<999;i++) if(!taken[base+' '+i]) return base+' '+i;
    return base+' '+Date.now();
  }
  async function newFolder(target){
    var dir = (!target) ? root : (target.type==='dir' ? target : nodes.get(parentOf(target.path)) || root);
    if(!dir.loaded) await loadDir(dir, true);
    var name=uniqueName(dir,'New folder');
    try{
      await opMkdir(dir.path, name);
      dir.expanded=true;
      await loadDir(dir, true);
      selPath=join(dir.path,name); render(); saveExp(); refreshUndo();
      var n=nodes.get(selPath); if(n) startRename(n);        // let the user rename right away
    }catch(e){ toast(e.message||'Could not create folder'); }
  }

  // ===== DELETE WITH CONFIRMATION (.fp-sheet primitive) =====
  var cfName=document.getElementById('sb-cf-name'), cfPath=document.getElementById('sb-cf-path'),
      cfNote=document.getElementById('sb-cf-note'), cfWarn=document.getElementById('sb-cf-warn'),
      cfOk=document.getElementById('sb-cf-ok'), cfCancel=document.getElementById('sb-cf-cancel');
  var cfNode=null;
  function askDelete(n){
    if(!n || n===root) return;
    cfNode=n;
    cfName.innerHTML=(n.type==='dir'?I_DIR:fileIco(extOf(n.name)));   // static icon
    var t=document.createElement('span'); t.textContent=n.name; cfName.appendChild(t);
    cfPath.textContent=n.path;
    cfNote.textContent = n.type==='dir' ? 'Delete folder?' : 'Delete file?';
    if(n.type==='dir'){
      cfWarn.hidden=false;
      var cnt=(n.children||[]).length;
      cfWarn.textContent='The folder will be deleted TOGETHER with all its contents'+
        (n.loaded&&cnt?(' ('+cnt+' item'+(cnt===1?'':'s')+' at the top level)'):'')+'.';
    } else cfWarn.hidden=true;
    cfEl.classList.remove('fp-hidden');
    setTimeout(function(){ cfCancel.focus(); },0);            // default is "Cancel"
  }
  function closeConfirm(){ cfEl.classList.add('fp-hidden'); cfNode=null; treeEl.focus({preventScroll:true}); }
  cfEl.addEventListener('click', function(e){ if(e.target.closest('[data-sb-close]')) closeConfirm(); });
  cfCancel.onclick=closeConfirm;
  cfOk.onclick=async function(){
    var n=cfNode; if(!n) return;
    closeConfirm();
    var parent=nodes.get(parentOf(n.path));
    var idx=parent&&parent.children?parent.children.indexOf(n):-1;
    if(idx>=0) parent.children.splice(idx,1);                 // optimistic
    if(selPath===n.path) selPath=null;
    render();
    try{ await opDelete(n.path); toast('Moved to trash: '+n.name); }
    catch(e){
      if(idx>=0){ parent.children.splice(idx,0,n); render(); } // rollback
      toast(e.message||'Could not delete');
    }
    refreshUndo();
  };

  // ===== CONTEXT MENU =====
  function closeMenu(){ menuEl.classList.add('fp-hidden'); menuOvl.classList.add('fp-hidden'); }
  // "Open in terminal" - an expandable agent choice right in the menu.
  // The remembered choice is marked with a dot, default is Claude; the choice is EXPLICIT,
  // a stale value stuck in localStorage is never launched silently.
  function termSubmenu(n){
    var dirPath=(n.type==='dir')?n.path:parentOf(n.path);
    var cur=savedAgent();
    var wrap=document.createElement('div'); wrap.className='sb-mterm';
    var b=document.createElement('button'); b.type='button'; b.className='sb-mi';
    var i=document.createElement('span'); i.innerHTML=I_TERM; b.appendChild(i.firstChild);
    var t=document.createElement('span'); t.textContent='Open in terminal'; b.appendChild(t);
    var hint=document.createElement('span'); hint.className='sb-mi-key'; hint.textContent=agentLabel(cur);
    b.appendChild(hint);
    var chev=document.createElement('span'); chev.className='sb-mi-chev'; chev.innerHTML=I_CHEV;
    b.appendChild(chev);
    var sub=document.createElement('div'); sub.className='sb-msub'; sub.hidden=true;
    agentList().forEach(function(a){
      var ab=document.createElement('button'); ab.type='button';
      ab.className='sb-mi sb-mi-agent'+(a.cmd===cur?' sb-mi-cur':'');
      var d=document.createElement('span'); d.className='sb-mi-dot'; ab.appendChild(d);
      var l=document.createElement('span'); l.textContent=a.label; ab.appendChild(l);
      ab.onclick=function(){
        try{ localStorage.setItem('mterm_defcmd', a.cmd); }catch(e){}
        closeMenu();
        openDir(dirPath, a.cmd);
      };
      sub.appendChild(ab);
    });
    b.onclick=function(){
      sub.hidden=!sub.hidden;
      b.classList.toggle('sb-mi-open', !sub.hidden);
      // the menu grew - do not let it run off the bottom of the screen
      var r=menuEl.getBoundingClientRect();
      if(r.bottom>window.innerHeight-8)
        menuEl.style.top=Math.max(6, window.innerHeight-8-r.height)+'px';
    };
    wrap.appendChild(b); wrap.appendChild(sub);
    return wrap;
  }
  function openMenu(x, y, n){
    menuEl.innerHTML='';
    var items=[
      { node:termSubmenu(n) },
      // "Download" exists only for files: a folder has nothing to download, and an item
      // that always answers with an error is worse than a missing item
      (n.type==='file') ? { ico:I_DL, label:'Download', act:function(){ downloadNode(n); } } : null,
      { sep:true },
      { ico:I_PEN,    label:'Rename', key:'F2', act:function(){ startRename(n); } },
      { ico:I_TRASH,  label:'Delete', key:'Del', danger:true, act:function(){ askDelete(n); } },
      { sep:true },
      { ico:I_NEWDIR, label:'New folder', act:function(){ newFolder(n); } },
      { ico:I_SYNC,   label:'Refresh', act:function(){
          var d=(n.type==='dir')?n:(nodes.get(parentOf(n.path))||root); loadDir(d); } }
    ];
    items.forEach(function(it){
      if(!it) return;
      if(it.node){ menuEl.appendChild(it.node); return; }
      if(it.sep){ var s=document.createElement('div'); s.className='sb-msep'; menuEl.appendChild(s); return; }
      var b=document.createElement('button');
      b.type='button'; b.className='sb-mi'+(it.danger?' sb-mi-danger':'');
      var i=document.createElement('span'); i.innerHTML=it.ico; b.appendChild(i.firstChild);
      var t=document.createElement('span'); t.textContent=it.label; b.appendChild(t);
      if(it.key){ var k=document.createElement('span'); k.className='sb-mi-key'; k.textContent=it.key; b.appendChild(k); }
      b.onclick=function(){ closeMenu(); it.act(); };
      menuEl.appendChild(b);
    });
    menuOvl.classList.remove('fp-hidden'); menuEl.classList.remove('fp-hidden');
    menuEl.style.left='0px'; menuEl.style.top='0px';
    var r=menuEl.getBoundingClientRect();
    var left=Math.min(x, window.innerWidth-r.width-8);
    var top =Math.min(y, window.innerHeight-r.height-8);
    menuEl.style.left=Math.max(6,left)+'px';
    menuEl.style.top =Math.max(6,top)+'px';
  }
  menuOvl.addEventListener('mousedown', closeMenu);
  menuOvl.addEventListener('contextmenu', function(e){ e.preventDefault(); closeMenu(); });

  // ===== TREE EVENTS =====
  treeEl.addEventListener('mousedown', function(e){
    if(e.target.closest('.sb-rn')) return;
    treeEl.focus({preventScroll:true});                       // take focus from the iframe, otherwise F2/Del will not arrive
  });
  treeEl.addEventListener('click', function(e){
    var r=e.target.closest('.sb-row'); if(!r||!r._n) return;
    if(r._dragged) return;
    if(e.detail>1) return;                                    // the second click is handled by dblclick
    var n=r._n;
    selectNode(n);
    if(n.type==='dir') toggle(n);
  });
  treeEl.addEventListener('dblclick', function(e){
    var r=e.target.closest('.sb-row'); if(!r||!r._n) return;
    var n=r._n;
    if(n.type==='dir'){ toggle(n, true); openDir(n.path); }   // double click on a folder = working directory
  });
  treeEl.addEventListener('contextmenu', function(e){
    var r=e.target.closest('.sb-row');
    e.preventDefault();
    if(!r||!r._n){ return; }
    selectNode(r._n);
    openMenu(e.clientX, e.clientY, r._n);
  });
  treeEl.addEventListener('keydown', function(e){
    if(renaming) return;
    var n=selPath?nodes.get(selPath):null;
    var i=n?flat.indexOf(n):-1;
    if(e.key==='ArrowDown'){ e.preventDefault(); if(flat.length) selectNode(flat[Math.min(flat.length-1,i+1)]||flat[0]); }
    else if(e.key==='ArrowUp'){ e.preventDefault(); if(flat.length) selectNode(flat[Math.max(0,i-1)]||flat[0]); }
    else if(e.key==='ArrowRight'){ e.preventDefault();
      if(n&&n.type==='dir'){ if(!n.expanded) toggle(n,true); else if(flat[i+1]) selectNode(flat[i+1]); } }
    else if(e.key==='ArrowLeft'){ e.preventDefault();
      if(n&&n.type==='dir'&&n.expanded) toggle(n,false);
      else if(n){ var p=nodes.get(parentOf(n.path)); if(p&&p!==root) selectNode(p); } }
    else if(e.key==='Enter'){ e.preventDefault(); if(n&&n.type==='dir') toggle(n); }
    else if(e.key==='F2'){ e.preventDefault(); if(n) startRename(n); }
    else if(e.key==='Delete'||e.key==='Backspace'&&e.metaKey){ e.preventDefault(); if(n) askDelete(n); }
    else if(e.key==='Escape'){ closeMenu(); }
  });

  // ===== DRAG-AND-DROP (op=move). Techniques from attachAvaDrag/attachTabDrag. =====
  var DRAG_TH=6;
  treeEl.addEventListener('pointerdown', function(e){
    if(e.button===2 || renaming) return;
    var r=e.target.closest('.sb-row'); if(!r||!r._n||r._n===root) return;
    startDrag(r, r._n, e);
  });
  function startDrag(rowNode, n, ev0){
    var sx=ev0.clientX, sy=ev0.clientY, cx=sx, cy=sy;
    var active=false, ghost=null, gw=0, gh=0, hot=null, hotRoot=false, why='', expTimer=null, expFor=null;
    rowNode._dragged=false;

    function makeGhost(){
      var g=document.createElement('div'); g.className='sb-ghost';
      var i=document.createElement('span');
      i.innerHTML=(n.type==='dir'?I_DIR:fileIco(extOf(n.name)));
      g.appendChild(i.firstChild);
      var t=document.createElement('span'); t.textContent=n.name; g.appendChild(t);   // name via textContent
      var w=document.createElement('span'); w.className='sb-ghost-why'; g.appendChild(w);
      document.body.appendChild(g);
      var gr=g.getBoundingClientRect(); gw=gr.width; gh=gr.height;
      return g;
    }
    function place(x,y,scale){
      if(!ghost) return;
      ghost.style.transform='translate('+(x+12)+'px,'+(y-gh/2)+'px) scale('+(scale||1)+')';
    }
    function begin(){
      active=true; rowNode._dragged=true;
      document.body.classList.add('sb-dragging');
      rowNode.classList.add('sb-drag-src');
      ghost=makeGhost(); place(cx,cy,1);
      closeMenu();
    }
    // the reason for refusal is computed ON THE FRONTEND, before any request
    function reason(dstDir){
      if(!dstDir) return 'no target';
      if(dstDir.path===parentOf(n.path)) return 'already here';
      if(n.type==='dir' && isInside(n.path, dstDir.path)) return 'folder into itself';
      if(dstDir.loaded && (dstDir.children||[]).some(function(c){ return c.name===n.name; })) return 'name taken';
      return '';
    }
    function setHot(target, isRoot){
      if(hot && hot._el) hot._el.classList.remove('sb-drop');
      treeEl.classList.remove('sb-drop-root');
      hot=target; hotRoot=isRoot;
      why=target?reason(target):'';
      if(ghost){
        ghost.classList.toggle('sb-bad', !!(target&&why));
        ghost.lastChild.textContent = (target&&why)?('— '+why):'';
      }
      if(target && !why){
        if(isRoot) treeEl.classList.add('sb-drop-root');
        else if(target._el) target._el.classList.add('sb-drop');
      }
    }
    function updateHot(){
      var el=document.elementFromPoint(cx,cy);                 // ghost — pointer-events:none
      var row=el&&el.closest?el.closest('.sb-row'):null;
      var inTree=el&&el.closest?el.closest('.sb-tree'):null;
      var target=null, isRoot=false;
      if(row&&row._n){
        target=(row._n.type==='dir')?row._n:nodes.get(parentOf(row._n.path));
        if(target&&target.path===root.path) isRoot=(target===root);
      } else if(inTree){ target=root; isRoot=true; }
      if(target!==hot) setHot(target,isRoot);
      // hovering over a collapsed folder expands it (like VS Code)
      var overDir=(row&&row._n&&row._n.type==='dir')?row._n:null;
      if(overDir!==expFor){ expFor=overDir; clearTimeout(expTimer);
        if(overDir&&!overDir.expanded) expTimer=setTimeout(function(){ toggle(overDir,true); },650); }
      // autoscroll near the edges
      var tr=treeEl.getBoundingClientRect();
      if(cy<tr.top+20) treeEl.scrollTop-=7;
      else if(cy>tr.bottom-20) treeEl.scrollTop+=7;
    }
    function move(e){
      cx=e.clientX; cy=e.clientY;
      if(!active){
        if(Math.abs(cx-sx)<DRAG_TH && Math.abs(cy-sy)<DRAG_TH) return;
        begin();
      }
      e.preventDefault();
      place(cx,cy,1);
      updateHot();
    }
    function cleanup(){
      clearTimeout(expTimer);
      document.removeEventListener('pointermove',move,true);
      document.removeEventListener('pointerup',up,true);
      document.removeEventListener('pointercancel',up,true);
      document.removeEventListener('keydown',key,true);
    }
    // Escape during a drag = cancel: the ghost springs back,
    // no request. Capture + stopPropagation so that the global Escape
    // (closing a menu/dialog) does not fire a second time on the same keypress.
    function key(e){
      if(e.key!=='Escape' || !active) return;
      e.preventDefault(); e.stopPropagation();
      if(typeof e.stopImmediatePropagation==='function') e.stopImmediatePropagation();
      cleanup();
      if(hot&&hot._el) hot._el.classList.remove('sb-drop');
      treeEl.classList.remove('sb-drop-root');
      document.body.classList.remove('sb-dragging');
      rowNode.classList.remove('sb-drag-src');
      springBack();
      setTimeout(function(){ rowNode._dragged=false; },60);
    }
    function springBack(){
      // missed the target - the ghost springs back to the row and fades
      var g=ghost; ghost=null;
      if(!g) return;
      var r=rowNode.getBoundingClientRect();
      g.classList.add('sb-anim');
      g.style.transform='translate('+(r.left+18)+'px,'+(r.top+r.height/2-gh/2)+'px) scale(.9)';
      setTimeout(function(){ g.style.opacity='0'; },160);
      setTimeout(function(){ if(g.parentNode) g.parentNode.removeChild(g); },320);
    }
    function up(){
      cleanup();
      if(!active){ return; }
      var dst=hot, bad=why;
      if(hot&&hot._el) hot._el.classList.remove('sb-drop');
      treeEl.classList.remove('sb-drop-root');
      document.body.classList.remove('sb-dragging');
      rowNode.classList.remove('sb-drag-src');
      if(!dst || bad){
        if(bad) toast('Not allowed: '+bad);
        springBack();
      }else{
        if(ghost){ var g=ghost; ghost=null; g.classList.add('sb-anim'); g.style.opacity='0';
          setTimeout(function(){ if(g.parentNode) g.parentNode.removeChild(g); },260); }
        doMove(n, dst);
      }
      setTimeout(function(){ rowNode._dragged=false; },60);
    }
    document.addEventListener('pointermove',move,true);
    document.addEventListener('pointerup',up,true);
    document.addEventListener('pointercancel',up,true);
    document.addEventListener('keydown',key,true);
  }
  // optimistic move with rollback based on the server response
  async function doMove(n, dst){
    var oldParent=nodes.get(parentOf(n.path));
    var oldPath=n.path, oldIdx=(oldParent&&oldParent.children)?oldParent.children.indexOf(n):-1;
    if(oldIdx>=0) oldParent.children.splice(oldIdx,1);
    rekey(n, join(dst.path, n.name));
    if(dst.loaded){ dst.children=dst.children||[]; dst.children.push(n); dst.children.sort(cmpEntries); }
    dst.expanded=true; selPath=n.path; render();
    try{
      await opMove(oldPath, dst.path);
      toast('Moved to '+baseName(dst.path));
      saveExp(); refreshUndo();
      if(!dst.loaded) loadDir(dst,true);
    }catch(e){
      // rollback: return the node to the original parent
      if(dst.children){ var i=dst.children.indexOf(n); if(i>=0) dst.children.splice(i,1); }
      rekey(n, oldPath);
      if(oldParent){ oldParent.children=oldParent.children||[];
        oldParent.children.splice(oldIdx<0?oldParent.children.length:oldIdx,0,n); }
      selPath=oldPath; render();
      toast(e.message||'Could not move');
    }
  }

  // ===== FILE SECTION TOOLBAR =====
  document.getElementById('sb-newdir').onclick=function(){ newFolder(selPath?nodes.get(selPath):null); };
  document.getElementById('sb-collapse').onclick=collapseAll;
  document.getElementById('sb-refresh').onclick=function(){ reloadAll(); refreshUndo(); };
  function applyDot(){
    dotBtn.classList.toggle('sb-tbtn-on', showHidden);
    dotBtn.setAttribute('aria-pressed', showHidden?'true':'false');
    dotBtn.title=showHidden?'Hide hidden files':'Show hidden files';
  }
  dotBtn.onclick=function(){
    showHidden=!showHidden;
    try{ showHidden?localStorage.setItem(K_DOT,'1'):localStorage.removeItem(K_DOT); }catch(e){}
    applyDot(); render();
  };
  applyDot();

  // ===== PANEL WIDTH (resizer) =====
  function setW(w){
    w=Math.max(W_MIN, Math.min(W_MAX, Math.round(w)));
    document.documentElement.style.setProperty('--sb-w', w+'px');
    return w;
  }
  var curW=W_DEF;
  try{ var sw=parseInt(localStorage.getItem(K_W),10); if(sw>=W_MIN&&sw<=W_MAX) curW=sw; }catch(e){}
  setW(curW);
  resizer.addEventListener('pointerdown', function(e){
    e.preventDefault();
    var left=sidebar.getBoundingClientRect().left;
    document.body.classList.add('sb-resizing');
    function mv(ev){ curW=setW(ev.clientX-left); }
    function up(){
      document.body.classList.remove('sb-resizing');
      document.removeEventListener('pointermove',mv,true);
      document.removeEventListener('pointerup',up,true);
      document.removeEventListener('pointercancel',up,true);
      try{ localStorage.setItem(K_W, String(curW)); }catch(e){}
      nudgeTerm();                                            // width changed - notify xterm
    }
    document.addEventListener('pointermove',mv,true);
    document.addEventListener('pointerup',up,true);
    document.addEventListener('pointercancel',up,true);
  });

  // ===== "CHATS / FILES" DIVIDER =====
  var split=.4;
  try{ var sp=parseFloat(localStorage.getItem(K_SPLIT)); if(sp>=.1&&sp<=.75) split=sp; }catch(e){}
  function applySplit(){ topEl.style.height=(split*100).toFixed(2)+'%'; }
  applySplit();
  hsplit.addEventListener('pointerdown', function(e){
    e.preventDefault();
    var box=sidebar.getBoundingClientRect();
    document.body.classList.add('sb-vresizing');
    function mv(ev){
      split=Math.max(.10, Math.min(.72, (ev.clientY-box.top)/Math.max(1,box.height)));
      applySplit();
    }
    function up(){
      document.body.classList.remove('sb-vresizing');
      document.removeEventListener('pointermove',mv,true);
      document.removeEventListener('pointerup',up,true);
      document.removeEventListener('pointercancel',up,true);
      try{ localStorage.setItem(K_SPLIT, split.toFixed(3)); }catch(e){}
    }
    document.addEventListener('pointermove',mv,true);
    document.addEventListener('pointerup',up,true);
    document.addEventListener('pointercancel',up,true);
  });

  // ===== NOTIFY THE TERMINAL ABOUT THE NEW WIDTH =====
  // #wrap is squeezed by flex, the iframe resizes -> its window gets resize itself,
  // but ttyd hooks fit on window.resize with a debounce: we dispatch the event explicitly
  // so that xterm recalculates cols/rows right after the animation/drag. We do NOT call reloadTerm -
  // every reload = a new tmux-attach and zombie clients (see the comment at autoReload).
  function nudgeTerm(){
    try{
      var w=document.getElementById('term').contentWindow;
      if(w) w.dispatchEvent(new w.Event('resize'));
    }catch(e){}
  }
  sidebar.addEventListener('transitionend', function(e){ if(e.propertyName==='width') nudgeTerm(); });

  // ===== PANEL TOGGLE =====
  function isOpen(){ return document.body.classList.contains('sb-on'); }
  function setOpen(on){
    document.body.classList.toggle('sb-on', on);
    if(sideBtn){ sideBtn.setAttribute('aria-pressed', on?'true':'false');
      sideBtn.title = on?'Hide files panel':'Show files panel'; }
    try{ localStorage.setItem(K_OPEN, on?'1':'0'); }catch(e){}
    if(on && !inited){ inited=true; initTree(); }
    if(on) setTimeout(function(){ treeEl.focus({preventScroll:true}); }, 240);
    setTimeout(nudgeTerm, 260);
  }
  window.sbToggleSidebar=function(on){ setOpen(on==null?!isOpen():!!on); };
  if(sideBtn) sideBtn.onclick=function(){ setOpen(!isOpen()); };
  if(undoBtn) undoBtn.onclick=doUndo;

  function initTree(){
    // First ask the server for its root (fs/ls without path = WORKDIR): this
    // fixes the main failure scenario - localStorage held "/" or another
    // path outside the sandbox, and every visit hit "path outside the working folder".
    var boot=function(home){
      HOME=norm(home||ROOT_DEF);
      window.sbHome=HOME;                                   // for the chats panel (shortCwd/groupLabel)
      var start=HOME;
      try{ var s=localStorage.getItem(K_ROOT);
        if(s){ s=norm(s); if(isInside(HOME,s)) start=s; } }catch(e){}
      setRoot(start);                                       // setRoot clamps again and heals the key
      refreshUndo();
    };
    fsApi('fs/ls?path=').then(function(d){ boot(d&&d.path); }, function(){ boot(null); });
  }

  // ===== AVAILABLE ON DESKTOP ONLY =====
  // On a phone there is no button at all; in auto mode we watch for width/device changes.
  function syncAvail(){
    var ok=ttIsClassic();
    document.body.classList.toggle('sb-avail', ok);
    if(!ok && isOpen()) setOpen(false);
  }
  syncAvail();
  window.addEventListener('resize', function(){ if(ttModeIsAuto()) syncAvail(); });

  // restore state on load
  try{ if(ttIsClassic() && localStorage.getItem(K_OPEN)==='1') setOpen(true); }catch(e){}

  // Escape - close menu/dialog (when focus is in the document, not in the iframe)
  document.addEventListener('keydown', function(e){
    if(e.key!=='Escape') return;
    if(!menuEl.classList.contains('fp-hidden')){ e.preventDefault(); closeMenu(); return; }
    if(!cfEl.classList.contains('fp-hidden')){ e.preventDefault(); closeConfirm(); }
  });
})();

// ============================================================
//
// CLAUDE CODE CHATS PANEL - fills the #sb-chats container (created by the
// sidebar code). Self-contained IIFE: if the container is missing it quietly
// does nothing and waits for it to appear (MutationObserver), so the insertion order
// of blocks and the sidebar init order do not matter.
// DESKTOP only: built only if ttIsClassic().
// API: GET chats?limit=&project=&q= · POST chat/open?id= · chat/rename?id=&name=
//      · chat/autoname?id=
// Exposed: window.chatPanel = {init(), refresh()}.
// WARNING: chat titles are user text: ONLY textContent.
//    innerHTML is used exclusively for static SVG icons.
// ============================================================
(function(){
  var CH_LIMIT=500;                       // how many chats we ask for at once
  var PAGE=40;                            // rows per render pass
  var SEARCH_DELAY=200;                   // search debounce, ms
  var SOFT_MS=120000;                     // "soft" auto-refresh - infrequent: data changes rarely
  // which project sections are EXPANDED (list of cwd). No key - default:
  // ALL sections collapsed (only the owner or an active search expands them).
  var LS_OPEN='mterm_chats_open';
  var LS_ACTIVE='mterm_chat_active';

  // --- static icons (24x24, stroke=currentColor) ---
  var SVG_OPEN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">';
  var IC_RELOAD=SVG_OPEN+'<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>';
  var IC_SEARCH=SVG_OPEN+'<circle cx="11" cy="11" r="7"/><path d="M20.2 20.2 16.7 16.7"/></svg>';
  var IC_CHEV=SVG_OPEN+'<path d="M6 9l6 6 6-6"/></svg>';
  var IC_SPARK=SVG_OPEN+'<path d="M12 3l1.9 4.6L18.5 9.5 13.9 11.4 12 16l-1.9-4.6L5.5 9.5 10.1 7.6 12 3z"/>'+
    '<path d="M18.5 15.5l.7 1.7 1.8.8-1.8.8-.7 1.7-.7-1.7-1.8-.8 1.8-.8.7-1.7z"/></svg>';
  var IC_ENTER=SVG_OPEN+'<path d="M5 12h13M13 6l6 6-6 6"/></svg>';
  var IC_PEN=SVG_OPEN+'<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  var IC_UNDO=SVG_OPEN+'<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 3.8V8h4.2"/></svg>';
  var IC_COPY=SVG_OPEN+'<rect x="9" y="9" width="11" height="11" rx="2.2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';

  var PANEL_HTML=
    '<div class="ch-head">'+
      '<span class="ch-h-title">Chats</span>'+
      '<span class="ch-count" id="ch-count">—</span>'+
      '<span class="ch-sp"></span>'+
      '<button type="button" class="ch-ibtn" id="ch-reload" title="Refresh chat list" aria-label="Refresh">'+IC_RELOAD+'</button>'+
    '</div>'+
    '<div class="ch-searchrow" id="ch-srow">'+
      '<span class="ch-sicon">'+IC_SEARCH+'</span>'+
      '<input type="text" class="ch-search" id="ch-search" placeholder="Search chats"'+
        ' autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" aria-label="Search chats">'+
      '<button type="button" class="ch-sclear" id="ch-sclear" title="Clear search" aria-label="Clear search">✕</button>'+
    '</div>'+
    '<div class="ch-list" id="ch-list" role="listbox" aria-label="Claude Code chats"></div>';

  // ---- state ----
  var host=null, root=null, listEl=null, cntEl=null, srowEl=null, searchEl=null, reloadBtn=null;
  var chats=[], total=0, items=[], shown=0;
  // opened: null = the user has not expanded anything yet - everything collapsed
  var opened=null, activeId=null;
  var q='', qTimer=null, loading=false, loadSeq=0, lastLoad=0;
  var io=null, sentinel=null, moreBtn=null, softTimer=null, refTimer=null, obs=null;
  var menuEl=null, menuChat=null, menuRow=null, editing=false, docBound=false;

  // ---- small utilities ----
  function say(t){ if(typeof toast==='function') toast(t); else if(t) console.log(t); }
  function apiBase(){ return (typeof API==='string' && API) ? API : 'api/'; }
  // own fetch (not the shared api()): we need the error TEXT from the server, e.g.
  // 502 from chat/autoname when the model key has run out.
  function chApi(path, opts){
    return fetch(apiBase()+path, opts||{}).then(function(r){
      return r.json().catch(function(){ return null; }).then(function(d){
        if(!r.ok || (d && d.error)) throw new Error((d && d.error) || ('error '+r.status));
        return d || {};
      });
    });
  }
  function two(n){ return (n<10?'0':'')+n; }
  function chPlural(n,a,b,c){ return Math.abs(n)===1 ? a : b; }
  var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  // compact relative time, like in the Telegram list
  function relTime(ts){
    if(!ts) return '';
    var d=new Date(ts*1000), now=new Date(), s=(now.getTime()-d.getTime())/1000;
    if(s<45) return 'now';
    if(s<3600) return Math.max(1,Math.round(s/60))+' min';
    var sod=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
    var t=d.getTime();
    if(t>=sod) return two(d.getHours())+':'+two(d.getMinutes());
    if(t>=sod-86400000) return 'yesterday';
    if(d.getFullYear()===now.getFullYear()) return d.getDate()+' '+MON[d.getMonth()];
    return two(d.getDate())+'.'+two(d.getMonth()+1)+'.'+String(d.getFullYear()).slice(2);
  }
  function fullTime(ts){
    if(!ts) return '';
    var d=new Date(ts*1000);
    return d.getDate()+' '+MON[d.getMonth()]+' '+d.getFullYear()+', '+two(d.getHours())+':'+two(d.getMinutes());
  }
  // Home folder: the sidebar learns it from the server and publishes it to window.sbHome.
  // IMPORTANT: collapse to "~" ONLY the real home. Foreign /home/x and /root
  // (chats from an old server) are shown with the full path - otherwise two different
  // folders would merge into one "home".
  function homeDir(){
    return (typeof window.sbHome==='string' && window.sbHome) ? window.sbHome : HOME_DIR;
  }
  // ~/project -> ~/project ; a long path is shown as its tail
  function shortCwd(p){
    var s=String(p||'');
    if(!s) return 'no folder';
    var h=homeDir();
    if(s===h) s='~'; else if(s.indexOf(h+'/')===0) s='~'+s.slice(h.length);
    if(s.length>36){
      var parts=s.split('/').filter(Boolean);
      if(parts.length>2) s='…/'+parts.slice(-2).join('/');
    }
    return s;
  }
  // project section label: do not repeat the common ~/ prefix, call the home folder by a word
  function groupLabel(p){
    var s=String(p||'');
    if(!s) return 'no folder';
    var h=homeDir();
    if(s===h) return 'home';
    if(s.indexOf(h+'/')===0) s=s.slice(h.length+1);
    if(s.length>34){
      var parts=s.split('/').filter(Boolean);
      if(parts.length>2) s='…/'+parts.slice(-2).join('/');
    }
    return s;
  }
  function chatName(c){
    var n=(c && c.name || '').trim();
    if(n) return n;
    var t=(c && c.title || '').trim();
    return t || 'Untitled';
  }
  // match highlight: built from nodes, text only via textContent
  function fillName(el, text){
    el.textContent='';
    var t=String(text==null?'':text);
    var needle=q.trim().toLowerCase();
    if(!needle){ el.textContent=t; return; }
    var low=t.toLowerCase(), i=0, p=low.indexOf(needle), guard=0;
    if(p<0){ el.textContent=t; return; }
    while(p>=0 && guard++<120){
      if(p>i) el.appendChild(document.createTextNode(t.slice(i,p)));
      var m=document.createElement('mark'); m.className='ch-hl';
      m.textContent=t.slice(p, p+needle.length);
      el.appendChild(m);
      i=p+needle.length; p=low.indexOf(needle, i);
    }
    if(i<t.length) el.appendChild(document.createTextNode(t.slice(i)));
  }

  // ---- expanded state of sections in localStorage ----
  function loadOpened(){
    opened=null;
    try{ var a=JSON.parse(localStorage.getItem(LS_OPEN)||'null');
      if(Object.prototype.toString.call(a)==='[object Array]'){
        opened={}; for(var i=0;i<a.length;i++) opened[a[i]]=true; } }catch(e){}
  }
  function saveOpened(){
    try{ var a=[]; for(var k in opened) if(opened[k]) a.push(k);
      localStorage.setItem(LS_OPEN, JSON.stringify(a)); }catch(e){}
  }
  function isOpen(cwd){ return !!(q || (opened && opened[cwd])); }

  // ---- time bucketing within a section (like browser history) ----
  var BUCKETS=['Today','Yesterday','This week','This month','Older'];
  function bucketOf(ts){
    var now=new Date();
    var sod=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime()/1000;
    if(ts>=sod) return 0;
    if(ts>=sod-86400) return 1;
    if(ts>=sod-6*86400) return 2;
    if(ts>=sod-29*86400) return 3;
    return 4;
  }
  // group rows -> items; for a big group, with date separators
  function pushRows(rows){
    var withDates=rows.length>7 &&
      bucketOf(rows[0].mtime||0)!==bucketOf(rows[rows.length-1].mtime||0);
    var prev=-1;
    for(var r=0;r<rows.length;r++){
      if(withDates){
        var b=bucketOf(rows[r].mtime||0);
        if(b!==prev){ items.push({t:'d', label:BUCKETS[b]}); prev=b; }
      }
      items.push({t:'r', c:rows[r]});
    }
  }

  // ---- grouping by project (cwd), sections ordered by recency ----
  function buildItems(){
    var map={}, order=[];
    for(var i=0;i<chats.length;i++){
      var c=chats[i], k=c.cwd||'';
      var g=map[k];
      if(!g){ g=map[k]={cwd:k, rows:[], mtime:0}; order.push(g); }
      g.rows.push(c);
      if((c.mtime||0)>g.mtime) g.mtime=c.mtime||0;
    }
    order.sort(function(a,b){ return b.mtime-a.mtime; });
    for(var j=0;j<order.length;j++)
      order[j].rows.sort(function(a,b){ return (b.mtime||0)-(a.mtime||0); });
    // default (the user has not touched anything yet): ALL sections collapsed with no
    // exceptions - only rows "name + counter + date". Whatever is expanded by hand is
    // remembered in localStorage; search expands everything forcibly.
    // The header is skipped only when it adds nothing: one project,
    // it is the home folder and there is no search. With a search or a "foreign" project
    // the section stays - the path is no longer repeated in rows, context is needed.
    var single=order.length<2 && !q &&
      (!order.length || !order[0].cwd || order[0].cwd===homeDir());
    items=[];
    for(var n=0;n<order.length;n++){
      var gr=order[n];
      gr.open=single || isOpen(gr.cwd);
      if(!single){
        items.push({t:'g', g:gr});
        if(!gr.open) continue;
      }
      pushRows(gr.rows);
    }
  }
  function toggleGroup(g){
    if(q) return;                     // with a search everything is expanded forcibly
    if(opened===null) opened={};      // first click: the default becomes explicit
    if(opened[g.cwd]) delete opened[g.cwd]; else opened[g.cwd]=true;
    saveOpened(); buildItems(); renderAll(true);
  }

  // ---- render in batches: hundreds of chats are not inserted at once ----
  function detachIO(){ if(io){ try{ io.disconnect(); }catch(e){} io=null; } }
  function dropTail(){
    if(sentinel && sentinel.parentNode) sentinel.parentNode.removeChild(sentinel);
    if(moreBtn && moreBtn.parentNode) moreBtn.parentNode.removeChild(moreBtn);
    moreBtn=null;
  }
  function appendChunk(n){
    dropTail();
    var end=Math.min(items.length, shown+n);
    var frag=document.createDocumentFragment();
    for(var i=shown;i<end;i++){
      var it=items[i];
      frag.appendChild(it.t==='g' ? groupNode(it.g) :
                       it.t==='d' ? dateNode(it.label) : rowNode(it.c));
    }
    shown=end;
    listEl.appendChild(frag);
    ensureTail();
  }
  function ensureTail(){
    detachIO();
    if(shown>=items.length) return;
    if(!sentinel){ sentinel=document.createElement('div'); sentinel.className='ch-sent'; }
    listEl.appendChild(sentinel);
    if(typeof IntersectionObserver==='function'){
      io=new IntersectionObserver(function(es){
        for(var i=0;i<es.length;i++) if(es[i].isIntersecting){ appendChunk(PAGE); return; }
      }, {root:listEl, rootMargin:'260px 0px'});
      io.observe(sentinel);
    }else{
      moreBtn=document.createElement('button'); moreBtn.type='button'; moreBtn.className='ch-more';
      moreBtn.textContent='Show more '+(items.length-shown);
      moreBtn.onclick=function(){ appendChunk(PAGE); };
      listEl.insertBefore(moreBtn, sentinel);
    }
  }
  function renderAll(keep){
    if(!listEl) return;
    var prevShown=shown, top=keep? listEl.scrollTop : 0;
    detachIO(); dropTail();
    listEl.textContent=''; shown=0;
    if(!items.length){ renderEmpty(); return; }
    // on an in-place redraw restore as many rows as there were
    var want=keep ? Math.min(items.length, Math.max(PAGE, prevShown)) : PAGE;
    appendChunk(want);
    if(keep) listEl.scrollTop=top;
  }
  function stateNode(title, sub, btnText, onBtn){
    var d=document.createElement('div'); d.className='ch-state';
    var b=document.createElement('b'); b.textContent=title; d.appendChild(b);
    if(sub){ var s=document.createElement('span'); s.className='ch-q'; s.textContent=sub; d.appendChild(s); }
    if(btnText){
      d.appendChild(document.createElement('br'));
      var btn=document.createElement('button'); btn.type='button'; btn.className='ch-link';
      btn.textContent=btnText; btn.onclick=onBtn; d.appendChild(btn);
    }
    return d;
  }
  function renderEmpty(){
    listEl.textContent='';
    if(loading){ listEl.appendChild(stateNode('Loading chats…','')); return; }
    if(q) listEl.appendChild(stateNode('Nothing found', 'for "'+q+'"', 'Clear search', clearSearch));
    else listEl.appendChild(stateNode('No chats yet', 'Run Claude in any folder - the chat will appear here', 'Refresh', function(){ load(); }));
  }

  // ---- project section node ----
  function groupNode(g){
    var b=document.createElement('button'); b.type='button';
    b.className='ch-ghead'+(g.open?'':' ch-gcol');
    b.title=(g.cwd||'')+'\n'+g.rows.length+' '+chPlural(g.rows.length,'chat','chats','chats')+
      ' · last: '+fullTime(g.mtime);
    var ch=document.createElement('span'); ch.className='ch-chev'; ch.innerHTML=IC_CHEV;  // static icon
    var nm=document.createElement('span'); nm.className='ch-gname'; nm.textContent=groupLabel(g.cwd);
    var cn=document.createElement('span'); cn.className='ch-gcnt'; cn.textContent=g.rows.length;
    var sp=document.createElement('span'); sp.className='ch-gsp';
    var tm=document.createElement('span'); tm.className='ch-gtime'; tm.textContent=relTime(g.mtime);
    b.appendChild(ch); b.appendChild(nm); b.appendChild(cn); b.appendChild(sp); b.appendChild(tm);
    b.setAttribute('aria-expanded', g.open?'true':'false');
    b.onclick=function(){ toggleGroup(g); };
    return b;
  }

  // ---- time separator within a section ----
  function dateNode(label){
    var d=document.createElement('div'); d.className='ch-dsep';
    d.textContent=label;
    return d;
  }

  // ---- chat row node ----
  function rowNode(c){
    var row=document.createElement('div');
    row.className='ch-row';
    row.dataset.id=c.id||'';
    row.tabIndex=0; row.setAttribute('role','option');
    row._chat=c;

    var top=document.createElement('div'); top.className='ch-top';
    var nm=document.createElement('span'); nm.className='ch-name';
    var tm=document.createElement('span'); tm.className='ch-time';
    top.appendChild(nm); top.appendChild(tm);

    row.appendChild(top);    row.appendChild(top); row.appendChild(ai);
    applyRow(row, c);

    // Click on a row = open the chat. Double click on the TITLE = rename,
    // so a click on the title opens with a 220 ms delay (the spinner
    // starts at once - feels instant), and dblclick cancels that delay.
    row.addEventListener('click', function(e){
      if(row._edit) return;
      if(e.target.closest && e.target.closest('.ch-input')) return;
      if(e.target.closest && e.target.closest('.ch-name')){
        if(row._openT) return;
        row.classList.add('ch-loading');
        row._openT=setTimeout(function(){ row._openT=null; row.classList.remove('ch-loading'); openChat(c,row); }, 220);
        return;
      }
      openChat(c, row);
    });
    row.addEventListener('dblclick', function(e){
      if(row._openT){ clearTimeout(row._openT); row._openT=null; row.classList.remove('ch-loading'); }
      e.preventDefault();
      startRename(c, row);
    });
    row.addEventListener('contextmenu', function(e){ e.preventDefault(); openMenu(c, row, e.clientX, e.clientY); });
    row.addEventListener('keydown', function(e){
      if(row._edit) return;
      if(e.key==='Enter'){ e.preventDefault(); openChat(c, row); }
      else if(e.key==='F2'){ e.preventDefault(); startRename(c, row); }
    });
    return row;
  }
  // row name/marks - without rebuilding the DOM
  function applyRow(row, c){
    var nm=row.querySelector('.ch-name'), tm=row.querySelector('.ch-time');
    var name=chatName(c);
    if(nm) fillName(nm, name);
    if(tm){ tm.textContent=relTime(c.mtime); }
    // whose name is it: set by the owner (in the panel or in Claude Code) or by a machine
    var ownName=!!(c.name && String(c.name).trim()) || c.tsrc==='own';
    var aiName=!ownName && (c.tsrc==='ai' || c.tsrc==='sum');
    // everything secondary (message count, exact time, folder) goes in the tooltip,
    // not in the row: the row is entirely given to the title
    row.title=name+'\n'+
      (ownName ? 'Title: yours' : aiName ? 'Title: automatic (AI)' :
        c.tsrc==='date' ? 'Title: by date' : 'Title: first message')+'\n'+
      (c.msgs ? (c.msgs+' '+chPlural(c.msgs,'message','messages','messages')+' · ') : '')+
      fullTime(c.mtime)+
      (c.cwd ? ('\n'+shortCwd(c.cwd)) : '');
    row.classList.toggle('ch-named', ownName);
    row.classList.toggle('ch-auto', aiName);
    row.classList.toggle('ch-on', !!(activeId && c.id===activeId));
    row.setAttribute('aria-selected', (activeId && c.id===activeId) ? 'true' : 'false');
  }
  function markActive(){
    if(!listEl) return;
    var rows=listEl.querySelectorAll('.ch-row');
    for(var i=0;i<rows.length;i++){
      var on=!!(activeId && rows[i].dataset.id===activeId);
      rows[i].classList.toggle('ch-on', on);
      rows[i].setAttribute('aria-selected', on?'true':'false');
    }
  }

  // ---- open chat: POST chat/open -> redraw tabs -> terminal ----
  function openChat(c, row){
    if(!c || !c.id || row._busy) return;
    closeMenu();
    row._busy=true; row.classList.add('ch-loading');
    var qs='chat/open?id='+encodeURIComponent(c.id);
    chApi(qs, {method:'POST'}).then(function(r){
      activeId=c.id;
      try{ localStorage.setItem(LS_ACTIVE, c.id); }catch(e){}
      markActive();
      applyWindows(r && r.windows);
      toTerminal();
      // warn = the chat directory is gone: the window opened anyway, but the user must be warned
      if(r && r.warn) say(r.warn);
      else say('Opened chat: '+chatName(c));
      scheduleRefresh(1500);                       // mtime/list will change - refresh once
    }).catch(function(e){
      say('Chat did not open: '+(e.message||'error'));
    }).then(function(){
      row._busy=false; row.classList.remove('ch-loading');
    });
  }
  // redraw the tab strip from the server response (like in select()/joinWin())
  function applyWindows(wins){
    try{
      if(wins && wins.length){
        if(typeof winsSig==='function') _winsSig=winsSig(wins);   // reset the tab strip dedup
        if(typeof render==='function') render(wins, true);
        var a=null;
        for(var i=0;i<wins.length;i++) if(wins[i].active) a=wins[i];
        if(a) lastActive=a.index;
        if(typeof updateIndicators==='function') updateIndicators(wins);
      }else if(typeof refresh==='function'){ refresh(); }
    }catch(e){ if(typeof refresh==='function') refresh(); }
    if(typeof window.ttRefreshList==='function') window.ttRefreshList();   // sessions list 2.0
  }
  function toTerminal(){
    // mobile mode 2.0: the session list screen lies on top of the terminal - go to the terminal
    if(document.body.classList.contains('tt-2') && typeof window.ttShowTerm==='function') window.ttShowTerm();
    try{ var f=document.getElementById('term'); if(f && f.contentWindow) f.contentWindow.focus(); }catch(e){}
  }

  // ---- inline rename (same approach as startEdit() from the bar) ----
  function startRename(c, row){
    if(row._edit) return;
    var nm=row.querySelector('.ch-name'); if(!nm) return;
    closeMenu();
    row._edit=true; editing=true;
    var top=nm.parentNode;
    var inp=document.createElement('input');
    inp.type='text'; inp.className='ch-input'; inp.maxLength=80;
    inp.value=(c.name||'');
    inp.placeholder=(c.title||'chat name').slice(0,60);
    inp.autocomplete='off'; inp.spellcheck=false;
    inp.title='Enter - save, Esc - cancel, empty - restore the automatic title';
    nm.hidden=true; top.insertBefore(inp, nm);
    inp.focus(); inp.select();

    function stop(e){ e.stopPropagation(); }
    inp.addEventListener('click', stop);
    inp.addEventListener('dblclick', stop);
    inp.addEventListener('mousedown', stop);
    inp.addEventListener('keydown', function(e){
      if(e.key==='Enter'){ e.preventDefault(); finish(true); }
      else if(e.key==='Escape'){ e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    inp.addEventListener('blur', function(){ finish(true); });

    function finish(save){
      if(!row._edit) return;
      row._edit=false; editing=false;
      var val=(inp.value||'').trim().slice(0,80);
      if(inp.parentNode) inp.parentNode.removeChild(inp);
      nm.hidden=false;
      if(!save) return;
      var old=c.name||'';
      if(val===old) return;
      c.name=val||null; applyRow(row, c);          // optimistic - the UI does not wait for the network
      chApi('chat/rename?id='+encodeURIComponent(c.id)+'&name='+encodeURIComponent(val), {method:'POST'})
        .then(function(r){
          if(r && typeof r.name!=='undefined'){ c.name=r.name||null; applyRow(row, c); }
          say(val ? 'Name saved' : 'Name reset to automatic');
        })
        .catch(function(e){
          c.name=old||null; applyRow(row, c);
          say('Rename failed: '+(e.message||'error'));
        });
    }
  }

  // ---- open a chat ----
  function chEngineItems(c, row){ /* single engine: nothing extra to offer */ }

  // ---- row context menu ----
  function menuItem(label, iconHtml, fn){
    var b=document.createElement('button'); b.type='button'; b.className='ch-mitem';
    b.innerHTML=iconHtml;                                   // static icon
    var s=document.createElement('span'); s.textContent=label; b.appendChild(s);
    b.onclick=function(e){ e.stopPropagation(); closeMenu(); fn(); };
    return b;
  }
  function openMenu(c, row, x, y){
    closeMenu();
    menuChat=c; menuRow=row;
    menuEl=document.createElement('div'); menuEl.className='ch-menu'; menuEl.setAttribute('role','menu');
    var h=document.createElement('div'); h.className='ch-mhead'; h.textContent=chatName(c);
    menuEl.appendChild(h);
    menuEl.appendChild(menuItem('Open chat', IC_ENTER, function(){ openChat(c, row); }));
    chEngineItems(c, row);
    menuEl.appendChild(menuItem('Rename', IC_PEN, function(){ startRename(c, row); }));
    if(c.name) menuEl.appendChild(menuItem('Reset name', IC_UNDO, function(){ resetName(c, row); }));
    menuEl.appendChild(menuItem('Copy path', IC_COPY, function(){ copyText(c.cwd||'', 'Path copied'); }));
    menuEl.appendChild(menuItem('Copy chat id', IC_COPY, function(){ copyText(c.id||'', 'ID copied'); }));
    document.body.appendChild(menuEl);
    var r=menuEl.getBoundingClientRect();
    var lx=Math.min(x, window.innerWidth-r.width-8), ly=Math.min(y, window.innerHeight-r.height-8);
    menuEl.style.left=Math.max(8,lx)+'px'; menuEl.style.top=Math.max(8,ly)+'px';
    setTimeout(function(){
      document.addEventListener('mousedown', onDocDown, true);
      document.addEventListener('keydown', onMenuKey, true);
      window.addEventListener('resize', closeMenu);
      if(listEl) listEl.addEventListener('scroll', closeMenu, {passive:true});
    },0);
  }
  function onDocDown(e){ if(menuEl && !menuEl.contains(e.target)) closeMenu(); }
  function onMenuKey(e){ if(e.key==='Escape'){ e.preventDefault(); closeMenu(); } }
  function closeMenu(){
    if(!menuEl) return;
    document.removeEventListener('mousedown', onDocDown, true);
    document.removeEventListener('keydown', onMenuKey, true);
    window.removeEventListener('resize', closeMenu);
    if(listEl) listEl.removeEventListener('scroll', closeMenu);
    if(menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
    menuEl=null; menuChat=null; menuRow=null;
  }
  function resetName(c, row){
    var old=c.name||'';
    c.name=null; applyRow(row, c);
    chApi('chat/rename?id='+encodeURIComponent(c.id)+'&name=', {method:'POST'})
      .then(function(){ say('Name reset to automatic'); })
      .catch(function(e){ c.name=old||null; applyRow(row, c); say('Reset failed: '+(e.message||'error')); });
  }
  function copyText(t, ok){
    if(!t){ say('Nothing to copy'); return; }
    try{
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(t).then(function(){ say(ok); }, function(){ say('Clipboard unavailable'); });
        return;
      }
    }catch(e){}
    try{
      var ta=document.createElement('textarea'); ta.value=t;
      ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta);
      ta.select(); document.execCommand('copy'); document.body.removeChild(ta); say(ok);
    }catch(e){ say('Clipboard unavailable'); }
  }

  // ---- load the list ----
  function setSpin(on){ if(reloadBtn) reloadBtn.classList.toggle('ch-spin', !!on); }
  function updateCount(){
    if(!cntEl) return;
    var n=chats.length;
    cntEl.textContent = (total>n) ? (n+'/'+total) : String(n);
    cntEl.title = (q ? 'Found ' : 'Total ')+n+' '+chPlural(n,'chat','chats','chats')+
      (total>n ? (' of '+total) : '');
  }
  function load(opts){
    if(!listEl) return Promise.resolve();
    var seq=++loadSeq, keep=!!(opts && opts.keep);
    loading=true; setSpin(true);
    if(!chats.length) renderEmpty();                        // "Loading chats..."
    var path='chats?limit='+CH_LIMIT+(q ? ('&q='+encodeURIComponent(q)) : '');
    return chApi(path).then(function(d){
      if(seq!==loadSeq) return;                             // stale response (fast typing in search)
      loading=false; setSpin(false);                        // BEFORE render: otherwise an empty result
      chats=(d && d.chats) || [];                           // would show "Loading..." instead of the placeholder
      total=(d && typeof d.total==='number') ? d.total : chats.length;
      lastLoad=Date.now();
      buildItems(); renderAll(keep); updateCount();
    }).catch(function(e){
      if(seq!==loadSeq) return;
      loading=false; setSpin(false);
      if(!chats.length){
        listEl.textContent='';
        listEl.appendChild(stateNode('Could not load chats', e.message||'network error', 'Retry', function(){ load(); }));
      }else say('Chats: '+(e.message||'error'));
    }).then(function(){
      if(seq===loadSeq){ loading=false; setSpin(false); }
    });
  }
  function scheduleRefresh(ms){
    clearTimeout(refTimer);
    refTimer=setTimeout(function(){ if(!editing) load({keep:true}); }, ms||800);
  }
  function visible(){
    // sidebar is collapsed/hidden - do not hit the network
    return !!(root && root.getClientRects && root.getClientRects().length);
  }
  // "soft" auto-refresh: data changes rarely, so once every 2 minutes
  // and only if the panel is really visible, the tab is active and we are not editing anything
  function startSoft(){
    if(softTimer) return;
    softTimer=setInterval(function(){
      if(document.hidden || loading || editing || !visible()) return;
      if(Date.now()-lastLoad < SOFT_MS) return;
      load({keep:true});
    }, 30000);
  }
  function clearSearch(){
    if(searchEl) searchEl.value='';
    if(srowEl) srowEl.classList.remove('ch-has');
    clearTimeout(qTimer);
    if(q!==''){ q=''; load(); }
    else { buildItems(); renderAll(false); }
    if(searchEl) searchEl.focus();
  }

  // ---- panel assembly ----
  function bind(){
    reloadBtn.onclick=function(){ if(!loading) load({keep:true}); };
    document.getElementById('ch-sclear').onclick=clearSearch;
    searchEl.addEventListener('input', function(){
      srowEl.classList.toggle('ch-has', !!searchEl.value);
      clearTimeout(qTimer);
      qTimer=setTimeout(function(){
        var v=searchEl.value.trim();
        if(v===q) return;
        q=v; load();
      }, SEARCH_DELAY);
    });
    searchEl.addEventListener('keydown', function(e){
      if(e.key==='Escape'){ e.preventDefault(); e.stopPropagation();
        if(searchEl.value) clearSearch(); else searchEl.blur(); return; }
      if(e.key==='Enter'){ e.preventDefault(); clearTimeout(qTimer);
        var v=searchEl.value.trim(); if(v!==q){ q=v; load(); } return; }
      e.stopPropagation();                                  // do not interfere with global app hotkeys
    });
    if(!docBound){            // a repeated init() must not multiply handlers
      docBound=true;
      document.addEventListener('visibilitychange', function(){
        if(document.hidden || !visible() || editing) return;
        if(Date.now()-lastLoad > 60000) load({keep:true});
      });
    }
  }
  function canRun(){ return (typeof ttIsClassic!=='function') || ttIsClassic(); }

  function init(){
    if(!canRun()) return false;                             // mobile 2.0 - no panel
    var h=document.getElementById('sb-chats');
    if(!h) return false;                                    // no container yet - quietly leave
    if(root && root.parentNode===h && h===host){ load({keep:true}); return true; }
    host=h;
    host.textContent='';
    root=document.createElement('div'); root.className='ch-panel';
    root.innerHTML=PANEL_HTML;                              // static markup, no data
    host.appendChild(root);
    listEl=root.querySelector('#ch-list');
    cntEl=root.querySelector('#ch-count');
    srowEl=root.querySelector('#ch-srow');
    searchEl=root.querySelector('#ch-search');
    reloadBtn=root.querySelector('#ch-reload');
    chats=[]; items=[]; shown=0; total=0; q='';
    loadOpened();
    try{ activeId=localStorage.getItem(LS_ACTIVE)||null; }catch(e){}
    bind();
    load();
    startSoft();
    return true;
  }

  // container may appear later (sidebar is built by its own code) - wait for it
  function boot(){
    if(!canRun()) return;
    if(init()) return;
    if(obs) return;
    if(typeof MutationObserver!=='function') return;
    obs=new MutationObserver(function(){
      if(!document.getElementById('sb-chats')) return;
      if(init()){ obs.disconnect(); obs=null; }
    });
    try{ obs.observe(document.body||document.documentElement, {childList:true, subtree:true}); }catch(e){}
    setTimeout(function(){ if(obs){ obs.disconnect(); obs=null; } }, 30000);   // do not hang forever
  }

  // exposed: the sidebar author calls this when the panel opens / after its own rebuilds
  window.chatPanel={
    init:function(){ return init(); },
    refresh:function(){
      var h=document.getElementById('sb-chats');
      if(!root || !h || h!==host || root.parentNode!==host) return init();
      return load({keep:true});
    }
  };

  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

// ============================================================
// CLAUDE CHATS ON THE PHONE. A button in the session list header
// opens a screen with all chats: search, tap = POST chat/open (new
// tab with claude --resume) and straight to the terminal. A separate block, not a
// rework of the desktop panel: a downstream fork has its own version of that panel,
// and changing its code would break merging.
// WARNING: chat names are user text: use textContent only.
// ============================================================
(function(){
  var btn=document.getElementById('ttc-btn'), scr=document.getElementById('ttc-screen');
  if(!btn || !scr) return;
  var listEl=document.getElementById('ttc-list'), qEl=document.getElementById('ttc-q'),
      cntEl=document.getElementById('ttc-count'), backEl=document.getElementById('ttc-back');
  var STEP=60, chats=[], total=0, shown=0, seq=0, qTimer=null;
  function base(){ return (typeof API==='string' && API) ? API : 'api/'; }
  function say(t){ if(typeof toast==='function') toast(t); }
  function plural(n,a,b,c){ return Math.abs(n)===1 ? a : b; }
  var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function rel(ts){
    if(!ts) return ''; var d=new Date(ts*1000), now=new Date(), s=(now-d)/1000;
    if(s<60) return 'now'; if(s<3600) return Math.floor(s/60)+' min';
    if(d.toDateString()===now.toDateString()) return (d.getHours()<10?'0':'')+d.getHours()+':'+(d.getMinutes()<10?'0':'')+d.getMinutes();
    if(s<7*86400) return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()];
    return d.getDate()+' '+MON[d.getMonth()];
  }
  function shortCwd(c){ c=c||''; var h=HOME_DIR; if(c===h) return '~'; if(c.indexOf(h+'/')===0) c='~'+c.slice(h.length); return c; }
  function open(){ document.body.classList.add('ttc-open'); scr.setAttribute('aria-hidden','false'); load(); }
  function close(){ document.body.classList.remove('ttc-open'); scr.setAttribute('aria-hidden','true'); try{ qEl.blur(); }catch(e){} }
  function load(){
    var my=++seq, q=qEl.value.trim();
    if(!chats.length){ listEl.textContent=''; var w=document.createElement('div'); w.className='ttc-empty'; w.textContent='Loading...'; listEl.appendChild(w); }
    fetch(base()+'chats?limit=500&project=all&q='+encodeURIComponent(q)).then(function(r){ return r.json(); }).then(function(d){
      if(my!==seq) return;
      chats=(d && d.chats)||[]; total=(d && d.total)||chats.length; shown=0; listEl.textContent=''; render();
      cntEl.textContent=total+' '+plural(total,'chat','chats','chats')+(q?' matching':'');
    }).catch(function(){ if(my!==seq) return; listEl.textContent=''; var e=document.createElement('button');
      e.className='ttc-more'; e.textContent='Failed to load - retry'; e.onclick=load; listEl.appendChild(e); });
  }
  function render(){
    var old=listEl.querySelector('.ttc-more'); if(old) old.remove();
    if(!chats.length){ var e=document.createElement('div'); e.className='ttc-empty'; e.textContent='No chats found'; listEl.appendChild(e); return; }
    chats.slice(shown, shown+STEP).forEach(function(c){
      var b=document.createElement('button'); b.type='button'; b.className='ttc-row';
      var r1=document.createElement('div'); r1.className='ttc-r1';
      var n=document.createElement('span'); n.className='ttc-name'; n.textContent=c.name||c.title||'Untitled';
      var t=document.createElement('span'); t.className='ttc-time'; t.textContent=rel(c.mtime);
      r1.appendChild(n);
      r1.appendChild(t);
      var sub=document.createElement('div'); sub.className='ttc-sub';
      sub.textContent=shortCwd(c.cwd)+(c.msgs?' · '+c.msgs+' '+plural(c.msgs,'message','messages','messages'):'');
      b.appendChild(r1); b.appendChild(sub);
      b.onclick=function(){ if(b._lp){ b._lp=false; return; } openChat(c,b); };
      attachLongPress(b, c);
      listEl.appendChild(b);
    });
    shown=Math.min(chats.length, shown+STEP);
    if(shown<chats.length){ var m=document.createElement('button'); m.type='button'; m.className='ttc-more';
      m.textContent='Show more ('+(chats.length-shown)+')'; m.onclick=render; listEl.appendChild(m); }
  }
  // long press (like the session list: 480 ms, finger moved >10px means scroll)
  // -> sheet 'Open in Claude / Claude GLM'. Right mouse click does the same.
  function attachLongPress(b, c){
    var t=null, sx=0, sy=0;
    function cancel(){ clearTimeout(t); t=null; }
    b.addEventListener('touchstart', function(e){ var p=e.touches[0]; sx=p.clientX; sy=p.clientY; b._lp=false; cancel();
      t=setTimeout(function(){ t=null; b._lp=true; if(navigator.vibrate) navigator.vibrate(12); openSheet(c,b); }, 480); }, {passive:true});
    b.addEventListener('touchmove', function(e){ var p=e.touches[0]; if(Math.abs(p.clientX-sx)>10||Math.abs(p.clientY-sy)>10) cancel(); }, {passive:true});
    // after it fires, suppress the synthetic click, or it would open the chat / close the sheet
    b.addEventListener('touchend', function(e){ cancel();
      if(b._lp){ if(e.cancelable) e.preventDefault(); setTimeout(function(){ b._lp=false; }, 700); } });
    b.addEventListener('touchcancel', cancel);
    b.addEventListener('contextmenu', function(e){ e.preventDefault(); cancel(); if(!b._lp) openSheet(c,b); });
  }
  function openSheet(c, row){
    var scrim=document.getElementById('tt-scrim'), sheet=document.getElementById('tt-sheet'),
        body=document.getElementById('tt-sheet-body');
    if(!scrim || !sheet || !body) return;
    body.textContent='';
    var h=document.createElement('div'); h.className='tt-shead';
    var hn=document.createElement('div'); hn.className='tt-hn'; hn.textContent=c.name||c.title||'Untitled';
    var hs=document.createElement('div'); hs.className='tt-hs'; hs.textContent=shortCwd(c.cwd);
    h.appendChild(hn); h.appendChild(hs); body.appendChild(h);
    var items=[['Open in Claude','claude','']];
    items.forEach(function(it){
      var x=document.createElement('button'); x.type='button'; x.className='tt-sitem'; x.textContent=it[0];
      x.onclick=function(){ scrim.classList.remove('tt-show'); sheet.classList.remove('tt-show'); openChat(c, row); };
      body.appendChild(x);
    });
    scrim.classList.add('tt-show'); sheet.classList.add('tt-show');
  }
  function openChat(c,row){
    if(row._busy) return; row._busy=true; row.classList.add('ttc-busy');
    fetch(base()+'chat/open?id='+encodeURIComponent(c.id),{method:'POST'}).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(d){ if(!r.ok || d.error) throw new Error(d.error||('error '+r.status)); return d; });
    }).then(function(d){
      close();
      say(d.warn || ('Chat opened: '+(c.name||c.title||'')));
      if(typeof window.ttEnterLastNew==='function') window.ttEnterLastNew();
    }).catch(function(e){ say('Chat failed to open: '+(e.message||'error')); })
      .then(function(){ row._busy=false; row.classList.remove('ttc-busy'); });
  }
  btn.onclick=open;
  backEl.onclick=close;
  qEl.addEventListener('input', function(){ clearTimeout(qTimer); qTimer=setTimeout(load, 250); });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape' && document.body.classList.contains('ttc-open')) close(); });
})();
</script>
</body>
</html>
"""


LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex">
<title>thumbmux - sign in</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#0b0e14;color:#d6dbe5;font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
form{width:min(340px,88vw)}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
h1 b{color:#e6b450}
p{margin:0 0 18px;color:#7d8590;font-size:14px}
input,button{width:100%;font:inherit;border-radius:12px;border:1px solid #2a3140;padding:13px 14px}
input{background:#121722;color:#d6dbe5;margin-bottom:10px}
input:focus{outline:2px solid #3b82f6;border-color:transparent}
button{background:#e6b450;color:#1a1304;font-weight:600;border:0;cursor:pointer}
.err{color:#f07178;font-size:14px;min-height:20px;margin:0 0 10px}
</style></head><body>
<form method="post" action="/login" autocomplete="on">
<h1>thumb<b>mux</b></h1><p>Sign in to your terminal.</p>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="__NEXT__">
<div class="err">__ERROR__</div>
<button type="submit">Sign in</button>
</form></body></html>
"""


def _render_html(html):
    """Fill the page template: the agent buttons come from THUMBMUX_AGENTS."""
    agents = _AGENT_CMDS
    first = agents[0] if agents else "shell"
    nm = "\n".join('<button type="button" class="nm-item%s" data-cmd="%s">%s</button>'
                   % (" nm-primary" if i == 0 else "", c, c) for i, c in enumerate(agents))
    fp = "\n".join('<button type="button" class="fp-seg%s" data-cmd="%s">%s</button>'
                   % (" active" if i == 0 else "", c, c) for i, c in enumerate(agents))
    labels = dict(_AGENT_LABELS)
    opts = ",".join("[%s,%s]" % (json.dumps(c), json.dumps(labels[c]))
                    for c, _ in _AGENT_LABELS)
    html = html.replace("__NM_AGENTS__", nm).replace("__FP_AGENTS__", fp)
    html = html.replace("__AGENT_OPTS__", "[%s]" % opts)
    html = html.replace("__AGENT_LABEL__", json.dumps(labels))
    html = html.replace("__AGENT_CMDS__", json.dumps({c: 1 for c in agents}))
    html = html.replace("__CMDS__", json.dumps(["shell"] + agents))
    html = html.replace("__DEFAULT_AGENT__", first)
    html = html.replace("__HOME_DIR__", json.dumps(WORKDIR))
    html = html.replace("__VOICE_ON__", "true" if voice.enabled() else "false")
    return html


HTML = _render_html(HTML)


class Handler(BaseHTTPRequestHandler):
    server_version = "thumbmux"
    sys_version = ""

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            ctype = "application/json"
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, real, size):
        """Stream the file in 64 KB chunks - it is not read into memory
        whole, so file size does not matter (a gigabyte log will not
        crash the server). In docker mode bytes go through `cat` inside
        the container - the same way as other filesystem operations."""
        name = posixpath.basename(real) or "file"
        # OPEN THE FILE BEFORE THE HEADERS. Otherwise 'no read permission' would show up
        # after a 200 was already sent - the browser would save an empty file and no
        # error sign would reach the user. It also removes the stat/read mismatch:
        # the size is taken from the SAME descriptor.
        src = proc = None
        try:
            if LOCAL_MODE:
                src = open(real, "rb")
                size = os.fstat(src.fileno()).st_size
            else:
                proc = subprocess.Popen(_wrap(["cat", "--", real]),
                                        stdout=subprocess.PIPE)
                src = proc.stdout
        except PermissionError:
            return self._send(403, {"error": "no access to the file"})
        except FileNotFoundError:
            return self._send(404, {"error": "file not found"})
        except OSError as e:
            return self._send(500, {"error": str(e)[:200]})
        self.send_response(200)
        self.send_header("Content-Type", fs_download_type(name))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", fs_download_disposition(name))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        try:
            while sent < size:
                chunk = src.read(min(65536, size - sent))
                if not chunk:
                    break          # file shrank on the fly - nothing more to send
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                   # download cancelled in the browser - not an error
        finally:
            try:
                if src:
                    src.close()
            except Exception:
                pass
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        if sent < size:
            # sent less than the promised Content-Length: drop the connection so the
            # browser sees the break instead of waiting forever for the missing bytes
            self.close_connection = True

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return ""
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    def _read_exact(self, n):
        """Read exactly n bytes (binary-safe, chunked) for uploads."""
        buf = bytearray()
        rem = n
        while rem > 0:
            chunk = self.rfile.read(min(rem, 65536))
            if not chunk:
                break
            buf.extend(chunk)
            rem -= len(chunk)
        return bytes(buf)

    def _win_arg(self):
        q = self._qs()
        try:
            return int(q.get("w", [""])[0])
        except (ValueError, IndexError):
            return None

    # ---- security helpers -------------------------------------------------
    def _client_ip(self):
        if config.TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()
        return self.client_address[0]

    def _is_https(self):
        return config.COOKIE_SECURE or (
            config.TRUST_PROXY and
            self.headers.get("X-Forwarded-Proto", "").lower() == "https")

    def _authed(self):
        return AUTH.from_headers(self.headers)

    def _redirect(self, where, cookie=None):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _cookie(self, value, max_age):
        c = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
            authmod.COOKIE, value, max_age)
        return c + ("; Secure" if self._is_https() else "")

    def _deny(self, path):
        """Not logged in: pages go to /login, API calls get a plain 401."""
        if path.startswith("/m/api/") or path.startswith("/m/term/"):
            return self._send(401, {"error": "login required"})
        return self._redirect("/login?next=" + quote(self.path, safe="/?=&"))

    def _login_page(self, error="", code=200, nxt="/m/"):
        page = LOGIN_HTML.replace("__ERROR__", htmllib.escape(error)).replace(
            "__NEXT__", htmllib.escape(nxt, quote=True))
        return self._send(code, page, "text/html; charset=utf-8")

    @staticmethod
    def _safe_next(nxt):
        # only same-site absolute paths: no scheme, no //host, no backslashes
        if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt \
                and "\r" not in nxt and "\n" not in nxt:
            return nxt
        return "/m/"

    def _handle_login(self):
        if not authmod.same_origin(self.headers):
            return self._send(403, {"error": "bad origin"})
        ip = self._client_ip()
        if AUTH.locked(ip):
            return self._login_page("Too many attempts. Try again in a minute.", 429)
        form = parse_qs(self._body(), keep_blank_values=True)
        nxt = self._safe_next(form.get("next", ["/m/"])[0])
        if AUTH.check_password(form.get("password", [""])[0]):
            AUTH.succeeded(ip)
            return self._redirect(nxt, self._cookie(AUTH.make_cookie(), authmod.SESSION_TTL))
        AUTH.failed(ip)
        time.sleep(0.5)
        return self._login_page("Wrong password.", 401, nxt)

    def _term(self, path):
        """Authenticated reverse proxy to ttyd (HTTP + WebSocket)."""
        if self.headers.get("Upgrade", "").lower() == "websocket":
            if not authmod.same_origin(self.headers):
                return self._send(403, {"error": "bad origin"})
            return termproxy.proxy_ws(self)
        return termproxy.proxy_http(self)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        super().end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path == "/login":
            nxt = self._safe_next(self._qs().get("next", ["/m/"])[0])
            if self._authed():
                return self._redirect(nxt)
            return self._login_page(nxt=nxt)
        if not self._authed():
            return self._deny(path)
        if path == "/":
            return self._redirect("/m/")
        if path.startswith("/m/term"):
            return self._term(path)
        return self._do_get(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self._handle_login()
        if not self._authed():
            return self._deny(path)
        if not authmod.same_origin(self.headers):
            return self._send(403, {"error": "bad origin"})
        if path == "/logout":
            return self._redirect("/login", self._cookie("", 0))
        return self._do_post(path)

    def _do_get(self, path):
        if path == "/m":
            # RELATIVE redirect to m/ - keeps the external URL prefix
            # (e.g. /tt/mlv/m -> /tt/mlv/m/); an absolute /m/ would strip it.
            self.send_response(301)
            self.send_header("Location", "m/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/m/":
            # code fingerprint goes into the page itself: the page compares it with health
            # and reloads after a deploy. Taking it 'from the first health' is wrong:
            # if a deploy happened between page load and the first health, the page
            # would remember the NEW version and keep running the old JS.
            return self._send(200, HTML.replace("__APP_VER__", APP_VER, 1),
                              "text/html; charset=utf-8")
        if path == "/m/api/windows":
            try:
                return self._send(200, list_windows())
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if path == "/m/api/previews":
            try:
                data = previews_cached()
                # optional ?n=<lines>: the chat list needs the last 2-3
                # non-empty lines (preview), not the whole visible screen. Cut from the
                # ALREADY cached lines - no new docker exec. ?cols= is an
                # extra width cut (default as in cache, PREVIEW_COLS).
                q = self._qs()
                nraw = q.get("n", [None])[0]
                if nraw is not None:
                    try:
                        n = max(1, min(PREVIEW_LINES, int(nraw)))
                    except ValueError:
                        n = 3
                    try:
                        cols = max(8, min(PREVIEW_COLS, int(q.get("cols", [PREVIEW_COLS])[0])))
                    except ValueError:
                        cols = PREVIEW_COLS
                    trimmed = {"windows": []}
                    for w in data.get("windows", []):
                        lines = _clean_chat_preview(w.get("lines", []))[-n:]
                        if cols < PREVIEW_COLS:
                            lines = [ln[:cols] for ln in lines]
                        nw = {k: v for k, v in w.items() if k != "lines"}
                        nw["lines"] = lines
                        trimmed["windows"].append(nw)
                    return self._send(200, trimmed)
                return self._send(200, data)
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if path == "/m/api/stat":
            try:
                return self._send(200, read_mem())
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if path == "/m/api/ls":
            target = self._qs().get("path", [WORKDIR])[0] or WORKDIR
            try:
                return self._send(200, list_dirs(target))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if path == "/m/api/favs":
            return self._send(200, {"favs": load_favs()})
        if path == "/m/api/folders":
            return self._send(200, load_folders())
        if path == "/m/api/settings":
            return self._send(200, load_settings())
        if path == "/m/api/stt-job":
            return self._send(200, voice.view(self._qs().get("id", [""])[0]))
        if path == "/m/api/health":
            return self._send(200, {"ok": True, "ver": APP_VER})
        # ── file manager ────────────────────────────────────────────
        if path == "/m/api/fs/ls":
            target = self._qs().get("path", [WORKDIR])[0] or WORKDIR
            try:
                return self._send(200, fs_ls(target))
            except FsError as e:
                return self._send(e.code, {"error": e.msg})
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
        if path == "/m/api/fs/stat":
            # 'will you serve this file?' - the frontend asks BEFORE downloading, to
            # show the refusal reason in words. fs/get itself answers with a byte
            # stream, and its error text would be silently saved by the browser as a file.
            try:
                real, size = fs_download(self._qs().get("path", [""])[0])
            except FsError as e:
                return self._send(e.code, {"error": e.msg})
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
            return self._send(200, {"ok": True, "path": real, "size": size,
                                    "name": posixpath.basename(real)})
        if path == "/m/api/fs/get":
            # download a file from the explorer (Content-Disposition: attachment)
            try:
                real, size = fs_download(self._qs().get("path", [""])[0])
            except FsError as e:
                return self._send(e.code, {"error": e.msg})
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
            return self._send_file(real, size)
        if path == "/m/api/fs/undoable":
            # to highlight the 'Undo' button in the UI (what exactly will be rolled back)
            try:
                return self._send(200, fs_undoable())
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
        # ── end of file manager ─────────────────────────────────────────
        if path == "/m/api/chats":
            # Claude Code chat list. Expensive transcript parsing is cached
            # in DATA_DIR/chats-cache.json, so here it is almost always just scandir+stat.
            q = self._qs()
            project = (q.get("project", ["all"])[0] or "all").strip()
            if not chat_project_check(project):
                return self._send(403, {"error": "bad project"})
            try:
                limit = int(q.get("limit", [CHAT_LIMIT_DEFAULT])[0])
            except ValueError:
                limit = CHAT_LIMIT_DEFAULT
            limit = max(1, min(CHAT_LIMIT_MAX, limit))
            try:
                return self._send(200, chats_list(
                    limit, project, q.get("q", [""])[0]))
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _do_post(self, path):
        try:
            if path == "/m/api/select":
                w = self._win_arg()
                if w is None:
                    return self._send(400, {"error": "bad w"})
                # one docker exec: switch + redraw + return the window list at once
                # (the frontend used to make a separate ~450ms windows request = lag)
                fmt = "#{window_index}\t#{window_name}\t#{window_active}\t#{window_id}"
                rc, out = tmux_live(
                    "select-window", "-t", f"{SESSION}:{w}", ";",
                    "refresh-client", ";",
                    "list-windows", "-t", SESSION, "-F", fmt)
                if rc != 0:
                    return self._send(500, {"error": "select failed"})
                # st by id from the last state snapshot (no new exec) -
                # otherwise indicators would fade until the next windows poll.
                # selected window (active) -> clear done at once (opened = seen).
                with _state_lock:
                    st_by_id = {x["id"]: x["st"]
                                for x in (_state_cache.get("data") or [])}
                wins = []
                for line in out.splitlines():
                    p = line.split("\t")
                    if len(p) >= 4:
                        wid = p[3]
                        active = p[2] == "1"
                        st = st_by_id.get(wid, "idle")
                        if active and st == "done":
                            st = "idle"
                        wins.append({"index": int(p[0]), "name": p[1],
                                     "active": active, "id": wid, "st": st})
                # the windows cache still remembers the PREVIOUS active window: a second client
                # (PC when selecting from the phone) would see the old active one until _STATE_TTL
                _state_invalidate()
                return self._send(200, {"ok": True, "index": w, "windows": wins})

            if path == "/m/api/reorder":
                # reorder windows into the order set by the frontend (tab drag).
                # order = list of CURRENT window indexes in the new order L->R.
                raw = self._qs().get("order", [""])[0]
                try:
                    order = [int(x) for x in raw.split(",") if x != ""]
                except ValueError:
                    return self._send(400, {"error": "bad order"})
                _state_invalidate()          # decide on the FRESH list, not the cache
                cur = list_windows()
                cur_idx = sorted(w["index"] for w in cur)
                # integrity: the same set of windows and the order really changed
                if not order or sorted(order) != cur_idx or order == cur_idx:
                    return self._send(200, {"ok": True, "windows": cur})
                # active window - by stable id (indexes will shift after reordering)
                _, aid = tmux("display-message", "-p", "-t", SESSION, "#{window_id}")
                active_id = aid.strip()
                # place into free high indexes in the needed order, then
                # renumber compactly (move-window -r keeps the order).
                # -d - do not switch focus to the moved window. Verified on a
                # throwaway session: index gaps and the active window survive.
                base = 900
                for i, w in enumerate(order):
                    tmux("move-window", "-d",
                         "-s", f"{SESSION}:{w}", "-t", f"{SESSION}:{base + i}")
                tmux("move-window", "-r", "-t", SESSION)
                if active_id:
                    tmux("select-window", "-t", active_id)
                    tmux("refresh-client")
                _state_invalidate()          # the response carries the NEW order, not the cache
                return self._send(200, {"ok": True, "windows": list_windows()})

            if path == "/m/api/type":
                # literal text injection — survives swipe/autocorrect since the
                # user types into a normal field, not xterm.js. -l = literal,
                # -- = stop flag parsing (leading dashes / cyrillic all safe).
                text = self._body()
                if len(text) > TYPE_MAX_CHARS:
                    # explicit refusal instead of silent truncation - the frontend shows a toast
                    return self._send(413, {"error": "too long",
                                            "len": len(text),
                                            "max": TYPE_MAX_CHARS})
                q = self._qs()
                exit_copy_mode()
                ok = True
                if text:
                    lit = q.get("lit", ["0"])[0] == "1"
                    if lit and len(text.encode("utf-8", "replace")) <= 4000:
                        # literal keystrokes — for short single-line input like
                        # slash-commands (/resume, /model), so Claude recognises
                        # them as typed, not as a paste blob. A large lit text
                        # would hit the same ~16K argv limit -> falls back to paste.
                        rc, _ = tmux_live("send-keys", "-t", SESSION,
                                          "-l", "--", text)
                        ok = rc == 0
                    else:
                        # bracketed paste — newlines land in the field as a paste
                        # (multi-line message to Claude isn't submitted per line).
                        # Text goes to tmux VIA STDIN (load-buffer -):
                        # set-buffer used to pass it as an argv argument and above ~16K bytes tmux
                        # answered 'command too long' - the text was SILENTLY lost (rc was not checked,
                        # the frontend saw 'ok'). The NAMED buffer -b m_inject protects from a race:
                        # with `set-clipboard on` a running TUI (Claude/Codex)
                        # sends OSC-52 and puts ITS OWN buffer on top of the stack - paste
                        # by NAME inserts exactly our text, -d deletes it.
                        # (see above)
                        rc1, _ = tmux_stdin(text, "load-buffer",
                                            "-b", "m_inject", "-")
                        rc2, _ = tmux_live("paste-buffer", "-b", "m_inject",
                                           "-p", "-d", "-t", SESSION)
                        ok = rc1 == 0 and rc2 == 0
                    if not ok:
                        # honest error: the draft stays on the frontend
                        return self._send(500, {"error": "tmux inject failed"})
                if q.get("enter", ["0"])[0] == "1":
                    tmux("send-keys", "-t", SESSION, "Enter")
                return self._send(200, {"ok": True})

            if path == "/m/api/key":
                k = self._qs().get("k", [""])[0]
                if k in ("PageUp", "PageDown"):
                    # history scrolling: via copy-mode (-e = exit on reaching
                    # the bottom), otherwise PgUp is dead in Claude's classic renderer
                    tmux_live("copy-mode", "-e", "-t", SESSION)
                    tmux("send-keys", "-t", SESSION, "-X",
                         "page-up" if k == "PageUp" else "page-down")
                    return self._send(200, {"ok": True})
                key = KEYMAP.get(k)
                if not key:
                    return self._send(400, {"error": "unknown key"})
                exit_copy_mode()
                tmux_live("send-keys", "-t", SESSION, key)
                return self._send(200, {"ok": True})

            if path == "/m/api/new":
                # cmd= what to run in the new window: shell (default) | Claude/Codex/Kimi/...
                # if cmd/cwd are NOT passed explicitly - take them from settings.json
                # (default_cmd/default_cwd), otherwise the previous behaviour.
                ensure_session()
                q = self._qs()
                _cfg = load_settings()
                cmd = q.get("cmd", [None])[0]
                if cmd is None:
                    cmd = _cfg.get("default_cmd") or "shell"
                cwd = q.get("cwd", [None])[0]
                if cwd is None or not cwd.strip():
                    cwd = _cfg.get("default_cwd") or WORKDIR
                model = (q.get("model", [""])[0] or "").strip()
                if model and not _MODEL_RE.match(model):
                    return self._send(400, {"error": "bad model: " + model[:40]})
                # the agent must be allowed for this workspace (THUMBMUX_AGENTS)
                if cmd not in _ALLOWED_CMDS:
                    return self._send(400, {"error": "agent unavailable: " + str(cmd)})
                try:
                    rc, out = tmux("new-window", "-P", "-t", SESSION, "-c", cwd,
                                   "-F", "#{window_index}", check=True)
                    idx = int(out.strip() or "0")
                except RuntimeError as exc:
                    return self._send(503, {"error": str(exc)})
                if cmd != "shell":
                    line = cmd
                    if model and cmd == "claude":
                        line += " --model " + shlex.quote(model)
                    tmux("send-keys", "-t", f"{SESSION}:{idx}", "-l", "--", line)
                    tmux("send-keys", "-t", f"{SESSION}:{idx}", "Enter")
                tmux("select-window", "-t", f"{SESSION}:{idx}", ";", "refresh-client")
                _state_invalidate()          # new tab - return a fresh list, not the cache
                return self._send(200, {"ok": True, "index": idx,
                                        "windows": list_windows()})

            if path == "/m/api/open":
                # open a folder = new window with that cwd, named after the folder.
                # cmd= what to launch in it: shell (default) | Claude/Codex/Kimi/...
                target = self._qs().get("path", [""])[0].strip()
                if not target:
                    return self._send(400, {"error": "no path"})
                # cmd not passed explicitly -> take it from settings.json (default_cmd)
                cmd = self._qs().get("cmd", [None])[0]
                if cmd is None:
                    cmd = load_settings().get("default_cmd") or "shell"
                if cmd not in _ALLOWED_CMDS:
                    return self._send(400, {"error": "agent unavailable: " + str(cmd)})
                ensure_session()
                try:
                    rc, out = tmux("new-window", "-P", "-t", SESSION, "-c", target,
                                   "-F", "#{window_index}")
                    if rc != 0:
                        return self._send(400, {"error": "cannot open: " + target})
                    idx = int(out.strip() or "0")
                except RuntimeError as exc:
                    return self._send(503, {"error": str(exc)})
                tmux("set-option", "-w", "-u", "-t", f"{SESSION}:{idx}", "@manual")
                wname = NAME_RE.sub("", posixpath.basename(target.rstrip("/")))[:18] or "dir"
                tmux("rename-window", "-t", f"{SESSION}:{idx}", wname)
                if cmd != "shell":
                    tmux("send-keys", "-t", f"{SESSION}:{idx}", "-l", "--", cmd)
                    tmux("send-keys", "-t", f"{SESSION}:{idx}", "Enter")
                tmux("select-window", "-t", f"{SESSION}:{idx}", ";", "refresh-client")
                _state_invalidate()          # new tab - return a fresh list, not the cache
                return self._send(200, {"ok": True, "index": idx,
                                        "windows": list_windows()})

            if path == "/m/api/favs":
                # favorite folders (quick access in the modal): op=add|remove|reorder
                q = self._qs()
                op = q.get("op", [""])[0]
                if op == "reorder":
                    raw = q.get("paths", [""])[0]
                    try:
                        order = json.loads(raw) if raw.strip().startswith("[") else []
                    except Exception:
                        order = []
                    order = [_norm_path(x) for x in order if isinstance(x, str) and x]
                    return self._send(200, {"favs": reorder_favs(order)})
                p = _norm_path(q.get("path", [""])[0])
                if not p or p == "/":
                    return self._send(400, {"error": "no path"})
                favs = update_fav(p, op)
                return self._send(200, {"favs": favs})

            if path == "/m/api/folders":
                # grouping windows by folders (chat list). All mutations are
                # under _folders_lock, atomic write. op from the query string.
                q = self._qs()
                op = q.get("op", [""])[0]
                if op not in ("create", "rename", "delete",
                              "assign", "unassign", "reorder"):
                    return self._send(400, {"error": "bad op"})
                args = {
                    "name": q.get("name", [""])[0],
                    "id": q.get("id", [""])[0],
                    "win": q.get("win", [""])[0],
                    "folder": q.get("folder", [""])[0],
                    "ids": q.get("ids", [""])[0],
                }
                data = folders_op(op, args)
                return self._send(200, data)

            if path == "/m/api/settings":
                # server settings: accept a JSON patch (body), merge,
                # save under the lock. Return the full object.
                raw = self._body()
                try:
                    patch = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                if not isinstance(patch, dict):
                    return self._send(400, {"error": "patch must be object"})
                data = patch_settings(patch)
                return self._send(200, data)

            if path == "/m/api/pane":
                # split screen: split the active window into parts (tmux panes)
                ensure_session()
                act = self._qs().get("act", [""])[0]
                if act in ("splitv", "splith"):
                    rc, cwd = tmux("display-message", "-p", "-t", SESSION,
                                   "#{pane_current_path}")
                    cwd = cwd.strip() or WORKDIR
                    flag = "-v" if act == "splitv" else "-h"
                    tmux_live("split-window", flag, "-t", SESSION, "-c", cwd,
                              ";", "refresh-client")
                    return self._send(200, {"ok": True})
                if act == "next":
                    tmux_live("select-pane", "-t", f"{SESSION}:.+", ";", "refresh-client")
                    return self._send(200, {"ok": True})
                if act == "kill":
                    _, out = tmux("list-panes", "-t", SESSION, "-F", "p")
                    if len([x for x in out.split() if x]) <= 1:
                        return self._send(200, {"ok": False, "error": "no split"})
                    tmux_live("kill-pane", "-t", SESSION, ";", "refresh-client")
                    return self._send(200, {"ok": True})
                if act == "break":
                    # detach the active pane back into its own tab (without losing it)
                    _, out = tmux("list-panes", "-t", SESSION, "-F", "p")
                    if len([x for x in out.split() if x]) <= 1:
                        return self._send(200, {"ok": False, "error": "no split"})
                    tmux_live("break-pane", "-t", SESSION, ";", "refresh-client")
                    _state_invalidate()      # a new tab appeared - return a fresh list
                    return self._send(200, {"ok": True, "windows": list_windows()})
                return self._send(400, {"error": "bad act"})

            if path == "/m/api/join":
                # drag a session tab into the split: window src is merged as a pane
                # into the current (active) window - left/right/top/bottom
                ensure_session()
                try:
                    src = int(self._qs().get("src", [""])[0])
                except ValueError:
                    return self._send(400, {"error": "bad src"})
                direction = self._qs().get("dir", ["h"])[0]
                before = self._qs().get("before", ["0"])[0] == "1"
                _, act = tmux("display-message", "-p", "-t", SESSION, "#{window_index}")
                act = act.strip()
                if not act or act == str(src):
                    return self._send(200, {"ok": False, "error": "this is the current window"})
                args = ["join-pane", "-s", f"{SESSION}:{src}", "-t", f"{SESSION}:{act}",
                        "-h" if direction == "h" else "-v"]
                if before:
                    args.append("-b")
                rc, err = tmux_live(*args)
                if rc != 0:
                    return self._send(500, {"error": (err or "join failed").strip()[:120]})
                tmux("refresh-client")
                _state_invalidate()          # window src merged as a pane - its tab is gone
                return self._send(200, {"ok": True, "windows": list_windows()})

            if path == "/m/api/transcribe":
                if not voice.enabled():
                    return self._send(501, {"error": "voice input is not configured"})
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0:
                    return self._send(400, {"error": "empty recording"})
                if n > config.MAX_AUDIO:
                    return self._send(413, {"error": "recording too long"})
                raw = self._read_exact(n)
                ext = "." + re.sub(r"[^a-z0-9]", "", self._qs().get("ext", ["webm"])[0].lower())[:6]
                return self._send(200, {"job": voice.start_job(raw, ext or ".webm")})

            if path == "/m/api/upload":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0:
                    return self._send(400, {"error": "empty"})
                if n > MAX_UPLOAD:
                    return self._send(413, {"error": "file too large (>15 MB)"})
                data = self._read_exact(n)
                if is_heic(data):
                    return self._send(415, {"error": "HEIC is not supported - choose JPEG/PNG"})
                fname = uuid.uuid4().hex[:8] + "_" + safe_filename(
                    self._qs().get("filename", ["image.jpg"])[0])
                os.makedirs(UPLOAD_DIR_HOST, exist_ok=True)
                with open(os.path.join(UPLOAD_DIR_HOST, fname), "wb") as f:
                    f.write(data)
                ctr_path = UPLOAD_DIR_CTR + "/" + fname
                # inject the path literally so Claude auto-attaches it as [Image];
                # trailing space leaves the cursor ready for the user's question.
                # &w= pins the window chosen when the upload started, so the path
                # can't land in another tab if the user switched mid-upload.
                # defer=1 (thumbnail-preview client): DON'T inject now — the client
                # holds the path in a preview strip and injects it at send-time via
                # /m/api/attach-inject, so ✕ can drop a photo before anything lands
                # in the window. Default (no defer) keeps the original behaviour so
                # paste/other callers are untouched.
                wq = self._qs().get("w", [""])[0]
                tgt = f"{SESSION}:{wq}" if wq.isdigit() else SESSION
                if self._qs().get("defer", [""])[0] != "1":
                    exit_copy_mode()
                    tmux_live("send-keys", "-t", tgt, "-l", "--", ctr_path)
                    tmux("send-keys", "-t", tgt, "Space")
                return self._send(200, {"ok": True, "path": ctr_path, "bytes": len(data)})

            if path == "/m/api/attach-inject":
                # inject deferred photo paths (from the preview strip) into the
                # window right before the user's text is sent. Body = JSON
                # {"paths": [...]}. Each path lands literally + trailing space,
                # exactly as the original upload did, so Claude sees [Image].
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                raw = self._read_exact(n) if n > 0 else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    return self._send(400, {"error": "bad json"})
                paths = body.get("paths") or []
                if not isinstance(paths, list):
                    return self._send(400, {"error": "paths must be a list"})
                wq = self._qs().get("w", [""])[0]
                tgt = f"{SESSION}:{wq}" if wq.isdigit() else SESSION
                injected = 0
                for p in paths:
                    # only allow paths we actually handed out (inside .uploads),
                    # so this can't be used to type arbitrary text into the window
                    if not isinstance(p, str) or not p.startswith(UPLOAD_DIR_CTR + "/"):
                        continue
                    exit_copy_mode()
                    tmux_live("send-keys", "-t", tgt, "-l", "--", p)
                    tmux("send-keys", "-t", tgt, "Space")
                    injected += 1
                return self._send(200, {"ok": True, "injected": injected})

            if path == "/m/api/file":
                # any file -> into the working folder of the selected window (project cwd),
                # into the container via docker cp (cwd may be outside the bind mount)
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    n = 0
                if n <= 0:
                    return self._send(400, {"error": "empty file"})
                if n > MAX_FILE:
                    return self._send(413, {"error": "file too large (>100 MB)"})
                wq = self._qs().get("w", [""])[0]
                tgt = f"{SESSION}:{wq}" if wq.isdigit() else SESSION
                rc, cwd = tmux("display-message", "-p", "-t", tgt, "#{pane_current_path}")
                cwd = cwd.strip()
                if rc != 0 or not cwd:
                    return self._send(500, {"error": "cannot determine the window folder"})
                fname = safe_filename(self._qs().get("filename", ["file.bin"])[0])
                data = self._read_exact(n)
                fd, tmp = tempfile.mkstemp(prefix="ufile_")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    dest = posixpath.join(cwd, fname)
                    if LOCAL_MODE:
                        # same filesystem — plain copy instead of docker cp
                        try:
                            shutil.copyfile(tmp, dest)
                        except Exception as e:
                            return self._send(500, {"error": str(e)[:200]})
                    else:
                        cp = subprocess.run(
                            ["docker", "cp", tmp, f"{CONTAINER}:{dest}"],
                            capture_output=True, text=True, timeout=60,
                        )
                        if cp.returncode != 0:
                            return self._send(500, {"error": (cp.stderr or "copy failed").strip()[:200]})
                finally:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
                # inject the absolute path into the window (like a photo) - Claude sees where
                # the file landed; a trailing space leaves the cursor for the question
                exit_copy_mode()
                tmux_live("send-keys", "-t", tgt, "-l", "--", dest)
                tmux("send-keys", "-t", tgt, "Space")
                return self._send(200, {"ok": True, "path": dest, "bytes": len(data)})

            if path == "/m/api/kill":
                w = self._win_arg()
                if w is None:
                    return self._send(400, {"error": "bad w"})
                # do not allow killing the last window (would kill session)
                _state_invalidate()          # count on the fresh list, not the cache
                if len(list_windows()) <= 1:
                    return self._send(409, {"error": "last window"})
                tmux("kill-window", "-t", f"{SESSION}:{w}", check=True)
                # Cache reset + fresh list in the response. Before, the cache lived another
                # _STATE_TTL after the window closed: the frontend called refresh(), got the same
                # snapshot, the signature did not change - the tab vanished only on the next
                # poll, 5-10 s in total.
                _state_invalidate()
                return self._send(200, {"ok": True, "windows": list_windows()})

            if path == "/m/api/rename":
                w = self._win_arg()
                # WINNAME_RE (NOT NAME_RE) - allows Cyrillic: session names may be non-Latin,
                # otherwise manual rename in the 2.0 list would strip all the non-Latin text.
                name = WINNAME_RE.sub("", self._qs().get("name", [""])[0])[:24] or "win"
                if w is None:
                    return self._send(400, {"error": "bad w"})
                tmux("rename-window", "-t", f"{SESSION}:{w}", name, check=True)
                _mark_manual(w)
                _state_invalidate()          # new name - immediately, not after _STATE_TTL
                return self._send(200, {"ok": True, "windows": list_windows()})

            # ── file manager ────────────────────────────────────────────
            if path == "/m/api/fs/op":
                # file operations: op from the query string, paths too.
                # All mutations are under _fs_lock, each writes an entry to the undo
                # journal. delete does NOT delete but moves to the trash.
                q = self._qs()
                op = q.get("op", [""])[0]
                if op not in ("mkdir", "rename", "move", "delete"):
                    return self._send(400, {"error": "bad op"})
                args = {
                    "path": q.get("path", [""])[0],
                    "name": q.get("name", [""])[0],
                    "src": q.get("src", [""])[0],
                    "dst": q.get("dst", [""])[0],
                }
                try:
                    return self._send(200, fs_op(op, args))
                except FsError as e:
                    return self._send(e.code, {"error": e.msg})

            if path == "/m/api/fs/undo":
                # roll back the last operation from the journal (stack of 50)
                try:
                    return self._send(200, fs_undo())
                except FsError as e:
                    return self._send(e.code, {"error": e.msg})
            # ── end of file manager ────────────────────────────────────────────
            if path == "/m/api/chat/open":
                # Resume a Claude Code chat: new tmux window in the chat's cwd
                # running `claude --resume`. Returns windows[] like /m/api/select.
                sid = self._qs().get("id", [""])[0].strip()
                if not CHAT_UUID_RE.match(sid):
                    return self._send(400, {"error": "bad id"})
                try:
                    idx, wins, warn = chat_open(sid)
                except KeyError as e:
                    return self._send(404, {"error": str(e)})
                except PermissionError as e:
                    return self._send(403, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                resp = {"ok": True, "index": idx, "windows": wins}
                if warn:
                    resp["warn"] = warn
                return self._send(200, resp)

            if path == "/m/api/chat/rename":
                # user-defined chat name (survives restart, lives in
                # DATA_DIR/chat-names.json). Empty name= clears the name.
                q = self._qs()
                sid = q.get("id", [""])[0].strip()
                if not CHAT_UUID_RE.match(sid):
                    return self._send(400, {"error": "bad id"})
                name = chat_name_clean(q.get("name", [""])[0])
                return self._send(200, {"ok": True, "id": sid,
                                        "name": chat_name_set(sid, name)})

            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


# -- Telegram bridge (optional; see tgbot.py) -------------------------------
WID_RE = re.compile(r"^@\d{1,9}$")


class TgBackend:
    """What the Telegram bot may do with tmux: list tabs, read a tab's tail,
    type into one tab, open a new tab. Targets are tmux window ids (@N)."""

    def windows(self, fresh=False):
        if fresh:
            _state_invalidate()       # a tab opened a second ago must be visible
        return [{"id": w["id"], "index": w["index"], "name": w["name"],
                 "active": w["active"], "st": w.get("st", "idle")}
                for w in list_windows()]

    def tail(self, wid, n):
        if not WID_RE.match(wid):
            return []
        rc, out = tmux("capture-pane", "-p", "-t", wid)
        lines = [l for l in out.splitlines() if l.strip()] if rc == 0 else []
        return lines[-n:]

    def send(self, wid, text):
        if not WID_RE.match(wid):
            return False
        tmux("if-shell", "-F", "-t", wid, "#{pane_in_mode}",
             f"send-keys -t {wid} -X cancel")
        if "\n" not in text and len(text.encode("utf-8", "replace")) <= 4000:
            rc, _ = tmux("send-keys", "-t", wid, "-l", "--", text)
        else:
            rc1, _ = tmux_stdin(text, "load-buffer", "-b", "m_tg", "-")
            rc2, _ = tmux("paste-buffer", "-b", "m_tg", "-p", "-d", "-t", wid)
            rc = rc1 or rc2
        if rc != 0:
            return False
        time.sleep(0.25)      # let a TUI take the paste before Enter
        rc, _ = tmux("send-keys", "-t", wid, "Enter")
        return rc == 0

    def new_window(self, name, agent=None):
        ensure_session()
        cfg = load_settings()
        cmd = agent or cfg.get("default_cmd") or "shell"
        if cmd not in _ALLOWED_CMDS:
            raise ValueError("agent unavailable: %s (allowed: %s)"
                             % (cmd, ", ".join(sorted(_ALLOWED_CMDS))))
        cwd = cfg.get("default_cwd") or WORKDIR
        rc, out = tmux("new-window", "-d", "-P", "-t", SESSION, "-c", cwd,
                       "-F", "#{window_id}")
        wid = out.strip()
        if rc != 0 or not WID_RE.match(wid):
            raise RuntimeError("new-window failed")
        tmux("rename-window", "-t", wid, name)
        tmux("set-option", "-w", "-t", wid, "@manual", "1")
        if cmd != "shell":
            tmux("send-keys", "-t", wid, "-l", "--", cmd)
            tmux("send-keys", "-t", wid, "Enter")
        _state_invalidate()
        return wid


def start_telegram():
    """Start the bot only when a token AND a valid allowlist are configured."""
    if not config.TG_TOKEN:
        return None
    try:
        allowed = tgbot.parse_allowed(config.TG_ALLOWED_RAW)
    except ValueError as e:
        print("Telegram integration NOT started: %s" % e, flush=True)
        return None
    if not allowed:
        print("Telegram integration NOT started: THUMBMUX_TG_ALLOWED_CHAT_IDS is "
              "empty. The bot types into your shell, so an allowlist is mandatory.",
              flush=True)
        return None
    bot = tgbot.Bot(TgBackend(), config.TG_TOKEN, allowed, DATA_DIR,
                    public_url=config.PUBLIC_URL, api_base=config.TG_API_BASE,
                    stt=voice, poll_timeout=config.TG_POLL_TIMEOUT,
                    rate_per_min=config.TG_RATE_PER_MIN,
                    min_work=config.TG_MIN_WORK_SECS, notify=config.TG_NOTIFY,
                    tail_lines=config.TG_TAIL_LINES)
    bot.start()
    return bot


AUTH = None


def main():
    global AUTH
    AUTH = authmod.Auth()        # exits if the configured password is weak
    ensure_session()
    termproxy.start()            # ttyd on a private unix socket
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    start_telegram()
    srv = ThreadingHTTPServer(LISTEN, Handler)
    host, port = LISTEN
    print("thumbmux listening on http://%s:%d  (login required)" % (host, port), flush=True)
    if host not in ("127.0.0.1", "::1", "localhost"):
        print("WARNING: bound to %s - traffic is plain HTTP unless a TLS reverse "
              "proxy sits in front. See README > Security." % host, flush=True)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        termproxy.stop()


if __name__ == "__main__":
    main()
