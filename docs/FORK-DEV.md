# 本仓库的分支与开发约定（Fork 版）

> 这份文档只描述**这个 fork** 的开发流程；上游规范见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
> 给 AI / 智能体：改本仓库前先读 `../AGENTS.md` 与 `../CONTRIBUTING.md`，冒烟门禁一条都不能跳。

## 1. 远端与分支模型（单 main）

```
origin    git@github.com:<YOU>/workbuddy2api-hub.git       # 自己的 fork，可写
upstream  https://github.com/ardeyouxipianyi/workbuddy2api-hub.git  # 上游，只读
main      = 唯一长期分支：上游 main + 我们的提交，日常提交都在这里
```

约定：

1. **只有一条长期分支 `main`**。没有 `dev`、没有 `feat/*`：这个仓库自用，改动不打算回馈上游，
   多一条分支只多一份同步成本。
2. **每个提交一件事**（上游 CONTRIBUTING 第 0 节）：能独立评审、独立回滚。
3. **提交信息**：标题 `<type>(<scope>): 中文摘要`，不带句号、一行只说一件事；
   需要解释时开正文，按「问题 → 做法 → 影响」三段写，中英双语，验证写命令与数字，
   不写心路历程，也不写「清理 / 脱敏 / 删了真实标识」这类元描述（提交信息在托管平台是公开可见的）。
4. **历史提交保持原样**：早期几条提交信息格式不规范，重写会改掉所有 hash，
   收益小于代价，因此从本约定起的新提交才按上面格式写。

## 2. 本地跑起来（隔离实例，别动线上 8788）

线上实例通常正跑在 `127.0.0.1:8788`，**不要停它**（可能是活跃对话链路）。联调用另一套数据和端口：

```bash
# 1) 隔离数据目录：只放要联调的账号副本
mkdir -p _devdata/accounts _devdata/usage _devdata/logs
cp /path/to/prod/accounts/<uid>.json _devdata/accounts/
cp /path/to/prod/accounts/settings.json _devdata/accounts/

# 2) 起实例（ACCOUNTS_DIR 覆盖账号目录；端口用 8801，见 §6）
ACCOUNTS_DIR="$PWD/_devdata/accounts" python wb_proxy.py --port 8801

# 3) 通了再验
curl -s localhost:8801/health
curl -s -H "Authorization: Bearer <KEY>" localhost:8801/v1/models | head -c 200
```

> ⚠️ **refreshToken 是单次轮换的**：同一个账号在两台实例上同时跑，谁后刷新谁把对方踢下线。
> 联调只用**访问令牌仍在有效期**的账号副本（`expiresAt` 未过），或干脆用一次性小号。

## 3. 提交前门禁（摘自 CONTRIBUTING 第 ⚡ 节，硬性）

| 项 | 判定标准 |
|---|---|
| 面板 | 页面能打开、能登录、浏览器控制台无 JS 报错 |
| 服务 | 日志无报错、`/health` 返回 `ok:true` |
| 端到端 | 真实 `POST /v1/chat/completions` 拿到**上游模型回复**（不只是前后端往返） |
| 测试 | `python tests/run_all.py` 全绿；跑完 `python scripts/check_clean_checkout.py` |
| 口径 | 纯标准库、兼容 Python 3.9、文本文件 LF、不夹带密钥与本机绝对路径 |

跑测试时**不要带** `ACCOUNTS_DIR`（否则 `_test_agents.py` 会失败，见 §6）。

## 4. 同步上游 / 推送

日常：

```bash
git checkout main
git add -A && git commit -m "feat(scope): 说明"
git push origin main
```

吸收上游（建议每 1-2 周一次）：

```bash
git fetch upstream --tags
git merge upstream/main          # 在 main 上做合并提交；冲突「两边都留」
git push origin main
```

- 用 **merge** 而不是 rebase：`main` 已经推到 origin，rebase 会让历史与远端分叉、必须强推。
- 冲突高发区只有 `wb_proxy.py` / `dashboard.html` 两个大文件，本地改动各 13-14 行。
- 合并后跑一遍 §3 的门禁再推。

真要把某个改动回馈上游时，临时开一条一次性分支、`git rebase upstream/main`、单独开 PR，
合入后删掉——那是一次性动作，不是长期分支。

## 5. 目录增量（相对上游）

| 路径 | 内容 |
|---|---|
| `panel/` | 本机控制面板（单文件 Python 标准库）：启停/重启网关、LAN 开关、开机自启 |
| `docs/FORK-DEV.md` | 本文 |
| `docs/LAN-SHARING.md` | 局域网共享网关操作手册（脱敏） |
| `docs/PERF-NOTES.md` | 面板卡顿的根因、实测数据、改动清单与上线/回滚步骤 |

