#!/usr/bin/env python3
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

# Mejor búsqueda de árbitro (busca en todo el JSON)
def buscar_referee(data, prof_max=10):
    def walk(n, prof=0):
        if prof > prof_max:
            return None
        if isinstance(n, dict):
            for k, v in n.items():
                if "referee" in _k(k) or "official" in _k(k):
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                    if isinstance(v, dict):
                        nm = v.get("name") or v.get("fullName") or v.get("title")
                        if isinstance(nm, str) and nm.strip():
                            return nm.strip()
            for v in n.values():
                r = walk(v, prof + 1)
                if r:
                    return r
        elif isinstance(n, list):
            for v in n:
                r = walk(v, prof + 1)
                if r:
                    return r
        return None
    return walk(data)

# Respaldo Sofascore
def buscar_referee_sofascore(home, away, date_str):
    try:
        url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
        r = http().get(url, timeout=15)
        r.raise_for_status()
        eventos = r.json().get("events", [])
        event_id = None
        for ev in eventos:
            if (ev.get("homeTeam", {}).get("name") == home and
                ev.get("awayTeam", {}).get("name") == away):
                event_id = ev.get("id")
                break
        if not event_id:
            return None
        url_det = f"https://api.sofascore.com/api/v1/event/{event_id}"
        r2 = http().get(url_det, timeout=15)
        r2.raise_for_status()
        data = r2.json().get("event", {})
        ref = data.get("referee", {})
        if isinstance(ref, dict):
            return ref.get("name")
        return None
    except Exception:
        return None

def parse_cards(data, match_id, league_id, date, home, away):
    red_h = dig(data, "header", "status", "numberOfHomeRedCards")
    red_w = dig(data, "header", "status", "numberOfAwayRedCards")
    eventos = dig(data, "content", "matchFacts", "events", "events") or []

    if red_h is None and red_w is None and not eventos:
        return []

    yellow_h = yellow_w = 0
    segunda_h = segunda_w = 0
    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        card = _k(ev.get("card") or "")
        is_home = ev.get("isHome")
        if card == "yellow":
            if is_home:
                yellow_h += 1
            elif is_home is False:
                yellow_w += 1
        elif card == "yellowred":
            if is_home:
                segunda_h += 1
            elif is_home is False:
                segunda_w += 1

    if red_h is None or red_w is None:
        rojas_directas_h = sum(1 for e in eventos if isinstance(e, dict)
                               and _k(e.get("card") or "") == "red" and e.get("isHome"))
        rojas_directas_w = sum(1 for e in eventos if isinstance(e, dict)
                               and _k(e.get("card") or "") == "red" and e.get("isHome") is False)
        red_h = (red_h if red_h is not None else rojas_directas_h + segunda_h)
        red_w = (red_w if red_w is not None else rojas_directas_w + segunda_w)

    referee = buscar_referee(data)
    if not referee:
        referee = buscar_referee_sofascore(home, away, date)

    fuente = "eventos+status"
    total_h = yellow_h * 1 + red_h * 2
    total_w = yellow_w * 1 + red_w * 2

    return [
        {"team": home, "opponent": away, "is_home": 1,
         "yellow": int(yellow_h), "red": int(red_h),
         "second_yellow": int(segunda_h), "total_cards": total_h,
         "referee": referee, "fuente": fuente},
        {"team": away, "opponent": home, "is_home": 0,
         "yellow": int(yellow_w), "red": int(red_w),
         "second_yellow": int(segunda_w), "total_cards": total_w,
         "referee": referee, "fuente": fuente},
    ]

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
            # CORRECCIÓN: Usar primaryId o parentLeagueId
            lid = lg.get("primaryId") or lg.get("id") or lg.get("parentLeagueId")
            # Si el id encontrado no es 112 pero su padre es 112, usar 112
            if lid not in LIGAS:
                parent = lg.get("parentLeagueId")
                if parent in LIGAS:
                    lid = parent
                else:
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

def process_matches(limite=600):
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
    ap.add_argument("--update", action="store_true", help="trae fixtures y procesa tarjetas")
    ap.add_argument("--fixtures", action="store_true", help="solo fixtures")
    ap.add_argument("--procesar", action="store_true", help="solo procesar")
    ap.add_argument("--inspeccionar", type=int, metavar="MATCH_ID", help="vuelca estructura del JSON de un partido")
    ap.add_argument("--reset-tarjetas", action="store_true", help="borra team_cards y marca todo para reprocesar")
    ap.add_argument("--dias", type=int, default=365)
    a = ap.parse_args()
    db_init()
    if a.reset_tarjetas:
        with cx() as c:
            n = c.execute("SELECT COUNT(*) FROM team_cards").fetchone()[0]
            c.execute("DELETE FROM team_cards")
            c.execute("UPDATE matches SET processed=0, error=NULL")
        print(f"Borradas {n} filas. Corre: python cards_data.py --procesar")
        return
    if a.inspeccionar:
        import json
        out = {
            "partido": "Test",
            "extraidas": [],
            "estructura": []
        }
        data = api("matchDetails", matchId=a.inspeccionar)
        home = dig(data, "general", "homeTeam", "name") or "?"
        away = dig(data, "general", "awayTeam", "name") or "?"
        filas = parse_cards(data, a.inspeccionar, None, None, home, away)
        out = {"partido": f"{home} vs {away}", "extraidas": filas}
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
