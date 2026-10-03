#!/usr/bin/env python3
"""DomusChat — minimal chat GUI backed by SQLite.

Zero dependencies: Python stdlib only.
- GET    /                        -> index.html
- GET    /healthz                 -> liveness
- GET    /api/models              -> upstream model catalog (normalized)
- GET    /api/settings            -> {"system_prompt": ...}
- PUT    /api/settings            -> save system prompt
- GET    /api/usage?conv_id=&model= -> ctx window stats
- GET    /api/conversations       -> conversation list
- POST   /api/conversations       -> create {title?}
- DELETE /api/conversations/{id}  -> delete conversation
- GET    /api/conversations/{id}/messages
- POST   /api/upload              -> multipart text file, RAM only (temporary)
- DELETE /api/files/{id}          -> drop temp file from RAM
- POST   /api/chat                -> SSE stream {message, model, conv_id?, file_ids?}
- POST   /api/compress            -> manual context compression {conv_id, model}

Environment:
  UPSTREAM        base URL of the OpenAI-compatible backend (default :1234)
  UPSTREAM_AUTH   manager API token (sent as api_key query param — header auth
                  is stripped in transit on the LAN)
  DB_PATH         sqlite file (default /data/chat.db)
  PORT            listen port (default 8080)
  COMPRESS_AT     fraction of ctx window that triggers auto-compression (default 0.9)
  COMPRESS_KEEP   recent messages kept verbatim after compression (default 6)
  RESP_RESERVE    tokens reserved for the model response (default 8192)
  FILE_TTL_HOURS  lifetime of temporary uploads in RAM (default 6)
  FILE_MAX_MB     max upload size (default 5)
"""

import html
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:1234").rstrip("/")
AUTH_TOKEN = os.environ.get("UPSTREAM_AUTH", "")
DB_PATH = os.environ.get("DB_PATH", "/data/chat.db")
TOKENS_PATH = os.environ.get("TOKENS_PATH", os.path.join(os.path.dirname(DB_PATH), "tokens.json"))
PORT = int(os.environ.get("PORT", "8080"))

COMPRESS_AT = float(os.environ.get("COMPRESS_AT", "0.9"))
STT_URL = os.environ.get("STT_URL", "").rstrip("/")
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
COMPRESS_KEEP = int(os.environ.get("COMPRESS_KEEP", "6"))
RESP_RESERVE = int(os.environ.get("RESP_RESERVE", "8192"))
FILE_TTL_S = int(float(os.environ.get("FILE_TTL_HOURS", "6")) * 3600)
FILE_MAX_BYTES = int(float(os.environ.get("FILE_MAX_MB", "5")) * 1024 * 1024)

CHARS_PER_TOKEN = 4          # rough estimate, fine for budgeting
FALLBACK_CTX = 8192          # when the catalog has no ctx info
SUMMARY_MAX_TOKENS = 2048

_db_lock = threading.Lock()
_files_lock = threading.Lock()
FILE_STORE = {}              # fid -> {name, text, size, ts}  (RAM only, never persisted)
_model_cache = {"ts": 0.0, "models": []}


def est_tokens(text):
    return max(1, len(text or "") // CHARS_PER_TOKEN)


# ── sessioni: token chiari in tokens.json {token: etichetta} ────────────────
# Auth leggera: chi ha un token valido può creare/chat; chi apre un link
# condiviso (?conv=id) legge quella conversazione anche senza token.
_tokens_lock = threading.Lock()


def load_tokens():
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def issue_token(label=""):
    """Crea un token sessione e lo salva nel JSON (chiaro, come richiesto).
    Chiave semplice per la famiglia: 6 cifre numeriche, uniche nel file."""
    with _tokens_lock:
        d = load_tokens()
        for _ in range(200):
            tok = "".join(secrets.choice("0123456789") for _ in range(6))
            if tok not in d:
                break
        else:
            tok = "dc-" + secrets.token_urlsafe(24)  # fallback se qualcuno abuse
        d[tok] = label or "sessione-" + time.strftime("%Y%m%d-%H%M")
        tmp = TOKENS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, TOKENS_PATH)
    return tok


def valid_token(tok):
    if not tok:
        return False
    with _tokens_lock:
        return tok in load_tokens()


def token_from_request(self):
    """Header X-Session -> cookie dc_session -> query ?token=***"""
    h = self.headers.get("X-Session") or ""
    if valid_token(h):
        return h
    ck = self.headers.get("Cookie") or ""
    m = re.search(r"(?:^|;\s*)dc_session=([^;]+)", ck)
    if m and valid_token(m.group(1)):
        return m.group(1)
    qtok = urllib.parse.parse_qs(urlparse(self.path).query).get("token", [""])[0]
    if valid_token(qtok):
        return qtok
    return ""


def cookie_header(tok):
    # 30 giorni; SameSite=Lax per il link condiviso; niente Secure così
    # funziona anche su http interno (l'edge HTTPS lo accetta comunque)
    return ("Set-Cookie", "dc_session=%s; Max-Age=2592000; Path=/; SameSite=Lax" % tok)


def owner_key(tok):
    """Chiave di proprietà deterministica dal token (non espone il token in DB)."""
    return hashlib.sha256(("domuschat-owner|" + tok).encode()).hexdigest()[:16]


def require_session(self):
    """Restituisce il token o None (e ha già risposto 401)."""
    tok = token_from_request(self)
    if not tok:
        self._send({"error": "sessione richiesta"}, 401)
        return None
    return tok


