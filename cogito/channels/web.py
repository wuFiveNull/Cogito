"""
cogito.channels.web — Web Channel（纯 asyncio HTTP 服务器）

支持多 Session 管理：
  - GET  /              → 聊天页面 HTML
  - POST /api/chat      → 发送消息（需要 session_id）
  - POST /api/new_session → 创建新 session
  - GET  /api/sessions  → 列出当前 sessions
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from uuid import uuid4

from cogito.bus.events import (
    DeliveryReceipt,
    InboundMessage,
    MessagePayload,
    OutboundRequest,
    TextPart,
)
from cogito.bus.inbound import InboundPort

logger = logging.getLogger(__name__)


CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cogito Chat</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,"Segoe UI",sans-serif;background:#f0f2f5;height:100vh;display:flex;justify-content:center;align-items:center}
  #app{width:900px;height:90vh;background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.08);display:flex;overflow:hidden}

  /* Sidebar */
  #sidebar{width:200px;background:#fafafa;border-right:1px solid #e8e8e8;display:flex;flex-direction:column;flex-shrink:0}
  #sidebar-header{padding:14px 12px;border-bottom:1px solid #e8e8e8;font-weight:600;font-size:14px;color:#1a1a1a;display:flex;justify-content:space-between;align-items:center}
  #new-session-btn{background:none;border:1px solid #d9d9d9;border-radius:6px;cursor:pointer;font-size:18px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;color:#666;transition:.15s}
  #new-session-btn:hover{background:#1677ff;color:#fff;border-color:#1677ff}
  #session-list{flex:1;overflow-y:auto;padding:6px}
  .session-item{padding:8px 10px;border-radius:6px;cursor:pointer;font-size:13px;color:#333;transition:.1s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px}
  .session-item:hover{background:#e8e8e8}
  .session-item.active{background:#1677ff;color:#fff}

  /* Main */
  #main{flex:1;display:flex;flex-direction:column;min-width:0}
  #chat-header{padding:14px 20px;border-bottom:1px solid #e8e8e8;font-weight:600;font-size:14px;color:#1a1a1a;display:flex;align-items:center;gap:8px}
  #chat-header .sid{font-weight:400;font-size:12px;color:#999}
  #messages{flex:1;overflow-y:auto;padding:16px 20px}
  .msg{margin-bottom:14px;display:flex}
  .msg.user{justify-content:flex-end}
  .msg.assistant{justify-content:flex-start}
  .bubble{max-width:72%;padding:10px 16px;border-radius:14px;line-height:1.5;font-size:14px;white-space:pre-wrap;word-break:break-word}
  .msg.user .bubble{background:#1677ff;color:#fff;border-bottom-right-radius:4px}
  .msg.assistant .bubble{background:#f0f2f5;color:#1a1a1a;border-bottom-left-radius:4px}
  .msg.system .bubble{background:#fffbe6;color:#ad8b00;border:1px solid #ffe58f;font-size:13px}
  .msg.error .bubble{background:#fff2f0;color:#ff4d4f;border:1px solid #ffccc7}
  .typing-dots::after{content:'...';animation:dots 1.2s steps(3,end) infinite}
  @keyframes dots{0%{content:'.'}33%{content:'..'}66%{content:'...'}}
  #input-area{padding:12px 20px;border-top:1px solid #e8e8e8;display:flex;gap:10px}
  #input{flex:1;padding:10px 14px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;outline:none;resize:none;font-family:inherit;max-height:120px}
  #input:focus{border-color:#1677ff}
  #send-btn{padding:10px 22px;background:#1677ff;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-weight:500;white-space:nowrap}
  #send-btn:disabled{opacity:.5;cursor:not-allowed}
</style>
</head>
<body>
<div id="app">
  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-header">
      <span>会话</span>
      <button id="new-session-btn" title="新建会话" onclick="newSession()">+</button>
    </div>
    <div id="session-list"></div>
  </div>
  <!-- Main -->
  <div id="main">
    <div id="chat-header">
      <span id="session-label">当前会话</span>
      <span class="sid" id="session-id-display"></span>
    </div>
    <div id="messages"></div>
    <div id="input-area">
      <textarea id="input" rows="1" placeholder="输入消息..."
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
      <button id="send-btn" onclick="send()">发送</button>
    </div>
  </div>
</div>
<script>
// ── State ──────────────────────────────────────────────────────────
let sessions = {};        // {sid: {id, title, messages:[]}}
let currentSid = null;
let sending = false;

const $msg=document.getElementById('messages');
const $inp=document.getElementById('input');
const $btn=document.getElementById('send-btn');
const $slist=document.getElementById('session-list');
const $slabel=document.getElementById('session-label');
const $sdisplay=document.getElementById('session-id-display');

// ── Helpers ────────────────────────────────────────────────────────
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}

function sidShort(sid){return sid ? sid.slice(0,8)+'...' : ''}

// ── Messages ───────────────────────────────────────────────────────
function addMsg(role,text,cls){
  const d=document.createElement('div');
  d.className='msg '+role+(cls?' '+cls:'');
  d.innerHTML='<div class="bubble">'+esc(text)+'</div>';
  $msg.appendChild(d);$msg.scrollTop=$msg.scrollHeight;
}

function clearMessages(){
  while($msg.firstChild)$msg.removeChild($msg.firstChild);
}

function appendHistory(messages){
  for(const m of messages){
    addMsg(m.role, m.content);
  }
}

// ── Session list ───────────────────────────────────────────────────
function renderSessionList(){
  $slist.innerHTML='';
  for(const [sid, s] of Object.entries(sessions)){
    const div=document.createElement('div');
    div.className='session-item'+(sid===currentSid?' active':'');
    div.textContent=s.title || sidShort(sid);
    div.title=sid;
    div.onclick=()=>switchSession(sid);
    $slist.appendChild(div);
  }
}

function switchSession(sid){
  if(sid===currentSid)return;
  currentSid=sid;
  clearMessages();
  const s=sessions[sid];
  if(s && s.messages && s.messages.length>0){
    appendHistory(s.messages);
  }else{
    // Try to load history from server
    loadHistory(sid);
  }
  $slabel.textContent=s ? (s.title || '会话') : '会话';
  $sdisplay.textContent=sidShort(sid);
  renderSessionList();
  localStorage.setItem('cogito_current_sid', sid);
  $inp.focus();
}

async function loadHistory(sid){
  try{
    const r=await fetch('/api/session_history?session_id='+encodeURIComponent(sid));
    const d=await r.json();
    const s=sessions[sid];
    if(!s) return;
    if(d.messages && d.messages.length>0){
      s.messages=d.messages;
      clearMessages();
      appendHistory(s.messages);
      // Update title from first user message
      const first=d.messages.find(m=>m.role==='user');
      if(first) s.title=first.content.slice(0,20)+(first.content.length>20?'...':'');
      $slabel.textContent=s.title;
      renderSessionList();
    }else{
      addMsg('assistant','你好！有什么可以帮你的？');
    }
  }catch(e){
    // Silently show welcome message on error
    addMsg('assistant','你好！有什么可以帮你的？');
  }
}

// ── API calls ──────────────────────────────────────────────────────
async function newSession(){
  try{
    const r=await fetch('/api/new_session',{method:'POST'});
    const d=await r.json();
    const sid=d.session_id;
    sessions[sid]={id:sid, title:'新会话 '+sidShort(sid), messages:[]};
    renderSessionList();
    switchSession(sid);
  }catch(e){
    addMsg('system','创建会话失败: '+e.message,'error');
  }
}

async function send(){
  if(sending)return;
  const text=$inp.value.trim();if(!text)return;
  if(!currentSid){addMsg('system','请先创建或选择一个会话','error');return;}

  $inp.value='';$btn.disabled=true;sending=true;
  addMsg('user',text);
  addMsg('assistant','','');

  const typingEl=$msg.lastElementChild;
  try{
    const r=await fetch('/api/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text, session_id:currentSid})
    });
    const d=await r.json();
    typingEl.remove();

    if(d.error){
      addMsg('assistant',d.error,'error');
      // Still save user message to local history
    }else{
      addMsg('assistant',d.response);
    }
    // Update local message history
    const s=sessions[currentSid];
    if(s){
      s.messages.push({role:'user',content:text});
      if(d.response) s.messages.push({role:'assistant',content:d.response});
      // Update title from first user message
      if(s.title===('新会话 '+sidShort(currentSid))){
        s.title=text.slice(0,20)+(text.length>20?'...':'');
        renderSessionList();
      }
    }
  }catch(e){
    typingEl.remove();
    addMsg('assistant','网络错误: '+e.message,'error');
  }
  $btn.disabled=false;sending=false;$inp.focus();
}

// ── Init ───────────────────────────────────────────────────────────
(async function init(){
  // Load sessions from server
  try{
    const r=await fetch('/api/sessions');
    const d=await r.json();
    for(const sids of d.sessions){
      sessions[sids]={id:sids, title:sids.slice(0,8)+'...', messages:[]};
    }
  }catch(e){/* ignore */}

  // Create first session if none
  if(Object.keys(sessions).length===0){
    await newSession();
  }else{
    // Restore last active session
    const lastSid=localStorage.getItem('cogito_current_sid');
    if(lastSid && sessions[lastSid]){
      // Load history for this session
      try{
        const r=await fetch('/api/session_history?session_id='+encodeURIComponent(lastSid));
        const d=await r.json();
        const s=sessions[lastSid];
        s.messages=d.messages || [];
        if(s.messages.length>0){
          const first=s.messages.find(m=>m.role==='user');
          if(first) s.title=first.content.slice(0,20)+(first.content.length>20?'...':'');
        }
        renderSessionList();
        switchSession(lastSid);
      }catch(e){
        switchSession(lastSid);
      }
    }else{
      switchSession(Object.keys(sessions)[0]);
    }
  }
})();
</script>
</body>
</html>"""


