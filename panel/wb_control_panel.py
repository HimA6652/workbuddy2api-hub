#!/usr/bin/env python3
"""wb_control_panel.py —— WorkBuddy2API-Hub 本机可视化控制面板

职责：
  1. 提供 Web 面板：启动 / 停止 / 重启三个按钮，带状态反馈与禁用逻辑；
  2. 每 2 秒轮询服务状态（启动中 / 停止中自动提速到 0.8 秒）；
  3. 以后台方式拉起 wb_proxy.py（pythonw + DETACHED_PROCESS），不弹任何窗口；
  4. 停止优先走网关自带的 /shutdown 优雅退出，失败才强制结束进程。

只依赖标准库，Windows 优先；非 Windows 也能跑（进程发现降级）。
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_DIR = os.path.join(PROJECT, "logs")
SERVICE_LOG = os.path.join(LOG_DIR, "service.log")
PANEL_LOG = os.path.join(LOG_DIR, "panel.log")
PID_FILE = os.path.join(BASE, "run.pid")

DEFAULT_CONFIG = {
    "panel_host": "127.0.0.1",
    "panel_port": 8790,
    "service_port": 8788,
    "python_exe": "",
    # 局域网共享：True 时网关按 --lan 启动（监听 0.0.0.0 + 强制 API Key），
    # 局域网内其他电脑/手机可用 http://<本机IP>:<service_port>/v1 直连。
    "lan": False,
    "panel_password": "admin",
    "start_timeout": 35,
    "stop_timeout": 15,
    "open_browser": True,
}

IS_WIN = os.name == "nt"

# --------------------------------------------------------------------------
# 配置 & 日志
# --------------------------------------------------------------------------


def log_panel(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(PANEL_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception as exc:  # 配置坏了也别让面板起不来
            # log_panel 定义在上面，这里可以放心调用；早期版本把它放在后面，
            # 于是一个坏掉的 config.json 会以 NameError 收场，真正的解析错误
            # 反而被盖住。
            log_panel("配置读取失败，使用默认值: %s" % exc)
    return cfg


CFG = load_config()
PORT = int(CFG["service_port"])


def log_tail(path, lines=60):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            raw = fh.read().decode("utf-8", "replace")
        return "\n".join(raw.splitlines()[-lines:])
    except Exception as exc:
        return "读取日志失败: %s" % exc


# --------------------------------------------------------------------------
# Windows 进程工具（纯 ctypes，不依赖 psutil / wmic / netstat 本地化输出）
# --------------------------------------------------------------------------


class _TCPROW(ctypes.Structure):
    _fields_ = [
        ("dwState", ctypes.c_ulong),
        ("dwLocalAddr", ctypes.c_ulong),
        ("dwLocalPort", ctypes.c_ulong),
        ("dwRemoteAddr", ctypes.c_ulong),
        ("dwRemotePort", ctypes.c_ulong),
        ("dwOwningPid", ctypes.c_ulong),
    ]


def listening_pid(port):
    """返回监听该端口的进程 PID，没有则 None。"""
    if not IS_WIN:
        return None
    iphlpapi = ctypes.windll.iphlpapi
    kernel32 = ctypes.windll.kernel32
    AF_INET, TCP_TABLE_OWNER_PID_LISTENER = 2, 3
    size = ctypes.c_ulong(0)
    try:
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                     TCP_TABLE_OWNER_PID_LISTENER, 0)
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                           TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != 0:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value
        rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_ulong),
                           ctypes.POINTER(_TCPROW))
        for i in range(count):
            row = rows[i]
            if socket.ntohs(row.dwLocalPort & 0xFFFF) == port:
                return int(row.dwOwningPid)
    except Exception:
        return None
    finally:
        del kernel32
    return None


def _open_process(pid, access):
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    return kernel32.OpenProcess(access, False, int(pid))


def pid_alive(pid):
    if not pid:
        return False
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    SYNCHRONIZE = 0x00100000
    handle = _open_process(pid, SYNCHRONIZE)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def proc_create_time(pid):
    """进程创建时间（epoch 秒），用于算运行时长；拿不到返回 None。"""
    if not IS_WIN or not pid:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        ft = ctypes.c_ulonglong()
        dummy = ctypes.c_ulonglong()
        ok = ctypes.windll.kernel32.GetProcessTimes(
            ctypes.c_void_p(handle), ctypes.byref(ft), ctypes.byref(dummy),
            ctypes.byref(dummy), ctypes.byref(dummy))
        if not ok:
            return None
        return ft.value / 1e7 - 11644473600.0
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def proc_image(pid):
    """进程可执行文件全路径；用于判断端口占用者是不是 Python 进程。"""
    if not IS_WIN or not pid:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def kill_pid(pid):
    if not pid:
        raise RuntimeError("没有可结束的 PID")
    if not IS_WIN:
        os.kill(pid, 9)
        return True
    PROCESS_TERMINATE, SYNCHRONIZE = 0x0001, 0x00100000
    handle = _open_process(pid, PROCESS_TERMINATE | SYNCHRONIZE)
    if not handle:
        raise RuntimeError("无法打开进程 PID=%s（权限不足或进程已退出）" % pid)
    try:
        ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
        ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 5000)
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


# --------------------------------------------------------------------------
# 网关探针（HTTP）
# --------------------------------------------------------------------------


def http_json(url, payload=None, headers=None, timeout=3.0):
    data = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    # 面板与网关同在 127.0.0.1，绝不能走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


def probe_health(timeout=1.6):
    """返回 (kind, payload)。kind: ok / foreign / down。

    foreign = 端口有人应答但不是本网关（含非 JSON 响应）；
    down    = 连不上 / 超时；此时若端口仍在监听，由调用方判为 unhealthy。
    """
    try:
        data = http_json("http://127.0.0.1:%d/health" % PORT, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return "foreign", {"error": "HTTP %s" % exc.code}
    except json.JSONDecodeError:
        return "foreign", {"raw": "端口有 HTTP 响应但不是 JSON，疑似其他程序"}
    except Exception:
        return "down", None
    if isinstance(data, dict) and "accounts" in data and data.get("ok"):
        return "ok", data
    return "foreign", data if isinstance(data, dict) else {"raw": str(data)[:200]}


def panel_login(password, timeout=3.0):
    try:
        rep = http_json("http://127.0.0.1:%d/panel/login" % PORT,
                        {"password": password}, timeout=timeout)
        return rep.get("token") or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# 状态机 & 操作
# --------------------------------------------------------------------------


class Controller:
    """单例：串行化所有操作，维护瞬时状态与最近一次操作结果。"""

    def __init__(self):
        self.lock = threading.Lock()          # 操作互斥
        self.state_lock = threading.Lock()    # 状态字段互斥
        self.transient = None                 # {"state":..., "until":..., "detail":...}
        self.last_action = {"action": None, "ok": None, "msg": "", "at": None}
        self._cache = (0.0, None)

    # ---- 状态 -------------------------------------------------------------
    def snapshot(self, force=False, ignore_busy=False):
        now = time.time()
        if not force and self._cache[1] and now - self._cache[0] < 0.4:
            return self._cache[1]
        with self.state_lock:
            transient = self.transient
            if transient and now > transient["until"]:
                transient = None
                self.transient = None

        port_pid = listening_pid(PORT)
        kind, health = probe_health()
        pid = port_pid
        known_pid = read_pidfile()
        with self.state_lock:
            busy = self.lock.locked() and not ignore_busy
            busy_action = (self.last_action.get("action") if busy else None)
            # 热切换之后，TCP 表里的 OwningPid 仍然是那个 socket 的创建者：
            # 它可能早就退出了，句柄却被接管的新进程继续用着。这种死 PID 不能
            # 当成本机进程，否则面板会显示一个幽灵 PID，"停止"也会照着它去结束。
            if pid is not None and not pid_alive(pid):
                pid = None
            # 端口没在听（或属主已死）时，才回落到 PID 文件；且必须复核进程仍活着，
            # 否则上一轮留下的 stale pid 会伪造出一个"幽灵进程"。
            if pid is None and known_pid and pid_alive(known_pid):
                pid = known_pid

        if transient:
            state = transient["state"]
            detail = transient.get("detail") or ""
        elif kind == "ok":
            state, detail = "running", "健康检查通过"
        elif port_pid:
            # 归属判定：PID 与记录一致，或占用者是 Python 解释器（面板/启动脚本
            # 拉起的网关都长这样）。不是自己的进程一律判冲突，绝不越权结束。
            image = proc_image(port_pid) or ""
            looks_ours = bool((known_pid and port_pid == known_pid) or
                              os.path.basename(image).lower() in ("python.exe", "pythonw.exe"))
            if looks_ours:
                state, detail = "unhealthy", "本网关端口在监听，但健康检查未通过"
            else:
                state, detail = "conflict", "端口被其他程序占用（%s，PID %s）" % (
                    os.path.basename(image) or "未知程序", port_pid)
        else:
            state, detail = "stopped", "端口未监听"

        started = proc_create_time(pid) if pid else None
        uptime = int(now - started) if started else None
        payload = {
            "state": state,
            "label": LABELS.get(state, state),
            "detail": detail,
            "pid": pid,
            "port": PORT,
            "uptime_sec": uptime,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else None,
            "health": health if state in ("running", "unhealthy") else None,
            "checked_at": time.strftime("%H:%M:%S"),
            "busy": busy,
            "busy_action": busy_action,
            "last_action": {k: v for k, v in self.last_action.items() if not k.startswith("_")},
            "can": self._can(state, busy),
        }
        self._cache = (now, payload)
        return payload

    def _can(self, state, busy):
        if busy or state in ("starting", "stopping"):
            return {"start": False, "stop": False, "restart": False}
        ok = {"start": False, "stop": False, "restart": False}
        if state in ("stopped", "error"):
            ok["start"] = True
        if state in ("running", "unhealthy"):
            ok["stop"] = True
            ok["restart"] = True
        return ok

    def _set_transient(self, state, seconds, detail=""):
        with self.state_lock:
            self.transient = {"state": state, "until": time.time() + seconds, "detail": detail}
        self._cache = (0.0, None)

    def _clear_transient(self):
        with self.state_lock:
            self.transient = None
        self._cache = (0.0, None)

    def _result(self, action, ok, msg):
        self.last_action = {"action": action, "ok": ok, "msg": msg,
                            "at": time.strftime("%H:%M:%S"),
                            "_pid": self.last_action.get("_pid")}
        log_panel("[%s] %s: %s" % (action, "成功" if ok else "失败", msg))

    # ---- 启动 -------------------------------------------------------------
    def start(self):
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "state": "busy", "msg": "已有操作正在执行，请稍后再试"}
        action = "start"
        try:
            snap = self.snapshot(force=True)
            if snap["state"] in ("running", "unhealthy"):
                return self._fail(action, "服务已在运行（PID %s），无需重复启动" % snap.get("pid"))
            if snap["state"] == "conflict":
                return self._fail(action, "端口 %d 被其他程序占用，请先释放端口" % PORT)

            python_exe = resolve_python()
            if not python_exe:
                return self._fail(action, "找不到可用的 Python 解释器（.venv/Scripts/pythonw.exe）")
            script = os.path.join(PROJECT, "wb_proxy.py")
            if not os.path.exists(script):
                return self._fail(action, "缺少 wb_proxy.py：%s" % script)

            self._set_transient("starting", int(CFG["start_timeout"]), "正在拉起网关进程…")
            pid = spawn_service(python_exe, script)
            self.last_action["_pid"] = pid
            log_panel("[start] 已派生进程 PID=%s (%s)" % (pid, python_exe))

            ok, msg = self._wait_ready(pid)
            if ok:
                # venv 的 pythonw.exe 是"重定向器"：它再派生真正的解释器后自己
                # 立即退出。所以 Popen 的 PID 不是真正的服务进程，一律以
                # 端口占用者为准，回写 PID 文件供停止/重启使用。
                actual = listening_pid(PORT) or pid
                self.last_action["_pid"] = actual
                write_pidfile(actual)
                self._clear_transient()
                if lan_enabled():
                    msg = "启动成功，PID %s，已监听 %s；局域网入口 http://%s:%d/v1" % (
                        actual, bind_label(), lan_ip(), PORT)
                else:
                    msg = "启动成功，PID %s，已监听 %s" % (actual, bind_label())
                return self._success(action, msg)
            self._clear_transient()
            return self._fail(action, msg)
        finally:
            self.lock.release()

    def _wait_ready(self, pid):
        deadline = time.time() + float(CFG["start_timeout"])
        while time.time() < deadline:
            time.sleep(0.5)
            kind, _ = probe_health(timeout=1.2)
            if kind == "ok":
                return True, "ok"
            # 不做"父进程已退出就失败"的早退判断：在 venv 重定向器 + 双进程
            # 结构下它必然先退出，真正的判据只有端口 + 健康检查。
        tail = log_tail(SERVICE_LOG, 12)
        return False, "启动超时（%ss 内未通过健康检查）。日志尾部：\n%s" % (
            CFG["start_timeout"], tail or "(空)")

    # ---- 停止 -------------------------------------------------------------
    def stop(self, reason="手动停止"):
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "state": "busy", "msg": "已有操作正在执行，请稍后再试"}
        action = "stop"
        try:
            snap = self.snapshot(force=True)
            pid = snap.get("pid")
            if snap["state"] == "stopped" or not pid:
                return self._success(action, "服务当前已是停止状态")
            self._set_transient("stopping", int(CFG["stop_timeout"]), "正在停止…")

            graceful = self._graceful_stop()
            if not graceful:
                log_panel("[stop] 优雅退出不可用，回退强制结束 PID=%s" % pid)
                try:
                    kill_pid(pid)
                except Exception as exc:
                    self._clear_transient()
                    return self._fail(action, "停止失败：%s" % exc)

            gone = self._wait_port_free(pid)
            self._clear_transient()
            if gone:
                how = "优雅退出" if graceful else "强制结束"
                return self._success(action, "%s成功，端口 %d 已释放" % (how, PORT))
            return self._fail(action, "停止超时：端口 %d 仍被占用" % PORT)
        finally:
            self.lock.release()

    def _graceful_stop(self):
        token = panel_login(CFG.get("panel_password") or "")
        if not token:
            return False
        try:
            http_json("http://127.0.0.1:%d/shutdown" % PORT, {},
                      headers={"X-Panel-Token": token}, timeout=3.0)
            return True
        except Exception:
            return False

    def _wait_port_free(self, pid):
        deadline = time.time() + float(CFG["stop_timeout"])
        while time.time() < deadline:
            if not listening_pid(PORT) and not pid_alive(pid):
                _clear_pidfile()
                return True
            time.sleep(0.3)
        return not listening_pid(PORT)

    # ---- 重启 -------------------------------------------------------------
    def restart(self):
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "state": "busy", "msg": "已有操作正在执行，请稍后再试"}
        action = "restart"
        try:
            snap = self.snapshot(force=True)
            if snap["state"] not in ("running", "unhealthy"):
                return self._fail(action, "服务未在运行，请直接点「启动」")
            self._set_transient("stopping", int(CFG["stop_timeout"]), "重启中：正在停止旧进程…")
            pid = snap.get("pid")
            graceful = self._graceful_stop()
            if not graceful:
                try:
                    kill_pid(pid)
                except Exception as exc:
                    self._clear_transient()
                    return self._fail(action, "重启失败（停止阶段）：%s" % exc)
            if not self._wait_port_free(pid):
                self._clear_transient()
                return self._fail(action, "重启失败：旧进程未能在超时内释放端口")

            self._set_transient("starting", int(CFG["start_timeout"]), "重启中：正在拉起新进程…")
            python_exe = resolve_python()
            pid2 = spawn_service(python_exe, os.path.join(PROJECT, "wb_proxy.py"))
            self.last_action["_pid"] = pid2
            ok, msg = self._wait_ready(pid2)
            self._clear_transient()
            if ok:
                actual = listening_pid(PORT) or pid2
                self.last_action["_pid"] = actual
                write_pidfile(actual)
                return self._success(action, "重启成功，新 PID %s（旧进程%s）"
                                     % (actual, "优雅退出" if graceful else "强制结束"))
            return self._fail(action, "重启失败（启动阶段）：%s" % msg)
        finally:
            self.lock.release()

    # ---- 结果封装 ---------------------------------------------------------
    def _success(self, action, msg):
        self._result(action, True, msg)
        # 操作刚结束时当前线程仍持锁，此处忽略 busy 才能回传正确的按钮可用性
        return {"ok": True, "state": "ok", "msg": msg,
                "status": self.snapshot(force=True, ignore_busy=True)}

    def _fail(self, action, msg):
        log_panel("[%s] 失败: %s" % (action, msg))
        self.last_action = {"action": action, "ok": False, "msg": msg,
                            "at": time.strftime("%H:%M:%S"),
                            "_pid": self.last_action.get("_pid")}
        return {"ok": False, "state": "error", "msg": msg,
                "status": self.snapshot(force=True, ignore_busy=True)}


LABELS = {
    "running": "运行中",
    "stopped": "已停止",
    "starting": "启动中",
    "stopping": "停止中",
    "unhealthy": "异常",
    "conflict": "端口冲突",
    "error": "异常",
}


def _clear_pidfile():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def write_pidfile(pid):
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except Exception:
        pass


def read_pidfile():
    try:
        if os.path.exists(PID_FILE):
            return int(open(PID_FILE, encoding="utf-8").read().strip() or 0) or None
    except Exception:
        return None
    return None


def resolve_python():
    """优先 .venv 的 pythonw（GUI 子系统，天然无控制台窗口）。"""
    configured = (CFG.get("python_exe") or "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    candidates += [
        os.path.join(PROJECT, ".venv", "Scripts", "pythonw.exe"),
        os.path.join(PROJECT, "python", "pythonw.exe"),
        os.path.join(PROJECT, ".venv", "Scripts", "python.exe"),
        os.path.join(PROJECT, "python", "python.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def lan_enabled():
    """网关是否按局域网共享模式启动。

    每次调用都重新读 config.json：面板进程是常驻的，直接改配置文件也应该在
    下一次「启动 / 重启」时立刻生效，不必先重启面板。
    """
    try:
        return bool(load_config().get("lan"))
    except Exception:
        return bool(CFG.get("lan"))


def service_argv(python_exe, script):
    """网关命令行。--lan 会同时把监听地址切成 0.0.0.0 并强制 API Key。"""
    argv = [python_exe, script, "--port", str(PORT)]
    if lan_enabled():
        argv.append("--lan")
    return argv


def bind_label():
    """给按钮反馈用的监听描述。"""
    if lan_enabled():
        return "0.0.0.0:%d（局域网共享，需 API Key）" % PORT
    return "127.0.0.1:%d（仅本机）" % PORT


def lan_ip():
    """挑一个本机局域网 IPv4：UDP connect 不发包，只让系统选出口地址。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            sock.close()
        except Exception:
            pass


