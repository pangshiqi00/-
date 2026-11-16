#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
签到「抽签」门控 —— 决定本次定时运行要不要真的签到。三条工作流共用。

为什么要门控：
  GitHub 的 cron 不支持「每天在某个区间内随机一个时刻」，只能定点触发。
  所以这里每 30 分钟触发一次，用「日期」做种子抽一个当天的目标时刻：
    · 没到目标 → 几秒内退出（几乎不耗额度）
    · 到目标（含容差，吃掉 GitHub 的调度延迟）→ 真正签到
  同一天最多执行 2 次，签到接口是幂等的（第二次返回「今日已领取」），不会重复扣次数。

为什么不用 sleep 实现随机延时：
  runner 上的 sleep 全程计入 Actions 计费分钟。随机延时 0~4 小时 = 单条工作流
  每天最多 241 分钟，两条就能把私有仓库的免费额度烧穿，导致所有签到一起停摆。

时区：目标窗口按 --tz 指定的时区计算，自动跟随夏令时/冬令时。

用法：
  python3 checkin_gate.py                    # 国际版默认：纽约 07:00~17:00
  python3 checkin_gate.py --tz Asia/Shanghai --start 0 --end 4 --hard-stop 5 --seed wb-cn-
  加 -v 本地调试打印判断过程；结果写到 $GITHUB_OUTPUT 的 run=1/0
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def emit(run, message):
    print(message, flush=True)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("run=%d\n" % (1 if run else 0))
        except Exception as e:
            print("⚠️ 写入 GITHUB_OUTPUT 失败: %s" % e, flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="签到抽签门控")
    ap.add_argument("--tz", default="America/New_York", help="目标窗口的时区")
    ap.add_argument("--start", type=int, default=7, help="窗口起始小时（含）")
    ap.add_argument("--end", type=int, default=17, help="窗口结束小时（不含）")
    ap.add_argument("--hard-stop", type=int, default=18, help="超过这个小时一律不执行")
    ap.add_argument("--slot", type=int, default=30, help="抽签粒度（分钟）")
    ap.add_argument("--tolerance", type=int, default=45, help="目标时刻之后多少分钟内仍算有效")
    ap.add_argument("--seed", default="wb-intl-", help="随机种子前缀，区分不同工作流")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印判断过程")
    args = ap.parse_args()

    # 手动触发一律执行，方便验证
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return emit(True, "手动触发 → 直接执行")

    tz = ZoneInfo(args.tz)
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    slots = (args.end - args.start) * 60 // args.slot
    seed = args.seed + now.strftime("%Y-%m-%d")
    index = random.Random(seed).randrange(slots)
    target = midnight + timedelta(hours=args.start, minutes=index * args.slot)

    if args.verbose:
        print("当前时间（%s）: %s" % (args.tz, now.strftime("%Y-%m-%d %H:%M:%S %Z")))
        print("今日抽中目标: %s（%d 个候选时段中第 %d 个）"
              % (target.strftime("%H:%M"), slots, index + 1))

    if now.hour < args.start:
        return emit(False, "未到窗口（%s）→ 跳过" % now.strftime("%H:%M"))

    if now >= midnight + timedelta(hours=args.hard_stop):
        return emit(False, "已过窗口（%s）→ 跳过" % now.strftime("%H:%M"))

    if now < target:
        return emit(False, "今日目标 %s，当前 %s → 等待"
                    % (target.strftime("%H:%M"), now.strftime("%H:%M")))

    delta = (now - target).total_seconds() / 60
    if delta > args.tolerance:
        return emit(False, "已过目标 %s 的 %d 分钟容差（当前 %s）→ 跳过"
                    % (target.strftime("%H:%M"), args.tolerance, now.strftime("%H:%M")))

    return emit(True, "今日目标 %s，当前 %s（延迟 %d 分钟）→ 执行签到"
                % (target.strftime("%H:%M"), now.strftime("%H:%M"), int(delta)))


if __name__ == "__main__":
    sys.exit(main())
