"""One saved Chrome profile embedded in the Windows desktop window."""
import ctypes
from ctypes import wintypes
import json
import os
import pickle
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4

import websocket

from automation import cdp_login
from automation.fb_login import _safe_session_filename
from automation.runtime_paths import sessions_dir
from automation.session_health import HEALTHY, set_session_health
from automation.task_control import active_tasks

_lock = threading.RLock()
_account = _account_lock = _chrome_hwnd = None
_generation = 0
_needs_focus = False
_owner = None
_view = None  # 'listings' while Manage Listings shows only the listings column
_listings_session = None  # page connection that keeps the listings-only script registered
_held = False  # a Manage Listings job is running: keep Chrome open even with no page
user32 = ctypes.windll.user32 if os.name == 'nt' else None
kernel32 = ctypes.windll.kernel32 if os.name == 'nt' else None
gdi32 = ctypes.windll.gdi32 if os.name == 'nt' else None
WS_CHILD, WS_POPUP, WS_CAPTION, WS_THICKFRAME = 0x40000000, 0x80000000, 0x00C00000, 0x00040000
WS_EX_FRAME_EDGES = 0x00000100 | 0x00000200 | 0x00000001 | 0x00020000
SW_HIDE, SW_SHOW, SWP_NOZORDER, SWP_FRAMECHANGED = 0, 5, 4, 0x20
if user32:
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.EnableWindow.restype = wintypes.BOOL
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.CreateRectRgn.restype = wintypes.HANDLE
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _chrome_content_window(hwnd):
    """Find Chrome's keyboard-receiving content widget inside the embedded window."""
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(child, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, name, len(name))
        if name.value == 'Chrome_RenderWidgetHostHWND' and user32.IsWindowVisible(child):
            found.append(child)
        return True

    user32.EnumChildWindows(hwnd, visit, 0)
    return found[-1] if found else hwnd


def _focus_browser(hwnd, host):
    """Connect the request thread to Chrome's input queue before focusing it."""
    current = kernel32.GetCurrentThreadId()
    chrome_thread = user32.GetWindowThreadProcessId(hwnd, None)
    host_thread = user32.GetWindowThreadProcessId(host, None)
    attached = []
    try:
        for target in {chrome_thread, host_thread}:
            if target and target != current and user32.AttachThreadInput(current, target, True):
                attached.append(target)
        user32.SetForegroundWindow(host)
        user32.SetFocus(_chrome_content_window(hwnd))
    finally:
        for target in reversed(attached):
            user32.AttachThreadInput(current, target, False)


def _window_for_pid(pid):
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


# The embedded Chrome is read without chromedriver: the page URL comes from the
# DevTools HTTP list and cookies from the browser-level target, so nothing is
# attached to or evaluated in the Facebook page itself.
def _devtools_json(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}{path}', timeout=3) as response:
        return json.load(response)


def _page_target(port):
    return next((t for t in _devtools_json(port, '/json/list') if t.get('type') == 'page'), None)


_cdp_ids = iter(range(1, 1 << 30))


