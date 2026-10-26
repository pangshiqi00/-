#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy AI（国际版 www.workbuddy.ai）设备授权登录 —— 零第三方依赖。

国际版账号多为 Google / GitHub / X 注册，没有手机号，短信登录接口不可用，
所以走客户端同款的设备授权流程：

  1. POST /v2/plugin/auth/state?platform=workbuddy-ai  → {state, authUrl}
  2. 浏览器打开 authUrl，用 Google / GitHub / X 登录并授权
  3. 轮询 GET /v2/plugin/auth/token?state=…            → accessToken + refreshToken
  4. GET  /v2/plugin/login/account?state=…             → uid / 昵称

输出一行，直接填进仓库 Secret `WORKBUDDY_INTL_TOKENS`：

    备注|accessToken|uid|refreshToken

用法：
  python intl_login.py                       # 打印链接 → 浏览器授权 → 自动拿令牌
  python intl_login.py --no-open             # 只打印链接
  python intl_login.py --timeout 600         # 自定义等待秒数
  python intl_login.py --refresh '备注|AT|uid|RT'   # 用已有 RT 换新令牌
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

ENDPOINT = "https://www.workbuddy.ai"
PLATFORM = "workbuddy-ai"
DOMAIN = "www.workbuddy.ai"
UA = "WorkBuddy/5.4.2"

STATE_PATH = "/v2/plugin/auth/state"
TOKEN_PATH = "/v2/plugin/auth/token"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
ACCOUNT_PATH = "/v2/plugin/login/account"

RETRY_TOKEN = 11217       # 服务端「仍在等待授权」
POLL_INTERVAL = 1.0
RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "intl_login_result.json")


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


def poll_token(state, timeout_s=300, on_tick=None):
    url = ENDPOINT + TOKEN_PATH + "?state=" + state
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            _, payload = http("GET", url, anon_headers(), timeout=20)
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if isinstance(data, dict) and data.get("accessToken"):
            return data
        code = payload.get("code")
        if code not in (RETRY_TOKEN, 0, None):
            msg = str(payload.get("msg") or "")[:120]
            if msg:
                raise RuntimeError("轮询失败: %s" % msg)
        if on_tick:
            on_tick()
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("等待授权超时（%d 秒），请重新运行并尽快在浏览器完成登录" % timeout_s)


def fetch_account(state, at):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Authorization": "Bearer " + str(at),
        "X-Domain": DOMAIN,
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _, payload = http("GET", ENDPOINT + ACCOUNT_PATH + "?state=" + state, headers, timeout=15)
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if isinstance(data, dict) and data.get("uid"):
            return data
        time.sleep(POLL_INTERVAL)
    return {}


def refresh(rt, at=""):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": str(rt),
        "X-Auth-Refresh-Source": "plugin",
        "X-Domain": DOMAIN,
        "Authorization": "Bearer " + str(at or ""),
    }
    status, payload = http("POST", ENDPOINT + REFRESH_PATH, headers, body={})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise RuntimeError("刷新失败: %s" % json.dumps(payload, ensure_ascii=False)[:200])
    return data


def emit(note, at, uid, rt, auth=None):
    line = "%s|%s|%s|%s" % (note, at, uid, rt)
    print()
    print("=" * 68)
    print("✅ 把下面这一行填进仓库 Secret：WORKBUDDY_INTL_TOKENS")
    print("   （多账号：换行分隔，一行一个账号）")
    print("=" * 68)
    print(line)
    print("=" * 68)

    record = {"note": note, "uid": uid, "env_line": line,
              "login_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if isinstance(auth, dict):
        record["expires_in"] = auth.get("expiresIn")
        record["refresh_expires_in"] = auth.get("refreshExpiresIn")
    items = []
    if os.path.exists(RESULT_FILE):
        try:
            items = json.load(open(RESULT_FILE, encoding="utf-8"))
        except Exception:
            items = []
    items.append(record)
    with open(RESULT_FILE, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    print("📁 已另存: %s" % RESULT_FILE)


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy AI 国际版设备授权登录")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--timeout", type=int, default=300, help="等待授权秒数（默认 300）")
    ap.add_argument("--refresh", metavar="ENTRY", help="用已有 RT 刷新，格式：备注|AT|uid|RT")
    args = ap.parse_args()

    print("🔐 WorkBuddy AI 国际版 · 设备授权登录")

    if args.refresh:
        parts = args.refresh.split("|")
        note = parts[0] if parts and parts[0] else "账号1"
        old_at = parts[1] if len(parts) > 1 else ""
        uid = parts[2] if len(parts) > 2 else ""
        rt = parts[3] if len(parts) > 3 else ""
        print("🔁 正在用 refresh_token 刷新…")
        data = refresh(rt, old_at)
        new_at = data["accessToken"]
        new_rt = data.get("refreshToken") or rt
        print("  ✅ 刷新成功")
        emit(note, new_at, uid or uid_of(new_at), new_rt, data)
        return 0

    print("[1/3] 申请授权会话…")
    started = start_login()
    state, auth_url = started["state"], started["authUrl"]
    print("  ✅ 已生成授权链接")
    print()
    print("=" * 68)
    print("👉 在浏览器打开下面链接，用 Google / GitHub / X 登录并授权：")
    print()
    print("   " + auth_url)
    print("=" * 68)
    if not args.no_open:
        try:
            webbrowser.open(auth_url, new=2)
            print("（已尝试自动打开浏览器）")
        except Exception:
            pass

    print("[2/3] 等待浏览器完成授权", end="", flush=True)
    auth = poll_token(state, args.timeout,
                      on_tick=lambda: (sys.stdout.write("."), sys.stdout.flush()))
    print("\n  ✅ 已获取令牌")

    at = auth["accessToken"]
    rt = auth.get("refreshToken") or ""
    uid = uid_of(at)

    print("[3/3] 获取账号信息…")
    account = fetch_account(state, at)
    if account.get("uid"):
        uid = account["uid"]
    note = account.get("nickname") or (uid[:8] if uid else "账号1")
    print("  ✅ %s（uid=%s）" % (note, uid or "?"))

    emit(note, at, uid, rt, auth)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as e:
        print("\n❌ %s" % e)
        sys.exit(1)
