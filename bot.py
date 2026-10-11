#!/usr/bin/env python3
"""JJ EBP BloFin v2.2.5 — confirmed reports and sampled trade telemetry; execution hardening, not a proven profitable strategy.
Reports use confirmed positions-history, separate fees and funding bills.
History queries use positionId when known; matching failures are explained.
Sampled mark-price extremes are estimates, not exact intrabar highs/lows.
Existing positions start measurement at upgrade; old extremes cannot be recovered.
Completed report records remain in the persistent state journal.
Deployment: replace bot.py; retain all existing strategy/environment settings.
A persistent volume is optional. Without one, state is stored locally and may
be lost at redeployment. Existing unknown positions still block new entries.
If a volume exists, use JJ_STATE_FILE within its actual mount path.
If deployment previously lost state, JJ_RECOVER_SYMBOLS must explicitly list
the actual open instruments, comma separated. Recovery only accepts existing
full-position market TP/SL using mark price, isolated net mode and matching leverage.
Recovery does not place an entry order. Remove this whitelist after recovery.
Reporting cannot reconstruct old peak/trough PnL from closure history.
Unmatched history is queued durably, retried every minute, and never guessed.
Reasons use confirmed closing order categories/IDs, not the sign of PnL.
No exact-once Telegram guarantee after a crash; a duplicate can occur.
Keep existing JJ_MIN_MARGIN_USDT value and persistent jj_ebp_v22 state file.
This file is based on v2.2: no cash TP/trailing changes.
Liquidity filters apply only to new entries, never to ongoing management.
Defaults are conservative trial settings, not demonstrated optimal thresholds:
JJ_MIN_24H_VOLUME_USDT=1000000, JJ_MAX_SPREAD_PCT=0.001,
JJ_DEPTH_BAND_PCT=0.002, JJ_MIN_DEPTH_MULT=10,
JJ_MAX_BOOK_IMPACT_PCT=0.001. Missing/stale data means no entry.
Volume covers last 96 completed 15m bars (rolling completed 24 hours).
Order book snapshots cannot guarantee future exit liquidity or fill price.
Install: pip install requests pandas flask
Default PAPER_TRADING=True: no private API calls, simulated balance 250 USDT.
For exchange demo: PAPER_TRADING=False, BLOFIN_DEMO=True, demo API credentials.
LIVE requires net_mode, isolated margin and a dedicated account/subaccount.
Do not run alongside v2.1 or another bot on the same instruments.
State v2.1 is intentionally not imported; reconcile old positions on BloFin first.
Keep the state file on persistent storage; run only one process per account.

Defaults: 15m closed EBP candles, >=12 USDT estimated margin, 12x, 2R,
2 total positions, <=3 USDT estimated stop loss including fee/slippage allowance.
Minimum margin is an estimate before fill, not a guarantee of actual margin.
Paper exits use sampled mark prices, not an intrabar backtest; funding excluded.
No promise of hourly trades or improved win rate. Test in exchange demo first.
"""
import os
import time
import json
import math
import hmac
import base64
import hashlib
import threading
import logging
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from uuid import uuid4
from urllib.parse import urlencode
from collections import Counter
import requests
import pandas as pd

VERSION = '2.2.5-NoVolume'
def envf(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value):
        raise ValueError(name + ': invalid number')
    return value
PAPER_TRADING = os.getenv('PAPER_TRADING', 'True').lower() == 'true'
BLOFIN_DEMO = os.getenv('BLOFIN_DEMO', 'False').lower() == 'true'
BASE_URL = 'https://demo-trading-openapi.blofin.com' if BLOFIN_DEMO else 'https://openapi.blofin.com'
API_KEY = os.getenv('EXCHANGE_API_KEY', '').strip()
API_SECRET = os.getenv('EXCHANGE_SECRET', '').strip()
API_PASSWORD = os.getenv('EXCHANGE_PASSWORD', '').strip()
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip().removeprefix('bot')
TG_CHAT = os.getenv('TELEGRAM_CHAT_ID', '').strip()
BAR = os.getenv('JJ_SIGNAL_BAR', '15m')
BAR_MS = {'1m':60000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1H':3600000}
MARGIN = envf('JJ_MIN_MARGIN_USDT', 8)
LEV = int(os.getenv('JJ_LEVERAGE', '12'))
RR = envf('JJ_FIXED_RR', 2)
MAX_POS = int(os.getenv('JJ_MAX_ACTIVE_POSITIONS', '2'))
SCAN_SECONDS = envf('JJ_SCAN_SECONDS', 30)
RISK_CAP = envf('JJ_MAX_LOSS_USDT', 3)
FEE = envf('JJ_TAKER_FEE_RATE', .0006)  # each side; adjust to your actual tier
SLIPPAGE = envf('JJ_SLIPPAGE_ALLOWANCE', .001)
MAX_SPREAD = envf('JJ_MAX_SPREAD_PCT', .001)  # 0.10%; applies only to entries
MIN_VOLUME = envf('JJ_MIN_24H_VOLUME_USDT', 1000000)
DEPTH_BAND = envf('JJ_DEPTH_BAND_PCT', .002)  # 0.20% around executable top price
DEPTH_MULT = envf('JJ_MIN_DEPTH_MULT', 10)  # each side >= 10x position notional
MAX_IMPACT = envf('JJ_MAX_BOOK_IMPACT_PCT', .001)  # 0.10% VWAP vs best price
liquidity_cache = {}
balance_diagnostic = {}

MAX_CHASE_R = envf('JJ_MAX_ENTRY_DRIFT_R', .35)
MAX_STOP_PCT = envf('JJ_MAX_STOP_PCT', .04)
MIN_STOP_PCT = envf('JJ_MIN_STOP_PCT', .002)
COOLDOWN = envf('JJ_COOLDOWN_SECONDS', 900)
MAX_AGE = envf('JJ_MAX_SIGNAL_AGE_SECONDS', 120)
PAPER_BALANCE = envf('JJ_PAPER_BALANCE_USDT', 250)
VOLUME_PATH = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
STATE_NAME = f'jj_ebp_v22_{"paper" if PAPER_TRADING else "demo" if BLOFIN_DEMO else "live"}.json'
STATE_FILE = os.getenv('JJ_STATE_FILE', os.path.join(VOLUME_PATH, STATE_NAME) if VOLUME_PATH else STATE_NAME)
RECOVER_SYMBOLS = {x.strip().upper() for x in os.getenv('JJ_RECOVER_SYMBOLS', '').split(',') if x.strip()}

def prepare_state_directory():
    # A missing Railway volume is allowed. A normal /data directory is not
    # persistent storage; never claim persistence from directory existence alone.
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)