def q(sql, args=(), one=False, insert=False, returning=False):
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, args)
            if insert:
                conn.commit()
                return cur.lastrowid
            if returning:
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
            rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return (rows[0] if rows else None) if one else rows
        finally:
            conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT NOT NULL DEFAULT 'Nuova chat',
                  model TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conv_id INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  reasoning TEXT,
                  hidden INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
                CREATE TABLE IF NOT EXISTS settings(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prompts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_name ON prompts(name COLLATE NOCASE);
                """
            )
            # idempotent migration for pre-existing DBs (hidden column)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
            if "hidden" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if "stats" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN stats TEXT")
            ccols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
            if "owner" not in ccols:
                conn.execute("ALTER TABLE conversations ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            # idempotent migration: legacy single system_prompt -> named prompt library
            prow = conn.execute("SELECT value FROM settings WHERE key='system_prompt'").fetchone()
            if prow and (prow[0] or "").strip():
                nrow = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()
                if nrow[0] == 0:
                    now = int(time.time())
                    conn.execute(
                        "INSERT INTO prompts(name, content, created_at, updated_at) VALUES(?,?,?,?)",
                        ("Work", prow[0], now, now),
                    )
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES('active_prompt','Work') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES('active_prompt_id',CAST(("
                        "SELECT id FROM prompts WHERE name='Work' LIMIT 1) AS TEXT)) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.commit()
        finally:
            conn.close()


def get_setting(key, default=""):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    q(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ── libreria system prompt ─────────────────────────────────────────
def prompt_list():
    return q("SELECT id, name, LENGTH(content) AS chars, updated_at FROM prompts ORDER BY name COLLATE NOCASE")


def prompt_get(pid):
    rows = q("SELECT id, name, content, updated_at FROM prompts WHERE id=?", (pid,))
    return rows[0] if rows else None


def prompt_by_name(name):
    rows = q("SELECT id, name, content, updated_at FROM prompts WHERE name=? COLLATE NOCASE", (name,))
    return rows[0] if rows else None


def prompt_create(name, content):
    now = int(time.time())
    row = q(
        "INSERT INTO prompts(name, content, created_at, updated_at) VALUES(?,?,?,?) "
        "RETURNING id, name, content, updated_at",
        (name, content, now, now),
        returning=True,
    )
    if row is None:  # driver senza RETURNING
        row = prompt_by_name(name)
    if row is None:  # ultima spiaggia: verifica l'esistenza per il chiamante
        return {}
    return row


def prompt_update(pid, name=None, content=None):
    row = prompt_get(pid)
    if row is None:
        return False
    now = int(time.time())
    if name is not None:
        try:
            q("UPDATE prompts SET name=?, updated_at=? WHERE id=?", (name, now, pid))
        except sqlite3.IntegrityError:
            return False
    if content is not None:
        q("UPDATE prompts SET content=?, updated_at=? WHERE id=?", (content, now, pid))
    return True


def prompt_delete(pid):
    if prompt_get(pid) is None:
        return False
    q("DELETE FROM prompts WHERE id=?", (pid,))
    if get_setting("active_prompt_id") == str(pid):
        set_setting("active_prompt_id", "")
        set_setting("active_prompt", "")
    return True


def active_prompt():
    """Il system prompt attivo. Vuoto se l'utente ha scelto 'nessuno'."""
    pid = get_setting("active_prompt_id").strip()
    if pid.isdigit():
        row = prompt_get(int(pid))
        if row:
            return row["content"]
    name = get_setting("active_prompt").strip()
    if name:
        row = prompt_by_name(name)
        if row:
            return row["content"]
    return ""


def active_prompt_name():
    pid = get_setting("active_prompt_id").strip()
    if pid.isdigit():
        row = prompt_get(int(pid))
        if row:
            return row["name"]
    return ""


def auth_url(path="/v1/chat/completions"):
    url = UPSTREAM + path
    if AUTH_TOKEN:
        url += "?" + urllib.parse.urlencode({"api_key": AUTH_TOKEN})
    return url


def upstream_open(payload: dict, stream: bool, path="/v1/chat/completions"):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(auth_url(path), data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream" if stream else "application/json")
    return urllib.request.urlopen(req, timeout=900)


def manager_status():
    """Stato reale del LLM manager (/status). None se irraggiungibile."""
    try:
        with urllib.request.urlopen(auth_url("/status"), timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def status_text(st):
    """Frase di stato leggibile dallo stato reale del manager."""
    if not st:
        return None
    ph = st.get("state")
    if ph == "loading":
        ld = st.get("loading") or {}
        s = "Carico %s in VRAM" % (ld.get("model") or st.get("alias") or "?")
        pct = ld.get("progress_pct")
        if pct is not None:
            s += " — %d%%" % pct
        eta = ld.get("eta_remaining")
        if eta:
            s += " · ETA ~%ds" % int(eta)
        return s
    if ph == "ready":
        return "Modello pronto — prefill in corso"
    if ph == "idle":
        return "Nessun modello in VRAM — richiesta di caricamento"
    if ph == "error":
        return "Il manager segnala un errore"
    return None


# ---------------- temp files (RAM only) ----------------

def files_prune():
    now = time.time()
    with _files_lock:
        for fid in [k for k, v in FILE_STORE.items() if now - v["ts"] > FILE_TTL_S]:
            del FILE_STORE[fid]


def file_put(name, text):
    files_prune()
    fid = "f%d" % int(time.time() * 1000) + os.urandom(3).hex()
    with _files_lock:
        FILE_STORE[fid] = {"name": name, "text": text, "size": len(text), "ts": time.time()}
    return fid


def file_get(fid):
    with _files_lock:
        f = FILE_STORE.get(fid)
        return dict(f) if f else None


def file_del(fid):
    with _files_lock:
        return FILE_STORE.pop(fid, None) is not None


def parse_multipart(body: bytes, content_type: str):
    """Minimal multipart/form-data parser -> [(name, filename, data)]."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return []
    boundary = m.group(1).encode()
    out = []
    for part in body.split(b"--" + boundary):
        part = part.lstrip(b"\r\n")
        if not part or part.startswith(b"--"):
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        htxt = head.decode("utf-8", "replace")
        name = fname = None
        for line in htxt.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                nm = re.search(r'name="([^"]*)"', line)
                fm = re.search(r'filename="([^"]*)"', line)
                name = nm.group(1) if nm else None
                fname = fm.group(1) if fm else None
        out.append((name, fname, data))
    return out


TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".yml", ".yaml",
    ".html", ".css", ".sh", ".sql", ".xml", ".ini", ".toml", ".cfg", ".conf",
    ".c", ".h", ".cpp", ".go", ".rs", ".java", ".rb", ".php", ".pl", ".lua",
    ".tex", ".rst", ".srt", ".vtt", ".env", ".gitignore", ".dockerfile",
}


def decode_text(data: bytes):
    """Return decoded text or None if binary."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except UnicodeDecodeError:
            return None


# ---------------- context budgeting & compression ----------------

def catalog(force=False):
    now = time.time()
    if not force and now - _model_cache["ts"] < 15:
        return _model_cache["models"]
    try:
        with urllib.request.urlopen(auth_url("/v1/models"), timeout=15) as r:
            raw = json.loads(r.read().decode())
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        models = []
        for m in data:
            if not isinstance(m, dict):
                continue
            status = m.get("status") or ""
            # the endpoint is the source of truth: hide models it reports as
            # deleted from disk or unusable in this runtime
            if status in ("missing", "unsupported", "removed"):
                continue
            models.append({
                "id": m.get("id") or "?",
                "label": m.get("label") or m.get("alias") or m.get("id") or "?",
                "alias": m.get("alias") or m.get("id") or "",
                "available": m.get("available", True),
                "loaded": m.get("loaded", False),
                "status": status,
                "ctx": int(m.get("ctx") or FALLBACK_CTX),
            })
        _model_cache.update(ts=now, models=models)
    except Exception:
        if not _model_cache["models"]:
            _model_cache["models"] = []
    return _model_cache["models"]


def model_ctx(model_id):
    for m in catalog():
        if m["id"] == model_id:
            return m["ctx"] or FALLBACK_CTX
    return FALLBACK_CTX


def ctx_limit(model_id):
    """Token budget for the prompt (history + system + files) before compressing."""
    return max(2048, int(model_ctx(model_id) * COMPRESS_AT) - RESP_RESERVE)


def visible_messages(conv_id):
    return q(
        "SELECT id, role, content, reasoning, stats FROM messages "
        "WHERE conv_id=? AND hidden=0 ORDER BY id ASC",
        (conv_id,),
    )


def last_assistant_id(conv_id):
    """Ultimo messaggio assistant non compresso (per regenerate/elimina)."""
    row = q(
        "SELECT id FROM messages "
        "WHERE conv_id=? AND role='assistant' AND hidden=0 "
        "ORDER BY id DESC LIMIT 1",
        (conv_id,),
        one=True,
    )
    return row["id"] if row else None


def history_tokens(conv_id):
    total = est_tokens(get_setting("system_prompt"))
    for m in visible_messages(conv_id):
        total += est_tokens(m["content"]) + 4
    return total


def summarize(model_id, msgs):
    """Ask the model itself for a dense summary of old messages."""
    transcript = "\n\n".join(
        "%s: %s" % (m["role"].upper(), m["content"]) for m in msgs
    )
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": (
                "Sei un compressore di contesto. Riassumi la conversazione in modo denso e "
                "fattuale, in italiano, preservando: decisioni prese, numeri, nomi di file, "
                "configurazioni, richieste dell'utente e risultati ottenuti. Niente preamboli, "
                "solo il riassunto strutturato in punti."
            )},
            {"role": "user", "content": transcript},
        ],
        "stream": False,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "temperature": 0.3,
    }
    with upstream_open(payload, stream=False) as r:
        resp = json.loads(r.read().decode())
    return (resp["choices"][0]["message"].get("content") or "").strip()


