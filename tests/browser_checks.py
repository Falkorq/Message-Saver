import json,hashlib,hmac,time
from pathlib import Path
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright, expect
out=Path('outputs/browser');out.mkdir(exist_ok=True,parents=True)
results=[];errors=[]
def auth(actor,expired=False):
 f={'auth_date':str(int(time.time())-(90000 if expired else 0)),'user':json.dumps({'id':actor}),'query_id':'synthetic'}
 secret=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest();f['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(f.items())).encode(),hashlib.sha256).hexdigest();return urlencode(f)
def check(page,name):
 assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'),name+' horizontal overflow'
 page.screenshot(path=str(out/(name+'.png')))
 results.append({'scenario':name,'passed':True})
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
 def page_for(actor=100,width=375,theme='light',expired=False,outside=False,reduced=False):
  ctx=browser.new_context(viewport={'width':width,'height':812},reduced_motion='reduce' if reduced else 'no-preference')
  page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  page.route('https://telegram.org/**',lambda route:route.abort())
  script="localStorage.setItem('stats-theme',"+json.dumps(theme)+");"
  if not outside:script+='window.Telegram={WebApp:{initData:'+json.dumps(auth(actor,expired))+',ready(){},expand(){},BackButton:{onClick(){},show(){},hide(){}},isVersionAtLeast(){return false;},showAlert(){},close(){}}};'
  page.add_init_script(script);page.goto('http://127.0.0.1:8765');page.wait_for_load_state('networkidle');page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
  return ctx,page
 for width in (320,375,430):
  for theme in ('light','dark'):
   ctx,page=page_for(width=width,theme=theme)
   assert page.locator('.chat-row').count()==50
   check(page,f'chats-{width}-{theme}')
   if width==375 and theme=='light':
    page.get_by_role('button',name='Ещё чаты',exact=True).click();page.wait_for_function("() => document.querySelectorAll('.chat-row').length===65")
    results.append({'scenario':'server-pagination-65-chats','passed':True})
    page.locator('.chat-row').first.click();page.wait_for_function("() => document.querySelector('.contact-header') && document.getElementById('content').getAttribute('aria-busy')==='false'")
    check(page,'chat-overview')
    for label in ('Ритм','Медиа','Изменения'):
     page.get_by_role('button',name=label,exact=True).click();expect(page.get_by_role('button',name=label,exact=True)).to_have_attribute('aria-pressed','true');page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'");check(page,'chat-'+{'Ритм':'rhythm','Медиа':'media','Изменения':'events'}[label])
    page.get_by_role('button',name='Действия с чатом',exact=True).click();page.get_by_role('button',name='Закрепить чат',exact=True).click();
    page.get_by_role('button',name='Вернуться к чатам',exact=True).click();page.wait_for_function("() => document.querySelector('input[type=search]')!==null")
    page.get_by_role('button',name='Закреплённые',exact=True).click();page.wait_for_function("() => document.querySelectorAll('.chat-row').length===1");check(page,'pinned-server-filter');page.get_by_role('button',name='Все',exact=True).click();page.wait_for_function("() => document.querySelectorAll('.chat-row').length===50");
    search=page.get_by_role('searchbox',name='Поиск по чатам');search.fill('Синтетический чат 64');page.wait_for_timeout(700);page.wait_for_load_state('networkidle')
    assert page.locator('.chat-row').count()==1;check(page,'server-search')
    page.get_by_role('searchbox',name='Поиск по чатам').fill('неттакогособеседника');page.wait_for_timeout(700);page.wait_for_load_state('networkidle');assert page.locator('.chat-row').count()==0;check(page,'search-empty')
    page.get_by_role('button',name='Очистить поиск',exact=True).click();page.wait_for_function("() => document.querySelectorAll('.chat-row').length===50")
    page.get_by_role('button',name='Действия и настройки',exact=True).click();assert page.get_by_role('button',name='Уведомления',exact=True).is_visible();check(page,'settings')
   ctx.close()
 for actor,name,text in [(500,'not-connected','Аккаунт ещё не подключён'),(600,'waiting-first-event','Ждём первое событие'),(700,'disconnected','Соединение отключено'),(999,'no-invitation','Нет доступа')]:
  ctx,page=page_for(actor);assert text in page.locator('body').inner_text(),name;check(page,name);ctx.close()
 ctx,page=page_for(expired=True);assert 'Откройте приложение заново' in page.locator('body').inner_text();assert page.get_by_role('button',name='Открыть заново из бота').is_visible();check(page,'expired-auth');ctx.close()
 ctx,page=page_for(outside=True);assert '/chatstats' in page.locator('body').inner_text();check(page,'outside-telegram');ctx.close()
 ctx,page=page_for(reduced=True);assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches");check(page,'reduced-motion')
 # Keep the last successful screen during a transient server error.
 page.route('**/api/overview?**',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'Временная ошибка сети','code':'server_error'})))
 page.evaluate('state.overviewAt=0;load()');page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
 assert page.locator('.chat-row').count()>0;assert 'сохранённые данные' in page.locator('#status').inner_text();check(page,'temporary-error-keeps-data');ctx.close()
 ctx,page=page_for(600)
 page.route('**/api/overview?**',lambda route:(time.sleep(.5),route.continue_()))
 page.evaluate('() => {state.overviewAt=0;void load();}');page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='true'");check(page,'slow-network-loading');page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'");ctx.close()
 # Visibility return causes one refresh, without recurring polling.
 ctx,page=page_for(600);requests=[]
 page.on('request',lambda request:requests.append(request.url) if '/api/overview?' in request.url else None)
 page.evaluate("() => {Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));backgroundAt=Date.now()-30000;Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));}")
 page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
 assert len(requests)==1;check(page,'return-from-background');ctx.close()
 ctx,page=page_for(600)
 page.evaluate("() => {window.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true}));}")
 page.evaluate('() => {state.overviewAt=0;void load();}')
 page.wait_for_function("() => document.getElementById('status').textContent.includes('Ответ задерживается')",timeout=20000)
 check(page,'api-timeout');page.unroute_all(behavior='ignoreErrors');ctx.close()
 ctx,page=page_for(600);response=page.request.get('http://127.0.0.1:8765/privacy');assert response.status==200;ctx.close()
 results.append({'scenario':'privacy-without-auth','passed':True})
 browser.close()
assert not errors,errors
Path('outputs/browser-results.json').write_text(json.dumps({'checks':results,'js_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print('Browser checks passed:',len(results),'JS errors:',len(errors))
