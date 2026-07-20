const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('./ui.js');

const fleet = [
  {id: 'b1', name: 'workstation', online: true, agents: [
    {id: 'a1', handle: 'claude', display_name: 'Claude', runtime: 'claude-code', presence: 'online'},
    {id: 'a2', handle: 'codex', display_name: 'Codex', runtime: 'codex-cli', presence: 'offline'},
  ]},
  {id: 'b2', name: 'laptop', online: false, agents: [
    {id: 'a3', handle: 'copilot', display_name: 'Copilot', runtime: 'copilot-cli', presence: 'busy'},
  ]},
];

test('escapeHtml neutralizes markup and quotes', () => {
  assert.equal(ui.escapeHtml('<b>"x"&\'</b>'),
    '&lt;b&gt;&quot;x&quot;&amp;&#39;&lt;/b&gt;');
  assert.equal(ui.escapeHtml(null), '');
});

test('initials builds a stable monogram', () => {
  assert.equal(ui.initials('Claude Code'), 'CC');
  assert.equal(ui.initials('@copilot'), 'CO');
  assert.equal(ui.initials(''), '?');
  assert.equal(ui.initials('  '), '?');
});

test('runtimeLabel keeps adapter-owned runtime IDs opaque', () => {
  assert.equal(ui.runtimeLabel('claude-code'), 'claude-code');
  assert.equal(ui.runtimeLabel('mystery-runtime'), 'mystery-runtime');
  assert.equal(ui.runtimeLabel(''), 'runtime');
});

test('windowsConnectorCommand is complete and directly copyable', () => {
  assert.equal(
    ui.windowsConnectorCommand('https://deepbox.example', 'hpc_box_test'),
    'set "DEEPBOX_SERVER_URL=https://deepbox.example"\n' +
      'set "DEEPBOX_TOKEN=hpc_box_test"\n' +
      'python -m connector --doctor\n' +
      'python -m connector'
  );
});

test('status helpers always return a text label', () => {
  assert.deepEqual(ui.devboxStatus({online: true}), {state: 'online', label: 'Online'});
  assert.deepEqual(ui.devboxStatus({online: false}), {state: 'offline', label: 'Offline'});
  assert.equal(ui.agentStatus({presence: 'busy'}).label, 'Busy');
  assert.equal(ui.agentStatus({}).state, 'offline');
});

test('fleetSummary rolls up online/total counts', () => {
  assert.deepEqual(ui.fleetSummary(fleet), {
    devboxTotal: 2, devboxOnline: 1, agentTotal: 3, agentOnline: 1,
  });
  assert.deepEqual(ui.fleetSummary(null), {
    devboxTotal: 0, devboxOnline: 0, agentTotal: 0, agentOnline: 0,
  });
});

test('filterDevboxes keeps devbox and narrows agents on agent match', () => {
  const all = ui.filterDevboxes(fleet, '');
  assert.equal(all.length, 2);
  const byBox = ui.filterDevboxes(fleet, 'laptop');
  assert.equal(byBox.length, 1);
  assert.equal(byBox[0].id, 'b2');
  const byAgent = ui.filterDevboxes(fleet, 'codex');
  assert.equal(byAgent.length, 1);
  assert.equal(byAgent[0].agents.length, 1);
  assert.equal(byAgent[0].agents[0].id, 'a2');
  // Original fleet is not mutated.
  assert.equal(fleet[0].agents.length, 2);
});

test('flattenAgents annotates each agent with its devbox', () => {
  const flat = ui.flattenAgents(fleet);
  assert.equal(flat.length, 3);
  assert.equal(flat[0].devbox_name, 'workstation');
  assert.equal(flat[2].devbox_online, false);
});

test('filterAgents matches handle, runtime label and devbox', () => {
  const flat = ui.flattenAgents(fleet);
  assert.equal(ui.filterAgents(flat, 'copilot').length, 1);
  assert.equal(ui.filterAgents(flat, 'laptop').length, 1);
  assert.equal(ui.filterAgents(flat, '').length, 3);
});

test('commandItems includes actions, agents and owner console when allowed', () => {
  const ownerItems = ui.commandItems({devboxes: fleet, isOwner: true});
  assert.ok(ownerItems.some(i => i.id === 'owner.open'));
  assert.ok(ownerItems.some(i => i.id === 'devbox.create'));
  assert.ok(ownerItems.some(i => i.kind === 'agent' && i.agentId === 'a1'));
  assert.ok(ownerItems.some(i => i.kind === 'history' && i.agentId === 'a1'));
  const memberItems = ui.commandItems({devboxes: fleet, isOwner: false});
  assert.ok(!memberItems.some(i => i.id === 'owner.open'));
});

test('filterCommands ranks title-prefix matches first', () => {
  const items = ui.commandItems({devboxes: fleet, isOwner: true});
  const hits = ui.filterCommands(items, 'claude');
  assert.ok(hits.length >= 1);
  assert.ok(/claude/i.test(hits[0].title));
  assert.equal(ui.filterCommands(items, 'zzz-none').length, 0);
  assert.equal(ui.filterCommands(items, '').length, items.length);
});

test('moveSelection wraps in both directions', () => {
  assert.equal(ui.moveSelection(0, 1, 3), 1);
  assert.equal(ui.moveSelection(2, 1, 3), 0);
  assert.equal(ui.moveSelection(0, -1, 3), 2);
  assert.equal(ui.moveSelection(0, 1, 0), 0);
});


test('terminal focus policy protects forms but restores xterm deliberately', () => {
  assert.equal(ui.shouldFocusTerminal(false, 'INPUT', false), false);
  assert.equal(ui.shouldFocusTerminal(false, 'BUTTON', false), false);
  assert.equal(ui.shouldFocusTerminal(false, 'BODY', false), true);
  assert.equal(ui.shouldFocusTerminal(false, 'TEXTAREA', true), true);
  assert.equal(ui.shouldFocusTerminal(true, 'INPUT', false), true);
});

test('terminal input sender forwards each xterm event synchronously', () => {
  const sent = [];
  const sender = ui.createTerminalInputSender(sent.push.bind(sent));
  sender.push('a');
  sender.push('b');
  assert.deepEqual(sent, ['a', 'b']);
});

test('terminal input sender survives lease transitions but closes with its socket', () => {
  const sent = [];
  const sender = ui.createTerminalInputSender(sent.push.bind(sent));
  sender.push('before');
  sender.discard();
  sender.push('after reacquire');
  sender.close();
  sender.push('stale socket');
  assert.deepEqual(sent, ['before', 'after reacquire']);
});
