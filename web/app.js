// deepbox SPA — Terminal-first Switchboard.
// Renders login, first-owner bootstrap, the main shell (topbar + fleet panel +
// terminal stage), owner console, command palette and modals. API behaviour is
// unchanged; this file only reshapes the DOM/UX around the existing endpoints.
const app = document.getElementById('app');
// Inject the external stylesheet without touching index.html. It loads after
// the inline reset there, so its rules become the source of truth.
if(!document.querySelector('link[data-deepbox-styles]')){
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/static/styles.css';
  link.setAttribute('data-deepbox-styles','');
  document.head.appendChild(link);
}
let me = null, devboxes = [], term = null, fit = null, termWS = null, curSession = null;
let replayMode = false;
let curAgentId = null;   // currently open agent, for active-row highlighting
let fleetQuery = '';     // fleet search text
let nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock;
let deriveCollaborationState, canSendInput;
let collabState = null;  // normalized collaboration view model for curSession
let keyboardRequester = null;
let termInputSender = null;
let ui = null;           // DeepboxUI pure helpers (dynamically loaded)

// Cached dynamic-module loaders (same pattern as replay/collaboration).
let replayHelpersPromise = null, collaborationHelpersPromise = null, uiPromise = null;

function loadScriptOnce(src, globalName, errMsg){
  return new Promise((resolve, reject) => {
    if(window[globalName]) return resolve(window[globalName]);
    const script = document.createElement('script');
    script.src = src;
    script.onload = () => resolve(window[globalName]);
    script.onerror = () => reject(new Error(errMsg));
    document.head.appendChild(script);
  });
}

async function loadReplayHelpers(){
  replayHelpersPromise = replayHelpersPromise ||
    loadScriptOnce('/static/replay.js', 'DeepboxReplay', 'failed to load replay helpers');
  const mod = await replayHelpersPromise;
  ({nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock} = mod);
  return mod;
}

async function loadCollaborationHelpers(){
  collaborationHelpersPromise = collaborationHelpersPromise ||
    loadScriptOnce('/static/collaboration.js', 'DeepboxCollaboration', 'failed to load collaboration helpers');
  const mod = await collaborationHelpersPromise;
  ({deriveCollaborationState, canSendInput} = mod);
  return mod;
}

