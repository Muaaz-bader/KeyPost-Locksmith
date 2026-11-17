"""Extract Marketplace listings with the selected account and save them as templates.

Flow: the account's signed-in Chrome (same profile posting uses) -> Marketplace -> type the
keywords into "Search Marketplace" -> collect result links while scrolling ->
open each listing, close the login pop-up, expand "See more", read title, price,
condition, description, "Listed ... in ..." and the large photos -> download the
photos -> save as a manual template (Backup Posts) under the app's active account.
"""
import os
import re
import threading
import time
from urllib.parse import quote
from uuid import uuid4

import requests
from PIL import Image
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation.cdp_login import close_posting_tab, get_cdp_account, open_posting_tab
from automation.runtime_paths import data_dir

MARKETPLACE_URL = 'https://www.facebook.com/marketplace/'
SEARCH_URL = 'https://www.facebook.com/marketplace/search/?query={}'
MAX_LISTINGS = 40
MAX_PHOTOS = 5

_lock = threading.Lock()
_state = {'running': False}
_stop = threading.Event()
_driver_holder = {}


def _log(message):
    """Log without ever failing: listing titles often contain emoji the console can't encode."""
    line = '[ExtractListings] ' + str(message)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
        except Exception:
            pass


def status():
    with _lock:
        return dict(_state, items=list(_state.get('items') or []), errors=list(_state.get('errors') or []))


def _update(**changes):
    with _lock:
        _state.update(changes)


def _listings(count):
    return f'{count} listing' + ('' if count == 1 else 's')


def _stopped_message():
    saved = status().get('saved', 0)
    if not saved:
        return 'Stopped. No listings were saved.'
    return f'Stopped. {_listings(saved)} saved to Backup Posts.'


def _add(key, value):
    with _lock:
        _state.setdefault(key, []).append(value)


def start(account, keywords, count):
    keywords = ' '.join(str(keywords or '').split())
    if not account:
        raise ValueError('Pick an account in the top bar first.')
    meta = get_cdp_account(account)
    if not meta or meta.get('status') != 'ready':
        raise ValueError("This account isn't signed in to Facebook yet. Sign in from Settings → Accounts, then try again.")
    if os.getenv('FB_AUTOMATION_DESKTOP') == '1':
        from automation.account_browser import status as account_browser_status
        if account_browser_status().get('account') == account:
            raise ValueError("This account is open in Account Browser. Close it there, then try again.")
    if not keywords:
        raise ValueError('Type what you want to search for.')
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ValueError('Enter how many listings you want as a whole number, like 3.')
    if not 1 <= count <= MAX_LISTINGS:
        raise ValueError(f'Choose between 1 and {MAX_LISTINGS} listings.')
    with _lock:
        if _state.get('running'):
            raise ValueError('A search is already running. Wait for it to finish or press Stop.')
        _state.clear()
        _state.update(running=True, account=account, keywords=keywords, target=count, found=0, saved=0,
                      stage='Getting ready…', message='', items=[], errors=[], started_at=time.time())
    _stop.clear()
    threading.Thread(target=_run, args=(account, keywords, count), daemon=True, name='extract-listings').start()


def stop():
    with _lock:
        if not _state.get('running'):
            return False
        _state['stage'] = 'Stopping'
    _stop.set()
    driver = _driver_holder.get('driver')
    if driver is not None:
        threading.Thread(target=_close_browser_now, args=(driver,), daemon=True).start()
    return True


def _close_browser_now(driver):
    try:
        driver.execute_cdp_cmd('Browser.close', {})
    except Exception:
        pass


def _check_stop():
    if _stop.is_set():
        raise InterruptedError('Stopped by user.')


def _sleep(seconds):
    if _stop.wait(seconds):
        raise InterruptedError('Stopped by user.')


# ---------------------------------------------------------------- page helpers

_CLOSE_DIALOG_JS = r"""
  const buttons = [...document.querySelectorAll('[role="dialog"] [aria-label="Close"]')]
    .filter(b => b.getClientRects().length);
  buttons.forEach(b => { try { b.click(); } catch (e) {} });
  return buttons.length;
"""

_ITEM_LINKS_JS = r"""
  const seen = new Set(), out = [];
  for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
    const m = a.href.match(/\/marketplace\/item\/(\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    out.push(['https://www.facebook.com/marketplace/item/' + m[1] + '/', (a.innerText || '').trim()]);
  }
  return out;
"""

