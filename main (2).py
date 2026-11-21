import os, time, math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_VERSION = '4.0.0'
ROOT = Path(__file__).resolve().parent
API_KEY = os.getenv('TWELVE_DATA_API_KEY', '')
BASE = 'https://api.twelvedata.com'
SYMBOLS = ['XAU/USD','EUR/USD','GBP/USD','USD/JPY','USD/CHF','AUD/USD','USD/CAD','NZD/USD']
TF = {'1min','5min','15min','30min','1h','4h','1day','1week','1month'}
CACHE: dict[str, tuple[float, Any]] = {}
CACHE_SECONDS = 35
app = FastAPI(title='APEX Market Intelligence', version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])


def provider(endpoint: str, params: dict, ttl: int = CACHE_SECONDS):
    if not API_KEY: raise HTTPException(503, 'TWELVE_DATA_API_KEY is not configured on Render')
    key = endpoint + repr(sorted(params.items()))
    cached = CACHE.get(key)
    if cached and time.time() - cached[0] < ttl: return cached[1]
    try:
        r = requests.get(BASE + endpoint, params={**params, 'apikey': API_KEY}, timeout=18)
        data = r.json()
    except Exception:
        raise HTTPException(502, 'Market-data provider is unreachable. Try refresh shortly.')
    if r.status_code >= 400 or not isinstance(data, dict) or data.get('status') == 'error' or ('message' in data and 'values' not in data and 'price' not in data):
        msg = data.get('message', 'Provider rejected the request') if isinstance(data, dict) else 'Invalid provider response'
        raise HTTPException(502, str(msg))
    CACHE[key] = (time.time(), data)
    return data


def get_candles(symbol: str, interval: str, limit: int = 250):
    if interval not in TF: raise HTTPException(400, 'Unsupported timeframe')
    data = provider('/time_series', {'symbol':symbol, 'interval':interval, 'outputsize':limit, 'timezone':'UTC'})
    values = data.get('values') or []
    out = []
    for v in reversed(values):
        try:
            out.append({'time':v['datetime'], 'open':float(v['open']), 'high':float(v['high']), 'low':float(v['low']), 'close':float(v['close'])})
        except (KeyError, ValueError, TypeError): continue
    if not out: raise HTTPException(502, f'No candles returned for {symbol} {interval}')
    return out


def ema(values, period):
    if not values: return None
    alpha = 2/(period+1); result = values[0]
    for x in values[1:]: result = x*alpha + result*(1-alpha)
    return result


def rsi(values, period=14):
    if len(values) <= period: return None
    changes = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [max(0,x) for x in changes[-period:]]; losses = [max(0,-x) for x in changes[-period:]]
    avg_g=sum(gains)/period; avg_l=sum(losses)/period
    return 100 if avg_l == 0 else 100 - 100/(1+avg_g/avg_l)


def atr(cs, period=14):
    if len(cs)<2: return None
    trs=[]
    for i in range(1,len(cs)):
        c=cs[i]; p=cs[i-1]['close']
        trs.append(max(c['high']-c['low'], abs(c['high']-p), abs(c['low']-p)))
    tail=trs[-period:]
    return sum(tail)/len(tail) if tail else None


def pivots(cs, left=2, right=2):
    highs=[]; lows=[]
    for i in range(left, len(cs)-right):
        window=cs[i-left:i+right+1]
        if cs[i]['high'] == max(c['high'] for c in window) and sum(c['high']==cs[i]['high'] for c in window)==1: highs.append((i,cs[i]['high']))
        if cs[i]['low'] == min(c['low'] for c in window) and sum(c['low']==cs[i]['low'] for c in window)==1: lows.append((i,cs[i]['low']))
    return highs,lows


def structure(cs):
    highs,lows=pivots(cs)
    h=highs[-2:]; l=lows[-2:]
    if len(h)<2 or len(l)<2: return 'RANGE', {'highs':[x[1] for x in highs[-4:]],'lows':[x[1] for x in lows[-4:]],'labels':[]}
    hh=h[-1][1]>h[-2][1]; hl=l[-1][1]>l[-2][1]
    lh=h[-1][1]<h[-2][1]; ll=l[-1][1]<l[-2][1]
    label='BULLISH' if hh and hl else 'BEARISH' if lh and ll else 'RANGE'
    labels=[]
    for idx,val in highs[-4:]: labels.append({'index':idx,'price':val,'label':'HH' if len(highs)>1 and val>highs[-2][1] else 'LH'})
    for idx,val in lows[-4:]: labels.append({'index':idx,'price':val,'label':'HL' if len(lows)>1 and val>lows[-2][1] else 'LL'})
    return label, {'highs':[x[1] for x in highs[-4:]],'lows':[x[1] for x in lows[-4:]],'labels':labels}


