#!/usr/bin/env python3
"""Disposable Chromium plus synthetic OAuth/Core adapter; no real credentials."""
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

SHELL = 'https://shell.example.test'
CORE = 'https://core.example.test'
CLIENT = 'duplicate-tab-fixture'
PUBLIC = Path(__file__).resolve().parent / 'public'
state = {'refresh_calls': 0, 'replay_count': 0, 'revoked': False, 'protected_calls': 0}

def route_request(route):
    request = route.request
    url = urlparse(request.url)
    origin = f'{url.scheme}://{url.netloc}'
    if origin == SHELL:
        if url.path == '/':
            route.fulfill(status=200, content_type='text/html', body='<!doctype html><title>fixture</title>')
        elif url.path in ('/api.js', '/oauth.js'):
            route.fulfill(status=200, content_type='text/javascript', body=(PUBLIC / url.path[1:]).read_bytes())
        else:
            route.fulfill(status=404, body='not found')
        return
    if origin != CORE:
        route.abort()
        return
    headers = {'access-control-allow-origin': SHELL,
               'access-control-allow-headers': 'authorization,content-type',
               'access-control-allow-methods': 'GET,POST,OPTIONS'}
    if request.method == 'OPTIONS':
        route.fulfill(status=204, headers=headers)
        return
    if url.path == '/oauth/token':
        fields = parse_qs(request.post_data or '')
        assert fields.get('grant_type') == ['refresh_token'] and fields.get('client_id') == [CLIENT]
        assert fields.get('refresh_token') == ['fixture-refresh']
        state['refresh_calls'] += 1
        if state['refresh_calls'] == 1:
            data = {'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh',
                    'token_type': 'Bearer', 'expires_in': 900, 'scope': 'client:' + CLIENT}
            status = 200
        else:
            state['replay_count'] += 1
            state['revoked'] = True
            data, status = {'error': 'invalid_grant'}, 400
    elif url.path == '/api/platform/v1/skills':
        state['protected_calls'] += 1
        data, status = ({'error': {'code': 'INVALID_CREDENTIAL', 'message': 'revoked'}}, 401) if state['revoked'] else ({'items': []}, 200)
    else:
        route.fulfill(status=404, headers=headers)
        return
    route.fulfill(status=status, headers=headers, content_type='application/json', body=json.dumps(data))

with tempfile.TemporaryDirectory(prefix="xelab-duplicate-tab-repro-") as temp:
    root = Path(temp)
    extension = root / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "Disposable tab repro", "version": "1.0",
        "permissions": ["tabs"], "background": {"service_worker": "background.js"}}))
    (extension / "background.js").write_text("// Disposable test extension.\n")
    (extension / "page.html").write_text("<!doctype html><title>fixture extension</title>")
    with sync_playwright() as driver:
        context = driver.chromium.launch_persistent_context(
            str(root / "profile"), executable_path=os.environ["XELAB_OAUTH_BROWSER_CHROMIUM"], headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  f"--disable-extensions-except={extension}", f"--load-extension={extension}"])
        def handle(route):
            if route.request.url.startswith("chrome-extension://"):
                route.continue_()
            else:
                route_request(route)
        context.route("**/*", handle)
        first = context.pages[0] if context.pages else context.new_page()
        first.goto(SHELL + "/")
        first.evaluate("""({core,client}) => {
          const key = `xelab.oauth:${core}:${client}`;
          sessionStorage.setItem(key, JSON.stringify({core_origin:core,client_id:client,
            access_token:'fixture-access',refresh_token:'fixture-refresh',
            expires_at:Date.now()+20000,scope:`client:${client}`}));
        }""", {"core": CORE, "client": CLIENT})
        workers = context.service_workers or [context.wait_for_event("serviceworker")]
        extension_id = urlparse(workers[0].url).netloc
        control = context.new_page()
        control.goto(f"chrome-extension://{extension_id}/page.html")
        with context.expect_page() as opened:
            control.evaluate("""async () => {
              const tabs = await chrome.tabs.query({url:'https://shell.example.test/*'});
              await chrome.tabs.duplicate(tabs[0].id);
            }""")
        second = opened.value
        second.wait_for_load_state()
        key = f"xelab.oauth:{CORE}:{CLIENT}"
        copied = second.evaluate("(key) => sessionStorage.getItem(key) !== null", key)
        opener = second.evaluate("window.opener !== null")
        assert copied and not opener, "duplicate did not clone storage without an opener"
        setup = """async ({core,client}) => {
          const {OAuthSession} = await import('/oauth.js');
          const {ApiClient} = await import('/api.js');
          window.fixtureSession = new OAuthSession({core_origin:core,client_id:client});
          window.fixtureApi = new ApiClient({core_origin:core,client_id:client}, window.fixtureSession);
        }"""
        for page in (first, second):
            page.evaluate(setup, {"core": CORE, "client": CLIENT})
        first_result = first.evaluate("() => fixtureSession.accessToken().then(()=>'ok', e=>e.code)")
        second_result = second.evaluate("() => fixtureSession.accessToken().then(()=>'ok', e=>e.code)")
        first_api = first.evaluate("() => fixtureApi.invoke('listSkills').then(()=>'ok', e=>e.code)")
        states = [first.evaluate("() => fixtureSession.loggedIn"), second.evaluate("() => fixtureSession.loggedIn")]
        assert (first_result, second_result, first_api, states) == ("ok", "invalid_grant", "INVALID_CREDENTIAL", [False, False])
        assert state == {"refresh_calls": 2, "replay_count": 1, "revoked": True, "protected_calls": 1}
        print(json.dumps({"user_agent": first.evaluate("navigator.userAgent"), "session_storage_copied": copied,
                          "opener_present": opener, "first_refresh": first_result, "second_refresh": second_result,
                          "subsequent_first_api": first_api, "tabs_logged_in": states,
                          "fixture_counts": state, "scope": "actual Chromium duplicate; simulated Core, not real OAuth/OCI"}))
        context.close()
