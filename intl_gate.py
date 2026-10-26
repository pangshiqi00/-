#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国际版签到「抽签」门控 —— 决定本次定时运行要不要真的签到。

为什么要门控：
  GitHub 的 cron 不支持「每天在某个区间内随机一个时刻」，只能定点触发。
  所以这里每 30 分钟触发一次，用「日期」做种子抽一个当天的目标时刻：
    · 没到目标 → 几秒内退出（几乎不耗额度）
    · 到目标（含 45 分钟容差，吃掉 GitHub 的调度延迟）→ 真正签到
  同一天最多执行 2 次，签到接口是幂等的（第二次返回「今日已领取」），不会重复扣次数。

时区：目标窗口按 **America/New_York** 计算，自动跟随夏令时/冬令时。

用法（仅给 Actions 用）：
  python intl_gate.py        # 结果写到 $GITHUB_OUTPUT 的 run=1/0
  python intl_gate.py -v     # 本地调试：直接打印判断过程
"""

import os
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

WINDOW_START_H = 7          # 纽约时间 07:00 开始
WINDOW_END_H = 17           # 纽约时间 17:00 结束
SLOT_MIN = 30               # 抽签粒度（分钟）
TOLERANCE_MIN = 45          # 目标时刻之后多久内仍算有效（吃掉 GitHub 调度延迟）
HARD_STOP_H = 18            # 超过这个点（纽约时间）一律不再执行


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
    verbose = "-v" in sys.argv

    # 手动触发一律执行，方便验证
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return emit(True, "手动触发 → 直接执行")

    now = datetime.now(NY)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    slots = (WINDOW_END_H - WINDOW_START_H) * 60 // SLOT_MIN
    seed = "wb-intl-" + now.strftime("%Y-%m-%d")
    index = random.Random(seed).randrange(slots)
    target = midnight + timedelta(hours=WINDOW_START_H, minutes=index * SLOT_MIN)

    if verbose:
        print("纽约当前时间: %s" % now.strftime("%Y-%m-%d %H:%M:%S %Z"))
        print("今日抽中目标: %s（%d 个候选时段中第 %d 个）"
              % (target.strftime("%H:%M"), slots, index + 1))

    if now.hour < WINDOW_START_H:
        return emit(False, "未到窗口（纽约 %s）→ 跳过" % now.strftime("%H:%M"))

    if now >= midnight + timedelta(hours=HARD_STOP_H):
        return emit(False, "已过窗口（纽约 %s）→ 跳过" % now.strftime("%H:%M"))

    if now < target:
        return emit(False, "今日目标 %s，当前 %s → 等待"
                    % (target.strftime("%H:%M"), now.strftime("%H:%M")))

    delta = (now - target).total_seconds() / 60
    if delta > TOLERANCE_MIN:
        return emit(False, "已过目标 %s 的 %d 分钟容差（当前 %s）→ 跳过"
                    % (target.strftime("%H:%M"), TOLERANCE_MIN, now.strftime("%H:%M")))

    return emit(True, "今日目标 %s，当前 %s（延迟 %d 分钟）→ 执行签到"
                % (target.strftime("%H:%M"), now.strftime("%H:%M"), int(delta)))


if __name__ == "__main__":
    sys.exit(main())
