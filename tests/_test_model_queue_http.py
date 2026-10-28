"""Real HTTP evidence for shared model-queue 6020/6021 handling.

Reuses the isolated loopback gateway from the Anthropic protocol tests. Every
upstream is a local fixture; there are no production accounts or network calls.
"""
import json
import os
import sys
import http.client
import io
import socket
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _test_anthropic_http as H
import wb_accounts
import wb_model_queue as Q

P = H.P
PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


class Clock(object):
    now = 0.0
    def __call__(self):
        return self.now


clock = Clock()
P.MODEL_QUEUE_GATE = Q.ModelQueueGate(clock)
# Isolated fixture defaults preserve the legacy fast-fail contract. Each
# opt-in retry section sets and restores its own explicit finite limits.
P.wb_settings.set_model_queue_retry(P.ACCOUNTS_DIR, {
    "model_queue_retry_attempts": 0, "model_queue_wait_budget_seconds": 30})
# The fixture owns every model call. In particular, startup's daily activity
# uses K3.1 and otherwise shows up as an unexplained thirteenth HTTP request.
P.SCHEDULER.stop()
# Bootstrap deliberately starts an independent catch-up worker. Stop only that
# fixture thread and join it before replacing the transport: otherwise its
# scheduled K3.1 activity can consume a scenario's hold latch with no Gate lease.
if P.SCHEDULER._thread is not None:
    P.SCHEDULER._thread.join(35)
for name in ("CREDITS_REFRESHER", "PRICING", "UPDATES"):
    resource = getattr(P, name, None)
    if resource is not None:
        resource.stop()
        thread = getattr(resource, "_thread", None)
        if thread is not None:
            thread.join(5)
_original_urlopen = P.wb_accounts.urlopen
def _fixture_only_urlopen(request, *args, **kwargs):
    from urllib.parse import urlparse
    target = urlparse(request.full_url)
    if (target.hostname, target.port) != ("127.0.0.1", H.UPSTREAM_PORT):
        raise AssertionError("unowned HTTP fixture caller: " + request.full_url)
    return _original_urlopen(request, *args, **kwargs)
P.wb_accounts.urlopen = _fixture_only_urlopen
original_do_post = H.MockUpstream.do_POST
hold_started = threading.Event()
hold_release = threading.Event()


def queued_do_post(self):
    length = int(self.headers.get("Content-Length") or 0)
    raw = self.rfile.read(length) if length else b""
    body = json.loads(raw.decode("utf-8")) if raw else {}
    H.MOCK["requests"].append({"path": self.path, "body": body})
    H.MOCK.setdefault("attempt_times", []).append(time.monotonic())
    if H.MOCK.get("hold"):
        hold_started.set()
        hold_release.wait(20)
        # Each new hold window is explicitly armed by the scenario. Do not
        # release a later probe through this scenario's old latch.
        H.MOCK["hold"] = False
    if H.MOCK.get("status") == 429:
        payload = json.dumps(H.MOCK["queue_body"]).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return
    payload = H.sse(H.TEXT_CHUNKS)
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)


H.MockUpstream.do_POST = queued_do_post
H.MOCK["stream"] = True
H.MOCK["chunks"] = H.TEXT_CHUNKS


