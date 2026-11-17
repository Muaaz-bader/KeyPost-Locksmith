"""Delete Marketplace listings from the embedded Manage Listings page.

The user picks listings on Facebook's own "Your listings" page (selection
added by PAGE_JS). A background job then deletes them the way a person would:
More options -> Delete listing -> Delete (closing the optional "Did you sell
this item?" popup), using real mouse input sent through DevTools. Each run is
recorded for the Delete Results page.

Repost: for each picked listing, open its edit page (More options -> Edit
listing), read title, price, description, location and photos, post a new copy
with the regular item posting code, then delete the old listing once Facebook
confirms the new one. Runs are recorded on the Reposting page.
"""
import base64
import copy
import json
import os
import random
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import requests
from PIL import Image

from automation import account_browser, cdp_login
from automation.diagnostics import event as diagnostic_event
from automation.runtime_paths import data_dir, screenshots_dir

MAX_PER_RUN = 50
POLL_SECONDS = 0.1   # how often a step checks whether Facebook is ready for the next click

_lock = threading.Lock()
_stop = threading.Event()
_queue = []
_job = {'running': False, 'kind': 'delete', 'phase': '', 'total': 0, 'done': 0, 'deleted': 0, 'failed': [],
        'current': '', 'step': '', 'next_at': 0, 'message': '', 'finished_at': 0}
REPOST_PHOTO_LIMIT = 5
# Current repost run per account for the Reposting page.
_repost_runs = {}
# Set by the web layer: on_repost_result(account, title, status, error, timing) adds a history row.
on_repost_result = None
# Last run per account, in the shape the Delete Results page shows.
_results = {}
# Set once Facebook's final "Delete" was clicked for the listing being deleted now.
_confirm_clicked = threading.Event()


class _Stopped(Exception):
    """Stop was pressed during a delete run: abandon the current step at once."""


def _check_stop():
    # Deletes stop on the spot. Reposts finish the current listing so a half-made
    # copy or a duplicate is never left behind.
    if _stop.is_set() and _job.get('kind') == 'delete':
        raise _Stopped()