_SEE_MORE_JS = r"""
  const main = document.querySelector('[role="main"]') || document.body;
  const el = [...main.querySelectorAll('[role="button"], span')]
    .find(e => (e.innerText || '').trim() === 'See more' && e.getClientRects().length);
  if (el) { el.click(); return true; }
  return false;
"""

_READ_ITEM_JS = r"""
  const main = document.querySelector('[role="main"]') || document.body;
  const h1 = main.querySelector('h1');
  if (!h1) return null;
  let col = h1;
  while (col.parentElement && !/Seller information|Details|Location is approximate/.test(col.innerText)) col = col.parentElement;
  const lines = col.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const listed = [...main.querySelectorAll('span')].map(s => (s.innerText || '').trim())
    .find(t => /^Listed\s/i.test(t)) || '';
  return {title: h1.innerText.trim(), lines, listed};
"""

# Photo candidates: visible Facebook-CDN images in the media pane — left of the title column on a
# wide window, or above the title on a narrow window (stacked layout).
# "Product photo of" alt text is preferred, but many listings use other alt text, so fall back to
# any CDN image there; seller avatars and the map sit beside/below the title and are excluded.
_PHOTO_CANDIDATES_JS = r"""
  const main = document.querySelector('[role="main"]') || document.body;
  const h1 = main.querySelector('h1');
  const t = h1 ? h1.getBoundingClientRect() : null;
  const all = [...main.querySelectorAll('img')].filter(i => {
    const r = i.getBoundingClientRect();
    const inMedia = !t || r.right <= t.left + 10 || r.bottom <= t.top + 10;
    return i.getClientRects().length && r.width >= 30 && inMedia
      && /fbcdn|scontent/.test(i.currentSrc || i.src || '');
  });
  const tagged = all.filter(i => /^Product photo of/i.test(i.alt || ''));
  return tagged.length ? tagged : all;
"""

_LARGE_PHOTO_JS = r"""
  const imgs = (function () {""" + _PHOTO_CANDIDATES_JS + r"""})()
    .filter(i => i.getBoundingClientRect().width > 150)
    .sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return rb.width * rb.height - ra.width * ra.height;
    });
  return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : '';
"""

_THUMBNAILS_JS = r"""
  return (function () {""" + _PHOTO_CANDIDATES_JS + r"""})()
    .filter(i => i.getBoundingClientRect().width < 120)
    .map(i => i.closest('[role="button"],[tabindex]') || i);
"""


def _close_dialogs(driver):
    try:
        return driver.execute_script(_CLOSE_DIALOG_JS)
    except Exception:
        return 0


def _search(driver, keywords):
    _update(stage='Searching Marketplace…')
    driver.get(MARKETPLACE_URL)
    _sleep(3)
    _close_dialogs(driver)
    try:
        box = WebDriverWait(driver, 60).until(lambda d: next(
            (el for el in d.find_elements(By.CSS_SELECTOR, 'input[aria-label="Search Marketplace"]')
             if el.is_displayed()), None))
        box.click()
        box.send_keys(Keys.CONTROL, 'a')
        box.send_keys(keywords)
        box.send_keys(Keys.ENTER)
        WebDriverWait(driver, 60).until(lambda d: '/search' in (d.current_url or ''))
        _log('Searched using the Marketplace search field.')
    except Exception as exc:
        _log(f'Search field did not work ({type(exc).__name__}); opening the search URL instead.')
        driver.get(SEARCH_URL.format(quote(keywords)))
    _sleep(4)
    _close_dialogs(driver)


def _collect_links(driver, count):
    _update(stage='Finding listings…')
    links, idle = [], 0
    while len(links) < count and idle < 5:
        _check_stop()
        before = len(links)
        known = {url for url, _ in links}
        for url, text in driver.execute_script(_ITEM_LINKS_JS) or []:
            if url not in known:
                links.append((url, text))
                known.add(url)
        _update(found=min(len(links), count))
        idle = idle + 1 if len(links) == before else 0
        if len(links) >= count:
            break
        driver.execute_script('window.scrollBy(0, Math.max(900, window.innerHeight));')
        _sleep(2)
        _close_dialogs(driver)
    _log(f'Collected {len(links)} listing link(s).')
    return links[:count]