def state_storage_notice():
    if not os.getenv('RAILWAY_ENVIRONMENT_ID'):
        return ''
    if VOLUME_PATH and os.path.isdir(VOLUME_PATH):
        root, target = os.path.realpath(VOLUME_PATH), os.path.realpath(STATE_FILE)
        if os.path.commonpath([root,target]) == root:
            return 'Statusbestand staat op het gekoppelde Railway-volume.'
    return ('⚠️ Zonder vaste opslag: positiegegevens, afrekeningen en winstmetingen '
            'kunnen bij een deployment verdwijnen. Onbekende open posities worden '
            'niet automatisch overgenomen; gebruik zo nodig JJ_RECOVER_SYMBOLS.')

DEFAULT_SYMBOLS = '''BTC ETH SOL BNB XRP ADA AVAX DOT NEAR SUI APT SEI FTM HBAR THETA LINK ARB OP INJ TIA FET RENDER TAO STRK MANTA ALT DYM ZETA PORTAL AXL ENA CRV LDO JUP ORDI DOGE SHIB PEPE WIF WLD COR PUMP BOME MEW PNUT ACT GOAT NEIRO POPCAT FLOKI BONK CHZ RUNE ATOM ALGO SAND MANA AXS GALA GMT'''.split()
SYMBOLS = list(dict.fromkeys(x.strip().upper() for x in os.getenv('JJ_SYMBOLS', ','.join(x+'-USDT' for x in DEFAULT_SYMBOLS)).split(',') if x.strip()))
session = requests.Session()
meta = {}
state = {'mode': 'paper' if PAPER_TRADING else 'demo' if BLOFIN_DEMO else 'live',
         'managed': {}, 'seen': {}, 'last_exit': {}, 'paper_realized': 0.0, 'reports': {}}
health = {'last_cycle': 0, 'error': ''}
log = logging.getLogger('ebp')

def notify(message):
    log.info(message)
    if TG_TOKEN and TG_CHAT:
        try:
            response = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id':TG_CHAT,'text':message,'disable_web_page_preview':True}, timeout=8)
            if response.status_code != 200:
                log.warning('Telegram rejected message: HTTP %s', response.status_code)
                return False
            return True
        except requests.RequestException:
            log.warning('Telegram unavailable')  # never log token-containing URL
            return False
    return True

def num(value):
    x = float(value)
    if not math.isfinite(x):
        raise ValueError('Non-finite API value')
    return x

def amount(value):
    s = format(Decimal(str(value)), 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s

def step(value, quantum, up=False):
    d, q = Decimal(str(value)), Decimal(str(quantum))
    if q <= 0:
        raise ValueError('Invalid tick/lot size')
    return float((d/q).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*q)

def save():
    # A failed durable write must propagate and stop trading.
    temp = STATE_FILE + '.tmp'
    with open(temp, 'w', encoding='utf8') as f:
        json.dump(state, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)

def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf8') as f:
            saved = json.load(f)
        if saved.get('mode') != state['mode']:
            raise RuntimeError('State mode mismatch')
        for key in ('managed','seen','last_exit'):
            if not isinstance(saved.get(key), dict):
                raise RuntimeError('Invalid state: '+key)
        state.update(saved)

def api(method, path, params=None, body=None, private=False):
    query = urlencode(params or {})
    request_path = path + ('?'+query if query else '')
    body_text = json.dumps(body, separators=(',', ':')) if body is not None else ''
    # GET retries only. Never blindly retry POST after an ambiguous timeout.
    for attempt in range(3 if method == 'GET' else 1):
        headers = {'Content-Type':'application/json'}
        if private:
            if PAPER_TRADING:
                raise RuntimeError('Private API forbidden in paper mode')
            stamp, nonce = str(int(time.time()*1000)), uuid4().hex
            digest = hmac.new(API_SECRET.encode(),
                (request_path+method+stamp+nonce+body_text).encode(), hashlib.sha256).hexdigest()
            headers.update({'ACCESS-KEY':API_KEY,'ACCESS-PASSPHRASE':API_PASSWORD,
                'ACCESS-TIMESTAMP':stamp,'ACCESS-NONCE':nonce,
                'ACCESS-SIGN':base64.b64encode(digest.encode()).decode()})
        try:
            response = session.request(method, BASE_URL+request_path, headers=headers,
                data=body_text.encode() if body is not None else None, timeout=(5,10))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or str(result.get('code')) != '0':
                raise RuntimeError(f'{path}: {result.get("code")} {result.get("msg")}')
            data = result.get('data')
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and 'code' in item and str(item['code']) != '0':
                    raise RuntimeError(f'{path}: nested {item["code"]} {item.get("msg")}')
            return data
        except (requests.RequestException, ValueError):
            if attempt == (2 if method == 'GET' else 0):
                raise RuntimeError(f'{path}: network/response failure; POST outcome may be unknown') from None
            time.sleep(.5*(attempt+1))

def get(path, params=None, private=False):
    return api('GET', path, params=params, private=private)
def post(path, body):
    return api('POST', path, body=body, private=True)
def one(data):
    if isinstance(data, list):
        if len(data) != 1:
            raise RuntimeError('Expected exactly one API result')
        return data[0]
    if not isinstance(data, dict):
        raise RuntimeError('Expected API object')
    return data

def candles(symbol):
    rows = []
    for c in get('/api/v1/market/candles', {'instId':symbol,'bar':BAR,'limit':'80'}):
        if not isinstance(c, list) or len(c) < 9 or str(c[8]) != '1':
            continue
        rows.append([int(c[0]), *[num(v) for v in c[1:6]]])
    df = pd.DataFrame(rows, columns=['ts','open','high','low','close','volume'])
    return df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)

def detect_bullish_ebp(df):
    return (df.low < df.low.shift()) & (df.close > df.open.shift())
def detect_bearish_ebp(df):
    return (df.high > df.high.shift()) & (df.close < df.open.shift())

