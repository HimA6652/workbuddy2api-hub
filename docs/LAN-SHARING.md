# 局域网共享网关 · 操作手册

把本机的 workbuddy2api-hub 变成团队共用的 OpenAI 兼容网关：同事的客户端连
`http://<你的局域网IP>:8788/v1`，用你发的 API Key 调用。

## 1. 原理（三个开关缺一不可）

| 层 | 本机默认 | 共享后 | 谁改 |
|---|---|---|---|
| 监听地址 | `127.0.0.1` | `0.0.0.0` | `--lan`（或面板 `"lan": true`） |
| 鉴权 | 可关 | 强制 API Key | `--lan` 自动打开 |
| 防火墙 | 入站默认拒 | 放行 TCP 8788（限网段） | `netsh advfirewall` |

`--lan` 只做两件事：把 host 切成 `0.0.0.0`，以及在没有可用 key 时生成一条
`wb-…` 的 launcher key 并强制鉴权。**面板里已经建过 key 时，launcher key 不被接受**
（`identify_key()` 只认 `accounts/settings.json` 的 `api_keys` 列表），这是最常见的坑。

## 2. 开起来

**方式 A（面板）**：`panel/config.json` 里 `"lan": true` → 面板点「重启」。
启动成功的提示会带 `已监听 0.0.0.0:8788（局域网共享，需 API Key）` 和
`局域网入口 http://<lan_ip>:8788/v1`。

**方式 B（命令行）**：

```bat
python wb_proxy.py --port 8788 --lan
```

放行入站（管理员权限，按需收窄网段）：

```bat
netsh advfirewall firewall add rule name="WorkBuddy2API-Hub-LAN-TCP8788" ^
  dir=in action=allow protocol=TCP localport=8788 remoteip=<你的网段>
```

## 3. 发 Key

面板「API Key」页建 key，字段含义：

| 字段 | 作用 |
|---|---|
| `models` | 白名单，空 = 全部模型 |
| `accounts` | 限定可调度的账号；空 = 不限（但不会突破账号自身 `enabled` 与 `ready()` 判定） |
| `concurrency_limit` | 该 key 的并发上限，0 = 不限 |
| `realm` | `cn` / `intl`，决定走哪批账号 |

`/usage` 里按 key 统计，方便按人核账。**每人一条 key**，别共用——泄露时才能只吊销一条。

## 4. 同事怎么填

- Base URL：`http://<你的局域网IP>:8788/v1`（OpenAI 兼容）
- API Key：你发的 `wb…` / 32 位 hex 串
- 模型名：`GET /v1/models` 列出的名字，直接抄
- curl 自检：

```bash
curl -s http://<本机IP>:8788/v1/models -H "Authorization: Bearer <KEY>" | head -c 200
curl -s http://<本机IP>:8788/v1/chat/completions \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'
```

## 5. 排错

| 现象 | 先查 |
|---|---|
| 本机能用、同事连不上 | 防火墙规则（`netsh advfirewall firewall show rule name=…`）、网段是否被 `remoteip` 排除 |
| 连上但 401 | key 是否在 `api_keys` 里且 `enabled`；是不是拿的 launcher key |
| 连上但超时 | 安全软件（如火绒）的网络防护、交换机 AP 隔离 |
| 只有部分模型可用 | key 的 `models` 白名单、账号 `disabledModels` |
| 偶发 429 | 账号并发/风控，面板里看该 key 的并发上限与账号冷却 |

## 6. 回滚

```bat
REM 1) 退回只听本机
REM    panel/config.json 改 "lan": false → 面板「重启」
REM    或命令行去掉 --lan 重启
REM 2) 删掉防火墙规则
netsh advfirewall firewall delete rule name="WorkBuddy2API-Hub-LAN-TCP8788"
```
