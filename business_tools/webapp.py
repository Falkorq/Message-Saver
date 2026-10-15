"""Read-only Telegram Mini App served in the bot's event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque, OrderedDict
import sqlite3
import hashlib
import hmac
import json
import os
import ssl
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from .analytics import PERIODS
from .service import STAT_VIEWS
from .web_metrics import enrich_overview, enrich_detail, web_chats, period_totals
from .avatars import AvatarCache
from public_pages import render_public_page

SECTIONS = {'overview': 'Обзор', 'replies': 'Ответы', 'activity': 'Активность',
            'events': 'Удаления и правки', 'media': 'Медиа', 'words': 'Слова',
            'records': 'Рекорды', 'info': 'Об учёте'}


def validate_init_data(raw, token, now=None):
    """Check Telegram's signature and a bounded authentication lifetime."""
    try:
        pairs = parse_qsl(raw, strict_parsing=True)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            raise ValueError('duplicate fields')
        signature = fields.pop('hash')
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError('signature')
        age = (time.time() if now is None else now) - int(fields['auth_date'])
        if not -30 <= age <= 86400:
            raise ValueError('expired')
        actor = json.loads(fields['user'])['id']
        if type(actor) is not int or actor <= 0:
            raise ValueError('user')
        return actor
    except (ValueError, KeyError, TypeError):
        raise web.HTTPUnauthorized(text='Откройте приложение заново из бота.') from None


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.fragment
            or parsed.query or parsed.path not in ('', '/')):
        raise ValueError('WEBAPP_URL должен быть публичным HTTPS-адресом в корне домена')
    return url


