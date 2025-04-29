# -*- coding: utf-8 -*-
# bot_v1.2.py

import discord
from discord.ext import commands, tasks
import os
import google.generativeai as genai
from dotenv import load_dotenv
import asyncio
import logging
import traceback
import datetime
import psycopg2 # PostgreSQL için
from psycopg2.extras import DictCursor # Satırlara sözlük gibi erişim için
from flask import Flask # Koyeb/Render için web sunucusu
import threading      # Web sunucusunu ayrı thread'de çalıştırmak için
import sys
import requests # OpenRouter için requests kütüphanesi
import json     # JSON verileri için

# --- Logging Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('discord_ai_bot')
# Flask'ın kendi loglarını biraz kısmak için
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


# .env dosyasındaki değişkenleri yükle
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# DeepSeek için artık OpenRouter anahtarını kullanacağız
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# İsteğe bağlı OpenRouter başlıkları için ortam değişkenleri
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "") # Varsayılan boş
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "Discord AI Bot") # Varsayılan isim

# API Anahtarı Kontrolleri
if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); exit()
# Artık Gemini VEYA OpenRouter anahtarı yeterli
if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
    logger.critical("HATA: Ne Gemini ne de OpenRouter API Anahtarı bulunamadı! En az biri gerekli.")
    exit()
if not GEMINI_API_KEY:
    logger.warning("UYARI: Gemini API Anahtarı bulunamadı! Gemini modelleri kullanılamayacak.")
if not OPENROUTER_API_KEY:
    logger.warning("UYARI: OpenRouter API Anahtarı bulunamadı! DeepSeek (OpenRouter üzerinden) kullanılamayacak.")
# 'requests' kütüphanesi kontrolü (genellikle gereksiz ama garanti olsun)
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.error(">>> HATA: 'requests' kütüphanesi bulunamadı. Lütfen 'pip install requests' ile kurun ve requirements.txt'e ekleyin.")
    if OPENROUTER_API_KEY: # Anahtar var ama kütüphane yoksa
        logger.error("HATA: OpenRouter API anahtarı bulundu ancak 'requests' kütüphanesi yüklenemedi. DeepSeek kullanılamayacak.")
        OPENROUTER_API_KEY = None # Yok say

# Render PostgreSQL bağlantısı için
DATABASE_URL = os.getenv("DATABASE_URL")

# Model Ön Ekleri ve Varsayılanlar
GEMINI_PREFIX = "gs:"
DEEPSEEK_OPENROUTER_PREFIX = "ds:" # DeepSeek için hala bu prefix'i kullanalım
# OpenRouter üzerinden kullanılacak TEK DeepSeek modeli
OPENROUTER_DEEPSEEK_MODEL_NAME = "deepseek/deepseek-chat" # <<<--- BURAYI KONTROL EDİN: API dokümanınızdaki tam model adını yazın (örn: "deepseek/deepseek-chat" veya "deepseek/deepseek-coder" vb.)
# VEYA eğer ücretsizse:
# OPENROUTER_DEEPSEEK_MODEL_NAME = "deepseek/deepseek-chat:free" # Dökümandaki tam adı kullanın

DEFAULT_GEMINI_MODEL_NAME = 'gemini-2.5-flash-preview-04-17' # Prefixsiz temel ad
# Varsayılan model Gemini olsun (OpenRouter sadece 1 DeepSeek modeli destekliyorsa)
DEFAULT_MODEL_NAME = f"{GEMINI_PREFIX}{DEFAULT_GEMINI_MODEL_NAME}"

# OpenRouter API Endpoint
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Varsayılan değerler (DB'den veya ortamdan okunamzsa)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Global Değişkenler ---
entry_channel_id = None
inactivity_timeout = None
# Aktif sohbet oturumları ve geçmişleri
# Yapı: channel_id -> {'model': 'prefix:model_name', 'session': GeminiSession or None, 'history': Mesaj Listesi or None}
active_ai_chats = {}
temporary_chat_channels = set()
user_to_channel_map = {}
channel_last_active = {}
user_next_model = {}
warned_inactive_channels = set()

# --- Veritabanı Yardımcı Fonksiyonları (PostgreSQL - DEĞİŞİKLİK YOK) ---
# db_connect, setup_database, save_config, load_config,
# load_all_temp_channels, add_temp_channel_db, update_channel_activity_db,
# remove_temp_channel_db, update_channel_model_db fonksiyonları aynı kalır.
# Sadece setup_database'deki default model adının hala mantıklı olduğundan emin olun.

def db_connect():
    """PostgreSQL veritabanı bağlantısı oluşturur."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL ortam değişkeni ayarlanmamış.")
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except psycopg2.DatabaseError as e:
        logger.error(f"PostgreSQL bağlantı hatası: {e}")
        raise

def setup_database():
    """PostgreSQL tablolarını oluşturur (varsa dokunmaz)."""
    conn = None
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Varsayılan model Gemini olduğu için bu kısım hala geçerli
        default_model_with_prefix_for_db = DEFAULT_MODEL_NAME
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_active TIMESTAMPTZ NOT NULL,
                model_name TEXT DEFAULT %s
            )
        ''', (default_model_with_prefix_for_db,))
        conn.commit()
        cursor.close()
        logger.info("PostgreSQL veritabanı tabloları kontrol edildi/oluşturuldu.")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.critical(f"PostgreSQL veritabanı kurulumu sırasında KRİTİK HATA: {e}")
        if conn: conn.rollback()
        exit()
    finally:
        if conn: conn.close()

def save_config(key, value):
    """Yapılandırma ayarını PostgreSQL'e kaydeder (varsa günceller)."""
    conn = None
    sql = """
        INSERT INTO config (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
    """
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(sql, (key, str(value)))
        conn.commit()
        cursor.close()
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Yapılandırma kaydedilirken PostgreSQL hatası (Key: {key}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def load_config(key, default=None):
    """Yapılandırma ayarını PostgreSQL'den yükler."""
    conn = None
    sql = "SELECT value FROM config WHERE key = %s;"
    try:
        conn = db_connect()
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute(sql, (key,))
        result = cursor.fetchone()
        cursor.close()
        return result['value'] if result else default
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Yapılandırma yüklenirken PostgreSQL hatası (Key: {key}): {e}")
        return default
    finally:
        if conn: conn.close()

def load_all_temp_channels():
    """Tüm geçici kanal durumlarını PostgreSQL'den yükler."""
    conn = None
    sql = "SELECT channel_id, user_id, last_active, model_name FROM temp_channels;"
    loaded_data = []
    try:
        conn = db_connect()
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute(sql)
        channels = cursor.fetchall()
        cursor.close()

        for row in channels:
            try:
                last_active_dt = row['last_active']
                model_name_db = row['model_name']
                # Model adı kontrolü DeepSeek prefix'ini de içermeli
                if not model_name_db or (not model_name_db.startswith(GEMINI_PREFIX) and not model_name_db.startswith(DEEPSEEK_OPENROUTER_PREFIX)):
                    logger.warning(f"DB'de geçersiz model adı bulundu (channel_id: {row['channel_id']}), varsayılana dönülüyor: {DEFAULT_MODEL_NAME}")
                    model_name_db = DEFAULT_MODEL_NAME
                    update_channel_model_db(row['channel_id'], DEFAULT_MODEL_NAME)
                # Ekstra kontrol: Eğer DeepSeek modeli ise, bilinen tek modele eşit mi diye bakılabilir (opsiyonel)
                elif model_name_db.startswith(DEEPSEEK_OPENROUTER_PREFIX) and model_name_db != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
                     logger.warning(f"DB'de eski/farklı DeepSeek modeli bulundu ({model_name_db}), OpenRouter modeline güncelleniyor.")
                     model_name_db = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
                     update_channel_model_db(row['channel_id'], model_name_db)


                loaded_data.append((row['channel_id'], row['user_id'], last_active_dt, model_name_db))
            except (ValueError, TypeError, KeyError) as row_error:
                logger.error(f"DB satırı işlenirken hata (channel_id: {row.get('channel_id', 'Bilinmiyor')}): {row_error} - Satır: {row}")
        return loaded_data
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanallar yüklenirken PostgreSQL DB hatası: {e}")
        return []
    finally:
        if conn: conn.close()

def add_temp_channel_db(channel_id, user_id, timestamp, model_used_with_prefix):
    """Yeni geçici kanalı PostgreSQL'e ekler veya günceller (ön ekli model adı ile)."""
    conn = None
    sql = """
        INSERT INTO temp_channels (channel_id, user_id, last_active, model_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            last_active = EXCLUDED.last_active,
            model_name = EXCLUDED.model_name;
    """
    try:
        conn = db_connect()
        cursor = conn.cursor()
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        # Eklenen modelin OpenRouter DeepSeek modeli olup olmadığını kontrol et (opsiyonel ama iyi pratik)
        if model_used_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and model_used_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
             logger.warning(f"DB'ye eklenirken farklı DeepSeek modeli ({model_used_with_prefix}) algılandı, OpenRouter modeline ({OPENROUTER_DEEPSEEK_MODEL_NAME}) düzeltiliyor.")
             model_used_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"

        cursor.execute(sql, (channel_id, user_id, timestamp, model_used_with_prefix))
        conn.commit()
        cursor.close()
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal PostgreSQL'e eklenirken/güncellenirken hata (channel_id: {channel_id}, model: {model_used_with_prefix}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def update_channel_activity_db(channel_id, timestamp):
    """Kanalın son aktivite zamanını PostgreSQL'de günceller."""
    conn = None
    sql = "UPDATE temp_channels SET last_active = %s WHERE channel_id = %s;"
    try:
        conn = db_connect()
        cursor = conn.cursor()
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        cursor.execute(sql, (timestamp, channel_id))
        conn.commit()
        cursor.close()
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Kanal aktivitesi PostgreSQL'de güncellenirken hata (channel_id: {channel_id}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def remove_temp_channel_db(channel_id):
    """Geçici kanalı PostgreSQL'den siler."""
    conn = None
    sql = "DELETE FROM temp_channels WHERE channel_id = %s;"
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(sql, (channel_id,))
        conn.commit()
        rowcount = cursor.rowcount
        cursor.close()
        if rowcount > 0:
            logger.info(f"Geçici kanal {channel_id} PostgreSQL veritabanından silindi.")
        else:
            logger.warning(f"Silinecek geçici kanal {channel_id} PostgreSQL'de bulunamadı.")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal PostgreSQL'den silinirken hata (channel_id: {channel_id}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def update_channel_model_db(channel_id, model_with_prefix):
     """DB'deki bir kanalın modelini günceller."""
     conn = None
     sql = "UPDATE temp_channels SET model_name = %s WHERE channel_id = %s;"
     try:
          conn = db_connect()
          cursor = conn.cursor()
          # Güncellenen modelin OpenRouter DeepSeek modeli olup olmadığını kontrol et
          if model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and model_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
               logger.warning(f"DB güncellenirken farklı DeepSeek modeli ({model_with_prefix}) algılandı, OpenRouter modeline ({OPENROUTER_DEEPSEEK_MODEL_NAME}) düzeltiliyor.")
               model_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
          cursor.execute(sql, (model_with_prefix, channel_id))
          conn.commit()
          cursor.close()
          logger.info(f"DB'deki Kanal {channel_id} modeli {model_with_prefix} olarak güncellendi.")
     except (Exception, psycopg2.DatabaseError) as e:
          logger.error(f"DB Kanal modeli güncellenirken hata (channel_id: {channel_id}): {e}")
          if conn: conn.rollback()
     finally:
          if conn: conn.close()


# --- Yapılandırma Kontrolleri (Başlangıç) ---
if not DATABASE_URL:
    logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı! Render PostgreSQL eklendi mi?")
    exit()
setup_database()

entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))

