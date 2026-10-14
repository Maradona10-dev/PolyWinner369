#!/usr/bin/env python3
"""Polymarket momentum v6 — Python 3.11+, SDK 0.13.0.
Momentum thresholds, RSI rounding, pair order and 120..255s window are from
user-supplied v5.3. No pullback route. The old 15m check always returned NEUTRAL
(10 closes <15 needed for RSI): preserved explicitly, NOT silently strengthened.
Binance remains a SIGNAL reference only. Payoff uses official RESOLVED payouts.
Paper buys walk real asks with an adverse one-tick estimate and dynamic fees.
Paper is a snapshot simulation, not a reservation or guarantee of live fills.
LIVE: explicit Python setting, FOK with hard 0.70 cap; persisted intent BEFORE
posting. Ambiguous submission blocks ALL new entries, never retries blindly.
Live trade quantities/prices come from CONFIRMED account fills. Fee figures
remain estimates (no claim of exact wallet PnL). Redemption is manual on site.
A private key never enables live. No private key is printed or stored in state.
State persists across redeploys ONLY on a real mounted persistent volume.
Run: python bot.py ; read-only live account check: python bot.py --check-live
"""
import argparse
import dataclasses
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import requests
from polymarket import PublicClient, SecureClient, RelayerApiKey

VERSION = '6.0-Polymarket-Momentum'
MODE = 'paper'                    # change to 'live' EXPLICITLY after account check
PAIRS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ZECUSDT']
MAX_TRADES_PER_CYCLE = 2
MAX_ALLOWABLE_ODDS = Decimal('0.70')
PAPER_START = 1000.0
PAPER_TRADE_AMOUNT = Decimal('25')  # all-in budget, including modeled fees
LIVE_TRADE_AMOUNT = Decimal('5')    # all-in target; actual fees can change
SCAN_INTERVAL_SEC = 10
PAPER_SLIPPAGE_TICKS = 1
MAX_BOOK_AGE = 15
REPORT_SECONDS = 1800
# False reproduces v5.3's EFFECTIVE neutral 15m behavior. True would be a
# strategy change and requires its own comparison, so it is deliberately off.
ENABLE_CORRECTED_15M_FILTER = False
BASE = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or os.getenv('BOT_DATA_DIR') or '.')
LOG = logging.getLogger('momentum')
SESSION = requests.Session()
PUBLIC = None
LIVE = None
STATE = None
STATE_FILE = None
COUNTS = Counter()

class Skip(Exception): pass


def dec(value):
    value = Decimal(str(value))
    if not value.is_finite(): raise ValueError('Niet-eindig getal')
    return value


def dump(value):
    if hasattr(value, 'model_dump'): return value.model_dump(mode='json')
    if dataclasses.is_dataclass(value): return dataclasses.asdict(value)
    return value


def save():
    temporary = STATE_FILE.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(STATE,f,allow_nan=False,default=str)
        f.flush();os.fsync(f.fileno())
    os.replace(temporary,STATE_FILE)


def load_state(wallet=None):
    global STATE,STATE_FILE
    BASE.mkdir(parents=True,exist_ok=True)
    STATE_FILE=BASE/f'polymarket_momentum_{MODE}.json'
    if STATE_FILE.exists():
        STATE=json.loads(STATE_FILE.read_text())
        if STATE['mode']!=MODE or STATE.get('wallet')!=wallet:
            raise RuntimeError('State hoort bij andere modus/wallet; niet overschrijven.')
    else:
        STATE={'mode':MODE,'wallet':wallet,'cash':PAPER_START,'trades':{},'started':time.time()}
        save()


def get_json(url,params=None):
    r=SESSION.get(url,params=params,timeout=(5,10));r.raise_for_status();return r.json()


