"""Post a locksmith service ad through Facebook's desktop *item* listing form.

Same approach as the vehicle poster (post_to_fb_marketplace): dedicated tab in
the account's CDP Chrome, open the category URL directly, upload all photos in
one call, fill each field with verified helpers, review the live form, then
Next -> optional group selection -> Publish -> confirmation.

Fields filled: Photos, Title, Price, Category (auto/car parts), Condition (New),
Description, Location (when given), Public meetup.

post_item_to_marketplace() opens and closes its own posting tab;
post_item_in_tab() does the same form work in a tab the caller already has
(Manage Listings reposts inside its embedded Chrome).
"""
import os
import re
import unicodedata

from selenium.webdriver.common.keys import Keys

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from automation.diagnostics import event as diagnostic_event, traced
from automation.task_control import cancellable, cancellable_sleep, check_cancelled, guard_driver
from automation.cdp_login import open_posting_tab
from automation.posting_inputs import fill_posting_input
from automation.posting_description import fill_description
from automation.posting_recovery import click_action
from automation.posting_review import review_posting_fields
from automation.reposting import read_edit_form_snapshot
from automation.auto_listing_excel_poster import (
    print,
    _log_field_method,
    _close_account_browser,
    _norm_text,
    _input_for_label_xpath,
    _marketplace_combo,
    _marketplace_combo_value,
    _select_marketplace_combo,
    _select_groups_for_listing,
    _marketplace_publish_confirmed,
)

CREATE_ITEM_URL = 'https://www.facebook.com/marketplace/create/item'
WAIT_SECONDS = 90
MAX_PHOTOS = 5
CONDITION = 'New'
# Facebook names this category differently per account/region:
# "Auto parts", "Car parts", "Auto & car parts", "Car & auto parts", ...
_PREFERRED_CATEGORIES = ('auto parts', 'car parts', 'auto car parts', 'auto & car parts',
                         'car & auto parts', 'auto and car parts', 'car and auto parts')
_REVIEW_LABELS = {'title': 'Title', 'price': 'Price', 'description': 'Description'}


def _category_score(text):
    """Lower is better; None means the option is not an auto/car parts category."""
    value = re.sub(r'\s+', ' ', str(text or '')).strip().casefold()
    if value in _PREFERRED_CATEGORIES:
        return _PREFERRED_CATEGORIES.index(value)
    if re.search(r'\bparts?\b', value) and re.search(r'\b(auto|autos|car|cars|automotive)\b', value):
        return len(_PREFERRED_CATEGORIES)
    if re.search(r'\bparts?\b', value) and re.search(r'\bvehicles?\b', value):
        return len(_PREFERRED_CATEGORIES) + 1
    return None


def _best_category(choices):
    scored = [(score, index, element, text) for index, (element, text) in enumerate(choices)
              for score in [_category_score(text)] if score is not None]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[0][2], scored[0][3]


_CATEGORY_FIELD_JS = r"""
  const norm = v => (v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = el => !!(el && el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const clickable = el => el.closest('[role="combobox"],[role="button"],label,[tabindex]') || el;
  // 1) Primary: any visible <span> whose own text is exactly "Category".
  const span = [...document.querySelectorAll('span')].filter(visible)
    .find(el => norm(el.textContent) === 'category');
  if (span) return clickable(span);
  // 2) Fallback: element whose aria-label is "Category".
  const byAria = [...document.querySelectorAll('[aria-label]')].filter(visible)
    .find(el => norm(el.getAttribute('aria-label')) === 'category');
  if (byAria) return byAria;
  // 3) Fallback: combobox/label whose leading text is "Category".
  const byText = [...document.querySelectorAll('[role="combobox"],label')].filter(visible)
    .find(el => /^category\b/.test(norm(el.innerText || el.textContent)));
  return byText || null;
"""

