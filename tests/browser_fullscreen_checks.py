import hashlib,hmac,json,time
from pathlib import Path
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

out=Path('outputs');out.mkdir(exist_ok=True)
fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':100}),'query_id':'synthetic'}
secret=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
auth=urlencode(fields)
checks=[];errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    for width,height,theme in [(320,712,'light'),(375,812,'dark'),(430,932,'light'),(812,375,'dark')]:
        landscape=width>height
        safe={'top':0 if landscape else 47,'bottom':21 if landscape else 34,'left':44 if landscape else 0,'right':44 if landscape else 0}
        content={'top':32 if landscape else 56,'bottom':0,'left':0,'right':0}
        ctx=browser.new_context(viewport={'width':width,'height':height},has_touch=True,is_mobile=True)
        page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('https://telegram.org/**',lambda r:r.abort())
        script='''window.events={};localStorage.setItem('stats-theme',THEME);
        window.Telegram={WebApp:{initData:AUTH,safeAreaInset:SAFE,contentSafeAreaInset:CONTENT,
        ready(){},expand(){},onEvent(name,callback){events[name]=callback},
        BackButton:{onClick(){},show(){},hide(){}},isVersionAtLeast(){return false},showAlert(){},close(){}}};'''
        for a,b in [('THEME',json.dumps(theme)),('AUTH',json.dumps(auth)),('SAFE',json.dumps(safe)),('CONTENT',json.dumps(content))]:script=script.replace(a,b)
        page.add_init_script(script)
        page.goto('http://127.0.0.1:8765');page.wait_for_load_state('networkidle')
        page.wait_for_function("document.querySelectorAll('.chat-row').length===50")
        bounds=page.evaluate('''() => {
          const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right}};
          return {brand:rect('.brand'),nav:rect('.bottom-nav'),button:rect('.bottom-nav button'),
          overflow:document.documentElement.scrollWidth>innerWidth,
          padding:parseFloat(getComputedStyle(document.querySelector('main')).paddingTop)};
        }''')
        assert bounds['padding']==safe['top']+content['top'],bounds
        assert bounds['brand']['top']>=safe['top']+content['top'],bounds
        assert bounds['brand']['left']>=safe['left']+16,bounds
        assert bounds['button']['bottom']<=height-safe['bottom'],bounds
        assert not bounds['overflow'],bounds
        page.locator('.bottom-nav button').first.tap()
        assert page.locator('.bottom-nav button md-ripple').first.evaluate("e=>getComputedStyle(e).display")=='none'
        page.locator('.bottom-nav button').first.focus()
        page.keyboard.press('Tab')
        assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle")=='solid'
        page.locator('.brand').click()
        page.screenshot(path=str(out/f'fullscreen-{width}-{theme}.png'))
        page.locator('#more-tab').tap()
        sheet=page.locator('#action-sheet').bounding_box()
        assert sheet['y']>=safe['top']+content['top'],sheet
        page.locator('#action-sheet .sheet-close').tap()
        page.evaluate("Telegram.WebApp.safeAreaInset.top=10;Telegram.WebApp.contentSafeAreaInset.top=20;events.safeAreaChanged();events.contentSafeAreaChanged();events.fullscreenChanged();")
        assert page.locator('main').evaluate("e=>parseFloat(getComputedStyle(e).paddingTop)")==30
        page.evaluate("Telegram.WebApp.safeAreaInset.top=0;Telegram.WebApp.contentSafeAreaInset.top=0;events.fullscreenChanged();")
        assert page.locator('main').evaluate("e=>parseFloat(getComputedStyle(e).paddingTop)")==0
        page.evaluate("insets => {Telegram.WebApp.safeAreaInset=insets.safe;Telegram.WebApp.contentSafeAreaInset=insets.content;events.safeAreaChanged();}", {'safe':safe,'content':content})
        page.locator('.chat-row').first.click()
        page.wait_for_function("() => document.querySelector('.contact-header') && document.querySelector('#content').getAttribute('aria-busy')==='false'")
        page.evaluate("window.scrollTo(0,420)")
        page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        protected=page.evaluate("""() => {
            const mask=getComputedStyle(document.body,'::before');
            const header=document.querySelector('#chat-context').getBoundingClientRect();
            const tabs=document.querySelector('.detail-navigation').getBoundingClientRect();
            return {maskHeight:parseFloat(mask.height), maskColor:mask.backgroundColor, maskZ:Number(mask.zIndex), headerTop:header.top, tabsTop:tabs.top, headerBottom:header.bottom};
        }""")
        assert protected['maskHeight']==safe['top']+content['top'],protected
        assert protected['headerTop']>=safe['top']+content['top']-1,protected
        assert protected['tabsTop']>=protected['headerBottom']-1,protected
        assert protected['maskZ']>11,protected
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), 'scrolled detail horizontal overflow'
        page.evaluate("() => Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})))")
        page.screenshot(path=str(out/f'fullscreen-detail-scrolled-{width}-{theme}.png'))
        checks.append({'scrolled_detail':protected,'viewport':[width,height],'theme':theme,'passed':True,'checks':['system + Telegram top','bottom home indicator','landscape sides','no horizontal overflow','no dock ripple','keyboard focus','sheet clearance','live inset updates','exit fullscreen']})
        ctx.close()
    browser.close()
assert not errors,errors
(out/'fullscreen-results.json').write_text(json.dumps({'checks':checks,'js_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Fullscreen: {len(checks)} viewports, safe-area and scrolled-detail checks passed, no JS errors')