def notify(message):
    LOG.info(message)
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip().removeprefix('bot')
    chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:return
    try:
        for offset in range(0,len(message),3500):
            r=SESSION.post('https://api.telegram.org/bot'+token+'/sendMessage',
                           json={'chat_id':chat,'text':message[offset:offset+3500]},timeout=(5,10))
            if not r.ok or not r.json().get('ok'):LOG.warning('Telegram delivery failed')
    except requests.RequestException:LOG.warning('Telegram tijdelijk niet beschikbaar')


def calculate_rsi(prices,period=14):
    if len(prices)<period+1:return 50.0
    changes=[b-a for a,b in zip(prices,prices[1:])]
    gains=[max(c,0) for c in changes];losses=[max(-c,0) for c in changes]
    gain=sum(gains[:period])/period;loss=sum(losses[:period])/period
    for i in range(period,len(gains)):
        gain=(gain*(period-1)+gains[i])/period;loss=(loss*(period-1)+losses[i])/period
    if loss==0:return 100.0  # preserved original convention, including flat data
    return round(100-100/(1+gain/loss),1)


def momentum(symbol,rsi,delta,volume,remaining,trend='NEUTRAL'):
    if symbol in ['ZECUSDT','DOGEUSDT']: min_delta,min_volume=.14,1.6
    elif symbol in ['SOLUSDT','BNBUSDT','XRPUSDT']:min_delta,min_volume=.08,1.3
    else:min_delta,min_volume=.05,1.2
    if 120<=remaining<=255:
        if 53<=rsi<=68 and delta>=min_delta and volume>=min_volume and trend!='BEARISH':return 'UP'
        if 32<=rsi<=47 and delta<=-min_delta and volume>=min_volume and trend!='BULLISH':return 'DOWN'
    return None


def klines(symbol,interval,limit):
    result=get_json('https://api.binance.com/api/v3/klines',{'symbol':symbol,'interval':interval,'limit':limit})
    if not isinstance(result,list) or len(result)<limit:raise Skip('binance_data')
    return result


def signal_data(symbol):
    five=klines(symbol,'5m',2);one=klines(symbol,'1m',30)
    now=time.time();start=int(now)//300*300
    if int(five[-1][0])!=start*1000:raise Skip('binance_window')
    if not 0<=now-int(one[-1][0])/1000<90:raise Skip('binance_stale')
    prices=[float(k[4]) for k in one];volumes=[float(k[5]) for k in one]
    strike=float(five[-1][1]);price=prices[-1]
    avg=sum(volumes[-11:-1])/10
    trend='NEUTRAL'
    if ENABLE_CORRECTED_15M_FILTER:
        fifteen=klines(symbol,'15m',60)
        closed=[k for k in fifteen if int(k[6])<now*1000]
        closes=[float(k[4]) for k in closed];r=calculate_rsi(closes)
        change=(closes[-1]/closes[-3]-1)*100
        trend='BULLISH' if change>.10 and r>50 else 'BEARISH' if change<-.10 and r<50 else 'NEUTRAL'
    return dict(symbol=symbol,cycle=start,expiry=start+300,binance_reference=strike,
                price=price,rsi=calculate_rsi(prices),delta=round((price/strike-1)*100,3),
                volume_ratio=round(volumes[-1]/avg,2) if avg>0 else 1.0,trend=trend)


def asset_of(market,outcome):
    version=getattr(market.version,'value',market.version)
    asset=outcome.position_id if version=='v2' else outcome.token_id
    if not asset:raise Skip('asset_id_missing')
    return str(asset)