# Every visible, clickable-looking text in an open popup, whatever its HTML structure.
_POPUP_CHOICES_JS = r"""
  const field = arguments[0];
  const norm = v => (v || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const popups = [...document.querySelectorAll('[role="listbox"],[role="menu"],[role="dialog"],[role="list"]')]
    .filter(el => visible(el) && !(field && el.contains(field)));
  const scopes = popups.length ? popups : [document.body];
  const out = [], seen = new Set();
  for (const scope of scopes) {
    const nodes = [...scope.querySelectorAll('[role="option"],[role="menuitem"],[role="radio"],[role="button"],[role="listitem"],[tabindex],span')];
    for (const el of nodes) {
      if (!visible(el) || (field && field.contains(el))) continue;
      const text = norm(el.innerText || el.textContent);
      if (!text || text.length > 60 || seen.has(text.toLowerCase())) continue;
      if ([...el.children].some(child => norm(child.innerText || child.textContent) === text)) continue;
      seen.add(text.toLowerCase());
      const target = el.closest('[role="option"],[role="menuitem"],[role="radio"],[role="button"],[role="listitem"],[tabindex]') || el;
      out.push([target, text]);
    }
  }
  return out;
"""

_SCROLL_POPUP_JS = r"""
  const field = arguments[0];
  const visible = el => !!(el && el.getClientRects().length);
  const boxes = [...document.querySelectorAll('[role="listbox"],[role="menu"],[role="dialog"],[role="list"],div')]
    .filter(el => visible(el) && !(field && el.contains(field)) && el.scrollHeight > el.clientHeight + 20
      && /(auto|scroll)/.test(getComputedStyle(el).overflowY));
  if (!boxes.length) return false;
  const box = boxes.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
  const before = box.scrollTop;
  box.scrollTop = before + Math.max(120, box.clientHeight - 40);
  return box.scrollTop > before;
"""

_CATEGORY_VALUE_JS = r"""
  const el = arguments[0], norm = v => (v || '').replace(/\s+/g, ' ').trim();
  const input = el.matches('input') ? el : el.querySelector('input');
  if (input && norm(input.value)) return norm(input.value);
  const text = norm(el.innerText || el.textContent).replace(/^category\s*/i, '');
  return /^(select|choose)/i.test(text) ? '' : text;
"""


def _category_field(driver):
    return driver.execute_script(_CATEGORY_FIELD_JS)


def _category_value(driver):
    field = _category_field(driver)
    return _norm_text(driver.execute_script(_CATEGORY_VALUE_JS, field)) if field else ''


def _select_category(driver, open_wait=10):
    label = 'Category'
    _log_field_method(label, 'locate field', 'START', detail='timeout=30s')
    field = WebDriverWait(driver, 30, poll_frequency=0.5).until(lambda d: _category_field(d))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
    _log_field_method(label, 'locate field', 'SUCCESS')
    current = _category_value(driver)
    if current and _category_score(current) is not None:
        _log_field_method(label, 'existing selection matches', 'SUCCESS', value=current)
        return current

    seen = []
    for method in ('javascript', 'native'):
        check_cancelled()
        _log_field_method(label, f'open menu using {method}', 'START', detail=f'wait up to {open_wait}s for options')
        try:
            field = _category_field(driver)
            if method == 'javascript':
                driver.execute_script('arguments[0].click();', field)
            else:
                field.click()
        except Exception as exc:
            _log_field_method(label, f'open menu using {method}', 'FAILED', error=exc)
            continue
        match = None
        for _ in range(int(open_wait / 0.4)):
            check_cancelled()
            choices = driver.execute_script(_POPUP_CHOICES_JS, _category_field(driver)) or []
            for _, text in choices:
                if text not in seen:
                    seen.append(text)
            match = _best_category(choices)
            if match:
                break
            if choices:
                driver.execute_script(_SCROLL_POPUP_JS, _category_field(driver))
            cancellable_sleep(0.4)
        if not match:
            _log_field_method(label, f'open menu using {method}', 'FAILED',
                              detail='no auto/car parts option visible; options seen: ' + ', '.join(seen[:80]))
            continue
        option, text = match
        _log_field_method(label, f'open menu using {method}', 'SUCCESS', value=text)
        for click in ('javascript', 'native'):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", option)
                if click == 'javascript':
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                        "arguments[0].click();", option)
                else:
                    option.click()
                WebDriverWait(driver, 8, poll_frequency=0.3).until(
                    lambda d: _category_score(_category_value(d)) is not None)
                selected = _category_value(driver)
                _log_field_method(label, f'select option using {click}', 'SUCCESS', value=selected)
                return selected
            except Exception as exc:
                _log_field_method(label, f'select option using {click}', 'FAILED', value=text, error=exc)
    raise RuntimeError('No auto/car parts category could be selected. Options seen: '
                       + (', '.join(seen[:80]) or 'none (the Category menu did not open)'))