def signal(symbol):
    df = candles(symbol)
    if len(df) < 20:
        return None, 'few_candles'
    row = df.iloc[-1]
    stamp = int(row.ts)
    if int(state['seen'].get(symbol, 0)) >= stamp:
        return None, 'already_seen'
    age = time.time() - (stamp+BAR_MS[BAR])/1000
    if age < 0 or age > MAX_AGE:
        return None, 'stale'
    bull, bear = bool(detect_bullish_ebp(df).iloc[-1]), bool(detect_bearish_ebp(df).iloc[-1])
    if bull == bear:
        return None, 'ambiguous' if bull else 'no_ebp'
    return {'symbol':symbol,'direction':'long' if bull else 'short','ts':stamp,
            'close':float(row.close),'low':float(row.low),'high':float(row.high)}, 'signal'

def positions():
    data = get('/api/v1/account/positions', private=True)
    if not isinstance(data, list):
        raise RuntimeError('Invalid positions response')
    return {p['instId']:p for p in data if num(p['positions']) != 0}

def balance():
    if PAPER_TRADING:
        return PAPER_BALANCE + state['paper_realized'] - sum(p['notional']/LEV for p in state['managed'].values())
    data = one(get('/api/v1/account/balance', private=True))
    for item in data.get('details', []):
        if item.get('currency') == 'USDT':
            return num(item['available'])
    raise RuntimeError('USDT available balance missing')

def mark(symbol):
    return num(one(get('/api/v1/market/mark-price', {'instId':symbol}))['markPrice'])

def plan(sig, entry, available):
    m = meta[sig['symbol']]
    tick, cv, lot = num(m['tickSize']), num(m['contractValue']), num(m['lotSize'])
    long = sig['direction'] == 'long'
    stop = step(sig['low']*.998 if long else sig['high']*1.002, tick, up=not long)
    distance = entry-stop if long else stop-entry
    if distance <= 0 or not MIN_STOP_PCT <= distance/entry <= MAX_STOP_PCT:
        return None, 'invalid_stop'
    if abs(entry-sig['close']) > MAX_CHASE_R*distance:
        return None, 'entry_drift'
    tp = step(entry + distance*RR if long else entry-distance*RR, tick, up=long)
    if tp <= 0 or cv <= 0:
        return None, 'invalid_contract'
    size = step(max(MARGIN*LEV/(cv*entry), num(m['minSize'])), lot, up=True)
    notional = size*cv*entry
    risk = size*cv*distance + notional*(2*FEE+SLIPPAGE)
    if risk > RISK_CAP:
        return None, 'risk_cap'
    needed = notional/LEV + notional*(2*FEE+SLIPPAGE)
    balance_diagnostic.update(symbol=sig['symbol'], available=available, required=needed/.88,
                              ts=time.time())
    if needed > available*.88:
        balance_diagnostic['blocked'] = True
        return None, 'balance'
    balance_diagnostic['blocked'] = False
    if size > num(m['maxMarketSize']):
        return None, 'max_size'
    return {'direction':sig['direction'],'entry_price':entry,'stop':stop,'tp':tp,
            'contracts':size,'notional':notional,'risk_estimate':risk,'signal_ts':sig['ts'],
            'entry_ts':time.time(),'status':'open'}, 'ok'

def close_live(symbol, p, reason):
    # Retain record until exchange position is confirmed zero. No blind retries.
    current = positions().get(symbol)
    if not current:
        return
    size = num(current['positions'])
    expected_long = p['direction'] == 'long'
    if (size > 0) != expected_long or current['marginMode'] != 'isolated':
        raise RuntimeError('Position changed externally: manual reconciliation needed')
    p['status'] = 'closing'
    p['close_reason'] = reason
    p['close_client_id'] = 'jjclose'+uuid4().hex[:24]
    save()
    post('/api/v1/trade/order', {'instId':symbol,'marginMode':'isolated','positionSide':'net',
        'side':'sell' if size > 0 else 'buy','orderType':'market','size':amount(abs(size)),
        'reduceOnly':'true','clientOrderId':p['close_client_id']})
    notify(f'CLOSE aangevraagd {symbol}: {reason}; wacht op exchangebevestiging')

def protected(symbol, p):
    orders = get('/api/v1/trade/orders-tpsl-pending', {'instId':symbol}, private=True)
    for o in orders:
        if o.get('positionSide') != 'net' or o.get('marginMode') != 'isolated':
            continue
        if o.get('side') != ('sell' if p['direction']=='long' else 'buy'):
            continue
        if o.get('slTriggerPriceType') != 'mark' or o.get('tpTriggerPriceType') != 'mark':
            continue
        if o.get('slOrderPrice') != '-1' or o.get('tpOrderPrice') != '-1':
            continue
        qty = num(o['size'])
        if qty != -1 and qty < p['contracts']:
            continue
        tick = num(meta[symbol]['tickSize'])
        if abs(num(o['slTriggerPrice'])-p['stop']) < tick/2 and abs(num(o['tpTriggerPrice'])-p['tp']) < tick/2:
            return True
    return False

def reconcile_pending(symbol, p):
    detail = one(get('/api/v1/trade/order-detail',
        {'instId':symbol,'clientOrderId':p['client_id']}, private=True))
    if detail.get('state') not in ('filled','canceled'):
        raise RuntimeError('Entry order unresolved; new entries blocked')
    filled = num(detail['filledSize'])
    if filled <= 0:
        del state['managed'][symbol]
        save()
        notify(f'Geen fill {symbol}; order afgesloten')
        return False
    if detail.get('positionId'):
        p['position_id'] = str(detail['positionId'])
    p.update(contracts=filled, entry_price=num(detail['averagePrice']), status='open')
    p['original_contracts'] = filled
    p['notional'] = filled*num(meta[symbol]['contractValue'])*p['entry_price']
    save()
    notify(f'🟢 OPEN {symbol} {p["direction"].upper()} @ {p["entry_price"]:.8g}\n'
           f'{filled:g} contracten | TP {p["tp"]:.8g} | SL {p["stop"]:.8g}\n'
           f'Nominale positie: {p["notional"]:.2f} USDT | marge indicatief: {p["notional"]/LEV:.2f} USDT')
    return True

def sample_trade(symbol, p, price):
    """Measure existing mark samples; no additional exchange order or exit rule."""
    stamp = time.time()
    direction = 1 if p['direction']=='long' else -1
    units = p['contracts']*num(meta[symbol]['contractValue'])
    gross = direction*(price-p['entry_price'])*units
    approx_net = gross - units*(p['entry_price']+price)*FEE
    m = p.setdefault('metrics', {})
    first = not m
    if first:
        m.update(started=stamp, samples=0, peak_gross=gross, trough_gross=gross,
                 peak_after_estimated_fees=approx_net, peak_ts=stamp, trough_ts=stamp)
    p.setdefault('original_stop', p['stop'])
    p.setdefault('original_tp', p['tp'])
    p.setdefault('original_contracts', p['contracts'])
    if gross > m['peak_gross']:
        m.update(peak_gross=gross, peak_ts=stamp)
    if gross < m['trough_gross']:
        m.update(trough_gross=gross, trough_ts=stamp)
    m['peak_after_estimated_fees'] = max(m['peak_after_estimated_fees'], approx_net)
    m.update(samples=m['samples']+1, last_ts=stamp, last_mark=price,
             last_gross=gross, last_after_estimated_fees=approx_net)
    # Extremes are durable at every sample, including just before exit handling.
    save()

