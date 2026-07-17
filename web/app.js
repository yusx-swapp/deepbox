// deepbox minimal SPA
const app = document.getElementById('app');
let me = null, devboxes = [], term = null, fit = null, termWS = null, curSession = null;

async function api(path, opts={}) {
  const r = await fetch(path, {credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
  return r.status === 204 ? null : r.json();
}

// ---------------- auth ----------------
function renderLogin() {
  app.innerHTML = `<div class="center stack card">
    <h2>deepbox</h2>
    <div class="muted">Connect your devbox agents. Chat like you're local.</div>
    <input id="u" placeholder="username"/>
    <input id="p" type="password" placeholder="password"/>
    <div class="row"><button id="login">Login</button><button class="ghost" id="reg">Register</button></div>
    <div id="err" class="muted"></div></div>`;
  const go = async (path) => {
    try {
      me = await api(path, {method:'POST', body: JSON.stringify({
        username:u.value, password:p.value})});
      boot();
    } catch(e){ err.textContent = e.message; }
  };
  login.onclick = ()=>go('/api/auth/login');
  reg.onclick = ()=>go('/api/auth/register');
}

// ---------------- main shell ----------------
async function boot() {
  try { me = me || await api('/api/me/user'); }
  catch { return renderLogin(); }
  await loadDevboxes();
  renderShell();
}

async function loadDevboxes(){ devboxes = await api('/api/devboxes'); }

function renderShell() {
  app.innerHTML = `
  <header>
    <b>deepbox</b>
    <span class="muted">${me.display_name}</span>
    <span style="flex:1"></span>
    <button class="ghost" id="newbox">+ Devbox</button>
    <button class="ghost" id="logout">Logout</button>
  </header>
  <main>
    <div class="side" id="side"></div>
    <div class="content">
      <div id="termhead" class="row" style="padding:8px 12px;border-bottom:1px solid var(--border)">
        <span class="muted">Select an agent to open a terminal</span></div>
      <div id="term"></div>
    </div>
  </main>`;
  logout.onclick = async()=>{ await api('/api/auth/logout',{method:'POST'}); me=null; renderLogin(); };
  newbox.onclick = createDevbox;
  renderSide();
  setupTerm();
}

function renderSide() {
  const side = document.getElementById('side');
  side.innerHTML = devboxes.map(d => `
    <div class="card">
      <div class="row">
        <span class="dot ${d.online?'on':'off'}"></span>
        <b>${d.name}</b>
        <span style="flex:1"></span>
        <button class="ghost" data-agent="${d.id}">+agent</button>
      </div>
      <div class="muted">caps: ${(d.capabilities||[]).join(', ')||'—'}</div>
      <div style="margin-top:6px">
        ${d.agents.map(a=>`<div class="agent" data-open="${a.id}" data-name="${a.display_name}">
          <span class="dot ${a.presence==='online'?'on':'off'}"></span>
          @${a.handle} <span class="muted">(${a.runtime})</span></div>`).join('') || '<div class="muted">no agents</div>'}
      </div>
      <div class="row" style="margin-top:6px">
        <button class="ghost" data-token="${d.id}">rotate token</button>
        <button class="ghost" data-del="${d.id}">delete</button>
      </div>
    </div>`).join('') || '<div class="muted">No devboxes yet. Create one →</div>';

  side.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>createAgent(b.dataset.agent));
  side.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openAgent(b.dataset.open, b.dataset.name));
  side.querySelectorAll('[data-token]').forEach(b=>b.onclick=()=>rotateToken(b.dataset.token));
  side.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delDevbox(b.dataset.del));
}

async function createDevbox() {
  const name = prompt('Devbox name?', 'My Devbox'); if(!name) return;
  const res = await api('/api/devboxes',{method:'POST',body:JSON.stringify({name})});
  await loadDevboxes(); renderSide();
  showToken(res.token);
}
function showToken(tok){
  alert('Copy this token now — it is shown only once:\n\n'+tok+
    '\n\nRun the connector with:\nset DEEPBOX_TOKEN='+tok+'\npython -m connector');
}
async function rotateToken(id){
  const res = await api(`/api/devboxes/${id}/tokens`,{method:'POST'});
  showToken(res.token);
}
async function delDevbox(id){
  if(!confirm('Delete this devbox and its agents?')) return;
  await api(`/api/devboxes/${id}`,{method:'DELETE'});
  await loadDevboxes(); renderSide();
}
async function createAgent(devboxId){
  const handle = prompt('Agent handle? (e.g. claude)'); if(!handle) return;
  const runtime = prompt('Runtime? mock | claude-code | copilot-cli | codex-cli','mock')||'mock';
  const cwd = prompt('Working dir? (optional, blank = default)','')||null;
  await api(`/api/devboxes/${devboxId}/agents`,{method:'POST',
    body:JSON.stringify({handle,display_name:handle,runtime,cwd})});
  await loadDevboxes(); renderSide();
}

