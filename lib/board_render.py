"""
relay board — a self-contained, app-style overview of everything relay knows.

Master–detail, calm by design: a left rail (a pinned "Needs review" group of reported executors,
then one group per lead) and a right detail pane showing one executor at a time. Each packet reads
as one line — number, status, outcome — with risk / unverified tucked behind a click so the page
never looks alarmed. Warm-white ground, one cobalt accent, muted semantic colours. Navigation is
client-side (click a rail item, or a URL #hash); search filters the rail. Light by default, dark
via a remembered toggle. Self-contained: no web fonts, no external assets.

Pure: render(data) -> html. `data` is assembled by bin/relay's board_data from the same helpers
`relay list` uses, so the page can never disagree with the table.
"""
import html


def _e(x):
    return html.escape("" if x is None else str(x), quote=True)


_STATUS = {
    "reported": "reported", "busy": "busy", "stalled": "stalled", "dead": "dead",
    "launch-failed": "dead", "closed": "closed", "superseded": "closed",
}

_CSS = """
:root{
 --bg:#faf9f7;--rail:#f4f2ee;--panel:#ffffff;--ink:#1c1b22;--muted:#6c6a76;--faint:#a5a2ad;
 --line:#ece9e4;--line2:#f2f0eb;--accent:#2f56e6;--accent-ink:#2447c4;--tint:#eef1fe;
 --ok:#1f8f52;--ok-bg:#e9f5ee;--warn:#b06a00;--warn-bg:#fbf0dd;--bad:#c73a3a;--bad-bg:#fbe8e6;
 --dim:#a5a2ad;--dim-bg:#f0eeea;--chip:#f3f1ec;--chip-ink:#4a4854;
 --sh:0 1px 2px rgba(28,27,34,.05),0 3px 10px rgba(28,27,34,.04);--sh2:0 6px 22px rgba(28,27,34,.08);--r:12px}
:root[data-theme=dark]{
 --bg:#131217;--rail:#0f0e13;--panel:#1a191f;--ink:#eceaf1;--muted:#9a97a4;--faint:#66636f;
 --line:#272530;--line2:#201f28;--accent:#7c93ff;--accent-ink:#a9baff;--tint:#1b2340;
 --ok:#5cd08a;--ok-bg:#12291c;--warn:#e9a94a;--warn-bg:#2c2109;--bad:#f0736b;--bad-bg:#2d1513;
 --dim:#66636f;--dim-bg:#1c1b23;--chip:#22212a;--chip-ink:#b4b1bd;
 --sh:none;--sh2:0 8px 26px rgba(0,0,0,.5);--r:12px}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Inter,system-ui,sans-serif;
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}
.num{font-variant-numeric:tabular-nums}
.lbl{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
/* top */
.top{height:60px;display:flex;align-items:center;gap:18px;padding:0 22px;background:var(--bg);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
.brand{font-weight:700;font-size:16px;letter-spacing:-.02em;display:flex;align-items:center;gap:8px;white-space:nowrap}
.kpis{display:flex;gap:20px}
.kpi{display:flex;flex-direction:column;line-height:1.1}
.kpi b{font-size:19px;font-weight:700}.kpi span{font-size:11px;color:var(--muted);letter-spacing:.01em}
.kpi.rev b{color:var(--ok)} .kpi.busy b{color:var(--warn)} .kpi.bad b{color:var(--bad)}
.grow{flex:1}
.search{width:min(300px,30vw);padding:9px 13px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);font-size:13px;transition:border-color .12s}
.search::placeholder{color:var(--faint)}
.search:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--tint)}
.tbtn{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:10px;width:38px;height:38px;font-size:16px;cursor:pointer;transition:.12s}
.tbtn:hover{border-color:var(--accent);color:var(--accent)}
/* split */
.app{display:flex;height:calc(100vh - 60px)}
.side{width:296px;min-width:264px;background:var(--rail);border-right:1px solid var(--line);overflow:auto;padding:14px 12px}
.detail{flex:1;overflow:auto;padding:30px 40px 90px}
@media(max-width:860px){.app{flex-direction:column;height:auto}.side{width:auto;border-right:none;border-bottom:1px solid var(--line);max-height:42vh}.detail{padding:24px 20px 60px}}
/* rail groups */
.grp{margin-bottom:14px}
.grp>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:5px 8px;margin-bottom:2px;border-radius:8px}
.grp>summary::-webkit-details-marker{display:none}
.grp>summary:hover{background:var(--dim-bg)}
.grp .gl{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grp.rev .gl{color:var(--ok)}
.cdot{width:8px;height:8px;border-radius:50%;flex:none}
.count{margin-left:auto;background:var(--chip);color:var(--chip-ink);border-radius:999px;padding:1px 8px;font-size:11px;font-weight:600}
.wake{font-size:10px;font-weight:600;padding:1px 6px;border-radius:5px;background:var(--dim-bg);color:var(--muted)}
.wake.bad{background:var(--bad-bg);color:var(--bad)} .lv-bad{color:var(--bad)}
/* rail items */
.item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;cursor:pointer;transition:background .1s}
.item:hover{background:var(--dim-bg)}
.item.active{background:var(--tint)}
.item.active .nm{color:var(--accent-ink)}
.sdot{width:9px;height:9px;border-radius:50%;flex:none}
.sdot.reported{background:var(--ok)} .sdot.busy{background:var(--warn);box-shadow:0 0 0 3px var(--warn-bg)} .sdot.stalled,.sdot.dead{background:var(--bad)} .sdot.closed{background:var(--dim)}
.item .nm{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .sub{color:var(--faint);font-size:11px;margin-left:auto;white-space:nowrap}
.item .flag{font-size:12px;line-height:1}
/* detail: overview */
.h1{font-size:24px;font-weight:700;letter-spacing:-.02em;margin:0}
.sub-h{color:var(--muted);margin:2px 0 22px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:14px;margin:0 0 22px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;box-shadow:var(--sh)}
.tile b{display:block;font-size:30px;font-weight:700;line-height:1;letter-spacing:-.02em}
.tile span{color:var(--muted);font-size:12px;margin-top:6px;display:block}
.tile.rev b{color:var(--ok)} .tile.busy b{color:var(--warn)}
.banner{display:flex;gap:11px;align-items:flex-start;border-radius:11px;padding:12px 15px;margin-bottom:10px;font-size:13px;background:var(--panel);border:1px solid var(--line);box-shadow:var(--sh)}
.banner .ic{flex:none;font-size:15px;line-height:1.3}
.banner.bad{border-color:color-mix(in srgb,var(--bad) 40%,var(--line));background:var(--bad-bg);color:var(--bad)}
.banner.warn{border-color:color-mix(in srgb,var(--warn) 35%,var(--line));background:var(--warn-bg);color:var(--warn)}
.banner.info{background:var(--tint);border-color:transparent;color:var(--accent-ink)}
.hint{margin:26px 0 12px}
.rev-list{display:grid;gap:9px}
.rev-card{display:flex;align-items:center;gap:13px;background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 16px;cursor:pointer;box-shadow:var(--sh);transition:.12s}
.rev-card:hover{box-shadow:var(--sh2);transform:translateY(-1px)}
.rev-card .nm{font-weight:650}.rev-card .oc{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
/* detail: executor */
.panel{display:none}.panel.show{display:block;animation:fade .14s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
@media(prefers-reduced-motion:reduce){.panel.show{animation:none}.rev-card:hover{transform:none}}
.crumb{color:var(--muted);font-size:12px;cursor:pointer;display:inline-flex;gap:5px;align-items:center}
.crumb:hover{color:var(--accent)}
.exhead{display:flex;align-items:center;gap:13px;flex-wrap:wrap;margin:12px 0 3px}
.exhead h1{font-size:23px;font-weight:700;letter-spacing:-.02em;margin:0}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 12px;font-size:12px;font-weight:600}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.reported{background:var(--ok-bg);color:var(--ok)} .pill.busy{background:var(--warn-bg);color:var(--warn)} .pill.stalled,.pill.dead{background:var(--bad-bg);color:var(--bad)} .pill.closed{background:var(--dim-bg);color:var(--dim)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 6px}
.chip{background:var(--chip);color:var(--chip-ink);border-radius:9px;padding:5px 11px;font-size:12px}
.chip b{color:var(--ink);font-weight:650;margin-left:4px}
.chip.flag{background:var(--warn-bg);color:var(--warn)} .chip.pin{background:var(--tint);color:var(--accent-ink)} .chip.bad{background:var(--bad-bg);color:var(--bad)}
.wt{color:var(--faint);font-size:12px;margin:10px 0 0}
/* packet dot strip */
.dots{display:flex;gap:7px;flex-wrap:wrap;margin:22px 0 4px}
.pdot{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:12px;font-weight:700;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--muted);transition:.1s}
.pdot.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent} .pdot.flight{background:var(--warn-bg);color:var(--warn);border-color:transparent} .pdot.none{background:var(--bad-bg);color:var(--bad);border-color:transparent}
.pdot:hover{box-shadow:var(--sh)} .pdot.active{box-shadow:0 0 0 2px var(--accent)}
/* packet rows — full task + outcome, readable inline */
.tl{margin:16px 0 0;display:grid;gap:10px}
.pk{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;box-shadow:var(--sh)}
.pk.sel{box-shadow:0 0 0 2px var(--accent),var(--sh)}
.pk-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.pk .n{font-weight:700;color:var(--faint);font-size:12.5px}
.pk .task{font-weight:600;line-height:1.45}
.pk .oc{color:var(--muted);line-height:1.45;margin-top:5px}
.pk .oc::before{content:"→ ";color:var(--faint)}
.spill{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.spill.ok{background:var(--ok-bg);color:var(--ok)} .spill.flight{background:var(--warn-bg);color:var(--warn)} .spill.none{background:var(--bad-bg);color:var(--bad)}
.tldr{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:12px 0 0;padding:11px 14px;background:var(--bg);border-radius:9px;font-size:12.5px}
.tldr dt{color:var(--faint);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;align-self:baseline;padding-top:1px}
.tldr dd{margin:0;color:var(--ink);line-height:1.4}
.tldr dd.warn{color:var(--warn)} .tldr dd.bad{color:var(--bad)}
.doc{margin-top:10px;font-size:12.5px}
.doc>summary{list-style:none;cursor:pointer;color:var(--accent-ink);font-weight:600;display:inline-flex;gap:6px;align-items:center;padding:4px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);transition:.1s;width:max-content}
.doc>summary::-webkit-details-marker{display:none}
.doc>summary::before{content:"▸";color:var(--accent);font-size:11px}
.doc[open]>summary::before{content:"▾"}
.doc>summary:hover{border-color:var(--accent);background:var(--tint)}
.doc>summary:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.doc .trunc{color:var(--faint);font-weight:400;font-size:11px;text-transform:none;letter-spacing:0}
.doc pre{margin:8px 0 0;padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,monospace;font-size:12px;line-height:1.5;max-height:420px;overflow-y:auto}
.fpath{margin-top:6px;color:var(--faint);font-size:11px}
.dlink{margin-top:12px;display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;color:var(--accent-ink);border:1px solid var(--line);border-radius:8px;padding:6px 12px;background:var(--panel);box-shadow:var(--sh);transition:.1s;width:max-content}
.dlink:hover{border-color:var(--accent);background:var(--tint);text-decoration:none}
.dlink .ext{color:var(--accent);font-size:13px}
/* commands — labelled action buttons that copy the underlying command */
.cmd-h{margin:26px 0 9px}
.cmds{display:flex;gap:9px;flex-wrap:wrap}
.cmd{display:inline-flex;align-items:center;gap:8px;font:inherit;font-size:13px;font-weight:550;border:1px solid var(--line);border-radius:9px;padding:8px 13px;cursor:pointer;color:var(--ink);background:var(--panel);box-shadow:var(--sh);transition:.1s}
.cmd:hover{border-color:var(--accent);background:var(--tint);color:var(--accent-ink)}
.cmd .ci{color:var(--faint);font-size:14px} .cmd:hover .ci{color:var(--accent)}
.cmd.go{color:var(--accent-ink);border-color:color-mix(in srgb,var(--accent) 30%,var(--line))}
.muted{color:var(--muted)}.empty{color:var(--faint);padding:70px 0;text-align:center}
"""

