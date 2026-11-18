#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 国内版（www.workbuddy.cn）设备授权登录 —— 零第三方依赖。

注意：workbuddy.cn 与 codebuddy.cn 登录态独立、令牌互不通用，但积分与签到状态
实测是同一份后端数据，签一边另一边即生效，所以自动签到只签 workbuddy.cn。

与国际版 intl_login.py 是同一套 /v2/plugin/auth/ 协议，只是域名不同。
国内版账号用手机号/微信注册，浏览器里正常登录即可，不需要抓包。

  1. POST /v2/plugin/auth/state?platform=workbuddy  → {state, authUrl}
  2. 浏览器打开 authUrl，登录并授权
  3. 轮询 GET /v2/plugin/auth/token?state=…         → accessToken + refreshToken
  4. GET  /v2/plugin/login/account?state=…          → uid / 昵称
  5. 调 checkin-activity-status 自证令牌有效

产出 checkin.py 认的凭证格式，写入 auths/workbuddy-N.json（已 gitignored），
同时打印完整 JSON，直接贴进仓库 Secret WORKBUDDY_AUTH_1 / WORKBUDDY_AUTH_2。

用法：
  python3 cn_login.py                  # 第一个账号
  python3 cn_login.py                  # 再跑一次拿第二个账号（自动递增编号）
  python3 cn_login.py --no-open        # 只打印链接，不自动开浏览器
  python3 cn_login.py --timeout 600    # 自定义等待秒数
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser

# 站点 -> (接口域名, platform 参数, X-Domain 头)
SITES = {
    "workbuddy": ("https://www.workbuddy.cn", "workbuddy", "workbuddy.cn"),
    "codebuddy": ("https://www.codebuddy.cn", "codebuddy", "codebuddy.cn"),
}

ENDPOINT = "https://www.workbuddy.cn"
PLATFORM = "workbuddy"
DOMAIN = "workbuddy.cn"
UA = "CLI/2.63.2 CodeBuddy/2.63.2"

STATE_PATH = "/v2/plugin/auth/state"
TOKEN_PATH = "/v2/plugin/auth/token"
ACCOUNT_PATH = "/v2/plugin/login/account"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"

RETRY_TOKEN = 11217       # 服务端「仍在等待授权」
POLL_INTERVAL = 1.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_DIR = os.path.join(BASE_DIR, "auths")


def anon_headers():
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Domain": DOMAIN,
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }


def http(method, url, headers, body=None, timeout=25):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    except urllib.error.URLError as e:
        raise RuntimeError("网络请求失败: %s" % e.reason)
    try:
        return status, (json.loads(text) if text else {})
    except json.JSONDecodeError:
        return status, {"_raw": text}


def pick(data, *names):
    for n in names:
        if data.get(n):
            return data[n]
    return None


def uid_of(token):
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        return str(json.loads(base64.urlsafe_b64decode(part)).get("sub") or "")
    except Exception:
        return ""


def start_login():
    status, payload = http("POST", ENDPOINT + STATE_PATH + "?platform=" + PLATFORM,
                           anon_headers(), body={})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if not isinstance(data, dict) or not data.get("state") or not data.get("authUrl"):
        raise RuntimeError("auth/state 未返回 state/authUrl: %s"
                           % json.dumps(payload, ensure_ascii=False)[:300])
    return data


def poll_token(state, timeout_s, on_tick=None):
    url = ENDPOINT + TOKEN_PATH + "?state=" + state
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            _, payload = http("GET", url, anon_headers(), timeout=20)
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if isinstance(data, dict) and pick(data, "accessToken", "access_token"):
            return data
        code = payload.get("code")
        if code not in (RETRY_TOKEN, 0, None):
            msg = str(payload.get("msg") or payload.get("message") or "")[:120]
            if msg:
                raise RuntimeError("轮询失败: %s" % msg)
        if on_tick:
            on_tick()
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("等待授权超时（%d 秒），请重新运行并尽快在浏览器完成登录" % timeout_s)


