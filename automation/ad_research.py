"""Ad Research: find which locksmith listings work on Facebook Marketplace.

One run, no history: search up to 10 keywords (Facebook's "Last 30 days" filter),
optionally in a chosen location, merge listings found by several keywords, open each
unique listing once, then rank by how many keyword searches found it (relevancy stars)
and how old it is ("Listed X ago" on the listing page; no date = 4+ weeks old).

The account's own Marketplace location is noted before the run and put back after it.
Photos are temporary: they are deleted on the next run or Clear, except listings the
user adds to Backup Posts (those are copied into permanent backup storage).
"""
import json
import re
import shutil
import statistics
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation import listing_extractor as extractor
from automation.cdp_login import close_posting_tab, get_cdp_account, open_posting_tab
from automation.runtime_paths import data_dir

SEARCH_URL = 'https://www.facebook.com/marketplace/search/?query={}&daysSinceListed=30'
MAX_KEYWORDS = 10
MAX_PER_KEYWORD = 40
BEST_MIN_APPEARANCES = 3
OCR_PHOTOS_PER_LISTING = 3

_lock = threading.Lock()
_state = {'running': False}
_stop = threading.Event()

PHONE_RE = re.compile(r'(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)')
AGE_RE = re.compile(r'listed\s+(?:about\s+)?(a|an|one|\d+)\s+(minute|min|hour|day|week|month|year)s?\s+ago', re.I)
STOP_WORDS = {
    'a', 'an', 'and', 'the', 'for', 'of', 'to', 'in', 'on', 'at', 'with', 'by', 'or', 'all', 'any', 'your',
    'you', 'we', 'our', 'my', 'is', 'are', 'from', 'near', 'me', 'get', 'call', 'now', 'new', 'more', '&',
    '-', '|', '/', '+',
}
AGE_ORDER = ['new', '1_week', '2_weeks', '3_weeks', 'oldest']
AGE_LABELS = {'new': 'Under 1 week', '1_week': '1 week', '2_weeks': '2 weeks', '3_weeks': '3 weeks',
              'oldest': '4+ weeks (no date shown)', 'shop': 'Shop item / no date', 'unread': "Couldn't read"}


def _log(message):
    line = '[AdResearch] ' + str(message)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- storage

def _root():
    return data_dir('ad_research')


def _results_path():
    return _root() / 'results.json'


def photos_dir():
    return _root() / 'photos'


