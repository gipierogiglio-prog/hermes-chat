"""
Hermes Chat App
===============
Web app com login Google e grupos contextuais para conversar com Hermes.
Cada grupo tem seu próprio vault no Obsidian.
"""

import os
import json
import uuid
import hashlib
import hmac
import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
import httpx
import sqlite3
import sqlite3 as sqlite
from dotenv import load_dotenv

# Load .env
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me")
VAULT_BASE_DIR = Path(os.getenv("VAULT_BASE_DIR", str(Path.home() / "obsidian-vault" / "Grupos")))
DB_PATH = Path(__file__).parent / "chat.db"

app = FastAPI(title="Hermes Chat")

# Session middleware for OAuth state
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            picture TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '',
            vault_path TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            user_id TEXT,
            user_name TEXT,
            content TEXT NOT NULL,
            is_hermes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
    """)
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Session helpers (signed cookies)
# ---------------------------------------------------------------------------
def make_session_token(user_id: str) -> str:
    """Create a signed session token."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    raw = f"{user_id}|{ts}"
    sig = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{raw}|{sig}"

def verify_session_token(token: str) -> Optional[str]:
    """Verify signed session token and return user_id."""
    try:
        parts = token.split("|")
        if len(parts) != 3:
            return None
        user_id, ts, sig = parts
        raw = f"{user_id}|{ts}"
        expected = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
        if hmac.compare_digest(expected, sig):
            return user_id
    except Exception:
        pass
    return None

def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("session")
    if not token:
        return None
    user_id = verify_session_token(token)
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------
def ensure_vault_dir(group_id: str, group_name: str) -> Path:
    """Create vault directory for a group if it doesn't exist."""
    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in group_name).strip()
    vault_dir = VAULT_BASE_DIR / f"{safe_name} ({group_id[:8]})"
    vault_dir.mkdir(parents=True, exist_ok=True)
    return vault_dir

def write_to_vault(vault_dir: Path, message_data: dict, is_hermes: bool = False):
    """Append a message to the group's vault conversation log."""
    vault_file = vault_dir / "conversas.md"
    author = message_data.get("user_name", "Hermes" if is_hermes else "Desconhecido")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"\n### {ts} — {author}\n{message_data['content']}\n"
    with open(vault_file, "a", encoding="utf-8") as f:
        f.write(line)

def write_context_to_vault(vault_dir: Path, group_name: str, context: str):
    """Write the group's context to the vault index."""
    index_file = vault_dir / "index.md"
    content = f"""# {group_name}

## Contexto
{context}

## Conversas
As conversas deste grupo estão registradas em [conversas.md](conversas.md).
"""
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(content)

# ---------------------------------------------------------------------------
# Hermes API Client
# ---------------------------------------------------------------------------
async def ask_hermes(messages: list[dict], group_context: str = "") -> str:
    """Send messages to Hermes API Server and return the response."""
    system_prompt = f"""You are Hermes, an AI agent running on Gian's VPS.
You are in a group chat with context: {group_context}

Respond naturally to the user's messages. You have full access to your tools
(terminal, file operations, web search, etc.) to help with whatever the group needs.
Be concise and helpful."""
    
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            HERMES_API_URL,
            headers={
                "Authorization": f"Bearer {HERMES_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "hermes-agent",
                "messages": full_messages,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------
from authlib.integrations.starlette_client import OAuth

oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    init_db()
    VAULT_BASE_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/auth/login")
async def login_google(request: Request):
    """Redirect to Google OAuth."""
    if not GOOGLE_CLIENT_ID:
        return HTMLResponse(
            "<h1>Google OAuth não configurado</h1>"
            "<p>Configure GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET no .env</p>"
            "<p>Veja o .env.example para instruções.</p>"
        )
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback."""
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get("userinfo") or (await oauth.google.parse_id_token(request, token))
    except Exception as e:
        return HTMLResponse(f"<h1>Erro no login</h1><p>{e}</p>")
    
    user_id = userinfo["sub"]
    email = userinfo.get("email", "")
    name = userinfo.get("name", email.split("@")[0])
    picture = userinfo.get("picture", "")
    
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO users (id, email, name, picture) VALUES (?, ?, ?, ?)",
        (user_id, email, name, picture),
    )
    conn.commit()
    conn.close()
    
    # Create session cookie
    session_token = make_session_token(user_id)
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=30 * 24 * 3600,  # 30 days
        httponly=True,
        samesite="lax",
    )
    return response

@app.get("/auth/me")
async def get_me(request: Request):
    """Get current user info."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
        }
    })