def spawn_service(python_exe, script):
    """后台派生网关：无窗口 + 独立进程组 + 日志重定向。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    _rotate_if_big(SERVICE_LOG)
    log_fh = open(SERVICE_LOG, "ab", buffering=0)
    argv = service_argv(python_exe, script)
    log_fh.write(("\n===== [panel] %s 启动 %s %s =====\n"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(python_exe),
                     " ".join(argv[2:]))).encode("utf-8"))

    flags = 0
    startupinfo = None
    if IS_WIN:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    proc = subprocess.Popen(
        argv,
        cwd=PROJECT,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
        creationflags=flags,
        startupinfo=startupinfo,
    )
    try:
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
    except Exception:
        pass
    return proc.pid


def _rotate_if_big(path, limit=20 * 1024 * 1024):
    try:
        if os.path.exists(path) and os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except Exception:
        pass


CTRL = Controller()

# 面板自身的 HTTP 服务实例，由 main() 赋值。「关闭面板」按钮通过
# /api/exit 让面板进程优雅退出（先回响应，再 shutdown + os._exit），
# 网关进程不受影响，继续在后台运行。
HTTPD = None


def request_panel_exit(reason="手动关闭"):
    """回完响应后关闭面板：0.4s 让 body flush 到 socket，再退出进程。"""
    log_panel("[exit] 收到关闭面板请求：%s" % reason)

    def _bye():
        time.sleep(0.4)
        try:
            if HTTPD is not None:
                HTTPD.shutdown()
        except Exception:
            pass
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()

# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "WBControlPanel/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(200, CTRL.snapshot(force=True))
        if path == "/api/logs":
            qs = parse_qs(parsed.query)
            lines = int((qs.get("lines") or ["60"])[0])
            lines = max(10, min(300, lines))
            return self._json(200, {
                "service": log_tail(SERVICE_LOG, lines),
                "panel": log_tail(PANEL_LOG, min(lines, 80)),
            })
        if path == "/api/config":
            return self._json(200, {
                "panel_port": CFG["panel_port"],
                "service_port": PORT,
                "start_timeout": CFG["start_timeout"],
                "stop_timeout": CFG["stop_timeout"],
                "lan": lan_enabled(),
                "lan_base_url": ("http://%s:%d/v1" % (lan_ip(), PORT)) if lan_enabled() else None,
                "dashboard": "http://127.0.0.1:%d/?pwd=%s" % (PORT, urllib.parse.quote(str(CFG.get("panel_password") or ""))),
            })
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 排空 body，避免污染 keep-alive
        if path == "/api/start":
            return self._json(200, CTRL.start())
        if path == "/api/stop":
            return self._json(200, CTRL.stop())
        if path == "/api/restart":
            return self._json(200, CTRL.restart())
        if path == "/api/exit":
            # 只关面板本身；网关（service_port）不动，需要时重新运行
            # start-panel.vbs / start-panel.bat 再打开面板。
            request_panel_exit("面板按钮")
            return self._json(200, {"ok": True,
                                    "msg": "控制面板已关闭，网关继续在后台运行"})
        return self._json(404, {"error": "not found"})


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy2API-Hub 控制面板</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --line:#e3e6eb; --text:#1f2329; --muted:#6b7280;
    --green:#0f9d58; --green-bg:#e8f6ef; --gray:#6b7280; --gray-bg:#eef0f3;
    --amber:#b26a00; --amber-bg:#fdf3e3; --red:#c5221f; --red-bg:#fce8e6;
    --blue:#1a73e8; --blue-bg:#e8f0fe; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1120px;margin:0 auto;padding:24px 20px 40px}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px;flex-wrap:wrap}
  h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
  .link{color:var(--blue);text-decoration:none;font-size:13px;border:1px solid var(--line);
        background:var(--card);padding:7px 12px;border-radius:8px;white-space:nowrap}
  .link:hover{border-color:#c3d3f5;background:var(--blue-bg)}
  button.link{font:inherit;font-size:13px;font-weight:600;line-height:1.2;cursor:pointer}
  .link-exit{color:var(--red);border-color:#f0c6c3}
  .link-exit:not(:disabled):hover{border-color:var(--red);background:var(--red-bg)}
  .link-exit:disabled{opacity:.45;cursor:not-allowed}
  .header-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  body.panel-down .wrap{opacity:.4;filter:grayscale(.35);pointer-events:none}
  .banner{display:none;position:fixed;inset:0;z-index:60;align-items:center;justify-content:center;
          background:rgba(245,246,248,.78);backdrop-filter:blur(2px)}
  body.panel-down .banner{display:flex}
  .banner .box{background:var(--card);border:1px solid var(--line);border-radius:14px;
               padding:22px 26px;box-shadow:0 12px 40px rgba(16,24,40,.14);
               max-width:460px;text-align:center}
  .banner h3{margin:0 0 8px;font-size:16px;font-weight:650}
  .banner p{margin:0;color:var(--muted);font-size:13px;line-height:1.75}
  .grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}
  @media(max-width:880px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:18px 18px 16px}
  .card h2{font-size:13px;margin:0 0 14px;color:var(--muted);font-weight:600;
           letter-spacing:.6px;text-transform:uppercase}
  .status-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;
        font-size:15px;font-weight:650}
  .dot{width:9px;height:9px;border-radius:50%;background:currentColor;
       box-shadow:0 0 0 3px rgba(0,0,0,.06)}
  .pill.running{color:var(--green);background:var(--green-bg)}
  .pill.stopped{color:var(--gray);background:var(--gray-bg)}
  .pill.starting,.pill.stopping{color:var(--amber);background:var(--amber-bg)}
  .pill.unhealthy,.pill.error,.pill.conflict{color:var(--red);background:var(--red-bg)}
  .pill.running .dot{animation:pulse 1.8s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(15,157,88,.35)}70%{box-shadow:0 0 0 7px rgba(15,157,88,0)}100%{box-shadow:0 0 0 0 rgba(15,157,88,0)}}
  .detail{color:var(--muted);font-size:12.5px;margin-top:10px;min-height:18px}
  .kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-top:16px;
      border-top:1px solid var(--line);padding-top:14px}
  .kv div span{display:block;color:var(--muted);font-size:11.5px;margin-bottom:2px}
  .kv div b{font-weight:600;font-size:13.5px;font-variant-numeric:tabular-nums;word-break:break-all}
  .actions{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}
  @media(max-width:520px){.actions{grid-template-columns:1fr}}
  button{font:inherit;font-weight:600;border-radius:9px;padding:11px 14px;cursor:pointer;
         border:1px solid transparent;transition:.15s ease;display:flex;align-items:center;
         justify-content:center;gap:8px}
  button:disabled{cursor:not-allowed;opacity:.45}
  .btn-start{background:var(--green);color:#fff}
  .btn-start:not(:disabled):hover{background:#0c8a4c}
  .btn-stop{background:#fff;color:var(--red);border-color:#f0c6c3}
  .btn-stop:not(:disabled):hover{background:var(--red-bg)}
  .btn-restart{background:#fff;color:#374151;border-color:var(--line)}
  .btn-restart:not(:disabled):hover{background:#f1f3f6}
  .spin{width:13px;height:13px;border:2px solid currentColor;border-right-color:transparent;
        border-radius:50%;animation:rot .7s linear infinite}
  @keyframes rot{to{transform:rotate(360deg)}}
  .meta-line{display:flex;justify-content:space-between;align-items:center;margin-top:14px;
             color:var(--muted);font-size:12px;gap:10px;flex-wrap:wrap}
  .toggle{display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
  .health-tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
  .tag{font-size:11.5px;padding:3px 8px;border-radius:6px;background:#f1f3f6;color:#4b5563}
  .tag b{color:#111;font-weight:600}
  .logs{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
  @media(max-width:880px){.logs{grid-template-columns:1fr}}
  pre{margin:0;background:#0f172a;color:#d7e0ea;border-radius:8px;padding:12px 14px;
      font:12px/1.55 "Cascadia Mono",Consolas,monospace;height:270px;overflow:auto;white-space:pre-wrap;
      word-break:break-all}
  .pre-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .pre-head span{font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.4px}
  .toast{position:fixed;top:18px;right:18px;max-width:420px;padding:13px 16px;border-radius:10px;
         font-size:13px;line-height:1.55;box-shadow:0 8px 26px rgba(16,24,40,.14);
         opacity:0;transform:translateY(-8px);transition:.22s;pointer-events:none;z-index:50;
         white-space:pre-wrap}
  .toast.show{opacity:1;transform:none}
  .toast.ok{background:#fff;border-left:4px solid var(--green);color:#14532d}
  .toast.err{background:#fff;border-left:4px solid var(--red);color:#7f1d1d}
  .hint{font-size:12px;color:var(--muted);margin-top:6px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>WorkBuddy2API-Hub 控制面板</h1>
      <div class="sub">本机网关进程管理 · 端口 <b id="portText">8788</b> · 后台运行，无控制台窗口</div>
    </div>
    <div class="header-actions">
      <button class="link link-exit" id="btnExit" type="button"
              title="结束控制面板进程（网关继续在后台运行）">关闭面板</button>
      <a class="link" id="dashLink" href="#" target="_blank" rel="noopener">打开业务看板 ↗</a>
    </div>
  </header>

  <div class="grid">
    <section class="card">
      <h2>运行状态</h2>
      <div class="status-row">
        <span class="pill stopped" id="pill"><i class="dot"></i><span id="stateText">检测中…</span></span>
        <span class="detail" id="detailText" style="margin:0"></span>
      </div>

      <div class="kv">
        <div><span>进程 PID</span><b id="pidText">—</b></div>
        <div><span>运行时长</span><b id="uptimeText">—</b></div>
        <div><span>监听端口</span><b id="listenText">—</b></div>
        <div><span>最近检测</span><b id="checkedText">—</b></div>
      </div>

      <div class="health-tags" id="healthTags"></div>

      <div class="actions">
        <button class="btn-start" id="btnStart">启动</button>
        <button class="btn-stop" id="btnStop" disabled>停止</button>
        <button class="btn-restart" id="btnRestart" disabled>重启</button>
      </div>
      <div class="hint" id="hintText">启动后服务在后台运行，可关闭本面板不影响网关。</div>

      <div class="meta-line">
        <label class="toggle"><input type="checkbox" id="autoRefresh" checked> 自动刷新</label>
        <span id="refreshText">—</span>
      </div>
    </section>

    <section class="card">
      <h2>最近操作</h2>
      <div id="lastAction" style="min-height:96px;font-size:13px;line-height:1.7;color:var(--muted)">
        尚无操作记录。
      </div>
      <div style="border-top:1px solid var(--line);margin-top:14px;padding-top:12px">
        <h2 style="margin-bottom:8px">状态判定规则</h2>
        <div class="hint" style="line-height:1.8">
          端口监听 + <code>/health</code> 通过 → <b>运行中</b><br>
          启动命令已发出、尚未就绪 → <b>启动中</b>；停止中 → <b>停止中</b><br>
          端口在听但健康检查失败 → <b>异常</b>；端口被别的程序占用 → <b>端口冲突</b><br>
          端口空闲 → <b>已停止</b>
        </div>
      </div>
    </section>
  </div>

  <div class="logs">
    <section class="card">
      <div class="pre-head"><span>服务日志（logs/service.log 尾部）</span><span id="svcHint"></span></div>
      <pre id="svcLog">加载中…</pre>
    </section>
    <section class="card">
      <div class="pre-head"><span>操作记录（logs/panel.log 尾部）</span><span></span></div>
      <pre id="panelLog">加载中…</pre>
    </section>
  </div>
</div>

<div class="banner" id="exitBanner">
  <div class="box">
    <h3>控制面板已关闭</h3>
    <p>网关服务仍在后台运行，业务看板不受影响。<br>
       可以直接关闭本页面；需要再管理网关时，双击 <b>start-panel.vbs</b>（或 <b>start-panel.bat</b>）重新打开面板。</p>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let busy = null;           // 'start' | 'stop' | 'restart' | null
let timer = null;
let lastOkAt = 0;
let status = null;
let panelDown = false;     // 面板已关闭：停止全部轮询，页面进入「已退出」态

function fmtDuration(sec){
  if(sec === null || sec === undefined) return '—';
  if(sec < 60) return sec + ' 秒';
  const m = Math.floor(sec/60), h = Math.floor(m/60), d = Math.floor(h/24);
  if(h === 0) return m + ' 分 ' + (sec%60) + ' 秒';
  if(d === 0) return h + ' 小时 ' + (m%60) + ' 分';
  return d + ' 天 ' + (h%24) + ' 小时';
}

function toast(msg, ok){
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, ok ? 5000 : 12000);
}

function renderStatus(s){
  status = s;
  const pill = $('pill');
  pill.className = 'pill ' + (s.state === 'stopped' ? 'stopped' : s.state);
  $('stateText').textContent = s.label || s.state;
  $('detailText').textContent = s.detail || '';
  $('pidText').textContent = s.pid ? s.pid : '—';
  $('uptimeText').textContent = fmtDuration(s.uptime_sec);
  $('listenText').textContent = '127.0.0.1:' + s.port;
  $('checkedText').textContent = s.checked_at || '—';
  $('portText').textContent = s.port;

  const tags = $('healthTags');
  tags.innerHTML = '';
  if(s.health){
    const h = s.health;
    const items = [
      ['区域', h.realm || '—'],
      ['账号', (h.accounts ?? '—') + ' / 可用 ' + (h.accounts_ready ?? '—')],
      ['API Key', h.api_key_required ? '必需' : '本机免密']
    ];
    items.forEach(([k, v]) => {
      const d = document.createElement('div');
      d.className = 'tag';
      d.innerHTML = k + ' <b>' + String(v) + '</b>';
      tags.appendChild(d);
    });
  }

  const busyNow = busy || s.busy;
  $('btnStart').disabled = busyNow || !s.can.start;
  $('btnStop').disabled = busyNow || !s.can.stop;
  $('btnRestart').disabled = busyNow || !s.can.restart;

  if(s.last_action && s.last_action.msg){
    const a = s.last_action;
    $('lastAction').innerHTML =
      '<div style="color:' + (a.ok ? '#0f9d58' : '#c5221f') + ';font-weight:600;margin-bottom:4px">' +
      (a.ok ? '✓ ' : '✗ ') + (a.action === 'start' ? '启动' : a.action === 'stop' ? '停止' : a.action === 'restart' ? '重启' : a.action) +
      ' · ' + (a.ok ? '成功' : '失败') + '</div>' +
      '<div style="color:#4b5563;white-space:pre-wrap">' + (a.msg || '').replace(/</g,'&lt;') + '</div>' +
      '<div style="color:#9aa1ac;margin-top:6px;font-size:12px">' + (a.at || '') + '</div>';
  }

  const hint = {
    running: '网关正在接管请求；重启会短暂中断正在进行的对话。',
    stopped: '点「启动」以后台方式拉起网关，不弹任何窗口。',
    starting: '正在拉起进程并等待健康检查通过…',
    stopping: '正在停止，优先优雅退出，超时才强制结束。',
    unhealthy: '进程在听但健康检查失败，建议先看重启日志再重启。',
    conflict: '端口被其他程序占用，本面板不会去动别人的进程。',
    error: '上一次操作未成功，详情见右侧「最近操作」。'
  }[s.state] || '';
  $('hintText').textContent = hint;
}

async function refresh(){
  if(panelDown) return;
  try{
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 4000);
    const r = await fetch('/api/status', {signal: ctl.signal, cache: 'no-store'});
    clearTimeout(t);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    lastOkAt = Date.now();
    renderStatus(s);
    $('refreshText').textContent = '自动刷新 ' + (s.state === 'starting' || s.state === 'stopping' ? '0.8s' : '2s') + ' · 刚刚更新';
  }catch(e){
    $('refreshText').textContent = '面板连接中断，重试中…';
  }
  schedule();
}

function schedule(){
  clearTimeout(timer);
  if(!$('autoRefresh').checked) return;
  const fast = status && (status.state === 'starting' || status.state === 'stopping' || busy);
  timer = setTimeout(refresh, fast ? 800 : 2000);
}

async function refreshLogs(){
  if(panelDown) return;
  try{
    const r = await fetch('/api/logs?lines=80', {cache: 'no-store'});
    const d = await r.json();
    const svc = $('svcLog'), pnl = $('panelLog');
    const atBottom = el => el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    const stickSvc = atBottom(svc), stickPnl = atBottom(pnl);
    if(svc.dataset.v !== d.service){ svc.textContent = d.service || '(空)'; svc.dataset.v = d.service; if(stickSvc) svc.scrollTop = svc.scrollHeight; }
    if(pnl.dataset.v !== d.panel){ pnl.textContent = d.panel || '(空)'; pnl.dataset.v = d.panel; if(stickPnl) pnl.scrollTop = pnl.scrollHeight; }
  }catch(e){}
}

async function act(name){
  if(busy) return;
  busy = name;
  const label = {start:'启动', stop:'停止', restart:'重启'}[name];
  const btn = {start:$('btnStart'), stop:$('btnStop'), restart:$('btnRestart')}[name];
  const old = btn.innerHTML;
  btn.innerHTML = '<i class="spin"></i>' + label + '中…';
  $('btnStart').disabled = $('btnStop').disabled = $('btnRestart').disabled = true;
  $('hintText').textContent = '正在执行「' + label + '」，请稍候…';
  try{
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 60000);
    const r = await fetch('/api/' + name, {method:'POST', signal: ctl.signal,
                       headers:{'Content-Type':'application/json'}, body:'{}'});
    clearTimeout(t);
    const d = await r.json();
    if(d.status) renderStatus(d.status);
    toast(d.msg || (d.ok ? label + '成功' : label + '失败'), !!d.ok);
  }catch(e){
    toast(label + '请求失败：' + e.message, false);
  }finally{
    btn.innerHTML = old;
    busy = null;
    await refresh();
    await refreshLogs();
  }
}

async function exitPanel(){
  if(panelDown) return;
  if(!confirm('确定关闭控制面板？\n\n网关服务会继续在后台运行，不受影响；本页面随后将失去连接。')) return;
  const btn = $('btnExit');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '正在关闭…';
  try{
    await fetch('/api/exit', {method:'POST', cache:'no-store',
                 headers:{'Content-Type':'application/json'}, body:'{}'});
  }catch(e){
    // 面板进程退出会掐断连接，请求报错也可能已经关成功，下面用 /api/status 复核
  }
  let alive = true;
  for(let i = 0; i < 12; i++){
    await new Promise(res => setTimeout(res, 400));
    try{
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 1200);
      const res = await fetch('/api/status', {cache:'no-store', signal: ctl.signal});
      clearTimeout(t);
      alive = !!res.ok;
    }catch(e){ alive = false; }
    if(!alive) break;
  }
  if(alive){
    btn.disabled = false;
    btn.textContent = old;
    toast('关闭请求已发送，但面板仍在响应：请检查是否有其他实例占用端口', false);
    return;
  }
  panelDown = true;
  clearTimeout(timer);
  document.body.classList.add('panel-down');
  $('stateText').textContent = '面板已关闭';
  $('btnExit').textContent = '已关闭';
}

$('btnStart').onclick = () => act('start');
$('btnStop').onclick = () => act('stop');
$('btnRestart').onclick = () => act('restart');
$('btnExit').onclick = exitPanel;
$('autoRefresh').onchange = () => { if($('autoRefresh').checked){ refresh(); } else { clearTimeout(timer); $('refreshText').textContent = '自动刷新已暂停'; } };

fetch('/api/config').then(r => r.json()).then(c => {
  $('dashLink').href = c.dashboard;
  $('portText').textContent = c.service_port;
}).catch(()=>{});

refresh();
refreshLogs();
setInterval(refreshLogs, 3000);
setInterval(() => {
  if(!lastOkAt) return;
  const s = Math.round((Date.now() - lastOkAt)/1000);
  if(lastOkAt && $('autoRefresh').checked && s > 4 && !(status && (status.state==='starting'||status.state==='stopping')))
    $('refreshText').textContent = '上次成功更新 ' + s + ' 秒前';
}, 1000);
</script>
</body>
</html>
"""


