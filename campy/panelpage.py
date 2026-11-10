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
--warn:#80500b;--warnbg:#fbefd8;--bad:#a32d2d;--badbg:#fbe9e9;--accent:#185fa5;--blueled:#0078d4;--yellowled:#6f8f00;--gate:#6b4fbb}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1a;--card:#252524;--soft:#2e2e2c;--text:#ecebe6;--muted:#a3a29c;
--line:#3a3a37;--ok:#9fd27a;--okbg:#22351a;--warn:#f2c46f;--warnbg:#3a2c10;--bad:#f2a0a0;--badbg:#3d1d1d;--accent:#85b7eb;
--blueled:#0092ff;--yellowled:#c3ff00;--gate:#c0a8ff}}
/* Sizes in rem: the browser's own font size (Settings > Appearance > Font size) and the page's Text size buttons scale
   all of it, and the layout reflows rather than overflowing. Nothing smaller than 15 px at the browser's default. */
html{font-size:100%}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:1.0625rem/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:76rem;margin:0 auto;padding:1rem;display:flex;flex-direction:column;gap:.875rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:.875rem 1rem}
.row{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:12rem}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9375rem}
.note,.hint{font-size:.9375rem}.hint{min-height:1.4em}
.tag{display:inline-block;padding:1px .5em;border-radius:6px;font-size:.9375rem}.ok{background:var(--okbg);color:var(--ok)}
.warn{background:var(--warnbg);color:var(--warn)}.bad{background:var(--badbg);color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.625rem}
.tile{background:var(--soft);border-radius:8px;padding:.625rem .75rem}.big{font-size:1.75rem;font-weight:500;line-height:1.25}
.cams{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.625rem}
.cam img,.cam .ph{width:100%;aspect-ratio:5/4;object-fit:contain;background:var(--soft);border-radius:6px;display:block}
.cam .ph{display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:.9375rem;padding:.5rem;box-sizing:border-box}
.cam [hidden]{display:none}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(22rem,100%),1fr));gap:.75rem}
button{font:inherit;padding:.375em .875em;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button:hover{background:var(--soft)}button:disabled{opacity:.45;cursor:default;background:var(--card)}button.stop{color:var(--bad);border-color:var(--bad)}button.go{color:var(--ok);border-color:var(--ok)}
input{font:inherit;padding:.375em .5em;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
label{font-size:.9375rem;color:var(--muted)}h2{font-size:1.25rem;font-weight:500;margin:0 0 .375rem}h3{font-size:1.0625rem;font-weight:600;margin:0 0 .25rem}
.logo{width:3rem;height:3rem;flex:none;display:block}.okmsg{color:var(--ok);margin-left:.5rem}.gone{border-left:4px solid var(--bad)}
.topline{display:flex;gap:.75rem;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:-.5rem 2px -.375rem}
.version{text-align:right;font-size:.9375rem}.textsize{display:flex;gap:.375rem;align-items:center;color:var(--muted);font-size:.9375rem}
.textsize button{padding:.125em .625em;min-width:2.75em}
.bar{height:6px;background:var(--soft);border-radius:3px;overflow:hidden}.bar div{height:6px;background:var(--ok)}
.copybar{max-width:28rem;margin:4px 0 2px}.copybar div{background:var(--accent)}
.bgcopy{border-left:4px solid var(--accent)}.copyeta{text-align:right;line-height:1.1;flex:none;min-width:9rem}
.copyeta .big{font-size:3rem;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text)}.copyeta .big.wait{font-size:1.5rem;font-weight:500}.copyeta .gap{margin-left:1.25rem}
.copyeta .lab{font-size:1.625rem;color:var(--muted)}#clock b{font-variant-numeric:tabular-nums}.bgcopy .copybar{max-width:none;height:10px}.bgcopy .copybar div{height:10px}
.err{color:var(--bad);font-size:1rem}ul{margin:.25rem 0 0;padding-left:1.25rem}li{margin:.25rem 0}
select{font:inherit;padding:.375em .5em;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);width:100%}
.steps li{list-style:none;margin-left:-1.25rem}.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:1rem}
.modal .card{max-width:36rem}[hidden]{display:none!important}
.bgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.25rem .75rem;margin:.25rem 0}
.types li{list-style:none;margin-left:-1.25rem;display:flex;gap:.5rem;align-items:baseline;padding:.25rem 0;border-top:1px solid var(--line)}
.types li span{flex:1}.star{padding:0 .5rem;font-size:1.25rem;line-height:1.4;border:none;background:none;color:var(--accent)}
.drive{display:flex;gap:.625rem;align-items:center}.drive svg{flex:none;display:block;width:3.75rem;height:3.75rem}
.drives .drive{margin-top:.5rem;font-size:1rem;line-height:1.35}.drives .drive svg{width:2.25rem;height:2.25rem}
.legend{font-size:.9375rem;margin-top:.125rem}.legend i{display:inline-block;width:.7em;height:.7em;border-radius:2px;margin:0 .25em 0 .5em}.legend i:first-child{margin-left:0}
.bgrid>div{display:flex;flex-direction:column;justify-content:flex-end}.bgrid input.changed{border-color:var(--accent);border-width:2px}details summary{cursor:pointer;font-size:1rem;margin-top:.375rem}
.ledrows{display:grid;grid-template-columns:auto minmax(6rem,1fr) minmax(6rem,1fr);gap:.375rem .75rem;align-items:end;margin:.5rem 0}
.ledrows b{font-weight:500;align-self:center;white-space:nowrap}
.sw{display:inline-block;width:.9em;height:.9em;border-radius:3px;margin-right:.4em;vertical-align:-.1em;border:1px solid var(--line)}
.boutpic{display:grid;align-items:center;margin:.375rem 0;row-gap:.25rem}.boutpic .lab{padding-right:.625rem;line-height:1.2;font-size:.9375rem}
.boutpic .lab b{font-weight:500;color:var(--text)}.boutpic svg{display:block;width:100%;height:2.25rem;grid-column:2/-1}
.boutpic .dur{font-size:.9375rem;line-height:1.25;text-align:center;padding:0 2px;min-width:0;color:var(--muted)}
.boutpic .zoom{color:var(--accent)}.nw{white-space:nowrap}.boutpic .ledpw{color:var(--text);white-space:nowrap}
.boutpic .axis{grid-column:2/-1;position:relative;height:1.9em;font-size:.9375rem;color:var(--muted);border-top:1px solid var(--muted)}
.boutpic .axis i{position:absolute;top:0;width:1px;height:.4em;background:var(--muted);transform:translateX(-50%)}
.boutpic .axis i:first-child{transform:none}.boutpic .axis i:nth-last-of-type(1){transform:translateX(-100%)}
.boutpic .axis span{position:absolute;top:.45em;transform:translateX(-50%);white-space:nowrap}.boutpic .axis span.first{transform:none}
.boutpic .axis span.last{transform:translateX(-100%)}.boutpic .axis span.zero{color:var(--text);font-weight:600}
</style></head><body><main>
<div class="topline"><div class="textsize" role="group" aria-label="Text size">Text size <button id="smaller" title="Smaller text" aria-label="Smaller text">A&minus;</button><button id="larger" title="Larger text" aria-label="Larger text">A+</button><span id="textpct"></span></div><div id="version" class="mono muted version"></div></div>

<div class="card row">
  <img class="logo" src="@LOGO@" alt="LUC3D" width="48" height="48">
  <div class="grow"><div id="rig" style="font-weight:500">campy session</div><div id="where" class="mono muted"></div></div>
  <span id="state" class="tag">connecting</span>
  <div id="clockbox" style="min-width:12rem"><div id="clock"></div><div class="bar"><div id="progress" style="width:0"></div></div></div>
  <button id="stop" class="stop" hidden>Stop</button>
  <button id="close" hidden title="Close the panel (nothing is recording)">Close the panel</button>
