"""
relay board — a self-contained HTML snapshot of everything relay knows: leads → their executors →
each executor's packet timeline (gist, report outcome, TL;DR), with status, launch flags, tokens,
warnings, and copyable commands. Pure: `render(data) -> html`. The data dict is assembled by
bin/relay's cmd_board from the same functions `relay list` uses, so the page can never disagree
with the table.

Design: light by default, dark via a toggle persisted in localStorage (the page is opened from a
terminal, so it must not depend on the OS theme — the lead picked light). No external assets.
"""
import html
import json

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;
--ok:#15803d;--okbg:#dcfce7;--warn:#b45309;--warnbg:#fef3c7;--bad:#b91c1c;--badbg:#fee2e2;
--dim:#9ca3af;--dimbg:#f3f4f6;--busy:#92400e;--busybg:#ffedd5;--chip:#eef2ff;--chipink:#3730a3;--shadow:0 1px 2px rgba(0,0,0,.06)}
[data-theme=dark]{--bg:#0f1115;--card:#171a21;--ink:#e6e8ec;--muted:#9aa3b2;--line:#2a2f3a;--accent:#7aa2ff;
--ok:#4ade80;--okbg:#14301f;--warn:#fbbf24;--warnbg:#3a2a07;--bad:#f87171;--badbg:#3b1212;
--dim:#6b7280;--dimbg:#1f242e;--busy:#fdba74;--busybg:#3a2410;--chip:#1e2438;--chipink:#b9c5ff;--shadow:none}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Inter,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;box-shadow:var(--shadow)}
header h1{font-size:17px;margin:0;font-weight:650}header .meta{color:var(--muted);font-size:12px}
header input{flex:1;min-width:220px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 10px;cursor:pointer;font-size:12px}
.btn.on{background:var(--chip);color:var(--chipink);border-color:transparent}
main{max-width:1280px;margin:0 auto;padding:18px 20px 60px}
.banner{border-radius:10px;padding:10px 14px;margin:0 0 12px;border:1px solid transparent}
.banner.warn{background:var(--warnbg);color:var(--warn)}.banner.bad{background:var(--badbg);color:var(--bad)}.banner.info{background:var(--chip);color:var(--chipink)}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:110px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:20px;font-weight:650}.stat span{color:var(--muted);font-size:12px}
.lead{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 0 16px;box-shadow:var(--shadow);overflow:hidden}
.lead>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;border:1px solid var(--line);vertical-align:middle;margin-right:6px}
.lead>summary::-webkit-details-marker{display:none}
.lead h2{margin:0;font-size:15px;font-weight:650}.lead .sid{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.pill{display:inline-block;border-radius:999px;padding:1px 9px;font-size:11px;font-weight:600;line-height:18px;white-space:nowrap}
.p-ok{background:var(--okbg);color:var(--ok)}.p-warn{background:var(--warnbg);color:var(--warn)}.p-bad{background:var(--badbg);color:var(--bad)}
.p-dim{background:var(--dimbg);color:var(--dim)}.p-busy{background:var(--busybg);color:var(--busy)}.p-chip{background:var(--chip);color:var(--chipink)}
table{width:100%;border-collapse:collapse}th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:6px 10px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.02em}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
tr.ex{cursor:pointer}tr.ex:hover{background:var(--bg)}tr.detail>td{background:var(--bg);padding:10px 14px 14px 30px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.tl{margin:6px 0 0;padding:0;list-style:none}.tl li{padding:8px 10px;border-left:2px solid var(--line);margin:0 0 6px;background:var(--card);border-radius:0 8px 8px 0}
.tl .n{font-weight:650;margin-right:6px}.tl .gist{color:var(--ink)}.tl .out{display:block;margin-top:3px}.tl .tldr{color:var(--muted);font-size:12px;margin-top:3px}
.cmds{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.cmd{font-family:ui-monospace,Menlo,monospace;font-size:11px;border:1px dashed var(--line);border-radius:6px;padding:3px 8px;cursor:pointer;color:var(--muted)}
.cmd:hover{color:var(--ink);border-style:solid}.muted{color:var(--muted)}.small{font-size:12px}
.hidden{display:none}footer{color:var(--muted);font-size:12px;margin-top:30px}
.ledger{font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;max-height:260px}
"""

_JS = """
(function(){
var root=document.documentElement,key='relay-board-theme';
try{var t=localStorage.getItem(key);if(t==='dark')root.setAttribute('data-theme','dark');}catch(e){}
window.toggleTheme=function(){var d=root.getAttribute('data-theme')==='dark';if(d)root.removeAttribute('data-theme');else root.setAttribute('data-theme','dark');try{localStorage.setItem(key,d?'light':'dark')}catch(e){}document.getElementById('themebtn').textContent=d?'🌙 Dark':'☀️ Light';};
document.addEventListener('DOMContentLoaded',function(){
 var d=root.getAttribute('data-theme')==='dark';document.getElementById('themebtn').textContent=d?'☀️ Light':'🌙 Dark';
 var q=document.getElementById('q');q.addEventListener('input',function(){var v=q.value.toLowerCase();
  document.querySelectorAll('tr.ex').forEach(function(tr){var hit=!v||tr.dataset.hay.indexOf(v)>=0;tr.classList.toggle('hidden',!hit);var det=tr.nextElementSibling;if(det&&det.classList.contains('detail')&&!hit)det.classList.add('hidden');});
  document.querySelectorAll('details.lead').forEach(function(dl){var any=dl.querySelectorAll('tr.ex:not(.hidden)').length>0;dl.classList.toggle('hidden',!any&&v.length>0);});});
 document.querySelectorAll('tr.ex').forEach(function(tr){tr.addEventListener('click',function(e){if(e.target.closest('a,.cmd'))return;var det=tr.nextElementSibling;if(det&&det.classList.contains('detail'))det.classList.toggle('hidden');});});
 document.querySelectorAll('.cmd').forEach(function(el){el.addEventListener('click',function(){var t=el.dataset.cmd||el.textContent;navigator.clipboard&&navigator.clipboard.writeText(t);var o=el.textContent;el.textContent='copied ✓';setTimeout(function(){el.textContent=o},900);});});
 var sc=document.getElementById('showclosed');if(sc)sc.addEventListener('click',function(){var on=sc.classList.toggle('on');document.querySelectorAll('.closed-row').forEach(function(r){r.classList.toggle('hidden',!on);});});
});})();
"""


def _e(x):
    return html.escape("" if x is None else str(x), quote=True)


def _status_pill(s):
    st = s.get("status") or "?"
    label = s.get("rendered_status") or st
    cls = {"reported": "p-ok", "busy": "p-busy", "stalled": "p-bad", "dead": "p-bad", "launch-failed": "p-bad",
           "closed": "p-dim", "superseded": "p-dim"}.get(st, "p-dim")
    return f'<span class="pill {cls}">{_e(label)}</span>'


def _lead_pills(m):
    out = []
    live = m.get("liveness") or "?"
    out.append(f'<span class="pill {"p-ok" if live == "live" else ("p-warn" if live == "unreachable" else "p-dim")}">{_e(live)}</span>')
    wake = m.get("wake") or "?"
    out.append(f'<span class="pill {"p-ok" if wake == "ok" else ("p-bad" if wake in ("stale", "stuck") else "p-dim")}">wake {_e(wake)}</span>')
    if m.get("auto"):
        out.append('<span class="pill p-warn">AUTO</span>')
    if m.get("paused"):
        out.append('<span class="pill p-dim">⏸ paused</span>')
    return " ".join(out)


def _packet_timeline(ex):
    items = []
    for p in ex.get("packets") or []:
        t = p.get("tldr") or {}
        links = []
        if p.get("packet_path"):
            links.append(f'<a href="{_e(p["packet_url"])}">packet</a>')
        if p.get("report_path"):
            links.append(f'<a href="{_e(p["report_url"])}">report</a>')
        if p.get("diff_path"):
            links.append(f'<a href="{_e(p["diff_url"])}">diff</a>')
        outcome = t.get("outcome")
        tl = []
        if t.get("status"):
            tl.append(f"Status: {_e(t['status'])}")
        if t.get("risk"):
            tl.append(f"Risk: {_e(t['risk'])}")
        if t.get("unverified"):
            tl.append(f"UNVERIFIED: {_e(t['unverified'])}")
        state = ('<span class="pill p-ok">reported</span>' if p.get("report_path")
                 else ('<span class="pill p-busy">in flight</span>' if p.get("current") else '<span class="pill p-warn">no report</span>'))
        items.append(
            f'<li><span class="n">#{_e(p["n"])}</span>{state} <span class="gist">{_e(p.get("gist"))}</span>'
            + (f'<span class="out">↳ {_e(outcome)}</span>' if outcome else "")
            + (f'<div class="tldr">{" · ".join(tl)}</div>' if tl else "")
            + (f'<div class="small" style="margin-top:4px">{" · ".join(links)}</div>' if links else "")
            + "</li>")
    return f'<ul class="tl">{"".join(items)}</ul>' if items else '<div class="muted small">no packets on disk</div>'


def _cmds(ex, relay_bin="relay"):
    sid = ex["session_id"]
    cmds = [f"{relay_bin} check {sid}", f"{relay_bin} focus {sid}", f"{relay_bin} diff {sid} --open",
            f"{relay_bin} send {sid} <packet.md>"]
    if ex.get("status") in ("closed", "dead", "superseded"):
        cmds.append(f"{relay_bin} resume {sid}")
    elif ex.get("status") == "reported":
        cmds.append(f"{relay_bin} verify {sid}")
    cmds.append(f"{relay_bin} close {sid}" if ex.get("status") not in ("closed", "superseded") else f"{relay_bin} restart {sid}")
    return '<div class="cmds">' + "".join(f'<span class="cmd" data-cmd="{_e(c)}">{_e(c)}</span>' for c in cmds) + "</div>"


def _exec_rows(execs, relay_bin):
    rows = []
    for ex in execs:
        terminal = ex.get("status") in ("closed", "superseded", "dead", "launch-failed")
        hay = " ".join(str(ex.get(k) or "") for k in ("session_id", "topic", "scope", "status", "model", "launch", "owner_project", "worktree")).lower()
        flags = []
        if ex.get("heavy"):
            flags.append('<span class="pill p-warn">heavy</span>')
        if ex.get("keep"):
            flags.append('<span class="pill p-chip">📌 pinned</span>')
        if ex.get("queued"):
            flags.append(f'<span class="pill p-warn">📥 {ex["queued"]} queued</span>')
        if ex.get("auto_closed"):
            flags.append(f'<span class="pill p-dim">auto: {_e(ex["auto_closed"])}</span>')
        if ex.get("unannounced"):
            flags.append('<span class="pill p-bad">🚦 report not yet delivered</span>')
        if ex.get("orphan"):
            flags.append('<span class="pill p-bad">orphan</span>')
        rows.append(
            f'<tr class="ex{" closed-row hidden" if terminal else ""}" data-hay="{_e(hay)}">'
            f'<td><b>{_e(ex["session_id"])}</b><div class="muted small">{_e(ex.get("topic"))}' + (f' · {_e(ex.get("scope"))}' if ex.get("scope") and ex.get("scope") != ex.get("topic") else "") + '</div></td>'
            f'<td>{_status_pill(ex)}<div style="margin-top:4px">{" ".join(flags)}</div></td>'
            f'<td class="mono">{_e(ex.get("model"))}<div class="muted">{_e(ex.get("launch"))}</div></td>'
            f'<td class="mono">{_e(ex.get("tokens") or "-")}<div class="muted">{_e(ex.get("mb") or "-")} MB</div></td>'
            f'<td>{_e(ex.get("pkt"))}<div class="muted small">{"report ✓" if ex.get("reported") else "no report"}</div></td>'
            f'<td class="muted small">{_e(ex.get("updated") or "")}</td>'
            "</tr>"
            f'<tr class="detail hidden{" closed-row" if terminal else ""}"><td colspan="6">'
            f'<div class="small muted mono">worktree {_e(ex.get("worktree"))} · claude_session {_e(ex.get("claude_session") or "-")} · created {_e(ex.get("created") or "-")}</div>'
            + _packet_timeline(ex) + _cmds(ex, relay_bin) + "</td></tr>")
    return "".join(rows)


def _exec_table(execs, relay_bin):
    if not execs:
        return '<div class="muted small" style="padding:10px 16px">no executors</div>'
    return ('<table><thead><tr><th>executor</th><th>status</th><th>model / launch</th><th>tokens / MB</th><th>pkt</th><th>updated</th></tr></thead>'
            f'<tbody>{_exec_rows(execs, relay_bin)}</tbody></table>')


def render(data):
    relay_bin = data.get("relay_bin") or "relay"
    leads = data.get("leads") or []
    execs = data.get("executors") or []
    by_lead = {}
    for ex in execs:
        by_lead.setdefault(ex.get("owner_lead"), []).append(ex)
    live = [e for e in execs if e.get("status") not in ("closed", "superseded", "dead", "launch-failed")]
    n_rep = sum(1 for e in live if e.get("status") == "reported")
    n_busy = sum(1 for e in live if e.get("status") in ("busy", "stalled"))
    warnings = data.get("warnings") or []
    parts = [f"<style>{_CSS}</style><script>{_JS}</script>",
             '<header><h1>🚦 relay board</h1>',
             f'<span class="meta">generated {_e(data.get("generated"))} · relay {_e(data.get("plugin_version") or "?")} · {_e(data.get("state_root"))}</span>',
             '<input id="q" placeholder="filter executors: name, topic, status, model, worktree…">',
             '<button class="btn" id="showclosed">show closed</button>',
             '<button class="btn" id="themebtn" onclick="toggleTheme()">🌙 Dark</button></header><main>']
    parts.append('<div class="summary">'
                 f'<div class="stat"><b>{len(leads)}</b><span>leads</span></div>'
                 f'<div class="stat"><b>{len(live)}</b><span>live executors</span></div>'
                 f'<div class="stat"><b>{n_busy}</b><span>busy / stalled</span></div>'
                 f'<div class="stat"><b>{n_rep}</b><span>reported · awaiting review</span></div>'
                 f'<div class="stat"><b>{len(execs) - len(live)}</b><span>closed / dead</span></div></div>')
    for w in warnings:
        parts.append(f'<div class="banner {_e(w.get("level") or "warn")}">{_e(w.get("text"))}</div>')
    for m in leads:
        sid = m.get("session_id")
        color = m.get("color")
        dot = (f'<span class="dot" title="iTerm tab color" style="background:rgb({int(color[0])},{int(color[1])},{int(color[2])})"></span>'
               if color and len(color) == 3 else "")
        mine = by_lead.get(sid, [])
        parts.append(
            f'<details class="lead" open><summary><h2>{dot}{_e(m.get("project") or "(no project)")}</h2>'
            f'{_lead_pills(m)}<span class="sid">{_e(sid)}</span>'
            f'<span class="muted small">model {_e(m.get("model") or "-")} · v{_e(m.get("plugin_version") or "?")} · last active {_e(m.get("last_active_age") or m.get("last_active") or "-")} · {_e(m.get("mb") or "-")} MB'
            f' · {len([e for e in mine if e.get("status") not in ("closed", "superseded", "dead")])} live exec(s)</span>'
            f'<span class="muted small mono" style="flex-basis:100%">{_e(m.get("cwd") or "")}</span></summary>'
            + _exec_table(mine, relay_bin)
            + f'<div class="cmds" style="padding:8px 16px 12px"><span class="cmd" data-cmd="{relay_bin} list --lead {_e(sid)}">{relay_bin} list --lead {_e(sid)}</span>'
              f'<span class="cmd" data-cmd="{relay_bin} focus {_e(sid)}">{relay_bin} focus {_e(sid)}</span>'
              f'<span class="cmd" data-cmd="{relay_bin} resume {_e(sid)}">{relay_bin} resume {_e(sid)}</span></div></details>')
    unowned = by_lead.get(None, [])
    orphaned = [ex for k, v in by_lead.items() if k is not None and k not in {m.get("session_id") for m in leads} for ex in v]
    if unowned or orphaned:
        parts.append('<details class="lead" open><summary><h2>Unowned / orphaned executors</h2>'
                     '<span class="muted small">no live lead owns these — `relay send`/`resume` adopt automatically, or `relay adopt &lt;sid&gt;`</span></summary>'
                     + _exec_table(unowned + orphaned, relay_bin) + "</details>")
    if data.get("ledger_tail"):
        parts.append('<h3 class="muted" style="font-size:13px;margin:22px 0 6px">recent ledger</h3><div class="ledger">'
                     + "\n".join(_e(l) for l in data["ledger_tail"]) + "</div>")
    parts.append(f'<footer>static snapshot — re-run <span class="mono">{_e(relay_bin)} board --open</span> to refresh. '
                 'Click an executor row for its packet timeline and copyable commands; links open the packet / report / diff files.</footer></main>')
    return "<!doctype html><html><head><meta charset=\"utf-8\"><title>relay board</title></head><body>" + "".join(parts) + "</body></html>"
