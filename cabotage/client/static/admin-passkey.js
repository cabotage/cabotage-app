/* Native WebAuthn; drafts, files and one-use proofs stay in this page's memory. */
(() => {
  if (window.cabotageAdminPasskey) return;
  window.cabotageAdminPasskey = true;
  const nativeFetch = window.fetch.bind(window);
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content
    || document.querySelector('[name="csrf_token"]')?.value || '';
  const decode = value => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const encode = value => btoa(String.fromCharCode(...new Uint8Array(value)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const entryContext = {kind: 'entry', options_url: '/admin/passkey/options', verify_url: '/admin/passkey/verify'};
  const band = document.querySelector('[data-admin-access-band]');
  let accessDeadline = 0;
  let accessExpiry = null;
  let accessServerTime = 0;
  let busy = false;
  let statusRequest = null;
  const cleanup = [];

  function updateBand(state, started = Date.now()) {
    if (!band || state.server_time < accessServerTime) return;
    accessServerTime = state.server_time;
    const deadline = state.active && state.expires_at
      ? started + (state.expires_at - state.server_time) * 1000 : 0;
    // A status refresh cannot extend the deadline of the same absolute grant.
    accessDeadline = state.expires_at === accessExpiry && accessDeadline
      ? Math.min(accessDeadline, deadline) : deadline;
    accessExpiry = state.expires_at;
    band.dataset.expiresAt = state.expires_at || '';
    band.dataset.serverTime = state.server_time;
    renderBand();
  }

  function renderBand() {
    if (!band) return;
    const remaining = Math.max(0, Math.ceil((accessDeadline - Date.now()) / 1000));
    const active = remaining > 0;
    band.dataset.active = String(active);
    band.querySelector('[data-admin-access-label]').textContent = active ? 'Admin access' : 'Admin access ended';
    band.querySelector('[data-admin-access-countdown]').textContent = active
      ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')} remaining`
      : band.dataset.endedText || 'Your unsaved changes are still here.';
    band.querySelector('[data-admin-access-renew]').hidden = active;
    band.querySelector('[data-admin-access-end]').hidden = !active;
  }

  async function refreshStatus() {
    if (statusRequest) return statusRequest;
    const started = Date.now();
    statusRequest = (async () => {
      const response = await nativeFetch('/admin/passkey/status', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('Could not check admin access. Your changes are still here; try again.');
      const state = await response.json();
      updateBand(state, started);
      return state;
    })();
    try { return await statusRequest; } finally { statusRequest = null; }
  }

  async function jsonPost(url, data, signal) {
    const response = await nativeFetch(url, {
      method: 'POST', credentials: 'same-origin', signal,
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()},
      body: JSON.stringify(data),
    });
    if (!response.ok) {
      const detail = response.headers.get('Content-Type')?.includes('application/json')
        ? await response.json() : {};
      throw new Error(detail.error || 'Passkey verification failed or approval expired. Nothing changed. Try the action again.');
    }
    if (response.redirected || !response.headers.get('Content-Type')?.includes('application/json')) {
      throw new Error('Your sign-in ended. Keep this page open to copy your unsaved changes, then sign in again.');
    }
    return response.json();
  }

  async function assertion(context, signal) {
    if (!window.isSecureContext || !navigator.credentials) {
      throw new Error('A secure connection and a passkey-capable browser are required.');
    }
    const options = await jsonPost(context.options_url, {request_id: context.request_id}, signal);
    const publicKey = options.publicKey;
    publicKey.challenge = decode(publicKey.challenge);
    publicKey.allowCredentials = (publicKey.allowCredentials || []).map(credential => ({
      ...credential, id: decode(credential.id),
    }));
    const credential = await navigator.credentials.get({publicKey, signal});
    if (!credential) throw new Error('No passkey was selected. Nothing changed.');
    const response = credential.response;
    const started = Date.now();
    const proof = await jsonPost(context.verify_url, {
      request_id: options.request_id,
      credential: {
        id: credential.id, rawId: encode(credential.rawId), type: credential.type,
        response: {
          authenticatorData: encode(response.authenticatorData),
          clientDataJSON: encode(response.clientDataJSON),
          signature: encode(response.signature),
          userHandle: response.userHandle ? encode(response.userHandle) : null,
        },
        clientExtensionResults: credential.getClientExtensionResults(),
      },
    }, signal);
    if (proof.admin_access) updateBand(proof.admin_access, started);
    proof.deadline = started + (proof.expires_at - proof.server_time) * 1000;
    return proof;
  }

  function failureMessage(failure) {
    return ['NotAllowedError', 'AbortError'].includes(failure.name)
      ? 'Action cancelled or passkey timed out. Nothing changed.' : failure.message;
  }

  function failureResponse(message, cancelled = false) {
    // Keep the Response contract for existing JSON callers, including their !ok branch.
    return new Response(JSON.stringify({error: message, admin_cancelled: cancelled}), {
      status: 409, headers: {'Content-Type': 'application/json'},
    });
  }

  async function pending(response) {
    if (response.status !== 428 || !response.headers.get('Content-Type')?.includes('application/json')) return null;
    return (await response.clone().json()).admin_verification || null;
  }

  function openGate(requestSignal) {
    const previousFocus = document.activeElement;
    const controller = new AbortController();
    const dialog = document.createElement('dialog');
    dialog.className = 'modal';
    dialog.setAttribute('aria-labelledby', 'admin-review-title');
    dialog.setAttribute('aria-describedby', 'admin-review-description');
    dialog.innerHTML = `<div class="modal-box max-w-lg border border-base-300">
      <h2 id="admin-review-title" class="text-lg font-semibold" tabindex="-1">Verify with passkey</h2>
      <p id="admin-review-description" class="text-sm text-base-content/70 mt-2">Authenticate first. You will review the impact and confirm before anything changes.</p>
      <div data-review-detail hidden class="mt-4 space-y-3">
        <p data-review-target class="text-sm font-medium break-words"></p>
        <p data-review-impact class="text-sm text-base-content/80"></p>
        <p data-review-request class="text-xs font-mono text-base-content/70 break-all"></p>
        <div data-review-typing hidden>
          <label for="admin-review-typed" data-review-type-label class="block text-sm mb-2"></label>
          <input id="admin-review-typed" class="input input-bordered input-sm w-full" autocomplete="off" spellcheck="false">
        </div>
      </div>
      <p data-review-status class="text-sm text-base-content/70 mt-4" role="status">Waiting for your passkey…</p>
      <div class="modal-action">
        <button type="button" class="btn btn-ghost btn-sm" data-review-cancel>Cancel</button>
        <button type="button" class="btn btn-primary btn-sm" data-review-confirm hidden>Confirm action</button>
      </div>
    </div>`;
    document.body.append(dialog);
    const find = selector => dialog.querySelector(selector);
    let resolveDecision = null;
    let interval = null;
    let cancelled = false;
    let committing = false;
    const cancel = () => {
      if (committing) return;
      cancelled = true;
      controller.abort();
      if (resolveDecision) resolveDecision('cancel');
    };
    requestSignal?.addEventListener('abort', cancel, {once: true});
    if (requestSignal?.aborted) cancel();
    dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
    find('[data-review-cancel]').addEventListener('click', cancel);
    dialog.showModal();
    find('[data-review-cancel]').focus();
    return {
      signal: controller.signal,
      get cancelled() { return cancelled; },
      authenticating(entry = false) {
        find('#admin-review-title').textContent = entry ? 'Renew admin access' : 'Verify with passkey';
        find('#admin-review-description').textContent = entry
          ? 'Your access ended. Verify again without leaving this page. Your unsaved changes and selected files stay here.'
          : 'Authenticate first. You will review the impact and confirm before anything changes.';
        find('[data-review-detail]').hidden = true;
        find('[data-review-confirm]').hidden = true;
        find('[data-review-status]').textContent = 'Waiting for your passkey…';
      },
      review(context, proof) {
        if (cancelled) return Promise.resolve('cancel');
        const summary = context.summary || {};
        const confirm = find('[data-review-confirm]');
        const input = find('#admin-review-typed');
        find('#admin-review-title').textContent = summary.title || 'Review admin action';
        find('#admin-review-description').textContent = 'Passkey verified. Review the impact below; nothing has changed yet.';
        find('[data-review-target]').textContent = summary.target || context.target;
        find('[data-review-impact]').textContent = summary.consequence || 'Submit this exact request using admin access.';
        find('[data-review-request]').textContent = context.request_label || '';
        find('[data-review-detail]').hidden = false;
        find('[data-review-typing]').hidden = !summary.confirm_text;
        find('[data-review-type-label]').textContent = `Type ${summary.confirm_text || ''} to confirm`;
        input.value = '';
        confirm.hidden = false;
        const tick = () => {
          const expired = Date.now() >= proof.deadline || (band && Date.now() >= accessDeadline);
          confirm.textContent = expired ? 'Verify again' : summary.confirm_label || 'Confirm action';
          confirm.disabled = !expired && !!summary.confirm_text && input.value !== summary.confirm_text;
          find('[data-review-status]').textContent = expired
            ? 'Approval expired. Verify again, then review this action before confirming.'
            : 'This approval is used once, for exactly this request.';
        };
        input.oninput = tick;
        tick();
        interval = setInterval(tick, 1000);
        find('#admin-review-title').focus();
        return new Promise(resolve => {
          resolveDecision = resolve;
          confirm.onclick = () => {
            const expired = Date.now() >= proof.deadline || (band && Date.now() >= accessDeadline);
            if (!expired && summary.confirm_text && input.value !== summary.confirm_text) return;
            clearInterval(interval);
            confirm.disabled = true;
            resolve(expired ? 'renew' : 'confirm');
          };
        });
      },
      submitting() {
        committing = true;
        clearInterval(interval);
        find('[data-review-confirm]').disabled = true;
        find('[data-review-cancel]').disabled = true;
        find('[data-review-status]').textContent = 'Applying confirmed change…';
      },
      close() {
        clearInterval(interval);
        requestSignal?.removeEventListener('abort', cancel);
        dialog.close();
        dialog.remove();
        if (previousFocus?.isConnected) previousFocus.focus();
      },
    };
  }

  function attempt(original, token, signal) {
    const request = original.clone();
    request.headers.set('X-Admin-Fetch', '1');
    request.headers.delete('X-Admin-Action');
    if (token) request.headers.set('X-Admin-Action', token);
    return nativeFetch(request, {signal: signal || original.signal});
  }

  async function runAction(original) {
    let ownsGate = false;
    let gate = null;
    try {
      let response = await attempt(original);
      let context = await pending(response);
      if (!context) return response;
      if (busy) return failureResponse('Finish or cancel the current admin action first.');
      busy = true;
      ownsGate = true;
      if (band) await refreshStatus();
      gate = openGate(original.signal);
      while (context && !gate.cancelled) {
        gate.authenticating(context.kind === 'entry');
        const proof = await assertion(context, gate.signal);
        if (gate.cancelled) break;
        if (context.kind === 'entry') {
          // Entry verification grants access only. Prepare a NEW action-bound proof.
          response = await attempt(original, null, gate.signal);
          context = await pending(response);
          if (!context) return response;
          if (context.kind === 'entry') throw new Error('Admin access could not be renewed. Nothing changed. Try again.');
          continue;
        }
        const decision = await gate.review(context, proof);
        if (decision === 'cancel') break;
        const state = await refreshStatus();
        if (gate.cancelled) break;
        if (decision === 'renew' || !state.active || Date.now() >= proof.deadline) {
          response = await attempt(original, null, gate.signal);
          context = await pending(response);
          if (!context) return response;
          continue;
        }
        gate.submitting();
        // The only mutation replay: a distinct, explicit post-authentication click.
        return await attempt(original, proof.action_token);
      }
      return failureResponse('Action cancelled. Nothing changed.', true);
    } catch (failure) {
      return failureResponse(failureMessage(failure), ['AbortError', 'NotAllowedError'].includes(failure.name));
    } finally {
      gate?.close();
      if (ownsGate) busy = false;
    }
  }

  // Existing fetch callers keep a Response; server 428 intent alone invokes the gate.
  window.fetch = async (input, init) => {
    const original = new Request(input, init);
    const url = new URL(original.url);
    if (url.origin !== location.origin || url.pathname.startsWith('/admin/passkey/')
        || ['GET', 'HEAD', 'OPTIONS'].includes(original.method)) return nativeFetch(original);
    return runAction(original);
  };

  async function navigateResult(response, url) {
    if (!response.ok) {
      if (response.headers.get('Content-Type')?.includes('application/json')) {
        const detail = await response.json();
        throw new Error(detail.error || 'The action was rejected. Your unsaved changes are still here.');
      }
      throw new Error(`The request was rejected (${response.status}). Your unsaved changes are still here. Check the form and try again.`);
    }
    if (response.redirected) { location.assign(response.url); return; }
    if (response.headers.get('Content-Type')?.includes('application/json')) {
      const body = await response.json();
      if (body.admin_redirect) { location.assign(body.admin_redirect); return; }
      throw new Error('The server did not return a destination. Keep this page open and check the action status before retrying.');
    }
    if (response.headers.get('Content-Type')?.includes('text/html')) {
      const html = await response.text();
      history.replaceState(null, '', url);
      // document.write retains Window properties. The next page must install a
      // fresh fetch wrapper, form listeners and countdown against its own DOM.
      cleanup.forEach(dispose => dispose());
      window.fetch = nativeFetch;
      window.cabotageAdminPasskey = false;
      document.open(); document.write(html); document.close();
    }
  }

  function showError(container, message) {
    let error = container.querySelector('[data-admin-passkey-error]');
    if (!error) {
      error = document.createElement('p');
      error.className = 'adm-error';
      error.dataset.adminPasskeyError = '';
      error.setAttribute('role', 'alert');
      container.append(error);
    }
    error.textContent = message;
    error.hidden = false;
  }

  const submittingForms = new WeakSet();
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!band || event.defaultPrevented || !(form instanceof HTMLFormElement)
        || form.matches('[data-admin-access-end]')) return;
    const submitter = event.submitter;
    const method = (submitter?.getAttribute('formmethod') || form.method || 'get').toUpperCase();
    const url = new URL(submitter?.getAttribute('formaction') || form.action, location.href);
    if (method === 'GET' || method === 'DIALOG' || url.origin !== location.origin
        || url.pathname.startsWith('/admin/passkey/')) return;
    event.preventDefault();
    if (submittingForms.has(form)) return;
    submittingForms.add(form);
    const wasDisabled = submitter?.disabled;
    try {
      // Snapshot after the form's own submit handlers. File objects never leave memory
      // except in the original server request; no draft is copied into browser storage.
      const data = new FormData(form, submitter);
      const enctype = submitter?.getAttribute('formenctype') || form.enctype;
      let body = data;
      if (enctype !== 'multipart/form-data') {
        const fields = Array.from(data, ([key, value]) => [key, typeof value === 'string' ? value : value.name]);
        body = enctype === 'text/plain'
          ? fields.map(([key, value]) => `${key}=${value}\r\n`).join('') : new URLSearchParams(fields);
      }
      if (submitter) submitter.disabled = true;
      const error = form.querySelector('[data-admin-passkey-error]');
      if (error) error.hidden = true;
      const original = new Request(url, {
        method, body, credentials: 'same-origin', headers: {'X-Admin-Navigate': '1', 'X-CSRFToken': csrf()},
      });
      await navigateResult(await runAction(original), url);
    } catch (failure) {
      showError(form, failureMessage(failure));
    } finally {
      submittingForms.delete(form);
      if (submitter) submitter.disabled = wasDisabled;
    }
  });

  if (band) {
    updateBand({active: true, expires_at: Number(band.dataset.expiresAt), server_time: Number(band.dataset.serverTime)});
    const countdown = setInterval(renderBand, 1000); // Local only; no database polling.
    const onFocus = () => {
      refreshStatus().catch(() => { band.querySelector('[data-admin-access-countdown]').textContent = 'Access status unavailable'; });
    };
    window.addEventListener('focus', onFocus);
    cleanup.push(() => clearInterval(countdown), () => window.removeEventListener('focus', onFocus));
    band.querySelector('[data-admin-access-renew]').addEventListener('click', async event => {
      // Pages without the review dialog (Flask-Admin) renew through the full-page gate.
      if (busy || event.currentTarget.matches('a[href]')) return;
      busy = true;
      const gate = openGate();
      gate.authenticating(true);
      try { await assertion(entryContext, gate.signal); }
      catch (failure) { showError(band, failureMessage(failure)); }
      finally { gate.close(); busy = false; }
    });
    band.querySelector('[data-admin-access-end]').addEventListener('submit', async event => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      try {
        const result = await jsonPost(event.currentTarget.action, {}, undefined);
        updateBand(result.admin_access);
      } catch (failure) { showError(band, failureMessage(failure)); }
      finally { busy = false; }
    });
  }

  const data = document.getElementById('admin-passkey-data');
  const button = document.querySelector('[data-admin-passkey-verify]');
  if (!data || !button) return;
  button.addEventListener('click', async () => {
    if (busy) return;
    button.disabled = true;
    const context = JSON.parse(data.textContent);
    const container = button.closest('.adm-gate');
    const error = container.querySelector('[data-admin-passkey-error]');
    if (error) error.hidden = true;
    try {
      if (context.kind === 'entry') {
        busy = true;
        await assertion(context);
        location.assign(context.return_url);
        return;
      }
      if (!context.replay) throw new Error('Return to the original form and try the action again. Nothing changed.');
      const headers = {'X-CSRFToken': csrf(), 'X-Admin-Navigate': '1'};
      if (context.replay.content_type) headers['Content-Type'] = context.replay.content_type;
      const original = new Request(new URL(context.replay.url, location.origin), {
        method: context.replay.method, body: context.replay.body || undefined, headers, credentials: 'same-origin',
      });
      // OAuth callbacks and shell GETs stage a POST that must mint its own intent.
      const response = await runAction(original);
      await navigateResult(response, context.replay.url);
    } catch (failure) {
      showError(container, failureMessage(failure));
    } finally {
      busy = false;
      button.disabled = false;
    }
  });
})();
