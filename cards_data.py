#!/usr/bin/env python3
"""
cards_data.py - Recolector de datos de TARJETAS por equipo (Liga Profesional
y las que agregues). Hermano de remates_v10.py, misma infra (FotMob + 
Sofascore de respaldo), pero en vez de remates de jugador guarda tarjetas
por equipo por partido + el arbitro.

============================ COMO SE USA ==============================

  python cards_data.py --update              trae fixtures + procesa tarjetas
  python cards_data.py --fixtures            solo trae la lista de partidos
  python cards_data.py --procesar             solo procesa pendientes
  python cards_data.py --inspeccionar <id>    vuelca la estructura del JSON
                                               de un partido puntual, para
                                               ajustar el extractor si hace
                                               falta (pasame el output)

Archivo que crea: cards.db (al lado del script). Nada mas.
=========================================================================
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    sys.exit('Falta httpx.  Corre:  pip install "httpx>=0.27"')

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


# ======================= CONFIG =======================================

DB_PATH = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.path.dirname(os.path.abspath(__file__)),
    "cards.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Liga Profesional Argentina primero. Mismo formato que remates_v10 asi
# despues podes pegarle el selector de ligas de ahi si queres mas ligas.
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


# ======================= BASE DE DATOS =================================

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
CREATE TABLE IF NOT EXISTS arbitros_proximos(
  home TEXT, away TEXT, arbitro TEXT, actualizado TEXT,
  PRIMARY KEY(home, away));
CREATE TABLE IF NOT EXISTS arbitro_historial(
  match_id INTEGER PRIMARY KEY, arbitro TEXT, home TEXT, away TEXT,
  date TEXT, total_cards_partido REAL);
"""


def cx():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
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


# ======================= CLIENTE FOTMOB (igual a remates_v10) ==========

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


# ======================= EXTRACTOR DE TARJETAS ==========================
# Dos estrategias, igual de espiritu que el extractor generico de remates:
#   1) Barra de stats del partido: busca un par [home, away] bajo una
#      clave tipo "Yellow cards" / "Red cards".
#   2) Lista de eventos del partido: cuenta tarjetas evento por evento y
#      las asigna a home/away. Mas confiable si (1) no aparece.
# Si ninguna de las dos anda, --inspeccionar te muestra la estructura
# real para que la ajustemos.

# Confirmado contra un partido real (Defensa y Justicia vs Platense,
# match_id 5115967):
#   - Los ROJOS totales por equipo (directos + por doble amarilla) estan
#     en header.status.numberOfHomeRedCards / numberOfAwayRedCards.
#     Es el dato mas confiable posible: lo calcula FotMob mismo.
#   - Los eventos de tarjeta (una fila por tarjeta) estan en
#     content.matchFacts.events.events, cada uno con "isHome": bool y
#     "card": "Yellow" | "Red" | "YellowRed". Contamos "Yellow" ahi para
#     las amarillas puras (las YellowRed ya se cuentan como rojo arriba).


import unicodedata

ALIAS_ARBITRAJE = {
    "estudiantes rio cuarto": "estudiantes de rio cuarto",
    "gimnasia mza": "gimnasia mendoza",
    "gimnasia mendoza": "gimnasia mendoza",
    "ind rivadavia mza": "independiente rivadavia",
    "central cordoba": "central cordoba de santiago",
    "newells": "newells old boys",
    "river": "river plate",
    "racing": "racing club",
    "boca": "boca juniors",
    "velez": "velez sarsfield",
    "dep riestra": "deportivo riestra",
}


def _normalizar(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_PALABRAS_VACIAS = {"de", "del", "la", "el", "los"}


def mapear_equipo_lpf(nombre_corto, nombres_db):
    """Busca en nombres_db (nombres tal cual vienen de FotMob) cual
    corresponde a nombre_corto (tal cual aparece en la nota de la LPF).
    Devuelve el nombre de nombres_db que mejor matchea, o None."""
    n = _normalizar(nombre_corto)
    n = ALIAS_ARBITRAJE.get(n, n)
    palabras_corto = set(n.split()) - _PALABRAS_VACIAS
    if not palabras_corto:
        return None
    mejor, mejor_score = None, 0
    for db_name in nombres_db:
        nd = _normalizar(db_name)
        palabras_db = set(nd.split()) - _PALABRAS_VACIAS
        if not palabras_db:
            continue
        if palabras_corto.issubset(palabras_db) or palabras_db.issubset(palabras_corto):
            score = len(palabras_corto & palabras_db)
            if score > mejor_score:
                mejor, mejor_score = db_name, score
    return mejor


MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}