def _cdp_send(ws, method, params=None):
    message_id = next(_cdp_ids)
    ws.send(json.dumps({'id': message_id, 'method': method, 'params': params or {}}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get('id') == message_id:
            if 'error' in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get('result') or {}


def _cdp_connect(ws_url):
    return websocket.create_connection(ws_url, timeout=10, suppress_origin=True)


def _facebook_cookies(port):
    """Return the logged-in Facebook cookies in Selenium's format, or None."""
    page = _page_target(port)
    url = str((page or {}).get('url') or '').lower()
    host = urlsplit(url).hostname or ''
    if not (host == 'facebook.com' or host.endswith('.facebook.com')):
        return None
    if any(marker in url for marker in ('/login', 'checkpoint', 'two_step_verification')):
        return None
    ws = _cdp_connect(_devtools_json(port, '/json/version')['webSocketDebuggerUrl'])
    try:
        raw = _cdp_send(ws, 'Storage.getCookies').get('cookies') or []
    finally:
        ws.close()
    cookies = []
    for c in raw:
        domain = str(c.get('domain') or '')
        if not (domain.lstrip('.') == 'facebook.com' or domain.endswith('.facebook.com')):
            continue
        cookie = {'name': c['name'], 'value': c['value'], 'domain': domain, 'path': c.get('path') or '/',
                  'secure': bool(c.get('secure')), 'httpOnly': bool(c.get('httpOnly'))}
        if c.get('sameSite'):
            cookie['sameSite'] = c['sameSite']
        if not c.get('session') and float(c.get('expires') or -1) > 0:
            cookie['expiry'] = int(c['expires'])
        cookies.append(cookie)
    names = {c['name'] for c in cookies if c['value']}
    return cookies if {'c_user', 'xs'} <= names else None


def _save_cookies(account, cookies):
    if not cookies:
        return False
    path = os.path.join(sessions_dir(), _safe_session_filename(account))
    temporary = f'{path}.{uuid4().hex}.tmp'
    try:
        with open(temporary, 'wb') as stream:
            pickle.dump(cookies, stream)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    cdp_login._update_account(account, status='ready', relogin=False,
                              cookies_updated_at=int(time.time()),
                              message='Facebook login saved. Posting browser is ready.')
    set_session_health(account, HEALTHY)
    return True


def _watch(account, generation):
    """Keep the saved session fresh: every 5s until the first save, then once a minute."""
    last_check = last_save = 0
    while True:
        time.sleep(5)
        with _lock:
            if generation != _generation or account != _account:
                return
        meta = cdp_login.get_cdp_account(account)
        if not meta or not cdp_login._port_open(meta.get('port')):
            return
        if time.time() - last_check < (60 if last_save else 5):
            continue
        last_check = time.time()
        try:
            if _save_cookies(account, _facebook_cookies(meta['port'])):
                last_save = time.time()
        except Exception:
            pass


class PageSession:
    """One DevTools connection to the embedded page, safe to call from several threads.

    A reader thread matches replies to calls and discards the Page events Chrome
    sends, so nothing piles up while the connection stays open.
    """

    def __init__(self, ws_url):
        self._ws = _cdp_connect(ws_url)
        self._ws.settimeout(None)
        self._send_lock = threading.Lock()
        self._pending = {}
        self._ids = iter(range(1, 1 << 30))
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                reply = json.loads(self._ws.recv())
                waiter = self._pending.get(reply.get('id'))
                if waiter:
                    waiter[1] = reply
                    waiter[0].set()
        except Exception:
            pass
        finally:
            self.closed = True
            for waiter in list(self._pending.values()):
                waiter[0].set()

    def call(self, method, params=None, timeout=15):
        if self.closed:
            raise RuntimeError('The listings page is closed.')
        message_id = next(self._ids)
        waiter = self._pending[message_id] = [threading.Event(), None]
        try:
            with self._send_lock:
                self._ws.send(json.dumps({'id': message_id, 'method': method, 'params': params or {}}))
            if not waiter[0].wait(timeout):
                raise TimeoutError(f'{method} took too long.')
            reply = waiter[1]
            if reply is None:
                raise RuntimeError('The listings page is closed.')
            if 'error' in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get('result') or {}
        finally:
            self._pending.pop(message_id, None)

    def evaluate(self, expression, timeout=15):
        """Run JavaScript in the page and return its JSON value (Runtime stays disabled)."""
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                'awaitPromise': True}, timeout=timeout)
        if 'exceptionDetails' in result:
            details = result['exceptionDetails']
            raise RuntimeError(str((details.get('exception') or {}).get('description') or details.get('text')))
        return (result.get('result') or {}).get('value')

    def close(self):
        self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass


def _close_listings_session():
    global _listings_session
    session, _listings_session = _listings_session, None
    if session:
        session.close()


def listings_session():
    """The open Manage Listings page connection, or None."""
    with _lock:
        session = _listings_session
        return session if session and not session.closed and _view == 'listings' else None


def set_input_blocked(blocked):
    """Stop mouse and keyboard reaching the embedded Chrome; DevTools input still works."""
    with _lock:
        if user32 and _chrome_hwnd and user32.IsWindow(_chrome_hwnd):
            user32.EnableWindow(_chrome_hwnd, not blocked)


def status():
    with _lock:
        return {'account': _account, 'open': _account is not None}


def _conceal():
    """Make Chrome invisible without hiding it, so a running job keeps rendering normally."""
    if user32 and _chrome_hwnd and user32.IsWindow(_chrome_hwnd):
        region = gdi32.CreateRectRgn(0, 0, 0, 0)
        if region and not user32.SetWindowRgn(_chrome_hwnd, region, True):
            gdi32.DeleteObject(region)


def hide():
    with _lock:
        if _held:
            _conceal()
        elif user32 and _chrome_hwnd and user32.IsWindow(_chrome_hwnd):
            user32.ShowWindow(_chrome_hwnd, SW_HIDE)
    return status()


def hold_open(held):
    """Keep Chrome running while a Manage Listings job works, even if the user leaves the page.

    When the hold ends and no page owns the browser any more, it is closed.
    """
    global _held
    with _lock:
        _held = bool(held)
        orphaned = not _held and _account is not None and _owner is None
    if orphaned:
        stop(force=True)


def stop(*, force=False, owner=None):
    """Close the embedded browser. With owner, only if that page still owns it."""
    global _account, _account_lock, _chrome_hwnd, _generation, _needs_focus, _owner, _view
    with _lock:
        if _account is None:
            return status()
        if owner is not None and owner != _owner:
            # A page that already handed the browser to the next page is leaving.
            return status()
        if _held:
            # A job is still deleting listings: keep Chrome running out of sight.
            # hold_open(False) closes it once the job ends, unless a page owns it again.
            _owner = None
            _conceal()
            return status()
        if not force and active_tasks(_account):
            raise RuntimeError('This account has active automation. Wait for it to finish.')
        account, lock = _account, _account_lock
        _close_listings_session()
        _account = _account_lock = _chrome_hwnd = _owner = _view = None
        _needs_focus = False
        _generation += 1
        try:
            meta = cdp_login.get_cdp_account(account)
            if meta and cdp_login._port_open(meta.get('port')):
                try:
                    _save_cookies(account, _facebook_cookies(meta['port']))
                except Exception:
                    pass
            cdp_login.close_cdp_browser(account)
        finally:
            if lock:
                lock.release()
    return status()