def metric_text(p):
    m = p.get('metrics')
    if not m:
        return 'Winstverloop: niet gemeten (positie voor deze update gesloten)'
    return (f"Beste/slechtste gemeten bruto-PnL: {m['peak_gross']:+.2f} / "
            f"{m['trough_gross']:+.2f} USDT\n"
            f"Beste na geschatte handelskosten: {m['peak_after_estimated_fees']:+.2f} USDT "
            f"(zonder funding; {m['samples']} koersmetingen)\n"
            "Gemeten vanaf deze botversie; pieken tussen metingen kunnen ontbreken")

def manage():
    live_positions = {} if PAPER_TRADING else positions()
    ok = True
    for symbol, p in list(state['managed'].items()):
        try:
            if not PAPER_TRADING and p['status'] == 'pending':
                if not reconcile_pending(symbol, p):
                    continue
                live_positions = positions()
            if not PAPER_TRADING and symbol not in live_positions:
                p['zero_checks'] = p.get('zero_checks',0)+1
                if p['zero_checks'] < 2:
                    ok = False
                    continue
                key = uuid4().hex
                state.setdefault('reports', {})[key] = {'symbol':symbol, 'position':dict(p),
                    'observed_closed':time.time(), 'next_attempt':0, 'waiting_sent':False}
                del state['managed'][symbol]
                state['last_exit'][symbol] = time.time()
                save()
                continue
            p['zero_checks'] = 0
            if not PAPER_TRADING:
                current = live_positions[symbol]
                if current.get('positionId') and not p.get('position_id'):
                    p['position_id'] = str(current['positionId'])
                    save()
                if current.get('createTime') and not p.get('exchange_entry_ts'):
                    p['exchange_entry_ts'] = num(current['createTime'])/1000
                    save()
                q = num(current['positions'])
                if (q > 0) != (p['direction']=='long') or abs(q) != p['contracts'] or current['marginMode'] != 'isolated':
                    raise RuntimeError('Position differs from bot state; new entries blocked')
                if p['status'] == 'closing':
                    detail = one(get('/api/v1/trade/order-detail',
                        {'instId':symbol,'clientOrderId':p['close_client_id']}, private=True))
                    if detail.get('state') in ('filled','canceled'):
                        # A terminal partial close may leave a residual position.
                        p['contracts'] = abs(q)
                        close_live(symbol, p, 'remaining position after close')
                    ok = False
                    continue
            price = mark(symbol)
            sample_trade(symbol, p, price)
            long = p['direction']=='long'
            hit_sl = price <= p['stop'] if long else price >= p['stop']
            hit_tp = price >= p['tp'] if long else price <= p['tp']
            if PAPER_TRADING and (hit_sl or hit_tp):
                pnl = (price-p['entry_price'])*(1 if long else -1)*p['contracts']*num(meta[symbol]['contractValue'])
                pnl -= p['contracts']*num(meta[symbol]['contractValue'])*(price+p['entry_price'])*FEE
                pnl -= p['notional']*SLIPPAGE
                state['paper_realized'] += pnl
                del state['managed'][symbol]
                state['last_exit'][symbol] = time.time()
                save()
                notify(f'PAPER CLOSE {symbol}: {"SL" if hit_sl else "TP"}; geschat netto {pnl:+.2f} USDT')
            elif not PAPER_TRADING:
                # Verify actual exchange TP/SL, not merely accepted entry fields.
                actual_distance = p['entry_price']-p['stop'] if long else p['stop']-p['entry_price']
                actual_risk = p['contracts']*num(meta[symbol]['contractValue'])*max(actual_distance, 0) + p['notional']*(2*FEE+SLIPPAGE)
                has_protection = protected(symbol, p)
                invalid_fill = actual_distance <= 0 or actual_risk > RISK_CAP
                if hit_sl or hit_tp or not has_protection or invalid_fill:
                    close_live(symbol, p, 'bot SL reached' if hit_sl else 'bot TP reached' if hit_tp else 'fill risk invalid' if invalid_fill else 'exchange TP/SL missing')
                    ok = False
        except OSError:
            raise
        except Exception as exc:
            ok = False
            log.error('Management %s: %s', symbol, exc)
    health['last_cycle'] = time.time()
    return ok, live_positions

class ReportPending(RuntimeError):
    pass

def matched_history(symbol, p, closed_at):
    """Position ID + close window, price and quantity; never guess a trade."""
    params = {'instId':symbol, 'limit':'100'}
    pid = str(p.get('position_id') or '')
    if pid:
        # Avoid relying on a server timestamp filter's creation/update semantics.
        params['positionId'] = pid
    pages, rows, cursors = 0, [], set()
    while pages < 5:
        batch = get('/api/v1/account/positions-history', params, private=True)
        if not isinstance(batch, list):
            raise ReportPending('Ongeldig antwoord op positiehistorie')
        rows.extend(batch)
        pages += 1
        if len(batch) < 100:
            break
        cursor = str(batch[-1].get('historyId') or '')
        if not cursor or cursor in cursors:
            raise ReportPending('Historie kon niet volledig worden gepagineerd')
        cursors.add(cursor)
        params['after'] = cursor
    else:
        raise ReportPending('Te veel historie; controleer positie-ID')
    expected_start = p.get('exchange_entry_ts', p['entry_ts'])
    found, seen, rejects = [], set(), Counter()
    for h in rows:
        if h.get('instId') != symbol or h.get('marginMode') != 'isolated':
            continue
        if h.get('side') != ('buy' if p['direction']=='long' else 'sell'):
            rejects['richting'] += 1
            continue
        opened, closed = num(h['createTime'])/1000, num(h['updateTime'])/1000
        # A position ID is stronger evidence than local order submission time.
        # Close time, entry price and total quantity must still all match.
        if pid and str(h.get('positionId') or '') != pid:
            rejects['positie-ID'] += 1
            continue
        if not pid and abs(opened-expected_start) > 120:
            rejects['openingstijd'] += 1
            continue
        if not p['entry_ts']-120 <= closed <= closed_at+120:
            rejects['sluitingstijd'] += 1
            continue
        if abs(num(h['openAveragePrice'])-p['entry_price']) > num(meta[symbol]['tickSize'])*2:
            rejects['instapprijs'] += 1
            continue
        hid = str(h.get('historyId') or '')
        if not hid:
            rejects['historie-ID'] += 1
            continue
        if hid in seen:
            continue
        seen.add(hid)
        found.append(h)
    if not found:
        detail = ', '.join(f'{k}: {v}' for k,v in rejects.items())
        raise ReportPending('Geen passende afrekening'+(' ('+detail+')' if detail else '; historie nog leeg'))
    if len({str(h.get('positionId') or '') for h in found}) != 1:
        raise ReportPending('Meerdere posities passen; afrekening niet gekoppeld')
    quantity = sum(abs(num(h['closePositions'])) for h in found)
    expected = p.get('original_contracts', p['contracts'])
    if abs(quantity-expected) > num(meta[symbol]['lotSize'])/100:
        raise ReportPending(f'Gesloten aantal past niet: historie {quantity:g}, verwacht {expected:g}')
    return sorted(found, key=lambda h: num(h['updateTime']))