def discover(symbol,cycle,direction,client=None):
    client=client or PUBLIC
    slug=f'{symbol.removesuffix("USDT").lower()}-updown-5m-{cycle}'
    m=client.get_market(slug=slug)
    if m.slug!=slug or not m.condition_id:raise Skip('wrong_contract')
    state=m.state
    if state.closed or not state.active or not state.accepting_orders or not state.enable_order_book:
        raise Skip('market_not_tradable')
    if state.end_date is None or abs(state.end_date.timestamp()-(cycle+300))>1:raise Skip('wrong_expiry')
    chosen=None
    for index,outcome in enumerate((m.outcomes.yes,m.outcomes.no)):
        if outcome.label.strip().upper()==direction:chosen=(index,outcome)
    if chosen is None or {m.outcomes.yes.label.strip().upper(),m.outcomes.no.label.strip().upper()}!={'UP','DOWN'}:
        raise Skip('outcome_mapping')
    index,outcome=chosen
    fees=m.trading.fee_schedule
    if m.trading.fees_enabled is not False and fees is None:raise Skip('fee_unknown')
    return m,dict(slug=slug,market_id=str(m.id),condition_id=str(m.condition_id),
                  asset_id=asset_of(m,outcome),outcome_index=index,
                  fee_rate=str(fees.rate if fees and m.trading.fees_enabled is not False else 0),fee_exponent=float(fees.exponent if fees else 1),
                  resolution_source=m.resolution.source,
                  url='https://polymarket.com/event/'+slug)


def fee_per_share(price,record):
    p=dec(price);rate=dec(record['fee_rate']);exponent=dec(record['fee_exponent'])
    if not 0<p<1 or rate<0 or exponent<0:raise ValueError('Invalid fee inputs')
    return rate*((p*(1-p))**exponent)


def paper_quote(book,record,budget):
    now=time.time()
    if book.timestamp is None or not -5<=now-book.timestamp.timestamp()<=MAX_BOOK_AGE:raise Skip('book_stale')
    if str(book.asset_id)!=record['asset_id'] or str(book.condition_id)!=record['condition_id']:raise Skip('wrong_book')
    left=dec(budget);qty=Decimal(0);notional=Decimal(0);fee=Decimal(0)
    for level in sorted(book.asks,key=lambda x:x.price):
        price=dec(level.price)+PAPER_SLIPPAGE_TICKS*dec(book.tick_size)
        size=dec(level.size)
        if price>MAX_ALLOWABLE_ODDS:break
        if size<=0 or price<=0:continue
        unitfee=fee_per_share(price,record)
        take=min(size,left/(price+unitfee))
        qty+=take;notional+=take*price;fee+=take*unitfee;left-=take*(price+unitfee)
        if left<=Decimal('.000001'):break
    if left>Decimal('.01'):raise Skip('insufficient_depth_under_price_cap')
    qty=qty.quantize(Decimal('.000001'),rounding=ROUND_FLOOR)
    if qty<dec(book.min_order_size):raise Skip('minimum_size')
    # Conservative rounding: retain full modeled spend while flooring shares.
    return dict(shares=float(qty),notional=float(notional),fee_estimate=float(fee),
                spent=float(notional+fee),average_price=float(notional/qty),quote_time=now)


def final_payout(record,client=None):
    client=client or PUBLIC
    matches=[r for r in client.get_resolutions(condition_ids=record['condition_id'])
             if str(r.condition_id)==record['condition_id'] and getattr(r.status,'value',r.status)=='resolved']
    if len(matches)!=1 or matches[0].payouts is None:return None
    payouts=list(map(dec,matches[0].payouts))
    if len(payouts)!=2 or any(p<0 or p>1 for p in payouts) or abs(sum(payouts)-1)>Decimal('.00000001'):
        raise ValueError('Invalid final payouts')
    return float(payouts[record['outcome_index']])