try: entry_channel_id = int(entry_channel_id_str) if entry_channel_id_str else None
except (ValueError, TypeError): logger.error(f"DB/Env'den ENTRY_CHANNEL_ID yüklenemedi: {entry_channel_id_str}."); entry_channel_id = None

try: inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str))
except (ValueError, TypeError): logger.error(f"DB/Env'den inactivity_timeout_hours yüklenemedi: {inactivity_timeout_hours_str}. Varsayılan {DEFAULT_INACTIVITY_TIMEOUT_HOURS} saat kullanılıyor."); inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)

# Gemini API'yi yapılandır (varsa) - AYNI KALIYOR
gemini_default_model_instance = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("Gemini API anahtarı yapılandırıldı.")
        try:
             gemini_default_model_instance = genai.GenerativeModel(f"models/{DEFAULT_GEMINI_MODEL_NAME}")
             logger.info(f".ask komutu için varsayılan Gemini modeli ('{DEFAULT_GEMINI_MODEL_NAME}') yüklendi.")
        except Exception as model_error:
             logger.error(f"HATA: Varsayılan Gemini modeli ('{DEFAULT_GEMINI_MODEL_NAME}') oluşturulamadı: {model_error}")
             gemini_default_model_instance = None
    except Exception as configure_error:
        logger.error(f"HATA: Gemini API genel yapılandırma hatası: {configure_error}")
        GEMINI_API_KEY = None
else:
    logger.warning("Gemini API anahtarı ayarlanmadığı için Gemini özellikleri devre dışı.")

# OpenRouter durumu zaten yukarıda kontrol edildi.

# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Yardımcı Fonksiyonlar ---

# create_private_chat_channel fonksiyonu aynı kalır
async def create_private_chat_channel(guild: discord.Guild, author: discord.Member):
    """Verilen kullanıcı için özel sohbet kanalı oluşturur ve kanal nesnesini döndürür."""
    if not guild.me.guild_permissions.manage_channels:
        logger.warning(f"'{guild.name}' sunucusunda 'Kanalları Yönet' izni eksik.")
        return None
    safe_username = "".join(c for c in author.display_name if c.isalnum() or c == ' ').strip().replace(' ', '-').lower()
    if not safe_username: safe_username = "kullanici"
    safe_username = safe_username[:80]
    base_channel_name = f"sohbet-{safe_username}"
    channel_name = base_channel_name
    counter = 1
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}

    while len(channel_name) > 100 or channel_name.lower() in existing_channel_names:
        potential_name = f"{base_channel_name[:90]}-{counter}"
        if len(potential_name) > 100: potential_name = f"{base_channel_name[:80]}-long-{counter}"
        channel_name = potential_name[:100]
        counter += 1
        if counter > 1000:
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı).")
            timestamp_str = datetime.datetime.now().strftime('%M%S%f')[:-3]
            channel_name = f"sohbet-{author.id}-{timestamp_str}"[:100]
            if channel_name.lower() in existing_channel_names:
                logger.error(f"Alternatif rastgele kanal adı '{channel_name}' de mevcut. Kanal oluşturulamıyor.")
                return None
            logger.warning(f"Alternatif rastgele kanal adı kullanılıyor: {channel_name}")
            break

    logger.info(f"Oluşturulacak kanal adı: {channel_name}")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True)
    }
    try:
        new_channel = await guild.create_text_channel(channel_name, overwrites=overwrites, reason=f"{author.name} için otomatik AI sohbet kanalı.")
        logger.info(f"Kullanıcı {author.name} ({author.id}) için '{channel_name}' (ID: {new_channel.id}) kanalı oluşturuldu.")
        return new_channel
    except discord.errors.Forbidden:
        logger.error(f"Kanal oluşturulamadı (ID: {author.id}, Kanal adı: {channel_name}): Botun 'Kanalları Yönet' izni yok.")
        return None
    except discord.errors.HTTPException as http_e:
        logger.error(f"Kanal oluşturulamadı (ID: {author.id}, Kanal adı: {channel_name}): Discord API hatası: {http_e.status} {http_e.code} - {http_e.text}")
        return None
    except Exception as e:
        logger.error(f"Kanal oluşturmada beklenmedik hata: {e}\n{traceback.format_exc()}")
        return None