def confirmed_reason(symbol, p, rows):
    if any(num(h.get('liquidationPositions',0)) > 0 for h in rows):
        return 'Liquidatie gemeld door BloFin'
    orders = get('/api/v1/trade/orders-history', {'instId':symbol,'limit':'100'}, private=True)
    categories = set()
    close_ids = set()
    for o in orders:
        if o.get('instId') != symbol or o.get('side') != ('sell' if p['direction']=='long' else 'buy'):
            continue
        if o.get('state') not in ('filled','canceled') or num(o.get('filledSize',0)) <= 0:
            continue
        stamp = num(o['updateTime'])
        if not min(num(h['updateTime']) for h in rows)-120000 <= stamp <= max(num(h['updateTime']) for h in rows)+120000:
            continue
        if p.get('position_id') and o.get('positionId') and str(o['positionId']) != p['position_id']:
            continue
        category = o.get('orderCategory')
        if category in ('tp','sl','adl','full_liquidation','partial_liquidation'):
            categories.add(category)
        if o.get('clientOrderId') == p.get('close_client_id') and p.get('close_client_id'):
            close_ids.add(o['clientOrderId'])
    if len(categories)==1:
        return {'tp':'Take-profit uitgevoerd', 'sl':'Stop-loss uitgevoerd',
            'adl':'Automatische deleveraging', 'full_liquidation':'Liquidatie',
            'partial_liquidation':'Gedeeltelijke liquidatie'}[next(iter(categories))]
    if close_ids:
        reason = p.get('close_reason','')
        if reason == 'exchange TP/SL missing':
            return 'Veiligheidssluiting: TP/SL ontbrak'
        if reason == 'fill risk invalid':
            return 'Veiligheidssluiting: risico na uitvoering te hoog'
        if reason == 'bot TP reached':
            return 'Door de bot gesloten: TP-niveau bereikt (sluitorder bevestigd)'
        if reason == 'bot SL reached':
            return 'Door de bot gesloten: SL-niveau bereikt (sluitorder bevestigd)'
        return 'Door de bot gesloten (sluitorder bevestigd)'
    return 'Sluiting bevestigd; oorzaak niet bevestigd'

def report_values(rows, funding):
    quantity = sum(abs(num(h['closePositions'])) for h in rows)
    if quantity <= 0:
        raise ReportPending('Historie bevat geen gesloten hoeveelheid')
    gross = sum(num(h['realizedPnl']) for h in rows)
    fees = sum(num(h['fee']) for h in rows)
    return dict(gross=gross, fees=fees, funding=funding,
        net=gross-fees+funding if funding is not None else None,
        after_fees=gross-fees,
        exit_price=sum(abs(num(h['closePositions']))*num(h['closeAveragePrice']) for h in rows)/quantity)

def format_report(symbol, p, rows, funding, reason):
    v = report_values(rows, funding)
    opened = p.get('exchange_entry_ts', p['entry_ts'])*1000
    closed = max(num(h['updateTime']) for h in rows)
    value = v['net'] if v['net'] is not None else v['after_fees']
    text = [f'{"✅" if value >= 0 else "🔴"} {symbol} {p["direction"].upper()} gesloten']
    if v['net'] is not None:
        text.append(f'Netto na handelskosten/funding: {v["net"]:+.2f} USDT')
    else:
        text.append(f'Na handelskosten: {v["after_fees"]:+.2f} USDT; funding nog onbekend')
    text.append(f'Bruto: {v["gross"]:+.2f} | Handelskosten: {v["fees"]:.4f} USDT')
    if funding is not None:
        text.append(f'Funding: {funding:+.4f} USDT')
    text.extend([f'Instap {p["entry_price"]:.8g} → Uitstap {v["exit_price"]:.8g}',
        f'Duur: {max(0, (closed-opened)/60000):.0f} minuten',
        f'Reden: {reason}', metric_text(p)])
    return '\n'.join(text)

def funding_for(symbol, p, rows):
    begin = int(p.get('exchange_entry_ts', p['entry_ts'])*1000)
    end = max(int(h['updateTime']) for h in rows)
    if not 0 <= end-begin <= 7*86400000 or time.time()*1000-begin > 7*86400000:
        return None
    bills = get('/api/v1/account/funding-fees', {'instId':symbol,
                'begin':str(begin),'end':str(end),'limit':'1000'}, private=True)
    if not isinstance(bills,list) or len(bills)>=1000:
        return None
    return sum(num(f['fundingFee']) for f in bills
        if f.get('instId')==symbol and f.get('marginMode')=='isolated'
        and begin <= num(f['fundingTime']) <= end)

def archive_report(key, symbol, p, rows, funding, reason):
    v = report_values(rows, funding)
    record = dict(symbol=symbol, direction=p['direction'], entry_price=p['entry_price'],
        entry_ts=p.get('exchange_entry_ts',p['entry_ts']),
        closed_ts=max(num(h['updateTime']) for h in rows)/1000,
        reason=reason, history_ids=[str(h['historyId']) for h in rows],
        metrics=dict(p.get('metrics',{})), original_stop=p.get('original_stop',p['stop']),
        original_tp=p.get('original_tp',p['tp']), **v)
    journal = state.setdefault('journal',{})
    journal[key] = record
    while len(journal)>1000:
        del journal[next(iter(journal))]