def live_client():
    if importlib.metadata.version('polymarket-client')!='0.13.0':raise RuntimeError('Installeer requirements.txt exact.')
    key=os.getenv('POLYMARKET_PRIVATE_KEY','').strip()
    if not re.fullmatch(r'(0x)?[0-9a-fA-F]{64}',key):raise RuntimeError('Ongeldige private-key-notatie; niet automatisch gewijzigd.')
    wallet=os.getenv('POLYMARKET_WALLET_ADDRESS','').strip()
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet):raise RuntimeError('POLYMARKET_WALLET_ADDRESS ontbreekt/is ongeldig.')
    geo=get_json('https://polymarket.com/api/geoblock')
    if geo.get('blocked') is not False:raise RuntimeError('Live geblokkeerd door locatiecontrole of onbekende uitkomst.')
    kwargs={}
    relayer=os.getenv('POLYMARKET_RELAYER_API_KEY','').strip()
    if relayer:
        kwargs['api_key']=RelayerApiKey(key=relayer,address=os.environ['POLYMARKET_RELAYER_API_KEY_ADDRESS'])
    client=SecureClient.create(private_key=key,wallet=wallet,**kwargs)
    # No guessed signature_type, no new wallet, no funds transfer.
    balance=client.get_balance_allowance(asset_type='COLLATERAL')
    approvals=client.get_trading_approvals_state()
    notify(f'Live accountcontrole: wallet {client.wallet} | type {client.wallet_type}\n'
           f'Collateral {balance.balance/1_000_000:.6f} | trading approvals compleet: {approvals.is_fully_approved}.\n'
           'Geen order geplaatst en geen approvals gewijzigd.')
    return client


def open_trade(data,direction):
    key=f'{data["cycle"]}:{data["symbol"]}:MOMENTUM'
    if key in STATE['trades']:return
    if any(t['status'] in ('intent','unknown','submitted','reconcile') for t in STATE['trades'].values()):
        raise Skip('live_reconciliation_pending')
    _,contract=discover(data['symbol'],data['cycle'],direction)
    book=PUBLIC.get_order_book(asset_id=contract['asset_id'])
    amount=PAPER_TRADE_AMOUNT if MODE=='paper' else LIVE_TRADE_AMOUNT
    quote=paper_quote(book,contract,amount)
    if MODE=='paper' and dec(STATE['cash'])<dec(quote['spent']):raise Skip('paper_insufficient_cash')
    # Recheck time AFTER network calls. Never enter the next round accidentally.
    remaining=data['expiry']-time.time()
    if not 120<=remaining<=255:raise Skip('signal_expired')
    record=dict(data,**contract,direction=direction,key=key,opened=time.time(),mode=MODE,
                status='intent',estimate=quote,requested=float(amount),order_id=None)
    if MODE=='paper':
        record.update(quote,status='open',fee_basis='orderbook_model')
        STATE['cash']-=quote['spent'];STATE['trades'][key]=record;save()
    else:
        bal=LIVE.get_balance_allowance(asset_type='COLLATERAL')
        if dec(bal.balance)/1_000_000<amount:raise Skip('live_insufficient_cash')
        # SDK respects tick/size and protocol version and includes fee allowance.
        signed=LIVE.create_market_order(asset_id=record['asset_id'],side='BUY',amount=str(amount),
                                        max_spend=str(amount),max_price=str(MAX_ALLOWABLE_ODDS),order_type='FOK')
        if not 120<=data['expiry']-time.time()<=255:raise Skip('signal_expired')
        record['signed_shares']=float(Decimal(signed.taker_amount)/1_000_000)
        STATE['trades'][key]=record;save()  # ambiguous on restart: NEVER repost
        try:
            result=LIVE.post_order(signed)
        except Exception:
            record['status']='unknown';save()
            notify('LIVE orderuitkomst onbekend; nieuwe entries geblokkeerd. Controleer Orders/Trades op Polymarket. Niet opnieuw indienen.')
            return
        if not result.ok:
            record.update(status='rejected',error=str(result.message));save()
            notify('LIVE order geweigerd: '+str(result.message));return
        record.update(status='submitted',order_id=result.order_id,response=dump(result));save()
        notify(f'LIVE order ontvangen ({result.status}); wacht op bevestigde fills. {record["url"]}')
        return
    notify(f'PAPER MOMENTUM {data["symbol"]} {direction}\nRSI {data["rsi"]} | volume {data["volume_ratio"]}x | delta {data["delta"]}%\n'
           f'Binance-signaalreferentie {data["binance_reference"]}; officiële payoff volgt marktregels.\n'
           f'Geschatte VWAP {quote["average_price"]:.4f} | aandelen {quote["shares"]:.6f}\n'
           f'All-in {quote["spent"]:.2f}, geschatte fee {quote["fee_estimate"]:.4f} | resterend {remaining:.0f}s\n{record["url"]}')