def _public_meetup_checkbox(driver):
    return driver.execute_script(r"""
      const norm = v => (v || '').replace(/\s+/g, ' ').trim().toLowerCase();
      return [...document.querySelectorAll('[role="checkbox"]')].find(el =>
        [...el.querySelectorAll('span')].some(span => norm(span.textContent) === 'public meetup')) || null;
    """)


def _check_public_meetup(driver):
    label = 'Public meetup'
    _log_field_method(label, 'locate checkbox', 'START')
    box = WebDriverWait(driver, WAIT_SECONDS).until(lambda d: _public_meetup_checkbox(d))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    _log_field_method(label, 'locate checkbox', 'SUCCESS')

    def checked(d):
        current = _public_meetup_checkbox(d)
        return bool(current) and (current.get_attribute('aria-checked') or '').lower() == 'true'

    if checked(driver):
        _log_field_method(label, 'existing state matches', 'SUCCESS', value='checked')
        return
    for method in ('native', 'javascript'):
        _log_field_method(label, f'click using {method}', 'START')
        try:
            box = _public_meetup_checkbox(driver)
            if method == 'native':
                box.click()
            else:
                driver.execute_script('arguments[0].click();', box)
            WebDriverWait(driver, 10).until(checked)
            _log_field_method(label, f'click using {method}', 'SUCCESS', value='checked')
            return
        except Exception as exc:
            _log_field_method(label, f'click using {method}', 'FAILED', error=exc)
    raise RuntimeError('Public meetup could not be checked.')


_LOCATION_INPUT_JS = r"""
  const vis = el => !!(el && el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  // 1) Primary: the visible "Location" label text, then its input.
  const span = [...document.querySelectorAll('span')]
    .find(el => vis(el) && el.textContent.trim() === 'Location' && !el.querySelector('span'));
  const label = span && span.closest('label');
  const viaText = label && label.querySelector('input');
  if (viaText && vis(viaText)) return viaText;
  // 2) Fallback: the Location combobox by its aria-label.
  return [...document.querySelectorAll('input[aria-label="Location"]')].find(vis) || null;
"""

_LOCATION_OPTIONS_JS = r"""
  const input = arguments[0];
  const vis = el => !!(el && el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const owned = input && input.getAttribute('aria-controls') ? document.getElementById(input.getAttribute('aria-controls')) : null;
  const scope = owned && vis(owned) ? owned : document;
  return [...scope.querySelectorAll('[role="option"]')].filter(vis)
    .map(el => [el, (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()])
    .filter(([, text]) => text);
"""


def _location_words(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r'[^\W_]+', text)


def _location_result_matches(requested, result):
    """A Facebook location matches when it contains every word of the requested city
    (the part before the first comma), so "Dallas, Texas" accepts "Dallas, TX"."""
    city = _location_words(str(requested or '').split(',')[0])
    found = _location_words(result)
    return bool(city) and all(word in found for word in city)


def _location_input(driver):
    return driver.execute_script(_LOCATION_INPUT_JS)


def _location_value(driver):
    field = _location_input(driver)
    return _norm_text(field.get_attribute('value')) if field else ''