def compress_conversation(conv_id, model_id):
    """Summarize old messages, hide them, insert summary row. Returns info dict or None."""
    msgs = visible_messages(conv_id)
    if len(msgs) <= COMPRESS_KEEP + 2:
        return None  # too short to be worth compressing
    old, recent = msgs[:-COMPRESS_KEEP], msgs[-COMPRESS_KEEP:]
    before = sum(est_tokens(m["content"]) for m in msgs)
    summary = summarize(model_id, old)
    if not summary:
        return None
    now = int(time.time())
    q(
        "UPDATE messages SET hidden=1 WHERE conv_id=? AND hidden=0 AND id <= ("
        "  SELECT id FROM messages WHERE conv_id=? AND hidden=0 ORDER BY id ASC LIMIT 1 OFFSET ?"
        ")",
        (conv_id, conv_id, len(old) - 1),
    )
    q(
        "INSERT INTO messages(conv_id, role, content, reasoning, hidden, created_at) "
        "VALUES(?, 'system', ?, NULL, 0, ?)",
        (conv_id, "📜 CONTESTO COMPRESSO — riassunto dei messaggi precedenti:\n" + summary, now),
    )
    q("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
    after = sum(est_tokens(m["content"]) for m in visible_messages(conv_id))
    return {"hidden": len(old), "kept": len(recent), "before": before, "after": after,
            "summary_chars": len(summary)}


def build_payload_messages(conv_id, model_id, user_text, file_blocks):
    """Assemble the message list: system prompt + summary/history + files + user text.
    user_text=None -> history only (regenerate: l'ultimo turno è già in history)."""
    msgs = []
    sys_prompt = get_setting("system_prompt").strip()
    if sys_prompt:
        msgs.append({"role": "system", "content": sys_prompt})
    for m in visible_messages(conv_id):
        if m["role"] == "system":
            msgs.append({"role": "system", "content": m["content"]})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    if user_text is None:
        return msgs
    content = user_text
    if file_blocks:
        content = file_blocks + "\n\n" + user_text
    msgs.append({"role": "user", "content": content})
    return msgs


def make_file_blocks(conv_id, model_id, file_ids, user_text):
    """Build the <file> block string, truncating to fit the remaining ctx budget."""
    files = []
    for fid in file_ids or []:
        f = file_get(fid)
        if f:
            files.append(f)
    if not files:
        return "", 0
    used = est_tokens(user_text) + history_tokens(conv_id) + est_tokens(get_setting("system_prompt"))
    budget_tokens = max(1024, ctx_limit(model_id) - used - RESP_RESERVE // 2)
    per_file = max(512, budget_tokens // len(files))
    blocks, total_tokens = [], 0
    for f in files:
        text = f["text"]
        cap = per_file * CHARS_PER_TOKEN
        truncated = ""
        if len(text) > cap:
            text = text[:cap]
            truncated = "\n…[TRONCATO: file troppo grande per il contesto, mostrati i primi %d caratteri]" % cap
        blocks.append('<file name="%s">\n%s%s\n</file>' % (f["name"], text, truncated))
        total_tokens += est_tokens(text)
    header = "[File temporanei allegati dall'utente — contenuti forniti solo per questa " \
             "elaborazione, non memorizzati. Usa gli attributi name per riferirti ai file.]"
    return header + "\n" + "\n".join(blocks), total_tokens


def make_handler():
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # keep logs clean; errors are surfaced to client

        # ---------- helpers ----------
        def _send(self, obj, status=200, headers=None):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for kv in headers or []:
                self.send_header(*kv)
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self, limit=64 * 1024 * 1024):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                n = 0
            if n <= 0:
                return b""
            if n > limit:
                raise ValueError("payload too large")
            return self.rfile.read(n)

        def _json_body(self):
            raw = self._read_body()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                return {}

        def _sse_begin(self):
            # SSE has no Content-Length: end-of-body == close. Force connection
            # close after the stream or keep-alive clients hang waiting for EOF.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

        def _sse_write(self, event: str, data: dict):
            payload = ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode()
            self.wfile.write(payload)
            self.wfile.flush()

        def _serve_index(self):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._send({"error": "index.html missing"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ---------- static assets (app shell PWA) ----------
        STATIC_TYPES = {
            ".png": "image/png",
            ".json": "application/manifest+json",
            ".js": "application/javascript; charset=utf-8",
        }
        STATIC_FILES = ("manifest.json", "sw.js", "registerSW.js",
                        "icon-192.png", "icon-512.png", "icon-512-maskable.png")

        def _serve_static(self, name):
            if name not in self.STATIC_FILES:
                return self._send({"error": "not found"}, 404)
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", name)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._send({"error": "asset missing"}, 404)
            ctype = self.STATIC_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # il SW deve poter aggiornare: niente cache lunga sugli asset dell'app shell
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)

        # ---------- routes ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path.startswith("/static/") or path.count("/") == 1 and path.endswith(
                    (".png", ".json", ".js")):
                name = path[len("/static/"):] if path.startswith("/static/") else path[1:]
                if "/" not in name:
                    return self._serve_static(name)
            if path == "/healthz":
                return self._send({"ok": True, "upstream": UPSTREAM})
            if path == "/api/session":
                tok = token_from_request(self)
                if not tok:
                    ua = (self.headers.get("User-Agent") or "")[:40]
                    tok = issue_token(ua or "sessione")
                    return self._send({"token": tok, "new": True}, 200, [cookie_header(tok)])
                return self._send({"token": tok, "new": False}, 200, [cookie_header(tok)])
            if path == "/api/models":
                return self._send(catalog())
            if path == "/api/settings":
                return self._send({
                    "system_prompt": active_prompt(),
                    "active_prompt": active_prompt_name(),
                })
            if path == "/api/prompts":
                items = prompt_list() or []
                active_id = get_setting("active_prompt_id").strip()
                for it in items:
                    it["active"] = (str(it["id"]) == active_id)
                cur = next((x for x in items if str(x["id"]) == active_id), None)
                nm = cur["name"] if cur else ""
                return self._send({"items": items, "active": nm,
                                   "active_name": nm, "active_id": active_id})
            if path.startswith("/api/prompts/"):
                rest = path[len("/api/prompts/"):]
                if rest == "active":
                    row = None
                    pid = get_setting("active_prompt_id").strip()
                    if pid.isdigit():
                        row = prompt_get(int(pid))
                    if row is None:
                        return self._send({"error": "nessun prompt attivo"}, 404)
                    return self._send(row)
                try:
                    pid = int(rest)
                except ValueError:
                    return self._send({"error": "bad id"}, 400)
                row = prompt_get(pid)
                if row is None:
                    return self._send({"error": "not found"}, 404)
                return self._send(row)
            if path == "/api/usage":
                conv_id = int(qs.get("conv_id", ["0"])[0] or 0)
                model_id = qs.get("model", [""])[0]
                ctx = model_ctx(model_id) if model_id else FALLBACK_CTX
                used = history_tokens(conv_id) if conv_id else 0
                with _files_lock:
                    nfiles = len(FILE_STORE)
                return self._send({
                    "ctx": ctx, "used": used,
                    "pct": round(100.0 * used / ctx, 2) if ctx else 0,
                    "temp_files": nfiles,
                })
            if path == "/api/conversations":
                # lista della sessione corrente + legacy senza owner (visibili a tutti)
                tok = token_from_request(self)
                if tok:
                    rows = q(
                        "SELECT c.id, c.title, c.model, c.updated_at, "
                        "  (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id AND m.hidden=0) AS n "
                        "FROM conversations c WHERE c.owner=? OR c.owner='' "
                        "ORDER BY c.updated_at DESC LIMIT 200",
                        (owner_key(tok),),
                    )
                else:
                    rows = []
                return self._send(rows)
            if path.endswith("/messages") and path.startswith("/api/conversations/"):
                try:
                    conv_id = int(path[len("/api/conversations/"):-len("/messages")])
                except ValueError:
                    return self._send({"error": "bad id"}, 400)
                rows = q(
                    "SELECT id, role, content, reasoning, created_at, stats FROM messages "
                    "WHERE conv_id=? AND hidden=0 ORDER BY id ASC",
                    (conv_id,),
                )
                return self._send(rows)
            return self._send({"error": "not found"}, 404)

        def do_PUT(self):
            path = self.path.split("?")[0]
            m1 = re.match(r"^/api/conversations/(\d+)$", path)
            if m1:
                # rinomina la conversazione (solo il proprietario o legacy)
                conv_id = int(m1.group(1))
                tok = require_session(self)
                if not tok:
                    return
                body = self._json_body()
                title = str(body.get("title") or "").strip()[:80]
                if not title:
                    return self._send({"error": "title richiesto"}, 400)
                row = q("SELECT owner FROM conversations WHERE id=?", (conv_id,), one=True)
                if not row:
                    return self._send({"error": "not found"}, 404)
                if row["owner"] and row["owner"] != owner_key(tok):
                    return self._send({"error": "non tua"}, 401)
                q("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
                return self._send({"ok": True, "id": conv_id, "title": title})
            m2 = re.match(r"^/api/conversations/(\d+)/messages/(\d+)$", path)
            if m2:
                body = self._json_body()
                new = (body.get("content") or "").strip()
                if not new:
                    return self._send({"error": "content required"}, 400)
                if not self._check_conv_owner(int(m2.group(1))):
                    return
                row = q("SELECT role FROM messages WHERE id=? AND conv_id=?",
                        (int(m2.group(2)), int(m2.group(1))), one=True)
                if not row:
                    return self._send({"error": "message not found"}, 404)
                if row["role"] != "user":
                    return self._send({"error": "solo messaggi utente modificabili"}, 400)
                q("UPDATE messages SET content=? WHERE id=? AND conv_id=?",
                  (new, int(m2.group(2)), int(m2.group(1))))
                return self._send({"ok": True, "id": int(m2.group(2))})
            if path == "/api/settings":
                body = self._json_body()
                sp = body.get("system_prompt", "")
                if not isinstance(sp, str) or len(sp) > 16000:
                    return self._send({"error": "system_prompt must be a string <= 16000 chars"}, 400)
                # aggiorna il prompt ATTIVO (se ce n'è uno)
                pid = get_setting("active_prompt_id").strip()
                if pid.isdigit() and prompt_get(int(pid)):
                    prompt_update(int(pid), content=sp)
                    return self._send({"ok": True, "chars": len(sp), "prompt": active_prompt_name()})
                # nessun prompt attivo: creane uno e attivalo
                row = prompt_create("Work", sp)
                if row:
                    set_setting("active_prompt_id", str(row["id"]))
                    set_setting("active_prompt", row["name"])
                return self._send({"ok": True, "chars": len(sp), "prompt": row["name"] if row else ""})
            if path.startswith("/api/prompts/"):
                try:
                    pid = int(path[len("/api/prompts/"):])
                except ValueError:
                    return self._send({"error": "bad id"}, 400)
                body = self._json_body()
                name = body.get("name")
                content = body.get("content")
                if name is not None:
                    name = str(name).strip()[:64]
                    if not name:
                        return self._send({"error": "name non può essere vuoto"}, 400)
                if content is not None:
                    content = str(content)
                    if len(content) > 16000:
                        return self._send({"error": "content troppo lungo (max 16000 char)"}, 400)
                if not prompt_update(pid, name=name, content=content):
                    return self._send({"error": "not found or nome duplicato"}, 404)
                return self._send({"ok": True, "prompt": prompt_get(pid)})
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/prompts":
                body = self._json_body()
                name = str(body.get("name") or "").strip()[:64]
                content = str(body.get("content") or "")
                if not name:
                    return self._send({"error": "name obbligatorio"}, 400)
                if len(content) > 16000:
                    return self._send({"error": "content troppo lungo (max 16000 char)"}, 400)
                try:
                    row = prompt_create(name, content)
                except sqlite3.IntegrityError:
                    return self._send({"error": "esiste già un prompt con questo nome"}, 409)
                if row is None:
                    return self._send({"error": "creazione fallita"}, 500)
                row = prompt_by_name(str(name))
                if body.get("activate") and row:
                    set_setting("active_prompt_id", str(row["id"]))
                    set_setting("active_prompt", row["name"])
                return self._send(row or {"name": str(name)})
            if path == "/api/prompts/active":
                body = self._json_body()
                pid = body.get("id")
                nm = body.get("name") or body.get("active")
                row = None
                if nm not in (None, ""):
                    row = prompt_by_name(str(nm))
                elif pid not in (None, "", 0):
                    try:
                        row = prompt_get(int(pid))
                    except (TypeError, ValueError):
                        row = None
                set_setting("active_prompt_id", str(row["id"]) if row else "")
                set_setting("active_prompt", row["name"] if row else "")
                return self._send({"active": row["name"] if row else "",
                                   "id": row["id"] if row else None})
            if path == "/api/conversations":
                body = self._json_body()
                tok = require_session(self)
                if not tok:
                    return
                now = int(time.time())
                title = (body.get("title") or "Nuova chat").strip()[:80] or "Nuova chat"
                cid = q(
                    "INSERT INTO conversations(title, model, created_at, updated_at, owner) VALUES(?,?,?,?,?)",
                    (title, body.get("model"), now, now, owner_key(tok)),
                    insert=True,
                )
                return self._send({"id": cid, "title": title})
            if path == "/api/upload":
                return self._handle_upload()
            if path == "/api/stt":
                return self._handle_stt()
            if path == "/api/tts":
                return self._handle_tts()
            if path == "/api/compress":
                body = self._json_body()
                if not self._check_conv_owner(int(body.get("conv_id") or 0)):
                    return
                conv_id = int(body.get("conv_id") or 0)
                model_id = body.get("model") or ""
                if not conv_id or not model_id:
                    return self._send({"error": "conv_id and model required"}, 400)
                try:
                    info = compress_conversation(conv_id, model_id)
                except Exception as e:
                    return self._send({"error": "compression failed: %s" % e}, 502)
                if info is None:
                    return self._send({"ok": False, "reason": "conversazione troppo corta per la compressione"})
                info["ok"] = True
                return self._send(info)
            if path == "/api/chat":
                return self._handle_chat()
            if path == "/api/regenerate":
                # rigenera l'ultima risposta: stesso payload chat, senza nuovo turno utente
                body = self._json_body()
                body["regenerate"] = True
                return self._handle_chat(body)
            return self._send({"error": "not found"}, 404)

        def _check_conv_owner(self, conv_id):
            """True se la sessione può scrivere su conv_id (owner o legacy->adotta).
            Risponde 401/404 e restituisce False in caso contrario."""
            tok = token_from_request(self)
            if not tok:
                self._send({"error": "sessione richiesta"}, 401)
                return False
            me = owner_key(tok)
            row = q("SELECT owner FROM conversations WHERE id=?", (conv_id,), one=True)
            if not row:
                self._send({"error": "conversazione inesistente"}, 404)
                return False
            if row["owner"] and row["owner"] != me:
                self._send({"error": "conversazione di un'altra sessione"}, 401)
                return False
            if not row["owner"]:
                q("UPDATE conversations SET owner=? WHERE id=?", (me, conv_id))
            return True

        def do_DELETE(self):
            path = self.path.split("?")[0]
            if path.startswith("/api/prompts/"):
                try:
                    pid = int(path[len("/api/prompts/"):])
                except ValueError:
                    return self._send({"error": "bad id"}, 400)
                if not prompt_delete(pid):
                    return self._send({"error": "not found"}, 404)
                if get_setting("active_prompt_id").strip() == str(pid):
                    set_setting("active_prompt_id", "")
                    set_setting("active_prompt", "")
                return self._send({"deleted": pid})
            m2 = re.match(r"^/api/conversations/(\d+)/messages/(\d+)$", path)
            if m2:
                msg_id = int(m2.group(2))
                if not self._check_conv_owner(int(m2.group(1))):
                    return
                row = q("SELECT id, role FROM messages WHERE id=? AND conv_id=?",
                        (msg_id, int(m2.group(1))), one=True)
                if not row:
                    return self._send({"error": "message not found"}, 404)
                q("DELETE FROM messages WHERE id=? AND conv_id=?",
                  (msg_id, int(m2.group(1))))
                return self._send({"deleted": msg_id, "role": row["role"]})
            if path.startswith("/api/conversations/"):
                try:
                    conv_id = int(path[len("/api/conversations/"):])
                except ValueError:
                    return self._send({"error": "bad id"}, 400)
                if not self._check_conv_owner(conv_id):
                    return
                q("DELETE FROM messages WHERE conv_id=?", (conv_id,))
                q("DELETE FROM conversations WHERE id=?", (conv_id,))
                return self._send({"deleted": conv_id})
            if path.startswith("/api/files/"):
                fid = path[len("/api/files/"):]
                ok = file_del(fid)
                return self._send({"deleted": fid} if ok else {"error": "not found"}, 200 if ok else 404)
            return self._send({"error": "not found"}, 404)

        # ---------- upload ----------
        def _handle_upload(self):
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._send({"error": "expected multipart/form-data"}, 400)
            try:
                body = self._read_body(limit=FILE_MAX_BYTES + 1024 * 1024)
            except ValueError:
                return self._send({"error": "file troppo grande (max %d MB)" % (FILE_MAX_BYTES // (1024 * 1024))}, 413)
            parts = parse_multipart(body, ctype)
            saved = []
            for name, filename, data in parts:
                if name != "file" or not filename:
                    continue
                if len(data) > FILE_MAX_BYTES:
                    return self._send({"error": "%s: troppo grande (max %d MB)" % (filename, FILE_MAX_BYTES // (1024 * 1024))}, 413)
                text = decode_text(data)
                if text is None:
                    return self._send({"error": "%s: file binario non supportato — solo testo (txt, md, code, json, csv, log…)" % filename}, 415)
                ext = os.path.splitext(filename)[1].lower()
                base = os.path.basename(filename).strip() or ("file" + ext)
                fid = file_put(base, text)
                saved.append({"id": fid, "name": base, "chars": len(text),
                              "tokens_est": est_tokens(text)})
            if not saved:
                return self._send({"error": "nessun file ricevuto"}, 400)
            return self._send({"files": saved})

        # ---------- voice: STT proxy + TTS edge ----------
        def _handle_stt(self):
            """Riceve l'audio dal browser (multipart) e lo gira a STT_URL."""
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._send({"error": "expected multipart/form-data"}, 400)
            try:
                body = self._read_body(limit=25 * 1024 * 1024)
            except ValueError:
                return self._send({"error": "audio troppo grande (max 25 MB)"}, 413)
            req = urllib.request.Request(
                STT_URL + "/transcribe", data=body, method="POST",
                headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return self._send(json.loads(r.read().decode()))
            except Exception as e:
                return self._send({"error": "STT non raggiungibile: %s" % e}, 502)

        @staticmethod
        def _tts_clean(text):
            """Prepara la risposta per la voce: via markdown, codice, link."""
            import re
            t = re.sub(r"```.*?```", " [codice omesso] ", text, flags=re.S)
            t = re.sub(r"`([^`]*)`", r"\1", t)
            t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
            t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
            t = re.sub(r"[#*_>|~]+", " ", t)
            t = re.sub(r"^\s*[-•]+\s+", "", t, flags=re.M)
            return re.sub(r"\s{2,}", " ", t).strip()

        def _handle_tts(self):
            """Sintetizza la risposta in mp3 (edge-tts, voce it-IT configurabile)."""
            body = self._json_body()
            text = self._tts_clean(str(body.get("text") or ""))
            if not text:
                return self._send({"error": "testo vuoto"}, 400)
            try:
                import edge_tts
            except ImportError:
                return self._send({"error": "edge-tts non installato nel container"}, 501)
            voice = body.get("voice") or TTS_VOICE
            import io as _io
            buf = _io.BytesIO()
            try:
                import asyncio

                async def _gen():
                    com = edge_tts.Communicate(text[:4000], voice)
                    async for chunk in com.stream():
                        if chunk.get("type") == "audio":
                            buf.write(chunk["data"])
                asyncio.run(_gen())
            except Exception as e:
                return self._send({"error": "TTS fallita: %s" % e}, 502)
            data = buf.getvalue()
            if not data:
                return self._send({"error": "TTS vuota"}, 502)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # ---------- chat (SSE) ----------
        def _handle_chat(self, body=None):
            if body is None:
                body = self._json_body()
            regen = bool(body.get("regenerate"))
            model = body.get("model") or ""
            user_text = (body.get("message") or "").strip()
            conv_id = body.get("conv_id")
            file_ids = body.get("file_ids") or []
            max_tokens = int(body.get("max_tokens") or 4096)
            if regen:
                # niente nuovo turno utente: serve conv_id e testo da reinviare al modello
                if not conv_id:
                    return self._send({"error": "conv_id required for regenerate"}, 400)
            if not model or (not user_text and not regen):
                return self._send({"error": "model and message required"}, 400)
            tok = require_session(self)
            if not tok:
                return
            me = owner_key(tok)

            # auto-load model if the manager supports it (fire and forget on failure)
            load_model_if_needed(model)

            if not conv_id:
                now = int(time.time())
                title = user_text[:60].replace("\n", " ")
                conv_id = q(
                    "INSERT INTO conversations(title, model, created_at, updated_at, owner) VALUES(?,?,?,?,?)",
                    (title, model, now, now, me),
                    insert=True,
                )
            else:
                conv_id = int(conv_id)
                row = q("SELECT owner FROM conversations WHERE id=?", (conv_id,), one=True)
                if not row:
                    return self._send({"error": "conversazione inesistente"}, 404)
                if row["owner"] and row["owner"] != me:
                    return self._send({"error": "conversazione di un'altra sessione"}, 401)
                if not row["owner"]:
                    # eredita le chat legacy: le adotta chi ci scrive per primo
                    q("UPDATE conversations SET owner=? WHERE id=?", (me, conv_id))

            # regenerate: riusa l'ultimo turno utente della conversazione
            if body.get("regenerate"):
                last_user = q(
                    "SELECT content FROM messages WHERE conv_id=? AND role='user' AND hidden=0 "
                    "ORDER BY id DESC LIMIT 1",
                    (conv_id,), one=True,
                )
                if not last_user:
                    return self._send({"error": "nothing to regenerate"}, 400)
                user_text = re.sub(r"\s*📎.*$", "", last_user["content"], flags=re.S)
                # scarta l'ultima risposta: il payload dev'finire col turno utente
                q(
                    "DELETE FROM messages WHERE id=("
                    "  SELECT id FROM messages WHERE conv_id=? AND role='assistant' AND hidden=0 "
                    "  ORDER BY id DESC LIMIT 1)",
                    (conv_id,),
                )

            # temp files -> prompt blocks (never persisted; only names go to the DB)
            file_blocks, file_tokens = make_file_blocks(conv_id, model, file_ids, user_text)

            # auto-compression check (history only; files are re-injected each turn)
            compressed = None
            limit = ctx_limit(model)
            projected = history_tokens(conv_id) + est_tokens(user_text) + file_tokens + est_tokens(get_setting("system_prompt"))
            if projected > limit:
                try:
                    compressed = compress_conversation(conv_id, model)
                except Exception as e:
                    compressed = {"error": str(e)}

            msgs = build_payload_messages(
                conv_id, model,
                None if body.get("regenerate") else user_text,  # regen: turno già in history
                file_blocks,
            )
            payload = {
                "model": model,
                "messages": msgs,
                "stream": True,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
                "temperature": body.get("temperature", 0.7),
            }

            # persist the user turn (file *names* only, as reference)
            stored_text = user_text
            names = [file_get(f)["name"] for f in file_ids if file_get(f)]
            if names:
                stored_text += "\n📎 " + ", ".join(names)
            now = int(time.time())
            user_msg_id = None
            if not body.get("regenerate"):
                user_msg_id = q(
                    "INSERT INTO messages(conv_id, role, content, reasoning, hidden, created_at) VALUES(?,?,?,?,0,?)",
                    (conv_id, "user", stored_text, None, now),
                    insert=True,
                )
            q("UPDATE conversations SET updated_at=?, model=? WHERE id=?", (now, model, conv_id))

            # begin SSE
            self._sse_begin()
            meta = {"conv_id": conv_id, "ctx": model_ctx(model)}
            if compressed:
                meta["compressed"] = compressed
            self._sse_write("meta", meta)

            full = []
            reasoning = []
            err = None
            # poller: finché l'upstream non risponde, emetti lo STATO REALE del manager
            stop_status = threading.Event()

            def status_pump():
                last = None
                while True:
                    st = manager_status()
                    txt = status_text(st)
                    if txt and txt != last and not stop_status.is_set():
                        last = txt
                        try:
                            self._sse_write("status", {"s": txt})
                        except Exception:
                            break
                    if stop_status.wait(2.0):
                        break

            pump = threading.Thread(target=status_pump, daemon=True)
            pump.start()
            t_stream = None
            ttft = None
            usage = None      # {prompt_tokens, completion_tokens, ...}
            timings = None    # timings llama.cpp (prefill/gen)
            try:
                with upstream_open(payload, stream=True) as resp:
                    stop_status.set()
                    pump.join(timeout=5)  # azzera il pump prima di scrivere contenuti
                    t_stream = time.time()
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            j = json.loads(chunk)
                        except Exception:
                            continue
                        if j.get("usage"):
                            usage = j["usage"]
                        if j.get("timings"):
                            timings = j["timings"]
                        ch0 = (j.get("choices") or [{}])[0]
                        delta = ch0.get("delta") or {}
                        rc = delta.get("reasoning_content")
                        cc = delta.get("content")
                        if (rc or cc) and ttft is None:
                            ttft = time.time() - t_stream
                        if rc:
                            reasoning.append(rc)
                            self._sse_write("reasoning", {"t": rc})
                        if cc:
                            full.append(cc)
                            self._sse_write("content", {"t": cc})
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode(errors="replace")[:500]
                except Exception:
                    detail = str(e)
                err = "upstream %s: %s" % (e.code, detail)
            except Exception as e:
                err = "upstream error: %s" % e

            text = "".join(full).strip()
            rtext = "".join(reasoning).strip()
            if err:
                text = (text + "\n\n" if text else "") + "[errore] " + err
                self._sse_write("error", {"message": err})

            # metriche reali dai timings llama.cpp (chunk usage):
            # prefill_tps = velocità di calcolo dei token NON in cache
            # gen_tps     = token/s in generazione
            stats = {}
            if timings:
                if timings.get("prompt_per_second"):
                    stats["prefill_tps"] = round(timings["prompt_per_second"], 1)
                if timings.get("predicted_per_second"):
                    stats["gen_tps"] = round(timings["predicted_per_second"], 1)
                if usage:
                    stats["prompt_tokens"] = usage.get("prompt_tokens")
                    stats["completion_tokens"] = usage.get("completion_tokens")
                if timings.get("cache_n"):
                    stats["cache_n"] = timings["cache_n"]
            if ttft is not None:
                stats["ttft_s"] = round(ttft, 2)
            stats["model"] = model

            msg_id = None
            if text or rtext:
                now2 = int(time.time())
                msg_id = q(
                    "INSERT INTO messages(conv_id, role, content, reasoning, hidden, created_at, stats) VALUES(?,?,?,?,0,?,?)",
                    (conv_id, "assistant", text, rtext or None, now2,
                     json.dumps(stats) if stats else None),
                    insert=True,
                )
                q("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))

            used = history_tokens(conv_id)
            self._sse_write("done", {
                "conv_id": conv_id, "chars": len(text), "msg_id": msg_id,
                "user_msg_id": user_msg_id,
                "ctx": model_ctx(model), "ctx_used": used,
                "pct": round(100.0 * used / model_ctx(model), 2),
                "stats": stats,
            })

    return H


def load_model_if_needed(model: str):
    """Ask the manager to load the model if it exposes an admin endpoint.
    BEST-EFFORT E NON-BLOCCANTE: la POST chat in coda al manager innesca il
    load comunque; aspettare qui (20s+ di caricamento VRAM) bloccherebbe il
    thread HTTP oltre il timeout del client -> 'errore rete'. Timeout 3s."""
    try:
        req = urllib.request.Request(
            auth_url("/v1/models/%s/load" % urllib.parse.quote(model)),
            data=b"{}",
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read(200)
    except Exception:
        pass  # manager may not support explicit load; chat will trigger it anyway


def main():
    init_db()
    files_prune()
    catalog(force=True)  # warm cache
    handler = make_handler()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    srv.daemon_threads = True
    print("DomusChat listening on :%d -> upstream %s (db %s)" % (PORT, UPSTREAM, DB_PATH), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
