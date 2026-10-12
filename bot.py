#!/usr/bin/env python3
"""Trend + pullback + confirmation: BloFin public futures data, PAPER ONLY.
Python >=3.10. Install: requests pandas flask. Start: python bot.py
Telegram secrets remain TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway.
No BloFin credentials are needed; this program has no exchange order endpoint.
All trading settings below are Python constants; Railway cannot enable live.
Uses exchange-confirmed closed 15m candles and closed 4H EMA20/EMA50 trend.
A: body engulfing. B: wick rejection confirmed within two closed candles.
If both qualify, A wins; never two entries on one signal. Stop beyond the
three-candle pullback extreme, or earlier rejection extreme, plus 0.15 ATR.
Targets 1.8 price R, not guaranteed net R. Maximum holding time is sampled
at each management cycle; API outages can delay simulated exits.
Risk sizing includes modeled stop slippage and both fees; gaps can exceed it.
Paper entries/exits use executable ask/bid plus adverse slippage. Mark-price
TP/SL are polled, not real protective orders. Moves between samples can be
missed. No funding model. No claims of profitability or historical backtest.
UTC daily/weekly boundaries; weekly and loss-streak stops require manual review.
State is separate from CRT. Without a Railway volume, redeploy may reset it.
"""
import os
import json
import math
import time
import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from collections import Counter
import requests
import pandas as pd

# ====================== SETTINGS: CHANGE IN PYTHON ======================
VERSION = '1.0-Trend-Pullback-Paper'
PAPER_TRADING = True  # fixed paper-only program; no real order functionality
SYMBOLS = ['BTC-USDT','ETH-USDT','SOL-USDT','BNB-USDT','XRP-USDT','ZEC-USDT']
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
MAX_ENTRY_DRIFT_ATR = 0.25
MAX_HOLD_SECONDS = 4*3600
MAX_SIGNAL_AGE = 120
MIN_STOP_PCT = 0.0001
MAX_STOP_PCT = 0.04
FEE = 0.0004  # modeled fee per side, not verified account fee tier
SLIPPAGE = 0.0002
MAX_SPREAD = 0.001
MIN_24H_VOLUME = 1000000.0  # USDT quote volume from 96 completed 15m candles
COOLDOWN_SECONDS = 900
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
HEALTH = {'cycle':0,'error':''}
STATE = {'strategy':'trend-pullback-paper-v1','capital':INITIAL_CAPITAL,'positions':{},
         'seen':{},'used_rejections':{},'last_exit':{},'trades':[], 'route_stats':{},'guard':{}}
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
    temporary = STATE_FILE+'.tmp'
    with open(temporary,'w') as handle:
        json.dump(STATE,handle,allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary,STATE_FILE)


def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as handle:
            saved = json.load(handle)
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
    last, earlier = htf.iloc[-1],htf.iloc[-4]
    bias = 1 if last.ema20>last.ema50 and last.ema20>earlier.ema20 else -1 if last.ema20<last.ema50 and last.ema20<earlier.ema20 else 0
    return bias,{'ts':int(last.ts),'ema20':float(last.ema20),'ema50':float(last.ema50),'ema20_before':float(earlier.ema20)},'ok'


def touches_zone(row):
    bottom,top = min(row.ema20,row.ema50),max(row.ema20,row.ema50)
    return row.low<=top and row.high>=bottom


def pattern(df,direction):
    """Pure closed-candle rules, suitable for offline tests. A has precedence."""
    i = len(df)-1
    row,prev = df.iloc[i],df.iloc[i-1]
    recent = df.iloc[i-2:i+1]
    if not any(touches_zone(r) for _,r in recent.iterrows()):
        return None,'no_pullback'
    favorable = direction*(row.close-row.open)>0
    engulf = (prev.close<prev.open and row.open<=prev.close and row.close>=prev.open) if direction==1 else (prev.close>prev.open and row.open>=prev.close and row.close<=prev.open)
    if favorable and engulf and abs(row.close-row.open)>abs(prev.close-prev.open):
        return {'route':'engulfing','anchor':int(prev.ts),'start':i-2},'ok'
    for j in (i-1,i-2):
        rejection = df.iloc[j]
        body = abs(rejection.close-rejection.open)
        wick = min(rejection.close,rejection.open)-rejection.low if direction==1 else rejection.high-max(rejection.close,rejection.open)
        if body<=0 or wick<2*body or not touches_zone(rejection):
            continue
        threshold = rejection.high if direction==1 else rejection.low
        broke = direction*(row.close-threshold)>0
        already_broke = any(direction*(r.close-threshold)>0 for _,r in df.iloc[j+1:i].iterrows())
        if favorable and broke and not already_broke:
            return {'route':'rejection','anchor':int(rejection.ts),'start':min(i-2,j)},'ok'
    return None,'no_confirmation'


