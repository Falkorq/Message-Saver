"""Run against the local synthetic browser fixture; no credentials or Telegram session."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

out=Path('outputs/public-pages')
out.mkdir(parents=True,exist_ok=True)
checks=[]
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    for width in (320,375,812):
        ctx=browser.new_context(viewport={'width':width,'height':900},has_touch=True)
        page=ctx.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        requested=[]
        page.on('request',lambda request:requested.append(request.url))
        for route,title in [('help','Помощь с Nyan'),('privacy','Данные и приватность'),('terms','Условия использования')]:
            response=page.goto('http://127.0.0.1:8765/'+route)
            page.wait_for_load_state('networkidle')
            assert response.status==200
            assert page.locator('h1').inner_text()==title
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            assert page.locator('nav [aria-current=page]').count()==1
            assert page.locator('script').count()==0
            assert page.locator('section').count()>=5
            page.screenshot(path=str(out/f'{route}-{width}.png'))
            page.locator('summary').click()
            assert page.locator('details').get_attribute('open') is not None
            anchors=page.locator('.contents a').evaluate_all('(links)=>links.map(a=>a.getAttribute("href").slice(1))')
            assert all(page.locator('[id="'+anchor+'"]').count()==1 for anchor in anchors)
            page.locator('.contents a').last.click()
            page.keyboard.press('Tab')
            assert page.evaluate('getComputedStyle(document.activeElement).outlineStyle')=='solid'
            checks.append({'route':route,'width':width,'passed':True})
        assert not any('/api/' in url or 'telegram.org/js/' in url for url in requested)
        assert not ctx.cookies()
        ctx.close()
    browser.close()
assert not errors,errors
Path('outputs/public-pages-results.json').write_text(json.dumps({'checks':checks,'js_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(f'Public pages: {len(checks)} page/viewport combinations passed; no JS errors, API requests or cookies')
