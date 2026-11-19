
import feedparser
import xml.etree.ElementTree as ET
from curl_cffi import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from curl_cffi.requests.exceptions import Timeout, RequestException
import time
import random
import yfinance as yf
import os
import re
import json

# --- 配置区 ---
SEC_HEADERS = {
    "User-Agent": "Institutional Alpha Analyst (kafkahf@gmail.com)",
    "Accept": "application/atom+xml, application/xml, text/xml, */*"
}

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&count=100&output=atom"
BUY_THRESHOLD = 1000000
PRICE_FLOOR = 10
MKT_CAP_FLOOR = 1_000_000_000
STATE_FILE = "processed_ids.txt"
PENDING_FILE = "pending_telegram.json"

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_IDS = os.getenv("TG_CHAT_ID", "").split(",")


# ============================================================
# Telegram 消息队列
# ============================================================

def load_pending_messages():
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Telegram 队列读取失败: {e}")
        return []


def save_pending_messages(queue):
    temp_file = PENDING_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, PENDING_FILE)


def flush_pending_messages():
    queue = load_pending_messages()
    if not queue:
        return

    current_ids = [cid.strip() for cid in TG_CHAT_IDS if cid.strip()]
    if not TG_TOKEN or not current_ids:
        print("⚠️ Telegram 配置缺失，消息保留待下次重试")
        return

    for item in queue:
        if "chat_ids" not in item:
            item["chat_ids"] = current_ids.copy()

        targets = item["chat_ids"][:]
        if not targets:
            continue

        remaining = []
        for i, chat_id in enumerate(targets):
            try:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
                payload = {"chat_id": chat_id, "text": item["text"], "parse_mode": "HTML", "disable_web_page_preview": False}
                resp = requests.post(url, json=payload, timeout=15)
                resp.raise_for_status()
                result = resp.json()
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram API 返回错误: {result}")
                print(f"✅ Telegram 推送成功: {chat_id}")
            except Exception as e:
                print(f"❌ Telegram 推送失败 [{chat_id}]: {e}")
                remaining.append(chat_id)

            item["chat_ids"] = remaining + targets[i + 1:]
            save_pending_messages(queue)

    queue = [item for item in queue if item.get("chat_ids")]
    save_pending_messages(queue)

    if queue:
        print(f"⚠️ 仍有 {len(queue)} 条消息未全部发送，已保存待下次重试")
    else:
        print("✅ 所有 Telegram 待发送消息已处理完成")


def send_tg_message(text):
    queue = load_pending_messages()
    chat_ids = [cid.strip() for cid in TG_CHAT_IDS if cid.strip()]
    queue.append({"text": text, "chat_ids": chat_ids})
    save_pending_messages(queue)
    flush_pending_messages()


# ============================================================
# 市场数据
# ============================================================

def get_market_data(ticker):
    try:
        yf_ticker = ticker.replace(".", "-")
        stock = yf.Ticker(yf_ticker)
        info = stock.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        mkt_cap = info.get("marketCap")
        return price, mkt_cap
    except Exception as e:
        print(f"⚠️ Yahoo Finance 查询失败 [{ticker}]: {e}")
        return None, None


def format_large_number(num):
    if num is None:
        return "Unknown"
    if num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    return f"${num / 1_000_000:.2f}M"


# ============================================================
# StockTitan Offering 过滤
# ============================================================

def check_stock_titan_offering(symbol):
    """检查过去10天内 StockTitan RSS 新闻标题是否包含 offering。"""
    try:
        rss_url = f"https://www.stocktitan.net/rss/news/{symbol}"
        resp = requests.get(rss_url, timeout=10)
        if resp.status_code != 200:
            return False

        feed = feedparser.parse(resp.content)
        now = datetime.now(timezone.utc)
        ten_days_ago = now - timedelta(days=10)

        for entry in feed.entries:
            pub_date = getattr(entry, "published_parsed", None)
            if pub_date:
                pub_dt = datetime.fromtimestamp(time.mktime(pub_date), tz=timezone.utc)
                if pub_dt >= ten_days_ago:
                    title = entry.get("title", "")
                    if re.search(r"\boffering\b", title, re.IGNORECASE):
                        print(f"🚫 拦截 [{symbol}]：发现10天内新闻标题包含 offering -> {title}")
                        return True
        return False
    except Exception as e:
        print(f"⚠️ 检查 StockTitan RSS 失败 [{symbol}]: {e}")
        return False


