"""
OKX 模拟盘自动交易 · ZigZag++ 多币种策略（多空双向）
=====================================================
基于 MT4 ZigZag 算法，检测市场结构（HH/LH/HL/LL）和方向反转。

支持的币种：
- BTC-USDT-SWAP  15分钟K线  Depth=12 Deviation=5 Backstep=2
- ETH-USDT-SWAP   5分钟K线  Depth=8  Deviation=3 Backstep=2
- NEAR-USDT-SWAP  5分钟K线  Depth=8  Deviation=3 Backstep=2

策略逻辑（每个币种独立运行）：
- ZigZag 方向转多 → 开多
- ZigZag 方向转空 → 开空
- 反向信号 → 平仓后反手开仓
- HH/HL + 持空 → 风控平空
- LH/LL + 持多 → 风控平多

每个币种用 10% 资金，5x 杠杆，双向持仓模式。

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
import threading
from datetime import datetime, timezone

# ============================================================
#  配置
# ============================================================
API_KEY     = "e2ab057d-db0f-43b0-b450-86ce6d97d7f3"
SECRET_KEY  = "A2EDD5A639B73ED3FF0039CBB4E5CC21"
PASSPHRASE  = "Aa610106$"

# 多币种配置
SYMBOLS = [
    {
        'inst_id': 'BTC-USDT-SWAP',
        'bar': '15m',
        'depth': 12,
        'deviation': 5,
        'backstep': 2,
        'ct_val': 0.01,       # 1张=0.01 BTC
        'name': 'BTC',
    },
    {
        'inst_id': 'ETH-USDT-SWAP',
        'bar': '5m',
        'depth': 8,
        'deviation': 3,
        'backstep': 2,
        'ct_val': 0.1,        # 1张=0.1 ETH
        'name': 'ETH',
    },
    {
        'inst_id': 'NEAR-USDT-SWAP',
        'bar': '5m',
        'depth': 8,
        'deviation': 3,
        'backstep': 2,
        'ct_val': 1,          # 1张=1 NEAR (需确认)
        'name': 'NEAR',
    },
]

KLINE_LIMIT    = 500
POSITION_RATIO = 0.10   # 每个币种用 10% 资金
LEVERAGE       = 5      # 杠杆倍数

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
#  账户 & 下单（自动适配单向/双向持仓模式）
# ============================================================
# 全局变量：当前账户持仓模式，启动时自动检测
POS_MODE = 'net_mode'  # 或 'long_short_mode'


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


def detect_position_mode():
    """检测当前账户持仓模式"""
    global POS_MODE
    result = okx_request('GET', '/api/v5/account/config')
    if result and result.get('code') == '0':
        pos_mode = result.get('data', [{}])[0].get('posMode', 'net_mode')
        POS_MODE = pos_mode
        print(f'  ✓ 当前持仓模式: {POS_MODE}')
    else:
        print(f'  ⚠️ 无法检测持仓模式，默认 net_mode: {result}')
    return POS_MODE


def get_position(inst_id):
    """查询指定币种的持仓"""
    path = f'/api/v5/account/positions?instId={inst_id}'
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
    """尝试设置双向持仓模式，如果失败就保持当前模式"""
    path = '/api/v5/account/set-position-mode'
    body = json.dumps({"posMode": "long_short_mode"})
    result = okx_request('POST', path, body)
    if result and result.get('code') == '0':
        global POS_MODE
        POS_MODE = 'long_short_mode'
        print(f'  ✓ 持仓模式已设为: 双向 (long_short_mode)')
    else:
        print(f'  ℹ️ 持仓模式设置返回: {result}')


def set_leverage(inst_id):
    """根据当前持仓模式设置杠杆"""
    path = '/api/v5/account/set-leverage'
    if POS_MODE == 'long_short_mode':
        # 双向模式：多空分别设置
        for pos_side in ['long', 'short']:
            body = json.dumps({
                "instId": inst_id,
                "lever": str(LEVERAGE),
                "mgnMode": "isolated",
                "posSide": pos_side
            })
            result = okx_request('POST', path, body)
            if result and result.get('code') == '0':
                print(f'  ✓ {inst_id} 杠杆: {LEVERAGE}x ({pos_side})')
            else:
                print(f'  ℹ️ {inst_id} 杠杆设置({pos_side}): {result}')
    else:
        # 单向模式：用 net
        body = json.dumps({
            "instId": inst_id,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated",
            "posSide": "net"
        })
        result = okx_request('POST', path, body)
        if result and result.get('code') == '0':
            print(f'  ✓ {inst_id} 杠杆: {LEVERAGE}x (net 单向)')
        else:
            print(f'  ℹ️ {inst_id} 杠杆设置(net): {result}')


def place_order(inst_id, side, sz, pos_side, reduce_only=False):
    """
    下单
    pos_side 会根据当前 POS_MODE 自动调整，如果传 long/short 但当前是 net_mode，则强制改为 net
    """
    path = '/api/v5/trade/order'

    actual_pos_side = pos_side
    if POS_MODE == 'net_mode':
        actual_pos_side = 'net'

    body_dict = {
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "posSide": actual_pos_side,
        "ordType": "market",
        "sz": str(sz)
    }
    if reduce_only:
        body_dict["reduceOnly"] = True

    body = json.dumps(body_dict)
    result = okx_request('POST', path, body)

    if result and result.get('code') == '0':
        ord_id = result['data'][0].get('ordId', '')
        print(f'  ✓ 下单成功: {inst_id} {side} {sz}张 posSide={actual_pos_side}  ID: {ord_id}')
        return True, ord_id
    else:
        print(f'  ✗ 下单失败: {inst_id}')
        if result:
            for d in result.get('data', []):
                print(f'    错误码: {d.get("sCode")}  {d.get("sMsg")}')
        return False, result


def open_long(inst_id, sz):
    """开多"""
    if POS_MODE == 'net_mode':
        return place_order(inst_id, 'buy', sz, 'net')
    return place_order(inst_id, 'buy', sz, 'long')


def open_short(inst_id, sz):
    """开空"""
    if POS_MODE == 'net_mode':
        return place_order(inst_id, 'sell', sz, 'net')
    return place_order(inst_id, 'sell', sz, 'short')


def close_position(inst_id, pos=None):
    """平仓"""
    if pos is None:
        pos = get_position(inst_id)
    if pos is None:
        return False
    pos_size = abs(pos['pos'])
    if pos_size == 0:
        return False

    if POS_MODE == 'net_mode':
        # 单向模式：pos>0 是多仓，pos<0 是空仓
        side = 'sell' if pos['pos'] > 0 else 'buy'
        return place_order(inst_id, side, pos_size, 'net', reduce_only=True)[0]
    else:
        # 双向模式
        if pos['posSide'] == 'long':
            return place_order(inst_id, 'sell', pos_size, 'long', reduce_only=True)[0]
        elif pos['posSide'] == 'short':
            return place_order(inst_id, 'buy', pos_size, 'short', reduce_only=True)[0]
    return False


def calc_order_size(price, ct_val):
    """计算合约张数"""
    bal = get_balance()
    if bal <= 0:
        return 0, bal
    order_value = bal * POSITION_RATIO * LEVERAGE
    sz = int(order_value / price / ct_val)
    return sz, bal


# ============================================================
#  钉钉
# ============================================================
def send_dingtalk(title, text):
    # 钉钉关键词: BTC, Pivot, 策略 — 但现在用 ZigZag，关键词可能要调
    # 保险起见加上所有可能的关键词
    if not any(k in text for k in ('BTC', 'Pivot', '策略', 'ZigZag')):
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


def fmt_trade(sym_name, action, price, sz, balance, ts, extra=''):
    emoji_map = {
        'OPEN_LONG':  '🟢 开多',
        'OPEN_SHORT': '🔴 开空',
        'CLOSE_LONG': '🔵 平多',
        'CLOSE_SHORT':'🟡 平空',
    }
    emoji = emoji_map.get(action, action)
    return f"""## {sym_name} ZigZag 策略 · {emoji}（模拟盘）

