# GitHub 每日情绪日报

这个子项目用于：

- 每个工作日自动运行
- 计算最近 `5` 个交易日的情绪数据
- 通过 QQ 邮箱发送给你

## 目录

- `run_daily_report.py`：主脚本
- `requirements.txt`：依赖
- `daily_report_latest.csv`：脚本运行后生成的最近 5 天结果

## GitHub Secrets

在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 里配置：

- `TUSHARE_TOKEN`
- `QQ_SMTP_USER`
- `QQ_SMTP_PASS`
- `MAIL_TO`

可选：

- `MAIL_CC`

## QQ 邮箱说明

- `QQ_SMTP_USER` 填你的 QQ 邮箱，例如 `123456@qq.com`
- `QQ_SMTP_PASS` 填 QQ 邮箱 SMTP 授权码，不是登录密码

## 默认运行方式

- 每个工作日北京时间 `17:30`
- 只输出最近 `5` 个交易日

## 本地运行

```bash
pip install -r github_daily_report/requirements.txt
python github_daily_report/run_daily_report.py
```

## 可调参数

通过环境变量覆盖：

- `RECENT_DAYS`：默认 `5`
- `TARGET_DATE`：默认当天，例如 `20260607`
- `SEND_EMAIL`：默认 `1`，设为 `0` 可只生成 CSV 不发邮件
