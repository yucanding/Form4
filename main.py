import feedparser
import xml.etree.ElementTree as ET
from curl_cffi import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import random
import yfinance as yf
import os

# --- 配置区 ---
SEC_HEADERS = {
    "User-Agent": "Institutional Alpha Analyst (your-email@example.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml",
    "Host": "www.sec.gov"
}

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&count=100&output=atom"
BUY_THRESHOLD = 1000000  # 100万美元门槛
PRICE_FLOOR = 0.5        # 股价低于0.5美元过滤
STATE_FILE = "processed_ids.txt"

# 从环境变量获取 Telegram 配置
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

def send_tg_message(text):
    """ 推送到 Telegram """
    if not TG_TOKEN or not TG_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"发送失败: {e}")

def get_market_data(ticker):
    try:
        yf_ticker = ticker.replace('.', '-')
        stock = yf.Ticker(yf_ticker)
        info = stock.info
        price = info.get('regularMarketPrice') or info.get('currentPrice')
        mkt_cap = info.get('marketCap')
        return price, mkt_cap
    except:
        return None, None

def format_large_number(num):
    if num is None: return "Unknown"
    if num >= 1_000_000_000: return f"${num / 1_000_000_000:.2f}B"
    return f"${num / 1_000_000:.2f}M"

def get_real_xml_url(index_url):
    try:
        time.sleep(random.uniform(0.2, 0.4))
        resp = requests.get(index_url, headers=SEC_HEADERS, impersonate="chrome120", timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, 'html.parser')
            table = soup.find('table', {'class': 'tableFile', 'summary': 'Document Format Files'})
            if table:
                for row in table.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) > 2:
                        file_name = cells[2].text.strip()
                        if file_name.lower().endswith('.xml') and 'xsd' not in file_name.lower():
                            return f"{index_url.rsplit('/', 1)[0]}/{file_name}"
    except: pass
    return None

def parse_and_aggregate_buys(xml_url, pub_time_raw):
    try:
        time.sleep(random.uniform(0.1, 0.2))
        resp = requests.get(xml_url, headers=SEC_HEADERS, impersonate="chrome120", timeout=15)
        if resp.status_code != 200: return None
        
        root = ET.fromstring(resp.content)
        symbol = root.find(".//issuerTradingSymbol").text
        issuer_name = root.find(".//issuerName").text
        buyer_name = root.find(".//rptOwnerName").text
        
        buy_time_node = root.find(".//periodOfReport")
        buy_time = buy_time_node.text if buy_time_node is not None else "N/A"
        
        off_node = root.find(".//officerTitle")
        is_dir = root.find(".//isDirector").text == '1'
        rel = off_node.text if off_node is not None else ("Director" if is_dir else "10% Owner")

        total_shares = 0
        total_value = 0
        final_owned = 0

        for trans in root.findall(".//nonDerivativeTransaction"):
            if trans.find(".//transactionCode").text == 'P':
                s_node = trans.find(".//transactionShares/value")
                p_node = trans.find(".//transactionPricePerShare/value")
                o_node = trans.find(".//sharesOwnedFollowingTransaction/value")
                
                shares = float(s_node.text) if s_node is not None and s_node.text else 0
                price = float(p_node.text) if p_node is not None and p_node.text else 0
                
                total_shares += shares
                total_value += (shares * price)
                final_owned = float(o_node.text) if o_node is not None and o_node.text else final_owned

        if total_value < BUY_THRESHOLD: return None

        curr_price, market_cap = get_market_data(symbol)
        if curr_price is not None and curr_price < PRICE_FLOOR:
            return None 

        shares_before = final_owned - total_shares
        if shares_before > 0:
            pos_change_pct = (total_shares / shares_before) * 100
            if pos_change_pct < 5:
                return None
            pos_change_str = f"+{pos_change_pct:.2f}%"
        else:
            pos_change_str = "首次建仓"

        mkt_impact_str = "N/A"
        if market_cap:
            mkt_impact = (total_value / market_cap) * 100
            mkt_impact_str = f"{mkt_impact:.4f}%"

        base_url, file_name = xml_url.rsplit('/', 1)
        view_url = f"{base_url}/xslF345X05/{file_name}"

        try:
            dt = datetime.fromisoformat(pub_time_raw.replace('Z', '+00:00'))
            pub_time_fmt = dt.strftime("%Y-%m-%d %H:%M:%S") + " ET"
        except: pub_time_fmt = pub_time_raw

        # 修改为 HTML 格式
        output = (
            f"🕒 发布时间: {pub_time_fmt}\n"
            f"📅 购买时间: {buy_time}\n"
            f"🏢 公司: <b>${symbol}</b> ({issuer_name})\n"
            f"💰 买入金额: <b>${total_value:,.2f}</b> ({pos_change_str})\n"
            f"👤 人名: {buyer_name} ({rel})\n"
            f"💵 股价: ${curr_price if curr_price else 'N/A'}\n"
            f"🌊 占市值比: {mkt_impact_str}\n"
            f"📊 买入股数: {total_shares:,.0f}股\n"
            f"🏛️ 市值: {format_large_number(market_cap)}\n"
            f"🔗 <a href='{view_url}'>点击查看公告</a>\n"
        )
        return output
    except: return None

def run():
    # 读取已处理的 ID
    processed_ids = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            processed_ids = set(f.read().splitlines())

    print(f"📡 正在开启深度分析...")
    
    try:
        resp = requests.get(FEED_URL, headers=SEC_HEADERS, impersonate="chrome120", timeout=30)
        feed = feedparser.parse(resp.content)
        header_printed = False 
        new_ids = []

        for entry in feed.entries:
            if entry.category != '4': continue
            acc_no = entry.id.split('=')[-1]
            
            # 去重判断
            if acc_no in processed_ids: continue

            real_xml_url = get_real_xml_url(entry.link)
            if real_xml_url:
                msg = parse_and_aggregate_buys(real_xml_url, entry.updated)
                if msg:
                    if not header_printed:
                        # 仅在有符合条件的信号时，推送到 TG 的消息头部
                        send_tg_message("<b>🔔 内部人士买入警报</b>")
                        header_printed = True
                    
                    send_tg_message(msg)
                    new_ids.append(acc_no)
        
        # 更新去重文件（仅保留最新的 500 个 ID 防止文件无限增大）
        updated_ids = list(new_ids) + list(processed_ids)
        with open(STATE_FILE, "w") as f:
            f.write("\n".join(updated_ids[:500]))

    except Exception as e:
        print(f"\n🚨 运行异常: {e}")

if __name__ == "__main__":
    run()
