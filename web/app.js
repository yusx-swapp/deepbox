// deepbox minimal SPA
const app = document.getElementById('app');
// Inject the collaboration stylesheet without touching index.html.
if(!document.querySelector('link[data-deepbox-styles]')){
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/static/styles.css';
  link.setAttribute('data-deepbox-styles','');
  document.head.appendChild(link);
}
let me = null, devboxes = [], term = null, fit = null, termWS = null, curSession = null;
let replayMode = false;
let nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock;
let deriveCollaborationState, canSendInput;
let collabState = null;  // normalized collaboration view model for curSession
let keyboardRequester = null;

async function loadReplayHelpers(){
  if(!window.DeepboxReplay){
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/replay.js';
      script.onload = resolve;
      script.onerror = () => reject(new Error('failed to load replay helpers'));
      document.head.appendChild(script);
    });
  }
  ({nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock} = window.DeepboxReplay);
}

async function loadCollaborationHelpers(){
  if(!window.DeepboxCollaboration){
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/collaboration.js';
      script.onload = resolve;
      script.onerror = () => reject(new Error('failed to load collaboration helpers'));
      document.head.appendChild(script);
    });
  }
  ({deriveCollaborationState, canSendInput} = window.DeepboxCollaboration);
}
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ''));
const queryParams = new URLSearchParams(location.search);
let pendingInvite = hashParams.get('invite') || queryParams.get('invite') || '';
if (pendingInvite) history.replaceState(null, '', location.pathname);

