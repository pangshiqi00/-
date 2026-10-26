#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy AI（国际版 www.workbuddy.ai）每日签到 / 活跃 —— 零第三方依赖。

国际版的「每日奖励」不是单纯签到，而是一条活跃任务链：
  1. 探测   POST /billing/meter/checkin-activity-status
            → data.active（活动是否开放）/ data.today_checked_in（今日是否已领）
  2. 领取   POST /v2/billing/meter/daily-checkin（仅当活动开放且今日未领）
  3. 活跃   POST /v2/report（chat_request_send 事件）→ 点亮成长中心「连登」
            GET  /activity/growth/streak 回读连登天数
            （可选 --keepalive：再发一次最小对话 + 建一个网页会话，上游「当日活跃」
              计数只认网页会话那一路，默认关闭以省调用）

口径：
  · 活动未开放属于正常状态，不算失败；
  · 签到成功 ≠ 活跃成功，分开报告；活跃失败不会把签到判成失败；
  · 命中账号级风控（11140 / request illegal）立即停止该账号本轮，不刷新、不重试。

环境变量：
  WORKBUDDY_INTL_TOKENS  多账号，换行或 & 分隔，每条：
                         `token` / `备注|token` / `备注|token|uid` / `备注|token|uid|refresh_token`
  WORKBUDDY_TOKEN        单账号兜底
  WORKBUDDY_INTL_MODELS  可选，保活模型链，逗号分隔
  BARK_URL               可选，Bark 推送（与仓库内其他签到一致）
  NO_NOTIFY=1            关闭推送

用法：
  python intl_checkin.py                 # 探测 + 领取 + 活跃上报 + 积分
  python intl_checkin.py --keepalive     # 额外发保活对话与网页会话
  python intl_checkin.py --only 1        # 只跑第一个账号
  python intl_checkin.py --debug         # 打印请求细节

国际版令牌获取：python intl_login.py（设备授权，Google / GitHub / X 登录，免手机号）
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://www.workbuddy.ai"

STATUS_PATH = "/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
CREDITS_PATH = "/v2/billing/meter/get-user-resource"
REPORT_PATH = "/v2/report"
STREAK_PATH = "/activity/growth/streak"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHAT_PATH = "/v2/chat/completions"
WEB_CONV_PATH = "/console/as/conversations/"

CLIENT_VERSION = "5.5.2"
CLI_VERSION = "2.137.1"
UA = "WorkBuddy/%s WorkBuddy AI/%s CLI/%s" % (CLIENT_VERSION, CLIENT_VERSION, CLI_VERSION)
CHAT_UA = "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
DEFAULT_MODELS = ("deepseek-v4.1-flash", "hy3", "hy4-preview", "hy4-preview-f")

# 提前 1 小时判定令牌即将过期
REFRESH_MARGIN = 3600

DEBUG = False
TIMEOUT = 30


def log(msg):
    print(msg, flush=True)


def dbg(msg):
    if DEBUG:
        print("    [debug] " + str(msg), flush=True)


def mask(text, keep=6):
    text = str(text or "")
    return text if len(text) <= keep + 4 else text[:keep] + "****" + text[-4:]


# ---------------------------------------------------------------------------
# HTTP（标准库）
# ---------------------------------------------------------------------------
def http(method, url, headers, body=None, timeout=TIMEOUT, raw=False):
    """返回 (status_code, payload)。payload 尽量解析为 dict，失败时给 {"_raw": ...}。"""
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Content-Length", str(len(data)))

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)

    dbg("%s %s" % (method, url))
    if body is not None:
        dbg("  body: %s" % json.dumps(body, ensure_ascii=False)[:300])

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    except urllib.error.URLError as e:
        raise RuntimeError("网络请求失败: %s" % e.reason)

    dbg("  status: %s" % status)
    dbg("  resp:  %s" % text[:400])
    if raw:
        return status, {"_raw": text}
    try:
        return status, (json.loads(text) if text else {})
    except json.JSONDecodeError:
        return status, {"_raw": text}