async def send_to_ai_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    """Belirtilen kanalda seçili AI modeline (Gemini/DeepSeek@OpenRouter) mesaj gönderir ve yanıtlar."""
    global channel_last_active, active_ai_chats

    if not prompt_text.strip(): return False

    # --- Aktif Sohbeti Başlat veya Yükle ---
    if channel_id not in active_ai_chats:
        try:
            conn = db_connect()
            cursor = conn.cursor(cursor_factory=DictCursor)
            cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = %s", (channel_id,))
            result = cursor.fetchone()
            conn.close()

            current_model_with_prefix = result['model_name'] if result and result['model_name'] else DEFAULT_MODEL_NAME
            # Model adı kontrolü (DeepSeek için OpenRouter modelini kontrol et)
            if not current_model_with_prefix.startswith(GEMINI_PREFIX) and not current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                 logger.warning(f"DB'den geçersiz prefix'li model adı okundu ({current_model_with_prefix}), varsayılana dönülüyor.")
                 current_model_with_prefix = DEFAULT_MODEL_NAME
                 update_channel_model_db(channel_id, DEFAULT_MODEL_NAME)
            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and current_model_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
                 logger.warning(f"DB'den okunan DeepSeek modeli ({current_model_with_prefix}) OpenRouter modelinden farklı, düzeltiliyor.")
                 current_model_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
                 update_channel_model_db(channel_id, current_model_with_prefix)


            logger.info(f"'{channel.name}' (ID: {channel_id}) için AI sohbet oturumu {current_model_with_prefix} ile başlatılıyor.")

            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                # Gemini başlatma kodu aynı kalır
                if not GEMINI_API_KEY: raise ValueError("Gemini API anahtarı ayarlı değil.")
                actual_model_name = current_model_with_prefix[len(GEMINI_PREFIX):]
                target_gemini_name = f"models/{actual_model_name}"
                try:
                    await asyncio.to_thread(genai.get_model, target_gemini_name)
                    gemini_model_instance = genai.GenerativeModel(target_gemini_name)
                except Exception as model_err:
                    logger.error(f"Gemini modeli '{target_gemini_name}' yüklenemedi/bulunamadı: {model_err}. Varsayılana dönülüyor.")
                    current_model_with_prefix = DEFAULT_MODEL_NAME
                    update_channel_model_db(channel_id, DEFAULT_MODEL_NAME)
                    if not GEMINI_API_KEY: raise ValueError("Varsayılan Gemini için de API anahtarı yok.")
                    actual_model_name = DEFAULT_MODEL_NAME[len(GEMINI_PREFIX):]
                    gemini_model_instance = genai.GenerativeModel(f"models/{actual_model_name}")

                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': gemini_model_instance.start_chat(history=[]),
                    'history': None
                }
            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                # DeepSeek (OpenRouter) için sadece history listesi gerekli
                if not OPENROUTER_API_KEY: raise ValueError("OpenRouter API anahtarı ayarlı değil.")
                if not REQUESTS_AVAILABLE: raise ImportError("'requests' kütüphanesi bulunamadı.")
                # Model adının doğru olduğunu zaten yukarıda kontrol ettik
                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': None,
                    'history': [] # Boş mesaj listesi
                }
            else:
                raise ValueError(f"Tanımsız model ön eki: {current_model_with_prefix}")

        # Hata yakalama blokları (except) aynı kalır
        except (psycopg2.DatabaseError, ValueError, ImportError) as init_err:
             logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı (DB/Config/Import): {init_err}")
             try: await channel.send("Yapay zeka oturumu başlatılamadı. Veritabanı, yapılandırma veya kütüphane sorunu.", delete_after=15)
             except discord.errors.NotFound: pass
             except Exception as send_err: logger.warning(f"Oturum başlatma hata mesajı gönderilemedi: {send_err}")
             active_ai_chats.pop(channel_id, None)
             remove_temp_channel_db(channel_id)
             return False
        except Exception as e:
            logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı (Genel Hata): {e}\n{traceback.format_exc()}")
            try: await channel.send("Yapay zeka oturumu başlatılamadı. Beklenmedik bir hata oluştu.", delete_after=15)
            except discord.errors.NotFound: pass
            except Exception as send_err: logger.warning(f"Oturum başlatma hata mesajı gönderilemedi: {send_err}")
            active_ai_chats.pop(channel_id, None)
            remove_temp_channel_db(channel_id)
            return False


    # --- Sohbet Verilerini Al ---
    if channel_id not in active_ai_chats:
        logger.error(f"Kritik Hata: Kanal {channel_id} için aktif sohbet verisi bulunamadı (başlatma sonrası).")
        try: await channel.send("Sohbet durumu bulunamadı, lütfen tekrar deneyin veya kanalı kapatıp açın.", delete_after=15)
        except: pass
        return False

    chat_data = active_ai_chats[channel_id]
    current_model_with_prefix = chat_data['model']
    logger.info(f"[AI CHAT/{current_model_with_prefix}] [{author.name} @ {channel.name}] gönderiyor: {prompt_text[:100]}{'...' if len(prompt_text)>100 else ''}")

    ai_response_text = None
    error_occurred = False
    user_error_msg = "Yapay zeka ile konuşurken bir sorun oluştu."
    response_data = None # API yanıtını saklamak için (requests için)

    async with channel.typing():
        try:
            # --- API Çağrısı (Modele Göre) ---
            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                # Gemini kısmı aynı kalır
                gemini_session = chat_data.get('session')
                if not gemini_session: raise ValueError("Gemini oturumu bulunamadı.")
                response = await gemini_session.send_message_async(prompt_text)
                ai_response_text = response.text.strip()

                # Gemini güvenlik/hata kontrolü (AYNI KALIYOR)
                finish_reason = None
                try: finish_reason = response.candidates[0].finish_reason.name
                except (IndexError, AttributeError): pass
                prompt_feedback_reason = None
                try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
                except AttributeError: pass

                if prompt_feedback_reason == "SAFETY": user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı."; error_occurred = True; logger.warning(f"Gemini prompt safety block...")
                elif finish_reason == "SAFETY": user_error_msg = "Yanıt güvenlik filtrelerine takıldı."; error_occurred = True; logger.warning(f"Gemini response safety block..."); ai_response_text = None
                elif finish_reason == "RECITATION": user_error_msg = "Yanıt, alıntı filtrelerine takıldı."; error_occurred = True; logger.warning(f"Gemini response recitation block..."); ai_response_text = None
                elif finish_reason == "OTHER": user_error_msg = "Yanıt oluşturulamadı (bilinmeyen sebep)."; error_occurred = True; logger.warning(f"Gemini response 'OTHER' finish reason..."); ai_response_text = None
                elif not ai_response_text and not error_occurred: logger.warning(f"Gemini'den boş yanıt alındı, finish_reason: {finish_reason}...")


            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                # OpenRouter API'sine istek gönder
                if not OPENROUTER_API_KEY: raise ValueError("OpenRouter API anahtarı ayarlı değil.")
                if not REQUESTS_AVAILABLE: raise ImportError("'requests' kütüphanesi bulunamadı.")

                history = chat_data.get('history')
                if history is None: raise ValueError("DeepSeek (OpenRouter) geçmişi bulunamadı.")

                # OpenRouter için model adı (prefixsiz, tam ad)
                # Zaten başta kontrol ettiğimiz için direkt OPENROUTER_DEEPSEEK_MODEL_NAME kullanabiliriz.
                target_model_name = OPENROUTER_DEEPSEEK_MODEL_NAME

                # Yeni mesajı geçmişe ekle
                history.append({"role": "user", "content": prompt_text})

                # İstek başlıklarını (Headers) oluştur
                headers = {
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                }
                # İsteğe bağlı başlıkları ekle
                if OPENROUTER_SITE_URL: headers["HTTP-Referer"] = OPENROUTER_SITE_URL
                if OPENROUTER_SITE_NAME: headers["X-Title"] = OPENROUTER_SITE_NAME

                # İstek verisini (Payload) oluştur
                payload = {
                    "model": target_model_name,
                    "messages": history,
                    # "max_tokens": 1024, # İsteğe bağlı
                    # "temperature": 0.7, # İsteğe bağlı
                    # "stream": False # Stream kullanmıyoruz
                }

                # API çağrısını thread'de yap (requests senkron olduğu için)
                try:
                    api_response = await asyncio.to_thread(
                        requests.post,
                        OPENROUTER_API_URL,
                        headers=headers,
                        json=payload, # requests'te data=json.dumps() yerine json=payload kullanmak daha iyi
                        timeout=120 # Uzun yanıtlar için timeout ekleyelim (saniye)
                    )
                    # HTTP Hata kodlarını kontrol et (4xx, 5xx)
                    api_response.raise_for_status()
                    # Yanıtı JSON olarak işle
                    response_data = api_response.json()

                except requests.exceptions.Timeout:
                    logger.error("OpenRouter API isteği zaman aşımına uğradı.")
                    error_occurred = True
                    user_error_msg = "Yapay zeka sunucusundan yanıt alınamadı (zaman aşımı)."
                    if history: history.pop()
                except requests.exceptions.RequestException as e:
                    logger.error(f"OpenRouter API isteği sırasında hata: {e}")
                    # Yanıt alınabildiyse detayları logla
                    if e.response is not None:
                         logger.error(f"OpenRouter Hata Yanıt Kodu: {e.response.status_code}")
                         try: logger.error(f"OpenRouter Hata Yanıt İçeriği: {e.response.text}")
                         except: pass
                         if e.response.status_code == 401: user_error_msg = "OpenRouter API Anahtarı geçersiz veya yetki reddi."
                         elif e.response.status_code == 402: user_error_msg = "OpenRouter krediniz yetersiz."
                         elif e.response.status_code == 429: user_error_msg = "OpenRouter API kullanım limitine ulaştınız."
                         elif 400 <= e.response.status_code < 500: user_error_msg = f"OpenRouter API Hatası ({e.response.status_code}): Geçersiz istek (Model adı?, İçerik?)."
                         elif 500 <= e.response.status_code < 600: user_error_msg = f"OpenRouter API Sunucu Hatası ({e.response.status_code}). Lütfen sonra tekrar deneyin."
                    else: # Yanıt alınamayan bağlantı hataları vb.
                         user_error_msg = "OpenRouter API'sine bağlanırken bir sorun oluştu."
                    error_occurred = True
                    if history: history.pop() # Başarısız isteği geçmişten çıkar

                # Hata oluşmadıysa yanıtı işle
                if not error_occurred and response_data:
                    try:
                        if response_data.get("choices"):
                            choice = response_data["choices"][0]
                            if choice.get("message") and choice["message"].get("content"):
                                ai_response_text = choice["message"]["content"].strip()
                                # Başarılı yanıtı geçmişe ekle
                                history.append({"role": "assistant", "content": ai_response_text})

                                # finish_reason kontrolü (varsa)
                                finish_reason = choice.get("finish_reason")
                                if finish_reason == 'length':
                                    logger.warning(f"OpenRouter/DeepSeek yanıtı max_tokens sınırına ulaştı (model: {target_model_name}, Kanal: {channel_id})")
                                elif finish_reason == 'content_filter': # OpenRouter bunu destekliyor mu? Kontrol edilmeli.
                                    user_error_msg = "Yanıt içerik filtrelerine takıldı."
                                    error_occurred = True
                                    logger.warning(f"OpenRouter/DeepSeek content filter block (Kanal: {channel_id}).")
                                    if history and history[-1]["role"] == "assistant": history.pop() # Eklenen yanıtı sil
                                    if history and history[-1]["role"] == "user": history.pop() # Kullanıcıyı da sil
                                    ai_response_text = None # Yanıt yok
                                elif finish_reason != 'stop' and not ai_response_text: # Durma sebebi 'stop' değilse ve yanıt yoksa
                                    user_error_msg = f"Yanıt beklenmedik bir sebeple durdu ({finish_reason})."
                                    error_occurred = True
                                    logger.warning(f"OpenRouter/DeepSeek unexpected finish reason: {finish_reason} (Kanal: {channel_id}).")
                                    if history and history[-1]["role"] == "assistant": history.pop()
                                    if history and history[-1]["role"] == "user": history.pop()
                                    ai_response_text = None
                            else: # message veya content alanı yoksa
                                logger.warning(f"OpenRouter yanıtında 'message' veya 'content' alanı eksik. Yanıt: {response_data}")
                                user_error_msg = "Yapay zekadan geçerli bir yanıt alınamadı (eksik alanlar)."
                                error_occurred = True
                                if history: history.pop()
                        else: # choices listesi yoksa veya boşsa
                            logger.warning(f"OpenRouter yanıtında 'choices' listesi boş veya yok. Yanıt: {response_data}")
                            user_error_msg = "Yapay zekadan bir yanıt alınamadı (boş 'choices')."
                            error_occurred = True
                            if history: history.pop()
                    except (KeyError, IndexError, TypeError) as parse_error:
                        logger.error(f"OpenRouter yanıtı işlenirken hata: {parse_error}. Yanıt: {response_data}")
                        error_occurred = True
                        user_error_msg = "Yapay zeka yanıtı işlenirken bir sorun oluştu."
                        if history: history.pop()

            else: # Bilinmeyen prefix
                logger.error(f"İşlenemeyen model türü: {current_model_with_prefix}")
                user_error_msg = "Bilinmeyen bir yapay zeka modeli yapılandırılmış."
                error_occurred = True

            # --- Yanıt İşleme ve Gönderme (Aynı Kalıyor) ---
            if not error_occurred and ai_response_text:
                if len(ai_response_text) > 2000:
                    logger.info(f"Yanıt >2000kr (Kanal: {channel_id}), parçalanıyor...")
                    parts = [ai_response_text[i:i+2000] for i in range(0, len(ai_response_text), 2000)]
                    for part in parts:
                        await channel.send(part)
                        await asyncio.sleep(0.5)
                else:
                    await channel.send(ai_response_text)

                now_utc = datetime.datetime.now(datetime.timezone.utc)
                channel_last_active[channel_id] = now_utc
                update_channel_activity_db(channel_id, now_utc)
                warned_inactive_channels.discard(channel_id)
                return True
            elif not error_occurred and not ai_response_text:
                 logger.info(f"AI'dan boş yanıt alındı, mesaj gönderilmiyor (Kanal: {channel_id}).")
                 now_utc = datetime.datetime.now(datetime.timezone.utc)
                 channel_last_active[channel_id] = now_utc
                 update_channel_activity_db(channel_id, now_utc)
                 warned_inactive_channels.discard(channel_id)
                 return True

        # Genel Hata Yakalama (except blokları) - Import hatası dışında büyük ölçüde aynı kalır
        except ImportError as e:
             logger.error(f"Gerekli kütüphane bulunamadı: {e}")
             error_occurred = True
             user_error_msg = "Gerekli bir Python kütüphanesi sunucuda bulunamadı."
             active_ai_chats.pop(channel_id, None)
             remove_temp_channel_db(channel_id)
        except genai.types.StopCandidateException as stop_e:
             logger.error(f"Gemini StopCandidateException (Kanal: {channel_id}): {stop_e}")
             error_occurred = True; user_error_msg = "Gemini yanıtı beklenmedik bir şekilde durdu."
        except genai.types.BlockedPromptException as block_e:
             logger.warning(f"Gemini BlockedPromptException (Kanal: {channel_id}): {block_e}")
             error_occurred = True; user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı (BlockedPromptException)."
        except Exception as e: # Diğer tüm genel hatalar
            # Hata mesajı zaten yukarıdaki OpenRouter try-except bloğunda ayarlanmış olabilir.
            # Ayarlanmadıysa genel hatayı logla ve kullanıcıya bildir.
            if not error_occurred: # Eğer önceki bloklarda error_occurred True yapılmadıysa
                 logger.error(f"[AI CHAT/{current_model_with_prefix}] API/İşlem hatası (Kanal: {channel_id}): {type(e).__name__}: {e}")
                 logger.error(traceback.format_exc())
                 error_occurred = True
                 # user_error_msg zaten "Yapay zeka ile konuşurken bir sorun oluştu." şeklinde
                 # İsterseniz burada daha spesifik kontrol yapabilirsiniz ama requests hataları yukarıda ele alındı.

    # Hata oluştuysa kullanıcıya mesaj gönder (Aynı Kalıyor)
    if error_occurred:
        try:
            await channel.send(f"⚠️ {user_error_msg}", delete_after=20)
        except discord.errors.NotFound: pass
        except Exception as send_err: logger.warning(f"Hata mesajı gönderilemedi (Kanal: {channel_id}): {send_err}")
        return False

    return False

