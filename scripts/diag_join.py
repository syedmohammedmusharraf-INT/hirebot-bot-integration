"""Standalone join diagnostic: builds a real ``GoogleMeetBotAdapter`` (same
Xvfb + headed Chrome + injected CDP payload as the actual bot), navigates to
a Meet URL, and prints what happened after every step -- current URL/title/
readyState over 10s, the browser console, and a summary of CDP performance-
log network/frame events (loadingFailed entries + Page.frame* events). Not
part of the application; run manually against a running control container:

    docker cp scripts/diag_join.py <container>:/tmp/diag_join.py
    docker exec <container> sh -c 'PYTHONPATH=/app python3 /tmp/diag_join.py https://meet.google.com/xxx-xxxx-xxx'

This is what found and confirmed the fix for a real bug: the injected
payload's WebSocketClient opening `ws://localhost:PORT` synchronously in its
constructor made `driver.get()` hang 40-70s and report `net::ERR_ABORTED`
for the Document load, even though `Page.frameNavigated` had already fired
for the correct origin -- see the "Wiring" section of
meet_voice_bot/google_meet_bot_adapter/payload/google_meet_chromedriver_payload.js
for the full story and fix (deferring the connect to `window.load`). Reach
for this script again first if a join ever hangs at `current_url='data:,'`
with no exception raised.
"""

import json
import sys
import time

from meet_voice_bot.google_meet_bot_adapter.google_meet_bot_adapter import GoogleMeetBotAdapter

meeting_url = sys.argv[1] if len(sys.argv) > 1 else "https://meet.google.com/abc-defg-hij"

adapter = GoogleMeetBotAdapter(
    meeting_url=meeting_url,
    bot_name="Diag Bot",
    on_status_change=lambda s, e: print("STATUS", s, e, flush=True),
)

print("init()...", flush=True)
adapter.init()
print("driver started, session:", adapter.driver.session_id, flush=True)

print("Browser.grantPermissions...", flush=True)
from urllib.parse import urlparse

origin = f"{urlparse(meeting_url).scheme}://{urlparse(meeting_url).netloc}"
adapter.driver.execute_cdp_cmd("Browser.grantPermissions", {"origin": origin, "permissions": ["audioCapture", "videoCapture", "displayCapture"]})
print("granted.", flush=True)

print(f"driver.get({meeting_url!r})...", flush=True)
t0 = time.time()
try:
    adapter.driver.get(meeting_url)
    print(f"driver.get() RETURNED after {time.time() - t0:.1f}s, no exception", flush=True)
except Exception as e:
    print(f"driver.get() RAISED after {time.time() - t0:.1f}s: {type(e).__name__}: {e}", flush=True)

for i in range(10):
    try:
        print(f"[{i}] current_url={adapter.driver.current_url!r} title={adapter.driver.title!r} readyState={adapter.driver.execute_script('return document.readyState')!r}", flush=True)
    except Exception as e:
        print(f"[{i}] error reading state: {e}", flush=True)
    time.sleep(1)

print("browser console logs:", flush=True)
try:
    browser_logs = adapter.driver.get_log("browser")
    for entry in browser_logs[-40:]:
        print(" ", entry, flush=True)
    if not browser_logs:
        print("  <empty>", flush=True)
except Exception as e:
    print("  (could not read):", e, flush=True)

print("performance / network logs:", flush=True)
try:
    perf_logs = adapter.driver.get_log("performance")
    failures = []
    responses = []
    for entry in perf_logs:
        try:
            msg = json.loads(entry.get("message", "{}")).get("message", {})
        except Exception:
            continue
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "Network.loadingFailed":
            failures.append(params)
        elif method in ("Network.requestWillBeSent",) and params.get("type") == "Document":
            responses.append({"method": method, "url": params.get("request", {}).get("url"), "documentURL": params.get("documentURL")})
        elif method in ("Page.frameStartedLoading", "Page.frameNavigated", "Page.navigatedWithinDocument", "Page.frameStoppedLoading"):
            responses.append({"method": method, "params": params})
    print(f"  {len(perf_logs)} total perf log entries, {len(failures)} loadingFailed, {len(responses)} doc/frame events", flush=True)
    for f in failures[:10]:
        print("  FAIL:", f, flush=True)
    for r in responses[:20]:
        print("  EVT:", r, flush=True)
except Exception as e:
    print("  (could not read):", e, flush=True)

print("saving screenshot to /tmp/diag.png", flush=True)
adapter.driver.save_screenshot("/tmp/diag.png")

print("teardown...", flush=True)
adapter.teardown_driver()
print("done.", flush=True)