def open_account(account, owner=None, view=None):
    global _account, _account_lock, _chrome_hwnd, _generation, _needs_focus, _owner, _view
    if os.name != 'nt' or os.getenv('FB_AUTOMATION_DESKTOP') != '1':
        raise RuntimeError('Account Browser is available in the Windows desktop app only.')
    account = str(account or '').strip()
    meta = cdp_login.get_cdp_account(account)
    if not meta or meta.get('status') not in {'ready', 'relogin_expired'}:
        raise ValueError('This account needs a saved Chrome login. Add or re-login to it first.')
    with _lock:
        if _held and not (_account == account and view == 'listings'):
            raise RuntimeError(f'Listings are being deleted or reposted for {_account}. Wait for it to finish, '
                               'or stop it on Manage Listings.')
        if _account == account:
            # A Chrome trimmed to the listings column is not reused for the full Account Browser.
            if (_chrome_hwnd and user32.IsWindow(_chrome_hwnd) and cdp_login._port_open(meta.get('port'))
                    and (_view != 'listings' or view == 'listings')):
                _owner, _view = owner, view
                return status()
            stop(force=True)
        if active_tasks(account):
            raise RuntimeError('This account is running automation. Wait for it to finish.')
        account_lock = cdp_login._account_locks.setdefault(account, threading.Lock())
        if not account_lock.acquire(blocking=False):
            raise RuntimeError('This account is being used by another operation. Wait for it to finish.')
        try:
            if _account:
                stop()
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            # Manage Listings starts blank: Facebook then loads once, straight on
            # "Your listings", with the listings-only view already registered.
            # Blank must be a data: URL: Chrome rejects --app=about:blank and opens a
            # normal window with tabs and an address bar instead of an app window.
            proc = cdp_login._launch_profile(meta, background=False, app_mode=True,
                                             start_url='data:text/html,' if view == 'listings' else None)
            deadline, hwnd = time.time() + 15, None
            while time.time() < deadline:
                if not hwnd:
                    hwnd = _window_for_pid(proc.pid)
                    if hwnd:
                        # Hide it the moment it exists (it opens off-screen anyway).
                        user32.ShowWindow(hwnd, SW_HIDE)
                if hwnd and cdp_login._port_open(meta['port']):
                    break
                time.sleep(0.05)
            if not hwnd:
                raise RuntimeError('Chrome did not create an account window.')
            host = user32.FindWindowW(None, 'KeyPost Locksmith')
            if not host:
                raise RuntimeError('The desktop app window is not available.')
            user32.ShowWindow(hwnd, SW_HIDE)
            style = user32.GetWindowLongW(hwnd, -16)
            user32.SetWindowLongW(hwnd, -16, (style | WS_CHILD) & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME))
            extended_style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, extended_style & ~WS_EX_FRAME_EDGES)
            if not user32.SetParent(hwnd, host):
                raise RuntimeError('Could not place Chrome inside the app window.')
            _account, _account_lock, _chrome_hwnd, _owner, _view = account, account_lock, hwnd, owner, view
            _needs_focus = True
            _generation += 1
            threading.Thread(target=_watch, args=(account, _generation), daemon=True).start()
            return status()
        except Exception:
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            account_lock.release()
            raise


FACEBOOK_HOME_URL = 'https://www.facebook.com/'
MANAGE_LISTINGS_URL = 'https://www.facebook.com/marketplace/you/selling'


def open_manage_listings(account, owner=None, extra_scripts=()):
    """Open the account embedded in the app on its Marketplace "Your listings" page."""
    global _needs_focus, _listings_session
    with _lock:
        open_account(account, owner=owner, view='listings')
        if _held and _listings_session and not _listings_session.closed:
            # Coming back while a delete is running: show the same page, don't reload it.
            _needs_focus = True
            return status()
        meta = cdp_login.get_cdp_account(account)
        _close_listings_session()
        page = _page_target(meta['port'])
        if not page or not page.get('webSocketDebuggerUrl'):
            raise RuntimeError('The account browser page is not available.')
        session = PageSession(page['webSocketDebuggerUrl'])
        try:
            # Chrome runs the registered scripts in every new document while this
            # connection stays open, so reloads keep the listings-only view
            # without polling the page. Registered scripts only run with Page enabled.
            session.call('Page.enable')
            for source in (LISTINGS_VIEW_JS, *extra_scripts):
                session.call('Page.addScriptToEvaluateOnNewDocument', {'source': source})
            session.call('Page.navigate', {'url': MANAGE_LISTINGS_URL})
        except Exception:
            session.close()
            raise
        _listings_session = session
        _needs_focus = True
        return status()


def return_home():
    """Stop the current Facebook destination by returning the open browser home."""
    global _needs_focus
    with _lock:
        if not _account:
            return status()
        meta = cdp_login.get_cdp_account(_account)
        if meta and cdp_login._port_open(meta.get('port')):
            driver = cdp_login._attach(meta['port'])
            try:
                driver.execute_cdp_cmd('Page.navigate', {'url': FACEBOOK_HOME_URL})
                _needs_focus = True
            finally:
                cdp_login._detach(driver)
        return status()


