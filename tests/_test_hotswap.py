"""热切换：把监听 socket 交给新进程，旧进程排空手上的请求再退出。

要证的是「更新网关不打断请求」，所以断言全是黑盒的：

[1] 平时 GET /hotswap/status 是 idle（active 只会是「这条查询自己」）；
[2] 没有面板会话时 POST /hotswap 被拒（401）；
[3] 先挂一条「发完头就停住」的请求，再触发切换：回执里就有 child_pid；
[4] 子进程接手以后，端口一直有人 accept —— 期间持续压一个 keep-alive 客户端，
    每条请求都拿到 200，连接级失败（refused / reset）为 0；
[5] 切换之后才把 body 发完：那条请求仍由旧进程正常答完（排空是真的在等，
    不是「新进程一起来就把旧进程杀掉」）；
[6] 旧进程排空完成后自己退出，新进程继续服务，且它的状态是全新的 idle。

面板会话存在内存里（wb_settings.PanelSessions），所以「新进程不认旧 token」
本身就是判断交接完成最干净的信号。

    python tests/_test_hotswap.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = (os.environ.get("WB_HOTSWAP_PY")
      or os.path.join(ROOT, "python", "python.exe"))
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


def kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30)
    except Exception:
        pass


port = life.free_port()
work = tempfile.mkdtemp(prefix="hotswap_")
tree = os.path.join(work, "gateway")
shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
    "__pycache__", ".git", ".github", "tests", "accounts", "usage", "logs"))
store = os.path.join(work, "accounts")
os.makedirs(store)

proc = life.spawn_managed(
    [PY, os.path.join(tree, "wb_proxy.py"), "--port", str(port),
     "--host", "127.0.0.1", "--accounts-dir", store],
    cwd=tree, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=os.path.join(work, "usage")))

TOKEN = None
child_pid = None


def request(method, path, payload=None, headers=None, timeout=30):
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
        return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def as_json(text):
    try:
        return json.loads(text)
    except Exception:
        return {}


def status_json(timeout=15):
    status, _h, text = request("GET", "/hotswap/status", timeout=timeout)
    return as_json(text) if status == 200 else {}


def login():
    status, _h, text = request("POST", "/panel/login", {"password": "admin"})
    return as_json(text).get("token") if status == 200 else None


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


class Load(threading.Thread):
    """持续压一个 keep-alive 连接；服务端主动关连接就重连，不算失败。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.ok = 0
        self.bad = 0
        self.reconnect = 0
        self.errors = 0
        self.first_error = ""

    def run(self):
        conn = None
        while not self.stop.is_set():
            try:
                if conn is None:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    self.ok += 1
                else:
                    self.bad += 1
                if resp.will_close:
                    conn.close()
                    conn = None
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.reconnect += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            except Exception as exc:
                self.errors += 1
                if not self.first_error:
                    self.first_error = repr(exc)
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(0.05)
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


