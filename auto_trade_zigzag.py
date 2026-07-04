"""
OKX 模拟盘自动交易 · ZigZag++ 策略（多空双向）
=================================================
基于 MT4 ZigZag 算法，检测市场结构（HH/LH/HL/LL）和方向反转。

策略逻辑：
- ZigZag 方向从空转多（direction > 0）→ 开多
- ZigZag 方向从多转空（direction < 0）→ 开空
- 反向信号 → 先平仓再反手开仓
- HH/HL（看多结构）+ 持空 → 风控平空
- LH/LL（看空结构）+ 持多 → 风控平多

ZigZag 参数：Depth=12, Deviation=5, Backstep=2

运行方式（Mac 终端）：
  pip3 install requests numpy websocket-client
  python3 auto_trade_zigzag.py

按 Ctrl+C 停止
"""

import numpy as np
import requests
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timezone

# ============================================================
#  配置
# ============================================================
API_KEY     = "e2ab057d-db0f-43b0-b450-86ce6d97d7f3"
SECRET_KEY  = "A2EDD5A639B73ED3FF0039CBB4E5CC21"
PASSPHRASE  = "Aa610106$"

INST_ID      = 'BTC-USDT-SWAP'
BAR          = '15m'

# ZigZag 参数（MT4 原版算法）
DEPTH        = 12       # 极值点搜索范围（K线根数）
DEVIATION    = 5        # 最小价格变动幅度（点数，BTC合约这里用百分比近似）
BACKSTEP     = 2        # 回溯步数（防止极值点过密）

KLINE_LIMIT  = 500
POSITION_RATIO = 0.10   # 每次用 10% 资金
LEVERAGE     = 5        # 杠杆倍数

# OKX 模拟盘 API
OKX_REST = 'https://www.okx.com'
OKX_WS   = 'wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999'
DINGTALK_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=36df93460e83f02f0530386d1e30544a661740619d073e2990ef5cc9e480cac0'


# ============================================================
#  OKX API 签名
# ============================================================
def get_timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def sign(secret, timestamp, method, path, body=''):
    msg = f'{timestamp}{method.upper()}{path}{body}'
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_request(method, path, body=''):
    ts = get_timestamp()
    sign_str = sign(SECRET_KEY, ts, method, path, body)
    headers = {
        'OK-ACCESS-KEY': API_KEY,
        'OK-ACCESS-SIGN': sign_str,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': PASSPHRASE,
        'x-simulated-trading': '1',
        'Content-Type': 'application/json'
    }
    url = OKX_REST + path
    try:
        if method.upper() == 'GET':
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.post(url, headers=headers, data=body, timeout=15)
        return r.json()
    except Exception as e:
        print(f'  API 请求失败: {e}')
        return None


# ============================================================
#  账户 & 下单（双向持仓模式）
# ============================================================
def get_balance():
    result = okx_request('GET', '/api/v5/account/balance')
    if result and result.get('code') == '0':
        for d in result.get('data', []):
            for detail in d.get('details', []):
                if detail.get('ccy') == 'USDT':
                    return float(detail.get('availBal', 0))
    else:
        print(f'  ⚠️ 查询余额返回: {result}')
    return 0.0


def get_position():
    path = f'/api/v5/account/positions?instId={INST_ID}'
    result = okx_request('GET', path)
    if result and result.get('code') == '0':
        for pos in result.get('data', []):
            if float(pos.get('pos', 0)) != 0:
                return {
                    'pos': float(pos['pos']),
                    'posSide': pos.get('posSide', ''),
                    'avg_px': float(pos.get('avgPx', 0)),
                    'margin': float(pos.get('margin', 0))
                }
    return None


def set_position_mode():
    path = '/api/v5/account/set-position-mode'
    body = json.dumps({"posMode": "long_short_mode"})
    result = okx_request('POST', path, body)
    if result and result.get('code') == '0':
        print(f'  ✓ 持仓模式: 双向 (long_short_mode)')
    else:
        print(f'  ℹ️ 持仓模式设置返回: {result}')