async function api(path, opts={}) {
  const r = await fetch(path, {credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
  return r.status === 204 ? null : r.json();
}

// ---------------- auth ----------------
async function renderLogin() {
  // First-owner bootstrap form when available.
  let status = {available:false};
  try { status = await api('/api/auth/bootstrap-status'); } catch {}
  if (status.available) return renderBootstrap();

  const inviteFromUrl = pendingInvite;
  app.innerHTML = `<div class="center stack card">
    <h2>deepbox</h2>
    <div class="muted">Connect your devbox agents. Chat like you're local.</div>
    <input id="u" placeholder="username"/>
    <input id="p" type="password" placeholder="password"/>
    <div class="row"><button id="login">Login</button></div>
    <h4>Have an invite code?</h4>
    <input id="inv" placeholder="invite code" value="${escapeHtml(inviteFromUrl)}"/>
    <input id="rd" placeholder="display name (optional)"/>
    <div class="row"><button class="ghost" id="reg">Register with invite</button></div>
    <div id="err" class="muted"></div></div>`;
  login.onclick = async () => {
    try {
      me = await api('/api/auth/login', {method:'POST', body: JSON.stringify({
        username:u.value, password:p.value})});
      boot();
    } catch(e){ err.textContent = e.message; }
  };
  reg.onclick = async () => {
    try {
      me = await api('/api/auth/register', {method:'POST', body: JSON.stringify({
        username:u.value, password:p.value, display_name:rd.value||undefined,
        invite_code:inv.value||undefined})});
      pendingInvite = '';
      boot();
    } catch(e){ err.textContent = e.message; }
  };
}

function renderBootstrap() {
  app.innerHTML = `<div class="center stack card">
    <h2>deepbox — first owner setup</h2>
    <div class="muted">Create the first owner account with the bootstrap token.</div>
    <input id="bt" type="password" placeholder="bootstrap token"/>
    <input id="bu" placeholder="username"/>
    <input id="bp" type="password" placeholder="password"/>
    <input id="bd" placeholder="display name (optional)"/>
    <div class="row"><button id="bgo">Create owner</button></div>
    <div id="err" class="muted"></div></div>`;
  bgo.onclick = async () => {
    try {
      me = await api('/api/auth/bootstrap', {method:'POST', body: JSON.stringify({
        token:bt.value, username:bu.value, password:bp.value,
        display_name:bd.value||undefined})});
      boot();
    } catch(e){ err.textContent = 'Setup failed.'; }
  };
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
    ${me.role==='owner'?'<button class="ghost" id="owner">Owner</button>':''}
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
  if(me.role==='owner') document.getElementById('owner').onclick = renderOwner;
  renderSide();
  setupTerm();
}

// ---------------- owner admin ----------------
async function renderOwner(){
  let invites=[], users=[];
  try { [invites, users] = await Promise.all([
    api('/api/invitations'), api('/api/users')]); } catch(e){}
  app.innerHTML = `
  <header>
    <b>deepbox — owner</b>
    <span style="flex:1"></span>
    <button class="ghost" id="back">← Back</button>
  </header>
  <main style="display:block;padding:16px;overflow:auto">
    <div class="card">
      <h4>Invitations</h4>
      <div class="row">
        <input id="inote" placeholder="note (optional)"/>
        <input id="ittl" type="number" value="24" title="TTL hours" style="width:120px"/>
        <button id="mint">Mint invite</button>
      </div>
      <div id="mintout"></div>
      <div id="invlist" style="margin-top:8px"></div>
    </div>
    <div class="card">
      <h4>Members</h4>
      <div id="userlist"></div>
    </div>
  </main>`;
  document.getElementById('back').onclick = renderShell;

  const renderInv = () => {
    document.getElementById('invlist').innerHTML = invites.map(i=>`
      <div class="row" style="border-top:1px solid var(--border);padding:4px 0">
        <span>${i.note?escapeHtml(i.note):'<span class="muted">(no note)</span>'}</span>
        <span class="muted">${i.status}, expires ${i.expires_at}</span>
        <span style="flex:1"></span>
        ${i.status==='active'?`<button class="ghost" data-revoke="${i.id}">revoke</button>`:''}
      </div>`).join('') || '<div class="muted">No invitations.</div>';
    document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
      await api(`/api/invitations/${b.dataset.revoke}`,{method:'DELETE'});
      invites = await api('/api/invitations'); renderInv();
    });
  };
  const renderUsers = () => {
    document.getElementById('userlist').innerHTML = users.map(u=>`
      <div class="row" style="border-top:1px solid var(--border);padding:4px 0">
        <b>${escapeHtml(u.display_name)}</b>
        <span class="muted">@${escapeHtml(u.username)} · ${u.role}${u.disabled?' · disabled':''}</span>
        <span style="flex:1"></span>
        ${u.role==='member'?(u.disabled
          ?`<button class="ghost" data-enable="${u.id}">enable</button>`
          :`<button class="ghost" data-disable="${u.id}">disable</button>`):''}
      </div>`).join('') || '<div class="muted">No users.</div>';
    document.querySelectorAll('[data-disable]').forEach(b=>b.onclick=async()=>{
      try{ await api(`/api/users/${b.dataset.disable}/disable`,{method:'POST'}); }
      catch(e){ alert(e.message); }
      users = await api('/api/users'); renderUsers();
    });
    document.querySelectorAll('[data-enable]').forEach(b=>b.onclick=async()=>{
      await api(`/api/users/${b.dataset.enable}/enable`,{method:'POST'});
      users = await api('/api/users'); renderUsers();
    });
  };
  renderInv(); renderUsers();

  document.getElementById('mint').onclick = async()=>{
    const res = await api('/api/invitations',{method:'POST',body:JSON.stringify({
      note:inote.value||undefined, ttl_hours:Number(ittl.value)||24})});
    // Show plaintext + prefilled invite URL exactly once; not retained.
    // URL fragments never reach the HTTP server or its access logs.
    const url = `${location.origin}${location.pathname}#invite=${encodeURIComponent(res.token)}`;
    document.getElementById('mintout').innerHTML =
      `<div class="token">Invite code (shown once): ${escapeHtml(res.token)}<br><br>`+
      `Invite URL: <a href="${url}">${escapeHtml(url)}</a></div>`;
    invites = await api('/api/invitations'); renderInv();
  };
}

function escapeHtml(s){ return String(s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

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
          @${a.handle} <span class="muted">(${a.runtime})</span>
          <button class="ghost" data-hist="${a.id}" data-histname="${escapeHtml(a.display_name)}" style="float:right;padding:2px 6px;font-size:11px">history</button></div>`).join('') || '<div class="muted">no agents</div>'}
      </div>
      <div class="row" style="margin-top:6px">
        <button class="ghost" data-token="${d.id}">rotate token</button>
        <button class="ghost" data-del="${d.id}">delete</button>
      </div>
    </div>`).join('') || '<div class="muted">No devboxes yet. Create one →</div>';

  side.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>createAgent(b.dataset.agent));
  side.querySelectorAll('[data-open]').forEach(b=>b.onclick=(e)=>{
    if(e.target.closest('[data-hist]')) return;
    openAgent(b.dataset.open, b.dataset.name);
  });
  side.querySelectorAll('[data-hist]').forEach(b=>b.onclick=(e)=>{
    e.stopPropagation();
    openHistory(b.dataset.hist, b.dataset.histname);
  });
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
  term.onData(d => { if(!replayMode && termWS && termWS.readyState===1 && curSession
    && canSendInput && canSendInput(collabState))
    termWS.send(JSON.stringify({type:'input',session_id:curSession,data:d})); });
}