def _single_instance_guard():
    """单实例保护：第二个实例立刻退出，避免多实例并发操作同一个网关。

    返回互斥体句柄（保持引用，进程退出才释放）；判定为重复实例时返回 False。
    用 Local\\ 命名空间，普通用户无需管理员权限。
    """
    if not IS_WIN:
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    name = "Local\\WBControlPanel-%d" % int(CFG["panel_port"])
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return True  # 创建失败不阻塞启动，交给端口绑定去报错
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    return handle


def _autostart_gateway(delay=1.0):
    """--start-service：开机自启时把网关一并拉起（LAN 共享网关要常驻）。"""
    time.sleep(delay)
    try:
        res = CTRL.start()
        log_panel("[autostart] %s：%s" % ("成功" if res.get("ok") else "未启动", res.get("msg")))
    except Exception as exc:  # noqa: BLE001 - 自启动失败不能拖垮面板
        log_panel("[autostart] 拉起网关异常：%s" % exc)


def main():
    global HTTPD
    os.makedirs(LOG_DIR, exist_ok=True)
    guard = _single_instance_guard()
    if guard is False:
        msg = "控制面板已在运行（端口 %d），本次重复启动已退出。" % CFG["panel_port"]
        log_panel(msg)
        print(msg)
        sys.exit(0)
    host = CFG["panel_host"]
    port = int(CFG["panel_port"])
    try:
        HTTPD = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        log_panel("面板启动失败：端口 %d 不可用（%s）" % (port, exc))
        print("端口 %d 已被占用：%s" % (port, exc))
        sys.exit(1)
    log_panel("控制面板已启动：http://%s:%d/ （网关端口 %d）" % (host, port, PORT))
    print("控制面板：http://%s:%d/   网关端口：%d" % (host, port, PORT))
    flags = sys.argv[1:]
    if CFG.get("open_browser") and "--no-browser" not in flags:
        threading.Timer(0.8, lambda: webbrowser.open("http://%s:%d/" % (host, port))).start()
    if "--start-service" in flags:
        threading.Thread(target=_autostart_gateway, daemon=True).start()
    try:
        HTTPD.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HTTPD.server_close()


if __name__ == "__main__":
    main()
