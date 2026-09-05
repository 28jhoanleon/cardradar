#!/usr/bin/env python3
"""
cards_app.py - Dashboard de CardRadar. Muestra los proximos partidos de
Liga Profesional con la probabilidad estimada de "menos de X tarjetas"
por equipo. Usa la misma base y logica de extraccion de cards_data.py.

CORRE EN DOS LADOS SIN CAMBIAR NADA:

  LOCAL (Termux):
    python cards_app.py
    -> abrí http://localhost:8080

  RAILWAY:
    - Subí este archivo + cards_data.py al repo.
    - Procfile:  web: python cards_app.py
    - Montá un Volume y anda con la env var RAILWAY_VOLUME_MOUNT_PATH
      (Railway la pone sola si le asignaste un volumen al servicio) asi
      la base sqlite no se borra en cada deploy.

Antes del primer deploy, corré UNA vez el backfill historico (pesado,
no lo dejes corriendo en background en Railway):
    python cards_app.py --backfill
En Railway podes hacerlo con:  railway run python cards_app.py --backfill
"""
import contextlib
import math
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cards_data import (api, dig, LIGAS, cx, db_init, update_fixtures,
                         process_matches, _normalizar, actualizar_arbitros,
                         actualizar_historial_arbitros, diagnosticar_nota_vieja,
                         evaluar_predicciones_pendientes)

try:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, HTMLResponse
    from starlette.routing import Route
except ImportError:
    sys.exit("Falta starlette.  Corre:  pip install starlette uvicorn")


# ======================= MODELO POISSON (sin scipy) =====================

def poisson_cdf(k, lam):
    """P(X <= k) para una Poisson(lam)."""
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


# Curada a mano, no calculada. Solo los clasicos "grandes" de equipos
# que estan en la Liga Profesional esta temporada, para no arriesgar
# con rivalidades dudosas o equipos que ya no estan en primera.
CLASICOS = [
    frozenset({"boca juniors", "river plate"}),
    frozenset({"racing club", "independiente"}),
    frozenset({"rosario central", "newells old boys"}),
    frozenset({"estudiantes", "gimnasia lp"}),
    frozenset({"talleres", "belgrano"}),
    frozenset({"independiente rivadavia", "gimnasia mendoza"}),
]


def es_clasico(team, rival):
    par = frozenset({_normalizar(team), _normalizar(rival)})
    return par in CLASICOS


def arbitro_promedio(arbitro):
    """Promedio de tarjetas TOTALES del partido (ambos equipos) en los
    partidos historicos que dirigio este arbitro, y cuantos son."""
    if not arbitro:
        return None, 0
    with cx() as c:
        rows = c.execute(
            "SELECT total_cards_partido FROM arbitro_historial "
            "WHERE arbitro=?", (arbitro,)).fetchall()
    if not rows:
        return None, 0
    vals = [r["total_cards_partido"] for r in rows]
    return sum(vals) / len(vals), len(vals)


