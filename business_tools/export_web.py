"""Authenticated upload, preview and cooperative statistics import jobs."""
import asyncio
from itertools import islice
import secrets
import time

from aiohttp import web
from .chat_export import ExportError, inspect_export, records, MAX_BYTES
from .export_import import EncryptedUpload, import_batch


def register_import(app, repository, routes, product, invalidate):
    jobs = {}
    tasks = set()

    def state(account):
        row = repository.db.execute('SELECT collecting,erased_through FROM account_state WHERE account_id=?',
                                    (account,)).fetchone()
        if not row or not row['collecting']:
            raise ExportError('Сохранение остановлено. Возобновите его в «Мои данные» перед импортом.')
        return row['erased_through']

    def available():
        if product is None:
            raise web.HTTPServiceUnavailable(text='Импорт пока недоступен.')

    def allowed(job):
        return (routes.get(job['actor'])==job['owner'] and job['account'] in repository.check_account(job['owner'])
                and state(job['account'])==job['barrier'])

    def close_upload(job):
        upload = job.pop('upload', None)
        if upload:
            upload.close()

    def clean():
        now = time.monotonic()
        for key, job in list(jobs.items()):
            try:
                valid = allowed(job)
            except ExportError:
                valid = False
            if (now>job['expires'] and job['state']!='importing') or not valid:
                job['cancel'] = True
                if job['state'] not in ('reading','importing'):
                    close_upload(job)
                    jobs.pop(key,None)
        finished = sorted((j for j in jobs.values() if j['state'] in ('done','stopped','error')),
                          key=lambda j:j['created'])
        for job in finished[:max(0,len(jobs)-64)]:
            jobs.pop(job['id'],None)

    def public(job):
        return {key:job[key] for key in ('id','state','account','preview','processed','added','skipped','error') if key in job}

    def find(request):
        available()
        clean()
        job = jobs.get(request.match_info['job'])
        if not job or job['owner'] != request['owner']:
            raise web.HTTPNotFound(text='Импорт не найден или истёк. Выберите файл заново.')
        return job

    async def preview(request):
        available()
        clean()
        try:
            account = int(request.query.get('account','0'))
            repository.check_account(request['owner'],account)
            barrier = state(account)
            if any(j['owner']==request['owner'] and j['state'] in ('reading','preview','importing') for j in jobs.values()):
                raise ExportError('Предыдущий импорт ещё открыт. Завершите его или отмените.')
            if sum('upload' in j for j in jobs.values()) >= 2:
                raise web.HTTPTooManyRequests(text='Импорт занят. Попробуйте через несколько минут.')
            if request.content_length and request.content_length > MAX_BYTES:
                raise ExportError('Максимальный размер экспорта — 128 МБ.')
            key = secrets.token_urlsafe(24)
            job = dict(id=key,state='reading',actor=request['actor'],owner=request['owner'],account=account,
                       barrier=barrier,created=time.monotonic(),expires=time.monotonic()+600,upload=EncryptedUpload(),cancel=False,
                       processed=0,added=0,skipped=0)
            jobs[key] = job
            try:
                try:
                    async with asyncio.timeout(180):
                        async for chunk in request.content.iter_chunked(262144):
                            if job['cancel']:
                                raise ExportError('Время загрузки истекло. Выберите файл заново.')
                            job['upload'].write(chunk)
                except TimeoutError:
                    raise ExportError('Загрузка заняла слишком долго. Выберите файл заново.') from None
                worker = asyncio.create_task(asyncio.to_thread(inspect_export,job['upload'].reader(),account))
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Do not close a file that a parser thread is still reading.
                    await worker
                    raise
                if job['cancel'] or not allowed(job):
                    raise ExportError('Доступ или состояние аккаунта изменились. Выберите файл заново.')
                job.update(state='preview',preview=result,expires=time.monotonic()+600)
                return web.json_response(public(job))
            except BaseException:
                close_upload(job)
                jobs.pop(key,None)
                raise
        except ExportError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None

    async def run(job):
        iterator = records(job['upload'].reader(),job['preview'])
        try:
            while True:
                worker = asyncio.create_task(asyncio.to_thread(lambda:list(islice(iterator,20))))
                try:
                    batch = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    await worker
                    raise
                if not allowed(job) or job['cancel']:
                    raise ExportError('Импорт остановлен. Уже добавленные сообщения повторно не посчитаются при новой загрузке.')
                if not batch:
                    break
                # No await in this small transaction: receiver, erasure and import
                # use the same connection and cannot interleave database writes.
                added, skipped = import_batch(repository,job['owner'],job['account'],batch)
                job['added'] += added
                job['skipped'] += skipped
                job['processed'] += len(batch)
                await asyncio.sleep(0)
            job['state'] = 'done'
        except asyncio.CancelledError:
            job.update(state='stopped',error='Импорт прерван перезапуском. Повторная загрузка пропустит уже учтённые сообщения.')
            raise
        except (ExportError,LookupError) as exc:
            job.update(state='stopped',error=str(exc) if isinstance(exc,ExportError) else 'Доступ к аккаунту изменился. Импорт остановлен.')
        except Exception:
            job.update(state='error',error='Не удалось завершить импорт. Повторная загрузка пропустит уже учтённые сообщения.')
        finally:
            close_upload(job)
            job['expires'] = time.monotonic()+600
            invalidate()

    async def start(request):
        job = find(request)
        if job['state'] == 'preview':
            try:
                if not allowed(job) or job['cancel']:
                    raise ExportError('Состояние аккаунта изменилось. Выберите файл заново.')
                if any(j['state']=='importing' for j in jobs.values()):
                    raise web.HTTPTooManyRequests(text='Другой импорт ещё обрабатывается. Повторите позже.')
            except ExportError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from None
            job['state'] = 'importing'
            task = asyncio.create_task(run(job))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return web.json_response(public(job))

    async def current(request):
        available()
        clean()
        found = [j for j in jobs.values() if j['owner']==request['owner']]
        return web.json_response(public(max(found,key=lambda j:j['created'])) if found else {})

    async def cancel(request):
        job = find(request)
        job['cancel'] = True
        if job['state'] not in ('reading','importing'):
            close_upload(job)
            jobs.pop(job['id'],None)
        return web.json_response({'cancelled':True})

    async def sweep():
        while True:
            await asyncio.sleep(10)
            clean()

    async def startup(app):
        app[sweeper_key] = asyncio.create_task(sweep())

    async def cleanup(app):
        app[sweeper_key].cancel()
        for job in jobs.values():
            job['cancel'] = True
        # Let an in-flight parser thread finish before closing its source.
        await asyncio.gather(*tasks,return_exceptions=True)
        await asyncio.gather(app[sweeper_key],return_exceptions=True)
        for job in jobs.values():
            close_upload(job)
        jobs.clear()

    sweeper_key = web.AppKey('import_sweeper',asyncio.Task)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_post('/api/import/preview',preview)
    app.router.add_get('/api/import',current)
    app.router.add_post('/api/import/{job}/start',start)
    app.router.add_delete('/api/import/{job}',cancel)
