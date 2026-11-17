(() => {
  // Embeds the active account's Chrome on Marketplace "Your listings" while this page is shown.
  const page = document.querySelector('.manage-listings');
  const surface = document.getElementById('manage-surface');
  const placeholder = document.getElementById('manage-placeholder');
  const title = document.getElementById('manage-title');
  const message = document.getElementById('manage-message');
  const retry = document.getElementById('manage-retry');
  const content = document.querySelector('.content');
  const topbar = document.querySelector('.topbar');
  const account = page.dataset.account;
  // Identifies this page as the owner of the embedded browser (see /account/browser/leave).
  const owner = Date.now().toString(36) + Math.random().toString(36).slice(2);
  let opened = false;
  let leaving = false;
  let layoutPending = false;
  let layoutAgain = false;

  async function post(path, payload, timeoutMs) {
    const response = await appFetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload), timeoutMs: timeoutMs || 15000});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || 'Browser action failed.');
    return data;
  }

  function showMessage(heading, text, canRetry) {
    placeholder.hidden = false;
    title.textContent = heading;
    message.textContent = text;
    retry.hidden = !canRetry;
  }

  async function layout() {
    if (layoutPending) { layoutAgain = true; return; }
    if (content && topbar) content.style.paddingTop = Math.ceil(topbar.getBoundingClientRect().bottom + 6) + 'px';
    if (!opened || leaving || document.hidden) return;
    if (typeof reposting === 'function' && reposting()) return;
    layoutPending = true;
    const box = surface.getBoundingClientRect();
    const rect = {left: box.left + surface.clientLeft, top: box.top + surface.clientTop,
      width: surface.clientWidth, height: surface.clientHeight};
    try {
      if (rect.width < 1 || rect.height < 1 || rect.top < 0 || rect.left < 0
          || rect.top + rect.height > window.innerHeight || rect.left + rect.width > window.innerWidth) {
        await post('/account/browser/hide', {});
        return;
      }
      await post('/account/browser/layout', {x: rect.left, y: rect.top, width: rect.width,
        height: rect.height, viewport_width: window.innerWidth, viewport_height: window.innerHeight});
    } catch (error) { showMessage('Could not show the listings', error.message, true); }
    finally {
      layoutPending = false;
      if (layoutAgain) { layoutAgain = false; requestAnimationFrame(layout); }
    }
  }

  async function open() {
    if (!account) return;
    retry.hidden = true;
    showMessage('Opening your Marketplace listings...', account, false);
    try {
      const state = await post('/manage_listings/open', {account, owner}, 90000);
      if (leaving) return;
      opened = !!state.open;
      if (opened) {
        placeholder.hidden = true;
        layout();
        startPolling();
      } else {
        showMessage('Could not open the listings', 'The account browser did not open.', true);
      }
    } catch (error) {
      showMessage('Could not open the listings', error.message, true);
    }
  }

  // ---------------------------------------------------------------- delete / repost buttons (header)
  // Each button walks through: Delete|Repost listings -> (pick listings) Delete|Repost N ->
  // Confirm ... N -> running (spinner) with a Stop button next to it.
  const actions = document.getElementById('ml-actions');
  const selectAllBox = document.getElementById('ml-select-all');
  const selectAll = document.getElementById('ml-all');
  const stopButton = document.getElementById('ml-stop');
  const MODES = {
    delete: {button: document.getElementById('ml-delete'), label: document.getElementById('ml-delete-label'),
             idle: 'Delete listings', verb: 'Delete', running: 'Deleting', path: '/manage_listings/delete'},
    repost: {button: document.getElementById('ml-repost'), label: document.getElementById('ml-repost-label'),
             idle: 'Repost listings', verb: 'Repost', running: 'Reposting', path: '/manage_listings/repost'},
  };
  const STEP_TEXT = {reading: 'reading the listing', posting: 'posting the new copy', deleting: 'deleting the old listing'};
  const CONFIRM_MS = 5000;
  let listings = null;        // page state from Facebook: {ready, selecting, cards}
  let job = {running: false};
  let mode = null;            // 'delete' | 'repost' while picking listings
  let awaitingSelect = false; // asked Facebook to start picking; its state hasn't caught up yet
  let armed = false;          // the active button is showing "Confirm ... N"
  let armedTimer = null;
  let armedKeys = '';
  let allPicked = false;      // "Select all" was used: act on every listing, not just the loaded ones
  let polling = null;
  let wasReposting = false;

  async function send(path, payload) {
    // Only problems are shown; progress is on the button, results on the results pages.
    try {
      const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload || {})});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'Something went wrong.');
      // Say when some picked listings were left out (e.g. not current listings for a repost).
      if (/skipped/i.test(data.message || '') && window.appNotify) window.appNotify(data.message, 'info');
      return data;
    } catch (error) {
      if (window.appNotify) window.appNotify(error.message, 'failed');
      throw error;
    } finally {
      poll();
    }
  }

  async function poll() {
    if (leaving) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch('/manage_listings/state', {signal: controller.signal});
      const data = await response.json();
      listings = data.page;
      job = data.job || {running: false};
    } catch (_) { /* the next poll retries */ }
    finally { clearTimeout(timer); }
    render();
  }

  function startPolling() {
    if (polling) return;
    poll();
    polling = setInterval(poll, 800);
  }

  function disarm() {
    armed = false;
    clearTimeout(armedTimer);
  }

  function selectedKeys() {
    return ((listings && listings.cards) || []).filter(card => card.selected).map(card => card.key);
  }

  function reposting() { return !!(job.running && job.kind === 'repost'); }

  function render() {
    if (!opened || leaving) return;
    const ready = !!(listings && listings.ready);
    actions.hidden = !ready && !job.running;
    const cards = ready ? listings.cards : [];
    const keys = selectedKeys();
    const selecting = ready && listings.selecting;
    // Don't forget the clicked button while the page is still switching into picking mode.
    if (selecting) awaitingSelect = false;
    else if (!job.running && !awaitingSelect) mode = null;
    if (armed && (keys.join('\n') !== armedKeys || !selecting)) disarm();
    if (keys.length < cards.length) allPicked = false;

    stopButton.hidden = !job.running;
    stopButton.disabled = String(job.message || '').startsWith('Stopping');
    stopButton.textContent = stopButton.disabled ? 'Stopping...' : 'Stop';
    selectAllBox.hidden = !(selecting && !job.running && cards.length);
    selectAll.checked = cards.length > 0 && keys.length === cards.length;
    selectAll.indeterminate = keys.length > 0 && keys.length < cards.length;

    for (const [name, m] of Object.entries(MODES)) {
      let state = 'idle', label = m.idle, disabled = !ready || !cards.length, hidden = false;
      if (job.running) {
        hidden = job.kind !== name;
        state = 'running';
        disabled = true;
        label = job.phase === 'loading' ? 'Loading listings...' : `${m.running} ${Math.min(job.total, job.done + 1)} of ${job.total}`;
      } else if (selecting) {
        hidden = !!mode && mode !== name;
        state = armed ? 'confirm' : 'selecting';
        disabled = false;
        const n = allPicked ? 'all' : String(keys.length);
        label = armed ? `Confirm ${m.verb.toLowerCase()} ${n}` : (keys.length ? `${m.verb} ${n}` : 'Cancel');
      }
      m.button.hidden = hidden;
      m.button.dataset.state = state;
      m.button.disabled = disabled;
      m.label.textContent = label;
    }

    // A repost walks through edit and create pages: keep Facebook out of sight and say what is happening.
    if (reposting()) {
      const step = STEP_TEXT[job.step] ? ` - ${STEP_TEXT[job.step]}` : '';
      showMessage(`Reposting ${Math.min(job.total, job.done + 1)} of ${job.total}${step}`,
        job.message || (job.current ? `“${job.current}”` : ''), false);
      if (!wasReposting) post('/account/browser/hide', {}).catch(() => {});
      wasReposting = true;
    } else if (wasReposting) {
      wasReposting = false;
      placeholder.hidden = true;
      layout();
    }
  }

  function onAction(name) {
    const m = MODES[name];
    const selecting = listings && listings.selecting;
    const keys = selectedKeys();
    if (job.running) return;
    if (!selecting) {
      mode = name;
      allPicked = false;
      awaitingSelect = true;
      send('/manage_listings/page', {action: 'selecting', value: true})
        .catch(() => { awaitingSelect = false; mode = null; render(); });
    } else if (!keys.length) {
      send('/manage_listings/page', {action: 'selecting', value: false}).catch(() => {});
    } else if (!armed) {
      mode = name;
      armed = true;
      armedKeys = keys.join('\n');
      clearTimeout(armedTimer);
      armedTimer = setTimeout(() => { armed = false; render(); }, CONFIRM_MS);
    } else {
      disarm();
      send(m.path, allPicked ? {all: true} : {keys}).catch(() => {});
    }
    render();
  }

  for (const name of Object.keys(MODES)) MODES[name].button.addEventListener('click', () => onAction(name));
  stopButton.addEventListener('click', () => {
    stopButton.disabled = true;
    send('/manage_listings/stop').catch(() => {});
  });
  selectAll.addEventListener('change', () => {
    allPicked = selectAll.checked;
    send('/manage_listings/page', {action: 'select_all', value: selectAll.checked}).catch(() => {});
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && listings && listings.selecting && !job.running) {
      disarm();
      send('/manage_listings/page', {action: 'selecting', value: false}).catch(() => {});
    }
  });

  retry.addEventListener('click', open);
  window.addEventListener('resize', layout);
  document.addEventListener('scroll', layout, true);
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(() => requestAnimationFrame(layout));
    if (topbar) observer.observe(topbar);
    // Header controls appearing or wrapping change the space left for Facebook.
    observer.observe(surface);
  }
  // The embedded browser belongs to this page only: release it as soon as the user goes elsewhere.
  window.addEventListener('pagehide', () => {
    leaving = true;
    if (polling) clearInterval(polling);
    navigator.sendBeacon('/account/browser/leave', new Blob([JSON.stringify({owner})], {type: 'application/json'}));
  });
  layout();
  open();
})();
