import asyncio
import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit

from aiohttp import web

SESSION_COOKIE = 'session'
SESSION_LIFETIME = 30 * 24 * 3600
CALL_LINK_LIFETIME = 7 * 24 * 3600
# Served to anyone: the login page, and the two assets the call-link page is built from.
PUBLIC_PATHS = {'/login', '/app.css', '/call-view.js'}
# All a call link's token opens: that one call's page and its own API routes.
CALL_LINK_PATH = re.compile(r'/c/(?P<page_id>[^/]+)|/api/calls/(?P<api_id>[^/]+)(/audio|/read)?')
# Seconds a wrong password costs: a cheap brake on guessing.
# ponytail: no lockout or per-IP limit. Add one if this is ever exposed without a long random password.
LOGIN_FAILURE_DELAY = 1.0


def session_secret(password):
    """Signing key derived from the dashboard password: changing the password logs everyone out."""
    return hashlib.sha256(b'voice-agent-session:' + password.encode()).digest()


def _signature(secret, subject, expires):
    return hmac.new(secret, f'{subject}|{expires}'.encode(), hashlib.sha256).hexdigest()


def make_token(secret, subject, lifetime, now=None):
    """A stateless, expiring proof of access to `subject`: '<expiry>.<hmac>'."""
    expires = int(time.time() if now is None else now) + int(lifetime)
    return f'{expires}.{_signature(secret, subject, expires)}'


def token_is_valid(secret, subject, token, now=None):
    try:
        expires, signature = token.split('.')
        expires = int(expires)
    except (AttributeError, ValueError):
        return False
    if expires < (time.time() if now is None else now):
        return False
    # Compared as bytes: compare_digest() raises on non-ASCII str, and the token is attacker-supplied.
    return hmac.compare_digest(signature.encode(), _signature(secret, subject, expires).encode())


def make_call_link(secret, base_url, call_id):
    """A URL that opens one call for a week without logging in — what the Telegram button points at."""
    return f'{base_url}/c/{call_id}?t={make_token(secret, f"call:{call_id}", CALL_LINK_LIFETIME)}'


def _has_call_link(request, secret):
    match = CALL_LINK_PATH.fullmatch(request.path)
    if not match:
        return False
    return token_is_valid(secret, f"call:{match['page_id'] or match['api_id']}", request.query.get('t'))


def _is_https(request):
    return request.secure or request.headers.get('X-Forwarded-Proto') == 'https'


def _same_origin(request):
    # Browsers always send Origin on a WebSocket handshake; a non-browser client still needs the cookie.
    origin = request.headers.get('Origin')
    return origin is None or urlsplit(origin).netloc == request.host


def setup_auth(app, password, login_page):
    """Puts everything behind one password: pages redirect to /login, the API and /ws answer 401."""
    secret = session_secret(password)

    @web.middleware
    async def require_session(request, handler):
        allowed = (
            request.path in PUBLIC_PATHS
            or token_is_valid(secret, 'session', request.cookies.get(SESSION_COOKIE))
            or _has_call_link(request, secret)
        )
        if not allowed:
            if request.path.startswith('/api/') or request.path == '/ws':
                raise web.HTTPUnauthorized()
            raise web.HTTPFound('/login')
        # The cookie alone doesn't stop another site's page from opening our socket in the owner's browser.
        if request.path == '/ws' and not _same_origin(request):
            raise web.HTTPForbidden()
        return await handler(request)

    async def show_login(request):
        return web.FileResponse(login_page)

    async def login(request):
        form = await request.post()
        if not hmac.compare_digest(str(form.get('password', '')).encode(), password.encode()):
            await asyncio.sleep(LOGIN_FAILURE_DELAY)
            raise web.HTTPFound('/login?error=1')
        response = web.HTTPFound('/')
        response.set_cookie(
            SESSION_COOKIE,
            make_token(secret, 'session', SESSION_LIFETIME),
            max_age=SESSION_LIFETIME,
            httponly=True,
            samesite='Strict',
            secure=_is_https(request),
        )
        raise response

    async def logout(request):
        response = web.HTTPFound('/login')
        response.del_cookie(SESSION_COOKIE)
        raise response

    app.middlewares.append(require_session)
    app.router.add_get('/login', show_login)
    app.router.add_post('/login', login)
    app.router.add_post('/logout', logout)
