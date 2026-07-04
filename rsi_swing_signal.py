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
    {'coin_key': 'wld',       'name': 'WLD',  'market': 'okx'},
]

# RSI 参数（和 Pine Script 一致）
RSI_LENGTH     = 7
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

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
#  钉钉通知
# ============================================================
def send_dingtalk(title, text):
    """推送到钉钉"""
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


# ============================================================
#  主程序
# ============================================================
def main():
    print('=' * 60)
    print('  RSI Swing 波段信号通知（日线）')
    print('  ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 60)
    print(f'  币种: {[s["name"] for s in SYMBOLS]}')
    print(f'  周期: 日线（1d）')
    print(f'  RSI: 长度={RSI_LENGTH} 超买={RSI_OVERBOUGHT} 超卖={RSI_OVERSOLD}')
    print(f'  信号: HL→买入  LH→卖出  HH/LL→结构标记')
    print(f'  检查间隔: {CHECK_INTERVAL}秒')
    print(f'  数据源: AICoin v3 API')
    print('=' * 60)

    # 推送启动消息
    send_dingtalk('✅ RSI Swing 信号已启动', f"""## BTC RSI Swing 策略 · 信号通知启动

**币种**: {[s["name"] for s in SYMBOLS]}
**周期**: 日线
**RSI**: 长度={RSI_LENGTH} 超买={RSI_OVERBOUGHT} 超卖={RSI_OVERSOLD}
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

信号规则：
- 🟢 HL（更高低点）→ 买入信号
- 🔴 LH（更低高点）→ 卖出信号
- 📊 HH/LL → 结构标记（不产生交易信号）

> 日线波段信号，每天收盘后检查""")

    # 记录每个币种上次推送的信号（避免重复推送）
    last_pushed = {}  # {name: (signal_type, date_str)}

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
                if len(klines) < RSI_LENGTH + 10:
                    print(f'  {name} K线不足: {len(klines)}')
                    continue

                print(f'  {name} 获取 {len(klines)} 根日线')
                print(f'  {name} 最新收盘: ${klines[-1][4]:,.4f}  日期: {datetime.fromtimestamp(klines[-1][0]/1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")}')

                # 检测信号
                signals, last_struct, rsi_now, price_now = detect_rsi_swing(klines)

                if not signals:
                    print(f'  {name} RSI={rsi_now:.1f}  暂无结构信号')
                    continue

                # 取最近的信号
                latest = signals[-1]
                sig_type, sig_price, sig_date = latest

                print(f'  {name} RSI={rsi_now:.1f}  最新结构: {sig_type} @ ${sig_price:,.4f} ({sig_date})')

                # 检查是否已推送过这个信号
                push_key = (sig_type, sig_date)
                if last_pushed.get(name) == push_key:
                    print(f'  {name} 已推送过 {sig_type} ({sig_date})，跳过')
                    continue

                # 只推送交易信号（HL=买入, LH=卖出），HH/LL 只记录不推送
                if sig_type in ('HL', 'LH'):
                    action = '🟢 买入' if sig_type == 'HL' else '🔴 卖出'
                    text = fmt_signal(name, sig_type, sig_price, rsi_now, sig_date)
                    send_dingtalk(f'{action} {name} {sig_type} @ ${sig_price:,.2f}', text)
                    last_pushed[name] = push_key
                elif sig_type in ('HH', 'LL'):
                    # 结构标记也推送，但不那么紧急
                    text = fmt_signal(name, sig_type, sig_price, rsi_now, sig_date)
                    send_dingtalk(f'📊 {name} {sig_type} @ ${sig_price:,.2f}', text)
                    last_pushed[name] = push_key

            print(f'\n[{datetime.now().strftime("%H:%M:%S")}] 检查完成，{CHECK_INTERVAL}秒后再次检查...')

        except KeyboardInterrupt:
            print('\n\n  已停止')
            break
        except Exception as e:
            print(f'\n  ✗ 异常: {e}')

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