# Runs inside the Facebook page. Adds the selection UI to each listing card and
# exposes window.__kp for the job. Finds cards by their "More options for
# <title>" button, never by Facebook's generated class names.
PAGE_JS = r'''
(() => {
  if (window.top !== window || window.__kp) return;
  const PREFIX = 'More options for ';
  const LABELS = {queued: 'Queued', deleting: 'Deleting...', deleted: 'Deleted', failed: 'Could not delete',
                  reposting: 'Reposting...', reposted: 'Reposted', repost_failed: 'Could not repost'};
  const kp = window.__kp = {selected: new Set(), status: new Map(), selecting: false, busy: false};
  const style = document.createElement('style');
  style.textContent = `
    [data-kp-card]{position:relative!important}
    .kp-ui{position:absolute;inset:0;z-index:3;pointer-events:none;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
    .kp-check{position:absolute;top:8px;left:8px;width:30px;height:30px;display:none;place-items:center;border-radius:8px;background:#fff;box-shadow:0 1px 5px rgba(0,0,0,.35)}
    html[data-kp-selecting] .kp-ui{pointer-events:auto;cursor:pointer}
    html[data-kp-selecting] .kp-check{display:grid}
    html[data-kp-selecting] .kp-ui:hover{background:rgba(24,119,242,.06);border-radius:8px}
    .kp-check span{width:18px;height:18px;box-sizing:border-box;border:2px solid #8a8d91;border-radius:5px;background:#fff center/14px no-repeat}
    [data-kp-selected] .kp-check span{border-color:#1877f2;background-color:#1877f2;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 12l5 5 9-10'/%3E%3C/svg%3E")}
    [data-kp-selected]{box-shadow:0 0 0 3px #1877f2!important;border-radius:8px}
    .kp-badge{position:absolute;inset:0;display:none;align-items:center;justify-content:center;gap:8px;border-radius:8px;background:rgba(255,255,255,.78);color:#1c2733;font-size:15px;font-weight:700}
    .kp-ui[data-status]:not([data-status=""]) .kp-badge{display:flex}
    .kp-ui[data-status="failed"] .kp-badge{color:#d32f2f}
    .kp-ui[data-status="deleted"] .kp-badge{color:#2e7d32}
    .kp-ui[data-status="deleting"] .kp-badge::before{content:"";width:16px;height:16px;border:3px solid #c5d3e8;border-top-color:#1877f2;border-radius:50%;animation:kp-spin .8s linear infinite}
    html[data-kp-busy] .kp-ui{cursor:default}
    @keyframes kp-spin{to{transform:rotate(360deg)}}`;
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const box = el => {
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
  };
  const onPage = () => /^\/marketplace\/(you\/)?selling/.test(location.pathname);

  function scan() {
    const main = document.querySelector('[role="main"]');
    if (!main || !onPage()) return [];
    const cards = [];
    for (const button of main.querySelectorAll('[role="button"][aria-label^="' + PREFIX + '"]')) {
      if (!visible(button)) continue;
      const title = button.getAttribute('aria-label').slice(PREFIX.length).trim();
      let row = button;
      while (row.parentElement && row !== main && !row.querySelector('img')) row = row.parentElement;
      if (!row || row === main) continue;
      const lines = row.innerText.split('\n').map(line => line.trim()).filter(Boolean);
      const at = lines.findIndex(line => line === title || title.startsWith(line) || line.startsWith(title));
      const price = at >= 0 ? (lines[at + 1] || '') : '';
      const info = lines.find(line => line.includes('·')) || '';
      // The photo's file name is unique per upload, so look-alike listings
      // (same title, price and date) still get different keys.
      const img = row.querySelector('img');
      const photo = img ? (img.currentSrc || img.src || '').split('?')[0].split('/').pop() : '';
      cards.push({key: [title, price, info, photo].join('\u001f'), title, info, row, button});
    }
    return cards;
  }

  function build() {
    const ui = document.createElement('div');
    ui.className = 'kp-ui';
    ui.innerHTML = '<span class="kp-check"><span></span></span><div class="kp-badge"></div>';
    // While selecting, the whole card is one big checkbox; Facebook never sees the click.
    const swallow = event => { if (kp.selecting) event.stopPropagation(); };
    for (const type of ['pointerdown', 'pointerup', 'mousedown', 'mouseup']) ui.addEventListener(type, swallow, true);
    ui.addEventListener('click', event => {
      if (!kp.selecting) return;
      event.preventDefault();
      event.stopPropagation();
      const key = ui.dataset.key;
      if (kp.busy || !key) return;
      if (kp.selected.has(key)) kp.selected.delete(key); else kp.selected.add(key);
      tick();
    }, true);
    return ui;
  }

  function tick() {
    if (!document.documentElement) return;
    if (!style.isConnected) (document.head || document.documentElement).appendChild(style);
    document.documentElement.toggleAttribute('data-kp-busy', kp.busy);
    document.documentElement.toggleAttribute('data-kp-selecting', kp.selecting && !kp.busy);
    for (const card of scan()) {
      const row = card.row;
      row.setAttribute('data-kp-card', '');
      let ui = [...row.children].find(child => child.classList && child.classList.contains('kp-ui'));
      if (!ui) {
        ui = build();
        row.appendChild(ui);
      }
      const status = kp.status.get(card.key) || '';
      ui.dataset.key = card.key;
      ui.dataset.status = status;
      ui.querySelector('.kp-badge').textContent = LABELS[status] || '';
      row.toggleAttribute('data-kp-selected', kp.selected.has(card.key));
    }
  }

  const find = key => {
    const cards = scan();
    const exact = cards.find(card => card.key === key);
    if (exact) return exact;
    // A photo that had not loaded yet when the key was made: match the rest.
    const [title, price, info, photo] = key.split('\u001f');
    return cards.find(card => {
      const parts = card.key.split('\u001f');
      return parts[0] === title && parts[1] === price && parts[2] === info && (!photo || !parts[3]);
    }) || null;
  };
  const enabled = el => visible(el) && el.getAttribute('aria-disabled') !== 'true' && !el.disabled;
  const escapeRe = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const clickable = el => el.closest('[role="button"], button, [role="menuitem"], [role="radio"], label, a') || el;
  // Controls are found by their visible <span> text first ("Delete", "Next", ...);
  // aria-labels and internal values are only fallbacks.
  const byText = (root, words) => {
    if (!root) return null;
    const re = new RegExp('^(' + words.map(escapeRe).join('|') + ')$', 'i');
    const own = span => re.test(span.textContent.trim())
      && ![...span.querySelectorAll('span')].some(child => re.test(child.textContent.trim()));
    return [...root.querySelectorAll('span')].filter(span => visible(span) && own(span))
      .map(clickable).find(enabled) || null;
  };
  const byLabel = (root, labels) => root
    ? [...root.querySelectorAll(labels.map(l => `[aria-label="${l}"]`).join(','))].find(enabled) || null
    : null;
  const textOrLabel = (root, words) => byText(root, words) || byLabel(root, words);
  const DECLINE = ["I'd rather not answer", 'I’d rather not answer'];
  // Facebook's delete dialogs: the plain "Are you sure?" box (Delete) and the
  // "Did you sell this item?" survey (pick an answer, then Next). Both name the
  // listing, which is how we know a dialog belongs to the listing being deleted.
  const isDeleteDialog = dialog => visible(dialog) && !!(
    byText(dialog, ['Delete listing', 'Delete', 'Next', ...DECLINE])
    || dialog.querySelector('input[name="responseRadioList"], [aria-label="Delete"], [aria-label="Next"]'));
  const deleteDialogs = () => [...document.querySelectorAll('[role="dialog"]')].filter(isDeleteDialog);
  // The delete dialog for this listing. Facebook names the listing in it, except
  // for listings waiting on identity confirmation ("Are you sure...?" only). With
  // untitledOk (no delete dialog was open before our click), the single delete
  // dialog on screen is accepted even without the title.
  const dialogFor = (title, untitledOk) => {
    const needle = String(title).slice(0, 40);
    const dialogs = deleteDialogs();
    const named = dialogs.find(dialog => dialog.innerText.includes(needle));
    if (named) return named;
    if (!untitledOk || dialogs.length !== 1) return null;
    // Never accept a dialog that names a different listing.
    const others = scan().map(card => card.title.slice(0, 40)).filter(t => t.length >= 3 && t !== needle);
    return others.some(t => dialogs[0].innerText.includes(t)) ? null : dialogs[0];
  };
  // Facebook's dialog layer scrolls in a short window: bring the control into view first.
  const aim = el => { el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); return box(el); };
  Object.assign(kp, {
    state() {
      const seen = new Set();
      const cards = [];
      for (const card of scan()) {
        if (seen.has(card.key)) continue;
        seen.add(card.key);
        cards.push({key: card.key, title: card.title, selected: kp.selected.has(card.key), status: kp.status.get(card.key) || ''});
      }
      return {ready: onPage(), selecting: kp.selecting, cards};
    },
    select(key, on) { if (on) kp.selected.add(key); else kp.selected.delete(key); tick(); },
    selectAll(on) { if (on) scan().forEach(card => kp.selected.add(card.key)); else kp.selected.clear(); tick(); },
    setSelecting(on) { kp.selecting = !!on; if (!on) kp.selected.clear(); tick(); },
    setBusy(on) { kp.busy = !!on; tick(); },
    setStatus(key, status) {
      if (status) kp.status.set(key, status); else kp.status.delete(key);
      if (status === 'deleted') kp.selected.delete(key);
      tick();
    },
    keys() { return [...new Set(scan().map(card => card.key))]; },
    // '' when the listing can be reposted: a current listing (Active, Sold or Pending)
    // shown under "All listings". "Needs attention" and repeated (duplicate) cards
    // are hidden by the listings view and never qualify.
    repostable(key) {
      const card = find(key);
      if (!card) return 'missing';
      const main = document.querySelector('[role="main"]');
      const re = /^All listings$/i;
      const head = main && [...main.querySelectorAll('span, h2, h3')]
        .find(el => re.test(el.textContent.trim()) && ![...el.children].some(c => re.test(c.textContent.trim())));
      if (!head || !(head.compareDocumentPosition(card.button) & Node.DOCUMENT_POSITION_FOLLOWING)) return 'not_current';
      if (card.button.closest('[data-kp-hide]')) return 'not_current';
      if (!/^(active|sold|pending)\b/i.test(String(card.info || '').trim())) return 'not_current';
      return '';
    },
    locate(key) {
      const card = find(key);
      if (!card) return false;
      card.button.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
      return true;
    },
    target(key) {
      const card = find(key);
      if (!card) return null;
      const b = box(card.button);
      return b.y > 0 && b.y < innerHeight && b.x > 0 && b.x < innerWidth ? b : null;
    },
    menuItem() {
      const menus = [...document.querySelectorAll('[role="menu"]')].filter(visible);
      let item = null;
      for (const menu of menus) item = item || byText(menu, ['Delete listing', 'Delete']);
      item = item || [...document.querySelectorAll('[role="menuitem"]')].find(el => enabled(el) && /delete/i.test(el.innerText));
      return item ? box(item) : null;
    },
    onList() { return onPage() && !!document.querySelector('[role="main"]'); },
    // A choice in the open "More options" menu, by its visible text.
    menuChoice(words) {
      for (const menu of [...document.querySelectorAll('[role="menu"]')].filter(visible)) {
        const item = byText(menu, words);
        if (item) return box(item);
      }
      return null;
    },
    // What to click next in this listing's delete dialog.
    deleteStep(title, untitledOk) {
      const dialog = dialogFor(title, untitledOk);
      if (!dialog) return {kind: 'none'};
      const named = dialog.innerText.includes(String(title).slice(0, 40));
      // "Did you sell this item?" appears after Facebook has already deleted the
      // listing; answering is optional, so it is simply closed.
      const survey = byText(dialog, DECLINE) || dialog.querySelector('input[name="responseRadioList"], input[type="radio"]');
      if (survey) {
        const close = textOrLabel(dialog, ['Close', 'Not now']);
        return close ? {kind: 'dismiss', named, ...aim(close)} : {kind: 'wait'};
      }
      const del = byText(dialog, ['Delete']);
      if (del) return {kind: 'delete', how: 'text', named, ...aim(del)};
      const delByLabel = byLabel(dialog, ['Delete']);
      if (delByLabel) return {kind: 'delete', how: 'aria-label', named, ...aim(delByLabel)};
      return {kind: 'wait'};
    },
    dialogCount() { return deleteDialogs().length; },
    // Short text of every visible dialog, for the log when a step fails.
    dialogTexts() {
      return [...document.querySelectorAll('[role="dialog"]')].filter(visible)
        .map(d => d.innerText.replace(/\s+/g, ' ').trim().slice(0, 160));
    },
    // Close button of a delete dialog left open by an earlier attempt.
    leftoverClose() {
      for (const dialog of deleteDialogs()) {
        const close = textOrLabel(dialog, ['Cancel', 'Close']);
        if (close) return box(close);
      }
      return null;
    },
    dialogOpen(title) { return !!dialogFor(title); },
    gone(key) { return !find(key); },
    async loadAll() {
      let last = -1, stable = 0;
      for (let i = 0; i < 80 && stable < 3 && !window.__kpStop; i++) {
        window.scrollTo(0, document.scrollingElement.scrollHeight);
        await new Promise(done => setTimeout(done, 1200));
        const count = kp.keys().length;
        stable = count === last ? stable + 1 : 0;
        last = count;
      }
      window.scrollTo(0, 0);
      return last;
    },
  });
  setInterval(tick, 500);
  tick();
})();
'''


