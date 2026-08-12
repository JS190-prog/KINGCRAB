from __future__ import annotations

import hmac
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .protocol import DaemonClient


PAIR_FILE = "mobile-pair.json"


def _pair_path(workspace: Path) -> Path:
    return workspace.resolve() / ".crabagent" / PAIR_FILE


def ensure_pairing(workspace: Path) -> Dict[str, Any]:
    path = _pair_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        value = {}
    token = str(value.get("token") or "").strip() if isinstance(value, dict) else ""
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
        value = {"schema": "crab.mobile-pair/v1", "token": token}
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"path": str(path), "token": token, "schema": "crab.mobile-pair/v1"}


def local_network_url(host: str, port: int) -> str:
    if host not in {"0.0.0.0", "::", ""}:
        return "http://%s:%d" % (host, port)
    try:
        address = socket.gethostbyname(socket.gethostname())
    except OSError:
        address = "127.0.0.1"
    if address.startswith("127."):
        address = "<this-mac-ip>"
    return "http://%s:%d" % (address, port)


def _mobile_messages(messages: Any) -> list[Dict[str, Any]]:
    visible: list[Dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "system")
        metadata = message.get("metadata") or {}
        internal_role = str(metadata.get("role") or "").upper()
        if role == "assistant" and internal_role in {"KING", "QUEEN", "WORKER", "SOLDIER", "ORACLE"}:
            if not metadata.get("user_facing") and not metadata.get("goal_outcome"):
                continue
        label = "YOU" if role == "user" else (internal_role or "CRAB" if role == "assistant" else "SYSTEM")
        visible.append(
            {
                "message_id": message.get("message_id"),
                "role": role,
                "label": label,
                "content": str(message.get("content") or ""),
                "created_at": message.get("created_at"),
            }
        )
    return visible