def _http(status: int, body: dict | str, *, ctype: str = "application/json") -> bytes:
    data: bytes
    if isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    reason = {
        200: "OK", 201: "Created", 400: "Bad Request",
        404: "Not Found", 500: "Internal Server Error", 504: "Gateway Timeout",
    }.get(status, "Unknown")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {ctype}; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\n"
        f"Connection: close\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"\r\n"
    ).encode() + data


async def _read_http(reader: asyncio.StreamReader, timeout: float = 30):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line:
            return None
        parts = line.decode().strip().split(" ")
        if len(parts) < 2:
            logger.warning("Bad request line: %r", line)
            return None
        method, path = parts[0], parts[1]

        headers = {}
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            s = h.decode().strip()
            if ":" in s:
                k, v = s.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        cl = int(headers.get("content-length", "0"))
        body = b""
        if cl > 0:
            body = await asyncio.wait_for(reader.readexactly(cl), timeout=timeout)

        logger.debug("HTTP %s %s (%d bytes body)", method, path, len(body))
        return method, path, headers, body
    except asyncio.TimeoutError:
        logger.warning("HTTP read timeout")
    except Exception as exc:
        logger.warning("HTTP parse error: %s", exc)
    return None


class AsyncWebServer:
    """纯 asyncio HTTP 服务器，支持多 session 管理。"""

    def __init__(
        self,
        inbound_port: InboundPort,
        host: str = "0.0.0.0",
        port: int = 8888,
        db_manager: object | None = None,
    ) -> None:
        self._inbound = inbound_port
        self._host = host
        self._port = port
        self._db = getattr(db_manager, "db", None) if db_manager else None
        self._pending: dict[str, tuple[asyncio.Event, str]] = {}
        self._server: asyncio.Server | None = None
        # In-memory session store
        self._sessions: dict[str, dict] = {}

    async def run(self) -> None:
        # 从数据库加载已有 sessions
        await self._load_sessions_from_db()

        self._server = await asyncio.start_server(
            self._on_connect, host=self._host, port=self._port,
        )
        addr = (
            self._server.sockets[0].getsockname()
            if self._server.sockets else (self._host, self._port)
        )
        logger.info("Web 服务器已启动: http://%s:%s (sessions=%d)", addr[0], addr[1], len(self._sessions))
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ── 数据库操作 ────────────────────────────────────────────────────

    async def _load_sessions_from_db(self) -> None:
        """从数据库加载已有 sessions 到内存。"""
        if not self._db or not self._db.is_connected:
            logger.debug("No database available, skipping session load")
            return
        try:
            rows = await self._db.fetchall(
                "SELECT session_id, created_at FROM sessions ORDER BY created_at ASC",
            )
            for row in rows:
                sid = row.get("session_id", "")
                if sid:
                    self._sessions[sid] = {
                        "id": sid,
                        "title": row.get("title", ""),
                        "created_at": row.get("created_at", ""),
                    }
            if rows:
                logger.info("Loaded %d sessions from database", len(rows))
        except Exception:
            logger.exception("Failed to load sessions from DB")

    # ── 被 DeliveryManager 调用 ────────────────────────────────────

    def send_response(self, trace_id: str, text: str) -> None:
        entry = self._pending.get(trace_id)
        if entry:
            self._pending[trace_id] = (entry[0], text)
            entry[0].set()
        else:
            logger.warning("No pending request for trace=%s", trace_id)

    # ── TCP 连接处理 ───────────────────────────────────────────────

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        resp: bytes | None = None
        try:
            req = await _read_http(reader)
            if req is None:
                resp = _http(400, {"error": "bad request"})
                return

            method, path, headers, body = req

            if method == "GET" and path == "/":
                resp = _http(200, CHAT_HTML, ctype="text/html")

            elif method == "POST" and path == "/api/chat":
                resp = await self._handle_chat(body)

            elif method == "POST" and path == "/api/new_session":
                resp = self._handle_new_session()

            elif method == "GET" and path == "/api/sessions":
                resp = await self._handle_list_sessions()

            elif method == "GET" and path.startswith("/api/session_history"):
                resp = await self._handle_session_history(body, path)

            elif method == "GET" and path == "/health":
                resp = _http(200, {"status": "ok"})

            else:
                resp = _http(404, {"error": "not found"})

        except Exception as exc:
            logger.exception("Unhandled HTTP handler error")
            resp = _http(500, {"error": str(exc)[:200]})

        finally:
            if resp is None:
                resp = _http(500, {
                    "error": "no response generated - this is a bug",
                })
            try:
                writer.write(resp)
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass

    # ── API: New session ───────────────────────────────────────────

    def _handle_new_session(self) -> bytes:
        session_id = f"web-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        self._sessions[session_id] = {
            "id": session_id,
            "title": "",
            "created_at": now,
        }

        # 持久化到数据库
        if self._db and self._db.is_connected:
            try:
                asyncio.ensure_future(
                    self._db.execute(
                        "INSERT INTO sessions (session_id, user_id) VALUES (:sid, :uid)",
                        {"sid": session_id, "uid": "web:default"},
                    ),
                )
            except Exception:
                logger.exception("Failed to persist session to DB")

        logger.info("New session created: %s", session_id)
        return _http(201, {"session_id": session_id})

    # ── API: List sessions ─────────────────────────────────────────

    async def _handle_list_sessions(self) -> bytes:
        """List sessions — load from sessions table + in-memory fallback.

        Returns all sessions that exist in the sessions table,
        plus any in-memory-only sessions (e.g. just created).
        """
        session_ids: set[str] = set(self._sessions.keys())

        # Load sessions that have persisted entries in the DB sessions table
        if self._db and self._db.is_connected:
            try:
                rows = await self._db.fetchall(
                    "SELECT session_id, created_at FROM sessions ORDER BY created_at ASC",
                )
                for row in rows:
                    sid = row.get("session_id", "")
                    if sid:
                        session_ids.add(sid)
                        # Ensure in-memory also has this entry
                        if sid not in self._sessions:
                            self._sessions[sid] = {
                                "id": sid,
                                "title": "",
                                "created_at": row.get("created_at", ""),
                            }
            except Exception:
                logger.exception("Failed to load session list from DB")

        return _http(200, {"sessions": sorted(session_ids)})

    # ── API: Session history (from DB) ─────────────────────────────

    async def _handle_session_history(
        self, body: bytes, path: str,
    ) -> bytes:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse("http://host" + path)
        params = parse_qs(parsed.query)
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            return _http(400, {"error": "session_id required"})

        # Query events from database to reconstruct conversation history
        messages: list[dict[str, str]] = []
        if self._db and self._db.is_connected:
            try:
                rows = await self._db.fetchall(
                    "SELECT role, content, created_at "
                    "FROM events "
                    "WHERE session_id = :session_id "
                    "ORDER BY seq_no ASC",
                    {"session_id": session_id},
                )
                for row in rows:
                    role = row.get("role", "")
                    content = row.get("content", "")
                    if role in ("user", "assistant") and content:
                        messages.append({"role": role, "content": content})
            except Exception:
                logger.exception("Failed to load session history")
                messages = []

        return _http(200, {"session_id": session_id, "messages": messages})

    # ── API: Chat ──────────────────────────────────────────────────

    async def _handle_chat(self, body: bytes) -> bytes:
        try:
            data = json.loads(body)
            text = data.get("message", "").strip()
            session_id = data.get("session_id", "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _http(400, {"error": "invalid JSON"})

        if not text:
            return _http(400, {"error": "message is empty"})

        if not session_id:
            return _http(400, {"error": "session_id is required"})

        # Ensure session exists
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "id": session_id,
                "title": "",
                "created_at": datetime.now().isoformat(),
            }

        # 确保 DB 中 session 的 user_id 与 TurnRunner 生成的 actor_id 一致
        if self._db and self._db.is_connected:
            try:
                async def _ensure_session():
                    # 尝试插入（session 不存在时）
                    row = await self._db.fetchone(
                        "SELECT session_id, user_id FROM sessions WHERE session_id = :sid",
                        {"sid": session_id},
                    )
                    if row is None:
                        await self._db.execute(
                            "INSERT INTO sessions (session_id, user_id) VALUES (:sid, :uid)",
                            {"sid": session_id, "uid": "web:default"},
                        )
                    elif row.get("user_id") != "web:default":
                        # 修复旧 session 的 user_id
                        await self._db.execute(
                            "UPDATE sessions SET user_id = :uid WHERE session_id = :sid",
                            {"sid": session_id, "uid": "web:default"},
                        )
                asyncio.ensure_future(_ensure_session())
            except Exception:
                pass

        trace_id = uuid4().hex
        event = asyncio.Event()
        self._pending[trace_id] = (event, "")

        # Build InboundMessage with proper session_key
        im = InboundMessage(
            message_id=uuid4().hex,
            external_message_id=None,
            session_key=session_id,
            channel="web",
            target="default",
            payload=MessagePayload(
                parts=[TextPart(text=text)],
            ),
            trace_id=trace_id,
            received_at=datetime.now(),
        )

        # 投递到 InboundBus
        try:
            await self._inbound.publish(im)
            logger.debug("Published to bus, sid=%s, trace=%s", session_id, trace_id)
        except Exception as exc:
            self._pending.pop(trace_id, None)
            return _http(500, {"error": f"submit failed: {exc}"})

        # Wait for agent response (with timeout)
        try:
            await asyncio.wait_for(event.wait(), timeout=180)
        except asyncio.TimeoutError:
            self._pending.pop(trace_id, None)
            logger.warning("Chat timeout trace=%s", trace_id)
            return _http(504, {"error": "agent timeout (180s)"})

        entry = self._pending.pop(trace_id, None)
        response_text = entry[1] if entry else ""
        return _http(200, {
            "response": response_text,
            "session_id": session_id,
        })


class WebChannel:
    """HTTP Web Channel — 纯 asyncio 实现，无额外依赖。

    Registers as ``"web"`` in ``ChannelRegistry``.
    The ``DeliveryManager`` calls ``send()`` to deliver agent responses
    back to the waiting HTTP request.
    """

    name = "web"

    def __init__(self, host: str = "0.0.0.0", port: int = 8888, db_manager: object | None = None) -> None:
        self._host = host
        self._port = port
        self._db_manager = db_manager
        self._server: AsyncWebServer | None = None

    async def run(self, inbound: InboundPort) -> None:
        self._server = AsyncWebServer(
            inbound_port=inbound,
            host=self._host,
            port=self._port,
            db_manager=self._db_manager,
        )
        await self._server.run()

    async def send(self, request: OutboundRequest) -> DeliveryReceipt:
        trace_id = request.trace_id
        text_parts = [
            p.text for p in request.payload.parts
            if isinstance(p, TextPart)
        ]
        text = "\n".join(text_parts)
        if self._server is not None:
            self._server.send_response(trace_id, text)
        return DeliveryReceipt(
            outbound_id=request.outbound_id,
            status="delivered",
        )

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()
