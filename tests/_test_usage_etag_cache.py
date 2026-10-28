"""面板轮询的 JSON 读接口：条件请求（ETag + 304），数字没变就不再重传。

回归背景（四段）：

1. 面板每 5 秒轮询一批读接口（/usage、/usage/perf、/usage/analytics、
   /usage/timeseries、/usage/by-account），单个响应 10~100KB。这些接口在服务端
   带 _STATS_TTL 缓存，两次重建之间响应体逐字节不变，却每次整份重传。
2. 校验符只能由「缓存条目的构建时刻 + 缓存键」派生，不能对响应字节做哈希：
   payload 里有 time.time() 派生的字段（/usage 的 started/since、
   /usage/timeseries 的桶边界），同一份缓存序列化两次都不逐字节相同，哈希出来的
   tag 永远不命中，304 一次也拿不到。这条推理钉在 [5]/[7]：缓存没重建时 tag
   必须稳定，重建后必须变化。
3. 校验符还必须**描述它跟着一起发出去的那份响应体**：取数前后各取一次缓存戳，
   两次一致才发 tag（见 wb_proxy._json_cached 的注释）。不一致说明中间发生了
   重建，这次就不发校验符、走普通 200——宁可少一次 304，也不能让客户端拿着一个
   不描述当前响应体的 tag 回来命中 304、然后一直显示过期数字。[6] 钉住这条。
4. 只改服务端等于没改：面板前端用 cache:'no-store' 发 fetch，浏览器既不存副本
   也不发 If-None-Match。两端一起改这件事，前端那一半钉在
   _test_panel_poll_revalidate.js 里。
5. 304 是「内容没变」的答复，不是免鉴权的捷径：未授权时该 401 还是 401，哪怕
   请求带着一个完全正确的 If-None-Match。[9] 就是为这条安全红线设的。

套件在空闲端口上起一个真实网关，用真 HTTP 走一遍完整周期。WB_STATS_TTL 压到
4 秒，让「缓存重建」在一次测试里就能发生，而不是等 30 分钟。

    python tests/_test_usage_etag_cache.py
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)

import _lifecycle as life            # noqa: E402  (spare port + managed process)

PASS = 0
FAIL = 0

# 每个被 ETag 覆盖的接口。
COVERED = ("/usage", "/v1/usage", "/usage/perf", "/usage/analytics",
           "/usage/timeseries", "/usage/by-account")
# 没被覆盖的接口：/usage/recent 每次新请求都进榜，服务端没有缓存、也就没有稳定
# 的校验符可发，照旧整份重取（面板那一侧同样没给它 POLL_READ）。
UNCOVERED = ("/usage/recent",)
# 4 秒 TTL：够"同一份缓存期间"连续发几个请求，也够等一次重建。
TTL = 4.0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


port = life.free_port()
work = tempfile.mkdtemp(prefix="usageetag_")
usage_dir = os.path.join(work, "usage")
os.makedirs(usage_dir)
store = os.path.join(work, "accounts")
os.makedirs(store)
log_path = os.path.join(usage_dir, "usage.jsonl")


def row(at, total_tokens=100, realm="intl", account="a1", model="m1", credit=0.5):
    """一行用量记录，字段与 record_usage() 写出来的一致。"""
    return {"at": at, "model": model, "realm": realm, "total_tokens": total_tokens,
            "prompt_tokens": 60, "completion_tokens": 30, "reasoning_tokens": 10,
            "cached_tokens": 5, "credit": credit, "error": False,
            "outcome": "completed", "account": account,
            "elapsed_ms": 900, "ttft_ms": 300, "gen_ms": 600}


def write_rows(rows):
    with open(log_path, "w", encoding="utf-8") as fh:
        for item in rows:
            fh.write(json.dumps(item) + "\n")


def append_row(item):
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")


now = time.time()
write_rows([row(now - 600 - 30 * i, total_tokens=100 + i, account="a%d" % (i % 2 + 1))
            for i in range(12)])

server_log_path = os.path.join(work, "gateway.log")
server_log = open(server_log_path, "w+b")
proc = life.spawn_managed(
    [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
     "--accounts-dir", store],
    cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT,
    env=dict(os.environ, WB_PROXY_USAGE_DIR=usage_dir, WB_STATS_TTL=str(TTL)))

TOKEN = None


def get(path, headers=None, conn=None):
    """一次 GET；返回 (status, headers, body)。conn 给定时复用它（keep-alive）。"""
    own = conn is None
    if own:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        if own:
            conn.close()


def panel_headers(extra=None):
    h = dict(extra or {})
    if TOKEN:
        h["X-Panel-Token"] = TOKEN
    return h


def poll(path, extra=None, conn=None):
    """一次面板会话下的 GET。"""
    return get(path, panel_headers(extra), conn=conn)


def wait_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        # Always sample the listener after a failed connection: a refused
        # connection while the child is still starting is not proof it died.
        if proc.poll() is not None:
            return False
        try:
            status, _, _ = get("/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def server_diagnostics():
    server_log.flush()
    with open(server_log_path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")[-8000:]
    return "pid=%s returncode=%r port=%s\n%s" % (
        proc.pid, proc.poll(), port, text)


def login():
    """面板会话：POST /panel/login（默认口令 admin）换一个 X-Panel-Token。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        body = json.dumps({"password": "admin"}).encode("utf-8")
        conn.request("POST", "/panel/login", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("token")
    finally:
        conn.close()


def etag_of(headers):
    return headers.get("ETag") or ""


def is_strong_tag(tag):
    return bool(tag) and tag.startswith('"') and tag.endswith('"') and not tag.startswith("W/")


def body_matches(body):
    """响应体是不是这个接口此刻的正常答复（用来证明 200 是真答复、不是空壳）。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and bool(payload)


def synced_tag(path, extra=None):
    """拿到该接口**当前生效**的校验符。

    冷缓存或刚重建的那一轮可能刻意不发 tag（取数前后两次缓存戳不一致），所以
    再取一次；这一轮缓存已新鲜，tag 一定发得出来。
    """
    _s, headers, _b = poll(path, extra)
    tag = etag_of(headers)
    if is_strong_tag(tag):
        return tag
    _s, headers, _b = poll(path, extra)
    return etag_of(headers)


try:
    if not wait_ready():
        print("  [FAIL] server did not start\n" + server_diagnostics())
        sys.exit(1)
    TOKEN = login()
    if not TOKEN:
        print("  [FAIL] panel login failed")
        sys.exit(1)

    print("[1] 200：带强校验符 + no-cache，body 是完整 JSON，不发 Last-Modified")
    for path in COVERED:
        status, headers, body = poll(path)
        check("%s -> 200" % path, status == 200, status)
        check("%s 的 body 是正常载荷" % path, body_matches(body), body[:80])
        check("%s 不发布 Last-Modified" % path,
              headers.get("Last-Modified") is None, headers.get("Last-Modified"))
        tag = synced_tag(path)
        check("%s 带带引号的强 ETag" % path, is_strong_tag(tag), tag)
        _s, headers2, _b = poll(path)
        check("%s Cache-Control 允许存储但要求回源校验（no-cache）" % path,
              (headers2.get("Cache-Control") or "").strip() == "no-cache",
              headers2.get("Cache-Control"))
        check("%s 的 200 带 Content-Length" % path,
              headers2.get("Content-Length") is not None,
              headers2.get("Content-Length"))

    print("[2] If-None-Match 命中：304，无 body，校验头原样重发")
    for path in COVERED:
        tag = synced_tag(path)
        status, headers2, body = poll(path, {"If-None-Match": tag})
        check("%s -> 304" % path, status == 304, status)
        check("%s 的 304 是 0 字节" % path, body == b"", "%d bytes" % len(body))
        check("%s 的 304 不带 Content-Length" % path,
              headers2.get("Content-Length") is None, headers2.get("Content-Length"))
        check("%s 的 304 重发 ETag" % path, headers2.get("ETag") == tag,
              headers2.get("ETag"))
        check("%s 的 304 重发 Cache-Control" % path,
              (headers2.get("Cache-Control") or "").strip() == "no-cache",
              headers2.get("Cache-Control"))

    print("[3] 弱比较与 *：W/<etag> 和 * 都命中（RFC 7232 §3.2）")
    for path in COVERED:
        tag = synced_tag(path)
        status, _h, _b = poll(path, {"If-None-Match": "W/" + tag})
        check("%s 对 W/<etag> 回 304" % path, status == 304, status)
        status, _h, _b = poll(path, {"If-None-Match": "*"})
        check("%s 对 * 回 304" % path, status == 304, status)

    print("[4] 畸形 / 过期 / 不匹配的 If-None-Match：200 整份，不崩")
    junk = ("not-a-tag", "", "W/", "*x", '",","', '"deadbeef"', 'W/"deadbeef", *x')
    for path in COVERED:
        for value in junk:
            status, _h, body = poll(path, {"If-None-Match": value})
            check("%s 对 %r 回 200 整份" % (path, value),
                  status == 200 and body_matches(body),
                  "%s / %d bytes" % (status, len(body)))

    print("[5] 缓存没重建：tag 稳定，同一个 tag 一直命中 304（省流量的主体）")
    for path in COVERED:
        _s, headers, first = poll(path)
        tag = etag_of(headers)
        if not is_strong_tag(tag):
            # 刚重建的那一轮可能不发 tag，重取一次再断言。
            _s, headers, first = poll(path)
            tag = etag_of(headers)
        hits = []
        for _ in range(3):
            status, _h, body = poll(path, {"If-None-Match": tag})
            hits.append((status, len(body)))
        check("%s 连续三次条件请求都是 304 且 0 字节" % path,
              all(s == 304 and n == 0 for s, n in hits), hits)
        status, headers2, again = poll(path)
        check("%s 同一份缓存期间两次 200 逐字节一致、tag 不变" % path,
              status == 200 and again == first and etag_of(headers2) == tag,
              "%s / %d vs %d bytes" % (status, len(again), len(first)))

    print("[6] 校验符必须描述它一起发出去的那份 body（重建那一轮尤其关键）")
    time.sleep(TTL + 0.4)
    for path in COVERED:
        status, headers, body = poll(path)          # 这一轮触发重建
        tag = etag_of(headers)
        check("%s 重建轮仍是 200 且 body 完整" % path,
              status == 200 and body_matches(body), status)
        if is_strong_tag(tag):
            # 发了 tag 就必须立刻命中：tag 描述的就是刚发出去的这份 body。
            s2, _h, b2 = poll(path, {"If-None-Match": tag})
            check("%s 重建轮发出的 tag 立刻命中 304" % path,
                  s2 == 304 and b2 == b"", "%s / %d bytes" % (s2, len(b2)))
        else:
            check("%s 重建轮没有发出校验符（宁可少一次 304，不发错 tag）" % path,
                  tag == "", tag)

    print("[7] 缓存重建：ETag 必变（重建后必然拿到新戳），旧 tag 不再 304")
    before = {}
    for path in COVERED:
        before[path] = synced_tag(path)
    time.sleep(TTL + 0.4)
    for path in COVERED:
        tag = synced_tag(path)                      # 已经历一次重建
        check("%s 重建后仍带 ETag" % path, is_strong_tag(tag), tag)
        check("%s 重建后 tag 与重建前不同" % path, tag != before[path],
              "%s -> %s" % (before[path], tag))
        status, _h, _b = poll(path, {"If-None-Match": before[path]})
        check("%s 上一代的 tag 不再 304" % path, status == 200, status)
        status, _h, body = poll(path, {"If-None-Match": tag})
        check("%s 当前 tag 命中 304" % path, status == 304 and body == b"",
              "%s / %d bytes" % (status, len(body)))

    print("[8] 数据变了：旧 tag 绝不能 304（把过期内容当成最新的就完了）")
    before, before_body = {}, {}
    for path in COVERED:
        _s, headers, body = poll(path)
        before[path] = etag_of(headers)
        before_body[path] = body
        if not is_strong_tag(before[path]):
            _s, headers, body = poll(path)
            before[path] = etag_of(headers)
            before_body[path] = body
    # 追加一行 = 一个新请求落进日志（生产里就是这么变的）。
    append_row(row(time.time() - 5, total_tokens=999999, account="a9", model="m9"))
    time.sleep(TTL + 0.4)
    for path in COVERED:
        status, headers, body = poll(path, {"If-None-Match": before[path]})
        check("%s 数据变化后旧 tag 回 200（不是 304）" % path, status == 200, status)
        check("%s 的响应体确实变了（新数据进来了）" % path, body != before_body[path],
              "%d bytes" % len(body))
        tag = synced_tag(path)
        check("%s 同时给出新 tag" % path,
              is_strong_tag(tag) and tag != before[path],
              "%s -> %s" % (before[path], tag))
    # 新行确实进了载荷（证明上面那次 200 不是白发的）
    _s, _h, body = poll("/usage")
    check("/usage 里能看到新写入的那一行", b"999999" in body, body[:120])

    print("[9] 鉴权红线：没有面板会话时，304 路径照样 401")
    for path in COVERED:
        tag = synced_tag(path)
        status, _h, body = get(path, {"If-None-Match": tag})   # 无 token
        check("%s 无会话 + 正确 tag -> 401（不是 304）" % path, status == 401, status)
        check("%s 的 401 是 JSON 错误体" % path,
              b"panel password required" in body, body[:120])
        status, _h, _b = get(path, {"If-None-Match": tag,
                                    "X-Panel-Token": "not-a-session"})
        check("%s 假 token + 正确 tag -> 401" % path, status == 401, status)
        status, _h, _b = get(path, {"If-None-Match": "*"})
        check("%s 无会话 + * -> 401" % path, status == 401, status)

    print("[10] 未覆盖的接口：不发校验符，也不发可缓存的 Cache-Control")
    for path in UNCOVERED:
        status, headers, body = poll(path)
        check("%s -> 200" % path, status == 200, status)
        check("%s 不带 ETag" % path, headers.get("ETag") is None, headers.get("ETag"))
        check("%s 不带 no-cache" % path, headers.get("Cache-Control") is None,
              headers.get("Cache-Control"))

    print("[11] 不同查询参数是不同的表示：tag 必须跟着键走")
    tag_intl = synced_tag("/usage?realm=intl")
    tag_cn = synced_tag("/usage?realm=cn")
    check("realm=intl 与 realm=cn 的 tag 不同", tag_intl != tag_cn,
          "%s vs %s" % (tag_intl, tag_cn))
    status, _h, _b = poll("/usage?realm=cn", {"If-None-Match": tag_intl})
    check("拿 intl 的 tag 请求 realm=cn -> 200", status == 200, status)
    status, _h, _b = poll("/usage?realm=cn", {"If-None-Match": tag_cn})
    check("拿 cn 自己的 tag 请求 realm=cn -> 304", status == 304, status)
    # 带引号的 realm 不能把 ETag 头拼成畸形值（缓存键里有调用方给的字符串）
    tag_weird = synced_tag('/usage?realm=a%22b')
    check("带引号的 realm 仍发出合法的 ETag", is_strong_tag(tag_weird), tag_weird)

    print("[12] keep-alive：一条连接上，校验符不会漏给下一个响应")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        status, headers, _b = poll("/usage", conn=conn)
        check("连接内第一个请求（/usage）带 ETag", is_strong_tag(etag_of(headers)),
              etag_of(headers))
        status, headers, body = poll("/usage/recent", conn=conn)
        check("同一连接上下一个请求（/usage/recent）不带 ETag",
              status == 200 and headers.get("ETag") is None, headers.get("ETag"))
        status, headers, _b = poll("/usage", conn=conn)
        check("再回到 /usage 仍带 ETag", is_strong_tag(etag_of(headers)),
              etag_of(headers))
    finally:
        conn.close()

finally:
    life.stop_managed(proc)
    server_log.close()
    shutil.rmtree(work, ignore_errors=True)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