_JS = r"""
(function(){
 var root=document.documentElement,K='relay-board-theme';
 try{if(localStorage.getItem(K)==='dark')root.setAttribute('data-theme','dark');}catch(e){}
 function themed(){return root.getAttribute('data-theme')==='dark';}
 window.toggleTheme=function(){var d=themed();if(d)root.removeAttribute('data-theme');else root.setAttribute('data-theme','dark');try{localStorage.setItem(K,d?'light':'dark')}catch(e){}sync();};
 function sync(){var b=document.getElementById('tbtn');if(b)b.textContent=themed()?'☀︎':'☾';}
 function select(id){
  document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('show',p.id===id);});
  document.querySelectorAll('.item').forEach(function(it){it.classList.toggle('active',it.dataset.target===id);});
  if(id&&id!=='home'){try{history.replaceState(null,'',location.pathname+'#'+id);}catch(e){}}
  var d=document.querySelector('.detail');if(d)d.scrollTop=0;
 }
 window.selectPacket=function(exid,n){
  document.querySelectorAll('#'+exid+' .pdot').forEach(function(el){el.classList.toggle('active',el.dataset.n===n);});
  document.querySelectorAll('#'+exid+' .pk').forEach(function(el){var m=el.dataset.n===n;el.classList.toggle('sel',m);if(m)el.scrollIntoView({block:'nearest',behavior:'smooth'});});
 };
 document.addEventListener('DOMContentLoaded',function(){
  sync();
  document.body.addEventListener('click',function(e){
   var nav=e.target.closest('[data-target]');if(nav){select(nav.dataset.target);return;}
   var pd=e.target.closest('.pdot');if(pd){selectPacket(pd.dataset.ex,pd.dataset.n);return;}
   var cm=e.target.closest('.cmd');if(cm){var t=cm.dataset.cmd||cm.textContent;if(navigator.clipboard)navigator.clipboard.writeText(t);var o=cm.textContent;cm.textContent='copied ✓';setTimeout(function(){cm.textContent=o},900);return;}
  });
  var q=document.getElementById('q');
  q.addEventListener('input',function(){
   var v=q.value.toLowerCase();
   document.querySelectorAll('.item').forEach(function(it){it.style.display=(!v||it.dataset.hay.indexOf(v)>=0)?'':'none';});
   document.querySelectorAll('.grp').forEach(function(g){var any=!v||g.querySelectorAll('.item:not([style*="none"])').length>0;g.style.display=any?'':'none';if(v)g.open=true;});
  });
  var b=document.getElementById('board');var def=(b&&b.dataset.default)||'home';
  var h=location.hash.slice(1);select(h&&document.getElementById(h)?h:def);
 });
})();
"""