def set_leverage():
    path = '/api/v5/account/set-leverage'
    for pos_side in ['long', 'short']:
        body = json.dumps({
            "instId": INST_ID,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated",
            "posSide": pos_side
        })
        result = okx_request('POST', path, body)
        if result and result.get('code') == '0':
            print(f'  ✓ 杠杆已设置: {LEVERAGE}x ({pos_side} 逐仓)')
        else:
            print(f'  ℹ️ 杠杆设置({pos_side})返回: {result}')


def place_order(side, sz, pos_side, reduce_only=False):
    path = '/api/v5/trade/order'
    body_dict = {
        "instId": INST_ID,
        "tdMode": "isolated",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(sz)
    }
    if reduce_only:
        body_dict["reduceOnly"] = True

    body = json.dumps(body_dict)
    result = okx_request('POST', path, body)

    if result and result.get('code') == '0':
        ord_id = result['data'][0].get('ordId', '')
        print(f'  ✓ 下单成功: {side} {sz}张 posSide={pos_side}  订单ID: {ord_id}')
        return True, ord_id
    else:
        print(f'  ✗ 下单失败:')
        if result:
            for d in result.get('data', []):
                print(f'    错误码: {d.get("sCode")}  {d.get("sMsg")}')
            print(f'    完整返回: {result}')
        return False, result


def open_long(sz):
    return place_order('buy', sz, 'long')

def open_short(sz):
    return place_order('sell', sz, 'short')

def close_long(sz):
    return place_order('sell', sz, 'long', reduce_only=True)

def close_short(sz):
    return place_order('buy', sz, 'short', reduce_only=True)


def close_position(pos=None):
    if pos is None:
        pos = get_position()
    if pos is None:
        return False
    pos_size = abs(pos['pos'])
    if pos_size == 0:
        return False
    if pos['posSide'] == 'long':
        return close_long(pos_size)[0]
    elif pos['posSide'] == 'short':
        return close_short(pos_size)[0]
    return False


def calc_order_size(price):
    bal = get_balance()
    if bal <= 0:
        return 0, bal
    order_value = bal * POSITION_RATIO * LEVERAGE
    sz = int(order_value / price / 0.01)
    return sz, bal


# ============================================================
#  钉钉
# ============================================================
def send_dingtalk(title, text):
    if 'BTC' not in text or 'ZigZag' not in text:
        text = f'BTC ZigZag 策略\n{text}'
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


def fmt_trade(action, price, sz, balance, ts, extra=''):
    emoji_map = {
        'OPEN_LONG':  '🟢 开多',
        'OPEN_SHORT': '🔴 开空',
        'CLOSE_LONG': '🔵 平多',
        'CLOSE_SHORT':'🟡 平空',
    }
    emoji = emoji_map.get(action, action)
    return f"""## BTC ZigZag 策略 · {emoji}（模拟盘）

**标的**: {INST_ID} {BAR}
**动作**: {emoji}
**价格**: ${price:,.2f}
**数量**: {sz} 张
**账户余额**: ${balance:,.2f}
{extra}
**K线时间**: {ts}
**触发时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
> 由 ZigZag++ · OKX 模拟盘自动交易（双向）"""