def layout(x, y, width, height, viewport_width=None, viewport_height=None):
    global _needs_focus
    with _lock:
        if not _chrome_hwnd or not user32.IsWindow(_chrome_hwnd):
            return status()
        host = user32.FindWindowW(None, 'KeyPost Locksmith')
        client = wintypes.RECT()
        if not host or not user32.GetClientRect(host, ctypes.byref(client)):
            raise RuntimeError('The app window is not available for browser sizing.')
        client_width = client.right - client.left
        client_height = client.bottom - client.top
        css_width = float(viewport_width or 0)
        css_height = float(viewport_height or 0)
        if css_width <= 0 or css_height <= 0:
            raise ValueError('Browser view size is not available.')
        scale_x = client_width / css_width
        scale_y = client_height / css_height
        left = max(0, min(client_width - 1, int(float(x) * scale_x)))
        top = max(0, min(client_height - 1, int(float(y) * scale_y)))
        wide = max(1, min(client_width - left, int(float(width) * scale_x)))
        high = max(1, min(client_height - top, int(float(height) * scale_y)))
        # Chrome app windows paint their own title strip and dark outer frame
        # inside the child window. Crop those pixels with a window region.
        edge_x = max(1, round(10 * scale_x))
        caption = max(1, round(28 * scale_y))
        edge_bottom = max(1, round(10 * scale_y))
        region = gdi32.CreateRectRgn(edge_x, caption, edge_x + wide, caption + high)
        if not region:
            raise RuntimeError('Could not prepare the Facebook viewport.')
        if not user32.SetWindowRgn(_chrome_hwnd, region, True):
            gdi32.DeleteObject(region)
            raise RuntimeError('Could not crop Chrome to the Facebook viewport.')
        if not user32.SetWindowPos(_chrome_hwnd, None, left - edge_x, top - caption,
                                   wide + 2 * edge_x, high + caption + edge_bottom,
                                   SWP_NOZORDER | SWP_FRAMECHANGED):
            raise RuntimeError(f'Could not size Chrome inside the app: {ctypes.WinError()}')
        user32.ShowWindow(_chrome_hwnd, SW_SHOW)
        if _needs_focus:
            _focus_browser(_chrome_hwnd, host)
            _needs_focus = False
    return status()

