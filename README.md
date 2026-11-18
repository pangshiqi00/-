# trae-signin — TRAE SOLO 纯签到工具

TRAE Work (SOLO CN) 的轻量签到工具。只做三件事：**登录 → 签到 → 查积分**，没有任何 OpenAI API 反代或对话功能。

基于 [traework2api](https://github.com/Sliverkiss/traework2api) 精简而来。

纯 Go 标准库，零第三方依赖。

## 功能

| 功能 | 说明 |
|------|------|
| **登录** | `login.sh` 生成登录链接 → 浏览器登录 → 粘贴回调 → 自动换 token 落盘 |
| **签到** | 批量遍历 `auths/` 下所有账号，自动刷新过期 token，逐个签到 |
| **到账校验** | 签到后回查确认，接口返回成功但积分未到账会标记为 `UNCONFIRMED`（不谎报成功） |
| **积分拆分** | 分别显示「月赠额度」与「签到奖励额度」，一眼看出签到有没有加分 |
| **Bark 通知** | 签到完成后自动推送到 Bark（iOS 通知） |
| **定时签到** | GitHub Actions 每天定时执行 |

## 快速开始

```bash
# 1. 安装依赖（需要 Go 1.21+ 和 Python 3）
# macOS: brew install go python3
# Ubuntu: apt install golang python3

# 2. 克隆仓库
git clone https://github.com/<your-username>/trae-signin.git
cd trae-signin
chmod +x login.sh signin.sh

# 3. 登录账号
./login.sh
# → 浏览器打开链接 → 手机号/验证码登录 → 粘贴回调链接

# 4. 签到
./signin.sh
# → 自动刷新 token → 签到 → 显示积分 → Bark 通知
```

## Bark 通知配置

签到完成后自动推送到 Bark。通过环境变量设置（**不要把真实 Key 写进仓库**）：

```bash
export BARK_URL="https://api.day.app/你的Key"
./signin.sh
```

推送内容示例：

> **✅ TRAE 签到成功**
> 总计2 | 成功1 | 已签1 | 未确认0 | 失败0
> 弎水 ALREADY 总600(签到+100)
> 用户0670362 ✅ OK 总600(签到+100)

> **⚠️ TRAE 签到未确认**
> 总计2 | 成功0 | 已签1 | 未确认1 | 失败0
> 弎水 ALREADY 总600(签到+100)
> 用户0670362 UNCONFIRMED 总500(签到0)

## ⚠️ 关键：必须使用真实设备号

签到接口会校验 `x-device-id`，**必须是 TRAE 客户端真实注册过的设备号**，否则一律返回：

```json
{"code": 9074, "message": "当前参与用户太多，请稍后再试"}
```

> **这句话是骗人的。** 跟"人多不多"没关系，真实原因是设备校验不通过。

### 为什么随机设备号不行

- 自己生成的 32 位 hex / UUID → 服务端直接忽略 → `9074`
- **走一遍 OAuth 登录也不会把设备号注册成可信设备** → 照样 `9074`
- 真号只能从**装过 TRAE 客户端并登录过的机器**上取

### 怎么取真实设备号

在装了 TRAE 客户端的机器上，打开客户端的 `storage.json`（Windows 通常在
`%APPDATA%\TRAE SOLO CN\` 或 `%APPDATA%\Trae\`），搜索 `iCubeAuthInfo://icube-dc`：

```json
"iCubeAuthInfo://icube-dc:1234567890123": "dGMFEAAA..."
                        ^^^^^^^^^^^^^^^
                        这串数字就是设备号（12~20 位十进制，示例中为占位值）
```

**注意**：不要用 `telemetry.devDeviceId`（UUID 格式，会被拒绝）。
设备号是**纯数字**。

### 一台设备一天只能签一个账号

```
9095 = 当前设备今日已经签到
```

签发额度是按**设备**算的，不是按账号。所以：

| 场景 | 需要几台设备 |
|---|---|
| 签 1 个账号 | 1 台 |
| 签 N 个账号 | **N 台**（每台都要装过 TRAE 客户端并登录） |

同一设备号填给多个账号，第二个起必然 `9095`。

### 用法

```bash
# 登录时指定设备号（不再随机生成）
DEVICE_ID=1234567890123 ./login.sh

# 已有凭证文件的话，直接改 deviceId 字段即可
```

## 签到状态说明

| 状态 | 含义 |
|---|---|
| `✅ OK` | 签到成功，且已回查确认积分到账 |
| `ALREADY` | 今日已签到，无需重复 |
| `BAD_DEVICE` | 设备号未被服务端认可（`code=9074`），需按上文更换真实设备号 |
| `UNCONFIRMED` | 签到接口返回成功，但回查发现积分未到账，**不计为失败但会告警** |
| `DISABLED` | 账号被服务端禁用签到，不计为失败 |
| `FAIL` | 网络错误、HTTP 4xx/5xx 或 token 刷新失败 |

> **关于「假成功」**：TRAE 的 `claim` 接口在业务失败时**仍返回 HTTP 200**，真正的结果在响应体的 `code` 字段里（`0` 为成功，`9074` 为设备校验失败）。本工具会解析 `code`，并按 3s → 12s 退避重试；重试后仍失败会**回查积分确认到账**，只有确实进账才判为成功。

## 积分构成说明

TRAE 的积分由**多个独立到期的包**组成，`总计` 是它们的合计：

| 包 | 来源 | 有效期 |
|---|---|---|
| 每月登录赠送 | 每月自动发放（通常 500） | 按自然月 |
| 签到奖励 | 每日签到发放（100/天） | 自签到日起 31 天 |

所以「签到成功但总积分没涨」的常见原因是：**签到包是独立的一份，且会单独过期**。本工具已把两项拆开显示在 `月赠` / `签到` 两列。

## 定时签到

### 方式一：GitHub Actions（推荐）

最稳定的方案，不依赖本地环境，GitHub 服务器每天自动触发。

**配置步骤：**

1. Fork 或推送本仓库到你的 GitHub

2. 在仓库 Settings → Secrets and variables → Actions 中添加以下 Secrets：

   | Secret 名称 | 值 | 说明 |
   |---|---|---|
   | `TRAE_AUTH_1` | 账号1 的完整凭证 JSON | `auths/trae-*.json` 的文件内容 |
   | `TRAE_AUTH_2` | 账号2 的完整凭证 JSON | 同上，多个账号依次添加 |
   | `WORKBUDDY_AUTH_1` | workbuddy.cn 账号1 的凭证 JSON | `auths/workbuddy-*.json` 的文件内容 |
   | `WORKBUDDY_AUTH_2` | workbuddy.cn 账号2 的凭证 JSON | 同上 |
   | `BARK_URL` | `https://api.day.app/你的Key` | Bark 推送地址 |

3. 每天**北京时间 00:00~06:00 之间抽一个时刻**，目标之后直到当天 23:00 的任何一次被交付的运行都会执行签到。
   也可在 Actions 页面手动触发（手动触发不抽签，立即签到）

> 凭证 JSON 格式（登录后从 `auths/trae-*.json` 复制）：
> ```json
> {"account":{"uid":"...","nickname":"..."},"auth":{"accessToken":"...","refreshToken":"...","expiresAt":1786858238,...}}
> ```

#### WorkBuddy 国内版凭证怎么拿

GitHub 的 Secret **只写不可读**，建好之后取不回原值。所以换 Secret 名或凭证过期时，
用 `cn_login.py` 重新生成（与国际版同一套 `/v2/plugin/auth/` 设备授权协议，只是域名不同）：

```bash
python3 cn_login.py          # 浏览器弹链接 → 手机号/微信登录授权 → 自动拿令牌
python3 cn_login.py          # 再跑一次拿第二个账号，编号自动递增
```

> **只签 workbuddy.cn**：`www.workbuddy.cn` 与 `www.codebuddy.cn` 登录态独立、令牌互不通用，
> 但积分与签到状态实测是同一份后端数据（签一边另一边立即显示已签），所以自动签到只签
> workbuddy.cn，不重复签 codebuddy.cn。登录第二个账号时用浏览器隐身窗口或另一个浏览器，
> 避免沿用第一个账号的会话。**登录只需这一次**：之后每天由 refreshToken 自动续期，无需再登录。

脚本会：写入 `auths/workbuddy-N.json`（gitignored，权限 600）→ 调 `checkin-activity-status`
自证令牌被服务端认可 → 打印完整 JSON 供贴进 `WORKBUDDY_AUTH_N`。

产出格式（与 `checkin.py` 的 `WorkBuddy.to_cred()` 一致）：

```json
{"uid":"...","accessToken":"...","refreshToken":"...","expiresAt":1786858238,"domain":"workbuddy.cn"}
```

> 校验不通过时脚本会明确告警并以退出码 2 结束，不会让你把无效令牌填进 Secret。

### 方式二：Crontab

```bash
# 每天 08:00 签到
crontab -e
# 添加：
0 8 * * * cd /path/to/trae-signin && bash signin.sh >> signin.log 2>&1
```

### ⚠️ 随机时刻必须用抽签，不能用 sleep

早期版本在 runner 上 `sleep $((RANDOM % 14400 + 60))` 来模拟真人，**这是把免费额度烧穿的直接原因**：

| 做法 | 每天计费分钟 | 每月（30 天） |
|---|---|---|
| runner 上 sleep 0~4 小时（两条工作流） | 平均 246，最坏 486 | 7380 分钟 ≈ $44 |
| 窄窗口抽签（每天 44 次触发） | 约 52 | 1560 分钟 ≈ $9.4 |
| **全天抽签（当前，每天 144 次触发）** | **≥144** | **≥4320 分钟 ≈ $26** |

runner 上的 `sleep` 全程计入 Actions 计费分钟。额度耗尽后**所有**工作流一起无法启动，
表现为「三个平台的签到同时全挂」——而它们的登录态其实互不相干。

三条工作流统一用 `checkin_gate.py`：cron 每 30 分钟触发，用日期做种子抽一个当天时刻，
没抽中的运行在几秒内退出（不装运行时、不跑签到）。

> **计费按 job 向上取整到整分钟**：空跑只花 5 秒，也计 1 分钟，所以触发次数就是成本下限。
> 私有仓库 GitHub Free 只含 $12/月（Linux 2 核 $0.006/分钟 ≈ 2000 分钟），
> **全天抽签在私有仓库一定会烧穿额度**——这套调度的前提是仓库 public
> （公共仓库标准 runner 完全免费、不计量）。若转回 private，必须把 cron 收窄回抽签窗口内触发。

> **为什么有效区间要从「目标 + 45 分钟容差」放宽到「目标之后直到 23:00」**：
> GitHub 的 schedule 会延迟甚至直接丢弃。实测 2026-11-17~18 两天，国内版按 cron 应有 22 次触发，
> 只交付了 1 次，还在窗口之外，结果整天没签（账号连登天数原地不动）。
> 签到接口幂等，重复执行只会返回「今日已签到」，所以宁可多执行几次也不能漏掉一整天。

## 🌍 海外版（WorkBuddy AI · www.workbuddy.ai）

国内版与海外版是**两套独立登录态**（`www.workbuddy.cn` ↔ `www.workbuddy.ai`，令牌互不通用），
所以海外版用独立脚本 + 独立工作流，与现有国内签到并存、互不影响。

| 文件 | 作用 |
|---|---|
| `intl_login.py` | 设备授权登录（Google / GitHub / X，免手机号），输出 `备注\|AT\|uid\|RT` |
| `intl_checkin.py` | 探测活动 → 领取每日奖励 → 活跃上报（连登）→ 查积分 |
| `checkin_gate.py` | 抽签门控（三条工作流共用）：国际版默认纽约 07:00~17:00，国内版传参为北京时间 00:00~06:00 |
| `.github/workflows/workbuddy-intl-signin.yml` | 全天每 30 分钟触发一次，抽签决定从哪个时刻起执行 |

**配置三步：**

1. 本地执行 `python intl_login.py`，浏览器打开授权链接登录，复制输出的那一行
2. 仓库 Settings → Secrets and variables → Actions 新增 `WORKBUDDY_INTL_TOKENS`
   （多账号用换行分隔，一行一个：`备注|AT|uid|RT`）
3. 到 Actions 手动跑一次「WorkBuddy 国际版签到」验证

**口径说明：**

- 海外版的「每日奖励」不是单纯签到，而是一条活跃任务链：`daily-checkin` 领取 + `/v2/report` 活跃上报（点亮连登）
- 活动未开放是正常状态，不算失败；签到成功与活跃结果分开报告
- 令牌失效会明确报「令牌失效」，重跑 `intl_login.py` 更新 secret 即可；带 RT 时会先自动续期再试
- 命中账号级风控（`11140 / request illegal`）立即停止该账号本轮，不刷新、不重试
- 需要额外保活对话与网页会话：`python intl_checkin.py --keepalive`

## 🩺 账号管理工具（accounts.py）

`accounts.py` 把「查登录态 / 加新账号 / 导入 / 删除 / 推 Secret」收在一个入口，只依赖标准库，
Python 3.9+ 可用：

```bash
python3 accounts.py status                    # 检查所有账号：登录态、今日签到、连续天数、积分、到期
python3 accounts.py status --refresh          # 令牌临期时自动续期（会轮换 refreshToken）
python3 accounts.py status --json             # 机器可读输出
python3 accounts.py login --site workbuddy    # 新增/更新 WorkBuddy 国内版
python3 accounts.py login --site codebuddy    # 新增/更新 CodeBuddy 国内版
python3 accounts.py login --site intl         # 新增/更新国际版
python3 accounts.py import --line '备注|AT|uid|RT'
python3 accounts.py import --file ~/Downloads/export.json
python3 accounts.py remove --uid 5c6a3c12
python3 accounts.py secrets                   # 列出待写入的 Secret（默认不显示值）
python3 accounts.py secrets --push            # 用 gh 写入 GitHub Secrets
python3 accounts.py secrets --push --trigger  # 写入后顺手触发一次签到
```

### 桌面应用

仓库根目录有 **`账号工具.app`**，双击直接打开图形界面：

- 体检 / 查登录态
- 自动修复失效账号
- 按站点一键登录（WorkBuddy 国内版 / CodeBuddy 国内版 / 国际版）
- 预览或推送 GitHub Secrets
- 打开 `auths` 目录
- 实时输出日志（只在本地显示，不上传）

可以把它拖到「应用程序」文件夹。应用会读 `Contents/Resources/repo_path.txt` 找仓库目录；
如果以后仓库搬家，改这个文件里的路径即可。启动器源码是 `app_launcher.c`，
重新编译：

```bash
cc -O2 -o "账号工具.app/Contents/MacOS/launcher" app_launcher.c
```

> 如果 macOS 提示「无法验证开发者」，右键应用 →「打开」一次即可。

### 一键登录

最省事的方式：双击仓库根目录的 **`一键登录.command`**，按菜单选择：

```
1) 登录 / 新增账号
2) 自动检测并重新登录失效账号
3) 只看登录状态
```

命令行等价：

```bash
python3 accounts.py login              # 不带 --site 时弹出站点菜单
python3 accounts.py relogin            # 先检测，只对失效账号拉起浏览器
python3 accounts.py relogin --refresh  # 先尝试续期，续期成功就不重新登录
```

`relogin` 会逐个站点打开授权页；登录哪个账号就按 uid 更新哪个账号。同一站点有多个失效账号时，
跑完一轮再执行一次即可补齐。

要点：

- `status` 默认只读；只有加 `--refresh` 才会消耗/轮换 refreshToken，避免和 AI Work 助手或 GitHub Actions 抢着续期。
- 同一 uid 在 `workbuddy-*.json` 和 `codebuddy-*.json` 各有一份时，会自动按有效期保留最新那份，并提示清理旧文件。
- `login` 复用设备授权协议，成功后按 uid 覆盖旧凭证，不会生成重复账号。
- `import` 支持 `备注|AT|uid|RT` 文本、单账号 JSON、账号数组，以及 AI Work 助手「导出账号」的 `accounts` 结构；不含凭证的导出会被跳过。
- `secrets --push` 依赖 `gh auth login`，默认从 `git remote` 推断仓库；不加 `--push` 只预览。
- 国际版账号同时写入 `auths/intl-tokens.txt`，本地跑 `intl_checkin.py` 时即使没设环境变量也能读到；旧的 `intl_login_result.json` 会自动补进 `auths/`。

> `--refresh` 和 `--push` 会改变 refreshToken / 远端 Secret。若 AI Work 助手也在自动续期同一批账号，
> 建议只保留一条续期链路，避免两边互相把对方的 refreshToken 刷失效。

## 目录结构

```
cmd/
└── signin/main.go      签到入口

internal/
├── auth/auth.go        凭证文件解析与原子写回
└── upstream/upstream.go 上游 API 客户端（签到/积分/刷新 token）

login.sh                登录脚本
signin.sh               签到脚本（含 Bark 通知）
crontab.txt             Crontab 配置参考
accounts.py             账号体检 / 一键登录 / 导入 / GitHub Secret 同步
accounts_gui.py         桌面图形界面（Tkinter）
app_launcher.c          账号工具.app 的启动器源码
账号工具.app            双击运行的桌面应用
一键登录.command        双击运行的一键登录入口
auths/                  （gitignored）凭证文件
```

## API 端点

本工具调用 TRAE 官方 API：

| 端点 | 用途 |
|------|------|
| `api.trae.com.cn/.../ExchangeToken` | 用 refreshToken 换 accessToken |
| `api.trae.cn/.../checkin_credits/status` | 查询签到状态 |
| `api.trae.cn/.../checkin_credits/claim` | 执行签到 |
| `api.trae.cn/.../ide_user_ent_usage` | 查询积分余额 |

## 安全说明

- 所有 token 仅保存在 `auths/` 目录（已 gitignored），不会上传到 Git
- 终端输出只显示 UID/昵称/积分，不打印 token
- `login.sh` 回调链接使用 `read -s` 不回显，避免终端留痕

## 致谢

- [traework2api](https://github.com/Sliverkiss/traework2api) — 原项目，本工具的精简来源
- [Bark](https://github.com/Finb/Bark) — iOS 通知推送工具

## License

MIT
