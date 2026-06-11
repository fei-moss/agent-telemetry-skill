"""Generate diverse, realistic agent telemetry scenarios for demos/tests.

Builds rich multi-step sessions (reasoning / progress / tool calls) for several
agent types — coding, market research, writing, data analysis, trading — and
pushes them to a telemetry backend so the dashboard shows varied, valuable
content (not just one bot). Each scenario uses its own service name.

Usage:
    AGENT_TELEMETRY_ENDPOINT=http://host:4318/v1/traces \\
    AGENT_TELEMETRY_TOKEN=... python3 examples/gen_scenarios.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_telemetry_skill import session_trace
from agent_telemetry_skill.config import load_config
from agent_telemetry_skill.exporters import ConsoleExporter, OTLPHTTPExporter
from agent_telemetry_skill.redaction import RedactionConfig, Redactor
from agent_telemetry_skill.schema import new_span_id, new_trace_id
from agent_telemetry_skill.watchers._common import make_narrative_span, make_tool_span


# step kinds: ("think", text) / ("say", text) / ("tool", name, args, result)
SCENARIOS: dict[str, list] = {
    "coding-agent": [
        ("think", "用户报登录接口 500。先看 auth 路由实现，定位异常来源。"),
        ("say", "我先读一下登录接口的代码。"),
        ("tool", "read_file", {"path": "app/auth/login.py"},
         "def login(req):\n    user = db.find(req.email)\n    return make_token(user.id)  # user 可能为 None"),
        ("think", "找到了：user 为 None 时 user.id 抛 AttributeError，没有处理用户不存在的情况。"),
        ("say", "定位到根因：用户不存在时未做判空，导致空指针 500。我来加判空。"),
        ("tool", "apply_patch", {"file": "app/auth/login.py"},
         "+ if user is None:\n+     raise HTTPError(401, 'invalid credentials')"),
        ("tool", "terminal", {"command": "pytest tests/test_auth.py -q"},
         '{"output": "5 passed in 0.42s", "exit_code": 0}'),
        ("say", "修复完成：登录接口对用户不存在返回 401 而非 500，测试 5 项全过。"),
    ],
    "research-agent": [
        ("think", "用户要调研 2026 年 AI agent 框架市场。先定维度：主流框架、采用度、差异化。"),
        ("say", "我先搜索 2026 年主流 AI agent 框架的概况。"),
        ("tool", "web_search", {"query": "2026 AI agent framework market landscape"},
         '{"results": 8, "top": ["LangGraph", "CrewAI", "AutoGen", "Claude Agent SDK"]}'),
        ("tool", "web_fetch", {"url": "https://example.com/agent-frameworks-2026"},
         "LangGraph 在生产编排占主导；CrewAI 主打多角色协作；Claude Agent SDK 增长最快。"),
        ("think", "综合：编排能力、多智能体、生态成熟度是三大分水岭。LangGraph 生产份额最高。"),
        ("say", "调研结论：2026 年 agent 框架呈三足之势——LangGraph(生产编排领先)、CrewAI(多角色)、Claude Agent SDK(增长最快)。差异化集中在编排与多智能体协作。"),
    ],
    "writing-agent": [
        ("think", "用户要一篇关于'本地优先 AI'的短文。先搭结构：是什么、为什么、怎么做。"),
        ("say", "我先列一个三段式提纲。"),
        ("tool", "save_outline", {"sections": ["定义", "动机", "实践"]},
         '{"status": "saved", "words_target": 600}'),
        ("think", "动机段要突出隐私与延迟；实践段给一个遥测的例子比较具体。"),
        ("say", "初稿完成：《本地优先 AI——把智能放回你自己的机器》,约 600 字,含定义、隐私/延迟动机、以及本地遥测实践示例。"),
    ],
    "data-analysis-agent": [
        ("think", "用户要分析上月销售 CSV。先看表结构和行数，再按品类聚合。"),
        ("tool", "terminal", {"command": "head -1 sales.csv && wc -l sales.csv"},
         '{"output": "date,category,amount\\n12840 sales.csv", "exit_code": 0}'),
        ("think", "12840 行,按 category 分组求和,找 top 品类。"),
        ("tool", "run_python", {"code": "df.groupby('category').amount.sum().nlargest(3)"},
         '{"output": "Electronics 1.82M\\nApparel 0.94M\\nHome 0.61M"}'),
        ("say", "分析完成:上月销售 Top3 品类为电子(182万)、服饰(94万)、家居(61万),电子占比近半。"),
    ],
    "trading-agent": [
        ("think", "BTC 突破 69500 区间上沿并放量,趋势策略下出现做多信号。先确认持仓与风险敞口。"),
        ("tool", "query_position", {"symbol": "BTC/USDT"},
         '{"status": "success", "output": "持仓: 0, 余额: 9540 USDT, BTC=69820"}'),
        ("think", "空仓且突破确认,按 2% 风险开多,止损放 68800。"),
        ("say", "本轮决策:开多 BTC,10x 轻仓,止损 68800(突破确认 + 放量)。"),
        ("tool", "place_order", {"side": "long", "symbol": "BTC/USDT", "leverage": 10},
         '{"status": "success", "order_id": "ord_7f3a", "filled": "69820"}'),
    ],
}


def build_session(service: str, steps: list, redactor: Redactor, base_ns: int):
    sid = f"demo_{service}_{base_ns}"
    record = session_trace.SessionTrace(
        session_id=sid, trace_id=new_trace_id(), root_span_id=new_span_id(),
        start_time_unix_nano=base_ns, agent_name=service, attributes={},
    )
    spans = []
    t = base_ns
    seq = 0
    for step in steps:
        t += 1_000_000_000  # +1s per step
        if step[0] == "think":
            spans.append(make_narrative_span(record, kind="reasoning", text=step[1],
                         source_file=f"/sessions/{sid}.jsonl", time_unix_nano=t,
                         redactor=redactor, sequence=seq))
        elif step[0] == "say":
            spans.append(make_narrative_span(record, kind="message", text=step[1],
                         source_file=f"/sessions/{sid}.jsonl", time_unix_nano=t,
                         redactor=redactor, sequence=seq))
        elif step[0] == "tool":
            _, name, args, result = step
            spans.append(make_tool_span(record, tool_name=name, call_id=f"c{seq}",
                         arguments=args, source_file=f"/sessions/{sid}.jsonl",
                         start_time_unix_nano=t, end_time_unix_nano=t + 300_000_000,
                         result=result, redactor=redactor))
        seq += 1
    return spans


def main() -> int:
    cfg = load_config()
    redactor = Redactor(RedactionConfig(capture_content=True, max_string_length=4000))
    base = 1_781_100_000_000_000_000
    total = 0
    for i, (service, steps) in enumerate(SCENARIOS.items()):
        spans = build_session(service, steps, redactor, base + i * 60_000_000_000)
        if cfg.endpoint:
            exporter = OTLPHTTPExporter(
                cfg.endpoint,
                headers={"Authorization": f"Bearer {cfg.token}"} if cfg.token else {},
                service_name=service,
            )
        else:
            exporter = ConsoleExporter()
        exporter.export(spans)
        total += len(spans)
        print(f"  {service}: {len(spans)} spans")
    dest = cfg.endpoint or "console"
    print(f"pushed {total} spans across {len(SCENARIOS)} scenarios -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
