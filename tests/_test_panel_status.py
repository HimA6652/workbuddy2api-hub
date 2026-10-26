"""面板状态机：热切换之后别把死 PID 当成本机进程。

netstat / Get-NetTCPConnection 报的 OwningPid 是 socket 的**创建者**。热切换把监听
句柄交给新进程之后，这个 PID 还是那个早就退出的旧进程——面板于是显示一个幽灵 PID，
"停止"也照着它去结束，真正在跑的进程反而不动。这里把 Controller.snapshot 的判定钉死：

[1] 属主 PID 已死、run.pid 活着 -> 报 run.pid 指向的进程；
[2] 属主 PID 活着 -> 就用它；
[3] 属主 PID 已死、run.pid 也没有 -> 状态照旧 running，但不报 PID；
[4] 健康检查失败且端口被别的程序占着 -> conflict，detail 里带占用者 PID；
[5] config.json 坏掉时 load_config 只能用默认值，不能以 NameError 收场。

    python tests/_test_panel_status.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL = os.path.join(ROOT, "panel", "wb_control_panel.py")

PASS = 0
FAIL = 0


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (label, extra))


def load_panel(path=PANEL):
    spec = importlib.util.spec_from_file_location("panel_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot_with(mod, port_pid=None, runpid=None, live=(), health=None,
                  image=None):
    """把外部世界（端口属主 / 存活 / run.pid / 健康检查）全换成固定值。"""
    live = set(int(p) for p in live)
    mod.listening_pid = lambda port: port_pid
    mod.pid_alive = lambda pid: int(pid) in live
    mod.read_pidfile = lambda: runpid
    mod.probe_health = lambda timeout=1.6: (health or ("ok", {"ok": True, "accounts": 0}))
    mod.proc_create_time = lambda pid: time.time() - 5.0
    mod.proc_image = (lambda pid: image) if image else (lambda pid: "C:\\Python\\python.exe")
    mod.PORT = 8788
    mod.CTRL._cache = (0.0, None)
    return mod.CTRL.snapshot(force=True, ignore_busy=True)


print("[1] 热切换之后的幽灵 PID")
mod = load_panel()
state = snapshot_with(mod, port_pid=4242, runpid=5555, live=(5555,))
check("状态仍然是 running", state["state"] == "running", state)
check("报的是 run.pid 里的新进程", state["pid"] == 5555, state["pid"])
check("不是那个已经退出的属主", state["pid"] != 4242, state["pid"])
check("uptime 正常给出", isinstance(state["uptime_sec"], int), state["uptime_sec"])

print("[2] 属主还活着就用属主")
state = snapshot_with(mod, port_pid=4242, runpid=5555, live=(4242, 5555))
check("pid 取端口属主", state["pid"] == 4242, state["pid"])

print("[3] 两边都不可信时不报 PID")
state = snapshot_with(mod, port_pid=4242, runpid=5555, live=())
check("状态 running", state["state"] == "running", state)
check("pid 为空", state["pid"] is None, state["pid"])
check("uptime 也为空", state["uptime_sec"] is None, state["uptime_sec"])

print("[4] 端口被别的程序占着")
state = snapshot_with(
    mod, port_pid=9999, runpid=5555, live=(9999,), health=("down", None),
    image="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
check("判为 conflict", state["state"] == "conflict", state)
check("detail 里写着占用者 PID", "9999" in (state["detail"] or ""), state["detail"])
check("冲突时不给 health", state["health"] is None, state["health"])

print("[5] config.json 坏掉")
tmp = tempfile.mkdtemp(prefix="wb-panel-cfg-")
try:
    pkg = os.path.join(tmp, "panel")
    os.makedirs(pkg)
    shutil.copy2(PANEL, os.path.join(pkg, "wb_control_panel.py"))
    with open(os.path.join(pkg, "config.json"), "w", encoding="utf-8") as fh:
        fh.write('{"panel_port": ')
    probe = (
        "import importlib.util, sys\n"
        "p = sys.argv[1]\n"
        "spec = importlib.util.spec_from_file_location('bad_cfg_panel', p)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('PORT=%d' % mod.PORT)\n"
        "print('LAN=%r' % mod.CFG.get('lan'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe,
                           os.path.join(pkg, "wb_control_panel.py")],
                          capture_output=True, text=True, timeout=60)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("解释器没崩", proc.returncode == 0, out.strip()[-400:])
    check("退回到默认端口 8788", "PORT=8788" in out, out.strip()[-200:])
    check("没有 NameError", "NameError" not in out, out.strip()[-200:])
    # log_panel 写的是 <project>/logs/panel.log（project = 这个临时目录的上一层）
    plog = os.path.join(tmp, "logs", "panel.log")
    text = ""
    if os.path.exists(plog):
        with open(plog, "r", encoding="utf-8") as fh:
            text = fh.read()
    check("panel.log 里写明了原因", "配置读取失败" in text, text.strip()[-200:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