def _status_key(st):
    return _STATUS.get(st, "closed")


def _packet_state(p):
    if p.get("report_path"):
        return "ok"
    if p.get("current"):
        return "flight"
    return "none"


def _mcp_short(ex):
    if ex.get("launch"):
        return ex["launch"].split("/")[0]
    m = ex.get("mcp")
    if isinstance(m, list):
        return ",".join(m) or "none"
    return m if m is not None else "?"


def _rail_item(ex):
    sk = _status_key(ex.get("status"))
    hay = " ".join(str(ex.get(k) or "") for k in ("session_id", "topic", "scope", "status", "model", "launch", "owner_project", "worktree")).lower()
    sub = ex.get("tokens") if ex.get("tokens") and ex.get("tokens") != "-" else (f'{ex.get("pkt")}pk' if ex.get("pkt") else "")
    flag = '<span class="flag" title="report not delivered">\U0001F6A6</span>' if ex.get("unannounced") else ""
    return (f'<div class="item" data-target="ex-{_e(ex["session_id"])}" data-hay="{_e(hay)}">'
            f'<span class="sdot {sk}"></span>'
            f'<span class="nm" title="{_e(ex["session_id"])}">{_e(ex["session_id"])}</span>'
            f'{flag}<span class="sub num">{_e(sub)}</span></div>')


