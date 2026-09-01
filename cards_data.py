#!/usr/bin/env python3
"""
cards_data.py - Recolector de datos de TARJETAS por equipo (Liga Profesional
y las que agregues). Hermano de remates_v10.py, misma infra (FotMob +
Sofascore de respaldo), pero en vez de remates de jugador guarda tarjetas
por equipo por partido + el arbitro.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    sys.exit('Falta httpx.  Corre:  pip install "httpx>=0.27"')


DB_PATH = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.path.dirname(os.path.abspath(__file__)),
    "cards.db")

LIGAS = {
    112: "Argentina - Liga Profesional",
}

BASES = [
    "https://www.fotmob.com/api/data",
    "https://www.fotmob.com/api",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-A536E) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile",
    "Accept": "application/json",
    "Referer": "https://www.fotmob.com/",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS matches(
  match_id INTEGER PRIMARY KEY, league_id INTEGER, league TEXT,
  date TEXT, home TEXT, away TEXT, processed INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS team_cards(
  match_id INTEGER, team TEXT, opponent TEXT, is_home INTEGER,
  league_id INTEGER, date TEXT,
  yellow INTEGER, red INTEGER, second_yellow INTEGER,
  total_cards REAL, referee TEXT, fuente TEXT,
  PRIMARY KEY(match_id, team));
CREATE INDEX IF NOT EXISTS ix_team ON team_cards(team);
CREATE TABLE IF NOT EXISTS cfg(k TEXT PRIMARY KEY, v TEXT);
"""


def cx():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    with cx() as c:
        c.executescript(SCHEMA)


def cfg_get(k, d=None):
    try:
        with cx() as c:
            r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d
    except Exception:
        return d


def cfg_set(k, v):
    with cx() as c:
        c.execute("INSERT OR REPLACE INTO cfg VALUES(?,?)", (k, v))


_client = None


def http():
    global _client
    if _client is None:
        _client = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
    return _client


def base_ok():
    return cfg_get("fotmob_base") or BASES[0]


def api(path, retries=3, **params):
    orden = [base_ok()] + [b for b in BASES if b != base_ok()]
    last = None
    for base in orden:
        for i in range(retries):
            try:
                r = http().get(f"{base}/{path}", params=params)
                r.raise_for_status()
                data = r.json()
                if base != base_ok():
                    cfg_set("fotmob_base", base)
                time.sleep(1.1)
                return data
            except Exception as e:
                last = e
                resp = getattr(e, "response", None)
                code = getattr(resp, "status_code", None)
                if code in (404, 400):
                    break
                if i < retries - 1:
                    time.sleep(2 * (i + 1))
    raise last


def dig(o, *ks, default=None):
    for k in ks:
        if isinstance(o, dict) and k in o:
            o = o[k]
        else:
            return default
    return o


def num(v, d=0.0):
    if v is None:
        return d
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("value", "total", "stat"):
            if k in v:
                return num(v[k], d)
        return d
    try:
        return float(str(v).replace("'", "").split("(")[0].strip())
    except ValueError:
        return d


def _k(x):
    return str(x).lower().replace(" ", "").replace("_", "")


CLAVES_AMARILLA = ("yellowcards", "yellowcard", "cardsyellow")
CLAVES_ROJA = ("redcards", "redcard", "cardsred")
CLAVES_2AMARILLA = ("secondyellowcard", "yellowredcard", "yellowred")


def _buscar_par_stat(nodo, claves, prof=0):
    if prof > 6 or not isinstance(nodo, (dict, list)):
        return None
    if isinstance(nodo, dict):
        for k, v in nodo.items():
            if _k(k) in claves:
                if isinstance(v, list) and len(v) == 2:
                    return num(v[0]), num(v[1])
                if isinstance(v, dict) and "home" in v and "away" in v:
                    return num(v["home"]), num(v["away"])
                if isinstance(v, dict) and "stats" in v:
                    st = v["stats"]
                    if isinstance(st, list) and len(st) == 2:
                        return num(st[0]), num(st[1])
        for v in nodo.values():
            r = _buscar_par_stat(v, claves, prof + 1)
            if r is not None:
                return r
    else:
        for v in nodo[:60]:
            r = _buscar_par_stat(v, claves, prof + 1)
            if r is not None:
                return r
    return None