## 6. 三个踩过的坑（都有实测）

1. **跑测试不要带 `ACCOUNTS_DIR`。** 带着指向真实账号目录的 `ACCOUNTS_DIR` 跑
   `tests/run_all.py`，`_test_agents.py` 的 `opencode models populated from catalog fallback`
   会失败（134 pass / 1 fail）——因为 handler 走了真实账号目录的模型目录，而不是测试里 mock 的
   `_agents_models()`。`unset ACCOUNTS_DIR` 后单测 135/0、全套 **227 passed / 0 failed**（约 67 s，`--jobs 4`）。
2. **8799 在本机不是空闲端口。** 本机 `8799` 被另一个服务（返回 `keypool: 仅支持 GET
   /v1/models...`）占着，wb_proxy 会直接打印 `the port is reserved or held by another program;
   wb-proxy did NOT start.` 然后退出。隔离实例统一用 **8801**。
3. **面板配置只在启动时读一次。** `CFG = load_config()` 在导入期执行，改完
   `panel/config.json`（端口、`python_exe`、`lan`）必须重启面板进程才生效。
   另外 `panel/config.json` 里有本机面板密码，已进 `.gitignore`，不要提交。

## 7. 自己用模式（不提 PR 也完全可行）

这个仓库是给自己组内用的分叉，**上游 PR 是可选项**。日常就是 §4 那三行：改、提交、推 `main`。

### 7.1 仓库可见性（重要）

`HimA6652/workbuddy2api-hub` 是 **public fork**：GitHub 不允许把 fork 直接改成 private，
所以推上去的任何提交都是公开可读的（包括提交信息）。三条路：

- **保持公开**：提交前必须确认没夹带密钥/账号。预检：
  ```bash
  git diff --cached --name-only | grep -E 'accounts/|settings\.json|panel/config\.json' && echo '✗ 停，别提交' || echo '✓ 路径干净'
  git diff --cached | grep -nE 'wb-[A-Za-z0-9_-]{20,}|refreshToken|accessToken|panel_password' && echo '✗ 有敏感串' || echo '✓ 无敏感串'
  ```
- **脱离 fork 网络**后转私有：见 §7.4。
- **新建私有仓当主库**：
  ```bash
  # GitHub 网页 New repository → 名字 wb2api-hub-private → 选 Private（不要 fork）
  git remote add private git@github.com:HimA6652/wb2api-hub-private.git
  git push private main
  git remote set-url origin git@github.com:HimA6652/wb2api-hub-private.git   # 以后 push 走私有仓
  ```
  `origin` 换成私有仓后，公有 fork 不再更新；`upstream` 照 §4 同步，随时能重新拉起来。

### 7.2 维护规矩

- 只在 `main` 上提交；`main` 同时是「我们的线」和「上游同步点」，靠 merge 保持一条历史。
- 改 `.py` 前先备份（`patch_panel.py` 就是这么做的），出问题 `git checkout -- <file>` 一条命令回滚。
- 提交信息写「做了什么 + 怎么验证」，别写过程记录；验证数字要能复现。
- 文档里的路径、IP、网段、口令一律写占位值（`<安装目录>` / `<本机IP>` / `<你的网段>` / `<KEY>`），
  不写本机的真实值；示例命令要能被别人直接替换后使用。

### 7.3 去掉 GitHub 上的 Fork 标记（脱离 fork 网络）

想让仓库看起来是「自己的独立项目」而不是某个项目的分叉，可以用 GitHub 的 **Leave fork network**
（离开分叉网络）。它只在同时满足三个条件时出现（GitHub 文档原文）：

| 条件 | 本仓库实测 |
|---|---|
| fork 是公开的 | `visibility: public` ✓ |
| fork 小于 1GB | `size: 4.84 MB` ✓ |
| 没有任何子 fork | `forks_count: 0` ✓ |

另外本仓库没有任何指向上游的 PR（搜索 `repo:ardeyouxipianyi/workbuddy2api-hub type:pr author:HimA6652`
→ `total_count: 0`），脱离不会关掉任何合作中的 PR。

操作路径：仓库主页 → **Settings** → General（默认页）→ 滚到最下面的 **Danger zone** →
**Leave fork network / 离开分叉网络** → 读警告点「I have read and understand these effects」→
输入仓库全名确认。**不能通过 API 或 git 命令完成**，只有网页这一条路。

会发生什么：

- 仓库名旁的 `forked from ardeyouxipianyi/workbuddy2api-hub` 标记消失，网络图断链，
  上游的 fork 列表里不再显示本仓库。