def fetch_account(state, at):
    headers = dict(anon_headers())
    headers.pop("X-No-Authorization", None)
    headers["Authorization"] = "Bearer " + str(at)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _, payload = http("GET", ENDPOINT + ACCOUNT_PATH + "?state=" + state,
                              headers, timeout=15)
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if isinstance(data, dict) and data.get("uid"):
            return data
        time.sleep(POLL_INTERVAL)
    return {}


def verify_token(at):
    """只读探测：能拿到业务响应就说明令牌被服务端认了。"""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(at),
        "X-Domain": DOMAIN,
    }
    try:
        status, payload = http("POST", ENDPOINT + STATUS_PATH, headers, body={}, timeout=20)
    except Exception as e:
        return False, "请求失败: %s" % e
    code = payload.get("code")
    msg = str(payload.get("msg") or payload.get("message") or "")
    if status == 200 and code in (0, None):
        return True, "服务端已认可令牌（活动状态: %s）" % (msg or "正常返回")
    return False, "HTTP %s code=%s %s" % (status, code, msg[:100])


def next_cred_path():
    os.makedirs(CRED_DIR, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(CRED_DIR, "workbuddy-%d.json" % n)):
        n += 1
    return os.path.join(CRED_DIR, "workbuddy-%d.json" % n), n


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 国内版设备授权登录")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--timeout", type=int, default=300, help="等待授权秒数（默认 300）")
    args = ap.parse_args()

    print("🔐 WorkBuddy 国内版（%s）· 设备授权登录" % DOMAIN)

    print("[1/4] 申请授权会话…")
    started = start_login()
    state, auth_url = started["state"], started["authUrl"]
    print("  ✅ 已生成授权链接")
    print()
    print("=" * 68)
    print("👉 在浏览器打开下面链接，用手机号/微信登录并授权：")
    print()
    print("   " + auth_url)
    print("=" * 68)
    if not args.no_open:
        try:
            webbrowser.open(auth_url, new=2)
            print("（已尝试自动打开浏览器）")
        except Exception:
            pass

    print("[2/4] 等待浏览器完成授权", end="", flush=True)
    auth = poll_token(state, args.timeout,
                      on_tick=lambda: (sys.stdout.write("."), sys.stdout.flush()))
    print("\n  ✅ 已获取令牌")

    at = pick(auth, "accessToken", "access_token")
    rt = pick(auth, "refreshToken", "refresh_token") or ""
    expires_in = pick(auth, "expiresIn", "expires_in") or 7 * 24 * 3600

    print("[3/4] 获取账号信息…")
    account = fetch_account(state, at)
    uid = account.get("uid") or uid_of(at)
    note = account.get("nickname") or (uid[:8] if uid else "未知账号")
    print("  ✅ %s（uid=%s）" % (note, uid or "?"))

    cred = {
        "uid": uid,
        "accessToken": at,
        "refreshToken": rt,
        "expiresAt": int(time.time()) + int(expires_in),
        "domain": DOMAIN,
    }

    print("[4/4] 校验令牌是否被服务端认可…")
    ok, detail = verify_token(at)
    print(("  ✅ " if ok else "  ⚠️ ") + detail)
    if not ok:
        print("  令牌可能无效，先别填进 Secret，重新运行本脚本再试一次")

    path, n = next_cred_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cred, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)

    print()
    print("=" * 68)
    print("✅ 账号 %s 的凭证已生成" % note)
    print("   本地文件: %s（已 gitignored，权限 600）" % path)
    print()
    print("👉 把下面整段 JSON 贴进仓库 Secret：WORKBUDDY_AUTH_%d" % n)
    print("   https://github.com/pangshiqi00/-/settings/secrets/actions")
    print("=" * 68)
    print(json.dumps(cred, ensure_ascii=False, indent=2))
    print("=" * 68)
    print("有效期到: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(cred["expiresAt"])))
    print("多个账号：再跑一次本脚本，编号自动递增")
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as e:
        print("\n❌ %s" % e)
        sys.exit(1)