# ============================================================
#  ZigZag 算法（MT4 原版 Python 移植）
# ============================================================
def calc_zigzag(high, low, depth=12, deviation=5, backstep=2):
    """
    MT4 ZigZag 算法实现

    参数:
        high: 高价数组
        low: 低价数组
        depth: 极值点搜索范围（K线根数）
        deviation: 最小价格变动幅度（百分比，BTC用0.5%等）
        backstep: 回溯步数

    返回:
        zigzag_points: [(index, price, type), ...]  type='H'(高点) 或 'L'(低点)
        direction: 当前方向 (1=向上, -1=向下)
        structure: 最新结构标签 ('HH'/'LH'/'HL'/'LL' 或 None)
    """
    n = len(high)
    if n < depth + backstep + 1:
        return [], 0, None

    # deviation 转成百分比阈值（BTC 价格较高，用百分比更合理）
    # MT4 里 deviation 是点数，这里用价格的百分比近似
    dev_threshold = deviation * 0.0001  # 5 -> 0.05% （可调）

    # Step 1: 计算 HighMapBuffer 和 LowMapBuffer
    high_map = np.zeros(n)
    low_map = np.zeros(n)

    for i in range(depth, n):
        # 在 [i-depth, i] 范围内找最高点
        window_high = high[i - depth: i + 1]
        val_high = np.max(window_high)
        # 在 [i-depth, i] 范围内找最低点
        window_low = low[i - depth: i + 1]
        val_low = np.min(window_low)

        # 记录高点（如果当前K线的最高价就是窗口最高）
        if high[i] == val_high:
            # 检查 deviation：与上一个记录的高点比较
            last_high = 0.0
            for back in range(1, min(backstep + 1, i + 1)):
                if high_map[i - back] != 0:
                    last_high = high_map[i - back]
                    break

            if last_high == 0 or abs(val_high - last_high) >= last_high * dev_threshold:
                # 回溯清除更近的、但不如当前高的高点
                for back in range(1, min(backstep + 1, i + 1)):
                    if high_map[i - back] != 0 and high_map[i - back] < val_high:
                        high_map[i - back] = 0.0
                high_map[i] = val_high
            else:
                high_map[i] = 0.0
        else:
            high_map[i] = 0.0

        # 记录低点（如果当前K线的最低价就是窗口最低）
        if low[i] == val_low:
            last_low = 0.0
            for back in range(1, min(backstep + 1, i + 1)):
                if low_map[i - back] != 0:
                    last_low = low_map[i - back]
                    break

            if last_low == 0 or abs(val_low - last_low) >= last_low * dev_threshold:
                for back in range(1, min(backstep + 1, i + 1)):
                    if low_map[i - back] != 0 and low_map[i - back] > val_low:
                        low_map[i - back] = 0.0
                low_map[i] = val_low
            else:
                low_map[i] = 0.0
        else:
            low_map[i] = 0.0

    # Step 2: 从 HighMap 和 LowMap 筛选最终 ZigZag 点
    zigzag_points = []
    # 高低点候选
    candidates = []
    for i in range(n):
        if high_map[i] != 0:
            candidates.append((i, high_map[i], 'H'))
        if low_map[i] != 0:
            candidates.append((i, low_map[i], 'L'))

    # 合并并按时间排序，交替选取高点和低点
    if not candidates:
        return [], 0, None

    # 按时间排序
    candidates.sort(key=lambda x: x[0])

    # 交替选取：高点之后选低点，低点之后选高点
    # 同类型的取极值（多个连续高点取最高，连续低点取最低）
    filtered = []
    last_type = None
    i = 0
    while i < len(candidates):
        idx_i, price_i, type_i = candidates[i]

        if last_type is None:
            # 第一个点
            # 合并连续同类型点
            j = i
            best_idx, best_price = idx_i, price_i
            while j < len(candidates) and candidates[j][2] == type_i:
                if type_i == 'H' and candidates[j][1] > best_price:
                    best_price = candidates[j][1]
                    best_idx = candidates[j][0]
                elif type_i == 'L' and candidates[j][1] < best_price:
                    best_price = candidates[j][1]
                    best_idx = candidates[j][0]
                j += 1
            filtered.append((best_idx, best_price, type_i))
            last_type = type_i
            i = j
            continue

        if type_i == last_type:
            # 同类型，取极值并更新
            if type_i == 'H':
                if price_i > filtered[-1][1]:
                    filtered[-1] = (idx_i, price_i, type_i)
            else:
                if price_i < filtered[-1][1]:
                    filtered[-1] = (idx_i, price_i, type_i)
            i += 1
        else:
            # 类型变化，添加新点
            # 合并连续同类型
            j = i
            best_idx, best_price = idx_i, price_i
            while j < len(candidates) and candidates[j][2] == type_i:
                if type_i == 'H' and candidates[j][1] > best_price:
                    best_price = candidates[j][1]
                    best_idx = candidates[j][0]
                elif type_i == 'L' and candidates[j][1] < best_price:
                    best_price = candidates[j][1]
                    best_idx = candidates[j][0]
                j += 1
            filtered.append((best_idx, best_price, type_i))
            last_type = type_i
            i = j

    zigzag_points = filtered

    if len(zigzag_points) < 2:
        return zigzag_points, 0, None

    # Step 3: 计算方向和结构标签
    # 方向：最后一个点是高点 → direction=1（向上），最后一个点是低点 → direction=-1（向下）
    last_point = zigzag_points[-1]
    prev_point = zigzag_points[-2] if len(zigzag_points) >= 2 else None

    direction = 1 if last_point[2] == 'H' else -1

    # 结构标签：比较最近两个同类型极值点
    structure = None
    same_type_points = [p for p in zigzag_points if p[2] == last_point[2]]
    if len(same_type_points) >= 2:
        prev_same = same_type_points[-2]
        curr_same = same_type_points[-1]
        if last_point[2] == 'H':
            # 比较高点
            structure = 'HH' if curr_same[1] > prev_same[1] else 'LH'
        else:
            # 比较低点
            structure = 'HL' if curr_same[1] > prev_same[1] else 'LL'

    return zigzag_points, direction, structure


