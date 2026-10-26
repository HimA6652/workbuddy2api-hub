# 零中断热更新：改完代码不用停网关

网关是本机唯一的上游出口。当 DSH、客户端或脚本正把请求打给它时，重启（哪怕只是
`/api/restart`）会把正在跑的对话掐断。这份文档说明本分支的做法：**新进程原地接管
监听 socket，旧进程把手上的请求做完再退出**——端口一秒都没关。

实测：交接 0.35 – 0.46 秒，旧进程排空 0 – 0.1 秒，全程 7481 – 8925 次 keep-alive
请求 0 失败（`tests/_test_hotswap.py`）。对比一次 `/api/restart` 的断档 3.9 秒。

---

## 1. 一条命令

```bash
python scripts/hotswap_update.py            # 默认 127.0.0.1:8788，面板 8790
```

它做的事，按顺序：

1. `POST /panel/login` 拿面板 token（密码取自 `--password` → `WB_PANEL_PASSWORD`
   → `panel/config.json` 的 `panel_password`）；
2. `GET /hotswap/status` 确认对方支持热切换且当前 `phase == idle`（有请求在排空时拒绝）；
3. `POST /hotswap` —— 旧进程 Popen 一个新进程把监听句柄继承过去，回执里直接带 `child_pid`；
4. 拿旧 token 反复请求，直到 **401**（面板会话只活在进程内存里，401 就是"新进程在答"）；
5. 重新登录，把新 PID 写进 `panel/run.pid`（面板读的就是这个文件）；
6. 等旧 PID 消失，最后 `GET /health`（可选再验一次 `/v1/models`）。

退出码：`0` 成功；`2` 对方还不支持 `/hotswap` 且没有加 `--bootstrap`；`1` 其他失败。

常用参数：

| 参数 | 说明 |
|---|---|
| `--port 8788` | 网关端口 |
| `--panel-port 8790` | 控制面板端口（只用于 `--bootstrap`） |
| `--password` | 面板密码；不给就按上面顺序找 |
| `--pidfile` | 默认 `panel/run.pid`，文件不存在就不写 |
| `--timeout 600` | 等旧进程退出的上限（秒） |
| `--api-key` | 收尾时顺带验一次 `/v1/models` |
| `--bootstrap` | 见下一节 |

---

## 2. 第一次：旧版本没有 /hotswap

热切换代码本身要靠新版本才存在，所以第一次必然有一次真实重启——**仅此一次**。
推荐用带自动回滚的引导脚本（先确认现在健康 → 重启 → 等到 `/hotswap/status` 可用 →
超时或失败就还原 `*.bak-<后缀>` 再启动一次）：

```bash
python scripts/bootstrap_update.py --backup-suffix bak-20261026-prehotswap
python scripts/bootstrap_update.py --dry-run        # 只看它会做什么
```

参数：`--port 8788` / `--panel-port 8790` / `--password`（不给就按 `WB_PANEL_PASSWORD`、
`panel/config.json`）/ `--backup-suffix` / `--timeout 120`。

实测（数百个账号的实例，2026-10-26）：重启耗时 **3.5 秒**，新进程起来后
`/hotswap/status` 从 404 变 401（要面板 token），随后一条 `/v1/models` 200。
回滚路径也实测过：把 `wb_proxy.py` 弄成语法错误再跑，面板报启动失败 → 脚本还原
备份 → 面板说「服务未在运行」时自动改打 `/api/start` → 网关恢复 200、文件 md5 回到旧版。

只想跑现成流程、不要回滚的话，`python scripts/hotswap_update.py --bootstrap` 也行：
它同样走一次 `POST /api/restart`（实测断档 3.9 秒）、量出真实秒数、确认新代码在跑。


完整流程（本机实测 2026-10-26）：

```text
$ python scripts/hotswap_update.py --bootstrap
网关 http://127.0.0.1:8788
  正在跑的版本还不支持 /hotswap，先做一次重启（这一次会断）……
  重启完成，断档 3.92 秒（这一次是有感的；之后不会再有了）
  新代码已经在跑（/health 200），这次更新到此完成

$ # 以后改完文件就这么更新
$ python scripts/hotswap_update.py
  网关 http://127.0.0.1:8788
  当前进程 7540，开始交接……
  新进程已拉起：12592
  新进程已接手，端口一秒都没关（0.35 秒）
  现在服务的是 PID 24832
  已把新 PID 写进 panel/run.pid
  旧进程还在把手上请求做完（最多等 600 秒）……
  旧进程已退出
  /health 200 accounts=<N> ready=<M>
完成：7540 -> 24832，总耗时 0.49 秒，业务无中断
```

---

## 3. 怎么做到的

```text
旧进程                         新进程
  │ POST /hotswap
  │ Popen(close_fds=False)  ──────►  继承监听句柄（--inherit-socket）
  │ 回执 {started, child_pid}        _build_server() 直接用这个 socket
  │                                  serve_forever() 开始 accept
  │ 等 ready 文件
  │ 日志 "pid N is serving"          （新进程在答了）
  │ phase = draining
  │ SCHEDULER.enabled = False
  │ server.shutdown()（不再 accept）
  │ 等 active == 0（≤600 s）          （旧连接继续读写）
  │ os._exit(0) ────────────────────►  scheduler 恢复
```

