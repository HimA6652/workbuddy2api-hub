"""积分刷新改后台 + 列表载荷瘦身（P4/P5）。

两个真实数字决定了这次改动：

1. `GET /accounts?realm=all` 是 6 031 888 B，其中 87.9% 是每个账号的 packages
   明细 —— 列表页一个字段都不显示它。P4 把 packages 从列表载荷里摘掉，只留
   总额与 packagesReady / packagesCount 两个标记；明细照旧走
   /accounts/credits/detail，refresh:false 直接吃缓存、不打上游。
2. 全池刷积分是 数百个账号 x 0.8~1.2s 拟人间隔，约 6~10 分钟。以前它占着
   HTTP 请求同步跑，浏览器只能等到超时；P5 改成立刻回执 started/busy，进度
   看 GET /accounts/credits/status。三个断言钉住“真的没在等”：回执耗时、
   刷的过程里 /accounts 仍然秒回、并发第二次 POST 被单飞拒掉而不是排队。

套件在空闲端口上起一个真实网关，但子进程里把 Account.fetch_credits 换成假
实现，**一次上游请求都不发**：真实账号池的 refreshToken 是单次轮换的，测试
碰一下就会把生产账号踢下线。

    python tests/_test_credits_async.py
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402  (spare port + managed process)

PASS = 0
FAIL = 0

ACCOUNTS = 3        # 3 个账号 x 400ms 间隔 = 至少 1.2s 才刷得完
FAKE_DELAY = 0.4    # 假 fetch_credits 的耗时
SWEEP_GAP_MS = 400  # 账号之间的拟人间隔（生产是 800~1200ms，这里压到 400）


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


port = life.free_port()
work = tempfile.mkdtemp(prefix="creditsasync_")
usage_dir = os.path.join(work, "usage")
os.makedirs(usage_dir)
store = os.path.join(work, "accounts")
os.makedirs(store)

# 账号文件：只给网关认一个号必需的字段（Pool.load 只认 *.json）。
UIDS = ["credits-async-000%d" % i for i in range(1, ACCOUNTS + 1)]
for i, uid in enumerate(UIDS):
    with open(os.path.join(store, uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"uid": uid, "nickname": "acct%d" % (i + 1), "realm": "cn",
                   "accessToken": "fake-token-%d" % i, "enabled": True,
                   "addedAt": time.time()}, fh)

BOOT = """import os
import sys
import time

sys.path.insert(0, %(root)r)
import wb_accounts

DELAY = float(os.environ.get("FAKE_CREDITS_DELAY") or "0.4")


def fake_fetch_credits(self, *args, **kwargs):
    time.sleep(DELAY)
    self.credits = {
        "remain": 10,
        "used": 1,
        "size": 11,
        "updated_at": time.time(),
        "packages": [{"remain": 10,
                      "cycle_end_time": int(time.time() * 1000) + 86400000}],
    }
    return {"ok": True}


wb_accounts.Account.fetch_credits = fake_fetch_credits

import wb_proxy

sys.argv = ["wb_proxy.py", "--port", "%(port)s", "--host", "127.0.0.1",
            "--accounts-dir", %(store)r]