</div>
<div id="error" class="err" hidden></div>
<div id="gone" class="card gone" hidden><h2 id="gonetitle"></h2><p id="gonetext" class="muted"></p></div>
<div id="bgcopy" class="card bgcopy" hidden><div class="row" style="align-items:flex-start"><div class="grow"><b id="bgcopytitle">Copying in the background</b>
  <div id="bgcopyok" style="font-weight:500"></div>
  <div class="muted">Leave the computer on, and the panel's black window open, until it is done.</div></div>
  <div id="bgeta" class="copyeta" hidden><div><span id="bgetapct" class="big"></span> <span id="bgetapctlab" class="lab"></span><span id="bgetanum" class="big gap"></span> <span id="bgetalab" class="lab"></span></div><div id="bgetaend" class="muted"></div></div></div>
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
        <div class="row" style="margin-top:4px"><span id="mscan" class="muted grow note"></span><button id="mrescan" title="Look through panel.models' folders again">Look again</button></div></div>
      <div id="lengthbox" hidden><label for="length">Session length</label><select id="length"></select>
        <div class="muted" id="plantext"></div>
        <details id="boutbox" open hidden><summary>Bout settings <span id="boutsum" class="muted"></span></summary>
          <div id="boutfields" class="bgrid"></div>
          <div class="muted note" id="boutshape"></div>
          <div class="row" style="margin-top:4px"><span class="muted grow note">The config file isn't changed: these are for the recordings started here, and the next panel opened with this config starts from them. Each recording's session-config.yaml says which came from here.</span><button id="boutreset" hidden>Back to the config's</button></div></details>
        <label for="picoat">Start the Pico at (optional: otherwise a button, once every camera is ready)</label><input id="picoat" placeholder="7:20 PM"></div>
      <h3 style="margin-top:4px">Saved with the recording</h3>
      <label for="experiment">Experiment name</label><input id="experiment" placeholder="Social hierarchy, week 3">
      <label for="ids">Animal IDs, comma-separated</label><input id="ids" placeholder="M12, M14, M15">
      <label for="notes">Notes</label><input id="notes" placeholder="Cage 2, new bedding">
      <label for="copyto">Copy each recording to (optional)</label><input id="copyto" placeholder="\\server\share\my folder">
      <div class="muted note">After each recording, its folder is copied into this one in the background, while the next records, and every file is read back and checked byte for byte. Empty: no copy.</div>
      <label class="tick" style="display:flex;gap:8px;align-items:flex-start;margin-top:6px;font-weight:500"><input type="checkbox" id="copydelete" style="width:auto;margin-top:3px">
        <span>Delete each recording from this computer once its copy is checked</span></label>
      <div class="muted note" id="copydeletenote">Off: every recording stays here too. On: once every file is the same in the copy, the recording's folder is deleted here. It is kept if the recording didn't end cleanly, if any file differs, if a program still has a file in it open, or if anything in it changed after the check. This can't be undone.</div>
    </div>
    <div><div id="orphan" class="err" hidden></div><button id="stoporphan" class="stop" hidden>Stop that recording</button>
      <h3 style="margin-top:4px">Checks</h3>
      <div class="muted note">Green: fine. Yellow: you can record, but read it. Red: it stops the start until it is fixed.</div>
      <ul id="checks"></ul><button id="start" class="go">Start recording</button>
      <div id="ledlast" class="muted note" style="margin-top:.375rem" hidden></div>
      <div id="starterr" class="err"></div>
      <div id="boutpicbox" hidden><h3 style="margin-top:1rem">One bout, as the Pico runs it</h3>
        <div id="boutpic" class="boutpic" role="img"></div><div id="boutpicnote" class="muted note"></div></div></div>
  </div>
</div>

<div id="left" class="card" hidden><h2>Still running</h2><div id="lefttext" class="err"></div>
  <div class="row" style="margin-top:8px"><button id="stoppico" class="stop" hidden>Stop the Pico</button><button id="stopsglx" class="stop" hidden>Stop SpikeGLX's run</button></div></div>

<div id="devs" class="two" hidden>
  <div class="card"><h2>Steps</h2><ul id="steps" class="steps"><li class="muted">At Start: SpikeGLX's run, the gate check, the cameras, then the Pico.</li></ul></div>
  <div class="card"><h2 id="picotitle">Pico</h2><div id="picostate" class="muted">-</div>
    <div id="camready" style="margin-top:8px" hidden></div>
    <div id="picostart" class="row" style="margin-top:8px" hidden><button id="startpico" class="go">Start the Pico</button>
      <input id="picoat2" style="width:8em" placeholder="7:20 PM"><button id="picoatbtn">At this time</button></div>
    <div id="picoerr" class="err"></div>
    <h2 style="margin-top:10px">SpikeGLX</h2><div id="sglxstate" class="muted">-</div></div>
</div>

<div id="during" hidden>
  <div class="tiles" style="margin-bottom:12px">
    <div class="tile"><label>Disk free</label>
      <div class="drive"><span id="diskpie"></span><div><div class="big" id="disk">-</div><div class="muted" id="diskname"></div></div></div>
      <div class="muted" id="diskleft"></div><div id="disklegend" class="legend muted"></div><div id="drives"></div></div>
    <div class="tile"><label>Tracked in 3D</label><div class="big" id="good3d">-</div><div class="muted">animals, last second</div><div class="muted mono" id="usedmodels"></div></div>
    <div class="tile"><label>Programs</label><div id="programs"></div></div>
    <div class="tile"><label>Bookmarks</label><div class="big" id="nmarks">0</div><div class="muted" id="lastmark"></div></div>
  </div>
  <div class="cams" id="cams" style="margin-bottom:12px"></div>
  <div class="two">
    <div class="card"><h2>Warnings</h2><div class="muted note">Problems now, and the ones that went away (in grey). They stay in the recording's alerts.log.</div>
      <ul id="warnings"><li class="muted">None</li></ul></div>
    <div class="card"><h2>Bookmark</h2>
      <div class="muted note">A moment to find later: type what happened and press Add. It is saved with its exact time and frame in the recording's bookmarks.csv.</div>
      <div class="row"><input id="note" class="grow" placeholder="Mouse 3 rears at the feeder"><button id="mark">Add</button></div>
      <ul id="marks"></ul><div id="markerr" class="err"></div>
      <div style="margin-top:8px"><button id="viewer" title="The window with every camera's picture. Closing it never affects the recording.">Open the viewer again</button></div></div>
  </div>
</div>

<div id="copies" class="card" hidden><h2>Copies</h2><div class="muted note">Each recording's folder, copied into the folder in "Copy each recording to", one at a time.</div><div id="copywait" class="muted" hidden></div>
  <ul id="copylist"></ul><button id="closenow" class="stop" hidden>Close now, without the copies</button></div>

<div id="after" class="card" hidden><h2 id="aftertitle">After the recording</h2><div id="afterwhen" class="muted"></div>
  <h3 id="summaryhead" style="margin-top:10px">How it went</h3>
  <ul id="summary"></ul><div id="summarynote" class="muted" hidden>From each camera's metadata.csv and the recording's alerts.log, in its folder.</div>
  <h3 style="margin-top:10px">Steps after the recording</h3><ul id="post"></ul>
  <p class="muted">Closing this page never stops a recording or these steps. You can start the next recording above at any time.</p></div>

<div id="stopdlg" class="modal" hidden><div class="card"><h2>End the recording?</h2>
  <p class="muted" id="stoptext"></p>
  <div class="row"><button id="keep">Keep recording</button><button id="stopbout" class="stop">Stop after this bout</button><button id="stopnow" class="stop">Stop now</button></div></div></div>
