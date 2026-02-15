import html
import json


DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — API Docs</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
:root {{
    --bg: #0d1117; --surface: #161b22; --border: #21262d;
    --text: #e6edf3; --muted: #8b949e; --accent: #a78bfa;
    --green: #3fb950; --blue: #58a6ff; --orange: #d29922;
    --red: #f85149; --yellow: #e3b341;
}}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }}
.top-bar {{ padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }}
.top-bar h1 {{ font-size: 20px; font-weight: 700; }}
.top-bar h1 span {{ color: var(--accent); }}
.top-bar .version {{ font-size: 12px; background: var(--accent); color: #0d1117; padding: 2px 8px; border-radius: 10px; font-weight: 600; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
.search {{ width: 100%; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 14px; margin-bottom: 24px; outline: none; }}
.search:focus {{ border-color: var(--accent); }}
.route {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 12px; overflow: hidden; transition: border-color 0.2s; }}
.route:hover {{ border-color: var(--accent); }}
.route-header {{ padding: 16px 20px; cursor: pointer; display: flex; align-items: center; gap: 14px; user-select: none; }}
.method {{ font-family: 'SF Mono', monospace; font-size: 11px; font-weight: 700; padding: 5px 10px; border-radius: 5px; min-width: 60px; text-align: center; letter-spacing: 0.5px; }}
.GET {{ background: rgba(63,185,80,0.12); color: var(--green); }}
.POST {{ background: rgba(88,166,255,0.12); color: var(--blue); }}
.PUT {{ background: rgba(227,179,65,0.12); color: var(--yellow); }}
.PATCH {{ background: rgba(210,153,34,0.12); color: var(--orange); }}
.DELETE {{ background: rgba(248,81,73,0.12); color: var(--red); }}
.route-path {{ font-family: 'SF Mono', monospace; font-size: 14px; flex: 1; }}
.route-path .param {{ color: var(--accent); }}
.route-name {{ font-size: 12px; color: var(--muted); }}
.chevron {{ color: var(--muted); transition: transform 0.2s; font-size: 12px; }}
.route.open .chevron {{ transform: rotate(90deg); }}
.route-body {{ display: none; padding: 0 20px 20px; border-top: 1px solid var(--border); margin-top: 0; }}
.route.open .route-body {{ display: block; padding-top: 16px; }}
.params-table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
.params-table th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--border); }}
.params-table td {{ padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border); }}
.params-table td:first-child {{ font-family: 'SF Mono', monospace; color: var(--accent); }}
.type-badge {{ background: var(--bg); padding: 2px 8px; border-radius: 4px; font-size: 11px; font-family: 'SF Mono', monospace; color: var(--muted); }}
.try-it {{ margin-top: 16px; }}
.try-it h4 {{ font-size: 13px; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px; }}
.try-row {{ display: flex; gap: 8px; margin-bottom: 8px; }}
.try-input {{ flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; color: var(--text); font-family: 'SF Mono', monospace; font-size: 13px; outline: none; }}
.try-input:focus {{ border-color: var(--accent); }}
.try-btn {{ padding: 10px 20px; background: var(--accent); color: #0d1117; border: none; border-radius: 6px; font-weight: 600; font-size: 13px; cursor: pointer; white-space: nowrap; }}
.try-btn:hover {{ opacity: 0.9; }}
.response-box {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; margin-top: 12px; overflow: hidden; display: none; }}
.response-status {{ padding: 10px 14px; border-bottom: 1px solid var(--border); font-family: 'SF Mono', monospace; font-size: 12px; display: flex; justify-content: space-between; }}
.response-body {{ padding: 14px; font-family: 'SF Mono', monospace; font-size: 13px; white-space: pre-wrap; max-height: 300px; overflow-y: auto; line-height: 1.6; }}
.response-headers {{ padding: 10px 14px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); font-family: 'SF Mono', monospace; max-height: 150px; overflow-y: auto; }}
.status-ok {{ color: var(--green); }}
.status-err {{ color: var(--red); }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-right: 4px; }}
.tag-rl {{ background: rgba(248,81,73,0.12); color: var(--red); }}
.tag-auth {{ background: rgba(227,179,65,0.12); color: var(--yellow); }}
.body-input {{ width: 100%; min-height: 80px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; color: var(--text); font-family: 'SF Mono', monospace; font-size: 13px; outline: none; resize: vertical; }}
.body-input:focus {{ border-color: var(--accent); }}
.tab-bar {{ display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 12px; }}
.tab {{ padding: 8px 16px; font-size: 12px; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; }}
.tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.summary {{ padding: 16px 0; display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 8px; }}
.summary-item {{ font-size: 13px; color: var(--muted); }}
.summary-item strong {{ color: var(--text); }}
</style>
</head>
<body>
<div class="top-bar">
    <h1>⚡ <span>{title}</span></h1>
    <span class="version">{version}</span>
    <span style="margin-left:auto; font-size:13px; color:var(--muted);">{route_count} endpoints</span>
</div>
<div class="container">
    <div class="summary">
        <div class="summary-item"><strong>{get_count}</strong> GET</div>
        <div class="summary-item"><strong>{post_count}</strong> POST</div>
        <div class="summary-item"><strong>{put_count}</strong> PUT</div>
        <div class="summary-item"><strong>{delete_count}</strong> DELETE</div>
        <div class="summary-item"><strong>{patch_count}</strong> PATCH</div>
    </div>
    <input type="text" class="search" placeholder="Search endpoints..." onkeyup="filterRoutes(this.value)">
    {routes_html}