async function loadUI(){
  uiPromise = uiPromise ||
    loadScriptOnce('/static/ui.js', 'DeepboxUI', 'failed to load ui helpers');
  ui = await uiPromise;
  return ui;
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

// esc(): always available HTML escaper. Delegates to ui.js once loaded, but has
// a self-contained fallback so early render paths never throw.
function esc(s){
  if(ui) return ui.escapeHtml(s);
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
const escapeHtml = esc;

// ---------------- auth ----------------
async function renderLogin() {
  closeOverlay();
  // First-owner bootstrap form when available.
  let status = {available:false};
  try { status = await api('/api/auth/bootstrap-status'); } catch {}
  if (status.available) return renderBootstrap();

  const inviteFromUrl = pendingInvite;
  app.innerHTML = `<div class="auth"><div class="auth-card stack">
    <div class="auth-brand"><span class="glyph">_</span><b>deepbox</b></div>
    <div class="auth-sub">Your devbox agent switchboard. Sign in to reach the fleet.</div>
    <input id="u" placeholder="username" autocomplete="username"/>
    <input id="p" type="password" placeholder="password" autocomplete="current-password"/>
    <div class="row"><button id="login" style="flex:1">Sign in</button></div>
    <div class="auth-divider">or use an invite</div>
    <input id="inv" placeholder="invite code" value="${escapeHtml(inviteFromUrl)}"/>
    <input id="rd" placeholder="display name (optional)"/>
    <div class="row"><button class="ghost" id="reg" style="flex:1">Register with invite</button></div>
    <div id="err" class="auth-err"></div></div></div>`;
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
  p.addEventListener('keydown', e=>{ if(e.key==='Enter') login.click(); });
}

function renderBootstrap() {
  app.innerHTML = `<div class="auth"><div class="auth-card stack">
    <div class="auth-brand"><span class="glyph">_</span><b>deepbox</b></div>
    <div class="auth-sub">First-owner setup. Create the owner account with the bootstrap token.</div>
    <input id="bt" type="password" placeholder="bootstrap token"/>
    <input id="bu" placeholder="username"/>
    <input id="bp" type="password" placeholder="password"/>
    <input id="bd" placeholder="display name (optional)"/>
    <div class="row"><button id="bgo" style="flex:1">Create owner</button></div>
    <div id="err" class="auth-err"></div></div></div>`;
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
  closeOverlay();
  curAgentId = null;
  app.innerHTML = `
  <header class="topbar">
    <div class="brand"><span class="glyph">_</span><b>deepbox</b><span class="tag">switchboard</span></div>
    <span class="spacer"></span>
    <button class="cmd-hint" id="cmdk" title="Command palette">
      <span class="label">Search</span><span class="kbd">${cmdKeyLabel()} K</span></button>
    ${me.role==='owner'?'<button class="ghost" id="owner">Owner</button>':''}
    <div class="who"><span class="avatar">${esc(ui.initials(me.display_name||me.username))}</span>
      <span>${esc(me.display_name)}</span></div>
    <button class="ghost" id="logout">Sign out</button>
  </header>
  <main class="shell">
    <aside class="fleet" id="fleet"></aside>
    <section class="stage">
      <div class="stage-head" id="termhead"></div>
      <div class="stage-body" id="stagebody"></div>
    </section>
  </main>`;
  logout.onclick = async()=>{ await api('/api/auth/logout',{method:'POST'}); me=null; renderLogin(); };
  document.getElementById('cmdk').onclick = openCommandPalette;
  if(me.role==='owner') document.getElementById('owner').onclick = renderOwner;
  renderFleet();
  renderStageEmpty();
}

// Cmd on macOS, Ctrl elsewhere — for the palette hint label only.
function cmdKeyLabel(){
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '\u2318' : 'Ctrl';
}

// ---------------- fleet panel ----------------
function renderFleet() {
  const fleet = document.getElementById('fleet');
  if(!fleet) return;
  const summary = ui.fleetSummary(devboxes);
  const filtered = ui.filterDevboxes(devboxes, fleetQuery);

  const boxesHtml = filtered.map(d => {
    const boxStatus = ui.devboxStatus(d);
    const agents = (d.agents||[]).map(a => {
      const st = ui.agentStatus(a);
      const active = a.id === curAgentId ? ' is-active' : '';
      return `<div class="agent-row${active}" data-open="${esc(a.id)}" data-name="${esc(a.display_name)}">
        <span class="agent-mono">${esc(ui.initials(a.handle||a.display_name))}</span>
        <div class="agent-main">
          <div class="agent-handle">@${esc(a.handle)}<span class="rt">${esc(ui.runtimeLabel(a.runtime))}</span></div>
          <div class="agent-meta">
            <span class="status is-${st.state}"><span class="status-dot"></span><span class="status-label">${esc(st.label)}</span></span>
          </div>
        </div>
        <div class="agent-actions">
          <button class="ghost" data-hist="${esc(a.id)}" data-histname="${esc(a.display_name)}">History</button>
        </div>
      </div>`;
    }).join('') || '<div class="box-empty">No agents on this devbox yet.</div>';
    return `<div class="box">
      <div class="box-head">
        <span class="status is-${boxStatus.state}"><span class="status-dot"></span></span>
        <span class="box-title">${esc(d.name)}</span>
        <span class="spacer"></span>
        <span class="muted">${esc(boxStatus.label)}</span>
      </div>
      <div class="agent-list">${agents}</div>
      <div class="box-foot">
        <button class="ghost" data-agent="${esc(d.id)}">+ Agent</button>
        <span class="spacer"></span>
        <button class="ghost" data-token="${esc(d.id)}">Rotate token</button>
        <button class="danger" data-del="${esc(d.id)}">Delete</button>
      </div>
    </div>`;
  }).join('');

  const listHtml = filtered.length ? boxesHtml : (devboxes.length
    ? '<div class="fleet-empty"><div class="muted">No matches for your search.</div></div>'
    : `<div class="fleet-empty"><h4>No devboxes yet</h4>
        <p class="muted">Create a devbox, then run the connector on your machine to bring agents online.</p>
        <button id="fleet-create">Create devbox</button></div>`);

  fleet.innerHTML = `
    <div class="fleet-head">
      <div class="fleet-title"><h3>Fleet</h3>
        <button class="ghost" id="newbox" style="padding:4px 9px;font-size:12px">+ Devbox</button></div>
      <div class="fleet-summary">
        <span class="metric"><span class="status-dot" style="background:var(--ok)"></span>
          <b>${summary.devboxOnline}</b>/${summary.devboxTotal} devboxes online</span>
        <span class="metric"><b>${summary.agentOnline}</b>/${summary.agentTotal} agents</span>
      </div>
      <div class="fleet-search">
        <span class="icon">\u2315</span>
        <input id="fleet-q" placeholder="Search devboxes and agents" value="${esc(fleetQuery)}"/>
      </div>
    </div>
    <div class="fleet-list">${listHtml}</div>`;

  const q = document.getElementById('fleet-q');
  if(q){
    q.oninput = () => { fleetQuery = q.value; renderFleet();
      const nq = document.getElementById('fleet-q'); if(nq){ nq.focus();
        nq.setSelectionRange(nq.value.length, nq.value.length); } };
  }
  const nb = document.getElementById('newbox'); if(nb) nb.onclick = createDevbox;
  const fc = document.getElementById('fleet-create'); if(fc) fc.onclick = createDevbox;

  fleet.querySelectorAll('[data-agent]').forEach(b=>b.onclick=()=>createAgent(b.dataset.agent));
  fleet.querySelectorAll('[data-open]').forEach(b=>b.onclick=(e)=>{
    if(e.target.closest('[data-hist]')) return;
    openAgent(b.dataset.open, b.dataset.name);
  });
  fleet.querySelectorAll('[data-hist]').forEach(b=>b.onclick=(e)=>{
    e.stopPropagation();
    openHistory(b.dataset.hist, b.dataset.histname);
  });
  fleet.querySelectorAll('[data-token]').forEach(b=>b.onclick=()=>rotateToken(b.dataset.token));
  fleet.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delDevbox(b.dataset.del));
}

// Empty state for the terminal stage (no agent open).
function renderStageEmpty(){
  const head = document.getElementById('termhead');
  const body = document.getElementById('stagebody');
  if(head) head.innerHTML = `<span class="title">Terminal</span>
    <span class="sep">\u2014</span><span class="muted">no session open</span>`;
  if(!body) return;
  body.innerHTML = `<div class="stage-empty"><div class="inner">
    <div class="art">_</div>
    <h3>Pick an agent to open its terminal</h3>
    <p>Sessions keep running on your devbox even after you close the browser.</p>
    <div class="tips">
      <div class="tip"><span class="kbd">${cmdKeyLabel()} K</span><span>Open the command palette to jump to any agent</span></div>
      <div class="tip"><span class="kbd">Click</span><span>Select an agent in the fleet to attach live</span></div>
      <div class="tip"><span class="kbd">History</span><span>Replay a recorded session from any agent</span></div>
    </div>
  </div></div>`;
}

// ---------------- owner admin ----------------
async function renderOwner(){
  closeOverlay();
  let invites=[], users=[];
  try { [invites, users] = await Promise.all([
    api('/api/invitations'), api('/api/users')]); } catch(e){}
  app.innerHTML = `
  <header class="topbar">
    <div class="brand"><span class="glyph">_</span><b>deepbox</b><span class="tag">owner</span></div>
    <span class="spacer"></span>
    <button class="ghost" id="back">\u2190 Back to switchboard</button>
  </header>
  <main class="owner-main">
    <div class="card">
      <h4>Invitations</h4>
      <div class="row">
        <input id="inote" placeholder="note (optional)"/>
        <input id="ittl" type="number" value="24" title="TTL hours" style="width:120px"/>
        <button id="mint" style="white-space:nowrap">Mint invite</button>
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
      <div class="list-row">
        <span>${i.note?esc(i.note):'<span class="muted">(no note)</span>'}</span>
        <span class="muted">${esc(i.status)} \u00b7 expires ${esc(i.expires_at)}</span>
        <span class="spacer"></span>
        ${i.status==='active'?`<button class="ghost" data-revoke="${esc(i.id)}">Revoke</button>`:''}
      </div>`).join('') || '<div class="muted">No invitations.</div>';
    document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{
      await api(`/api/invitations/${b.dataset.revoke}`,{method:'DELETE'});
      invites = await api('/api/invitations'); renderInv();
    });
  };
  const renderUsers = () => {
    document.getElementById('userlist').innerHTML = users.map(u=>`
      <div class="list-row">
        <span class="avatar" style="width:26px;height:26px;border-radius:6px;background:var(--panel-2);border:1px solid var(--border);display:grid;place-items:center;font-size:11px">${esc(ui.initials(u.display_name||u.username))}</span>
        <b>${esc(u.display_name)}</b>
        <span class="muted">@${esc(u.username)} \u00b7 ${esc(u.role)}${u.disabled?' \u00b7 disabled':''}</span>
        <span class="spacer"></span>
        ${u.role==='member'?(u.disabled
          ?`<button class="ghost" data-enable="${esc(u.id)}">Enable</button>`
          :`<button class="ghost" data-disable="${esc(u.id)}">Disable</button>`):''}
      </div>`).join('') || '<div class="muted">No users.</div>';
    document.querySelectorAll('[data-disable]').forEach(b=>b.onclick=async()=>{
      try{ await api(`/api/users/${b.dataset.disable}/disable`,{method:'POST'}); }
      catch(e){ await showAlert('Cannot disable user', e.message); }
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
      `<div class="token">Invite code (shown once): ${esc(res.token)}<br><br>`+
      `Invite URL: <a href="${esc(url)}">${esc(url)}</a></div>`;
    invites = await api('/api/invitations'); renderInv();
  };
}

// ---------------- devbox / agent mutations ----------------
async function createDevbox() {
  const name = await showForm({
    title:'Create devbox', desc:'Give this devbox a name. Run the connector on it afterwards.',
    fields:[{name:'name', label:'Name', value:'My Devbox', required:true}],
    submit:'Create'});
  if(!name) return;
  const res = await api('/api/devboxes',{method:'POST',body:JSON.stringify({name:name.name})});
  await loadDevboxes(); renderFleet();
  await showToken(res.token);
}
// Present a one-time devbox token. Rendered from memory into a modal only;
// never written to localStorage, cookies, the URL or the console.
async function showToken(tok){
  const command = ui.windowsConnectorCommand(location.origin, tok);
  const bindCopy = (overlay, selector, text)=>{
    const button = overlay.querySelector(selector);
    if(!button) return;
    button.onclick = async ()=>{
      const original = button.textContent;
      try {
        await copyText(text);
        button.textContent = 'Copied';
        setTimeout(()=>{ if(button.isConnected) button.textContent = original; }, 1400);
      } catch(e) {
        button.textContent = 'Copy failed';
        setTimeout(()=>{ if(button.isConnected) button.textContent = original; }, 1800);
      }
    };
  };
  await showModal({
    title:'Devbox token \u2014 shown once',
    desc:'Copy this token now. The server stores only its hash and cannot show it again.',
    bodyHtml:`<div class="token-head"><b>Token</b><button class="ghost compact" id="copy-token">Copy token</button></div>
      <div class="token">${esc(tok)}</div>
      <div class="token-head"><b>Windows connector command</b><button class="ghost compact" id="copy-command">Copy command</button></div>
      <div class="token token-command">${esc(command)}</div>`,
    actions:[{label:'Done', primary:true, value:true}],
    onReady:(overlay)=>{
      bindCopy(overlay, '#copy-token', tok);
      bindCopy(overlay, '#copy-command', command);
    }});
}

async function copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    await navigator.clipboard.writeText(String(text));
    return;
  }
  const area = document.createElement('textarea');
  area.value = String(text);
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if(!copied) throw new Error('Clipboard unavailable');
}
async function rotateToken(id){
  const res = await api(`/api/devboxes/${id}/tokens`,{method:'POST'});
  await showToken(res.token);
}
async function delDevbox(id){
  const ok = await showConfirm('Delete devbox',
    'This removes the devbox and all of its agents. This cannot be undone.', 'Delete');
  if(!ok) return;
  await api(`/api/devboxes/${id}`,{method:'DELETE'});
  await loadDevboxes(); renderFleet();
}
async function createAgent(devboxId){
  const res = await showForm({
    title:'Add agent', desc:'Register an agent runtime on this devbox.',
    fields:[
      {name:'handle', label:'Handle', placeholder:'e.g. claude', required:true},
      {name:'runtime', label:'Runtime adapter ID', placeholder:'e.g. claude-code', required:true},
      {name:'cwd', label:'Working directory (optional)', placeholder:'blank = default'},
    ], submit:'Add agent'});
  if(!res) return;
  await api(`/api/devboxes/${devboxId}/agents`,{method:'POST',
    body:JSON.stringify({handle:res.handle, display_name:res.handle,
      runtime:res.runtime.trim(), cwd:res.cwd||null})});
  await loadDevboxes(); renderFleet();
}

