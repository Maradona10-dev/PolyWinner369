#!/usr/bin/env python3
"""CRT BloFin + Telegram v1.0 — automatic futures trading, paper by default.
Standalone new strategy; do not run alongside the EBP bot on the same account.
Closed 15m reference candle >=1.5 ATR; sweep/reclaim within 5 bars; closed 4H bias.
Engulfing/rejection/ADX optional and disabled by default. No separate second retest.
Entry occurs AFTER exchange confirms the signal candle, at the current bid/ask,
not at a guaranteed next-bar opening. Excessive delay/drift skips the trade.
Risk target 1% equity including estimated trading costs/stop slippage; gaps,
funding and actual fills can exceed this estimate. Contract sizes round DOWN.
Default notional cap 95% equity (not 95% margin); leverage is independently 12.
No fixed minimum margin. Exchange minSize above risk budget means no entry.
Daily/weekly gates use futures account equity, sampled; UTC periods; dedicated
USDT subaccount assumed (deposits/transfers change equity and these gates).
Four attempted entries/day; four confirmed net losses permanently block entries.
Weekly loss permanently blocks entries; day gate resets next UTC day.
Management/protection continues while entries are blocked. Missing account data,
unknown positions, unresolved closure accounting/funding block new entries.
Counters/state are LOCAL unless JJ_STATE_FILE points inside a mounted volume.
Without persistence deployment loses counters/reports; never deploy over open
positions without reconciliation. Old EBP state is NOT imported automatically.
Initial/recovered positions retain actual exchange TP/SL; realized RR may differ
from planned 1.8R due to fills/tick size/fees. No unverified RR guarantee.
Live TP/SL are market mark triggers; protection is checked after fill. Unknown
POST results reconcile by persisted client ID, not automatic resubmission.
Telegram uses EXCHANGE_API_KEY, EXCHANGE_SECRET, EXCHANGE_PASSWORD,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID from environment. No credentials in code.
PAPER_TRADING=True default: public data only, sampled exits, no funding model.
For exchange demo set PAPER_TRADING=False and BLOFIN_DEMO=True with demo keys.
For real live set PAPER_TRADING=False and BLOFIN_DEMO=False with real keys.
Install requests pandas flask. Python >=3.10, Linux/Railway process lock.
This file is a LIVE/PAPER LOOP, not the historical backtest. No profit claim.
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
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from uuid import uuid4
from urllib.parse import urlencode
from collections import Counter
import requests
import pandas as pd

VERSION = '1.0-CRT-BloFin'
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
BAR_MS = {'1m':60000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1H':3600000,'4H':14400000}
MARGIN = 1.0  # legacy validation only; no fixed margin in CRT sizing
LEV = int(os.getenv('JJ_LEVERAGE', '12'))
RR = envf('CRT_RR', 1.8)
MAX_POS = int(os.getenv('CRT_MAX_POSITIONS', '1'))
SCAN_SECONDS = envf('JJ_SCAN_SECONDS', 30)
RISK_CAP = envf('CRT_MAX_LOSS_USDT', 1e12)  # optional absolute cap; percent risk normally binds
FEE = envf('JJ_TAKER_FEE_RATE', .0004)  # each side; adjust to your actual tier
SLIPPAGE = envf('JJ_SLIPPAGE_ALLOWANCE', .0002)
MAX_SPREAD = envf('JJ_MAX_SPREAD_PCT', .001)  # 0.10%; applies only to entries
MIN_VOLUME = envf('JJ_MIN_24H_VOLUME_USDT', 1000000)
DEPTH_BAND = envf('JJ_DEPTH_BAND_PCT', .002)  # 0.20% around executable top price
DEPTH_MULT = envf('JJ_MIN_DEPTH_MULT', 10)  # each side >= 10x position notional
MAX_IMPACT = envf('JJ_MAX_BOOK_IMPACT_PCT', .001)  # 0.10% VWAP vs best price

CRT_MULT = envf('CRT_RANGE_MULTIPLIER', 1.5)
CRT_WINDOW = int(os.getenv('CRT_RETEST_BARS', '5'))
CRT_ENGULF = os.getenv('CRT_REQUIRE_ENGULF', 'False').lower() == 'true'
CRT_REJECTION = os.getenv('CRT_A_PLUS_ONLY', 'False').lower() == 'true'
CRT_ADX = os.getenv('CRT_USE_ADX', 'False').lower() == 'true'
CRT_ADX_MIN = envf('CRT_ADX_THRESHOLD', 20)
RISK_PCT = envf('CRT_RISK_PER_TRADE', .01)
NOTIONAL_PCT = envf('CRT_MAX_POSITION_PCT', .95)
DAY_LOSS = envf('CRT_MAX_DAILY_LOSS', .03)
WEEK_LOSS = envf('CRT_MAX_WEEKLY_LOSS', .07)
DAY_TRADES = int(os.getenv('CRT_MAX_TRADES_PER_DAY', '12'))
LOSS_STREAK = int(os.getenv('CRT_MAX_CONSECUTIVE_LOSSES', '4'))
risk_snapshot = {'equity':0.0, 'updated':0.0}

liquidity_cache = {}
balance_diagnostic = {}

MAX_CHASE_R = envf('JJ_MAX_ENTRY_DRIFT_R', .35)
MAX_STOP_PCT = envf('JJ_MAX_STOP_PCT', .04)
MIN_STOP_PCT = envf('CRT_MIN_STOP_PCT', .0001)
COOLDOWN = envf('JJ_COOLDOWN_SECONDS', 900)
MAX_AGE = envf('JJ_MAX_SIGNAL_AGE_SECONDS', 120)
PAPER_BALANCE = envf('JJ_PAPER_BALANCE_USDT', 10000)
VOLUME_PATH = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
STATE_NAME = f'crt_blofin_v1_{"paper" if PAPER_TRADING else "demo" if BLOFIN_DEMO else "live"}.json'
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
SYMBOLS = list(dict.fromkeys(x.strip().upper() for x in os.getenv('CRT_SYMBOLS', 'BTC-USDT').split(',') if x.strip()))
session = requests.Session()
meta = {}
state = {'strategy':'CRT-v1', 'mode': 'paper' if PAPER_TRADING else 'demo' if BLOFIN_DEMO else 'live',
         'managed': {}, 'seen': {}, 'last_exit': {}, 'paper_realized': 0.0, 'reports': {}}
health = {'last_cycle': 0, 'error': ''}
log = logging.getLogger('crt')

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
        if saved.get('strategy')!='CRT-v1':
            raise RuntimeError('Wrong strategy state: choose a new JJ_STATE_FILE for CRT')
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

def candles(symbol, bar=None, limit=120):
    bar = bar or BAR
    rows = []
    for c in get('/api/v1/market/candles', {'instId':symbol,'bar':bar,'limit':str(limit)}):
        if not isinstance(c,list) or len(c)<9 or str(c[8])!='1':
            continue
        values = [num(v) for v in c[1:6]]
        if min(values[:4])<=0 or values[1]<max(values[0],values[3]) or values[2]>min(values[0],values[3]):
            raise ValueError('Invalid candle')
        rows.append([int(c[0]),*values])
    df = pd.DataFrame(rows, columns=['ts','open','high','low','close','volume'])
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    duration = BAR_MS[bar]
    if len(df)>1 and not (df.ts.diff().iloc[1:]==duration).all():
        raise ValueError('Candle gap: entries blocked')
    if len(df) and df.ts.iloc[-1]+duration > time.time()*1000:
        raise ValueError('Future candle: entries blocked')
    return df

def detect_bullish_ebp(df):
    return (df.low < df.low.shift()) & (df.close > df.open.shift())
def detect_bearish_ebp(df):
    return (df.high > df.high.shift()) & (df.close < df.open.shift())

def rma(series, length=14):
    result = pd.Series(float('nan'),index=series.index,dtype=float)
    valid = series.dropna()
    if len(valid)<length:
        return result
    first = valid.index[length-1]
    result.loc[first] = valid.iloc[:length].mean()
    value = result.loc[first]
    for index in valid.index[length:]:
        value = (value*(length-1)+float(series.loc[index]))/length
        result.loc[index] = value
    return result

def crt_indicators(df):
    df = df.copy()
    previous = df.close.shift()
    tr = pd.concat([df.high-df.low,(df.high-previous).abs(),(df.low-previous).abs()],axis=1).max(axis=1)
    tr.iloc[0] = float('nan')
    df['atr'] = rma(tr)
    if CRT_ADX:
        up,down = df.high.diff(),-df.low.diff()
        plus = up.where((up>down)&(up>0),0.0)
        minus = down.where((down>up)&(down>0),0.0)
        plus.iloc[0]=minus.iloc[0]=float('nan')
        pdm,mdm = rma(plus),rma(minus)
        denominator = (pdm+mdm).replace(0,float('nan'))
        df['adx'] = rma(100*(pdm-mdm).abs()/denominator)
    return df

def signal(symbol):
    df = candles(symbol, BAR, 120)
    if len(df)<40:
        return None,'few_candles'
    row = df.iloc[-1]
    stamp = int(row.ts)
    if int(state['seen'].get(symbol,0)) >= stamp:
        return None,'already_seen'
    closed_at = stamp+BAR_MS[BAR]
    age = time.time()-closed_at/1000
    if not 0<=age<=MAX_AGE:
        return None,'stale'
    df = crt_indicators(df)
    anchor = None
    for i in range(14,len(df)):
        r = df.iloc[i]
        if not math.isfinite(float(r.atr)) or r.high-r.low <= float(r.atr)*CRT_MULT or r.close==r.open:
            continue
        bull = r.close>r.open
        prev = df.iloc[i-1]
        engulf = r.close>prev.high and r.open<=prev.low if bull else r.close<prev.low and r.open>=prev.high
        if not CRT_ENGULF or engulf:
            anchor = i
    if anchor is None or not 1<=len(df)-1-anchor<=CRT_WINDOW:
        return None,'no_crt'
    ref, row = df.iloc[anchor],df.iloc[-1]
    ref_ts = int(ref.ts)
    if int(state.setdefault('crt_used',{}).get(symbol,0))>=ref_ts:
        return None,'crt_used'
    long = ref.close>ref.open
    sweep = row.low<ref.low and row.close>ref.low if long else row.high>ref.high and row.close<ref.high
    if not sweep or (row.close>row.open)!=long or row.close==row.open:
        return None,'no_sweep'
    body = abs(row.close-row.open)
    wick = min(row.open,row.close)-row.low if long else row.high-max(row.open,row.close)
    if CRT_REJECTION and wick<=body*1.5:
        return None,'rejection'
    if CRT_ADX and (not math.isfinite(float(row.adx)) or row.adx<CRT_ADX_MIN):
        return None,'adx'
    htf = candles(symbol,'4H',60)
    known = htf[htf.ts+BAR_MS['4H']<=closed_at]
    if known.empty:
        return None,'htf_missing'
    hr = known.iloc[-1]
    if closed_at-(int(hr.ts)+BAR_MS['4H'])>=BAR_MS['4H']:
        return None,'htf_stale'
    bias = 1 if hr.close>hr.open else -1 if hr.close<hr.open else 0
    if bias!=(1 if long else -1):
        return None,'htf_direction'
    atr = float(row.atr)
    stop = float(row.low)-atr*.05 if long else float(row.high)+atr*.05
    return dict(symbol=symbol,direction='long' if long else 'short',ts=stamp,
        close=float(row.close),low=float(row.low),high=float(row.high),planned_sl=stop,
        crt_ts=ref_ts,evidence=dict(signal_ts=stamp,open=float(row.open),high=float(row.high),
        low=float(row.low),close=float(row.close),crt_ts=ref_ts,crt_high=float(ref.high),
        crt_low=float(ref.low),atr=atr,htf_ts=int(hr.ts),htf_open=float(hr.open),
        htf_close=float(hr.close),bias=bias)), 'signal' 

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
    equity = risk_snapshot['equity']
    if equity<=0 or time.time()-risk_snapshot['updated']>30:
        return None,'equity_stale'
    m = meta[sig['symbol']]
    tick,cv,lot = num(m['tickSize']),num(m['contractValue']),num(m['lotSize'])
    long = sig['direction']=='long'
    stop = step(sig['planned_sl'],tick,up=not long)
    distance = entry-stop if long else stop-entry
    if stop<=0 or distance<=0 or not MIN_STOP_PCT<=distance/entry<=MAX_STOP_PCT:
        return None,'invalid_stop'
    if abs(entry-sig['close'])>MAX_CHASE_R*distance:
        return None,'entry_drift'
    tp = step(entry+distance*RR if long else entry-distance*RR,tick,up=long)
    if tp<=0 or cv<=0:
        return None,'invalid_contract'
    budget = min(equity*RISK_PCT,RISK_CAP)
    per_contract = cv*(distance+entry*(2*FEE+SLIPPAGE))
    size = step(min(budget/per_contract,equity*NOTIONAL_PCT/(cv*entry),
        available*.88/(cv*entry*(1/LEV+2*FEE+SLIPPAGE)),num(m['maxMarketSize'])),lot)
    if size<num(m['minSize']):
        return None,'below_min_size'
    notional = size*cv*entry
    risk = size*per_contract
    needed = notional/LEV+notional*(2*FEE+SLIPPAGE)
    balance_diagnostic.update(symbol=sig['symbol'],available=available,required=needed/.88,
        ts=time.time(),blocked=needed>available*.88)
    if needed>available*.88:
        return None,'balance'
    return dict(direction=sig['direction'],entry_price=entry,stop=stop,tp=tp,
        contracts=size,notional=notional,risk_estimate=risk,risk_budget=budget,
        signal_ts=sig['ts'],crt_ts=sig['crt_ts'],entry_ts=time.time(),status='open',
        signal_evidence=dict(sig['evidence'])), 'ok' 

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

def signal_explanation(p):
    e = p.get('signal_evidence') or {}
    if 'crt_ts' not in e:
        return '\nCRT-details niet beschikbaar voor herstelde positie.'
    start = datetime.fromtimestamp(e['signal_ts']/1000,timezone.utc)
    long = p['direction']=='long'
    return (f'\n🔎 CRT {BAR} | signaal {start:%d-%m %H:%M} UTC (candlebegin)\n'
        f'Referentie H {e["crt_high"]:.8g} / L {e["crt_low"]:.8g} | ATR {e["atr"]:.8g}\n'
        f'✅ Sweep {"low" if long else "high"} {e["low"] if long else e["high"]:.8g}; '
        f'slot terug {e["close"]:.8g}\n'
        f'✅ Afgesloten 4H-bias {"LONG" if e["bias"]==1 else "SHORT"}: '
        f'open {e["htf_open"]:.8g} → slot {e["htf_close"]:.8g}\n'
        f'Geschat stoprisico incl. handelskosten {p["risk_estimate"]:.2f} USDT '
        f'| budget {p["risk_budget"]:.2f} USDT')

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
           f'Nominale positie: {p["notional"]:.2f} USDT | marge indicatief: {p["notional"]/LEV:.2f} USDT' + signal_explanation(p))
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
                record_risk_result(uuid4().hex,pnl)
                del state['managed'][symbol]
                state['last_exit'][symbol] = time.time()
                save()
                notify(f'PAPER CLOSE {symbol}: {"SL" if hit_sl else "TP"}; geschat netto {pnl:+.2f} USDT')
            elif not PAPER_TRADING:
                # Verify actual exchange TP/SL, not merely accepted entry fields.
                actual_distance = p['entry_price']-p['stop'] if long else p['stop']-p['entry_price']
                actual_risk = p['contracts']*num(meta[symbol]['contractValue'])*max(actual_distance, 0) + p['notional']*(2*FEE+SLIPPAGE)
                has_protection = protected(symbol, p)
                invalid_fill = actual_distance <= 0 or actual_risk > min(RISK_CAP,p.get('risk_budget',RISK_CAP))*1.02
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
        # History side may describe a closing order rather than the position.
        # Never use buy/sell alone to decide whether this is our trade.
        position_side = str(h.get('positionSide') or '').lower()
        if position_side in ('long', 'short') and position_side != p['direction']:
            rejects['positierichting'] += 1
            continue
        try:
            entry = float(h['openAveragePrice'])
            exit_price = float(h['closeAveragePrice'])
            qty = abs(float(h['closePositions']))
            realized = float(h['realizedPnl'])
            contract_value = float(meta[symbol]['contractValue'])
            values = (entry, exit_price, qty, realized, contract_value)
            if not all(math.isfinite(v) for v in values) or min(entry, exit_price, qty, contract_value) <= 0:
                raise ValueError('invalid history numbers')
        except (KeyError, TypeError, ValueError):
            rejects['onvolledige afrekening'] += 1
            continue
        expected_pnl = (exit_price-entry)*qty*contract_value*(1 if p['direction']=='long' else -1)
        # Allow rounded monetary values; costs/funding are reported separately.
        pnl_tolerance = max(0.01, abs(expected_pnl)*0.0001)
        if abs(realized-expected_pnl) > pnl_tolerance:
            rejects['PnL past niet bij positierichting'] += 1
            continue
        if not pid and position_side not in ('long', 'short') and abs(expected_pnl) <= pnl_tolerance:
            rejects['richting onvoldoende bevestigd zonder positie-ID'] += 1
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
    if v.get('net') is not None:
        record_risk_result(key,v['net'])
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
            if funding is None:
                raise ReportPending('Funding nog niet verifieerbaar; entryblokkade blijft actief')
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


def account_equity():
    if PAPER_TRADING:
        value = PAPER_BALANCE+state['paper_realized']
        for symbol,p in state['managed'].items():
            value += p.get('metrics',{}).get('last_after_estimated_fees',0.0)
        return value
    data = one(get('/api/v1/account/balance',private=True))
    value = num(data['totalEquity'])
    if value<=0:
        raise RuntimeError('Futures equity unavailable/nonpositive')
    return value

def record_risk_result(key, net):
    guard = state.setdefault('risk_guard',{})
    done = guard.setdefault('processed',[])
    if key in done:
        return
    guard['losses'] = guard.get('losses',0)+1 if net<0 else 0
    done.append(key)
    if guard['losses']>=LOSS_STREAK:
        guard['permanent'] = f'{LOSS_STREAK} bevestigde nettoverliezen op rij'

def risk_gate():
    # This gate NEVER skips manage()/exchange protection.
    equity = account_equity()
    risk_snapshot.update(equity=equity,updated=time.time())
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    year,week,_ = now.isocalendar()
    week_key = f'{year}-{week}'
    guard = state.setdefault('risk_guard',{})
    before = json.dumps(guard,sort_keys=True)
    if guard.get('day')!=day:
        guard.update(day=day,day_start=equity,trades_today=0,daily_stop=False)
    if guard.get('week')!=week_key:
        guard.update(week=week_key,week_start=equity)
    if equity<=0:
        guard['permanent']='Equity niet positief'
    if equity<=guard['day_start']*(1-DAY_LOSS):
        guard['daily_stop']=True
    if equity<=guard['week_start']*(1-WEEK_LOSS):
        guard['permanent']='Weekverlieslimiet (permanente entryblokkade)'
    unresolved = bool(state.get('reports'))
    journal = state.get('journal',{})
    unknown_funding = any(v.get('funding') is None for v in journal.values())
    reason = (guard.get('permanent') or ('dagverlieslimiet' if guard.get('daily_stop') else '')
        or ('dagelijkse entrylimiet' if guard.get('trades_today',0)>=DAY_TRADES else '')
        or ('afrekening/funding nog niet bevestigd' if unresolved or unknown_funding else ''))
    previous = guard.get('blocked','')
    guard['blocked']=reason
    if json.dumps(guard,sort_keys=True)!=before:
        save()
    if reason and reason!=previous:
        notify('⏸ Nieuwe entries geblokkeerd: '+reason+'. Positiebeheer en BloFin TP/SL blijven actief.')
    return reason

def open_signal(sig):
    symbol = sig['symbol']
    if symbol in state['managed']:
        return 'existing_managed_position'
    blocked = risk_gate()
    if blocked:
        return blocked
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
        p['client_id'] = 'crtbot'+uuid4().hex[:27]
    # Persist intent BEFORE sending order. Unknown POST result is reconciled by ID.
    state['risk_guard']['trades_today'] += 1
    state.setdefault('crt_used',{})[symbol] = sig['crt_ts']
    state['managed'][symbol] = p
    state['seen'][symbol] = sig['ts']
    save()
    if PAPER_TRADING:
        notify(f'PAPER OPEN {symbol} {p["direction"]}: entry {entry:.8g}, SL {p["stop"]:.8g}, TP {p["tp"]:.8g}, risico ~{p["risk_estimate"]:.2f} USDT' + signal_explanation(p))
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
    if BAR!='15m' or not 0<RISK_PCT<=.1 or not 0<NOTIONAL_PCT<=1 or not 0<DAY_LOSS<1 or not 0<WEEK_LOSS<1 or min(DAY_TRADES,LOSS_STREAK,CRT_WINDOW,CRT_MULT)<1:
        raise ValueError('Invalid CRT/risk configuration')
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
    notify(f'CRT BloFin v{VERSION} gestart: {state["mode"]}, {len(allowed)} munten, {BAR} + 4H, {LEV}x, {RR:g}R. Risicodoel {RISK_PCT*100:g}% equity; positiewaarde maximaal {NOTIONAL_PCT*100:g}% equity. Geen vaste minimummarge.')
    notify(f'Liquiditeitsfilter: 24u volume >= {MIN_VOLUME:,.0f} USDT; spread <= {MAX_SPREAD*100:.2f}%; diepte beide zijden >= {DEPTH_MULT:g}x positie binnen {DEPTH_BAND*100:.2f}%')
    if missing:
        notify('Overgeslagen (niet beschikbaar / leverage): '+', '.join(missing))
    risk_gate()
    return allowed

SCAN_LABELS = {'stale':'buiten instapvenster', 'no_crt':'geen recente CRT', 'no_sweep':'geen sweep/reclaim', 'crt_used':'CRT al gebruikt', 'htf_direction':'4H-bias niet passend', 'below_min_size':'minimumcontract boven risicobudget',
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
        text.append(f'Vrij beschikbaar: {available:.2f} USDT | risicodoel: {RISK_PCT*100:g}% equity')
    except Exception:
        text.append('Vrij saldo: tijdelijk niet beschikbaar')
    guard = state.get('risk_guard',{})
    text.append(f'Entries vandaag (UTC): {guard.get("trades_today",0)}/{DAY_TRADES} | verliesreeks {guard.get("losses",0)}/{LOSS_STREAK}')
    if guard.get('blocked'):
        text.append('Nieuwe entries geblokkeerd: '+guard['blocked'])
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
        return {'bot':'CRT BloFin','version':VERSION,'mode':state['mode'],
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
            entry_block = risk_gate()
            health['error'] = '' if good else 'position reconciliation blocked'
            if good and not entry_block and time.time() >= next_scan:
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
