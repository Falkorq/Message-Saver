"""Simulate missing iOS 16 abort APIs in Chromium, using synthetic accounts only."""
import hashlib,hmac,json,time
from pathlib import Path
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright


def signed(actor):
    fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':actor}),'query_id':'synthetic-ios'}
    secret=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


checks=[];errors=[]
Path('outputs/legacy-ios').mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    for actor in (100,300,999):
        ctx=browser.new_context(viewport={'width':375,'height':812},is_mobile=True,has_touch=True,
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_11 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148')
        page=ctx.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('https://telegram.org/**',lambda route:route.abort())
        page.add_init_script("Object.defineProperty(AbortSignal,'any',{value:undefined,configurable:true});Object.defineProperty(AbortSignal,'timeout',{value:undefined,configurable:true});window.Telegram={WebApp:{initData:"+json.dumps(signed(actor))+",ready(){},expand(){},BackButton:{onClick(){},show(){},hide(){}},isVersionAtLeast(){return false},close(){},showAlert(){}}};")
        with page.expect_response(lambda response:'/api/overview?' in response.url) as awaited:
            page.goto('http://127.0.0.1:8765')
        response=awaited.value
        page.wait_for_load_state('networkidle')
        assert page.evaluate('AbortSignal.any===undefined && AbortSignal.timeout===undefined')
        data=response.json()
        if actor==999:
            assert response.status==403
            assert page.get_by_role('button',name='Получить приглашение').count()==1
        else:
            assert response.status==200
            assert {a['id'] for a in data['accounts']}=={actor}
            assert 'Не удалось подключиться' not in page.locator('#status').inner_text()
            if actor==100:assert page.locator('.chat-row').count()==50
        page.screenshot(path=f'outputs/legacy-ios/account-{actor}.png')
        checks.append({'scenario':'unsupported-abort-apis-account-'+str(actor),'passed':True})
        if actor==100:
            # Timeout still covers a response body that stalls after HTTP headers arrive.
            page.evaluate("""() => {
                const original=requestDeadline;
                requestDeadline=parent=>original(parent,60);
                window.fetch=(url,options)=>Promise.resolve({ok:true,status:200,headers:new Headers(),json:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true}))});
                state.overviewAt=0;void load();
            }""")
            page.wait_for_function("() => document.getElementById('status').textContent.includes('Ответ задерживается')")
            checks.append({'scenario':'legacy-timeout-in-response-body','passed':True})
            # Navigation cancellation propagates without turning into a connectivity error.
            canceled=page.evaluate("""async () => {
                window.fetch=(url,options)=>new Promise((resolve,reject)=>{
                    if(options.signal.aborted)reject(new DOMException('Aborted','AbortError'));
                    else options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
                });
                const controller=new AbortController();
                const task=api('/synthetic',new URLSearchParams(),controller.signal).catch(error=>error.name);
                controller.abort();return await task;
            }""")
            assert canceled=='AbortError',canceled
            checks.append({'scenario':'legacy-navigation-cancellation','passed':True})
        ctx.close()
    browser.close()
assert not errors,errors
Path('outputs/legacy-ios-results.json').write_text(json.dumps({'checks':checks,'js_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Legacy iOS API simulation: {len(checks)} checks passed; no JS errors')