<div id="leddlg" class="modal" hidden><div class="card" role="dialog" aria-labelledby="ledtitle"><h2 id="ledtitle">LED power, before connecting the animal</h2>
  <p class="muted note">Set each LED's current on the Doric LED driver, then measure its power at the tip of the fiber with the power meter, before
    the fiber is connected to the animal. Enter both, or check that the last ones still hold. They are saved with the recording (its session.json).</p>
  <div id="ledrows" class="ledrows"></div><div id="ledwhen" class="muted note"></div><div id="lederr" class="err"></div>
  <div class="row" style="margin-top:.5rem"><button id="ledno">Cancel</button><button id="ledyes" class="go">Confirm and start</button></div></div></div>
<div id="picodlg" class="modal" hidden><div class="card"><h2>Start the Pico?</h2>
  <p class="muted" id="picotext"></p>
  <div class="row"><button id="picono">Not yet</button><button id="picoyes" class="go">Start the Pico</button></div></div></div>

</main><script>
const key = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id);
let thumbs = 0, last = null, stopArmed = 0;
function q(path){ return path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(key); }
async function postFull(path, body){
  const r = await fetch(q(path), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body||{})});
  return await r.json().catch(() => ({}));
}
async function post(path, body){ const j = await postFull(path, body); return j.error || null; }
function offerCreate(path){
  // The copy folder isn't there yet, but the folder it goes in is: a button that makes it (one level), then Start again
  const b = document.createElement("button"); b.textContent = "Create this folder"; b.style.marginLeft = "8px";
  b.title = "Make " + path + " (only this last folder: the one it goes in is already there)";
  b.onclick = async () => { b.disabled = true; const e = await post("/copy-folder", {path});
    $("starterr").textContent = e || ""; if(!e){ const ok = document.createElement("span"); ok.className = "okmsg";
      ok.textContent = "Made " + path + ". Press Start recording again."; $("starterr").appendChild(ok); } };
  $("starterr").appendChild(b);
}
function hms(s){ if(s==null) return "-"; s=Number(s); if(!isFinite(s)) return s>0 ? "\u221e" : "-"; s=Math.max(0,Math.round(s)); return Math.floor(s/3600)+":"+String(Math.floor(s/60)%60).padStart(2,"0")+":"+String(s%60).padStart(2,"0"); }
function esc(t){ return String(t==null?"":t).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function dur(s){ s = Math.max(0, Math.floor(s)); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
let camsWereReady = false; const baseTitle = document.title;
function chime(){ try { const a = new (window.AudioContext||window.webkitAudioContext)(); [0, 0.18].forEach((t, i) => { const o = a.createOscillator(), g = a.createGain(); o.frequency.value = i ? 1100 : 880; g.gain.setValueAtTime(0.15, a.currentTime+t); g.gain.exponentialRampToValueAtTime(0.001, a.currentTime+t+0.16); o.connect(g); g.connect(a.destination); o.start(a.currentTime+t); o.stop(a.currentTime+t+0.17); }); } catch(e) {} }
// The disk tile's drives: a size as the page says it, and a donut of a drive (used, the rest of the plan's estimate, free)
function size(gb){ return gb >= 999.5 ? (gb/1000).toFixed(1)+"\u00a0TB" : Math.round(gb).toLocaleString()+"\u00a0GB"; } // never "1.0" and "TB" on two lines
const PIE = {used: "var(--muted)", plan: "var(--warn)", short: "var(--bad)", free: "var(--ok)"};
function pie(d, px){
  const t = d.total_gb, plan = Math.min(d.plan_gb || 0, d.free_gb), short = (d.plan_gb || 0) > d.free_gb;
  if(!(t > 0)) return "";
  const parts = [[d.used_gb, PIE.used, "used"], [plan, short ? PIE.short : PIE.plan, "the rest of the plan (an estimate)"],
    [d.free_gb - plan, PIE.free, short ? "free" : "free after it"]];
  let at = 0, out = '<svg width="'+px+'" height="'+px+'" viewBox="0 0 42 42" role="img" aria-label="'+esc(d.name)+": "+
    size(d.free_gb)+' free of '+size(t)+'"><circle cx="21" cy="21" r="15.915" fill="none" stroke="var(--line)" stroke-width="7"/>';
  parts.forEach(([gb, color, name], i) => { // a slice of 100 around (r 15.915), from the top, clockwise; the plan's at least 1
    let p = Math.max(0, gb) / t * 100; if(i == 1 && gb > 0) p = Math.max(p, 1); p = Math.min(p, 100 - at); if(p <= 0) return;
    out += '<circle cx="21" cy="21" r="15.915" fill="none" stroke="'+color+'" stroke-width="7" stroke-dasharray="'+p.toFixed(2)+' '+
      (100-p).toFixed(2)+'" stroke-dashoffset="'+(25-at).toFixed(2)+'"><title>'+esc(name)+": "+size(gb)+'</title></circle>'; at += p; });
  return out + '</svg>';
}
function renderDrives(D){
  const ds = D.drives || [], rec = ds.find(d => d.recording);
  $("diskpie").innerHTML = rec ? pie(rec, 54) : "";
  $("diskname").textContent = rec ? "of "+size(rec.total_gb)+" on "+rec.name+(rec.copies ? ", with the copies" : "") : "";
  $("disklegend").innerHTML = rec ? '<i style="background:'+PIE.used+'"></i>used'+(rec.plan_gb != null ?
    '<i style="background:'+(rec.plan_gb > rec.free_gb ? PIE.short : PIE.plan)+'"></i>the plan' : "")+'<i style="background:'+PIE.free+'"></i>free' : "";
  $("drives").className = "drives";
  $("drives").innerHTML = ds.filter(d => !d.recording).map(d => '<div class="drive">'+pie(d, 30)+'<div><b>'+esc(d.name)+'</b> '+
    size(d.free_gb)+' free of '+size(d.total_gb)+'<br><span class="muted">'+(d.copies ? "the copies go here" : "")+'</span></div></div>').join("");
}
function tag(text, kind){ return '<span class="tag '+kind+'">'+esc(text)+'</span>'; }
// The page's clocks tick every second on their own (paint, below), counted from the last status: the Pico's time from
// its start on this computer's clock (the server's now: the Pico's status is read only every 2 s, and it writes it
// once a second), the recording's from its elapsed_s, a copy's time left counted down from its estimate
let clock = null, clockShown = {key: null, t: -1}, clockHtml = null, offsets = [], serverOffset = 0, bgEta = null, etaHtml = null;
function serverNow(){ return (Date.now() + serverOffset) / 1000; }
function picoTime(P){ // the Pico's session time now: from its start (this computer's clock), up to its end
  if(P.started==null || !P.alive) return P.session_s;
  const t = Math.max(0, serverNow() - P.started); return P.ends_s!=null ? Math.min(t, P.ends_s) : t;
}
function steady(key, t){
  // Never a second back from a poll's small delay (the clock re-synced to each status), only a real correction (2 s+)
  if(t==null) return t;
  if(clockShown.key == key && t < clockShown.t && t > clockShown.t - 2) t = clockShown.t;
  clockShown = {key, t}; return t;
}
function paintClock(){
  const C = clock; if(!C) return; let html, pct = 0;
  if(C.text!=null) html = C.text;
  else if(C.pico){ const t = steady(C.key, picoTime(C.pico)), E = C.pico.ends_s;
    pct = E && t!=null ? Math.min(100, 100*t/E) : 0; html = "<b>"+hms(t==null ? null : Math.floor(t))+"</b> <span class=muted>of "+hms(E)+", the Pico's"+percent(E && t!=null, pct)+"</span>"; }
  else { const t = steady(C.key, C.elapsed + (C.ticking ? Math.max(0, (Date.now()-C.at)/1000) : 0));
    pct = C.planned ? Math.min(100, 100*t/C.planned) : 0; html = "<b>"+hms(Math.floor(t))+"</b> <span class=muted>recorded, of "+hms(C.planned)+percent(C.planned, pct)+"</span>"; }
  if(html != clockHtml){ $("clock").innerHTML = clockHtml = html; }
  $("progress").style.width = pct ? pct.toFixed(2)+"%" : "0";
}
function percent(known, pct){ return known ? " · <b>"+Math.floor(pct)+"%</b>" : ""; } // rounded down: 100% only once it is
function mss(s){ s = Math.max(0, Math.floor(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  return (h ? h+":"+String(m).padStart(2,"0") : m)+":"+String(s%60).padStart(2,"0"); }
function clockAt(ms){ return new Date(ms).toLocaleTimeString([], {hour:"numeric", minute:"2-digit", hour12:true}); }
function etaLeft(j){ // seconds left in the copy's phase: the card's smoothed count for the copy it shows, else the status's
  if(bgEta && bgEta.folder == j.folder && bgEta.phase == j.phase && bgEta.end!=null) return Math.max(0, (bgEta.end - Date.now())/1000);
  return j.eta_s;
}
function followEta(j){
  // The copy the card shows: its time left as an end time on this page's clock, moved a third of the way to each new
  // estimate (the copy's progress is measured every 3 s), so it neither jitters nor jumps back further than the
  // estimate did; a new copy or phase starts from its own
  if(!j){ bgEta = null; return; }
  const same = bgEta && bgEta.folder == j.folder && bgEta.phase == j.phase;
  // How much of it is done (of the bytes to copy, or to read back), as last measured
  const done = j.phase=="checking" ? j.checked_bytes : j.done_bytes, total = j.phase=="checking" ? j.check_bytes : j.bytes;
  const pct = total ? Math.min(100, 100*(done||0)/total) : null;
  if(j.eta_s==null){ bgEta = {folder: j.folder, phase: j.phase, end: null, pct}; return; }
  const target = Date.now() + j.eta_s*1000;
  bgEta = {folder: j.folder, phase: j.phase, pct, end: same && bgEta.end!=null ? bgEta.end + (target - bgEta.end)/3 : target};
}
function paintEta(){
  const E = bgEta; $("bgeta").hidden = !E; if(!E) return;
  // As big as the time left, in front of it: how much is done (65% copied), then the time left (2:07 left)
  let pct = "", pctLab = "", num, lab = "", end = "", wait = false;
  if(E.phase!="copying" && E.phase!="checking"){ num = "finishing…"; wait = true; }
  else { if(E.pct!=null){ pct = Math.floor(E.pct)+"%"; pctLab = E.phase=="checking" ? "checked" : "copied"; }
    if(E.end==null){ num = "estimating…"; wait = true; if(!pct) end = E.phase=="checking" ? "the time left to check" : "the time left to copy"; }
    else { num = mss(Math.max(0, (E.end - Date.now())/1000)); lab = "left";
      end = "done around "+clockAt(E.end)+(E.phase=="checking" ? "" : ", then checked"); } }
  const html = [pct, pctLab, num, lab, end, wait].join("|"); if(html == etaHtml) return; etaHtml = html;
  $("bgetapct").textContent = pct; $("bgetapctlab").textContent = pctLab;
  $("bgetanum").textContent = num; $("bgetanum").className = "big"+(pct ? " gap" : "")+(wait ? " wait" : ""); $("bgetalab").textContent = lab; $("bgetaend").textContent = end;
}
function paint(){ paintClock(); paintEta(); }
function render(s){
  last = s;
  // The server's clock against this page's: the largest offset of the last few statuses (a slow answer only makes it
  // look smaller)
  if(s.now!=null){ offsets.push(s.now*1000 - Date.now()); if(offsets.length > 10) offsets.shift(); serverOffset = Math.max(...offsets); }
  // Which campy the panel runs (version, branch, commit), in the top right corner, as LUC3D shows its own
  const V = s.version || {}; $("version").textContent = V.text || ""; $("version").title = V.title || "";
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
    clock = on ? {key: "pico|"+DP.started, pico: DP} : {text: live ? "<span class=muted>"+(s.devices.pico_started ? "the Pico's session has ended" : "the Pico hasn't started")+"</span>" : ""};
  } else {
  // Only while it records: after it, the previous recording's time against its length meant nothing (1:19 of 1:00)
  const shown = (running && rec.elapsed_s!=null) || st=="post"; $("clockbox").hidden = !shown;
  // Counted on from the status's elapsed_s while it records (the page shows it every second, polled or not)
  clock = shown ? {key: "rec|"+rec.started, elapsed: rec.elapsed_s, at: Date.now(), planned: rec.planned_s, ticking: st=="recording"} : {text: ""};
  }
  paintClock();
  $("error").hidden = !s.error; $("error").textContent = s.error || "";
  $("before").hidden = !(st=="ready" || st=="done" || st=="failed");
  if(st=="recording") $("starterr").textContent = ""; // a refusal from before this recording no longer applies
  $("orphan").hidden = $("stoporphan").hidden = !s.orphan;
  if(s.orphan) $("orphan").textContent = "A recording an earlier panel started is still running, in " + s.orphan.folder +
    " (its window was closed; the recording went on). Stop it, or wait for it to end, before starting another.";
  $("checks").innerHTML = s.checks.map(c => "<li>"+tag(c.level=="block"?"stops the start":c.level=="warn"?"warning":"ok",
    c.level=="block"?"bad":c.level=="warn"?"warn":"ok")+" "+esc(c.text)+(c.action=="pause-updates"?' <button onclick="pauseUpdates(this)">Pause Windows Update</button>':"")+"</li>").join("");
  renderModels(s.models);
  const LP = s.led_power; ledPower = LP; $("ledlast").hidden = !LP;
  if(LP) $("ledlast").innerHTML = "At Start, the page asks for each LED's power at the fiber tip"+(LP.last && LP.last.leds ? ". Last confirmed "+
    esc(confirmedWhen(LP.last.at))+": "+LP.last.leds.map(ledText).join("; ") : "")+".";
  const LM = L.live_models; $("usedmodels").textContent = LM && LM.centroid ? base(LM.centroid)+" + "+base(LM.instance) : "";
  const lengths = s.lengths || []; $("lengthbox").hidden = !lengths.length;
  if(lengths.length && $("length").options.length != lengths.length){ $("length").innerHTML = lengths.map(l => '<option value="'+l.seconds+'">'+esc(l.name)+'</option>').join(""); }
  if(lengths.length && document.activeElement != $("length")) $("length").value = String(s.length);
  renderBouts(s.bouts);
  const L0 = lengths.find(l => l.seconds == s.length); $("plantext").textContent = L0 ? L0.text + (L0.videoFolder ? ", into "+L0.videoFolder : "") : "";
  // Another length (or experiment) picked: what was typed into the copy box stays with the one left, for when it is
  // picked again. A length with a copy folder of its own (pico.lengths' copyTo) has a box of its own; the others share one
  const lenKey = s.config + "|" + (L0 ? L0.seconds : ""), lenMoved = lenKey != copyLen && document.activeElement != $("copyto");
  if(lenMoved && copySlot && $("copyto").value != copyGiven) copyTyped[copySlot] = $("copyto").value;
  if(refill){ refill = false; // another experiment picked: its config's fields, where it has them (what was typed stays otherwise)
    if(s.fields.experiment) $("experiment").value = s.fields.experiment; if(s.fields.animal_ids) $("ids").value = s.fields.animal_ids;
    $("copyto").value = copyGiven = s.fields.copy_to||""; $("copydelete").checked = !!s.fields.copy_delete; } // the copy destination is each config's own
  if(!$("experiment").dataset.set){ $("experiment").value = s.fields.experiment||""; $("ids").value = s.fields.animal_ids||""; $("copyto").value = copyGiven = s.fields.copy_to||""; $("copydelete").checked = !!s.fields.copy_delete; $("experiment").dataset.set=1; }
  $("copydeletenote").style.color = $("copydelete").checked ? "var(--bad)" : "";
  // The length picked (or the page opened on it) fills the box: what was typed for it on this page, else for a length
  // with its own copyTo what the last Start had for it (copyBox, that copyTo unless changed there), else the config's
  if(lenMoved){ copySlot = L0 ? s.config + "|" + (L0.copyTo ? L0.seconds : "*") : ""; // no lengths: the config's, as refill
    if(L0) $("copyto").value = copyGiven = copySlot in copyTyped ? copyTyped[copySlot] :
      L0.copyTo ? (L0.copyBox != null ? L0.copyBox : L0.copyTo) : s.fields.copy_to||"";
    copyLen = lenKey; }
  // The server checks the room in the folder the box holds, which Start copies into: told whenever that changes (not
  // while it is being typed in, so a share half typed isn't looked for)
  const box = $("copyto").value.trim();
  if(document.activeElement != $("copyto") && box !== s.fields.copy_box && box !== copyAsked){
    copyAsked = box; post("/check", {copy_to: box}).finally(() => { copyAsked = null; }); }
  renderCopies(s.copies || {jobs: []}, L.folder, st);
  $("during").hidden = !(st=="recording" || st=="stopping" || st=="starting live");
  $("disk").textContent = s.disk.free_gb!=null ? Math.round(s.disk.free_gb)+" GB" : "-";
  // With a Pico's plan: what the rest of it still needs (SpikeGLX and the videos, estimated), not hours at the rate so far
  const D_ = s.disk, short = D_.plan_left_gb!=null && D_.free_gb!=null ? D_.plan_left_gb - D_.free_gb : null;
  $("diskleft").innerHTML = D_.plan_left_gb!=null ? "the rest of the plan needs about "+Math.round(D_.plan_left_gb).toLocaleString()+" GB (an estimate)"+
    (short > 0 ? '<br><span class="err">'+Math.round(short).toLocaleString()+" GB short</span>" : "") :
    D_.hours_left!=null ? "about "+D_.hours_left+" h at this rate" : "";
  renderDrives(D_);
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
  // Every step, in order: the running one with its latest line, time so far and progress; those with nothing to do greyed
  const plan = (L.post_plan || STEP_ORDER).filter(k => STEPS[k]);
  $("post").innerHTML = plan.map(k => { const e = P[k];
    if(!e) return "<li class=muted>"+STEPS[k]+(st=="post"?": waiting":"")+"</li>";
    if(e.state=="running"){ const t = since(e.time);
      return "<li>"+tag("running","warn")+" <b>"+STEPS[k]+"</b>"+(t!=null ? " <span class=muted>"+dur(t)+" so far</span>" : "")+(e.message ? ": "+esc(String(e.message).replace(/^\[\d+ s\] /, "")) : "")+
        (e.fraction!=null && e.fraction > 0 ? '<div class="bar copybar"><div style="width:'+(100*e.fraction).toFixed(0)+'%"></div></div>' : "")+"</li>"; }
    const idle = e.state=="skipped" && NOTHING.test(e.message||"");
    return "<li"+(idle ? " class=muted" : "")+">"+tag(e.state, e.state=="done"||e.state=="copying"?"ok":e.state=="failed"?"bad":idle?"":"warn")+" "+STEPS[k]+": "+esc(e.message)+(e.bar||"")+"</li>"; }).join("");
  if(st=="post"){ // the header: which step of how many, and a bar of the whole
    const G = postProgress(P, plan);
    $("clock").innerHTML = "<b>Step "+G.i+" of "+G.n+"</b> <span class=muted>"+(G.running ? esc(STEPS[G.running]) : "starting")+
      (G.running && since(P[G.running].time)!=null ? " · "+dur(since(P[G.running].time)) : "")+"</span>";
    $("progress").style.width = (100*G.fraction).toFixed(0)+"%";
  }
}
let typeSig = "", listSig = "", choosing = false, refill = false;
const PROGRAMS = {acquire: "Cameras and video files (campy-acquire)", view: "Viewer window", watch: "Alerts (campy-watch)",
  "campy-live": "Live tracking (campy-live)"};
const STEPS = {remux: "Video files", join: "Joined video files", align: "Cameras on the trigger's timeline", calibrate: "Calibration (calibrat3)",
  export: "Tracking files for LUC3D", copy: "Copy", run: "Extra commands"};
const STEP_ORDER = ["remux","join","align","calibrate","export","copy","run"];
const FINISHED = ["done","skipped","failed"];
function since(iso){ const t = Date.parse(iso); return isNaN(t) ? null : Math.max(0, (Date.now() - t) / 1000); }
// Steps with nothing to do: shown greyed, with why
const NOTHING = /not a calibration recording|no camera's video is in more than one file|campy-live did not run|no commands|no copy\.to|calibrate is off/;
function postProgress(P, plan){
  // How far the steps after the recording have got: finished steps, plus the running one's own progress (half way
  // when it doesn't say). {n, i (1-based, the running one), running, fraction}
  const n = plan.length; let done = 0, running = null;
  plan.forEach(k => { const e = P[k]; if(e && FINISHED.includes(e.state)) done++; else if(e && e.state=="running") running = k; });
  const part = running ? (P[running].fraction!=null && P[running].fraction > 0 ? P[running].fraction : 0.5) : 0;
  return {n, i: Math.min(n, done + 1), running, fraction: n ? Math.min(1, (done + part) / n) : 0};
}
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
  if(j.state=="copying" && j.phase=="checking") return "checking the copy byte for byte: "+gb(j.checked_bytes)+" of "+gb(j.check_bytes)+" GB read back, "+dur(j.seconds||0)+copyEta(j);
  if(j.state=="copying" && j.phase=="deleting") return "every file the same in the copy: deleting it from this computer";
  if(j.state=="copying" && j.phase=="finishing") return "every file the same in the copy: copying its logs";
  if(j.state=="copying") return "copying into "+j.to+": "+gb(j.done_bytes)+" of "+gb(j.bytes)+" GB, "+dur(j.seconds||0)+copyEta(j);
  if(j.state=="waiting") return "waits for the copies before it, into "+j.to+(j.delete ? ", then deleted here once checked" : "");
  return j.message || j.state;
}
function copyCheck(j){
  // What the byte-for-byte check found, and what became of the folder here: tags beside the copy's state
  let t = "";
  if(j.mismatched) t += " "+tag(j.mismatched+" not the same", "bad");
  else if(j.verified!=null && j.state=="done") t += " "+tag("verified", "ok");
  if(j.state=="done" && j.delete) t += " "+(/deleted from this computer/.test(j.message||"") ? tag("deleted here", "ok") : tag("kept here", "warn"));
  return t;
}
function copyEta(j){
  // How long the rest of the copy (or of its check) takes at its average rate so far (the server's eta_s, once it has
  // one), and the clock time it should end
  const left = etaLeft(j); if(left==null) return "";
  const m = Math.round(left/60), end = clockAt(Date.now() + left*1000);
  return ", " + (left < 60 ? "under a minute" : m < 60 ? "about "+m+" min" : "about "+Math.floor(m/60)+" h "+(m%60)+" min") + " left (done around "+end+")";
}
function copyBar(j){
  // A copy under way: a bar of the bytes copied so far (none until its size is known), then of the bytes checked
  if(j.state=="copying" && j.phase=="checking" && j.check_bytes)
    return '<div class="bar copybar"><div style="width:'+Math.min(100, 100*(j.checked_bytes||0)/j.check_bytes).toFixed(1)+'%"></div></div>';
  if(j.state!="copying" || !j.bytes) return "";
  return '<div class="bar copybar"><div style="width:'+Math.min(100, 100*(j.done_bytes||0)/j.bytes).toFixed(1)+'%"></div></div>';
}
function renderCopies(C, folder, st){
  const jobs = C.jobs || [];
  // Copies still to do, at the top of the page in every state (a new recording too) until the last is done: not only in
  // the Copies card, below the cameras while one records
  const now = jobs.filter(j => j.state=="copying"), queued = jobs.filter(j => j.state=="waiting");
  $("bgcopy").hidden = !now.length && !queued.length;
  // The big time left, top right: the copy under way (one at a time), not the ones waiting behind it
  followEta(now[0]); paintEta();
  $("bgcopytitle").textContent = now.length ? "Copying in the background" : "Copies waiting to start";
  // Said plainly: the copy is no reason to wait (it keeps going while the next one records)
  $("bgcopyok").textContent = ["ready","done","failed"].includes(st) ? "You can start a new recording now: the copy goes on beside it and doesn't disturb it." :
    ["recording","starting","checking","preparing","starting live","stopping"].includes(st) ? "It doesn't disturb this recording." : "";
  $("bgcopytext").innerHTML = now.map(j => "<div><b>"+esc(j.name)+"</b> "+esc(copyText(j))+copyBar(j)+"</div>").join("") +
    (queued.length ? '<div class="muted">'+(now.length ? "Then " : "")+queued.map(j => esc(j.name)).join(", ")+(queued.length==1 ? " waits" : " wait")+" to be copied.</div>" : ""); $("copies").hidden = !jobs.length && !C.close_when_copied;
  $("copywait").hidden = $("closenow").hidden = !C.close_when_copied;
  if(C.close_when_copied) $("copywait").textContent = C.pending ? "The panel closes by itself once these copies are done ("+C.pending+" left)." : "Closing…";
  $("copylist").innerHTML = jobs.map(j => "<li>"+tag(j.state, j.state=="done"?"ok":j.state=="failed"||j.state=="left"?"bad":"warn")+copyCheck(j)+" <b>"+esc(j.name)+"</b> "+esc(copyText(j))+copyBar(j)+"</li>").join("");
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
let copyAsked = null; // the box's folder, while the server is being told it
let copyLen = ""; // the experiment and length whose copy folder the box was last given
// The copy box: its slot (the experiment and a length with a copyTo of its own, or "*" for the others), what the page
// last put in it, and what was typed for each slot left
let copySlot = "", copyGiven = "", copyTyped = {};
let boutSig = "", ledPower = null; // ledPower: the status's led_power, for the bout drawing's LED rows
// An LED row's last confirmed power (panel.led_power's names matched by colour: "Blue LED (465 nm)" for blue), or null
function ledPowerOf(colour){
  const leds = ((ledPower||{}).last||{}).leds || [];
  return leds.find(l => String(l.led).toLowerCase().includes(colour)) || null; }
// An LED row's name in panel.led_power ("Blue LED (465 nm)" for blue), for its wavelength, or null
function ledNameOf(colour){ return ((ledPower||{}).leds || []).find(n => String(n).toLowerCase().includes(colour)) || null; }
// The drawing of a bout: a row for each of the Pico's outputs, its time cut into stretches (5 s before the gate
// opens, with every output low; gate open to camera on, camera to the LEDs, the LEDs, LEDs to camera off, camera off
// to gate closed, to the next bout). Each stretch has its width whatever it lasts (a cartoon), with the time axis
// under it in seconds from the gate's opening (0 s); the LEDs' stretch is to scale in ms, with the camera's real
// pulses in it.
// The LEDs' own colours: 465 nm is about rgb(0, 146, 255) and 560 nm rgb(195, 255, 0), a yellow-green (as a spectrum
// is shown on a screen); in the light theme the same hues darker, to stand out on white
const LED_COLOUR = {blue: "var(--blueled)", yellow: "var(--yellowled)"};
// A stretch's length, rounded as the drawing says it: "235 s", "4.5 s", "30 ms"
function secs(t){ return t >= 9.95 ? Math.round(t).toLocaleString()+"\u00a0s" : t >= 0.9995 ? (+t.toFixed(1))+"\u00a0s" : (+(t*1000).toFixed(1))+"\u00a0ms"; }
function boutPicture(D){
  const b = D && D.bouts && D.bouts[0];
  $("boutpicbox").hidden = !b || !b.leds.length; if($("boutpicbox").hidden) return;
  const n = D.bouts[1], frame = 1 / D.fps, L = b.leds;
  // The LEDs' stretch: from a frame and a half before the first LED to a frame and a half after the last (within the camera)
  const z0 = Math.max(b.camera[0], L[0].on - 1.5*frame), z1 = Math.min(b.camera[1], L[L.length-1].off + 1.5*frame);
  const cuts = [-5, 0, b.camera[0], z0, z1, b.camera[1], b.gate[1], D.interval_s]; // from 5 s before the gate opens
  const weights = [1.6, 1, 1, 2.2, 1.6, 1, 1.4].map((w, i) => cuts[i+1] - cuts[i] > 1e-9 ? w : 0.12);
  const W = weights.reduce((a, w) => a + w, 0) * 100, H = 30, hi = 5, lo = 25;
  const starts = weights.reduce((a, w, i) => (a.push(a[i] + w*100), a), [0]);
  const x = t => { let i = cuts.findIndex((c, k) => k > 0 && t <= c + 1e-9); if(i < 1) i = t <= 0 ? 1 : cuts.length - 1;
    const a = cuts[i-1], c = cuts[i]; return starts[i-1] + (c > a ? (t - a) / (c - a) : 0) * weights[i-1]*100; };
  const wave = spans => { let d = "M0,"+lo; spans.forEach(([on, off]) => { const a = x(on).toFixed(1), c = Math.max(x(off), x(on)+1.2).toFixed(1);
    d += " L"+a+","+lo+" L"+a+","+hi+" L"+c+","+hi+" L"+c+","+lo; }); return d + " L"+W+","+lo; };
  const fill = (spans, colour) => spans.map(([on, off]) => '<rect x="'+x(on).toFixed(1)+'" y="'+hi+'" width="'+Math.max(x(off)-x(on), 1.2).toFixed(1)+
    '" height="'+(lo-hi)+'" fill="'+colour+'" opacity=".3"/>').join("");
  // The camera's pulses: real ones in the LEDs' stretch, a comb (as many as fit) elsewhere
  const pulses = [];
  for(let k = Math.ceil((z0 - b.camera[0]) / frame - 1e-6); b.camera[0] + k*frame < z1 - 1e-9; k++) pulses.push(b.camera[0] + k*frame);
  [[b.camera[0], z0, 2], [z1, b.camera[1], 4]].forEach(([a, c, i]) => { if(c - a <= 1e-9) return;
    const m = Math.max(2, Math.round(weights[i] * 6)); for(let j = 0; j < m; j++) pulses.push(a + (c - a) * (j + 0.3) / m); });
  const pulseSpans = pulses.map(t => [t, t + Math.min(D.pulse_s, frame/2)]);
  const guide = '<rect x="'+starts[3]+'" y="0" width="'+(weights[3]*100)+'" height="'+H+'" fill="var(--soft)"/>' +
    starts.slice(1, -1).map(v => '<line x1="'+v+'" x2="'+v+'" y1="0" y2="'+H+'" stroke="var(--line)" stroke-dasharray="2 2" vector-effect="non-scaling-stroke"/>').join("");
  const svg = (inner, title) => '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" aria-hidden="true">'+guide+inner+'</svg>';
  const line = (d, colour) => '<path d="'+d+'" fill="none" stroke="'+colour+'" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
  const rows = D.rows.length == 4 ? D.rows : [{pin:"", name:"Record gate"}, {pin:"", name:"Camera"}, {pin:"", name:"Blue LED"}, {pin:"", name:"Yellow LED"}];
  const led = name => L.filter(l => l.name == name).map(l => [l.on, l.off]);
  const signals = [[[b.gate], "var(--gate)"], [pulseSpans, "var(--text)"], [led("blue"), LED_COLOUR.blue], [led("yellow"), LED_COLOUR.yellow]];
  let html = "";
  rows.forEach((r, i) => { const [spans, colour] = signals[i];
    const led = i >= 2 ? (i == 2 ? "blue" : "yellow") : null, pw = led ? ledPowerOf(led) : null, full = led ? ledNameOf(led) : null;
    const nm = full && /(\d{3})\s*nm/i.exec(full); // the wavelength, and its colour (the line's, without one)
    const mark = !led ? "" : full && nmColour(full) ? swatch(full) : '<i class="sw" style="background:'+colour+'"></i>';
    html += '<div class="lab"><b>'+mark+esc(r.name)+'</b>'+(r.pin || nm ? '<br><span class="muted">'+esc([r.pin, nm ? nm[1]+"\u00a0nm" : ""].filter(x => x).join(" \u00b7 "))+'</span>' : '')+
      (pw ? '<br><span class="ledpw">'+(+pw.driver_mA)+'\u00a0mA \u00b7 '+(+pw.fiber_tip_uW)+'\u00a0\u00b5W</span>' : '')+'</div>' +
      svg((i >= 2 ? fill(spans, colour) : "") + line(wave(spans), colour)); });
  // The LEDs' stretch in words: "yellow 50 ms", "150 ms dark", "blue 50 ms", each kept on one line
  const ledParts = []; L.forEach((l, i) => { if(i && l.on - L[i-1].off > 1e-6) ledParts.push(secs(l.on - L[i-1].off)+" dark");
    ledParts.push(l.name+" "+secs(l.off - l.on)); });
  const ledText = ledParts.join(", ");
  // The time axis, in seconds from the gate's opening: each stretch's edge (the LEDs' once, at their middle); labels
  // that would overlap are hidden once drawn (0 s and the two ends kept first)
  const ticks = [[-5, 0], [0, starts[1]], [b.camera[0], starts[2]], [L[0].on, starts[3] + weights[3]*50], [b.camera[1], starts[5]],
    [b.gate[1], starts[6]], [D.interval_s, W]];
  const axis = ticks.map(([t, at]) => '<i style="left:'+(at/W*100).toFixed(2)+'%"></i>').join("") + ticks.map(([t, at], i, all) =>
    '<span class="'+(i == 0 ? "first" : i == all.length-1 ? "last" : "")+(t == 0 ? " zero" : "")+'" style="left:'+(at/W*100).toFixed(2)+'%">'+
    (t < 0 ? "\u2212"+secs(-t) : t == 0 ? "0\u00a0s" : secs(t))+'</span>').join("");
  html += '<div class="lab muted">time</div><div class="axis">'+axis+'</div>' +
    '<div class="dur zoom" style="grid-column:4 / 7">'+ledParts.map(t => '<span class="nw">'+esc(t)+'</span>').join(", ")+'</div>';
  const box = $("boutpic"); box.style.gridTemplateColumns = "auto " + weights.map(w => w+"fr").join(" "); box.innerHTML = html;
  const kept = [], marks = [...box.querySelectorAll(".axis span")];
  [...marks.filter(m => m.classList.contains("zero")), marks[0], marks[marks.length-1], ...marks].forEach(m => {
    if(kept.includes(m)) return; const r = m.getBoundingClientRect();
    if(kept.some(k => { const q = k.getBoundingClientRect(); return r.left < q.right + 6 && q.left < r.right + 6; })) m.style.visibility = "hidden";
    else kept.push(m); });
  box.setAttribute("aria-label", "One bout: gate open "+secs(b.gate[1])+", camera from "+secs(b.camera[0])+" to "+secs(b.camera[1])+", "+ledText+
    ", next bout at "+secs(D.interval_s));
  $("boutpicnote").textContent = "Time from the gate opening (0 s), from 5 s before it to the next bout at "+secs(D.interval_s)+". The stretches aren't "+
    "to scale, except the LEDs' (blue text, in ms), which shows the camera's real pulses at "+(+D.fps.toFixed(3))+" a second."+
    (n ? " Drawn: a long bout. A normal bout's camera stops "+secs(n.camera[1] - n.leds[n.leds.length-1].off)+" after the LEDs, and its gate closes "+
      secs(n.gate[1])+" after it opened." : " Every bout is long.") + ledNote();
}
function ledNote(){
  // The LED rows' power: the driver current and the power at the fiber tip, as last confirmed at a Start
  const P = (ledPower||{}).last; if(!ledPower) return "";
  if(!P || !P.at) return " The LEDs' power is asked for at Start.";
  const old = new Date(P.at).toDateString() != new Date().toDateString();
  return " Under each LED: its driver current and power at the fiber tip, as confirmed "+confirmedWhen(P.at)+(old ? " (an earlier day: measure again before connecting the animal)" : "")+".";
}
function renderBouts(B){
  $("boutbox").hidden = !B; if(!B){ $("boutpicbox").hidden = true; return; }
  const fields = B.fields || [];
  // The fields only when the settings change, so a field being typed in isn't redrawn under the cursor
  const sig = JSON.stringify(fields.map(f => f.key));
  if(sig != boutSig){
    boutSig = sig;
    $("boutfields").innerHTML = fields.map(f => '<div><label for="b_'+f.key+'" title="'+f.key+'">'+esc(f.label)+(f.unit ? " ("+esc(f.unit)+")" : "")+
      '</label><input id="b_'+f.key+'" type="number" min="0" step="any" data-key="'+f.key+'"><div class="muted hint" id="bc_'+f.key+'"></div></div>').join("");
    // Sent once a field is left or Enter pressed, and also after a pause in typing, so the plan and the drawing follow it
    fields.forEach(f => { const el = $("b_"+f.key); let wait = null;
      el.onchange = e => { clearTimeout(wait); setBout(f.key, e.target.value); };
      el.oninput = e => { clearTimeout(wait); const v = e.target.value.trim();
        if(v !== "" && isFinite(Number(v))) wait = setTimeout(() => setBout(f.key, v), 700); }; });
  }
  fields.forEach(f => { const el = $("b_"+f.key); if(!el) return;
    if(document.activeElement != el) el.value = f.value;
    el.classList.toggle("changed", f.changed);
    $("bc_"+f.key).textContent = f.changed ? "the config's: "+f.config : ""; });
  const n = (B.changed || []).length;
  $("boutsum").textContent = n ? "· "+n+" changed"+(B.remembered ? ", as the last panel left them ("+B.remembered+")" : " on this page") : "· as the config says";
  $("boutreset").hidden = !n;
  boutPicture(B.error ? null : B.diagram);
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
    else if(P.alive) pt = "Running: "+(P.bout ? "bout "+P.bout+" of "+P.bouts : "between bouts ("+P.bouts+" in all)")+" · "+(P.pulses||0).toLocaleString()+" camera pulses · ends "+(P.ends_at||"?")+" ("+hms(Math.max(0,(P.ends_s||0)-(picoTime(P)||0)))+" left)"+(P.end_requested?" · stop asked ("+P.end_requested+")":"");
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
function gone(title, text){ // the page can't reach its panel: say so plainly, above everything, until it can again
  $("gone").hidden = !title; if(!title) return;
  $("gonetitle").textContent = title; $("gonetext").textContent = text;
}
async function tick(){
  if(closed) return;
  try { const r = await fetch(q("/status.json"));
    if(r.status==403){ $("state").textContent = "Old tab"; $("state").className = "tag bad";
      gone("This tab belongs to a panel that was closed", "Close this tab: the panel that is open now has its own page, in another tab. "+
        "To open it again, use the campy recording icon."); return; }
    gone(null); render(await r.json());
  } catch(e){ $("state").textContent = "Panel not running"; $("state").className = "tag bad";
    gone("The panel is closed", "Open it from the campy recording icon. This page reconnects by itself as soon as the panel is open again."); }
}
function images(){ for(let i=0;i<thumbs;i++){ const im = $("th"+i); if(im && !im.hidden) im.src = q("/thumb/"+i+".png?k="+Date.now()); } }
$("start").onclick = async () => { $("starterr").textContent = "";
  // A length that doesn't fit on the disk (and nothing else stopping the start): only on purpose, from a confirm naming
  // the shortfall
  const blocks = ((last||{}).checks||[]).filter(c => c.level=="block"), disk = blocks.find(c => c.action=="disk");
  const override = !!disk && blocks.length == 1;
  if(override && !confirm("This session doesn't fit on the disk.\n\n" + disk.text + "\n\nStart anyway? Only if data will be moved off "+
    "this disk during the session. Otherwise the disk fills up: campy stops cleanly near the end of the space, but SpikeGLX doesn't.")) return;
  const start = {experiment:$("experiment").value, animal_ids:$("ids").value, notes:$("notes").value, copy_to:$("copyto").value.trim(),
    copy_delete: !!$("copyto").value.trim() && $("copydelete").checked,
    length: $("length").value || null, pico_at: $("picoat").value.trim() || null, disk_override: override};
  // With panel.led_power: each LED's driver current and power at the fiber tip, entered or confirmed first
  if((last||{}).led_power) return askLeds(start);
  startWith(start); };
async function startWith(start){
  const j = await postFull("/start", start);
  $("starterr").textContent = j.error || ""; if(j.create) offerCreate(j.create); tick(); }
let ledStart = null;
// An LED's colour from the wavelength in its name ("Blue LED (465 nm)"): the visible spectrum as a screen shows it (465 nm
// about rgb(0, 146, 255), 560 nm rgb(195, 255, 0), as the bout drawing's LED rows); none without one
function nmColour(name){
  const m = /(\d{3})\s*nm/i.exec(String(name)); if(!m) return null; const w = Number(m[1]); if(w < 380 || w > 780) return null;
  const [r, g, b] = w < 440 ? [(440-w)/60, 0, 1] : w < 490 ? [0, (w-440)/50, 1] : w < 510 ? [0, 1, (510-w)/20] :
    w < 580 ? [(w-510)/70, 1, 0] : w < 645 ? [1, (645-w)/65, 0] : [1, 0, 0];
  return "rgb("+[r, g, b].map(v => Math.round(255 * Math.pow(v, 0.8))).join(", ")+")"; }
function swatch(name){ const c = nmColour(name); return c ? '<i class="sw" style="background:'+c+'"></i>' : ""; }
function ledText(l){ return swatch(l.led)+esc(l.led)+' <span class="nw">'+(+l.driver_mA)+"\u00a0mA, "+(+l.fiber_tip_uW)+"\u00a0\u00b5W</span>"; }
function confirmedWhen(iso){ const d = new Date(iso); if(isNaN(d)) return ""; const today = d.toDateString() == new Date().toDateString();
  return (today ? "today" : d.toLocaleDateString([], {weekday:"short", month:"short", day:"numeric"}))+" at "+d.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}); }
function askLeds(start){
  const P = last.led_power, had = {}; ((P.last||{}).leds||[]).forEach(l => had[l.led] = l);
  $("ledrows").innerHTML = '<span></span><label>Driver current (mA)</label><label>At the fiber tip (\u00b5W)</label>' + P.leds.map((n, i) =>
    '<b>'+swatch(n)+esc(n)+'</b><input id="lma'+i+'" type="number" min="0" step="any" inputmode="decimal" aria-label="'+esc(n)+' driver current in mA" value="'+
    esc(had[n] ? had[n].driver_mA : "")+'"><input id="luw'+i+'" type="number" min="0" step="any" inputmode="decimal" aria-label="'+esc(n)+
    ' power at the fiber tip in microwatts" value="'+esc(had[n] ? had[n].fiber_tip_uW : "")+'">').join("");
  const old = P.last && P.last.at && new Date(P.last.at).toDateString() != new Date().toDateString();
  $("ledwhen").innerHTML = P.last && P.last.at ? "The values shown were confirmed "+esc(confirmedWhen(P.last.at))+"."+
    (old ? ' <span style="color:var(--warn)">From an earlier day: measure them again.</span>' : "") : "No values confirmed yet with this experiment.";
  $("lederr").textContent = ""; ledStart = start; $("leddlg").hidden = false; const f = $("lma0"); if(f) f.focus(); }
$("ledno").onclick = () => { $("leddlg").hidden = true; ledStart = null; };
$("ledyes").onclick = () => {
  const P = last.led_power, leds = [];
  for(let i = 0; i < P.leds.length; i++){ const ma = $("lma"+i).value.trim(), uw = $("luw"+i).value.trim();
    if(ma === "" || uw === "" || !(Number(ma) >= 0) || !(Number(uw) >= 0)){
      $("lederr").textContent = "Enter both numbers for "+P.leds[i]+" (0 or more; 0 for an LED that is off)."; return; }
    leds.push({led: P.leds[i], driver_mA: Number(ma), fiber_tip_uW: Number(uw)}); }
  $("leddlg").hidden = true; const start = Object.assign({}, ledStart, {led_power: leds}); ledStart = null; startWith(start); };
$("copydelete").onchange = () => { $("copydeletenote").style.color = $("copydelete").checked ? "var(--bad)" : ""; };
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
// Text size: a share of the browser's own font size (which scales the page too), kept by this browser for the next visit
const TEXT_SIZES = [100, 115, 130, 150, 175, 200], TEXT_KEY = "campy-panel-text-size";
function textSize(pct){
  pct = TEXT_SIZES.includes(pct) ? pct : 100;
  document.documentElement.style.fontSize = pct + "%"; $("textpct").textContent = pct == 100 ? "" : pct + "%";
  $("smaller").disabled = pct == TEXT_SIZES[0]; $("larger").disabled = pct == TEXT_SIZES[TEXT_SIZES.length-1];
  try { localStorage.setItem(TEXT_KEY, String(pct)); } catch(e) {}
  return pct;
}
let textPct = 100; try { textPct = Number(localStorage.getItem(TEXT_KEY)) || 100; } catch(e) {}
textPct = textSize(textPct);
$("smaller").onclick = () => { textPct = textSize(TEXT_SIZES[Math.max(0, TEXT_SIZES.indexOf(textPct)-1)]); };
$("larger").onclick = () => { textPct = textSize(TEXT_SIZES[Math.min(TEXT_SIZES.length-1, TEXT_SIZES.indexOf(textPct)+1)]); };
tick(); setInterval(tick, 1000); setInterval(images, 1000); setInterval(paint, 200);
</script></body></html>
"""

# The LUC3D logo in the header, and as the tab's icon (campy/panellogo.py: inline, nothing fetched)
PAGE = PAGE.replace("@LOGO@", panellogo.LOGO).replace("@ICON@", panellogo.ICON)

# Served at / for an address with a key the panel doesn't know: a tab of an earlier panel, which the browser restored
OLD_KEY = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>campy session: an old tab</title><style>body{margin:0;font:1.0625rem/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
background:#f7f6f2;color:#1f1e1c}@media (prefers-color-scheme:dark){body{background:#1b1b1a;color:#ecebe6}}
main{max-width:640px;margin:15vh auto;padding:0 16px}h1{font-size:1.5rem;font-weight:600}</style></head><body><main>
<h1>This tab belongs to a panel that was closed</h1>
<p>Close this tab. The panel that is open now has its own page, in another tab. To open it again, use the
<b>campy recording</b> icon.</p></main></body></html>"""