**标的**: {sym_name}
**动作**: {emoji}
**价格**: ${price:,.4f}
**数量**: {sz} 张
**账户余额**: ${balance:,.2f}
{extra}
**K线时间**: {ts}
**触发时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---
> 由 ZigZag++ · OKX 模拟盘自动交易（多币种双向）"""


# ============================================================
#  ZigZag 算法（MT4 原版 Python 移植）
# ============================================================
def calc_zigzag(high, low, depth=12, deviation=5, backstep=2):
    n = len(high)
    if n < depth + backstep + 1:
        return [], 0, None

    dev_threshold = deviation * 0.0001

    high_map = np.zeros(n)
    low_map = np.zeros(n)

    for i in range(depth, n):
        window_high = high[i - depth: i + 1]
        val_high = np.max(window_high)
        window_low = low[i - depth: i + 1]
        val_low = np.min(window_low)

        if high[i] == val_high:
            last_high = 0.0
            for back in range(1, min(backstep + 1, i + 1)):
                if high_map[i - back] != 0:
                    last_high = high_map[i - back]
                    break
            if last_high == 0 or abs(val_high - last_high) >= last_high * dev_threshold:
                for back in range(1, min(backstep + 1, i + 1)):
                    if high_map[i - back] != 0 and high_map[i - back] < val_high:
                        high_map[i - back] = 0.0
                high_map[i] = val_high
            else:
                high_map[i] = 0.0
        else:
            high_map[i] = 0.0

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

    # 筛选最终 ZigZag 点
    candidates = []
    for i in range(n):
        if high_map[i] != 0:
            candidates.append((i, high_map[i], 'H'))
        if low_map[i] != 0:
            candidates.append((i, low_map[i], 'L'))

    if not candidates:
        return [], 0, None

    candidates.sort(key=lambda x: x[0])

    filtered = []
    last_type = None
    i = 0
    while i < len(candidates):
        idx_i, price_i, type_i = candidates[i]
        if last_type is None:
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
            if type_i == 'H':
                if price_i > filtered[-1][1]:
                    filtered[-1] = (idx_i, price_i, type_i)
            else:
                if price_i < filtered[-1][1]:
                    filtered[-1] = (idx_i, price_i, type_i)
            i += 1
        else:
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

    last_point = zigzag_points[-1]
    direction = 1 if last_point[2] == 'H' else -1

    structure = None
    same_type_points = [p for p in zigzag_points if p[2] == last_point[2]]
    if len(same_type_points) >= 2:
        prev_same = same_type_points[-2]
        curr_same = same_type_points[-1]
        if last_point[2] == 'H':
            structure = 'HH' if curr_same[1] > prev_same[1] else 'LH'
        else:
            structure = 'HL' if curr_same[1] > prev_same[1] else 'LL'

    return zigzag_points, direction, structure


# ============================================================
#  单币种交易器
# ============================================================
class SymbolTrader:
    """每个币种一个独立的交易器实例"""

    def __init__(self, config):
        self.inst_id    = config['inst_id']
        self.bar        = config['bar']
        self.depth      = config['depth']
        self.deviation  = config['deviation']
        self.backstep   = config['backstep']
        self.ct_val     = config['ct_val']
        self.name       = config['name']

        self.klines          = []
        self.last_direction  = None
        self.last_structure  = None
        self.last_kline_time = None

    def fetch_history(self):
        """拉取历史 K 线"""
        try:
            params = {'instId': self.inst_id, 'bar': self.bar, 'limit': str(KLINE_LIMIT)}
            r = requests.get(f'{OKX_REST}/api/v5/market/candles', params=params, timeout=15)
            data = r.json()
            if data.get('code') == '0':
                raw = data['data']
                raw.reverse()
                self.klines = [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in raw]
                print(f'  ✓ {self.name} 获取 {len(self.klines)} 根 K 线 ({self.bar})')
                return True
            else:
                print(f'  ✗ {self.name} K线获取失败: {data}')
                return False
        except Exception as e:
            print(f'  ✗ {self.name} K线获取失败: {e}')
            return False

    def init_state(self):
        """用历史K线初始化方向状态"""
        if len(self.klines) < self.depth + self.backstep + 10:
            return
        high = np.array([float(k[2]) for k in self.klines])
        low  = np.array([float(k[3]) for k in self.klines])
        _, direction, structure = calc_zigzag(high, low, self.depth, self.deviation, self.backstep)
        self.last_direction = direction
        self.last_structure = structure
        dir_str = "多↑" if direction > 0 else "空↓" if direction < 0 else "未知"
        print(f'  {self.name} 当前方向: {dir_str}  结构: {structure or "无"}')

    def update_kline(self, item):
        """更新 K 线数据，返回是否为新收盘 K 线"""
        ts = item[0]
        o, h, l, c = item[1], item[2], item[3], item[4]
        confirm = item[5] if len(item) > 5 else '0'

        if self.klines and self.klines[-1][0] == ts:
            self.klines[-1] = [ts, o, h, l, c, item[5] if len(item) > 5 else '0']
        else:
            self.klines.append([ts, o, h, l, c, item[5] if len(item) > 5 else '0'])

        if confirm != '1':
            return None
        if len(self.klines) > KLINE_LIMIT + 10:
            self.klines.pop(0)
        if self.last_kline_time == ts:
            return None
        self.last_kline_time = ts
        return ts

    def on_kline_close(self, ts):
        """K线收盘时执行策略，返回是否产生了交易"""
        kline_time = datetime.fromtimestamp(int(ts) / 1000).strftime('%Y-%m-%d %H:%M:%S')
        print(f'\n[{datetime.now().strftime("%H:%M:%S")}] {self.name} K线收盘: {kline_time}')

        if len(self.klines) < self.depth + self.backstep + 10:
            print(f'  {self.name} K线不足，跳过')
            return False

        high  = np.array([float(k[2]) for k in self.klines])
        low   = np.array([float(k[3]) for k in self.klines])
        close = np.array([float(k[4]) for k in self.klines])
        price = close[-1]

        zz_points, direction, structure = calc_zigzag(high, low, self.depth, self.deviation, self.backstep)
        print(f'  {self.name} ${price:,.4f}  方向: {"多↑" if direction>0 else "空↓" if direction<0 else "?"}  结构: {structure or "无"}')

        # 查询当前持仓（兼容 net_mode 和 long_short_mode）
        position = get_position(self.inst_id)
        cur_side = None
        if position:
            if POS_MODE == 'net_mode':
                # 单向模式：pos > 0 视为多仓，pos < 0 视为空仓
                cur_side = 'long' if position['pos'] > 0 else 'short'
            else:
                cur_side = position['posSide']
        traded = False

        # =========================================================
        #  1. 方向变化 → 开仓/反手
        # =========================================================
        direction_changed = (self.last_direction is not None and direction != self.last_direction and direction != 0)

        if direction_changed:
            dir_str = "多↑" if direction > 0 else "空↓"
            print(f'  → {self.name} 方向变化: {dir_str}  (持仓: {cur_side or "空"})')

            if direction > 0:
                if cur_side == 'long':
                    print(f'  {self.name} 已持多仓，跳过')
                else:
                    if cur_side == 'short':
                        print(f'  {self.name} 反手: 先平空...')
                        close_position(self.inst_id, position)
                        time.sleep(0.5)
                        send_dingtalk(f'🟡 {self.name} 平空 @ {price}',
                            fmt_trade(self.name, 'CLOSE_SHORT', price, abs(position['pos']), get_balance(), kline_time,
                                      f'**策略**: ZigZag 方向转多，反手平空'))

                    sz, bal = calc_order_size(price, self.ct_val)
                    if sz > 0 and bal > 0:
                        ok, oid = place_order(self.inst_id, 'buy', sz, 'long')
                        if ok:
                            text = fmt_trade(self.name, 'OPEN_LONG', price, sz, bal, kline_time,
                                             f'**策略**: ZigZag 方向转多')
                            send_dingtalk(f'🟢 {self.name} 开多 {sz}张 @ {price}', text)
                            cur_side = 'long'
                            traded = True

            elif direction < 0:
                if cur_side == 'short':
                    print(f'  {self.name} 已持空仓，跳过')
                else:
                    if cur_side == 'long':
                        print(f'  {self.name} 反手: 先平多...')
                        close_position(self.inst_id, position)
                        time.sleep(0.5)
                        send_dingtalk(f'🔵 {self.name} 平多 @ {price}',
                            fmt_trade(self.name, 'CLOSE_LONG', price, abs(position['pos']), get_balance(), kline_time,
                                      f'**策略**: ZigZag 方向转空，反手平多'))

                    sz, bal = calc_order_size(price, self.ct_val)
                    if sz > 0 and bal > 0:
                        ok, oid = place_order(self.inst_id, 'sell', sz, 'short')
                        if ok:
                            text = fmt_trade(self.name, 'OPEN_SHORT', price, sz, bal, kline_time,
                                             f'**策略**: ZigZag 方向转空')
                            send_dingtalk(f'🔴 {self.name} 开空 {sz}张 @ {price}', text)
                            cur_side = 'short'
                            traded = True

        # =========================================================
        #  2. 结构变化 → 风控平仓
        # =========================================================
        structure_changed = (structure is not None and structure != self.last_structure)

        if structure_changed:
            print(f'  → {self.name} 新结构: {structure}')

            if structure in ('LH', 'LL') and cur_side == 'long':
                print(f'  → {self.name} 风控平多: {structure}')
                pos = get_position(self.inst_id)
                if pos:
                    ok = close_position(self.inst_id, pos)
                    if ok:
                        bal = get_balance()
                        extra = f'**策略**: ZigZag 结构 {structure}（风控平多）'
                        text = fmt_trade(self.name, 'CLOSE_LONG', price, abs(pos['pos']), bal, kline_time, extra)
                        send_dingtalk(f'🔵 {self.name} 风控平多 {structure} @ {price}', text)
                        cur_side = None
                        traded = True

            elif structure in ('HH', 'HL') and cur_side == 'short':
                print(f'  → {self.name} 风控平空: {structure}')
                pos = get_position(self.inst_id)
                if pos:
                    ok = close_position(self.inst_id, pos)
                    if ok:
                        bal = get_balance()
                        extra = f'**策略**: ZigZag 结构 {structure}（风控平空）'
                        text = fmt_trade(self.name, 'CLOSE_SHORT', price, abs(pos['pos']), bal, kline_time, extra)
                        send_dingtalk(f'🟡 {self.name} 风控平空 {structure} @ {price}', text)
                        cur_side = None
                        traded = True

        # 更新状态
        if direction != 0:
            self.last_direction = direction
        if structure is not None:
            self.last_structure = structure

        if not direction_changed and not structure_changed:
            print(f'  {self.name} 无交易信号')

        return traded


# ============================================================
#  主程序
# ============================================================
def main():
    import websocket

    print('=' * 60)
    print('  OKX 模拟盘自动交易 · ZigZag++ 多币种策略（多空双向）')
    print('  ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 60)
    for s in SYMBOLS:
        print(f'  {s["name"]:4s} {s["inst_id"]:16s} {s["bar"]:4s}  Depth={s["depth"]} Dev={s["deviation"]} Back={s["backstep"]}')
    print(f'  仓位: 每币种 {POSITION_RATIO*100}%  杠杆: {LEVERAGE}x')
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
        print('\n检测持仓模式...')
        detect_position_mode()
        print('\n设置合约杠杆...')
        for s in SYMBOLS:
            set_leverage(s['inst_id'])

    # 创建每个币种的交易器
    traders = {}
    for s in SYMBOLS:
        traders[s['inst_id']] = SymbolTrader(s)

    # 拉取历史 K 线
    print('\n拉取历史 K 线...')
    for s in SYMBOLS:
        traders[s['inst_id']].fetch_history()

    # 初始化方向状态
    print('\n初始化 ZigZag 状态...')
    for s in SYMBOLS:
        traders[s['inst_id']].init_state()

    # 推送启动消息
    symbol_list = '\n'.join([f'- {s["name"]} {s["bar"]} D={s["depth"]}/Dev={s["deviation"]}/B={s["backstep"]}' for s in SYMBOLS])
    send_dingtalk('✅ ZigZag++ 多币种已启动', f"""## BTC ZigZag 策略 · 多币种模拟盘启动