def team_lambda(team, rival=None, es_local=None, arbitro=None, n_recientes=15):
    """Promedio de tarjetas propias del equipo, con mas peso a lo
    reciente y 'shrinkage' hacia la media de la liga si hay poca
    muestra. Ajusta ademas por (cada uno con su peso maximo, para que
    ninguno domine el numero final):
      - local/visitante: si hay 3+ partidos en el mismo contexto,
        se mezcla con el promedio general propio (peso maximo 45%).
      - rival: que tan 'duro' es el rival de turno (peso maximo 35%).
      - cabeza a cabeza (H2H): historial puntual contra este rival, si
        hay 2+ antecedentes (peso maximo 30%).
      - arbitro: promedio de tarjetas de los partidos que dirigio,
        contra la media de la liga, si hay 3+ antecedentes de ese
        arbitro (peso maximo 30%).
    Compara nombres normalizados (sin tildes) para que no se parta la
    muestra si FotMob guardo el mismo equipo con variantes de acento."""
    obj_team = _normalizar(team)
    with cx() as c:
        todas = c.execute(
            "SELECT team, opponent, is_home, total_cards, date FROM team_cards "
            "ORDER BY date DESC").fetchall()

    propias = [r for r in todas if _normalizar(r["team"]) == obj_team]
    rows = propias[:n_recientes]
    media_liga = liga_media()
    if not rows:
        lam_propio, n = media_liga, 0
    else:
        pesos = [0.9 ** i for i in range(len(rows))]
        vals = [r["total_cards"] for r in rows]
        prom_pond = sum(p * v for p, v in zip(pesos, vals)) / sum(pesos)
        n = len(rows)
        w_confianza = min(1.0, n / 8.0)
        lam_propio = w_confianza * prom_pond + (1 - w_confianza) * media_liga

    lam = lam_propio
    info = {"ajuste_rival_pct": None, "h2h": None,
            "ajuste_local_pct": None, "ajuste_arbitro_pct": None,
            "es_clasico": False}

    if es_local is not None:
        contexto = [r for r in propias
                    if bool(r["is_home"]) == bool(es_local)][:n_recientes]
        if len(contexto) >= 3:
            pesos_c = [0.9 ** i for i in range(len(contexto))]
            vals_c = [r["total_cards"] for r in contexto]
            prom_ctx = sum(p * v for p, v in zip(pesos_c, vals_c)) / sum(pesos_c)
            w_ctx = min(0.45, len(contexto) / 12.0)
            lam_ajustado = lam * (1 - w_ctx) + prom_ctx * w_ctx
            info["ajuste_local_pct"] = round((lam_ajustado / lam - 1) * 100, 1) if lam else 0
            lam = lam_ajustado

    if rival:
        obj_rival = _normalizar(rival)
        riv_rows = [r for r in todas
                    if _normalizar(r["opponent"]) == obj_rival][:20]
        if riv_rows and media_liga > 0:
            dureza = sum(r["total_cards"] for r in riv_rows) / len(riv_rows)
            factor = max(0.6, min(1.6, dureza / media_liga))
            w_riv = min(1.0, len(riv_rows) / 10.0) * 0.35
            lam_ajustado = lam * (1 + w_riv * (factor - 1))
            info["ajuste_rival_pct"] = round((lam_ajustado / lam - 1) * 100, 1) if lam else 0
            lam = lam_ajustado

        h2h_rows = [r for r in propias
                    if _normalizar(r["opponent"]) == obj_rival][:10]
        if len(h2h_rows) >= 2:
            h2h_prom = sum(r["total_cards"] for r in h2h_rows) / len(h2h_rows)
            w_h2h = min(1.0, len(h2h_rows) / 4.0) * 0.30
            lam = lam * (1 - w_h2h) + h2h_prom * w_h2h
            info["h2h"] = {"partidos": len(h2h_rows), "promedio": round(h2h_prom, 2)}

    if arbitro:
        arb_prom, n_arb = arbitro_promedio(arbitro)
        if arb_prom and n_arb >= 3 and media_liga > 0:
            media_liga_partido = media_liga * 2  # aprox: partido = 2 equipos
            factor_arb = max(0.7, min(1.4, arb_prom / media_liga_partido))
            w_arb = min(1.0, n_arb / 8.0) * 0.30
            lam_ajustado = lam * (1 + w_arb * (factor_arb - 1))
            info["ajuste_arbitro_pct"] = round((lam_ajustado / lam - 1) * 100, 1) if lam else 0
            lam = lam_ajustado

    if rival and es_clasico(team, rival):
        lam = lam * 1.12
        info["es_clasico"] = True

    return lam, n, info


LINEAS = [4, 5, 6, 7, 8]


def probas_equipo(team, rival=None, es_local=None, arbitro=None):
    lam, n, info = team_lambda(team, rival=rival, es_local=es_local, arbitro=arbitro)
    return {
        "equipo": team, "lambda": round(lam, 2), "muestra": n,
        "confiable": n >= 6,
        "ajuste_rival_pct": info["ajuste_rival_pct"],
        "ajuste_local_pct": info["ajuste_local_pct"],
        "ajuste_arbitro_pct": info["ajuste_arbitro_pct"],
        "es_clasico": info["es_clasico"],
        "h2h": info["h2h"],
        "under": {str(x): round(poisson_cdf(x - 1, lam) * 100, 1)
                  for x in LINEAS},
    }


# ======================= PROXIMOS PARTIDOS ================================

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
                    "home_id": dig(m, "home", "id"),
                    "away_id": dig(m, "away", "id"),
                    "liga": LIGAS[lid],
                })
    return out


def mejor_pick_partido(home_est, away_est, combinado):
    """Replica la logica de 'Recomendado' del frontend, en Python, para
    poder guardar una foto de la recomendacion en el momento y despues
    medir si acerto o no. Prioriza el total del partido (linea 6-9 con
    65%+ de confianza), si no hay ninguna cae a la mejor pata por
    equipo (lineas 4/5/6)."""
    for x in (6, 7, 8, 9):
        val = combinado["under"].get(str(x))
        if val is not None and val >= 65:
            return {"tipo": "total", "equipo": "", "umbral": x, "probabilidad": val}
    candidatas = []
    for t in (home_est, away_est):
        for x in ("5", "6", "4"):
            if x in t["under"]:
                candidatas.append((t["confiable"], t["under"][x], t["equipo"], int(x)))
    if not candidatas:
        return None
    candidatas.sort(key=lambda c: (c[0], c[1]), reverse=True)
    conf, prob, equipo, umbral = candidatas[0]
    return {"tipo": "equipo", "equipo": equipo, "umbral": umbral, "probabilidad": prob}


def guardar_prediccion(match_id, home, away, fecha, pick):
    if not pick or not match_id:
        return
    with cx() as c:
        c.execute(
            "INSERT OR IGNORE INTO predicciones "
            "(match_id, tipo, equipo, texto, umbral, probabilidad, home, away, "
            "fecha, guardado, evaluado) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (match_id, pick["tipo"], pick["equipo"],
             f"{pick['equipo'] or 'Total del partido'} - Menos de {pick['umbral']}",
             pick["umbral"], pick["probabilidad"], home, away, fecha,
             datetime.now(timezone.utc).isoformat()))