@app.post("/auth/logout")
async def logout():
    response = RedirectResponse(url="/")
    response.delete_cookie("session")
    return response

# ---------------------------------------------------------------------------
# Groups API
# ---------------------------------------------------------------------------

class GroupCreate(BaseModel):
    name: str
    context: str = ""

@app.post("/api/groups")
async def create_group(request: Request, data: GroupCreate):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    group_id = uuid.uuid4().hex[:12]
    vault_path = str(ensure_vault_dir(group_id, data.name))
    
    conn = get_db()
    conn.execute(
        "INSERT INTO groups (id, name, context, vault_path, created_by) VALUES (?, ?, ?, ?, ?)",
        (group_id, data.name, data.context, vault_path, user["id"]),
    )
    conn.execute(
        "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)",
        (group_id, user["id"], "admin"),
    )
    conn.commit()
    conn.close()
    
    # Write context to vault
    write_context_to_vault(Path(vault_path), data.name, data.context)
    
    return JSONResponse({
        "id": group_id,
        "name": data.name,
        "context": data.context,
        "vault_path": vault_path,
    })

@app.get("/api/groups")
async def list_groups(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    conn = get_db()
    rows = conn.execute("""
        SELECT g.*, gm.role,
            (SELECT COUNT(*) FROM messages WHERE group_id = g.id) as msg_count
        FROM groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE gm.user_id = ?
        ORDER BY g.created_at DESC
    """, (user["id"],)).fetchall()
    conn.close()
    
    return JSONResponse([{
        "id": r["id"],
        "name": r["name"],
        "context": r["context"],
        "role": r["role"],
        "msg_count": r["msg_count"],
        "created_at": r["created_at"],
    } for r in rows])

@app.patch("/api/groups/{group_id}")
async def update_group(request: Request, group_id: str):
    """Update group context."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    data = await request.json()
    context = data.get("context", "")
    
    conn = get_db()
    group = conn.execute("""
        SELECT g.*, gm.role FROM groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE g.id = ? AND gm.user_id = ?
    """, (group_id, user["id"])).fetchone()
    
    if not group:
        conn.close()
        raise HTTPException(404, "Grupo não encontrado")
    
    if group["role"] != "admin":
        conn.close()
        raise HTTPException(403, "Apenas admins podem editar o contexto")
    
    conn.execute("UPDATE groups SET context = ? WHERE id = ?", (context, group_id))
    conn.commit()
    vault_dir = Path(group["vault_path"])
    write_context_to_vault(vault_dir, group["name"], context)
    conn.close()
    
    return JSONResponse({"status": "ok", "context": context})

@app.get("/api/groups/{group_id}")
async def get_group(request: Request, group_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    conn = get_db()
    group = conn.execute("""
        SELECT g.*, gm.role
        FROM groups g
        JOIN group_members gm ON g.id = gm.group_id
        WHERE g.id = ? AND gm.user_id = ?
    """, (group_id, user["id"])).fetchone()
    
    if not group:
        conn.close()
        raise HTTPException(404, "Grupo não encontrado")
    
    # Get members
    members = conn.execute("""
        SELECT u.id, u.name, u.email, u.picture, gm.role
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ?
    """, (group_id,)).fetchall()
    conn.close()
    
    return JSONResponse({
        "id": group["id"],
        "name": group["name"],
        "context": group["context"],
        "vault_path": group["vault_path"],
        "role": group["role"],
        "members": [{
            "id": m["id"],
            "name": m["name"],
            "email": m["email"],
            "picture": m["picture"],
            "role": m["role"],
        } for m in members],
    })

@app.post("/api/groups/{group_id}/join")
async def join_group(request: Request, group_id: str):
    """Join a group by invite code (group_id acts as invite)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        conn.close()
        raise HTTPException(404, "Grupo não encontrado")
    
    # Check if already a member
    existing = conn.execute(
        "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user["id"]),
    ).fetchone()
    if existing:
        conn.close()
        return JSONResponse({"status": "already_member"})
    
    conn.execute(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, user["id"]),
    )
    conn.commit()
    conn.close()
    
    return JSONResponse({"status": "joined"})

