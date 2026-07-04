"""
RSI Swing 波段信号通知（日线）
==============================
基于 RSI Swing 指标，日线级别波段买卖信号。

策略逻辑（来自 Pine Script RSI Swing Indicator）：
- RSI >= 70 超买，标记高点（HH=更高高点 / LH=更低高点）
- RSI <= 30 超卖，标记低点（HL=更高低点 / LL=更低低点）
- HL → 买入信号 🟢
- LH → 卖出信号 🔴
- HH/LL 不产生交易信号，只标记结构

币种：BTC, ETH, NEAR, WLD
周期：日线（1d）
数据源：AICoin v3 API（免费版）
通知：钉钉机器人
检查频率：每小时检查一次（日线收盘后推送）

运行方式（Mac 终端）：
  pip3 install requests numpy
  python3 rsi_swing_signal.py

按 Ctrl+C 停止
"""

import numpy as np
import requests
import json
import time
import hmac
import hashlib
import base64
import random
from datetime import datetime, timezone, timedelta

# ============================================================
#  配置
# ============================================================
# AICoin API（免费版）
AICOIN_KEY    = "sUKjMn3ko5DHWagDKc5XLmKg8zIhWc4A"
AICOIN_SECRET = "iizFDMxWQNJj9ma48RaFpmeiGf4hJINR"
AICOIN_BASE   = "https://open.aicoin.com"

# 钉钉机器人
DINGTALK_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=36df93460e83f02f0530386d1e30544a661740619d073e2990ef5cc9e480cac0'

# 币种配置
SYMBOLS = [
    {'coin_key': 'bitcoin',   'name': 'BTC',  'market': 'okx'},
    {'coin_key': 'ethereum',  'name': 'ETH',  'market': 'okx'},
    {'coin_key': 'near',      'name': 'NEAR', 'market': 'okx'},
    {'coin_key': 'trump',     'name': 'TRUMP', 'market': 'okx'},
    {'coin_key': 'ena',       'name': 'ENA',   'market': 'okx'},
    {'coin_key': 'icp',       'name': 'ICP',   'market': 'okx'},
    {'coin_key': 'filecoin',  'name': 'FIL',   'market': 'okx'},
]

# RSI 参数（和 Pine Script 一致）
RSI_LENGTH     = 7
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# Pivot Points 参数（LuxAlgo Pivot Points High Low）
PIVOT_LENGTH   = 50   # Pivot 回溯长度（日线图约2个月）

# 检查间隔（秒）
CHECK_INTERVAL = 3600  # 每小时检查一次


# ============================================================
#  AICoin API
# ============================================================
def aicoin_headers():
    """生成 AICoin v3 API 认证 headers"""
    nonce = ''.join(random.choices('0123456789abcdef', k=16))
    ts = str(int(time.time()))
    sign_str = f'AccessKeyId={AICOIN_KEY}&SignatureNonce={nonce}&Timestamp={ts}'
    mac = hmac.new(AICOIN_SECRET.encode(), sign_str.encode(), hashlib.sha1)
    signature = base64.b64encode(mac.hexdigest().encode()).decode()
    return {
        'X-Aic-AccessKey-Id': AICOIN_KEY,
        'X-Aic-Signature-Nonce': nonce,
        'X-Aic-Timestamp': ts,
        'X-Aic-Signature': signature,
    }


def fetch_klines(coin_key, market='okx', interval='1d', limit=300):
    """从 AICoin 获取 K 线数据"""
    url = f'{AICOIN_BASE}/api/v3/market/klines'
    params = {
        'coin_key': coin_key,
        'market': market,
        'quote_coin_key': 'usdt',
        'contract_type': 'perpetual',
        'interval': interval,
        'limit': str(limit),
    }
    try:
        r = requests.get(url, headers=aicoin_headers(), params=params, timeout=15)
        data = r.json()
        if data.get('ok') and data.get('data', {}).get('candles'):
            candles = data['data']['candles']
            # 转成 [time, open, high, low, close, volume]
            klines = []
            for c in candles:
                klines.append([c['time'], c['open'], c['high'], c['low'], c['close'], c['volume']])
            return klines
        else:
            print(f'  ✗ {coin_key} K线获取失败: {data.get("error", {}).get("message", data)}')
            return []
    except Exception as e:
        print(f'  ✗ {coin_key} 请求失败: {e}')
        return []


