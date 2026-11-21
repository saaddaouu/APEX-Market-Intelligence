import os, requests
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

API_KEY=os.getenv('TWELVE_DATA_API_KEY'); BASE='https://api.twelvedata.com'; ROOT=Path(__file__).resolve().parent
app=FastAPI(title='APEX Market Intelligence',version='3.0.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

@app.get('/',include_in_schema=False)
def home():
    f=ROOT/'index.html'
    if not f.exists(): raise HTTPException(404,'index.html missing')
    return FileResponse(f)
@app.get('/api/health')
def health(): return {'status':'online','version':'3.0.0','timestamp':datetime.now(timezone.utc).isoformat()}

def td(endpoint,params):
    if not API_KEY: raise HTTPException(500,'Twelve Data API key not configured on Render')
    try:
        r=requests.get(BASE+'/'+endpoint,params={**params,'apikey':API_KEY},timeout=25); d=r.json()
    except requests.RequestException as e: raise HTTPException(502,'Market data request failed')
    except ValueError: raise HTTPException(502,'Invalid market-data response')
    if not isinstance(d,dict) or r.status_code>=400 or d.get('status')=='error':
        raise HTTPException(502,detail={'provider':'Twelve Data','message':d.get('message','Provider error') if isinstance(d,dict) else 'Unexpected response','code':d.get('code') if isinstance(d,dict) else None})
    return d

def candles(symbol,interval,count=500):
    data=td('time_series',{'symbol':symbol,'interval':interval,'outputsize':count,'timezone':'UTC'})
    vals=data.get('values') or []
    out=[]
    for c in reversed(vals):
        try: out.append({'time':c['datetime'],'open':float(c['open']),'high':float(c['high']),'low':float(c['low']),'close':float(c['close'])})
        except (ValueError,KeyError,TypeError): continue
    return out
@app.get('/api/market/price')
def price(symbol:str=Query('XAU/USD')):
    d=td('price',{'symbol':symbol})
    try: return {'symbol':symbol,'price':float(d['price']),'timestamp':datetime.now(timezone.utc).isoformat()}
    except (ValueError,KeyError,TypeError): raise HTTPException(502,'Bad price response')
@app.get('/api/market/candles')
def candle_api(symbol:str=Query('XAU/USD'),interval:str=Query('5min'),outputsize:int=Query(300,ge=1,le=5000)):
    valid=['1min','5min','15min','30min','45min','1h','2h','4h','1day','1week','1month']
    if interval not in valid: raise HTTPException(400,'Unsupported timeframe')
    cs=candles(symbol,interval,outputsize)
    if not cs: raise HTTPException(502,'No candles returned')
    return {'symbol':symbol,'interval':interval,'count':len(cs),'candles':cs}

# Core technical calculations
def ema(vals,n):
    if len(vals)<n:return None
    v=sum(vals[:n])/n; k=2/(n+1)
    for x in vals[n:]:v=x*k+v*(1-k)
    return v
def rsi(vals,n=14):
    if len(vals)<=n:return None
    dif=[vals[i]-vals[i-1] for i in range(1,len(vals))]; up=[max(x,0) for x in dif]; dn=[max(-x,0) for x in dif]
    ag=sum(up[:n])/n; al=sum(dn[:n])/n
    for i in range(n,len(up)): ag=(ag*(n-1)+up[i])/n; al=(al*(n-1)+dn[i])/n
    return 100 if al==0 and ag>0 else (50 if al==0 else 100-100/(1+ag/al))
def atr(cs,n=14):
    if len(cs)<n+1:return None
    trs=[]
    for i in range(1,len(cs)):
        c,p=cs[i],cs[i-1]['close']; trs.append(max(c['high']-c['low'],abs(c['high']-p),abs(c['low']-p)))
    a=sum(trs[:n])/n
    for x in trs[n:]:a=(a*(n-1)+x)/n
    return a
def pivots(cs,k=3):
    hi=[];lo=[]
    for i in range(k,len(cs)-k):
        h=cs[i]['high']; l=cs[i]['low']
        if all(h>cs[j]['high'] for j in range(i-k,i)) and all(h>=cs[j]['high'] for j in range(i+1,i+k+1)):hi.append({'i':i,'price':h,'time':cs[i]['time']})
        if all(l<cs[j]['low'] for j in range(i-k,i)) and all(l<=cs[j]['low'] for j in range(i+1,i+k+1)):lo.append({'i':i,'price':l,'time':cs[i]['time']})
    return hi,lo
def structure(cs):
    hi,lo=pivots(cs,3)
    if len(hi)<2 or len(lo)<2:return {'bias':'RANGE','label':'Insufficient swings','swings_high':hi[-2:],'swings_low':lo[-2:]}
    h_up=hi[-1]['price']>hi[-2]['price']; l_up=lo[-1]['price']>lo[-2]['price']
    h_dn=hi[-1]['price']<hi[-2]['price']; l_dn=lo[-1]['price']<lo[-2]['price']
    bias='BULLISH' if h_up and l_up else 'BEARISH' if h_dn and l_dn else 'RANGE'
    label='HH + HL' if bias=='BULLISH' else 'LH + LL' if bias=='BEARISH' else 'Mixed structure'
    return {'bias':bias,'label':label,'swings_high':hi[-4:],'swings_low':lo[-4:]}
def engulfing(cs):
    # User rule: confirmation is a long-bodied directional candle, not necessarily
    # a textbook engulfing pattern. Require body >= 0.70 ATR and >= 60% of range.
    if len(cs)<20:return 'NONE'
    b=cs[-1]; av=atr(cs)
    if not av or av<=0:return 'NONE'
    body=abs(b['close']-b['open']); span=max(b['high']-b['low'],1e-12)
    if body<av*0.70 or body/span<0.60:return 'NONE'
    return 'BULLISH' if b['close']>b['open'] else 'BEARISH' if b['close']<b['open'] else 'NONE'
def zone_merge(zones,atrv):
    if not zones:return []
    merged=[]
    for z in sorted(zones,key=lambda x:x['low']):
        match=next((m for m in merged if z['low']<=m['high']+(atrv or 0)*.15 and z['high']>=m['low']-(atrv or 0)*.15 and m['type']==z['type']),None)
        if match:match['low']=min(match['low'],z['low']);match['high']=max(match['high'],z['high']);match['hits']+=z.get('hits',1);match['sources']=list(set(match['sources']+z['sources']))
        else:merged.append(z.copy())
    return sorted(merged,key=lambda z:z.get('hits',1),reverse=True)[:8]
def zones_for(cs,tf):
    if len(cs)<20:return {'supply':[],'demand':[],'poi':[]}
    av=atr(cs) or 0; hi,lo=pivots(cs,2 if tf in ('15min','1h') else 3); supply=[];demand=[];poi=[]
    # displacement-origin candle zones from recent pivots with subsequent impulsive movement
    for p in hi[-18:]:
        i=p['i']; nxt=cs[i+1:min(i+5,len(cs))]
        if nxt and max(c['close'] for c in nxt)<p['price']-av*.7:
            c=cs[i]; supply.append({'low':min(c['open'],c['close']),'high':max(c['open'],c['close']) if c['open']!=c['close'] else c['high'],'type':'SUPPLY','time':c['time'],'hits':1,'sources':['Swing rejection']})
    for p in lo[-18:]:
        i=p['i']; nxt=cs[i+1:min(i+5,len(cs))]
        if nxt and max(c['close'] for c in nxt)>p['price']+av*.7:
            c=cs[i]; demand.append({'low':min(c['open'],c['close']) if c['open']!=c['close'] else c['low'],'high':max(c['open'],c['close']),'type':'DEMAND','time':c['time'],'hits':1,'sources':['Swing rejection']})
    # approximate double tops/bottoms with ATR tolerance; merge zones for repeated swing prices
    for group,typ in ((hi,'SUPPLY'),(lo,'DEMAND')):
        for i in range(1,len(group)):
            a,b=group[i-1],group[i]
            if abs(a['price']-b['price'])<=max(av*.25,1e-8):
                p=(a['price']+b['price'])/2; width=max(av*.15,abs(p)*.00015)
                z={'low':p-width,'high':p+width,'type':typ,'time':b['time'],'hits':2,'sources':['Double top' if typ=='SUPPLY' else 'Double bottom']}
                (supply if typ=='SUPPLY' else demand).append(z)
    supply=zone_merge(supply,av); demand=zone_merge(demand,av)
    poi=zone_merge([dict(z,sources=['POI: displacement origin']) for z in supply+demand],av)
    return {'supply':supply,'demand':demand,'poi':poi}
def body_levels(symbol):
    # monthly series used to build completed previous-month, previous-3-month and previous-year bodies
    monthly=candles(symbol,'1month',30)
    if len(monthly)<2:return []
    prev=monthly[-2]
    levels=[{'label':'Previous Month Body','top':max(prev['open'],prev['close']),'bottom':min(prev['open'],prev['close']),'period':'1M'}]
    if len(monthly)>=4:
        block=monthly[-4:-1]; first=block[0]; last=block[-1]
        levels.append({'label':'Previous 3 Months Body','top':max(max(c['open'],c['close']) for c in block),'bottom':min(min(c['open'],c['close']) for c in block),'period':'3M'})
    if len(monthly)>=13:
        block=monthly[-13:-1]; first=block[0]; last=block[-1]
        levels.append({'label':'Previous 1 Year Body','top':max(max(c['open'],c['close']) for c in block),'bottom':min(min(c['open'],c['close']) for c in block),'period':'1Y'})
    return levels
@app.get('/api/analysis')
def analysis(symbol:str=Query('XAU/USD'),interval:str=Query('5min')):
    allowed=['1min','5min','15min','30min','1h','2h','4h','1day']
    if interval not in allowed:raise HTTPException(400,'Unsupported timeframe')
    # Each analysis uses timeframe-specific zones and higher-level context.
    tf_cs=candles(symbol,interval,400); d_cs=candles(symbol,'1day',250); h4_cs=candles(symbol,'4h',300); h1_cs=candles(symbol,'1h',300); m15_cs=candles(symbol,'15min',300)
    if len(tf_cs)<50 or len(d_cs)<30:raise HTTPException(502,'Insufficient candles to analyze')
    price=tf_cs[-1]['close']; vals=[c['close'] for c in tf_cs]; av=atr(tf_cs); st=structure(tf_cs); d_st=structure(d_cs); h4_st=structure(h4_cs); h1_st=structure(h1_cs); m15_st=structure(m15_cs)
    # zones mapped for swing/intraday and scalp
    daily4={'daily':zones_for(d_cs,'1day'),'4h':zones_for(h4_cs,'4h')}; scalp={'1h':zones_for(h1_cs,'1h'),'15m':zones_for(m15_cs,'15min')}
    key=body_levels(symbol)
    def price_in(zs):return any(z['low']<=price<=z['high'] for z in zs)
    higher_zones=daily4['daily']['supply']+daily4['daily']['demand']+daily4['4h']['supply']+daily4['4h']['demand']
    scalp_zones=scalp['1h']['supply']+scalp['1h']['demand']+scalp['15m']['supply']+scalp['15m']['demand']
    zone_touch=price_in(higher_zones); scalp_touch=price_in(scalp_zones)
    conf1=engulfing(h1_cs); conf15=engulfing(m15_cs)
    sentiment='BULLISH' if d_st['bias']=='BULLISH' and h4_st['bias']=='BULLISH' else 'BEARISH' if d_st['bias']=='BEARISH' and h4_st['bias']=='BEARISH' else 'MIXED'
    # High TF context is directional only when both daily and 4h structure align.
    # Intraday/swing requires 1h and 15m engulfing in same direction, at a mapped zone, with aligned higher TF.
    intraday_dir=conf1 if conf1==conf15 and conf1 in ('BULLISH','BEARISH') and sentiment==conf1 and zone_touch else 'WAIT'
    scalp_dir=conf1 if conf1==conf15 and conf1 in ('BULLISH','BEARISH') and price_in(scalp_zones) else 'WAIT'
    # Retest is not presumed: signal stays pending until price revisits the signal candle body zone.
    def retest_plan(direction,signal_candles):
        if direction not in ('BULLISH','BEARISH') or not signal_candles:return {'status':'WAIT','entry':None,'entry_zone':None,'stop_loss':None,'targets':[],'reason':'No confirmed multi-timeframe engulfing at qualifying zone'}
        c=signal_candles[-1]; lo=min(c['open'],c['close']); hi=max(c['open'],c['close']); risk=max(av or 0,hi-lo)
        if risk<=0:return {'status':'WAIT','reason':'Risk range unavailable'}
        if direction=='BULLISH': sl=lo-risk*.25; ent=[lo,hi]; t=[hi+r*risk for r in (1,2,3)]
        else: sl=hi+risk*.25; ent=[lo,hi]; t=[lo-r*risk for r in (1,2,3)]
        return {'status':'CONFIRMATION_PENDING_RETEST','entry':None,'entry_zone':{'low':round(ent[0],6),'high':round(ent[1],6)},'stop_loss':round(sl,6),'targets':[round(x,6) for x in t],'reason':'Engulfing confirmation formed; wait for price to retest entry zone before entry'}
    swing_plan=retest_plan(intraday_dir,h1_cs if intraday_dir!='WAIT' else [])
    scalp_plan=retest_plan(scalp_dir,m15_cs if scalp_dir!='WAIT' else [])
    return {'engine':'APEX Final Rules Engine','symbol':symbol,'timeframe':interval,'timestamp':datetime.now(timezone.utc).isoformat(),'price':price,'market_structure':{'selected':st,'daily':d_st,'4h':h4_st,'1h':h1_st,'15m':m15_st,'sentiment':sentiment},'key_levels':key,'zones':{'intraday_swing':daily4,'scalp':scalp},'poi':{'intraday_swing':{'daily':daily4['daily']['poi'],'4h':daily4['4h']['poi']},'scalp':{'1h':scalp['1h']['poi'],'15m':scalp['15m']['poi']}},'confirmation':{'1h_strong_candle':conf1,'15m_strong_candle':conf15,'higher_timeframe_alignment':sentiment,'intraday_swing':intraday_dir,'scalp':scalp_dir,'daily_4h_zone_touch':zone_touch,'1h_15m_zone_touch':scalp_touch},'trade_plan':{'intraday_swing':swing_plan,'scalp':scalp_plan},'disclaimer':'Technical rules-based analysis, not a guarantee or financial advice.'}
