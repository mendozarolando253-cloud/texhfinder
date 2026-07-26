#!/usr/bin/env python3
"""
TechFinder Bot
==============
Bot de Telegram que agrega ofertas, apps gratis, noticias tech y trucos,
monetizando los enlaces compartidos a través de ShrinkEarn.

Diseñado para ejecutarse como un proceso CORTO (no persistente) disparado
periódicamente por un workflow de GitHub Actions (cron cada 10 minutos).

Cada ejecución:
  1. Lee el último update_id procesado desde last_update_id.txt
  2. Pide a Telegram (long polling corto) los mensajes pendientes
  3. Procesa cada comando y responde
  4. Guarda el nuevo último update_id procesado
  5. Termina (el workflow se encarga de hacer commit/push del archivo)

Autor: TechFinder
"""

import os
import sys
import json
import random
import logging
import asyncio
from datetime import datetime, timezone

import requests
import feedparser
import praw
from telegram import Bot
from telegram.error import TelegramError

# --------------------------------------------------------------------------
# CONFIGURACIÓN Y LOGGING
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TechFinderBot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(BASE_DIR, "last_update_id.txt")
TIPS_FILE = os.path.join(BASE_DIR, "tips.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "techfinder-bot/1.0")
SHRINKEARN_API_KEY = os.environ.get("SHRINKEARN_API_KEY")

# Cuánto tiempo (segundos) esperamos como máximo recibiendo updates en esta
# ejecución. GitHub Actions no tiene problema con procesos largos, pero
# mantenemos esto corto para no chocar con la siguiente ejecución programada.
POLLING_TIMEOUT_SECONDS = 8
MAX_RUNTIME_SECONDS = 50

REQUIRED_ENV_VARS = ["TELEGRAM_BOT_TOKEN"]


def check_env_vars():
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error(f"Faltan variables de entorno obligatorias: {missing}")
        sys.exit(1)


# --------------------------------------------------------------------------
# ACORTADOR DE ENLACES: SHRINKEARN
# --------------------------------------------------------------------------

def shrink_url(long_url: str) -> str:
    """
    Acorta una URL usando la API de ShrinkEarn.
    Si falla o no hay API key configurada, devuelve la URL original
    para que el bot nunca se rompa por un fallo del acortador.
    Docs: https://shrinkearn.com/api.html
    """
    if not SHRINKEARN_API_KEY:
        logger.warning("SHRINKEARN_API_KEY no configurada, se usará el enlace original.")
        return long_url

    api_url = "https://shrinkearn.com/api"
    params = {
        "api": SHRINKEARN_API_KEY,
        "url": long_url,
        "format": "json",
    }

    try:
        resp = requests.get(api_url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # La API de ShrinkEarn (y clones similares) devuelve algo como:
        # {"status": "success", "shortenedUrl": "https://shrinkearn.com/xxxx"}
        if data.get("status") == "success" and data.get("shortenedUrl"):
            return data["shortenedUrl"]

        logger.warning(f"ShrinkEarn no devolvió un enlace válido: {data}")
        return long_url

    except Exception as e:
        logger.error(f"Error acortando URL con ShrinkEarn: {e}")
        return long_url


def format_post(titulo: str, dato_clave: str, url_larga: str, hashtags: str) -> str:
    """Genera el texto final atractivo con el enlace ya acortado."""
    enlace_corto = shrink_url(url_larga)
    return (
        f"🔥 {titulo} - {dato_clave}\n"
        f"👉 {enlace_corto}\n"
        f"{hashtags}"
    )


# --------------------------------------------------------------------------
# FUENTE: REDDIT (PRAW)
# --------------------------------------------------------------------------

def get_reddit_client():
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        logger.warning("Credenciales de Reddit no configuradas.")
        return None
    try:
        return praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
        )
    except Exception as e:
        logger.error(f"Error creando cliente de Reddit: {e}")
        return None


def buscar_ofertas_buildapcsales(min_upvotes: int = 50, limite: int = 3):
    """Busca las N ofertas más recientes de r/buildapcsales con más de min_upvotes."""
    reddit = get_reddit_client()
    if not reddit:
        return []

    resultados = []
    try:
        subreddit = reddit.subreddit("buildapcsales")
        for post in subreddit.new(limit=50):
            if post.score >= min_upvotes and not post.stickied:
                resultados.append(post)
            if len(resultados) >= limite:
                break
    except Exception as e:
        logger.error(f"Error buscando en r/buildapcsales: {e}")

    return resultados


def buscar_chollo_gadgetdeals(min_upvotes: int = 30):
    """Busca 1 oferta de r/gadgetdeals con más de min_upvotes."""
    reddit = get_reddit_client()
    if not reddit:
        return None

    try:
        subreddit = reddit.subreddit("gadgetdeals")
        for post in subreddit.new(limit=50):
            if post.score >= min_upvotes and not post.stickied:
                return post
    except Exception as e:
        logger.error(f"Error buscando en r/gadgetdeals: {e}")

    return None


def buscar_apps_gratis():
    """Busca apps temporalmente gratis en r/AppHookup."""
    reddit = get_reddit_client()
    if not reddit:
        return []

    resultados = []
    try:
        subreddit = reddit.subreddit("AppHookup")
        for post in subreddit.new(limit=30):
            titulo = post.title.upper()
            # Los posts de apps gratis suelen marcar la plataforma entre [corchetes]
            if any(tag in titulo for tag in ["[FREE]", "[GRATIS]", "[100% OFF]"]) and not post.stickied:
                resultados.append(post)
            if len(resultados) >= 3:
                break
    except Exception as e:
        logger.error(f"Error buscando en r/AppHookup: {e}")

    return resultados


# --------------------------------------------------------------------------
# FUENTE: RSS (NOTICIAS)
# --------------------------------------------------------------------------

def obtener_ultima_noticia():
    """Obtiene el último titular de The Verge o, si falla, de Xataka."""
    feeds = [
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Xataka", "https://www.xataka.com/feedburner.xml"),
    ]

    for nombre_fuente, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            if feed.entries:
                entrada = feed.entries[0]
                return {
                    "fuente": nombre_fuente,
                    "titulo": entrada.get("title", "Sin título"),
                    "url": entrada.get("link", ""),
                }
        except Exception as e:
            logger.error(f"Error leyendo RSS de {nombre_fuente}: {e}")
            continue

    return None


# --------------------------------------------------------------------------
# FUENTE: TIPS / TRUCOS
# --------------------------------------------------------------------------

def cargar_tips():
    try:
        with open(TIPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error cargando tips.json: {e}")
        return ["No se pudieron cargar los tips en este momento."]


def obtener_tip_aleatorio():
    tips = cargar_tips()
    return random.choice(tips)


# --------------------------------------------------------------------------
# FUENTE: YOUTUBE (yt-dlp, sin API key)
# --------------------------------------------------------------------------

def buscar_youtube(termino: str):
    """Busca en YouTube usando yt-dlp y devuelve el primer resultado."""
    try:
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch1",
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(termino, download=False)

            if "entries" in info and info["entries"]:
                entry = info["entries"][0]
            else:
                entry = info

            video_id = entry.get("id")
            titulo = entry.get("title", termino)
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else entry.get("url")

            return {"titulo": titulo, "url": url}

    except Exception as e:
        logger.error(f"Error buscando en YouTube con yt-dlp: {e}")
        return None


# --------------------------------------------------------------------------
# MANEJADORES DE COMANDOS
# --------------------------------------------------------------------------

MENU_TEXTO = (
    "🤖 *¡Bienvenido a TechFinder!*\n\n"
    "Tu bot para encontrar ofertas, apps gratis, noticias y trucos de tecnología.\n\n"
    "*Comandos disponibles:*\n"
    "/ofertas - 3 ofertas recientes de r/buildapcsales\n"
    "/chollo - 1 oferta de r/gadgetdeals\n"
    "/appgratis - Apps temporalmente gratis\n"
    "/noticia - Último titular de tecnología\n"
    "/truco - Un tip aleatorio de tecnología\n"
    "/buscar [término] - Busca un vídeo en YouTube\n\n"
    "¡Comparte los enlaces con tus amigos! 🚀"
)


async def cmd_start() -> str:
    return MENU_TEXTO


async def cmd_ofertas() -> str:
    posts = buscar_ofertas_buildapcsales(min_upvotes=50, limite=3)
    if not posts:
        return "😕 No encontré ofertas recientes con más de 50 votos en r/buildapcsales. Prueba de nuevo en un rato."

    mensajes = []
    for post in posts:
        mensajes.append(
            format_post(
                titulo=post.title,
                dato_clave=f"👍 {post.score} votos",
                url_larga=f"https://www.reddit.com{post.permalink}",
                hashtags="#TechFinder #Oferta #PCBuilding",
            )
        )
    return "\n\n".join(mensajes)


async def cmd_chollo() -> str:
    post = buscar_chollo_gadgetdeals(min_upvotes=30)
    if not post:
        return "😕 No encontré chollos recientes con más de 30 votos en r/gadgetdeals. Prueba de nuevo en un rato."

    return format_post(
        titulo=post.title,
        dato_clave=f"👍 {post.score} votos",
        url_larga=f"https://www.reddit.com{post.permalink}",
        hashtags="#TechFinder #Chollo #Gadgets",
    )


async def cmd_appgratis() -> str:
    posts = buscar_apps_gratis()
    if not posts:
        return "😕 No encontré apps gratis marcadas en r/AppHookup ahora mismo. Prueba de nuevo en un rato."

    mensajes = []
    for post in posts:
        mensajes.append(
            format_post(
                titulo=post.title,
                dato_clave="App temporalmente gratis",
                url_larga=f"https://www.reddit.com{post.permalink}",
                hashtags="#TechFinder #AppGratis #Descuento",
            )
        )
    return "\n\n".join(mensajes)


async def cmd_noticia() -> str:
    noticia = obtener_ultima_noticia()
    if not noticia:
        return "😕 No pude obtener noticias en este momento. Prueba de nuevo en un rato."

    return format_post(
        titulo=noticia["titulo"],
        dato_clave=f"Fuente: {noticia['fuente']}",
        url_larga=noticia["url"],
        hashtags="#TechFinder #Noticia #Tecnología",
    )


async def cmd_truco() -> str:
    tip = obtener_tip_aleatorio()
    return f"💡 *Truco tech del día:*\n\n{tip}\n\n#TechFinder #Truco #Tecnología"


async def cmd_buscar(termino: str) -> str:
    if not termino:
        return "✏️ Usa el comando así: /buscar iPhone 16"

    resultado = buscar_youtube(termino)
    if not resultado or not resultado.get("url"):
        return f"😕 No encontré resultados en YouTube para '{termino}'."

    return format_post(
        titulo=resultado["titulo"],
        dato_clave=f"Resultado para '{termino}'",
        url_larga=resultado["url"],
        hashtags="#TechFinder #YouTube #Video",
    )


# --------------------------------------------------------------------------
# ENRUTADOR DE MENSAJES
# --------------------------------------------------------------------------

async def procesar_comando(texto: str) -> str:
    """Recibe el texto crudo del mensaje y devuelve la respuesta del bot."""
    texto = texto.strip()
    partes = texto.split(maxsplit=1)
    comando = partes[0].lower().split("@")[0]  # soporta /comando@NombreDelBot
    argumento = partes[1] if len(partes) > 1 else ""

    try:
        if comando == "/start" or comando == "/help":
            return await cmd_start()
        elif comando == "/ofertas":
            return await cmd_ofertas()
        elif comando == "/chollo":
            return await cmd_chollo()
        elif comando == "/appgratis":
            return await cmd_appgratis()
        elif comando == "/noticia":
            return await cmd_noticia()
        elif comando == "/truco":
            return await cmd_truco()
        elif comando == "/buscar":
            return await cmd_buscar(argumento)
        else:
            return (
                "🤔 No reconozco ese comando.\n\n" + MENU_TEXTO
            )
    except Exception as e:
        logger.error(f"Error procesando comando '{comando}': {e}")
        return "⚠️ Ocurrió un error procesando tu solicitud. Inténtalo de nuevo en unos minutos."


# --------------------------------------------------------------------------
# PERSISTENCIA DEL OFFSET (last_update_id)
# --------------------------------------------------------------------------

def leer_ultimo_update_id() -> int:
    if not os.path.exists(LAST_UPDATE_FILE):
        return 0
    try:
        with open(LAST_UPDATE_FILE, "r", encoding="utf-8") as f:
            contenido = f.read().strip()
            return int(contenido) if contenido else 0
    except Exception as e:
        logger.warning(f"No se pudo leer {LAST_UPDATE_FILE}, se asume 0: {e}")
        return 0


def guardar_ultimo_update_id(update_id: int):
    try:
        with open(LAST_UPDATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(update_id))
        logger.info(f"Guardado last_update_id = {update_id}")
    except Exception as e:
        logger.error(f"Error guardando {LAST_UPDATE_FILE}: {e}")


# --------------------------------------------------------------------------
# BUCLE PRINCIPAL
# --------------------------------------------------------------------------

async def main():
    check_env_vars()
    logger.info(f"Iniciando TechFinder Bot - {datetime.now(timezone.utc).isoformat()}")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Verificamos que el token sea válido antes de continuar
    try:
        me = await bot.get_me()
        logger.info(f"Conectado como @{me.username}")
    except TelegramError as e:
        logger.error(f"Token de Telegram inválido o error de conexión: {e}")
        sys.exit(1)

    ultimo_update_id = leer_ultimo_update_id()
    offset = ultimo_update_id + 1 if ultimo_update_id else None
    logger.info(f"Procesando updates desde offset={offset}")

    try:
        updates = await bot.get_updates(
            offset=offset,
            timeout=POLLING_TIMEOUT_SECONDS,
            allowed_updates=["message"],
        )
    except TelegramError as e:
        logger.error(f"Error obteniendo updates de Telegram: {e}")
        sys.exit(0)  # Salimos sin error fatal; el siguiente cron lo reintentará

    if not updates:
        logger.info("No hay mensajes nuevos pendientes.")
        return

    logger.info(f"Se recibieron {len(updates)} update(s) nuevos.")
    max_update_id = ultimo_update_id

    for update in updates:
        max_update_id = max(max_update_id, update.update_id)

        if not update.message or not update.message.text:
            continue

        chat_id = update.message.chat_id
        texto = update.message.text

        if not texto.startswith("/"):
            continue  # Ignoramos mensajes que no sean comandos

        logger.info(f"Procesando comando '{texto}' del chat {chat_id}")
        respuesta = await procesar_comando(texto)

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=respuesta,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        except TelegramError as e:
            logger.error(f"Error enviando respuesta al chat {chat_id}: {e}")
            # Reintento sin Markdown por si el error es de parseo
            try:
                await bot.send_message(chat_id=chat_id, text=respuesta)
            except TelegramError as e2:
                logger.error(f"Error en el reintento sin Markdown: {e2}")

    guardar_ultimo_update_id(max_update_id)
    logger.info("Ejecución finalizada correctamente.")


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=MAX_RUNTIME_SECONDS))
    except asyncio.TimeoutError:
        logger.warning(f"Se alcanzó el límite de {MAX_RUNTIME_SECONDS}s de ejecución. Cerrando de forma segura.")
    except Exception as e:
        logger.error(f"Error fatal no controlado: {e}")
        sys.exit(1)