def snapshot():
    with _lock:
        job = dict(_job, failed=list(_job['failed']), remaining=len(_queue))
    job['next_in'] = max(0, round(job.pop('next_at') - time.time())) if job['running'] else 0
    return job


def results(account):
    """The account's last Manage Listings delete run for the Delete Results page, or None."""
    with _lock:
        return copy.deepcopy(_results.get(account))


def _record_items(keys, status='pending'):
    """Add listings to the current run's results (call with _lock held)."""
    record = _results.get(_job.get('account'))
    if record is not None:
        for key in keys:
            record['items'].append({'key': key, 'listing_id': key, 'title': _title(key), 'status': status,
                                    'error': '', 'position': len(record['items'])})
        record['total'] = len(record['items'])


def _record(key, status, error=''):
    """Update one listing's row on the Delete Results page."""
    with _lock:
        record = _results.get(_job.get('account'))
        for item in (record or {}).get('items', []):
            if item['key'] == key:
                item.update(status=status, error=error)
        if record is not None:
            record['deleted'] = sum(1 for item in record['items'] if item['status'] == 'deleted')


def page_state():
    session = account_browser.listings_session()
    page = None
    if session:
        try:
            page = session.evaluate('window.__kp ? window.__kp.state() : null', timeout=5)
        except Exception:
            page = None
    return {'page': page, 'job': snapshot()}