# ---------------------------------------------------------------------------
# Messages API
# ---------------------------------------------------------------------------

@app.get("/api/groups/{group_id}/messages")
async def get_messages(request: Request, group_id: str, before: str = Query(""), limit: int = Query(50)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    conn = get_db()
    membership = conn.execute(
        "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user["id"]),
    ).fetchone()
    if not membership:
        conn.close()
        raise HTTPException(403, "Você não é membro deste grupo")
    
    if before:
        rows = conn.execute("""
            SELECT * FROM messages
            WHERE group_id = ? AND id < ?
            ORDER BY created_at DESC LIMIT ?
        """, (group_id, before, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM messages
            WHERE group_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (group_id, limit)).fetchall()
    conn.close()
    
    return JSONResponse([{
        "id": r["id"],
        "user_id": r["user_id"],
        "user_name": r["user_name"],
        "content": r["content"],
        "is_hermes": bool(r["is_hermes"]),
        "created_at": r["created_at"],
    } for r in reversed(rows)])

@app.post("/api/groups/{group_id}/messages")
async def send_message(request: Request, group_id: str, data: dict):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Não autenticado")
    
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(400, "Mensagem vazia")
    
    conn = get_db()
    membership = conn.execute(
        "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user["id"]),
    ).fetchone()
    if not membership:
        conn.close()
        raise HTTPException(403, "Você não é membro deste grupo")
    
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    conn.close()
    
    # Save user message
    msg_id = uuid.uuid4().hex[:12]
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (id, group_id, user_id, user_name, content) VALUES (?, ?, ?, ?, ?)",
        (msg_id, group_id, user["id"], user["name"], content),
    )
    conn.commit()
    conn.close()
    
    # Write user message to vault
    vault_dir = Path(group["vault_path"])
    write_to_vault(vault_dir, {"user_name": user["name"], "content": content})
    
    # Get recent messages for context
    conn = get_db()
    recent = conn.execute("""
        SELECT user_name, content, is_hermes FROM messages
        WHERE group_id = ? ORDER BY created_at ASC LIMIT 20
    """, (group_id,)).fetchall()
    conn.close()
    
    # Build Hermes message history
    hermes_messages = []
    for m in recent:
        role = "assistant" if m["is_hermes"] else "user"
        name = "Hermes" if m["is_hermes"] else m["user_name"]
        hermes_messages.append({"role": role, "content": f"{name}: {m['content']}"})
    
    # Ask Hermes
    try:
        hermes_response = await ask_hermes(hermes_messages, group["context"])
    except Exception as e:
        hermes_response = f"⚠️ Erro ao contactar Hermes: {e}"
    
    # Save Hermes response
    hermes_msg_id = uuid.uuid4().hex[:12]
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (id, group_id, user_name, content, is_hermes) VALUES (?, ?, ?, ?, 1)",
        (hermes_msg_id, group_id, "Hermes", hermes_response),
    )
    conn.commit()
    conn.close()
    
    # Write Hermes response to vault
    write_to_vault(vault_dir, {"user_name": "Hermes", "content": hermes_response}, is_hermes=True)
    
    return JSONResponse({
        "user_message": {
            "id": msg_id,
            "content": content,
            "user_name": user["name"],
            "is_hermes": False,
        },
        "hermes_message": {
            "id": hermes_msg_id,
            "content": hermes_response,
            "user_name": "Hermes",
            "is_hermes": True,
        },
    })

# ---------------------------------------------------------------------------
# WebSocket for real-time
# ---------------------------------------------------------------------------
connected_clients: dict[str, list] = {}  # group_id -> [websocket, ...]