def zones_from_candles(cs, timeframe, category='SD'):
    zones=[]
    if len(cs)<30: return zones
    vol=atr(cs)
    if not vol: return zones
    # Base candle followed by a meaningful displacement. Zone is the base body/wick range.
    for i in range(3,len(cs)-4):
        base=cs[i]; nxt=cs[i+1:i+4]
        rise=max(c['close'] for c in nxt)-base['low']; fall=base['high']-min(c['close'] for c in nxt)
        move=max(rise,fall)
        if move < vol*1.65: continue
        bullish=rise>fall
        kind='DEMAND' if bullish else 'SUPPLY'
        if bullish: lo=min(base['open'],base['close']); hi=max(base['open'],base['close'])
        else: lo=min(base['open'],base['close']); hi=max(base['open'],base['close'])
        if hi-lo < vol*.12: lo=base['low']; hi=base['high']
        # POI is the same impulse origin but kept as a distinct overlay category.
        z={'low':lo,'high':hi,'type':kind,'tf':timeframe,'poi':category=='POI','tested':any((c['low']<=hi and c['high']>=lo) for c in cs[i+4:]),'start_time':base['time'],'pattern':'IMPULSE BASE'}
        if not any(q['type']==kind and abs(q['low']-lo)<vol*.3 for q in zones): zones.append(z)
    return zones[-12:]


def double_patterns(cs, timeframe):
    h,l=pivots(cs); tol=(atr(cs) or 0)*0.22; found=[]
    for points,typ in ((h,'DOUBLE TOP'),(l,'DOUBLE BOTTOM')):
        if len(points)<2: continue
        # Pair the last few pivots, not just consecutive tiny swings.
        for a,b in zip(points[-5:-1],points[-4:]):
            if abs(a[1]-b[1])<=tol and tol>0:
                p=(a[1]+b[1])/2; found.append({'low':p-tol/2,'high':p+tol/2,'type':'SUPPLY' if typ=='DOUBLE TOP' else 'DEMAND','tf':timeframe,'poi':False,'tested':False,'start_time':cs[a[0]]['time'],'pattern':typ})
    return found[-4:]


def candle_engulf(cs):
    if len(cs)<2:return None
    p,c=cs[-2],cs[-1]
    bullish=c['close']>c['open'] and p['close']<p['open'] and c['open']<=p['close'] and c['close']>=p['open']
    bearish=c['close']<c['open'] and p['close']>p['open'] and c['open']>=p['close'] and c['close']<=p['open']
    body=abs(c['close']-c['open']); span=c['high']-c['low']
    if bullish and span>0 and body/span>=.62:return 'BULLISH ENGULFING'
    if bearish and span>0 and body/span>=.62:return 'BEARISH ENGULFING'
    return None


def monthly_body_levels(symbol):
    data=provider('/time_series',{'symbol':symbol,'interval':'1month','outputsize':14,'timezone':'UTC'})
    vals=data.get('values') or []
    # API is newest-first; skip current month (index 0), use completed bars only.
    done=vals[1:13]
    levels=[]
    if done:
        o=float(done[0]['open']); c=float(done[0]['close']); levels.extend([{'label':'1M BODY TOP','price':max(o,c),'period':'1M'},{'label':'1M BODY BOTTOM','price':min(o,c),'period':'1M'}])
    for label,group in [('3M',done[:3]),('1Y',done[:12])]:
        if not group:continue
        bodytops=[max(float(x['open']),float(x['close'])) for x in group]
        bodybottoms=[min(float(x['open']),float(x['close'])) for x in group]
        levels.extend([{'label':f'{label} BODY TOP','price':max(bodytops),'period':label},{'label':f'{label} BODY BOTTOM','price':min(bodybottoms),'period':label}])
    return levels

@app.get('/')
def home():
    file=ROOT/'index.html'
    if not file.exists():raise HTTPException(404,'Dashboard file not found')
    return FileResponse(file)
@app.get('/api/health')
def health():return {'project':'APEX Market Intelligence','status':'online','version':APP_VERSION,'provider':'Twelve Data','key_configured':bool(API_KEY)}
@app.get('/api/market/symbols')
def symbols():return {'symbols':SYMBOLS}
@app.get('/api/market/timeframes')
def timeframes():return {'timeframes':sorted(TF)}
@app.get('/api/market/price')
def live_price(symbol:str=Query('XAU/USD')):
    data=provider('/price',{'symbol':symbol})
    try:return {'symbol':symbol,'price':float(data['price']),'timestamp':datetime.now(timezone.utc).isoformat()}
    except (KeyError,TypeError,ValueError):raise HTTPException(502,'Price unavailable')
@app.get('/api/market/candles')
def candle_route(symbol:str=Query('XAU/USD'),interval:str=Query('5min'),outputsize:int=Query(250,ge=10,le=500)):
    return {'symbol':symbol,'interval':interval,'candles':get_candles(symbol,interval,outputsize)}