class MobileGateway:
    """Authenticated local-LAN control plane over the existing crabd socket."""

    def __init__(self, workspace: Path, client: Optional[DaemonClient] = None, token: str = "") -> None:
        self.workspace = workspace.resolve()
        pair = ensure_pairing(self.workspace)
        self.token = token.strip() or str(pair["token"])
        self.client = client or DaemonClient(self.workspace, timeout=12.0)
        self.server: Optional[ThreadingHTTPServer] = None

    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        return self.client.request(action, **payload)

    def snapshot(self, session_id: str = "", after_message_id: int = 0) -> Dict[str, Any]:
        session = self.request("session.ensure", session_id=session_id) if not session_id else None
        selected = str((session or {}).get("session_id") or session_id)
        raw = self.request("ui.snapshot", session_id=selected, after_message_id=max(0, int(after_message_id)))
        result = dict(raw)
        result["messages"] = _mobile_messages(raw.get("messages"))
        result["pending_requests"] = [
            {
                "request_id": row.get("request_id"),
                "method": row.get("method"),
                "prompt": row.get("prompt") or row.get("method"),
            }
            for row in raw.get("pending_requests") or []
            if isinstance(row, dict)
        ]
        return result

    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        gateway = self

        class BoundServer(ThreadingHTTPServer):
            daemon_threads = True

            def __init__(self, address: Tuple[str, int]) -> None:
                self.gateway = gateway
                super().__init__(address, _MobileRequestHandler)

        self.server = BoundServer((host, port))
        return self.server

    def serve_forever(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        server = self.make_server(host, port)
        actual_port = int(server.server_address[1])
        print("KINGCRAB mobile gateway: %s" % local_network_url(host, actual_port), flush=True)
        print("Pair token: %s" % self.token, flush=True)
        if host in {"0.0.0.0", "::", ""}:
            print("LAN mode: bearer token required; keep this URL on a trusted network.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()


class _MobileRequestHandler(BaseHTTPRequestHandler):
    server_version = "KINGCRABMobile/0.1"

    @property
    def gateway(self) -> MobileGateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Crab-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else (
            payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(status)
        self._headers(content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = str(self.headers.get("Authorization") or "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else str(self.headers.get("X-Crab-Token") or "")
        return bool(supplied) and hmac.compare_digest(supplied, self.gateway.token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send(401, {"error": "mobile pairing token required"})
        return False

    def _json_body(self) -> Dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1024 * 1024)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, MOBILE_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            try:
                ping = self.gateway.request("ping")
                self._send(200, {"status": "online", "runtime": ping})
            except Exception as exc:
                self._send(503, {"status": "offline", "error": str(exc)})
            return
        if not self._require_auth():
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/v1/snapshot":
            try:
                snapshot = self.gateway.snapshot(
                    str((query.get("session_id") or [""])[0]),
                    int((query.get("after_message_id") or ["0"])[0]),
                )
                self._send(200, snapshot)
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return
        if parsed.path == "/v1/orchestrations":
            try:
                self._send(200, self.gateway.request("orchestration.status"))
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        try:
            body = self._json_body()
            if parsed.path == "/v1/prompt":
                session_id = str(body.get("session_id") or "")
                if not session_id:
                    session_id = str(self.gateway.request("session.ensure")["session_id"])
                objective = str(body.get("objective") or "").strip()
                if not objective:
                    raise ValueError("objective is empty")
                disposition = str(body.get("disposition") or "start")
                if disposition not in {"start", "now", "wait"}:
                    raise ValueError("disposition must be start, now, or wait")
                result = self.gateway.request(
                    "prompt.submit",
                    session_id=session_id,
                    objective=objective,
                    disposition=disposition,
                    interaction=str(body.get("interaction") or ""),
                )
                self._send(202, result)
                return
            if parsed.path == "/v1/interrupt":
                self._send(200, self.gateway.request("session.interrupt", session_id=str(body.get("session_id") or "")))
                return
            if parsed.path == "/v1/approval":
                self._send(
                    200,
                    self.gateway.request(
                        "runtime.respond",
                        session_id=str(body.get("session_id") or ""),
                        request_id=str(body.get("request_id") or ""),
                        approve=bool(body.get("approve")),
                        result=dict(body.get("result") or {}),
                        reason=str(body.get("reason") or "Mobile decision"),
                    ),
                )
                return
            if parsed.path == "/v1/orchestrations/plan":
                self._send(201, self.gateway.request("orchestration.plan", **body))
                return
            if parsed.path == "/v1/orchestrations/run":
                self._send(202, self.gateway.request("orchestration.run", **body))
                return
            if parsed.path == "/v1/orchestrations/stop":
                self._send(200, self.gateway.request("orchestration.stop", **body))
                return
            self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


MOBILE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>KINGCRAB Mobile Deck</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#151b23;--line:#303a49;--muted:#91a0b5;--blue:#55aef0;--orange:#ff765f;--yellow:#eac76a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#dce5f2;font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Mono",ui-monospace,monospace}
main{max-width:760px;margin:auto;padding:16px env(safe-area-inset-right) 24px env(safe-area-inset-left)}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;border-bottom:1px solid var(--line);padding:4px 0 14px}h1{font-size:17px;letter-spacing:.12em;color:var(--orange);margin:0}.status{color:var(--muted);font-size:12px}
section{margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}.row{display:flex;gap:8px}.row>*{min-width:0;flex:1}label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;margin-bottom:4px}
input,textarea,button{width:100%;border:1px solid var(--line);border-radius:6px;background:#0f141b;color:#e7edf7;font:inherit;padding:10px}textarea{min-height:94px;resize:vertical}button{cursor:pointer;background:#202a37}button.primary{background:#146dcc;border-color:#4598ed}button.warn{background:#7c4b27;border-color:#d28851}button.stop{background:#5b2930;border-color:#d1636c}.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
#conversation{max-height:56vh;overflow:auto;display:flex;flex-direction:column;gap:10px}.message{white-space:pre-wrap;border-left:3px solid var(--line);padding-left:10px}.message.you{border-color:var(--blue)}.message.answer{border-color:var(--orange)}.message.system{border-color:var(--yellow)}.role{font-size:11px;color:var(--muted);margin-bottom:2px}.empty{color:var(--muted)}pre{white-space:pre-wrap;margin:0;color:#b9c8dd;font-size:12px}
@media(max-width:430px){main{padding:12px}.actions{grid-template-columns:1fr 1fr}.actions button:last-child{grid-column:span 2}}
</style></head><body><main>
<header><h1>KINGCRAB</h1><span id="status" class="status">offline</span></header>
<section><div class="row"><div><label>Pair token</label><input id="token" type="password" autocomplete="off"></div><div><label>Session</label><input id="session" placeholder="auto"></div></div><div class="actions"><button class="primary" onclick="connectDeck()">Connect</button><button onclick="loadSnapshot()">Refresh</button><button class="stop" onclick="interruptRun()">Stop</button></div></section>
<section><div id="conversation"><div class="empty">No conversation loaded.</div></div></section>
<section><label>Mission</label><textarea id="objective" placeholder="Tell KINGCRAB what to do"></textarea><div class="actions"><button class="primary" onclick="submitPrompt('start')">Start</button><button class="warn" onclick="submitPrompt('wait')">Queue</button><button onclick="submitPrompt('now')">Now</button></div></section>
<section><label>Orchestration</label><input id="orchestration" placeholder="orch-..."><div class="actions"><button class="primary" onclick="runOrchestration()">Run</button><button class="stop" onclick="stopOrchestration()">Stop</button><button onclick="loadOrchestrations()">Refresh</button></div><pre id="orchestration-list">No orchestration loaded.</pre></section>
<section><label>Runtime</label><pre id="runtime">Not connected</pre></section>
<script>
let bearer=sessionStorage.getItem('crab-token')||'';let sessionId=sessionStorage.getItem('crab-session')||'';let lastId=0;let timer=0;
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function headers(){return {'Authorization':'Bearer '+bearer,'Content-Type':'application/json'}}
async function api(path,options={}){let r=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})}});let v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v}
async function connectDeck(){bearer=$('token').value.trim()||bearer;if(!bearer){$('status').textContent='token required';return}sessionStorage.setItem('crab-token',bearer);try{await loadSnapshot();$('status').textContent='connected';if(!timer)timer=setInterval(loadSnapshot,1600)}catch(e){$('status').textContent=e.message}}
async function loadSnapshot(){if(!bearer)return;try{let q=new URLSearchParams();if(sessionId)q.set('session_id',sessionId);q.set('after_message_id',String(lastId));let v=await api('/v1/snapshot?'+q);sessionId=v.session.session_id;sessionStorage.setItem('crab-session',sessionId);$('session').value=sessionId;for(let m of v.messages||[]){lastId=Math.max(lastId,Number(m.message_id||0));appendMessage(m)}$('status').textContent=v.session.status||'ready';$('runtime').textContent=JSON.stringify({session:v.session.status,mission:v.mission?.mission?.status||v.mission?.status||null,oracle:v.outcome?.verdict||null,queue:(v.queue||[]).length},null,2);await loadOrchestrations()}catch(e){$('status').textContent=e.message}}
function appendMessage(m){let wrap=$('conversation');if(wrap.querySelector('.empty'))wrap.innerHTML='';let el=document.createElement('div');el.className='message '+(m.role==='user'?'you':m.role==='assistant'?'answer':'system');el.innerHTML='<div class="role">'+esc(m.label)+'</div><div>'+esc(m.content)+'</div>';wrap.appendChild(el);wrap.scrollTop=wrap.scrollHeight}
async function submitPrompt(disposition){let objective=$('objective').value.trim();if(!objective)return;try{await api('/v1/prompt',{method:'POST',body:JSON.stringify({session_id:sessionId,objective,disposition})});$('objective').value='';await loadSnapshot()}catch(e){$('status').textContent=e.message}}
async function interruptRun(){if(!sessionId)return;try{await api('/v1/interrupt',{method:'POST',body:JSON.stringify({session_id:sessionId})});await loadSnapshot()}catch(e){$('status').textContent=e.message}}
async function loadOrchestrations(){if(!bearer)return;try{let v=await api('/v1/orchestrations');$('orchestration-list').textContent=(v.summaries||[]).map(x=>x.orchestration_id+'  '+x.status+'  '+x.child_count+' child(ren)').join('\n')||'No orchestration loaded.'}catch(e){$('orchestration-list').textContent=e.message}}
async function runOrchestration(){let id=$('orchestration').value.trim();if(!id)return;try{await api('/v1/orchestrations/run',{method:'POST',body:JSON.stringify({orchestration_id:id})});await loadOrchestrations()}catch(e){$('status').textContent=e.message}}
async function stopOrchestration(){let id=$('orchestration').value.trim();if(!id)return;try{await api('/v1/orchestrations/stop',{method:'POST',body:JSON.stringify({orchestration_id:id})});await loadOrchestrations()}catch(e){$('status').textContent=e.message}}
$('token').value=bearer;$('session').value=sessionId;
</script></main></body></html>"""