# --- Bot Olayları ---

# on_ready fonksiyonu aynı kalır
@bot.event
async def on_ready():
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active
    logger.info(f'{bot.user} olarak giriş yapıldı (ID: {bot.user.id}).')
    logger.info(f"Mevcut Ayarlar - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")
    logger.info("Kalıcı veriler (geçici kanallar) yükleniyor...");
    temporary_chat_channels.clear(); user_to_channel_map.clear(); channel_last_active.clear(); active_ai_chats.clear(); warned_inactive_channels.clear()
    loaded_channels = load_all_temp_channels()
    valid_channel_count = 0; invalid_channel_ids = []; guild_ids = {g.id for g in bot.guilds}
    for ch_id, u_id, last_active_ts, ch_model_name_with_prefix in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel) and channel_obj.guild.id in guild_ids:
            temporary_chat_channels.add(ch_id); user_to_channel_map[u_id] = ch_id; channel_last_active[ch_id] = last_active_ts; valid_channel_count += 1
        else:
            reason = "Discord'da bulunamadı/geçersiz"
            if channel_obj and channel_obj.guild.id not in guild_ids: reason = f"Bot artık '{channel_obj.guild.name}' sunucusunda değil"
            elif not channel_obj: reason = "Discord'da bulunamadı"
            logger.warning(f"DB'deki geçici kanal {ch_id} yüklenemedi ({reason}). DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)
    for invalid_id in invalid_channel_ids: remove_temp_channel_db(invalid_id)
    logger.info(f"{valid_channel_count} geçerli geçici kanal DB'den yüklendi.")
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")
    entry_channel_name = "Ayarlanmadı"
    try:
        if entry_channel_id:
            entry_channel = await bot.fetch_channel(entry_channel_id)
            if entry_channel: entry_channel_name = f"#{entry_channel.name}"
            else: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı veya erişilemiyor.")
        activity_text = f"Sohbet için {entry_channel_name}"
        await bot.change_presence(activity=discord.Game(name=activity_text))
        logger.info(f"Bot aktivitesi ayarlandı: '{activity_text}'")
    except discord.errors.NotFound:
        logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı (aktivite ayarlanırken).")
        await bot.change_presence(activity=discord.Game(name="Sohbet için kanal?"))
    except Exception as e: logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")
    if not check_inactivity.is_running(): check_inactivity.start(); logger.info("İnaktivite kontrol görevi başlatıldı.")
    logger.info("Bot komutları ve mesajları dinliyor..."); print("-" * 20)


# on_message fonksiyonu, ilk mesajı yeni kanala gönderme eklemesiyle birlikte aynı kalır
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or message.author.bot: return
    if not message.guild: return
    if not isinstance(message.channel, discord.TextChannel): return

    author = message.author; author_id = author.id
    channel = message.channel; channel_id = channel.id
    guild = message.guild

    ctx = await bot.get_context(message)
    if ctx.command: await bot.process_commands(message); return

    if entry_channel_id and channel_id == entry_channel_id:
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]
            active_channel = bot.get_channel(active_channel_id)
            if active_channel:
                 mention = active_channel.mention
                 logger.info(f"{author.name} giriş kanalına yazdı ama aktif kanalı var: {mention}")
                 try:
                     info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15)
                     await message.delete(delay=15)
                 except discord.errors.NotFound: pass
                 except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
                 return
            else:
                 logger.warning(f"{author.name} için map'te olan kanal ({active_channel_id}) bulunamadı. Map temizleniyor.")
                 user_to_channel_map.pop(author_id, None)
                 remove_temp_channel_db(active_channel_id)

        initial_prompt = message.content
        original_message_id = message.id
        if not initial_prompt.strip():
             logger.info(f"{author.name} giriş kanalına boş mesaj gönderdi, yoksayılıyor.")
             try: await message.delete()
             except: pass
             return

        # Model seçimi (DeepSeek için sadece tek model olduğunu unutma)
        chosen_model_with_prefix = user_next_model.pop(author_id, DEFAULT_MODEL_NAME)
        # Eğer seçilen model DeepSeek ise, belirli OpenRouter modeline ayarla
        if chosen_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
             chosen_model_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
             logger.info(f"{author.name} için DeepSeek seçildi, OpenRouter modeline ({chosen_model_with_prefix}) ayarlandı.")
        logger.info(f"{author.name} giriş kanalına yazdı, {chosen_model_with_prefix} ile kanal oluşturuluyor...")


        try: processing_msg = await channel.send(f"{author.mention}, özel sohbet kanalın oluşturuluyor...", delete_after=15)
        except Exception as e: logger.warning(f"Giriş kanalına 'işleniyor' mesajı gönderilemedi: {e}"); processing_msg = None

        new_channel = await create_private_chat_channel(guild, author)

        if new_channel:
            new_channel_id = new_channel.id
            temporary_chat_channels.add(new_channel_id)
            user_to_channel_map[author_id] = new_channel_id
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[new_channel_id] = now_utc
            # DB'ye eklerken doğru model adının eklendiğinden emin ol (add_temp_channel_db içinde kontrol var)
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_with_prefix)

            # Hoşgeldin Embed'i
            display_model_name = chosen_model_with_prefix.split(':')[-1] # Kullanıcıya gösterilecek isim
            try:
                 embed = discord.Embed(title="👋 Özel Yapay Zeka Sohbeti Başlatıldı!", description=(f"Merhaba {author.mention}!\n\n" f"Bu kanalda `{display_model_name}` modeli ile sohbet edeceksin."), color=discord.Color.og_blurple())
                 embed.set_thumbnail(url=bot.user.display_avatar.url)
                 timeout_hours_display = "Asla"
                 if inactivity_timeout: timeout_hours_display = f"`{inactivity_timeout.total_seconds() / 3600:.1f}` saat"
                 embed.add_field(name="⏳ Otomatik Kapanma", value=f"Kanal {timeout_hours_display} işlem görmezse otomatik olarak silinir.", inline=False)
                 prefix = bot.command_prefix[0]
                 embed.add_field(name="🛑 Kapat", value=f"`{prefix}endchat`", inline=True)
                 embed.add_field(name="🔄 Model Seç (Sonraki)", value=f"`{prefix}setmodel <model>`", inline=True)
                 embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{prefix}resetchat`", inline=True)
                 await new_channel.send(embed=embed)
            except Exception as e:
                 logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                 try: await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.")
                 except Exception as fallback_e: logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")

            # Kullanıcının ilk mesajını yeni kanala gönder
            try:
                user_message_format = f"**{author.display_name}:** {initial_prompt}"
                await new_channel.send(user_message_format)
                logger.info(f"Kullanıcının ilk mesajı ({original_message_id}) yeni kanal {new_channel_id}'ye gönderildi.")
            except discord.errors.Forbidden: logger.warning(f"Yeni kanala ({new_channel_id}) kullanıcının ilk mesajı gönderilemedi: İzin yok.")
            except Exception as e: logger.warning(f"Yeni kanala ({new_channel_id}) kullanıcının ilk mesajı gönderilirken hata: {e}")

            # Kullanıcıya bilgi ver (Giriş kanalında)
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            try: await channel.send(f"{author.mention}, özel sohbet kanalı {new_channel.mention} oluşturuldu! Oradan devam edebilirsin.", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            # AI'ye ilk isteği gönder
            try:
                logger.info(f"-----> AI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_with_prefix})")
                success = await send_to_ai_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success: logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else: logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e: logger.error(f"İlk mesaj işlenirken/gönderilirken hata: {e}")

            # Orijinal mesajı sil (Giriş kanalından)
            try:
                entry_channel_message = discord.Object(id=original_message_id)
                await channel.delete_messages([entry_channel_message])
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except discord.errors.NotFound: pass
            except discord.errors.Forbidden: logger.warning(f"Giriş kanalında ({channel_id}) mesaj silme izni yok.")
            except Exception as e: logger.warning(f"Giriş kanalındaki orijinal mesaj ({original_message_id}) silinirken hata: {e}")
        else: # new_channel None ise
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            try: await channel.send(f"{author.mention}, üzgünüm, özel kanal oluşturulamadı. İzinleri veya Discord API durumunu kontrol edin.", delete_after=20)
            except: pass
            try: await message.delete(delay=20)
            except: pass
        return

    # --- Geçici Sohbet Kanallarındaki Mesajlar ---
    if channel_id in temporary_chat_channels and not message.author.bot:
        prompt_text = message.content
        await send_to_ai_and_respond(channel, author, prompt_text, channel_id)


# --- Arka Plan Görevi: İnaktivite Kontrolü ---
# check_inactivity, before_check_inactivity, on_guild_channel_delete fonksiyonları aynı kalır
@tasks.loop(minutes=5)
async def check_inactivity():
    global warned_inactive_channels
    if inactivity_timeout is None: return
    now = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []; channels_to_warn = []
    warning_delta = min(datetime.timedelta(minutes=10), inactivity_timeout * 0.1)
    warning_threshold = inactivity_timeout - warning_delta
    last_active_copy = channel_last_active.copy()
    for channel_id, last_active_time in last_active_copy.items():
        if channel_id not in temporary_chat_channels:
             logger.warning(f"İnaktivite kontrol: {channel_id} `channel_last_active` içinde ama `temporary_chat_channels` içinde değil. State tutarsızlığı, temizleniyor.")
             channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); active_ai_chats.pop(channel_id, None)
             user_id_to_remove = None
             for user_id, ch_id in list(user_to_channel_map.items()):
                 if ch_id == channel_id: user_id_to_remove = user_id; break
             if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
             remove_temp_channel_db(channel_id)
             continue
        if not isinstance(last_active_time, datetime.datetime): logger.error(f"İnaktivite kontrolü: Kanal {channel_id} için geçersiz last_active_time tipi ({type(last_active_time)}). Atlanıyor."); continue
        if last_active_time.tzinfo is None: logger.warning(f"İnaktivite kontrolü: Kanal {channel_id} için timezone bilgisi olmayan last_active_time ({last_active_time}). UTC varsayılıyor."); last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)
        try: time_inactive = now - last_active_time
        except TypeError as te: logger.error(f"İnaktivite süresi hesaplanırken hata (Kanal: {channel_id}, Now: {now}, LastActive: {last_active_time}): {te}"); continue
        if time_inactive > inactivity_timeout: channels_to_delete.append(channel_id)
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels: channels_to_warn.append(channel_id)
    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                current_last_active = channel_last_active.get(channel_id)
                if not current_last_active: continue
                if current_last_active.tzinfo is None: current_last_active = current_last_active.replace(tzinfo=datetime.timezone.utc)
                remaining_time = inactivity_timeout - (now - current_last_active)
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir. Devam etmek için mesaj yazın.", delete_after=300)
                warned_inactive_channels.add(channel_id); logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id} ({channel_obj.name})")
            except discord.errors.NotFound: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Kanal bulunamadı."); warned_inactive_channels.discard(channel_id); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]; (user_to_channel_map.pop(user_id[0], None) if user_id else None); remove_temp_channel_db(channel_id)
            except discord.errors.Forbidden: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Mesaj gönderme izni yok."); warned_inactive_channels.add(channel_id)
            except Exception as e: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else: logger.warning(f"İnaktivite uyarısı için kanal {channel_id} Discord'da bulunamadı."); warned_inactive_channels.discard(channel_id); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]; (user_to_channel_map.pop(user_id[0], None) if user_id else None); remove_temp_channel_db(channel_id)
    if channels_to_delete:
        logger.info(f"İnaktivite: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id); reason = "İnaktivite nedeniyle otomatik silindi."
            if channel_to_delete:
                channel_name_log = channel_to_delete.name
                try:
                    await channel_to_delete.delete(reason=reason); logger.info(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) başarıyla silindi.")
                    temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id)
                except discord.errors.NotFound: logger.warning(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken bulunamadı. State zaten temizlenmiş olabilir veya manuel temizleniyor."); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); user_id_to_remove = None; [user_to_channel_map.pop(user_id, None) for user_id, ch_id in list(user_to_channel_map.items()) if ch_id == channel_id]; remove_temp_channel_db(channel_id)
                except discord.errors.Forbidden: logger.error(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinemedi: 'Kanalları Yönet' izni yok."); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); user_id_to_remove = None; [user_to_channel_map.pop(user_id, None) for user_id, ch_id in list(user_to_channel_map.items()) if ch_id == channel_id]; remove_temp_channel_db(channel_id)
                except Exception as e: logger.error(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken hata: {e}\n{traceback.format_exc()}"); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); user_id_to_remove = None; [user_to_channel_map.pop(user_id, None) for user_id, ch_id in list(user_to_channel_map.items()) if ch_id == channel_id]; remove_temp_channel_db(channel_id)
            else: logger.warning(f"İnaktif kanal (ID: {channel_id}) Discord'da bulunamadı. DB'den ve state'den siliniyor."); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); user_id_to_remove = None; [user_to_channel_map.pop(user_id, None) for user_id, ch_id in list(user_to_channel_map.items()) if ch_id == channel_id]; remove_temp_channel_db(channel_id)
            warned_inactive_channels.discard(channel_id)

@check_inactivity.before_loop
async def before_check_inactivity(): await bot.wait_until_ready(); logger.info("Bot hazır, inaktivite kontrol döngüsü başlıyor.")

@bot.event
async def on_guild_channel_delete(channel):
    channel_id = channel.id
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) silindi (Discord Event), ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id: user_id_to_remove = user_id; break
        if user_id_to_remove:
            removed_channel_id = user_to_channel_map.pop(user_id_to_remove, None)
            if removed_channel_id: logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi (Silinen Kanal ID: {removed_channel_id}).")
            else: logger.warning(f"Silinen kanal {channel_id} için kullanıcı {user_id_to_remove} haritadan çıkarılamadı (zaten yok?).")
        else: logger.warning(f"Silinen geçici kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")
        remove_temp_channel_db(channel_id)

# --- Komutlar ---

# endchat, resetchat, clear komutları aynı kalır
@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx: commands.Context):
    channel_id = ctx.channel.id; author_id = ctx.author.id
    is_temp_channel = False; expected_user_id = None
    if channel_id in temporary_chat_channels:
        is_temp_channel = True
        for user_id, ch_id in user_to_channel_map.items():
            if ch_id == channel_id: expected_user_id = user_id; break
    if not is_temp_channel or expected_user_id is None:
        conn = None
        try:
            conn = db_connect(); cursor = conn.cursor(cursor_factory=DictCursor); cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,)); owner_row = cursor.fetchone(); cursor.close()
            if owner_row:
                is_temp_channel = True; db_user_id = owner_row['user_id']
                if expected_user_id is None: expected_user_id = db_user_id
                elif expected_user_id != db_user_id: logger.warning(f".endchat: Kanal {channel_id} için state sahibi ({expected_user_id}) ile DB sahibi ({db_user_id}) farklı! DB sahibine öncelik veriliyor."); expected_user_id = db_user_id
                if channel_id not in temporary_chat_channels: temporary_chat_channels.add(channel_id)
                if expected_user_id not in user_to_channel_map or user_to_channel_map[expected_user_id] != channel_id: user_to_channel_map[expected_user_id] = channel_id
            else:
                 if not is_temp_channel: await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10); await ctx.message.delete(delay=10); return
        except (Exception, psycopg2.DatabaseError) as e: logger.error(f".endchat DB kontrol hatası (channel_id: {channel_id}): {e}"); await ctx.send("Kanal bilgisi kontrol edilirken bir hata oluştu.", delete_after=10); await ctx.message.delete(delay=10); return
        finally:
            if conn: conn.close()
    if expected_user_id and author_id != expected_user_id: owner = ctx.guild.get_member(expected_user_id); owner_name = f"<@{expected_user_id}>" if not owner else owner.mention; await ctx.send(f"Bu kanalı sadece oluşturan kişi ({owner_name}) kapatabilir.", delete_after=10); await ctx.message.delete(delay=10); return
    elif not expected_user_id: logger.error(f".endchat: Kanal {channel_id} sahibi (state veya DB'de) bulunamadı! Yine de silmeye çalışılıyor."); await ctx.send("Kanal sahibi bilgisi bulunamadı. Kapatma işlemi yapılamıyor.", delete_after=10); await ctx.message.delete(delay=10); return
    if not ctx.guild.me.guild_permissions.manage_channels: await ctx.send("Kanalları yönetme iznim yok, bu yüzden kanalı silemiyorum.", delete_after=10); return
    try:
        channel_name_log = ctx.channel.name; logger.info(f"Kanal '{channel_name_log}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
    except discord.errors.NotFound: logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı..."); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); (user_to_channel_map.pop(expected_user_id, None) if expected_user_id else None); remove_temp_channel_db(channel_id)
    except discord.errors.Forbidden: logger.error(f"Kanal '{ctx.channel.name}' (ID: {channel_id}) manuel silinemedi: 'Kanalları Yönet' izni yok."); await ctx.send("Kanalları yönetme iznim yok, bu yüzden kanalı silemiyorum.", delete_after=10)
    except Exception as e: logger.error(f".endchat komutunda kanal silinirken hata: {e}\n{traceback.format_exc()}"); await ctx.send("Kanal silinirken bir hata oluştu.", delete_after=10); temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); (user_to_channel_map.pop(expected_user_id, None) if expected_user_id else None); remove_temp_channel_db(channel_id)

@bot.command(name='resetchat', aliases=['sıfırla'])
@commands.guild_only()
async def reset_chat_session(ctx: commands.Context):
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels: await ctx.send("Bu komut sadece aktif geçici sohbet kanallarında kullanılabilir.", delete_after=10); await ctx.message.delete(delay=10); return
    if channel_id in active_ai_chats: active_ai_chats.pop(channel_id, None); logger.info(f"Sohbet geçmişi/oturumu {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı."); await ctx.send("✅ Konuşma geçmişi/oturumu sıfırlandı. Bir sonraki mesajınızla yeni bir oturum başlayacak.", delete_after=15)
    else: logger.info(f"Sıfırlanacak aktif oturum/geçmiş yok: Kanal {channel_id}"); await ctx.send("✨ Şu anda sıfırlanacak aktif bir konuşma geçmişi/oturumu bulunmuyor. Zaten temiz.", delete_after=10)
    try: 
        await ctx.message.delete(delay=15); 
    except: 
        pass

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: str = None):
    if not ctx.channel.permissions_for(ctx.guild.me).manage_messages: await ctx.send("Mesajları silebilmem için bu kanalda 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10); return
    if amount is None: await ctx.send(f"Silinecek mesaj sayısı (`{ctx.prefix}clear 5`) veya tümü için `{ctx.prefix}clear all` yazın.", delete_after=10); await ctx.message.delete(delay=10); return
    deleted_count = 0; original_command_message_id = ctx.message.id
    try:
        if amount.lower() == 'all':
            status_msg = await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)... Lütfen bekleyin.", delete_after=15); await ctx.message.delete()
            while True:
                try:
                    deleted_messages = await ctx.channel.purge(limit=100, check=lambda m: not m.pinned, bulk=True)
                    if not deleted_messages: break; deleted_count += len(deleted_messages)
                    if len(deleted_messages) < 100: break
                    await asyncio.sleep(1)
                except discord.errors.NotFound: break
                except discord.errors.HTTPException as http_e:
                     if http_e.status == 429: retry_after = float(http_e.response.headers.get('Retry-After', 1)); logger.warning(f".clear 'all' rate limited. Retrying after {retry_after}s"); await status_msg.edit(content=f"Rate limit! {retry_after:.1f} saniye bekleniyor...", delete_after=retry_after + 2); await asyncio.sleep(retry_after); continue
                     else: raise
            await status_msg.edit(content=f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi (sabitlenmişler hariç).", delete_after=10)
        else:
            try:
                limit = int(amount)
                if limit <= 0: raise ValueError("Sayı pozitif olmalı")
                if limit > 500: limit = 500; await ctx.send("Tek seferde en fazla 500 mesaj silebilirsiniz.", delete_after=5)
                deleted_messages = await ctx.channel.purge(limit=limit, check=lambda m: not m.pinned, before=ctx.message, bulk=True)
                actual_deleted_count = len(deleted_messages); await ctx.message.delete()
                msg = f"{actual_deleted_count} mesaj silindi (sabitlenmişler hariç)."
                await ctx.send(msg, delete_after=7)
            except ValueError: await ctx.send(f"Geçersiz sayı: '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10); await ctx.message.delete(delay=10)
    except discord.errors.Forbidden: logger.error(f"HATA: '{ctx.channel.name}' kanalında mesaj silme izni yok (Bot için)!"); await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10); await ctx.message.delete(delay=10)
    except Exception as e: logger.error(f".clear hatası: {e}\n{traceback.format_exc()}"); await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10); await ctx.message.delete(delay=10)

# ask komutu sadece Gemini için çalışır, aynı kalır
@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def ask_in_channel(ctx: commands.Context, *, question: str = None):
    if not gemini_default_model_instance:
        if not GEMINI_API_KEY: user_msg = "⚠️ Gemini API anahtarı ayarlanmadığı için bu komut kullanılamıyor."
        else: user_msg = "⚠️ Varsayılan Gemini modeli yüklenemedi. Bot loglarını kontrol edin."
        await ctx.reply(user_msg, delete_after=15); await ctx.message.delete(delay=15); return
    if question is None or not question.strip():
        await ctx.reply(f"Lütfen bir soru sorun (örn: `{ctx.prefix}ask Evren nasıl oluştu?`).", delete_after=15); await ctx.message.delete(delay=15); return
    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None
    try:
        async with ctx.typing():
            try: response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e: logger.error(f".ask için Gemini API hatası: {type(gemini_e).__name__}: {gemini_e}"); user_msg = "..."; await ctx.reply(f"⚠️ {user_msg}", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            gemini_response_text = ""; finish_reason = None; prompt_feedback_reason = None
            try: gemini_response_text = response.text.strip()
            except ValueError as ve: logger.warning(f".ask Gemini yanıtını okurken hata: {ve}..."); gemini_response_text = ""
            except Exception as text_err: logger.error(f".ask Gemini response.text okuma hatası: {text_err}"); gemini_response_text = ""
            try: finish_reason = response.candidates[0].finish_reason.name
            except (IndexError, AttributeError): pass
            try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
            except AttributeError: pass
            if prompt_feedback_reason == "SAFETY": await ctx.reply("...", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            elif finish_reason == "SAFETY": await ctx.reply("...", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            elif finish_reason == "RECITATION": await ctx.reply("...", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            elif finish_reason == "OTHER": await ctx.reply("...", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            elif not gemini_response_text and finish_reason != "STOP": await ctx.reply("...", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
            elif not gemini_response_text: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); return
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)
        question_display = question if len(question) <= 1024 else question[:1021] + "..."; embed.add_field(name="Soru", value=question_display, inline=False)
        response_display = gemini_response_text if len(gemini_response_text) <= 1024 else gemini_response_text[:1021] + "..."; embed.add_field(name="Yanıt", value=response_display, inline=False)
        footer_text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik silinecektir."; (footer_text := footer_text + " (Yanıt kısaltıldı)") if len(gemini_response_text) > 1024 else None; embed.set_footer(text=footer_text)
        bot_response_message = await ctx.reply(embed=embed, mention_author=False); logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")
        await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        if bot_response_message: await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY)
    except Exception as e: logger.error(f".ask genel hatası: {e}\n{traceback.format_exc()}"); await ctx.reply("Sorunuz işlenirken beklenmedik bir hata oluştu.", delete_after=15); await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)

@ask_in_channel.error
async def ask_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown): delete_delay = max(5, int(error.retry_after) + 1); await ctx.send(f"⏳ `.ask` komutu için beklemedesiniz. Lütfen **{error.retry_after:.1f} saniye** sonra tekrar deneyin.", delete_after=delete_delay); await ctx.message.delete(delay=delete_delay)
    else: pass

# listmodels komutu güncellendi
@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def list_available_models(ctx: commands.Context):
    """Sohbet için kullanılabilir Gemini ve DeepSeek (OpenRouter) modellerini listeler."""
    status_msg = await ctx.send("Kullanılabilir modeller kontrol ediliyor...", delete_after=5)

    async def fetch_gemini(): # Gemini kısmı aynı
        if not GEMINI_API_KEY: return ["_(Gemini API anahtarı ayarlı değil)_"]
        try:
            gemini_models_list = []
            gemini_models = await asyncio.to_thread(genai.list_models)
            for m in gemini_models:
                if 'generateContent' in m.supported_generation_methods and m.name.startswith("models/"):
                    model_id = m.name.split('/')[-1]
                    prefix = "";
                    if "gemini-1.5-flash" in model_id: prefix = "⚡ "
                    elif "gemini-1.5-pro" in model_id: prefix = "✨ "
                    elif "gemini-pro" == model_id and "vision" not in model_id: prefix = "✅ "
                    elif "aqa" in model_id: prefix="❓ "
                    gemini_models_list.append(f"{GEMINI_PREFIX}{prefix}`{model_id}`")
            gemini_models_list.sort(key=lambda x: x.split('`')[1])
            return gemini_models_list if gemini_models_list else ["_(Kullanılabilir Gemini modeli bulunamadı)_"]
        except Exception as e: logger.error(f"Gemini modelleri listelenirken hata: {e}"); return ["_(Gemini modelleri alınamadı - API Hatası)_"]

    async def fetch_deepseek_openrouter():
        # Sadece OpenRouter üzerinden bilinen modeli listele
        if not OPENROUTER_API_KEY: return ["_(OpenRouter API anahtarı ayarlı değil)_"]
        if not REQUESTS_AVAILABLE: return ["_('requests' kütüphanesi bulunamadı)_"]
        # Sadece tek model olduğu için direkt listeye ekle
        # Kullanıcıya gösterilecek isim yine de prefix + model adı olsun
        return [f"{DEEPSEEK_OPENROUTER_PREFIX}🧭 `{OPENROUTER_DEEPSEEK_MODEL_NAME}`"]

    results = await asyncio.gather(fetch_gemini(), fetch_deepseek_openrouter())
    all_models_list = []
    all_models_list.extend(results[0])
    all_models_list.extend(results[1])

    valid_models = [m for m in all_models_list if not m.startswith("_(")]
    error_models = [m for m in all_models_list if m.startswith("_(")]

    if not valid_models:
        error_text = "\n".join(error_models) if error_models else "API anahtarları ayarlanmamış veya bilinmeyen bir sorun var."
        await ctx.send(f"Kullanılabilir model bulunamadı.\n{error_text}"); await status_msg.delete(); return

    embed = discord.Embed(
        title="🤖 Kullanılabilir Yapay Zeka Modelleri",
        description=f"Bir sonraki özel sohbetiniz için `{ctx.prefix}setmodel <model_adi_on_ekli>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(valid_models),
        color=discord.Color.gold()
    )
    # DeepSeek prefix'ini güncelle
    embed.add_field(name="Ön Ekler", value=f"`{GEMINI_PREFIX}` Gemini Modeli\n`{DEEPSEEK_OPENROUTER_PREFIX}` DeepSeek Modeli (OpenRouter)", inline=False)
    embed.set_footer(text="⚡ Flash, ✨ Pro (Gemini), ✅ Eski Pro (Gemini), 🧭 DeepSeek (OpenRouter), ❓ AQA (Gemini)")
    if error_models: footer_text = embed.footer.text + "\nUyarılar: " + " ".join(error_models); embed.set_footer(text=footer_text[:1024])

    await status_msg.delete(); await ctx.send(embed=embed); await ctx.message.delete()

