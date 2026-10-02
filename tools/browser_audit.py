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
        page=browser.new_page(viewport={'width':1440,'height':1100},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(os.environ.get('AUDIT_URL',f'http://127.0.0.1:{server.server_port}'),wait_until='networkidle')
        page.wait_for_function('window.__baxter?.ready')
        assert page.locator('[data-beat="scope"]').get_attribute('data-state') == 'pending'
        assert '10 jobs' in page.locator('#run-count').inner_text()
        assert page.locator('#tasks').is_hidden()
        assert page.locator('#content').is_hidden()
        assert page.evaluate("document.querySelector('#run').click(); document.querySelector('.queue').open && __baxter.running")
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        assert page.evaluate('Object.values(__baxter.states).every(s=>s==="passed")')
        assert page.locator('[data-beat="scope"]').get_attribute('data-state') == 'passed'
        assert page.locator('[data-beat="verify"]').get_attribute('data-active') == 'true'
        assert page.locator('#run-phase').inner_text() == 'Verified'
        assert page.locator('#status').inner_text() == ''
        assert page.locator('.report-preview').bounding_box()['y'] < page.locator('.queue').bounding_box()['y']
        assert page.locator('#tasks').is_hidden()
        assert page.locator('#content').is_hidden()
        page.locator('.queue > summary').click()
        assert page.locator('#tasks').is_visible()
        assert page.locator('.task').count() == 10
        page.locator('.queue > summary').click()
        page.locator('.inspector > summary').click()
        assert 'Team order report' in page.locator('#content').input_value()
        with page.expect_download() as dl:page.locator('#export').click()
        import json
        exported_bytes=Path(dl.value.path()).read_bytes()
        output=json.loads(exported_bytes)
        assert json.loads(output['files']['proof/totals.json'])['checkedOrders']==220
        # Disclosure only changes visibility, including exact receipt timestamps/proofs.
        page.locator('.queue > summary').click()
        page.locator('.queue > summary').click()
        page.locator('.inspector > summary').click()
        page.locator('.inspector > summary').click()
        with page.expect_download() as dl:page.locator('#export').click()
        assert Path(dl.value.path()).read_bytes() == exported_bytes
        page.locator('.proof-rail > details > summary').nth(1).click()
        page.locator('#break').click();page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts>0')
        assert page.evaluate('__baxter.states.schema')=='failed'
        assert page.evaluate('__baxter.states.package')=='blocked'
        assert page.locator('#report-preview').is_hidden()
        assert page.locator('#run-phase').inner_text() == 'Blocked'
        assert page.locator('#tasks').is_visible()
        (ROOT/'output'/'playwright').mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(ROOT/'output'/'playwright'/'failure.png'),full_page=True)
        page.locator('#reset').click();page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        assert page.evaluate('Object.values(__baxter.states).every(s=>s==="passed")')
        page.locator('#show-receipts').click()
        assert page.locator('.receipt').count()==10
        page.locator('#show-receipts').click()
        assert page.locator('.receipt').count()==10
        page.locator('#show-artifacts').click()
        # Attachment is a real input: upload a new CSV and verify changed output.
        page.locator('#input-file').set_input_files({'name':'small.csv','mimeType':'text/csv','buffer':b'id,team,amount,quantity\n1,research,12,3\n2,platform,5,2'})
        page.wait_for_function("document.querySelector('#input-name').textContent==='small.csv'")
        page.locator('#run').focus();page.keyboard.press('Enter')
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        with page.expect_download() as dl:page.locator('#export').click()
        small=json.loads(Path(dl.value.path()).read_text())
        assert json.loads(small['files']['proof/totals.json'])['checkedOrders']==2
        assert sum(row['revenue'] for row in json.loads(small['files']['output/summary.json']))==46
        page.locator('#input-file').set_input_files({'name':'large.csv','mimeType':'text/csv','buffer':b'x'*250001})
        assert '250 KB' in page.locator('#input-status').inner_text()
        page.locator('.proof-rail > details > summary').nth(2).click()
        page.locator('#definitions').fill('{broken')
        page.locator('#apply').click()
        assert page.locator('#task-error').inner_text()
        page.locator('#reset').click();page.locator('#run').click()
        page.wait_for_function('!__baxter.running && __baxter.receipts===10')
        page.locator('.proof-rail > details > summary').nth(1).click()
        page.locator('.proof-rail > details > summary').nth(2).click()
        page.locator('.inspector > summary').click()
        assert page.locator('#content').is_hidden()
        assert page.locator('#tasks').is_hidden()
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(ROOT/'examples/portfolio/preview.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        (ROOT/'output'/'playwright').mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(ROOT/'output'/'playwright'/'mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile overflow'
        assert page.locator('#report-preview table tbody tr').count()==4
        assert page.locator('.report-preview').bounding_box()['y'] < page.locator('.queue').bounding_box()['y']
        assert page.evaluate("getComputedStyle(document.querySelector('#run')).transitionDuration")=='0s'
        # A blocked asset produces a usable error, no working-looking run button.
        failed=browser.new_page()
        failed.route('**/data/workflow.json',lambda route:route.fulfill(status=503,body='unavailable'))
        failed.goto(f'http://127.0.0.1:{server.server_port}',wait_until='networkidle')
        assert 'Refresh to retry' in failed.locator('#status').inner_text()
        assert failed.locator('#run').is_disabled()
        assert not errors,errors
        print('PASS: worker proof, failure gating/recovery, CSV upload/rejection, keyboard run, repeated tabs, invalid graph, export, desktop/mobile390, reduced motion and load error; no page errors')
        browser.close()
finally: server.shutdown()