def process_reports():
    if PAPER_TRADING:
        return
    # Old unmatched reports must not starve newer trades.
    due = sorted(state.setdefault('reports',{}).items(),
                 key=lambda item: item[1].get('next_attempt',0))
    for key, report in due:
        if time.time() < report.get('next_attempt',0):
            break
        symbol, p = report['symbol'], report['position']
        report['next_attempt'] = time.time()+60
        report['attempts'] = report.get('attempts',0)+1
        save()
        try:
            rows = matched_history(symbol,p,report['observed_closed'])
            if rows is None:  # compatibility with earlier matchers/test adapters
                raise ReportPending('Historie nog niet gekoppeld')
            funding = None
            try:
                funding = funding_for(symbol,p,rows)
            except Exception:
                pass
            try:
                reason = confirmed_reason(symbol,p,rows)
            except Exception:
                reason = 'Sluiting bevestigd; oorzaak niet bevestigd'
            # Save evidence BEFORE notification; a Telegram error does not lose data.
            archive_report(key,symbol,p,rows,funding,reason)
            save()
            if notify(format_report(symbol,p,rows,funding,reason)):
                del state['reports'][key]
                save()
        except OSError:
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc,ReportPending) else 'API/datafout; bekijk Railway-log'
            report['last_problem'] = detail
            log.warning('Afrekening %s: %s',symbol,exc)
            if time.time()-report.get('last_notice',0) >= 1800:
                delay = max(0,int((time.time()-report['observed_closed'])/60))
                if notify(f'🔒 {symbol} gesloten; resultaat nog niet bevestigd ({delay} min).\n'
                          f'{detail}. De bot probeert opnieuw; dit is geen winst/verliesmelding.'):
                    report['last_notice'] = time.time()
            save()
        return

def trailing_volume(symbol):
    """Exact quote volume of last 96 consecutive completed 15m candles.
    No approximate base-volume * last-price conversion. Cached for one minute.
    """
    cached = liquidity_cache.get(symbol)
    if cached and time.time()-cached['checked'] < 60:
        return cached['volume']
    data = get('/api/v1/market/candles', {'instId':symbol,'bar':'15m','limit':'100'})
    rows = {}
    for c in data:
        if isinstance(c,list) and len(c)>=9 and str(c[8])=='1':
            ts, vol = int(c[0]), num(c[7])  # documented quote-currency volume
            if vol < 0:
                raise RuntimeError('Negative quote volume')
            rows[ts] = vol
    stamps = sorted(rows)[-96:]
    if len(stamps)!=96 or any(y-x!=900000 for x,y in zip(stamps,stamps[1:])):
        raise RuntimeError('Incomplete 24h liquidity data')
    age = time.time()*1000-(stamps[-1]+900000)
    if not -5000 <= age <= 1020000:
        raise RuntimeError('Stale volume data')
    total = sum(rows[t] for t in stamps)
    liquidity_cache[symbol] = {'volume':total,'checked':time.time()}
    return total

def book_side(levels, contracts, cv, buy, entry_notional):
    if not isinstance(levels,list) or not levels:
        return False
    parsed = [(num(x[0]),num(x[1])) for x in levels if isinstance(x,list) and len(x)>=2]
    if not parsed or any(price<=0 or qty<0 for price,qty in parsed):
        return False
    parsed.sort(key=lambda x:x[0], reverse=not buy)
    best = parsed[0][0]
    band = [(price,qty) for price,qty in parsed
        if abs(price/best-1)<=DEPTH_BAND]
    if sum(price*qty*cv for price,qty in band) < entry_notional*DEPTH_MULT:
        return False
    remaining, cost = contracts, 0.0
    for price,qty in band:
        take = min(remaining,qty)
        cost += price*take
        remaining -= take
        if remaining <= contracts*1e-10:
            break
    if remaining > contracts*1e-10:
        return False
    vwap = cost/contracts
    impact = vwap/best-1 if buy else 1-vwap/best
    return impact <= MAX_IMPACT

def liquidity_check(symbol, p):
    try:
        if trailing_volume(symbol) < MIN_VOLUME:
            return 'liquidity_volume'
        book = one(get('/api/v1/market/books', {'instId':symbol,'size':'100'}))
        if abs(time.time()*1000-num(book['ts'])) > 10000:
            return 'liquidity_stale'
        cv = num(meta[symbol]['contractValue'])
        # Both opening and eventual closing sides must have sufficient depth.
        if not book_side(book['asks'],p['contracts'],cv,True,p['notional']):
            return 'liquidity_depth'
        if not book_side(book['bids'],p['contracts'],cv,False,p['notional']):
            return 'liquidity_depth'
        ask = min(num(x[0]) for x in book['asks'])
        bid = max(num(x[0]) for x in book['bids'])
        if bid<=0 or ask<bid or (ask-bid)/((ask+bid)/2)>MAX_SPREAD:
            return 'liquidity_spread'
        return 'ok'
    except OSError:
        raise
    except Exception as exc:
        log.warning('Liquiditeitsdata %s niet bruikbaar: %s',symbol,exc)
        return 'liquidity_data'