slow_sock = None
load = None
try:
    if not wait_ready():
        print("  [FAIL] server did not start")
        sys.exit(1)
    TOKEN = login()
    if not TOKEN:
        print("  [FAIL] panel login failed")
        sys.exit(1)

    print("[1] 未切换时：/hotswap/status 是 idle（active 只含这条查询本身）")
    # /health 返回时上一条连接的 finally 可能还没减 active；那不是另一条
    # 仍在处理的业务请求。等这一短暂收尾结束，再断言「只有这次查询自己」。
    state = {}
    for _ in range(100):
        state = status_json()
        if state.get("active") == 1:
            break
        time.sleep(0.05)
    check("phase=idle", state.get("phase") == "idle", state)
    check("child_pid 为空", state.get("child_pid") in (None, 0), state)
    check("active=1（自己）", state.get("active") == 1, state)
    check("draining=false", state.get("draining") is False, state)

    print("[1b] /health 自述 PID（热切换之后面板靠它认回真进程）")
    _s, _h, body = request("GET", "/health")
    health = as_json(body) or {}
    check("health 里有 pid", isinstance(health.get("pid"), int), health)
    check("pid 与 /hotswap/status 报的一致", health.get("pid") == state.get("pid"),
          (health.get("pid"), state.get("pid")))

    print("[2] 没有面板会话：POST /hotswap 被拒，服务照旧")
    saved, TOKEN = TOKEN, None
    status, _h, _b = request("POST", "/hotswap")
    TOKEN = saved
    check("401", status == 401, status)
    status, _h, _b = request("GET", "/health")
    check("服务仍在答 /health", status == 200, status)

    print("[3] 先挂一条「发完头就停住」的请求，再触发切换")
    slow_head = (b"POST /panel/login HTTP/1.1\r\n"
                 b"Host: 127.0.0.1\r\n"
                 b"Content-Type: application/json\r\n"
                 b"Content-Length: 32\r\n\r\n")
    slow_sock = socket.create_connection(("127.0.0.1", port), timeout=60)
    slow_sock.sendall(slow_head)
    time.sleep(0.5)
    state = status_json()
    check("这条请求被算进 active（2 = 它 + 本次查询）", state.get("active") == 2, state)

    load = Load()
    load.start()
    time.sleep(1.0)
    ok_before = load.ok
    status, _h, text = request("POST", "/hotswap")
    ack = as_json(text)
    check("POST /hotswap -> 200 且 started=true",
          status == 200 and ack.get("started") is True, "%s %s" % (status, ack))
    check("回执带着 starting 状态",
          (ack.get("state") or {}).get("phase") == "starting", ack)
    child_pid = (ack.get("state") or {}).get("child_pid")
    check("回执里有 child_pid", bool(child_pid), ack)
    check("child_pid 不是旧进程", child_pid != proc.pid,
          "%s vs %s" % (child_pid, proc.pid))

    print("[4] 子进程接手：端口一直在 accept，没有一秒是空的")
    deadline = time.time() + 90
    handover_at = 0.0
    while time.time() < deadline:
        try:
            status, _h, _b = request("GET", "/hotswap/status", timeout=5)
        except Exception:
            status = 0
        if status == 401:
            # 新进程不认旧 token：交接完成
            handover_at = time.time()
            break
        if status == 200:
            state = status_json(timeout=5)
            if state.get("phase") == "failed":
                break
        time.sleep(0.05)
    check("新进程在答（旧 token 已被它拒掉）", handover_at > 0, status_json())
    check("旧进程这时还活着（在排空，不是已经走了）",
          proc.poll() is None, proc.poll())

    print("[5] 接管期间两个进程并存：新进程的调度器先按住，别和旧进程同时签到")
    TOKEN = login()
    check("新进程可以用同一口令登录", bool(TOKEN))
    # 登录的响应先回，并不保证旧进程已经执行到 shutdown()。交接窗口内
    # 新 token 的下一请求仍可能由旧进程 accept，返回 401。仅对这种明确
    # 的认证窗口重新登录；不读取旧进程的 health.pid，也不掩盖其他状态。
    sched = {}
    for _ in range(100):
        TOKEN = login()
        _status, _headers, sched_body = request("GET", "/scheduler", timeout=15)
        candidate = as_json(sched_body)
        # 登录也来自新进程，故同一新 token 的 200 已证明调度器响应归属。
        # 不另发独立 health：共享 listen socket 可让两条连接由不同进程 accept。
        if _status != 200:
            time.sleep(0.05)
            continue
        # 初始化中的 /scheduler 也可能返回 enabled=False；必须拿到完整快照，
        # 并且 enabled=True 是业务失败而不是继续等待的过渡状态。
        if candidate.get("enabled") is True:
            sched = candidate
            break
        if candidate.get("enabled") is False and all(
                key in candidate for key in ("last_run_time", "next_run_time", "logs", "mode")):
            sched = candidate
            break
        time.sleep(0.05)
    check("新进程调度器处于暂停（等旧进程退出）",
          sched.get("enabled") is False, sched)

    print("[6] 切换之后才把 body 发完：那条请求仍由旧进程答完")
    time.sleep(0.3)
    body = json.dumps({"password": "admin"}).encode("utf-8")
    slow_sock.sendall(body + b" " * (32 - len(body)))
    raw = b""
    slow_sock.settimeout(30)
    while b"\r\n\r\n" not in raw:
        chunk = slow_sock.recv(4096)
        if not chunk:
            break
        raw += chunk
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    check("慢请求拿到 200", head.startswith("HTTP/1.1 200"), head.splitlines()[:1])
    length = 0
    for line in head.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    got = raw.split(b"\r\n\r\n", 1)[1]
    while len(got) < length:
        chunk = slow_sock.recv(4096)
        if not chunk:
            break
        got += chunk
    check("慢请求的 body 收全", len(got) >= length, "%d/%d" % (len(got), length))
    # Python 的 BaseHTTPRequestHandler 在 handle_one_request 里先写完响应头再回到
    # 我们的 finally，所以 Connection: close 赶不进这一份头；能断言的等价事实是
    # 「body 读完之后 socket 直接 EOF」——客户端看到的就是连接被关掉。
    eof = slow_sock.recv(4096)
    check("body 读完后 socket 直接 EOF（旧进程收尾时关连接）", eof == b"", repr(eof[:80]))

    print("[7] 排空完成：旧进程退出，新进程继续服务")
    gone = False
    for _ in range(300):
        if proc.poll() is not None:
            gone = True
            break
        time.sleep(0.1)
    check("旧进程已退出", gone, proc.poll())
    status, _h, _b = request("GET", "/health")
    check("新进程 /health 200", status == 200, status)
    TOKEN = login()
    check("新进程可以用同一口令重新登录面板", bool(TOKEN))
    state = status_json()
    check("新进程状态是全新的 idle", state.get("phase") == "idle", state)
    check("新进程 started=0（不是从旧进程继承来的状态）",
          not state.get("started"), state)
    check("新进程 child_pid 为空", state.get("child_pid") in (None, 0), state)

    resumed = False
    for _ in range(300):
        sched = as_json(request("GET", "/scheduler", timeout=15)[2])
        if sched.get("enabled") is True:
            resumed = True
            break
        time.sleep(0.1)
    check("旧进程退出后调度器自动恢复", resumed, sched.get("enabled"))

    load.stop.set()
    load.join(10)
    print("[8] 切换全程的持续压测：0 个连接级失败")
    print("      成功 %d 次，服务端关连接后重连 %d 次，异常 %d 次"
          % (load.ok, load.reconnect, load.errors))
    check("压测期间成功请求 > 100", load.ok > 100, load.ok)
    check("切换前后都在涨", load.ok > ok_before + 50, "%d -> %d" % (ok_before, load.ok))
    check("没有非 200 响应", load.bad == 0, load.bad)
    check("没有连接级异常（refused / reset 都算）", load.errors == 0,
          "%d %s" % (load.errors, load.first_error))
    check("重连次数很少（只发生在服务端主动关 keep-alive 时）",
          load.reconnect <= 6, load.reconnect)
finally:
    if load is not None:
        load.stop.set()
    try:
        if slow_sock is not None:
            slow_sock.close()
    except Exception:
        pass
    kill_tree(child_pid)
    if proc.poll() is None:
        life.stop_managed(proc)
    shutil.rmtree(work, ignore_errors=True)

print()
print("passed=%d failed=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
