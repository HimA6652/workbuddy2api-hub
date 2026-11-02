# 上游批次移植记录（2026-11）

> 范围：本 fork 从 `main@cfaf674`（v1.10.1 发布提交）起，移植上游 `ardeyouxipianyi/workbuddy2api-hub` 在 2026-10-25 至 2026-11-02 之间、本地尚未吸收的修正。
> 状态：目标版本 `v1.11.0`；开发分支 `port/upstream-2026-11` 保留。tag / GitHub Release 与生产部署不在本文范围。
> 本文只记录可复现的提交映射、验证数字与合并步骤；不包含账号、密钥、本机路径或生产数据。

## 1. 基线与范围

- 本地基线：`main` / `origin/main` / tag `v1.10.1` = `cfaf6747515262f731a1cac30eae9614033385d5`。
- 上游基线：`upstream/main` = `28527f48cfaea0e456612eb8922cb51b5d804a22`（上游自己的 v1.10.1，2026-11-02）。
- 审查范围：上游 `6daaae4..upstream/main` 中本地尚未包含的提交；实际落地 17 个上游提交（其中 `28527f4` 只取 `normalize_proxy_url` 部分）。
- 注意：上游 `28527f4` 的版本号也是 v1.10.1，但它与本 fork 的 `cfaf674` 不是同一个提交；不能按版本号判断代码是否相同。
- 开发分支：从 `cfaf674` 切出 `port/upstream-2026-11`，按三批 cherry-pick，手工解决冲突后再补本地复查修复与文档提交。

## 2. 提交映射

### 第一批：账号、鉴权、工具名与 6004 事实

| 上游提交 | 本地提交 | 内容 |
| --- | --- | --- |
| `e38fcb2` | `4321fa7` | 正数 `reserve_credits` 不再拦截目录标为免费的模型；`reserve_blocked(model)`、`apply_reserve_credits(..., free_models)` |
| `5f2bdb1` | `dad3389` | 解析 `WWW-Authenticate`，把真正的 `invalid_token` 放在 HTML 豁免之前；死 401 不再当代理页 |
| `b04868e` | `59bb76e` | `normalize_tools()` 丢弃 `function.name` 非法或重复的工具，空列表时移除 `tools` |
| `85a7968` | `d4a4af9` | 付费模型撞 6004 也保留 `cappedAt`；`model_cap_marks` 运行时表与窗口落盘恢复 |
| 本地测试修正 | `2a33f95` | `_test_analytics_credit_range.js` 的 Date stub 跟随当前日期，修掉固定 2026-10-28 的过期夹具 |

### 第二批：失败状态、健康检查与池空语义

| 上游提交 | 本地提交 | 内容 |
| --- | --- | --- |
| `f60965e` | `dc23814` | 新增 `upstream_failure_response()`：先定 503/502，再写 usage 与日志，客户端和用量行同码 |
| `2c34a9e` | `9e40e52` | `/health` 增加按 realm、按模型和 Key 绑定范围的可用数；模型统计走只读 `refresh=False` |
| `7b453f3` | `1f3663d` | 池空明细只列本次候选；所有候选只在等模型窗口时返回 429 + Retry-After，而不是 503 |
| 本地复查修复 | `d2a8ec9` | 6004 标记只在当前窗口内有效；普通 429 清掉旧 `model_cap_marks`，避免把临时限流误标成配额耗尽 |

### 第三批：面板、请求体、界面与代理地址

| 上游提交 | 本地提交 | 内容 |
| --- | --- | --- |
| `b7bc62b` | `62336d0` | 面板可调单请求体上限 1–256 MB；413 使用实际上限；chunked 超限在首块关闭 |
| `1ea7f61` | `b196fbb` | PBKDF2 不可用时 `PanelPasswordCryptoError` + 可读 500，面板显示警告 |
| `1040686` | `ea9e5e4` | 最近请求时间跨日显示 `MM-DD HH:MM` |
| `bdd8bce` | `b194d56` | 国内版缺失活跃记录显示「无本地记录」，不再显示「未领」 |
| `1d67ce9` | `6e95dfe` | API Key 选择器显示并搜索账号备注 |
| `179c005` | `7ca634d` | Key 搜索框占位改为「搜索昵称、备注或 UID」 |
| `fb1eb82` | `d8db72e` | 活动记录说明/昵称列分档截断，手机端换行 |
| `ee4fa4d` | `254d57b` | 自绘下拉只在显式 `width:100%` 时按全宽处理，并复制 `max-width` |
| `a3f584a` | `86c4f7d` | 活动表短标签列 `nowrap` |
| `28527f4`（局部） | `ac9facf` | `normalize_proxy_url()` 支持 `host:port:user:pass`、`user:pass@host:port` 等简写 |
| 本地文档 | `7004d84` | README/CHANGELOG 同步 252 套件并记录本批改动 |

## 3. 行为变化（按使用面）

- **账号与额度**：`reserve_credits > 0` 时只拦付费或目录未知模型；6004 的 `cappedAt` 现在对任何模型都保留，重启后可从 `accounts/model_windows.json` 恢复；旧窗口的 cap 标记不会污染后续普通 429。
- **鉴权与工具**：`WWW-Authenticate: ... invalid_token` 优先于 HTML 豁免，死号能被标成 `tokenExpired`；非法工具名会被丢弃并记录数量，不再把上游 400 当成网关故障。
- **失败语义**：池空时先确定 503/502 再落盘；候选只等模型窗口时返回 429 + Retry-After；`/health` 可按 realm/model 查看可用数，探针不再被其它模型的健康号掩盖。
- **请求体**：`max_payload_mb` 默认 16 MB，可调 1–256 MB；`/settings` 同时返回实际生效字节数。
- **面板**：PBKDF2 缺失时不再登录崩栈；最近时间、活动记录、Key 选择器、短标签列和自绘下拉按第三批修正。
- **代理槽**：常见简写在保存和建连前统一成标准 `http://` URL，旧数据不需要手工改写。