def _click_location_field(driver, field):
    """Click into Location; if something on the page covers it, focus it with a script click."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
    try:
        field.click()
    except Exception:
        driver.execute_script('arguments[0].focus(); arguments[0].click();', field)


def _fill_location(driver, location):
    """Type the location exactly as the user wrote it and pick Facebook's first suggestion.

    No spelling match is required: Facebook's suggestions correct typos, so the first
    one is taken. Returns the value Facebook shows after the pick.
    """
    label = 'Location'
    requested = _norm_text(location)
    _log_field_method(label, 'locate field', 'START', value=requested)
    try:
        field = WebDriverWait(driver, 20, poll_frequency=0.3).until(lambda d: _location_input(d))
    except Exception:
        # Some layouts keep Location inside a collapsed "More details" section.
        driver.execute_script(r"""
          const span = [...document.querySelectorAll('span')].find(el => el.textContent.trim() === 'More details');
          const button = span && (span.closest('[role="button"]') || span);
          if (button) button.click();
        """)
        field = WebDriverWait(driver, WAIT_SECONDS, poll_frequency=0.3).until(lambda d: _location_input(d))
    seen = []
    for attempt in (1, 2):
        check_cancelled()
        _log_field_method(label, 'type and pick first suggestion', 'START', detail=f'attempt={attempt}/2')
        field = _location_input(driver)
        _click_location_field(driver, field)
        field.send_keys(Keys.CONTROL, 'a')
        field.send_keys(Keys.BACKSPACE)
        field.send_keys(requested)
        first = None
        for _ in range(40):  # up to ~12s for Facebook's suggestions
            check_cancelled()
            options = driver.execute_script(_LOCATION_OPTIONS_JS, _location_input(driver)) or []
            if options:
                first = options[0]
                seen = [text for _, text in options[:8]]
                break
            cancellable_sleep(0.3)
        if not first:
            _log_field_method(label, 'type and pick first suggestion', 'FAILED', detail='Facebook showed no suggestions')
            continue
        element, text = first
        try:
            element.click()
        except Exception:
            driver.execute_script('arguments[0].click();', element)
        # Picked once the dropdown closes and the field holds a value.
        try:
            WebDriverWait(driver, 10, poll_frequency=0.3).until(
                lambda d: _location_value(d) and not driver.execute_script(_LOCATION_OPTIONS_JS, _location_input(d)))
        except Exception:
            _log_field_method(label, 'type and pick first suggestion', 'FAILED',
                              detail=f'picked "{text}" but the field shows "{_location_value(driver)}"')
            continue
        selected = _location_value(driver)
        _log_field_method(label, 'type and pick first suggestion', 'SUCCESS', value=selected, detail=f'picked "{text}"')
        return selected
    raise RuntimeError(f'Facebook did not accept the location "{requested}". '
                       f'It suggested: {"; ".join(seen) or "nothing"}.')



def _upload_photos(driver, images):
    _log_field_method('Images', 'single Selenium multi-file upload', 'START', detail=f'files={len(images)}')
    file_input = WebDriverWait(driver, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.XPATH, "//input[@type='file' and not(@disabled)]")))
    try:
        driver.execute_script("arguments[0].style.display='block';arguments[0].style.visibility='visible';"
                              "arguments[0].removeAttribute('hidden');", file_input)
    except Exception:
        pass
    file_input.send_keys('\n'.join(images))
    _log_field_method('Images', 'single Selenium multi-file upload', 'SUCCESS',
                      detail=f'Submitted {len(images)} file path(s).')
    try:
        removed = driver.execute_script("""
          const seen = new Set(); let removed = 0;
          for (const btn of document.querySelectorAll('[aria-label="Remove photo"]')) {
            const card = btn.closest('[role="button"], div, li') || btn.parentElement;
            const src = card && card.querySelector('img') ? card.querySelector('img').src : '';
            if (!src) continue;
            if (seen.has(src)) { try { btn.click(); removed++; } catch (e) {} } else seen.add(src);
          }
          return removed;
        """)
        if removed:
            _log_field_method('Images', 'duplicate preview cleanup', 'SUCCESS', detail=f'removed={removed}')
    except Exception:
        pass


@cancellable
@traced('POST')
def post_item_to_marketplace(data, account, group_names=None):
    """Return True when Publish succeeded; raise with the failing step otherwise."""
    _check_item_data(data)
    driver, posting_handle, original_handles, posting_lock = open_posting_tab(account)
    unguarded_execute = driver.execute
    try:
        # Every browser command (including waits) now stops at once on Cancel.
        guard_driver(driver)
        return post_item_in_tab(driver, data, account, group_names)
    finally:
        # Let the cleanup commands run even after a cancel, so the tab closes.
        driver.execute = unguarded_execute
        _close_account_browser(driver, posting_handle, original_handles, posting_lock)


def _check_item_data(data):
    title, price = _norm_text(data.get('title')), _norm_text(data.get('price'))
    images = [os.path.abspath(str(data['image_path%d' % i]))
              for i in range(1, MAX_PHOTOS + 1) if _norm_text(data.get('image_path%d' % i))]
    if not title or not price:
        raise ValueError('Title and price are required.')
    if not images:
        raise ValueError('At least one photo is required.')
    missing = [path for path in images if not os.path.isfile(path)]
    if missing:
        raise ValueError(f'{len(missing)} selected photo file(s) are missing when posting starts.')
    return images


def post_item_in_tab(driver, data, account, group_names=None):
    """Fill and publish the item form in the driver's current tab. The caller owns the tab."""
    title = _norm_text(data.get('title'))
    price = _norm_text(data.get('price'))
    description = _norm_text(data.get('description'))
    location = _norm_text(data.get('location'))
    if group_names is None:
        group_names = data.get('group_names') or []
    if isinstance(group_names, str):
        group_names = [group_names]
    group_names = [str(name).strip() for name in group_names if str(name or '').strip()]

    images = _check_item_data(data)

    diagnostic_event('ITEM_POST', 'START', account=account, title=title, photos=len(images),
                     location=location or '(Facebook default)')
    active_field = 'Create page'
    try:
        driver.set_page_load_timeout(240)

        # --- Open the item form directly (no category menu on a start page) ---
        ready = False
        for attempt in (1, 2):
            _log_field_method('Create page', 'navigate to item form', 'START', detail=f'attempt={attempt}/2')
            driver.get(CREATE_ITEM_URL)
            try:
                WebDriverWait(driver, WAIT_SECONDS).until(lambda d: (
                    '/marketplace/create/item' in str(d.current_url or '').lower()
                    and bool(d.find_elements(By.XPATH, "//input[@type='file' and not(@disabled)]"))))
                ready = True
                _log_field_method('Create page', 'navigate to item form', 'SUCCESS', detail=f'attempt={attempt}/2')
                break
            except Exception as exc:
                _log_field_method('Create page', 'navigate to item form', 'FAILED', error=exc,
                                  detail=f'URL={driver.current_url}')
        if not ready:
            raise RuntimeError('Facebook item form did not load. Check login and Marketplace access.')

        active_field = 'Images'
        _upload_photos(driver, images)

        active_field = 'Title'
        if not fill_posting_input(driver, 'title', 'Title', title, (By.XPATH, _input_for_label_xpath('Title')),
                                  log=_log_field_method, timeout=WAIT_SECONDS):
            raise RuntimeError('Title did not retain the requested text.')

        active_field = 'Price'
        if not fill_posting_input(driver, 'price', 'Price', price, (By.XPATH, _input_for_label_xpath('Price')),
                                  log=_log_field_method, timeout=WAIT_SECONDS):
            raise RuntimeError('Price did not retain the requested amount.')

        active_field = 'Category'
        category = _select_category(driver)
        data['category'] = category

        active_field = 'Condition'
        data['item_condition'] = _select_marketplace_combo(driver, 'Condition', CONDITION,
                                                           timeout=WAIT_SECONDS, exact_only=True)

        active_field = 'Description'
        if description and not fill_description(driver, description, log=_log_field_method, timeout=WAIT_SECONDS):
            # Review below retries; some layouts only keep Description after a re-render.
            _log_field_method('Description', 'initial fill', 'FAILED', detail='will be retried during review')

        chosen_location = ''
        if location:
            active_field = 'Location'
            chosen_location = data['location'] = _fill_location(driver, location)

        active_field = 'Public meetup'
        _check_public_meetup(driver)

        # --- Review live form; refill anything Facebook cleared ---
        def refill(key, label, value):
            if key == 'description':
                if not fill_description(driver, value, log=_log_field_method, timeout=WAIT_SECONDS):
                    raise RuntimeError('Description did not retain the supplied text.')
            elif key in ('title', 'price'):
                if not fill_posting_input(driver, key, label, value, (By.XPATH, _input_for_label_xpath(label)),
                                          log=_log_field_method, timeout=WAIT_SECONDS):
                    raise RuntimeError(f'{label} did not retain the requested value.')
            elif key == 'category':
                _select_category(driver)
            elif key == 'item_condition':
                _select_marketplace_combo(driver, 'Condition', CONDITION, timeout=WAIT_SECONDS, exact_only=True)

        def refill_location():
            nonlocal chosen_location
            chosen_location = data['location'] = _fill_location(driver, location)

        def repair_cleared_fields():
            snapshot = read_edit_form_snapshot(driver)
            repaired = 0
            for key, value in (('title', title), ('price', price), ('description', description)):
                field = snapshot.get(key, {})
                if value and field.get('state') == 'empty':
                    refill(key, _REVIEW_LABELS[key], value)
                    repaired += 1
            if _category_field(driver) and not _category_value(driver):
                refill('category', 'Category', None)
                repaired += 1
            if _marketplace_combo(driver, 'Condition') and not _marketplace_combo_value(driver, 'Condition'):
                refill('item_condition', 'Condition', CONDITION)
                repaired += 1
            if location and _location_input(driver):
                if _location_value(driver) != chosen_location:
                    refill_location()
                    repaired += 1
            return repaired

        review_values = {'title': title, 'price': price, 'description': description}
        verified = set()

        def require_review(stage, **options):
            result = review_posting_fields(
                driver, review_values, refill, stage=stage, labels=_REVIEW_LABELS,
                verified_fields=verified, log=_log_field_method, event=diagnostic_event,
                account=account, timeout=WAIT_SECONDS, **options)
            if not result.ok:
                raise RuntimeError(result.message or f'{stage}: form review failed.')
            return True

        require_review('before Next', defer_description=True)
        if location:
            # Facebook can reset Location to the profile's city while other fields re-render.
            if _location_value(driver) != chosen_location:
                _log_field_method('Location', 'check before Next', 'FAILED',
                                  detail=f'field shows "{_location_value(driver)}"; filling again')
                refill_location()

        active_field = 'Next'
        if click_action(driver, 'Next', repair_cleared_fields,
                        verify=lambda: require_review('immediately before Next', defer_description=True),
                        alternative='Publish', log=_log_field_method):
            print('Clicked Next')
            cancellable_sleep(0.5)

        active_field = 'Form review after Next'
        require_review('after Next', visible_only=True)

        if group_names:
            active_field = 'Groups'
            try:
                WebDriverWait(driver, WAIT_SECONDS).until(EC.presence_of_element_located((By.XPATH,
                    "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz'), 'list in your groups')]")))
                selected = _select_groups_for_listing(driver, group_names)
                _log_field_method('Groups', 'select requested groups', 'SUCCESS' if selected else 'SKIPPED',
                                  detail=f'{len(selected or [])} of {len(group_names)} selected')
            except Exception as exc:
                _log_field_method('Groups', 'group selection', 'FAILED', error=exc)

        active_field = 'Publish'
        if not click_action(driver, 'Publish', repair_cleared_fields,
                            verify=lambda: require_review('immediately before Publish', visible_only=True),
                            log=_log_field_method):
            raise RuntimeError('Publish did not receive a successful click.')
        print('Clicked Publish')
        diagnostic_event('PUBLISH', 'CLICKED', account=account, title=title)
        cancellable_sleep(7)
        if data.get('_require_publish_confirmation'):
            try:
                WebDriverWait(driver, WAIT_SECONDS).until(_marketplace_publish_confirmed)
            except Exception as exc:
                raise RuntimeError('Publish was clicked, but Facebook did not confirm publication. '
                                   'Check Facebook before retrying.') from exc
            diagnostic_event('PUBLISH', 'CONFIRMED_BY_UI', account=account, title=title)
            url = str(driver.current_url or '')
            if re.search(r'/marketplace/item/\d+(?:/|\?|$)', url):
                data['_confirmed_listing_url'] = url
        diagnostic_event('ITEM_POST', 'COMPLETED', account=account, title=title, category=category)
        return True
    except Exception as exc:
        diagnostic_event('ITEM_POST', 'FAILED', account=account, title=title, stage=active_field,
                         error_type=type(exc).__name__, error=str(exc))
        raise RuntimeError(f'{active_field}: {exc}') from exc
