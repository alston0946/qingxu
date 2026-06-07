# -*- coding: utf-8 -*-

import os
import time
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import tushare as ts


REPORT_DIR = os.path.dirname(__file__)
OUTPUT_FILE = os.path.join(REPORT_DIR, "daily_report_latest.csv")

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "5"))

# 为最近 5 个交易日输出提供足够历史，保证年分位数/MA5/连续信号可计算
LOOKBACK_CALENDAR_DAYS = int(os.getenv("LOOKBACK_CALENDAR_DAYS", "540"))
YEAR_WINDOW_TRADING_DAYS = int(os.getenv("YEAR_WINDOW_TRADING_DAYS", "252"))
NEW_HIGH_LOOKBACK_DAYS = int(os.getenv("NEW_HIGH_LOOKBACK_DAYS", "5"))
INDEX_MA_WINDOW = int(os.getenv("INDEX_MA_WINDOW", "5"))
UP_RATIO_THRESHOLD_PCT = float(os.getenv("UP_RATIO_THRESHOLD_PCT", "80"))
PERCENTILE_SIGNAL_THRESHOLD = float(os.getenv("PERCENTILE_SIGNAL_THRESHOLD", "99"))
SIGNAL_STREAK_DAYS = int(os.getenv("SIGNAL_STREAK_DAYS", "2"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.05"))

CSI1000_TS_CODE = os.getenv("CSI1000_TS_CODE", "000852.SH").strip()
CSI300_TS_CODE = os.getenv("CSI300_TS_CODE", "000300.SH").strip()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("QQ_SMTP_USER", "").strip()
SMTP_PASS = os.getenv("QQ_SMTP_PASS", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "").strip()
MAIL_CC = os.getenv("MAIL_CC", "").strip()
SEND_EMAIL = os.getenv("SEND_EMAIL", "1").strip() == "1"


def get_today_cn_str():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d")


def calc_start_date(target_date: str):
    end_dt = datetime.strptime(target_date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    return start_dt.strftime("%Y%m%d")


def is_mainland_a_share(ts_code: str) -> bool:
    code6 = str(ts_code)[:6]
    return not code6.startswith(("200", "900"))


def percentile_of_latest(arr):
    arr = pd.to_numeric(pd.Series(arr), errors="coerce").dropna().to_numpy(dtype=float)
    if len(arr) == 0:
        return np.nan
    current = arr[-1]
    return float((arr <= current).mean() * 100)


def build_extreme_breadth_signal(df: pd.DataFrame, threshold_pct: float):
    out = df.copy()
    out["is_extreme_up"] = out["up_ratio_pct"] >= threshold_pct
    out["is_extreme_down"] = out["down_ratio_pct"] >= threshold_pct

    up_streak = 0
    down_streak = 0
    remarks = []

    for _, row in out.iterrows():
        up_streak = up_streak + 1 if bool(row["is_extreme_up"]) else 0
        down_streak = down_streak + 1 if bool(row["is_extreme_down"]) else 0

        if up_streak >= 2:
            remarks.append(f"连续上涨超80%第{up_streak}天")
        elif down_streak >= 2:
            remarks.append(f"连续下跌超80%第{down_streak}天")
        else:
            remarks.append("")

    out["备注"] = remarks
    return out


def add_index_columns(df: pd.DataFrame, prefix: str, close_col: str, ma_col: str):
    out = df.copy()
    out[f"{prefix}是否低于MA5"] = np.where(
        pd.to_numeric(out[ma_col], errors="coerce").notna()
        & (pd.to_numeric(out[close_col], errors="coerce") < pd.to_numeric(out[ma_col], errors="coerce")),
        "是",
        "否",
    )
    return out


def build_two_day_risk_signal(df: pd.DataFrame):
    out = df.copy()
    out["过去一年分位数是否>=99"] = np.where(
        pd.to_numeric(out["过去一年分位数_pct"], errors="coerce") >= PERCENTILE_SIGNAL_THRESHOLD,
        "是",
        "否",
    )

    pct_streak = 0
    csi1000_streak = 0
    csi300_streak = 0
    csi1000_signals = []
    csi300_signals = []

    for _, row in out.iterrows():
        pct_streak = pct_streak + 1 if row["过去一年分位数是否>=99"] == "是" else 0
        csi1000_streak = csi1000_streak + 1 if row["中证1000是否低于MA5"] == "是" else 0
        csi300_streak = csi300_streak + 1 if row["沪深300是否低于MA5"] == "是" else 0

        csi1000_signals.append(
            "是" if pct_streak >= SIGNAL_STREAK_DAYS and csi1000_streak >= SIGNAL_STREAK_DAYS else "否"
        )
        csi300_signals.append(
            "是" if pct_streak >= SIGNAL_STREAK_DAYS and csi300_streak >= SIGNAL_STREAK_DAYS else "否"
        )

    out["危险信号_99分位2天且中证1000连续2天在MA5下方"] = csi1000_signals
    out["危险信号_99分位2天且沪深300连续2天在MA5下方"] = csi300_signals
    return out


def fetch_margin_buy_history(pro, start_date: str, end_date: str):
    df = pro.margin(
        start_date=start_date,
        end_date=end_date,
        fields="trade_date,exchange_id,rzmre",
    )

    if df is None or df.empty:
        raise RuntimeError("pro.margin 没有返回数据。")

    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["rzmre"] = pd.to_numeric(df["rzmre"], errors="coerce")

    return (
        df.groupby("trade_date", as_index=False)
        .agg(margin_buy_amount_yuan=("rzmre", "sum"))
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def fetch_daily_snapshot_one_day(pro, trade_date: str):
    df = pro.daily(
        trade_date=trade_date,
        fields="ts_code,trade_date,amount,pct_chg,close",
    )

    if df is None or df.empty:
        return None

    df = df.copy()
    df["ts_code"] = df["ts_code"].astype(str)
    df = df[df["ts_code"].map(is_mainland_a_share)].copy()

    if df.empty:
        return None

    df["trade_date"] = df["trade_date"].astype(str)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["amount", "pct_chg", "close"]).copy()

    if df.empty:
        return None

    return df[["trade_date", "ts_code", "amount", "pct_chg", "close"]].copy()


def aggregate_market_one_day(df: pd.DataFrame):
    up_count = int((df["pct_chg"] >= 0).sum())
    down_count = int((df["pct_chg"] < 0).sum())
    flat_count = int((df["pct_chg"] == 0).sum())
    total_count = int(len(df))

    return {
        "trade_date": str(df["trade_date"].iloc[0]),
        "all_a_amount_yuan": float(df["amount"].sum(skipna=True) * 1000),
        "stock_count_with_amount": int(df["amount"].notna().sum()),
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_count": total_count,
        "up_ratio_pct": up_count / total_count * 100 if total_count > 0 else np.nan,
        "down_ratio_pct": down_count / total_count * 100 if total_count > 0 else np.nan,
        "median_pct_chg": float(df["pct_chg"].median()) if total_count > 0 else np.nan,
    }


def fetch_limit_counts_one_day(pro, trade_date: str):
    try:
        df = pro.limit_list_d(
            trade_date=trade_date,
            fields="trade_date,ts_code,limit",
        )
    except Exception:
        return {
            "trade_date": trade_date,
            "limit_up_count": np.nan,
            "limit_down_count": np.nan,
        }

    if df is None or df.empty:
        return {
            "trade_date": trade_date,
            "limit_up_count": 0,
            "limit_down_count": 0,
        }

    df = df.copy()
    if "limit" not in df.columns:
        return {
            "trade_date": trade_date,
            "limit_up_count": np.nan,
            "limit_down_count": np.nan,
        }

    df["limit"] = df["limit"].astype(str).str.upper().str.strip()
    return {
        "trade_date": trade_date,
        "limit_up_count": int(df["limit"].isin(["U", "UP", "ZT", "涨停"]).sum()),
        "limit_down_count": int(df["limit"].isin(["D", "DOWN", "DT", "跌停"]).sum()),
    }


def calc_new_high_stats(detail_df: pd.DataFrame, lookback_days: int):
    if detail_df.empty:
        return pd.DataFrame(columns=["trade_date", "new_high_count"])

    work_df = detail_df.copy()
    work_df = work_df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    work_df["rolling_high_close"] = (
        work_df.groupby("ts_code")["close"]
        .transform(lambda s: s.rolling(window=lookback_days, min_periods=lookback_days).max())
    )
    work_df["is_new_high_n_days"] = (
        work_df["rolling_high_close"].notna()
        & (work_df["close"] >= work_df["rolling_high_close"])
    )

    return (
        work_df.groupby("trade_date", as_index=False)
        .agg(new_high_count=("is_new_high_n_days", "sum"))
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def fetch_market_history(pro, trade_dates):
    summary_rows = []
    limit_rows = []
    detail_frames = []

    for i, trade_date in enumerate(trade_dates, 1):
        print(f"获取全市场明细：{i}/{len(trade_dates)} | {trade_date}")

        day_df = fetch_daily_snapshot_one_day(pro, trade_date)
        if day_df is not None and not day_df.empty:
            detail_frames.append(day_df)
            summary_rows.append(aggregate_market_one_day(day_df))

        limit_rows.append(fetch_limit_counts_one_day(pro, trade_date))

        if SLEEP_SEC > 0:
            time.sleep(SLEEP_SEC)

    if not summary_rows:
        raise RuntimeError("没有获取到任何全市场交易数据。")

    history_df = pd.DataFrame(summary_rows).sort_values("trade_date").reset_index(drop=True)
    detail_df = pd.concat(detail_frames, ignore_index=True)
    limit_df = pd.DataFrame(limit_rows).drop_duplicates(subset=["trade_date"], keep="last")
    new_high_df = calc_new_high_stats(detail_df, NEW_HIGH_LOOKBACK_DAYS)

    history_df = history_df.merge(new_high_df, on="trade_date", how="left")
    history_df = history_df.merge(limit_df, on="trade_date", how="left")
    history_df["new_high_count"] = pd.to_numeric(history_df["new_high_count"], errors="coerce").fillna(0).astype(int)
    return history_df


def fetch_index_history_generic(pro, ts_code: str, start_date: str, end_date: str, close_col_name: str, ma_col_name: str):
    df = pro.index_daily(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields="ts_code,trade_date,close",
    )

    if df is None or df.empty:
        raise RuntimeError(f"index_daily 没有返回 {ts_code} 数据。")

    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).copy()
    df = df.sort_values("trade_date").reset_index(drop=True)

    df[close_col_name] = df["close"].round(2)
    df[ma_col_name] = (
        df[close_col_name]
        .rolling(window=INDEX_MA_WINDOW, min_periods=INDEX_MA_WINDOW)
        .mean()
        .round(2)
    )
    return df[["trade_date", close_col_name, ma_col_name]].copy()


def dataframe_to_html(df: pd.DataFrame):
    styled = df.copy()
    return styled.to_html(index=False, border=0, justify="center")


def send_email(report_df: pd.DataFrame, target_date: str):
    if not SEND_EMAIL:
        print("已跳过邮件发送：SEND_EMAIL=0")
        return

    required = [SMTP_USER, SMTP_PASS, MAIL_TO]
    if not all(required):
        raise ValueError("邮件发送缺少必要环境变量：QQ_SMTP_USER / QQ_SMTP_PASS / MAIL_TO")

    latest = report_df.iloc[-1]
    subject = f"情绪日报 {target_date} 最近{len(report_df)}个交易日"

    html = f"""
    <html>
      <body>
        <p>情绪日报已生成，以下是最近 {len(report_df)} 个交易日：</p>
        <p>
          最新交易日：{latest['交易日期']}<br>
          融资买入额占全A成交额：{latest['融资买入额占全A成交额_pct']}<br>
          过去一年分位数：{latest['过去一年分位数_pct']}<br>
          中证1000危险信号：{latest['危险信号_99分位2天且中证1000连续2天在MA5下方']}<br>
          沪深300危险信号：{latest['危险信号_99分位2天且沪深300连续2天在MA5下方']}
        </p>
        {dataframe_to_html(report_df)}
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    if MAIL_CC:
        msg["Cc"] = MAIL_CC

    msg.attach(MIMEText(html, "html", "utf-8"))

    recipients = [x.strip() for x in (MAIL_TO.split(",") + MAIL_CC.split(",")) if x.strip()]
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, recipients, msg.as_string())

    print("邮件发送完成")


def main():
    if not TUSHARE_TOKEN:
        raise ValueError("请先设置 TUSHARE_TOKEN 环境变量。")

    target_date = TARGET_DATE if TARGET_DATE else get_today_cn_str()
    start_date = calc_start_date(target_date)

    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    margin_hist = fetch_margin_buy_history(pro, start_date, target_date)
    trade_dates = margin_hist["trade_date"].drop_duplicates().sort_values().tolist()
    market_hist = fetch_market_history(pro, trade_dates)
    csi1000_hist = fetch_index_history_generic(
        pro, CSI1000_TS_CODE, start_date, target_date, "中证1000收盘价", "中证1000_MA5"
    )
    csi300_hist = fetch_index_history_generic(
        pro, CSI300_TS_CODE, start_date, target_date, "沪深300收盘价", "沪深300_MA5"
    )

    df = margin_hist.merge(market_hist, on="trade_date", how="inner")
    df = df.merge(csi1000_hist, on="trade_date", how="left")
    df = df.merge(csi300_hist, on="trade_date", how="left")

    df["margin_buy_to_all_a_amount_pct"] = df["margin_buy_amount_yuan"] / df["all_a_amount_yuan"] * 100
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["margin_buy_to_all_a_amount_pct"])
    df = df[df["all_a_amount_yuan"] > 0].copy()
    df = df.sort_values("trade_date").reset_index(drop=True)

    df["past_1y_percentile_pct"] = (
        df["margin_buy_to_all_a_amount_pct"]
        .rolling(window=YEAR_WINDOW_TRADING_DAYS, min_periods=YEAR_WINDOW_TRADING_DAYS)
        .apply(percentile_of_latest, raw=True)
    )

    df = build_extreme_breadth_signal(df, UP_RATIO_THRESHOLD_PCT)

    df["融资买入额_亿元"] = (df["margin_buy_amount_yuan"] / 100000000).round(2)
    df["全A成交额_亿元"] = (df["all_a_amount_yuan"] / 100000000).round(2)
    df["融资买入额占全A成交额_pct"] = df["margin_buy_to_all_a_amount_pct"].round(4)
    df["过去一年分位数_pct"] = df["past_1y_percentile_pct"].round(2)
    df["有成交额股票数量"] = df["stock_count_with_amount"]
    df["上涨家数"] = df["up_count"]
    df["下跌家数"] = df["down_count"]
    df["平盘家数"] = df["flat_count"]
    df["上涨占比_pct"] = pd.to_numeric(df["up_ratio_pct"], errors="coerce").round(2)
    df["下跌占比_pct"] = pd.to_numeric(df["down_ratio_pct"], errors="coerce").round(2)
    df[f"近{NEW_HIGH_LOOKBACK_DAYS}日新高家数"] = df["new_high_count"]
    df["中位数涨跌幅_pct"] = pd.to_numeric(df["median_pct_chg"], errors="coerce").round(2)
    df["涨停家数"] = df["limit_up_count"]
    df["跌停家数"] = df["limit_down_count"]

    df = add_index_columns(df, "中证1000", "中证1000收盘价", "中证1000_MA5")
    df = add_index_columns(df, "沪深300", "沪深300收盘价", "沪深300_MA5")

    output_df = df.dropna(subset=["过去一年分位数_pct"]).copy()
    output_df = build_two_day_risk_signal(output_df)

    history_out = output_df[[
        "trade_date",
        "融资买入额_亿元",
        "全A成交额_亿元",
        "融资买入额占全A成交额_pct",
        "过去一年分位数_pct",
        "有成交额股票数量",
        "上涨家数",
        "下跌家数",
        "平盘家数",
        "上涨占比_pct",
        "下跌占比_pct",
        f"近{NEW_HIGH_LOOKBACK_DAYS}日新高家数",
        "中位数涨跌幅_pct",
        "涨停家数",
        "跌停家数",
        "中证1000收盘价",
        "中证1000_MA5",
        "中证1000是否低于MA5",
        "沪深300收盘价",
        "沪深300_MA5",
        "沪深300是否低于MA5",
        "过去一年分位数是否>=99",
        "危险信号_99分位2天且中证1000连续2天在MA5下方",
        "危险信号_99分位2天且沪深300连续2天在MA5下方",
        "备注",
    ]].copy()

    history_out = history_out.rename(columns={"trade_date": "交易日期"})
    history_out = history_out.tail(RECENT_DAYS).reset_index(drop=True)

    count_cols = [
        "有成交额股票数量",
        "上涨家数",
        "下跌家数",
        "平盘家数",
        f"近{NEW_HIGH_LOOKBACK_DAYS}日新高家数",
        "涨停家数",
        "跌停家数",
    ]
    for col in count_cols:
        history_out[col] = pd.to_numeric(history_out[col], errors="coerce").astype("Int64")

    for col in ["中证1000收盘价", "中证1000_MA5", "沪深300收盘价", "沪深300_MA5"]:
        history_out[col] = pd.to_numeric(history_out[col], errors="coerce").round(2)

    history_out.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print(f"已输出最近 {RECENT_DAYS} 个交易日到：{OUTPUT_FILE}")

    send_email(history_out, target_date)


if __name__ == "__main__":
    main()
