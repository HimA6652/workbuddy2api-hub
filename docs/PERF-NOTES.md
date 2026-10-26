# 面板性能：根因、实测数据与已落地的改动

本文件记录账号规模上来之后「进面板卡一下、每隔十几秒又卡一下」的完整排查与改动，
数字都是本机实测（数百个账号、Windows、headless Edge + CDP），不是估算。

## 1. 症状与基线

| 指标 | 基线（未改动） |
|---|---|
| 进面板到账号表可交互 | **4 984 ms** |
| 账号表 DOM 节点 | 33 595（约 300 行 × 约 108） |
| 每 15 s 一轮的“主线程长任务” | **3.7 – 4.7 s**（PerformanceObserver longtask，间隔精确 15.0 s） |
| `GET /accounts?realm=all` 响应体 | **6 031 888 B**，无 `Content-Encoding` |
| 该接口服务端耗时（本机、无并发） | p50 256 ms / max 271 ms；页面内 280 ms |
| `renderAccounts()` 整表重画 | 89 ms（`box.innerHTML` 全表替换） |
| 面板自身轮询（后台线程服务） | 每 15–30 s 一次 `POST /panel/login` + `/settings` + `/v1/models` |

## 2. 三个根因

1. **载荷**：`/accounts` 每行都带完整的 `credits.packages`，353 个账号各带一整包，
   光这一项就占 87.9%（5.3 MB / 6.0 MB）。列表页一个字节都用不到它。
2. **渲染**：约 300 行 × 11 列每次轮询整表 `innerHTML` 重画；同时行内 618 个 `select`
   被自绘下拉增强器（`dashboard.html:17961 scan()` + `:17983 MutationObserver`）逐个包装，
   单这一项实测 4.3 s —— **「每几秒卡一下」的全部原因**。
3. **放大**：账号轮询 15 s 一轮；积分自动补拉的条件是「任意账号缺 credits」，
   而 128 个停用号永远拿不到 credits，条件恒真 → 每 15 s 一次全池积分扫描
   （`POST /accounts/credits`）+ 一次 6 MB 往返 + 一次额外整表重画。

## 3. 改动（已在本分支）

| 编号 | 改动 | 位置 |
|---|---|---|
| 前端-1 | 账号表内的 `select` 不参与自绘下拉 | `dashboard.html` `enhance()` |
| 前端-2 | 积分自动补拉只跑一次，且只看 `enabled` | `dashboard.html` `loadAccounts()` |
| 前端-3 | 账号轮询 15 s → 60 s（`document.hidden` 仍跳过，切回前台补刷） | `dashboard.html` 定时器 |
| 后端-1 | `_json` 对大响应体 gzip（>1 KB 且客户端接受时；>1 MB 用 level 1） | `wb_proxy.py` `_json` |
| 后端-2 | `/accounts` 载荷瘦身：`credits.packages` 摘要成 `packagesReady` / `packagesCount` | `wb_proxy.py` `_credits_summary()` / `account_views()` |
| 后端-3 | 全池积分扫描后台化：POST 立即回执 + 新增 `GET /accounts/credits/status` 进度 | `wb_proxy.py` `_credits_sweep_async()` |
| 后端-4 | 面板首页 HTML 也走 gzip（同一套判定：>1 KB 且客户端接受） | `wb_proxy.py` `_dashboard()` |
| 前端-4 | 积分按钮改轮询进度；弹窗用 `refresh:false` 读缓存明细，不再重拉全池 | `dashboard.html` `fetchCredits()` / `openCreditsModal()` |

配套：`tests/_test_credits_sweep_guard.py`（路由接线断言，19 例）与新增
`tests/_test_credits_async.py`（真起一个隔离网关，36 项断言：进度单调、单飞、
单账号同步路径不变、`/accounts` 不再带 `packages`）；
`tests/_test_dashboard_cache_headers.py` 新增 [11] 组 11 项断言，钉住 HTML gzip
的两种表示（编码头、`Vary`、解压后逐字节一致、各自独立的校验符、304 行为）。

## 4. 实测收益

前端三项（同一份数据、补丁页 vs 基线页）：

| 指标 | 基线 | 改动后 |
|---|---|---|
| 进面板到表可交互 | 4 984 ms | **1 332 ms** |
| 账号表 DOM 节点 | 33 595 | 20 308 |
| 自绘下拉数量 | 618 | 0 |
| 20 s 窗口内的长任务 | 3 733 ms | **0 ms** |