def _buscar_eventos_tarjetas(data, home_name, away_name):
    amarillas = {"home": 0, "away": 0}
    rojas = {"home": 0, "away": 0}
    segundas = {"home": 0, "away": 0}
    referee = [None]

    def lado_de(ev):
        if "isHome" in ev:
            return "home" if ev["isHome"] else "away"
        tn = ev.get("teamName") or dig(ev, "team", "name")
        if isinstance(tn, str):
            if _k(tn) == _k(home_name):
                return "home"
            if _k(tn) == _k(away_name):
                return "away"
        return None

    def walk(n, prof=0):
        if prof > 10:
            return
        if isinstance(n, dict):
            if referee[0] is None:
                for rk in ("referee", "refereeName"):
                    rv = n.get(rk)
                    if isinstance(rv, str) and rv.strip():
                        referee[0] = rv.strip()
                    elif isinstance(rv, dict):
                        nm = rv.get("name") or rv.get("fullName")
                        if isinstance(nm, str) and nm.strip():
                            referee[0] = nm.strip()
            tipo = ""
            for tk in ("type", "cardType", "eventType"):
                tv = n.get(tk)
                if isinstance(tv, str):
                    tipo = _k(tv)
                    break
            if tipo:
                lado = lado_de(n)
                if lado:
                    if "red" in tipo and "yellow" in tipo:
                        segundas[lado] += 1
                        rojas[lado] += 1
                    elif "red" in tipo:
                        rojas[lado] += 1
                    elif "yellow" in tipo and "card" in tipo:
                        amarillas[lado] += 1
                    elif tipo == "yellowcard":
                        amarillas[lado] += 1
            for v in n.values():
                walk(v, prof + 1)
        elif isinstance(n, list):
            for v in n:
                walk(v, prof + 1)

    walk(data)
    return amarillas, rojas, segundas, referee[0]


def parse_cards(data, match_id, league_id, date, home, away):
    ah = aw = rh = rw = None
    sh = sw = 0
    par_am = _buscar_par_stat(data, CLAVES_AMARILLA)
    par_ro = _buscar_par_stat(data, CLAVES_ROJA)
    fuente = "stats"
    if par_am is None:
        am_ev, ro_ev, seg_ev, referee = _buscar_eventos_tarjetas(data, home, away)
        ah, aw = am_ev["home"], am_ev["away"]
        rh, rw = ro_ev["home"], ro_ev["away"]
        sh, sw = seg_ev["home"], seg_ev["away"]
        fuente = "eventos"
    else:
        ah, aw = par_am
        rh, rw = par_ro if par_ro else (0, 0)
        _, _, _, referee = _buscar_eventos_tarjetas(data, home, away)

    if ah is None and aw is None:
        return []

    total_h = (ah or 0) * 1 + (rh or 0) * 2
    total_w = (aw or 0) * 1 + (rw or 0) * 2

    return [
        {"team": home, "opponent": away, "is_home": 1,
         "yellow": int(ah or 0), "red": int(rh or 0),
         "second_yellow": int(sh), "total_cards": total_h,
         "referee": referee, "fuente": fuente},
        {"team": away, "opponent": home, "is_home": 0,
         "yellow": int(aw or 0), "red": int(rw or 0),
         "second_yellow": int(sw), "total_cards": total_w,
         "referee": referee, "fuente": fuente},
    ]


def inspeccionar(match_id):
    data = api("matchDetails", matchId=match_id)

    def mapa(n, prof=0, camino=""):
        out = []
        if prof > 3:
            return out
        if isinstance(n, dict):
            for k, v in list(n.items())[:22]:
                c = f"{camino}.{k}" if camino else k
                if isinstance(v, dict):
                    out.append(f"{c} {{{len(v)} claves}}")
                    out += mapa(v, prof + 1, c)
                elif isinstance(v, list):
                    out.append(f"{c} [lista de {len(v)}]")
                    if v and isinstance(v[0], dict):
                        out.append(f"  {c}[0] claves: "
                                   + ", ".join(list(v[0].keys())[:14]))
                        out += mapa(v[0], prof + 2, c + "[0]")
                else:
                    out.append(f"{c} = {str(v)[:45]}")
        return out

    home = dig(data, "general", "homeTeam", "name") or "?"
    away = dig(data, "general", "awayTeam", "name") or "?"
    filas = parse_cards(data, match_id, None, None, home, away)
    return {
        "partido": f"{home} vs {away}",
        "extraidas": filas,
        "estructura": mapa(data)[:150],
    }