_PATRON_FECHA = re.compile(
    r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+"
    r"(\d{1,2})\s+de\s+(\w+)", re.IGNORECASE)
_PATRON_PARTIDO_LINEA = re.compile(
    r"^\d{1,2}[:.]\d{2}\s+(.+?)\s*[–—-]\s*(.+?)(?:\s*\([^)]*\))*\s*$")
_PATRON_ARBITRO_LINEA = re.compile(r"^[ÁA]rbitro:\s*(.+)$", re.IGNORECASE)


_PATRON_FIXTURE_INLINE = re.compile(
    r"(\d{1,2})[:.](\d{2})\s+(.+?)\s*[–—-]\s*(.+?)\s+[ÁA]rbitro:\s*(.+?)"
    r"(?=\s+[ÁA]rbitro\b|\s+Cuarto\b|\s+VAR:|\s+AVAR:|\s+\d{1,2}[:.]\d{2}\s+\S|\s*$)",
    re.IGNORECASE | re.DOTALL)


_RUIDO_ZONA = re.compile(r"\(\s*(?:zona\s+[a-z]|interzonal)\s*\)", re.IGNORECASE)
_RUIDO_CANAL = re.compile(r"-[^-]{2,30}-\s*$")


def _limpiar_nombre_corto(s):
    """Saca el ruido de '(Zona A)' / '(Interzonal)' y '-Canal-' que la
    LPF agrega despues del nombre del equipo en las notas viejas, sin
    tocar los casos donde el parentesis SI es parte del nombre del
    equipo (ej. 'Estudiantes (Rio Cuarto)', 'Gimnasia (Mza.)')."""
    s = _RUIDO_ZONA.sub("", s)
    for _ in range(3):
        nuevo = _RUIDO_CANAL.sub("", s).strip()
        if nuevo == s:
            break
        s = nuevo
    return re.sub(r"\s+", " ", s).strip()


def _parsear_bloque_arbitros(bloque, anio):
    """Funciona tanto con el formato viejo de la LPF (todo el texto
    corrido, sin saltos de linea, con la fecha pegada al primer
    partido del dia) como con el nuevo (una linea por dato). En vez de
    ir linea por linea, ubica TODAS las fechas y TODOS los partidos
    por su posicion en el texto, y le asigna a cada partido la fecha
    mas cercana que aparece antes que el."""
    fechas = []
    for m in _PATRON_FECHA.finditer(bloque):
        dia = int(m.group(1))
        mes = MESES.get(_normalizar(m.group(2)))
        if mes:
            fechas.append((m.start(), f"{anio}-{mes:02d}-{dia:02d}"))

    resultado = []
    for m in _PATRON_FIXTURE_INLINE.finditer(bloque):
        pos = m.start()
        fecha_actual = None
        for fpos, f in fechas:
            if fpos <= pos:
                fecha_actual = f
            else:
                break
        home_corto = _limpiar_nombre_corto(m.group(3).strip())
        away_corto = _limpiar_nombre_corto(m.group(4).strip())
        arbitro = m.group(5).strip().rstrip(".")
        resultado.append((home_corto, away_corto, arbitro, fecha_actual))
    return resultado