def call(path, model="kimi-k3.1-pro", headers=None):
    auth = headers or ({"Authorization": "Bearer TESTKEY"} if "chat" in path
                       else {"x-api-key": "TESTKEY"})
    return H.request("POST", path, {
        "model": model, "max_tokens": 8, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=auth)


def set_retry(attempts, budget):
    return P.wb_settings.set_model_queue_retry(P.ACCOUNTS_DIR, {
        "model_queue_retry_attempts": attempts,
        "model_queue_wait_budget_seconds": budget})


def wall_clock():
    return time.monotonic()


def reset():
    # Every preceding scenario joins its clients before reset. Release any
    # completed hold, then arm a fresh latch for the next scenario.
    hold_release.set()
    H.MOCK["hold"] = False
    hold_release.clear()
    hold_started.clear()
    P.MODEL_QUEUE_GATE.clear()
    H.MOCK["label"] = None
    H.MOCK["requests"] = []
    H.MOCK["attempt_times"] = []
    H.MOCK["status"] = 429
    H.MOCK["queue_body"] = {"code": 6021, "msg": "queue.full.title"}


try:
    print("[0] OpenAI envelope, fast-fail and health")
    reset()
    second = wb_accounts.Account(P.POOL.accounts[0].to_dict())
    second.uid = "acct-http-2"
    P.POOL.accounts.append(second)
    code, headers, body = call("/v1/chat/completions")
    obj = json.loads(body)
    check("6021 returns HTTP 429 with one upstream attempt",
          code == 429 and len(H.MOCK["requests"]) == 1, (code, len(H.MOCK["requests"])))
    check("OpenAI keeps numeric code 429 and separates upstream_code 6021",
          obj.get("error", {}).get("code") == 429
          and obj.get("error", {}).get("upstream_code") == 6021, obj)
    check("Retry-After header and body agree",
          headers.get("Retry-After") == "5"
          and obj.get("error", {}).get("retry_after") == 5, (headers, obj))
    H.MOCK["requests"] = []
    code, headers, body = call("/v1/chat/completions")
    local_obj = json.loads(body)
    check("a second request is local fast-fail, not another pool scan",
          code == 429 and not H.MOCK["requests"], (code, H.MOCK["requests"]))
    check("local OpenAI fast-fail does not invent an upstream code",
          local_obj.get("error", {}).get("code") == 429
          and local_obj.get("error", {}).get("upstream_code") is None
          and headers.get("Retry-After") == "5", (headers, local_obj))
    check("queue errors leave both accounts ready and uncooled",
          all(a.ready(model="kimi-k3.1-pro") and not a.model_cooldowns
              and a.fails == 0 for a in P.POOL.accounts),
          [(a.uid, a.model_cooldowns, a.fails) for a in P.POOL.accounts])
    code, _, body = H.request("GET", "/health")
    health = json.loads(body)
    check("health exposes public queue phase without account identifiers",
          code == 200 and len(health.get("model_queues", [])) == 1
          and health["model_queues"][0]["phase"] == "open"
          and "uid" not in health["model_queues"][0]
          and "kimi-k3.1-pro" in str(health["model_queues"]), health)
    H.MOCK["status"] = 200
    code, _, _ = call("/v1/chat/completions", "deepseek-v4.1-flash")
    check("another model on the same account is unaffected", code == 200, code)

    print("[1] real request is the single half-open probe")
    scope = Q.queue_scope(P.POOL.accounts[0], "kimi-k3.1-pro")
    P.MODEL_QUEUE_GATE.note(scope, 40)
    clock.now = 41.0
    H.MOCK["requests"] = []
    H.MOCK["status"] = 200
    H.MOCK["hold"] = True
    result = []
    first = threading.Thread(target=lambda: result.append(call("/v1/chat/completions")))
    first.start()
    if not hold_started.wait(3):
        check("real half-open request reached upstream", False)
    else:
        check("real half-open request reached upstream", True)
        blocked = [call("/v1/chat/completions") for _ in range(8)]
        check("eight concurrent requests fast-fail while one probe is in flight",
              all(row[0] == 429 for row in blocked)
              and len(H.MOCK["requests"]) == 1,
              ([row[0] for row in blocked], len(H.MOCK["requests"])))
    hold_release.set()
    first.join(10)
    H.MOCK["hold"] = False
    check("the one real probe completes and closes the Gate",
          bool(result) and result[0][0] == 200 and not P.MODEL_QUEUE_GATE.snapshot(),
          (result, P.MODEL_QUEUE_GATE.snapshot()))
    H.MOCK["requests"] = []
    code, _, _ = call("/v1/chat/completions")
    check("next request passes normally after recovery",
          code == 200 and len(H.MOCK["requests"]) == 1, code)

    print("[2] Anthropic native envelope and 6020 retry hint")
    reset()
    os.environ["WB_ENABLE_ANTHROPIC_MESSAGES"] = "1"
    H.MOCK["queue_body"] = {"code": 6020, "data": {
        "retry_after": 40, "estimated_wait": 16195, "queue_position": 3239}}
    code, headers, body = call("/v1/messages")
    obj = json.loads(body)
    check("6020 returns native Anthropic rate_limit_error",
          code == 429 and obj.get("type") == "error"
          and obj.get("error", {}).get("type") == "rate_limit_error", (code, obj))
    check("Anthropic preserves upstream code and seconds-based Retry-After",
          headers.get("Retry-After") == "40"
          and obj.get("error", {}).get("upstream_code") == 6020
          and obj.get("error", {}).get("retry_after") == 40, (headers, obj))
    H.MOCK["requests"] = []
    code, headers, body = call("/v1/messages")
    local_obj = json.loads(body)
    check("Anthropic retries also fast-fail without an upstream call",
          code == 429 and not H.MOCK["requests"], code)
    check("local Anthropic fast-fail keeps the native envelope without an upstream code",
          local_obj.get("type") == "error"
          and local_obj.get("error", {}).get("type") == "rate_limit_error"
          and local_obj.get("error", {}).get("upstream_code") is None
          and headers.get("Retry-After") == "40", (headers, local_obj))

    print("[3] opt-in 6020 retry honors eight seconds exactly")
    reset()
    P.MODEL_QUEUE_GATE.clock = wall_clock
    set_retry(1, 12)
    H.MOCK["queue_body"] = {"code": 6020, "data": {"retry_after": 8}}
    original_queued_do_post = H.MockUpstream.do_POST
    def recover_after_eight(self):
        # Flip only the retry, not the initial queued response.
        if H.MOCK["attempt_times"]:
            H.MOCK["status"] = 200
        original_queued_do_post(self)
    H.MockUpstream.do_POST = recover_after_eight
    started = time.monotonic()
    code, _, body = call("/v1/chat/completions")
    elapsed = time.monotonic() - started
    times = H.MOCK["attempt_times"]
    check("eight-second upstream hint is waited, not just echoed to the client",
          code == 200 and 7.8 <= elapsed < 11.5 and len(times) == 2,
          (code, elapsed, times))
    check("the second real request is at least eight seconds later",
          len(times) == 2 and 7.8 <= times[1] - times[0] < 11.5, times)
    H.MockUpstream.do_POST = original_queued_do_post

    print("[4] finite budget and repeated busy responses retain explicit 429")
    reset()
    set_retry(2, 1)
    H.MOCK["queue_body"] = {"code": 6020, "data": {"retry_after": 8}}
    started = time.monotonic()
    code, headers, body = call("/v1/chat/completions")
    elapsed = time.monotonic() - started
    obj = json.loads(body)
    check("persistent queue congestion stops at the finite one-second budget",
          code == 429 and 0.8 <= elapsed < 3 and len(H.MOCK["attempt_times"]) == 1,
          (code, elapsed, H.MOCK["attempt_times"]))
    check("final busy failure is not hidden and retains its upstream hint",
          headers.get("Retry-After") == "8"
          and obj.get("error", {}).get("retry_after") == 8
          and obj.get("error", {}).get("upstream_code") == 6020, (headers, obj))
    check("budget exhaustion leaves no Gate waiters or half-open leases",
          not P.MODEL_QUEUE_GATE.waiters and not P.MODEL_QUEUE_GATE.probes,
          (P.MODEL_QUEUE_GATE.waiters, P.MODEL_QUEUE_GATE.probes))

    print("[5] high concurrency shares one recovery request, no polling storm")
    reset()
    set_retry(2, 5)
    H.MOCK["status"] = 200
    scope = Q.queue_scope(P.POOL.accounts[0], "kimi-k3.1-pro")
    P.MODEL_QUEUE_GATE.clear()
    # Keep the gate open long enough for the anchor to enter its real
    # half-open request; the 11 followers are created only after that event.
    P.MODEL_QUEUE_GATE.clock = clock
    P.MODEL_QUEUE_GATE.note(scope, 30)
    clock.now += 31
    before = len(H.MOCK["requests"])
    results = []
    H.MOCK["hold"] = True
    hold_started.clear()
    hold_release.clear()
    # Anchor one request in the real upstream first. Starting all twelve at
    # once would only prove scheduler timing, not the half-open invariant.
    clients = [threading.Thread(target=lambda: results.append(
        call("/v1/chat/completions")))]
    # Exercise the actual Gate admission: no transport is admitted while this
    # held request owns the single half-open token.
    clients[0].start()
    anchored = hold_started.wait(3)
    check("concurrency fixture has one real probe held upstream",
          anchored and len(P.MODEL_QUEUE_GATE.probes) == 1,
          (anchored, len(H.MOCK["requests"]), P.MODEL_QUEUE_GATE.states,
           P.MODEL_QUEUE_GATE.probes))
    clients.extend(threading.Thread(target=lambda: results.append(
        call("/v1/chat/completions"))) for _ in range(11))
    for client in clients[1:]: client.start()
    deadline = time.monotonic() + 3
    while sum(P.MODEL_QUEUE_GATE.waiters.values()) < 11 and time.monotonic() < deadline:
        time.sleep(0.01)
    check("eleven requests wait while only one real probe is in flight",
          sum(P.MODEL_QUEUE_GATE.waiters.values()) == 11
          and len(H.MOCK["requests"]) - before == 1,
          (P.MODEL_QUEUE_GATE.waiters, len(H.MOCK["requests"]) - before))
    hold_release.set()
    for client in clients: client.join(8)
    H.MOCK["hold"] = False
    check("twelve concurrent busy requests have bounded completion",
          all(not client.is_alive() for client in clients)
          and len(results) == 12 and all(row[0] == 200 for row in results),
          [(row[0] if row else None) for row in results])
    check("one probe is followed by normal traffic, not twelve retry storms",
          len(H.MOCK["requests"]) - before == 12
          and not P.MODEL_QUEUE_GATE.waiters
          and not P.MODEL_QUEUE_GATE.probes,
          (len(H.MOCK["requests"]) - before, P.MODEL_QUEUE_GATE.waiters,
           P.MODEL_QUEUE_GATE.probes))
    check("waiting and recovery do not penalize or repin account rotation",
          all(a.ready(model="kimi-k3.1-pro") and not a.model_cooldowns
              and a.fails == 0 for a in P.POOL.accounts),
          [(a.uid, a.model_cooldowns, a.fails) for a in P.POOL.accounts])

    print("[6] repeated busy responses stop at the configured attempt count")
    reset()
    P.MODEL_QUEUE_GATE.clock = wall_clock
    set_retry(3, 12)
    H.MOCK["status"] = 429
    H.MOCK["queue_body"] = {"code": 6020, "data": {"retry_after": 1}}
    started = time.monotonic()
    code, headers, body = call("/v1/chat/completions")
    elapsed = time.monotonic() - started
    obj = json.loads(body)
    check("three finite retries make four total upstream attempts then fail clearly",
          code == 429 and len(H.MOCK["attempt_times"]) == 4
          and 2.8 <= elapsed < 9 and headers.get("Retry-After") == "1"
          and obj.get("error", {}).get("upstream_code") == 6020,
          (code, elapsed, H.MOCK["attempt_times"], obj))

    print("[7] a real disconnected client stops waiting without a spurious 502")
    reset()
    set_retry(1, 30)
    P.MODEL_QUEUE_GATE.note(scope, 30)
    connection = http.client.HTTPConnection("127.0.0.1", H.GATEWAY_PORT, timeout=8)
    connection.connect()
    request_body = json.dumps({
        "model": "kimi-k3.1-pro", "max_tokens": 8, "stream": False,
        "messages": [{"role": "user", "content": "cancel"}]}).encode("utf-8")
    connection.sock.sendall((
        "POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        "Authorization: Bearer TESTKEY\r\nContent-Type: application/json\r\n"
        "Content-Length: %d\r\n\r\n" % len(request_body)).encode("ascii")
        + request_body)
    deadline = time.monotonic() + 3
    while not P.MODEL_QUEUE_GATE.waiters and time.monotonic() < deadline:
        time.sleep(0.01)
    check("cancellation fixture reached the shared Gate waiter",
          bool(P.MODEL_QUEUE_GATE.waiters), P.MODEL_QUEUE_GATE.waiters)
    started = time.monotonic()
    connection.close()
    deadline = time.monotonic() + 3
    while P.MODEL_QUEUE_GATE.waiters and time.monotonic() < deadline:
        time.sleep(0.01)
    check("client disconnect releases the waiter promptly, without another upstream call",
          not P.MODEL_QUEUE_GATE.waiters and time.monotonic() - started < 2
          and not H.MOCK["requests"] and not P.MODEL_QUEUE_GATE.probes,
          (P.MODEL_QUEUE_GATE.waiters, H.MOCK["requests"], P.MODEL_QUEUE_GATE.probes))

    print("[8] queue waiters yield the global slot to an unrelated model")
    reset()
    set_retry(1, 15)
    P.MODEL_QUEUE_GATE.clock = wall_clock
    P.MODEL_QUEUE_GATE.note(scope, 1)
    old_slots = P._chat_slots
    P._chat_slots = threading.BoundedSemaphore(1)
    held_client = None
    healthy_result = []
    try:
        held_client = threading.Thread(target=lambda: healthy_result.append(
            call("/v1/chat/completions")))
        held_client.start()
        deadline = time.monotonic() + 3
        while not P.MODEL_QUEUE_GATE.waiters and time.monotonic() < deadline:
            time.sleep(0.01)
        check("the waiting request yielded its only global chat slot",
              bool(P.MODEL_QUEUE_GATE.waiters)
              and P._chat_slots.acquire(blocking=False), P.MODEL_QUEUE_GATE.waiters)
        # This token is the slot just yielded by the waiter; return it before
        # the normal HTTP request exercises the same single-slot gateway.
        P._chat_slots.release()
        old_status = H.MOCK["status"]
        H.MOCK["status"] = 200
        try:
            healthy_result.append(call("/v1/chat/completions", model="deepseek-v4.1-flash"))
        finally:
            H.MOCK["status"] = old_status
        check("an unrelated healthy model completes while K3.1 is waiting",
              len(healthy_result) == 1 and healthy_result[0][0] == 200,
              healthy_result)
        held_client.join(8)
        check("the waiter eventually returns an explicit final 429 within budget",
              not held_client.is_alive() and len(healthy_result) == 2
              and healthy_result[1][0] == 429, healthy_result)
    finally:
        if held_client is not None: held_client.join(1)
        P._chat_slots = old_slots
        H.MOCK["hold"] = False
        hold_release.set()

    print("[9] persistent connection owns a fresh slot after an unreacquired wait")
    reset()
    set_retry(1, 1)
    P.MODEL_QUEUE_GATE.clock = wall_clock
    P.MODEL_QUEUE_GATE.clear()
    old_slots = P._chat_slots
    P._chat_slots = threading.BoundedSemaphore(1)
    persistent_result = []
    held_client = None
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", H.GATEWAY_PORT, timeout=10)
        held_client = threading.Thread(target=lambda: persistent_result.append(
            call("/v1/chat/completions", headers={
                "Authorization": "Bearer TESTKEY", "Connection": "keep-alive"})))
        # Use the same client connection for both requests: this is the
        # handler-thread lifetime that a connection-per-request test misses.
        original_request = H.request
        def persistent_request(method, path, body=None, headers=None, timeout=30):
            data = None if body is None else (
                body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
            outgoing = dict(headers or {})
            outgoing["Content-Type"] = "application/json"
            connection.request(method, path, data, outgoing)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.headers), raw
        H.request = persistent_request
        try:
            held_client.start()
            deadline = time.monotonic() + 3
            while not P.MODEL_QUEUE_GATE.waiters and time.monotonic() < deadline:
                time.sleep(0.01)
            check("persistent waiter yielded its slot",
                  bool(P.MODEL_QUEUE_GATE.waiters)
                  and P._chat_slots.acquire(blocking=False))
            # Let the retry reacquire before the unrelated model releases its
            # token, so the subsequent real 429 leaves it unreacquired.
            P.MODEL_QUEUE_GATE.clear()
            P._chat_slots.release()
            held_client.join(8)
            check("persistent waiter finishes with its original 429",
                  not held_client.is_alive() and persistent_result[-1][0] == 429,
                  persistent_result[-1:])
            # The new Gate is still fresh: this request is a local 429.
            # The old bug leaked this request's newly acquired slot; a third
            # request then over-released and failed in the server thread.
            code, _, _ = call("/v1/chat/completions")
            check("second keep-alive request is a local queue rejection", code == 429, code)
            P.MODEL_QUEUE_GATE.clear()
            H.MOCK["status"] = 200
            code, _, _ = call("/v1/chat/completions")
            check("third keep-alive request succeeds and releases its own slot",
                  code == 200 and P._chat_slots.acquire(blocking=False), code)
            P._chat_slots.release()
        finally:
            H.request = original_request
    finally:
        if connection is not None: connection.close()
        if held_client is not None: held_client.join(1)
        P._chat_slots = old_slots

    print("[10] mixed-product exhaustion retains queue semantics on every API")
    reset()
    set_retry(0, 30)
    P.MODEL_QUEUE_GATE.clock = clock
    clock.now = 100.0
    second.product = "vscode"
    for account in P.POOL.accounts:
        P.MODEL_QUEUE_GATE.note(Q.queue_scope(account, "kimi-k3.1-pro"), 40)
    H.MOCK["requests"] = []
    for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
        if path == "/v1/responses":
            code, headers, _ = H.request("POST", path,
                {"model": "kimi-k3.1-pro", "input": "hi"},
                {"Authorization": "Bearer TESTKEY"})
        else:
            code, headers, _ = call(path)
        check("all mixed scopes open: %s returns 429 with Retry-After" % path,
              code == 429 and headers.get("Retry-After") == "40",
              (path, code, headers))
    check("mixed-scope exhaustion makes zero upstream calls and preserves accounts",
          not H.MOCK["requests"]
          and all(a.fails == 0 and not a.model_cooldowns for a in P.POOL.accounts),
          H.MOCK["requests"])
finally:
    P.wb_accounts.urlopen = _original_urlopen
    H.MockUpstream.do_POST = original_do_post
    hold_release.set()
    H.MOCK["hold"] = False
    set_retry(0, 30)
    P.MODEL_QUEUE_GATE.clear()
    hold_release.set()
    P.MODEL_QUEUE_GATE.clock = clock
    H.close_test_servers()

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