// ---------------- terminal ----------------
let reconnectDelay = 500, reconnectTimer = null, wantOpen = false;

function setupTerm(){
  term = new Terminal({fontFamily:'Consolas,monospace',fontSize:13,cursorBlink:true,
    scrollback:5000, theme:{background:'#000000'}});
  fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(document.getElementById('term'));
  fit.fit();
  window.onresize = ()=>{ try{fit.fit(); sendResize();}catch(e){} };
  term.onData(d => { if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'input',session_id:curSession,data:d})); });
}
function sendResize(){
  if(termWS && termWS.readyState===1 && curSession) termWS.send(JSON.stringify(
    {type:'resize',session_id:curSession,cols:term.cols,rows:term.rows}));
}

async function openAgent(agentId, name){
  // leaving previous session? detach it (do NOT kill its PTY)
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'detach',session_id:curSession}));
  document.getElementById('termhead').innerHTML =
    `<b>${name}</b> <span class="muted">— live terminal</span>
     <span style="flex:1"></span>
     <span id="stat" class="muted"></span>`;
  term.reset();
  // Resume the newest PTY that is still alive on the devbox. Previously every
  // click silently created a new session, making persisted history invisible.
  const sessions = await api(`/api/agents/${agentId}/sessions`);
  let sess = sessions.find(s => s.state === 'live');
  const resumed = !!sess;
  if(!sess) sess = await api(`/api/agents/${agentId}/sessions`,{method:'POST'});
  curSession = sess.id;
  if(resumed) document.getElementById('termhead').querySelector('.muted').textContent =
    '— resumed live session';
  wantOpen = true;
  connectTermWS();
}

function setStat(txt, color){
  const el = document.getElementById('stat');
  if(el){ el.textContent = txt; el.style.color = color||'#8b949e'; }
}

function connectTermWS(){
  if(termWS){ try{ wantOpen && (termWS.onclose=null); termWS.close(); }catch(e){} }
  const proto = location.protocol==='https:'?'wss':'ws';
  termWS = new WebSocket(`${proto}://${location.host}/ws/term`);
  termWS.onopen = ()=>{
    reconnectDelay = 500;
    setStat('● live', '#3fb950');
    termWS.send(JSON.stringify({type:'attach',session_id:curSession,
      cols:term.cols,rows:term.rows}));
  };
  termWS.onmessage = (ev)=>{
    const f = JSON.parse(ev.data);
    if(f.session_id && f.session_id!==curSession) return;
    switch(f.type){
      case 'restore':          // reconnect: instantly repaint current screen
        term.reset(); term.write(f.data); break;
      case 'output':
        term.write(f.data); break;
      case 'status':
        if(f.state==='live') setStat('● live','#3fb950');
        else if(f.state==='offline'){ setStat('● devbox offline','#d29922');
          term.write('\r\n[devbox offline — the connector isn\'t running]\r\n'); }
        else if(f.state==='ended'){ setStat('● ended','#8b949e');
          term.write(`\r\n[session ended, code ${f.code}]\r\n`); }
        break;
      case 'exit':
        setStat('● ended','#8b949e');
        if(f.data) term.write(f.data);
        term.write(`\r\n[session ended, code ${f.code}]\r\n`); break;
      case 'error':
        term.write(`\r\n[error] ${f.message}\r\n`); break;
    }
  };
  termWS.onclose = ()=>{
    if(!wantOpen) return;
    setStat('● reconnecting…','#d29922');
    reconnectTimer = setTimeout(connectTermWS, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay*2, 5000);  // exponential backoff
  };
}

// ---------------- start ----------------
boot();