def reconcile_live(record):
    if not record.get('order_id'):return
    order=LIVE.get_order(order_id=record['order_id'])
    if str(order.asset_id)!=record['asset_id'] or order.side!='BUY':raise RuntimeError('Order identity mismatch')
    trades=[]
    for page in LIVE.list_account_trades(asset_id=record['asset_id'],market=record['condition_id']):
        trades.extend(t for t in page.items if t.taker_order_id==record['order_id'] and t.side=='BUY')
    unique={t.id:t for t in trades};trades=list(unique.values())
    if any(t.status=='FAILED' for t in trades):
        record['status']='reconcile';save();return
    confirmed=[t for t in trades if t.status=='CONFIRMED']
    qty=sum((t.size for t in confirmed),Decimal(0))
    matched=dec(order.size_matched)
    if matched==0 and str(order.status).upper() in ('CANCELED','CANCELLED','UNMATCHED'):
        record['status']='unfilled';save();return
    if matched<=0 or abs(qty-matched)>Decimal('.00001') or len(confirmed)!=len(trades):return
    # Require FOK's entire signed quantity, not merely an accepted response.
    if abs(qty-dec(record['signed_shares']))>Decimal('.0001'):
        record['status']='reconcile';save();return
    notional=sum(t.size*t.price for t in confirmed)
    fees=sum(t.size*fee_per_share(t.price,record) for t in confirmed)
    record.update(status='open',shares=float(qty),notional=float(notional),fee_estimate=float(fees),
                  spent=float(notional+fees),average_price=float(notional/qty),fee_basis='snapshot_estimate',
                  fill_ids=[t.id for t in confirmed],confirmed_at=time.time())
    save();notify(f'LIVE fills CONFIRMED: {record["symbol"]} {record["direction"]}\n'
                  f'{qty} aandelen | echte VWAP {notional/qty:.6f} | fees nog geschat. {record["url"]}')


def manage():
    for record in list(STATE['trades'].values()):
        try:
            if record['status']=='intent':
                record['status']='unknown';save()  # crash may have happened after POST
                notify('Onzekere live-intentie hersteld na herstart; nieuwe entries geblokkeerd. Controleer de orderhistorie; niet blind opnieuw plaatsen.')
            if record['mode']=='live' and record['status'] in ('submitted','reconcile'):
                reconcile_live(record)
            if record['status']!='open' or time.time()<record['expiry']+3:continue
            if time.time()-record.get('last_resolution_check',0)<20:continue
            record['last_resolution_check']=time.time()
            payout=final_payout(record)
            if payout is None:continue
            value=record['shares']*payout
            record.update(status='resolved',payout_per_share=payout,payout=value,
                          net_estimate=value-record['spent'],resolved_at=time.time())
            if record['mode']=='paper':STATE['cash']+=value
            save()
            label='PAPER netto geschat' if record['mode']=='paper' else 'LIVE resultaat geschat; redemption nog controleren'
            notify(f'{record["symbol"]} {record["direction"]} officieel RESOLVED\n'
                   f'Uitbetaling/aandeel {payout:g} | {label}: {record["net_estimate"]:+.2f}\n'
                   'Livewinst wordt niet automatisch als bijgeschreven saldo gemeld; geen automatische redemption.')
        except OSError:raise
        except Exception as e:
            LOG.warning('Positiecontrole %s: %s',record['symbol'],type(e).__name__)
            COUNTS['management_'+type(e).__name__]+=1


