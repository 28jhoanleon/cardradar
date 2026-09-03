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
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cards_data import (api, dig, LIGAS, cx, db_init, update_fixtures,
                         process_matches, _normalizar, actualizar_arbitros)

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


def team_lambda(team, rival=None, n_recientes=15):
    """Promedio de tarjetas propias del equipo, con mas peso a lo
    reciente y 'shrinkage' hacia la media de la liga si hay poca
    muestra. Si se pasa 'rival', ajusta ademas por:
      - que tan 'duro' es el rival (cuantas tarjetas sacan sus
        oponentes cuando lo enfrentan), con peso limitado a 35%.
      - el historial cabeza a cabeza entre estos dos equipos
        puntuales, con peso limitado a 30% y solo si hay 2+ partidos
        previos entre ellos.
    Compara nombres normalizados (sin tildes) para que no se parta la
    muestra si FotMob guardo el mismo equipo con variantes de acento."""
    obj_team = _normalizar(team)
    with cx() as c:
        todas = c.execute(
            "SELECT team, opponent, total_cards, date FROM team_cards "
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
    info = {"ajuste_rival_pct": None, "h2h": None}

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

    return lam, n, info


LINEAS = [4, 5, 6, 7, 8]


def probas_equipo(team, rival=None):
    lam, n, info = team_lambda(team, rival=rival)
    return {
        "equipo": team, "lambda": round(lam, 2), "muestra": n,
        "confiable": n >= 6,
        "ajuste_rival_pct": info["ajuste_rival_pct"],
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
                    "liga": LIGAS[lid],
                })
    return out


def api_proximos():
    with cx() as c:
        arbitros_map = {(_normalizar(r["home"]), _normalizar(r["away"])): r["arbitro"]
                         for r in c.execute("SELECT home, away, arbitro FROM arbitros_proximos")}
    out = []
    for p in proximos_partidos():
        home_est = probas_equipo(p["home"], rival=p["away"])
        away_est = probas_equipo(p["away"], rival=p["home"])
        lam_total = home_est["lambda"] + away_est["lambda"]
        # Lineas mas altas para el total del partido, tiene sentido que
        # sea la suma de las de cada equipo.
        LINEAS_TOTAL = [6, 7, 8, 9, 10, 11]
        combinado = {
            "lambda": round(lam_total, 2),
            "under": {str(x): round(poisson_cdf(x - 1, lam_total) * 100, 1)
                      for x in LINEAS_TOTAL},
        }
        arbitro = arbitros_map.get((_normalizar(p["home"]), _normalizar(p["away"])))
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
function avatar(nombre){
  let hash = 0;
  for(let i=0;i<nombre.length;i++) hash = (hash*31 + nombre.charCodeAt(i)) >>> 0;
  const color = PALETA_EQUIPOS[hash % PALETA_EQUIPOS.length];
  const iniciales = nombre.split(' ').filter(w=>w.length>2 || w===nombre.split(' ')[0])
    .slice(0,2).map(w=>w[0]).join('').toUpperCase().slice(0,2) || nombre.slice(0,2).toUpperCase();
  return `<span style="display:inline-flex;align-items:center;justify-content:center;
    width:22px;height:22px;border-radius:50%;background:${color}22;
    color:${color};font-size:10px;font-weight:700;flex-shrink:0;
    border:1px solid ${color}55">${iniciales}</span>`;
}
function tarjetaEquipo(t){
  const u = t.under;
  let notas = '';
  if(t.ajuste_rival_pct !== null && t.ajuste_rival_pct !== undefined && Math.abs(t.ajuste_rival_pct) >= 1){
    const signo = t.ajuste_rival_pct >= 0 ? '+' : '';
    notas += `<div class="nota">ajustado por rival: ${signo}${t.ajuste_rival_pct}%</div>`;
  }
  if(t.h2h){
    notas += `<div class="nota">h2h vs este rival: ${t.h2h.partidos} partidos, prom. ${t.h2h.promedio}</div>`;
  }
  return `<div class="team">
    <div class="tname" style="display:flex;align-items:center;gap:6px">
      ${avatar(t.equipo)}<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.equipo}</span>
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
              ${avatar(p.home)}${p.home} <span style="color:var(--mut);font-weight:400">vs</span> ${p.away}${avatar(p.away)}
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
cargar();
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
        update_fixtures(dias=365)
        process_matches(limite=99999)
    else:
        main()