# ============================================================
#  信号检测
# ============================================================
def check_signals(klines):
    """
    返回:
        direction: 当前 ZigZag 方向 (1=多, -1=空, 0=未知)
        direction_changed: 方向是否刚发生变化
        structure: 最新结构 ('HH'/'LH'/'HL'/'LL' 或 None)
        structure_changed: 结构是否刚出现新的
        price: 当前价格
        zigzag_points: ZigZag 转折点列表
    """
    if len(klines) < DEPTH + BACKSTEP + 10:
        return 0, False, None, False, 0, []

    high  = np.array([float(k[2]) for k in klines])
    low   = np.array([float(k[3]) for k in klines])
    close = np.array([float(k[4]) for k in klines])

    price = close[-1]

    zigzag_points, direction, structure = calc_zigzag(high, low, DEPTH, DEVIATION, BACKSTEP)

    return direction, structure, price, zigzag_points


# ============================================================
#  主程序
# ============================================================
def main():
    import websocket

    print('=' * 60)
    print('  OKX 模拟盘自动交易 · ZigZag++ 策略（多空双向）')
    print('  ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 60)
    print(f'  标的: {INST_ID} {BAR}')
    print(f'  ZigZag: Depth={DEPTH}  Deviation={DEVIATION}  Backstep={BACKSTEP}')
    print(f'  仓位: {POSITION_RATIO*100}%  杠杆: {LEVERAGE}x')
    print(f'  模式: 双向持仓 (long_short_mode)')
    print(f'  环境: OKX 模拟盘（虚拟资金）')
    print('=' * 60)

    # 查询余额
    print('\n查询模拟盘账户...')
    balance = get_balance()
    print(f'  账户余额: ${balance:,.2f} USDT')

    if balance == 0:
        print('  ⚠️  余额为 0，请先在 OKX 模拟交易页面充值虚拟资金')
        print('  继续运行（仅监控信号，不下单）...')
    else:
        print('\n设置持仓模式...')
        set_position_mode()
        print('\n设置合约杠杆...')
        set_leverage()

    # 推送启动消息
    send_dingtalk('✅ ZigZag++ 模拟盘已启动', f"""## BTC ZigZag 策略 · 模拟盘自动交易启动

**标的**: {INST_ID} {BAR}
**环境**: OKX 模拟盘（虚拟资金）
**模式**: 双向持仓
**ZigZag**: Depth={DEPTH}  Deviation={DEVIATION}  Backstep={BACKSTEP}
**仓位**: {POSITION_RATIO*100}%  杠杆: {LEVERAGE}x
**账户余额**: ${balance:,.2f} USDT
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

策略：
- ZigZag 方向转多 → 开多
- ZigZag 方向转空 → 开空
- 反向信号 → 平仓后反手开仓
- HH/HL + 持空 → 风控平空
- LH/LL + 持多 → 风控平多

> ⚠️ 模拟盘环境，使用虚拟资金""")

    # 拉取历史 K 线
    print('\n拉取历史 K 线...')
    klines = []
    try:
        params = {'instId': INST_ID, 'bar': BAR, 'limit': str(KLINE_LIMIT)}
        r = requests.get(f'{OKX_REST}/api/v5/market/candles', params=params, timeout=15)
        data = r.json()
        if data.get('code') == '0':
            raw = data['data']
            raw.reverse()
            klines = [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in raw]
            print(f'  ✓ 获取 {len(klines)} 根 K 线')
        else:
            print(f'  ✗ K线获取失败: {data}')
            return
    except Exception as e:
        print(f'  ✗ 失败: {e}')
        return

    # 初始化上次状态
    last_direction = None
    last_structure = None
    last_kline_time = None

    # 先用历史K线计算当前方向
    if len(klines) >= DEPTH + BACKSTEP + 10:
        high  = np.array([float(k[2]) for k in klines])
        low   = np.array([float(k[3]) for k in klines])
        _, direction, structure = calc_zigzag(high, low, DEPTH, DEVIATION, BACKSTEP)
        last_direction = direction
        last_structure = structure
        print(f'\n  当前 ZigZag 方向: {"多↑" if direction > 0 else "空↓" if direction < 0 else "未知"}')
        print(f'  当前结构: {structure or "无"}')

    def on_message(ws, message):
        nonlocal last_direction, last_structure, last_kline_time

        if message == 'pong':
            return
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if 'data' not in data:
            return

        for item in data['data']:
            ts = item[0]
            o, h, l, c = item[1], item[2], item[3], item[4]
            confirm = item[5] if len(item) > 5 else '0'

            if klines and klines[-1][0] == ts:
                klines[-1] = [ts, o, h, l, c, item[5] if len(item) > 5 else '0']
            else:
                klines.append([ts, o, h, l, c, item[5] if len(item) > 5 else '0'])

            if confirm != '1':
                continue
            if len(klines) > KLINE_LIMIT + 10:
                klines.pop(0)
            if last_kline_time == ts:
                continue
            last_kline_time = ts

            kline_time = datetime.fromtimestamp(int(ts) / 1000).strftime('%Y-%m-%d %H:%M:%S')
            print(f'\n[{datetime.now().strftime("%H:%M:%S")}] K线收盘: {kline_time}')

            # 计算 ZigZag
            direction, structure, price, zz_points = check_signals(klines)
            print(f'  价格: ${price:,.2f}')
            print(f'  ZigZag方向: {"多↑" if direction > 0 else "空↓" if direction < 0 else "未知"}  结构: {structure or "无"}')
            if len(zz_points) >= 2:
                print(f'  最近转折点: {zz_points[-1][2]} ${zz_points[-1][1]:,.2f}')

            # 查询当前持仓
            position = get_position()
            cur_side = position['posSide'] if position else None

            # =========================================================
            #  1. 方向变化 → 开仓/反手
            # =========================================================
            direction_changed = (last_direction is not None and direction != last_direction and direction != 0)

            if direction_changed:
                dir_str = "多↑" if direction > 0 else "空↓"
                print(f'  → ZigZag 方向变化: {dir_str}  (当前持仓: {cur_side or "空"})')

                if direction > 0:
                    # 方向转多 → 开多
                    if cur_side == 'long':
                        print('  已持多仓，跳过')
                    else:
                        if cur_side == 'short':
                            print('  反手: 先平空仓...')
                            close_position(position)
                            time.sleep(1)
                            send_dingtalk(f'🟡 平空 @ {price}',
                                fmt_trade('CLOSE_SHORT', price, abs(position['pos']), get_balance(), kline_time,
                                          f'**策略**: ZigZag 方向转多，反手平空'))

                        sz, bal = calc_order_size(price)
                        if sz > 0 and bal > 0:
                            ok, oid = open_long(sz)
                            if ok:
                                text = fmt_trade('OPEN_LONG', price, sz, bal, kline_time,
                                                 f'**策略**: ZigZag 方向转多')
                                send_dingtalk(f'🟢 开多 {sz}张 @ {price}', text)
                                cur_side = 'long'

                elif direction < 0:
                    # 方向转空 → 开空
                    if cur_side == 'short':
                        print('  已持空仓，跳过')
                    else:
                        if cur_side == 'long':
                            print('  反手: 先平多仓...')
                            close_position(position)
                            time.sleep(1)
                            send_dingtalk(f'🔵 平多 @ {price}',
                                fmt_trade('CLOSE_LONG', price, abs(position['pos']), get_balance(), kline_time,
                                          f'**策略**: ZigZag 方向转空，反手平多'))

                        sz, bal = calc_order_size(price)
                        if sz > 0 and bal > 0:
                            ok, oid = open_short(sz)
                            if ok:
                                text = fmt_trade('OPEN_SHORT', price, sz, bal, kline_time,
                                                 f'**策略**: ZigZag 方向转空')
                                send_dingtalk(f'🔴 开空 {sz}张 @ {price}', text)
                                cur_side = 'short'

            # =========================================================
            #  2. 结构变化 → 风控平仓
            # =========================================================
            structure_changed = (structure is not None and structure != last_structure)

            if structure_changed:
                print(f'  → 新结构: {structure}')

                # LH/LL 是看空结构，有多仓则平
                if structure in ('LH', 'LL') and cur_side == 'long':
                    print(f'  → 风控平多: {structure}')
                    pos = get_position()
                    if pos:
                        ok = close_position(pos)
                        if ok:
                            bal = get_balance()
                            extra = f'**策略**: ZigZag 结构 {structure}（风控平多）'
                            text = fmt_trade('CLOSE_LONG', price, abs(pos['pos']), bal, kline_time, extra)
                            send_dingtalk(f'🔵 风控平多 {structure} @ {price}', text)
                            cur_side = None

                # HH/HL 是看多结构，有空仓则平
                elif structure in ('HH', 'HL') and cur_side == 'short':
                    print(f'  → 风控平空: {structure}')
                    pos = get_position()
                    if pos:
                        ok = close_position(pos)
                        if ok:
                            bal = get_balance()
                            extra = f'**策略**: ZigZag 结构 {structure}（风控平空）'
                            text = fmt_trade('CLOSE_SHORT', price, abs(pos['pos']), bal, kline_time, extra)
                            send_dingtalk(f'🟡 风控平空 {structure} @ {price}', text)
                            cur_side = None

            # 更新状态
            if direction != 0:
                last_direction = direction
            if structure is not None:
                last_structure = structure

            if not direction_changed and not structure_changed:
                print('  无交易信号')

    def on_error(ws, error):
        msg = str(error)
        if any(k in msg for k in ('32', '1006', 'Connection', 'closed', 'EOF', 'Reset', 'Broken pipe', 'timeout')):
            print(f'\n  ⚠️ 连接异常: {msg[:80]}')
        else:
            print(f'\n  ✗ WebSocket 错误: {msg}')

    def on_close(ws, close_status, msg):
        print(f'\n  ⚠️ WebSocket 断开 (code={close_status})，3秒后自动重连...')

    def on_open(ws):
        sub = {"op": "subscribe", "args": [{"channel": f"candle{BAR.lower()}", "instId": INST_ID}]}
        ws.send(json.dumps(sub))
        print('\n  ✓ WebSocket 已连接，自动交易运行中...\n')

    def on_ping(ws, message):
        try:
            ws.send('pong')
        except Exception:
            pass

    # ---- 应用层心跳 ----
    import threading

    def heartbeat(ws_ref, stop_event):
        while not stop_event.wait(15):
            ws = ws_ref[0]
            if ws is None:
                continue
            try:
                ws.send('ping')
            except Exception:
                break

    reconnect_count = 0
    while True:
        try:
            ws = websocket.WebSocketApp(
                OKX_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_ping=on_ping
            )

            ws_ref = [ws]
            stop_event = threading.Event()
            hb = threading.Thread(target=heartbeat, args=(ws_ref, stop_event), daemon=True)
            hb.start()

            ws.run_forever(ping_interval=15, ping_timeout=10)

            stop_event.set()
            ws_ref[0] = None

            reconnect_count += 1
            if reconnect_count > 100:
                print('\n  ✗ 重连次数过多，停止。请检查网络。')
                break

            wait = min(3 * reconnect_count, 30)
            print(f'  第 {reconnect_count} 次重连，等待 {wait} 秒...')
            time.sleep(wait)
        except KeyboardInterrupt:
            print('\n\n  已停止')
            break
        except Exception as e:
            print(f'\n  异常: {e}，5秒后重连...')
            time.sleep(5)


if __name__ == '__main__':
    main()