# Hides Facebook's top bar and both side rails on "Your listings", leaving the
# listings column, which scrolls with the page. Uses roles and layout, not class names.
LISTINGS_VIEW_JS = r'''
(() => {
  if (window.top !== window || window.__kpListingsView) return;
  window.__kpListingsView = true;
  const style = document.createElement('style');
  style.textContent = [
    'html[data-kp-listings] [role="banner"]{display:none!important}',
    'html[data-kp-listings] div:has(> [role="main"]) > [role="navigation"]{display:none!important}',
    'html[data-kp-listings] [data-kp-top]{top:0!important}',
    'html[data-kp-listings] [data-kp-hide]{display:none!important}',
    // Keep the page invisible until only "All listings" is left, so Facebook's
    // full layout never flashes up while it loads.
    'html[data-kp-listings]:not([data-kp-ready]) body{visibility:hidden!important}',
  ].join('\n');
  const started = Date.now();
  const REVEAL_ANYWAY_MS = 5000;  // never leave the page blank if Facebook looks different
  const visibleChildren = el => [...el.children].filter(c => c.getBoundingClientRect().width > 0);
  const reveal = () => document.documentElement.setAttribute('data-kp-ready', '');
  const tick = () => {
    // Registered scripts run before the document has an <html> element.
    if (!document.documentElement) return;
    if (!style.isConnected) (document.head || document.documentElement).appendChild(style);
    const on = /^\/marketplace\/(you\/)?selling/.test(location.pathname);
    document.documentElement.toggleAttribute('data-kp-listings', on);
    if (!on || Date.now() - started > REVEAL_ANYWAY_MS) reveal();
    if (!on) return;
    const main = document.querySelector('[role="main"]');
    if (!main) return;
    for (let el = main.parentElement; el && el !== document.body; el = el.parentElement) {
      const css = getComputedStyle(el);
      if (css.position === 'relative' && parseFloat(css.top) > 0) el.setAttribute('data-kp-top', '');
    }
    const queue = [[main, 0]];
    while (queue.length) {
      const [el, depth] = queue.shift();
      const kids = visibleChildren(el);
      if (kids.length === 2 && el.getBoundingClientRect().width >= 700) {
        const [col, rail] = kids.map(k => k.getBoundingClientRect());
        if (col.width >= 400 && rail.width >= 280 && rail.width <= 420 && rail.left >= col.right - 1 && rail.height > 200) {
          kids[1].setAttribute('data-kp-hide', '');
          break;
        }
      }
      if (depth < 8) kids.forEach(k => queue.push([k, depth + 1]));
    }
    if (onlyAllListings(main)) reveal();
  };
  // Inside the listings column show only the "All listings" section: hide
  // "Needs attention", the "Keep your listings up to date" banner, and any card
  // that repeats a listing already shown. Found by visible text first.
  const MORE = '[role="button"][aria-label^="More options for "]';
  const textEl = (root, re) => [...root.querySelectorAll('span, h2, h3')]
    .find(el => re.test(el.textContent.trim()) && ![...el.children].some(c => re.test(c.textContent.trim())));
  function onlyAllListings(main) {
    const allHead = textEl(main, /^All listings$/i);
    if (!allHead) return false;  // no "All listings" section yet: leave the page as Facebook shows it
    const keep = [allHead, main.querySelector('input')].filter(Boolean);  // the section and the search box
    const isolate = start => {
      let el = start;
      while (el.parentElement && el.parentElement !== main && !keep.some(k => el.parentElement.contains(k))) el = el.parentElement;
      return el;
    };
    for (const re of [/^Needs attention$/i, /^Keep your listings up to date$/i]) {
      const head = textEl(main, re);
      if (head && !head.contains(allHead)) isolate(head).setAttribute('data-kp-hide', '');
    }
    // A listing repeated in the list (same title and same photo) is shown once.
    const photoOf = img => (img.currentSrc || img.src || '').split('?')[0].split('/').pop();
    const oneListing = (el, label) => [...el.querySelectorAll(MORE)].every(b => b.getAttribute('aria-label') === label)
      && new Set([...el.querySelectorAll('img')].map(photoOf).filter(Boolean)).size <= 1;
    const seen = new Set(), cards = new Set();
    for (const button of main.querySelectorAll(MORE)) {
      const section = button.closest('[data-kp-hide]');
      if (section && !section.hasAttribute('data-kp-dup')) continue;
      const label = button.getAttribute('aria-label');
      let card = button;
      while (card.parentElement && card.parentElement !== main && oneListing(card.parentElement, label)) card = card.parentElement;
      if (cards.has(card)) continue;
      cards.add(card);
      const img = card.querySelector('img');
      const photo = img ? photoOf(img) : '';
      const sig = label + '\u001f' + photo;
      const repeat = !!photo && seen.has(sig);
      card.toggleAttribute('data-kp-hide', repeat);
      card.toggleAttribute('data-kp-dup', repeat);
      seen.add(sig);
    }
    return true;
  }
  // React to Facebook drawing the page instead of waiting for the next interval:
  // quickly while loading, then at a calm pace once the view is ready.
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    const ready = document.documentElement && document.documentElement.hasAttribute('data-kp-ready');
    setTimeout(() => { pending = false; tick(); }, ready ? 250 : 30);
  }).observe(document, {childList: true, subtree: true});
  setInterval(tick, 500);
  tick();
})();
'''