def signal(symbol):
    raw = candles(symbol)
    if len(raw)<100:
        return None,'few_candles',None
    stamp = int(raw.ts.iloc[-1])
    if STATE['seen'].get(symbol,-1)>=stamp:
        return None,'same_candle',stamp
    decision = stamp+DURATION['15m']
    age = time.time()-decision/1000
    if age<0 or age>MAX_SIGNAL_AGE:
        return None,'stale',stamp
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
    extreme = float(pullback.low.min() if direction==1 else pullback.high.max())
    stop = extreme-direction*STOP_ATR_BUFFER*atr
    return {'symbol':symbol,'direction':direction,'route':setup['route'],'anchor':setup['anchor'],
            'ts':stamp,'close':float(row.close),'atr':atr,'stop':stop,'htf':htf,
            'zone_low':float(min(row.ema20,row.ema50)),
            'zone_high':float(max(row.ema20,row.ema50)),
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


def plan(sig,bid,ask):
    if (ask-bid)/((ask+bid)/2)>MAX_SPREAD:
        return None,'spread'
    d = sig['direction']
    executable = ask if d==1 else bid
    fill = executable*(1+d*SLIPPAGE)
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
    return dict(sig,entry=fill,stop=stop,target=target,contracts=contracts,units=units,
                entry_fee=units*fill*FEE,risk=units*loss,risk_budget=budget,
                opened=time.time(),last_mark=executable),'ok'


def open_paper(sig):
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
    p['last_mark']=current
    STATE['capital']-=p['entry_fee']
    STATE['positions'][sig['symbol']]=p
    STATE['guard']['attempts']+=1
    if sig['route']=='rejection':STATE['used_rejections'][sig['symbol']]=sig['anchor']
    save()
    label = 'LONG' if p['direction']==1 else 'SHORT'
    notify(f'🧪 PAPER OPEN {sig["symbol"]} {label}\nRoute: {p["route"]} | 4H EMA20 {p["htf"]["ema20"]:.6g}, EMA50 {p["htf"]["ema50"]:.6g}\n'
           f'15m EMA-zone {p["zone_low"]:.8g}–{p["zone_high"]:.8g} | bevestigingsclose {p["close"]:.8g}\n'
           f'Entry {p["entry"]:.8g} | SL {p["stop"]:.8g} | TP {p["target"]:.8g}\n'
           f'15m-pullback + gesloten bevestiging | risico geschat {p["risk"]:.2f}/{p["risk_budget"]:.2f} USDT\n'
           f'Positiewaarde {p["units"]*p["entry"]:.2f} USDT | tijdslimiet 4 uur')
    return 'opened'


def close_paper(symbol,p,bid,ask,reason):
    d = p['direction']
    exit_price = (bid if d==1 else ask)*(1-d*SLIPPAGE)
    price_pnl = p['units']*(exit_price-p['entry'])*d
    exit_fee = p['units']*exit_price*FEE
    net = price_pnl-p['entry_fee']-exit_fee
    STATE['capital']+=price_pnl-exit_fee  # entry fee was already charged
    record = {'symbol':symbol,'route':p['route'],'direction':d,'opened':p['opened'],
              'closed':time.time(),'entry':p['entry'],'exit':exit_price,'reason':reason,
              'entry_fee':p['entry_fee'],'exit_fee':exit_fee,'net':net,'risk':p['risk']}
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
    notify(f'🧪 PAPER CLOSE {symbol} | {p["route"]} | {reason}\nGeschat netto {net:+.2f} USDT | saldo {STATE["capital"]:.2f}\nInclusief beide handelsfees/slippage; zonder funding.')


def manage():
    for symbol,p in list(STATE['positions'].items()):
        price = mark(symbol)
        p['last_mark']=price
        d = p['direction']
        reason = 'SL' if d*(price-p['stop'])<=0 else 'TP' if d*(price-p['target'])>=0 else 'TIME' if time.time()-p['opened']>=MAX_HOLD_SECONDS else None
        if reason:
            bid,ask = ticker(symbol)
            close_paper(symbol,p,bid,ask,reason)
        else:save()
    # Entry guards never interrupt management of existing paper positions.


LABELS = {'no_trend':'4H geen duidelijke trend','no_pullback':'15m geen pullback in EMA-zone',
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
    risk_gate()
    notify(f'🧪 {VERSION} gestart: PAPER\n{", ".join(META)}\n4H EMA20/50 + helling | 15m pullback + engulfing OF rejection-bevestiging\n'
           f'Startsaldo {INITIAL_CAPITAL:.2f} | risicodoel {RISK_PER_TRADE*100:g}% | maximaal {MAX_TRADES_PER_DAY} entries/dag\n'
           f'1 positie | 1.8R | tijdslimiet 4 uur | geen echte orders')
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
    next_scan,last_report,last_alert,cursor = 0,0,0,0
    counts = Counter()
    while True:
        try:
            manage()
            gate = risk_gate()
            if time.time()>=next_scan:
                symbols = list(META)
                ordered = symbols[cursor:]+symbols[:cursor]
                cursor=(cursor+1)%len(symbols)
                for symbol in ordered:
                    # No scanning for entries while a position or risk gate blocks them.
                    if gate or len(STATE['positions'])>=MAX_POSITIONS:break
                    if time.time()-STATE['last_exit'].get(symbol,0)<COOLDOWN_SECONDS:continue
                    try:
                        sig,reason,stamp = signal(symbol)
                        if reason=='same_candle':continue
                        if sig:reason=open_paper(sig)
                        if stamp is not None:
                            STATE['seen'][symbol]=stamp
                            DIAGNOSTICS[symbol]={'ts':stamp,'reason':reason}
                            save()
                        counts[reason]+=1
                    except OSError:raise
                    except Exception as exc:
                        LOG.error('Scan %s: %s',symbol,exc)
                        counts['API/data error']+=1
                    time.sleep(.1)
                next_scan=time.time()+SCAN_SECONDS
            if time.time()-last_report>=REPORT_SECONDS:
                notify(status(counts));counts.clear();last_report=time.time()
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