// ---------------- terminal ----------------
let reconnectDelay = 500, reconnectTimer = null, wantOpen = false;

// xterm theme aligned with the dark UI tokens.
const XTERM_THEME = {
  background:'#000000', foreground:'#e6e9ee', cursor:'#3bb1a8',
  cursorAccent:'#000000', selectionBackground:'rgba(59,177,168,0.30)',
  black:'#0b0d10', red:'#e5674f', green:'#4bbf7a', yellow:'#d8a63a',
  blue:'#5aa9e6', magenta:'#b98ae0', cyan:'#3bb1a8', white:'#c9d1d9',
  brightBlack:'#6b7480', brightRed:'#f08a74', brightGreen:'#6fd598',
  brightYellow:'#e6bd5f', brightBlue:'#7fbef0', brightMagenta:'#cda6ec',
  brightCyan:'#5fc7bd', brightWhite:'#f2f5f8',
};

function termHost(){
  let host = document.getElementById('term');
  if(!host){
    const body = document.getElementById('stagebody');
    if(!body) return null;
    body.innerHTML = '<div id="term"></div>';
    host = document.getElementById('term');
  }
  return host;
}

function focusTerminal(force){
  if(!term || replayMode) return;
  const active = document.activeElement;
  const terminalOwnsFocus = !!(active && active.classList
    && active.classList.contains('xterm-helper-textarea'));
  const mayFocus = ui && ui.shouldFocusTerminal
    ? ui.shouldFocusTerminal(force, active && active.tagName, terminalOwnsFocus)
    : force || terminalOwnsFocus;
  if(!mayFocus) return;
  requestAnimationFrame(()=>{
    if(!term || replayMode) return;
    term.focus();
    term.scrollToBottom();
  });
}