# ============================================================
#  RSI 计算
# ============================================================
def calc_rsi(closes, length=7):
    """计算 RSI（和 TradingView RSI 一致）"""
    if len(closes) < length + 1:
        return np.array([])

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # RMA（Wilder's smoothed moving average）
    avg_gain = np.zeros(len(deltas))
    avg_loss = np.zeros(len(deltas))
    avg_gain[0] = np.mean(gains[:length])
    avg_loss[0] = np.mean(losses[:length])

    for i in range(1, len(deltas)):
        avg_gain[i] = (avg_gain[i-1] * (length - 1) + gains[i]) / length
        avg_loss[i] = (avg_loss[i-1] * (length - 1) + losses[i]) / length

    rs = np.where(avg_loss == 0, 100.0, avg_gain / avg_loss)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # 前 length 个无效
    rsi[:length] = np.nan
    return rsi


# ============================================================
#  RSI Swing 信号检测（完全按 Pine Script 逻辑）
# ============================================================
def detect_rsi_swing(klines):
    """
    检测 RSI Swing 信号

    Pine Script 逻辑：
    - RSI >= overbought → 超买状态
    - RSI <= oversold → 超卖状态
    - 从超卖转到超买：标记高点（HH 或 LH）
    - 从超买转到超卖：标记低点（HL 或 LL）
    - HH: 当前高点 > 上一个高点
    - LH: 当前高点 <= 上一个高点
    - HL: 当前低点 > 上一个低点
    - LL: 当前低点 <= 上一个低点

    返回:
        signals: [(type, price, date_str), ...]  最近的信号
        last_structure: 最新的结构标签
        rsi_now: 当前 RSI 值
        price_now: 当前价格
    """
    if len(klines) < RSI_LENGTH + 10:
        return [], None, 0, 0

    high  = np.array([k[2] for k in klines])
    low   = np.array([k[3] for k in klines])
    close = np.array([k[4] for k in klines])

    rsi = calc_rsi(close, RSI_LENGTH)
    if len(rsi) == 0:
        return [], None, 0, close[-1]

    is_ob = rsi >= RSI_OVERBOUGHT
    is_os = rsi <= RSI_OVERSOLD

    # Pine Script 状态机
    last_state = 0  # 0=初始化, 1=超买, 2=超卖
    hh = low[0]     # 上次超买以来的最高价
    ll = high[0]    # 上次超卖以来的最低价

    last_hh_price = 0.0  # 上一个标记的高点价格
    last_ll_price = 0.0  # 上一个标记的低点价格

    signals = []

    for i in range(1, len(rsi)):
        if np.isnan(rsi[i]):
            continue

        # 从超卖转到超买 → 标记高点
        if last_state == 2 and is_ob[i]:
            hh = high[i]
            # 判断 HH 还是 LH
            if last_hh_price > 0 and hh > last_hh_price:
                label = 'HH'
            else:
                label = 'LH'

            date_str = datetime.fromtimestamp(
                klines[i][0] / 1000, tz=timezone(timedelta(hours=8))
            ).strftime('%Y-%m-%d')

            signals.append((label, hh, date_str))

            # 更新上一个低点价格（用于下次 HL/LL 判断）
            last_ll_price = ll
            last_hh_price = hh
            last_state = 1

        # 从超买转到超卖 → 标记低点
        elif last_state == 1 and is_os[i]:
            ll = low[i]
            # 判断 HL 还是 LL
            if last_ll_price > 0 and ll > last_ll_price:
                label = 'HL'
            else:
                label = 'LL'

            date_str = datetime.fromtimestamp(
                klines[i][0] / 1000, tz=timezone(timedelta(hours=8))
            ).strftime('%Y-%m-%d')

            signals.append((label, ll, date_str))

            # 更新上一个高点价格（用于下次 HH/LH 判断）
            last_hh_price = hh
            last_ll_price = ll
            last_state = 2

        # 超买中：跟踪最高价
        if is_ob[i]:
            if high[i] >= hh:
                hh = high[i]
            last_state = 1

        # 超卖中：跟踪最低价
        if is_os[i]:
            if low[i] <= ll:
                ll = low[i]
            last_state = 2

    rsi_now = rsi[-1] if not np.isnan(rsi[-1]) else 50.0
    last_structure = signals[-1][0] if signals else None

    return signals, last_structure, rsi_now, close[-1]


