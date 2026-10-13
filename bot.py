#!/usr/bin/env python3
"""Trend + pullback + confirmation: BloFin public futures data, PAPER ONLY.
Python >=3.10. Install: requests pandas flask. Start: python bot.py
Telegram secrets remain TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway.
No BloFin credentials are needed; this program has no exchange order endpoint.
All trading settings below are Python constants; Railway cannot enable live.
Uses exchange-confirmed closed 15m candles and closed 4H EMA20/EMA50 trend.
A: body engulfing. B: wick rejection confirmed within two closed candles.
If both qualify, A wins; never two entries on one signal. Stop beyond the
four-candle pullback extreme, plus 0.15 ATR. EMA-zone widened by 0.25 ATR.
4H slope compares EMA20 with one closed candle ago; rejection wick >=1.5 body.
Valid setups persist for up to 300 seconds; only spread/drift blocks are retried.
Evaluated candles and executed entries are tracked separately, across restarts.
Targets 1.8 price R, not guaranteed net R. Maximum holding time is sampled
at each management cycle; API outages can delay simulated exits.
Risk sizing includes modeled stop slippage and both fees; gaps can exceed it.
Paper entries/exits use executable ask/bid plus adverse slippage. Mark-price
TP/SL are polled, not real protective orders. Moves between samples can be
missed. No funding model. No claims of profitability or historical backtest. Ranking is a fixed heuristic,
not a probability. All currently eligible candidates are compared before entry.
Sampled net excursions and exit diagnostics are stored; old trades are not
retrospectively reconstructed. Metrics based on mark exclude actual exit spread.
Measurements start at entry for new positions, at upgrade for existing positions.
v1.4: confirmation AND current modeled fill must stay within 0.75 ATR of the
buffered zone. This is an experimental filter, not a proven optimal threshold.
Each NEW baseline entry starts a paired shadow with identical entry/size/fees.
At +1R quoted modeled NET profit the shadow moves its stop to modeled fee- and
slippage-covering bid/ask price. Baseline retains its original 1.8R/SL/time exits.
Both use the same snapshots while paired; gaps can still cause BE losses.
Shadow outcomes do not affect balance, daily caps or loss streaks. Comparison
includes only pairs with BOTH exits closed, and is not a portfolio backtest.
Prior entries are never given retroactive shadows. Active shadow exits persist.
State uses an atomic file and a periodic .bak checkpoint. BOTH disappear on
redeploy without a real Railway volume mounted at the configured path. Merely
creating /data or setting an environment variable does not provision a volume.
UTC daily/weekly boundaries; weekly and loss-streak stops require manual review.
State is separate from CRT. Without a Railway volume, redeploy may reset it.
"""
import os
import json
import math
import time
import logging
import threading
import shutil
from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from collections import Counter
import requests
import pandas as pd

# ====================== SETTINGS: CHANGE IN PYTHON ======================
VERSION = '1.4-Trend-Pullback-Paper-Paired'
PAPER_TRADING = True  # fixed paper-only program; no real order functionality
# Candidate universe for paper testing, not a measured ranking of profitability
# or signal frequency. Startup checks BloFin availability; entries retain the
# same quote-volume, spread, trend, confirmation and risk requirements.
SYMBOLS = [
    'BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'BNB-USDT', 'XRP-USDT',
    'ZEC-USDT', 'DOGE-USDT', 'ADA-USDT', 'AVAX-USDT', 'LINK-USDT',
    'SUI-USDT', 'NEAR-USDT', 'APT-USDT', 'ARB-USDT', 'OP-USDT',
    'INJ-USDT', 'TAO-USDT', 'SEI-USDT', 'LTC-USDT', 'BCH-USDT',
]
INITIAL_CAPITAL = 10000.0
RISK_PER_TRADE = 0.005
MAX_POSITION_PCT = 0.95  # notional/equity, not margin/equity
LEVERAGE = 12
MAX_POSITIONS = 1
MAX_TRADES_PER_DAY = 12
MAX_DAILY_LOSS = 0.03
MAX_WEEKLY_LOSS = 0.07
MAX_CONSECUTIVE_LOSSES = 4
RR = 1.8
STOP_ATR_BUFFER = 0.15
MAX_ENTRY_DRIFT_ATR = 0.35
MAX_HOLD_SECONDS = 4*3600
MAX_SIGNAL_AGE = 300
PULLBACK_LOOKBACK = 4
ZONE_BUFFER_ATR = 0.25
MAX_ZONE_DISTANCE_ATR = 0.75
SHADOW_BE_TRIGGER_R = 1.0  # quoted modeled net profit / original modeled risk
REJECTION_WICK_RATIO = 1.5
MIN_STOP_PCT = 0.0001
MAX_STOP_PCT = 0.04
FEE = 0.0004  # modeled fee per side, not verified account fee tier
SLIPPAGE = 0.0002
MAX_SPREAD = 0.001
MIN_24H_VOLUME = 1000000.0  # USDT quote volume from 96 completed 15m candles
COOLDOWN_SECONDS = 600
SCAN_SECONDS = 20
REPORT_SECONDS = 1800
MANAGE_SECONDS = 5
BASE_URL = 'https://openapi.blofin.com'
DURATION = {'15m':900000,'4H':14400000}
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','').strip().removeprefix('bot')
TG_CHAT = os.getenv('TELEGRAM_CHAT_ID','').strip()
VOLUME_PATH = os.getenv('RAILWAY_VOLUME_MOUNT_PATH','').strip()
STATE_FILE = os.path.join(VOLUME_PATH,'trend_pullback_paper_v1.json') if VOLUME_PATH else 'trend_pullback_paper_v1.json'
SESSION = requests.Session()
META = {}
HTF_CACHE = {}
DIAGNOSTICS = {}
OUTCOMES = {}  # one latest outcome per (symbol, candle) in a reporting period
HEALTH = {'cycle':0,'error':''}
LAST_BACKUP = 0.0
STATE = {'strategy':'trend-pullback-paper-v1','capital':INITIAL_CAPITAL,'positions':{},
         'seen':{},'executed':{},'pending':{},'used_rejections':{},'last_exit':{},'trades':[], 'route_stats':{},'guard':{},
         'recognitions':{'engulfing':0,'rejection':0,'overlap':0},'selections':[],
         'shadows':{},'pairs':{},'paired_stats':{'count':0,'baseline_net':0.0,'shadow_net':0.0,'shadow_better':0}}
