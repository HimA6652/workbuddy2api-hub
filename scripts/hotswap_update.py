#!/usr/bin/env python3
"""零中断更新网关：把正在跑的 wb_proxy 的监听 socket 交给新进程。

网关自己提供了两个接口，这个脚本只是把它们串成一次更新：

  POST /hotswap          起一个接管监听的新进程，本进程把在跑的请求做完再退出
  GET  /hotswap/status   看这次交接进行到哪一步（idle / starting / draining / failed）

为什么不会中断：监听 socket 是内核对象，句柄被新进程继承之后，旧进程退出也不会
让端口消失 —— 端口从来没有关闭过一次，内核里的 accept 队列也没丢过。旧进程这时
只是不再 accept，把手上的请求跑完（默认最多等 600 秒）才退出。

两种用法：

    # 一次正常的热更新（旧版本已经支持 /hotswap）
    python scripts/hotswap_update.py --password <面板口令>

    # 第一次：正在跑的旧版本还不知道 /hotswap，只能重启一次
    python scripts/hotswap_update.py --password <面板口令> --bootstrap

更新流程永远是：先改文件 → 再跑这个脚本。新进程是从磁盘上重新读代码的，所以
脚本跑起来的那一刻，改好的代码就生效了。如果新代码起不来（语法错、端口问题），
旧进程会继续服务，日志里留一行 hotswap failed，业务不受影响。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)


def _http(method, url, payload=None, headers=None, timeout=15):
    """返回 (status, body_text)。连不上时抛 OSError，由调用方决定怎么处理。"""
    data = None
    head = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        head["Content-Type"] = "application/json"
    if headers:
        head.update(headers)
    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _json(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


LISTEN_RE = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)


def listening_pid(port):
    """谁真的握着这个端口。

    netstat 的 OwningPid 是 socket 的创建者：热切换之后它仍然指向那个已经退出
    的旧进程，而句柄活在接管者的进程里。所以逐个候选复核存活，全死就当没查到，
    让调用方退回到 /hotswap/status 报的 pid（那是新进程自己说的，最可信）。
    """
    try:
        out = os.popen("netstat -ano -p tcp").read()
    except Exception:
        return None
    candidates = []
    for line in out.splitlines():
        match = LISTEN_RE.match(line)
        if match and int(match.group(1)) == int(port):
            pid = int(match.group(2))
            if pid not in candidates:
                candidates.append(pid)
    for pid in candidates:
        if pid_alive(pid):
            return pid
    return None


def load_password(args):
    if args.password:
        return args.password
    env = os.environ.get("WB_PANEL_PASSWORD")
    if env:
        return env
    cfg = os.path.join(PROJECT, "panel", "config.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            pwd = (json.load(fh) or {}).get("panel_password")
        if pwd:
            return str(pwd)
    except Exception:
        pass
    return None


def login(base, password):
    status, text = _http("POST", base + "/panel/login", {"password": password})
    if status != 200:
        raise SystemExit("面板登录失败（HTTP %s）：%s" % (status, text[:200]))
    token = _json(text).get("token")
    if not token:
        raise SystemExit("面板登录没有返回 token：%s" % text[:200])
    return token


def status(base, token=None):
    headers = {"X-Panel-Token": token} if token else {}
    return _http("GET", base + "/hotswap/status", headers=headers, timeout=10)


def supports_hotswap(base):
    status_code, _text = status(base)
    return status_code in (200, 401)


def bootstrap(base, panel_base, port):
    """旧版本还不认识 /hotswap：走面板重启一次，并量出真实断档时长。"""
    print("  正在跑的版本还不支持 /hotswap，先做一次重启（这一次会断）……")
    started = time.time()
    try:
        _http("POST", panel_base + "/api/restart", timeout=90)
    except OSError as exc:
        raise SystemExit("面板重启请求失败：%s（面板没开？可以用 --panel-port 指定）" % exc)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            status_code, text = _http("GET", base + "/health", timeout=5)
            if status_code == 200:
                return time.time() - started
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit("重启后 %d 秒仍没有 /health，请到面板看日志" % int(time.time() - started))


def write_pidfile(path, pid):
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(pid))
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="零中断地把监听交给新版本")
    ap.add_argument("--host", default="127.0.0.1", help="网关地址（默认本机）")
    ap.add_argument("--port", type=int, default=8788, help="网关端口（默认 8788）")
    ap.add_argument("--panel-port", type=int, default=8790, help="控制面板端口（默认 8790）")
    ap.add_argument("--password", default=None,
                    help="面板口令；省略时读 WB_PANEL_PASSWORD 或 panel/config.json")
    ap.add_argument("--pidfile", default=None,
                    help="交接后把新 PID 写进去（默认 panel/run.pid，存在才写）")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="等旧进程把手上请求做完的上限秒数（默认 600）")
    ap.add_argument("--api-key", default=None,
                    help="给了就顺手用这个 key 验一次 /v1/models")
    ap.add_argument("--bootstrap", action="store_true",
                    help="旧版本不支持 /hotswap 时，允许先用面板重启一次")
    args = ap.parse_args(argv)

    base = "http://%s:%d" % (args.host, args.port)
    panel_base = "http://%s:%d" % (args.host, args.panel_port)
    pidfile = args.pidfile
    if pidfile is None:
        candidate = os.path.join(PROJECT, "panel", "run.pid")
        pidfile = candidate if os.path.exists(candidate) else ""

    started_all = time.time()
    print("网关 %s" % base)

    if not supports_hotswap(base):
        if not args.bootstrap:
            print("  正在跑的版本还不认识 /hotswap 和 /hotswap/status。")
            print("  第一次升级只能重启一次，加 --bootstrap 让脚本自动做：")
            print("    python scripts/hotswap_update.py --bootstrap")
            print("  重启之后，这台机器上所有后续更新都是零中断的。")
            return 2
        gap = bootstrap(base, panel_base, args.port)
        print("  重启完成，断档 %.2f 秒（这一次是有感的；之后不会再有了）" % gap)
        if not supports_hotswap(base):
            raise SystemExit("重启后仍然没有 /hotswap —— 磁盘上跑的还是旧代码")
        code, text = _http("GET", base + "/health", timeout=10)
        print("  新代码已经在跑（/health %s），这次更新到此完成" % code)
        print("  以后改完文件再跑这条命令，就是零中断的了")
        return 0

    password = load_password(args)
    if not password:
        raise SystemExit("需要面板口令：--password、WB_PANEL_PASSWORD 或 panel/config.json")

    token = login(base, password)
    code, text = status(base, token)
    state = _json(text)
    if code != 200:
        raise SystemExit("读不到 /hotswap/status（HTTP %s）：%s" % (code, text[:200]))
    if state.get("phase") not in (None, "idle"):
        raise SystemExit("上一次交接还没结束（phase=%s），等它完成再跑"
                         % state.get("phase"))

    old_pid = state.get("pid") or listening_pid(args.port)
    print("  当前进程 %s，开始交接……" % old_pid)
    handover_start = time.time()
    code, text = _http("POST", base + "/hotswap", headers={"X-Panel-Token": token},
                       payload=None, timeout=60)
    ack = _json(text)
    if code != 200 or not ack.get("started"):
        raise SystemExit("交接没有启动（HTTP %s）：%s" % (code, text[:300]))

    print("  新进程已拉起：%s" % (ack.get("state") or {}).get("child_pid"))

    # 面板会话在内存里，新进程会拒掉旧 token —— 401 就是「已经在由新进程答话」。
    deadline = time.time() + 180
    handover = 0.0
    while time.time() < deadline:
        try:
            code, text = status(base, token)
        except OSError:
            time.sleep(0.05)
            continue
        if code == 401:
            handover = time.time() - handover_start
            break
        if code == 200 and _json(text).get("phase") == "failed":
            raise SystemExit("新进程没能接手，旧进程仍在服务：%s"
                             % _json(text).get("error"))
        time.sleep(0.05)
    if not handover:
        raise SystemExit("等了 180 秒新进程也没接手，去看 service.log 的 hotswap 行")
    print("  新进程已接手，端口一秒都没关（%.2f 秒）" % handover)

    token = login(base, password)
    code, text = status(base, token)
    new_state = _json(text)
    new_pid = new_state.get("pid") or listening_pid(args.port)
    print("  现在服务的是 PID %s" % new_pid)

    if pidfile and new_pid:
        if write_pidfile(pidfile, new_pid):
            print("  已把新 PID 写进 %s" % pidfile)

    if old_pid and pid_alive(old_pid):
        print("  旧进程还在把手上请求做完（最多等 %d 秒）……" % int(args.timeout))
        deadline = time.time() + args.timeout
        while time.time() < deadline and pid_alive(old_pid):
            time.sleep(0.1)
        drained = not pid_alive(old_pid)
        print("  旧进程%s" % ("已退出" if drained else "仍在跑（已超过 --timeout）"))
    else:
        drained = True

    code, text = _http("GET", base + "/health", timeout=10)
    health = _json(text)
    print("  /health %s accounts=%s ready=%s"
          % (code, health.get("accounts"), health.get("accounts_ready")))
    if args.api_key:
        code, text = _http("GET", base + "/v1/models",
                           headers={"Authorization": "Bearer " + args.api_key})
        models = _json(text).get("data") or []
        print("  /v1/models %s（%d 个模型）" % (code, len(models)))
        if code != 200:
            raise SystemExit("带 key 的 /v1/models 没通过，key 或新版本有问题")

    print("完成：%s -> %s，总耗时 %.2f 秒，业务无中断"
          % (old_pid, new_pid, time.time() - started_all))
    if not drained:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
