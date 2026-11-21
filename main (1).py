import os, requests, math
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app=FastAPI(title='APEX Market Intelligence',version='3.0.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
ROOT=Path(__file__).resolve().parent
API_KEY=os.getenv('TWELVE_DATA_API_KEY','')
BASE='https://api.twelvedata.com'
INTERVALS={'1min','5min','15min','30min','1h','4h','1day','1week','1month'}
SYMBOLS=['XAU/USD','EUR/USD','GBP/USD','USD/JPY','USD/CHF','AUD/USD','USD/CAD','NZD/USD']

def td(endpoint,params):
    if not API_KEY: raise HTTPException(503,'TWELVE_DATA_API_KEY is not configured')
    p=dict(params); p['apikey']=API_KEY
    try:
        r=requests.get(BASE+endpoint,params=p,timeout=20); d=r.json()
    except Exception as e: raise HTTPException(502,f'Market data request failed: {e}')
    if r.status_code>=400 or (isinstance(d,dict) and (d.get('status')=='error' or ('message' in d and 'values' not in d and 'price' not in d))):
        raise HTTPException(502,d.get('message','Provider error') if isinstance(d,dict) else 'Provider error')
    return d

def candles(symbol,interval,limit=250):
    if interval not in INTERVALS: raise HTTPException(400,'Unsupported timeframe')
    d=td('/time_series',{'symbol':symbol,'interval':interval,'outputsize':limit,'timezone':'UTC'})
    vals=d.get('values') or []
    out=[]
    for v in reversed(vals):
        try: out.append({'time':v['datetime'],'open':float(v['open']),'high':float(v['high']),'low':float(v['low']),'close':float(v['close'])})
        except (ValueError,KeyError,TypeError): continue
    if not out: raise HTTPException(502,'No candle data returned')
    return out

def ema(vals,period):
    if not vals:return None
    k=2/(period+1); x=vals[0]
    for v in vals[1:]:x=v*k+x*(1-k)
    return x

def rsi(vals,period=14):
    if len(vals)<period+1:return None
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        delta=vals[i]-vals[i-1]; gains.append(max(delta,0)); losses.append(max(-delta,0))
    ag=sum(gains[-period:])/period; al=sum(losses[-period:])/period
    if al==0:return 100.0
    return 100-100/(1+ag/al)

def atr(cs,period=14):
    if len(cs)<2:return None
    trs=[]
    for i in range(1,len(cs)):
        c=cs[i]; prev=cs[i-1]['close']
        trs.append(max(c['high']-c['low'],abs(c['high']-prev),abs(c['low']-prev)))
    return sum(trs[-period:])/min(period,len(trs)) if trs else None

def structure(cs,lookback=120):
    arr=cs[-lookback:]; highs=[]; lows=[]
    for i in range(2,len(arr)-2):
        if arr[i]['high']>max(x['high'] for x in arr[i-2:i]) and arr[i]['high']>=max(x['high'] for x in arr[i+1:i+3]): highs.append((i,arr[i]['high']))
        if arr[i]['low']<min(x['low'] for x in arr[i-2:i]) and arr[i]['low']<=min(x['low'] for x in arr[i+1:i+3]): lows.append((i,arr[i]['low']))
    h=highs[-2:]; l=lows[-2:]
    if len(h)>=2 and len(l)>=2:
        bullish=h[-1][1]>h[-2][1] and l[-1][1]>l[-2][1]
        bearish=h[-1][1]<h[-2][1] and l[-1][1]<l[-2][1]
        label='BULLISH' if bullish else 'BEARISH' if bearish else 'RANGE'
        return label, {'highs':[x[1] for x in h],'lows':[x[1] for x in l]}
    return 'RANGE',{'highs':[x[1] for x in h],'lows':[x[1] for x in l]}

def zones(cs,tf,kind):
    arr=cs[-160:]; out=[]
    if len(arr)<8:return out
    a=atr(arr,14) or 0
    for i in range(2,len(arr)-3):
        base=arr[i]; nxt=arr[i+1:i+4]
        move=max(x['close'] for x in nxt)-min(x['close'] for x in nxt)
        if a and move>=a*1.4:
            direction='DEMAND' if nxt[-1]['close']>base['close'] else 'SUPPLY'
            if kind and direction!=kind:continue
            if direction=='DEMAND': z={'low':base['low'],'high':max(base['open'],base['close']),'type':direction,'tf':tf,'poi':True,'tested':False}
            else:z={'low':min(base['open'],base['close']),'high':base['high'],'type':direction,'tf':tf,'poi':True,'tested':False}
            if not any(abs(z['low']-q['low'])<=a*.25 and z['type']==q['type'] for q in out):out.append(z)
    return out[-8:]

def double_levels(cs,tf):
    arr=cs[-150:]; result=[]; tol=(atr(arr) or 0)*0.18
    if tol<=0:return result
    pivh=[]; pivl=[]
    for i in range(2,len(arr)-2):
        if arr[i]['high']>=max(x['high'] for x in arr[i-2:i+3]):pivh.append(arr[i]['high'])
        if arr[i]['low']<=min(x['low'] for x in arr[i-2:i+3]):pivl.append(arr[i]['low'])
    for vals,typ in [(pivh,'SUPPLY'),(pivl,'DEMAND')]:
        if len(vals)>=2 and abs(vals[-1]-vals[-2])<=tol:
            p=(vals[-1]+vals[-2])/2; result.append({'low':p-tol/2,'high':p+tol/2,'type':typ,'tf':tf,'poi':False,'tested':False,'pattern':'DOUBLE TOP' if typ=='SUPPLY' else 'DOUBLE BOTTOM'})
    return result

@app.get('/')
def home():
    f=ROOT/'index.html'
    if not f.exists():raise HTTPException(404,'index.html missing')
    return FileResponse(f)
@app.get('/api/health')
def health():return {'project':'APEX Market Intelligence','status':'online','version':'3.0.0','provider':'Twelve Data','key_configured':bool(API_KEY)}
@app.get('/api/market/symbols')
def symbols():return {'symbols':SYMBOLS}
@app.get('/api/market/timeframes')
def timeframes():return {'timeframes':sorted(INTERVALS)}
@app.get('/api/market/price')
def price(symbol:str=Query('XAU/USD')):
    d=td('/price',{'symbol':symbol});
    try:return {'symbol':symbol,'price':float(d['price']),'timestamp':datetime.now(timezone.utc).isoformat()}
    except Exception:raise HTTPException(502,'Price unavailable')
@app.get('/api/market/candles')
def candle_endpoint(symbol:str=Query('XAU/USD'),interval:str=Query('5min'),outputsize:int=Query(200,ge=10,le=500)):
    cs=candles(symbol,interval,outputsize);return {'symbol':symbol,'interval':interval,'candles':cs}
@app.get('/api/analysis')
def analysis(symbol:str=Query('XAU/USD'),interval:str=Query('5min')):
    if interval not in INTERVALS:raise HTTPException(400,'Unsupported timeframe')
    chart=candles(symbol,interval,250); closes=[x['close'] for x in chart]; price=closes[-1]
    e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200); r=rsi(closes); a=atr(chart)
    st,sw=structure(chart); trend='BULLISH' if e20 and e50 and price>e20>e50 else 'BEARISH' if e20 and e50 and price<e20<e50 else 'MIXED'
    tfset={'1min':['1h','15min'],'5min':['1h','15min'],'15min':['1h','4h'],'30min':['4h','1day'],'1h':['4h','1day'],'4h':['1day'],'1day':['1week'],'1week':['1month'],'1month':[]}.get(interval,[])
    zoneset=[]; structs={}
    for tf in ['1day','4h','1h','15min']:
        try:
            c=candles(symbol,tf,180); structs[tf]=structure(c)[0]
            z=zones(c,tf,None)+double_levels(c,tf); zoneset.extend(z)
        except HTTPException: continue
    # Key body levels from completed monthly, quarterly-ish (3-month candles), yearly data
    keylevels=[]
    try:
        md=td('/time_series',{'symbol':symbol,'interval':'1month','outputsize':16,'timezone':'UTC'}); monthly=md.get('values') or []
        # Twelve Data returns newest first; skip the still-forming current month.
        completed=monthly[1:]
        groups=[('1M',completed[:1],'1month'),('3M',completed[:3],'3month'),('1Y',completed[:12],'1year')]
        for label,group,tfname in groups:
            if not group: continue
            op=float(group[-1]['open']); cl=float(group[0]['close'])
            keylevels += [{'label':label+' BODY TOP','price':max(op,cl),'tf':tfname},{'label':label+' BODY BOTTOM','price':min(op,cl),'tf':tfname}]
    except Exception: pass
    # HTF alignment check
    htf_states=[]
    for tf in ['1day','4h']:
        htf_states.append(structs.get(tf,'RANGE'))
    htf_bull=all(x=='BULLISH' for x in htf_states); htf_bear=all(x=='BEARISH' for x in htf_states)
    bias='WAIT'; score=40; reasons=[]
    if trend=='BULLISH':reasons.append('Price and EMA alignment bullish')
    elif trend=='BEARISH':reasons.append('Price and EMA alignment bearish')
    if st=='BULLISH':reasons.append('Chart timeframe structure bullish')
    if st=='BEARISH':reasons.append('Chart timeframe structure bearish')
    if r is not None and r>70:reasons.append('RSI overbought; avoid chasing longs')
    elif r is not None and r<30:reasons.append('RSI oversold; avoid chasing shorts')
    # candle confirmation on chart TF (engulfing) plus zone proximity, retest simplistic guard
    prev,cur=chart[-2],chart[-1]
    bull_engulf=cur['close']>cur['open'] and prev['close']<prev['open'] and cur['close']>=prev['open'] and cur['open']<=prev['close']
    bear_engulf=cur['close']<cur['open'] and prev['close']>prev['open'] and cur['open']>=prev['close'] and cur['close']<=prev['open']
    near_d=any(z['type']=='DEMAND' and z['low']<=price<=z['high'] for z in zoneset)
    near_s=any(z['type']=='SUPPLY' and z['low']<=price<=z['high'] for z in zoneset)
    if htf_bull and near_d and bull_engulf and trend=='BULLISH':bias='BUY';score=75;reasons.append('Bullish HTF alignment + demand touch + engulfing confirmation')
    elif htf_bear and near_s and bear_engulf and trend=='BEARISH':bias='SELL';score=75;reasons.append('Bearish HTF alignment + supply touch + engulfing confirmation')
    else:reasons.append('Waiting for HTF alignment, zone interaction and candle confirmation')
    nearest_support=min([z['low'] for z in zoneset if z['type']=='DEMAND' and z['low']<price],default=None)
    nearest_res=max([z['high'] for z in zoneset if z['type']=='SUPPLY' and z['high']>price],default=None)
    plan={'entry':None,'entry_zone':None,'stop_loss':None,'take_profit':{'TP1':None,'TP2':None,'TP3':None},'risk_reward':None}
    if bias in ['BUY','SELL'] and a:
        sl=price-a*1.3; tp1=price+a;tp2=price+a*2;tp3=price+a*3
        if bias=='SELL':sl=price+a*1.3;tp1=price-a;tp2=price-a*2;tp3=price-a*3
        plan={'entry':price,'entry_zone':{'low':price-a*.15,'high':price+a*.15},'stop_loss':sl,'take_profit':{'TP1':tp1,'TP2':tp2,'TP3':tp3},'risk_reward':2.0}
    return {'engine':'APEX Market Intelligence V3','symbol':symbol,'timeframe':interval,'timestamp':datetime.now(timezone.utc).isoformat(),'market':{'price':price,'trend':trend,'structure':st,'htf_structure':structs,'bias':bias},'decision':{'bias':bias,'confidence_score':score,'reasons':reasons},'indicators':{'ema20':e20,'ema50':e50,'ema200':e200,'rsi14':r,'atr14':a},'levels':{'support':nearest_support,'resistance':nearest_res,'key_levels':keylevels},'zones':zoneset,'trade_plan':plan,'disclaimer':'Algorithmic technical analysis is not a probability of success or financial advice.'}