关键点只有三个，都是"看着简单、漏了就出问题"的地方：

1. **句柄要能继承**。`subprocess.Popen` 默认 `close_fds=True`，那条手工
   `HANDLE_LIST` 路径在 Windows 上过不去；这里用 `close_fds=False` +
   `_inheritable_handle(server.socket)`，新进程 `socket.socket(fileno=handle)`
   原样接管。**不要**关掉再绑——backlog 里已排队的连接会一起丢。
2. **旧进程不能立刻死**。`server.shutdown()` 只停止 accept，已经进来的请求还要写完；
   所以 worker 线程是 `daemon=False`，主线程从 `serve_forever()` 返回后它还在跑，
   等到 `active == 0` 才 `os._exit(0)`。流式响应因此不会断在半截。
3. **调度器只能有一个进程在跑**。旧进程排空期间 `SCHEDULER.enabled = False`；
   新进程启动时带 `--hotswap-from-pid <旧pid>`，后台等旧 PID 消失（≤900 s）再把调度器
   打开，日志写 `previous process is gone, scheduler resumed`。否则两个进程会同时签到。

失败处理：子进程 180 秒内没起来（`HOTSWAP_START_TIMEOUT`）→ `phase = failed` +
错误信息；Windows 上用 `taskkill /F /T /PID` 杀掉整棵树（venv 的 `pythonw.exe` 只是
转发器，单杀会留下孤儿）。**子进程起不来时旧进程照常服务**，端口不受影响。

---

## 4. 状态与探针

`GET /hotswap/status`（面板 token，无 token 401）：

```json
{"phase": "idle", "child_pid": null, "active": 0, "started": 0,
 "error": "", "draining": false, "pid": 24832}
```

- `phase`：`idle` / `starting` / `draining` / `failed`；
- `active`：当前还在处理的请求数（含发起这次查询的那一条）；
- `pid`：**是谁在答**——这就是判断"现在服务的是哪个进程"的唯一可信来源。

### 别用 netstat 判断"现在是谁在服务"

`netstat -ano` 与 `Get-NetTCPConnection` 报的 `OwningPid` 是 **socket 的创建者**，
不随句柄继承改变。热切换之后它会一直指向那个已经退出的旧 PID：

```text
panel state=running pid=19356     ← 真的在跑（来自 /hotswap/status 与 run.pid）
pidfile=19356
TCP owner=8264                    ← 已经退出的旧进程，永远变不回来
```

所以：

- 面板 `Controller.snapshot()` 里，端口属主 PID 死了就丢弃、回落到 `panel/run.pid`
  （`tests/_test_panel_status.py` 钉住了这条规则）；
- `scripts/hotswap_update.py` 的 `listening_pid()` 也只认存活候选；
- 人肉排查时看 `/hotswap/status` 的 `pid`，或问面板。

---

## 5. 上线这一份改动（生产是本机部署）

```powershell
# 1. 备份（下面用 $WB_HOME 指部署目录）
Copy-Item wb_proxy.py  wb_proxy.py.bak-<日期>
Copy-Item panel\wb_control_panel.py panel\wb_control_panel.py.bak-<日期>

# 2. 覆盖：wb_proxy.py + panel/ + scripts/ + tests/
# 3. 第一次只能重启一次
python scripts\hotswap_update.py --bootstrap
```

面板的改动（状态机 + `log_panel` 顺序）要**面板重启**才生效；面板重启不会停网关
（面板退出时网关继续跑），所以顺序随意。

回滚：把 `.bak-<日期>` 覆盖回去，再 `POST http://127.0.0.1:8790/api/restart`。
热切换不写任何持久状态，回滚只需还原文件。

---

## 6. 验证

```bash
python tests/_test_hotswap.py       # 30 项：交接、排空、调度器互斥、压测
python tests/_test_panel_status.py  # 15 项：幽灵 PID、conflict、坏配置
python tests/run_all.py             # 全量
```

`_test_hotswap.py` 用一个**临时实例**跑，不碰生产：

- 先挂一条"发完请求头就不发 body"的请求，让 `active` 停在 2；
- 触发切换，确认新进程接手（旧 token 立刻 401）、旧进程还活着；
- 这时补发 body：那条慢请求仍由旧进程答完、body 收全、连接正常 EOF；
- 全程 keep-alive 压测，断言连接级失败为 0；
- 最后确认旧进程退出、调度器自动恢复。

---

## 7. 还没做的

- **Windows 之外的信号路径没实测**（`SIGTERM` 下 `os._exit` 的收尾只在 Linux CI 跑过语法层）；
  目前驱动脚本与测试都在 Windows 上验证。
- 交接期间新进程若加载失败（比如磁盘上的 `wb_proxy.py` 语法错误），旧进程会一直活着并报
  `phase=failed`，需要人按提示修好后重试；没有做"自动回滚到上一份文件"。
- 面板 UI 没有"一键热更新"按钮：驱动脚本 + 面板 `/api/*` 已经够用，加按钮要动 `dashboard.html`，
  留待需要时再说。