@app.websocket("/ws/{group_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str):
    token = websocket.query_params.get("token", "")
    user_id = verify_session_token(token)
    if not user_id:
        await websocket.close(code=4001)
        return
    
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    membership = conn.execute(
        "SELECT * FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    conn.close()
    
    if not user or not membership:
        await websocket.close(code=4003)
        return
    
    await websocket.accept()
    
    if group_id not in connected_clients:
        connected_clients[group_id] = []
    connected_clients[group_id].append(websocket)
    
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "message":
                content = data.get("content", "").strip()
                if not content:
                    continue
                
                # Save user message
                msg_id = uuid.uuid4().hex[:12]
                conn = get_db()
                conn.execute(
                    "INSERT INTO messages (id, group_id, user_id, user_name, content) VALUES (?, ?, ?, ?, ?)",
                    (msg_id, group_id, user_id, user["name"], content),
                )
                conn.commit()
                
                group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
                conn.close()
                
                # Broadcast user message
                user_msg = {
                    "type": "user_message",
                    "id": msg_id,
                    "user_id": user_id,
                    "user_name": user["name"],
                    "content": content,
                    "timestamp": datetime.datetime.now().isoformat(),
                }
                for ws in connected_clients.get(group_id, []):
                    try:
                        await ws.send_json(user_msg)
                    except Exception:
                        pass
                
                # Write to vault
                vault_dir = Path(group["vault_path"])
                write_to_vault(vault_dir, {"user_name": user["name"], "content": content})
                
                # Get context for Hermes
                conn = get_db()
                recent = conn.execute("""
                    SELECT user_name, content, is_hermes FROM messages
                    WHERE group_id = ? ORDER BY created_at ASC LIMIT 20
                """, (group_id,)).fetchall()
                conn.close()
                
                hermes_messages = []
                for m in recent:
                    role = "assistant" if m["is_hermes"] else "user"
                    name = "Hermes" if m["is_hermes"] else m["user_name"]
                    hermes_messages.append({"role": role, "content": f"{name}: {m['content']}"})
                
                # Broadcast "typing" indicator
                for ws in connected_clients.get(group_id, []):
                    try:
                        await ws.send_json({"type": "hermes_typing"})
                    except Exception:
                        pass
                
                # Ask Hermes
                try:
                    hermes_response = await ask_hermes(hermes_messages, group["context"])
                except Exception as e:
                    hermes_response = f"⚠️ Erro ao contactar Hermes: {e}"
                
                # Save Hermes response
                hermes_msg_id = uuid.uuid4().hex[:12]
                conn = get_db()
                conn.execute(
                    "INSERT INTO messages (id, group_id, user_name, content, is_hermes) VALUES (?, ?, ?, ?, 1)",
                    (hermes_msg_id, group_id, "Hermes", hermes_response),
                )
                conn.commit()
                conn.close()
                
                # Write to vault
                write_to_vault(vault_dir, {"user_name": "Hermes", "content": hermes_response}, is_hermes=True)
                
                # Broadcast Hermes response
                hermes_msg = {
                    "type": "hermes_message",
                    "id": hermes_msg_id,
                    "content": hermes_response,
                    "timestamp": datetime.datetime.now().isoformat(),
                }
                for ws in connected_clients.get(group_id, []):
                    try:
                        await ws.send_json(hermes_msg)
                    except Exception:
                        pass
                        
    except WebSocketDisconnect:
        pass
    finally:
        if group_id in connected_clients:
            connected_clients[group_id] = [ws for ws in connected_clients[group_id] if ws != websocket]
            if not connected_clients[group_id]:
                del connected_clients[group_id]

# Make sure static files are served (before route handlers)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Serve the index.html directly as a static file
    index_path = Path(__file__).parent / "templates" / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Hermes Chat</h1><p>Carregando...</p>")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    print(f"🚀 Hermes Chat rodando em http://localhost:{port}")
    print(f"📝 Configure Google OAuth no .env para ativar login")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True, ws_ping_interval=30)