def api_proximos():
    with cx() as c:
        arbitros_map = {(_normalizar(r["home"]), _normalizar(r["away"])): r["arbitro"]
                         for r in c.execute("SELECT home, away, arbitro FROM arbitros_proximos")}
    out = []
    for p in proximos_partidos():
        arbitro = arbitros_map.get((_normalizar(p["home"]), _normalizar(p["away"])))
        home_est = probas_equipo(p["home"], rival=p["away"], es_local=True, arbitro=arbitro)
        away_est = probas_equipo(p["away"], rival=p["home"], es_local=False, arbitro=arbitro)
        home_est["id"] = p.get("home_id")
        away_est["id"] = p.get("away_id")
        lam_total = home_est["lambda"] + away_est["lambda"]
        # Lineas mas altas para el total del partido, tiene sentido que
        # sea la suma de las de cada equipo.
        LINEAS_TOTAL = [6, 7, 8, 9, 10, 11]
        combinado = {
            "lambda": round(lam_total, 2),
            "under": {str(x): round(poisson_cdf(x - 1, lam_total) * 100, 1)
                      for x in LINEAS_TOTAL},
        }
        pick = mejor_pick_partido(home_est, away_est, combinado)
        try:
            guardar_prediccion(p.get("match_id"), p["home"], p["away"], p["fecha"], pick)
        except Exception as e:
            print(f"  [predicciones] no pude guardar: {e}")
        out.append({**p, "home_est": home_est, "away_est": away_est,
                    "combinado": combinado, "arbitro": arbitro})
    return out


# ======================= ACTUALIZADOR EN BACKGROUND ========================
# Liviano a proposito: solo mira los ultimos 5 dias y procesa de a poco,
# una vez cada 6hs. Pensado para no gastar de mas en el plan hobby de
# Railway. El backfill grande de 180 dias se hace UNA vez a mano.

def loop_actualizacion():
    db_init()
    with cx() as c:
        n = c.execute("SELECT COUNT(*) FROM team_cards").fetchone()[0]
    if n < 100:
        print(f"[actualizador] base con {n} filas, muy poco. "
              f"Corriendo backfill completo UNA vez (puede tardar ~20 min)...")
        try:
            update_fixtures(dias=365, quiet=True)
            process_matches(limite=99999)
            actualizar_arbitros()
            print("[actualizador] backfill inicial terminado.")
        except Exception as e:
            print(f"[actualizador] fallo el backfill inicial: {e}")
    while True:
        try:
            update_fixtures(dias=5, quiet=True)
            process_matches(limite=40)
            actualizar_arbitros()
        except Exception as e:
            print(f"[actualizador] error: {e}")
        time.sleep(6 * 3600)


# ======================= HTML ==============================================

HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CardRadar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0b0c0e; --card:#151619; --card2:#1c1e22; --line:#2a2c31;
  --text:#f0f0ee; --mut:#8b8d94; --em:#4ade80; --warn:#f0b429; --red:#f0725e;
  --accent:#f0b429;
  --f-body:'IBM Plex Sans',sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:var(--f-body);
  padding:16px 14px 200px}
h1{font-family:var(--f-body);font-size:22px;margin:0 0 4px;font-weight:700}
.hero{border-radius:14px;padding:20px 16px;margin-bottom:16px;
  background:linear-gradient(135deg, rgba(240,180,41,.10) 0%, var(--card) 55%);
  border-left:3px solid var(--accent)}
.hero .sub{color:var(--mut)}
.sub{color:var(--mut);font-size:13px;margin-bottom:6px}
.match{background:var(--card);border:1px solid var(--line);
  border-radius:12px;margin-bottom:10px;overflow:hidden}