- **永久且不可逆**：脱离后无法再连回 fork 网络。
- issues / PR / wiki / star / follower / 评论 / 子 fork 等元数据不保留；git 历史与提交元数据全部保留。
  本仓库 forks 0、star 0、issues 关闭，实际无损失。
- 脱离过程中个别功能会短暂不可用，完成后恢复。

脱离后再想变私有就顺了（不再是 fork，不再受「公开 fork 只能是公开」限制）：

```
Settings → Danger zone → Change visibility → Make private
```

本地完全不受影响：`upstream` 这个 remote 照旧 `git fetch upstream` 同步，
变的只是 GitHub 上两者有没有「亲缘关系」，不是 git 层面的父子。

## 8. 更新检查指向本仓库

本项目独立维护后，面板「设置 → 更新检查」问的不再是上游，而是本仓库的 Releases：

| 位置 | 值 |
|---|---|
| `wb_updates.py:35` `DEFAULT_REPOSITORY` | `HimA6652/workbuddy2api-hub` |
| `wb_update_portable.py:25` `REPO_DEFAULT` | 同上（portable 更新器） |
| 面板顶栏 GitHub 图标 / README 徽章 / quick-deploy 地址 | 同批改成 `HimA6652/workbuddy2api-hub` |

仍可用环境变量覆盖，容器或 systemd 单元里加一行即可（只允许 `owner/repo` 形状）：

```
WB_UPDATE_REPO=someone/workbuddy2api-hub
```

### 8.1 两条取数路径与节流

1. 主路径 `GET https://api.github.com/repos/<repo>/releases`（匿名 60 次/小时/出口 IP）；
2. 回退路径 `GET https://github.com/<repo>/releases/latest` 的 302 `Location` 末尾取 tag（走网页流量，不吃 API 配额）；
3. 单次 10s 超时、24 小时节流、失败下个窗口重试；凭据只从 `GITHUB_TOKEN` 环境变量读，绝不落盘。

### 8.2 首次启用前必须先发一个 Release

2026-10-26 实测本仓库还没有 Release：

```
curl -s https://api.github.com/repos/HimA6652/workbuddy2api-hub/releases      -> 200, []
curl -sI https://github.com/HimA6652/workbuddy2api-hub/releases/latest        -> 302 -> .../releases（无 tag）
```

空列表不算失败：主路径拿到 `[]` 会正常解析、不抛异常，只是 `pick_latest([])` 返回 `None`，
于是状态里 `latest_version` 为 `null`、`last_error` 为空，面板「更新状态」那一栏显示的是
**「尚未检查」**、最新版本一栏是 `-`（`dashboard.html:13170-13176` 的 `updateStateLabel`）。
回退路径在无 tag 时抛 `ValueError: no release tag in the redirect target`，但那次异常被
`fetch_releases` 的 `except` 丢弃、改抛主路径的结果，所以最终不会写进 `last_error`。

实测（`_perf/probe_update.py`，2026-10-26 15:43）：

```
fetch_releases()          -> "[]"（2 字符）
fetch_latest_release()    -> ValueError: no release tag in the redirect target
UpdateChecker.check()     -> {"current_version":"1.8.0","latest_version":null,"latest_tag":"",
                             "update_available":false,"last_error":"","release_url":""}
```

另外，生产 `accounts/settings.json` 里还存着改指之前上游检查的缓存
（`update_check.latest_version = "1.8.0"`，时间戳 1792986628.47 = 2026-10-26 11:50:28，
`update_check_enabled = true`）。下一次启动检查会把它覆盖成空值，于是面板从「已是最新」
退成「尚未检查」。发一个 Release 即可：

1. 仓库主页 → 右侧 **Releases** → **Draft a new release**；
2. **Choose a tag** 选已存在的 `v1.8.0`（或新建 `v1.8.1`）；
3. 标题填 `v1.8.0`，说明写本次自用改动要点 → **Publish release**。

发布后同一接口返回该条记录，面板显示「最新版本 v1.8.0 / 已是最新」。
注意 `v1.8.0` 这个 tag 在本地与远端都已存在（89 个 tag 一并推过），
所以「新建 tag」不是必需动作，**发布 Release 记录**才是。

### 8.3 版本号约定（供以后比较）

`parse_version` 只认 `vX.Y.Z` 三段数字，后缀一律视为不可比较。所以自用版本号这样走：

- 吸收上游新版本 → 沿用上游 tag（`v1.9.0`）；
- 只有本仓库的修复 → patch 位 +1（`v1.8.1`、`v1.8.2`），不要用 `v1.8.0-fork.1` 这类形式（会被判为不可比较）。

### 8.4 镜像仍来自上游

本仓库没有 CI 构建容器镜像，`README.md` 与 `docker-compose.yml` 里的
`ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest` 未改动。自建镜像时再一并替换。
