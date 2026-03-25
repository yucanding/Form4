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
PRICE_FLOOR = 20        # 股价低于20美元过滤
STATE_FILE = "processed_ids.txt"

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_IDS = os.getenv("TG_CHAT_ID", "").split(",")

def send_tg_message(text):
    """ 推送到 Telegram 目标 """
    if not TG_TOKEN or not TG_CHAT_IDS:
        print(text)
        return
    for chat_id in TG_CHAT_IDS:
        chat_id = chat_id.strip()
        if not chat_id: continue
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"发送到 {chat_id} 失败: {e}")

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
        # 礼貌性随机延迟，避免被 SEC 封锁
        time.sleep(random.uniform(0.1, 0.2))
        resp = requests.get(xml_url, headers=SEC_HEADERS, impersonate="chrome120", timeout=15)
        if resp.status_code != 200: return None
        
        root = ET.fromstring(resp.content)
        
        # --- 基础信息解析 ---
        symbol = root.find(".//issuerTradingSymbol").text
        issuer_name = root.find(".//issuerName").text
        buyer_name = root.find(".//rptOwnerName").text
        
        buy_time_node = root.find(".//periodOfReport")
        buy_time = buy_time_node.text if buy_time_node is not None else "N/A"
        
        # 身份解析
        off_node = root.find(".//officerTitle")
        is_dir_node = root.find(".//isDirector")
        is_dir = is_dir_node.text == '1' if is_dir_node is not None else False
        rel = off_node.text if off_node is not None else ("Director" if is_dir else "10% Owner")

        # --- 交易数据聚合 ---
        total_shares, total_value, final_owned = 0, 0, 0

        for trans in root.findall(".//nonDerivativeTransaction"):
            # 核心过滤：只统计 Code P (Open Market Purchase)
            t_code = trans.find(".//transactionCode")
            if t_code is not None and t_code.text == '1' or t_code.text == 'P':
                s_node = trans.find(".//transactionShares/value")
                p_node = trans.find(".//transactionPricePerShare/value")
                o_node = trans.find(".//sharesOwnedFollowingTransaction/value")
                
                shares = float(s_node.text) if s_node is not None and s_node.text else 0
                price = float(p_node.text) if p_node is not None and p_node.text else 0
                
                total_shares += shares
                total_value += (shares * price)
                # 记录交易后的最终持股数（以最后一笔交易为准）
                if o_node is not None and o_node.text:
                    final_owned = float(o_node.text)

        # --- 新增：计算平均买入价 ---
        avg_buy_price = total_value / total_shares if total_shares > 0 else 0

        # --- 过滤逻辑 1: 买入金额门槛 ---
        if total_value < BUY_THRESHOLD: return None

        # --- 获取市场数据 ---
        curr_price, market_cap = get_market_data(symbol)
        if curr_price is None or market_cap is None:
            return None
        
        # --- 过滤逻辑 2: 股价地板 ---
        if curr_price < PRICE_FLOOR: return None 

        # --- 仓位变动计算 ---
        shares_before = final_owned - total_shares
        if shares_before > 0:
            pos_change_pct = (total_shares / shares_before) * 100
            # --- 过滤逻辑 3: 增持比例必须 >= 20% ---
            if pos_change_pct < 20 and total_value < 100000000: return None
            pos_change_str = f"+{pos_change_pct:.2f}%"
        elif shares_before == 0 and total_value > 50000000:
            pos_change_str = "新建仓位"
        else:
            # 根据原逻辑，若为首次建仓或数据异常，返回 None
            return None

        # --- 市值影响计算 ---
        mkt_impact_str = "N/A"
        if market_cap:
            mkt_impact = (total_value / market_cap) * 100
            mkt_impact_str = f"{mkt_impact:.4f}%"

        # --- 链接生成 ---
        # 构造可直接阅读的 HTML 版本 URL
        view_url = f"{xml_url.rsplit('/', 1)[0]}/xslF345X05/{xml_url.rsplit('/', 1)[1]}"

        # --- 时间格式化 ---
        try:
            pub_time_fmt = datetime.fromisoformat(pub_time_raw.replace('Z', '+00:00')).strftime("%Y-%m-%d %H:%M:%S") + " ET"
        except: 
            pub_time_fmt = pub_time_raw

        # --- 最终输出文本组装 ---
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
        return output
    except Exception as e:
        # 调试用：print(f"解析 XML 出错: {e}")
        return None

def run():
    # 读取历史 ID
    processed_ids = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            processed_ids = set(f.read().splitlines())

    print(f"📡 正在开启深度分析...")
    
    try:
        resp = requests.get(FEED_URL, headers=SEC_HEADERS, impersonate="chrome120", timeout=30)
        feed = feedparser.parse(resp.content)
        
        # 本次运行抓到的新消息队列和新 ID
        message_queue = []
        new_ids = []
        
        # 建立一个本地临时 set 防止本次扫描中 Reporting/Issuer 重复处理
        current_run_seen_acc_nos = set()

        for entry in feed.entries:
            if entry.category != '4': continue
            
            # 1. 唯一性去重：提取 AccNo
            acc_no = entry.id.split('=')[-1]
            
            # 如果是历史发过的，或者是本次循环中已经处理过的（解决公司/个人双条目问题），直接跳过
            if acc_no in processed_ids or acc_no in current_run_seen_acc_nos:
                continue

            real_xml_url = get_real_xml_url(entry.link)
            if real_xml_url:
                msg = parse_and_aggregate_buys(real_xml_url, entry.updated)
                if msg:
                    message_queue.append(msg)
                    new_ids.append(acc_no)
                    current_run_seen_acc_nos.add(acc_no)
                    print(f"✅ 捕获有效信号: {acc_no}")
        
        # --- 合并推送逻辑 ---
        if message_queue:
            # 1. 组装标题
            final_text = "<b>🔔内部人士买入警报</b>\n\n"
            # 2. 将横线作为“连接符”放在信息之间，而不是放在开头
            final_text += ("\n\n" + "-" * 20 + "\n\n").join(message_queue)
            # 3. 加上结尾标签
            final_text += "\n\n#InsiderTrading #Form4"
            
            send_tg_message(final_text)

        # 保存状态
        updated_ids = list(new_ids) + list(processed_ids)
        with open(STATE_FILE, "w") as f:
            f.write("\n".join(updated_ids[:1000]))

        print(f"✅ 扫描结束。本次新增推送: {len(new_ids)}")

    except Exception as e:
        print(f"\n🚨 运行异常: {e}")

if __name__ == "__main__":
    run()