.mhead{padding:12px 14px;cursor:pointer;display:flex;flex-direction:column;gap:4px}
.mhead:active{background:var(--card2)}
.mteams{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.mteams .tt{font-weight:600;font-size:14.5px}
.mmeta{color:var(--mut);font-size:11px}
.mbest{display:flex;align-items:center;justify-content:space-between;
  gap:8px;margin-top:2px}
.mbest-txt{font-size:12.5px;color:var(--text)}
.chevron{color:var(--mut);font-size:11px;transition:transform .15s}
.mdetail{padding:0 14px 14px;border-top:1px solid var(--line)}
.meta{color:var(--mut);font-size:12px;margin:12px 0 10px}
.teams{display:flex;gap:10px}
.team{flex:1;background:var(--card2);border-radius:10px;padding:10px;min-width:0}
.total{background:var(--card2);border:1px solid var(--line);
  border-radius:10px;padding:10px;margin-top:8px}
.total-title{color:var(--accent);font-size:12px;font-weight:600;margin-bottom:6px}
.total-row{display:flex;justify-content:space-between;align-items:center;
  font-size:12.5px;padding:3px 0}
.tname{font-weight:600;font-size:14px;margin-bottom:2px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tsamp{color:var(--mut);font-size:10.5px;margin-bottom:8px}
.lam{color:var(--accent);font-size:11px;margin-bottom:8px}
.nota{color:var(--mut);font-size:10px;margin-bottom:4px;font-style:italic}
.row{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;
  padding:4px 0;border-top:1px solid var(--line);cursor:pointer}
.row:first-of-type{border-top:none}
.row.added,.total-row.added{background:rgba(74,222,128,.1);border-radius:6px;
  padding-left:4px;padding-right:4px}
.pctwrap{display:flex;align-items:center;gap:6px}
.chip{width:9px;height:13px;border-radius:2px;flex-shrink:0}
.chip.hi{background:var(--em)}
.chip.mid{background:var(--warn)}
.chip.lo{background:var(--red)}
.pct{font-weight:700;font-size:13px}
.hi{color:var(--em)}.mid{color:var(--warn)}.lo{color:var(--red)}
.badge{font-size:10px;padding:2px 7px;border-radius:20px;
  background:rgba(255,255,255,.06);color:var(--mut);white-space:nowrap}
.badge.ok{background:rgba(74,222,128,.15);color:var(--em)}
.empty{color:var(--mut);text-align:center;padding:40px 10px;font-size:14px;
  line-height:1.5}
.loading{color:var(--mut);text-align:center;padding:30px}
#combi-bar{display:none;position:fixed;left:0;right:0;bottom:0;
  background:var(--card);border-top:1px solid var(--accent);padding:12px 14px;
  max-height:55vh;overflow-y:auto;z-index:50;
  box-shadow:0 -6px 20px rgba(0,0,0,.4)}
#combi-cuota{width:100%;background:var(--card2);border:1px solid var(--line);
  border-radius:8px;padding:8px;color:var(--text);font-size:13px;margin-top:8px}
</style></head>
<body>
<div class="hero">
<h1>&#128203; CardRadar</h1>
<div class="sub">Proximos partidos - Liga Profesional - prob. de "menos de X tarjetas"</div>
<div class="sub" style="margin-bottom:0">Tocá un partido para ver el detalle. Tocá un porcentaje para sumarlo a tu combinada.</div>
<button onclick="sugerirCombinada()" style="margin-top:12px;background:var(--accent);
  color:#171208;border:none;border-radius:20px;padding:10px 18px;
  font-family:var(--f-body);font-weight:700;font-size:13px;cursor:pointer">
  &#10024; Sugerime una combinada
</button>
<div id="rendimiento" style="margin-top:10px;font-size:11.5px;color:var(--mut)"></div>
</div>
<div id="app" class="loading">Cargando proximos partidos...</div>

<div id="combi-bar">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <strong>Mi combinada (<span id="combi-count">0</span> patas)</strong>
    <span onclick="vaciarCombinada()" style="color:var(--mut);font-size:11px;cursor:pointer;text-decoration:underline">vaciar</span>
  </div>
  <div id="combi-list"></div>
  <input id="combi-cuota" type="number" step="0.01" placeholder="cuota total que te da bet365 (opcional)" oninput="calcularEdge()">
  <div id="combi-edge"></div>
</div>

<script>
function clase(p){ return p>=70?'hi':(p>=50?'mid':'lo'); }
let combinada = [];
let datosGlobales = [];