function resetTerminal(){
  if(term){ try{ term.dispose(); }catch(e){} }
  term = null; fit = null;
  const host = document.getElementById('term');
  host.innerHTML = '';
  host.style.cssText = 'flex:1;padding:6px;background:#000';
  setupTerm();
}
function sendResize(){
  if(termWS && termWS.readyState===1 && curSession) termWS.send(JSON.stringify(
    {type:'resize',session_id:curSession,cols:term.cols,rows:term.rows}));
}

async function openAgent(agentId, name){
  await loadCollaborationHelpers();
  // Switching sessions: reset collaboration state so a stale lease/holder from
  // the previous terminal never leaks input rights into the new one.
  collabState = null;
  keyboardRequester = null;
  const wasReplayOrHistory = replayMode || !!document.getElementById('replaybar') ||
    !!document.querySelector('#term [data-replay]');
  stopReplay();
  const replaybar = document.getElementById('replaybar');
  if(replaybar) replaybar.remove();
  if(wasReplayOrHistory) resetTerminal();
  // leaving previous session? detach it (do NOT kill its PTY)
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'detach',session_id:curSession}));
  document.getElementById('termhead').innerHTML =
    `<b>${name}</b> <span class="muted">— live terminal</span>
     <span style="flex:1"></span>
     <span id="collab" class="collab"></span>
     <span id="stat" class="muted"></span>`;
  renderCollab();
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
      case 'collaboration':
        collabState = deriveCollaborationState(f, me ? {id: me.id, username: me.username} : null);
        if(!collabState.isHolder) keyboardRequester = null;
        renderCollab();
        break;
      case 'keyboard_request':
        if(collabState && collabState.isHolder){
          keyboardRequester = {id:f.requester_user_id, username:f.requester_username};
          renderCollab();
        }
        break;
    }
  };
  termWS.onclose = ()=>{
    if(!wantOpen) return;
    setStat('● reconnecting…','#d29922');
    reconnectTimer = setTimeout(connectTermWS, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay*2, 5000);  // exponential backoff
  };
}

// ---------------- collaboration (keyboard lease) ----------------
function requestKeyboard(){
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'keyboard_acquire',session_id:curSession}));
}
function releaseKeyboard(){
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'keyboard_release',session_id:curSession}));
}
function handoffKeyboard(){
  if(termWS && termWS.readyState===1 && curSession && keyboardRequester)
    termWS.send(JSON.stringify({type:'keyboard_handoff',session_id:curSession,
      target_user_id:keyboardRequester.id}));
  keyboardRequester = null;
}
setInterval(()=>{
  if(collabState && collabState.isHolder && termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'keyboard_renew',session_id:curSession}));
}, 20000);
// Render the compact keyboard status + action into #collab (lives in termhead).
function renderCollab(){
  const el = document.getElementById('collab');
  if(!el) return;
  const s = collabState;
  if(!s){ el.innerHTML = ''; return; }
  let label, cls, btn = '';
  if(s.isViewer){
    label = 'read-only'; cls = 'collab-viewer';
  } else if(s.isHolder){
    label = keyboardRequester
      ? `${escapeHtml(keyboardRequester.username)} requests the keyboard`
      : 'you have the keyboard';
    cls = 'collab-holder';
    btn = keyboardRequester
      ? '<button class="ghost" id="collab-handoff">Hand off</button>'
      : '<button class="ghost" id="collab-release">Release</button>';
  } else if(s.heldByOther){
    label = `${escapeHtml(s.holderUsername || 'someone')} is typing`; cls = 'collab-busy';
    if(s.canRequest) btn = '<button class="ghost" id="collab-request">Request</button>';
  } else {
    label = 'keyboard free'; cls = 'collab-free';
    if(s.canRequest) btn = '<button class="ghost" id="collab-request">Take keyboard</button>';
  }
  el.className = 'collab ' + cls;
  el.innerHTML = `<span class="collab-dot"></span><span class="collab-label">${label}</span>${btn}`;
  const rq = document.getElementById('collab-request'); if(rq) rq.onclick = requestKeyboard;
  const rl = document.getElementById('collab-release'); if(rl) rl.onclick = releaseKeyboard;
  const ho = document.getElementById('collab-handoff'); if(ho) ho.onclick = handoffKeyboard;
}

// Pure helpers (testable, no DOM/side-effects).

// Format seconds as m:ss.
function fmtTime(sec){ return formatClock(sec); }

// Total duration = time of last event/checkpoint (fallback 0).
function replayDuration(replay){
  let t = 0;
  for(const e of (replay.events||[])) if(typeof e.time==='number' && e.time>t) t=e.time;
  for(const c of (replay.checkpoints||[])) if(typeof c.time==='number' && c.time>t) t=c.time;
  return t;
}

