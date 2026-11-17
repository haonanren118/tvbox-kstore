import sys
import os
import json
import hashlib
import time
import concurrent.futures
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, quote
import traceback

LOG_FILE = "generate.log"

def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    try:
        print(msg)
    except Exception:
        pass

log("=== generate.py START ===")
log("Timestamp: " + time.strftime("%Y-%m-%d %H:%M:%S"))
log("Python {}.{}".format(sys.version_info.major, sys.version_info.minor))

# ============================================================
NOTICE_TEXT = (
    "阁下好手段！本资源均来源于网络公开收集整理，仅供个人学习交流使用，"
    "严禁私自售卖、二次倒卖及商用，下载后请 24 小时内自行删除，"
    "使用产生一切后果均由使用者自行承担，与本人无关，特此警示！"
    "如有冒犯，请联系删除。"
)

CANDIDATE_SOURCES = [
    {"url": "https://tv.菜妮丝.top",                     "name": "杰翔"},
    {"url": "http://tvbox.王二小放牛娃.top",              "name": "王二小"},
    {"url": "http://feimao.pro",                          "name": "肥猫2"},
    {"url": "https://6296.kstore.vip/fm.gif",             "name": "肥猫3"},
    {"url": "https://盒子迷.top/禁止贩卖",                 "name": "盒子迷"},
    {"url": "https://gh-proxy.com/https://raw.githubusercontent.com/guot55/yg/main/pg/bh.json", "name": "寳盒"},
    {"url": "http://www.饭太硬.cc/tv",                    "name": "饭太硬1"},
    {"url": "http://www.饭太硬.net/tv",                   "name": "饭太硬2"},
    {"url": "http://www.饭太硬.art/tv",                   "name": "饭太硬3"},
    {"url": "http://fty.xxooo.cf/tv",                     "name": "饭太硬4"},
    {"url": "http://fty.888484.xyz/tv",                   "name": "饭太硬5"},
    {"url": "http://fty.333232.xyz/tv",                   "name": "饭太硬6"},
    {"url": "https://raw.atomgit.com/xxxooo/fan/blobs/cef5f441c422cffe4852e0fc8b102f9be6d2bb2b/in.bmp", "name": "饭太硬江苏郑州"},
]

TIMEOUT = 8
MAX_WORKERS = 10
CUSTOM_SOURCES_FILE = "custom_sources.json"
# 国内测速 API：pingtest.cn 免密钥，返回 result.ttfbMs（首字节毫秒）
CHINA_SPEED_API = "https://www.pingtest.cn/api/http/"  # 国内测速 API
# 有效配置必须包含的字段之一（TVBox 配置特征）
VALID_CONFIG_KEYS = ("sites", "lives", "urls")
# ============================================================


def check_speed_china(url):
    """通过 pingtest.cn 检测 URL 的国内访问延迟（首字节时间），返回毫秒数，失败返回 None"""
    try:
        ascii_url = url_to_ascii(url)
        api_url = CHINA_SPEED_API + "?url=" + quote(ascii_url, safe="")
        req = Request(api_url, method="GET")
        req.add_header("User-Agent", "okhttp/3.14.9")
        resp = urlopen(req, timeout=TIMEOUT * 2)
        data = json.loads(resp.read().decode("utf-8"))
        resp.close()
        # pingtest.cn: {"ok": true, "result": {"ttfbMs": 158.3, ...}}
        if data.get("ok") and isinstance(data.get("result"), dict):
            ttfb = data["result"].get("ttfbMs")
            if ttfb is not None:
                return int(float(ttfb))
    except Exception as e:
        log("  [CHINA_API_ERR] {} -> {}".format(url[:50], str(e)))
    return None


def check_all_china_speeds(sources):
    """阶段1：通过国内 API 批量检测所有线路的国内延迟"""
    log("Phase 1: China speed test for {} sources...".format(len(sources)))
    speed_map = {}

    for item in sources:
        name = item["name"]
        url = item["url"]
        ms = check_speed_china(url)
        if ms is not None:
            speed_map[name] = ms
            log("  [CHINA] {} -> {}ms".format(name, ms))
        else:
            log("  [CHINA] {} -> N/A (API failed, will use fallback)".format(name))

    return speed_map