function setupTerm(){
  const host = termHost();
  if(!host) return;
  term = new Terminal({fontFamily:"'JetBrains Mono',Consolas,monospace",fontSize:13,
    cursorBlink:true, scrollOnUserInput:true, scrollback:5000, theme:XTERM_THEME});
  fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(host);
  host.addEventListener('pointerdown', ()=>focusTerminal(true));
  fit.fit();
  window.onresize = ()=>{ try{fit.fit(); sendResize();}catch(e){} };
  term.onData(d => { if(!replayMode && termInputSender && curSession
    && canSendInput && canSendInput(collabState))
    termInputSender.push(d); });
}

function resetTerminal(){
  if(term){ try{ term.dispose(); }catch(e){} }
  term = null; fit = null;
  const body = document.getElementById('stagebody');
  if(body) body.innerHTML = '<div id="term"></div>';
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
  curAgentId = agentId;
  renderFleet();
  const wasReplayOrHistory = replayMode || !!document.getElementById('replaybar') ||
    !!document.querySelector('#term [data-replay]') ||
    !!document.querySelector('.history-wrap');
  stopReplay();
  const replaybar = document.getElementById('replaybar');
  if(replaybar) replaybar.remove();
  if(wasReplayOrHistory || !term || !document.getElementById('term')) resetTerminal();
  // leaving previous session? flush input, then detach it (do NOT kill its PTY)
  if(termInputSender) termInputSender.flush();
  if(termWS && termWS.readyState===1 && curSession)
    termWS.send(JSON.stringify({type:'detach',session_id:curSession}));
  document.getElementById('termhead').innerHTML =
    `<span class="title">@<span class="handle">${esc(name)}</span></span>
     <span class="sep">\u2014</span><span class="muted" id="livetag">live terminal</span>
     <span class="spacer"></span>
     <span id="collab" class="collab"></span>
     <span id="stat" class="status"></span>`;
  renderCollab();
  term.reset();
  focusTerminal(true);
  // Resume the newest PTY that is still alive on the devbox. Previously every
  // click silently created a new session, making persisted history invisible.
  const sessions = await api(`/api/agents/${agentId}/sessions`);
  let sess = sessions.find(s => s.state === 'live');
  const resumed = !!sess;
  if(!sess) sess = await api(`/api/agents/${agentId}/sessions`,{method:'POST'});
  curSession = sess.id;
  if(resumed){ const lt = document.getElementById('livetag');
    if(lt) lt.textContent = 'resumed live session'; }
  wantOpen = true;
  connectTermWS();
}

