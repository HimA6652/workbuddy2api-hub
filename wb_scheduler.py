"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. Token 保活 (Keepalive)：定期检查 Token 剩余寿命，低于提前量自动调用 Refresh Token
   （提前量默认 48 小时，可用 WB_KEEPALIVE_LEAD_HOURS 调整）。
2. 每日签到 (Daily Checkin)：每日定时为所有国内版账号自动签到领积分。
3. 对话活跃上报 (Activity Report)：每日定时为国内版账号上报轻量会话事件，点亮连登与热力墙。
4. 猫猫旅行与日常结算 (Cat Travel & Welfare)：自动派出猫猫旅行或领取归来奖励。
5. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。
"""
import io
import os
import threading
import time
import random
import wb_tasks
import wb_quota_scan
import wb_settings
from wb_tasks import do_cat_travel

#: 网页活跃时段到期后还能补多久（分钟）。两个时段相隔 12 小时，所以 12 小时既覆盖
#: 「巡检被占住 / 进程重启 / 漏了一次 tick」，又不会把昨天没跑的时段补到今天。
DAILY_CHAT_SLOT_GRACE_MINUTES = 720
#: 批次截止的余量（秒）：每个账号有自己的预算，批次再给一份等待/收尾的余量。
DAILY_CHAT_BATCH_SLACK = 60
#: 被放弃的工作线程最多留这么多条。它们通常几十秒内自己结束（每一轮的网络调用
#: 都有 20~30 秒超时），留上限是为了「上游一直滴数据 / DNS 卡住」这种病态情况：
#: 到了上限就不再开新的工作线程，宁可这一轮跳过账号，也不让线程无限堆。
DAILY_CHAT_ABANDONED_MAX = 8


def _proxy_module():
    """返回**正在运行的那一份** wb_proxy。

    网关以 `python[w] wb_proxy.py` 启动时，主模块名为 `__main__`；模块里裸写
    `import wb_proxy` 会再加载一份副本，副本的 LOG_STORE 是 None、区域暂停状态
    也是另一份。调度器写的每条日志都会掉进黑洞，巡检看到的 `hold` 恒为 0。
    """
    import sys
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "add_log_entry") and hasattr(main, "LOG_BUFFER"):
        return main
    cached = sys.modules.get("wb_proxy")
    if cached is not None:
        return cached
    import wb_proxy as module
    return module
# Token 保活的提前量：距离到期多久就该续。
#
# 这个值必须大于巡检间隔，否则到期时刻落在两次巡检之间时，只能等到过期之后
# 才发现（保活变成「过期后补救」）。巡检默认每天 22:00 一次（keepalive_hours
# 只有 [22]），间隔 24 小时；旧代码写死 7200 秒（2 小时），于是只有到期时刻
# 恰好落在 20:00~22:00 的账号能提前续上，其余 ~92% 的到期时刻都要先在过期
# 状态下待 0~24 小时，期间所有上游请求都是 401。
KEEPALIVE_LEAD_DEFAULT_HOURS = 48.0
KEEPALIVE_LEAD_MIN_HOURS = 1.0
KEEPALIVE_LEAD_MAX_HOURS = 24.0 * 30


def keepalive_lead_seconds():
    """返回 Token 保活提前量（秒）。非法或越界的 WB_KEEPALIVE_LEAD_HOURS 回落默认值。"""
    raw = os.environ.get("WB_KEEPALIVE_LEAD_HOURS")
    if raw is None or not str(raw).strip():
        return KEEPALIVE_LEAD_DEFAULT_HOURS * 3600
    try:
        hours = float(str(raw).strip())
    except (TypeError, ValueError):
        return KEEPALIVE_LEAD_DEFAULT_HOURS * 3600
    if not (KEEPALIVE_LEAD_MIN_HOURS <= hours <= KEEPALIVE_LEAD_MAX_HOURS):
        return KEEPALIVE_LEAD_DEFAULT_HOURS * 3600
    return hours * 3600


def keepalive_due(expires_at, now=None, lead=None):
    """Token 是否该续期。

    expires_at <= 0 表示拿不到到期时间（opaque token，或上游回执里没有
    expiresAt），此时无法判断剩余寿命，按「该续」处理 —— 这正是保活的意义。
    旧代码写成 `if exp and ...`，这类账号因此永远不会被续期。
    """
    if lead is None:
        lead = keepalive_lead_seconds()
    if now is None:
        now = time.time()
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp <= 0:
        return True
    return (exp - now) < lead


def _settings_call(name, directory, default):
    """读一个设置项；函数不存在（旧版本/被裁剪）或读失败时回落默认值。

    P0-P3 这批开关都放在 wb_settings 里，但调度器不能因为某个开关还没落地就
    整轮不跑——缺函数、坏 JSON、读盘报错一律按默认值继续。
    """
    if not directory:
        return default
    func = getattr(wb_settings, name, None)
    if not callable(func):
        return default
    try:
        return func(directory)
    except Exception:
        return default

class Scheduler:
    def __init__(self, pool):
        self.pool = pool
        # 默认排程对齐 Sliverkiss/workbuddy2api (CST 24小时制)；实际值从
        # settings.json 的 schedule 段读取，缺省 / 非法回落这些默认值。
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.travel_hours = [9, 21]      # 每日 09:00 派出、21:00 领奖闭环
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活检查
        self.cat_hours = [1]             # 每日 01:00 夜猫子专属任务
        # 网页活跃（国际版每日活跃）的独立时段，HH:MM；空 = 当天随机抽两个。
        self.daily_chat_times = []
        # 生效时段是不是「今天随机抽的两个」（面板没填时段时的默认行为）。
        self.daily_chat_random_slots = True
        # 今日已触发的网页活跃时段，防 30 秒轮询重复触发；重启后清空，
        # 账号级 can_daily_chat() 仍会挡住当天已完成的账号。
        self._daily_chat_done = {}
        # 正在跑的网页活跃：uid8 -> {uid, started, budget}。面板要能看见「现在
        # 卡在哪个账号、还剩多少预算」，否则一次卡死只能是黑盒。
        self._daily_chat_live = {}
        # 被放弃的工作线程（卡在网络上、到点不再等它）。它们通常几十秒内自己结束，
        # 但「上游一直滴数据」这种病态情况会拖很久——留个上限，别让线程无限堆。
        self._daily_chat_abandoned = []
        # 启动后第一次巡检要补跑当天还没活跃的国际版账号：进程在时段内停过
        # （或调度线程被卡住）时，旧实现只能等下一个时段，最多再等 12 小时。
        self._catchup_pending = True
        self.all_hours = sorted(list(set(self.checkin_hours + self.travel_hours + self.keepalive_hours + self.cat_hours)))
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self._probe_thread = None
        self._scan_thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, and a manual trigger can also land on top of the hourly job.
        self._run_lock = threading.Lock()
        # The daily-activity batch can log from several workers at once.
        self._log_lock = threading.Lock()
        self._reload_schedule()
        self._calc_next_fire()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log the panel shows.
        wb_tasks.set_logger(self.log)

    def log(self, msg, tag=None):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self._log_lock:
            self.logs.append(entry)
            if len(self.logs) > 60:
                self.logs = self.logs[-60:]
        self._append_task_log(entry)
        try:
            proxy = _proxy_module()
            # 任务/打卡/旅行类条目同时进入 tasks 模块日志，方便分类查看
            effective_tag = tag
            if not effective_tag:
                lower = str(msg).lower()
                if any(k in lower for k in ("签到", "打卡", "活跃", "猫猫", "连登", "夜猫", "抽奖", "兑换")):
                    effective_tag = "tasks"
                else:
                    effective_tag = "scheduler"
            proxy.add_log_entry(f"[调度器] {msg}", tag=effective_tag)
        except Exception:
            pass

    def _task_log_path(self):
        """P2-4：调度日志落盘到 <root>/logs/tasks-YYYYMMDD.log。

        面板与 /logs 只留最近 2000 条（约 1.5 小时），而 09:00 那轮「到底签了
        几个、谁被跳过」恰恰是事后最想查的——内存环一冲就没了。
        """
        directory = getattr(self.pool, "dir", "") or ""
        if not directory:
            return ""
        root = os.path.dirname(os.path.abspath(str(directory)))
        return os.path.join(root, "logs", "tasks-%s.log" % time.strftime("%Y%m%d"))

    def _append_task_log(self, entry):
        path = self._task_log_path()
        if not path:
            return
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            with io.open(path, "a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
        except Exception:
            pass
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检", catchup_tasks=True)
        except Exception as exc:
            self.log(f"初次巡检异常: {exc}")
        # 进程停过一段时间的话，重置时刻可能已经过去：启动就补一次窗口探针，
        # 把新一轮窗口的起点尽量抢回来。
        self._run_window_probes()

        while not self._stop_event.is_set():
            self._reload_schedule()
            self._calc_next_fire()
            # 每 30 秒检查一次当前整点
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                # 网页活跃的独立时段是分钟级：先于整点检查，避免整点那次
                # sleep(65) 把同一分钟的时段跨过去。
                due_slot = self._due_daily_chat_slot(now)
                if due_slot:
                    reason = f"网页活跃定时命中 ({due_slot})"
                    try:
                        self._execute_cycle(reason, daily_chat_only=True)
                    except Exception as exc:
                        self.log(f"网页活跃排程执行异常: {exc}")
                    time.sleep(35)
                # 到达设定的整点前 1 分钟内触发
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = f"整点排程命中 ({cur_hour}:00)"
                    try:
                        self._execute_cycle(reason)
                    except Exception as exc:
                        self.log(f"排程执行异常: {exc}")
                    time.sleep(65) # 避开当前这一分钟重复触发
            # 模型额度窗口探针：不受 enabled 影响。窗口是按首次请求起算的，
            # 到点没人请求就不会开始新一轮，这件事跟签到/保活开关无关。
            self._run_window_probes()
            # 模型额度定时主动查询：同样不受 enabled 影响——它是「哪些配额线
            # 已经打满」的唯一主动信息源，跟签到/保活不是一回事。
            self._run_quota_scans()
            self._stop_event.wait(self._next_wait())

    def _run_window_probes(self):
        """在独立线程里执行到点的窗口探针。

        一次探针最长可能等上游几十秒，跑在巡检循环里会把整点排程的节奏带偏；
        wb_window_probe 自带防重入锁，重叠启动是无害的。
        """
        if self._probe_thread and self._probe_thread.is_alive():
            return
        self._probe_thread = threading.Thread(target=self._probe_worker, daemon=True)
        self._probe_thread.start()

    def _probe_worker(self):
        try:
            import wb_window_probe
            wb_window_probe.run_due(self.pool, self.log)
        except Exception as exc:
            self.log(f"窗口探针异常: {exc}")

    def _run_quota_scans(self):
        """到点的模型额度主动查询，同样跑在独立线程里。

        一次查询要等上游几十秒，塞进巡检循环会把整点排程的节奏带偏；
        wb_quota_scan 自带防重入锁，重叠启动是无害的。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self._scan_thread.start()

    def _scan_worker(self):
        try:
            import wb_quota_scan
            wb_quota_scan.run_due(self.pool, self.log)
        except Exception as exc:
            self.log(f"额度查询异常: {exc}")

    def _next_wait(self):
        """下一轮巡检的等待时间：常规 30 秒，有窗口即将到点时提前醒来。

        探针的价值在于「到点立刻发」——晚一个巡检周期，新一轮窗口的起点就
        往后推 30 秒，所以这里按最近的窗口到期时间收敛等待。
        """
        wait = 30.0
        try:
            import wb_window_probe
            accounts_dir = getattr(self.pool, "dir", "") or ""
            secs = wb_window_probe.seconds_until_due(accounts_dir)
            if secs is not None:
                wait = min(wait, max(1.0, secs))
        except Exception:
            pass
        return wait

    def _due_daily_chat_slot(self, now=None):
        """The HH:MM web-activity slot due right now, or "" when none is.

        按**到期窗口**判定，不是精确到分。巡检循环 30 秒醒一次，但一轮巡检可能
        占住好几分钟（旧实现里甚至几小时），而 `HH:MM == now` 的精确匹配会让这个
        时段当天直接漏掉 —— 日志里一个字都没有，面板上就是「今天没跑」。现在只要
        「到点了、今天这个时段还没跑过、且没超过补跑窗口」就命中，所以重启、卡顿、
        漏 tick 都能补上。

        每个时段一天只返回一次；账号级 can_daily_chat() 仍然挡住当天已完成的账号。
        """
        if not self.daily_chat_times:
            return ""
        now = time.localtime() if now is None else now
        current = now.tm_hour * 60 + now.tm_min
        day = time.strftime("%Y-%m-%d", now)
        done = self._daily_chat_done.setdefault(day, set())
        for slot in self.daily_chat_times:
            if slot in done:
                continue
            try:
                hour, minute = [int(part) for part in str(slot).split(":")]
            except (TypeError, ValueError):
                continue
            target = hour * 60 + minute
            if current < target:
                continue
            if current - target > DAILY_CHAT_SLOT_GRACE_MINUTES:
                continue
            done.add(slot)
            # The map is a same-day guard, not history.
            for other in list(self._daily_chat_done):
                if other != day:
                    self._daily_chat_done.pop(other, None)
            return slot
        return ""

    def _reload_schedule(self):
        """Re-read the configurable hours; keep the current grid on failure.

        wb_settings.load() is cached by (path, mtime, size), so calling this
        every loop tick is a stat, not a parse. A changed grid recomputes the
        next fire time immediately, which is what makes an edit take effect
        without restarting the gateway.
        """
        directory = getattr(self.pool, "dir", "") or ""
        if not directory:
            return False
        try:
            hours = wb_settings.schedule_hours(directory)
        except Exception:
            return False
        try:
            # 生效时段 = 面板填的时段；留空时是「今天随机抽的两个、相隔 12 小时」。
            # 随机值当日只抽一次并落盘（见 wb_settings.daily_chat_random_times），
            # 所以这里每 30 秒重读不会让时段漂移。
            times = wb_settings.daily_chat_effective_times(directory)
        except Exception:
            times = list(self.daily_chat_times)
        try:
            self.daily_chat_random_slots = not bool(
                wb_settings.daily_chat_times(directory))
        except Exception:
            pass
        changed = (hours["checkin"] != self.checkin_hours
                   or hours["travel"] != self.travel_hours
                   or hours["keepalive"] != self.keepalive_hours
                   or hours["cat"] != self.cat_hours
                   or times != self.daily_chat_times)
        self.checkin_hours = hours["checkin"]
        self.travel_hours = hours["travel"]
        self.keepalive_hours = hours["keepalive"]
        self.cat_hours = hours["cat"]
        self.daily_chat_times = times
        self.all_hours = sorted(set(self.checkin_hours + self.travel_hours
                                    + self.keepalive_hours + self.cat_hours))
        if changed:
            self._calc_next_fire()
        return changed

    def _calc_next_fire(self):
        now = time.localtime()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            # 今天
            t_struct = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            # 明天第一个小时
            t_tomorrow = time.time() + 86400
            now_tom = time.localtime(t_tomorrow)
            first_h = self.all_hours[0]
            t_struct = time.struct_time((now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle,
                         args=("手动立即触发", True), daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", force=False,
                       daily_chat_only=False, catchup_tasks=False):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检 ({trigger_reason})：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason, force, daily_chat_only,
                            catchup_tasks)
        finally:
            self._run_lock.release()

    def _reload_accounts_if_changed(self):
        """P3-2：账号文件被手工改过时，下一轮就按新内容调度，不必重启网关。"""
        reload_fn = getattr(self.pool, "reload_if_changed", None)
        if not callable(reload_fn):
            return
        try:
            result = reload_fn() or {}
        except Exception as exc:      # noqa: BLE001 - 重载失败不该拖垮巡检
            self.log(f"! 账号文件重载失败: {exc}")
            return
        if result.get("changed"):
            self.log("账号文件有变化，已重新加载：新增 %s 个、移除 %s 个，当前 %s 个"
                     % (len(result.get("added") or []),
                        len(result.get("removed") or []),
                        len(self.pool.accounts)))

    def _tasks_dir(self):
        return getattr(self.pool, "dir", "") or ""

    def _tasks_concurrency(self):
        return int(_settings_call("tasks_concurrency", self._tasks_dir(), 3) or 1)

    def _tasks_budget(self):
        return int(_settings_call("tasks_budget", self._tasks_dir(), 1800) or 0)

    def _checkin_retries(self):
        return int(_settings_call("checkin_retries", self._tasks_dir(), 2) or 0)

    def _checkin_catchup_enabled(self):
        return bool(_settings_call("checkin_startup_catchup", self._tasks_dir(), True))

    def _checkin_enabled(self):
        return bool(_settings_call("checkin_enabled", self._tasks_dir(), True))

    def _plan_cycle_tasks(self, run_checkin, run_travel, run_cat, run_daily_chat,
                          run_keepalive=True, run_report=None):
        """P1-1：「要不要干活」先算清楚，再决定要不要为此等待。

        旧实现在循环体最前面就 sleep(2~5s)，481 个账号里绝大多数什么也不用做
        （今天已经签过、任务开关关着），照样睡满 1684 秒——一轮空转 28 分钟，
        20 分钟内到点的下一个时段就被顶掉了。现在只统计，不发请求也不睡。
        """
        if run_report is None:
            run_report = run_checkin
        keepalive_items = []
        cn_items = []
        intl_targets = []
        skips = {}

        def bump(reason):
            skips[reason] = skips.get(reason, 0) + 1

        now = time.time()
        for acc in list(self.pool.accounts):
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 上游账号级风控限制（403 / code 11140）中的账号一律跳过：无人值守
            # 的巡检/打卡再去「试探」一次，只会让同一台设备上的其他账号连坐。
            if getattr(acc, "restricted_until", 0.0) > now:
                self.log(f"跳过受限账号 [{uid8}]：上游风控限制剩余 "
                         f"{int(acc.restricted_until - now)}s")
                bump("restricted")
                continue
            # 区域级连坐暂停期间（同一窗口内多个账号被限制）整轮都不碰上游。
            try:
                hold = _proxy_module().restriction_hold_remaining(acc.realm)
            except Exception:
                hold = 0.0
            if hold > 0:
                self.log(f"跳过 [{uid8}]：{acc.realm} 区域风控暂停剩余 {int(hold)}s")
                bump("region_hold")
                continue
            # Token 保活对所有账号都跑（它只续凭证，不代表账号参与任务）。低提前量
            # 的账号一轮可能有几百个，同样不能让它们白等抖动。
            if run_keepalive and keepalive_due(acc.expires_at or 0):
                keepalive_items.append(acc)
            # Task participation is a separate switch from API dispatch: a
            # disabled account can still run check-in / growth / travel /
            # daily activity (issue #521).
            if not acc.task_active():
                bump("tasks_disabled")
                continue
            if acc.realm == "cn":
                want = {}
                if run_checkin:
                    if acc.can_checkin():
                        want["checkin"] = True
                    else:
                        bump("already_today")
                if run_report:
                    # 活跃上报跟着签到时段走，但不跟着 checkin_enabled 停：暂停
                    # 签到只是不领那笔积分，连登/热力墙没理由一起断。
                    if acc.can_report_activity():
                        want["report"] = True
                    else:
                        bump("report_done")
                if run_travel:
                    want["travel"] = True
                if run_cat:
                    want["cat"] = True
                if want:
                    cn_items.append((acc, want))
            elif acc.realm == "intl" and run_daily_chat:
                if acc.can_daily_chat():
                    intl_targets.append(acc)
                else:
                    bump("daily_chat_done")
        return keepalive_items, cn_items, intl_targets, skips

    def _run_cycle(self, trigger_reason="周期巡检", force=False,
                   daily_chat_only=False, catchup_tasks=False):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"开始执行任务 ({trigger_reason})...")
        self._reload_accounts_if_changed()
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        started = time.time()
        refreshed_count = 0
        cycle_hour = time.localtime().tm_hour
        # 启动补跑只消费一次（P3-1）：进程在时段内停过时，「今天已经过了时段」
        # 不该等于「今天这件事就不做了」——签到/旅行/国际活跃都要补一轮。
        catchup = self._consume_startup_catchup()
        # P3-1：启动补跑只由「启动后第一轮巡检」触发（catchup_tasks=True）。
        # 手工/整点触发的巡检仍旧严格按时段门禁——issue #515 之后，
        # 「10:00 的巡检不该顺手把 09:00 的旅行再跑一遍」是明确的既有行为。
        catchup_enabled = self._checkin_catchup_enabled()
        missed_task_window = (catchup_tasks and catchup_enabled
                              and any(h < cycle_hour for h in self.checkin_hours))
        missed_travel_window = (catchup_tasks and catchup_enabled
                                and any(h < cycle_hour for h in self.travel_hours))
        checkin_enabled = self._checkin_enabled()
        run_report = (not daily_chat_only
                      and (force or cycle_hour in self.checkin_hours
                           or missed_task_window))
        run_checkin = run_report and checkin_enabled
        if not checkin_enabled and not daily_chat_only:
            self.log("签到已按设置暂停（checkin_enabled=false），本轮只跑其它任务")
        run_travel = (not daily_chat_only
                      and (force or cycle_hour in self.travel_hours
                           or missed_travel_window))
        run_keepalive = (not daily_chat_only
                         and (force or cycle_hour in self.keepalive_hours))
        run_cat = (not daily_chat_only
                   and (force or cycle_hour in self.cat_hours))
        if missed_task_window or missed_travel_window:
            self.log("启动补跑：今天已经过了整点时段，补跑签到/旅行/活跃上报")
        # 时段永远存在（面板留空时是「今天随机抽的两个」），所以国际版活跃只按
        # 时段触发；启动后的第一次巡检额外补跑一次当天还没活跃的账号。
        run_daily_chat = (force or daily_chat_only or catchup
                          or (not self.daily_chat_times and run_checkin))

        keepalive_items, cn_items, intl_targets, skips = self._plan_cycle_tasks(
            run_checkin, run_travel, run_cat, run_daily_chat, run_keepalive,
            run_report)

        # 1. Token 保活（逐个刷新；数量少、且不参与抖动）
        for acc in keepalive_items:
            uid8 = acc.uid[:8] if acc.uid else "?"
            exp = acc.expires_at or 0
            if exp:
                left = int(exp - time.time())
                detail = f"剩余 {left}s" if left > 0 else f"已过期 {-left}s"
            else:
                detail = "到期时间未知"
            self.log(f"账号 [{uid8}] Token {detail}，执行主动保活刷新...")
            if acc.refresh():
                refreshed_count += 1
                self.log(f"✓ 账号 [{uid8}] Token 自动保活刷新成功")
            else:
                self.log(f"! 账号 [{uid8}] Token 保活刷新失败: {acc.last_error}")

        # 2. 中文任务跑道（签到 / 活跃上报 / 猫猫旅行 / 连登 / 夜猫）
        counts = self._run_cn_task_batch(cn_items)
        # 3. 国际版每日活跃（本来就有自己的并发与预算）
        daily_chat_count = self._run_daily_chat_batch(intl_targets) if intl_targets else 0

        self.log(f"巡检完成：Token保活 {refreshed_count} 个，国内签到 {counts['checkin']} 个，"
                 f"活跃上报 {counts['report']} 个，猫猫日常 {counts['travel']} 个，"
                 f"国际活跃 {daily_chat_count} 个")
        self._log_cycle_summary(trigger_reason, counts, refreshed_count,
                                daily_chat_count, skips, started,
                                len(cn_items) + len(intl_targets) + len(keepalive_items))

    def _log_cycle_summary(self, trigger_reason, counts, refreshed_count,
                           daily_chat_count, skips, started, candidates):
        """P2-4：每轮一行汇总，跳过原因按类目计数。

        「今天怎么一个都没签」这种问题，光看逐账号日志要翻几百行；一行汇总
        直接给出候选、完成与跳过原因分布。
        """
        reasons = " ".join("%s=%d" % (k, v) for k, v in sorted(skips.items())) or "无"
        self.log("巡检汇总（%s）：候选 %d，完成 签到 %d/上报 %d/旅行 %d/夜猫 %d，"
                 "失败 %d，国际活跃 %d，保活 %d，跳过 %s，用时 %.1fs，"
                 "旅行明细 已完成 %d/途中 %d"
                 % (trigger_reason, candidates, counts["checkin"], counts["report"],
                    counts["travel"], counts["cat"], counts["failed"],
                    daily_chat_count, refreshed_count, reasons,
                    max(0.0, time.time() - started),
                    counts.get("travel_done", 0), counts.get("traveling", 0)))

    def _deadline_hit(self, deadline):
        return deadline is not None and time.time() > deadline

    def _run_cn_task_batch(self, items):
        """P1-2：中文任务批量化。

        并发 1（或只有一个账号）走历史行为：逐个来、账号间隔 2~5 秒抖动。
        高于 1 时用线程池 + 提交错峰 0.25~0.6 秒，整批受 tasks_budget 约束：
        一个卡在上游的账号不能把整轮巡检拖住（这正是旧实现在 21:00 窗口跑满
        52 分钟、把次日 01:00 时段顶掉的原因）。
        """
        counts = {"checkin": 0, "report": 0, "travel": 0, "cat": 0,
                  "travel_done": 0, "traveling": 0, "failed": 0}
        if not items:
            return counts
        concurrency = max(1, self._tasks_concurrency())
        budget = self._tasks_budget()
        deadline = (time.time() + budget) if budget > 0 else None
        if concurrency <= 1 or len(items) <= 1:
            for index, (acc, want) in enumerate(items):
                if index > 0:
                    # 账号间随机错峰防并发风控（2~5秒浮动延时，issue #394）
                    time.sleep(random.uniform(2.0, 5.0))
                if self._deadline_hit(deadline):
                    self.log("中文任务本轮已到批次截止时间，剩余账号留到下一个时段")
                    break
                result = self._run_cn_tasks(acc, want, deadline)
                for key in counts:
                    counts[key] += int(result.get(key) or 0)
            return counts

        from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                        TimeoutError as FutureTimeout)
        pool = ThreadPoolExecutor(max_workers=concurrency,
                                  thread_name_prefix="tasks")
        try:
            futures = {}
            for index, (acc, want) in enumerate(items):
                delay = min(index * random.uniform(0.25, 0.6), 2.0)
                futures[pool.submit(self._delayed_cn_tasks, acc, want, delay,
                                    deadline)] = acc
            timeout = None if deadline is None else max(1.0, deadline - time.time())
            try:
                for future in as_completed(futures, timeout=timeout):
                    try:
                        result = future.result() or {}
                    except Exception as exc:      # noqa: BLE001 - 单个账号失败不中断整批
                        acc = futures[future]
                        uid8 = acc.uid[:8] if acc.uid else "?"
                        self.log(f"! 账号 [{uid8}] 中文任务并发执行异常: {exc}")
                        counts["failed"] += 1
                        continue
                    for key in counts:
                        counts[key] += int(result.get(key) or 0)
            except FutureTimeout:
                pending = [acc for future, acc in futures.items()
                           if not future.done()]
                self.log("中文任务本轮已到批次截止时间，%d 个账号留到下一个时段"
                         % len(pending))
                counts["failed"] += len(pending)
        finally:
            # wait=False：卡住的 worker 不能把整轮巡检一起拖住（预算到点后它
            # 自己会放弃当前账号）。
            pool.shutdown(wait=False)
        return counts

    def _delayed_cn_tasks(self, acc, want, delay, deadline):
        if delay:
            time.sleep(delay)
        try:
            return self._run_cn_tasks(acc, want, deadline)
        except Exception as exc:          # noqa: BLE001 - 记下来交给批次汇总
            uid8 = acc.uid[:8] if acc.uid else "?"
            self.log(f"! 账号 [{uid8}] 中文任务异常: {exc}")
            return {"failed": 1}

    def _run_cn_tasks(self, acc, want, deadline=None):
        """一个账号的中文任务：签到 -> 活跃上报 -> 猫猫旅行/连登 -> 夜猫。"""
        uid8 = acc.uid[:8] if acc.uid else "?"
        counts = {"checkin": 0, "report": 0, "travel": 0, "cat": 0,
                  "travel_done": 0, "traveling": 0, "failed": 0}
        if want.get("checkin") and not self._deadline_hit(deadline):
            self.log(f"检测到国内版账号 [{uid8}] 今日尚未签到，执行自动签到...")
            res = self._checkin_with_retry(acc, deadline)
            if res.get("ok"):
                counts["checkin"] += 1
                self.log(f"✓ 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
            else:
                self.log(f"! 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
            time.sleep(1.0)
        if want.get("report") and not self._deadline_hit(deadline):
            self.log(f"检测到国内版账号 [{uid8}] 今日尚未活跃上报，执行对话活跃上报...")
            res = acc.report_activity()
            if res.get("ok"):
                counts["report"] += 1
                self.log(f"✓ 账号 [{uid8}] {res.get('msg')}")
            else:
                self.log(f"! 账号 [{uid8}] 对话活跃上报失败: {res.get('error') or res.get('msg')}")
            time.sleep(1.0)
        if want.get("travel") and not self._deadline_hit(deadline):
            # 检查猫猫旅行
            tr = do_cat_travel(acc)
            action = tr.get("action")
            if action in ("claim", "depart"):
                counts["travel"] += 1
                self.log(f"🐱 账号 [{uid8}] 猫猫日常处理: {tr.get('msg')}")
            elif action == "idle":
                # daily_limit_reached：今日旅行额度已用完。P2-4 要求「跳过」要能解释清楚，
                # 否则「旅行 3」会被误读成只有 3 个号参与。
                counts["travel_done"] += 1
            elif action == "traveling":
                counts["traveling"] += 1
            time.sleep(1.0)
            # 连登管家闭环（补签保连登 -> 礼包/补偿 -> 连登兑换 -> 自动抽奖）
            bonus = wb_tasks.run_streak_bonus(acc)
            if bonus.get("logs"):
                for line in bonus["logs"]:
                    self.log(f"★ 账号 [{uid8}] {line}")
                time.sleep(1.0)
        if want.get("cat") and not self._deadline_hit(deadline):
            # 01:00 夜猫子专属任务: black_cat 只在 23:00-08:00 上报计数,
            # 之前这个整点只是空转通用巡检, 从未真正上报过夜猫事件。
            night = wb_tasks.run_night_growth(acc)
            for line in night.get("logs", []):
                self.log(f"🌙 {line}")
            time.sleep(1.0)
        return counts

    def _checkin_with_retry(self, acc, deadline=None):
        """P3-3 消费侧：瞬态失败（网络 / 5xx / 429）按 checkin_retries 退避重试。

        5s、15s、45s……指数退避，受批次 deadline 约束：重试只是「别把一次网络
        抖动当成今天没签」，不该把整批的时间预算吃掉。
        """
        retries = max(0, self._checkin_retries())
        attempt = 0
        delay = 5.0
        while True:
            res = acc.checkin(trigger="scheduler") or {}
            if res.get("ok") or not res.get("transient") or attempt >= retries:
                return res
            if deadline is not None and time.time() + delay > deadline:
                return res
            attempt += 1
            uid8 = acc.uid[:8] if acc.uid else "?"
            self.log(f"… 账号 [{uid8}] 签到瞬态失败，{int(delay)}s 后重试"
                     f"（第 {attempt}/{retries} 次）")
            time.sleep(delay)
            delay = min(delay * 3, 60.0)

    def _daily_chat_concurrency(self):
        directory = getattr(self.pool, "dir", "") or ""
        if not directory:
            return wb_settings.DAILY_CHAT_WEB_CONCURRENCY_DEFAULT
        try:
            return wb_settings.daily_chat_web_concurrency(directory)
        except Exception:
            return wb_settings.DAILY_CHAT_WEB_CONCURRENCY_DEFAULT

    def _daily_chat_budget(self):
        """单账号预算（秒）；0 = 不限制。"""
        directory = getattr(self.pool, "dir", "") or ""
        if not directory:
            return wb_settings.DAILY_CHAT_BUDGET_DEFAULT
        try:
            return wb_settings.daily_chat_budget(directory)
        except Exception:
            return wb_settings.DAILY_CHAT_BUDGET_DEFAULT

    def _consume_startup_catchup(self):
        """启动补跑只消费一次，并尊重开关。

        进程重启后旧实现是「今天已经过了时段就算了」——10-23 那次就是这样丢掉
        5 个账号的一整天：17:01 重启，巡检跑完「国际活跃 0 个」，再没有下一次机会。
        """
        if not self._catchup_pending:
            return False
        self._catchup_pending = False
        directory = getattr(self.pool, "dir", "") or ""
        try:
            enabled = (wb_settings.daily_chat_startup_catchup(directory)
                       if directory else True)
        except Exception:
            enabled = True
        if enabled:
            self.log("启动补跑：检查当天还没完成国际版每日活跃的账号")
        return enabled

    def _run_one_daily_chat(self, acc, wait=None):
        """Run one account's daily activity; returns whether it succeeded.

        wait 是硬性挂钟上限（秒，None = 按预算算、0 = 不限制）。真正的等待跑在
        单独一条线程里，到点还没返回就不再等它——因为它可能卡在 socket 上（DNS
        不吃 socket 超时，服务端也能一点一点滴数据），而那种卡法会让「单账号预算」
        这条软限制彻底失效：预算只在代码回到 Python 时才起作用。放弃之前先让**本
        线程**替它把网页会话收尾，否则那条会话会一直占着上游的活跃名额（#623 的
        坑，只是换了个入口）。
        """
        uid8 = acc.uid[:8] if acc.uid else "?"
        self._prune_abandoned_workers()
        if len(self._daily_chat_abandoned) >= DAILY_CHAT_ABANDONED_MAX:
            self.log(f"! 账号 [{uid8}] 本轮跳过：还有 {len(self._daily_chat_abandoned)} 条"
                     f"上几轮的工作线程没退出（都卡在网络上），先不再开新的，"
                     f"免得线程越堆越多；这个账号留到下一个时段")
            return False
        self.log(f"检测到国际版账号 [{uid8}] 今日尚未活跃，执行每日活跃打卡对话...")
        budget = self._daily_chat_budget()
        if wait is None:
            wait = (budget + DAILY_CHAT_BATCH_SLACK) if budget > 0 else 0
        self._daily_chat_live[uid8] = {"uid": acc.uid, "started": time.time(),
                                       "budget": budget}
        outcome = {}
        done = threading.Event()

        def work():
            try:
                outcome["res"] = acc.daily_chat(trigger="scheduler")
            except Exception as exc:      # noqa: BLE001 - 记下来交给下面统一处理
                outcome["exc"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=work, name=f"daily-chat-{uid8}", daemon=True)
        worker.start()
        try:
            finished = done.wait(wait) if wait else done.wait()
            if not finished:
                reclaimed = 0
                try:
                    reclaimed = acc.web_conversation_reclaim(0)
                except Exception:
                    pass
                self.log(f"! 账号 [{uid8}] 超过 {int(wait)}s 仍未返回（线程卡在网络上），"
                         f"已代为收尾 {reclaimed} 条网页会话，本轮放弃、留到下一个时段")
                self._daily_chat_abandoned.append(worker)
                self._record_daily_chat_hang(acc, wait, reclaimed)
                return False
            exc = outcome.get("exc")
            if exc is not None:
                self.log(f"! 账号 [{uid8}] 每日活跃对话异常: {exc}")
                return False
            res = outcome.get("res") or {}
        finally:
            self._daily_chat_live.pop(uid8, None)
        if res.get("ok"):
            # 成功时也把网页通道那一轮的结果带上（completed：N 段输出），
            # 光看「成功」分不清是真跑完了还是只有桌面腿过了（issue #236）。
            self.log(f"✓ 账号 [{uid8}] 每日活跃对话成功: {res.get('msg')}")
            return True
        self.log(f"! 账号 [{uid8}] 每日活跃对话失败: {res.get('error') or res.get('msg')}")
        return False

    def _prune_abandoned_workers(self):
        """把已经结束的「被放弃线程」从列表里去掉（还活着的留着继续计数）。"""
        self._daily_chat_abandoned = [t for t in self._daily_chat_abandoned
                                      if t.is_alive()]

    def _record_daily_chat_hang(self, acc, wait, reclaimed):
        """给被放弃的账号补一条活动记录，免得面板上整天看不到这个账号的结论。"""
        try:
            import wb_activity
            wb_activity.record_attempt(
                acc, wb_activity.TASK_DAILY_CHAT, "scheduler",
                {"ok": False, "phase": "hang",
                 "error": "执行超过 %ds 仍未返回（线程卡在网络上），已代为收尾 %d 条网页会话"
                          % (int(wait), reclaimed)})
        except Exception:
            pass

    def _run_daily_chat_batch(self, accounts):
        """Run today's intl daily activity, honouring the concurrency setting.

        Every account is bounded by its own budget (wb_accounts), and the batch
        has a deadline of its own on top: a worker that somehow ignores the
        budget must not be able to hold the whole cycle — and with it the cn
        check-in — hostage the way the old serial loop did.

        Concurrency 1 keeps the historical one-account-at-a-time order with the
        1.5 s gap. Above that, workers are started with a small stagger so the
        first conversations do not hit the upstream in the same millisecond.
        """
        concurrency = self._daily_chat_concurrency()
        budget = self._daily_chat_budget()
        deadline = None
        if budget > 0:
            waves = -(-len(accounts) // max(1, concurrency))   # ceil
            deadline = time.time() + waves * (budget + DAILY_CHAT_BATCH_SLACK)
        if concurrency <= 1 or len(accounts) <= 1:
            count = 0
            for acc in accounts:
                if deadline is not None and time.time() > deadline:
                    self.log("网页活跃本轮已到批次截止时间，剩余账号留到下一个时段")
                    break
                if self._run_one_daily_chat(acc):
                    count += 1
                time.sleep(1.5)
            return count
        from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                        TimeoutError as FutureTimeout)
        count = 0
        pool = ThreadPoolExecutor(max_workers=concurrency,
                                  thread_name_prefix="daily-chat")
        try:
            futures = {}
            for index, acc in enumerate(accounts):
                delay = min(index * 0.3, 2.0)
                futures[pool.submit(self._delayed_daily_chat, acc, delay)] = acc
            timeout = None if deadline is None else max(1.0, deadline - time.time())
            try:
                for future in as_completed(futures, timeout=timeout):
                    try:
                        if future.result():
                            count += 1
                    except Exception as exc:
                        acc = futures[future]
                        uid8 = acc.uid[:8] if acc.uid else "?"
                        self.log(f"! 账号 [{uid8}] 每日活跃并发执行异常: {exc}")
            except FutureTimeout:
                pending = [acc for future, acc in futures.items() if not future.done()]
                self.log("网页活跃本轮已到批次截止时间，%d 个账号留到下一个时段"
                         % len(pending))
                # 到点还挂着的大概率是卡在网络上：替它们把网页会话收尾，别让
                # 一条卡住的线程把活跃名额带走（收尾用本线程的连接，不受影响）。
                for acc in pending:
                    try:
                        acc.web_conversation_reclaim(0)
                    except Exception:
                        pass
        finally:
            # wait=False：卡住的 worker 不能把整轮巡检一起拖住（它自己的预算
            # 会结束它；这里只是不让 shutdown 去等）。
            pool.shutdown(wait=False)
        return count

    def _delayed_daily_chat(self, acc, delay):
        if delay:
            time.sleep(delay)
        return self._run_one_daily_chat(acc)

    def status(self):
        # 待触发的模型额度窗口：面板上要能看见「下一次到点是什么时候」，
        # 否则自动请求是个黑盒。读不到就当作没有，不影响状态接口本身。
        windows = []
        try:
            import wb_window_probe
            accounts_dir = getattr(self.pool, "dir", "") or ""
            windows = wb_window_probe.snapshot(accounts_dir)[:10]
        except Exception:
            windows = []
        # 主动查询队列：同样要让 periodicity 可见（下一次查哪个模型、还有多久）。
        scans = []
        try:
            import wb_quota_scan
            accounts_dir = getattr(self.pool, "dir", "") or ""
            scans = wb_quota_scan.snapshot(accounts_dir, limit=6)
        except Exception:
            scans = []
        self._reload_schedule()

        def _hhmm(hours):
            return "/".join("%02d:00" % h for h in hours)

        checkin = _hhmm(self.checkin_hours)
        travel = _hhmm(self.travel_hours)
        keepalive = _hhmm(self.keepalive_hours)
        cat = _hhmm(self.cat_hours)
        mode_cn = ("整点排程 (签到 %s · 猫猫旅行 %s · 每日活跃上报 · "
                   "Token保活 %s · 夜猫 %s) + 模型额度窗口探针"
                   % (checkin, travel, keepalive, cat))
        mode_intl = ("账号 Token 自动保活与凭证常驻 (每日 %s 集中巡检) "
                     "+ 模型额度窗口探针" % keepalive)
        return {
            "enabled": self.enabled,
            "mode": mode_cn,
            "mode_cn": mode_cn,
            "mode_intl": mode_intl,
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "windows": windows,
            "quota_scans": scans,
            # 国际版每日活跃的排程与现场：面板「任务与福利」页的状态块读这几项。
            "daily_chat": self._daily_chat_status(),
            "logs": self.logs[-20:],
        }

    def _daily_chat_status(self):
        """网页活跃的生效时段、下一个时段与正在跑的账号。"""
        now = time.time()
        live = []
        for uid8, item in list(self._daily_chat_live.items()):
            budget = item.get("budget") or 0
            elapsed = int(now - (item.get("started") or now))
            live.append({"uid8": uid8, "uid": item.get("uid") or "",
                         "elapsed_s": elapsed, "budget_s": budget,
                         "remaining_s": max(0, budget - elapsed) if budget else None})
        today = time.strftime("%Y-%m-%d")
        done_slots = sorted(self._daily_chat_done.get(today) or [])
        upcoming = [slot for slot in self.daily_chat_times if slot not in done_slots]
        return {
            "times": list(self.daily_chat_times),
            "random": bool(getattr(self, "daily_chat_random_slots", False)),
            "done_slots": done_slots,
            "next_slot": upcoming[0] if upcoming else "",
            "live": live,
            "budget_s": self._daily_chat_budget(),
            "abandoned": len(self._daily_chat_abandoned),
        }