</div>
<script>
function filterRoutes(q) {{
    q = q.toLowerCase();
    document.querySelectorAll('.route').forEach(r => {{
        const text = r.textContent.toLowerCase();
        r.style.display = text.includes(q) ? '' : 'none';
    }});
}}
function toggle(el) {{
    el.closest('.route').classList.toggle('open');
}}
async function tryEndpoint(btn, method, path, routeId) {{
    const route = btn.closest('.route');
    const urlInput = route.querySelector('.try-url');
    const bodyInput = route.querySelector('.body-input');
    const resBox = route.querySelector('.response-box');
    const resStatus = route.querySelector('.response-status');
    const resBody = route.querySelector('.response-body');
    const resHeaders = route.querySelector('.response-headers');

    const url = urlInput ? urlInput.value : path;
    const opts = {{ method: method, headers: {{'Accept': 'application/json'}} }};

    if (bodyInput && bodyInput.value.trim()) {{
        opts.body = bodyInput.value;
        opts.headers['Content-Type'] = 'application/json';
    }}

    try {{
        const resp = await fetch(url, opts);
        const text = await resp.text();
        let display = text;
        try {{ display = JSON.stringify(JSON.parse(text), null, 2); }} catch(e) {{}}

        const statusClass = resp.status < 400 ? 'status-ok' : 'status-err';
        resStatus.innerHTML = `<span class="${{statusClass}}">HTTP ${{resp.status}} ${{resp.statusText}}</span><span style="color:var(--muted)">${{new Date().toLocaleTimeString()}}</span>`;
        resBody.textContent = display;

        let hdrText = '';
        resp.headers.forEach((v, k) => {{ hdrText += k + ': ' + v + '\\n'; }});
        resHeaders.textContent = hdrText;
        resBox.style.display = 'block';
    }} catch(e) {{
        resStatus.innerHTML = `<span class="status-err">Error: ${{e.message}}</span>`;
        resBody.textContent = '';
        resBox.style.display = 'block';
    }}
}}
function showTab(el, tabName, routeId) {{
    const route = el.closest('.route-body');
    route.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    route.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    route.querySelector('.tab-' + tabName).classList.add('active');
}}
</script>
</body>
</html>"""


def _highlight_path(path):
    import re
    def replacer(m):
        return f'<span class="param">{m.group(0)}</span>'
    return re.sub(r'<[^>]+>', replacer, html.escape(path))


def _build_route_html(idx, route):
    parts = []
    for method in route.methods:
        tags = ""
        params_rows = ""

        if route._param_names:
            rows = ""
            for pname, ptype in route._param_names:
                rows += f"""<tr>
                    <td>{pname}</td>
                    <td><span class="type-badge">{ptype}</span></td>
                    <td>path</td>
                    <td>required</td>
                </tr>"""
            params_rows = f"""
            <table class="params-table">
                <tr><th>Name</th><th>Type</th><th>In</th><th>Required</th></tr>
                {rows}
            </table>"""

        has_body = method in ("POST", "PUT", "PATCH")
        body_section = ""
        if has_body:
            body_section = f"""
            <div style="margin-top:12px;">
                <h4 style="font-size:12px; color:var(--muted); margin-bottom:8px;">REQUEST BODY</h4>
                <textarea class="body-input" placeholder='{{"key": "value"}}'></textarea>
            </div>"""

        path_display = _highlight_path(route.path)
        default_url = route.path
        for pname, ptype in route._param_names:
            if ptype == "int":
                default_url = default_url.replace(f"<{ptype}:{pname}>", "1")
            else:
                default_url = default_url.replace(f"<{ptype}:{pname}>", "example")
                default_url = default_url.replace(f"<{pname}>", "example")

        parts.append(f"""
        <div class="route" id="route-{idx}-{method}">
            <div class="route-header" onclick="toggle(this)">
                <span class="method {method}">{method}</span>
                <span class="route-path">{path_display}</span>
                <span class="route-name">{route.name}</span>
                {tags}
                <span class="chevron">▶</span>
            </div>
            <div class="route-body">
                <div class="tab-bar">
                    <div class="tab active" onclick="showTab(this,'params','{idx}')">Parameters</div>
                    <div class="tab" onclick="showTab(this,'try','{idx}')">Try It</div>
                </div>
                <div class="tab-content tab-params active">
                    {params_rows or '<p style="color:var(--muted); font-size:13px;">No parameters</p>'}
                </div>
                <div class="tab-content tab-try">
                    <div class="try-it">
                        <div class="try-row">
                            <input type="text" class="try-input try-url" value="{default_url}">
                            <button class="try-btn" onclick="tryEndpoint(this, '{method}', '{default_url}', '{idx}')">Send</button>
                        </div>
                        {body_section}
                    </div>
                    <div class="response-box">
                        <div class="response-status"></div>
                        <div class="response-body"></div>
                        <div class="response-headers"></div>
                    </div>
                </div>
            </div>
        </div>""")

    return "\n".join(parts)


def generate_docs_html(app, title="PhotonAPI", version="1.0.0"):
    routes_html = ""
    method_counts = {"GET": 0, "POST": 0, "PUT": 0, "DELETE": 0, "PATCH": 0}

    for idx, route in enumerate(app.routes):
        if route.path == "/docs":
            continue
        routes_html += _build_route_html(idx, route)
        for m in route.methods:
            if m in method_counts:
                method_counts[m] += 1

    total = sum(method_counts.values())

    return DOCS_HTML.format(
        title=title,
        version=version,
        route_count=total,
        routes_html=routes_html,
        get_count=method_counts["GET"],
        post_count=method_counts["POST"],
        put_count=method_counts["PUT"],
        delete_count=method_counts["DELETE"],
        patch_count=method_counts["PATCH"],
    )
