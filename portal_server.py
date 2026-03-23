#!/usr/bin/env python3
"""PureBrain Portal Server — per-CIV mini server for purebrain.ai
Auth via Bearer token. JSONL-based chat history (same as TG bot).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / ".portal-token"
PORTAL_HTML = SCRIPT_DIR / "portal.html"
PORTAL_PB_HTML = SCRIPT_DIR / "portal-pb-styled.html"
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()
# Auto-detect CIV_NAME + HUMAN_NAME from identity file — works in any fleet container.
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = (_identity.get("civ_id") or _identity.get("civ_name") or "unknown").lower()
    HUMAN_NAME = _identity.get("human_name", "Human")
except Exception:
    CIV_NAME = os.environ.get("CIV_NAME", "unknown").lower()
    HUMAN_NAME = os.environ.get("HUMAN_NAME", "Human")
LOG_ROOT = Path.home() / ".claude" / "projects"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORTAL_CHAT_LOG = SCRIPT_DIR / "portal-chat.jsonl"
UPLOADS_DIR = Path.home() / "portal_uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Allowed directories for file downloads
DOWNLOAD_ALLOWED_DIRS = [
    Path.home() / "civ" / "docs",
    Path.home() / "civ" / "exports",
    Path.home() / "purebrain_portal",
    Path.home() / "from-acg",
    Path.home() / "portal_uploads",
]

# OAuth flow state
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OAUTH_URL_PATTERN = re.compile(r'https://[^\s\x1b\x07\]]*oauth/authorize\?[^\s\x1b\x07\]]+')
_captured_oauth_url = None

if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token: {BEARER_TOKEN}")


def get_tmux_session() -> str:
    """Find the live primary session for this CIV."""
    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name], stderr=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False

    marker = Path.home() / ".current_session"
    if marker.exists():
        name = marker.read_text().strip()
        if name and alive(name):
            return name
    try:
        out = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                      stderr=subprocess.DEVNULL, text=True)
        for line in out.splitlines():
            if f"{CIV_NAME}-primary" in line or CIV_NAME in line:
                return line.strip()
    except Exception:
        pass
    return f"{CIV_NAME}-primary"


def _find_current_session_id():
    """Find the current Claude Code session ID from history.jsonl."""
    try:
        if not HISTORY_FILE.exists():
            return None
        with HISTORY_FILE.open("r") as f:
            f.seek(0, 2)
            length = f.tell()
            window = min(16384, length)
            f.seek(max(0, length - window))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                proj = entry.get("project", "")
                if proj and (CIV_NAME in proj.lower() or "/home/aiciv" in proj):
                    return entry.get("sessionId")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


def _get_all_session_log_paths(max_files=10):
    """Get paths to recent JSONL session logs, ordered oldest-first."""
    try:
        logs = sorted(LOG_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return list(reversed(logs[:max_files]))
    except Exception:
        return []


def _despace(text):
    """Collapse spaced-out text like 'H  e  l  l  o' back to 'Hello'.
    Some older JSONL sessions store text with spaces between every character."""
    if not text or len(text) < 6:
        return text
    # Check if text follows the pattern: char, spaces, char, spaces...
    # Sample first 40 chars to detect the pattern
    sample = text[:40]
    # Pattern: single non-space char followed by 1-2 spaces, repeating
    spaced_chars = 0
    i = 0
    while i < len(sample):
        if i + 1 < len(sample) and sample[i] != " " and sample[i + 1] == " ":
            spaced_chars += 1
            i += 1
            while i < len(sample) and sample[i] == " ":
                i += 1
        else:
            i += 1
    # If >60% of non-space chars are followed by spaces, it's spaced text
    non_space = sum(1 for c in sample if c != " ")
    if non_space > 0 and spaced_chars / non_space > 0.6:
        # Collapse: take every non-space char, but preserve intentional word gaps
        result = []
        i = 0
        while i < len(text):
            if text[i] != " ":
                result.append(text[i])
                i += 1
                # Skip the inter-character spaces (1-2 spaces)
                spaces = 0
                while i < len(text) and text[i] == " ":
                    spaces += 1
                    i += 1
                # 3+ spaces likely means intentional word boundary
                if spaces >= 3:
                    result.append(" ")
            else:
                i += 1
        return "".join(result)
    return text


def _is_real_user_message(text):
    """Check if a user message is a real human message (not system/teammate noise)."""
    if not text or len(text) < 2:
        return False
    # Telegram messages from the human - always real
    if "[TELEGRAM" in text:
        return True
    # Portal-sent messages (stored in portal chat log)
    if text.startswith("[PORTAL]"):
        return True
    # Filter out noise
    noise_markers = [
        "<teammate-message", "<system-reminder", "system-reminder",
        "Base directory for this skill", "teammate_id=",
        "<tool_result", "<function_calls", "hook success",
        "Session Ledger", "MEMORY INJECTION", "<task-notification",
        "[Image: source:", "PHOTO saved to:",
        "This session is being continued from a previous",
        "Called the Read tool", "Called the Bash tool",
        "Called the Write tool", "Called the Glob tool",
        "Called the Grep tool", "Result of calling",
        "[from-ACG]",                  # Cross-CIV system messages
        "Context restored",
        "Summary:  ",                  # Agent task summaries
        "` regex", "` sed", "| sed",   # Code snippets leaking as messages
        "re.search(r'", "re.DOTALL",
        "<command-name>", "<command-message>",  # CLI commands
        "<command-args>", "<local-command",
        "local-command-caveat", "local-command-stdout",
        "Compacted (ctrl+o",           # Compaction messages
        "&& [ -x ", "| cut -d",        # Shell code fragments
        "[portal",                     # Portal messages from session JSONL (already in portal-chat.jsonl)
    ]
    for marker in noise_markers:
        if marker in text[:300]:
            return False
    # Skip messages that look like code/config (too many special chars)
    special = sum(1 for c in text[:200] if c in '{}[]|\\`$()#')
    if len(text) < 200 and special > len(text) * 0.15:
        return False
    return True


def _clean_user_text(text):
    """Clean up user message text for display."""
    # Strip Telegram prefix for cleaner display
    if "[TELEGRAM" in text:
        # Format: [TELEGRAM private:NNN from @Username] actual message
        idx = text.find("]")
        if idx > 0:
            return text[idx + 1:].strip()
    if text.startswith("[PORTAL] "):
        return text[9:]
    return text


def _is_real_assistant_message(text):
    """Check if an assistant message is substantive (not just tool calls)."""
    if not text or len(text) < 10:
        return False
    return True


_jsonl_cache: dict = {}  # path -> (mtime, messages)
_TAIL_BYTES = 5_000_000   # read last 5 MB of large files (session logs can be 30MB+)

# IDs already written to portal-chat.jsonl — prevents duplicate mirror writes
_portal_log_ids: set = set()

# Active WebSocket connections for pushing thinking blocks
_chat_ws_clients: set = set()

# Hashes of thinking blocks already sent — prevents duplicates across reconnects
_sent_thinking_hashes: set = set()


def _init_portal_log_ids():
    """Load IDs already in portal-chat.jsonl so we don't re-mirror them."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    mid = entry.get("id")
                    if mid:
                        _portal_log_ids.add(mid)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass


def _mirror_to_portal_log(msg):
    """Write a discovered session message to portal-chat.jsonl so it survives refreshes."""
    mid = msg.get("id")
    if not mid or mid in _portal_log_ids:
        return
    _portal_log_ids.add(mid)
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(msg) + "\n")
    except Exception:
        pass


def _parse_jsonl_messages_from_file(log_path):
    """Parse a single JSONL log into clean chat messages.
    Tail-reads large files and caches by mtime for fast repeated calls."""
    messages = []
    if not log_path or not log_path.exists():
        return messages

    try:
        stat = log_path.stat()
        mtime = stat.st_mtime
        cached = _jsonl_cache.get(str(log_path))
        if cached and cached[0] == mtime:
            return cached[1]

        # Read only the tail of large files to avoid parsing megabytes each poll
        with log_path.open("rb") as fb:
            if stat.st_size > _TAIL_BYTES:
                fb.seek(-_TAIL_BYTES, 2)
                fb.readline()  # skip partial first line
            raw = fb.read()
        lines_iter = raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return messages

    try:
        for line in lines_iter:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = entry.get("message", {})
                role = msg.get("role", entry.get("type", ""))

                if role not in ("user", "assistant"):
                    continue

                content_blocks = msg.get("content", []) or []
                text_parts = []    # For normal text blocks
                char_parts = []    # For single-character string blocks
                is_char_stream = False
                for block in content_blocks:
                    if isinstance(block, str):
                        # Single char blocks: preserve spaces for word boundaries
                        if len(block) <= 2:  # single chars including '\n'
                            char_parts.append(block)
                            is_char_stream = True
                        else:
                            s = block.strip()
                            if s:
                                text_parts.append(s)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        t = (block.get("text") or "").strip()
                        if t:
                            text_parts.append(t)

                # Build combined text
                if is_char_stream and len(char_parts) > 10:
                    # Join character stream directly (preserves spaces/newlines)
                    combined = "".join(char_parts).strip()
                    # Also append any text blocks
                    if text_parts:
                        combined += "\n\n" + "\n\n".join(text_parts)
                elif text_parts:
                    combined = "\n\n".join(text_parts)
                else:
                    continue

                if not combined or len(combined) < 2:
                    continue

                # Collapse spaced-out text from older sessions
                combined = _despace(combined)

                # Filter based on role
                if role == "user":
                    if not _is_real_user_message(combined):
                        continue
                    combined = _clean_user_text(combined)
                elif role == "assistant":
                    if not _is_real_assistant_message(combined):
                        continue

                ts = entry.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts = ts / 1000  # ms to seconds
                elif isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except (ValueError, AttributeError):
                        ts = time.time()
                else:
                    ts = time.time()

                messages.append({
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                })
    except Exception:
        pass

    _jsonl_cache[str(log_path)] = (mtime, messages)
    return messages


def _load_portal_messages():
    """Load messages sent via the portal chat."""
    messages = []
    if not PORTAL_CHAT_LOG.exists():
        return messages
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    messages.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return messages


def _save_portal_message(text, role="user"):
    """Save a message sent via the portal."""
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}",
    }
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log."""
    all_messages = []

    # JSONL session logs
    for log_path in _get_all_session_log_paths(max_files=10):
        all_messages.extend(_parse_jsonl_messages_from_file(log_path))

    # Portal-sent messages
    all_messages.extend(_load_portal_messages())

    # Sort by timestamp
    all_messages.sort(key=lambda m: m["timestamp"])

    # Deduplicate by ID
    seen = set()
    deduped = []
    for m in all_messages:
        if m["id"] not in seen:
            seen.add(m["id"])
            deduped.append(m)

    return deduped[-last_n:] if len(deduped) > last_n else deduped


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == BEARER_TOKEN
    return request.query_params.get("token") == BEARER_TOKEN


# ---------------------------------------------------------------------------

# ── Favicon ──────────────────────────────────────────────────────────────

async def favicon(request: Request):
    """Serve PureBrain favicon for unified branding across all subdomains."""
    ico = SCRIPT_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=204)

async def favicon_png(request: Request):
    """Serve 32px favicon PNG."""
    png = SCRIPT_DIR / "favicon-32.png"
    if png.exists():
        return FileResponse(str(png), media_type="image/png")
    return Response(status_code=204)

async def apple_touch_icon(request: Request):
    """Serve Apple touch icon."""
    icon = SCRIPT_DIR / "apple-touch-icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)

# Routes
# ---------------------------------------------------------------------------
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "uptime": int(time.time() - START_TIME)})


async def index(request: Request) -> Response:
    if PORTAL_PB_HTML.exists():
        return FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
    if PORTAL_HTML.exists():
        return FileResponse(str(PORTAL_HTML), media_type="text/html")
    return Response("<h1>Portal HTML not found</h1>", media_type="text/html", status_code=503)


async def index_pb(request: Request) -> Response:
    """Serve PureBrain-styled portal at /pb path."""
    if PORTAL_PB_HTML.exists():
        return FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
    return Response("<h1>PB Portal not found</h1>", media_type="text/html", status_code=503)


async def index_react(request: Request) -> Response:
    """Serve React portal at /react path."""
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response("<h1>React Portal not found — run npm run build in react-portal/</h1>",
                    media_type="text/html", status_code=503)


async def api_status(request: Request) -> JSONResponse:
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    session = get_tmux_session()
    tmux_alive = False
    try:
        subprocess.check_output(["tmux", "has-session", "-t", session], stderr=subprocess.DEVNULL)
        tmux_alive = True
    except subprocess.CalledProcessError:
        pass

    claude_running = False
    try:
        out = subprocess.check_output(["pgrep", "-f", "claude"], stderr=subprocess.DEVNULL, text=True)
        claude_running = bool(out.strip())
    except subprocess.CalledProcessError:
        pass

    tg_running = False
    try:
        out = subprocess.check_output(["pgrep", "-f", "telegram"], stderr=subprocess.DEVNULL, text=True)
        tg_running = bool(out.strip())
    except subprocess.CalledProcessError:
        pass

    ctx_pct = None
    try:
        ctx_file = Path("/tmp/claude_context_used.txt")
        if ctx_file.exists():
            ctx_pct = float(ctx_file.read_text().strip())
    except Exception:
        pass

    return JSONResponse({
        "civ": CIV_NAME, "uptime": int(time.time() - START_TIME),
        "tmux_session": session, "tmux_alive": tmux_alive,
        "claude_running": claude_running, "tg_bot_running": tg_running,
        "ctx_pct": ctx_pct,
        "timestamp": int(time.time()),
    })


async def api_chat_history(request: Request) -> JSONResponse:
    """Return recent chat messages from JSONL session log."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    last_n = int(request.query_params.get("last", "100"))
    last_n = min(last_n, 500)

    messages = _parse_all_messages(last_n=last_n)

    # Mirror any session messages to portal-chat.jsonl so they survive future refreshes
    for msg in messages:
        _mirror_to_portal_log(msg)

    return JSONResponse({"messages": messages, "count": len(messages), "timestamp": int(time.time())})


