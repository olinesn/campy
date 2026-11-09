"""
campy-session panel's page (campy/panel.py serves it): one HTML file with its
CSS and JavaScript, no build step and nothing fetched from elsewhere, so it
works on a rig without internet. It asks the panel for /status.json once a
second, and each camera's thumbnail (/thumb/<n>.png) once a second, with the
key from its own address on every request.
"""

from campy import panellogo

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>campy session</title><link rel="icon" type="image/png" href="@ICON@">
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
.cam img,.cam .ph{width:100%;aspect-ratio:5/4;object-fit:contain;background:var(--soft);border-radius:6px;display:block}
.cam .ph{display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:12px;padding:8px;box-sizing:border-box}
.cam [hidden]{display:none}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button:hover{background:var(--soft)}button.stop{color:var(--bad);border-color:var(--bad)}button.go{color:var(--ok);border-color:var(--ok)}
input{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
label{font-size:12px;color:var(--muted)}h2{font-size:15px;font-weight:500;margin:0 0 6px}h3{font-size:13px;font-weight:600;margin:0 0 4px}.logo{width:44px;height:44px;flex:none;display:block}
.bar{height:6px;background:var(--soft);border-radius:3px;overflow:hidden}.bar div{height:6px;background:var(--ok)}
.copybar{max-width:420px;margin:4px 0 2px}.copybar div{background:var(--accent)}
.bgcopy{border-left:4px solid var(--accent)}.bgcopy .copybar{max-width:none;height:10px}.bgcopy .copybar div{height:10px}
.err{color:var(--bad);font-size:13px}ul{margin:4px 0 0;padding-left:18px}li{margin:2px 0}
select{font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
.steps li{list-style:none;margin-left:-18px}.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:16px}
.modal .card{max-width:520px}[hidden]{display:none!important}
.bgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:4px 10px;margin:4px 0}
.types li{list-style:none;margin-left:-18px;display:flex;gap:8px;align-items:baseline;padding:3px 0;border-top:1px solid var(--line)}
.types li span{flex:1}.star{padding:0 8px;font-size:16px;line-height:1.4;border:none;background:none;color:var(--accent)}
.bgrid>div{display:flex;flex-direction:column;justify-content:flex-end}.bgrid input.changed{border-color:var(--accent);border-width:2px}details summary{cursor:pointer;font-size:13px;margin-top:6px}
</style></head><body><main>

<div class="card row">
  <img class="logo" src="@LOGO@" alt="LUC3D" width="44" height="44">
  <div class="grow"><div id="rig" style="font-weight:500">campy session</div><div id="where" class="mono muted"></div></div>
  <span id="state" class="tag">connecting</span>
  <div id="clockbox" style="min-width:180px"><div id="clock"></div><div class="bar"><div id="progress" style="width:0"></div></div></div>
  <button id="stop" class="stop" hidden>Stop</button>
  <button id="close" hidden title="Close the panel (nothing is recording)">Close the panel</button>
</div>
<div id="error" class="err" hidden></div>
<div id="bgcopy" class="card bgcopy" hidden><div class="row"><b class="grow" id="bgcopytitle">Copying in the background</b></div>
  <div id="bgcopyok" style="font-weight:500"></div>
  <div class="muted">Leave the computer on, and the panel's black window open, until it is done.</div>
  <div id="bgcopytext" style="margin-top:6px"></div></div>

<div id="typebox" class="card" hidden>
  <div class="row"><div class="grow"><label for="type">Experiment: the rig's settings to record with</label><select id="type"></select>
    <div class="muted" id="typetext"></div></div>
    <button id="typeall" title="Every config in the folder: star the ones the Experiment list shows">Show all configs</button></div>
  <div id="typeerr" class="err"></div>
  <div id="typelist" hidden><ul id="types" class="types"></ul>
    <div class="row" style="margin-top:4px"><span class="muted grow mono" id="typefolder"></span><button id="typescan" title="Read the folder's configs again (a new or changed file)">Look again</button></div></div>
</div>

