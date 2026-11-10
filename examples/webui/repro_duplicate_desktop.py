#!/usr/bin/env python3
"""Actual Chromium duplicate and XeLab desktop; Core adapter is synthetic."""
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / 'examples/webui'))
from test_browser import DesktopBrowser, preferences, SHELL, CORE, BASE

state = {'refresh_calls': 0, 'replay_count': 0, 'revoked': False, 'protected_401': 0}
with tempfile.TemporaryDirectory(prefix='xelab-desktop-duplicate-') as temp:
    root = Path(temp)
    extension = root / 'extension'
    extension.mkdir()
    (extension / 'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Disposable tab repro', 'version': '1.0', 'permissions': ['tabs'], 'background': {'service_worker': 'background.js'}}))
    (extension / 'background.js').write_text('// Disposable tab probe.\n')
    (extension / 'page.html').write_text('<!doctype html><title>fixture extension</title>')
    with sync_playwright() as driver:
        context = driver.chromium.launch_persistent_context(str(root / 'profile'), executable_path=os.environ['XELAB_OAUTH_BROWSER_CHROMIUM'], headless=True, args=['--no-sandbox', '--disable-dev-shm-usage', f'--disable-extensions-except={extension}', f'--load-extension={extension}'])
        context.add_init_script('''(() => { const actual=Date.now; globalThis.__fixtureOffset=0; Date.now=()=>actual()+globalThis.__fixtureOffset; })()''')
        first = context.pages[0] if context.pages else context.new_page()
        fixture = DesktopBrowser('test_trusted_login_pkce_and_no_password')
        fixture.context, fixture.page = context, first
        fixture.calls, fixture.saved, fixture.denied, fixture.contributions = [], preferences(), set(), {}
        fixture.call_result, fixture.status_overrides, fixture.refresh_enabled = None, {}, False
        fixture.abort_business, fixture.errors, fixture.expected_challenge = False, [], None
        fixture.hold_preferences, fixture.pending_preferences, fixture.real_reference = False, [], False
        headers = {'access-control-allow-origin': SHELL, 'access-control-allow-headers': 'authorization,content-type,if-match,idempotency-key', 'access-control-allow-methods': 'GET,POST,PUT,DELETE,PATCH,OPTIONS', 'access-control-expose-headers': 'etag,x-request-id'}
        def route(route):
            request = route.request
            u = urlparse(request.url)
            origin = f'{u.scheme}://{u.netloc}'
            if u.scheme == 'chrome-extension':
                route.continue_()
            elif origin == CORE and u.path == '/oauth/token' and request.method == 'POST':
                fields = parse_qs(request.post_data or '')
                assert fields.get('refresh_token') == ['fixture-refresh']
                state['refresh_calls'] += 1
                if state['refresh_calls'] == 1:
                    body, status = {'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh', 'token_type': 'Bearer', 'expires_in': 900, 'scope': 'client:browser-component'}, 200
                else:
                    state['replay_count'] += 1
                    state['revoked'] = True
                    body, status = {'error': 'invalid_grant'}, 400
                route.fulfill(status=status, headers=headers, content_type='application/json', body=json.dumps(body))
            elif origin == CORE and u.path.startswith('/api/') and request.method != 'OPTIONS' and state['revoked']:
                state['protected_401'] += 1
                route.fulfill(status=401, headers=headers, content_type='application/json', body=json.dumps({'error': {'code':'INVALID_CREDENTIAL', 'message':'fixture revoked'}}))
            else:
                fixture.route(route)
        context.route('**/*', route)
        first.goto(SHELL + BASE)
        first.evaluate('''() => sessionStorage.setItem('xelab.oauth:https://core.example.test:browser-component', JSON.stringify({core_origin:'https://core.example.test',client_id:'browser-component', access_token:'fixture-access',refresh_token:'fixture-refresh',expires_at:Date.now()+45000,scope:'client:browser-component'}))''')
        first.reload()
        first.get_by_role('button', name='退出', exact=True).wait_for()
        first.wait_for_function("document.querySelector('#connection').textContent === '已连接核心'")
        workers = context.service_workers or [context.wait_for_event('serviceworker')]
        control = context.new_page()
        control.goto(f"chrome-extension://{urlparse(workers[0].url).netloc}/page.html")
        with context.expect_page() as opened:
            control.evaluate("async () => { const tabs=await chrome.tabs.query({url:'https://shell.example.test/*'}); await chrome.tabs.duplicate(tabs[0].id); }")
        second = opened.value
        second.wait_for_load_state()
        second.get_by_role('button', name='退出', exact=True).wait_for()
        second.wait_for_function("document.querySelector('#connection').textContent === '已连接核心'")
        copied = second.evaluate("sessionStorage.getItem('xelab.oauth:https://core.example.test:browser-component') !== null")
        opener = second.evaluate('window.opener !== null')
        assert copied and not opener
        for page in (first, second): page.evaluate('globalThis.__fixtureOffset=20000')
        first.get_by_role('button', name='打开系统状态', exact=True).click()
        first.get_by_text('"component-only"', exact=False).wait_for()
        second.get_by_role('button', name='打开系统状态', exact=True).click()
        second.get_by_role('button', name='登录', exact=True).wait_for()
        first.get_by_role('button', name='打开Skill', exact=True).click()
        first.get_by_role('button', name='登录', exact=True).wait_for()
        assert state == {'refresh_calls': 2, 'replay_count': 1, 'revoked': True, 'protected_401': 1}, state
        print(json.dumps({'desktop': 'actual production app.js and oauth.js', 'session_storage_copied': copied, 'opener_present': opener, 'first_tab_logged_out': True, 'duplicate_tab_logged_out': True, 'fixture_counts': state, 'scope': 'actual Chromium desktop; synthetic Core, not real OAuth/OCI'}))
        context.close()
