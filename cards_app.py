#!/usr/bin/env python3
"""
cards_app.py - Dashboard de CardRadar. Muestra los proximos partidos de
Liga Profesional con la probabilidad estimada de "menos de X tarjetas"
por equipo. Usa la misma base y logica de extraccion de cards_data.py.
"""
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cards_data import (api, dig, LIGAS, cx, db_init, update_fixtures,
                         process_matches)

try:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, HTMLResponse
    from starlette.routing import Route
except ImportError:
    sys.exit("Falta starlette.  Corre:  pip install starlette uvicorn")


def poisson_cdf(k, lam):
    if lam <= 0:
        return 1.0 if k >= 0 else 0.0
    if k < 0:
        return 0.0
    s = term = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        s += term
    return min(s, 1.0)


def liga_media():
    with cx() as c:
        r = c.execute("SELECT AVG(total_cards) FROM team_cards").fetchone()
    return r[0] or 4.5


def team_lambda(team, n_recientes=12):
    with cx() as c:
        rows = c.execute(
            "SELECT total_cards FROM team_cards WHERE team=? "
            "ORDER BY date DESC LIMIT ?", (team, n_recientes)).fetchall()
    media_liga = liga_media()
    if not rows:
        return media_liga, 0
    pesos = [0.9 ** i for i in range(len(rows))]
    vals = [r["total_cards"] for r in rows]
    prom_pond = sum(p * v for p, v in zip(pesos, vals)) / sum(pesos)
    n = len(rows)
    w_confianza = min(1.0, n / 8.0)
    lam = w_confianza * prom_pond + (1 - w_confianza) * media_liga
    return lam, n


LINEAS = [4, 5, 6, 7, 8]


def probas_equipo(team):
    lam, n = team_lambda(team)
    return {
        "equipo": team, "lambda": round(lam, 2), "muestra": n,
        "confiable": n >= 6,
        "under": {str(x): round(poisson_cdf(x - 1, lam) * 100, 1)
                  for x in LINEAS},
    }


def proximos_partidos(dias=7):
    out = []
    hoy = datetime.now(timezone.utc).date()
    for d in range(dias):
        day = hoy + timedelta(days=d)
        try:
            data = api("matches", date=day.strftime("%Y%m%d"))
        except Exception:
            continue
        for lg in data.get("leagues", []):
            lid = lg.get("primaryId") or lg.get("id")
            if lid not in LIGAS:
                continue
            for m in lg.get("matches", []):
                if dig(m, "status", "finished", default=False):
                    continue
                if dig(m, "status", "cancelled", default=False):
                    continue
                out.append({
                    "match_id": m.get("id"),
                    "fecha": day.isoformat(),
                    "home": dig(m, "home", "name"),
                    "away": dig(m, "away", "name"),
                    "liga": LIGAS[lid],
                })
    return out


def api_proximos():
    out = []
    for p in proximos_partidos():
        out.append({
            **p,
            "home_est": probas_equipo(p["home"]),
            "away_est": probas_equipo(p["away"]),
        })
    return out


def loop_actualizacion():
    db_init()
    while True:
        try:
            update_fixtures(dias=5, quiet=True)
            process_matches(limite=40)
        except Exception as e:
            print(f"[actualizador] error: {e}")
        time.sleep(6 * 3600)


HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CardRadar</title>
<style>
:root{
  --bg:#0d1117; --card:#161b22; --card2:#1c2430; --line:#262f3d;
  --text:#e6edf3; --mut:#8b96a5; --em:#3fb950; --warn:#e3b341; --red:#f85149;
  --accent:#58a6ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  padding:16px 14px 40px}
h1{font-size:20px;margin:4px 0 2px;font-weight:700}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.match{background:var(--card);border:1px solid var(--line);
  border-radius:14px;padding:14px;margin-bottom:12px}
.meta{color:var(--mut);font-size:11.5px;text-transform:uppercase;
  letter-spacing:.04em;margin-bottom:10px;display:flex;justify-content:space-between;
  gap:8px;flex-wrap:wrap}
.teams{display:flex;gap:10px}
.team{flex:1;background:var(--card2);border-radius:10px;padding:10px;min-width:0}
.tname{font-weight:600;font-size:14px;margin-bottom:2px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tsamp{color:var(--mut);font-size:10.5px;margin-bottom:8px}
.lam{color:var(--accent);font-size:11px;margin-bottom:8px}
.row{display:flex;justify-content:space-between;font-size:12px;
  padding:3px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:none}
.pct{font-weight:700}
.hi{color:var(--em)}.mid{color:var(--warn)}.lo{color:var(--red)}
.badge{font-size:10px;padding:2px 7px;border-radius:20px;
  background:rgba(255,255,255,.06);color:var(--mut);white-space:nowrap}
.badge.ok{background:rgba(63,185,80,.15);color:var(--em)}
.empty{color:var(--mut);text-align:center;padding:40px 10px;font-size:14px;
  line-height:1.5}
.loading{color:var(--mut);text-align:center;padding:30px}
</style></head>
<body>
<h1>&#128203; CardRadar</h1>
<div class="sub">Proximos partidos - Liga Profesional - prob. de "menos de X tarjetas" por equipo</div>
<div id="app" class="loading">Cargando proximos partidos...</div>
<script>
function clase(p){ return p>=70?'hi':(p>=50?'mid':'lo'); }
function tarjetaEquipo(t){
  const u = t.under;
  return `<div class="team">
    <div class="tname">${t.equipo}</div>
    <div class="tsamp">${t.muestra} partidos en base${t.confiable?'':' - muestra chica'}</div>
    <div class="lam">prom. esperado: ${t.lambda} tarjetas</div>
    ${Object.keys(u).map(x=>`<div class="row">
        <span>Menos de ${x}</span>
        <span class="pct ${clase(u[x])}">${u[x]}%</span>
      </div>`).join('')}
  </div>`;
}
async function cargar(){
  const el = document.getElementById('app');
  try{
    const r = await fetch('/api/proximos');
    const data = await r.json();
    if(!data.length){
      el.innerHTML = '<div class="empty">No hay partidos proximos cargados '+
        'todavia, o la base esta vacia.<br>Corre <b>python cards_app.py --backfill</b> primero.</div>';
      return;
    }
    el.className='';
    el.innerHTML = data.map(p=>`
      <div class="match">
        <div class="meta"><span>${p.fecha} - ${p.liga}</span>
          <span class="badge ${p.home_est.confiable&&p.away_est.confiable?'ok':''}">
            ${p.home_est.confiable&&p.away_est.confiable?'muestra ok':'revisar muestra'}
          </span></div>
        <div class="teams">
          ${tarjetaEquipo(p.home_est)}
          ${tarjetaEquipo(p.away_est)}
        </div>
      </div>`).join('');
  }catch(e){
    el.innerHTML = '<div class="empty">Error cargando: '+e+'</div>';
  }
}
cargar();
</script>
</body></html>"""


async def home(request):
    return HTMLResponse(HTML)


async def r_proximos(request):
    return JSONResponse(api_proximos())


async def r_stats(request):
    with cx() as c:
        n_m = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        n_tc = c.execute("SELECT COUNT(*) FROM team_cards").fetchone()[0]
    return JSONResponse({"partidos": n_m, "filas_tarjetas": n_tc})


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    db_init()
    threading.Thread(target=loop_actualizacion, daemon=True).start()
    yield


app = Starlette(
    routes=[
        Route("/", home),
        Route("/api/proximos", r_proximos),
        Route("/api/stats", r_stats),
    ],
    lifespan=lifespan,
)


def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"\nCardRadar en http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        db_init()
        update_fixtures(dias=180)
        process_matches(limite=99999)
    else:
        main()