def create_app(token, repository, service, routes, avatars=None, product=None, inbox=None, profile_module=None):
    assets = Path(__file__).with_name('web')
    avatars = avatars or AvatarCache(service.bot, repository.db)

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='statistics')
    slots = asyncio.Semaphore(2)
    limits = defaultdict(deque)
    cache = OrderedDict()
    database = next((r[2] for r in repository.db.execute('PRAGMA database_list') if r[1]=='main'), '')

    async def calculate(request, function, *args):
        owner = request['owner']
        repository.check_account(owner, args[1] if len(args)>1 else None)
        key = (owner, request.path_qs, repository.db.total_changes, tuple(routes.items()))
        erase_row=repository.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_state'").fetchone()
        erase_before=repository.db.execute('SELECT MAX(erased_through) FROM account_state WHERE owner_id=?',(owner,)).fetchone()[0] if erase_row else None
        cached = cache.get(key)
        if cached and time.monotonic()-cached[0] < 20:
            return cached[1]
        try:
            await asyncio.wait_for(slots.acquire(), .15)
        except TimeoutError:
            raise web.HTTPTooManyRequests(text='Статистика занята. Повторите через несколько секунд.', headers={'Retry-After':'3'})
        def run():
            if not database:  # Only tiny isolated in-memory fixtures; production always uses a file.
                return function(repository, *args)
            db = sqlite3.connect(Path(database).as_uri()+'?mode=ro', uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            deadline=time.monotonic()+8
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            try:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                from .repository import BusinessToolsRepository
                return function(BusinessToolsRepository(db, dict(routes)), *args)
            finally:
                db.close()
        deferred=False
        try:
            if database:
                future=asyncio.get_running_loop().run_in_executor(pool,run)
                try:data=await asyncio.shield(future)
                except asyncio.CancelledError:
                    deferred=True
                    future.add_done_callback(lambda done: (slots.release(),done.exception() if not done.cancelled() else None))
                    raise
            else:data=run()
        finally:
            if not deferred:slots.release()
        if routes.get(request['actor'])!=owner:
            raise web.HTTPForbidden(text='Приглашение больше не действует.')
        repository.check_account(owner, args[1] if len(args)>1 else None)
        erase_after=repository.db.execute('SELECT MAX(erased_through) FROM account_state WHERE owner_id=?',(owner,)).fetchone()[0] if erase_row else None
        if erase_after!=erase_before:raise web.HTTPConflict(text='Данные удалены. Обновите экран.')
        cache[key] = (time.monotonic(), data)
        while len(cache)>64: cache.popitem(last=False)
        return data

    @web.middleware
    async def protect(request, handler):
        try:
            if request.path.startswith('/api/'):
                raw = request.headers.get('Authorization', '')
                if not raw.startswith('tma '):
                    raise web.HTTPUnauthorized(text='Откройте приложение из Telegram.')
                actor = validate_init_data(raw[4:], token)
                if actor not in routes:
                    raise web.HTTPForbidden(text='Нет доступа к статистике.')
                request['actor'] = actor
                request['owner'] = routes[actor]
                bucket = (request['owner'], 'avatars' if request.path.startswith('/api/avatars/') else 'stats')
                recent = limits[bucket]
                now = time.monotonic()
                while recent and recent[0] < now-60: recent.popleft()
                if len(recent) >= (120 if bucket[1]=='avatars' else 40):
                    raise web.HTTPTooManyRequests(text='Слишком много запросов. Подождите минуту.', headers={'Retry-After':'60'})
                recent.append(now)
                if len(limits)>2048:
                    for old in list(limits):
                        if not limits[old] or limits[old][-1]<now-60: del limits[old]
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text, 'code': {401:'auth_expired',403:'invitation_required',404:'chat_unavailable',429:'rate_limit'}.get(exc.status,'request_error')}, status=exc.status, headers={'Retry-After':exc.headers['Retry-After']} if 'Retry-After' in exc.headers else None)
        except (ValueError, LookupError):
            response = web.json_response({'error': 'Недоступный чат или неверные параметры.','code':'chat_unavailable'}, status=400)
        except Exception:
            response = web.json_response({'error':'Сервис временно недоступен. Попробуйте позже или обратитесь в поддержку.', 'code':'server_error'}, status=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
        return response

    def filters(request):
        period = request.query.get('period', '30')
        if period not in PERIODS:
            raise ValueError('period')
        account = request.query.get('account')
        return period, int(account) if account else None

    def overview_data(repository, owner, account, period, offset, search, sort, pins, favorites):
        now = int(time.time())
        rows = web_chats(repository, owner, account, period, now)
        rows.sort(key=lambda r: r['last_at'] or 0, reverse=True)
        chats = [{k: r[k] for k in ('account_id', 'chat_id', 'display_name', 'username',
                  'total_messages', 'owner_messages', 'peer_messages', 'last_at')} for r in rows]
        for chat, row in zip(chats, rows):
            m = row['metrics']
            chat.update(account_name=service.account_labels.get(row['account_id'], str(row['account_id'])),
                        totals_basis=row['totals_basis'], undetailed=row['undetailed'],
                        voice=m['p_media_voice'], voice_seconds=m['p_voice_seconds'],
                        voice_missing=m['p_voice_missing'], deleted=m['p_deleted'], own_deleted=m['o_deleted'])
        result = {'accounts': [{'id': a, 'name': service.account_labels.get(a, str(a))}
                                  for a in repository.check_account(owner)], 'chats': chats,
                                  **enrich_overview(repository, owner, account, period, rows, now),
                                  'summary': {'messages': sum(r['total_messages'] for r in rows),
                                  'own': sum(r['owner_messages'] for r in rows),
                                  'peer': sum(r['peer_messages'] for r in rows), 'chats': len(rows)}}

        matches = [c for c in chats if search in (str(c['display_name'] or '')+' '+str(c['username'] or '')+' '+str(c['chat_id'])).casefold()]
        if favorites: matches=[c for c in matches if f"{c['account_id']}:{c['chat_id']}" in pins]
        metric={'messages':'total_messages','peer_messages':'peer_messages','voice':'voice','voice_seconds':'voice_seconds','deleted':'deleted','own_deleted':'own_deleted'}.get(sort,'last_at')
        matches.sort(key=lambda c: c.get(metric) or 0,reverse=True)
        matches.sort(key=lambda c:f"{c['account_id']}:{c['chat_id']}" in pins,reverse=True)
        result['top_chats']=sorted(chats,key=lambda c:c['total_messages'],reverse=True)[:5]
        result['chats'] = matches[offset:offset+50]
        result['pagination'] = dict(offset=offset,limit=50,total=len(matches),has_more=len(matches)>offset+50)
        result['generated_at'] = now
        return result

    async def overview(request):
        period, account = filters(request)
        offset = int(request.query.get('offset','0'))
        if not 0<=offset<=100000: raise ValueError('offset')
        search = request.query.get('q','').strip().casefold()
        if len(search)>100: raise ValueError('search')
        pins=request.query.get('pins','').split(',') if request.query.get('pins') else []
        if len(pins)>200 or any(not __import__('re').fullmatch(r'\d+:-?\d+',value) for value in pins):raise ValueError('pins')
        data = dict(await calculate(request, overview_data, request['owner'], account, period, offset, search, request.query.get('sort','recent'),pins,request.query.get('favorites')=='1'))
        data['bot_username'] = os.getenv('BOT_USERNAME','').lstrip('@')
        data['connection_states'] = product.states(request['owner']) if product else []
        starts=[row['since'] for row in data['connection_states'] if account is None or row['account_id']==account]
        if starts:data['coverage']=dict(data['coverage'],since=min(starts))
        return web.json_response(data)

    async def detail(request):
        period, account = filters(request)
        now = int(time.time())
        if account is None:
            raise ValueError('account')
        chat = int(request.match_info['chat'])
        section = request.query.get('section', 'overview')
        side = request.query.get('side', 'both')
        page = int(request.query.get('page', '0'))
        if section not in SECTIONS or side not in ('both', 'o', 'p') or not 0 <= page <= 10000:
            raise ValueError('filter')
        view = request.query.get('view') or None
        choices = STAT_VIEWS.get(section, {})
        if view and view not in choices:
            raise ValueError('view')
        data = await calculate(request, detail_data, request['owner'], account, chat, period, now, side, page, section)
        return web.json_response(data)

    def detail_data(repository, owner, account, chat, period, now, side, page, section):
        choices = STAT_VIEWS.get(section, {})
        data = repository.analytics(owner, account, chat, period, now=now, side=side, page=page, word_limit=50)
        if data is None:
            raise web.HTTPNotFound(text='Чат не найден.')
        metrics = data['metrics']
        row = data['legacy'][0]
        totals = period_totals(row, metrics, period, now)
        if side == 'both' and totals['complete_scope']:
            media_count = sum(bool(n) for n in json.loads(row['media_json']).values())
        else:
            sides = ('o', 'p') if side == 'both' else (side,)
            media_count = len({k[len(f'{s}_media_'):] for s in sides
                               for k, n in metrics.items() if n and k.startswith(f'{s}_media_')})
        own, peer = totals['own'], totals['peer']
        return {'name': service.chat_name(row), 'own': own, 'peer': peer,
                                  'account_name': service.account_labels.get(account, str(account)),
                                  **enrich_detail(repository, owner, account, chat, period, data, now=now, side=side, page=page),
                                  'days': data['activity_days'],
                                  'views': choices, 'word_count': data['word_count'], 'media_count': media_count,
                                  'sections': SECTIONS, 'page': page, 'generated_at':now}

    async def asset(request):
        page = {'/privacy':'privacy', '/privacy.html':'privacy', '/help':'help', '/terms':'terms'}.get(request.path)
        if page:
            return web.Response(text=render_public_page(page), content_type='text/html')
        filename = 'privacy.html' if request.path=='/privacy' else request.match_info.get('file', 'index.html')
        if filename not in ('privacy.html','index.html', 'app.js', 'material.js', 'style.css', 'public.css', 'nyan-icon.jpg', 'icons.svg',
                            'nunito-400.ttf', 'nunito-600.ttf', 'nunito-700.ttf'):
            raise web.HTTPNotFound()
        return web.FileResponse(assets / filename)

    async def avatar(request):
        account = int(request.query.get('account', '0'))
        chat = int(request.match_info['chat'])
        known = repository.stat(request['owner'], account, chat)
        if known is None:
            raise web.HTTPNotFound(text='Чат не найден.')
        result, image, mime = avatars.request(request['owner'], account, chat, known[0]['username'])
        if result == 'ready':
            return web.Response(body=image, content_type=mime)
        if result == 'none':
            return web.Response(status=204)
        return web.Response(status=202 if result == 'pending' else 429,
                            headers={'Retry-After': '3' if result == 'pending' else '10'})

    async def start_avatars(app):
        await avatars.start()

    async def close_avatars(app):
        await avatars.close()
        pool.shutdown(wait=False, cancel_futures=True)

    app = web.Application(middlewares=[protect], client_max_size=16384)
    app.on_startup.append(start_avatars)
    app.on_cleanup.append(close_avatars)
    async def health(request):
        # Detailed operational data is available only through authenticated /api/health.
        incoming = inbox.health() if inbox else {}
        out = repository.db.execute('SELECT COUNT(*),MIN(created_at),SUM(failed) FROM outbox').fetchone() if inbox else (0,None,0)
        progress = repository.db.execute("SELECT value FROM business_stats_progress WHERE name='backfill_complete'").fetchone()
        state = dict(incoming=incoming, outbox=dict(pending=out[0],failed=out[2] or 0,
                     oldest_seconds=int(time.time())-out[1] if out[1] else 0),
                     statistics_recovering=not bool(progress and progress[0]),
                     mtproto=getattr(profile_module,'state','disabled'))
        maintenance=repository.db.execute("SELECT value FROM business_stats_progress WHERE name='maintenance_at'").fetchone()
        state['maintenance_age']=int(time.time())-maintenance[0] if maintenance else None
        ready = out[0]<20000 and (not out[1] or time.time()-out[1]<300) and (maintenance is not None and time.time()-maintenance[0]<180) and incoming.get('poll_age') is not None and incoming['poll_age']<90 and incoming.get('attempts',0)<5 and incoming.get('oldest_seconds',0)<300
        if request.path == '/healthz':
            return web.json_response({'ready':ready},status=200 if ready else 503)
        if not profile_module or request['owner'] not in getattr(profile_module.access_policy,'operator_ids',set()):
            raise web.HTTPForbidden(text='Диагностика доступна владельцу.')
        return web.json_response(state)
    app.router.add_get('/healthz', health)
    app.router.add_get('/api/health', health)
    app.router.add_get('/api/overview', overview)
    app.router.add_get('/api/chats/{chat}', detail)
    app.router.add_get('/api/avatars/{chat}', avatar)
    app.router.add_get('/', asset)
    app.router.add_get('/{file}', asset)
    return app


def server_ssl_context(cert=None, key=None):
    if bool(cert) != bool(key):
        raise ValueError('Укажите оба пути: WEBAPP_SSL_CERT и WEBAPP_SSL_KEY')
    if not cert:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


async def start_server(token, repository, service, routes, host, port, cert=None, key=None, profile_module=None, product=None, inbox=None):
    context = server_ssl_context(cert, key)
    avatars = AvatarCache(service.bot, repository.db, profile_module)
    runner = web.AppRunner(create_app(token, repository, service, routes, avatars, product, inbox, profile_module))
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port, ssl_context=context).start()
    except Exception:
        await runner.cleanup()
        raise
    return runner