// State for the active replay.
let replay = null, replayTimer = null, replayPlaying = false, replaySpeed = 1,
    replayCursor = 0, replayAgentId = null, replayAgentName = '';

function stopReplay(){
  replayMode = false;
  replayPlaying = false;
  if(replayTimer){ clearTimeout(replayTimer); replayTimer = null; }
  if(term) term.options && (term.options.disableStdin = false);
}

async function openHistory(agentId, name){
  // Cleanly leave any live session / reconnect loop.
  stopReplay();
  const replaybar = document.getElementById('replaybar');
  if(replaybar) replaybar.remove();
  wantOpen = false;
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  if(termWS){ try{ termWS.onclose=null; termWS.close(); }catch(e){} termWS=null; }
  curSession = null;

  replayAgentId = agentId; replayAgentName = name;
  let sessions = [];
  try { sessions = await api(`/api/agents/${encodeURIComponent(agentId)}/sessions`); }
  catch(e){ sessions = []; }

  document.getElementById('termhead').innerHTML =
    `<b>${escapeHtml(name)}</b> <span class="muted">\u2014 session history</span>
     <span style="flex:1"></span>
     <button class="ghost" id="hist-live">\u2190 back to live</button>`;
  document.getElementById('hist-live').onclick = ()=>openAgent(agentId, name);

  if(term){ try{ term.dispose(); }catch(e){} term = null; fit = null; }
  const termEl = document.getElementById('term');
  termEl.innerHTML = '';

  const list = sessions.map(s=>`
    <div class="card" style="margin:8px">
      <div class="row">
        <span class="dot ${s.state==='live'?'on':'off'}"></span>
        <b>${escapeHtml(s.id)}</b>
        <span class="muted">${escapeHtml(s.state||'')} \u00b7 started ${escapeHtml(s.created_at||'')}</span>
        <span style="flex:1"></span>
        <button class="ghost" data-replay="${escapeHtml(s.id)}">replay</button>
      </div>
    </div>`).join('') || '<div class="muted" style="padding:12px">No sessions recorded.</div>';
  termEl.innerHTML = `<div style="overflow:auto;height:100%;color:var(--fg)">
    <div class="muted" style="padding:8px">Session history for @${escapeHtml(name)}</div>${list}</div>`;
  termEl.querySelectorAll('[data-replay]').forEach(b=>
    b.onclick=()=>startReplay(b.dataset.replay));
}