# ============================================================
# SEC 网络请求
# ============================================================

def safe_get(url, headers=None, timeout=45, max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers or SEC_HEADERS, impersonate="chrome120", timeout=timeout)
            resp.raise_for_status()
            return resp
        except (Timeout, RequestException) as e:
            if attempt == max_retries - 1:
                print(f"❌ 请求最终失败 [{url}]: {e}")
                raise
            wait = min(3 * (2 ** attempt), 48) + random.uniform(0.5, 2)
            print(f"⚠️ 第 {attempt + 1}/{max_retries} 次请求失败: {e}")
            print(f"⏳ {wait:.1f} 秒后重试...")
            time.sleep(wait)
    return None


# ============================================================
# XML 非法字符清理
# ============================================================

def sanitize_xml_content(content):
    """清理 XML 1.0 不允许的控制字符及无效 UTF-8 字节。"""
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = str(content)

    cleaned = "".join(
        char for char in text
        if ord(char) in (0x09, 0x0A, 0x0D)
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
    )

    removed = len(text) - len(cleaned)
    if removed:
        print(f"🧹 已清理 {removed} 个 XML 非法字符")

    return cleaned


# ============================================================
# SEC RSS 获取及解析（本次主要修改）
# ============================================================

def fetch_sec_feed(max_attempts=3):
    last_error = None
    last_feed = None

    for attempt in range(max_attempts):
        try:
            print(f"📡 获取 SEC RSS [{attempt + 1}/{max_attempts}]")

            resp = safe_get(FEED_URL, timeout=45)
            content = resp.content

            if not content:
                raise RuntimeError("SEC RSS 返回空内容")

            content_start = content.lstrip()[:500].lower()
            if b"<!doctype html" in content_start or b"<html" in content_start:
                raise RuntimeError("SEC 返回 HTML 页面，而不是 RSS XML")

            # 首先尝试直接解析原始 RSS
            feed = feedparser.parse(content)
            last_feed = feed

            if not feed.bozo and feed.entries:
                print(f"✅ SEC RSS 解析成功，共 {len(feed.entries)} 条记录")
                return feed

            if feed.bozo:
                last_error = feed.bozo_exception
                print(f"⚠️ SEC RSS XML 格式异常: {last_error}")

                # 输出 XML 异常位置及附近内容
                try:
                    line = last_error.getLineNumber() if hasattr(last_error, "getLineNumber") else None
                    column = last_error.getColumnNumber() if hasattr(last_error, "getColumnNumber") else None
                    if line:
                        lines = content.decode("utf-8", errors="replace").splitlines()
                        if 1 <= line <= len(lines):
                            print(f"🔍 XML 异常位置：第 {line} 行，第 {column} 列")
                            print(f"🔍 原始内容：{lines[line - 1][:500]}")
                except Exception:
                    pass
            else:
                last_error = "SEC RSS 解析成功但没有 entries"

            # 尝试清理非法字符后重新解析
            print("🔧 尝试清理 XML 非法字符并重新解析...")
            cleaned_content = sanitize_xml_content(content)
            cleaned_feed = feedparser.parse(cleaned_content)

            if not cleaned_feed.bozo and cleaned_feed.entries:
                print(f"✅ XML 清理后解析成功，共 {len(cleaned_feed.entries)} 条记录")
                return cleaned_feed

            if cleaned_feed.entries:
                print(f"⚠️ 清理后仍存在 XML 格式问题，但已解析出 {len(cleaned_feed.entries)} 条记录")
                last_feed = cleaned_feed
            else:
                last_error = cleaned_feed.bozo_exception
                print(f"⚠️ XML 清理后仍无法获得有效记录: {last_error}")

            if attempt < max_attempts - 1:
                wait = random.uniform(3, 6)
                print(f"⏳ RSS 解析异常，{wait:.1f} 秒后重新请求...")
                time.sleep(wait)

        except (Timeout, RequestException):
            raise
        except Exception as e:
            last_error = e
            print(f"⚠️ SEC RSS 获取/解析异常: {e}")
            if attempt < max_attempts - 1:
                time.sleep(random.uniform(3, 6))

    # 多次尝试后仍有部分有效数据，则继续处理已解析记录
    if last_feed is not None and last_feed.entries:
        print(f"⚠️ SEC RSS 多次解析异常，但仍获得 {len(last_feed.entries)} 条记录，继续处理已解析记录")
        return last_feed

    raise RuntimeError(f"SEC RSS 最终无法解析: {last_error}")