# ============================================================
#  Pivot Points 信号检测（LuxAlgo Pivot Points High Low）
# ============================================================
def detect_pivot_points(klines, length=50):
    """
    检测 Pivot Points 信号

    Pine Script 逻辑：
    - ta.pivothigh(length, length): 前 length 根和后 length 根都低于中间点 → 高点
    - ta.pivotlow(length, length): 前 length 根和后 length 根都高于中间点 → 低点

    信号：
    - Pivot High (▼) → 卖出信号
    - Pivot Low  (▲) → 买入信号

    返回:
        pivot_signals: [(type, price, date_str, bar_index), ...]
        latest_pivot: 最近的 pivot (type, price, date_str) 或 None
    """
    if len(klines) < length * 2 + 1:
        return [], None

    high  = np.array([k[2] for k in klines])
    low   = np.array([k[3] for k in klines])
    n = len(high)

    pivot_signals = []

    # Pine Script 的 pivothigh/pivotlow:
    # 在 bar i 处，检查 high[i] 是否是 [i-length, i+length] 范围内的最高点
    # 注意：pivothigh 在 i+length 时刻才能确认（需要后面 length 根 K 线）
    for i in range(length, n - length):
        # Pivot High: high[i] 是 [i-length, i+length] 范围内最高
        window_high = high[i - length: i + length + 1]
        if high[i] == np.max(window_high) and high[i] > 0:
            # 确认这个高点不是平顶（唯一最高）
            count_max = np.sum(window_high == high[i])
            if count_max == 1:
                date_str = datetime.fromtimestamp(
                    klines[i][0] / 1000, tz=timezone(timedelta(hours=8))
                ).strftime('%Y-%m-%d')
                pivot_signals.append(('PH', high[i], date_str, i))

        # Pivot Low: low[i] 是 [i-length, i+length] 范围内最低
        window_low = low[i - length: i + length + 1]
        if low[i] == np.min(window_low) and low[i] > 0:
            count_min = np.sum(window_low == low[i])
            if count_min == 1:
                date_str = datetime.fromtimestamp(
                    klines[i][0] / 1000, tz=timezone(timedelta(hours=8))
                ).strftime('%Y-%m-%d')
                pivot_signals.append(('PL', low[i], date_str, i))

    latest = pivot_signals[-1] if pivot_signals else None
    return pivot_signals, latest


# ============================================================
#  钉钉通知
# ============================================================
def send_dingtalk(title, text):
    """推送到钉钉，确保包含关键词"""
    # 钉钉关键词要求：BTC, Pivot, 策略
    if not all(k in title or k in text for k in ['BTC', 'Pivot', '策略']):
        text = f'BTC Pivot 策略\n{text}'
    payload = {'msgtype': 'markdown', 'markdown': {'title': title, 'text': text}}
    try:
        r = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
        result = r.json()
        if result.get('errcode') == 0:
            print(f'  ✓ 钉钉已推送: {title}')
        else:
            print(f'  ✗ 钉钉错误: {result}')
    except Exception as e:
        print(f'  ✗ 钉钉失败: {e}')