def _chips(ex):
    out = []
    if ex.get("model"):
        out.append(f'<span class="chip">model<b>{_e(ex["model"])}</b></span>')
    ctx = ex.get("context") or ("1m" if str(ex.get("model", "")).endswith("[1m]") else None)
    if ctx:
        out.append(f'<span class="chip">context<b>{_e(ctx)}</b></span>')
    out.append(f'<span class="chip">mcp<b>{_e(_mcp_short(ex))}</b></span>')
    if ex.get("agent"):
        out.append('<span class="chip">role<b>agent</b></span>')
    if ex.get("tokens") and ex.get("tokens") != "-":
        out.append(f'<span class="chip">tokens<b>{_e(ex["tokens"])}</b></span>')
    if ex.get("mb") and ex.get("mb") != "-":
        out.append(f'<span class="chip">size<b>{_e(ex["mb"])} MB</b></span>')
    if ex.get("heavy"):
        out.append('<span class="chip flag">heavy</span>')
    if ex.get("keep"):
        out.append('<span class="chip pin">\U0001F4CC pinned</span>')
    if ex.get("queued"):
        out.append(f'<span class="chip flag">\U0001F4E5 {ex["queued"]} queued</span>')
    if ex.get("auto_closed"):
        out.append(f'<span class="chip">auto: {_e(ex["auto_closed"])}</span>')
    if ex.get("unannounced"):
        out.append('<span class="chip bad">\U0001F6A6 report not delivered</span>')
    if ex.get("orphan"):
        out.append('<span class="chip bad">orphan</span>')
    return "".join(out)


