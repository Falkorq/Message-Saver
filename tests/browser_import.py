"""Synthetic browser coverage; the fixture server is started by with_server.py."""
import hashlib
import hmac
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright, expect

out=Path('outputs/import-browser');out.mkdir(parents=True,exist_ok=True)
checks=[];errors=[]
fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':100})}
secret=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
auth=urlencode(fields)

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    for number,(width,theme) in enumerate(((320,'light'),(375,'dark'),(430,'light'))):
        context=browser.new_context(viewport={'width':width,'height':812})
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('https://telegram.org/**',lambda route:route.abort())
        page.add_init_script('localStorage.setItem("stats-theme",'+json.dumps(theme)+');window.Telegram={WebApp:{initData:'+json.dumps(auth)+',ready(){},expand(){},BackButton:{onClick(){},show(){},hide(){}},isVersionAtLeast(){return false;}}};')
        page.goto('http://127.0.0.1:8765');page.wait_for_load_state('networkidle')
        page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
        page.locator('#app-menu').click();page.locator('#import-open').click()
        if number:
            expect(page.locator('#import-another')).to_be_visible()
            page.locator('#import-another').click()
        expect(page.locator('#import-form')).to_be_visible()
        data=dict(name='Импортированный чат '+str(number),type='personal_chat',id=9000+number,
                  messages=[dict(id=n+1,type='message',date_unixtime=str(int(time.time())-86400+n),
                                 from_id='user100' if n%2 else 'user200',text='synthetic <script>not executable</script>') for n in range(10)])
        payload=dict(name='result.json',mimeType='application/json',buffer=json.dumps(data).encode())
        page.locator('#import-file').set_input_files(payload)
        page.locator('#import-upload').click()
        expect(page.locator('#import-start')).to_be_visible(timeout=15000)
        expect(page.locator('#import-form')).to_be_hidden()
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        assert page.locator('#import-sheet').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        assert 'not executable' not in page.locator('#import-preview').inner_text()
        page.screenshot(path=str(out/f'preview-{width}-{theme}.png'))
        checks.append({'scenario':f'preview-{width}-{theme}','passed':True})
        page.locator('#import-start').click()
        expect(page.locator('#import-progress')).to_contain_text('Готово.',timeout=15000)
        expect(page.locator('#import-progress')).to_contain_text('Добавлено 10')
        page.screenshot(path=str(out/f'done-{width}-{theme}.png'))
        checks.append({'scenario':f'confirmed-{width}-{theme}','passed':True})
        if number==0:
            page.locator('#import-another').click()
            expect(page.locator('#import-form')).to_be_visible()
            page.locator('#import-file').set_input_files(payload)
            page.locator('#import-upload').click()
            expect(page.locator('#import-start')).to_be_visible(timeout=15000)
            page.locator('#import-start').click()
            expect(page.locator('#import-progress')).to_contain_text('Готово.',timeout=15000)
            expect(page.locator('#import-progress')).to_contain_text('Добавлено 0')
            expect(page.locator('#import-progress')).to_contain_text('уже были учтены: 10')
            checks.append({'scenario':'repeat-upload-no-duplicates','passed':True})
        page.locator('#import-result').click()
        expect(page.locator('#import-sheet')).not_to_be_visible()
        page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
        expect(page.locator('#chat-context')).to_contain_text(data['name'])
        expect(page.locator('#period-scope')).to_contain_text('Всё время')
        checks.append({'scenario':f'open-imported-statistics-{width}','passed':True})
        context.close()
    browser.close()
assert not errors,errors
Path('outputs/import-browser-results.json').write_text(json.dumps(dict(checks=checks,js_errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(checks=len(checks),js_errors=errors)))