function esc(s){ return String(s).replace(/'/g, "\\'"); }

function agregarPata(texto, prob, el){
  if (combinada.find(c=>c.texto===texto)) return;
  combinada.push({texto, prob});
  if(el) el.classList.add('added');
  renderCombinada();
}
function quitarPata(idx){
  combinada.splice(idx,1);
  renderCombinada();
}
function vaciarCombinada(){
  combinada = [];
  document.querySelectorAll('.added').forEach(e=>e.classList.remove('added'));
  renderCombinada();
}
function probTotalCombinada(){
  return combinada.reduce((acc,c)=>acc*(c.prob/100), 1) * 100;
}
function calcularEdge(){
  const inputEl = document.getElementById('combi-cuota');
  const out = document.getElementById('combi-edge');
  if(!inputEl || !out) return;
  const cuota = parseFloat(inputEl.value) || null;
  if(!cuota || !combinada.length){ out.innerHTML=''; return; }
  const probTotal = probTotalCombinada();
  const impli = 100/cuota;
  const edge = probTotal - impli;
  out.innerHTML = `<div class="total-row"><span>Prob. implicita de esa cuota</span><span>${impli.toFixed(1)}%</span></div>
    <div class="total-row"><span>Diferencia (edge)</span><span class="pct ${edge>=0?'hi':'lo'}">${edge>=0?'+':''}${edge.toFixed(1)}%</span></div>`;
}
function mejorApuesta(p){
  // Prioriza el TOTAL del partido (lo que mas se apuesta en la
  // practica), buscando la linea mas baja que aun asi tenga
  // confianza razonable (mejor cuota, sigue siendo probable).
  const lineasTotal = ['6','7','8','9'];
  for(const x of lineasTotal){
    if(p.combinado.under[x] !== undefined && p.combinado.under[x] >= 65){
      return {
        texto: `Menos de ${x} tarjetas en total`,
        prob: p.combinado.under[x],
        clase: clase(p.combinado.under[x]),
      };
    }
  }
  // Fallback: si el total no tiene ninguna linea confiable, usamos la
  // mejor pata por equipo.
  const candidatas = [];
  [p.home_est, p.away_est].forEach(t=>{
    ['5','6','4'].forEach(x=>{
      if(t.under && t.under[x] !== undefined){
        candidatas.push({texto: `${t.equipo} - Menos de ${x}`,
                          prob: t.under[x], confiable: t.confiable});
      }
    });
  });
  candidatas.sort((a,b)=> (b.confiable - a.confiable) || (b.prob - a.prob));
  const m = candidatas[0] || {texto: 'sin datos', prob: 0};
  return {texto: m.texto, prob: m.prob, clase: clase(m.prob)};
}

function toggleMatch(i){
  const det = document.getElementById('det-'+i);
  const chev = document.getElementById('chev-'+i);
  const abierto = det.style.display !== 'none';
  det.style.display = abierto ? 'none' : 'block';
  chev.style.transform = abierto ? '' : 'rotate(180deg)';
}

function sugerirCombinada(){
  if(!datosGlobales.length){
    alert('Todavia no cargaron los partidos, esperá un segundo y probá de nuevo.');
    return;
  }
  vaciarCombinada();
  const candidatas = [];
  datosGlobales.forEach(p=>{
    // Una pata por partido, sobre el TOTAL combinado (lo que mas se
    // apuesta en la practica). Buscamos la linea mas baja que siga
    // siendo confiable, asi la cuota vale la pena.
    const lineas = ['6','7','8','9'];
    let elegida = null;
    for(const x of lineas){
      if(p.combinado.under[x] !== undefined && p.combinado.under[x] >= 60){
        elegida = {texto: `${p.home} vs ${p.away} - Menos de ${x} en total`,
                   prob: p.combinado.under[x],
                   confiable: p.home_est.confiable && p.away_est.confiable};
        break;
      }
    }
    if(elegida) candidatas.push(elegida);
  });
  // primero los partidos con muestra confiable en ambos equipos,
  // despues por probabilidad
  candidatas.sort((a,b)=> (b.confiable - a.confiable) || (b.prob - a.prob));
  let agregadas = 0;
  for(const c of candidatas){
    combinada.push({texto: c.texto, prob: c.prob});
    agregadas++;
    if(agregadas >= 6) break;
  }
  if(!agregadas){
    alert('No encontre suficientes partidos con muestra confiable todavia '+
          'para armar una sugerencia. Segui cargando historial.');
    return;
  }
  renderCombinada();
  document.getElementById('combi-bar').scrollIntoView({behavior:'smooth', block:'end'});
}

function renderCombinada(){
  const bar = document.getElementById('combi-bar');
  const count = document.getElementById('combi-count');
  const list = document.getElementById('combi-list');
  count.textContent = combinada.length;
  bar.style.display = combinada.length ? 'block' : 'none';
  if(!combinada.length){ list.innerHTML=''; return; }
  const probTotal = probTotalCombinada();
  list.innerHTML = combinada.map((c,i)=>`
    <div class="total-row"><span>${c.texto} (${c.prob}%)</span>
      <span onclick="quitarPata(${i})" style="color:var(--red);cursor:pointer">quitar</span></div>
  `).join('') + `
    <div class="total-row" style="border-top:1px solid var(--line);margin-top:6px;padding-top:8px">
      <span>Probabilidad combinada (asume partidos independientes)</span>
      <span class="pct ${clase(probTotal)}">${probTotal.toFixed(1)}%</span>
    </div>`;
  calcularEdge();
}

const PALETA_EQUIPOS = ['#f0725e','#f0b429','#4ade80','#38bdf8','#a78bfa',
                         '#f472b6','#fb923c','#2dd4bf'];
function avatar(nombre, id){
  let hash = 0;
  for(let i=0;i<nombre.length;i++) hash = (hash*31 + nombre.charCodeAt(i)) >>> 0;
  const color = PALETA_EQUIPOS[hash % PALETA_EQUIPOS.length];
  const iniciales = nombre.split(' ').filter(w=>w.length>2 || w===nombre.split(' ')[0])
    .slice(0,2).map(w=>w[0]).join('').toUpperCase().slice(0,2) || nombre.slice(0,2).toUpperCase();
  const circulo = `<span class="av-fallback" style="display:inline-flex;align-items:center;justify-content:center;
    width:22px;height:22px;border-radius:50%;background:${color}22;
    color:${color};font-size:10px;font-weight:700;flex-shrink:0;
    border:1px solid ${color}55">${iniciales}</span>`;
  if(!id) return circulo;
  return `<span style="display:inline-flex;width:22px;height:22px;flex-shrink:0;position:relative">
    <img src="https://images.fotmob.com/image_resources/logo/teamlogo/${id}.png"
      alt="" style="width:100%;height:100%;object-fit:contain"
      onerror="this.style.display='none';this.nextElementSibling.style.display='inline-flex'">
    <span class="av-fallback" style="display:none;align-items:center;justify-content:center;
      width:22px;height:22px;border-radius:50%;background:${color}22;
      color:${color};font-size:10px;font-weight:700;flex-shrink:0;
      border:1px solid ${color}55">${iniciales}</span>
  </span>`;
}
function tarjetaEquipo(t){
  const u = t.under;
  let notas = '';
  if(t.ajuste_local_pct !== null && t.ajuste_local_pct !== undefined && Math.abs(t.ajuste_local_pct) >= 1){
    const signo = t.ajuste_local_pct >= 0 ? '+' : '';
    notas += `<div class="nota">ajustado por local/visitante: ${signo}${t.ajuste_local_pct}%</div>`;
  }
  if(t.ajuste_rival_pct !== null && t.ajuste_rival_pct !== undefined && Math.abs(t.ajuste_rival_pct) >= 1){
    const signo = t.ajuste_rival_pct >= 0 ? '+' : '';
    notas += `<div class="nota">ajustado por rival: ${signo}${t.ajuste_rival_pct}%</div>`;
  }
  if(t.ajuste_arbitro_pct !== null && t.ajuste_arbitro_pct !== undefined && Math.abs(t.ajuste_arbitro_pct) >= 1){
    const signo = t.ajuste_arbitro_pct >= 0 ? '+' : '';
    notas += `<div class="nota">ajustado por arbitro: ${signo}${t.ajuste_arbitro_pct}%</div>`;
  }
  if(t.es_clasico){
    notas += `<div class="nota">&#9889; clasico: +12% (ajuste fijo, no calculado)</div>`;
  }
  if(t.h2h){
    notas += `<div class="nota">h2h vs este rival: ${t.h2h.partidos} partidos, prom. ${t.h2h.promedio}</div>`;
  }
  return `<div class="team">
    <div class="tname" style="display:flex;align-items:center;gap:6px">
      ${avatar(t.equipo, t.id)}<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.equipo}</span>
    </div>
    <div class="tsamp">${t.muestra} partidos en base${t.confiable?'':' - muestra chica'}</div>
    <div class="lam">prom. esperado: ${t.lambda} tarjetas</div>
    ${notas}
    ${Object.keys(u).map(x=>`<div class="row" onclick="agregarPata('${esc(t.equipo)} - Menos de ${x}', ${u[x]}, this)">
        <span>Menos de ${x}</span>
        <span class="pctwrap"><span class="chip ${clase(u[x])}"></span><span class="pct ${clase(u[x])}">${u[x]}%</span></span>
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
    datosGlobales = data;
    el.innerHTML = data.map((p,i)=>{
      const mejor = mejorApuesta(p);
      return `
      <div class="match">
        <div class="mhead" onclick="toggleMatch(${i})">
          <div class="mteams">
            <span class="tt" style="display:flex;align-items:center;gap:6px">
              ${avatar(p.home, p.home_id)}${p.home} <span style="color:var(--mut);font-weight:400">vs</span> ${p.away}${avatar(p.away, p.away_id)}
            </span>
            <span class="chevron" id="chev-${i}">&#9662;</span>
          </div>
          <div class="mmeta">${p.fecha}${p.arbitro ? ' &middot; \u26AB ' + p.arbitro : ''}</div>
          <div class="mbest">
            <span class="mbest-txt">Pick recomendado: ${mejor.texto}</span>
            <span class="pctwrap"><span class="chip ${mejor.clase}"></span><span class="pct ${mejor.clase}">${mejor.prob}%</span></span>
          </div>
        </div>
        <div class="mdetail" id="det-${i}" style="display:none">
          <div class="meta"><span class="badge ${p.home_est.confiable&&p.away_est.confiable?'ok':''}">
            ${p.home_est.confiable&&p.away_est.confiable?'muestra ok':'revisar muestra'}
          </span></div>
          <div class="teams">
            ${tarjetaEquipo(p.home_est)}
            ${tarjetaEquipo(p.away_est)}
          </div>
          <div class="total">
            <div class="total-title">Total del partido (ambos equipos combinados) - prom. ${p.combinado.lambda}</div>
            ${Object.keys(p.combinado.under).map(x=>`<div class="total-row" onclick="agregarPata('Total ${esc(p.home)} vs ${esc(p.away)} - Menos de ${x}', ${p.combinado.under[x]}, this)">
                <span>Menos de ${x} tarjetas en total</span>
                <span class="pctwrap"><span class="chip ${clase(p.combinado.under[x])}"></span><span class="pct ${clase(p.combinado.under[x])}">${p.combinado.under[x]}%</span></span>
              </div>`).join('')}
          </div>
        </div>
      </div>`;
    }).join('');
  }catch(e){
    el.innerHTML = '<div class="empty">Error cargando: '+e+'</div>';
  }
}
async function cargarRendimiento(){
  const el = document.getElementById('rendimiento');
  try{
    const r = await fetch('/api/rendimiento');
    const d = await r.json();
    if(!d.evaluadas){
      el.textContent = 'Todavia no hay picks evaluados (se van sumando a medida que terminan los partidos).';
      return;
    }
    el.innerHTML = `&#128202; Rendimiento historico: <b style="color:var(--text)">${d.porcentaje}%</b> `+
      `de acierto en ${d.evaluadas} picks evaluados`+
      (d.pendientes ? ` (${d.pendientes} mas esperando a que se jueguen)` : '');
  }catch(e){}
}
cargar();
cargarRendimiento();
</script>
<details style="margin-top:24px;color:var(--mut)">
  <summary style="cursor:pointer;font-size:12px">&#9881;&#65039; Admin (actualizar datos manualmente)</summary>
  <div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">
    <div style="font-size:11px;line-height:1.5">
      Estos botones piden tu clave de admin (la que pusiste en Railway como
      <code>ADMIN_TOKEN</code>) y disparan la tarea en el servidor, en segundo
      plano. Mirá los logs de Railway para ver el progreso.
    </div>
    <button onclick="adminRun('todo')" style="padding:8px 12px;border-radius:8px;
      border:1px solid var(--line);background:var(--card2);color:var(--text);
      font-size:12px;text-align:left;cursor:pointer">
      <b>Actualizar todo</b> - backfill completo + arbitros + historial.
      Usalo despues de un cambio grande, o si ves la base vacia. Tarda ~20 min.
    </button>
    <button onclick="adminRun('arbitros')" style="padding:8px 12px;border-radius:8px;
      border:1px solid var(--line);background:var(--card2);color:var(--text);
      font-size:12px;text-align:left;cursor:pointer">
      <b>Solo arbitros de la proxima fecha</b> - rapido. Usalo cada vez que
      la LPF publique una nota nueva de designaciones (una vez por semana).
    </button>
    <button onclick="adminRun('historial-arbitros')" style="padding:8px 12px;border-radius:8px;
      border:1px solid var(--line);background:var(--card2);color:var(--text);
      font-size:12px;text-align:left;cursor:pointer">
      <b>Historial de arbitros</b> - baja notas viejas para ir armando el
      promedio de tarjetas por arbitro. Usalo cada tanto para sumar mas datos.
    </button>
    <button onclick="adminRun('diagnostico-arbitros')" style="padding:8px 12px;border-radius:8px;
      border:1px solid var(--line);background:var(--card2);color:var(--text);
      font-size:12px;text-align:left;cursor:pointer">
      <b>Diagnostico de arbitros</b> - solo para depurar, muestra el texto
      crudo de una nota vieja. Usalo si "Historial de arbitros" da 0.
    </button>
    <div id="admin-resultado" style="font-size:11px;margin-top:4px"></div>
  </div>
</details>
<script>
let adminPolling = null;
async function adminRun(tarea){
  let token = localStorage.getItem('cardradar_admin_token');
  if(!token){
    token = prompt('Clave de admin (ADMIN_TOKEN de Railway). Se guarda en este navegador, no la vuelvo a pedir:');
    if(!token) return;
    localStorage.setItem('cardradar_admin_token', token);
  }
  const out = document.getElementById('admin-resultado');
  out.textContent = 'Enviando...';
  try{
    const r = await fetch(`/admin/run?tarea=${tarea}&token=${encodeURIComponent(token)}`);
    const data = await r.json();
    if(r.status === 403){
      localStorage.removeItem('cardradar_admin_token');
      out.textContent = 'Clave incorrecta, se borro. Volve a tocar el boton para escribirla de nuevo.';
      return;
    }
    if(!r.ok){ out.textContent = 'Error: ' + data.error; return; }
    if(adminPolling) clearInterval(adminPolling);
    adminPoll(token);
    adminPolling = setInterval(()=>adminPoll(token), 2000);
  }catch(e){
    out.textContent = 'Error de red: ' + e;
  }
}
async function adminPoll(token){
  const out = document.getElementById('admin-resultado');
  try{
    const r = await fetch(`/admin/estado?token=${encodeURIComponent(token)}`);
    const d = await r.json();
    const badge = d.estado==='corriendo' ? '&#128993; Corriendo...'
                : d.estado==='listo' ? '&#128994; Listo'
                : d.estado==='error' ? '&#128308; Error'
                : d.estado;
    out.innerHTML = `<div style="margin-bottom:6px"><b>${badge}</b>${d.tarea ? ' ('+d.tarea+')' : ''}</div>
      <pre id="admin-log" style="max-height:220px;overflow:auto;background:var(--card2);
        border:1px solid var(--line);border-radius:8px;padding:8px;font-size:10.5px;
        white-space:pre-wrap;user-select:text;margin:0">${(d.log||[]).join('\\n') || '(sin salida todavia)'}</pre>
      <button onclick="adminCopiarLog()" style="margin-top:6px;padding:6px 10px;
        border-radius:6px;border:1px solid var(--line);background:var(--card);
        color:var(--text);font-size:11px;cursor:pointer">Copiar log</button>`;
    if(d.estado !== 'corriendo' && adminPolling){
      clearInterval(adminPolling);
      adminPolling = null;
    }
  }catch(e){
    out.textContent = 'Error consultando estado: ' + e;
  }
}
function adminCopiarLog(){
  const el = document.getElementById('admin-log');
  if(!el) return;
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(el.textContent).then(
      ()=>alert('Log copiado'),
      ()=>alert('No se pudo copiar solo, seleccionalo a mano'));
  }else{
    alert('Tu navegador no soporta copiar automatico, seleccionalo a mano');
  }
}
</script>
</body></html>"""


# ======================= ROUTES =============================================

async def home(request):
    return HTMLResponse(HTML)


async def r_proximos(request):
    return JSONResponse(api_proximos())


async def r_stats(request):
    with cx() as c:
        n_m = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        n_tc = c.execute("SELECT COUNT(*) FROM team_cards").fetchone()[0]
        n_arb = c.execute("SELECT COUNT(*) FROM arbitro_historial").fetchone()[0]
    return JSONResponse({"partidos": n_m, "filas_tarjetas": n_tc,
                         "partidos_con_arbitro_historico": n_arb})


async def r_rendimiento(request):
    with cx() as c:
        fila = c.execute(
            "SELECT COUNT(*) AS total, SUM(acierto) AS aciertos "
            "FROM predicciones WHERE evaluado=1").fetchone()
        pendientes = c.execute(
            "SELECT COUNT(*) FROM predicciones WHERE evaluado=0").fetchone()[0]
    total = fila["total"] or 0
    aciertos = fila["aciertos"] or 0
    pct = round(aciertos / total * 100, 1) if total else None
    return JSONResponse({"evaluadas": total, "aciertos": aciertos,
                         "porcentaje": pct, "pendientes": pendientes})


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

ESTADO_ADMIN = {"tarea": None, "estado": "inactivo", "inicio": None,
                "fin": None, "log": [], "error": None}
_estado_lock = threading.Lock()
_stdout_real = sys.stdout


class _TeeLog:
    """Escribe a la salida real (para que siga apareciendo en los logs
    de Railway) y ademas guarda las lineas en ESTADO_ADMIN para poder
    consultarlas desde el navegador."""
    def write(self, s):
        _stdout_real.write(s)
        if s.strip():
            with _estado_lock:
                for linea in s.splitlines():
                    if linea.strip():
                        ESTADO_ADMIN["log"].append(linea)
                ESTADO_ADMIN["log"] = ESTADO_ADMIN["log"][-300:]
        return len(s)

    def flush(self):
        _stdout_real.flush()


async def r_admin(request):
    token = request.query_params.get("token")
    tarea = request.query_params.get("tarea", "")
    if not ADMIN_TOKEN:
        return JSONResponse(
            {"error": "Falta configurar la variable ADMIN_TOKEN en Railway "
                      "para poder usar este endpoint."}, status_code=403)
    if token != ADMIN_TOKEN:
        return JSONResponse({"error": "no autorizado"}, status_code=403)
    with _estado_lock:
        if ESTADO_ADMIN["estado"] == "corriendo":
            return JSONResponse(
                {"error": f"ya hay una tarea corriendo ({ESTADO_ADMIN['tarea']}), "
                          f"esperala a que termine"}, status_code=409)

    def hacer_todo():
        update_fixtures(dias=365, quiet=True)
        process_matches(limite=99999)
        actualizar_arbitros()
        actualizar_historial_arbitros(max_notas=10)

    tareas = {
        "fixtures": lambda: update_fixtures(dias=365, quiet=True),
        "procesar": lambda: process_matches(limite=99999),
        "arbitros": actualizar_arbitros,
        "historial-arbitros": lambda: actualizar_historial_arbitros(max_notas=10),
        "diagnostico-arbitros": diagnosticar_nota_vieja,
        "todo": hacer_todo,
    }
    fn = tareas.get(tarea)
    if not fn:
        return JSONResponse(
            {"error": f"tarea desconocida. Usa una de: {list(tareas.keys())}"},
            status_code=400)

    def correr():
        with _estado_lock:
            ESTADO_ADMIN.update({"tarea": tarea, "estado": "corriendo",
                                 "inicio": datetime.now(timezone.utc).isoformat(),
                                 "fin": None, "log": [], "error": None})
        tee = _TeeLog()
        try:
            with contextlib.redirect_stdout(tee):
                fn()
            with _estado_lock:
                ESTADO_ADMIN["estado"] = "listo"
        except Exception as e:
            with _estado_lock:
                ESTADO_ADMIN["estado"] = "error"
                ESTADO_ADMIN["error"] = str(e)
                ESTADO_ADMIN["log"].append("ERROR: " + str(e))
                ESTADO_ADMIN["log"].append(traceback.format_exc())
        finally:
            with _estado_lock:
                ESTADO_ADMIN["fin"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=correr, daemon=True).start()
    return JSONResponse({"status": "corriendo en background", "tarea": tarea})


async def r_admin_estado(request):
    token = request.query_params.get("token")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return JSONResponse({"error": "no autorizado"}, status_code=403)
    with _estado_lock:
        return JSONResponse(dict(ESTADO_ADMIN))




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
        Route("/api/rendimiento", r_rendimiento),
        Route("/admin/run", r_admin),
        Route("/admin/estado", r_admin_estado),
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
        update_fixtures(dias=365)
        process_matches(limite=99999)
    else:
        main()