async function startReplay(sessionId){
  let data;
  try { data = await api(`/api/sessions/${sessionId}/replay`); }
  catch(e){ alert('Replay unavailable: '+e.message); return; }
  replay = normalizeReplay(data);
  replayMode = true;
  replayPlaying = false;
  replaySpeed = 1;
  replayCursor = 0;
  const total = replayDuration(replay);

  // Rebuild xterm after the history list replaced its host contents.
  const termEl = document.getElementById('term');
  termEl.innerHTML = '';
  const bar = document.createElement('div');
  bar.id = 'replaybar';
  bar.style.cssText = 'padding:8px 12px;border-top:1px solid var(--border);background:var(--panel)';
  const meta = replay.metadata || {};
  const ret = replay.retention || (replay.metadata&&replay.metadata.retention) || 'permanent';
  bar.innerHTML = `
    <div class="row" style="gap:8px;flex-wrap:wrap">
      <button class="ghost" id="rp-playpause">\u25b6 play</button>
      <button class="ghost" id="rp-start">\u23ee start</button>
      <button class="ghost" id="rp-end">\u23ed end</button>
      <select id="rp-speed" style="width:auto">
        <option value="0.5">0.5x</option>
        <option value="1" selected>1x</option>
        <option value="2">2x</option>
        <option value="8">8x</option>
      </select>
      <input type="range" id="rp-seek" min="0" max="${total}" step="0.01" value="0" style="flex:1;min-width:160px"/>
      <span class="muted" id="rp-time">0:00 / ${fmtTime(total)}</span>
    </div>
    <div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">
      <button class="ghost" id="rp-final">final screen</button>
      <a class="ghost" id="rp-download" href="/api/sessions/${encodeURIComponent(sessionId)}/recording" download style="text-decoration:none;padding:6px 12px;border:1px solid var(--border);border-radius:6px;color:var(--fg)">download asciicast</a>
      <span style="flex:1"></span>
      <span class="muted">retention:</span>
      <select id="rp-retention" style="width:auto">
        <option value="none">none</option>
        <option value="7d">7 days</option>
        <option value="30d">30 days</option>
        <option value="permanent">permanent</option>
      </select>
      <span class="muted" id="rp-retmsg"></span>
    </div>`;
  termEl.parentNode.insertBefore(bar, termEl.nextSibling);
  resetTerminal();
  if(term.options) term.options.disableStdin = true;

  const selRet = document.getElementById('rp-retention');
  selRet.value = ret;
  selRet.onchange = async ()=>{
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}/retention`,
        {method:'PATCH', body: JSON.stringify({retention: selRet.value})});
      document.getElementById('rp-retmsg').textContent = 'saved';
    } catch(e){ document.getElementById('rp-retmsg').textContent = 'error'; }
  };

  document.getElementById('rp-speed').onchange = (e)=>{ replaySpeed = Number(e.target.value)||1; };
  document.getElementById('rp-playpause').onclick = toggleReplayPlay;
  document.getElementById('rp-start').onclick = ()=>replaySeek(0);
  document.getElementById('rp-end').onclick = ()=>replaySeek(total);
  document.getElementById('rp-final').onclick = ()=>showFinalScreen();
  document.getElementById('rp-seek').oninput = (e)=>{ pauseReplay(); replaySeek(Number(e.target.value)); };

  replaySeek(0);
}

// Reset terminal, load nearest checkpoint <= target, apply subsequent events.
function replaySeek(target){
  if(!replay) return;
  const total = replayDuration(replay);
  target = Math.max(0, Math.min(target, total));
  replayCursor = target;
  term.reset();
  const ci = nearestCheckpointIndex(replay.checkpoints, target);
  let startTime = -Infinity, startCursor = null;
  if(ci>=0){
    const cp = replay.checkpoints[ci];
    if(cp.serialized_screen) term.write(cp.serialized_screen);
    startTime = cp.time;
    startCursor = cp.cursor;
  }
  for(const e of eventsBetween(replay.events, startTime, target, startCursor)){
    if((e.type==='o' || e.type==='output') && e.data!=null) term.write(e.data);
  }
  updateReplayUI();
}

function updateReplayUI(){
  const total = replayDuration(replay);
  const seek = document.getElementById('rp-seek');
  const timeEl = document.getElementById('rp-time');
  if(seek) seek.value = replayCursor;
  if(timeEl) timeEl.textContent = fmtTime(replayCursor)+' / '+fmtTime(total);
  const pp = document.getElementById('rp-playpause');
  if(pp) pp.textContent = replayPlaying ? '\u2758\u2758 pause' : '\u25b6 play';
}

function toggleReplayPlay(){ replayPlaying ? pauseReplay() : playReplay(); }

function pauseReplay(){
  replayPlaying = false;
  if(replayTimer){ clearTimeout(replayTimer); replayTimer = null; }
  updateReplayUI();
}

function playReplay(){
  if(!replay) return;
  const total = replayDuration(replay);
  if(replayCursor >= total) replaySeek(0);
  replayPlaying = true;
  updateReplayUI();
  scheduleReplayStep();
}

// Advance to the next event after replayCursor, honoring speed.
function scheduleReplayStep(){
  if(!replayPlaying) return;
  const next = (replay.events||[]).find(e=> typeof e.time==='number' && e.time>replayCursor+1e-9);
  if(!next){ pauseReplay(); return; }
  const delayMs = Math.max(0, (next.time - replayCursor) * 1000 / replaySpeed);
  replayTimer = setTimeout(()=>{
    if(!replayPlaying) return;
    const batch = (replay.events||[]).filter(e =>
      typeof e.time==='number' && e.time>replayCursor+1e-9 && e.time<=next.time);
    replayCursor = next.time;
    for(const event of batch)
      if((event.type==='o' || event.type==='output') && event.data!=null) term.write(event.data);
    updateReplayUI();
    scheduleReplayStep();
  }, delayMs);
}

// Static preview of the final screen (last checkpoint) and seek to end.
function showFinalScreen(){
  if(!replay) return;
  pauseReplay();
  const total = replayDuration(replay);
  const cps = replay.checkpoints || [];
  const last = cps.length ? cps[cps.length-1] : null;
  if(last && last.serialized_screen && last.time >= (replay.events||[]).reduce((m,e)=>Math.max(m,e.time||0),0)){
    term.reset();
    term.write(last.serialized_screen);
    replayCursor = total;
    updateReplayUI();
  } else {
    replaySeek(total);
  }
}

// ---------------- start ----------------
loadReplayHelpers().then(boot).catch(error => {
  app.innerHTML = `<div class="empty"><h2>Unable to start</h2><p>${esc(error.message)}</p></div>`;
});