LOG = logging.getLogger('pullback')


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite number')
    return result


def notify(message):
    LOG.info(message)
    if not TG_TOKEN or not TG_CHAT:
        return False
    try:
        response = requests.post('https://api.telegram.org/bot'+TG_TOKEN+'/sendMessage',
            json={'chat_id':TG_CHAT,'text':message[:4000]},timeout=(5,10))
        return response.status_code==200 and response.json().get('ok') is True
    except (requests.RequestException,ValueError):
        LOG.warning('Telegram delivery failed; details retained in log/state')
        return False


def get(path,params=None):
    # Public GET only. There is deliberately no private API or order sender.
    if not path.startswith('/api/v1/market/'):
        raise RuntimeError('Only public market endpoints are allowed')
    for attempt in range(3):
        try:
            response = SESSION.get(BASE_URL+path,params=params,timeout=(5,10))
            response.raise_for_status()
            result = response.json()
            if str(result.get('code'))!='0':
                raise RuntimeError('Market API error: '+str(result.get('code')))
            return result['data']
        except (requests.RequestException,ValueError):
            if attempt==2:
                raise RuntimeError('Market API unavailable') from None
            time.sleep(.5*(attempt+1))


def one(data):
    if isinstance(data,list) and len(data)==1:
        return data[0]
    if isinstance(data,dict):
        return data
    raise ValueError('Expected one market result')