async def api_chat_send(request: Request) -> JSONResponse:
    """Inject a message into the tmux session. Response comes via /api/chat/stream or history."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    _save_portal_message(message, role="user")

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        tagged = f"[portal-react] {message}"
    else:
        tagged = f"[portal] {message}"

    session = get_tmux_session()
    try:
        subprocess.run(["tmux", "send-keys", "-t", session, "-l", tagged],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        return JSONResponse({"status": "sent", "timestamp": int(time.time())})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_notify(request: Request) -> JSONResponse:
    """Save a system notification to portal chat (role=assistant, no tmux injection)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    entry = _save_portal_message(message, role="assistant")
    return JSONResponse({"status": "saved", "id": entry["id"], "timestamp": entry["timestamp"]})


async def ws_chat(websocket: WebSocket) -> None:
    """Stream new chat messages via WebSocket. Polls JSONL log for new entries."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _chat_ws_clients.add(websocket)
    seen_ids = set()

    # Send initial batch of recent messages
    messages = _parse_all_messages(last_n=200)
    for msg in messages:
        seen_ids.add(msg["id"])

    try:
        while True:
            messages = _parse_all_messages(last_n=200)
            for msg in messages:
                if msg["id"] not in seen_ids:
                    seen_ids.add(msg["id"])
                    _mirror_to_portal_log(msg)  # Persist so page refreshes don't lose messages
                    await websocket.send_text(json.dumps(msg))
            await asyncio.sleep(1.5)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _chat_ws_clients.discard(websocket)


async def api_chat_upload(request: Request) -> JSONResponse:
    """Accept a file upload, save to UPLOADS_DIR + docs/from-telegram/, log to portal chat, inject tmux notification."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        form = await request.form()
        uploaded = form.get("file")
        if not uploaded or not hasattr(uploaded, "read"):
            return JSONResponse({"error": "no file"}, status_code=400)

        caption = str(form.get("caption", "")).strip()

        content = await uploaded.read()
        if len(content) > UPLOAD_MAX_BYTES:
            return JSONResponse({"error": "file too large (max 50 MB)"}, status_code=413)

        original_name = getattr(uploaded, "filename", None) or "upload"
        # Sanitize: keep alphanumerics, dots, dashes, underscores
        safe_name = "".join(c for c in original_name if c.isalnum() or c in "._-") or "upload"
        timestamp_ms = int(time.time() * 1000)
        stored_name = f"{timestamp_ms}_{safe_name}"
        dest = UPLOADS_DIR / stored_name
        dest.write_bytes(content)

        # Also save to from-telegram/ so the CIV finds it in the standard location
        from_tg_dir = Path.home() / "from-telegram"
        from_tg_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        portal_copy_name = f"portal_{timestamp_str}_{safe_name}"
        portal_copy_path = from_tg_dir / portal_copy_name
        portal_copy_path.write_bytes(content)

        # Save to portal chat log
        chat_text = f"[File: {original_name}] uploaded to {dest}"
        if caption:
            chat_text += f"\nCaption: {caption}"
        _save_portal_message(chat_text, role="user")

        # Inject notification into the CIV's tmux session (mirrors Telegram bridge pattern)
        notify_lines = [
            f"[Portal Upload from {HUMAN_NAME}]",
            f"File saved to: {portal_copy_path}",
        ]
        if caption:
            notify_lines.append(f"INSTRUCTIONS from {HUMAN_NAME}: {caption}")
        notification = "\n".join(notify_lines)

        session = get_tmux_session()
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "-l", notification],
                check=True, stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session, "Enter"],
                check=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass  # Don't fail the upload if tmux injection fails

        return JSONResponse({
            "ok": True,
            "filename": stored_name,
            "original": original_name,
            "path": str(dest),
            "civ_path": str(portal_copy_path),
            "size": len(content),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_chat_serve_upload(request: Request) -> Response:
    """Serve an uploaded file. Token auth via query param or Bearer header."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filename = request.path_params.get("filename", "")
    # Prevent path traversal
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    filepath = UPLOADS_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(filepath))


async def api_download(request: Request) -> Response:
    """Serve a file download from whitelisted directories."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filepath_str = request.query_params.get("path", "")
    if not filepath_str:
        return JSONResponse({"error": "missing 'path' query parameter"}, status_code=400)
    try:
        filepath = Path(filepath_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    # Security: reject path traversal and check whitelist
    if ".." in filepath_str:
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    allowed = any(
        filepath == d or d in filepath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": f"path not in allowed directories"}, status_code=403)
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(filepath), filename=filepath.name)


async def api_download_list(request: Request) -> JSONResponse:
    """List files in an allowed directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dir_str = request.query_params.get("dir", "")
    if not dir_str:
        # Return list of allowed base directories
        dirs = [{"path": str(d), "name": d.name, "exists": d.exists()} for d in DOWNLOAD_ALLOWED_DIRS]
        return JSONResponse({"directories": dirs})
    try:
        dirpath = Path(dir_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    allowed = any(
        dirpath == d or d in dirpath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": "directory not in allowed list"}, status_code=403)
    if not dirpath.exists() or not dirpath.is_dir():
        return JSONResponse({"error": "directory not found"}, status_code=404)
    items = []
    for item in sorted(dirpath.iterdir()):
        items.append({
            "name": item.name,
            "path": str(item),
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None,
        })
    return JSONResponse({"dir": str(dirpath), "items": items})


def _find_primary_pane():
    """Find the tmux pane ID running the primary Claude Code instance."""
    session = get_tmux_session()
    try:
        # List all panes with their IDs
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL, text=True
        )
        panes = [p.strip() for p in out.splitlines() if p.strip()]
        if not panes:
            return session  # fallback to session target

        # Primary is always the first pane (index 0)
        # Team leads are spawned in subsequent panes
        return panes[0]
    except Exception:
        return session


async def ws_terminal(websocket: WebSocket) -> None:
    """Stream tmux pane content via WebSocket. Read-only."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pane_target = _find_primary_pane()
    last_content = ""

    try:
        while True:
            try:
                content = subprocess.check_output(
                    ["tmux", "capture-pane", "-t", pane_target, "-p"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except subprocess.CalledProcessError:
                content = "[tmux session not found]"

            if content != last_content:
                await websocket.send_text(content)
                last_content = content

            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, Exception):
        pass


async def api_context(request: Request) -> JSONResponse:
    """Return real context window usage from the latest Claude session JSONL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        MAX_TOKENS = 170_000  # ~30k reserved for responses/summaries
        logs = sorted(LOG_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return JSONResponse({"input_tokens": 0, "max_tokens": MAX_TOKENS, "pct": 0})

        latest = logs[0]
        input_tokens = 0
        cache_read = 0
        cache_creation = 0

        # Read last entry that has usage data
        with open(latest) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    usage = entry.get("usage") or entry.get("message", {}).get("usage")
                    if usage and isinstance(usage, dict):
                        t = usage.get("input_tokens", 0)
                        if t:
                            input_tokens = t
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            cache_creation = usage.get("cache_creation_input_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    continue

        total = input_tokens + cache_read + cache_creation
        pct = round(min(total / MAX_TOKENS * 100, 100), 1)
        return JSONResponse({
            "input_tokens": input_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total_tokens": total,
            "max_tokens": MAX_TOKENS,
            "pct": pct,
            "session_id": latest.stem,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_resume(request: Request) -> JSONResponse:
    """Launch a new Claude instance resuming the most recent conversation session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        logs = sorted(LOG_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return JSONResponse({"error": "no sessions found"}, status_code=404)
        session_id = logs[0].stem  # UUID filename without .jsonl
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"
        project_dir = str(Path.home())
        # Kill any stale {CIV_NAME}-primary-* sessions so prefix-matching stays unambiguous
        try:
            old = subprocess.check_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                stderr=subprocess.DEVNULL, text=True
            ).splitlines()
            for s in old:
                if s.startswith(f"{CIV_NAME}-primary-"):
                    subprocess.run(["tmux", "kill-session", "-t", s],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # Write session name so portal can track it
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        claude_cmd = (
            f"claude --model claude-sonnet-4-6 --dangerously-skip-permissions "
            f"--resume {session_id}"
        )
        subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return JSONResponse({"status": "resuming", "session_id": session_id, "tmux": tmux_session})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_panes(request: Request) -> JSONResponse:
    """Return all tmux panes with their current content."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    session = get_tmux_session()
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_title}\t#{session_name}:#{window_index}.#{pane_index}"],
            stderr=subprocess.DEVNULL, text=True
        )
        panes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            pane_id = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else pane_id
            target = parts[2] if len(parts) > 2 else pane_id
            # Only include panes from the CIV's session
            session_name = session.split(":")[0] if ":" in session else session
            if session_name not in target and session not in target:
                continue
            try:
                capture = subprocess.check_output(
                    ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except subprocess.CalledProcessError:
                capture = ""
            panes.append({"id": pane_id, "title": title or pane_id, "target": target, "content": capture})
        return JSONResponse({"panes": panes})
    except Exception as e:
        return JSONResponse({"error": str(e), "panes": []})


async def api_inject_pane(request: Request) -> JSONResponse:
    """Inject a command into a specific tmux pane by pane_id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    pane_id = body.get("pane_id", "").strip()
    message = body.get("message", "").strip()
    if not pane_id or not message:
        return JSONResponse({"error": "pane_id and message required"}, status_code=400)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", message],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        return JSONResponse({"status": "sent"})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# BOOP / Skills Endpoints (from ACG — for Settings panel)
# ---------------------------------------------------------------------------
SKILLS_DIR = Path.home() / ".claude" / "skills"
BOOP_CONFIG_FILE = SCRIPT_DIR / "boop_config.json"


async def api_compact_status(request: Request) -> JSONResponse:
    """Check if Claude is currently compacting context (shows in tmux pane)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pane = _find_primary_pane()
    try:
        content = subprocess.check_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-20"],
            stderr=subprocess.DEVNULL, text=True
        )
        # Match the specific Claude Code compacting message (not "auto-compact" warnings)
        compacting = "Compacting (ctrl+o" in content or "Compacting…" in content
        return JSONResponse({"compacting": compacting})
    except Exception:
        return JSONResponse({"compacting": False})


async def api_boop_config(request: Request) -> JSONResponse:
    """GET: read active BOOP config. POST: update active_command and/or cadence_minutes."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "POST":
        try:
            body = await request.json()
            cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
            g = cfg.setdefault("global", {})
            if "active_command" in body:
                g["active_command"] = str(body["active_command"])
            if "cadence_minutes" in body:
                g["cadence_minutes"] = int(body["cadence_minutes"])
            if "paused" in body:
                g["paused"] = bool(body["paused"])
            BOOP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
            return JSONResponse({"ok": True, "active_command": g.get("active_command"),
                                 "cadence_minutes": g.get("cadence_minutes")})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    # GET
    try:
        cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
        g = cfg.get("global", {})
        return JSONResponse({
            "active_command": g.get("active_command", "/sprint-mode"),
            "cadence_minutes": g.get("cadence_minutes", 30),
            "paused": g.get("paused", False),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boops_list(request: Request) -> JSONResponse:
    """List available BOOP/skill entries from the skills directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boops = []
    if SKILLS_DIR.exists():
        for entry in sorted(SKILLS_DIR.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    boops.append({"name": entry.name, "path": str(skill_file)})
    return JSONResponse({"boops": boops})


async def api_boop_read(request: Request) -> JSONResponse:
    """Read the content of a specific BOOP/skill."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.path_params.get("name", "")
    if ".." in name or "/" in name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = skill_file.read_text(encoding="utf-8", errors="replace")
    return JSONResponse({"name": name, "content": content})


# ---------------------------------------------------------------------------
# Claude OAuth Auth Endpoints
# ---------------------------------------------------------------------------
async def api_claude_auth_status(request: Request) -> JSONResponse:
    """Check if Claude is authenticated (has valid OAuth credentials)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if not CREDENTIALS_FILE.exists():
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        creds = json.loads(CREDENTIALS_FILE.read_text())
        oauth = creds.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        expires_at = oauth.get("expiresAt", 0)
        now_ms = int(time.time() * 1000)
        # Claude Code refreshes tokens in memory without updating the file.
        # If the tmux session is alive and Claude is running, trust it — the
        # expiresAt in credentials.json is stale, not reality.
        tmux_alive = False
        try:
            subprocess.check_output(["tmux", "has-session", "-t", get_tmux_session()],
                                    stderr=subprocess.DEVNULL)
            tmux_alive = True
        except Exception:
            pass
        if expires_at and expires_at < now_ms and not tmux_alive:
            return JSONResponse({"authenticated": False, "account": oauth.get("account"),
                                 "expires_at": expires_at})
        return JSONResponse({
            "authenticated": True, "account": oauth.get("account"),
            "expires_at": expires_at, "subscription": oauth.get("subscriptionType"),
        })
    except Exception:
        return JSONResponse({"authenticated": False, "account": None, "expires_at": None})


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Inject /login into the Claude tmux session to start OAuth flow."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    _captured_oauth_url = None
    pane = _find_primary_pane()
    _save_portal_message(f"🔐 Auth flow started — injecting /login into {get_tmux_session()}", role="assistant")
    try:
        # CRITICAL: Resize tmux window to 500 cols BEFORE sending /login.
        # Claude prints the OAuth URL as one long line — if the window is narrow
        # (e.g. 80 cols), the URL wraps and tmux capture-pane -J can't reliably
        # un-wrap it. At 500 cols the URL fits on one line, no wrapping, clean capture.
        subprocess.run(["tmux", "resize-window", "-t", pane, "-x", "500"],
                       stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "/login"],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        # Wait for the 3-option login menu to render, then press Enter
        # to auto-select option 1 (already highlighted by default)
        time.sleep(2)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=False, stderr=subprocess.DEVNULL)
        _save_portal_message("⏳ /login sent — waiting for OAuth URL to appear in terminal...", role="assistant")
        return JSONResponse({"started": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Auth start failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Inject the OAuth authorization code into the Claude tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)
    pane = _find_primary_pane()
    _save_portal_message(f"⌨️ Auth code submitted — injecting into {get_tmux_session()}...", role="assistant")
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", code],
                       check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       check=True, stderr=subprocess.DEVNULL)
        _save_portal_message("✅ Code injected — Claude is authenticating...", role="assistant")
        return JSONResponse({"injected": True})
    except subprocess.CalledProcessError as e:
        _save_portal_message(f"❌ Code injection failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from tmux output."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})
    pane = _find_primary_pane()
    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = subprocess.check_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-200"],
            stderr=subprocess.DEVNULL, text=True
        )
        match = OAUTH_URL_PATTERN.search(content)
        if match:
            candidate = match.group(0).strip()
            # Validate URL is complete — must contain state= parameter.
            # A truncated URL is worse than no URL (causes "missing state" error on claude.ai).
            if "state=" not in candidate:
                _save_portal_message(f"⚠️ OAuth URL found but truncated (missing state=) — retrying capture", role="assistant")
            else:
                _captured_oauth_url = candidate
                _save_portal_message(f"🔗 OAuth URL ready ({len(candidate)} chars, state= confirmed)", role="assistant")
                return JSONResponse({"url": _captured_oauth_url, "ready": True})
        # Silently return — no notification on each poll. Only notify when URL is found.
    except Exception as e:
        _save_portal_message(f"❌ tmux capture failed: {e}", role="assistant")
    return JSONResponse({"url": None, "ready": False})



# ---------------------------------------------------------------------------
# Thinking Stream Monitor
# ---------------------------------------------------------------------------

async def _push_thinking_to_clients(text: str, ts: int) -> None:
    """Push a thinking block to all connected WebSocket clients."""
    msg = json.dumps({
        "role": "thinking",
        "text": text,
        "timestamp": ts,
        "id": f"thinking-{hashlib.sha256(text.encode()).hexdigest()[:12]}",
    })
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _thinking_monitor_loop() -> None:
    """Background task: tail latest JSONL session file and push thinking blocks to portal."""
    last_file: str = ""
    last_pos: int = 0

    while True:
        try:
            # Find the most recently modified JSONL session file
            logs = sorted(LOG_ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not logs:
                await asyncio.sleep(2)
                continue

            current_file = str(logs[0])

            # If we switched to a new file, reset position
            if current_file != last_file:
                last_file = current_file
                last_pos = 0

            # Read new lines from where we left off
            try:
                with open(current_file, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    if file_size < last_pos:
                        # File was truncated/rotated — reset
                        last_pos = 0
                    f.seek(last_pos)
                    new_bytes = f.read()
                    last_pos = f.tell()
            except Exception:
                await asyncio.sleep(2)
                continue

            if not new_bytes:
                await asyncio.sleep(1.5)
                continue

            lines = new_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant messages
                msg = entry.get("message", {})
                if not msg or msg.get("role") != "assistant":
                    continue

                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue

                # Skip sidechain (background agent output)
                if entry.get("isSidechain"):
                    continue

                # Skip messages with tool_use blocks (bash/tool noise)
                has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content_blocks
                )
                if has_tool_use:
                    continue

                # Extract thinking blocks only
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "thinking":
                        continue
                    text = block.get("thinking", "").strip()
                    if not text or len(text) < 10:
                        continue

                    # Dedup via hash
                    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
                    if content_hash in _sent_thinking_hashes:
                        continue
                    _sent_thinking_hashes.add(content_hash)

                    ts = entry.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                        except (ValueError, AttributeError):
                            ts = int(time.time())
                    elif isinstance(ts, (int, float)):
                        ts = int(ts / 1000) if ts > 1e10 else int(ts)
                    else:
                        ts = int(time.time())

                    # Push to all connected clients (non-blocking)
                    if _chat_ws_clients:
                        await _push_thinking_to_clients(text, ts)

        except Exception:
            pass

        await asyncio.sleep(1.5)


# ---------------------------------------------------------------------------
# Fleet Endpoint
# ---------------------------------------------------------------------------
FLEET_REGISTRY_FILE = Path("/home/aiciv/civ/registry/fleet-registry.json")

# Sensitive fields that must NEVER be returned to the frontend
_FLEET_SENSITIVE_FIELDS = frozenset({
    "tg_bot_token", "gateway_secret", "bearer_token",
    "ssh_key_pub", "ssh_key_private", "corey_backdoor",
    "oauth_account", "oauth_date", "human_email", "email",
    "portal_url", "auth_email",
})


def _is_civ_entry(obj: dict) -> bool:
    """Return True if a top-level registry dict looks like a CIV (not metadata)."""
    if not isinstance(obj, dict):
        return False
    return "status" in obj and ("container" in obj or "civ_name" in obj or "container_name" in obj)


def _derive_fleet_entry(key: str, entry: dict) -> dict:
    """Transform a raw registry entry into the frontend fleet format."""
    # Derive name
    name = (
        entry.get("civname")
        or entry.get("civ_name")
        or key.split("-")[0].capitalize()
    )

    # Derive IP
    ip = entry.get("host_ip") or entry.get("ip") or ""

    # Derive SSH user
    ssh_user = entry.get("ssh_user") or "aiciv"

    # isPaid: paid flag, first_paid_client flag, or Bonded tier
    tier = str(entry.get("tier", ""))
    payment_tier = str(entry.get("payment_tier", ""))
    payment_status = str(entry.get("payment_status", ""))
    is_paid = bool(
        entry.get("paid")
        or entry.get("first_paid_client")
        or "bonded" in tier.lower()
        or "bonded" in payment_tier.lower()
        or payment_status == "paid"
    )

    # isParent: Aether is the A-C-Gee parent
    is_parent = key.lower() == "aether"

    # isSelf: compare against this CIV
    is_self = key.lower() == CIV_NAME.lower()

    # isSibling: everything that is not self and not parent
    is_sibling = not is_self and not is_parent

    # isBareMetal: check special_circumstances.bare_metal
    special = entry.get("special_circumstances", {})
    is_bare_metal = bool(special.get("bare_metal") if isinstance(special, dict) else False)

    # isBonded: Bonded tier or paid status
    is_bonded = bool(
        "bonded" in tier.lower()
        or "bonded" in payment_tier.lower()
        or payment_status == "paid"
    )

    return {
        "name": name,
        "status": entry.get("status", "unknown"),
        "human": entry.get("human") or entry.get("human_name") or "",
        "ip": ip,
        "sshPort": entry.get("ssh_port"),
        "sshUser": ssh_user,
        "tmuxSession": entry.get("tmux_session", ""),
        "isSelf": is_self,
        "isPaid": is_paid,
        "isParent": is_parent,
        "isSibling": is_sibling,
        "isBareMetal": is_bare_metal,
        "isBonded": is_bonded,
    }


async def api_fleet(request: Request) -> JSONResponse:
    """Return fleet CIV list for the frontend fleet panel."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        if not FLEET_REGISTRY_FILE.exists():
            return JSONResponse({"error": "fleet registry not found"}, status_code=404)

        registry = json.loads(FLEET_REGISTRY_FILE.read_text())

        seen_keys: set = set()
        fleet: list[dict] = []

        # 1. Entries from the "aicivs" dict
        aicivs = registry.get("aicivs", {})
        for key, entry in aicivs.items():
            if not isinstance(entry, dict):
                continue
            seen_keys.add(key)
            fleet.append(_derive_fleet_entry(key, entry))

        # 2. Top-level entries that look like CIV objects (e.g. enigma-barbara, furious-fred)
        #    Skip if the derived CIV name already exists (avoids duplicates like
        #    aicivs.furious + top-level furious-fred both producing "Furious").
        seen_names = {e["name"].lower() for e in fleet}
        skip_sections = {"version", "last_updated", "last_validation", "updated_by",
                         "description", "fleet_host", "hosts", "gateway", "aicivs",
                         "birth_pool", "available_containers", "rules", "migration_context"}
        for key, value in registry.items():
            if key in skip_sections:
                continue
            if key in seen_keys:
                continue
            if isinstance(value, dict) and _is_civ_entry(value):
                derived = _derive_fleet_entry(key, value)
                if derived["name"].lower() in seen_names:
                    continue  # already have this CIV from aicivs
                seen_keys.add(key)
                seen_names.add(derived["name"].lower())
                fleet.append(derived)

        # Sort alphabetically by name
        fleet.sort(key=lambda c: c["name"].lower())

        return JSONResponse(fleet)

    except json.JSONDecodeError:
        return JSONResponse({"error": "fleet registry is not valid JSON"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _startup() -> None:
    """Start background tasks on server startup."""
    _init_portal_log_ids()
    asyncio.create_task(_thinking_monitor_loop())


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
_react_assets_mount = (
    [Mount("/react/assets", app=StaticFiles(directory=str(REACT_DIST / "assets")))]
    if (REACT_DIST / "assets").exists()
    else []
)

# ─── MAKR OS: Deal Flow ──────────────────────────────────────────────────────

MAKR_DATA_FILE = SCRIPT_DIR / "makr-deals.json"
MAKR_CONFIG_FILE = SCRIPT_DIR / "makr-config.json"

def _load_deals():
    if MAKR_DATA_FILE.exists():
        try:
            return json.loads(MAKR_DATA_FILE.read_text())
        except Exception:
            pass
    return []

def _save_deals(deals):
    MAKR_DATA_FILE.write_text(json.dumps(deals, indent=2))

def _load_makr_config():
    if MAKR_CONFIG_FILE.exists():
        try:
            return json.loads(MAKR_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_makr_config(cfg):
    MAKR_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

async def api_makr_deals(request: Request) -> JSONResponse:
    return JSONResponse({"deals": _load_deals()})

async def api_makr_deals_add(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    deals = _load_deals()
    import datetime
    deal = {
        "id": f"deal-{len(deals)+1:04d}",
        "added": datetime.datetime.utcnow().isoformat(),
        "name":   body.get("name", "").strip(),
        "sector": body.get("sector", "").strip(),
        "raise":  body.get("raise", ""),
        "source": body.get("source", ""),
        "owner":  body.get("owner", ""),
        "stage":  body.get("stage", "intake"),
        "notes":  body.get("notes", "").strip(),
        "fit_score": "",
        "status": "New",
    }
    if not deal["name"]:
        return JSONResponse({"error": "name required"}, status_code=400)
    deals.append(deal)
    _save_deals(deals)
    # Sync to Google Sheets if configured
    cfg = _load_makr_config()
    sheet_id = cfg.get("sheet_id")
    key_file = cfg.get("key_file")
    if sheet_id and key_file and Path(key_file).exists():
        try:
            _append_deal_to_sheet(deal, sheet_id, key_file)
            return JSONResponse({"ok": True, "deal": deal, "synced": True})
        except Exception as e:
            return JSONResponse({"ok": True, "deal": deal, "synced": False, "sync_error": str(e)})
    return JSONResponse({"ok": True, "deal": deal, "synced": False})

async def api_makr_sheets_connect(request: Request) -> JSONResponse:
    """Save Google Sheets config (sheet_id + path to service account key file)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    sheet_id = body.get("sheet_id", "").strip()
    key_file = body.get("key_file", "").strip()
    if not sheet_id or not key_file:
        return JSONResponse({"error": "sheet_id and key_file required"}, status_code=400)
    if not Path(key_file).exists():
        return JSONResponse({"error": f"Key file not found: {key_file}"}, status_code=400)
    # Test the connection
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            key_file,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.sheet1
        # Write headers if empty
        if not ws.get_all_values():
            ws.append_row(["ID","Added","Company","Sector","Stage","Source","Owner","Pipeline Stage","Fit Score","Notes","Status"])
        cfg = _load_makr_config()
        cfg["sheet_id"] = sheet_id
        cfg["key_file"] = key_file
        cfg["sheet_title"] = sh.title
        _save_makr_config(cfg)
        # Backfill existing deals
        deals = _load_deals()
        for d in deals:
            try:
                _append_deal_to_sheet(d, sheet_id, key_file)
            except Exception:
                pass
        return JSONResponse({"ok": True, "sheet_title": sh.title, "backfilled": len(deals)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def api_makr_sheets_status(request: Request) -> JSONResponse:
    cfg = _load_makr_config()
    connected = bool(cfg.get("sheet_id") and cfg.get("key_file") and Path(cfg.get("key_file","")).exists())
    return JSONResponse({
        "connected": connected,
        "sheet_title": cfg.get("sheet_title", ""),
        "sheet_id": cfg.get("sheet_id", ""),
    })

def _append_deal_to_sheet(deal: dict, sheet_id: str, key_file: str):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        key_file,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).sheet1
    ws.append_row([
        deal.get("id",""),
        deal.get("added",""),
        deal.get("name",""),
        deal.get("sector",""),
        deal.get("raise",""),
        deal.get("source",""),
        deal.get("owner",""),
        deal.get("stage",""),
        deal.get("fit_score",""),
        deal.get("notes",""),
        deal.get("status","New"),
    ])

MAKR_DECKS_DIR = SCRIPT_DIR / "makr-decks"
MAKR_DECKS_DIR.mkdir(exist_ok=True)

async def api_makr_decks_upload(request: Request) -> JSONResponse:
    import shutil, uuid
    try:
        form = await request.form()
        upload = form.get("file")
        if not upload:
            return JSONResponse({"error": "no file"}, status_code=400)
        orig = Path(upload.filename).name
        safe = "".join(c for c in orig if c.isalnum() or c in "._- ").strip()
        unique = f"{uuid.uuid4().hex[:8]}_{safe}"
        dest = MAKR_DECKS_DIR / unique
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        return JSONResponse({"filename": unique, "original": orig})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _infer_sector(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["semiconductor","quantum","simulation","nanoclu","thin-film","composite","material science"]):
        return "Deep Tech"
    if any(w in t for w in ["ai ","artificial intelligence","machine learning","neural","llm","language model","gpt"]):
        return "AI / ML"
    if any(w in t for w in ["biotech","drug","therapy","cancer","clinical","immunol","bioreac","enzyme","microbub","genomic"]):
        return "BioTech"
    if any(w in t for w in ["health","medical","diagnostic","patient","hospital","insurance broker","insurance"]):
        return "HealthTech"
    if any(w in t for w in ["fintech","bank","payment","brokerage","investment","shariah","financial","trade","capital market","data room"]):
        return "Fintech"
    if any(w in t for w in ["carbon","co2","emission","sustainable","renewable","solar","geotherm","net zero","climate","saf","aviation fuel","sequester"]):
        return "CleanTech"
    if any(w in t for w in ["agriculture","crop","irrigation","farm","soil","agri"]):
        return "AgriTech"
    if any(w in t for w in ["drone","autonomous","logistics","delivery","supply chain","3pl","shipping","freight"]):
        return "Logistics"
    if any(w in t for w in ["robot","automation","lab automation","manufacturing","factory","modular"]):
        return "Robotics"
    if any(w in t for w in ["saas","platform","software","workflow","b2b","enterprise","sdk","api"]):
        return "B2B SaaS"
    if any(w in t for w in ["blockchain","crypto","defi","web3","nft","token"]):
        return "Web3"
    if any(w in t for w in ["fashion","textile","apparel","fibre"]):
        return "FashionTech"
    return ""


def _parse_company_longlist(paragraphs: list) -> list:
    import re
    companies = []
    current = None
    for raw in paragraphs:
        text = raw.strip()
        if not text:
            continue
        # Skip document-level headers / section banners
        if re.match(r'^(PNP|MAKR|SMART TECH|UNIVERSITY|\d+ companies)', text, re.IGNORECASE):
            if current:
                companies.append(current)
                current = None
            continue
        # Company header: "1. Cosmos Innovation" or "17. Fort Alto  ✓"
        m = re.match(r'^(\d{1,2})\.\s+(.+?)(?:\s+[✓✔])?$', text)
        if m:
            if current:
                companies.append(current)
            name = m.group(2).replace('✓','').replace('✔','').strip()
            current = {
                "num": int(m.group(1)), "name": name,
                "raise": "", "country": "", "university": "",
                "description": "", "contact_name": "", "contact_email": "",
                "website": "", "has_contact": ('✓' in text or '✔' in text),
            }
            continue
        if current is None:
            continue
        # Raised / Country / University
        if text.startswith("Raised:"):
            r = re.search(r'Raised:\s*([^\s].*?)(?:\s{2,}|$)', text)
            c = re.search(r'Country:\s*([^\s].*?)(?:\s{2,}|University:|$)', text)
            u = re.search(r'University:\s*(.+)', text)
            if r: current["raise"]      = r.group(1).strip().rstrip("|").strip()
            if c: current["country"]    = c.group(1).strip().rstrip("|").strip()
            if u: current["university"] = u.group(1).strip()
            continue
        # Contact
        if text.startswith("Contact:"):
            cm = re.search(r'Contact:\s*(.+?)(?:\s*\|\s*Email:\s*(.+))?$', text)
            if cm:
                current["contact_name"]  = cm.group(1).strip()
                current["contact_email"] = (cm.group(2) or "").strip()
            continue
        # Website
        if text.startswith("Website:") or text.startswith("http"):
            current["website"] = text.replace("Website:", "").strip()
            continue
        # Description
        if current["description"]:
            current["description"] += " " + text
        else:
            current["description"] = text
    if current:
        companies.append(current)
    return companies


def _search_company_website(name: str) -> str:
    """Search DuckDuckGo for company website, fall back to domain guessing."""
    import requests as req
    from bs4 import BeautifulSoup
    import re, urllib.parse

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

    # 1. DuckDuckGo HTML search
    try:
        query = urllib.parse.quote(f"{name} official website startup")
        r = req.get(f"https://html.duckduckgo.com/html/?q={query}", headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.select(".result__url, .result__a"):
            href = el.get("href") or el.get_text()
            # DDG wraps URLs in redirects like //duckduckgo.com/l/?uddg=...
            if "uddg=" in str(href):
                m = re.search(r'uddg=([^&]+)', str(href))
                if m:
                    href = urllib.parse.unquote(m.group(1))
            href = str(href).strip()
            if not href or "duckduckgo" in href or "google" in href:
                continue
            if not href.startswith("http"):
                href = "https://" + href.lstrip("/")
            # Skip social / aggregator sites
            skip = ["linkedin","twitter","crunchbase","pitchbook","angel.co","facebook","instagram","youtube"]
            if any(s in href for s in skip):
                continue
            return href
    except Exception:
        pass

    # 2. Common domain guesses
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    for tld in [".com", ".ai", ".io", ".co"]:
        url = f"https://{slug}{tld}"
        try:
            r = req.head(url, headers=headers, timeout=4, allow_redirects=True)
            if r.status_code < 400:
                return r.url
        except Exception:
            pass

    return ""


async def api_makr_deals_enrich(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        name = params.get("name", "").strip()
        deal_id = params.get("id", name).strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)

        website = _search_company_website(name)
        if not website:
            return JSONResponse({"found": False, "website": ""})

        # Persist into deal notes
        deals = _load_deals()
        for d in deals:
            if d.get("id") == deal_id or d.get("name") == name:
                notes = d.get("notes", "")
                if "Web:" not in notes:
                    d["notes"] = (notes + f" | Web: {website}").strip(" |")
                break
        _save_deals(deals)
        return JSONResponse({"found": True, "website": website})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_makr_deals_update(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        deal_id = body.get("id")
        new_stage = body.get("stage")
        if not deal_id or not new_stage:
            return JSONResponse({"error": "id and stage required"}, status_code=400)
        deals = _load_deals()
        for d in deals:
            if d.get("id") == deal_id or d.get("name") == deal_id:
                d["stage"] = new_stage
                break
        _save_deals(deals)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_makr_docs_parse(request: Request) -> JSONResponse:
    import shutil, uuid
    try:
        form = await request.form()
        upload = form.get("file")
        if not upload:
            return JSONResponse({"error": "no file"}, status_code=400)
        orig = Path(upload.filename).name
        ext  = orig.rsplit(".", 1)[-1].lower()
        # Save temporarily
        tmp = MAKR_DECKS_DIR / f"tmp_{uuid.uuid4().hex[:8]}.{ext}"
        with tmp.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        companies = []
        if ext in ("docx", "doc"):
            from docx import Document
            doc = Document(str(tmp))
            paragraphs = [p.text for p in doc.paragraphs]
            parsed = _parse_company_longlist(paragraphs)
            for co in parsed:
                notes_parts = []
                if co["raise"]:       notes_parts.append(f"Raised: {co['raise']}")
                if co["country"]:     notes_parts.append(f"Country: {co['country']}")
                if co["university"]:  notes_parts.append(f"University: {co['university']}")
                if co["contact_email"]: notes_parts.append(f"Contact: {co['contact_name']} <{co['contact_email']}>")
                if co["website"]:     notes_parts.append(f"Web: {co['website']}")
                if co["description"]: notes_parts.append(co["description"][:300])
                sector = _infer_sector(co["description"])
                companies.append({
                    "name":    co["name"],
                    "sector":  sector,
                    "raise":   "",          # stage dropdown (Pre-seed/Seed etc) - leave blank, fundraise in notes
                    "source":  "Conference",
                    "notes":   " | ".join(notes_parts),
                    "country": co["country"],
                    "raised":  co["raise"],
                    "contact_email": co["contact_email"],
                    "website": co["website"],
                })
        tmp.unlink(missing_ok=True)
        return JSONResponse({"companies": companies, "count": len(companies), "filename": orig})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)

# ─── END MAKR OS ─────────────────────────────────────────────────────────────

routes = [
    Route("/favicon.ico", endpoint=favicon),
    Route("/favicon-32.png", endpoint=favicon_png),
    Route("/apple-touch-icon.png", endpoint=apple_touch_icon),
    Route("/", endpoint=index),
    Route("/guardian", endpoint=lambda r: FileResponse(str(SCRIPT_DIR / "guardian.html"), media_type="text/html")),
    Route("/makr-os", endpoint=lambda r: FileResponse(str(SCRIPT_DIR / "makr-os.html"), media_type="text/html", headers={"Cache-Control": "no-store"})),
    Route("/api/makr/deals", endpoint=api_makr_deals, methods=["GET"]),
    Route("/api/makr/deals/add", endpoint=api_makr_deals_add, methods=["POST"]),
    Route("/api/makr/sheets/connect", endpoint=api_makr_sheets_connect, methods=["POST"]),
    Route("/api/makr/sheets/status", endpoint=api_makr_sheets_status),
    Route("/api/makr/decks/upload", endpoint=api_makr_decks_upload, methods=["POST"]),
    Route("/api/makr/docs/parse",   endpoint=api_makr_docs_parse,    methods=["POST"]),
    Route("/api/makr/deals/update",  endpoint=api_makr_deals_update,  methods=["POST"]),
    Route("/api/makr/deals/enrich",  endpoint=api_makr_deals_enrich),
    Mount("/makr-decks", app=StaticFiles(directory=str(MAKR_DECKS_DIR)), name="makr-decks"),
    Route("/pb", endpoint=index_pb),
    Route("/react", endpoint=index_react),
    *_react_assets_mount,
    Route("/health", endpoint=health),
    Route("/api/status", endpoint=api_status),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/send", endpoint=api_chat_send, methods=["POST"]),
    Route("/api/notify", endpoint=api_notify, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/uploads/{filename}", endpoint=api_chat_serve_upload),
    Route("/api/auth/status", endpoint=api_claude_auth_status),
    Route("/api/auth/start", endpoint=api_claude_auth_start, methods=["POST"]),
    Route("/api/auth/code", endpoint=api_claude_auth_code, methods=["POST"]),
    Route("/api/auth/url", endpoint=api_claude_auth_url),
    Route("/api/resume", endpoint=api_resume, methods=["POST"]),
    Route("/api/panes", endpoint=api_panes),
    Route("/api/inject/pane", endpoint=api_inject_pane, methods=["POST"]),
    Route("/api/compact/status", endpoint=api_compact_status),
    Route("/api/context", endpoint=api_context),
    Route("/api/download", endpoint=api_download),
    Route("/api/download/list", endpoint=api_download_list),
    Route("/api/boop/config", endpoint=api_boop_config, methods=["GET", "POST"]),
    Route("/api/boops", endpoint=api_boops_list),
    Route("/api/boops/{name}", endpoint=api_boop_read),
    Route("/api/fleet", endpoint=api_fleet),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
]

app = Starlette(routes=routes, on_startup=[_startup])

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8097))
    print(f"[portal] Starting PureBrain Portal on port {port}")
    print(f"[portal] Bearer token: {BEARER_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