def status():
    closed=[t for t in STATE['trades'].values() if t['status']=='resolved']
    open_=[t for t in STATE['trades'].values() if t['status']=='open']
    uncertain=[t for t in STATE['trades'].values() if t['status'] in ('intent','unknown','submitted','reconcile')]
    return (f'{VERSION} | {MODE.upper()}\nOpen {len(open_)} | wachtende/onzekere orders {len(uncertain)}\n'
            f'Afgerond {len(closed)} | winst {sum(t["net_estimate"]>0 for t in closed)} | netto GESCHAT {sum(t["net_estimate"] for t in closed):+.2f}\n'
            +(f'Paper vrij {STATE["cash"]:.2f} | inzet geblokkeerd in open trades {sum(t["spent"] for t in open_):.2f}\n' if MODE=='paper' else '')
            +'Afwijzingen/controles: '+str(dict(COUNTS))+'\n'
            +'Opslag: '+('Railway-volume ingesteld; koppeling controleren' if os.getenv('RAILWAY_VOLUME_MOUNT_PATH') else 'lokaal; volume nodig voor behoud bij deployment'))


def main():
    global PUBLIC,LIVE
    parser=argparse.ArgumentParser();parser.add_argument('--check-live',action='store_true');args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if MODE not in ('paper','live'):raise RuntimeError('MODE moet paper of live zijn')
    PUBLIC=PublicClient()
    if args.check_live:
        client=live_client();client.close();return  # no orders, no approvals
    if MODE=='live':LIVE=live_client()
    BASE.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(BASE/f'polymarket_momentum_{MODE}.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    load_state(str(LIVE.wallet) if LIVE else None)
    notify(f'{VERSION} gestart | {MODE.upper()}\nAlleen oorspronkelijke MOMENTUM-regels, 120–255s resterend.\n'
           'Echte orderboeken, plafond 0.70; officiële resolutie. 15m-filter neutraal zoals oorspronkelijke code.\n'
           'Binance bepaalt signaal, niet Polymarket-afrekening. Paperresultaat blijft een simulatie.')
    if 'PORT' in os.environ:
        from flask import Flask
        app=Flask(__name__)
        @app.get('/')
        def health():return {'bot':VERSION,'mode':MODE,'last_cycle':heartbeat[0]},200 if time.time()-heartbeat[0]<120 else 503
        heartbeat=[time.time()]
        threading.Thread(target=lambda:app.run(host='0.0.0.0',port=int(os.environ['PORT']),use_reloader=False),daemon=True).start()
    report=0
    try:
        while True:
            manage()
            cycle=int(time.time())//300*300
            count=sum(t['cycle']==cycle for t in STATE['trades'].values())
            for symbol in PAIRS:
                manage()
                if count>=MAX_TRADES_PER_CYCLE:break
                try:
                    data=signal_data(symbol)
                    if data['cycle']!=cycle:break
                    direction=momentum(symbol,data['rsi'],data['delta'],data['volume_ratio'],data['expiry']-time.time(),data['trend'])
                    if not direction:COUNTS['no_momentum']+=1;continue
                    before=len(STATE['trades']);open_trade(data,direction)
                    count+=len(STATE['trades'])-before
                except Skip as e:COUNTS[str(e)]+=1
                except requests.RequestException as e:COUNTS[type(e).__name__]+=1
                except OSError:raise
                except Exception as e:
                    LOG.warning('Scan %s: %s',symbol,type(e).__name__);COUNTS[type(e).__name__]+=1
            if time.time()-report>=REPORT_SECONDS:
                notify(status());COUNTS.clear();report=time.time()
            if 'PORT' in os.environ:heartbeat[0]=time.time()
            time.sleep(SCAN_INTERVAL_SEC)
    finally:
        save();PUBLIC.close()
        if LIVE:LIVE.close()

if __name__=='__main__':main()