def page_action(action, key=None, value=None):
    """Selection changes made from the app toolbar."""
    session = account_browser.listings_session()
    if not session:
        raise RuntimeError('Open Manage Listings first.')
    calls = {
        'select': lambda: f'__kp.select({json.dumps(str(key))}, {json.dumps(bool(value))})',
        'select_all': lambda: f'__kp.selectAll({json.dumps(bool(value))})',
        'selecting': lambda: f'__kp.setSelecting({json.dumps(bool(value))})',
    }
    if action not in calls:
        raise ValueError('Unknown action.')
    session.evaluate(f'window.__kp && {calls[action]()}', timeout=5)


def repost_activity(account):
    """The account's current repost run for the Reposting page: [{title, status, error}]."""
    with _lock:
        run = _repost_runs.get(account) or {}
        return [dict(item) for item in run.get('items', [])] if run.get('running') else []


def start_delete(keys=None, delete_all=False):
    """Queue listings for deletion; starts the job or adds to the running one."""
    return _start('delete', keys, delete_all)


def start_repost(keys=None, repost_all=False):
    """Repost listings: post a new copy first, then delete the old listing."""
    return _start('repost', keys, repost_all)


def _start(kind, keys, load_all):
    session = account_browser.listings_session()
    if not session:
        raise RuntimeError('Open Manage Listings first.')
    keys = [str(k) for k in (keys or []) if str(k).strip()]
    if not keys and not load_all:
        raise ValueError('Select at least one listing.')
    skipped = 0
    if kind == 'repost' and keys:
        current = _repostable(session, keys)
        skipped = len(keys) - len(current)
        keys = current
        if not keys:
            raise ValueError(NOT_CURRENT_MESSAGE)
    skipped_note = f' {skipped} skipped (only Active, Sold or Pending listings can be reposted).' if skipped else ''
    with _lock:
        if _job['running'] and _job['kind'] != kind:
            raise RuntimeError(f"Wait for the current {_job['kind']} to finish.")
        if _job['running']:
            # Listings picked while a run is going join its queue.
            if load_all:
                raise RuntimeError(f'Wait for the current {kind} to finish.')
            room = MAX_PER_RUN - _job['total']
            added = [k for k in dict.fromkeys(keys) if k not in _queue][:max(0, room)]
            if not added:
                raise RuntimeError(f'This run already has {MAX_PER_RUN} listings. Start another run when it finishes.')
            _queue.extend(added)
            _job['total'] += len(added)
            _note_items(kind, added)
        else:
            added = None
            _stop.clear()
            _queue[:] = list(dict.fromkeys(keys))[:MAX_PER_RUN]
            account = account_browser.status().get('account')
            _job.update(running=True, kind=kind, phase='loading' if load_all else 'working', total=len(_queue),
                        done=0, deleted=0, failed=[], current='', step='', next_at=0, finished_at=0,
                        account=account, message='Loading all your listings...' if load_all else '')
            count = len(_queue)
            if kind == 'delete':
                _results[account] = {'source': 'manage_listings', 'running': True, 'started_at': time.time(),
                                     'finished_at': None, 'total': 0, 'deleted': 0, 'items': [],
                                     'message': 'Deleting listings from Manage Listings...'}
            else:
                _repost_runs[account] = {'running': True, 'started_at': time.time(), 'items': []}
            _note_items(kind, _queue)
    if added:
        _mark(session, added, 'queued')
        return f'Added {len(added)} listing(s) to the {kind} queue.' + skipped_note
    # Chrome must keep running for the whole job, even if the user opens another page.
    account_browser.hold_open(True)
    threading.Thread(target=_run, args=(session, kind, load_all), daemon=True).start()
    verb = 'Deleting' if kind == 'delete' else 'Reposting'
    return (f'{verb} your listings...' if load_all else f'{verb} {count} listing(s)...') + skipped_note


NOT_CURRENT_MESSAGE = ('Only Active, Sold or Pending listings under "All listings" can be reposted. '
                       'Needs attention and duplicate listings are skipped.')


