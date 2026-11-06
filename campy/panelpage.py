"""
campy-session panel's page (campy/panel.py serves it): one HTML file with its
CSS and JavaScript, no build step and nothing fetched from elsewhere, so it
works on a rig without internet. It asks the panel for /status.json once a
second, and each camera's thumbnail (/thumb/<n>.png) once a second, with the
key from its own address on every request.
"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>campy session</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--soft:#f0eee8;--text:#1f1e1c;--muted:#6b6a65;--line:#e2e0d9;--ok:#2f6d12;--okbg:#e6f1dc;
--warn:#80500b;--warnbg:#fbefd8;--bad:#a32d2d;--badbg:#fbe9e9;--accent:#185fa5}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1a;--card:#252524;--soft:#2e2e2c;--text:#ecebe6;--muted:#a3a29c;
--line:#3a3a37;--ok:#9fd27a;--okbg:#22351a;--warn:#f2c46f;--warnbg:#3a2c10;--bad:#f2a0a0;--badbg:#3d1d1d;--accent:#85b7eb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:200px}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px}.ok{background:var(--okbg);color:var(--ok)}
.warn{background:var(--warnbg);color:var(--warn)}.bad{background:var(--badbg);color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.tile{background:var(--soft);border-radius:8px;padding:10px 12px}.big{font-size:20px;font-weight:500}
.cams{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.cam img{width:100%;aspect-ratio:5/4;object-fit:contain;background:var(--soft);border-radius:6px;display:block}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button:hover{background:var(--soft)}button.stop{color:var(--bad);border-color:var(--bad)}button.go{color:var(--ok);border-color:var(--ok)}
input{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
label{font-size:12px;color:var(--muted)}h2{font-size:15px;font-weight:500;margin:0 0 6px}
.bar{height:6px;background:var(--soft);border-radius:3px;overflow:hidden}.bar div{height:6px;background:var(--ok)}
.err{color:var(--bad);font-size:13px}ul{margin:4px 0 0;padding-left:18px}li{margin:2px 0}
select{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
.steps li{list-style:none;margin-left:-18px}.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:16px}
.modal .card{max-width:520px}[hidden]{display:none!important}
</style></head><body><main>

<div class="card row">
  <div class="grow"><div id="rig" style="font-weight:500">campy session</div><div id="where" class="mono muted"></div></div>
  <span id="state" class="tag">connecting</span>
  <div style="min-width:180px"><div id="clock"></div><div class="bar"><div id="progress" style="width:0"></div></div></div>
  <button id="stop" class="stop" hidden>Stop</button>
  <button id="close" hidden title="Close the panel (nothing is recording)">Close the panel</button>
</div>
<div id="error" class="err" hidden></div>

<div id="before" class="card" hidden>
  <h2>Before the start</h2>
  <div class="two">
    <div>
      <div id="lengthbox" hidden><label for="length">Session length</label><select id="length"></select>
        <div class="muted" id="plantext"></div>
        <label for="picoat">Start the Pico at (optional: otherwise a button, once every camera is ready)</label><input id="picoat" placeholder="7:20 PM"></div>
      <label for="experiment">Experiment</label><input id="experiment" placeholder="Social hierarchy, week 3">
      <label for="ids">Animal IDs, comma-separated</label><input id="ids" placeholder="M12, M14, M15">
      <label for="notes">Notes</label><input id="notes" placeholder="Cage 2, new bedding">
    </div>
    <div><div id="orphan" class="err" hidden></div><button id="stoporphan" class="stop" hidden>Stop that recording</button>
      <ul id="checks"></ul><p class="muted" id="length"></p><button id="start" class="go">Start recording</button>
      <div id="starterr" class="err"></div></div>
  </div>
</div>

<div id="left" class="card" hidden><h2>Still running</h2><div id="lefttext" class="err"></div>
  <div class="row" style="margin-top:8px"><button id="stoppico" class="stop" hidden>Stop the Pico</button><button id="stopsglx" class="stop" hidden>Stop SpikeGLX's run</button></div></div>

<div id="devs" class="two" hidden>
  <div class="card"><h2>Steps</h2><ul id="steps" class="steps"><li class="muted">At Start: SpikeGLX's run, the gate check, the cameras, then the Pico.</li></ul></div>
  <div class="card"><h2 id="picotitle">Pico</h2><div id="picostate" class="muted">-</div>
    <div id="picostart" class="row" style="margin-top:8px" hidden><button id="startpico" class="go">Start the Pico</button>
      <input id="picoat2" style="width:110px" placeholder="7:20 PM"><button id="picoatbtn">At this time</button></div>
    <div id="picoerr" class="err"></div>
    <h2 style="margin-top:10px">SpikeGLX</h2><div id="sglxstate" class="muted">-</div></div>
</div>

<div id="during" hidden>
  <div class="tiles" style="margin-bottom:12px">
    <div class="tile"><label>Disk free</label><div class="big" id="disk">-</div><div class="muted" id="diskleft"></div></div>
    <div class="tile"><label>Tracked in 3D</label><div class="big" id="good3d">-</div><div class="muted">animals, last second</div></div>
    <div class="tile"><label>Programs</label><div id="programs"></div></div>
    <div class="tile"><label>Bookmarks</label><div class="big" id="nmarks">0</div><div class="muted" id="lastmark"></div></div>
  </div>
  <div class="cams" id="cams" style="margin-bottom:12px"></div>
  <div class="two">
    <div class="card"><h2>Warnings</h2><ul id="warnings"><li class="muted">None</li></ul></div>
    <div class="card"><h2>Bookmark</h2>
      <div class="row"><input id="note" class="grow" placeholder="Mouse 3 rears at the feeder"><button id="mark">Add</button></div>
      <ul id="marks"></ul><div id="markerr" class="err"></div>
      <div style="margin-top:8px"><button id="viewer">Open the viewer again</button></div></div>
  </div>
</div>

<div id="after" class="card" hidden><h2 id="aftertitle">After the recording</h2><ul id="post"></ul>
  <p class="muted">Closing this page never stops a recording or its steps. The panel is ready for the next recording.</p></div>

<div id="stopdlg" class="modal" hidden><div class="card"><h2>End the recording?</h2>
  <p class="muted" id="stoptext"></p>
  <div class="row"><button id="keep">Keep recording</button><button id="stopbout" class="stop">Stop after this bout</button><button id="stopnow" class="stop">Stop now</button></div></div></div>
<div id="picodlg" class="modal" hidden><div class="card"><h2>Start the Pico?</h2>
  <p class="muted" id="picotext"></p>
  <div class="row"><button id="picono">Not yet</button><button id="picoyes" class="go">Start the Pico</button></div></div></div>

</main><script>
const key = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id);
let thumbs = 0, last = null, stopArmed = 0;
function q(path){ return path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(key); }
async function post(path, body){
  const r = await fetch(q(path), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body||{})});
  const j = await r.json().catch(() => ({})); return j.error || null;
}
function hms(s){ if(s==null) return "-"; s=Number(s); if(!isFinite(s)) return s>0 ? "\u221e" : "-"; s=Math.max(0,Math.round(s)); return Math.floor(s/3600)+":"+String(Math.floor(s/60)%60).padStart(2,"0")+":"+String(s%60).padStart(2,"0"); }
function esc(t){ return String(t==null?"":t).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function tag(text, kind){ return '<span class="tag '+kind+'">'+esc(text)+'</span>'; }
function render(s){
  last = s;
  const L = s.launcher || {}, rec = L.recording || {}, H = s.health || {};
  $("rig").textContent = s.rig + " · " + s.config.split(/[\\/]/).pop();
  $("where").textContent = L.folder || s.config;
  const st = s.state, live = ["checking","starting","preparing","starting live","recording","stopping","post"].includes(st);
  $("state").className = "tag " + (st=="recording" ? "ok" : st=="failed" ? "bad" : live ? "warn" : "");
  $("state").textContent = {ready:"Ready", starting:"Starting", checking:"Checking", preparing:"Starting SpikeGLX, gate check", "starting live":"Starting campy-live",
    recording:"Recording", stopping:"Stopping", post:"After the recording", done:"Done", failed:"Failed"}[st] || st;
  $("stop").hidden = !(st=="recording" || st=="stopping");
  $("close").hidden = !(st=="ready" || st=="done" || st=="failed");
  renderDevices(s, st);
  $("stop").textContent = stopArmed ? "Stop now, without closing" : (rec.stop_asked ? "Stopping…" : "Stop");
  const DP = s.devices && s.devices.plan ? s.devices.pico : null;
  if(DP){ // a Pico's session: its time against its plan (campy's recTimeInSec is camera time, not the clock's)
    const on = DP.alive && s.devices.pico_started && DP.mode != "wiring";
    $("clock").innerHTML = on ? "<b>"+hms(DP.session_s)+"</b> <span class=muted>of "+hms(DP.ends_s)+", the Pico's</span>" : (live ? "<span class=muted>the Pico hasn't started</span>" : "");
    $("progress").style.width = on && DP.ends_s ? Math.min(100, 100*DP.session_s/DP.ends_s)+"%" : "0";
  } else {
  $("clock").innerHTML = rec.elapsed_s!=null ? "<b>"+hms(rec.elapsed_s)+"</b> <span class=muted>of "+hms(rec.planned_s)+"</span>" : "";
  $("progress").style.width = rec.elapsed_s && rec.planned_s ? Math.min(100, 100*rec.elapsed_s/rec.planned_s)+"%" : "0";
  }
  $("error").hidden = !s.error; $("error").textContent = s.error || "";
  $("before").hidden = !(st=="ready" || st=="done" || st=="failed");
  $("orphan").hidden = $("stoporphan").hidden = !s.orphan;
  if(s.orphan) $("orphan").textContent = "A recording an earlier panel started is still running, in " + s.orphan.folder +
    " (its window was closed; the recording went on). Stop it, or wait for it to end, before starting another.";
  $("checks").innerHTML = s.checks.map(c => "<li>"+tag(c.level=="block"?"stops the start":c.level=="warn"?"warning":"ok",
    c.level=="block"?"bad":c.level=="warn"?"warn":"ok")+" "+esc(c.text)+(c.action=="pause-updates"?' <button onclick="pauseUpdates(this)">Pause Windows Update</button>':"")+"</li>").join("");
  const lengths = s.lengths || []; $("lengthbox").hidden = !lengths.length;
  if(lengths.length && $("length").options.length != lengths.length){ $("length").innerHTML = lengths.map(l => '<option value="'+l.seconds+'">'+esc(l.name)+'</option>').join(""); }
  if(lengths.length && document.activeElement != $("length")) $("length").value = String(s.length);
  const L0 = lengths.find(l => l.seconds == s.length); $("plantext").textContent = L0 ? L0.text + (L0.videoFolder ? ", into "+L0.videoFolder : "") : "";
  if(!$("experiment").dataset.set){ $("experiment").value = s.fields.experiment||""; $("ids").value = s.fields.animal_ids||""; $("experiment").dataset.set=1; }
  $("during").hidden = !(st=="recording" || st=="stopping" || st=="starting live");
  $("disk").textContent = s.disk.free_gb!=null ? Math.round(s.disk.free_gb)+" GB" : "-";
  $("diskleft").textContent = s.disk.hours_left!=null ? "about "+s.disk.hours_left+" h at this rate" : "";
  const T = H.tracking; $("good3d").textContent = T && T.good3d!=null ? T.good3d+" of "+T.animals : "-";
  const parts = Object.assign({}, L.parts||{}); if((L.live||[]).length) parts["campy-live"] = L.live[L.live.length-1];
  $("programs").innerHTML = Object.entries(parts).map(([n,p]) => tag(p.state, p.state=="running"?"ok":p.exit_code?"bad":"warn")+" "+esc(n=="acquire"?"campy-acquire":n=="view"?"viewer":n=="watch"?"campy-watch":n)).join("<br>") || "-";
  const R = s.recording || {}, counters = {}; (R.cameras||[]).forEach(c => counters[c.name] = c);
  const cams = H.cameras || [];
  if(cams.length != thumbs){ thumbs = cams.length; $("cams").innerHTML = cams.map((c,i) => '<div class="card cam"><img id="th'+i+'" alt="'+esc(c.name)+'"><div id="cm'+i+'"></div></div>').join(""); }
  cams.forEach((c,i) => { const k = counters[c.name] || {};
    $("cm"+i).innerHTML = '<div class="row" style="margin-top:6px"><b class="grow">'+esc(c.name)+'</b>'+(c.problems.length ? c.problems.map(p => tag(p,"warn")).join(" ") : tag("OK","ok"))+'</div>'+
      '<div class="muted">'+(c.fps!=null?c.fps.toFixed(1):"-")+' frames/s · frame '+(c.frame||0).toLocaleString()+
      (k.dropped!=null?' · '+k.dropped+' dropped':'')+(k.write_queue_s!=null?' · queue '+k.write_queue_s.toFixed(1)+' s':'')+'</div>'; });
  const warn = [];
  (s.alerts.open||[]).forEach(a => warn.push(tag(a.severity||"now", a.severity=="critical"?"bad":"warn")+" "+esc(a.text)));
  cams.forEach(c => c.sentences.forEach(t => warn.push(tag("now","warn")+" "+esc(t))));
  ((T||{}).alerts||[]).forEach(t => warn.push(tag("3D","warn")+" "+esc(t)));
  (s.problems||[]).slice(-3).forEach(p => warn.push(tag(p.time.slice(11,19),"bad")+" "+esc(p.text)));
  (s.alerts.resolved||[]).forEach(a => warn.push('<span class="muted">'+esc(a.time.slice(11,19))+" resolved: "+esc(a.text)+"</span>"));
  $("warnings").innerHTML = warn.length ? warn.map(w => "<li>"+w+"</li>").join("") : '<li class="muted">None</li>';
  $("nmarks").textContent = s.bookmarks.length ? s.bookmarks[0].number : 0;
  $("lastmark").textContent = s.bookmarks.length ? "last at "+s.bookmarks[0].time : "";
  $("marks").innerHTML = s.bookmarks.map(m => "<li class=muted>"+m.number+" at "+m.time+", frame "+(m.frame!=null?m.frame.toLocaleString():"-")+(m.note?": "+esc(m.note):"")+"</li>").join("");
  const P = L.post || {};
  $("after").hidden = !(st=="post" || (["done","failed","ready"].includes(st) && L.folder));
  $("aftertitle").textContent = st=="post" ? "After the recording" : "The last recording: " + (L.state=="done" ? "every camera recorded to the end" : L.state || "");
  $("post").innerHTML = ["remux","export","copy","run"].map(k => P[k] ? "<li>"+tag(P[k].state, P[k].state=="done"?"ok":P[k].state=="failed"?"bad":"warn")+" "+k+": "+esc(P[k].message)+"</li>"
    : "<li class=muted>"+k+(st=="post"?": waiting":"")+"</li>").join("");
}
function renderDevices(s, st){
  const D = s.devices; $("devs").hidden = !D;
  if(!D){ $("left").hidden = true; return; }
  const stepTag = {done:"ok", failed:"bad", running:"warn", waiting:"warn", skipped:""};
  if((D.steps||[]).length) $("steps").innerHTML = D.steps.map(x => "<li>"+tag(x.state, stepTag[x.state]||"")+" <b>"+esc(x.name)+"</b> "+esc(x.text)+"</li>").join("");
  const P = D.pico; $("picotitle").textContent = P ? "Pico (" + P.port + ")" : "Pico";
  let pt = "-";
  if(P && P.state){
    if(P.alive && P.mode=="wiring") pt = "Blinking (the gate check)";
    else if(P.alive) pt = "Running: bout "+(P.bout||"-")+" of "+P.bouts+" · "+(P.pulses||0).toLocaleString()+" camera pulses · ends "+(P.ends_at||"?")+" ("+hms(Math.max(0,(P.ends_s||0)-(P.session_s||0)))+" left)"+(P.end_requested?" · stop asked ("+P.end_requested+")":"");
    else pt = "Idle. Last session: "+P.state+(P.done?" ("+P.done+")":"")+(P.error?" · "+P.error:"");
  } else if(P) pt = "Idle (no session run here yet)";
  if(D.plan && !(P && P.alive)) pt += " · This recording: " + D.plan.text;
  if(D.start_at && !D.pico_started) pt += " · starts at " + D.start_at;
  $("picostate").textContent = pt;
  $("picostart").hidden = !(P && st=="recording" && !D.pico_started && !D.stop_mode);
  $("startpico").disabled = !!D.can_start_pico; $("startpico").title = D.can_start_pico || "";
  if(P && st=="recording" && !D.pico_started) $("picoerr").textContent = D.can_start_pico && D.can_start_pico!="Not recording." ? D.can_start_pico : "";
  const G = D.spikeglx;
  $("sglxstate").innerHTML = !G ? "-" : G.busy && G.running==null ? tag("busy","warn")+" its run is starting" : !G.answers ? tag("not answering","warn")+" "+esc(G.error||"") :
    tag(G.running?"running":"idle", G.running?"ok":"")+(G.running? " "+(G.saving?"recording enabled, a file open (gate high)":"recording enabled, gate closed")+(G.samples!=null?" · "+Number(G.samples).toLocaleString()+" samples":"") : "")+
    (G.openings!=null?" · "+G.openings+" gate opening"+(G.openings==1?"":"s")+" this recording ("+G.files+" files)":"")+(G.folder?'<div class="mono">'+esc(G.folder)+"</div>":"");
  const idle = ["ready","done","failed"].includes(st), picoLeft = idle && P && P.alive, sglxLeft = idle && G && G.running;
  $("left").hidden = !(picoLeft || sglxLeft);
  $("lefttext").textContent = D.left_running || ((picoLeft?"The Pico is running a session":"")+(picoLeft&&sglxLeft?", and ":"")+(sglxLeft?"SpikeGLX is running a run":"")+", with no recording on this panel.");
  $("stoppico").hidden = !picoLeft; $("stopsglx").hidden = !sglxLeft || picoLeft;
}
async function pauseUpdates(b){ b.disabled = true; b.textContent = "Windows asks for administrator rights…"; const e = await post("/pause-updates"); if(e) alert(e); tick(); }
async function tick(){
  try { const r = await fetch(q("/status.json")); if(r.status==403){ $("state").textContent="Wrong key"; $("error").hidden=false; $("error").textContent=(await r.json()).error; return; }
    render(await r.json()); } catch(e){ $("state").textContent = "Panel not running"; $("state").className = "tag bad"; }
}
function images(){ for(let i=0;i<thumbs;i++){ const im = $("th"+i); if(im) im.src = q("/thumb/"+i+".png?k="+Date.now()); } }
$("start").onclick = async () => { $("starterr").textContent = "";
  const e = await post("/start", {experiment:$("experiment").value, animal_ids:$("ids").value, notes:$("notes").value,
    length: $("length").value || null, pico_at: $("picoat").value.trim() || null});
  $("starterr").textContent = e || ""; tick(); };
$("length").onchange = async () => { const e = await post("/length", {seconds: $("length").value}); $("starterr").textContent = e || ""; tick(); };
$("close").onclick = async () => { if(!confirm("Close the panel? Nothing is recording; the page stops answering.")) return; const e = await post("/close"); if(e) alert(e); };
$("keep").onclick = () => { $("stopdlg").hidden = true; };
async function stopMode(mode){ $("stopdlg").hidden = true; const e = await post("/stop", {mode}); if(e) alert(e); tick(); }
$("stopbout").onclick = () => stopMode("bout"); $("stopnow").onclick = () => stopMode("now");
$("startpico").onclick = () => { const D = (last||{}).devices||{}; $("picotext").textContent = "The Pico starts its session now: the gate, the camera pulses and the LEDs. " + (D.plan ? D.plan.text + "." : ""); $("picodlg").hidden = false; };
$("picono").onclick = () => { $("picodlg").hidden = true; };
$("picoyes").onclick = async () => { $("picodlg").hidden = true; const e = await post("/pico-start", {}); $("picoerr").textContent = e || ""; tick(); };
$("picoatbtn").onclick = async () => { const e = await post("/pico-start", {at: $("picoat2").value.trim()}); $("picoerr").textContent = e || ""; tick(); };
$("stoppico").onclick = async () => { if(!confirm("Stop the Pico's session now? Its outputs (gate, camera pulses, LEDs) all go low.")) return; const e = await post("/stop-left", {what:"pico"}); if(e) alert(e); tick(); };
$("stopsglx").onclick = async () => { if(!confirm("Stop SpikeGLX's run? Its files close.")) return; const e = await post("/stop-left", {what:"spikeglx"}); if(e) alert(e); tick(); };
$("stop").onclick = async () => {
  const D = (last||{}).devices;
  if(D && !(last.launcher && last.launcher.recording && last.launcher.recording.stop_asked)){
    const P = D.pico, on = P && P.alive && D.pico_started;
    $("stopbout").hidden = !on;
    $("stoptext").textContent = on ? "After this bout: the Pico finishes bout "+(P.bout||"-")+" (its gate falls on time, so SpikeGLX's file closes as planned), then campy and SpikeGLX stop. Now: the Pico stops at once, then campy, then SpikeGLX." :
      "campy stops (each camera closes its video)"+(D.spikeglx?", then SpikeGLX's run":"")+".";
    $("stopdlg").hidden = false; return; }
  if(last && last.launcher && last.launcher.recording && last.launcher.recording.stop_asked && !stopArmed){ stopArmed = 1; setTimeout(() => {stopArmed = 0;}, 5000); tick(); return; }
  const e = await post("/stop", {now: !!stopArmed}); stopArmed = 0; if(e) alert(e); tick(); };
$("mark").onclick = async () => { const e = await post("/bookmark", {note:$("note").value}); $("markerr").textContent = e || ""; if(!e) $("note").value = ""; tick(); };
$("note").addEventListener("keydown", ev => { if(ev.key=="Enter") $("mark").click(); });
let orphanArmed = 0;
$("stoporphan").onclick = async () => { const e = await post("/stop-orphan", {now: !!orphanArmed});
  if(e) alert(e); else if(!orphanArmed){ orphanArmed = 1; $("stoporphan").textContent = "Stop now, without closing"; setTimeout(() => { orphanArmed = 0; $("stoporphan").textContent = "Stop that recording"; }, 5000); } tick(); };
$("viewer").onclick = async () => { const e = await post("/viewer"); if(e) alert(e); };
tick(); setInterval(tick, 1000); setInterval(images, 1000);
</script></body></html>
"""