def open_signal(sig):
    symbol = sig['symbol']
    ticker = one(get('/api/v1/market/tickers', {'instId':symbol}))
    ask, bid = num(ticker['askPrice']), num(ticker['bidPrice'])
    if bid <= 0 or ask < bid or (ask-bid)/((ask+bid)/2) > MAX_SPREAD:
        return 'spread'
    entry = ask if sig['direction']=='long' else bid
    p, reason = plan(sig, entry, balance())
    if not p:
        return reason
    liquidity = liquidity_check(symbol, p)
    if liquidity != 'ok':
        return liquidity
    current_mark = mark(symbol)
    inside = p['stop'] < current_mark < p['tp'] if sig['direction']=='long' else p['tp'] < current_mark < p['stop']
    if not inside:
        return 'mark_outside_levels'
    fresh = one(get('/api/v1/market/tickers', {'instId':symbol}))
    fresh_ask, fresh_bid = num(fresh['askPrice']), num(fresh['bidPrice'])
    if abs(time.time()*1000-num(fresh['ts'])) > 10000:
        return 'liquidity_stale'
    if fresh_bid <= 0 or fresh_ask < fresh_bid or (fresh_ask-fresh_bid)/((fresh_ask+fresh_bid)/2)>MAX_SPREAD:
        return 'liquidity_spread'
    fresh_entry = fresh_ask if p['direction']=='long' else fresh_bid
    if abs(fresh_entry/entry-1)>MAX_IMPACT:
        return 'liquidity_price_moved'
    # Keep original sizing/levels only when price stayed inside the allowed tolerance.
    if not PAPER_TRADING:
        if symbol in positions():
            return 'existing_exchange_position'
        post('/api/v1/account/set-leverage',
            {'instId':symbol,'leverage':str(LEV),'marginMode':'isolated'})
        p['status'] = 'pending'
        p['client_id'] = 'jjebp'+uuid4().hex[:27]
    # Persist intent BEFORE sending order. Unknown POST result is reconciled by ID.
    state['managed'][symbol] = p
    state['seen'][symbol] = sig['ts']
    save()
    if PAPER_TRADING:
        notify(f'PAPER OPEN {symbol} {p["direction"]}: entry {entry:.8g}, SL {p["stop"]:.8g}, TP {p["tp"]:.8g}, risico ~{p["risk_estimate"]:.2f} USDT')
    else:
        post('/api/v1/trade/order', {'instId':symbol,'marginMode':'isolated','positionSide':'net',
            'side':'buy' if p['direction']=='long' else 'sell','orderType':'market',
            'size':amount(p['contracts']),'reduceOnly':'false','clientOrderId':p['client_id'],
            'slTriggerPrice':amount(p['stop']),'slOrderPrice':'-1','slTriggerPriceType':'mark',
            'tpTriggerPrice':amount(p['tp']),'tpOrderPrice':'-1','tpTriggerPriceType':'mark'})
        time.sleep(1)
        manage()
    return 'opened_or_pending'

def recover_existing(external):
    """Explicit, read-only exchange recovery; never creates or cancels orders."""
    unknown = set(external) - set(state['managed'])
    if unknown - RECOVER_SYMBOLS:
        raise RuntimeError('Unmanaged existing positions; set JJ_RECOVER_SYMBOLS only after review: '
                           + ','.join(sorted(unknown - RECOVER_SYMBOLS)))
    staged = {}
    for symbol in sorted(unknown):
        current = external[symbol]
        if symbol not in meta:
            raise RuntimeError('Recovery instrument unavailable: '+symbol)
        if current.get('positionSide') != 'net' or current.get('marginMode') != 'isolated':
            raise RuntimeError('Recovery requires isolated net position: '+symbol)
        if num(current.get('leverage')) != LEV:
            raise RuntimeError('Recovery leverage differs from configuration: '+symbol)
        q, entry = num(current['positions']), num(current['averagePrice'])
        cv = num(meta[symbol]['contractValue'])
        created = num(current.get('createTime'))/1000
        if q == 0 or min(entry, cv, created) <= 0 or created > time.time()+60:
            raise RuntimeError('Incomplete recovery position data: '+symbol)
        direction = 'long' if q > 0 else 'short'
        candidates = []
        for o in get('/api/v1/trade/orders-tpsl-pending', {'instId':symbol}, private=True):
            if o.get('instId') != symbol or o.get('positionSide') != 'net' or o.get('marginMode') != 'isolated':
                continue
            if o.get('side') != ('sell' if q > 0 else 'buy'):
                continue
            if o.get('slTriggerPriceType') != 'mark' or o.get('tpTriggerPriceType') != 'mark':
                continue
            if o.get('slOrderPrice') != '-1' or o.get('tpOrderPrice') != '-1':
                continue
            size = num(o.get('size'))
            if size != -1 and size < abs(q):
                continue
            stop, tp = num(o.get('slTriggerPrice')), num(o.get('tpTriggerPrice'))
            if not (0 < stop < entry < tp if q > 0 else 0 < tp < entry < stop):
                continue
            candidates.append((stop, tp))
        if len(candidates) != 1:
            raise RuntimeError('Recovery needs exactly one matching full-position mark-price market TP/SL: '+symbol)
        stop, tp = candidates[0]
        staged[symbol] = dict(entry_ts=created, entry_price=entry, direction=direction,
            stop=stop, tp=tp, contracts=abs(q), notional=abs(q)*cv*entry,
            risk_estimate=abs(q)*cv*abs(entry-stop), status='open',
            position_id=str(current.get('positionId') or ''), recovered=True)
    return staged

def startup():
    if MIN_VOLUME<=0 or DEPTH_MULT<1 or not 0<DEPTH_BAND<1 or not 0<MAX_IMPACT<1 or not 0<MAX_SPREAD<1:
        raise ValueError('Invalid liquidity configuration')
    if BAR not in BAR_MS or min(MARGIN,LEV,RR,MAX_POS,SCAN_SECONDS,RISK_CAP,MAX_AGE) <= 0:
        raise ValueError('Invalid configuration')
    if min(FEE,SLIPPAGE,COOLDOWN,MIN_STOP_PCT) < 0 or MAX_STOP_PCT <= MIN_STOP_PCT:
        raise ValueError('Invalid risk configuration')
    if os.getenv('JJ_MARGIN_MODE', 'isolated').lower() != 'isolated':
        raise ValueError('This version supports isolated only')
    storage_notice = state_storage_notice()
    if storage_notice:
        notify(storage_notice)
    load()
    state.setdefault('reports', {})
    state.setdefault('journal', {})
    instruments = get('/api/v1/market/instruments')
    meta.update({m['instId']:m for m in instruments if m.get('state')=='live'
        and m.get('contractType')=='linear' and m.get('settleCurrency')=='USDT'})
    missing = [s for s in SYMBOLS if s not in meta or num(meta[s]['maxLeverage']) < LEV]
    allowed = [s for s in SYMBOLS if s not in missing]
    if any(s not in meta for s in state['managed']):
        raise RuntimeError('Managed instrument unavailable: reconcile manually')
    if not allowed:
        raise RuntimeError('No supported instruments')
    if not PAPER_TRADING:
        if not all((API_KEY,API_SECRET,API_PASSWORD)):
            raise RuntimeError('API credentials missing')
        mode = one(get('/api/v1/account/position-mode', private=True))
        if mode['positionMode'] != 'net_mode':
            raise RuntimeError('Requires net_mode; account mode is not changed automatically')
        external = positions()
        recovered = recover_existing(external)
        orders = get('/api/v1/trade/orders-pending', private=True)
        if any(o.get('clientOrderId') not in {p.get('client_id') for p in state['managed'].values()} for o in orders):
            raise RuntimeError('Unmanaged pending orders; reconcile before start')
        if recovered:
            state['managed'].update(recovered)
            save()
            for symbol, p in recovered.items():
                notify(f"♻️ {symbol} {p['direction'].upper()} hersteld; "
                       f"entry {p['entry_price']:g}; TP {p['tp']:g}; SL {p['stop']:g}. "
                       "Bestaande BloFin TP/SL overgenomen; geen nieuwe entry geplaatst.")
    notify(f'JJ EBP v{VERSION} gestart: {state["mode"]}, {len(allowed)} munten, {BAR}, marge >=~{MARGIN:g}, {LEV}x, {RR:g}R, risicolimiet ~{RISK_CAP:g} USDT')
    notify(f'Liquiditeitsfilter: 24u volume >= {MIN_VOLUME:,.0f} USDT; spread <= {MAX_SPREAD*100:.2f}%; diepte beide zijden >= {DEPTH_MULT:g}x positie binnen {DEPTH_BAND*100:.2f}%')
    if missing:
        notify('Overgeslagen (niet beschikbaar / leverage): '+', '.join(missing))
    return allowed