def _repostable(session, keys):
    """The keys that are current listings (see __kp.repostable), in order."""
    try:
        flags = session.evaluate(f'{json.dumps(keys)}.map(k => __kp.repostable(k))', timeout=10) or []
    except Exception:
        return []
    return [key for key, reason in zip(keys, flags) if not reason]


def cancel_queued_repost(key):
    """Take a waiting listing out of the repost queue (Reposting page Cancel)."""
    with _lock:
        run = _repost_runs.get(_job.get('account')) or {}
        if not (_job['running'] and _job['kind'] == 'repost') or key not in _queue:
            item = next((i for i in run.get('items', []) if i['key'] == key), None)
            if item and item['status'] in ('reading', 'deleting', 'posting'):
                raise RuntimeError('This listing is being reposted right now and will finish. '
                                   'Use Stop on Manage Listings to stop after it.')
            raise RuntimeError('This listing is no longer waiting. Refresh the page.')
        _queue.remove(key)
        _job['total'] = max(0, _job['total'] - 1)
        for item in run.get('items', []):
            if item['key'] == key:
                item.update(status='cancelled', error='')
    session = account_browser.listings_session()
    if session:
        _mark(session, [key], '')
    return f'Removed "{_title(key)}" from the repost queue.'


def _note_items(kind, keys):
    """Add listings to the run's results page rows (call with _lock held)."""
    if kind == 'delete':
        _record_items(keys)
        return
    run = _repost_runs.get(_job.get('account'))
    if run is not None:
        for key in keys:
            run['items'].append({'key': key, 'title': _title(key), 'status': 'pending', 'error': ''})


def _repost_step(key, status, error=''):
    """Update a listing's row in the current repost run (reading -> posting -> deleting)."""
    with _lock:
        _job['step'] = status
        for item in (_repost_runs.get(_job.get('account')) or {}).get('items', []):
            if item['key'] == key:
                item.update(status=status, error=error)


def stop():
    with _lock:
        if not _job['running']:
            return 'Nothing is running.'
        _stop.set()
        kind = _job['kind']
        message = 'Stopping...' if kind == 'delete' else 'Stopping after the current listing...'
        _job['message'] = message
    if kind == 'delete':
        # Ends a running "load all listings" scroll in the page right away.
        session = account_browser.listings_session()
        if session:
            try:
                session.evaluate('window.__kpStop = true', timeout=3)
            except Exception:
                pass
    return message


def _mark(session, keys, status):
    for key in keys:
        try:
            session.evaluate(f'__kp.setStatus({json.dumps(key)}, {json.dumps(status)})', timeout=5)
        except Exception:
            pass


def _run(session, kind, delete_all):
    account_browser.set_input_blocked(True)
    driver_box = {}
    started = time.time()
    try:
        session.evaluate('__kp.setBusy(true)')
        if delete_all:
            session.evaluate('__kp.loadAll()', timeout=150)
            if _stop.is_set():
                raise _Stopped()
            keys = session.evaluate('__kp.keys()') or []
            if kind == 'repost':
                keys = _repostable(session, keys)
            with _lock:
                _queue[:] = keys[:MAX_PER_RUN]
                _job.update(phase='working', total=len(_queue), message='')
                _note_items(kind, _queue)
                if not _queue:
                    _job['message'] = 'There are no listings.'
        with _lock:
            queued = list(_queue)
        _mark(session, queued, 'queued')
        while not _stop.is_set():
            with _lock:
                if not _queue:
                    break
                key = _queue.pop(0)
                _job['current'] = _title(key)
            item_started = time.time()
            if kind == 'delete':
                _mark(session, [key], 'deleting')
                _record(key, 'searching')
                _confirm_clicked.clear()
                try:
                    error = _delete_with_retry(session, key)
                except _Stopped:
                    if _confirm_clicked.is_set():
                        _record(key, 'stopped', 'Stopped right after Facebook was told to delete it. '
                                                'Check Your listings to see if it is gone.')
                    else:
                        _record(key, 'stopped', 'Not deleted: you stopped the run.')
                    with _lock:
                        _queue.insert(0, key)  # counted with the listings that were not deleted
                    _log('STOPPED', title=_title(key), confirm_clicked=_confirm_clicked.is_set())
                    break
            else:
                _mark(session, [key], 'reposting')
                try:
                    error = _repost_one(session, driver_box, key)
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    _rlog('REPOST_FAILED', title=_title(key), error=error)
            if session.closed:
                with _lock:
                    _job['message'] = 'Stopped because the listings page was closed.'
                break
            if kind == 'delete':
                _mark(session, [key], 'failed' if error else 'deleted')
                _record(key, 'failed' if error else 'deleted', error)
            else:
                _repost_step(key, 'failed' if error else 'reposted', error)
                _report_repost(key, error, item_started)
            with _lock:
                _job['done'] += 1
                if error:
                    _job['failed'].append({'key': key, 'title': _title(key), 'error': error})
                else:
                    _job['deleted'] += 1
    except _Stopped:
        _log('STOPPED', kind=kind, note='while loading listings')
    except Exception as exc:
        _log('RUN_FAILED', kind=kind, error=str(exc))
        with _lock:
            _job['message'] = f'{kind.title()} stopped: {exc}'
    finally:
        if driver_box.get('driver'):
            cdp_login._detach(driver_box['driver'])  # leaves Chrome running
        with _lock:
            left = list(_queue)
            _queue.clear()
        if not session.closed:
            # Reloading also clears our badges and the busy state.
            _finish_page(session)
        account_browser.set_input_blocked(False)
        with _lock:
            stopped = _stop.is_set() and left
            if not _job['message'] or _job['message'].startswith('Stopping'):
                failed = len(_job['failed'])
                done_word = 'deleted' if kind == 'delete' else 'reposted'
                if stopped and kind == 'delete':
                    _job['message'] = (f"Stopped. {_job['deleted']} deleted" + (f', {failed} failed' if failed else '')
                                       + f', {len(left)} not deleted.')
                else:
                    _job['message'] = (f"{_job['deleted']} {done_word}" + (f', {failed} failed' if failed else '')
                                       + (f', {len(left)} not started (stopped)' if stopped else '') + '.')
            _job.update(running=False, phase='', current='', next_at=0, finished_at=time.time())
            run = _repost_runs.get(_job.get('account'))
            if kind == 'repost' and run is not None:
                run['running'] = False
            record = _results.get(_job.get('account')) if kind == 'delete' else None
            if record is not None:
                for item in record['items']:
                    if item['status'] in ('pending', 'searching'):
                        item.update(status='stopped', error='Not deleted: the run was stopped.')
                record.update(running=False, finished_at=_job['finished_at'], message=_job['message'])
        # Closes Chrome now if the user is no longer on Manage Listings.
        account_browser.hold_open(False)