def save():
    global LAST_BACKUP
    temporary = STATE_FILE+'.tmp'
    with open(temporary,'w') as handle:
        json.dump(STATE,handle,allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    # Periodic previous-state checkpoint; both files are ephemeral without a volume.
    if os.path.exists(STATE_FILE) and time.time()-LAST_BACKUP>=60:
        backup_tmp=STATE_FILE+'.bak.tmp'
        with open(STATE_FILE,'rb') as src,open(backup_tmp,'wb') as dest:
            shutil.copyfileobj(src,dest);dest.flush();os.fsync(dest.fileno())
        os.replace(backup_tmp,STATE_FILE+'.bak')
        LAST_BACKUP=time.time()
    os.replace(temporary,STATE_FILE)
    directory=os.open(os.path.dirname(os.path.abspath(STATE_FILE)),os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def load():
    if not os.path.exists(STATE_FILE) and os.path.exists(STATE_FILE+'.bak'):
        raise RuntimeError('Statusbestand ontbreekt maar backup bestaat; herstel gecontroleerd in plaats van stil resetten.')
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as handle:saved=json.load(handle)
        except (ValueError,OSError) as exc:
            raise RuntimeError('Statusbestand onleesbaar; controleer '+STATE_FILE+'.bak. Geen automatische reset.') from exc
        if saved.get('strategy')!=STATE['strategy']:
            raise RuntimeError('Wrong strategy state')
        if not isinstance(saved.get('positions'),dict) or not math.isfinite(float(saved.get('capital',float('nan')))):
            raise RuntimeError('Invalid saved paper state')
        STATE.update(saved)


def candles(symbol,bar='15m',limit=120):
    rows = []
    for c in get('/api/v1/market/candles',{'instId':symbol,'bar':bar,'limit':str(limit)}):
        if not isinstance(c,list) or len(c)<9 or str(c[8])!='1':
            continue
        o,h,l,close,volume = [number(v) for v in c[1:6]]
        if min(o,h,l,close)<=0 or h<max(o,close) or l>min(o,close) or h<l or volume<0:
            raise ValueError('Invalid OHLCV')
        # BloFin candle column 7 is quote turnover (USDT for these instruments).
        rows.append([int(c[0]),o,h,l,close,volume,number(c[7])])
    df = pd.DataFrame(rows,columns=['ts','open','high','low','close','volume','quote_volume'])
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    if len(df)>1 and not (df.ts.diff().iloc[1:]==DURATION[bar]).all():
        raise ValueError('Candle gap')
    if len(df) and int(df.ts.iloc[-1])+DURATION[bar]>time.time()*1000:
        raise ValueError('Future candle')
    return df


def indicators(df):
    result = df.copy()
    result['ema20'] = result.close.ewm(span=20,adjust=False).mean()
    result['ema50'] = result.close.ewm(span=50,adjust=False).mean()
    previous = result.close.shift()
    tr = pd.concat([result.high-result.low,(result.high-previous).abs(),(result.low-previous).abs()],axis=1).max(axis=1)
    atr = pd.Series(float('nan'),index=result.index,dtype=float)
    if len(result)>14:
        value = float(tr.iloc[1:15].mean())
        atr.iloc[14] = value
        for i in range(15,len(result)):
            value = (value*13+float(tr.iloc[i]))/14
            atr.iloc[i] = value
    result['atr'] = atr
    return result


def htf_trend(symbol,decision_ms):
    boundary = decision_ms//DURATION['4H']
    cached = HTF_CACHE.get(symbol)
    if cached is None or cached['boundary']!=boundary:
        raw = candles(symbol,'4H',300)
        HTF_CACHE[symbol] = {'boundary':boundary,'df':raw}
    else:
        raw = cached['df']
    known = raw[raw.ts+DURATION['4H']<=decision_ms].copy().reset_index(drop=True)
    if len(known)<100:
        return 0,None,'htf_missing'
    if decision_ms-(int(known.ts.iloc[-1])+DURATION['4H'])>=DURATION['4H']:
        HTF_CACHE.pop(symbol,None)
        return 0,None,'htf_stale'
    htf = indicators(known)
    last, earlier = htf.iloc[-1],htf.iloc[-2]
    bias = 1 if last.ema20>last.ema50 and last.ema20>earlier.ema20 else -1 if last.ema20<last.ema50 and last.ema20<earlier.ema20 else 0
    reason = 'ok' if bias else 'htf_order_equal' if last.ema20==last.ema50 else 'htf_slope_long' if last.ema20>last.ema50 else 'htf_slope_short'
    return bias,{'ts':int(last.ts),'ema20':float(last.ema20),'ema50':float(last.ema50),'ema20_before':float(earlier.ema20),'atr':float(last.atr)},reason


def touches_zone(row):
    bottom,top = min(row.ema20,row.ema50),max(row.ema20,row.ema50)
    buffer = ZONE_BUFFER_ATR*float(row.atr)
    return math.isfinite(buffer) and row.low<=top+buffer and row.high>=bottom-buffer


def pattern(df,direction):
    """Pure closed-candle rules, suitable for offline tests. A has precedence."""
    i = len(df)-1
    row,prev = df.iloc[i],df.iloc[i-1]
    recent = df.iloc[i-PULLBACK_LOOKBACK+1:i+1]
    if not any(touches_zone(r) for _,r in recent.iterrows()):
        return None,'no_pullback'
    favorable = direction*(row.close-row.open)>0
    engulf = (prev.close<prev.open and row.open<=prev.close and row.close>=prev.open) if direction==1 else (prev.close>prev.open and row.open>=prev.close and row.close<=prev.open)
    engulf_setup = favorable and engulf
    rejection_anchor = None
    for j in (i-1,i-2):
        rejection = df.iloc[j]
        body = abs(rejection.close-rejection.open)
        wick = min(rejection.close,rejection.open)-rejection.low if direction==1 else rejection.high-max(rejection.close,rejection.open)
        if body<=0 or wick<REJECTION_WICK_RATIO*body or not touches_zone(rejection):
            continue
        threshold = rejection.high if direction==1 else rejection.low
        broke = direction*(row.close-threshold)>0
        already_broke = any(direction*(r.close-threshold)>0 for _,r in df.iloc[j+1:i].iterrows())
        if favorable and broke and not already_broke:
            rejection_anchor = int(rejection.ts)
            break
    routes = (['engulfing'] if engulf_setup else []) + (['rejection'] if rejection_anchor is not None else [])
    if routes:
        return {'route':routes[0],'routes':routes,'rejection_anchor':rejection_anchor,
                'anchor':int(prev.ts) if engulf_setup else rejection_anchor,
                'start':i-PULLBACK_LOOKBACK+1},'ok'
    return None,'no_confirmation'


def signal(symbol):
    raw = candles(symbol)
    if len(raw)<100:
        return None,'few_candles',None
    stamp = int(raw.ts.iloc[-1])
    decision = stamp+DURATION['15m']
    age = time.time()-decision/1000
    if age<0 or age>MAX_SIGNAL_AGE:
        if symbol in STATE['pending']:
            del STATE['pending'][symbol];save()
        if STATE['seen'].get(symbol,-1)>=stamp:
            return None,'same_candle',stamp
        return None,'stale',stamp
    if STATE['executed'].get(symbol,-1)>=stamp:
        return None,'same_candle',stamp
    pending = STATE['pending'].get(symbol)
    if pending and pending['ts']==stamp:
        return dict(pending),'signal_retry',stamp
    if pending:
        del STATE['pending'][symbol];save()
    if STATE['seen'].get(symbol,-1)>=stamp:
        return None,'same_candle',stamp
    df = indicators(raw)
    atr = float(df.atr.iloc[-1])
    if not math.isfinite(atr) or atr<=0:
        return None,'atr_invalid',stamp
    direction,htf,reason = htf_trend(symbol,decision)
    if direction==0:
        return None,reason if reason!='ok' else 'no_trend',stamp
    setup,reason = pattern(df,direction)
    if not setup:
        return None,reason,stamp
    if setup['route']=='rejection' and STATE['used_rejections'].get(symbol,-1)>=setup['anchor']:
        return None,'used_rejection',stamp
    row = df.iloc[-1]
    pullback = df.iloc[setup['start']:]
    routes = list(setup['routes'])
    if 'rejection' in routes and STATE['used_rejections'].get(symbol,-1)>=setup['rejection_anchor']:
        routes.remove('rejection')
    for route in routes:STATE['recognitions'][route]+=1
    if len(routes)==2:STATE['recognitions']['overlap']+=1
    zone_low = float(min(row.ema20,row.ema50)-ZONE_BUFFER_ATR*atr)
    zone_high = float(max(row.ema20,row.ema50)+ZONE_BUFFER_ATR*atr)
    distance_atr = max(zone_low-float(row.close),float(row.close)-zone_high,0)/atr
    if distance_atr>MAX_ZONE_DISTANCE_ATR:
        return None,'zone_too_far',stamp
    touched = [int(r.ts) for _,r in pullback.iterrows() if touches_zone(r)]
    span = float(row.high-row.low)
    close_strength = (float(row.close-row.low) if direction==1 else float(row.high-row.close))/span if span>0 else 0
    extreme = float(pullback.low.min() if direction==1 else pullback.high.max())
    stop = extreme-direction*STOP_ATR_BUFFER*atr
    return {'symbol':symbol,'direction':direction,'route':setup['route'],'routes':routes,
            'rejection_anchor':setup['rejection_anchor'],'anchor':setup['anchor'],
            'ts':stamp,'close':float(row.close),'atr':atr,'stop':stop,'htf':htf,
            'zone_low':zone_low,'zone_high':zone_high,'zone_distance_atr':distance_atr,
            'body_atr':float(abs(row.close-row.open))/atr,'close_strength':close_strength,
            'last_zone_touch_ts':max(touched) if touched else None,
            'signal_ohlc':{k:float(row[k]) for k in ('open','high','low','close')},
            'quote_volume':float(raw.quote_volume.iloc[-96:].sum())},'signal',stamp


def ticker(symbol):
    data = one(get('/api/v1/market/tickers',{'instId':symbol}))
    bid,ask = number(data['bidPrice']),number(data['askPrice'])
    if bid<=0 or ask<bid or abs(time.time()*1000-number(data['ts']))>15000:
        raise ValueError('Invalid/stale ticker')
    return bid,ask


def mark(symbol):
    data = one(get('/api/v1/market/mark-price',{'instId':symbol}))
    price = number(data['markPrice'])
    if price<=0 or ('ts' in data and abs(time.time()*1000-number(data['ts']))>15000):
        raise ValueError('Invalid/stale mark')
    return price


def rounded(value,step,up=False):
    return float((Decimal(str(value))/Decimal(str(step))).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*Decimal(str(step)))


def equity():
    total = number(STATE['capital'])
    for p in STATE['positions'].values():
        total += p['units']*(p['last_mark']-p['entry'])*p['direction']
    return total


def risk_gate():
    now = datetime.now(timezone.utc)
    day,week = now.date().isoformat(),f'{now.isocalendar().year}-W{now.isocalendar().week}'
    guard = STATE['guard']
    value = equity()
    changed = False
    if guard.get('day')!=day:
        guard.update(day=day,day_start=value,attempts=0,day_stop=False)
        changed = True
    if guard.get('week')!=week:
        guard.update(week=week,week_start=value)
        changed = True
    if value<=0:
        guard['permanent']='equity niet positief';changed=True
    elif value<=guard['day_start']*(1-MAX_DAILY_LOSS):
        if not guard['day_stop']: changed=True
        guard['day_stop']=True
    if value<=guard['week_start']*(1-MAX_WEEKLY_LOSS) and not guard.get('permanent'):
        guard['permanent']='weekverlieslimiet';changed=True
    if changed:save()
    return guard.get('permanent') or ('dagverlieslimiet' if guard.get('day_stop') else 'dagelijkse entrylimiet' if guard['attempts']>=MAX_TRADES_PER_DAY else '')


def rank_setup(sig,fill,stop,bid,ask):
    """Fixed heuristic, NOT a win probability. Higher score ranks eligible setups."""
    htf = sig.get('htf',{})
    htf_atr = float(htf.get('atr',0))
    trend = abs(htf.get('ema20',0)-htf.get('ema50',0))/htf_atr if htf_atr>0 else 0
    cost_per_unit = (ask-bid)+fill*(2*FEE+2*SLIPPAGE)
    cost_risk = cost_per_unit/max(abs(fill-stop),1e-12)
    parts = {'body':min(1,max(0,sig.get('body_atr',0))),
             'close':min(1,max(0,sig.get('close_strength',0))),
             'location':1/(1+max(0,sig.get('zone_distance_atr',0))),
             'trend':min(1,max(0,trend)), 'costs':max(0,1-cost_risk)}
    score = 100*(.25*parts['body']+.25*parts['close']+.20*parts['location']+.15*parts['trend']+.15*parts['costs'])
    return round(score,2),parts,cost_risk


def plan(sig,bid,ask):
    if sig.get('zone_distance_atr',0)>MAX_ZONE_DISTANCE_ATR:
        return None,'zone_too_far'
    if (ask-bid)/((ask+bid)/2)>MAX_SPREAD:
        return None,'spread'
    d = sig['direction']
    executable = ask if d==1 else bid
    fill = executable*(1+d*SLIPPAGE)
    if 'zone_low' in sig and 'zone_high' in sig:
        fill_zone_distance=max(sig['zone_low']-fill,fill-sig['zone_high'],0)/sig['atr']
        if fill_zone_distance>MAX_ZONE_DISTANCE_ATR:return None,'zone_too_far'
    if abs(fill-sig['close'])>MAX_ENTRY_DRIFT_ATR*sig['atr']:
        return None,'entry_drift'
    meta = META[sig['symbol']]
    stop = rounded(sig['stop'],meta['tick'],up=d==-1)
    distance = d*(fill-stop)
    if not MIN_STOP_PCT*fill<=distance<=MAX_STOP_PCT*fill or stop<=0:
        return None,'stop_distance'
    target = rounded(fill+d*distance*RR,meta['tick'],up=d==1)
    if target<=0:
        return None,'target_invalid'
    stop_fill = stop*(1-d*SLIPPAGE)
    loss = d*(fill-stop_fill)+FEE*(fill+stop_fill)
    value = equity()
    budget = value*RISK_PER_TRADE
    max_units = min(budget/loss,value*MAX_POSITION_PCT/fill,
                    max(0,STATE['capital'])*.88/(fill*(1/LEVERAGE+FEE)))
    contracts = rounded(min(max_units/meta['cv'],meta['maximum']),meta['lot'])
    if contracts<meta['minimum'] or contracts<=0:
        return None,'minimum_contract'
    units = contracts*meta['cv']
    score,parts,cost_risk = rank_setup(sig,fill,stop,bid,ask)
    return dict(sig,entry=fill,stop=stop,target=target,contracts=contracts,units=units,
                entry_fee=units*fill*FEE,risk=units*loss,risk_budget=budget,
                rank_score=score,rank_parts=parts,cost_risk=cost_risk,
                policy_version=VERSION,
                opened=time.time(),last_mark=executable),'ok'


def open_paper(sig):
    if STATE['executed'].get(sig['symbol'],-1)>=sig['ts']:
        return 'already_executed'
    age = time.time()-(sig['ts']+DURATION['15m'])/1000
    if not 0<=age<=MAX_SIGNAL_AGE:
        return 'signal_expired'
    if risk_gate() or STATE['positions']:
        return 'entry_blocked'
    if sig['quote_volume']<MIN_24H_VOLUME:
        return 'low_volume'
    bid,ask = ticker(sig['symbol'])
    p,reason = plan(sig,bid,ask)
    if not p:return reason
    # Current mark must not already invalidate the planned stop or target.
    current = mark(sig['symbol'])
    if not p['direction']*(current-p['stop'])>0 or not p['direction']*(p['target']-current)>0:
        return 'mark_outside'
    if time.time()-(sig['ts']+DURATION['15m'])/1000>MAX_SIGNAL_AGE:
        return 'signal_expired'
    p['last_mark']=current
    p['opened']=time.time()
    p['pair_id']=uuid4().hex
    sample_position(p,current)
    STATE['capital']-=p['entry_fee']
    STATE['positions'][sig['symbol']]=p
    create_shadow(p)
    STATE['executed'][sig['symbol']]=sig['ts']
    STATE['pending'].pop(sig['symbol'],None)
    STATE['guard']['attempts']+=1
    rejection_anchor = sig.get('rejection_anchor')
    if rejection_anchor is not None:
        STATE['used_rejections'][sig['symbol']]=rejection_anchor
    elif sig['route']=='rejection':STATE['used_rejections'][sig['symbol']]=sig['anchor']
    save()
    label = 'LONG' if p['direction']==1 else 'SHORT'
    notify(f'🧪 PAPER OPEN {sig["symbol"]} {label}\nRoute: {p["route"]} | 4H EMA20 {p["htf"]["ema20"]:.6g}, EMA50 {p["htf"]["ema50"]:.6g}\n'
           f'Herkenningen: {", ".join(p.get("routes",[p["route"]]))} | selectiescore {p["rank_score"]:.1f}/100 (geen winstkans)\n'
           f'Afstand bevestiging/zone {p.get("zone_distance_atr",0):.2f} ATR | geschatte kosten/stopafstand {p["cost_risk"]*100:.1f}%\n'
           f'15m EMA-zone {p["zone_low"]:.8g}–{p["zone_high"]:.8g} | bevestigingsclose {p["close"]:.8g}\n'
           f'Entry {p["entry"]:.8g} | SL {p["stop"]:.8g} | TP {p["target"]:.8g}\n'
           f'15m-pullback + gesloten bevestiging | risico geschat {p["risk"]:.2f}/{p["risk_budget"]:.2f} USDT\n'
           f'Positiewaarde {p["units"]*p["entry"]:.2f} USDT | tijdslimiet 4 uur')
    return 'opened'


def create_shadow(p):
    pair_id=p['pair_id']
    shadow={k:p[k] for k in ('symbol','direction','entry','units','entry_fee','stop','target','risk','opened','pair_id')}
    shadow.update(armed=False,original_stop=p['stop'],policy_version=VERSION)
    STATE['shadows'][pair_id]=shadow
    STATE['pairs'][pair_id]={'symbol':p['symbol'],'opened':p['opened'],'baseline':None,'shadow':None,
                             'policy_version':VERSION,'finalized':False}


def finish_pair(pair_id):
    pair=STATE['pairs'].get(pair_id)
    if not pair or pair['finalized'] or pair['baseline'] is None or pair['shadow'] is None:return
    stats=STATE['paired_stats'];base=pair['baseline']['net'];variant=pair['shadow']['net']
    stats['count']+=1;stats['baseline_net']+=base;stats['shadow_net']+=variant
    stats['shadow_better']+=int(variant>base)
    pair['finalized']=True;pair['delta']=variant-base;save()
    notify(f'🔬 PAAR VERGELEKEN {pair["symbol"]}\nBasis 1.8R: {base:+.2f} | shadow +1R→kostendekkend: {variant:+.2f} USDT\n'
           f'Verschil {variant-base:+.2f} USDT. Zelfde entry/grootte, geen extra orders of saldo-impact.')


def be_stop(shadow):
    d=shadow['direction'];entry=shadow['entry']
    exit_zero=entry*(1+d*FEE)/(1-d*FEE)
    reference_zero=exit_zero/(1-d*SLIPPAGE)
    # BE is triggered on executable bid (long)/ask (short), not on mark.
    return rounded(reference_zero,META[shadow['symbol']]['tick'],up=d==1)


def shadow_net(shadow,bid,ask):
    d=shadow['direction'];exit_price=(bid if d==1 else ask)*(1-d*SLIPPAGE)
    net=shadow['units']*(exit_price-shadow['entry'])*d-shadow['entry_fee']-shadow['units']*exit_price*FEE
    return exit_price,net


def close_shadow(pair_id,shadow,bid,ask,reason):
    exit_price,net=shadow_net(shadow,bid,ask)
    result={'net':net,'reason':reason,'closed':time.time(),'exit':exit_price,'armed':shadow['armed'],
            'stop':shadow['stop'],'trigger':shadow.get('exit_trigger')}
    STATE['pairs'][pair_id]['shadow']=result
    del STATE['shadows'][pair_id];save();finish_pair(pair_id)


def manage_shadows(observations=None):
    observations=observations or {}
    for pair_id,s in list(STATE['shadows'].items()):
        symbol=s['symbol']
        if s.get('exit_trigger'):
            if symbol in observations:_,bid,ask=observations[symbol]
            else:bid,ask=ticker(symbol)
            close_shadow(pair_id,s,bid,ask,s['exit_trigger']['reason']);continue
        if symbol in observations:price,bid,ask=observations[symbol]
        else:
            price=mark(symbol)
            d=s['direction']
            early='SL' if not s['armed'] and d*(price-s['stop'])<=0 else 'TP' if d*(price-s['target'])>=0 else 'TIME' if time.time()-s['opened']>=MAX_HOLD_SECONDS else None
            if early:
                s['exit_trigger']={'mark':price,'reason':early,'time':time.time()};save()
            bid,ask=ticker(symbol)
            if early:
                close_shadow(pair_id,s,bid,ask,early);continue
        d=s['direction'];executable=bid if d==1 else ask
        stop_reference=executable if s['armed'] else price
        reason='BE' if s['armed'] and d*(stop_reference-s['stop'])<=0 else 'SL' if not s['armed'] and d*(price-s['stop'])<=0 else 'TP' if d*(price-s['target'])>=0 else 'TIME' if time.time()-s['opened']>=MAX_HOLD_SECONDS else None
        if reason:
            s['exit_trigger']={'mark':price,'executable':executable,'reason':reason,'time':time.time()};save()
            close_shadow(pair_id,s,bid,ask,reason);continue
        _,net=shadow_net(s,bid,ask)
        if not s['armed'] and net>=s['risk']*SHADOW_BE_TRIGGER_R:
            s['stop']=be_stop(s);s['armed']=True;s['armed_at']=time.time();save()


def sample_position(p,price):
    """Sampled excursions; never reconstruct unsampled highs/lows."""
    now = time.time()
    gross = p['units']*(price-p['entry'])*p['direction']
    modeled_exit = price*(1-p['direction']*SLIPPAGE)
    net = p['units']*(modeled_exit-p['entry'])*p['direction']-p['entry_fee']-p['units']*modeled_exit*FEE
    m = p.get('metrics')
    if not m:
        m = {'started':now,'samples':0,'peak_gross':gross,'trough_gross':gross,
             'peak_net':net,'trough_net':net,'max_gap_seconds':0,
             'partial_history':now-p['opened']>10}
        p['metrics']=m
    gap = max(0,now-m.get('last_sample',now))
    m.update(samples=m['samples']+1,last_sample=now,last_gap_seconds=gap,
             max_gap_seconds=max(m['max_gap_seconds'],gap),last_mark=price,
             peak_gross=max(m['peak_gross'],gross),trough_gross=min(m['trough_gross'],gross),
             peak_net=max(m['peak_net'],net),trough_net=min(m['trough_net'],net))
    risk = p.get('risk',0)
    m['peak_r']=m['peak_net']/risk if risk>0 else None
    m['trough_r']=m['trough_net']/risk if risk>0 else None


def loss_observation(p,net,reason):
    if net>=0:return 'Netto winst/nul; geen verliestrade.'
    m = p.get('metrics')
    if not m:return 'Verlies; geen opgeslagen koersmetingen voor deze trade.'
    if reason=='SL':
        if m['peak_net']>0:return 'SL na gemeten tussentijdse netto winst.'
        if m.get('partial_history'):return 'SL; meetdekking vanaf update, eerder koersverloop onbekend.'
        return 'SL; geen netto winst in de gemeten monsters.'
    if reason=='TIME':return 'Tijdslimiet gesloten met netto verlies.'
    return 'TP-trigger maar netto verlies; controleer uitvoering en kosten.'


def close_paper(symbol,p,bid,ask,reason):
    d = p['direction']
    exit_price = (bid if d==1 else ask)*(1-d*SLIPPAGE)
    price_pnl = p['units']*(exit_price-p['entry'])*d
    exit_fee = p['units']*exit_price*FEE
    net = price_pnl-p['entry_fee']-exit_fee
    STATE['capital']+=price_pnl-exit_fee  # entry fee was already charged
    record = {'symbol':symbol,'route':p['route'],'direction':d,'opened':p['opened'],
              'closed':time.time(),'entry':p['entry'],'exit':exit_price,'reason':reason,
              'entry_fee':p['entry_fee'],'exit_fee':exit_fee,'net':net,'risk':p['risk'],
              'stop':p['stop'],'target':p['target'],'units':p['units'],
              'routes':p.get('routes',[p['route']]),'metrics':p.get('metrics'),
              'trigger':p.get('exit_trigger'),'signal_evidence':{k:p.get(k) for k in
                ('ts','anchor','zone_distance_atr','body_atr','close_strength','signal_ohlc','htf','zone_low','zone_high','last_zone_touch_ts')},
              'rank_score':p.get('rank_score'),'rank_parts':p.get('rank_parts'),
              'selection_id':p.get('selection_id'),
              'policy_version':p.get('policy_version','before-analysis-update')}
    model_stop = p['stop']*(1-d*SLIPPAGE)
    record['exit_diagnostics']={'bid':bid,'ask':ask,'model_stop_fill':model_stop,
        'exit_spread_pct':(ask-bid)/((ask+bid)/2),
        'stop_extra_loss':max(0,-net-p['risk']) if reason=='SL' else None,
        'exit_r':net/p['risk'] if p['risk']>0 else None,
        'trigger_to_fill_seconds':max(0,time.time()-p['exit_trigger']['time']) if p.get('exit_trigger') else None}
    record['observation']=loss_observation(p,net,reason)
    record['pair_id']=p.get('pair_id')
    if p.get('pair_id') in STATE['pairs']:
        STATE['pairs'][p['pair_id']]['baseline']={'net':net,'reason':reason,'closed':time.time()}
    STATE['trades'].append(record)
    STATE['trades']=STATE['trades'][-2000:]
    stats = STATE['route_stats'].setdefault(p['route'],{'trades':0,'wins':0,'net':0.0,'gross_wins':0.0,'gross_losses':0.0})
    stats['trades']+=1;stats['wins']+=int(net>0);stats['net']+=net
    stats['gross_wins']+=max(0,net);stats['gross_losses']+=max(0,-net)
    guard = STATE['guard']
    guard['losses']=guard.get('losses',0)+1 if net<0 else 0
    if guard['losses']>=MAX_CONSECUTIVE_LOSSES:guard['permanent']='vier nettoverliezen op rij'
    del STATE['positions'][symbol]
    STATE['last_exit'][symbol]=time.time()
    save();risk_gate()
    if p.get('pair_id'):finish_pair(p['pair_id'])
    m = p.get('metrics')
    details = (f'Beste/slechtste gemeten netto {m["peak_net"]:+.2f}/{m["trough_net"]:+.2f} USDT '
               f'({m["peak_r"]:+.2f}R/{m["trough_r"]:+.2f}R)\nMeetdekking: {"vanaf update" if m.get("partial_history") else "vanaf entry"}; grootste meetpauze {m["max_gap_seconds"]:.1f}s\n') if m and p['risk']>0 else 'Historische koersmetingen ontbreken.\n'
    trigger = p.get('exit_trigger')
    execution = f'SL {p["stop"]:.8g} | trigger-mark {trigger["mark"]:.8g} | exit {exit_price:.8g}\n' if reason=='SL' and trigger else ''
    extra = record['exit_diagnostics']['stop_extra_loss']
    if extra is not None:execution+=f'Boven geschat stoprisico: {extra:.2f} USDT\n'
    notify(f'🧪 PAPER CLOSE {symbol} | {p["route"]} | {reason}\nGeschat netto {net:+.2f} USDT | saldo {STATE["capital"]:.2f}\n'
           +details+execution+record['observation']+'\nGemeten pieken kunnen bewegingen missen; inclusief fees/slippage, zonder funding.')


def manage():
    observations={}
    for symbol,p in list(STATE['positions'].items()):
        if p.get('exit_trigger'):
            bid,ask=ticker(symbol)
            close_paper(symbol,p,bid,ask,p['exit_trigger']['reason'])
            continue
        price = mark(symbol)
        p['last_mark']=price
        sample_position(p,price)
        d = p['direction']
        reason = 'SL' if d*(price-p['stop'])<=0 else 'TP' if d*(price-p['target'])>=0 else 'TIME' if time.time()-p['opened']>=MAX_HOLD_SECONDS else None
        if reason and not p.get('exit_trigger'):
            p['exit_trigger']={'reason':reason,'mark':price,'time':time.time()}
            save()  # retain trigger across a failed ticker request/restart
        # Same mark and quote snapshot is shared with the paired shadow.
        bid,ask=ticker(symbol)
        observations[symbol]=(price,bid,ask)
        if p.get('exit_trigger'):
            close_paper(symbol,p,bid,ask,p['exit_trigger']['reason'])
        else:save()
    manage_shadows(observations)
    # Entry guards never interrupt management of existing paper positions.


def scan_entries(symbols,counts):
    """Gather eligible setups before ranking; preserve one entry per signal."""
    candidates = []
    last_manage = time.monotonic()
    for symbol in symbols:
        if time.monotonic()-last_manage>=MANAGE_SECONDS:
            manage();last_manage=time.monotonic()
        if risk_gate() or STATE['positions']:return
        if time.time()-STATE['last_exit'].get(symbol,0)<COOLDOWN_SECONDS:continue
        try:
            sig,reason,stamp = signal(symbol)
            if reason=='same_candle':continue
            if sig:
                STATE['pending'][symbol]=dict(sig);STATE['seen'][symbol]=stamp;save()
                if sig['quote_volume']<MIN_24H_VOLUME:reason='low_volume'
                elif time.time()-(stamp+DURATION['15m'])/1000>MAX_SIGNAL_AGE:reason='signal_expired'
                else:
                    bid,ask=ticker(symbol)
                    prospect,reason=plan(sig,bid,ask)
                    if prospect:
                        candidates.append((prospect['rank_score'],sig));reason='eligible_not_selected'
                if reason not in ('spread','entry_drift','eligible_not_selected'):STATE['pending'].pop(symbol,None)
            if stamp is not None:
                STATE['seen'][symbol]=stamp;DIAGNOSTICS[symbol]={'ts':stamp,'reason':reason}
                OUTCOMES[(symbol,stamp)]=reason;save()
            else:counts[reason]+=1
        except OSError:raise
        except Exception as exc:
            LOG.error('Scan %s: %s',symbol,exc);counts['API/data error']+=1
        time.sleep(.1)
    if not candidates:return
    ranked=sorted(candidates,key=lambda item:(-item[0],-item[1]['ts'],item[1]['symbol']))
    audit={'id':str(time.time_ns()),'time':time.time(),'policy_version':VERSION,
           'ranked':[{'symbol':s['symbol'],'signal_ts':s['ts'],'score':score,
                      'routes':s.get('routes',[s['route']]),'zone_distance_atr':s.get('zone_distance_atr')}
                     for score,s in ranked], 'chosen':None}
    STATE['selections'].append(audit);STATE['selections']=STATE['selections'][-500:];save()
    for _,sig in ranked:
        if STATE['positions'] or risk_gate():break
        sig=dict(sig,selection_id=audit['id']);symbol,stamp=sig['symbol'],sig['ts']
        try:
            reason=open_paper(sig)
            DIAGNOSTICS[symbol]={'ts':stamp,'reason':reason};OUTCOMES[(symbol,stamp)]=reason
            if reason not in ('spread','entry_drift'):STATE['pending'].pop(symbol,None)
            if reason=='opened':audit['chosen']=symbol
            save()
        except OSError:raise
        except Exception as exc:
            LOG.error('Entry quote/plan %s: %s',symbol,exc);counts['API/data error']+=1
            if symbol in STATE['positions']:audit['chosen']=symbol;save()
        if STATE['positions']:break


def historical_loss_summary():
    losses=[t for t in STATE['trades'] if t.get('net',0)<0]
    if not losses:return ''
    lines=['Laatste opgeslagen verliestrades:']
    for t in losses[-3:]:
        detail=f'{t["symbol"]}: {t["net"]:+.2f} USDT | {t["reason"]}'
        if t.get('risk',0)>0:detail+=f' | resultaat {t["net"]/t["risk"]:+.2f}R'
        detail+=' | '+t.get('observation','Oude trade: koersmetingen/triggerdetails ontbreken.')
        lines.append(detail)
    return '\n'.join(lines)


LABELS = {'no_trend':'4H geen duidelijke trend','no_pullback':'15m geen pullback in EMA-zone',
          'zone_too_far':'bevestiging/entry verder dan 0.75 ATR van pullbackzone',
          'eligible_not_selected':'geldige kandidaat; andere setup geselecteerd/wacht',
          'htf_order_equal':'4H EMA20 en EMA50 gelijk; geen richting',
          'htf_slope_long':'4H EMA20 boven EMA50, maar EMA20 stijgt niet',
          'htf_slope_short':'4H EMA20 onder EMA50, maar EMA20 daalt niet',
          'signal_expired':'geldig signaal verlopen (5 minuten)',
          'no_confirmation':'15m nog geen bevestiging','stale':'candle te oud voor entry',
          'htf_missing':'te weinig afgesloten 4H-data','htf_stale':'4H-data te oud',
          'minimum_contract':'minimumcontract boven risicobudget','entry_drift':'prijs te ver van bevestiging',
          'spread':'spread te groot','low_volume':'24u volume te laag','opened':'paper-entry',
          'few_candles':'te weinig 15m-data','used_rejection':'rejection al gebruikt'}


def status(counts):
    gate = risk_gate()
    lines = [f'📊 {VERSION} | PAPER | {len(META)} munten',
             f'Equity {equity():.2f} USDT | gerealiseerd saldo {STATE["capital"]:.2f}',
             f'Posities {len(STATE["positions"])}/{MAX_POSITIONS} | entries UTC {STATE["guard"].get("attempts",0)}/{MAX_TRADES_PER_DAY}']
    if gate:lines.append('Nieuwe entries geblokkeerd: '+gate)
    for route in ('engulfing','rejection'):
        s = STATE['route_stats'].get(route,{'trades':0,'wins':0,'net':0})
        rate = 100*s['wins']/s['trades'] if s['trades'] else 0
        lines.append(f'{route}: {s["trades"]} gesloten | winst {rate:.1f}% | netto {s["net"]:+.2f} USDT')
    paired=STATE['paired_stats']
    lines.append(f'Volledig gesloten testparen {paired["count"]}: basis {paired["baseline_net"]:+.2f}, shadow {paired["shadow_net"]:+.2f}, verschil {paired["shadow_net"]-paired["baseline_net"]:+.2f} USDT')
    lines.append(f'Shadow beter in {paired["shadow_better"]} paren; {len(STATE["shadows"])} varianten nog open. Alleen basis beïnvloedt saldo/risicolimieten.')
    lines.append('Opslag: '+('volume-pad ingesteld; controleer Railway-koppeling' if VOLUME_PATH else 'lokaal/tijdelijk; deployment kan resultaten wissen'))
    recognized=STATE['recognitions']
    lines.append(f'Herkenningen sinds analyse-update: engulfing {recognized["engulfing"]}, rejection {recognized["rejection"]}, beide {recognized["overlap"]}. Niet allemaal uitgevoerd.')
    for symbol,p in STATE['positions'].items():
        m=p.get('metrics')
        if m:lines.append(f'{symbol}: beste/slechtste gemeten netto {m["peak_net"]:+.2f}/{m["trough_net"]:+.2f} USDT')
    if counts:
        lines.append('Een uitkomst per munt/afgesloten candle; geen herhaalde scans:')
        lines.extend(f'• {LABELS.get(k,k)}: {v}' for k,v in counts.items())
    for symbol,info in DIAGNOSTICS.items():
        stamp = datetime.fromtimestamp((info['ts']+DURATION['15m'])/1000,timezone.utc).strftime('%H:%M UTC')
        lines.append(f'{symbol} [{stamp}]: {LABELS.get(info["reason"],info["reason"])}')
    lines.append('Paper schat fills/kosten; geen echte orders en geen funding.')
    return '\n'.join(lines)


def startup():
    load()
    data = get('/api/v1/market/instruments',{'instType':'SWAP'})
    skipped = []
    for symbol in SYMBOLS:
        instrument = next((x for x in data if x.get('instId')==symbol and x.get('state')=='live'),None)
        if instrument is None or instrument.get('contractType')!='linear' or instrument.get('settleCurrency')!='USDT':
            skipped.append(symbol);continue
        values = {'tick':number(instrument['tickSize']),'lot':number(instrument['lotSize']),
                  'minimum':number(instrument['minSize']),'cv':number(instrument['contractValue']),
                  'maximum':number(instrument['maxMarketSize'])}
        if min(values.values())<=0:raise ValueError('Invalid contract metadata')
        META[symbol]=values
    if not META:raise RuntimeError('No supported symbols')
    if set(STATE['positions'])-set(META):raise RuntimeError('Saved paper position instrument unavailable')
    if any(s['symbol'] not in META for s in STATE['shadows'].values()):
        raise RuntimeError('Saved shadow instrument unavailable; do not discard paired history.')
    risk_gate()
    notify(f'🧪 {VERSION} gestart: PAPER\n{", ".join(META)}\n4H EMA20/50 + helling | 15m pullback + engulfing OF rejection-bevestiging\n'
           f'4-candle pullback | zonebuffer 0.25 ATR | wick 1.5× | signaal 5 min | drift 0.35 ATR\n'
           f'Startsaldo {INITIAL_CAPITAL:.2f} | risicodoel {RISK_PER_TRADE*100:g}% | maximaal {MAX_TRADES_PER_DAY} entries/dag\n'
           f'1 positie | 1.8R | tijdslimiet 4 uur | geen echte orders')
    notify('Alle geldige kandidaten worden vergeleken op een vaste selectiescore; geen bewezen winstkans. Nieuwe trades meten koersverloop en stop-uitvoering; beide patronen worden geteld.')
    notify('Afstandsgrens 0.75 ATR actief. Basis blijft 1.8R; shadow test +1 nettoR→kostendekkende bid/ask-stop. Alleen volledig gesloten paren worden vergeleken; gaps kunnen ook bij BE verlies geven.')
    summary=historical_loss_summary()
    if summary:notify(summary)
    if skipped:notify('Niet beschikbaar, overgeslagen: '+', '.join(skipped))
    if not VOLUME_PATH:notify('⚠️ Zonder gekoppeld volume kunnen paperstatus en resultaten bij deployment verdwijnen.')


def serve_health():
    from flask import Flask
    app = Flask(__name__)
    @app.route('/')
    def home():
        fresh = time.time()-HEALTH['cycle']<120
        return {'bot':VERSION,'paper':True,'recent_cycle':fresh,'error':HEALTH['error']},200 if fresh and not HEALTH['error'] else 503
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')),use_reloader=False)


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    import fcntl
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)),exist_ok=True)
    lock = open(STATE_FILE+'.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    startup()
    threading.Thread(target=serve_health,daemon=True).start()
    next_scan,last_report,last_alert = 0,0,0
    counts = Counter()
    while True:
        try:
            manage()
            gate = risk_gate()
            if time.time()>=next_scan:
                if not gate and len(STATE['positions'])<MAX_POSITIONS:
                    scan_entries(list(META),counts)
                next_scan=time.time()+SCAN_SECONDS
            if time.time()-last_report>=REPORT_SECONDS:
                reported = counts + Counter(OUTCOMES.values())
                notify(status(reported));counts.clear();OUTCOMES.clear();last_report=time.time()
            HEALTH.update(cycle=time.time(),error='')
        except KeyboardInterrupt:
            notify('Paper-bot gestopt; status opgeslagen.');break
        except OSError:raise
        except Exception as exc:
            LOG.error('Management cycle: %s',exc)
            HEALTH['error']='market data/management unavailable'
            if time.time()-last_alert>300:
                notify('Marktdata/positiecontrole tijdelijk niet beschikbaar; nieuwe entries gepauzeerd.')
                last_alert=time.time()
        time.sleep(MANAGE_SECONDS)


if __name__=='__main__':
    main()
