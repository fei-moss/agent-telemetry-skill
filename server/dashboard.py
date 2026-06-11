"""Simple telemetry dashboard — visualize reporting agents' activity.

Reads the ingest JSONL files written by ``ingest_server.py`` / ``minimal_ingest.py``
(``{received_at_unix_ms, payload: {resourceSpans...}}`` per line), groups spans
into sessions, and serves a small web UI that renders each session as a
human-readable timeline of reasoning / progress / tool calls / model calls.

Stdlib only. Optional HTTP Basic auth via env (recommended on public hosts).

Usage:
    python3 server/dashboard.py --ingest-dir /var/lib/agent-telemetry-ingest --port 8080
    DASHBOARD_USER=admin DASHBOARD_PASS=secret python3 server/dashboard.py ...
"""

from __future__ import annotations

import argparse
import base64
import glob
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# ---- data layer ------------------------------------------------------------

def _val(value: dict) -> object:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [_val(v) for v in value["arrayValue"].get("values", [])]
    return None


def _attrs(items: list) -> dict:
    return {item.get("key"): _val(item.get("value", {})) for item in items or []}


class Store:
    """Parses ingest JSONL into sessions, caching per-file by size."""

    def __init__(self, ingest_dir: str):
        self.ingest_dir = ingest_dir
        self._cache: dict[str, tuple[int, list]] = {}  # path -> (size, spans)

    def _files(self) -> list[str]:
        return sorted(glob.glob(os.path.join(self.ingest_dir, "*.jsonl")))

    def _spans_for_file(self, path: str) -> list:
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        cached = self._cache.get(path)
        if cached is not None and cached[0] == size:
            return cached[1]
        spans: list = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    recv = rec.get("received_at_unix_ms", 0)
                    payload = rec.get("payload", {})
                    for rs in payload.get("resourceSpans", []):
                        res = _attrs(rs.get("resource", {}).get("attributes", []))
                        for ss in rs.get("scopeSpans", []):
                            for sp in ss.get("spans", []):
                                spans.append(self._span_item(sp, res, recv))
        except OSError:
            return []
        self._cache[path] = (size, spans)
        return spans

    def _span_item(self, sp: dict, res: dict, recv: int) -> dict:
        a = _attrs(sp.get("attributes", []))
        name = sp.get("name", "")
        kind = name.split()[0] if name else ""
        text = ""
        facts: list[str] = []  # readable key=value lines from events + attrs
        for ev in sp.get("events", []):
            ea = _attrs(ev.get("attributes", []))
            primary = ea.get("text") or ea.get("result") or ea.get("decision.rationale")
            if isinstance(primary, str) and primary.strip():
                text = primary
            for key, value in ea.items():
                if key in ("text", "result"):
                    continue
                if value not in (None, "", []):
                    facts.append(f"{key.split('.')[-1]}={value}")
        # decision/cycle attributes carried directly on the span
        for key in ("decision.name", "decision.confidence"):
            if a.get(key) not in (None, ""):
                facts.append(f"{key.split('.')[-1]}={a[key]}")
        try:
            start = int(sp.get("startTimeUnixNano", 0))
            end = int(sp.get("endTimeUnixNano", 0) or start)
        except (TypeError, ValueError):
            start = end = 0
        try:
            seq = int(a.get("narrative.sequence"))
        except (TypeError, ValueError):
            seq = None
        return {
            "trace": sp.get("traceId", ""),
            "span": sp.get("spanId", ""),
            "name": name,
            "kind": kind,
            "session": a.get("session.id") or "",
            "service": res.get("service.name") or a.get("service.name") or "unknown",
            "tenant": a.get("tenant.id") or "",
            "layer": a.get("telemetry.collection_layer") or "",
            "tool": a.get("gen_ai.tool.name") or "",
            "model": a.get("gen_ai.request.model") or "",
            "in_tok": a.get("gen_ai.usage.input_tokens"),
            "out_tok": a.get("gen_ai.usage.output_tokens"),
            "text": text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
            "facts": facts[:8],
            "start": start,
            "end": end,
            "seq": seq,
            "recv": recv,
            "ms": max(0, (end - start) // 1_000_000),
        }

    def all_spans(self) -> list:
        spans: list = []
        for path in self._files():
            spans.extend(self._spans_for_file(path))
        return spans

    def sessions(self) -> list:
        groups: dict[str, dict] = {}
        for s in self.all_spans():
            sid = s["session"] or s["trace"] or "(none)"
            g = groups.get(sid)
            if g is None:
                g = groups[sid] = {
                    "session": sid, "service": s["service"], "tenant": s["tenant"],
                    "layers": set(), "spans": 0, "last": 0, "tools": 0, "thinks": 0,
                    "preview": "",
                }
            g["spans"] += 1
            g["layers"].add(s["layer"])
            g["last"] = max(g["last"], s["recv"], s["start"] // 1_000_000)
            if s["kind"] == "execute_tool":
                g["tools"] += 1
            if s["name"] == "reasoning":
                g["thinks"] += 1
            if s["service"] != "unknown":
                g["service"] = s["service"]
            if not g["preview"] and s["kind"] in ("reasoning", "message", "execute_tool"):
                snippet = s["text"] or (s["facts"][0] if s["facts"] else "")
                if snippet:
                    g["preview"] = snippet[:80]
        # fall back to any fact-bearing span (e.g. sdk cron) when no narrative
        for s in self.all_spans():
            sid = s["session"] or s["trace"] or "(none)"
            g = groups.get(sid)
            if g is not None and not g["preview"]:
                snippet = s["text"] or (s["facts"][0] if s["facts"] else "")
                if snippet:
                    g["preview"] = snippet[:80]
        out = []
        for g in groups.values():
            g["layers"] = sorted(x for x in g["layers"] if x)
            g["rich"] = g["thinks"] + g["tools"]
            out.append(g)
        # rich sessions (with thinking/tools) first, then by recency
        out.sort(key=lambda g: (g["rich"] > 0, g["last"]), reverse=True)
        return out

    def session_timeline(self, session_id: str) -> list:
        items = [s for s in self.all_spans() if (s["session"] or s["trace"]) == session_id]
        items.sort(key=lambda s: (s["start"], s["seq"] if s["seq"] is not None else 0))
        return items

    def stats(self) -> dict:
        spans = self.all_spans()
        return {
            "spans": len(spans),
            "sessions": len({s["session"] or s["trace"] for s in spans}),
            "services": sorted({s["service"] for s in spans if s["service"]}),
        }


# ---- web layer -------------------------------------------------------------

PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Agent Telemetry Dashboard</title><style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,'PingFang SC',sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:16px;padding:12px 18px;border-bottom:1px solid var(--border);background:var(--panel)}
header h1{font-size:16px;margin:0}header .stat{color:var(--mut);font-size:13px}header .dot{color:#3fb950}
.wrap{display:flex;height:calc(100vh - 50px)}
.list{width:340px;border-right:1px solid var(--border);overflow:auto;flex:none}
.detail{flex:1;overflow:auto;padding:18px}
.sess{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer}
.sess:hover{background:#1c2230}.sess.active{background:#1f2937;border-left:3px solid var(--acc)}
.sess .svc{font-weight:600}.sess .meta{color:var(--mut);font-size:12px;margin-top:3px}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;background:#21262d;color:var(--mut);margin-right:4px}
.tag.hook,.tag.plugin{color:#3fb950}.tag.log_watch{color:#58a6ff}.tag.model_reported,.tag.sdk{color:#d29922}
.item{border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px;background:var(--panel)}
.item .h{display:flex;gap:8px;align-items:center;margin-bottom:4px}
.item .ic{font-weight:700}.item.reasoning .ic{color:#d29922}.item.message .ic{color:#58a6ff}
.item.execute_tool .ic{color:#3fb950}.item.chat .ic{color:#bc8cff}.item.agent .ic{color:#e6edf3}
.item .sub{color:var(--mut);font-size:12px}
.item .body{white-space:pre-wrap;word-break:break-word;margin-top:4px}
.item .body.tool{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#9fb6cf;max-height:220px;overflow:auto}
.empty{color:var(--mut);padding:40px;text-align:center}
.filter{padding:8px 14px;border-bottom:1px solid var(--border)}
.filter input{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--fg);padding:6px 8px;border-radius:6px}
</style></head><body>
<header><h1>&#9877; Agent Telemetry</h1><span class=stat id=stat>loading&#8230;</span>
<span class=stat style=margin-left:auto><span class=dot>&#9679;</span> auto 5s</span></header>
<div class=wrap>
 <div class=list><div class=filter><input id=q placeholder="filter service / session...">
   <label style="display:block;margin-top:6px;color:#8b949e;font-size:12px;cursor:pointer">
   <input type=checkbox id=richonly> 只看有内容(思考/工具)</label></div><div id=sessions></div></div>
 <div class=detail id=detail><div class=empty>&#8592; select a session</div></div>
</div>
<script>
let cur=null, sessions=[], filter='', richonly=false;
const ICON={reasoning:'\\u{1F9E0} think',message:'\\u{1F4AC} progress',execute_tool:'\\u{1F527} tool',chat:'\\u2699 model','agent.run':'\\u25B6 run'};
function fmtTime(ms){if(!ms)return'';const d=new Date(ms);return d.toLocaleTimeString('zh-CN',{hour12:false})}
async function loadStat(){const s=await (await fetch('api/stats')).json();
 document.getElementById('stat').textContent=`${s.sessions} sessions \\u00b7 ${s.spans} spans \\u00b7 ${s.services.join(', ')}`}
async function loadSessions(){sessions=await (await fetch('api/sessions')).json();render()}
function render(){const box=document.getElementById('sessions');const f=filter.toLowerCase();
 box.innerHTML=sessions.filter(s=>(!f||(s.service+s.session).toLowerCase().includes(f))&&(!richonly||s.rich>0)).map(s=>
  `<div class="sess${s.session===cur?' active':''}" onclick="open_('${s.session.replace(/'/g,"")}')">
   <div class=svc>${esc(s.service)}</div>
   <div class=meta>${esc(s.session.slice(0,28))}</div>
   <div class=meta>${s.spans} spans \\u00b7 ${s.thinks} think \\u00b7 ${s.tools} tool \\u00b7 ${fmtTime(s.last)}</div>
   ${s.preview?`<div class=meta style="color:#c9d1d9;margin-top:4px">${esc(s.preview)}</div>`:''}
   <div style=margin-top:4px>${s.layers.map(l=>`<span class="tag ${l}">${l}</span>`).join('')}</div></div>`).join('')
 ||'<div class=empty>no data</div>'}
async function open_(id){cur=id;render();const items=await (await fetch('api/session?id='+encodeURIComponent(id))).json();
 const d=document.getElementById('detail');
 if(!items.length){d.innerHTML='<div class=empty>empty</div>';return}
 d.innerHTML=items.map(it=>{
  const ic=ICON[it.kind]||ICON[it.name]||it.name;
  let sub=[];if(it.tool)sub.push(it.tool);if(it.model)sub.push(it.model);
  if(it.in_tok||it.out_tok)sub.push(`tok ${it.in_tok||0}/${it.out_tok||0}`);
  if(it.ms)sub.push(it.ms+'ms');if(it.layer)sub.push(it.layer);
  let body=it.text?`<div class="body ${it.kind==='execute_tool'?'tool':''}">${esc(it.text)}</div>`:'';
  if(!it.text&&it.facts&&it.facts.length)body=`<div class=body style=color:#9fb6cf>${it.facts.map(esc).join(' · ')}</div>`;
  return `<div class="item ${it.kind}"><div class=h><span class=ic>${ic}</span>
   <span class=sub>${esc(sub.join(' \\u00b7 '))}</span></div>${body}</div>`}).join('')}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
document.getElementById('q').addEventListener('input',e=>{filter=e.target.value;render()});
document.getElementById('richonly').addEventListener('change',e=>{richonly=e.target.checked;render()});
async function tick(){await loadStat();await loadSessions();if(cur)open_(cur)}
tick();setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    store: Store
    auth: tuple[str, str] | None = None

    def _check_auth(self) -> bool:
        if not self.auth:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                user = pw = ""
            if hmac.compare_digest(user, self.auth[0]) and hmac.compare_digest(pw, self.auth[1]):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="telemetry"')
        self.end_headers()
        return False

    def _json(self, obj: object) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/stats":
                self._json(self.store.stats())
            elif path == "/api/sessions":
                self._json(self.store.sessions())
            elif path == "/api/session":
                sid = parse_qs(parsed.query).get("id", [""])[0]
                self._json(self.store.session_timeline(sid))
            elif path in ("/healthz", "/health"):
                self._json({"status": "ok"})
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent telemetry dashboard.")
    parser.add_argument("--ingest-dir", default=os.environ.get("INGEST_OUTPUT_DIR", "ingest-data"))
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8080")))
    args = parser.parse_args(argv)

    Handler.store = Store(args.ingest_dir)
    user = os.environ.get("DASHBOARD_USER")
    pw = os.environ.get("DASHBOARD_PASS")
    Handler.auth = (user, pw) if user and pw else None

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    auth_note = "Basic auth ON" if Handler.auth else "NO AUTH (set DASHBOARD_USER/PASS)"
    print(
        f"dashboard on http://{args.host}:{args.port}  | ingest: {args.ingest_dir} | {auth_note}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
