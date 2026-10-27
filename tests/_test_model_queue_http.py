"""Real HTTP evidence for shared model-queue 6020/6021 handling.

Reuses the isolated loopback gateway from the Anthropic protocol tests. Every
upstream is a local fixture; there are no production accounts or network calls.
"""
import json
import os
import sys
import threading

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
original_do_post = H.MockUpstream.do_POST
hold_started = threading.Event()
hold_release = threading.Event()


def queued_do_post(self):
    length = int(self.headers.get("Content-Length") or 0)
    raw = self.rfile.read(length) if length else b""
    body = json.loads(raw.decode("utf-8")) if raw else {}
    H.MOCK["requests"].append({"path": self.path, "body": body})
    if H.MOCK.get("status") == 429:
        payload = json.dumps(H.MOCK["queue_body"]).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return
    if H.MOCK.get("hold"):
        hold_started.set()
        hold_release.wait(5)
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


def reset():
    P.MODEL_QUEUE_GATE.clear()
    H.MOCK["requests"] = []
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

    print("[3] mixed-product exhaustion retains queue semantics on every API")
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
    H.MockUpstream.do_POST = original_do_post
    hold_release.set()
    H.MOCK["hold"] = False
    P.MODEL_QUEUE_GATE.clear()
    H.close_test_servers()

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
