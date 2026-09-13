import os
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from .analysis import indicators, score_row
load_dotenv()

def _metrics(returns):
    if not returns:
        return {"trades":0,"wins":0,"win_rate":None,"net_return":0,
                "profit_factor":None,"max_drawdown":0,"sharpe":None}
    arr=np.array(returns,dtype=float)
    equity=np.cumprod(1+arr)
    peak=np.maximum.accumulate(equity)
    dd=equity/peak-1
    wins=int((arr>0).sum())
    gains=arr[arr>0].sum()
    losses=-arr[arr<0].sum()
    pf=(gains/losses) if losses>0 else None
    sharpe=(arr.mean()/arr.std()*np.sqrt(len(arr))) if arr.std()>0 else None
    return {
        "trades":len(arr),
        "wins":wins,
        "win_rate":round(wins/len(arr)*100,2),
        "net_return":round((equity[-1]-1)*100,2),
        "profit_factor":round(pf,3) if pf is not None else None,
        "max_drawdown":round(dd.min()*100,2),
        "sharpe":round(float(sharpe),3) if sharpe is not None else None
    }

def run(df, hold_days=None):
    commission=float(os.getenv("BACKTEST_COMMISSION","0.001"))
    slippage=float(os.getenv("BACKTEST_SLIPPAGE","0.0005"))
    hold_days=int(hold_days or os.getenv("BACKTEST_HOLD_DAYS","20"))
    x=indicators(df).dropna().copy()
    returns=[]
    trades=[]
    # Walk-forward: at i, use indicators through i only. Entry is next day's open.
    for i in range(200, len(x)-hold_days-1):
        r=x.iloc[i]
        score,_,_=score_row(r)
        if score < 78:
            continue
        entry=float(x.iloc[i+1].Open)*(1+slippage)
        atr=float(r.ATR)
        stop=max(0, float(r.Close)-1.5*atr)
        risk=max(float(r.Close)-stop,1e-12)
        target=float(r.Close)+2*risk
        exit_price=None
        exit_reason="TIME"
        for j in range(i+1, min(i+1+hold_days,len(x))):
            low=float(x.iloc[j].Low); high=float(x.iloc[j].High)
            if low <= stop:
                exit_price=stop*(1-slippage); exit_reason="STOP"; break
            if high >= target:
                exit_price=target*(1-slippage); exit_reason="TARGET"; break
        if exit_price is None:
            exit_price=float(x.iloc[min(i+hold_days,len(x)-1)].Close)*(1-slippage)
        gross=exit_price/entry-1
        net=gross-2*commission
        returns.append(net)
        trades.append({"entry_date":str(x.index[i+1].date()),
                        "exit_reason":exit_reason,"return":round(net*100,2)})
    m=_metrics(returns)
    m["trades_detail"]=trades[-100:]
    m["commission"]=commission
    m["slippage"]=slippage
    m["hold_days"]=hold_days
    return m
