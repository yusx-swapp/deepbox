(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  if(root) root.DeepboxUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  'use strict';

  // ---- text helpers ------------------------------------------------------

  // HTML-escape for safe interpolation of any server-provided string.
  function escapeHtml(value){
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
  }

  // Short label for an avatar/monogram from a display name or handle.
  function initials(name){
    const cleaned = String(name == null ? '' : name).trim();
    if(!cleaned) return '?';
    const words = cleaned.replace(/^[@#]+/, '').split(/[\s._-]+/).filter(Boolean);
    if(words.length === 0) return '?';
    if(words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  }

  // Runtime IDs are adapter-owned and intentionally opaque to the web app. A new
  // connector adapter must remain usable without a frontend change, so the UI
  // displays the reported identifier rather than maintaining a runtime registry.
  function runtimeLabel(runtime){
    const value = String(runtime == null ? '' : runtime).trim();
    return value || 'runtime';
  }

  // Generate the exact Windows connector bootstrap command shown after a
  // devbox token is minted. Keeping this pure makes wrapping/copy regressions
  // testable without a browser.
  function windowsConnectorCommand(serverUrl, token){
    const server = String(serverUrl == null ? '' : serverUrl).trim();
    const secret = String(token == null ? '' : token).trim();
    return [
      'set "DEEPBOX_SERVER_URL=' + server + '"',
      'set "DEEPBOX_TOKEN=' + secret + '"',
      'python -m connector --doctor',
      'python -m connector',
    ].join('\n');
  }

  // ---- terminal input ----------------------------------------------------

  // Forward xterm input synchronously. A terminal is latency-sensitive: adding
  // even a short idle batching timer makes key echo feel sticky because the user
  // already pays the browser -> server -> connector -> PTY round trip. The
  // lifecycle guard prevents a sender captured by an old WebSocket from leaking
  // keystrokes after a reconnect or session switch.
  function shouldFocusTerminal(force, activeTag, terminalOwnsFocus){
    if(force || terminalOwnsFocus) return true;
    return !/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(String(activeTag || '').toUpperCase());
  }

  function createTerminalInputSender(send){
    let active = true;
    function push(data){
      const chunk = String(data == null ? '' : data);
      if(active && chunk) send(chunk);
    }
    // Kept for the lease transition API: there is no buffered input to drop.
    function discard(){}
    function close(){ active = false; }
    return {push: push, flush: function(){}, discard: discard, close: close};
  }

  // ---- status helpers ----------------------------------------------------

  // Devbox connection state -> {state, label}. Text is never colour-only.
  function devboxStatus(devbox){
    const online = !!(devbox && devbox.online);
    return {
      state: online ? 'online' : 'offline',
      label: online ? 'Online' : 'Offline',
    };
  }

  // Agent presence -> {state, label}.
  function agentStatus(agent){
    const presence = agent && agent.presence;
    if(presence === 'online') return {state: 'online', label: 'Online'};
    if(presence === 'busy') return {state: 'busy', label: 'Busy'};
    return {state: 'offline', label: 'Offline'};
  }

  // ---- fleet aggregation -------------------------------------------------

  // Roll up a fleet into online/total counts for both devboxes and agents.
  function fleetSummary(devboxes){
    const list = Array.isArray(devboxes) ? devboxes : [];
    let devboxOnline = 0, agentTotal = 0, agentOnline = 0;
    for(const box of list){
      if(box && box.online) devboxOnline++;
      const agents = (box && Array.isArray(box.agents)) ? box.agents : [];
      for(const agent of agents){
        agentTotal++;
        if(agent && agent.presence === 'online') agentOnline++;
      }
    }
    return {
      devboxTotal: list.length,
      devboxOnline: devboxOnline,
      agentTotal: agentTotal,
      agentOnline: agentOnline,
    };
  }

  // ---- filtering ---------------------------------------------------------

  function normalize(value){
    return String(value == null ? '' : value).toLowerCase().trim();
  }

  function agentMatches(agent, needle){
    if(!needle) return true;
    const hay = [
      agent && agent.display_name,
      agent && agent.handle,
      agent && agent.runtime,
      runtimeLabel(agent && agent.runtime),
    ].map(normalize).join(' ');
    return hay.indexOf(needle) !== -1;
  }

  // Filter a fleet by a free-text query. A devbox is kept when its name or ID
  // matches, or when any of its agents match; matching agents are kept.
  function filterDevboxes(devboxes, query){
    const needle = normalize(query);
    const list = Array.isArray(devboxes) ? devboxes : [];
    if(!needle) return list.slice();
    const result = [];
    for(const box of list){
      const agents = (box && Array.isArray(box.agents)) ? box.agents : [];
      const boxHay = [box && box.name, box && box.id].map(normalize).join(' ');
      const boxHit = boxHay.indexOf(needle) !== -1;
      const matchedAgents = agents.filter(a => agentMatches(a, needle));
      if(boxHit){
        result.push(box);
      } else if(matchedAgents.length){
        result.push(Object.assign({}, box, {agents: matchedAgents}));
      }
    }
    return result;
  }

  // Flat list of every agent in the fleet, annotated with its devbox.
  function flattenAgents(devboxes){
    const list = Array.isArray(devboxes) ? devboxes : [];
    const out = [];
    for(const box of list){
      const agents = (box && Array.isArray(box.agents)) ? box.agents : [];
      for(const agent of agents){
        out.push(Object.assign({}, agent, {
          devbox_id: box && box.id,
          devbox_name: box && box.name,
          devbox_online: !!(box && box.online),
        }));
      }
    }
    return out;
  }

  function filterAgents(agents, query){
    const needle = normalize(query);
    const list = Array.isArray(agents) ? agents : [];
    if(!needle) return list.slice();
    return list.filter(a => agentMatches(a, needle) ||
      normalize(a && a.devbox_name).indexOf(needle) !== -1);
  }

  // ---- command palette ---------------------------------------------------

  // Build the command list from the current view model. Pure data only; the
  // caller wires the `action` ids to real handlers.
  function commandItems(context){
    context = context || {};
    const items = [];
    items.push({id: 'devbox.create', kind: 'action', title: 'Create devbox',
      subtitle: 'Register a new devbox', keywords: 'new add box'});
    if(context.isOwner){
      items.push({id: 'owner.open', kind: 'action', title: 'Open owner console',
        subtitle: 'Invitations and members', keywords: 'admin invite member'});
    }
    const agents = flattenAgents(context.devboxes);
    for(const agent of agents){
      const status = agentStatus(agent);
      items.push({
        id: 'agent.open:' + agent.id,
        kind: 'agent',
        agentId: agent.id,
        title: '@' + (agent.handle || agent.display_name || agent.id),
        subtitle: (agent.devbox_name || 'devbox') + ' \u00b7 ' +
          runtimeLabel(agent.runtime) + ' \u00b7 ' + status.label,
        keywords: [agent.display_name, agent.handle, agent.runtime,
          agent.devbox_name].filter(Boolean).join(' '),
      });
      items.push({
        id: 'agent.history:' + agent.id,
        kind: 'history',
        agentId: agent.id,
        title: 'History: @' + (agent.handle || agent.display_name || agent.id),
        subtitle: 'Replay recorded sessions on ' + (agent.devbox_name || 'devbox'),
        keywords: ['history replay', agent.display_name, agent.handle,
          agent.devbox_name].filter(Boolean).join(' '),
      });
    }
    return items;
  }

  // Rank/filter command items by a query. Empty query keeps original order.
  function filterCommands(items, query){
    const list = Array.isArray(items) ? items : [];
    const needle = normalize(query);
    if(!needle) return list.slice();
    const scored = [];
    for(const item of list){
      const title = normalize(item.title);
      const hay = [title, normalize(item.subtitle), normalize(item.keywords)].join(' ');
      const idx = hay.indexOf(needle);
      if(idx === -1) continue;
      let score = idx;
      if(title.indexOf(needle) === 0) score -= 100;   // prefix on title wins
      else if(title.indexOf(needle) !== -1) score -= 40;
      scored.push({item: item, score: score});
    }
    scored.sort((a, b) => a.score - b.score);
    return scored.map(s => s.item);
  }

  // Clamp/rotate the active index over a list length for keyboard nav.
  function moveSelection(current, delta, length){
    if(!length) return 0;
    const next = (current + delta) % length;
    return next < 0 ? next + length : next;
  }

  return {
    escapeHtml: escapeHtml,
    initials: initials,
    runtimeLabel: runtimeLabel,
    windowsConnectorCommand: windowsConnectorCommand,
    shouldFocusTerminal: shouldFocusTerminal,
    createTerminalInputSender: createTerminalInputSender,
    devboxStatus: devboxStatus,
    agentStatus: agentStatus,
    fleetSummary: fleetSummary,
    filterDevboxes: filterDevboxes,
    flattenAgents: flattenAgents,
    filterAgents: filterAgents,
    commandItems: commandItems,
    filterCommands: filterCommands,
    moveSelection: moveSelection,
  };
});