def _parse_price(text):
    text = str(text or '').strip()
    if text.lower() == 'free':
        return '0'
    digits = re.sub(r'[^\d.]', '', text.split('·')[0])
    return digits.rstrip('.') if digits else ''


def _parse_item(raw):
    title = raw['title']
    lines = raw.get('lines') or []
    rest = lines[1:] if lines and lines[0] == title else lines
    price = next((line for line in rest if re.match(r'^(free|[^\w\s]?\s?[\d,.]+|[A-Z]{2,4}\s?[\d,.]+)', line, re.I)), '')
    condition, description = '', []
    if 'Condition' in rest:
        i = rest.index('Condition')
        condition = rest[i + 1] if i + 1 < len(rest) else ''
        tail = rest[i + 2:]
    elif 'Details' in rest:
        tail = rest[rest.index('Details') + 1:]
    else:
        tail = []
    for line in tail:
        if 'Location is approximate' in line or line in {'Seller information', 'Seller details'}:
            break
        description.append(line)
    text = '\n'.join(description).strip()
    text = re.sub(r'\s*See less\s*$', '', text).strip()
    listed = raw.get('listed') or ''
    match = re.search(r'\bin\s+(.+)$', listed)
    return {
        'title': title,
        'price': _parse_price(price),
        'price_text': price,
        'condition': condition,
        'description': text,
        'listed_info': listed,
        'location': match.group(1).strip() if match else '',
    }


def _photo_urls(driver):
    urls = []
    # The main photo can render a moment after the title; wait briefly for it.
    deadline = time.time() + 30
    first = driver.execute_script(_LARGE_PHOTO_JS)
    while not first and time.time() < deadline:
        _sleep(0.25)
        first = driver.execute_script(_LARGE_PHOTO_JS)
    if not first:
        return urls
    urls.append(first)
    thumbs = driver.execute_script(_THUMBNAILS_JS) or []
    if len(thumbs) < 2:
        return urls  # single-photo listing: nothing to click through
    current = first
    for thumb in thumbs[:MAX_PHOTOS + 2]:
        if len(urls) >= MAX_PHOTOS:
            break
        _check_stop()
        try:
            driver.execute_script('arguments[0].click();', thumb)
        except Exception:
            continue
        # Wait only until the large photo changes; the already-shown thumbnail never changes it.
        deadline = time.time() + 4
        while time.time() < deadline:
            src = driver.execute_script(_LARGE_PHOTO_JS)
            if src and src != current:
                current = src
                if src not in urls:
                    urls.append(src)
                break
            _sleep(0.15)
    return urls[:MAX_PHOTOS]