def _packets(ex):
    pk = ex.get("packets") or []
    exid = f'ex-{ex["session_id"]}'
    if not pk:
        return '<div class="muted" style="margin-top:16px">no packets on disk yet</div>'
    dots = "".join(
        f'<div class="pdot {_packet_state(p)}" data-ex="{_e(exid)}" data-n="{_e(p["n"])}" '
        f'title="packet {_e(p["n"])}">{_e(str(int(p["n"])) if str(p["n"]).isdigit() else p["n"])}</div>'
        for p in pk)
    rows = []
    for p in pk:
        t = p.get("tldr") or {}
        state = _packet_state(p)
        spill = {"ok": '<span class="spill ok">reported</span>', "flight": '<span class="spill flight">in flight</span>',
                 "none": '<span class="spill none">no report</span>'}[state]
        gist = p.get("gist")
        outcome = t.get("outcome")
        body = [f'<div class="pk-row"><span class="n num">#{_e(p["n"])}</span>{spill}</div>']
        if gist:
            body.append(f'<div class="task">{_e(gist)}</div>')
        if outcome and outcome != gist:
            body.append(f'<div class="oc">{_e(outcome)}</div>')
        # TL;DR as a clean label→value grid (shown only when a report exists); risk/unverified
        # carry a semantic colour only when their value is a real concern, else read muted.
        def _row(lbl, val, warn=False, bad=False):
            if val is None:
                return ""
            cls = "bad" if bad else ("warn" if warn else "")
            return f'<dt>{lbl}</dt><dd class="{cls}">{_e(val)}</dd>'
        risk = t.get("risk")
        uv = t.get("unverified")
        if t.get("status") or risk or uv:
            grid = (_row("Status", t.get("status"))
                    + _row("Risk", risk, bad=bool(risk and str(risk).lower() != "none"))
                    + _row("Unverified", uv, warn=bool(uv and str(uv).lower() != "none")))
            body.append(f'<dl class="tldr">{grid}</dl>')
        # inline, in-place expanders — no new path. Report first (what you review), then packet, diff.
        for label, bkey in (("report", "report_body"), ("packet", "packet_body")):
            b = p.get(bkey)
            if b and b.get("text"):
                more = ' <span class="trunc">(truncated — full file below)</span>' if b.get("truncated") else ""
                path = f'<div class="fpath mono">{_e(b["path"])}</div>' if b.get("truncated") else ""
                body.append(f'<details class="doc"><summary>{label}{more}</summary>'
                            f'<pre>{_e(b["text"])}</pre>{path}</details>')
        if p.get("diff_url"):
            body.append(f'<a class="dlink" href="{_e(p["diff_url"])}" target="_blank" rel="noopener">'
                        f'Open staged diff <span class="ext">↗</span></a>')
        rows.append(f'<div class="pk" data-n="{_e(p["n"])}">{"".join(body)}</div>')
    return f'<div class="dots">{dots}</div><div class="tl">{"".join(rows)}</div>'