def load_custom_sources():
    """从 custom_sources.json 读取用户自定义接口"""
    if not os.path.exists(CUSTOM_SOURCES_FILE):
        log("Custom sources file not found: {}".format(CUSTOM_SOURCES_FILE))
        return []
    try:
        with open(CUSTOM_SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log("  [WARN] custom_sources.json is not an array, skipping")
            return []
        sources = []
        for item in data:
            if isinstance(item, dict) and "url" in item and "name" in item:
                sources.append({"url": item["url"], "name": item["name"]})
            else:
                log("  [WARN] skipping invalid custom source: {}".format(repr(item)))
        log("Loaded {} custom sources from {}".format(len(sources), CUSTOM_SOURCES_FILE))
        return sources
    except Exception as e:
        log("  [WARN] failed to load {}: {}".format(CUSTOM_SOURCES_FILE, str(e)))
        return []


def url_to_ascii(url):
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return url
        ascii_host = parsed.hostname.encode("idna").decode("ascii")
        port_str = ":" + str(parsed.port) if parsed.port else ""
        path = quote(parsed.path or "", safe="/")
        query = "?" + quote(parsed.query, safe="&=") if parsed.query else ""
        return "{}://{}{}{}{}".format(parsed.scheme, ascii_host, port_str, path, query)
    except Exception as e:
        log("  [WARN] url_to_ascii failed for {}: {}".format(repr(url), str(e)))
        return url


def config_content_hash(data):
    """校验响应体是否为有效 TVBox 配置，有效则返回内容 MD5，否则返回 None。

    有效标准：能 json.loads 为 dict，且含 sites/lives/urls 之一。
    返回内容哈希用于后续同源镜像去重。
    """
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
        if not isinstance(obj, dict):
            return None
        if not any(k in obj for k in VALID_CONFIG_KEYS):
            return None
        return hashlib.md5(data).hexdigest()
    except Exception:
        return None


def check_url_alive(item):
    """HTTP 内容校验：响应必须是有效 TVBox 配置（JSON + 关键字段）才算存活。

    同时返回响应内容哈希，用于同源镜像去重。
    """
    url = item["url"]
    name = item["name"]
    ascii_url = url_to_ascii(url)
    t0 = time.time()

    try:
        get_req = Request(ascii_url, method="GET")
        # 用 TVBox 客户端真实 UA：部分国内源（如杰翔/王二小）对浏览器 UA 返回防抓页，
        # 仅对 okhttp UA 返回有效配置 JSON。用浏览器 UA 会误杀可用源。
        get_req.add_header("User-Agent", "okhttp/3.14.9")
        resp = urlopen(get_req, timeout=TIMEOUT)
        status = resp.status
        data = resp.read()
        resp.close()

        ms = int((time.time() - t0) * 1000)
        content_hash = config_content_hash(data)

        if content_hash is not None:
            log("  [OK] [{}] {} -> {} ({}ms, {}bytes, hash={})".format(
                status, name, repr(url), ms, len(data), content_hash[:8]))
            return item, True, str(status), ms, content_hash
        else:
            # 返回 200 但内容为 HTML/图片/广告/空 —— 伪源，判死
            head = data[:60].decode("utf-8", "replace").replace("\n", " ").replace("\r", " ")
            log("  [INVALID] [{}] {} -> {} ({}ms, {}bytes, not valid config: {})".format(
                status, name, repr(url), ms, len(data), repr(head)))
            return item, False, "invalid_content", ms, None
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        err = type(e).__name__
        log("  [FAIL] [{}] {} -> {} ({}ms)".format(err, name, repr(url), ms))
        return item, False, err, ms, None


def check_all_sources(sources, china_speed_map=None):
    """检测所有线路的内容有效性。

    策略（修复后）：
    - 所有线路（含国内测速成功的）都必须经过内容校验，伪源（HTML/图片/广告）一律判死
    - 国内测速结果仅用于排序加权，不再直接决定存活
    - 存活线路按响应内容 MD5 去重，同源镜像只保留延迟最低者
    """
    log("Phase 2: content validation for {} sources...".format(len(sources)))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {}
        for i, item in enumerate(sources):
            future_map[executor.submit(check_url_alive, item)] = i
        done, not_done = concurrent.futures.wait(
            future_map.keys(), timeout=TIMEOUT * 3,
            return_when=concurrent.futures.ALL_COMPLETED)
        for future in done:
            try:
                results[future_map[future]] = future.result(timeout=1)
            except concurrent.futures.TimeoutError:
                i = future_map[future]
                log("  [RESULT_TIMEOUT] {} -> getting result timed out".format(sources[i]["name"]))
                results[i] = (sources[i], False, "ResultTimeout", 99999, None)
            except Exception as e:
                i = future_map[future]
                log("  [FUTURE_ERR] {} -> {}".format(sources[i]["name"], str(e)))
                results[i] = (sources[i], False, "FutureError", 99999, None)
        for future in not_done:
            i = future_map[future]
            log("  [TIMEOUT] {} -> {}".format(sources[i]["name"], repr(sources[i]["url"])))
            results[i] = (sources[i], False, "Timeout", 99999, None)

    # 合并结果：优先用国内测速延迟排序，否则用本地响应耗时
    alive_with_hash = []
    dead = []
    dropped = []
    for i in sorted(results.keys()):
        item, ok, status, ms, content_hash = results[i]
        if ok:
            china_ms = china_speed_map.get(item["name"]) if china_speed_map else None
            rank_ms = china_ms if china_ms is not None else ms
            alive_with_hash.append((item, rank_ms, content_hash, china_ms))
        elif status == "invalid_content":
            # 内容校验失败 = 确定的伪源（HTML/图片/广告），彻底剔除，不进 backup
            dropped.append(item["name"])
        else:
            # 超时/HTTP错误 = 临时故障，保留在 backup 供下次重建尝试
            dead.append((item, status))
    if dropped:
        log("Dropped {} invalid-content sources entirely: {}".format(
            len(dropped), ", ".join(dropped)))

    # 按内容哈希去重：同源镜像只保留延迟最低者
    best_by_hash = {}
    for entry in alive_with_hash:
        h = entry[2]
        if h not in best_by_hash or entry[1] < best_by_hash[h][1]:
            best_by_hash[h] = entry

    alive = sorted(best_by_hash.values(), key=lambda x: x[1])
    log("Result: {} alive (after content-hash dedup), {} backup".format(len(alive), len(dead)))
    for rank, (item, rank_ms, h, china_ms) in enumerate(alive, 1):
        label = "{}ms(CN)".format(china_ms) if china_ms is not None else "{}ms(local)".format(rank_ms)
        log("  #{} {} -> {} [hash={}]".format(rank, item["name"], label, h[:8]))
    return [x[0] for x in alive], dead


def generate_json(alive_sources, dead_sources):
    config = {
        "notice": NOTICE_TEXT,
        "urls": alive_sources,
    }
    if dead_sources:
        config["backup_urls"] = [x[0] for x in dead_sources]
    out_dir = "download/1/tvbox"
    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.join(out_dir, "source.json")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    log("Generated: {} ({} alive, {} backup)".format(fname, len(alive_sources), len(dead_sources)))
    return fname


if __name__ == "__main__":
    t0 = time.time()
    try:
        custom_sources = load_custom_sources()
        all_sources = CANDIDATE_SOURCES + custom_sources
        log("Total sources: {} (built-in: {}, custom: {})".format(
            len(all_sources), len(CANDIDATE_SOURCES), len(custom_sources)))

        # 阶段1：国内节点测速
        china_speed_map = check_all_china_speeds(all_sources)
        log("China speed test done: {}/{} sources got CN latency".format(
            len(china_speed_map), len(all_sources)))

        # 阶段2：HTTP 连通性检测 + 排序
        alive_sources, dead_sources = check_all_sources(all_sources, china_speed_map)
        fname = generate_json(alive_sources, dead_sources)
        with open(fname, "r", encoding="utf-8") as f:
            data = json.load(f)
        log("JSON verified OK: {} entries, notice {} chars".format(
            len(data["urls"]), len(data.get("notice", ""))))
    except Exception as e:
        log("FATAL ERROR: {}".format(e))
        log(traceback.format_exc())
    elapsed = time.time() - t0
    log("Done in {:.1f}s".format(elapsed))
    log("=== END ===")