# ============================================================
# 获取 SEC Form 4 XML 地址
# ============================================================

def get_real_xml_url(index_url):
    try:
        time.sleep(random.uniform(0.2, 0.4))
        resp = safe_get(index_url, timeout=20)

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            table = soup.find("table", {"class": "tableFile", "summary": "Document Format Files"})

            if table:
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) > 2:
                        file_name = cells[2].text.strip()
                        if file_name.lower().endswith(".xml") and "xsd" not in file_name.lower():
                            return f"{index_url.rsplit('/', 1)[0]}/{file_name}"

    except Exception as e:
        print(f"⚠️ SEC XML URL 获取失败 [{index_url}]: {e}")

    return None


# ============================================================
# Form 4 买入数据解析
# ============================================================

def parse_and_aggregate_buys(xml_url, pub_time_raw):
    try:
        time.sleep(random.uniform(0.1, 0.2))
        resp = safe_get(xml_url, timeout=20)
        if resp.status_code != 200:
            return None

        root = ET.fromstring(resp.content)

        # 基础信息
        symbol_node = root.find(".//issuerTradingSymbol")
        issuer_node = root.find(".//issuerName")
        buyer_node = root.find(".//rptOwnerName")

        symbol = symbol_node.text.strip() if symbol_node is not None and symbol_node.text else None
        issuer_name = issuer_node.text.strip() if issuer_node is not None and issuer_node.text else "Unknown"
        buyer_name = buyer_node.text.strip() if buyer_node is not None and buyer_node.text else "Unknown"

        if not symbol:
            return None

        buy_time_node = root.find(".//periodOfReport")
        buy_time = buy_time_node.text if buy_time_node is not None else "N/A"

        try:
            if buy_time != "N/A":
                pub_dt = datetime.fromisoformat(pub_time_raw.replace("Z", "+00:00"))
                buy_dt = datetime.strptime(buy_time, "%Y-%m-%d").replace(tzinfo=pub_dt.tzinfo)
                if (pub_dt - buy_dt).days > 5:
                    return None
        except Exception as e:
            print(f"⚠️ 时间解析失败: {e}")

        # 身份解析
        off_node = root.find(".//officerTitle")
        is_dir_node = root.find(".//isDirector")
        is_dir = is_dir_node.text == "1" if is_dir_node is not None else False
        rel = off_node.text if off_node is not None and off_node.text else ("Director" if is_dir else "10% Owner")

        # 交易数据聚合
        total_shares, total_value, final_owned = 0, 0, 0

        for trans in root.findall(".//nonDerivativeTransaction"):
            t_code = trans.find(".//transactionCode")
            if t_code is not None and t_code.text in ["1", "P"]:
                s_node = trans.find(".//transactionShares/value")
                p_node = trans.find(".//transactionPricePerShare/value")
                o_node = trans.find(".//sharesOwnedFollowingTransaction/value")

                shares = float(s_node.text) if s_node is not None and s_node.text else 0
                price = float(p_node.text) if p_node is not None and p_node.text else 0

                total_shares += shares
                total_value += shares * price

                if o_node is not None and o_node.text:
                    final_owned = float(o_node.text)

        avg_buy_price = total_value / total_shares if total_shares > 0 else 0

        # 过滤逻辑 1：买入金额
        if total_value < BUY_THRESHOLD:
            return None

        # 获取市场数据
        curr_price, market_cap = get_market_data(symbol)

        # 过滤逻辑 2：股价及市值
        if curr_price is not None and curr_price < PRICE_FLOOR:
            return None
        if curr_price is None or market_cap is None:
            return None
        if market_cap < MKT_CAP_FLOOR:
            return None

        # StockTitan Offering 过滤
        if check_stock_titan_offering(symbol):
            return None

        # 仓位变化计算
        shares_before = final_owned - total_shares

        if shares_before > 0:
            pos_change_pct = (total_shares / shares_before) * 100
            if pos_change_pct < 3 and total_value < 10000000:
                return None
            pos_change_str = f"+{pos_change_pct:.2f}%"
        elif shares_before == 0 and total_value > 50000000:
            pos_change_str = "新建仓位"
        else:
            return None

        # 市值影响
        mkt_impact_str = "N/A"
        if market_cap:
            mkt_impact = (total_value / market_cap) * 100
            mkt_impact_str = f"{mkt_impact:.4f}%"

        # SEC 公告链接
        view_url = f"{xml_url.rsplit('/', 1)[0]}/xslF345X05/{xml_url.rsplit('/', 1)[1]}"

        # 时间格式
        try:
            pub_time_fmt = datetime.fromisoformat(pub_time_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S") + " ET"
        except Exception:
            pub_time_fmt = pub_time_raw

        trade_signature = f"{symbol}_{buy_time}_{total_shares}_{total_value:.2f}"

        output = (
            f"🕒 发布时间: {pub_time_fmt}\n"
            f"📅 购买时间: {buy_time}\n"
            f"🏢 公司: ${symbol} ({issuer_name})\n"
            f"💰 买入总额: ${total_value:,.2f} ({pos_change_str})\n"
            f"📊 买入股数: {total_shares:,.0f}股\n"
            f"💵 平均买入价: ${avg_buy_price:.2f}\n"
            f"🌊 占市值比: {mkt_impact_str}\n"
            f"👤 人名: {buyer_name} ({rel})\n"
            f"💵 当前股价: ${curr_price if curr_price else 'N/A'}\n"
            f"🏛️ 市值: {format_large_number(market_cap)}\n"
            f"🔗 <a href='{view_url}'>点击查看公告</a>"
        )

        return output, trade_signature

    except Exception as e:
        print(f"⚠️ Form 4 解析失败 [{xml_url}]: {e}")
        return None


# ============================================================
# 主程序
# ============================================================

def run():
    processed_ids = set()

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            processed_ids = set(f.read().splitlines())

    print("📡 正在开启深度分析...")
    seen_trades_this_run = set()

    try:
        # 获取 SEC RSS
        try:
            feed = fetch_sec_feed()
        except (Timeout, RequestException) as e:
            print(f"⏭️ SEC RSS 请求最终失败，本次扫描跳过，不更新已处理记录: {e}")
            return
        except Exception as e:
            print(f"⏭️ SEC RSS 解析最终失败，本次扫描跳过，不更新已处理记录: {e}")
            return

        message_queue = []
        new_ids = []
        current_run_seen_acc_nos = set()

        # 遍历 RSS
        for entry in feed.entries:
            if entry.get("category") != "4":
                continue

            acc_no = entry.id.split("=")[-1]

            if acc_no in processed_ids or acc_no in current_run_seen_acc_nos:
                continue

            real_xml_url = get_real_xml_url(entry.link)

            if real_xml_url:
                result = parse_and_aggregate_buys(real_xml_url, entry.updated)

                if result:
                    msg, sig = result

                    if sig in seen_trades_this_run:
                        current_run_seen_acc_nos.add(acc_no)
                        new_ids.append(acc_no)
                        continue

                    seen_trades_this_run.add(sig)
                    message_queue.append(msg)
                    new_ids.append(acc_no)
                    current_run_seen_acc_nos.add(acc_no)

                    print(f"✅ 捕获有效信号: {acc_no}")

        # Telegram 推送
        if message_queue:
            final_text = "<b>🔔内部人士买入警报</b>\n\n"
            final_text += ("\n\n" + "-" * 20 + "\n\n").join(message_queue)
            final_text += "\n\n#InsiderTrading #Form4"
            send_tg_message(final_text)
        else:
            flush_pending_messages()

        # 更新已处理记录
        updated_ids = list(new_ids) + list(processed_ids)

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(updated_ids[:1000]))

        print(f"✅ 扫描结束。本次新增推送: {len(new_ids)}")

    except Exception as e:
        # 异常只记录在 GitHub Actions 日志，不发送 Telegram
        print(f"\n🚨 运行异常: {e}")
        raise


if __name__ == "__main__":
    run()