<div id="before" class="card" hidden>
  <h2>Next recording</h2>
  <div class="two">
    <div>
      <div id="modelbox" hidden style="margin-bottom:10px"><h2>Live models</h2>
        <label for="mset">Pair</label><select id="mset"></select>
        <label for="mcen">Centroid model</label><select id="mcen"></select><div class="muted" id="mcentext"></div>
        <label for="mins">Instance model</label><select id="mins"></select><div class="muted" id="minstext"></div>
        <div class="row" style="margin-top:4px"><span id="mscan" class="muted grow" style="font-size:12px"></span><button id="mrescan" title="Look through panel.models' folders again">Look again</button></div></div>
      <div id="lengthbox" hidden><label for="length">Session length</label><select id="length"></select>
        <div class="muted" id="plantext"></div>
        <details id="boutbox" open hidden><summary>Bout settings <span id="boutsum" class="muted"></span></summary>
          <div id="boutfields" class="bgrid"></div>
          <div class="muted" id="boutshape" style="font-size:12px"></div>
          <div class="row" style="margin-top:4px"><span class="muted grow" style="font-size:12px">The config file isn't changed: these are for the recordings started here, and the next panel opened with this config starts from them. Each recording's session-config.yaml says which came from here.</span><button id="boutreset" hidden>Back to the config's</button></div></details>
        <label for="picoat">Start the Pico at (optional: otherwise a button, once every camera is ready)</label><input id="picoat" placeholder="7:20 PM"></div>
      <h3 style="margin-top:4px">Saved with the recording</h3>
      <label for="experiment">Experiment name</label><input id="experiment" placeholder="Social hierarchy, week 3">
      <label for="ids">Animal IDs, comma-separated</label><input id="ids" placeholder="M12, M14, M15">
      <label for="notes">Notes</label><input id="notes" placeholder="Cage 2, new bedding">
      <label for="copyto">Copy each recording to (optional)</label><input id="copyto" placeholder="\\server\share\my folder">
      <div class="muted" style="font-size:12px">After each recording, its folder is copied into this one in the background, while the next records. Nothing is deleted. Empty: no copy.</div>
    </div>
    <div><div id="orphan" class="err" hidden></div><button id="stoporphan" class="stop" hidden>Stop that recording</button>
      <h3 style="margin-top:4px">Checks</h3>
      <div class="muted" style="font-size:12px">Green: fine. Yellow: you can record, but read it. Red: it stops the start until it is fixed.</div>
      <ul id="checks"></ul><button id="start" class="go">Start recording</button>
      <div id="starterr" class="err"></div></div>
  </div>
</div>

<div id="left" class="card" hidden><h2>Still running</h2><div id="lefttext" class="err"></div>
  <div class="row" style="margin-top:8px"><button id="stoppico" class="stop" hidden>Stop the Pico</button><button id="stopsglx" class="stop" hidden>Stop SpikeGLX's run</button></div></div>

<div id="devs" class="two" hidden>
  <div class="card"><h2>Steps</h2><ul id="steps" class="steps"><li class="muted">At Start: SpikeGLX's run, the gate check, the cameras, then the Pico.</li></ul></div>
  <div class="card"><h2 id="picotitle">Pico</h2><div id="picostate" class="muted">-</div>
    <div id="camready" style="margin-top:8px" hidden></div>
    <div id="picostart" class="row" style="margin-top:8px" hidden><button id="startpico" class="go">Start the Pico</button>
      <input id="picoat2" style="width:110px" placeholder="7:20 PM"><button id="picoatbtn">At this time</button></div>
    <div id="picoerr" class="err"></div>
    <h2 style="margin-top:10px">SpikeGLX</h2><div id="sglxstate" class="muted">-</div></div>
</div>

<div id="during" hidden>
  <div class="tiles" style="margin-bottom:12px">
    <div class="tile"><label>Disk free</label><div class="big" id="disk">-</div><div class="muted" id="diskleft"></div></div>
    <div class="tile"><label>Tracked in 3D</label><div class="big" id="good3d">-</div><div class="muted">animals, last second</div><div class="muted mono" id="usedmodels"></div></div>
    <div class="tile"><label>Programs</label><div id="programs"></div></div>
    <div class="tile"><label>Bookmarks</label><div class="big" id="nmarks">0</div><div class="muted" id="lastmark"></div></div>
  </div>
  <div class="cams" id="cams" style="margin-bottom:12px"></div>
  <div class="two">
    <div class="card"><h2>Warnings</h2><div class="muted" style="font-size:12px">Problems now, and the ones that went away (in grey). They stay in the recording's alerts.log.</div>
      <ul id="warnings"><li class="muted">None</li></ul></div>
    <div class="card"><h2>Bookmark</h2>
      <div class="muted" style="font-size:12px">A moment to find later: type what happened and press Add. It is saved with its exact time and frame in the recording's bookmarks.csv.</div>
      <div class="row"><input id="note" class="grow" placeholder="Mouse 3 rears at the feeder"><button id="mark">Add</button></div>
      <ul id="marks"></ul><div id="markerr" class="err"></div>
      <div style="margin-top:8px"><button id="viewer" title="The window with every camera's picture. Closing it never affects the recording.">Open the viewer again</button></div></div>
  </div>
</div>

<div id="copies" class="card" hidden><h2>Copies</h2><div class="muted" style="font-size:12px">Each recording's folder, copied into the folder in "Copy each recording to", one at a time.</div><div id="copywait" class="muted" hidden></div>
  <ul id="copylist"></ul><button id="closenow" class="stop" hidden>Close now, without the copies</button></div>

