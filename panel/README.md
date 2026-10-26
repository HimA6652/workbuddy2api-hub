# WorkBuddy2API-Hub 本机控制面板

一个只用 Python 标准库写的本地进程控制面板：启动 / 停止 / 重启网关，一键关闭面板自身，
实时显示运行状态，并且**启动网关时不出现任何 cmd 或控制台窗口**。

## 文件清单

| 文件 | 作用 |
|---|---|
| `wb_control_panel.py` | 面板服务端 + 内嵌前端（单文件，无外部静态资源） |
| `config.json` | 本机实际配置（含面板密码，**不入库**；首次运行可从 `config.example.json` 复制） |
| `config.example.json` | 配置模板，字段与默认值说明见下文「配置项」 |
| `start-panel.vbs` | **推荐入口**：无窗口拉起面板并自动开浏览器 |
| `start-panel.bat` | 备用入口（.vbs 被策略拦截时用；窗口一闪即退） |
| `run.pid` | 运行期生成，记录当前网关 PID，停止/重启依据 |
| `../logs/panel.log` | 面板自己的操作审计 |
| `../logs/service.log` | 网关 stdout/stderr（后台启动时重定向到这里） |

## 启动面板

双击 `start-panel.vbs`（或桌面快捷方式 `start-panel.lnk`，它指向 `start-panel.bat`）。
面板监听 `http://127.0.0.1:8790/`，随后自动打开浏览器。

可选命令行开关（给开机自启用，手动双击不需要）：

| 开关 | 作用 |
|---|---|
| `--no-browser` | 不自动开浏览器（自启时用，避免开机弹窗） |
| `--start-service` | 面板起来后顺手把网关拉起（延迟 1s，失败只记 `logs/panel.log` 的 `[autostart]`，不影响面板） |

开机自启示例（写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`）：

```bat
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WB2API-Panel /t REG_SZ ^
  /d "\"<安装目录>\.venv\Scripts\pythonw.exe\" \"<安装目录>\panel\wb_control_panel.py\" --no-browser --start-service" /f
```

**启动器是幂等的**，双击多少次都只有一个面板实例：

```
探测 http://127.0.0.1:8790/api/status
  ├─ 返回 200 → 已在运行 → 只打开浏览器，不派生新进程
  └─ 连不上   → 未运行   → 用 pythonw 后台拉起（无窗口），面板自己开浏览器