def _commands(ex, relay_bin):
    sid = ex["session_id"]
    st = ex.get("status")
    acts = []
    if st == "reported":
        acts.append(("Verify report", f"{relay_bin} verify {sid}", True))
    elif st in ("closed", "dead", "superseded", "launch-failed"):
        acts.append(("Resume", f"{relay_bin} resume {sid}", True))
    acts.append(("Focus tab", f"{relay_bin} focus {sid}", False))
    acts.append(("Send follow-up", f"{relay_bin} send {sid} <packet.md>", False))
    if st not in ("closed", "superseded"):
        acts.append(("Close", f"{relay_bin} close {sid}", False))
    chips = "".join(
        f'<button class="cmd{" go" if go else ""}" data-cmd="{_e(cmd)}" title="Copies: {_e(cmd)}">'
        f'{_e(label)}<span class="ci">\u2398</span></button>' for label, cmd, go in acts)
    return f'<div class="cmd-h lbl">Actions · click to copy the command</div><div class="cmds">{chips}</div>'


def _exec_panel(ex, relay_bin, lead_name, show=False):
    sk = _status_key(ex.get("status"))
    label = ex.get("rendered_status") or ex.get("status") or "?"
    topic = _e(ex.get("topic"))
    if ex.get("scope") and ex.get("scope") != ex.get("topic"):
        topic += f' · {_e(ex.get("scope"))}'
    return (
        f'<section class="panel{" show" if show else ""}" id="ex-{_e(ex["session_id"])}">'
        f'<span class="crumb" data-target="home">← overview</span> <span class="muted" style="font-size:12px">· {_e(lead_name)}</span>'
        f'<div class="exhead"><h1>{_e(ex["session_id"])}</h1><span class="pill {sk}">{_e(label)}</span></div>'
        f'<div class="muted">{topic}</div>'
        f'<div class="chips">{_chips(ex)}</div>'
        f'{_packets(ex)}'
        f'{_commands(ex, relay_bin)}'
        "</section>")


def _lead_dot(m):
    color = m.get("color")
    if color and len(color) == 3:
        return f'<span class="cdot" style="background:rgb({int(color[0])},{int(color[1])},{int(color[2])})"></span>'
    return '<span class="cdot" style="background:var(--dim)"></span>'