def fmt_signal(name, signal_type, price, rsi, date_str, extra=''):
    """格式化信号消息"""
    if signal_type == 'HL':
        emoji = '🟢 买入信号'
        desc = '更高低点（HL），趋势可能向上反转'
    elif signal_type == 'LH':
        emoji = '🔴 卖出信号'
        desc = '更低高点（LH），趋势可能向下反转'
    elif signal_type == 'HH':
        emoji = '📊 结构标记'
        desc = '更高高点（HH），上涨趋势延续'
    elif signal_type == 'LL':
        emoji = '📊 结构标记'
        desc = '更低低点（LL），下跌趋势延续'
    else:
        emoji = signal_type
        desc = ''

    return f"""## {name} RSI Swing · {emoji}

**币种**: {name}
**信号**: {emoji}
**结构**: {signal_type}
**价格**: ${price:,.4f}
**RSI**: {rsi:.1f}
**说明**: {desc}
{extra}
**K线日期**: {date_str}
**检测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
> RSI Swing 波段信号 · 日线
> RSI长度={RSI_LENGTH} 超买={RSI_OVERBOUGHT} 超卖={RSI_OVERSOLD}"""


def fmt_pivot_signal(name, pivot_type, price, date_str, days_ago=0):
    """格式化 Pivot Points 信号消息"""
    if pivot_type == 'PL':
        emoji = '🟢 买入信号'
        desc = 'Pivot Low (▲)，价格在近期低点反转向上'
    elif pivot_type == 'PH':
        emoji = '🔴 卖出信号'
        desc = 'Pivot High (▼)，价格在近期高点反转向下'
    else:
        emoji = pivot_type
        desc = ''

    days_note = f'\n**距今天数**: {days_ago}天前' if days_ago > 0 else ''

    return f"""## {name} Pivot Points · {emoji}

**币种**: {name}
**信号**: {emoji}
**类型**: {"▼ Pivot High" if pivot_type=="PH" else "▲ Pivot Low"}
**价格**: ${price:,.4f}
**说明**: {desc}
**K线日期**: {date_str}{days_note}
**检测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
> Pivot Points (LuxAlgo) 波段信号 · 日线
> Pivot Length={PIVOT_LENGTH}"""


