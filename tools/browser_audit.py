"""Exercise actual worker jobs, verification failures and recovery locally or against its public deployment."""
import functools
import http.server
import os
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(ROOT/'examples/portfolio')))
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(**({'channel':'chrome'} if os.name=='nt' else {}))
        page=browser.new_page(viewport={'width':1280,'height':1000},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(os.environ.get('AUDIT_URL',f'http://127.0.0.1:{server.server_port}'),wait_until='networkidle')
        page.wait_for_function('window.__baxter?.ready')
        page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        assert page.evaluate('Object.values(__baxter.states).every(s=>s==="passed")')
        assert 'Team order report' in page.locator('#content').input_value()
        page.locator('details summary').first.click()
        with page.expect_download() as dl:page.locator('#export').click()
        import json
        output=json.loads(Path(dl.value.path()).read_text())
        assert json.loads(output['files']['proof/totals.json'])['checkedOrders']==220
        page.locator('details summary').nth(1).click()
        page.locator('#break').click();page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts>0')
        assert page.evaluate('__baxter.states.schema')=='failed'
        assert page.evaluate('__baxter.states.package')=='blocked'
        page.locator('#reset').click();page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        assert page.evaluate('Object.values(__baxter.states).every(s=>s==="passed")')
        page.locator('#show-receipts').click()
        assert page.locator('.receipt').count()==10
        page.locator('#show-artifacts').click()
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(ROOT/'examples/portfolio/preview.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile overflow'
        assert page.locator('#report-preview table tbody tr').count()==4
        assert not errors,errors
        print('PASS: real worker execution, generated report, independent proof, failure gating, recovery and export')
        browser.close()
finally: server.shutdown()