```

第二层保护写在面板进程里：`Local\WBControlPanel-<端口>` 命名互斥体，
即使绕过启动器直接跑 `pythonw wb_control_panel.py`，第二个实例也会打印
「控制面板已在运行…」并立即退出，不会变成第二个监听者。

> 注意：`start-panel.bat` / `start-panel.vbs` 里的探测地址硬编码为 8790。
> 改了 `config.json` 的 `panel_port` 要同步改这两个文件。

首次使用若提示找不到解释器，先建 venv：

```bat
cd /d <安装目录>
python -m venv .venv
```

## 界面布局

```
┌─────────────────────────────────────────────────────────────┐
│ WorkBuddy2API-Hub 控制面板      端口 8788 [关闭面板] [打开业务看板]│
├───────────────────────────────┬─────────────────────────────┤
│ 运行状态                       │ 最近操作                    │
│  ● 运行中   健康检查通过        │ ✓ 启动 · 成功               │
│  PID / 运行时长 / 端口 / 检测时间│   启动成功，PID 11204…      │
│  [区域] [账号] [API Key] 标签   │   11:39:20                  │
│  ┌──────┬──────┬──────┐        ├─────────────────────────────┤
│  │ 启动 │ 停止 │ 重启 │        │ 状态判定规则（图例）         │
│  └──────┴──────┴──────┘        │                             │
│  提示语 + 自动刷新开关          │                             │
├───────────────────────────────┴─────────────────────────────┤
│ 服务日志（logs/service.log 尾部） │ 操作记录（logs/panel.log） │
└─────────────────────────────────────────────────────────────┘
```

- 状态胶囊用颜色区分：绿=运行中、灰=已停止、琥珀=启动中/停止中、红=异常/端口冲突。
- 运行中时状态圆点做呼吸动画，异常状态不闪，避免误读成"正在启动"。
- 两个日志窗各自保持滚动位置：原本停在底部才自动跟到底，往上翻查历史不会被打断。

## 四个操作的行为逻辑

### 启动

1. 先取一次实时状态；已运行 → 直接返回"已在运行（PID x）"，不再派生第二个进程；
   端口被第三方占用 → 返回"端口被其他程序占用"，不会去动别人的进程。
2. 解析解释器：`config.python_exe` → `.venv\Scripts\pythonw.exe` → `python\pythonw.exe`
   → `.venv\Scripts\python.exe`。
3. `subprocess.Popen` 派生网关，状态切到 **启动中**（前端轮询同时提速到 0.8s）。
4. 每 0.5s 探 `GET /health`，直到通过或超时（默认 35s）。
5. 成功后**以端口占用者为真实 PID**回写 `run.pid`，返回"启动成功，PID x，已监听…"。

超时不早退：venv 的 `pythonw.exe` 是重定向器，派生真正解释器后自己立刻退出，
拿 Popen 的 PID 判活会误报失败，所以只认"端口 + 健康检查"。

### 停止

1. 状态为已停止 → 直接返回"已是停止状态"（幂等）。
2. 先试**优雅退出**：`POST /panel/login` 取 token → `POST /shutdown` 带 `X-Panel-Token`，
   网关自己 flush 日志后 `os._exit(0)`。
3. 密码不符或接口不通 → 回退 `TerminateProcess`（与项目自带 `restart-wb-proxy.bat` 的
   `Stop-Process -Force` 同级别），并如实标注"强制结束"。
4. 轮询端口释放，最多 15s；成功则删 `run.pid`，失败报"端口仍被占用"。

### 重启

停止（含上面的优雅/降级判定）→ 确认端口释放 → 重新派生 → 等健康检查。
任何一步失败都会立即返回失败原因，不会把网关留在"停了没起来"的中间态而静默。

### 关闭面板

顶栏「关闭面板」只结束**面板自身**（`wb_control_panel.py`，默认 8790），网关进程不受影响：

1. 前端 `confirm` 二次确认，确认后按钮变「正在关闭…」，全部按钮禁用；
2. `POST /api/exit` → 服务端先回 `{"ok":true}`，0.4s 后 `HTTPD.shutdown()` + `os._exit(0)`，
   端口随进程释放；
3. 前端用 `/api/status` 复核：连续 12 次探活都失败才判定已关闭，避免"请求被断但进程还活着"
   的误报；成功后面板进入灰化遮罩「控制面板已关闭」，所有轮询停止；
4. 重新打开：双击 `start-panel.vbs`（或 `start-panel.bat`）。

> 与「停止」的区别：「停止」停的是网关（8788），面板继续可用；「关闭面板」停的是面板
> （8790），网关继续在后台转发请求。

### 状态反馈

- 点击后按钮立刻变成「启动中… / 停止中… / 重启中…」+ 旋转图标，三个按钮同时禁用（防连点）。
- 结果用右上角 toast 显示：成功 5 秒自动消失，失败 12 秒并保留可读原因（含日志尾部）。
- 右侧「最近操作」持久显示最后一次操作的结果与时间，刷新页面也不丢（由服务端保存）。

## 状态检测与刷新机制

服务端 `snapshot()` 综合两个信号：

| 端口 `8788` | `GET /health` | 判定 |
|---|---|---|
| 未监听 | — | **已停止** |
| 监听 | JSON 带 `accounts` 字段且 `ok:true` | **运行中** |
| 监听 | 无响应/超时，占用者是 python | **异常**（可停止、可重启） |
| 监听 | 非本网关应答，占用者不是 python | **端口冲突**（全部禁用，不越权） |

- 占用者 PID 用 `GetExtendedTcpTable`（ctypes）取，不依赖 `netstat` 本地化输出、不需要 psutil。
- 归属校验：PID 与 `run.pid` 一致，或镜像名是 `python.exe/pythonw.exe`；
  第三方程序（实测用 node 占端口）一律只报冲突，禁止停止 —— 防误杀。
- 前端轮询：常规 2s，`starting/stopping` 或操作执行中 0.8s；服务端另有 0.4s 缓存防抖。
- 用 `setTimeout` 链而不是 `setInterval`，慢响应不会堆叠请求；`fetch` 带 4s AbortController。
- 面板自身连不上（浏览器报"面板连接中断"）说明面板进程没了，不是网关没了。

## 后台启动的实现方式（无窗口）

```python
DETACHED_PROCESS      = 0x00000008   # 不继承父控制台
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW      = 0x08000000
startupinfo.dwFlags |= STARTF_USESHOWWINDOW
startupinfo.wShowWindow = 0          # SW_HIDE
```

三重保证：

1. **解释器本身是 GUI 子系统**：`pythonw.exe` 不分配控制台，从根上没有黑框；
2. **`DETACHED_PROCESS`**：即使父进程在控制台里，子进程也不挂到它上面；
3. **`SW_HIDE` + `CREATE_NO_WINDOW`**：兜底，任何情况下窗口都不显示。

同时 `stdin=DEVNULL`、`stdout/stderr` 重定向到 `logs/service.log`（超过 20MB 自动滚存为
`.log.1`），所以没有窗口也能查日志。派生后父进程立刻返回，关掉面板不影响网关；
反过来网关状态由端口与 `/health` 判定，不依赖面板活着。

## 配置项（`config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `panel_host` | `127.0.0.1` | 面板监听地址，公网暴露前请自行加反代与鉴权 |
| `panel_port` | `8790` | 面板端口（与网关错开） |
| `service_port` | `8788` | 网关端口，改这里即可换端口 |
| `python_exe` | 空 | 留空自动找 `.venv\Scripts\pythonw.exe` |
| `lan` | `false` | 局域网共享：`true` 时网关带 `--lan` 启动 = 监听 `0.0.0.0` + 强制 API Key；改完下一次「启动 / 重启」即生效（面板不重启也读新值） |
| `panel_password` | `admin` | **网关看板密码**，停止时用它走 `/shutdown`；改了看板密码要同步改这里 |
| `start_timeout` | 35 | 启动等待健康检查的秒数 |
| `stop_timeout` | 15 | 停止后等端口释放的秒数 |
| `open_browser` | true | 面板启动后自动开浏览器 |

