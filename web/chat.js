/* Deepbox structured chat surface (Cut 10).
 *
 * Renders the agent-agnostic *canonical event* stream (see
 * connector/agent_session.py) as a chat UI — assistant bubbles with streaming
 * text, tool cards, and a permission prompt — instead of a terminal. Loaded
 * lazily by app.js and used only for agents whose runtime is `structured`.
 *
 * Design: a pure reducer `applyEvent(state, ev)` folds one canonical event into
 * an immutable-ish view model (so it is unit-testable under node --test with no
 * DOM), plus a thin DOM renderer `renderChat(container, state, handlers)`.
 */
(function (global) {
  'use strict';

  function initialChatState() {
    return {
      items: [],          // ordered: {kind, ...} render items
      pendingPermission: null, // {request_id, tool, input} or null
      status: null,       // last status note
      _openAssistant: null, // index of the assistant bubble accreting deltas
    };
  }

  // Fold one canonical event into state. Returns the SAME state object mutated
  // (callers treat it as owned) — cheap and enough for our append-only UI.
  function applyEvent(state, ev) {
    if (!ev || typeof ev !== 'object') return state;
    switch (ev.ev) {
      case 'status':
        state.status = ev.subtype || ev.note || ev.model || 'status';
        break;
      case 'message.delta': {
        let idx = state._openAssistant;
        if (idx == null) {
          state.items.push({ kind: 'assistant', text: '' });
          idx = state.items.length - 1;
          state._openAssistant = idx;
        }
        state.items[idx].text += (ev.text || '');
        break;
      }
      case 'message': {
        // A complete assistant message. If we were streaming and it carries no
        // text (a message_stop marker), just close the open bubble.
        if (ev.text) {
          if (state._openAssistant != null) {
            // Prefer the streamed text already accreted; only replace if empty.
            const cur = state.items[state._openAssistant];
            if (!cur.text) cur.text = ev.text;
          } else {
            state.items.push({ kind: 'assistant', text: ev.text });
          }
        }
        if (ev.final) state._openAssistant = null;
        break;
      }
      case 'tool.call':
        state._openAssistant = null;
        state.items.push({
          kind: 'tool', tool: ev.tool, tool_id: ev.tool_id,
          input: ev.input, streaming: !!ev.streaming, result: null,
          is_error: false,
        });
        break;
      case 'tool.result': {
        // Attach to the matching tool card if present, else append a card.
        let matched = null;
        for (let i = state.items.length - 1; i >= 0; i--) {
          const it = state.items[i];
          if (it.kind === 'tool' && it.tool_id === ev.tool_id && it.result == null) {
            matched = it; break;
          }
        }
        if (matched) {
          matched.result = ev.content || '';
          matched.is_error = !!ev.is_error;
        } else {
          state.items.push({
            kind: 'tool', tool: null, tool_id: ev.tool_id,
            input: null, result: ev.content || '', is_error: !!ev.is_error,
          });
        }
        break;
      }
      case 'permission.ask':
        state.pendingPermission = {
          request_id: ev.request_id, tool: ev.tool, input: ev.input,
        };
        break;
      case 'turn.end':
        state._openAssistant = null;
        state.items.push({
          kind: 'turn', is_error: !!ev.is_error, cost_usd: ev.cost_usd,
          result: ev.result || null,
        });
        break;
      case 'user.echo':
        state.items.push({ kind: 'user', text: ev.text || '' });
        break;
      case 'error':
        state.items.push({ kind: 'error', text: ev.message || 'error' });
        break;
      default:
        break;
    }
    return state;
  }

  // Append a local user turn immediately (0-RTT echo) before the agent replies.
  function appendUserTurn(state, text) {
    state._openAssistant = null;
    state.items.push({ kind: 'user', text: text });
    return state;
  }

  // --- DOM rendering (browser only) ---------------------------------------

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function renderChat(container, state, handlers) {
    if (!container) return;
    handlers = handlers || {};
    container.innerHTML = '';
    const log = el('div', 'chat-log');
    for (const it of state.items) {
      if (it.kind === 'user') {
        log.appendChild(bubble('user', it.text));
      } else if (it.kind === 'assistant') {
        log.appendChild(bubble('assistant', it.text));
      } else if (it.kind === 'tool') {
        log.appendChild(toolCard(it));
      } else if (it.kind === 'turn') {
        if (it.result) log.appendChild(bubble('assistant', it.result));
        log.appendChild(turnFooter(it));
      } else if (it.kind === 'error') {
        log.appendChild(bubble('error', it.text));
      }
    }
    container.appendChild(log);
    if (state.pendingPermission) {
      container.appendChild(permissionModal(state.pendingPermission, handlers));
    }
    // Auto-scroll to newest.
    log.scrollTop = log.scrollHeight;
  }

  function bubble(role, text) {
    const b = el('div', 'chat-msg chat-' + role);
    b.appendChild(el('div', 'chat-role', role));
    b.appendChild(el('div', 'chat-text', text || ''));
    return b;
  }

  function toolCard(it) {
    const c = el('div', 'chat-tool' + (it.is_error ? ' chat-tool-error' : ''));
    c.appendChild(el('div', 'chat-tool-name', (it.tool || 'tool') +
      (it.streaming && it.result == null ? ' \u2026' : '')));
    if (it.input != null) {
      c.appendChild(el('pre', 'chat-tool-input',
        typeof it.input === 'string' ? it.input : JSON.stringify(it.input, null, 2)));
    }
    if (it.result != null) {
      c.appendChild(el('pre', 'chat-tool-result', String(it.result)));
    }
    return c;
  }

  function turnFooter(it) {
    const parts = [];
    if (it.is_error) parts.push('error');
    if (it.cost_usd != null) parts.push('$' + Number(it.cost_usd).toFixed(4));
    const f = el('div', 'chat-turn', parts.join('  \u00b7  ') || 'turn complete');
    return f;
  }

  function permissionModal(p, handlers) {
    const m = el('div', 'chat-perm');
    m.appendChild(el('div', 'chat-perm-title',
      'Allow ' + (p.tool || 'tool') + '?'));
    if (p.input != null) {
      m.appendChild(el('pre', 'chat-perm-input',
        typeof p.input === 'string' ? p.input : JSON.stringify(p.input, null, 2)));
    }
    const row = el('div', 'chat-perm-actions');
    const allow = el('button', 'chat-perm-allow', 'Allow');
    const deny = el('button', 'chat-perm-deny', 'Deny');
    allow.addEventListener('click', () => handlers.onPermission &&
      handlers.onPermission(p.request_id, true));
    deny.addEventListener('click', () => handlers.onPermission &&
      handlers.onPermission(p.request_id, false));
    row.appendChild(allow); row.appendChild(deny);
    m.appendChild(row);
    return m;
  }

  const api = { initialChatState, applyEvent, appendUserTurn, renderChat };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.DeepboxChat = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
