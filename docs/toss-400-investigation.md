# Toss 资金流采集说明

本说明介绍匿名资金流接口的请求条件与数据校验，不依赖特定运行环境。

## HTTP 400 的请求条件

缺少 `deviceId` Cookie 时，资金流接口可能返回 HTTP 400 `invalid-request`。Toss 网页客户端会生成 `WTS-` 加 UUID 十六进制字符串形式的匿名设备标识，再用于请求。

`scripts/fetch_toss.py` 使用同样格式自动创建标识，保存在指定状态目录的 `toss-device-id` 文件中，后续请求固定复用：

```text
Cookie: deviceId=<本地自动生成的匿名标识>
```

不需要登录账户、密码、API 密钥或完整浏览器。标识文件属于本地运行状态，已被 Git 忽略，不应提交实际值。

## 分页与数据校验

采集器使用 Python 标准库 `urllib` 请求接口，并进行以下检查：

- 按接口返回的 `pagingParam` 游标翻页，不自行猜测日期或游标。
- 检查返回结构，拒绝冲突的重复日期与重复分页游标。
- 剔除未来日期、标准常规收盘时间前的当日记录及仍处于盘中的记录。
- 核对个人、外资、机构的“买入 − 卖出 = 净买入”。
- 核对最新完整交易日的七类机构买入及净买入之和与机构合计一致。
- 保留缺失字段，不将未知值补成零；数据不足时明确失败。
- 保存原始分页、抓取时间及校验结果，方便核对来源。

HTTP 请求被拒绝时会明确报错，不通过轮换标识反复尝试。看板后台保留最近成功数据，并记录失败状态。

## 本地运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/fetch_toss.py --product-code A000660 --days 120 --state-dir .state --output runtime/A000660.json
.\.venv\Scripts\python.exe scripts/fetch_toss.py --product-code A005930 --days 120 --state-dir .state --output runtime/A005930.json
```

输出的单位为股，`rows` 是完整交易日记录，`pages` 是原始分页，`checks` 是校验结果。独立脚本写出的文件不会自动导入看板数据库。

## 可选浏览器诊断

`scripts/probe_toss_browser.py` 仅用于对照空白浏览器会话中的匿名请求行为，日常采集不需要运行。使用该脚本需要另行安装 Playwright 及 Chromium，它们不属于看板运行依赖。

诊断报告写入本地 `toss-browser-probe.json`，已被 Git 忽略。该脚本不加载个人浏览器配置，不保存 Cookie 值。

## 数据边界

`data/live/` 保存通过 HTTP 采集的公开历史数据，来源标记为 `toss_http_live`；`data/samples/` 中的浏览器响应是单独保留的测试样本。样本及初始历史数据的日期不代表当前日期。

资金流接口中的 `close` 可能包含盘后变动，不能当作 KRX 常规收盘价；`updatedAt` 是资金流更新时间，不能当作价格成交时间。详情见 [价格与时段口径](price-sessions.md)。

接口未承诺稳定性或商业再分发许可；公开可访问不代表可以任意商业使用。