def diagnosticar_nota_vieja():
    """Baja la primera nota vieja de tipo 'autoridades-de-la-fecha' que
    encuentre (no la de esta semana) y muestra el texto crudo tal cual
    lo ve el parser, para poder ajustar las regex si hace falta."""
    if BeautifulSoup is None:
        print("falta beautifulsoup4")
        return
    r = http().get("https://www.ligaprofesional.ar/categoria/arbitraje/", timeout=20)
    r.raise_for_status()
    urls = re.findall(
        r'https://www\.ligaprofesional\.ar/notas/arbitraje/[^\s"\'<>]+/', r.text)
    urls = list(dict.fromkeys(urls))
    url_vieja = next((u for u in urls if "autoridades-de-la-fecha" in u), None)
    if not url_vieja:
        print("no encontre ninguna nota vieja tipo 'autoridades-de-la-fecha'")
        return
    print(f"Probando: {url_vieja}")
    r2 = http().get(url_vieja, timeout=20)
    r2.raise_for_status()
    soup = BeautifulSoup(r2.text, "html.parser")
    texto = soup.get_text("\n")
    ini = texto.find("Designaciones arbitrales")
    if ini == -1:
        ini = texto.find("Autoridades")
    fin = texto.find("Últimas noticias")
    if ini == -1:
        ini = 0
    if fin == -1:
        fin = len(texto)
    bloque = texto[ini:fin]
    print(f"\nlargo del bloque: {len(bloque)} caracteres")
    print("\n===== primeros 1500 caracteres (repr, para ver saltos de linea reales) =====")
    print(repr(bloque[:1500]))
    print("\n===== fechas que encuentra el patron de fecha =====")
    fechas_encontradas = list(_PATRON_FECHA.finditer(bloque))
    print(f"{len(fechas_encontradas)} fechas encontradas")
    for m in fechas_encontradas[:5]:
        print(" -", m.group(0))
    print("\n===== partidos que encuentra el patron inline =====")
    partidos_encontrados = list(_PATRON_FIXTURE_INLINE.finditer(bloque))
    print(f"{len(partidos_encontrados)} partidos encontrados")
    for m in partidos_encontrados[:3]:
        print(" -", m.group(0)[:150])