**币种**:
{symbol_list}

**环境**: OKX 模拟盘（虚拟资金）
**模式**: 双向持仓
**仓位**: 每币种 {POSITION_RATIO*100}%  杠杆: {LEVERAGE}x
**账户余额**: ${balance:,.2f} USDT
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

策略：
- ZigZag 方向转多 → 开多
- ZigZag 方向转空 → 开空
- 反向信号 → 平仓后反手开仓
- HH/HL + 持空 → 风控平空
- LH/LL + 持多 → 风控平多

> ⚠️ 模拟盘环境，使用虚拟资金""")

    # 构建订阅消息（一次订阅所有币种）
    sub_args = []
    for s in SYMBOLS:
        sub_args.append({"channel": f"candle{s['bar'].lower()}", "instId": s['inst_id']})

    def on_message(ws, message):
        if message == 'pong':
            return
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if 'data' not in data:
            return

        # 确定是哪个币种的数据
        channel_info = data.get('arg', {})
        inst_id = channel_info.get('instId', '')

        if inst_id not in traders:
            return

        trader = traders[inst_id]

        for item in data['data']:
            ts = trader.update_kline(item)
            if ts is None:
                continue
            # 新 K 线收盘，执行策略
            trader.on_kline_close(ts)

    def on_error(ws, error):
        msg = str(error)
        if any(k in msg for k in ('32', '1006', 'Connection', 'closed', 'EOF', 'Reset', 'Broken pipe', 'timeout')):
            print(f'\n  ⚠️ 连接异常: {msg[:80]}')
        else:
            print(f'\n  ✗ WebSocket 错误: {msg}')

    def on_close(ws, close_status, msg):
        print(f'\n  ⚠️ WebSocket 断开 (code={close_status})，3秒后自动重连...')

    def on_open(ws):
        sub = {"op": "subscribe", "args": sub_args}
        ws.send(json.dumps(sub))
        print('\n  ✓ WebSocket 已连接，多币种自动交易运行中...\n')

    def on_ping(ws, message):
        try:
            ws.send('pong')
        except Exception:
            pass

    # ---- 应用层心跳 ----
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