@app.get('/api/analysis')
def analyze(symbol:str=Query('XAU/USD'),interval:str=Query('5min')):
    if interval not in TF:raise HTTPException(400,'Unsupported timeframe')
    chart=get_candles(symbol,interval,250); closes=[c['close'] for c in chart]; price=closes[-1]; a=atr(chart); e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200); rr=rsi(closes)
    st,sw=structure(chart); trend='BULLISH' if price>e20>e50 else 'BEARISH' if price<e20<e50 else 'MIXED'
    htf={}; allzones=[]; liq=[]
    tfdata=[('1day','1day'),('4h','4h'),('1h','1h'),('15min','15min')]
    for interval2,tag in tfdata:
        try:
            c=get_candles(symbol,interval2,180); label,swings=structure(c); htf[tag]=label
            if tag in ('1day','4h'): allzones+=zones_from_candles(c,tag,'SD')+double_patterns(c,tag)
            if tag in ('1h','15min'): allzones+=zones_from_candles(c,tag,'SD')+double_patterns(c,tag)
            # POI overlays across all requested TFs, separate from S/D.
            allzones+=zones_from_candles(c,tag,'POI')
            ph,pl=pivots(c)
            for _,v in ph[-3:]:liq.append({'price':v,'label':f'{tag} liquidity high'})
            for _,v in pl[-3:]:liq.append({'price':v,'label':f'{tag} liquidity low'})
        except HTTPException: htf[tag]='UNAVAILABLE'
    try: keys=monthly_body_levels(symbol)
    except HTTPException: keys=[]
    demand=[z for z in allzones if z['type']=='DEMAND' and not z['poi']]; supply=[z for z in allzones if z['type']=='SUPPLY' and not z['poi']]
    poi=[z for z in allzones if z['poi']]
    near_d=any(z['low']<=price<=z['high'] for z in demand); near_s=any(z['low']<=price<=z['high'] for z in supply)
    near_p=any(z['low']<=price<=z['high'] for z in poi)
    confirm15=None; confirm1h=None
    try:confirm15=candle_engulf(get_candles(symbol,'15min',40))
    except HTTPException:pass
    try:confirm1h=candle_engulf(get_candles(symbol,'1h',40))
    except HTTPException:pass
    bullhtf=htf.get('1day')=='BULLISH' and htf.get('4h')=='BULLISH'; bearhtf=htf.get('1day')=='BEARISH' and htf.get('4h')=='BEARISH'
    bias='WAIT'; score=25; reasons=['No entry until price reaches a valid zone and all confirmation conditions align.']
    if trend=='BULLISH':score+=10
    elif trend=='BEARISH':score+=10
    if near_d and bullhtf and confirm15=='BULLISH ENGULFING' and confirm1h=='BULLISH ENGULFING' and trend=='BULLISH':bias='BUY';score=80;reasons=['Daily and 4H structure bullish','Price is inside a demand zone / POI','1H and 15M bullish engulfing confirmation']
    elif near_s and bearhtf and confirm15=='BEARISH ENGULFING' and confirm1h=='BEARISH ENGULFING' and trend=='BEARISH':bias='SELL';score=80;reasons=['Daily and 4H structure bearish','Price is inside a supply zone / POI','1H and 15M bearish engulfing confirmation']
    else:
        if not bullhtf and not bearhtf:reasons.append('Daily / 4H structures are not aligned')
        if not (near_d or near_s or near_p):reasons.append('Price is not currently inside an identified zone')
        if not confirm15:reasons.append('15M strong engulfing confirmation is missing')
        if not confirm1h:reasons.append('1H strong engulfing confirmation is missing')
    # Entry plan stays blank while WAIT. Conservative ATR-based levels once conditions pass.
    plan={'entry':None,'entry_zone':None,'stop_loss':None,'take_profit':{'TP1':None,'TP2':None,'TP3':None},'risk_reward':None}
    if bias in ('BUY','SELL') and a:
        direction=1 if bias=='BUY' else -1; entry=price; sl=entry-direction*a*1.25
        plan={'entry':entry,'entry_zone':{'low':entry-a*.12,'high':entry+a*.12},'stop_loss':sl,'take_profit':{'TP1':entry+direction*a,'TP2':entry+direction*a*2,'TP3':entry+direction*a*3},'risk_reward':2.4}
    support=min([z['low'] for z in demand if z['low']<price],default=None); resistance=min([z['high'] for z in supply if z['high']>price],default=None)
    return {'engine':'APEX Market Intelligence','symbol':symbol,'timeframe':interval,'timestamp':datetime.now(timezone.utc).isoformat(),'market':{'price':price,'trend':trend,'structure':st,'htf_structure':htf,'swing_points':sw},'decision':{'bias':bias,'confidence_score':min(score,100),'reasons':reasons,'confirmations':{'1h':confirm1h,'15m':confirm15}},'indicators':{'ema20':e20,'ema50':e50,'ema200':e200,'rsi14':rr,'atr14':a},'levels':{'support':support,'resistance':resistance,'key_levels':keys},'zones':allzones,'liquidity':liq,'trade_plan':plan,'disclaimer':'Technical analysis is not a probability or guarantee. Not financial advice.'}