function setStat(txt, state){
  const el = document.getElementById('stat');
  if(el){ el.className = 'status is-' + (state||'offline');
    el.innerHTML = `<span class="status-dot"></span><span class="status-label">${esc(txt)}</span>`; }
}

function connectTermWS(){
  if(termInputSender){ termInputSender.close(); termInputSender = null; }
  if(termWS){ try{ wantOpen && (termWS.onclose=null); termWS.close(); }catch(e){} }
  const proto = location.protocol==='https:'?'wss':'ws';
  termWS = new WebSocket(`${proto}://${location.host}/ws/term`);
  const inputWS = termWS;
  const inputSession = curSession;
  termInputSender = ui.createTerminalInputSender(data => {
    if(inputWS.readyState===1 && inputSession===curSession)
      inputWS.send(JSON.stringify({type:'input',session_id:inputSession,data:data}));
  });
  termWS.onopen = ()=>{
    reconnectDelay = 500;
    setStat('live', 'online');
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
        if(f.state==='live') setStat('live','online');
        else if(f.state==='offline'){ setStat('devbox offline','busy');
          term.write('\r\n[devbox offline \u2014 the connector isn\'t running]\r\n'); }
        else if(f.state==='ended'){ setStat('ended','offline');
          term.write(`\r\n[session ended, code ${f.code}]\r\n`); }
        break;
      case 'exit':
        setStat('ended','offline');
        if(f.data) term.write(f.data);
        term.write(`\r\n[session ended, code ${f.code}]\r\n`); break;
      case 'error':
        term.write(`\r\n[error] ${f.message}\r\n`); break;
      case 'collaboration': {
        const hadKeyboard = !!(collabState && collabState.isHolder);
        collabState = deriveCollaborationState(f, me ? {id: me.id, username: me.username} : null);
        if(!collabState.isHolder){
          keyboardRequester = null;
          if(termInputSender) termInputSender.discard();
        }
        renderCollab();
        if(!hadKeyboard && collabState.isHolder) focusTerminal(true);
        break;
      }
      case 'keyboard_request':
        if(collabState && collabState.isHolder){
          keyboardRequester = {id:f.requester_user_id, username:f.requester_username};
          renderCollab();
        }
        break;
    }
  };
  termWS.onclose = ()=>{
    if(termInputSender){ termInputSender.close(); termInputSender = null; }
    if(!wantOpen) return;
    setStat('reconnecting\u2026','busy');
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
  if(termInputSender) termInputSender.flush();
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
  if(term) term.options.disableStdin = !(s && s.isHolder);
  if(!s){ el.innerHTML = ''; return; }
  let label, cls, btn = '';
  if(s.isViewer){
    label = 'read-only'; cls = 'collab-viewer';
  } else if(s.isHolder){
    label = keyboardRequester
      ? `${esc(keyboardRequester.username)} requests the keyboard`
      : 'you have the keyboard';
    cls = 'collab-holder';
    btn = keyboardRequester
      ? '<button class="ghost" id="collab-handoff">Hand off</button>'
      : '<button class="ghost" id="collab-release">Release</button>';
  } else if(s.heldByOther){
    label = `${esc(s.holderUsername || 'someone')} is typing`; cls = 'collab-busy';
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
  curAgentId = agentId;
  renderFleet();
  if(reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  if(termWS){ try{ termWS.onclose=null; termWS.close(); }catch(e){} termWS=null; }
  curSession = null;

  replayAgentId = agentId; replayAgentName = name;
  let sessions = [];
  try { sessions = await api(`/api/agents/${encodeURIComponent(agentId)}/sessions`); }
  catch(e){ sessions = []; }

  document.getElementById('termhead').innerHTML =
    `<span class="title">@<span class="handle">${esc(name)}</span></span>
     <span class="sep">\u2014</span><span class="muted">session history</span>
     <span class="spacer"></span>
     <button class="ghost" id="hist-live">\u2190 Back to live</button>`;
  document.getElementById('hist-live').onclick = ()=>openAgent(agentId, name);

  if(term){ try{ term.dispose(); }catch(e){} term = null; fit = null; }
  const body = document.getElementById('stagebody');
  body.innerHTML = '<div id="term"></div>';
  const termEl = document.getElementById('term');
  termEl.style.background = 'transparent';

  const list = sessions.map(s=>{
    const st = s.state==='live' ? 'online' : 'offline';
    return `<div class="history-item">
      <span class="status is-${st}"><span class="status-dot"></span><span class="status-label">${esc(s.state||'')}</span></span>
      <b class="mono">${esc(s.id)}</b>
      <span class="muted">started ${esc(s.created_at||'')}</span>
      <span class="spacer"></span>
      <button class="ghost" data-replay="${esc(s.id)}">Replay</button>
    </div>`;
  }).join('') || '<div class="muted" style="padding:14px">No sessions recorded.</div>';
  termEl.innerHTML = `<div class="history-wrap">
    <div class="history-head">Session history for @${esc(name)}</div>${list}</div>`;
  termEl.querySelectorAll('[data-replay]').forEach(b=>
    b.onclick=()=>startReplay(b.dataset.replay));
}

async function startReplay(sessionId){
  let data;
  try { data = await api(`/api/sessions/${sessionId}/replay`); }
  catch(e){ await showAlert('Replay unavailable', e.message); return; }
  replay = normalizeReplay(data);
  replayMode = true;
  replayPlaying = false;
  replaySpeed = 1;
  replayCursor = 0;
  const total = replayDuration(replay);

  // Rebuild xterm after the history list replaced its host contents.
  const body = document.getElementById('stagebody');
  body.innerHTML = '<div id="term"></div>';
  const termEl = document.getElementById('term');
  const bar = document.createElement('div');
  bar.id = 'replaybar';
  bar.style.cssText = 'padding:8px 12px;border-top:1px solid var(--border);background:var(--panel)';
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
      <span class="spacer"></span>
      <span class="muted">retention:</span>
      <select id="rp-retention" style="width:auto">
        <option value="none">none</option>
        <option value="7d">7 days</option>
        <option value="30d">30 days</option>
        <option value="permanent">permanent</option>
      </select>
      <span class="muted" id="rp-retmsg"></span>
    </div>`;
  body.appendChild(bar);
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

// ---------------- command palette ----------------
let paletteState = null; // {items, filtered, index}

function openCommandPalette(){
  if(!ui || !me) return;
  closeOverlay();
  const items = ui.commandItems({devboxes, isOwner: me.role==='owner'});
  paletteState = {items, filtered: items, index: 0};
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'overlay';
  overlay.innerHTML = `
    <div class="palette" role="dialog" aria-label="Command palette">
      <div class="palette-input-wrap">
        <span class="icon">\u203a</span>
        <input class="palette-input" id="palette-q" placeholder="Search agents, history, actions\u2026" autocomplete="off"/>
      </div>
      <div class="palette-list" id="palette-list"></div>
      <div class="palette-foot">
        <span><span class="kbd">\u2191\u2193</span> navigate</span>
        <span><span class="kbd">\u21b5</span> open</span>
        <span><span class="kbd">esc</span> close</span>
      </div>
    </div>`;
  overlay.onclick = (e)=>{ if(e.target===overlay) closeOverlay(); };
  document.body.appendChild(overlay);
  const input = document.getElementById('palette-q');
  input.oninput = ()=>{
    paletteState.filtered = ui.filterCommands(paletteState.items, input.value);
    paletteState.index = 0;
    renderPaletteList();
  };
  input.onkeydown = (e)=>{
    if(e.key==='ArrowDown'){ e.preventDefault();
      paletteState.index = ui.moveSelection(paletteState.index, 1, paletteState.filtered.length); renderPaletteList(); }
    else if(e.key==='ArrowUp'){ e.preventDefault();
      paletteState.index = ui.moveSelection(paletteState.index, -1, paletteState.filtered.length); renderPaletteList(); }
    else if(e.key==='Enter'){ e.preventDefault(); runPaletteItem(paletteState.filtered[paletteState.index]); }
    else if(e.key==='Escape'){ e.preventDefault(); closeOverlay(); }
  };
  renderPaletteList();
  input.focus();
}

function renderPaletteList(){
  const list = document.getElementById('palette-list');
  if(!list || !paletteState) return;
  const badge = (kind)=> kind==='agent'?'agent':kind==='history'?'history':'action';
  list.innerHTML = paletteState.filtered.map((it,i)=>`
    <div class="palette-item${i===paletteState.index?' is-active':''}" data-i="${i}">
      <span class="p-badge">${esc(badge(it.kind))}</span>
      <div class="p-main">
        <div class="p-title">${esc(it.title)}</div>
        <div class="p-sub">${esc(it.subtitle||'')}</div>
      </div>
    </div>`).join('') || '<div class="palette-empty">No matches.</div>';
  list.querySelectorAll('[data-i]').forEach(el=>{
    el.onmousemove = ()=>{ paletteState.index = Number(el.dataset.i);
      list.querySelectorAll('.palette-item').forEach(n=>n.classList.remove('is-active'));
      el.classList.add('is-active'); };
    el.onclick = ()=> runPaletteItem(paletteState.filtered[Number(el.dataset.i)]);
  });
  const active = list.querySelector('.is-active');
  if(active) active.scrollIntoView({block:'nearest'});
}

function findAgent(agentId){
  for(const d of devboxes) for(const a of (d.agents||[]))
    if(a.id===agentId) return {agent:a, devbox:d};
  return null;
}

function runPaletteItem(item){
  if(!item) return;
  closeOverlay();
  if(item.id==='devbox.create'){ createDevbox(); return; }
  if(item.id==='owner.open'){ if(me.role==='owner') renderOwner(); return; }
  if(item.kind==='agent'){ const f = findAgent(item.agentId);
    if(f) openAgent(f.agent.id, f.agent.display_name); return; }
  if(item.kind==='history'){ const f = findAgent(item.agentId);
    if(f) openHistory(f.agent.id, f.agent.display_name); return; }
}

// Global shortcut: Ctrl/Cmd+K toggles the palette (only inside the shell).
document.addEventListener('keydown', (e)=>{
  if((e.ctrlKey||e.metaKey) && (e.key==='k'||e.key==='K')){
    if(!document.getElementById('fleet')) return; // only in the main shell
    e.preventDefault();
    if(document.getElementById('overlay')) closeOverlay();
    else openCommandPalette();
  }
});

// ---------------- overlay: palette + modals ----------------
function closeOverlay(){
  const o = document.getElementById('overlay');
  if(o) o.remove();
  paletteState = null;
}

// Generic modal. Returns a Promise resolving to the chosen action's value.
function showModal({title, desc, bodyHtml, actions, onReady}){
  return new Promise(resolve=>{
    closeOverlay();
    const overlay = document.createElement('div');
    overlay.className = 'overlay'; overlay.id = 'overlay';
    const acts = (actions||[{label:'OK', primary:true, value:true}]);
    overlay.innerHTML = `<div class="modal" role="dialog" aria-label="${esc(title||'')}">
      <div class="modal-head"><h3>${esc(title||'')}</h3>${desc?`<p>${esc(desc)}</p>`:''}</div>
      ${bodyHtml?`<div class="modal-body">${bodyHtml}</div>`:''}
      <div class="modal-actions">${acts.map((a,i)=>
        `<button class="${a.primary?'':'ghost'}${a.danger?' danger':''}" data-a="${i}">${esc(a.label)}</button>`).join('')}</div>
    </div>`;
    const done = (v)=>{ overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e)=>{ if(e.key==='Escape'){ e.preventDefault(); done(null); } };
    overlay.onclick = (e)=>{ if(e.target===overlay) done(null); };
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    if(onReady) onReady(overlay);
    overlay.querySelectorAll('[data-a]').forEach(b=>
      b.onclick = ()=> done(acts[Number(b.dataset.a)].value));
  });
}

function showAlert(title, message){
  return showModal({title, desc:message, actions:[{label:'OK', primary:true, value:true}]});
}
function showConfirm(title, message, confirmLabel){
  return showModal({title, desc:message, actions:[
    {label:'Cancel', value:false},
    {label:confirmLabel||'Confirm', primary:true, danger:true, value:true}]});
}

// Form modal. Returns {field:value,...} on submit, or null on cancel.
function showForm({title, desc, fields, submit}){
  return new Promise(resolve=>{
    closeOverlay();
    const overlay = document.createElement('div');
    overlay.className = 'overlay'; overlay.id = 'overlay';
    const fieldHtml = fields.map((f,i)=>{
      const id = 'f_'+i;
      const control = f.type==='select'
        ? `<select id="${id}">${f.options.map(o=>
            `<option value="${esc(o)}"${o===f.value?' selected':''}>${esc(o)}</option>`).join('')}</select>`
        : `<input id="${id}" placeholder="${esc(f.placeholder||'')}" value="${esc(f.value||'')}"/>`;
      return `<div class="field"><label for="${id}">${esc(f.label)}</label>${control}</div>`;
    }).join('');
    overlay.innerHTML = `<div class="modal" role="dialog" aria-label="${esc(title||'')}">
      <div class="modal-head"><h3>${esc(title||'')}</h3>${desc?`<p>${esc(desc)}</p>`:''}</div>
      <div class="modal-body">${fieldHtml}</div>
      <div class="modal-err" id="form-err"></div>
      <div class="modal-actions">
        <button class="ghost" data-cancel>Cancel</button>
        <button data-submit>${esc(submit||'Save')}</button>
      </div></div>`;
    const done = (v)=>{ overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const collect = ()=>{
      const out = {};
      for(let i=0;i<fields.length;i++) out[fields[i].name] = document.getElementById('f_'+i).value.trim();
      for(const f of fields) if(f.required && !out[f.name]){
        document.getElementById('form-err').textContent = f.label+' is required.'; return null; }
      return out;
    };
    const submitForm = ()=>{ const v = collect(); if(v) done(v); };
    const onKey = (e)=>{
      if(e.key==='Escape'){ e.preventDefault(); done(null); }
      else if(e.key==='Enter'){ e.preventDefault(); submitForm(); }
    };
    overlay.onclick = (e)=>{ if(e.target===overlay) done(null); };
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    overlay.querySelector('[data-cancel]').onclick = ()=> done(null);
    overlay.querySelector('[data-submit]').onclick = submitForm;
    const first = overlay.querySelector('input,select'); if(first) first.focus();
  });
}

// ---------------- start ----------------
Promise.all([loadReplayHelpers(), loadUI()]).then(boot).catch(error => {
  app.innerHTML = `<div class="auth"><div class="auth-card"><h2>Unable to start</h2>
    <p class="muted">${esc(error.message)}</p></div></div>`;
});
