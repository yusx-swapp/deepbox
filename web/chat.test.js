'use strict';
const test = require('node:test');
const assert = require('node:assert');
const C = require('./chat.js');

test('message.delta accretes into one assistant bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'Hel' });
  C.applyEvent(s, { ev: 'message.delta', text: 'lo' });
  assert.strictEqual(s.items.length, 1);
  assert.strictEqual(s.items[0].kind, 'assistant');
  assert.strictEqual(s.items[0].text, 'Hello');
});

test('message final closes the open assistant bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'Hi' });
  C.applyEvent(s, { ev: 'message', final: true, text: '' });
  assert.strictEqual(s._openAssistant, null);
  C.applyEvent(s, { ev: 'message.delta', text: 'Next' });
  assert.strictEqual(s.items.length, 2);
});

test('tool.call then tool.result attach to same card', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'tool.call', tool: 'Bash', tool_id: 't1', input: { command: 'ls' } });
  C.applyEvent(s, { ev: 'tool.result', tool_id: 't1', content: 'file1', is_error: false });
  const cards = s.items.filter(i => i.kind === 'tool');
  assert.strictEqual(cards.length, 1);
  assert.strictEqual(cards[0].result, 'file1');
  assert.strictEqual(cards[0].tool, 'Bash');
});

test('permission.ask sets pendingPermission', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'permission.ask', request_id: 'r1', tool: 'Write', input: {} });
  assert.ok(s.pendingPermission);
  assert.strictEqual(s.pendingPermission.request_id, 'r1');
});

test('turn.end appends a turn item and closes assistant', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'x' });
  C.applyEvent(s, { ev: 'turn.end', is_error: false, cost_usd: 0.01 });
  assert.strictEqual(s._openAssistant, null);
  assert.ok(s.items.some(i => i.kind === 'turn'));
});

test('appendUserTurn adds a user bubble immediately', () => {
  let s = C.initialChatState();
  C.appendUserTurn(s, 'do it');
  assert.strictEqual(s.items[0].kind, 'user');
  assert.strictEqual(s.items[0].text, 'do it');
});

test('tool.call after streaming deltas closes the open bubble', () => {
  let s = C.initialChatState();
  C.applyEvent(s, { ev: 'message.delta', text: 'thinking' });
  C.applyEvent(s, { ev: 'tool.call', tool: 'Read', tool_id: 't2', input: {} });
  assert.strictEqual(s._openAssistant, null);
  assert.strictEqual(s.items.length, 2);
});