def _title(key):
    return key.split('\x1f', 1)[0]


def _wait_for(session, expression, timeout):
    """Return the expression's value as soon as it is truthy (checked every POLL_SECONDS)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_stop()
        value = session.evaluate(expression, timeout=5)
        if value:
            return value
        time.sleep(POLL_SECONDS)
    return None


def _click(session, target):
    """A real left click inside the element, sent as browser input (isTrusted on the page)."""
    x = target['x'] + random.uniform(-0.2, 0.2) * min(target['w'], 40)
    y = target['y'] + random.uniform(-0.2, 0.2) * min(target['h'], 20)
    for kind, button in (('mouseMoved', 'none'), ('mousePressed', 'left'), ('mouseReleased', 'left')):
        session.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': button, 'clickCount': 1})


def _escape(session):
    for kind in ('keyDown', 'keyUp'):
        session.call('Input.dispatchKeyEvent', {'type': kind, 'key': 'Escape', 'code': 'Escape',
                                                'windowsVirtualKeyCode': 27})


def _log(status, **details):
    diagnostic_event('MANAGE_LISTINGS_DELETE', status, **details)


def _rlog(status, **details):
    diagnostic_event('MANAGE_LISTINGS_REPOST', status, **details)


def _screenshot(session, step):
    """Save what the page looked like when a step failed; returns the file path."""
    try:
        data = session.call('Page.captureScreenshot', timeout=10)['data']
        path = os.path.join(screenshots_dir(), f"manage_delete_{time.strftime('%Y%m%d_%H%M%S')}_{step}.png")
        with open(path, 'wb') as stream:
            stream.write(base64.b64decode(data))
        return path
    except Exception as exc:
        return f'screenshot failed: {exc}'


def _close_leftovers(session):
    """Close delete dialogs left open by an earlier attempt (the survey ignores Escape)."""
    for _ in range(3):
        close = session.evaluate('__kp.leftoverClose()')
        if not close:
            return
        _click(session, close)
        _wait_for(session, '!__kp.leftoverClose()', 3)


def _ensure_list(session, reload=False, current=None):
    """Make sure the page is "Your listings" with its cards loaded; restore our badges after a reload."""
    if not reload and session.evaluate('!!(window.__kp && __kp.onList())'):
        return
    _log('RELOAD_LIST', reason='forced' if reload else 'left the listings page')
    # Mark the old document so the wait below only passes on the newly loaded page.
    session.evaluate('window.__kpOld = true')
    session.call('Page.navigate', {'url': account_browser.MANAGE_LISTINGS_URL})
    _wait_for(session, '!!(!window.__kpOld && window.__kp && __kp.onList() && __kp.keys().length)', 20)
    session.evaluate('__kp.setBusy(true)')
    with _lock:
        queued = list(_queue)
    _mark(session, queued, 'queued')
    if current:
        _mark(session, [current], 'deleting')


def _delete_one(session, key):
    """One attempt at deleting a listing. Returns an error message, or '' when it is gone."""
    key_js = json.dumps(key)
    title = _title(key)
    title_js = json.dumps(title)
    _ensure_list(session, current=key)
    _close_leftovers(session)
    if not session.evaluate(f'__kp.locate({key_js})'):
        return 'The listing is not on the page.'
    target = _wait_for(session, f'__kp.target({key_js})', 2)
    if not target:
        return 'Could not reach the listing on the page.'
    _click(session, target)
    item = _wait_for(session, '__kp.menuItem()', 6)
    if not item:
        _escape(session)
        return 'The listing menu did not open.'
    # If no delete dialog is open before this click, the one that opens next is
    # ours even when Facebook leaves the title out of it.
    untitled_ok = json.dumps(bool(session.evaluate('__kp.dialogCount() === 0')))
    _click(session, item)
    _log('MENU_DELETE_CLICKED', title=title)

    # Follow Facebook's delete dialogs until they close: "Are you sure?" -> Delete,
    # then the optional "Did you sell this item?" popup, which is just closed.
    acted, last, clicked_at = [], None, 0
    deadline = time.time() + 40
    finished = False
    while time.time() < deadline:
        _check_stop()
        step = session.evaluate(f'__kp.deleteStep({title_js}, {untitled_ok})') or {'kind': 'none'}
        kind = step.get('kind')
        if kind == 'none' and acted:
            finished = True
            break
        if kind in ('delete', 'dismiss'):
            spot = (kind, round(step['x']), round(step['y']))
            # Click a new control at once; click the same one again only if it is
            # still there after 1.5s (never a quick double click on Delete).
            if spot != last or time.time() - clicked_at > 1.5:
                _click(session, step)
                acted.append(kind)
                if kind == 'delete':
                    _confirm_clicked.set()
                last, clicked_at = spot, time.time()
                _log('DIALOG_STEP', title=title, step=kind, how=step.get('how', 'text'),
                     titled=step.get('named'))
        time.sleep(POLL_SECONDS)
    if not finished:
        _log('DIALOGS_ON_SCREEN', title=title, dialogs=session.evaluate('__kp.dialogTexts()'))
        if not acted:
            return 'Facebook did not show the delete dialog.'
        return f"Facebook's delete dialog did not finish (after: {', '.join(acted)})."

    # Confirm it is really gone: the card disappears, and stays gone on the list.
    if not _wait_for(session, f'__kp.onList() ? __kp.gone({key_js}) : true', 15):
        return 'Facebook still shows the listing after deleting.'
    time.sleep(0.3)  # one short re-check, in case the list was only re-rendering
    _ensure_list(session, current=key)
    if not session.evaluate(f'__kp.gone({key_js})'):
        return 'Facebook still shows the listing after deleting.'
    return ''


def _delete_with_retry(session, key):
    title = _title(key)
    key_js = json.dumps(key)
    error = ''
    for attempt in (1, 2):
        _check_stop()
        _log('START', title=title, attempt=attempt)
        try:
            error = _delete_one(session, key)
        except _Stopped:
            raise
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        if not error:
            _log('DELETED', title=title, attempt=attempt)
            return ''
        if session.closed:
            return error
        _log('FAILED', title=title, attempt=attempt, error=error, screenshot=_screenshot(session, f'attempt{attempt}'))
        # Start the next attempt from a clean, freshly loaded list.
        try:
            _escape(session)
            _close_leftovers(session)
            _ensure_list(session, reload=True, current=key)
            if session.evaluate(f'__kp.gone({key_js})'):
                session.evaluate('__kp.loadAll()', timeout=150)
                _check_stop()
                if session.evaluate(f'__kp.gone({key_js})') and 'still shows' not in error:
                    # Not on the list even after loading everything: Facebook deleted it.
                    _log('DELETED', title=title, attempt=attempt, note='gone after reload')
                    return ''
        except _Stopped:
            raise
        except Exception as exc:
            _log('CLEANUP_FAILED', title=title, error=str(exc))
    return error


def _finish_page(session):
    """After a run: close any leftover dialog and show a fresh "Your listings" page."""
    try:
        _close_leftovers(session)
    except Exception:
        pass  # the reload below drops any dialog that is still open
    try:
        session.call('Page.navigate', {'url': account_browser.MANAGE_LISTINGS_URL})
    except Exception as exc:
        _log('FINISH_FAILED', error=str(exc))


# ------------------------------------------------------------------ repost
def _report_repost(key, error, started):
    """Add the finished listing to the Reposting page history."""
    if on_repost_result is None:
        return
    try:
        on_repost_result(_job.get('account'), _title(key), 'failed' if error else 'reposted', error,
                         {'started_at': started, 'finished_at': time.time()})
    except Exception as exc:
        _rlog('REPOST_HISTORY_FAILED', error=str(exc))


def _attached_driver(driver_box):
    """One chromedriver connection to the Manage Listings Chrome for the whole run."""
    if not driver_box.get('driver'):
        meta = cdp_login.get_cdp_account(_job.get('account'))
        driver_box['driver'] = cdp_login._attach(meta['port'])
    return driver_box['driver']


def _open_edit_page(session, key):
    """More options -> Edit listing on the card; returns the edit page URL."""
    key_js = json.dumps(key)
    _ensure_list(session, current=key)
    _close_leftovers(session)
    if not session.evaluate(f'__kp.locate({key_js})'):
        raise RuntimeError('The listing is not on the page.')
    target = _wait_for(session, f'__kp.target({key_js})', 2)
    if not target:
        raise RuntimeError('Could not reach the listing on the page.')
    _click(session, target)
    item = _wait_for(session, '__kp.menuChoice(["Edit listing"])', 6)
    if not item:
        _escape(session)
        raise RuntimeError('The listing menu has no "Edit listing".')
    _click(session, item)
    url = _wait_for(session, "location.href.includes('/marketplace/edit') ? location.href : ''", 20)
    if not url:
        raise RuntimeError('The edit page did not open.')
    return url


def _full_size_photos(driver, listing_id, urls):
    """The edit form shows small thumbnails; take the same photos at full size from the listing page."""
    if not listing_id or not urls:
        return urls
    wanted = {urlsplit(url).path.rsplit('/', 1)[-1] for url in urls}
    try:
        driver.get(f'https://www.facebook.com/marketplace/item/{listing_id}/')
        deadline = time.time() + 15
        images = []
        while time.time() < deadline:
            images = [image for image in (driver.execute_script(
                "return [...document.querySelectorAll('img')].map(i => ({url: i.currentSrc || i.src || '', "
                "w: i.naturalWidth, h: i.naturalHeight}));") or [])
                if urlsplit(image.get('url') or '').path.rsplit('/', 1)[-1] in wanted]
            if images:
                break
            time.sleep(0.3)
        best = {}
        for image in images:
            name = urlsplit(image['url']).path.rsplit('/', 1)[-1]
            score = int(image.get('w') or 0) * int(image.get('h') or 0)
            if name not in best or score > best[name][0]:
                best[name] = (score, image['url'])
        return [best.get(urlsplit(url).path.rsplit('/', 1)[-1], (0, url))[1] for url in urls]
    except Exception as exc:
        _rlog('REPOST_PHOTOS_FALLBACK', error=str(exc))
        return urls


def _download_photos(urls, folder):
    os.makedirs(folder, exist_ok=True)
    paths = []
    for index, url in enumerate(urls, 1):
        try:
            response = requests.get(url, timeout=(20, 90), headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            path = os.path.join(folder, f'photo_{index:02d}.jpg')
            with open(path, 'wb') as stream:
                stream.write(response.content)
            with Image.open(path) as image:
                image.verify()
            paths.append(path)
        except Exception as exc:
            _rlog('REPOST_PHOTO_FAILED', index=index, error=str(exc))
    return paths


def _clean_price(text):
    value = re.sub(r'[^\d.]', '', str(text or '').replace(',', ''))
    if not value and 'free' in str(text or '').casefold():
        value = '0'
    return value


def _read_for_repost(driver, edit_url):
    """Title, price, description, location and full-size photo URLs from the edit page."""
    from automation.reposting import _safe_image_urls, wait_for_edit_form_ready
    snapshot = wait_for_edit_form_ready(driver, timeout=40)
    value = lambda name: str((snapshot.get(name) or {}).get('value') or '').strip()
    details = {name: value(name) for name in ('title', 'price', 'description', 'location')}
    urls = _safe_image_urls(driver)[:REPOST_PHOTO_LIMIT]
    listing_id = (parse_qs(urlsplit(edit_url).query).get('listing_id') or [''])[0]
    details['photos'] = _full_size_photos(driver, listing_id, urls)
    details['listing_id'] = listing_id
    return details


def _keep_as_template(account, data):
    """Save a listing whose old copy is already deleted to Backup Posts, so it can be posted again."""
    try:
        from automation.manual_backups import save_manual_listing
        payload = dict(data, _manual_record_id='repost_' + uuid4().hex[:16], _listing_kind='service')
        payload.pop('_require_publish_confirmation', None)
        save_manual_listing(account, payload)
        return True
    except Exception as exc:
        _rlog('REPOST_BACKUP_FAILED', title=data.get('title'), error=str(exc))
        return False


def _repost_one(session, driver_box, key):
    """Repost one listing: read it, delete the old listing, then post the new copy.

    Returns an error message, or '' when the old listing is deleted and the new copy is up.
    Everything is read and the photos downloaded before anything is deleted.
    """
    title = _title(key)
    account = _job.get('account')
    _rlog('REPOST_START', title=title)
    _repost_step(key, 'reading')
    _ensure_list(session, current=key)
    if session.evaluate(f'__kp.repostable({json.dumps(key)})'):
        return NOT_CURRENT_MESSAGE + ' Nothing was deleted or posted.'
    edit_url = _open_edit_page(session, key)
    driver = _attached_driver(driver_box)
    details = _read_for_repost(driver, edit_url)
    missing = [name for name in ('title', 'price', 'description', 'location') if not details[name]]
    if not details['photos']:
        missing.append('photos')
    _rlog('REPOST_READ', title=title, listing_id=details['listing_id'], location=details['location'],
         price=details['price'], photos=len(details['photos']), missing=missing)
    if missing:
        return f"Could not read {', '.join(missing)} from the listing. Nothing was deleted or posted."
    folder = str(data_dir('manage_reposts', time.strftime('%Y%m%d_%H%M%S_') + uuid4().hex[:6]))
    photos = _download_photos(details['photos'], folder)
    if not photos:
        return 'The listing photos could not be downloaded. Nothing was deleted or posted.'
    data = {'title': details['title'], 'price': _clean_price(details['price']),
            'description': details['description'], 'location': details['location'],
            '_require_publish_confirmation': True}
    for index, path in enumerate(photos, 1):
        data[f'image_path{index}'] = path

    _repost_step(key, 'deleting')
    error = _delete_with_retry(session, key)
    if error:
        return f'The old listing could not be deleted, so nothing was posted: {error}'
    _rlog('REPOST_DELETED', title=title)

    _repost_step(key, 'posting')
    from automation.item_posting import post_item_in_tab
    try:
        post_item_in_tab(driver, data, account)
    except Exception as exc:
        _rlog('REPOST_POST_FAILED', title=title, error=str(exc))
        kept = _keep_as_template(account, data)
        return (f'The old listing was deleted, but posting the new copy failed: {exc}'
                + (' The listing was saved in Backup Posts so you can post it again.' if kept else ''))
    _rlog('REPOSTED', title=title, location=data.get('location'))
    return ''