<div id="after" class="card" hidden><h2 id="aftertitle">After the recording</h2><div id="afterwhen" class="muted"></div>
  <h3 id="summaryhead" style="margin-top:10px">How it went</h3>
  <ul id="summary"></ul><div id="summarynote" class="muted" hidden>From each camera's metadata.csv and the recording's alerts.log, in its folder.</div>
  <h3 style="margin-top:10px">Steps after the recording</h3><ul id="post"></ul>
  <p class="muted">Closing this page never stops a recording or these steps. You can start the next recording above at any time.</p></div>

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
function dur(s){ s = Math.max(0, Math.floor(s)); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
let camsWereReady = false; const baseTitle = document.title;
function chime(){ try { const a = new (window.AudioContext||window.webkitAudioContext)(); [0, 0.18].forEach((t, i) => { const o = a.createOscillator(), g = a.createGain(); o.frequency.value = i ? 1100 : 880; g.gain.setValueAtTime(0.15, a.currentTime+t); g.gain.exponentialRampToValueAtTime(0.001, a.currentTime+t+0.16); o.connect(g); g.connect(a.destination); o.start(a.currentTime+t); o.stop(a.currentTime+t+0.17); }); } catch(e) {} }
function tag(text, kind){ return '<span class="tag '+kind+'">'+esc(text)+'</span>'; }
function render(s){
  last = s;
  const L = s.launcher || {}, rec = L.recording || {}, H = s.health || {};
  const TY = s.types, chosen = TY ? TY.all.find(x => x.path == TY.chosen) : null;
  $("rig").textContent = s.rig + " · " + (chosen ? chosen.title : s.config.split(/[\\/]/).pop());
  const st = s.state, live = ["checking","starting","preparing","starting live","recording","stopping","post","finishing"].includes(st);
  const running = ["checking","starting","preparing","starting live","recording","stopping"].includes(st);
  // The line under the title says what the path is: the folder being recorded into, the previous recording's, or the config
  $("where").textContent = L.folder ? (running ? "Recording into " : st=="post" ? "This recording's folder: " : "Previous recording's folder: ") + L.folder :
    "Settings file: " + s.config;
  $("state").className = "tag " + (st=="recording" ? "ok" : st=="failed" ? "bad" : live ? "warn" : "");
  $("state").textContent = {ready:"Ready to record", starting:"Starting", checking:"Checking", preparing:"Starting SpikeGLX, gate check", finishing:"Finishing", "starting live":"Starting campy-live",
    recording:"Recording", stopping:"Stopping", post:"Finishing the recording's files", done:"Done", failed:"Failed"}[st] || st;
  $("stop").hidden = !(st=="recording" || st=="stopping");
  $("close").hidden = !(st=="ready" || st=="done" || st=="failed");
  renderDevices(s, st);
  renderTypes(TY, st, s);
  $("stop").textContent = stopArmed ? "Stop now, without closing" : (rec.stop_asked ? "Stopping…" : "Stop");
  const DP = s.devices && s.devices.plan ? s.devices.pico : null;
  if(DP){ // a Pico's session: its time against its plan (campy's recTimeInSec is camera time, not the clock's)
    const on = DP.alive && s.devices.pico_started && DP.mode != "wiring"; $("clockbox").hidden = !on && !live;
    $("clock").innerHTML = on ? "<b>"+hms(DP.session_s)+"</b> <span class=muted>of "+hms(DP.ends_s)+", the Pico's</span>" : (live ? "<span class=muted>"+(s.devices.pico_started ? "the Pico's session has ended" : "the Pico hasn't started")+"</span>" : "");
    $("progress").style.width = on && DP.ends_s ? Math.min(100, 100*DP.session_s/DP.ends_s)+"%" : "0";
  } else {
  // Only while it records: after it, the previous recording's time against its length meant nothing (1:19 of 1:00)
  const shown = running && rec.elapsed_s!=null; $("clockbox").hidden = !shown;
  $("clock").innerHTML = shown ? "<b>"+hms(rec.elapsed_s)+"</b> <span class=muted>recorded, of "+hms(rec.planned_s)+"</span>" : "";
  $("progress").style.width = shown && rec.planned_s ? Math.min(100, 100*rec.elapsed_s/rec.planned_s)+"%" : "0";
  }
  $("error").hidden = !s.error; $("error").textContent = s.error || "";
  $("before").hidden = !(st=="ready" || st=="done" || st=="failed");
  if(st=="recording") $("starterr").textContent = ""; // a refusal from before this recording no longer applies
  $("orphan").hidden = $("stoporphan").hidden = !s.orphan;
  if(s.orphan) $("orphan").textContent = "A recording an earlier panel started is still running, in " + s.orphan.folder +
    " (its window was closed; the recording went on). Stop it, or wait for it to end, before starting another.";
  $("checks").innerHTML = s.checks.map(c => "<li>"+tag(c.level=="block"?"stops the start":c.level=="warn"?"warning":"ok",
    c.level=="block"?"bad":c.level=="warn"?"warn":"ok")+" "+esc(c.text)+(c.action=="pause-updates"?' <button onclick="pauseUpdates(this)">Pause Windows Update</button>':"")+"</li>").join("");
  renderModels(s.models);
  const LM = L.live_models; $("usedmodels").textContent = LM && LM.centroid ? base(LM.centroid)+" + "+base(LM.instance) : "";
  const lengths = s.lengths || []; $("lengthbox").hidden = !lengths.length;
  if(lengths.length && $("length").options.length != lengths.length){ $("length").innerHTML = lengths.map(l => '<option value="'+l.seconds+'">'+esc(l.name)+'</option>').join(""); }
  if(lengths.length && document.activeElement != $("length")) $("length").value = String(s.length);
  renderBouts(s.bouts);
  const L0 = lengths.find(l => l.seconds == s.length); $("plantext").textContent = L0 ? L0.text + (L0.videoFolder ? ", into "+L0.videoFolder : "") : "";
  if(refill){ refill = false; // another experiment picked: its config's fields, where it has them (what was typed stays otherwise)
    if(s.fields.experiment) $("experiment").value = s.fields.experiment; if(s.fields.animal_ids) $("ids").value = s.fields.animal_ids;
    $("copyto").value = s.fields.copy_to||""; } // the copy destination is each config's own
  if(!$("experiment").dataset.set){ $("experiment").value = s.fields.experiment||""; $("ids").value = s.fields.animal_ids||""; $("copyto").value = s.fields.copy_to||""; $("experiment").dataset.set=1; }
  renderCopies(s.copies || {jobs: []}, L.folder, st);
  $("during").hidden = !(st=="recording" || st=="stopping" || st=="starting live");
  $("disk").textContent = s.disk.free_gb!=null ? Math.round(s.disk.free_gb)+" GB" : "-";
  // With a Pico's plan: what the rest of it still needs (SpikeGLX and the videos, estimated), not hours at the rate so far
  const D_ = s.disk, short = D_.plan_left_gb!=null && D_.free_gb!=null ? D_.plan_left_gb - D_.free_gb : null;
  $("diskleft").innerHTML = D_.plan_left_gb!=null ? "the rest of the plan needs about "+Math.round(D_.plan_left_gb).toLocaleString()+" GB (an estimate)"+
    (short > 0 ? '<br><span class="err">'+Math.round(short).toLocaleString()+" GB short</span>" : "") :
    D_.hours_left!=null ? "about "+D_.hours_left+" h at this rate" : "";
  const T = H.tracking; $("good3d").textContent = T && T.good3d!=null ? T.good3d+" of "+T.animals : "-";
  const parts = Object.assign({}, L.parts||{}); if((L.live||[]).length) parts["campy-live"] = L.live[L.live.length-1];
  $("programs").innerHTML = Object.entries(parts).map(([n,p]) => tag(p.state, p.state=="running"?"ok":p.exit_code?"bad":"warn")+" "+esc(PROGRAMS[n] || n)).join("<br>") || "-";
  const R = s.recording || {}, counters = {}; (R.cameras||[]).forEach(c => counters[c.name] = c);
  const cams = H.cameras || [];
  if(cams.length != thumbs){ thumbs = cams.length; $("cams").innerHTML = cams.map((c,i) => '<div class="card cam"><div class="ph" id="ph'+i+'"></div><img id="th'+i+'" alt="'+esc(c.name)+'" hidden><div id="cm'+i+'"></div></div>').join(""); }
  // No picture until a camera's first frame (on the ChronicRecord rig, the Pico's first pulse): a line, not a broken image
  const noPulses = !!(s.devices && s.devices.pico && !s.devices.pico_started);
  cams.forEach((c,i) => { const k = counters[c.name] || {}, first = (c.frame||0) > 0;
    $("th"+i).hidden = !first; $("ph"+i).hidden = first;
    if(!first) $("ph"+i).textContent = st=="recording" || st=="starting" ? (noPulses ? "No picture yet: the camera waits for the Pico's pulses" : "No picture yet: waiting for its first frame") : "No picture";
    $("cm"+i).innerHTML = '<div class="row" style="margin-top:6px"><b class="grow">'+esc(c.name)+'</b>'+(c.problems.length ? c.problems.map(p => tag(p,"warn")).join(" ") : c.paused_s!=null ? tag("PAUSED "+dur(c.paused_s),"muted") : tag("OK","ok"))+'</div>'+
      '<div class="muted">'+(c.fps!=null?c.fps.toFixed(1):"-")+' frames/s · frame '+(c.frame||0).toLocaleString()+
      (k.dropped!=null?' · '+k.dropped+' dropped':'')+(k.write_queue_s!=null?' · queue '+k.write_queue_s.toFixed(1)+' s':'')+'</div>'; });
  const warn = [];
  const lines = t => esc(t).replace(/\n/g, "<br>"); // several cameras at once: a line each
  (s.alerts.open||[]).forEach(a => warn.push(tag(a.severity||"now", a.severity=="critical"?"bad":"warn")+" "+lines(a.text)));
  cams.forEach(c => c.sentences.forEach(t => warn.push(tag("now","warn")+" "+esc(t))));
  ((T||{}).alerts||[]).forEach(t => warn.push(tag("3D","warn")+" "+esc(t)));
  (s.problems||[]).slice(-3).forEach(p => warn.push(tag(p.time.slice(11,19),"bad")+" "+esc(p.text)));
  (s.alerts.resolved||[]).forEach(a => warn.push('<span class="muted">'+esc(a.time.slice(11,19))+" resolved: "+lines(a.text)+"</span>"));
  $("warnings").innerHTML = warn.length ? warn.map(w => "<li>"+w+"</li>").join("") : '<li class="muted">None</li>';
  $("nmarks").textContent = s.bookmarks.length ? s.bookmarks[0].number : 0;
  $("lastmark").textContent = s.bookmarks.length ? "last at "+s.bookmarks[0].time : "";
  $("marks").innerHTML = s.bookmarks.map(m => "<li class=muted>"+m.number+" at "+m.time+", frame "+(m.frame!=null?m.frame.toLocaleString():"-")+(m.note?": "+esc(m.note):"")+"</li>").join("");
  const P = L.post || {};
  $("after").hidden = !(st=="post" || (["done","failed","ready","finishing"].includes(st) && L.folder));
  // What went wrong in it, from the files in its folder: until the next recording starts, and after the page or the panel is opened again
  const SM = s.summary, hurt = SM && SM.level != "ok";
  const AQ = (L.parts||{}).acquire || {};
  $("afterwhen").textContent = AQ.ended ? "Ended at "+when(AQ.ended)+(rec.elapsed_s!=null ? ", after "+mins(rec.elapsed_s) : "") : "";
  $("summaryhead").hidden = !SM;
  const C = L.clean; $("aftertitle").textContent = st=="post" ? "After the recording" : "Previous recording: " +
    (C ? (C.clean ? "every camera recorded to the end" + (hurt ? ", with problems" : "") : "it didn't end cleanly: " + (C.why||"").split(";")[0] + (String(C.why||"").includes(";") ? "; ..." : "")) :
    L.state=="done" ? "campy-acquire ended" : L.state || "");
  $("summary").innerHTML = SM ? SM.lines.map(x => "<li>"+tag(x.level=="ok"?"ok":x.level=="bad"?"problem":"warning", x.level)+" "+esc(x.text)+"</li>").join("") : "";
  $("summarynote").hidden = !hurt;
  const CJ = ((s.copies||{}).jobs||[]).find(j => L.folder && j.folder == L.folder);
  // The panel's own copy, in the background: copying (with how far it has got), waiting behind another, done or failed
  if(CJ) P.copy = {state: ["done","failed","copying"].includes(CJ.state) ? CJ.state : "waiting", message: copyText(CJ), bar: copyBar(CJ)};
  $("post").innerHTML = ["remux","export","copy","run"].map(k => P[k] ? "<li>"+tag(P[k].state, P[k].state=="done"?"ok":P[k].state=="failed"?"bad":"warn")+" "+STEPS[k]+": "+esc(P[k].message)+(P[k].bar||"")+"</li>"
    : "<li class=muted>"+STEPS[k]+(st=="post"?": waiting":"")+"</li>").join("");
}
let typeSig = "", listSig = "", choosing = false, refill = false;
const PROGRAMS = {acquire: "Cameras and video files (campy-acquire)", view: "Viewer window", watch: "Alerts (campy-watch)",
  "campy-live": "Live tracking (campy-live)"};
const STEPS = {remux: "Video files", export: "Tracking files for LUC3D", copy: "Copy", run: "Extra commands"};
function mins(s){ // 80 s as "1 min 20 s", 4000 s as "1 h 6 min"
  s = Math.round(s); const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  return h ? h+" h "+m+" min" : m ? m+" min"+(s%60 ? " "+s%60+" s" : "") : s+" s";
}
function when(iso){ // an ISO time as 2:40 PM (with the day, when not today)
  const d = new Date(iso); if(isNaN(d)) return "";
  const t = d.toLocaleTimeString([], {hour:"numeric", minute:"2-digit", hour12:true});
  return d.toDateString()==new Date().toDateString() ? t : d.toLocaleDateString([], {month:"short", day:"numeric"})+", "+t;
}
function renderTypes(T, st, s){
  $("typebox").hidden = !T; if(!T) return;
  // Changed only while nothing records: no recording, no earlier panel's, no Pico or SpikeGLX left running
  const idle = ["ready","done","failed"].includes(st) && !s.orphan && $("left").hidden;
  const list = T.favorites.slice(); if(!list.some(x => x.path == T.chosen)){ const c = T.all.find(x => x.path == T.chosen); if(c) list.push(c); }
  const sig = JSON.stringify([list.map(x => [x.path, x.title]), T.chosen]);
  if(sig != typeSig && document.activeElement != $("type")){ typeSig = sig;
    $("type").innerHTML = list.map(x => '<option value="'+esc(x.path)+'">'+esc(x.title)+'</option>').join(""); $("type").value = T.chosen; }
  $("type").disabled = !idle || choosing;
  const cur = T.all.find(x => x.path == T.chosen);
  if(!choosing) $("typetext").textContent = (cur ? cur.file : "") + (idle ? "" : " · another experiment can be picked once nothing is recording");
  $("typefolder").textContent = T.folder;
  if($("typelist").hidden) return;
  const lsig = JSON.stringify([T.all, T.chosen, idle]);
  if(lsig == listSig) return; listSig = lsig;
  $("types").innerHTML = T.all.map((x,i) => '<li><button class="star" data-star="'+i+'" title="'+(x.favorite ? "Take it off" : "Add it to")+' the Experiment list">'+(x.favorite ? "\u2605" : "\u2606")+'</button>'+
    '<span><b>'+esc(x.title)+'</b> <span class="muted mono">'+esc(x.file)+'</span>'+(x.error ? '<br><span class="err">'+esc(x.error)+'</span>' : "")+'</span>'+
    (x.path == T.chosen ? tag("in use","ok") : x.error ? "" : '<button data-use="'+i+'"'+(idle ? "" : " disabled")+'>Use</button>')+'</li>').join("") ||
    '<li class="muted">No config (*.yaml, *.yml) in this folder.</li>';
  $("types").querySelectorAll("[data-star]").forEach(b => { const x = T.all[+b.dataset.star]; b.onclick = async () => {
    const e = await post("/favorite", {path: x.path, on: !x.favorite}); $("typeerr").textContent = e || ""; listSig = ""; tick(); }; });
  $("types").querySelectorAll("[data-use]").forEach(b => { b.onclick = () => chooseType(T.all[+b.dataset.use].path); });
}
async function chooseType(path){
  choosing = true; $("type").disabled = true; $("typetext").textContent = "Loading the experiment's config and its checks…";
  const e = await post("/type", {path}); choosing = false; $("typeerr").textContent = e || "";
  if(!e){ refill = true; modelSig = ""; boutSig = ""; typeSig = ""; listSig = ""; $("length").innerHTML = ""; $("starterr").textContent = ""; }
  typeSig = ""; tick();
}
function base(p){ return String(p||"").split(/[\\/]/).filter(x => x).pop() || ""; }
function gb(b){ return b==null ? "?" : (b/1e9).toFixed(1); }
function copyText(j){
  if(j.state=="copying") return "copying into "+j.to+": "+gb(j.done_bytes)+" of "+gb(j.bytes)+" GB, "+dur(j.seconds||0)+copyEta(j);
  if(j.state=="waiting") return "waits for the copies before it, into "+j.to;
  return j.message || j.state;
}
function copyEta(j){
  // How long the rest takes at the copy's average rate so far, and the clock time it should end: once there is a rate
  if(!j.bytes || !j.done_bytes || (j.seconds||0) < 5 || j.done_bytes >= j.bytes) return "";
  const left = (j.bytes - j.done_bytes) / (j.done_bytes / j.seconds), m = Math.round(left/60);
  const end = new Date(Date.now() + left*1000).toLocaleTimeString([], {hour:"numeric", minute:"2-digit", hour12:true});
  return ", " + (left < 60 ? "under a minute" : m < 60 ? "about "+m+" min" : "about "+Math.floor(m/60)+" h "+(m%60)+" min") + " left (done around "+end+")";
}
function copyBar(j){
  // A copy under way: a bar of the bytes copied so far (none until its size is known)
  if(j.state!="copying" || !j.bytes) return "";
  return '<div class="bar copybar"><div style="width:'+Math.min(100, 100*(j.done_bytes||0)/j.bytes).toFixed(1)+'%"></div></div>';
}
function renderCopies(C, folder, st){
  const jobs = C.jobs || [];
  // Copies still to do, at the top of the page in every state (a new recording too) until the last is done: not only in
  // the Copies card, below the cameras while one records
  const now = jobs.filter(j => j.state=="copying"), queued = jobs.filter(j => j.state=="waiting");
  $("bgcopy").hidden = !now.length && !queued.length;
  $("bgcopytitle").textContent = now.length ? "Copying in the background" : "Copies waiting to start";
  // Said plainly: the copy is no reason to wait (it keeps going while the next one records)
  $("bgcopyok").textContent = ["ready","done","failed"].includes(st) ? "You can start a new recording now: the copy goes on beside it and doesn't disturb it." :
    ["recording","starting","checking","preparing","starting live","stopping"].includes(st) ? "It doesn't disturb this recording." : "";
  $("bgcopytext").innerHTML = now.map(j => "<div><b>"+esc(j.name)+"</b> "+esc(copyText(j))+copyBar(j)+"</div>").join("") +
    (queued.length ? '<div class="muted">'+(now.length ? "Then " : "")+queued.map(j => esc(j.name)).join(", ")+(queued.length==1 ? " waits" : " wait")+" to be copied.</div>" : ""); $("copies").hidden = !jobs.length && !C.close_when_copied;
  $("copywait").hidden = $("closenow").hidden = !C.close_when_copied;
  if(C.close_when_copied) $("copywait").textContent = C.pending ? "The panel closes by itself once these copies are done ("+C.pending+" left)." : "Closing…";
  $("copylist").innerHTML = jobs.map(j => "<li>"+tag(j.state, j.state=="done"?"ok":j.state=="failed"||j.state=="left"?"bad":"warn")+" <b>"+esc(j.name)+"</b> "+esc(copyText(j))+copyBar(j)+"</li>").join("");
}
let modelSig = "";
function renderModels(M){
  $("modelbox").hidden = !M; if(!M) return;
  // The lists only when they change, so a list being opened isn't redrawn under the pointer
  const sig = JSON.stringify([M.sets, M.config, M.centroids.map(m => m.folder), M.instances.map(m => [m.folder, m.same])]);
  if(sig != modelSig && ![$("mset"), $("mcen"), $("mins")].includes(document.activeElement)){
    modelSig = sig;
    $("mset").innerHTML = (M.config ? '<option value="config">The config\'s pair</option>' : "") +
      M.sets.map(x => '<option value="'+esc(x.name)+'">'+esc(x.name)+'</option>').join("") + '<option value="">Other: pick them below</option>';
    const opt = (m, other) => '<option value="'+esc(m.folder)+'">'+(other ? "(other skeleton) " : "")+esc(m.name)+(m.date ? " · "+esc(m.date) : "")+'</option>';
    $("mcen").innerHTML = '<option value="" disabled>Pick a centroid model</option>' + M.centroids.map(m => opt(m, false)).join("");
    $("mins").innerHTML = '<option value="" disabled>Pick an instance model</option>' + M.instances.map(m => opt(m, !m.same && M.pick.centroid)).join("");
  }
  if(document.activeElement != $("mset")) $("mset").value = M.set || "";
  if(document.activeElement != $("mcen")) $("mcen").value = M.pick.centroid || "";
  if(document.activeElement != $("mins")) $("mins").value = M.pick.instance || "";
  const about = (list, folder) => { const m = list.find(x => x.folder == folder); return m ? esc(m.text) + (m.problems.length ? '<br><span class="err">'+esc(m.problems.join("; "))+'</span>' : "") : ""; };
  $("mcentext").innerHTML = about(M.centroids, M.pick.centroid); $("minstext").innerHTML = about(M.instances, M.pick.instance);
  $("mscan").textContent = (M.roots.length ? "Looked through "+M.roots.join(", ")+": "+M.scanned+" model"+(M.scanned==1?"":"s")+", "+M.scan_s+" s." :
    "To pick from more models, add panel: models: (their folders) to the config or its rig profile.") + (M.notes.length ? " "+M.notes.join("; ")+"." : "");
  $("mrescan").hidden = !M.roots.length;
}
let boutSig = "";
function renderBouts(B){
  $("boutbox").hidden = !B; if(!B) return;
  const fields = B.fields || [];
  // The fields only when the settings change, so a field being typed in isn't redrawn under the cursor
  const sig = JSON.stringify(fields.map(f => f.key));
  if(sig != boutSig){
    boutSig = sig;
    $("boutfields").innerHTML = fields.map(f => '<div><label for="b_'+f.key+'" title="'+f.key+'">'+esc(f.label)+(f.unit ? " ("+esc(f.unit)+")" : "")+
      '</label><input id="b_'+f.key+'" type="number" min="0" step="any" data-key="'+f.key+'"><div class="muted" id="bc_'+f.key+'" style="font-size:11px;min-height:16px"></div></div>').join("");
    fields.forEach(f => { $("b_"+f.key).onchange = e => setBout(f.key, e.target.value); });
  }
  fields.forEach(f => { const el = $("b_"+f.key); if(!el) return;
    if(document.activeElement != el) el.value = f.value;
    el.classList.toggle("changed", f.changed);
    $("bc_"+f.key).textContent = f.changed ? "the config's: "+f.config : ""; });
  const n = (B.changed || []).length;
  $("boutsum").textContent = n ? "· "+n+" changed"+(B.remembered ? ", as the last panel left them ("+B.remembered+")" : " on this page") : "· as the config says";
  $("boutreset").hidden = !n;
  const b = B.bout;
  $("boutshape").innerHTML = B.error ? '<span class="err">'+esc(B.error)+'</span>' : !b ? "" : b.error ? '<span class="err">'+esc(b.error)+'</span>' :
    esc(b.text) + (b.warnings.length ? '<br><span class="err">Warning: '+esc(b.warnings.join("; "))+'</span>' : "");
}
async function setBout(key, value){ const e = await post("/bouts", {values: {[key]: value}}); $("starterr").textContent = e || ""; tick(); }
async function pickModels(body){ const e = await post("/models", body); $("starterr").textContent = e || ""; modelSig = ""; tick(); }
function renderDevices(s, st){
  const D = s.devices; $("devs").hidden = !D;
  if(!D){ $("left").hidden = true; return; }
  const stepTag = {done:"ok", failed:"bad", running:"warn", waiting:"warn", skipped:""};
  if((D.steps||[]).length) $("steps").innerHTML = D.steps.map(x => "<li>"+tag(x.state, stepTag[x.state]||"")+" <b>"+esc(x.name)+"</b> "+esc(x.text)+"</li>").join("");
  const P = D.pico; $("picotitle").textContent = P ? "Pico (" + P.port + ")" : "Pico";
  let pt = "-";
  if(P && P.state){
    if(P.alive && P.mode=="wiring") pt = "Blinking (the gate check)";
    else if(P.alive) pt = "Running: "+(P.bout ? "bout "+P.bout+" of "+P.bouts : "between bouts ("+P.bouts+" in all)")+" · "+(P.pulses||0).toLocaleString()+" camera pulses · ends "+(P.ends_at||"?")+" ("+hms(Math.max(0,(P.ends_s||0)-(P.session_s||0)))+" left)"+(P.end_requested?" · stop asked ("+P.end_requested+")":"");
    // The gate check's blink runs as a short wiring check: say so, not "Last session: wiring check stopped after 1 rounds"
    else if(P.mode=="wiring") pt = "Idle. Its last run was a gate blink (the gate check)"+(P.error?" · "+P.error:"");
    else pt = "Idle. Last session: "+P.state+(P.done?" ("+P.done+")":"")+(P.error?" · "+P.error:"");
  } else if(P) pt = "Idle (no session run here yet)";
  if(D.plan && !(P && P.alive)) pt += " · This recording: " + D.plan.text;
  if(D.start_at && !D.pico_started) pt += " · starts at " + D.start_at;
  $("picostate").textContent = pt;
  $("picostart").hidden = !(P && st=="recording" && !D.pico_started && !D.stop_mode);
  $("startpico").disabled = !!D.can_start_pico; $("startpico").title = D.can_start_pico || "";
  // The cameras getting ready, said plainly (no need to watch the console): a chime and the tab's title once all are
  const CR = D.cameras_ready, waitingPico = P && st=="recording" && !D.pico_started && !D.stop_mode;
  $("camready").hidden = !(waitingPico && CR);
  if(waitingPico && CR) $("camready").innerHTML = CR[0] >= CR[1] ? tag("All "+CR[1]+" cameras are ready","ok")+(D.start_at ? " The Pico starts at "+esc(D.start_at)+"." : " Press <b>Start the Pico</b>.") : tag("Cameras getting ready: "+CR[0]+" of "+CR[1],"warn");
  const allReady = !!(waitingPico && CR && CR[0] >= CR[1]);
  if(allReady && !camsWereReady) chime();
  camsWereReady = allReady; document.title = allReady ? "Cameras ready · campy panel" : baseTitle;
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
  if(closed) return;
  try { const r = await fetch(q("/status.json")); if(r.status==403){ $("state").textContent="Wrong key"; $("error").hidden=false; $("error").textContent=(await r.json()).error; return; }
    render(await r.json()); } catch(e){ $("state").textContent = "Panel not running"; $("state").className = "tag bad"; }
}
function images(){ for(let i=0;i<thumbs;i++){ const im = $("th"+i); if(im && !im.hidden) im.src = q("/thumb/"+i+".png?k="+Date.now()); } }
$("start").onclick = async () => { $("starterr").textContent = "";
  // A length that doesn't fit on the disk (and nothing else stopping the start): only on purpose, from a confirm naming
  // the shortfall
  const blocks = ((last||{}).checks||[]).filter(c => c.level=="block"), disk = blocks.find(c => c.action=="disk");
  const override = !!disk && blocks.length == 1;
  if(override && !confirm("This session doesn't fit on the disk.\n\n" + disk.text + "\n\nStart anyway? Only if data will be moved off "+
    "this disk during the session. Otherwise the disk fills up: campy stops cleanly near the end of the space, but SpikeGLX doesn't.")) return;
  const e = await post("/start", {experiment:$("experiment").value, animal_ids:$("ids").value, notes:$("notes").value, copy_to:$("copyto").value.trim(),
    length: $("length").value || null, pico_at: $("picoat").value.trim() || null, disk_override: override});
  $("starterr").textContent = e || ""; tick(); };
$("mset").onchange = () => { if($("mset").value) pickModels({set: $("mset").value}); };
$("mcen").onchange = () => pickModels({centroid: $("mcen").value});
$("mins").onchange = () => pickModels({centroid: $("mcen").value, instance: $("mins").value});
$("mrescan").onclick = async () => { $("mrescan").disabled = true; $("mscan").textContent = "Looking…"; const e = await post("/models-scan"); $("mrescan").disabled = false; $("starterr").textContent = e || ""; modelSig = ""; tick(); };
$("boutreset").onclick = async () => { const e = await post("/bouts", {reset: true}); $("starterr").textContent = e || ""; tick(); };
$("type").onchange = () => chooseType($("type").value);
$("typeall").onclick = () => { const open = $("typelist").hidden; $("typelist").hidden = !open; $("typeall").textContent = open ? "Hide the list" : "Show all configs"; listSig = ""; tick(); };
$("typescan").onclick = async () => { $("typescan").disabled = true; const e = await post("/types-scan"); $("typescan").disabled = false; $("typeerr").textContent = e || ""; listSig = ""; typeSig = ""; tick(); };
$("length").onchange = async () => { const e = await post("/length", {seconds: $("length").value}); $("starterr").textContent = e || ""; tick(); };
let closed = false;
function closedPage(){ closed = true; document.querySelector("main").innerHTML = '<div class="card"><h2>The panel is closed</h2><p class="muted">You can close this tab. '+
    'To record again, open the panel from its desktop icon.</p></div>'; }
$("close").onclick = async () => {
  const C = (last||{}).copies || {};
  if(!confirm(C.pending ? "Close the panel? Nothing is recording, but "+C.pending+" recording"+(C.pending==1?" is":"s are")+" still being copied: the panel closes by itself once the copies are done." : "Close the panel? Nothing is recording.")) return;
  const r = await fetch(q("/close"), {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); const j = await r.json().catch(() => ({}));
  if(j.error){ alert(j.error); return; } if(j.waiting){ tick(); return; } closedPage(); };
$("closenow").onclick = async () => {
  if(!confirm("Close the panel now? The recordings not copied yet stay on this computer; each one's post.log says how to copy it by hand.")) return;
  const e = await post("/close", {leave: true}); if(e){ alert(e); return; } closedPage(); };
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
    $("stoptext").textContent = on ? (P.bout ? "After this bout: the Pico finishes bout "+P.bout+" (its gate falls on time, so SpikeGLX's file closes as planned), then campy and SpikeGLX stop." : "After this bout: the Pico is between bouts, so it stops at once, before the next bout starts, then campy and SpikeGLX stop.")+" Now: the Pico stops at once, then campy, then SpikeGLX." :
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

# The LUC3D logo in the header, and as the tab's icon (campy/panellogo.py: inline, nothing fetched)
PAGE = PAGE.replace("@LOGO@", panellogo.LOGO).replace("@ICON@", panellogo.ICON)
