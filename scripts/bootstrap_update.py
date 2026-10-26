#!/usr/bin/env python3
"""一次性把旧版网关换成支持热切换的版本；失败自动回滚。

旧版（没有 /hotswap 的进程）没法把监听 socket 交出去，所以这一步必然有一次
真实重启：网关进程退出的那几百毫秒到两三秒内，新连接会被拒绝。跑完这一次，
以后每次更新都走 scripts/hotswap_update.py，接管 0.4 秒左右、在途请求不丢。

这个脚本把那次重启做安全：先确认现在健康，重启后必须在时限内回到 200 且
/hotswap/status 可用；否则还原 *.bak-<后缀> 再重启一次，回到重启前的样子。

    python scripts/bootstrap_update.py
    python scripts/bootstrap_update.py --backup-suffix bak-20261026-prehotswap
    python scripts/bootstrap_update.py --dry-run
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SUFFIX_DEFAULT = "bak-prehotswap"


def _http(method, url, payload=None, timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)


def _health(port, timeout=3):
    return _http("GET", "http://127.0.0.1:%d/health" % port, timeout=timeout)


def _hotswap_ready(port):
    status, _body = _http("GET", "http://127.0.0.1:%d/hotswap/status" % port, timeout=5)
    return status in (200, 401)


def _panel_password(args):
    if args.password:
        return args.password
    env = os.environ.get("WB_PANEL_PASSWORD")
    if env:
        return env
    path = os.path.join(ROOT, "panel", "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("panel_password") or "admin"
    except Exception:
        return "admin"


def _wait_healthy(port, deadline_s):
    started = time.time()
    while time.time() - started < deadline_s:
        status, body = _health(port, timeout=2)
        if status == 200:
            return time.time() - started, body
        time.sleep(0.2)
    return None, None


def _restart_or_start(panel_port, timeout):
    """面板的 restart 只在服务运行时可用；进程已经死透时它会让你点「启动」。"""
    status, body = _http("POST", "http://127.0.0.1:%d/api/restart" % panel_port,
                         {}, timeout=timeout)
    if '"ok": true' in body or '"ok":true' in body:
        return status, body
    if "未在运行" in body or "not running" in body:
        status, body = _http("POST", "http://127.0.0.1:%d/api/start" % panel_port,
                             {}, timeout=timeout)
    return status, body


def _restore(suffix):
    restored = []
    for rel in ("wb_proxy.py", "dashboard.html", os.path.join("panel", "wb_control_panel.py")):
        backup = os.path.join(ROOT, rel + "." + suffix)
        if not os.path.exists(backup):
            continue
        with open(backup, "rb") as src, open(os.path.join(ROOT, rel), "wb") as dst:
            dst.write(src.read())
        restored.append(rel)
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--panel-port", type=int, default=8790)
    parser.add_argument("--password", default="")
    parser.add_argument("--backup-suffix", default=SUFFIX_DEFAULT)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    status, body = _health(args.port)
    if status != 200:
        print("网关现在不健康（/health -> %s），先处理它再来换版本" % (status or "无响应"))
        return 2
    if _hotswap_ready(args.port):
        print("当前进程已经支持 /hotswap，直接用它，不用重启：")
        print("    python scripts/hotswap_update.py")
        return 0
    print("当前进程是旧版（没有 /hotswap）。这一步必须重启一次，之后都不用再重启。")
    if args.dry_run:
        print("[dry-run] 会 POST http://127.0.0.1:%d/api/restart 并等待 /health 回来" % args.panel_port)
        return 0

    password = _panel_password(args)
    status, body = _http("POST", "http://127.0.0.1:%d/panel/login" % args.port,
                         {"password": password})
    if status != 200:
        print("面板登录失败（%s）：%s" % (status, body[:200]))
        return 2

    started = time.time()
    status, body = _http("POST", "http://127.0.0.1:%d/api/restart" % args.panel_port,
                         {}, timeout=args.timeout + 30)
    print("面板 /api/restart -> %s %s" % (status, body[:180]))
    # 面板已经明确报错时不必再等满整个时限：它说启动失败，就是不会起来了。
    panel_says_ok = '"ok": true' in body or '"ok":true' in body
    gap, _health_body = (_wait_healthy(args.port, args.timeout) if panel_says_ok
                         else (None, None))
    if gap is None:
        if panel_says_ok:
            print("重启后 %.0f 秒内 /health 没回来，开始回滚" % args.timeout)
        else:
            print("面板报告重启失败，直接回滚")
        restored = _restore(args.backup_suffix)
        if not restored:
            print("找不到 *.%s 备份，未回滚；检查网关日志 logs/service.log" % args.backup_suffix)
            return 1
        print("已还原：%s" % ", ".join(restored))
        status, body = _restart_or_start(args.panel_port, args.timeout + 30)
        print("回滚后 /api/restart -> %s %s" % (status, body[:180]))
        gap, _ = _wait_healthy(args.port, args.timeout)
        if gap is None:
            print("回滚后网关仍未回来，看 logs/service.log 与 panel/logs/panel.log")
            return 1
        print("回滚完成，网关已恢复（%.1f 秒）" % gap)
        return 1

    total = time.time() - started
    if not _hotswap_ready(args.port):
        print("网关已恢复（%.1f 秒），但 /hotswap/status 不可用：新代码没生效？" % total)
        return 1
    print("完成：重启耗时 %.1f 秒，新进程已支持 /hotswap。" % total)
    print("以后改完代码只跑：python scripts/hotswap_update.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