def _download_photos(urls, folder):
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, url in enumerate(urls, 1):
        _check_stop()
        try:
            response = requests.get(url, timeout=(30, 120), headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
            path = folder / f'photo_{index:02d}.jpg'
            path.write_bytes(response.content)
            with Image.open(path) as image:
                image.verify()
            paths.append(str(path))
        except Exception as exc:
            _log(f'Photo {index} could not be downloaded: {exc}')
    return paths


def _read_listing(driver, url):
    driver.get(url)
    WebDriverWait(driver, 90).until(lambda d: d.find_elements(By.CSS_SELECTOR, '[role="main"] h1'))
    _sleep(0.8)
    _close_dialogs(driver)
    if driver.execute_script(_SEE_MORE_JS):
        _sleep(0.5)
    raw = driver.execute_script(_READ_ITEM_JS)
    if not raw:
        raise RuntimeError("the listing didn't open properly")
    item = _parse_item(raw)
    item['photo_urls'] = _photo_urls(driver)
    if not item['photo_urls']:
        _close_dialogs(driver)  # a late login pop-up can cover the photo
        item['photo_urls'] = _photo_urls(driver)
    if not item['photo_urls']:
        match = re.search(r'/item/(\d+)', url)
        shot = data_dir('extracted_listings') / f'no_photos_{match.group(1) if match else "item"}.png'
        try:
            driver.save_screenshot(str(shot))
        except Exception:
            pass
        _log(f'No photo found on {url}; screenshot={shot}')
    item['source_url'] = url
    return item


def _save_template(account, item, run_dir):
    from automation.manual_backups import save_manual_listing
    record_id = 'extract_' + uuid4().hex[:16]
    photos = _download_photos(item['photo_urls'], run_dir / record_id)
    if not photos:
        raise RuntimeError("its photos couldn't be downloaded")
    payload = {
        'title': item['title'], 'price': item['price'], 'description': item['description'],
        'condition': item['condition'], 'location': item['location'], 'listed_info': item['listed_info'],
        'source_url': item['source_url'],
        '_manual_record_id': record_id, '_listing_kind': 'service',
        '_extracted_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    for index, path in enumerate(photos, 1):
        payload[f'image_path{index}'] = path
    save_manual_listing(account, payload)
    from automation.listing_store import load
    saved = next((row for row in load(account).get('listings', []) if row.get('id') == record_id), {})
    names = [photo.get('name') for photo in saved.get('downloaded_images') or [] if photo.get('name')]
    return record_id, names


# ---------------------------------------------------------------- job

def _run(account, keywords, count):
    run_dir = data_dir('extracted_listings') / time.strftime('%Y%m%d_%H%M%S')
    driver = handle = original = account_lock = None
    try:
        _update(stage='Opening Facebook…')
        driver, handle, original, account_lock = open_posting_tab(account)
        _driver_holder['driver'] = driver
        driver.set_page_load_timeout(180)
        _search(driver, keywords)
        links = _collect_links(driver, count)
        # Facebook occasionally renders an empty result page; reload a few times first.
        for retry in range(1, 4):
            if links:
                break
            _check_stop()
            _log(f'No results shown; reloading the search (retry {retry}/3).')
            _update(stage=f'No results yet, trying again ({retry} of 3)…')
            _sleep(3 * retry)
            driver.get(SEARCH_URL.format(quote(keywords)))
            _sleep(4)
            _close_dialogs(driver)
            links = _collect_links(driver, count)
        if not links:
            shot = data_dir('extracted_listings') / 'no_results.png'
            try:
                driver.save_screenshot(str(shot))
            except Exception:
                pass
            _log(f'No listing links. URL={driver.current_url!r} title={driver.title!r} screenshot={shot}')
            raise RuntimeError(f'No listings were found for "{keywords}". Try different words.')
        for position, (url, _) in enumerate(links, 1):
            _check_stop()
            _update(stage=f'Saving listing {position} of {len(links)}…')
            try:
                item = _read_listing(driver, url)
                record_id, photos = _save_template(account, item, run_dir)
                _add('items', {'id': record_id, 'title': item['title'], 'price': item['price_text'],
                               'listed_info': item['listed_info'], 'condition': item['condition'],
                               'description': item['description'], 'photo_names': photos,
                               'photos': len(photos), 'url': url})
                with _lock:
                    _state['saved'] = _state.get('saved', 0) + 1
                _log(f'Saved template {position}/{len(links)}: {item["title"]!r} ({len(photos)} photo(s)).')
            except InterruptedError:
                raise
            except Exception as exc:
                if _stop.is_set():
                    raise InterruptedError('Stopped by user.')
                reason = str(exc) if isinstance(exc, RuntimeError) else "it couldn't be opened"
                _add('errors', f'Listing {position} skipped: {reason}.')
                _log(f'Listing {position} skipped ({url}): {exc}')
        saved = status().get('saved', 0)
        if not saved:
            _update(stage='Failed', message="None of the listings could be saved. Please try again.")
        elif saved == len(links):
            _update(stage='Finished', message=f'Done! {_listings(saved)} saved to Backup Posts.')
        else:
            _update(stage='Finished', message=f'Done. {saved} of {_listings(len(links))} saved to Backup Posts; '
                                              f"{len(links) - saved} couldn't be saved.")
    except InterruptedError:
        _update(stage='Stopped', message=_stopped_message())
    except Exception as exc:
        if _stop.is_set():
            _update(stage='Stopped', message=_stopped_message())
        else:
            _log(f'Extraction failed: {exc}')
            friendly = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('No listings')                 else 'Something went wrong while searching Facebook. Please try again.'
            _update(stage='Failed', message=friendly)
    finally:
        _driver_holder.pop('driver', None)
        if driver is not None:
            # Close the account's whole Chrome (verified, force-closed if needed) and unlock the account.
            try:
                close_posting_tab(driver, handle, original, account_lock,
                                  close_browser=True, verify_browser_closed=True)
            except Exception as exc:
                _log(f'Chrome close problem: {exc}')
                try:
                    account_lock.release()
                except RuntimeError:
                    pass
        # The saved templates live in Backup Posts now; drop the per-run result list.
        _update(running=False, finished_at=time.time(), items=[])
