"""Synthetic multi-account Mini App check; fixture requires --combined."""
import hashlib,hmac,json,time
from pathlib import Path
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright,expect

out=Path('outputs/combined-browser');out.mkdir(parents=True,exist_ok=True)
results=[];errors=[]
fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':100})}
secret=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    for width,theme in ((320,'light'),(375,'dark')):
        ctx=browser.new_context(viewport={'width':width,'height':812})
        page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('https://telegram.org/**',lambda route:route.abort())
        page.add_init_script('localStorage.setItem("stats-theme",'+json.dumps(theme)+');window.Telegram={WebApp:{initData:'+json.dumps(urlencode(fields))+',ready(){},expand(){},BackButton:{onClick(){},show(){},hide(){}},isVersionAtLeast(){return false;}}};')
        page.goto('http://127.0.0.1:8765');page.wait_for_load_state('networkidle')
        page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
        contact=page.locator('.chat-row').filter(has=page.get_by_text('Синтетический чат 0',exact=True))
        expect(contact).to_have_count(1)
        expect(contact).to_contain_text('Все ваши аккаунты · 2')
        assert page.evaluate('state.overview.summary.chats')==65
        assert page.evaluate('state.overview.summary.messages')==66
        contact.click()
        page.wait_for_function("() => state.detail?.account_ids?.length===2 && document.getElementById('content').getAttribute('aria-busy')==='false'")
        assert page.evaluate('state.detail.own+state.detail.peer')==2
        expect(page.locator('#chat-context')).to_contain_text('Все ваши аккаунты · 2')
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        page.screenshot(path=str(out/f'combined-{width}-{theme}.png'))
        for label in ('Ритм','Медиа','Изменения','Общение'):
            page.get_by_role('button',name=label,exact=True).click()
            page.wait_for_function("() => document.getElementById('content').getAttribute('aria-busy')==='false'")
            assert page.evaluate('state.detail.own+state.detail.peer')==2
        page.get_by_role('button',name='Действия с чатом',exact=True).click()
        page.get_by_role('button',name='Закрепить чат',exact=True).click()
        page.get_by_role('button',name='Вернуться к чатам',exact=True).click()
        page.wait_for_function("() => document.querySelectorAll('.chat-row').length>0 && document.getElementById('content').getAttribute('aria-busy')==='false'")
        page.locator('#account-scope').click()
        page.locator('#account-options').get_by_role('button',name='Личный',exact=True).click()
        page.wait_for_function("() => document.getElementById('account').value==='100' && document.getElementById('content').getAttribute('aria-busy')==='false'")
        page.get_by_role('button',name='Закреплённые',exact=True).click()
        page.wait_for_function("() => document.querySelectorAll('.chat-row').length===1")
        page.locator('.chat-row').click()
        page.wait_for_function("() => state.detail?.account_ids?.length===1 && document.getElementById('content').getAttribute('aria-busy')==='false'")
        assert page.evaluate('state.detail.own+state.detail.peer')==1
        results.append(dict(scenario=f'combined-tabs-filter-and-pins-{width}-{theme}',passed=True))
        ctx.close()
    browser.close()
assert not errors,errors
Path('outputs/combined-browser-results.json').write_text(json.dumps(dict(checks=results,js_errors=errors),indent=2),encoding='utf-8')
print('Combined accounts browser checks passed:',len(results))
