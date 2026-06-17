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
        # tool-call arguments (e.g. the command) so the timeline shows WHAT a
        # tool did, not only its output. Carried as tool.arguments.* span attrs.
        arg_parts: list[str] = []
        for key, value in a.items():
            if key.startswith("tool.arguments.") and value not in (None, "", []):
                arg_parts.append(f"{key.split('.')[-1]}={value}")
        args = " · ".join(arg_parts[:6])
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
            "args": args,
            # free-form narrative label (plan/analysis/review/…) — generic, any
            # kind a model reports is shown without per-kind dashboard code.
            "nkind": a.get("narrative.kind") or "",
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

    def agents(self) -> list:
        """Summary card per agent (service): valuable sessions, totals, recency."""
        by: dict[str, dict] = {}
        for g in self.sessions():
            svc = g["service"]
            a = by.get(svc)
            if a is None:
                a = by[svc] = {
                    "service": svc, "sessions": 0, "valuable": 0, "spans": 0,
                    "thinks": 0, "tools": 0, "msgs": 0, "last": 0,
                    "layers": set(), "preview": "",
                }
            a["sessions"] += 1
            if g["rich"] > 0:
                a["valuable"] += 1
            a["spans"] += g["spans"]
            a["thinks"] += g["thinks"]
            a["tools"] += g["tools"]
            a["msgs"] += g["msgs"]
            a["last"] = max(a["last"], g["last"])
            a["layers"].update(g["layers"])
            if not a["preview"] and g["rich"] > 0 and g["preview"]:
                a["preview"] = g["preview"]
        out = []
        for a in by.values():
            a["layers"] = sorted(x for x in a["layers"] if x)
            out.append(a)
        out.sort(key=lambda a: (a["valuable"] > 0, a["last"]), reverse=True)
        return out

    def sessions(self, service: str | None = None) -> list:
        groups: dict[str, dict] = {}
        for s in self.all_spans():
            if service is not None and s["service"] != service:
                continue
            sid = s["session"] or s["trace"] or "(none)"
            g = groups.get(sid)
            if g is None:
                g = groups[sid] = {
                    "session": sid, "service": s["service"], "tenant": s["tenant"],
                    "layers": set(), "spans": 0, "last": 0, "tools": 0, "thinks": 0,
                    "msgs": 0, "decisions": 0, "preview": "",
                }
            g["spans"] += 1
            g["layers"].add(s["layer"])
            g["last"] = max(g["last"], s["recv"], s["start"] // 1_000_000)
            if s["kind"] == "execute_tool":
                g["tools"] += 1
            if s["name"] == "reasoning":
                g["thinks"] += 1
            if s["name"] == "message":
                g["msgs"] += 1
            if s["kind"] == "decision":
                g["decisions"] += 1
            # SDK/model-reported agents carry their reasoning as text on the run
            # span (not a named "reasoning" span) — count it as a thinking item.
            if s["text"] and s["kind"] == "agent.run":
                g["thinks"] += 1
            if s["service"] != "unknown":
                g["service"] = s["service"]
            if not g["preview"] and (
                s["kind"] in ("reasoning", "message", "execute_tool", "decision", "agent.run")
            ):
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
            g["rich"] = g["thinks"] + g["tools"] + g["msgs"] + g.get("decisions", 0)
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
header h1{font-size:16px;margin:0;cursor:pointer}header .stat{color:var(--mut);font-size:13px}header .dot{color:#3fb950}
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px;padding:18px}
.card{border:1px solid var(--border);border-radius:10px;padding:16px;background:var(--panel);cursor:pointer;transition:border-color .15s}
.card:hover{border-color:var(--acc)}
.card h2{margin:0 0 10px;font-size:17px;display:flex;align-items:center;gap:8px}
.card .big{font-size:13px;color:var(--mut)}
.card .nums{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.chip{font-size:12px;padding:2px 8px;border-radius:8px;background:#21262d;color:#c9d1d9}
.chip.t{color:#d29922}.chip.m{color:#58a6ff}.chip.x{color:#3fb950}
.card .pv{color:#8b949e;font-size:12px;margin-top:6px;max-height:38px;overflow:hidden}
.crumb{padding:10px 18px;border-bottom:1px solid var(--border);background:var(--panel);color:var(--mut)}
.crumb a{color:var(--acc);cursor:pointer;text-decoration:none}
.wrap{display:flex;height:calc(100vh - 92px)}
.list{width:360px;border-right:1px solid var(--border);overflow:auto;flex:none}
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
.item .body.cmd{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#7ee787;border-left:2px solid #2ea043;padding-left:8px;margin-bottom:6px;white-space:pre-wrap;word-break:break-word}
.empty{color:var(--mut);padding:40px;text-align:center}
.filter{padding:8px 14px;border-bottom:1px solid var(--border)}
.filter input{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--fg);padding:6px 8px;border-radius:6px}
</style></head><body>
<header><h1 onclick="goHome()">&#9877; Agent Telemetry</h1><span class=stat id=stat>loading&#8230;</span>
<span class=stat style=margin-left:auto><span class=dot>&#9679;</span> auto 5s</span></header>
<div id=app></div>
<script>
let view='agents', curAgent=null, curSession=null, agents=[], sessions=[], filter='', richonly=true;
const ICON={reasoning:'\\u{1F9E0} think',message:'\\u{1F4AC} progress',execute_tool:'\\u{1F527} tool',chat:'\\u2699 model','agent.run':'\\u25B6 run'};
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmtTime(ms){if(!ms)return'';const d=new Date(ms);return d.toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
async function loadStat(){const s=await (await fetch('api/stats')).json();
 document.getElementById('stat').textContent=`${s.services.length} agents \\u00b7 ${s.sessions} sessions \\u00b7 ${s.spans} spans`}
function goHome(){view='agents';curAgent=null;curSession=null;tick()}
async function loadAgents(){agents=await (await fetch('api/agents')).json();if(view==='agents')renderAgents()}
function renderAgents(){const app=document.getElementById('app');
 app.innerHTML='<div class=agents>'+agents.map(a=>
  `<div class=card onclick="enter('${a.service.replace(/'/g,"")}')">
   <h2>${esc(a.service)}</h2>
   <div class=big>${a.valuable} 有内容会话 \\u00b7 ${a.spans} spans</div>
   <div class=nums>
     <span class="chip t">${a.thinks} 思考</span>
     <span class="chip m">${a.msgs} 进度</span>
     <span class="chip x">${a.tools} 工具</span></div>
   <div>${a.layers.map(l=>`<span class="tag ${l}">${l}</span>`).join('')}</div>
   ${a.preview?`<div class=pv>${esc(a.preview)}</div>`:''}
   <div class=big style=margin-top:6px>${fmtTime(a.last)}</div></div>`).join('')+'</div>'
  ||'<div class=empty>no agents yet</div>'}
async function enter(svc){view='agent';curAgent=svc;curSession=null;
 sessions=await (await fetch('api/sessions?service='+encodeURIComponent(svc))).json();renderAgent()}
function renderAgent(){const app=document.getElementById('app');
 app.innerHTML=`<div class=crumb><a onclick="goHome()">\\u2190 所有 Agent</a> / <b>${esc(curAgent)}</b></div>
  <div class=wrap><div class=list><div class=filter><input id=q placeholder="filter session...">
   <label style="display:block;margin-top:6px;color:#8b949e;font-size:12px;cursor:pointer">
   <input type=checkbox id=richonly ${richonly?'checked':''}> 只看有内容</label></div>
   <div id=sessions></div></div><div class=detail id=detail><div class=empty>&#8592; 选择一个会话</div></div></div>`;
 document.getElementById('q').addEventListener('input',e=>{filter=e.target.value;renderSessions()});
 document.getElementById('richonly').addEventListener('change',e=>{richonly=e.target.checked;renderSessions()});
 renderSessions();if(curSession)openSession(curSession)}
function renderSessions(){const box=document.getElementById('sessions');if(!box)return;const f=filter.toLowerCase();
 box.innerHTML=sessions.filter(s=>(!f||s.session.toLowerCase().includes(f))&&(!richonly||s.rich>0)).map(s=>
  `<div class="sess${s.session===curSession?' active':''}" onclick="openSession('${s.session.replace(/'/g,"")}')">
   <div class=svc>${esc(s.session.slice(0,30))}</div>
   <div class=meta>${s.thinks} 思考 \\u00b7 ${s.msgs} 进度 \\u00b7 ${s.tools} 工具 \\u00b7 ${fmtTime(s.last)}</div>
   ${s.preview?`<div class=meta style="color:#c9d1d9;margin-top:4px">${esc(s.preview)}</div>`:''}
   <div style=margin-top:4px>${s.layers.map(l=>`<span class="tag ${l}">${l}</span>`).join('')}</div></div>`).join('')
  ||'<div class=empty>无会话</div>'}
async function openSession(id){curSession=id;renderSessions();
 const items=await (await fetch('api/session?id='+encodeURIComponent(id))).json();
 const d=document.getElementById('detail');if(!d)return;
 if(!items.length){d.innerHTML='<div class=empty>empty</div>';return}
 d.innerHTML=items.map(it=>{
  const ic=ICON[it.kind]||ICON[it.name]||it.name;
  let sub=[];if(it.nkind)sub.push(it.nkind);if(it.tool)sub.push(it.tool);if(it.model)sub.push(it.model);
  if(it.in_tok||it.out_tok)sub.push(`tok ${it.in_tok||0}/${it.out_tok||0}`);
  if(it.ms)sub.push(it.ms+'ms');if(it.layer)sub.push(it.layer);
  let body=it.text?`<div class="body ${it.kind==='execute_tool'?'tool':''}">${esc(it.text)}</div>`:'';
  if(!it.text&&it.facts&&it.facts.length)body=`<div class=body style=color:#9fb6cf>${it.facts.map(esc).join(' \\u00b7 ')}</div>`;
  let cmd=it.args?`<div class="body cmd">$ ${esc(it.args)}</div>`:'';
  return `<div class="item ${it.kind}"><div class=h><span class=ic>${ic}</span>
   <span class=sub>${esc(sub.join(' \\u00b7 '))}</span></div>${cmd}${body}</div>`}).join('')}
async function tick(){await loadStat();
 if(view==='agents'){await loadAgents();renderAgents()}
 else{await loadAgents();sessions=await (await fetch('api/sessions?service='+encodeURIComponent(curAgent)).then(r=>r.json()));
  if(!document.querySelector('.wrap'))renderAgent();else{renderSessions();if(curSession)openSession(curSession)}}}
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
            elif path == "/api/agents":
                self._json(self.store.agents())
            elif path == "/api/sessions":
                service = parse_qs(parsed.query).get("service", [None])[0]
                self._json(self.store.sessions(service))
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