SCAN_LABELS = {'stale':'buiten instapvenster', 'no_ebp':'geen EBP',
    'balance':'onvoldoende vrij saldo', 'spread':'spread te groot',
    'entry_drift':'prijs te ver van signaal', 'risk_cap':'risicolimiet',
    'invalid_stop':'stopafstand ongeldig', 'cooldown':'wachttijd na sluiting',
    'opened_or_pending':'entry geplaatst/in afwachting', 'liquidity_volume':'24u volume te laag',
    'liquidity_depth':'orderboek onvoldoende', 'liquidity_spread':'spread te groot (boek)',
    'liquidity_stale':'liquiditeitsdata te oud', 'liquidity_data':'liquiditeitsdata ontbreekt',
    'already_seen':'signaal al verwerkt', 'errors':'scanfout'}

def status_report(counts):
    text = [f'📊 Status: {len(state["managed"])} beheerde posities']
    try:
        available = balance()
        text.append(f'Vrij beschikbaar: {available:.2f} USDT | ingestelde marge: {MARGIN:g} USDT')
    except Exception:
        text.append('Vrij saldo: tijdelijk niet beschikbaar')
    if balance_diagnostic.get('blocked'):
        age = int((time.time()-balance_diagnostic['ts'])/60)
        text.append(f'Laatste saldo-afwijzing ({age} min geleden): {balance_diagnostic["symbol"]}; '
                    f'nodig incl. kosten/buffer {balance_diagnostic["required"]:.2f} USDT; '
                    f'toen vrij {balance_diagnostic["available"]:.2f} USDT')
    for symbol,p in state['managed'].items():
        m = p.get('metrics',{})
        detail = f'{symbol} {p["direction"].upper()} | TP {p["tp"]:.8g} | SL {p["stop"]:.8g}'
        if m:
            detail += (f' | laatst gemeten bruto {m["last_gross"]:+.2f} USDT'
                       f' ({int(time.time()-m["last_ts"])} sec geleden)'
                       f' | beste {m["peak_gross"]:+.2f}')
        text.append(detail)
    if counts:
        text.append('Controles afgelopen periode (kunnen hetzelfde signaal herhalen):')
        text.extend(f'• {SCAN_LABELS.get(k,k)}: {v}' for k,v in counts.items())
    pending = state.get('reports',{})
    if pending:
        oldest = max(int((time.time()-r['observed_closed'])/60) for r in pending.values())
        text.append(f'Afrekening wacht: {len(pending)} trade(s); oudste {oldest} min')
    return '\n'.join(text)

def serve_health():
    try:
        from flask import Flask
    except ImportError:
        return
    app = Flask(__name__)
    @app.route('/')
    def home():
        fresh = time.time()-health['last_cycle'] < 120
        return {'bot':'JJ EBP','version':VERSION,'mode':state['mode'],
                'recent_cycle':fresh,'error':health['error']}, 200 if fresh and not health['error'] else 503
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','8080')), use_reloader=False)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Linux process lock prevents accidental duplicate runs using the same state.
    import fcntl
    prepare_state_directory()
    lock = open(STATE_FILE+'.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    symbols = startup()
    threading.Thread(target=serve_health, daemon=True).start()
    next_scan, last_report, last_alert = 0, 0, 0
    cursor = 0
    counts = Counter()
    while True:
        try:
            good, external = manage()
            process_reports()
            health['error'] = '' if good else 'position reconciliation blocked'
            if good and time.time() >= next_scan:
                # Rotate list to avoid favouring BTC/ETH every scan.
                ordered = symbols[cursor:]+symbols[:cursor]
                cursor = (cursor+1)%len(symbols)
                last_manage = time.monotonic()
                for symbol in ordered:
                    if time.monotonic()-last_manage >= 5:
                        good, external = manage()
                        last_manage = time.monotonic()
                        if not good:
                            break
                    if len(set(state['managed'])|set(external)) >= MAX_POS:
                        break
                    if symbol in state['managed'] or symbol in external:
                        continue
                    if time.time()-state['last_exit'].get(symbol,0) < COOLDOWN:
                        counts['cooldown'] += 1
                        continue
                    try:
                        sig, reason = signal(symbol)
                        if sig:
                            reason = open_signal(sig)
                            if any(p['status'] in ('pending','closing') for p in state['managed'].values()):
                                break
                        counts[reason] += 1
                    except OSError:
                        raise
                    except Exception as exc:
                        log.error('Scan %s: %s', symbol, exc)
                        counts['errors'] += 1
                        if any(p['status'] in ('pending','closing') for p in state['managed'].values()):
                            break
                    time.sleep(.10)
                next_scan = time.time()+SCAN_SECONDS
            if time.time()-last_report >= 1800:
                notify(status_report(counts))
                counts.clear()
                last_report = time.time()
        except KeyboardInterrupt:
            notify('Bot gestopt; live exchange TP/SL blijft staan. Paperposities blijven in state.')
            break
        except OSError:
            # Persistence failure must stop the process, not restart order submission.
            raise
        except Exception as exc:
            health['error'] = 'API/reconciliation error'
            log.error('Loop: %s', exc)
            if time.time()-last_alert >= 300:
                notify('API/reconciliatieprobleem: nieuwe entries geblokkeerd; bekijk log en BloFin.')
                last_alert = time.time()
        time.sleep(5)

if __name__ == '__main__':
    main()