后端 gzip（`/accounts?realm=all`，同一份数百账号数据）：

| 档位 | 体积 | 耗时 |
|---|---|---|
| 原始 | 6 031 888 B | — |
| gzip level 6 | 493 236 B（×12.23） | 63.3 ms |
| gzip level 1 | 671 165 B | 29.5 ms |

面板首页（`_dashboard()`，隔离实例实测，含语言注入后的字节）：

| 档位 | 体积 | 说明 |
|---|---|---|
| identity | 1 013 459 B | 不带 `Accept-Encoding` 时原样发出 |
| gzip（level 6） | **302 264 B**（×3.35） | 压缩耗时 30.8 ms，解压后与 identity 的 MD5 相同 |
| 304（两种表示各自） | 0 B | 校验符命中，`Vary: Accept-Encoding` 照常重发 |

ETag 按表示派生：identity 是 `"<mtime>-<size>-<语言>"`，gzip 变体多一个
`-gz` 后缀；两种变体互不命中，避免共享缓存把压缩副本回给不支持的客户端。

载荷瘦身（`account_views()` 之后）：

| 指标 | 改前 | 改后 | 变化 |
|---|---|---|---|
| raw | 6 027 957 B | **899 712 B** | −5 128 245 B |
| gzip6 | 490 057 B | **80 819 B** | −409 238 B |
| 仍带 `packages` 的行 | 353 | 0 | — |
| 带 `packagesReady` / `packagesCount` 的行 | — | 353 | — |
| 非 credits 字段逐行差异 | — | 0 / 全部行 | 等价 |

单账号明细（弹窗按需拉的那份）27 697 B / gzip6 2 575 B，保持完整、不受瘦身影响。

## 5. 接口契约变化

- `GET /accounts`：`credits` 里不再有 `packages`，改为 `packagesReady`（bool）与
  `packagesCount`（int）。取明细走 `POST /accounts/credits/detail`（原接口，未改）。
- `POST /accounts/credits`：多账号目标现在是**异步回执**
  （`{ok, started, busy, total, done, results: [], accounts}`），不再等整轮跑完；
  单账号目标（带 `uid`）仍是同步，响应形状不变。
- 新增 `GET /accounts/credits/status`：`{ok, running, busy, total, done, percent, started, finished, error}`。
- `GET /`（面板首页）：带 `Accept-Encoding: gzip` 时压缩，始终发
  `Vary: Accept-Encoding`，ETag 按表示区分（`-gz` 后缀）。
- 进度只存在内存里，进程重启归零；扫描间隔仍是 `WB_CREDITS_SWEEP_GAP_MS`（默认 800–1200 ms 抖动）。
  数百个账号串行一轮约 6.4–9.6 分钟，这段时间面板不再被一个挂起的请求拖住。

## 6. 上线与回滚

改动集中在两个文件，都是“替换后刷新/重启”级别：

```powershell
# 1) 备份
Copy-Item <安装目录>\dashboard.html <安装目录>\dashboard.html.bak-<日期>
Copy-Item <安装目录>\wb_proxy.py    <安装目录>\wb_proxy.py.bak-<日期>
# 2) 覆盖（本仓库 → 安装目录）
Copy-Item .\dashboard.html <安装目录>\dashboard.html -Force
Copy-Item .\wb_proxy.py    <安装目录>\wb_proxy.py    -Force
# 3) 重启网关（面板 API，或面板上的“重启”按钮）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8790/api/restart -ContentType application/json -Body '{}'
```

- 只改前端：覆盖 `dashboard.html` 后刷新浏览器即可，不必重启。
- 回滚：把两个 `.bak-<日期>` 覆盖回去 → 重启网关。数据（`accounts/`、`usage/`）不涉及。
- 旧前端 + 新后端、新前端 + 旧后端都能跑：`packages` 缺失时前端走
  `/accounts/credits/detail` 现拉；旧前端拿到异步回执只会看到一次“已提交”而不轮询进度。

## 7. 还没做的

- `/accounts` 分页 / 增量（`?since=`、`?fields=`）：账号几千个时仍会撞上同一面墙，
  属于结构性改动，等上游或账号规模真的过千再动。
- 表格虚拟滚动：同样只在大规模下才划算，当前约 300 行 + 60 s 轮询已经够用。