## 已知边界

- 服务跑在沙箱/受限账户里时，派生进程可能被作业对象回收；正常桌面双击运行不受影响。
- 归属判定是启发式：如果端口被**另一个 Python 程序**占着且不应答健康检查，会显示"异常"并允许停止。
  这种情况面板会先尝试优雅退出（对非网关无效），再强制结束 —— 属于已知取舍。
\- 面板本身无登录鉴权，默认只听 `127.0.0.1`；如需局域网访问，请自行加反向代理与访问控制。

## 局域网共享（`lan: true`）

面板只负责把 `--lan` 传给网关，其余交给网关自己：

1. `config.json` 里 `"lan": true` → 面板启动网关时命令行变成 `wb_proxy.py --port <service_port> --lan`；
2. 网关据此监听 `0.0.0.0:<service_port>` 并强制要求 API Key（`accounts/settings.json` 里没有可用 key 时会自动生成一条并打印）；
3. 启动成功的提示语与 `GET /api/config` 会给出 `0.0.0.0:<port>（局域网共享，需 API Key）` 以及用出口网卡探测到的 `http://<lan_ip>:<port>/v1`；
4. Windows 首次使用还要放行入站端口，否则本机能用、同事连不上：

```bat
netsh advfirewall firewall add rule name="WorkBuddy2API-Hub-LAN-TCP8788" ^
  dir=in action=allow protocol=TCP localport=8788 remoteip=<你的网段>
```

> 回滚：`"lan": false` + 面板「重启」即退回只听 `127.0.0.1`；防火墙规则用
> `netsh advfirewall firewall delete rule name="WorkBuddy2API-Hub-LAN-TCP8788"` 删除。