wb_proxy.main()
"""

boot_path = os.path.join(work, "boot.py")
with open(boot_path, "w", encoding="utf-8") as fh:
    fh.write(BOOT % {"root": ROOT, "port": port, "store": store})

proc = life.spawn_managed(
    [PY, boot_path],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=usage_dir,
             WB_CREDITS_SWEEP_GAP_MS=str(SWEEP_GAP_MS),
             FAKE_CREDITS_DELAY=str(FAKE_DELAY)))

TOKEN = None


def request(method, path, payload=None, headers=None, timeout=30):
    """一次请求；返回 (status, headers, body)，body 已解码成文本。"""
    h = dict(headers or {})
    if TOKEN:
        h["X-Panel-Token"] = TOKEN
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
        h["Content-Length"] = str(len(body))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, resp.headers, raw.decode("utf-8", "replace")
    finally:
        conn.close()


def wait_ready():
    for _ in range(60):
        time.sleep(0.5)
        try:
            status, _h, _b = request("GET", "/health")
            if status == 200:
                return True
        except Exception:
            if proc.poll() is not None:
                return False
    return False


def login():
    status, _h, text = request("POST", "/panel/login", {"password": "admin"})
    try:
        return json.loads(text).get("token")
    except Exception:
        return None


def as_json(text):
    try:
        return json.loads(text)
    except Exception:
        return {}


def accounts_payload():
    """一次 /accounts?realm=all（面板用的就是这条），返回 (耗时, 原文, 解析后, 状态)。

    realm 必须显式给 all：不带参数时网关按 CURRENT_REALM（默认 intl）过滤，
    这个套件的账号是 cn，会被筛成 0 行 —— 那是接口的正常行为，不是 bug。
    """
    started = time.time()
    status, _h, text = request("GET", "/accounts?realm=all")
    elapsed = time.time() - started
    return elapsed, text, as_json(text), status


try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)
    TOKEN = login()
    if not TOKEN:
        print("  [FAIL] panel login failed")
        sys.exit(1)

    print("[1] 列表载荷：还没有任何积分快照时也不该出现 packages 字段")
    _e, raw, payload, status = accounts_payload()
    rows = payload.get("accounts") or []
    check("/accounts -> 200", status == 200, status)
    check("/accounts 有 %d 个账号" % ACCOUNTS, len(rows) == ACCOUNTS, len(rows))
    check("冷启动载荷里没有 packages", '"packages"' not in raw, len(raw))

    print("[2] POST /accounts/credits 立刻回执，不占用请求")
    started = time.time()
    status, _h, text = request("POST", "/accounts/credits", {"realm": "all"})
    ack_ms = (time.time() - started) * 1000
    ack = as_json(text)
    check("回执耗时 < 1200ms（3 个账号 x 400ms 间隔，同步跑不可能这么快）",
          ack_ms < 1200, "%.0fms" % ack_ms)
    check("回执 started=True", ack.get("started") is True, ack)
    check("回执 total=%d" % ACCOUNTS, ack.get("total") == ACCOUNTS, ack)
    check("回执 done 从 0 起算", ack.get("done") == 0, ack)

    print("[3] 刷的过程中：进度可见，/accounts 仍然秒回")
    status, _h, text = request("GET", "/accounts/credits/status")
    view = as_json(text)
    check("GET /accounts/credits/status -> 200", status == 200, status)
    check("status.busy=True", view.get("busy") is True, view)
    check("status.total=%d" % ACCOUNTS, view.get("total") == ACCOUNTS, view)
    elapsed, raw, payload, status = accounts_payload()
    check("刷积分期间 /accounts 仍在 2s 内返回", elapsed < 2.0, "%.0fms" % (elapsed * 1000))
    check("刷积分期间载荷同样不含 packages", '"packages"' not in raw, len(raw))

    print("[4] 并发第二次 POST：被单飞拒掉，不排队、不报错")
    status, _h, text = request("POST", "/accounts/credits", {"realm": "all"})
    second = as_json(text)
    check("第二次 POST -> 200 回执", status == 200, status)
    check("第二次 started=False", second.get("started") is False, second)
    check("第二次 busy=True", second.get("busy") is True, second)

    print("[5] 跑完：busy 归零，done == total，percent == 100")
    view = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        _s, _h, text = request("GET", "/accounts/credits/status")
        view = as_json(text)
        if view.get("busy") is not True:
            break
        time.sleep(0.25)
    check("后台刷结束（busy=False）", view.get("busy") is False, view)
    check("done == total == %d" % ACCOUNTS,
          view.get("done") == ACCOUNTS and view.get("total") == ACCOUNTS, view)
    check("percent == 100", view.get("percent") == 100, view)
    check("没有记录到错误", not view.get("error"), view.get("error"))

    print("[6] 刷完之后列表只给摘要：总额在，packages 不在，标记在")
    _e, raw, payload, status = accounts_payload()
    rows = payload.get("accounts") or []
    check("刷完后仍有 %d 行" % ACCOUNTS, len(rows) == ACCOUNTS, len(rows))
    check("刷完后载荷仍不含 packages", '"packages"' not in raw, len(raw))
    for row in rows:
        credits = row.get("credits") or {}
        check("%s 的 credits.remain 透出来" % row.get("uid"),
              credits.get("remain") == 10, credits)
        check("%s 标了 packagesReady=True" % row.get("uid"),
              credits.get("packagesReady") is True, credits)
        check("%s 标了 packagesCount=1" % row.get("uid"),
              credits.get("packagesCount") == 1, credits)

    print("[7] 明细走 detail：refresh:false 吃缓存，不打上游")
    started = time.time()
    status, _h, text = request("POST", "/accounts/credits/detail",
                               {"uid": UIDS[0], "refresh": False})
    detail_ms = (time.time() - started) * 1000
    detail = as_json(text)
    packages = (detail.get("credits") or {}).get("packages") or []
    check("detail -> 200", status == 200, status)
    check("refresh:false 秒回（< 400ms，假上游一次就要 400ms）",
          detail_ms < 400, "%.0fms" % detail_ms)
    check("detail 带回缓存 packages", len(packages) == 1, detail)

    print("[8] GET /accounts/credits 同样是后台刷：立刻回执")
    started = time.time()
    status, _h, text = request("GET", "/accounts/credits")
    get_ms = (time.time() - started) * 1000
    ack = as_json(text)
    check("GET /accounts/credits 回执 < 1200ms", get_ms < 1200, "%.0fms" % get_ms)
    check("GET /accounts/credits started=True", ack.get("started") is True, ack)
    deadline = time.time() + 30
    while time.time() < deadline:
        _s, _h, text = request("GET", "/accounts/credits/status")
        if as_json(text).get("busy") is not True:
            break
        time.sleep(0.25)
    check("第二轮也跑完了", as_json(text).get("busy") is False, text[:120])
finally:
    life.stop_managed(proc)
    shutil.rmtree(work, ignore_errors=True)

print("")
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
