const test = require('node:test');
const assert = require('node:assert/strict');
const {deriveCollaborationState, canSendInput} = require('./collaboration.js');

function frame(role, keyboard, sessionId){
  return {type:'collaboration', session_id: sessionId || 's1', role, keyboard: keyboard || {}};
}

test('viewer is always read-only and cannot request or send input', () => {
  const state = deriveCollaborationState(
    frame('viewer', {holder_user_id: 5, holder_username: 'ann', is_holder: false, can_request: false}),
    {id: 9, username: 'me'});
  assert.equal(state.isViewer, true);
  assert.equal(state.canOperate, false);
  assert.equal(state.canRequest, false);
  assert.equal(state.canRelease, false);
  assert.equal(state.isHolder, false);
  assert.equal(state.status, 'busy');
  assert.equal(canSendInput(state), false);
});

test('holder can send input and release, not request', () => {
  const state = deriveCollaborationState(
    frame('operator', {holder_user_id: 9, holder_username: 'me', is_holder: true,
      can_request: false, expires_at: '2026-01-01T00:00:00Z'}),
    {id: 9, username: 'me'});
  assert.equal(state.isHolder, true);
  assert.equal(state.canRelease, true);
  assert.equal(state.canRequest, false);
  assert.equal(state.status, 'holding');
  assert.equal(state.expiresAt, '2026-01-01T00:00:00Z');
  assert.equal(canSendInput(state), true);
});

test('operator sees busy lease held by another and may request', () => {
  const state = deriveCollaborationState(
    frame('operator', {holder_user_id: 5, holder_username: 'ann', is_holder: false, can_request: true}),
    {id: 9, username: 'me'});
  assert.equal(state.heldByOther, true);
  assert.equal(state.status, 'busy');
  assert.equal(state.canRequest, true);
  assert.equal(state.canRelease, false);
  assert.equal(canSendInput(state), false);
});

test('no lease: operator can request when free', () => {
  const state = deriveCollaborationState(
    frame('admin', {holder_user_id: null, holder_username: null}),
    {id: 9, username: 'me'});
  assert.equal(state.heldByAnyone, false);
  assert.equal(state.status, 'free');
  assert.equal(state.canRequest, true);
  assert.equal(state.canRelease, false);
  assert.equal(canSendInput(state), false);
});

test('is_holder inferred from current user when server omits the flag', () => {
  const held = deriveCollaborationState(
    frame('owner', {holder_user_id: 9, holder_username: 'me'}),
    {id: 9, username: 'me'});
  assert.equal(held.isHolder, true);
  assert.equal(canSendInput(held), true);
  const other = deriveCollaborationState(
    frame('owner', {holder_user_id: 5, holder_username: 'ann'}),
    {id: 9, username: 'me'});
  assert.equal(other.isHolder, false);
  assert.equal(other.canRequest, true);
});

test('stale-session frame surfaces its session id for gating', () => {
  const state = deriveCollaborationState(
    frame('operator', {holder_user_id: 9, is_holder: true}, 'old-session'),
    {id: 9, username: 'me'});
  assert.equal(state.sessionId, 'old-session');
  // A frame for a different session than the active one must be ignored by the
  // caller; canSendInput itself only reflects holder status.
  assert.notEqual(state.sessionId, 's1');
});

test('empty / missing frame degrades to safe read-only defaults', () => {
  const state = deriveCollaborationState(undefined, undefined);
  assert.equal(state.role, 'viewer');
  assert.equal(state.isViewer, true);
  assert.equal(state.status, 'free');
  assert.equal(state.canRequest, false);
  assert.equal(canSendInput(state), false);
  assert.equal(canSendInput(null), false);
});