def first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def jwt_payload(token):
    """解出 JWT 载荷；失败返回 {}。"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def uid_of(token):
    return str(jwt_payload(token).get("sub") or "")


def expires_at(token):
    try:
        return float(jwt_payload(token).get("exp") or 0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------
def api_headers(token, uid, accept=None):
    headers = {
        "Accept": accept or "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token,
        "User-Agent": UA,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy AI",
        "X-IDE-Version": CLIENT_VERSION,
        "X-Product": "WorkBuddy AI",
        "X-Domain": "www.workbuddy.ai",
    }
    if uid:
        headers["X-User-Id"] = uid
    return headers


def chat_headers(token, uid):
    """对话链路头族（对齐国际版客户端 5.8.4，渠道校验按这组头识别客户端）。"""
    conversation_id = str(uuid.uuid4())
    request_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": CHAT_UA,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.8.4",
        "X-Product": "SaaS",
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "X-Agent-Type": "main",
        "X-Private-Data": "true",
        "X-Conversation-ID": conversation_id,
        "X-Conversation-Request-ID": request_id,
        "X-Root-Request-ID": request_id,
        "X-Conversation-Message-ID": message_id,
        "X-Request-ID": message_id,
        "X-Trace-ID": trace_id,
        "traceparent": "00-%s-%s-01" % (trace_id, span_id),
        "b3": "%s-%s-1" % (trace_id, span_id),
        "X-B3-TraceId": trace_id,
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
        "Accept-Language": "en-US",
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "Origin": BASE,
        "Referer": BASE + "/",
        "X-Domain": "www.workbuddy.ai",
        "Authorization": "Bearer %s" % token,
    }
    if uid:
        headers["X-User-Id"] = uid
    return headers


def web_headers(token, uid):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": WEB_UA,
        "Origin": BASE,
        "Referer": BASE + "/app",
        "Authorization": "Bearer %s" % token,
        "X-User-Id": uid,
    }


# ---------------------------------------------------------------------------
# 风控与结果文案
# ---------------------------------------------------------------------------
class AccountRestricted(RuntimeError):
    pass


def is_account_restricted(status_code, payload):
    """国际版账号级风控：11140 / request illegal。"""
    code = payload.get("code") if isinstance(payload, dict) else None
    blob = (json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, (dict, list)) else str(payload or "")).lower()
    if str(code) == "11140":
        return True
    markers = ("11140", "account restricted", "request illegal",
               "账号受限", "账号被限制", "账户受限")
    return int(status_code or 0) == 403 and any(m in blob for m in markers)


CLAIM_LABELS = {
    41000: "活动未开始或已结束",
    40901: "任务暂不可领取",
    401: "登录状态失效，请重新登录",
    40100: "登录状态失效，请重新登录",
    11140: "账号受限，请检查账号状态",
}


def claim_label(code, msg):
    return CLAIM_LABELS.get(code) or msg or ("code=%s" % code)


# ---------------------------------------------------------------------------
# 业务动作
# ---------------------------------------------------------------------------
def refresh_token(rt):
    """用 refreshToken 换新令牌；返回 (新AT, 新RT) 或抛 RuntimeError。"""
    if not rt:
        raise RuntimeError("未提供 refresh token")
    status, payload = http("POST", BASE + REFRESH_PATH,
                           {"X-Refresh-Token": rt,
                            "X-Auth-Refresh-Source": "plugin",
                            "X-Domain": "www.workbuddy.ai",
                            "Accept": "application/json",
                            "Content-Type": "application/json"}, body={})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    token = data.get("accessToken") or data.get("access_token")
    if status == 200 and token:
        return token, (data.get("refreshToken") or data.get("refresh_token") or rt)
    raise RuntimeError(first_text(payload.get("msg"), payload.get("message")) or ("HTTP %s" % status))


def preflight(token, uid):
    """只读探测：顺带识别账号级风控。返回 (status_code, payload)。"""
    status, payload = http("POST", BASE + STATUS_PATH, api_headers(token, uid), body={})
    if is_account_restricted(status, payload):
        raise AccountRestricted("账号受限（HTTP %s / 11140）" % status)
    return status, payload


def auth_failed(status, payload):
    """令牌无效 / 未登录（与「活动未开放」区分开）。"""
    code = payload.get("code") if isinstance(payload, dict) else None
    return status in (401, 403) or str(code) in ("401", "40100")


def detect_activity(payload, status):
    """返回 (available, active, checked, note)。"""
    code = payload.get("code")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    available = status == 200 and code == 0
    active = bool(data.get("active"))
    checked = bool(data.get("today_checked_in", data.get("todayCheckedIn")))
    if not available:
        note = "活动未开放（%s）" % claim_label(
            code, first_text(payload.get("msg"), payload.get("message")) or ("HTTP %s" % status))
    elif checked:
        note = "今日已领取"
    elif active:
        note = "活动开放，今日未领"
    else:
        note = "活动已关闭"
    return available, active, checked, note


def claim_daily(token, uid):
    status, payload = http("POST", BASE + CLAIM_PATH, api_headers(token, uid), body={})
    if is_account_restricted(status, payload):
        raise AccountRestricted("账号受限（HTTP %s / 11140）" % status)
    code = payload.get("code")
    message = first_text(payload.get("msg"), payload.get("message"))
    if status == 200 and code == 0:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        gained = data.get("credits", data.get("reward", data.get("amount")))
        return True, ("领取成功 +%s" % gained) if gained else "领取成功"
    if status in (400, 409) and any(k in message.lower() for k in ("已签", "已领", "already")):
        return True, "今日已领取"
    return False, claim_label(code, message) or ("HTTP %s" % status)


def report_event(uid):
    now_ms = int(time.time() * 1000)
    cid = "wbintl-" + uuid.uuid4().hex
    return {
        "eventCode": "chat_request_send", "timestamp": now_ms, "reportDelay": 0,
        "mode": "craft", "conversationId": cid, "requestId": cid,
        "inputLength": 12, "requestModelId": "deepseek-v4.1-flash",
        "requestModelName": "DeepSeek V4.1 Flash", "isPlan": False,
        "isAutoExecuteTerminal": False, "isAutoModify": False, "codebaseEnable": False,
        "maxToken": 0, "maxSteps": 0, "temperature": 0, "maxRetries": 0,
        "mentionContexts": [], "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
        "mentionContextCount": 0, "command": "", "expertId": "", "recommendId": "",
        "skillId": "", "skillCount": 0, "totalCount": 0, "fileUri": "",
        "presentAt": now_ms, "traceId": "", "rootRequestId": cid,
        "parentConversationId": cid, "agentName": "default",
        "agentType": "conversation", "userId": uid,
    }


def report_activity(token, uid):
    """发一条活跃上报并回读连登天数；返回 (ok, note)。"""
    if not uid:
        return False, "缺少 uid，无法上报活跃"
    status, payload = http("POST", BASE + REPORT_PATH, api_headers(token, uid),
                           body=[report_event(uid)])
    if is_account_restricted(status, payload):
        raise AccountRestricted("账号受限（HTTP %s / 11140）" % status)
    if status != 200 or payload.get("code") not in (0, None):
        return False, "HTTP %s code=%s" % (status, payload.get("code"))
    days = None
    try:
        _, streak = http("GET", BASE + STREAK_PATH, api_headers(token, uid))
        value = ((streak.get("data") or {}).get("streak") or {}).get("days")
        if isinstance(value, (int, float)):
            days = int(value)
    except Exception:
        days = None
    return True, ("活跃上报成功，连登 %d 天" % days) if days is not None else "活跃上报成功"


def model_chain():
    models = [m.strip() for m in (os.environ.get("WORKBUDDY_INTL_MODELS") or "").split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def keepalive(token, uid):
    """发一次最小流式对话，维持「账号有调用」；逐个免费模型尝试。"""
    last = ""
    for model in model_chain():
        body = {"model": model, "stream": True, "max_tokens": 16,
                "messages": [{"role": "system", "content": "You are a helpful assistant."},
                             {"role": "user", "content": "hi"}]}
        try:
            status, payload = http("POST", BASE + CHAT_PATH, chat_headers(token, uid),
                                   body=body, timeout=60, raw=True)
            if status == 200:
                return True, model
            detail = str(payload.get("_raw") or "")
            if is_account_restricted(status, detail):
                raise AccountRestricted("账号受限（HTTP %s / 11140）" % status)
            last = "HTTP %s" % status + ("（模型无权限，非账号风控）" if status == 403 else "")
        except AccountRestricted:
            raise
        except Exception as e:
            last = str(e)
    return False, last or "未知错误"


def web_conversation(token, uid):
    """网页通道建一个带 prompt 的会话（上游「当日活跃」计数只认这一发）。"""
    if not uid:
        return False, "缺少 uid，无法建网页会话"
    last = ""
    for model in model_chain():
        body = {"prompt": "Hi", "model": model,
                "conversationOrigin": "workbuddy-app"}
        try:
            status, payload = http("POST", BASE + WEB_CONV_PATH, web_headers(token, uid),
                                   body=body, timeout=45)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if status == 200 and payload.get("code") in (0, None) and data.get("id"):
                return True, "网页会话已创建"
            last = "HTTP %s code=%s" % (status, payload.get("code"))
        except Exception as e:
            last = str(e)
    return False, last or "网页会话创建失败"


def query_credits(token, uid):
    """尽力查询余额；失败不影响结论。"""
    try:
        status, payload = http("POST", BASE + CREDITS_PATH, api_headers(token, uid), body={})
    except Exception as e:
        return "查询积分失败: %s" % e
    if status != 200:
        return "查询积分失败: HTTP %s" % status
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    resp = data.get("Response") if isinstance(data.get("Response"), dict) else {}
    resp_data = resp.get("Data") if isinstance(resp.get("Data"), dict) else {}
    for value in (resp_data.get("TotalDosage"), data.get("credits"),
                  data.get("totalCredits"), data.get("balance"), data.get("TotalCount")):
        if value is not None:
            return "当前积分: %s" % value
    return "当前积分: %s" % json.dumps(data, ensure_ascii=False)[:100]


# ---------------------------------------------------------------------------
# 账号解析与推送
# ---------------------------------------------------------------------------
def parse_accounts(raw):
    accounts = []
    for index, line in enumerate((raw or "").replace("&", "\n").splitlines(), 1):
        line = line.strip().strip("\"'")
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        note, token, uid, rt = "账号%d" % index, "", "", ""
        if len(parts) == 1:
            token = parts[0]
        elif len(parts) == 2:
            note, token = parts
        elif len(parts) == 3:
            note, token, uid = parts
        else:
            note, token, uid, rt = parts[0], parts[1], parts[2], parts[3]
        if not token:
            continue
        accounts.append({"note": note, "token": token,
                         "uid": uid or uid_of(token), "refresh_token": rt})
    return accounts


def notify(title, body):
    url = (os.environ.get("BARK_URL") or "").strip()
    if not url or os.environ.get("NO_NOTIFY", "").strip() == "1":
        return
    try:
        target = "%s/%s/%s" % (url.rstrip("/"),
                               urllib.parse.quote(title), urllib.parse.quote(body))
        http("GET", target, {"Accept": "application/json"}, timeout=15, raw=True)
    except Exception as e:
        log("⚠️ Bark 推送失败: %s" % e)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_account(account, keepalive_enabled):
    token, uid = account["token"], account["uid"]
    lines = []

    # 令牌临期且有 RT → 先续期（续期前先做只读预检，避免在受限账号上刷新）
    exp = expires_at(token)
    if account["refresh_token"] and exp and exp - time.time() < REFRESH_MARGIN:
        try:
            preflight(token, uid)
        except AccountRestricted as e:
            raise
        try:
            new_at, new_rt = refresh_token(account["refresh_token"])
            token = account["token"] = new_at
            account["refresh_token"] = new_rt
            uid = account["uid"] = uid or uid_of(new_at)
            log("   🔑 令牌已续期（%s）" % mask(new_at))
        except RuntimeError as e:
            log("   ⚠️ 令牌续期失败: %s（继续用现有令牌）" % e)

    status, payload = preflight(token, uid)
    available, active, checked, note = detect_activity(payload, status)
    log("   探测: %s" % note)

    # 401 / 令牌失效 → 用 RT 换一次再重试
    if auth_failed(status, payload) and account["refresh_token"]:
        try:
            token, rt = refresh_token(account["refresh_token"])
            account["token"], account["refresh_token"] = token, rt
            uid = account["uid"] = uid or uid_of(token)
            status, payload = preflight(token, uid)
            available, active, checked, note = detect_activity(payload, status)
            log("   🔁 续期后重试: %s" % note)
        except (RuntimeError, AccountRestricted) as e:
            log("   ⚠️ 续期后重试失败: %s" % e)

    # 令牌确实无效时，后面的写操作没有意义（/v2/report 不校验令牌，会假成功）
    if auth_failed(status, payload):
        lines.append("%s：❌ 令牌失效，请重新运行 intl_login.py 取新令牌" % account["note"])
        log("   ❌ 令牌失效，跳过领取与活跃上报")
        return lines, account

    claim_ok = False
    if available and active and not checked:
        claim_ok, message = claim_daily(token, uid)
        log("   %s 领取: %s" % ("✅" if claim_ok else "❌", message))
    else:
        message = note

    report_ok, report_note = report_activity(token, uid)
    log("   %s 活跃上报: %s" % ("✅" if report_ok else "⚠️", report_note))

    keep_ok = web_ok = False
    keep_note = web_note = "已跳过（加 --keepalive 开启）"
    if keepalive_enabled:
        keep_ok, keep_note = keepalive(token, uid)
        log("   %s 保活: %s" % ("✅" if keep_ok else "⚠️", keep_note))
        web_ok, web_note = web_conversation(token, uid)
        log("   %s 网页会话: %s" % ("✅" if web_ok else "⚠️", web_note))

    # 活动未开启但活跃链路已完成 → 改口径（仅当令牌有效、活动确实未开放时）
    if not claim_ok and (report_ok or web_ok) and not active:
        reasons = [r for ok, r in ((report_ok, "连登已点亮"), (web_ok, "网页会话已建")) if ok]
        message = "活动未开启，但今日活跃已完成（%s；积分由上游异步发放）" % "、".join(reasons)

    credits = query_credits(token, uid)
    log("   💰 %s" % credits)

    lines.append("%s：%s | 活跃上报%s | %s" % (
        account["note"], message,
        ("成功（%s）" % report_note) if report_ok else ("未成功（%s）" % report_note),
        credits))
    return lines, account


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="WorkBuddy AI 国际版每日签到/活跃")
    ap.add_argument("--keepalive", action="store_true", help="额外发保活对话与网页会话")
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 个账号")
    ap.add_argument("--debug", action="store_true", help="打印请求细节")
    args = ap.parse_args()
    DEBUG = args.debug

    raw = (os.environ.get("WORKBUDDY_INTL_TOKENS") or "").strip() \
        or (os.environ.get("WORKBUDDY_TOKEN") or "").strip()
    accounts = parse_accounts(raw)
    if not accounts:
        log("❌ 未配置 WORKBUDDY_INTL_TOKENS / WORKBUDDY_TOKEN")
        log("   国际版登录态与国内版不通用，请用 intl_login.py 从 www.workbuddy.ai 获取")
        return 1

    log("🤖 WorkBuddy AI 国际版每日签到 | 账号数 %d" % len(accounts))
    results, failed = [], 0
    for index, account in enumerate(accounts, 1):
        if args.only and index != args.only:
            continue
        log("👤 [%d] %s  token=%s  uid=%s"
            % (index, account["note"], mask(account["token"]), account["uid"] or "?"))
        try:
            lines, _ = run_account(account, args.keepalive)
            results.extend(lines)
        except AccountRestricted as e:
            failed += 1
            log("   🚫 %s：本轮停止，不刷新、不重试" % e)
            results.append("%s：🚫 %s" % (account["note"], e))
        except Exception as e:
            failed += 1
            log("   ❌ 异常: %s" % e)
            results.append("%s：❌ %s" % (account["note"], e))
        time.sleep(2)

    summary = "WorkBuddy 国际版签到 %s\n%s\n失败 %d" % (
        time.strftime("%m-%d %H:%M"), "\n".join(results), failed)
    log("\n" + summary)
    notify("WorkBuddy 国际版签到", "\n".join(results) or "无结果")
    return 0 if failed < len(accounts) else 1


if __name__ == "__main__":
    sys.exit(main())