## 4. 未纳入的提交

以下提交经审查后暂不移植，原因记录在此，避免下次重复判断：

- **CodeArts 链**（`aaa8976`、`f5f90fa`、`80e97b2`、`e344105`、`228253d`、`0927d92`、`be1c031`）：新增第三 realm、`wb_codearts.py` / `wb_codearts_crypto.py` 与大量面板代码；本机没有 CodeArts 账号，合并面和测试成本都大。将来接入 CodeArts 时再单独评估。
- **环境变量开关**（`1bf70e5` 的 `WB_STOP_AT_SSE_DONE`、`97df705` 的 `WB_CACHE_HTTP_ERROR_BODY`）：默认关闭的调试选项，不影响现有行为；需要时再单独 cherry-pick。
- **429 绝对重置头**（`0ed5386`）：会给每个 429 出口增加 `X-RateLimit-Reset` / `reset_at`，与本地公共队列 Gate 的 429 路径重叠；等 Gate 语义稳定后单独适配。
- **已有等价实现**：`afc5d42` 的每日活跃 11140 持久化、`835da5a`/`abe6e91` 的 DSH 文件引用 11–128 清洗，本地已有同语义实现；`0fbd527` 的重复超时卡片本地本来就不存在。
- **发布提交的其余部分**：`28527f4` 只取代理地址规范化与对应测试/占位；版本号、CodeArts 上下文和发布附属改动不跟随。

## 5. 验证

2026-11-02 在 `port/upstream-2026-11` 上实测：

```text
python tests/_test_release_engineering.py   -> 7 tests, OK
python tests/run_all.py --jobs 4            -> 252 passed / 0 failed / 0 skipped, 153.5s（首次移植验收 127.9s）
python scripts/check_clean_checkout.py      -> checkout is clean
git diff --check main...HEAD                -> clean
git status --short                          -> empty
```

- 套件构成：189 个 Python + 63 个 JS；README 的计数与测试树一致。
- 专项回归包括：`_test_reserve_free_models.py`、`_test_token_expired.py`、`_test_tool_name_validation.py`、`_test_cap_mark_any_model.py`、`_test_error_status_consistency.py`、`_test_health_readiness.py`、`_test_no_usable_account_scope.py`、`_test_http_limits.py`、`_test_panel_crypto_unavailable.py`、`_test_proxy_slots.py`，以及第三批 JS 套件。
- 队列 Gate / 有界重试、hotswap、scheduler、release engineering 与 clean-checkout 守卫均在本批全量中通过。

## 6. 复查发现与修复

第一批和第二批合并后做了一次逐文件复查（13 个文件、+1512/−55），只发现一个功能缺陷：

- `model_cap_marks` 是运行时标记，但没有校验它属于哪个冷却窗口。一次 6004 留下时间戳后，同一模型的后续普通 429 可能继承旧标记，面板把临时限流显示成配额耗尽，甚至带出过期 `cappedAt`。
- 修复：`_throttle_snapshot()` 只在标记落在当前窗口内时采用；普通 429 主动清掉该模型的 cap mark。
- 回归：`tests/_test_cap_mark_any_model.py` 7/7 通过，覆盖“旧标记被忽略、当前标记正常显示”。

复查后已处理：

- README 历史区里 `#665` 的说明重复文案已删除；
- `/health` 只在总量 `accounts_ready` 保留一次 `refresh=True`，按区域和当前出口的计数改为 `refresh=False`，避免每个区域各刷新一遍 token。

## 7. 合并与发布步骤

1. 在开发分支上复核最后一次 `git status --short` 与 `git log --oneline cfaf674..HEAD`，确认移植与复查提交完整、工作树干净。
2. 推送分支并开 PR（或按团队约定直接合并）：

   ```bash
   git push origin port/upstream-2026-11
   ```

3. 合并回 `main` 后，在 `main` 上重跑完整门禁；不要拿分支上的旧结果代替合并后的结果。
4. 合并/放弃后删除临时分支：

   ```bash
   git branch -d port/upstream-2026-11
   ```

5. 版本号定为 `v1.11.0`：本批含面板新设置和 `/health` 新字段，按 minor 处理。
6. 生产部署单独做：先比较仓库与生产文件哈希、备份目标文件，再按 `docs/HOT-UPDATE.md` 热切换；本批当前未部署。
7. `tag` / GitHub Release 不在本次合并范围；发布前按 `docs/FORK-DEV.md` 8.2 在 `main` 上重跑矩阵，再推 `vX.Y.Z` 标签。

## 8. 回滚

- 分支未合并：直接删除 `port/upstream-2026-11`，`main` 不变。
- 已合并未发布：对合并提交做 `git revert -m 1 <merge-commit>`，或在 `main` 上按提交粒度回滚；不要 reset 掉其它会话的提交。
- 已发布/已部署：按发布流程回退到上一个 tag，并按 `docs/HOT-UPDATE.md` 恢复生产备份；代码回滚不等于账号数据回滚。

## 9. 维护约定

- 每批移植结束就更新本文的提交映射和门禁数字，不把结论只留在会话里。
- 新上游提交先判断“本地是否已有等价实现”，再决定 cherry-pick、手工适配或跳过；跳过要写原因。
- 公开文档只写相对路径、占位值和可复现命令，不写账号、密钥、本机绝对路径或生产数据。
- 截至 2026-11-02 的移植为 20 个功能/测试/文档提交，后续复查、版本与文档提交另行追加；以后新增提交时同步更新本页与 README/CHANGELOG 的套件计数。