def render(data):
    relay_bin = data.get("relay_bin") or "relay"
    leads = data.get("leads") or []
    execs = data.get("executors") or []
    by_lead = {}
    for ex in execs:
        by_lead.setdefault(ex.get("owner_lead"), []).append(ex)
    lead_names = {m.get("session_id"): (m.get("project") or m.get("session_id")) for m in leads}
    terminal = ("closed", "superseded", "dead", "launch-failed")
    live = [e for e in execs if e.get("status") not in terminal]
    reported = [e for e in live if e.get("status") == "reported"]
    busy = [e for e in live if e.get("status") in ("busy", "stalled")]
    closed = [e for e in execs if e.get("status") in terminal]
    warnings = data.get("warnings") or []

    kpis = (f'<div class="kpi rev"><b class="num">{len(reported)}</b><span>to review</span></div>'
            f'<div class="kpi busy"><b class="num">{len(busy)}</b><span>working</span></div>'
            f'<div class="kpi"><b class="num">{len(live)}</b><span>live</span></div>'
            f'<div class="kpi"><b class="num">{len(leads)}</b><span>leads</span></div>')
    if warnings:
        kpis += f'<div class="kpi bad"><b class="num">{len(warnings)}</b><span>alerts</span></div>'
    top = (f'<div class="top"><div class="brand">\U0001F6A6 relay</div><div class="kpis">{kpis}</div>'
           f'<div class="grow"></div><input id="q" class="search" placeholder="Filter executors…">'
           f'<button class="tbtn" id="tbtn" onclick="toggleTheme()">☾</button></div>')

    rail = ['<nav class="side">']
    if reported:
        rail.append('<details class="grp rev" open><summary><span class="cdot" style="background:var(--ok)"></span>'
                    f'<span class="gl">Needs review</span><span class="count num">{len(reported)}</span></summary>'
                    + "".join(_rail_item(e) for e in reported) + "</details>")
    for m in leads:
        sid = m.get("session_id")
        mine = [e for e in by_lead.get(sid, []) if e.get("status") not in terminal]
        wake = m.get("wake")
        wchip = f'<span class="wake {"bad" if wake in ("stale", "stuck") else ""}">{_e(wake)}</span>' if wake and wake != "ok" else ""
        liveclass = "gl lv-bad" if m.get("liveness") in ("unreachable", "ghost") else "gl"
        rail.append(f'<details class="grp" open><summary>{_lead_dot(m)}'
                    f'<span class="{liveclass}">{_e(m.get("project") or sid)}</span>'
                    f'{wchip}<span class="count num">{len(mine)}</span></summary>'
                    + ("".join(_rail_item(e) for e in mine) or '<div class="muted" style="padding:5px 10px;font-size:12px">no live executors</div>')
                    + "</details>")
    unowned = by_lead.get(None, [])
    lead_ids = set(lead_names)
    orphaned = [ex for k, v in by_lead.items() if k is not None and k not in lead_ids for ex in v]
    other = [e for e in (unowned + orphaned) if e.get("status") not in terminal]
    if other:
        rail.append('<details class="grp" open><summary><span class="cdot" style="background:var(--bad)"></span>'
                    f'<span class="gl">Unowned / orphaned</span><span class="count num">{len(other)}</span></summary>'
                    + "".join(_rail_item(e) for e in other) + "</details>")
    if closed:
        rail.append('<details class="grp"><summary><span class="cdot" style="background:var(--dim)"></span>'
                    f'<span class="gl">Closed</span><span class="count num">{len(closed)}</span></summary>'
                    + "".join(_rail_item(e) for e in closed) + "</details>")
    rail.append("</nav>")

    tiles = (f'<div class="tiles">'
             f'<div class="tile rev"><b class="num">{len(reported)}</b><span>reported · awaiting review</span></div>'
             f'<div class="tile busy"><b class="num">{len(busy)}</b><span>busy / stalled</span></div>'
             f'<div class="tile"><b class="num">{len(live)}</b><span>live executors</span></div>'
             f'<div class="tile"><b class="num">{len(leads)}</b><span>leads</span></div>'
             f'<div class="tile"><b class="num">{len(closed)}</b><span>closed / dead</span></div></div>')
    default_id = "ex-" + reported[0]["session_id"] if reported else ("ex-" + live[0]["session_id"] if live else "home")
    home_cls = "panel show" if default_id == "home" else "panel"
    home = [f'<section class="{home_cls}" id="home"><h1 class="h1">Overview</h1>',
            f'<div class="sub-h">generated {_e(data.get("generated"))} · relay {_e(data.get("plugin_version") or "?")} · click an executor to drill in</div>',
            tiles]
    for w in warnings:
        ic = {"bad": "⛔", "warn": "⚠︎", "info": "\U0001F4A1"}.get(w.get("level"), "•")
        home.append(f'<div class="banner {_e(w.get("level") or "warn")}"><span class="ic">{ic}</span><span>{_e(w.get("text"))}</span></div>')
    if reported:
        home.append('<div class="hint lbl">Needs review</div><div class="rev-list">')
        for e in reported:
            oc = ((e.get("packets") or [{}])[-1].get("tldr") or {}).get("outcome") if e.get("packets") else None
            home.append(f'<div class="rev-card" data-target="ex-{_e(e["session_id"])}">'
                        f'<span class="sdot reported"></span><span class="nm">{_e(e["session_id"])}</span>'
                        f'<span class="oc">{_e(oc or e.get("topic"))}</span>'
                        f'<span class="chip">{_e(e.get("tokens") or "")}</span></div>')
        home.append("</div>")
    if not execs:
        home.append('<div class="empty">no executor sessions yet — <span class="mono">relay spawn …</span></div>')
    home.append("</section>")

    panels = "".join(_exec_panel(e, relay_bin, lead_names.get(e.get("owner_lead"), "unowned"),
                                 show=(f'ex-{e["session_id"]}' == default_id)) for e in execs)

    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>relay board</title><style>" + _CSS + "</style></head><body>"
            + f'<div id="board" data-default="{_e(default_id)}">' + top + '<div class="app">' + "".join(rail) + '<main class="detail">'
            + "".join(home) + panels + "</main></div></div><script>" + _JS + "</script></body></html>")