def historial_arbitros_lpf(max_notas=8):
    """Baja hasta max_notas notas de designaciones (la mas reciente y
    las anteriores) y devuelve todas las designaciones encontradas,
    con fecha. Para construir el promedio de tarjetas por arbitro."""
    if BeautifulSoup is None:
        print("  [historial arbitros] falta beautifulsoup4")
        return []
    try:
        r = http().get("https://www.ligaprofesional.ar/categoria/arbitraje/",
                        timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [historial arbitros] no se pudo bajar la categoria: {e}")
        return []

    urls = re.findall(
        r'https://www\.ligaprofesional\.ar/notas/arbitraje/[^\s"\'<>]+/', r.text)
    urls = list(dict.fromkeys(urls))[:max_notas]
    if not urls:
        print("  [historial arbitros] no encontre notas en la categoria")
        return []

    todos = []
    for url in urls:
        m_anio = re.search(r"/arbitraje/(\d{4})/", url)
        anio = int(m_anio.group(1)) if m_anio else datetime.now().year
        try:
            r2 = http().get(url, timeout=20)
            r2.raise_for_status()
        except Exception as e:
            print(f"  [historial arbitros] fallo {url}: {e}")
            continue
        soup = BeautifulSoup(r2.text, "html.parser")
        texto = soup.get_text("\n")
        ini = texto.find("Designaciones arbitrales")
        if ini == -1:
            ini = texto.find("Autoridades")
        fin = texto.find("Últimas noticias")
        if ini == -1:
            ini = 0
        if fin == -1:
            fin = len(texto)
        bloque = texto[ini:fin]
        entradas = _parsear_bloque_arbitros(bloque, anio)
        todos.extend(entradas)
        print(f"  [historial arbitros] {url.rstrip('/').split('/')[-1][:40]}: "
              f"{len(entradas)} designaciones")
    return todos


def actualizar_historial_arbitros(max_notas=8):
    """Matchea las designaciones historicas contra partidos que YA
    tenemos jugados y procesados, y guarda cuantas tarjetas hubo en
    cada uno, agrupable despues por arbitro."""
    entradas = historial_arbitros_lpf(max_notas)
    if not entradas:
        return 0
    with cx() as c:
        nombres_db = [r[0] for r in c.execute(
            "SELECT DISTINCT home FROM matches UNION "
            "SELECT DISTINCT away FROM matches")]
    guardados = 0
    with cx() as c:
        for home_corto, away_corto, arbitro, fecha in entradas:
            if not fecha:
                continue
            home_db = mapear_equipo_lpf(home_corto, nombres_db)
            away_db = mapear_equipo_lpf(away_corto, nombres_db)
            if not home_db or not away_db:
                continue
            fila = c.execute(
                "SELECT match_id FROM matches WHERE home=? AND away=? "
                "AND date BETWEEN date(?, '-1 day') AND date(?, '+1 day')",
                (home_db, away_db, fecha, fecha)).fetchone()
            if not fila:
                continue
            match_id = fila["match_id"]
            tot = c.execute(
                "SELECT SUM(total_cards) AS total FROM team_cards "
                "WHERE match_id=?", (match_id,)).fetchone()
            if not tot or tot["total"] is None:
                continue
            c.execute(
                "INSERT OR REPLACE INTO arbitro_historial VALUES (?,?,?,?,?,?)",
                (match_id, arbitro, home_db, away_db, fecha, tot["total"]))
            guardados += 1
    print(f"\nHistorial de arbitros: {guardados}/{len(entradas)} "
          f"designaciones matcheadas contra partidos jugados")
    return guardados


def obtener_arbitros_lpf():
    """Baja la nota mas reciente de designaciones arbitrales de la LPF
    y devuelve una lista de (equipo_local_corto, equipo_visita_corto,
    arbitro). Si algo falla devuelve [] sin romper el resto."""
    if BeautifulSoup is None:
        print("  [arbitros] falta beautifulsoup4. Instalalo con: "
              "pip install beautifulsoup4")
        return []
    try:
        r = http().get("https://www.ligaprofesional.ar/categoria/arbitraje/",
                        timeout=20)
        r.raise_for_status()
        m = re.search(
            r'https://www\.ligaprofesional\.ar/notas/arbitraje/[^\s"\'<>]+/',
            r.text)
        if not m:
            print("  [arbitros] no encontre el link a la ultima nota")
            return []
        url_nota = m.group(0)
        r2 = http().get(url_nota, timeout=20)
        r2.raise_for_status()
    except Exception as e:
        print(f"  [arbitros] no se pudo bajar la nota: {e}")
        return []

    soup = BeautifulSoup(r2.text, "html.parser")
    texto = soup.get_text("\n")
    ini = texto.find("Designaciones arbitrales")
    fin = texto.find("Últimas noticias")
    if ini == -1:
        ini = 0
    if fin == -1:
        fin = len(texto)
    bloque = texto[ini:fin]

    patron = re.compile(
        r"\d{1,2}[:.]\d{2}\s+(.+?)\s*[–—-]\s*(.+?)\s*\([^)]*\)"
        r"(?:\s*\([^)]*\))?\s*\n+\s*[ÁA]rbitro:\s*([^\n]+)",
        re.IGNORECASE)

    resultado = []
    for m in patron.finditer(bloque):
        home_corto = m.group(1).strip()
        away_corto = m.group(2).strip()
        arbitro = m.group(3).strip()
        resultado.append((home_corto, away_corto, arbitro))
    return resultado


def actualizar_arbitros():
    """Descarga las designaciones y las guarda en arbitros_proximos,
    matcheando los nombres cortos de la LPF contra los nombres que
    usamos en el resto de la base (los de FotMob)."""
    designaciones = obtener_arbitros_lpf()
    if not designaciones:
        return 0
    with cx() as c:
        nombres_db = [r[0] for r in c.execute(
            "SELECT DISTINCT home FROM matches UNION "
            "SELECT DISTINCT away FROM matches")]
    guardados = 0
    with cx() as c:
        for home_corto, away_corto, arbitro in designaciones:
            home_db = mapear_equipo_lpf(home_corto, nombres_db)
            away_db = mapear_equipo_lpf(away_corto, nombres_db)
            if not home_db or not away_db:
                print(f"  [arbitros] no pude mapear '{home_corto}' vs "
                      f"'{away_corto}' (local={home_db}, visita={away_db})")
                continue
            c.execute(
                "INSERT OR REPLACE INTO arbitros_proximos VALUES (?,?,?,?)",
                (home_db, away_db, arbitro,
                 datetime.now(timezone.utc).isoformat()))
            guardados += 1
            print(f"  [arbitros] {home_db} vs {away_db}: {arbitro}")
    print(f"\nArbitros guardados: {guardados}/{len(designaciones)}")
    return guardados


def buscar_referee(data, prof_max=8):
    """Busca recursivamente una clave tipo 'referee' en el JSON."""
    def walk(n, prof=0):
        if prof > prof_max:
            return None
        if isinstance(n, dict):
            for k, v in n.items():
                if _k(k) in ("referee", "refereename"):
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                    if isinstance(v, dict):
                        nm = v.get("name") or v.get("fullName")
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


def parse_cards(data, match_id, league_id, date, home, away):
    """Devuelve dos filas (home, away) con tarjetas + arbitro, o [] si no
    se pudo extraer nada (partido queda marcado con error para revisar)."""
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

    # Si por algun motivo no vino el conteo de header.status, usamos lo
    # que se pudo contar de los eventos (rojas directas + segundas).
    if red_h is None or red_w is None:
        rojas_directas_h = sum(1 for e in eventos if isinstance(e, dict)
                               and _k(e.get("card") or "") == "red" and e.get("isHome"))
        rojas_directas_w = sum(1 for e in eventos if isinstance(e, dict)
                               and _k(e.get("card") or "") == "red" and e.get("isHome") is False)
        red_h = (red_h if red_h is not None else rojas_directas_h + segunda_h)
        red_w = (red_w if red_w is not None else rojas_directas_w + segunda_w)

    referee = buscar_referee(data)
    fuente = "eventos+status"

    # Mercado "total de tarjetas": amarilla=1, roja=2 (incluye directas y
    # las que salen de una segunda amarilla).
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


def inspeccionar(match_id):
    """Igual que en remates_v10: mapa de la estructura real del JSON,
    para poder ajustar CLAVES_AMARILLA/CLAVES_ROJA si no matchean."""
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


# ======================= ACTUALIZACION ==================================

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
    ap.add_argument("--update", action="store_true",
                    help="trae fixtures y procesa tarjetas")
    ap.add_argument("--fixtures", action="store_true", help="solo fixtures")
    ap.add_argument("--procesar", action="store_true", help="solo procesar")
    ap.add_argument("--inspeccionar", type=int, metavar="MATCH_ID",
                    help="vuelca estructura del JSON de un partido")
    ap.add_argument("--reset-tarjetas", action="store_true",
                    help="borra team_cards y marca todo para reprocesar "
                         "(usalo una vez tras actualizar el extractor)")
    ap.add_argument("--arbitros", action="store_true",
                    help="solo actualiza los arbitros designados de la LPF")
    ap.add_argument("--historial-arbitros", action="store_true",
                    help="baja notas viejas de designaciones y arma el "
                         "promedio de tarjetas por arbitro (mas lento)")
    ap.add_argument("--dias", type=int, default=365)
    a = ap.parse_args()
    db_init()

    if a.historial_arbitros:
        actualizar_historial_arbitros(max_notas=10)
        return
    if a.arbitros:
        actualizar_arbitros()
        return
    if a.reset_tarjetas:
        with cx() as c:
            n = c.execute("SELECT COUNT(*) FROM team_cards").fetchone()[0]
            c.execute("DELETE FROM team_cards")
            c.execute("UPDATE matches SET processed=0, error=NULL")
        print(f"Borradas {n} filas de team_cards. Todos los partidos "
              f"quedaron marcados para reprocesar.\nCorre ahora: "
              f"python cards_data.py --procesar")
        return
    if a.inspeccionar:
        out = inspeccionar(a.inspeccionar)
        print(json.dumps(out, ensure_ascii=False, indent=2)[:6000])
        return
    if a.fixtures or a.update:
        update_fixtures(a.dias)
    if a.procesar or a.update:
        process_matches()
    if a.update:
        try:
            actualizar_arbitros()
        except Exception as e:
            print(f"  [arbitros] fallo sin frenar el resto: {e}")
    if not any([a.fixtures, a.procesar, a.update, a.inspeccionar]):
        print(__doc__)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCortado.\n")