# setmodel komutu güncellendi
@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx: commands.Context, *, model_id_with_or_without_prefix: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini veya DeepSeek (OpenRouter) modelini ayarlar."""
    global user_next_model
    if model_id_with_or_without_prefix is None:
        await ctx.send(f"Lütfen bir model adı belirtin (örn: `{GEMINI_PREFIX}gemini-1.5-flash-latest` veya `{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}`). Modeller için `{ctx.prefix}listmodels`.", delete_after=15)
        await ctx.message.delete(delay=15); return

    model_input = model_id_with_or_without_prefix.strip().replace('`', '')
    selected_model_full_name = None
    is_valid = False
    error_message = None

    if not model_input.startswith(GEMINI_PREFIX) and not model_input.startswith(DEEPSEEK_OPENROUTER_PREFIX):
         await ctx.send(f"❌ Lütfen model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_OPENROUTER_PREFIX}` ön ekini ekleyin. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=20)
         await ctx.message.delete(delay=20); return

    async with ctx.typing():
        if model_input.startswith(GEMINI_PREFIX):
            # Gemini doğrulama kısmı aynı
            if not GEMINI_API_KEY: error_message = f"❌ Gemini API anahtarı ayarlı değil."; is_valid = False
            else:
                actual_model_name = model_input[len(GEMINI_PREFIX):]
                if not actual_model_name: error_message = "❌ Lütfen bir Gemini model adı belirtin."; is_valid = False
                else:
                    target_gemini_name = f"models/{actual_model_name}"
                    try: await asyncio.to_thread(genai.get_model, target_gemini_name); selected_model_full_name = model_input; is_valid = True; logger.info(f"{ctx.author.name} Gemini modelini doğruladı: {target_gemini_name}")
                    except Exception as e: logger.warning(f"Geçersiz Gemini modeli denendi ({target_gemini_name}): {e}"); error_message = f"❌ `{actual_model_name}` geçerli veya erişilebilir bir Gemini modeli değil."; is_valid = False

        elif model_input.startswith(DEEPSEEK_OPENROUTER_PREFIX):
            # OpenRouter için anahtar ve kütüphane kontrolü
            if not OPENROUTER_API_KEY: error_message = f"❌ OpenRouter API anahtarı ayarlı değil."; is_valid = False
            elif not REQUESTS_AVAILABLE: error_message = f"❌ DeepSeek (OpenRouter) için gerekli 'requests' kütüphanesi bulunamadı."; is_valid = False
            else:
                # Sadece bilinen tek OpenRouter DeepSeek modelini kabul et
                expected_full_name = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
                if model_input == expected_full_name:
                    selected_model_full_name = model_input
                    is_valid = True
                    logger.info(f"{ctx.author.name} DeepSeek (OpenRouter) modelini ayarladı: {OPENROUTER_DEEPSEEK_MODEL_NAME}")
                else:
                    error_message = f"❌ Bu API anahtarı ile sadece `{expected_full_name}` modeli kullanılabilir."
                    is_valid = False

    if is_valid and selected_model_full_name:
        user_next_model[ctx.author.id] = selected_model_full_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{selected_model_full_name}' olarak ayarlandı.")
        reply_msg = f"✅ Başarılı! Bir sonraki özel sohbetiniz `{selected_model_full_name}` modeli ile başlayacak."
        await ctx.send(reply_msg, delete_after=20)
    else:
        final_error_msg = error_message if error_message else f"❌ `{model_input}` geçerli bir model adı değil veya bir sorun oluştu."
        await ctx.send(f"{final_error_msg} `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)

    try: 
        await ctx.message.delete(delay=20)
    except: 
        pass

# setentrychannel, settimeout komutları aynı kalır
@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_entry_channel(ctx: commands.Context, channel: discord.TextChannel = None):
    global entry_channel_id
    if channel is None: current_entry_channel_mention = "Ayarlanmamış"; await ctx.send(f"..."); return
    perms = channel.permissions_for(ctx.guild.me)
    if not perms.view_channel or not perms.send_messages or not perms.manage_messages: await ctx.send(f"❌ ..."); return
    entry_channel_id = channel.id; save_config('entry_channel_id', entry_channel_id); logger.info(f"Giriş kanalı yönetici {ctx.author.name} tarafından {channel.mention} (ID: {channel.id}) olarak ayarlandı."); await ctx.send(f"✅ Giriş kanalı başarıyla {channel.mention} olarak ayarlandı.")
    try: await bot.change_presence(activity=discord.Game(name=f"Sohbet için #{channel.name}"))
    except Exception as e: logger.warning(f"Giriş kanalı ayarlandıktan sonra bot aktivitesi güncellenemedi: {e}")

@bot.command(name='settimeout', aliases=['zamanasimi'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_inactivity_timeout(ctx: commands.Context, hours: str = None):
    global inactivity_timeout; current_timeout_hours = 'Kapalı'
    if inactivity_timeout: current_timeout_hours = f"{inactivity_timeout.total_seconds()/3600:.2f}"
    if hours is None: await ctx.send(f"Lütfen pozitif bir saat değeri girin (örn: `{ctx.prefix}settimeout 2.5`) veya kapatmak için `0` yazın.\nMevcut: `{current_timeout_hours}` saat"); return
    try:
        hours_float = float(hours)
        if hours_float < 0: await ctx.send("Lütfen pozitif bir saat değeri veya `0` girin."); return
        if hours_float == 0: inactivity_timeout = None; save_config('inactivity_timeout_hours', '0'); logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından kapatıldı."); await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **kapatıldı**.")
        elif hours_float < 0.1: await ctx.send("Minimum zaman aşımı 0.1 saattir (6 dakika). Kapatmak için 0 girin."); return
        elif hours_float > 720: await ctx.send("Maksimum zaman aşımı 720 saattir (30 gün)."); return
        else: inactivity_timeout = datetime.timedelta(hours=hours_float); save_config('inactivity_timeout_hours', str(hours_float)); logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours_float} saat olarak ayarlandı."); await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **{hours_float:.2f} saat** olarak ayarlandı.")
    except ValueError: await ctx.send(f"Geçersiz saat değeri: '{hours}'. Lütfen sayısal bir değer girin (örn: 1, 0.5, 0).")

# commandlist (yardım) komutu aynı kalır, sadece DeepSeek açıklamasını güncelleyebiliriz.
@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx: commands.Context):
    entry_channel_mention = "Ayarlanmamış"
    if entry_channel_id:
        try: entry_channel = await bot.fetch_channel(entry_channel_id); entry_channel_mention = entry_channel.mention if entry_channel else f"ID: {entry_channel_id} (Bulunamadı/Erişilemiyor)"
        except Exception as e: logger.warning(f"Yardım komutunda giriş kanalı alınırken hata: {e}"); entry_channel_mention = f"ID: {entry_channel_id} (Hata)"
    embed = discord.Embed(title=f"{bot.user.name} Komut Listesi", description=f"**Özel Sohbet Başlatma:**\n{entry_channel_mention} kanalına mesaj yazın.\n\n**Diğer Komutlar:**", color=discord.Color.dark_purple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    user_cmds, chat_cmds, admin_cmds = [], [], []
    for command in sorted(bot.commands, key=lambda cmd: cmd.name):
        if command.hidden: continue
        help_text = command.help or command.short_doc or "Açıklama yok."; help_text = help_text.split('\n')[0]
        aliases = f" (Diğer: `{'`, `'.join(command.aliases)}`)" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""; cmd_string = f"`{ctx.prefix}{command.name}{params}`{aliases}\n_{help_text}_"
        is_admin_cmd = any(isinstance(check, type(commands.has_permissions)) or isinstance(check, type(commands.is_owner)) for check in command.checks)
        is_super_admin_cmd = command.name in ['setentrychannel', 'settimeout']
        if is_super_admin_cmd: admin_cmds.append(cmd_string)
        elif command.name in ['endchat', 'resetchat', 'clear'] and command.cog is None:
             if command.name == 'clear' and is_admin_cmd: admin_cmds.append(cmd_string)
             else: chat_cmds.append(cmd_string)
        elif is_admin_cmd: admin_cmds.append(cmd_string)
        else: user_cmds.append(cmd_string)
    if user_cmds: embed.add_field(name="👤 Genel Komutlar", value="\n\n".join(user_cmds)[:1024], inline=False)
    if chat_cmds: embed.add_field(name="💬 Sohbet Kanalı Komutları", value="\n\n".join(chat_cmds)[:1024], inline=False)
    if admin_cmds: embed.add_field(name="🛠️ Yönetici Komutları", value="\n\n".join(admin_cmds)[:1024], inline=False)
    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)}")
    try: await ctx.send(embed=embed); await ctx.message.delete()
    except discord.errors.HTTPException as e: logger.error(f"Yardım mesajı gönderilemedi (çok uzun olabilir): {e}"); await ctx.send("Komut listesi çok uzun olduğu için gönderilemedi.")

# === YENİ KOMUTLAR: BELİRLİ MODELLERE SORU ===

@bot.command(name='gemini', aliases=['g'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user) # Spam önleme
async def gemini_direct(ctx: commands.Context, *, question: str = None):
    """
    (Sadece Sohbet Kanalında) Geçerli kanaldaki varsayılan modeli DEĞİŞTİRMEDEN,
    doğrudan varsayılan Gemini modeline soru sorar.
    """
    channel_id = ctx.channel.id
    # Sadece geçici sohbet kanallarında çalışsın
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece özel sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    # Gemini API anahtarı ve modeli yüklü mü?
    global gemini_default_model_instance # .ask komutunun kullandığı global model
    if not GEMINI_API_KEY or not gemini_default_model_instance:
        await ctx.send("⚠️ Gemini API anahtarı ayarlanmamış veya varsayılan model yüklenememiş.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    if question is None or not question.strip():
        await ctx.reply(f"Lütfen komuttan sonra bir soru sorun (örn: `{ctx.prefix}gemini Türkiye'nin başkenti neresidir?`).", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] doğrudan Gemini'ye sordu: {question[:100]}...")
    response_message: discord.Message = None

    try:
        async with ctx.typing():
            try:
                # Tek seferlik istek için generate_content kullan
                response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e:
                 logger.error(f".gemini komutu için API hatası: {gemini_e}")
                 await ctx.reply("Gemini API ile iletişim kurarken bir sorun oluştu.", delete_after=10)
                 try: await ctx.message.delete(delay=10) # Hata durumunda komutu sil
                 except: pass
                 return

            gemini_response_text = ""
            finish_reason = None
            prompt_feedback_reason = None
            # Yanıt ve güvenlik kontrolü (.ask komutundakine benzer)
            try: gemini_response_text = response.text.strip()
            except ValueError as ve: logger.warning(f".gemini yanıtını okurken hata: {ve}...")
            except Exception as text_err: logger.error(f".gemini response.text okuma hatası: {text_err}")
            try: finish_reason = response.candidates[0].finish_reason.name
            except (IndexError, AttributeError): pass
            try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
            except AttributeError: pass

            user_error_msg = None
            if prompt_feedback_reason == "SAFETY": user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı."
            elif finish_reason == "SAFETY": user_error_msg = "Yanıt güvenlik filtrelerine takıldı."; gemini_response_text = None
            elif finish_reason == "RECITATION": user_error_msg = "Yanıt alıntı filtrelerine takıldı."; gemini_response_text = None
            elif finish_reason == "OTHER": user_error_msg = "Yanıt oluşturulamadı (bilinmeyen sebep)."; gemini_response_text = None
            elif not gemini_response_text and finish_reason != "STOP": user_error_msg = f"Yanıt beklenmedik bir sebeple durdu ({finish_reason})."

            if user_error_msg:
                 await ctx.reply(f"⚠️ {user_error_msg}", delete_after=15)
                 try: await ctx.message.delete(delay=15)
                 except: pass
                 return

            if not gemini_response_text:
                logger.warning(f"Gemini'den .gemini için boş yanıt alındı.")
                await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
                try: await ctx.message.delete(delay=15)
                except: pass
                return

        # Yanıtı gönder
        if len(gemini_response_text) > 2000:
             logger.info(f".gemini yanıtı >2000kr, parçalanıyor...")
             parts = [gemini_response_text[i:i+2000] for i in range(0, len(gemini_response_text), 2000)]
             response_message = await ctx.reply(parts[0]) # İlk parçayı yanıtla gönder
             for part in parts[1:]:
                  await ctx.send(part) # Kalanları normal gönder
                  await asyncio.sleep(0.5)
        else:
             response_message = await ctx.reply(gemini_response_text)

        logger.info(f".gemini komutuna yanıt gönderildi.")
        # İsteğe bağlı: Komut mesajını silebiliriz
        #try: await ctx.message.delete()
        #except: pass

    except Exception as e:
        logger.error(f".gemini komutunda genel hata: {e}\n{traceback.format_exc()}")
        await ctx.reply("Komutu işlerken beklenmedik bir hata oluştu.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass

@bot.command(name='deepseek', aliases=['ds'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def deepseek_direct(ctx: commands.Context, *, question: str = None):
    """
    (Sadece Sohbet Kanalında) Geçerli kanaldaki varsayılan modeli DEĞİŞTİRMEDEN,
    doğrudan yapılandırılmış DeepSeek (OpenRouter) modeline soru sorar.
    """
    channel_id = ctx.channel.id
    # Sadece geçici sohbet kanallarında çalışsın
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece özel sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    # OpenRouter API anahtarı ve requests kütüphanesi var mı?
    if not OPENROUTER_API_KEY:
        await ctx.send("⚠️ DeepSeek için OpenRouter API anahtarı ayarlanmamış.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return
    if not REQUESTS_AVAILABLE:
        await ctx.send("⚠️ DeepSeek (OpenRouter) için 'requests' kütüphanesi bulunamadı.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    if question is None or not question.strip():
        await ctx.reply(f"Lütfen komuttan sonra bir soru sorun (örn: `{ctx.prefix}deepseek Python kod örneği yaz`).", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] doğrudan DeepSeek'e sordu: {question[:100]}...")
    response_message: discord.Message = None
    ai_response_text = None
    error_occurred = False
    user_error_msg = "DeepSeek (OpenRouter) ile konuşurken bir sorun oluştu."

    try:
        async with ctx.typing():
            # OpenRouter API'sine tek seferlik istek gönder (geçmiş YOK)
            headers = { "Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json" }
            if OPENROUTER_SITE_URL: headers["HTTP-Referer"] = OPENROUTER_SITE_URL
            if OPENROUTER_SITE_NAME: headers["X-Title"] = OPENROUTER_SITE_NAME
            payload = {
                "model": OPENROUTER_DEEPSEEK_MODEL_NAME,
                "messages": [{"role": "user", "content": question}] # Sadece kullanıcı sorusu
            }
            response_data = None
            try:
                api_response = await asyncio.to_thread(requests.post, OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)
                api_response.raise_for_status() # HTTP hatalarını kontrol et
                response_data = api_response.json()
            except requests.exceptions.Timeout: logger.error("OpenRouter API isteği zaman aşımına uğradı."); error_occurred = True; user_error_msg = "Yapay zeka sunucusundan yanıt alınamadı (zaman aşımı)."
            except requests.exceptions.RequestException as e:
                logger.error(f"OpenRouter API isteği sırasında hata: {e}")
                if e.response is not None:
                     logger.error(f"OR Hata Kodu: {e.response.status_code}, İçerik: {e.response.text[:200]}")
                     if e.response.status_code == 401: user_error_msg = "OpenRouter API Anahtarı geçersiz."
                     elif e.response.status_code == 402: user_error_msg = "OpenRouter krediniz yetersiz."
                     elif e.response.status_code == 429: user_error_msg = "OpenRouter API limiti aşıldı."
                     elif 400 <= e.response.status_code < 500: user_error_msg = f"OpenRouter API Hatası ({e.response.status_code}): Geçersiz istek."
                     elif 500 <= e.response.status_code < 600: user_error_msg = f"OpenRouter API Sunucu Hatası ({e.response.status_code})."
                else: user_error_msg = "OpenRouter API'sine bağlanılamadı."
                error_occurred = True
            except Exception as thread_e: # asyncio.to_thread içindeki diğer hatalar
                logger.error(f"OpenRouter API isteği gönderilirken thread hatası: {thread_e}")
                error_occurred = True; user_error_msg = "Yapay zeka isteği gönderilirken beklenmedik bir hata oluştu."

            # Hata yoksa yanıtı işle
            if not error_occurred and response_data:
                try:
                    ai_response_text = response_data["choices"][0]["message"]["content"].strip()
                    finish_reason = response_data["choices"][0].get("finish_reason")
                    if finish_reason == 'length': logger.warning(f"OpenRouter/DeepSeek yanıtı max_tokens sınırına ulaştı (.ds)")
                    elif finish_reason == 'content_filter': user_error_msg = "Yanıt içerik filtrelerine takıldı."; error_occurred = True; logger.warning(f"OpenRouter/DeepSeek content filter block (.ds)"); ai_response_text = None
                    elif finish_reason != 'stop' and not ai_response_text: user_error_msg = f"Yanıt beklenmedik sebeple durdu ({finish_reason})."; error_occurred = True; logger.warning(f"OpenRouter/DeepSeek unexpected finish: {finish_reason} (.ds)"); ai_response_text = None
                except (KeyError, IndexError, TypeError) as parse_error:
                    logger.error(f"OpenRouter yanıtı işlenirken hata (.ds): {parse_error}. Yanıt: {response_data}")
                    error_occurred = True; user_error_msg = "Yapay zeka yanıtı işlenirken sorun oluştu."
            elif not error_occurred: # response_data yoksa (beklenmez ama olabilir)
                 error_occurred=True; user_error_msg="Yapay zekadan geçerli yanıt alınamadı."

        # Hata varsa kullanıcıya bildir
        if error_occurred:
            await ctx.reply(f"⚠️ {user_error_msg}", delete_after=15)
            try: await ctx.message.delete(delay=15)
            except: pass
            return

        # Başarılı yanıtı gönder
        if not ai_response_text:
            logger.warning(f"DeepSeek'ten .ds için boş yanıt alındı.")
            await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
            try: await ctx.message.delete(delay=15)
            except: pass
            return

        # Yanıtı gönder (parçalama dahil)
        if len(ai_response_text) > 2000:
             logger.info(f".ds yanıtı >2000kr, parçalanıyor...")
             parts = [ai_response_text[i:i+2000] for i in range(0, len(ai_response_text), 2000)]
             response_message = await ctx.reply(parts[0])
             for part in parts[1:]: await ctx.send(part); await asyncio.sleep(0.5)
        else:
             response_message = await ctx.reply(ai_response_text)

        logger.info(f".ds komutuna yanıt gönderildi.")
        # İsteğe bağlı: Komut mesajını sil
        try: await ctx.message.delete()
        except: pass

    except Exception as e:
        logger.error(f".ds komutunda genel hata: {e}\n{traceback.format_exc()}")
        await ctx.reply("Komutu işlerken beklenmedik bir hata oluştu.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass

# --- Genel Hata Yakalama ---
# on_command_error fonksiyonu aynı kalır
@bot.event
async def on_command_error(ctx: commands.Context, error):
    original_error = getattr(error, 'original', error)
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.CommandOnCooldown):
        if ctx.command and ctx.command.name == 'ask': return
        delete_delay = max(5, int(error.retry_after) + 1); await ctx.send(f"⏳ `{ctx.command.qualified_name}` ... **{error.retry_after:.1f} saniye** ...", delete_after=delete_delay); await ctx.message.delete(delay=delete_delay); return
    if isinstance(error, commands.UserInputError):
        delete_delay = 15; command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with; usage = f"`{ctx.prefix}{command_name} {ctx.command.signature if ctx.command else ''}`".replace('=None', '').replace('= Ellipsis', '...'); error_message = "Hatalı komut kullanımı."
        if isinstance(error, commands.MissingRequiredArgument): error_message = f"Eksik argüman: `{error.param.name}`."
        elif isinstance(error, commands.BadArgument): error_message = f"Geçersiz argüman türü: {error}"
        elif isinstance(error, commands.TooManyArguments): error_message = "Çok fazla argüman girdiniz."
        await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay); await ctx.message.delete(delay=delete_delay); return
    delete_user_msg = True; delete_delay = 10
    if isinstance(error, commands.MissingPermissions): logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {original_error.missing_permissions}"); perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions); delete_delay = 15; await ctx.send(f"⛔ Üzgünüm {ctx.author.mention}, ...: **{perms}**", delete_after=delete_delay)
    elif isinstance(error, commands.BotMissingPermissions): logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {original_error.missing_permissions}"); perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions); delete_delay = 15; delete_user_msg = False; await ctx.send(f"🆘 Benim ...: **{perms}**", delete_after=delete_delay)
    elif isinstance(error, commands.CheckFailure): logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {error}"); user_msg = "🚫 Bu komutu burada veya bu şekilde kullanamazsınız."; await ctx.send(user_msg, delete_after=delete_delay)
    else: logger.error(f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' işlenirken beklenmedik hata: {type(original_error).__name__}: {original_error}"); traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__)); logger.error(f"Traceback:\n{traceback_str}"); delete_delay = 15; await ctx.send("⚙️ Komut işlenirken beklenmedik bir hata oluştu...", delete_after=delete_delay)
    if delete_user_msg and ctx.guild: 
        try: 
            await ctx.message.delete(delay=delete_delay)
        except: 
            pass


# === Render/Koyeb için Web Sunucusu ===
# Flask kısmı aynı kalır
app = Flask(__name__)
@app.route('/')
def home():
    if bot and bot.is_ready():
        try: guild_count = len(bot.guilds); active_chats = len(temporary_chat_channels); return f"Bot '{bot.user.name}' çalışıyor. {guild_count} sunucu. {active_chats} aktif sohbet (state).", 200
        except Exception as e: logger.error(f"Sağlık kontrolü sırasında hata: {e}"); return "Bot çalışıyor ama durum alınırken hata oluştu.", 500
    elif bot and not bot.is_ready(): return "Bot başlatılıyor, henüz hazır değil...", 503
    else: return "Bot durumu bilinmiyor veya başlatılamadı.", 500

def run_webserver():
    port = int(os.environ.get("PORT", 8080)); host = os.environ.get("HOST", "0.0.0.0")
    try: logger.info(f"Flask web sunucusu http://{host}:{port} adresinde başlatılıyor..."); app.run(host=host, port=port, debug=False)
    except Exception as e: logger.critical(f"Web sunucusu başlatılırken KRİTİK HATA: {e}")
# ===================================

# --- Botu Çalıştır ---
# Başlangıç kısmı aynı kalır
if __name__ == "__main__":
    if not DISCORD_TOKEN: logger.critical("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı!"); exit()
    if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); exit()
    # API anahtarı kontrolleri yukarıda yapıldı

    logger.info("Bot başlatılıyor...")
    webserver_thread = None
    try:
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.LoginFailure: logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired: logger.critical("HATA: Gerekli Intent'ler (Members, Message Content) Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err: logger.critical(f"PostgreSQL bağlantı hatası (Başlangıçta): {db_err}")
    except ImportError as import_err: logger.critical(f"Bot çalıştırılırken kritik import hatası: {import_err}\n{traceback.format_exc()}")
    except Exception as e: logger.critical(f"Bot çalıştırılırken kritik hata: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally: logger.info("Bot kapatılıyor...")
    