# ============================================================
#  主程序
# ============================================================
def main():
    print('=' * 60)
    print('  波段信号通知（日线）· RSI Swing + Pivot Points')
    print('  ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 60)
    print(f'  币种: {[s["name"] for s in SYMBOLS]}')
    print(f'  周期: 日线（1d）')
    print(f'  策略1: RSI Swing (长度={RSI_LENGTH} 超买={RSI_OVERBOUGHT} 超卖={RSI_OVERSOLD})')
    print(f'  策略2: Pivot Points (长度={PIVOT_LENGTH})')
    print(f'  检查间隔: {CHECK_INTERVAL}秒')
    print(f'  数据源: AICoin v3 API')
    print('=' * 60)

    # 推送启动消息
    send_dingtalk('✅ 波段信号已启动', f"""## BTC Pivot 策略 · 双策略信号通知启动

**币种**: {[s["name"] for s in SYMBOLS]}
**周期**: 日线
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**策略1: RSI Swing**
- 🟢 HL（更高低点）→ 买入信号
- 🔴 LH（更低高点）→ 卖出信号
- 📊 HH/LL → 结构标记

**策略2: Pivot Points (LuxAlgo)**
- 🟢 ▲ Pivot Low → 买入信号
- 🔴 ▼ Pivot High → 卖出信号
- 参数: Length={PIVOT_LENGTH}

> 日线波段信号，每小时检查一次""")

    # 记录每个币种上次推送的信号（避免重复推送）
    # key 格式: f"{strategy}_{sig_type}_{date_str}"
    last_pushed = set()

    while True:
        try:
            print(f'\n[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] 开始检查...')

            for sym in SYMBOLS:
                name = sym['name']
                coin_key = sym['coin_key']
                market = sym['market']

                print(f'\n  --- {name} ---')

                # 获取日线数据
                klines = fetch_klines(coin_key, market, '1d', '300')
                if len(klines) < PIVOT_LENGTH * 2 + 10:
                    print(f'  {name} K线不足: {len(klines)}')
                    continue

                print(f'  {name} 获取 {len(klines)} 根日线')
                latest_date = datetime.fromtimestamp(klines[-1][0]/1000, tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
                print(f'  {name} 最新收盘: ${klines[-1][4]:,.4f}  日期: {latest_date}')

                # =====================================================
                #  策略1: RSI Swing 信号
                # =====================================================
                rsi_signals, rsi_struct, rsi_now, _ = detect_rsi_swing(klines)

                if rsi_signals:
                    latest_rsi = rsi_signals[-1]
                    rsi_type, rsi_price, rsi_date = latest_rsi
                    print(f'  {name} [RSI Swing] RSI={rsi_now:.1f}  最新: {rsi_type} @ ${rsi_price:,.4f} ({rsi_date})')

                    push_key = f'rsi_{rsi_type}_{rsi_date}_{name}'
                    if push_key not in last_pushed:
                        if rsi_type in ('HL', 'LH', 'HH', 'LL'):
                            text = fmt_signal(name, rsi_type, rsi_price, rsi_now, rsi_date)
                            action = '🟢 买入' if rsi_type == 'HL' else '🔴 卖出' if rsi_type == 'LH' else '📊 结构'
                            send_dingtalk(f'{action} {name} RSI {rsi_type} @ ${rsi_price:,.2f}', text)
                            last_pushed.add(push_key)
                    else:
                        print(f'  {name} [RSI Swing] 已推送过，跳过')
                else:
                    print(f'  {name} [RSI Swing] RSI={rsi_now:.1f}  暂无信号')

                # =====================================================
                #  策略2: Pivot Points 信号
                # =====================================================
                pivot_signals, latest_pivot = detect_pivot_points(klines, PIVOT_LENGTH)

                if latest_pivot:
                    pv_type, pv_price, pv_date = latest_pivot
                    print(f'  {name} [Pivot] 最新: {"▼ High" if pv_type=="PH" else "▲ Low"} @ ${pv_price:,.4f} ({pv_date})')

                    push_key = f'pivot_{pv_type}_{pv_date}_{name}'
                    if push_key not in last_pushed:
                        # 只推送最近 5 天内的 pivot 信号
                        try:
                            pv_dt = datetime.strptime(pv_date, '%Y-%m-%d')
                            days_ago = (datetime.now() - pv_dt).days
                        except:
                            days_ago = 999

                        if days_ago <= 5:
                            text = fmt_pivot_signal(name, pv_type, pv_price, pv_date, days_ago)
                            action = '🟢 买入' if pv_type == 'PL' else '🔴 卖出'
                            send_dingtalk(f'{action} {name} Pivot {"▼" if pv_type=="PH" else "▲"} @ ${pv_price:,.2f}', text)
                            last_pushed.add(push_key)
                        else:
                            print(f'  {name} [Pivot] 信号太久 ({days_ago}天前)，跳过')
                            last_pushed.add(push_key)
                    else:
                        print(f'  {name} [Pivot] 已推送过，跳过')
                else:
                    print(f'  {name} [Pivot] 暂无信号')

            print(f'\n[{datetime.now().strftime("%H:%M:%S")}] 检查完成，{CHECK_INTERVAL}秒后再次检查...')

        except KeyboardInterrupt:
            print('\n\n  已停止')
            break
        except Exception as e:
            print(f'\n  ✗ 异常: {e}')

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