def update_fixtures(dias=180, quiet=False):
    db_init()
    hoy = datetime.now(timezone.utc).date()
    nuevos = 0
    for d in range(dias):
        day = hoy - timedelta(days=d)
        try:
            data = api("matches", date=day.strftime("%Y%m%d"))
        except Exception as e:
            if not quiet:
                print(f"  {day}: error de red ({type(e).__name__})")
            continue
        rows = []
        for lg in data.get("leagues", []):
            lid = lg.get("primaryId") or lg.get("id")
            if lid not in LIGAS:
                continue
            for m in lg.get("matches", []):
                if not dig(m, "status", "finished", default=False):
                    continue
                rows.append((m.get("id"), lid, LIGAS[lid], day.isoformat(),
                             dig(m, "home", "name"), dig(m, "away", "name")))
        if rows:
            with cx() as c:
                before = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
                c.executemany("INSERT OR IGNORE INTO matches"
                              "(match_id,league_id,league,date,home,away)"
                              " VALUES(?,?,?,?,?,?)", rows)
                nuevos += c.execute(
                    "SELECT COUNT(*) FROM matches").fetchone()[0] - before
        if not quiet and d % 10 == 0:
            print(f"  {day} ... {nuevos} nuevos acumulados")
    print(f"\nFixtures: {nuevos} partidos nuevos guardados.")
    return nuevos


def process_matches(limite=150):
    db_init()
    with cx() as c:
        pend = c.execute("SELECT * FROM matches WHERE processed=0 "
                         "ORDER BY date DESC LIMIT ?", (limite,)).fetchall()
    ok = err = 0
    print(f"Procesando {len(pend)} partidos. Ctrl+C corta y guarda.\n")
    try:
        for i, m in enumerate(pend, 1):
            try:
                data = api("matchDetails", matchId=m["match_id"])
                filas = parse_cards(data, m["match_id"], m["league_id"],
                                    m["date"], m["home"], m["away"])
                with cx() as c:
                    if filas:
                        c.executemany(
                            "INSERT OR REPLACE INTO team_cards VALUES"
                            "(?,?,?,?,?,?,?,?,?,?,?,?)",
                            [(m["match_id"], r["team"], r["opponent"],
                              r["is_home"], m["league_id"], m["date"],
                              r["yellow"], r["red"], r["second_yellow"],
                              r["total_cards"], r["referee"], r["fuente"])
                             for r in filas])
                        ok += 1
                    else:
                        c.execute("UPDATE matches SET error=? WHERE match_id=?",
                                  ("sin tarjetas extraidas", m["match_id"]))
                        err += 1
                    c.execute("UPDATE matches SET processed=1 WHERE match_id=?",
                              (m["match_id"],))
                if filas:
                    print(f"  [{i}/{len(pend)}] {m['home']} {filas[0]['yellow']}A"
                          f"{filas[0]['red']}R vs {m['away']} "
                          f"{filas[1]['yellow']}A{filas[1]['red']}R "
                          f"(arbitro: {filas[0]['referee'] or '?'})")
                else:
                    print(f"  [{i}/{len(pend)}] {m['home']} vs {m['away']} "
                          f"-> NADA extraido")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                with cx() as c:
                    c.execute("UPDATE matches SET processed=1, error=? "
                              "WHERE match_id=?", (str(e)[:150], m["match_id"]))
                err += 1
                print(f"  [{i}/{len(pend)}] ERROR: {e}")
    except KeyboardInterrupt:
        print("\n  Cortado. Lo procesado quedo guardado.")
    print(f"\nOK {ok} | errores {err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--procesar", action="store_true")
    ap.add_argument("--inspeccionar", type=int, metavar="MATCH_ID")
    ap.add_argument("--dias", type=int, default=180)
    a = ap.parse_args()
    db_init()

    if a.inspeccionar:
        out = inspeccionar(a.inspeccionar)
        print(json.dumps(out, ensure_ascii=False, indent=2)[:6000])
        return
    if a.fixtures or a.update:
        update_fixtures(a.dias)
    if a.procesar or a.update:
        process_matches()
    if not any([a.fixtures, a.procesar, a.update, a.inspeccionar]):
        print(__doc__)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCortado.\n")
