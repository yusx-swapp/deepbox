(function(root, factory){
  const api = factory();
  if(typeof module === 'object' && module.exports) module.exports = api;
  if(root) root.DeepboxCollaboration = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  // Roles that are allowed to type when they hold the keyboard lease.
  const OPERATOR_ROLES = ['operator', 'admin', 'owner'];

  // deriveCollaborationState(frame, currentUser?)
  //   frame       : the raw {type:'collaboration', ...} message from the server.
  //   currentUser : optional {id, username} of the logged-in user, used as a
  //                 fallback when the server does not stamp is_holder/can_request.
  // Returns a normalized, DOM-free view model consumed by the UI and canSendInput.
  function deriveCollaborationState(frame, currentUser){
    frame = frame || {};
    const keyboard = frame.keyboard || {};
    const role = frame.role || 'viewer';
    const isViewer = role === 'viewer';
    const canOperate = OPERATOR_ROLES.indexOf(role) !== -1;

    const holderUserId = keyboard.holder_user_id != null ? keyboard.holder_user_id : null;
    const holderUsername = keyboard.holder_username != null ? keyboard.holder_username : null;
    const expiresAt = keyboard.expires_at != null ? keyboard.expires_at : null;
    const heldByAnyone = holderUserId != null;

    // Prefer the server's authoritative flags; fall back to matching the current
    // user against the holder id when the server omits is_holder.
    let isHolder;
    if(typeof keyboard.is_holder === 'boolean') isHolder = keyboard.is_holder;
    else isHolder = !!(currentUser && currentUser.id != null && currentUser.id === holderUserId);
    if(isViewer) isHolder = false;

    const heldByOther = heldByAnyone && !isHolder;

    // can_request from the server wins; otherwise operators may request when they
    // are not already holding the lease.
    let canRequest;
    if(typeof keyboard.can_request === 'boolean') canRequest = keyboard.can_request;
    else canRequest = canOperate && !isHolder;
    if(isViewer) canRequest = false;

    const canRelease = isHolder;

    let status;
    if(isHolder) status = 'holding';
    else if(heldByOther) status = 'busy';
    else status = 'free';

    return {
      sessionId: frame.session_id != null ? frame.session_id : null,
      role,
      isViewer,
      canOperate,
      holderUserId,
      holderUsername,
      expiresAt,
      heldByAnyone,
      heldByOther,
      isHolder,
      canRequest,
      canRelease,
      status,
    };
  }

  // canSendInput(state): only the current keyboard holder may transmit input.
  function canSendInput(state){
    return !!(state && state.isHolder === true);
  }

  return {deriveCollaborationState, canSendInput, OPERATOR_ROLES};
});