def load_results():
    try:
        return json.loads(_results_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _save_results(results):
    path = _results_path()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(results, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _wipe():
    """Delete the previous run's results and every temporary photo."""
    shutil.rmtree(photos_dir(), ignore_errors=True)
    _results_path().unlink(missing_ok=True)


def clear():
    with _lock:
        if _state.get('running'):
            raise ValueError('Research is running. Stop it first.')
        _state.clear()
        _state['running'] = False
    _wipe()


# ---------------------------------------------------------------- status / control

def status():
    with _lock:
        return dict(_state)


def _update(**changes):
    with _lock:
        _state.update(changes)


def _check_stop():
    if _stop.is_set():
        raise InterruptedError('Stopped by user.')


def _sleep(seconds):
    if _stop.wait(seconds):
        raise InterruptedError('Stopped by user.')


def _clean_inputs(keywords, location, per_keyword):
    seen, cleaned = set(), []
    for value in keywords or []:
        value = ' '.join(str(value or '').split())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            cleaned.append(value)
    if not cleaned:
        raise ValueError('Type at least one keyword.')
    if len(cleaned) > MAX_KEYWORDS:
        raise ValueError(f'Use up to {MAX_KEYWORDS} keywords.')
    try:
        per_keyword = int(per_keyword)
    except (TypeError, ValueError):
        raise ValueError('Enter listings per keyword as a whole number, like 10.')
    if not 1 <= per_keyword <= MAX_PER_KEYWORD:
        raise ValueError(f'Choose between 1 and {MAX_PER_KEYWORD} listings per keyword.')
    return cleaned, ' '.join(str(location or '').split()), per_keyword


def start(account, keywords, location, per_keyword):
    if not account:
        raise ValueError('Pick an account in the top bar first.')
    meta = get_cdp_account(account)
    if not meta or meta.get('status') != 'ready':
        raise ValueError("This account isn't signed in to Facebook yet. Sign in from Accounts, then try again.")
    import os
    if os.getenv('FB_AUTOMATION_DESKTOP') == '1':
        from automation.account_browser import status as account_browser_status
        if account_browser_status().get('account') == account:
            raise ValueError('This account is open in Account Browser. Close it there, then try again.')
    keywords, location, per_keyword = _clean_inputs(keywords, location, per_keyword)
    with _lock:
        if _state.get('running'):
            raise ValueError('Research is already running. Wait for it to finish or press Stop.')
        _state.clear()
        _state.update(running=True, account=account, keywords=keywords, location=location,
                      per_keyword=per_keyword, stage='Getting ready…', message='',
                      found=0, unique=0, read=0, started_at=time.time())
    _stop.clear()
    _wipe()
    threading.Thread(target=_run, args=(account, keywords, location, per_keyword),
                     daemon=True, name='ad-research').start()


def stop():
    """Ask the run to stop. The browser stays open so the old location can be restored."""
    with _lock:
        if not _state.get('running'):
            return False
        _state['stage'] = 'Stopping…'
    _stop.set()
    return True


# ---------------------------------------------------------------- age / scoring

def parse_age(listed_text, title_read=True, price_read=True):
    """Return (group, days) from the listing page's "Listed X ago" text.

    "Listed in City" with no time means an older ad (Facebook drops the time after a few
    weeks). No "Listed" line at all is a shop item. If the page wasn't fully read, unknown.
    """
    text = str(listed_text or '').strip()
    if not text:
        # No "Listed" line at all: shop/catalog items look like this. Never counted as old.
        return ('shop', -1) if title_read and price_read else ('unread', -1)
    lowered = text.lower()
    if re.match(r'^listed\s+in\s', lowered):
        # "Listed in Forney, TX": Facebook drops the time on older listings.
        return ('oldest', 28) if title_read and price_read else ('unread', -1)
    if re.search(r'listed\s+(just now|yesterday|today)', lowered):
        return 'new', 1 if 'yesterday' in lowered else 0
    match = AGE_RE.search(text)
    if not match:
        return 'unread', -1  # a date is shown but in a form we don't recognise
    amount = 1 if match.group(1).lower() in {'a', 'an', 'one'} else int(match.group(1))
    unit = match.group(2).lower()
    if unit in {'minute', 'min', 'hour'}:
        return 'new', 0
    if unit == 'day':
        return ('new', amount) if amount < 7 else ('1_week', amount)
    if unit == 'week':
        return {1: '1_week', 2: '2_weeks', 3: '3_weeks'}.get(amount, 'oldest'), min(amount * 7, 28)
    return 'oldest', 28


def stars_for(appearances):
    return max(1, min(5, int(appearances or 1)))


def is_best(appearances, age_group):
    return appearances >= BEST_MIN_APPEARANCES and age_group == 'oldest'


def has_phone(text):
    return bool(PHONE_RE.search(str(text or '')))


def _title_words(title):
    words = re.findall(r"[a-z0-9][a-z0-9'/&-]*", str(title or '').lower())
    return [word for word in words if word not in STOP_WORDS and len(word) > 1]


def _price_number(item):
    try:
        return float(item.get('price') or '')
    except (TypeError, ValueError):
        return None


def _traits(items):
    """Plain facts about a group of listings."""
    if not items:
        return None
    prices = [value for value in (_price_number(item) for item in items) if value is not None]
    real_prices = [value for value in prices if value > 1]
    words = Counter(word for item in items for word in set(_title_words(item.get('title'))))
    photo_counts = [len(item.get('photos') or []) for item in items]
    count = len(items)
    pct = lambda n: round(100 * n / count)
    return {
        'count': count,
        'typical_price': round(statistics.median(real_prices)) if real_prices else None,
        'placeholder_price_pct': pct(sum(1 for value in prices if value <= 1)),
        'top_words': [word for word, _ in words.most_common(8)],
        'avg_photos': round(sum(photo_counts) / count, 1),
        'phone_in_text_pct': pct(sum(1 for item in items if item.get('phone_in_text'))),
        'phone_in_photo_pct': pct(sum(1 for item in items if item.get('phone_in_photo'))),
        'avg_description_words': round(sum(len(str(item.get('description') or '').split()) for item in items) / count),
    }


def build_results(keywords, keyword_hits, listings, ocr_checked):
    """Score every unique listing and build the summary."""
    for item in listings:
        found_by = [keyword for keyword in keywords if item['id'] in keyword_hits.get(keyword, [])]
        item['keywords'] = found_by
        item['appearances'] = len(found_by)
        item['stars'] = stars_for(item['appearances'])
        item['best'] = is_best(item['appearances'], item['age_group'])
        item['age_label'] = AGE_LABELS.get(item['age_group'], item['age_group'])
    listings.sort(key=lambda item: (-item['appearances'], -item['age_days'], item.get('title') or ''))
    best = [item for item in listings if item['best']]
    basis = best or [item for item in listings if item['appearances'] >= 2] or listings[:10]
    keyword_stats = []
    for keyword in keywords:
        ids = keyword_hits.get(keyword, [])
        overlap = sum(1 for item in listings if item['id'] in ids and item['appearances'] > 1)
        keyword_stats.append({'keyword': keyword, 'results': len(ids), 'overlap': overlap})
    ages = Counter(item['age_group'] for item in listings)
    summary = {
        'total_results': sum(len(keyword_hits.get(keyword, [])) for keyword in keywords),
        'unique': len(listings),
        'best': len(best),
        'multi_keyword': sum(1 for item in listings if item['appearances'] > 1),
        'ages': [{'group': group, 'label': AGE_LABELS[group], 'count': ages.get(group, 0)}
                 for group in AGE_ORDER + ['shop', 'unread'] if ages.get(group) or group in AGE_ORDER],
        'basis': 'best' if best else ('multi' if basis and basis[0]['appearances'] >= 2 else 'top'),
        'basis_traits': _traits(basis),
        'all_traits': _traits(listings),
        'ocr_checked': ocr_checked,
    }
    return {'listings': listings, 'keyword_stats': keyword_stats, 'summary': summary}


# ---------------------------------------------------------------- Facebook: location

_OPEN_LOCATION_JS = r"""
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const clickable = el => el.closest('[role="button"],[role="link"],a,button') || el;
  const nodes = [...document.querySelectorAll('[role="button"],[role="link"],a,span')].filter(visible);
  const radius = nodes.find(el => /\bwithin\s+\d+|\d+\s*(mi|miles|km|kilomet)/i.test((el.innerText || '').trim())
    && (el.innerText || '').length < 120);
  const label = nodes.find(el => (el.innerText || '').trim() === 'Location');
  const target = radius || label;
  if (!target) return false;
  clickable(target).click();
  return true;
"""

_LOCATION_INPUT = 'input[role="combobox"][aria-label="Location"]'

_APPLY_JS = r"""
  const visible = el => !!el.getClientRects().length;
  const scope = [...document.querySelectorAll('[role="dialog"]')].filter(visible).pop() || document;
  const button = [...scope.querySelectorAll('[role="button"],button')].filter(visible)
    .find(el => (el.getAttribute('aria-label') || el.innerText || '').trim() === 'Apply');
  if (!button) return false;
  button.click();
  return true;
"""


def _location_input(driver):
    return next((el for el in driver.find_elements(By.CSS_SELECTOR, _LOCATION_INPUT) if el.is_displayed()), None)


def _open_location_dialog(driver):
    field = _location_input(driver)
    if field is None:
        if not driver.execute_script(_OPEN_LOCATION_JS):
            raise RuntimeError("Couldn't find Location in the Marketplace sidebar.")
        field = WebDriverWait(driver, 20).until(lambda d: _location_input(d))
    return field


def _set_location(driver, value):
    """Type the location, take the first dropdown option, click Apply. No matching."""
    field = _open_location_dialog(driver)
    field.click()
    field.send_keys(Keys.CONTROL, 'a')
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(value)
    try:
        option = WebDriverWait(driver, 12).until(lambda d: next(
            (el for el in d.find_elements(By.CSS_SELECTOR, '[role="listbox"] [role="option"], [role="option"]')
             if el.is_displayed()), None))
        option.click()
    except Exception:
        field.send_keys(Keys.ENTER)
    _wait_quietly(1)
    if not WebDriverWait(driver, 15).until(lambda d: d.execute_script(_APPLY_JS)):
        raise RuntimeError("Couldn't click Apply in the Location box.")
    WebDriverWait(driver, 20).until(lambda d: _location_input(d) is None)
    _wait_quietly(3)


def _wait_quietly(seconds):
    """Sleep even after Stop: restoring the location must still finish."""
    time.sleep(seconds)


def _read_location(driver):
    """Open the Location box and note the account's current location (box stays open)."""
    field = _open_location_dialog(driver)
    return (field.get_attribute('value') or '').strip()


# ---------------------------------------------------------------- Facebook: search and read

def _open_search(driver, keyword):
    driver.get(SEARCH_URL.format(quote(keyword)))
    _sleep(3)
    extractor._close_dialogs(driver)


def _collect_ids(driver, limit):
    """Listing IDs from the current results page, scrolling until the limit or the end."""
    ids, idle = [], 0
    while len(ids) < limit and idle < 5:
        _check_stop()
        before = len(ids)
        for url, _ in driver.execute_script(extractor._ITEM_LINKS_JS) or []:
            match = re.search(r'/item/(\d+)', url)
            if match and match.group(1) not in ids:
                ids.append(match.group(1))
        idle = idle + 1 if len(ids) == before else 0
        if len(ids) >= limit:
            break
        driver.execute_script('window.scrollBy(0, Math.max(900, window.innerHeight));')
        _sleep(2)
        extractor._close_dialogs(driver)
    return ids[:limit]


def listed_from_ax_names(names):
    """Find "Listed X ago [in City]" in accessibility-tree text pieces (may be split)."""
    gap = r'[\s\x1f]+'
    pattern = re.compile(rf'Listed{gap}(?:about{gap})?(?:a|an|one|\d+){gap}\w+{gap}ago'
                         rf'(?:{gap}in{gap}[^\x1f]+)?|Listed{gap}(?:just now|yesterday|today)', re.I)
    for index, name in enumerate(names):
        if not re.match(r'^\s*listed\b', name, re.I):
            continue
        # \x1f marks piece boundaries so the location stops at the end of its own piece.
        match = pattern.search(' \x1f '.join(names[index:index + 5]))
        if match:
            return ' '.join(match.group(0).replace('\x1f', ' ').split())
    return ''


def _listed_from_accessibility_tree(driver):
    """Fallback date reader: Chrome's accessibility tree, independent of page text selectors."""
    try:
        tree = driver.execute_cdp_cmd('Accessibility.getFullAXTree', {})
    except Exception as exc:
        _log(f'Accessibility tree unavailable: {exc}')
        return ''
    names = []
    for node in tree.get('nodes') or []:
        if node.get('ignored'):
            continue
        if (node.get('role') or {}).get('value') not in {'StaticText', 'text'}:
            continue
        name = str((node.get('name') or {}).get('value') or '').strip()
        if name:
            names.append(name)
    return listed_from_ax_names(names)


def _ocr_phone(paths):
    """True/False when OCR ran, None when the local OCR runtime is unavailable."""
    from automation.visual_listing_reader import tesseract_ocr
    for path in paths[:OCR_PHOTOS_PER_LISTING]:
        _check_stop()
        try:
            text = ' '.join(line['text'] for line in tesseract_ocr(path))
        except RuntimeError as exc:
            if 'missing' in str(exc).lower():
                return None
            continue
        except Exception:
            continue
        if has_phone(text):
            return True
    return False


def _photos_past_video(driver):
    """The viewer can open on a video, so no photo is found. Click thumbnails until a photo shows."""
    for thumb in (driver.execute_script(extractor._THUMBNAILS_JS) or [])[:8]:
        _check_stop()
        try:
            driver.execute_script('arguments[0].click();', thumb)
        except Exception:
            continue
        deadline = time.time() + 4
        while time.time() < deadline:
            if driver.execute_script(extractor._LARGE_PHOTO_JS):
                return extractor._photo_urls(driver)
            _sleep(0.2)
    return []


def _read_one(driver, listing_id, state):
    url = f'https://www.facebook.com/marketplace/item/{listing_id}/'
    item = extractor._read_listing(driver, url)
    if not item.get('photo_urls'):
        item['photo_urls'] = _photos_past_video(driver)
        if item['photo_urls']:
            _log(f'Listing {listing_id}: photos found after skipping past a video.')
    listed = item.get('listed_info') or ''
    date_source = 'text' if listed else ''
    if not listed:
        listed = _listed_from_accessibility_tree(driver)
        date_source = 'accessibility tree' if listed else 'none'
    location = item.get('location') or ''
    if listed and not location:
        match = re.search(r'\bin\s+(.+)$', listed)
        location = match.group(1).strip() if match else ''
    group, days = parse_age(listed, bool(item.get('title')), bool(item.get('price_text')))
    paths = extractor._download_photos(item.get('photo_urls') or [], photos_dir() / listing_id)
    text = f"{item.get('title') or ''}\n{item.get('description') or ''}"
    phone_in_photo = None
    if state.get('ocr', True) and paths:
        phone_in_photo = _ocr_phone(paths)
        if phone_in_photo is None:
            state['ocr'] = False
            _log('Local OCR is not available; photo text will not be checked.')
    return {
        'id': listing_id, 'url': url, 'title': item.get('title') or '', 'price': item.get('price') or '',
        'price_text': item.get('price_text') or '', 'description': item.get('description') or '',
        'location': location, 'listed_info': listed, 'date_source': date_source,
        'age_group': group, 'age_days': days,
        'photos': [Path(path).name for path in paths],
        'phone_in_text': has_phone(text), 'phone_in_photo': bool(phone_in_photo),
    }


# ---------------------------------------------------------------- job

def _run(account, keywords, location, per_keyword):
    driver = handle = original = account_lock = None
    old_location = ''
    location_changed = False
    keyword_hits, listings, errors = {}, [], []
    ocr_state = {'ocr': True}
    stopped = False
    try:
        _update(stage='Opening Facebook…')
        driver, handle, original, account_lock = open_posting_tab(account)
        driver.set_page_load_timeout(180)
        try:
            # A narrow window stacks the listing layout and shrinks portrait photos below
            # the photo finder's size limit, so they were missed. Use a desktop-size window.
            driver.set_window_size(1400, 1000)
        except Exception as exc:
            _log(f'Could not resize the research window: {exc}')

        if location:
            _update(stage='Setting the search location…')
            _open_search(driver, keywords[0])
            old_location = _read_location(driver)
            _log(f'Account location before research: {old_location!r}')
            location_changed = True  # set first: a half-finished change is still put back
            _set_location(driver, location)
            _log(f'Research location set from {location!r}.')

        for number, keyword in enumerate(keywords, 1):
            _check_stop()
            _update(stage=f'Searching "{keyword}" ({number} of {len(keywords)})…')
            _open_search(driver, keyword)
            ids = _collect_ids(driver, per_keyword)
            if not ids:
                _log(f'No results for {keyword!r}; reloading once.')
                _open_search(driver, keyword)
                ids = _collect_ids(driver, per_keyword)
            keyword_hits[keyword] = ids
            unique = list(dict.fromkeys(i for hits in keyword_hits.values() for i in hits))
            _update(found=sum(len(hits) for hits in keyword_hits.values()), unique=len(unique))
            _log(f'{keyword!r}: {len(ids)} listing(s).')

        unique = list(dict.fromkeys(i for hits in keyword_hits.values() for i in hits))
        if not unique:
            raise RuntimeError('No listings were found for these keywords. Try different words.')
        for position, listing_id in enumerate(unique, 1):
            _check_stop()
            _update(stage=f'Reading listing {position} of {len(unique)}…')
            try:
                listings.append(_read_one(driver, listing_id, ocr_state))
                _update(read=len(listings))
            except InterruptedError:
                raise
            except Exception as exc:
                if _stop.is_set():
                    raise InterruptedError('Stopped by user.')
                errors.append(f'Listing {listing_id} skipped: {exc}')
                _log(f'Listing {listing_id} skipped: {exc}')
    except InterruptedError:
        stopped = True
    except Exception as exc:
        if _stop.is_set():
            stopped = True
        else:
            _log(f'Research failed: {exc}')
            errors.append(str(exc))
    finally:
        restore_error = ''
        if location_changed and driver is not None and old_location:
            _update(stage='Putting your Marketplace location back…')
            try:
                # Not _open_search: its Stop-aware sleep would abort the restore after Stop.
                driver.get(SEARCH_URL.format(quote(keywords[0])))
                _wait_quietly(3)
                extractor._close_dialogs(driver)
                _set_location(driver, old_location)
                _log(f'Account location restored to {old_location!r}.')
            except Exception as exc:
                restore_error = (f'Your Marketplace location could not be put back to "{old_location}". '
                                 'Please set it on Marketplace.')
                _log(f'Location restore failed: {exc}')
        if driver is not None:
            try:
                close_posting_tab(driver, handle, original, account_lock,
                                  close_browser=True, verify_browser_closed=True)
            except Exception as exc:
                _log(f'Chrome close problem: {exc}')
                try:
                    account_lock.release()
                except RuntimeError:
                    pass
        _finish(account, keywords, location, per_keyword, keyword_hits, listings, errors,
                stopped, restore_error, ocr_state['ocr'])


def _finish(account, keywords, location, per_keyword, keyword_hits, listings, errors,
            stopped, restore_error, ocr_checked):
    message_parts = []
    if listings:
        results = build_results(keywords, keyword_hits, listings, ocr_checked)
        results.update(account=account, keywords=keywords, location=location, per_keyword=per_keyword,
                       finished_at=time.strftime('%Y-%m-%d %H:%M'), stopped=stopped,
                       errors=errors[-20:], saved={})
        _save_results(results)
        summary = results['summary']
        message_parts.append(('Stopped. ' if stopped else 'Done! ') +
                             f"{summary['unique']} unique listings analysed, {summary['best']} Best.")
        stage = 'Stopped' if stopped else 'Finished'
    elif stopped:
        message_parts.append('Stopped before any listing was read.')
        stage = 'Stopped'
    else:
        message_parts.append(errors[-1] if errors else 'Research could not finish. Please try again.')
        stage = 'Failed'
    if restore_error:
        message_parts.append(restore_error)
    _update(running=False, stage=stage, message=' '.join(message_parts), finished_at=time.time())


# ---------------------------------------------------------------- Backup Posts

def add_to_backup(account, listing_id):
    """Save one researched listing to Backup Posts (Extracted) with its rating mark."""
    if not account:
        raise ValueError('Pick an account in the top bar first.')
    results = load_results()
    if not results:
        raise ValueError('There are no research results. Run Ad Research first.')
    item = next((row for row in results.get('listings', []) if row.get('id') == str(listing_id)), None)
    if not item:
        raise ValueError('That listing is not in the current results.')
    if results.get('saved', {}).get(item['id']):
        raise ValueError('This listing is already in Backup Posts.')
    folder = photos_dir() / item['id']
    paths = [str(folder / name) for name in item.get('photos') or [] if (folder / name).is_file()]
    if not paths:
        raise ValueError("This listing has no downloaded photos, so it can't be saved.")
    from automation.manual_backups import save_manual_listing
    record_id = 'research_' + uuid4().hex[:16]
    payload = {
        'title': item['title'], 'price': item['price'], 'description': item['description'],
        'condition': '', 'location': item['location'], 'listed_info': item['listed_info'],
        'source_url': item['url'],
        '_manual_record_id': record_id, '_listing_kind': 'service',
        '_extracted_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        '_research': {'stars': item['stars'], 'best': item['best'], 'appearances': item['appearances'],
                      'keywords': item['keywords'], 'age_label': item['age_label']},
    }
    for index, path in enumerate(paths, 1):
        payload[f'image_path{index}'] = path
    save_manual_listing(account, payload)
    with _lock:
        results = load_results() or results
        results.setdefault('saved', {})[item['id']] = record_id
        _save_results(results)
    return record_id
