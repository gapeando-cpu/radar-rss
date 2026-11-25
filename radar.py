#!/usr/bin/env python3
"""Radar RSS - vigila fuentes (hilos de Rankia, canales RSS/Atom y paginas web)
y envia las publicaciones nuevas a Telegram. Sin IA.

Estado en SQLite (data/radar.db), que el workflow guarda en el repo.
Sin dependencias externas: solo la libreria estandar de Python.
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
RUTA_DB = RAIZ / "data" / "radar.db"
RUTA_FUENTES = RAIZ / "fuentes.json"

CABECERAS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 RadarRSS/1.0",
    "Accept-Language": "es-ES,es;q=0.9",
}

INTENTOS_HTTP = 3               # recuperacion automatica ante fallos de descarga
ESPERA_REINTENTO_S = 5
MAX_PAGINAS_ATRAS = 5           # paginas hacia atras si hay muchos mensajes nuevos
SEMILLA_AVISOS = 1              # avisos reales al estrenar una fuente
MAX_VISTOS = 3000               # poda del historial por fuente

# ---------------------------------------------------------------- utilidades


def http_get(url):
    """Descarga una URL con reintentos y espera creciente."""
    ultimo = None
    for intento in range(INTENTOS_HTTP):
        try:
            req = urllib.request.Request(url, headers=CABECERAS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            ultimo = exc
            if intento < INTENTOS_HTTP - 1:
                time.sleep(ESPERA_REINTENTO_S * (intento + 1))
    raise RuntimeError(f"no se pudo descargar tras {INTENTOS_HTTP} intentos: {ultimo}")


def limpiar_html(fragmento):
    """HTML -> texto plano legible."""
    fragmento = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragmento)
    fragmento = re.sub(r"(?i)<br\s*/?>", " ", fragmento)
    fragmento = re.sub(r"(?i)</(p|div|li|blockquote)>", " ", fragmento)
    fragmento = re.sub(r"<[^>]+>", " ", fragmento)
    return re.sub(r"\s+", " ", unescape(fragmento)).strip()


def ahora_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ telegram


def telegram(texto):
    """Envia un mensaje al chat configurado. Sin secretos: modo prueba (imprime)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("[modo prueba] mensaje que se enviaria:\n" + texto + "\n---")
        return True
    datos = urllib.parse.urlencode({
        "chat_id": chat,
        "text": texto,
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ultimo = None
    for intento in range(INTENTOS_HTTP):
        try:
            req = urllib.request.Request(url, data=datos, headers=CABECERAS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                ok = json.load(resp).get("ok", False)
            if ok:
                return True
            ultimo = "la API devolvio ok=false"
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            ultimo = exc
        if intento < INTENTOS_HTTP - 1:
            time.sleep(ESPERA_REINTENTO_S * (intento + 1))
    print(f"AVISO: no se pudo enviar a Telegram: {ultimo}")
    return False


# -------------------------------------------------------------------- sqlite


def abrir_db():
    RUTA_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(RUTA_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS vistos ("
        " fuente TEXT NOT NULL, pub_id TEXT NOT NULL, visto_en TEXT NOT NULL,"
        " PRIMARY KEY (fuente, pub_id))")
    db.execute(
        "CREATE TABLE IF NOT EXISTS meta ("
        " clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS revisiones ("
        " fuente TEXT PRIMARY KEY, revisado_en TEXT NOT NULL,"
        " resultado TEXT NOT NULL, nuevos INTEGER NOT NULL)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS pendientes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, fuente TEXT NOT NULL,"
        " texto TEXT NOT NULL, creado_en TEXT NOT NULL)")
    return db


class ErrorEntrega(RuntimeError):
    pass


def enviar_pendientes(db, fuente_id):
    """Retira cada bloque solo cuando Telegram confirma su recepcion."""
    filas = db.execute(
        "SELECT id, texto FROM pendientes WHERE fuente=? ORDER BY id",
        (fuente_id,)).fetchall()
    for pendiente_id, texto in filas:
        if not telegram(texto):
            raise ErrorEntrega("Telegram no confirmo el envio; bloque pendiente de reintento")
        db.execute("DELETE FROM pendientes WHERE id=?", (pendiente_id,))
        db.commit()


def meta_get(db, clave):
    fila = db.execute("SELECT valor FROM meta WHERE clave=?", (clave,)).fetchone()
    return fila[0] if fila else None


def meta_set(db, clave, valor):
    db.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES (?,?)",
               (clave, str(valor)))


def registrar_revision(db, fuente_id, resultado, nuevos):
    db.execute(
        "INSERT OR REPLACE INTO revisiones (fuente, revisado_en, resultado, nuevos)"
        " VALUES (?,?,?,?)", (fuente_id, ahora_iso(), resultado, nuevos))


def escribir_estado(db, fuentes):
    # JSON publico con el resultado de la ultima revision; el comando /estado
    # del bot lo lee por raw.githubusercontent.com sin tocar la API de GitHub.
    filas = {f[0]: f[1:] for f in db.execute(
        "SELECT fuente, revisado_en, resultado, nuevos FROM revisiones")}
    estado = {
        "ultima_revision_utc": ahora_iso(),
        "fuentes": [
            {
                "id": fuente["id"],
                "nombre": fuente.get("nombre", fuente["id"]),
                "resultado": filas.get(fuente["id"], (None, "pendiente", 0))[1],
                "nuevos": filas.get(fuente["id"], (None, "pendiente", 0))[2],
                "bloques_pendientes": db.execute(
                    "SELECT count(*) FROM pendientes WHERE fuente=?",
                    (fuente["id"],)).fetchone()[0],
            }
            for fuente in fuentes
        ],
    }
    with open(RUTA_DB.parent / "estado.json", "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


def filtrar_nuevos(db, fuente, pubs):
    """Deja solo publicaciones no vistas; actualiza vistos y marca de agua.

    Devuelve (nuevos, es_primera_vez)."""
    marca = meta_get(db, f"marca:{fuente['id']}")
    es_primera = marca is None
    vistos = {f[0] for f in db.execute(
        "SELECT pub_id FROM vistos WHERE fuente=?", (fuente["id"],))}
    nuevos = []
    max_id = int(marca) if marca else 0
    for pub in pubs:
        if pub["id"] in vistos:
            continue
        num = int(pub["id"]) if pub["id"].isdigit() else 0
        if num and num <= max_id:
            continue
        nuevos.append(pub)
        if num:
            max_id = max(max_id, num)
    ahora = ahora_iso()
    for pub in nuevos:
        db.execute("INSERT OR IGNORE INTO vistos (fuente, pub_id, visto_en)"
                   " VALUES (?,?,?)", (fuente["id"], pub["id"], ahora))
    if str(max_id) != (marca or ""):
        meta_set(db, f"marca:{fuente['id']}", max_id)
    # Poda del historial para que la base no crezca sin limite.
    db.execute(
        "DELETE FROM vistos WHERE fuente=? AND pub_id NOT IN ("
        " SELECT pub_id FROM vistos WHERE fuente=?"
        " ORDER BY visto_en DESC, pub_id DESC LIMIT ?)",
        (fuente["id"], fuente["id"], MAX_VISTOS))
    return nuevos, es_primera


# --------------------------------------------------------------- rankia

RE_ARTICULO = re.compile(
    '<article\\b(?=[^>]*\\bclass="[^"]*\\brnk-PostReply\\b)(?=[^>]*\\bid="respuesta_(\\d+)")(?=[^>]*\\buser="([^"]*)")[^>]*>')
RE_TIEMPO = re.compile(
    r'<time class="rnk-PostReplyMetadata_Date" datetime="([^"]*)"')
RE_PAGINA_ACTUAL = re.compile(
    r'pagination-current[^>]*aria-label="Página (\d+)"')


def parse_rankia(html, url_base):
    """Extrae mensajes de una pagina de hilo de Rankia y el numero de pagina."""
    pagina = 1
    m = RE_PAGINA_ACTUAL.search(html)
    if m:
        pagina = int(m.group(1))
    articulos = list(RE_ARTICULO.finditer(html))
    posts = []
    for i, art in enumerate(articulos):
        pub_id, autor = art.group(1), unescape(art.group(2)).strip()
        bloque = html[art.start():articulos[i + 1].start() if i + 1 < len(articulos)
                      else len(html)]
        t = RE_TIEMPO.search(bloque)
        fecha_iso = t.group(1) if t else ""
        cuerpo = ""
        cm = re.search(r'id="texto_respuesta_' + pub_id + r'"[^>]*>(.*?'
                       r')<div class="rnk-ThreadTools', bloque, re.S)
        if cm:
            cuerpo = limpiar_html(cm.group(1))
        posts.append({
            "id": pub_id,
            "autor": autor,
            "fecha_iso": fecha_iso,
            "texto": cuerpo,
            "pagina": pagina,
        })
    return posts, pagina


RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_EMAIL_CON_COLETA = re.compile(
    r"(?i)(?:mi\s+)?(?:e-?mail|correo(?:\s+electr[oó]nico)?)\s*(?:es\s*)?:\s*"
    + RE_EMAIL.pattern)


def quitar_emails(texto):
    """Sin direcciones de correo en los avisos (peticion de Carlos 2026-11-20):
    fuera la direccion y, si la precede, la coletilla tipo 'Mi correo:'."""
    texto = RE_EMAIL_CON_COLETA.sub("", texto)
    texto = RE_EMAIL.sub("", texto)
    texto = re.sub(r"\s{2,}", " ", texto)
    texto = re.sub(r"\s+([,.;:])", r"\1", texto)
    return texto.lstrip(" ,;:.").rstrip(" ,;:")


def revisar_rankia(fuente, db):
    """Descarga la ultima pagina del hilo (la URL canonica la sirve) y retrocede
    si hay mas mensajes nuevos de los que caben en una pagina."""
    url = fuente["url"].split("#")[0].split("?")[0]
    html = http_get(url)
    posts, pagina = parse_rankia(html, url)
    if not posts:
        raise RuntimeError("la pagina no contiene mensajes del hilo")

    marca = meta_get(db, f"marca:{fuente['id']}")
    if marca:
        marca = int(marca)
        vistos = {f[0] for f in db.execute(
            "SELECT pub_id FROM vistos WHERE fuente=?", (fuente["id"],))}
        atras = 0
        # Si el mensaje mas viejo de la pagina sigue por encima de la marca
        # de agua, hay novedades en paginas anteriores: retroceder. Sin el
        # requisito de pagina llena, que perdia mensajes justo cuando el
        # hilo saltaba de pagina (la pagina nueva empieza con pocos posts).
        while (pagina > 1 and atras < MAX_PAGINAS_ATRAS
               and min(int(p["id"]) for p in posts) > marca):
            pagina -= 1
            atras += 1
            html = http_get(f"{url}?page={pagina}")
            prev, pagina_real = parse_rankia(html, url)
            if not prev:
                break
            pagina = pagina_real
            posts = prev + posts
        if atras == MAX_PAGINAS_ATRAS:
            print(f"AVISO: {fuente['id']}: posible hueco tras retroceder "
                  f"{MAX_PAGINAS_ATRAS} paginas")

    # De viejo a nuevo.
    posts.sort(key=lambda p: int(p["id"]))
    nuevos, es_primera = filtrar_nuevos(db, fuente, posts)
    if es_primera:
        semilla = nuevos[-SEMILLA_AVISOS:]
        enviar_avisos_rankia(fuente, semilla, es_estreno=True, db=db)
        return len(semilla)
    if nuevos:
        enviar_avisos_rankia(fuente, nuevos, db=db)
    return len(nuevos)


def enviar_avisos_rankia(fuente, pubs, es_estreno=False, db=None):
    """Un solo aviso por hilo y revision: los mensajes nuevos van juntos,
    separados, y sin enlace (preferencia de Carlos). El texto de cada
    mensaje va completo, pero sin la cabecera 'autor · fecha' ni emails
    (preferencia de Carlos del 2026-11-20)."""
    cabecera = f"📡 {fuente['nombre']}"
    if es_estreno:
        cabecera += " (primer aviso del radar)"
    bloques = []
    for pub in pubs:
        cuerpo = pub["texto"] or "(mensaje sin texto, quizas solo imagen)"
        cuerpo = quitar_emails(cuerpo) or "(mensaje solo con datos de contacto)"
        bloques.append(cuerpo)
    return enviar_agrupado(cabecera, bloques, db=db, fuente_id=fuente["id"])


def enviar_agrupado(cabecera, bloques, db=None, fuente_id=None):
    """Junta los bloques en el menor numero de mensajes de Telegram posible
    (tope 4000 caracteres; el limite real de Telegram es 4096). Un bloque
    que no cabe ni solo en un mensaje se parte en trozos, sin perder texto."""
    separador = "\n\n──────────\n\n"
    limite = 4000 - len(cabecera) - 40  # margen: cabecera + " (cont.)" + saltos
    piezas = []
    for bloque in bloques:
        while len(bloque) > limite:
            corte = bloque.rfind("\n", limite // 2, limite)
            if corte == -1:
                corte = bloque.rfind(" ", limite // 2, limite)
            if corte == -1:
                corte = limite
            piezas.append(bloque[:corte].rstrip() + " …")
            bloque = "… " + bloque[corte:].lstrip()
        piezas.append(bloque)
    mensajes = []
    actual = cabecera
    for bloque in piezas:
        candidato = actual + (separador if actual != cabecera else "\n\n") + bloque
        if actual != cabecera and len(candidato) > 4000:
            mensajes.append(actual)
            actual = cabecera + " (cont.)" + "\n\n" + bloque
        else:
            actual = candidato
    mensajes.append(actual)
    if db is not None:
        db.executemany(
            "INSERT INTO pendientes (fuente, texto, creado_en) VALUES (?,?,?)",
            [(fuente_id, texto, ahora_iso()) for texto in mensajes])
        # Vistos, marca de agua y cola se guardan juntos antes de la red.
        db.commit()
        enviar_pendientes(db, fuente_id)
        return True
    for texto in mensajes:
        if not telegram(texto):
            return False
    return True


# --------------------------------------------------------------- rss / atom


def texto_xml(elem, nombre):
    hijo = elem.find(nombre)
    return (hijo.text or "").strip() if hijo is not None and hijo.text else ""


def revisar_rss(fuente, db):
    xml = http_get(fuente["url"])
    raiz = ET.fromstring(xml)
    pubs = []
    if raiz.tag == "rss" or raiz.find("channel") is not None:
        canal = raiz.find("channel") or raiz
        for item in canal.findall("item"):
            pub_id = texto_xml(item, "guid") or texto_xml(item, "link")
            if not pub_id:
                continue
            pubs.append({
                "id": pub_id, "autor": texto_xml(item, "author"),
                "fecha_iso": texto_xml(item, "pubDate"),
                "titulo": texto_xml(item, "title"),
                "texto": limpiar_html(texto_xml(item, "description")),
                "enlace": texto_xml(item, "link"),
            })
    else:  # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for item in raiz.findall(f"{ns}entry"):
            enlace = ""
            for link in item.findall(f"{ns}link"):
                if link.get("rel", "alternate") == "alternate":
                    enlace = link.get("href", "")
                    break
            pub_id = texto_xml(item, f"{ns}id") or enlace
            if not pub_id:
                continue
            pubs.append({
                "id": pub_id,
                "autor": texto_xml(item, f"{ns}author/{ns}name"),
                "fecha_iso": texto_xml(item, f"{ns}published")
                             or texto_xml(item, f"{ns}updated"),
                "titulo": texto_xml(item, f"{ns}title"),
                "texto": limpiar_html(texto_xml(item, f"{ns}summary")
                                      or texto_xml(item, f"{ns}content")),
                "enlace": enlace,
            })
    if not pubs:
        raise RuntimeError("el canal no contiene entradas reconocibles")
    nuevos, es_primera = filtrar_nuevos(db, fuente, pubs)
    if es_primera:
        nuevos = nuevos[-SEMILLA_AVISOS:]
    if nuevos:
        cabecera = f"📡 {fuente['nombre']}"
        if es_primera:
            cabecera += " (primer aviso del radar)"
        bloques = [f"{pub['titulo']}\n{quitar_emails(pub['texto'])}".strip()
                   for pub in nuevos]
        enviar_agrupado(cabecera, bloques)
    return len(nuevos)


# --------------------------------------------------------- pagina generica

RE_ENLACE = re.compile(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def revisar_pagina(fuente, db):
    """Vigila enlaces nuevos en el contenido principal de una pagina web."""
    html = http_get(fuente["url"])
    m = re.search(r"(?is)<(main|article)[^>]*>(.*)</\1>", html)
    zona = m.group(2) if m else html
    pubs = []
    for href, texto in RE_ENLACE.findall(zona):
        titulo = limpiar_html(texto)
        if len(titulo) < 20 or href.startswith(("#", "javascript")):
            continue
        enlace = urllib.parse.urljoin(fuente["url"], href)
        pubs.append({"id": enlace, "titulo": titulo, "texto": "",
                     "enlace": enlace, "autor": "", "fecha_iso": ""})
    if not pubs:
        raise RuntimeError("no se encontraron enlaces que vigilar")
    nuevos, es_primera = filtrar_nuevos(db, fuente, pubs)
    if es_primera:
        return 0  # pagina generica: solo siembra, sin avisos de estreno
    if nuevos:
        enviar_agrupado(f"📡 {fuente['nombre']}",
                        [pub["titulo"] for pub in nuevos])
    return len(nuevos)


# --------------------------------------------------------------------- main

REVISORES = {
    "rankia_hilo": revisar_rankia,
    "rss": revisar_rss,
    "pagina": revisar_pagina,
}


def en_horario():
    # Carlos: revisiones solo de 7:00 a 23:00, hora de Madrid. El cron de
    # GitHub va en UTC; con zoneinfo la ventana se ajusta sola al cambio de
    # hora de verano/invierno. Sin zoneinfo se revisa siempre.
    try:
        from zoneinfo import ZoneInfo
        hora = datetime.now(ZoneInfo("Europe/Madrid")).hour
    except Exception:
        return True
    return 7 <= hora <= 23


def main():
    if not en_horario():
        print("Fuera de horario (7:00-23:00 Madrid); revision omitida")
        return 0
    with open(RUTA_FUENTES, encoding="utf-8") as f:
        fuentes = json.load(f)["fuentes"]
    db = abrir_db()
    fallos = 0
    fallos_entrega = 0
    for fuente in fuentes:
        tipo = fuente.get("tipo", "")
        if tipo not in REVISORES:
            print(f"AVISO: tipo desconocido '{tipo}' en {fuente.get('id')}")
            continue
        try:
            enviar_pendientes(db, fuente["id"])
            nuevos = REVISORES[tipo](fuente, db)
            registrar_revision(db, fuente["id"], "ok", nuevos)
            print(f"{fuente['id']}: {nuevos} avisos nuevos")
        except Exception as exc:  # una fuente rota no frena a las demas
            fallos += 1
            if isinstance(exc, ErrorEntrega):
                fallos_entrega += 1
            registrar_revision(db, fuente["id"], f"error: {exc}", 0)
            print(f"ERROR en {fuente['id']}: {exc}")
    db.commit()
    escribir_estado(db, fuentes)

    if not meta_get(db, "activado"):
        nombres = "\n".join(f"· {f['nombre']}" for f in fuentes)
        telegram("✅ Radar RSS activado. Vigilo estas fuentes cada hora, de 7:00 a 23:00 hora de Madrid, "
                 f"y te aviso solo de lo nuevo:\n{nombres}\n\n"
                 "Comandos: /fuentes y /estado")
        meta_set(db, "activado", ahora_iso())
        db.commit()
    db.close()
    if fallos_entrega:
        print("Telegram dejo bloques pendientes; se reintentaran en el siguiente ciclo")
        return 1
    if fallos == len(fuentes):
        print("Todas las fuentes han fallado")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
