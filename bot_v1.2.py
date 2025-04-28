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
# import subprocess # Artık deepseek'i göstermeye gerek yok

# OpenAI kütüphanesini import et (DeepSeek için kullanılacak)
OPENAI_SDK_AVAILABLE = False
OpenAI = None
APIError = None
RateLimitError = None
logger = logging.getLogger('discord_ai_bot') # Logger'ı burada tanımla

try:
    from openai import OpenAI, APIError, RateLimitError # Gerekli OpenAI sınıfları
    OPENAI_SDK_AVAILABLE = True
    logger.info(">>> DEBUG: OpenAI kütüphanesi başarıyla import edildi (DeepSeek için kullanılacak).")
except ImportError:
    logger.error(">>> HATA: 'openai' kütüphanesi bulunamadı. Lütfen 'pip install openai' ile kurun ve requirements.txt'e ekleyin.")
    # Bu durumda DeepSeek kullanılamaz.


# --- Logging Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# Logger zaten yukarıda tanımlandı
# Flask'ın kendi loglarını biraz kısmak için
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


# .env dosyasındaki değişkenleri yükle
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") # DeepSeek anahtarı hala gerekli

# API Anahtarı Kontrolleri
if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); exit()
if not GEMINI_API_KEY and not DEEPSEEK_API_KEY:
    logger.critical("HATA: Ne Gemini ne de DeepSeek API Anahtarı bulunamadı! En az biri gerekli.")
    exit()
if not GEMINI_API_KEY:
    logger.warning("UYARI: Gemini API Anahtarı bulunamadı! Gemini modelleri kullanılamayacak.")
if not DEEPSEEK_API_KEY:
    logger.warning("UYARI: DeepSeek API Anahtarı bulunamadı! DeepSeek modelleri kullanılamayacak.")
elif not OPENAI_SDK_AVAILABLE: # DeepSeek anahtarı VARSA ama OpenAI kütüphanesi YOKSA
    logger.error("HATA: DeepSeek API anahtarı bulundu ancak gerekli 'openai' kütüphanesi yüklenemedi/import edilemedi. DeepSeek kullanılamayacak.")
    DEEPSEEK_API_KEY = None # OpenAI kütüphanesi yoksa DeepSeek anahtarını yok say

# Render PostgreSQL bağlantısı için
DATABASE_URL = os.getenv("DATABASE_URL")

# Model Ön Ekleri ve Varsayılanlar
GEMINI_PREFIX = "gs:"
DEEPSEEK_PREFIX = "ds:"
DEFAULT_GEMINI_MODEL_NAME = 'gemini-1.5-flash-latest' # Prefixsiz temel ad
DEFAULT_MODEL_NAME = f"{GEMINI_PREFIX}{DEFAULT_GEMINI_MODEL_NAME}" # Kullanılacak varsayılan (ön ekli)

# DeepSeek için kullanılacak OpenAI API Endpoint'i
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1" # v1 eklemek genellikle doğrudur, kontrol edin

# Varsayılan değerler (DB'den veya ortamdan okunamzsa)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Global Değişkenler ---
entry_channel_id = None
inactivity_timeout = None

# Aktif sohbet oturumları ve geçmişleri
# Yapı: channel_id -> {'model': 'prefix:model_name', 'session': GeminiSession or None, 'history': OpenAI-Uyumlu Mesaj Listesi or None}
active_ai_chats = {}

temporary_chat_channels = set() # Geçici kanal ID'leri
user_to_channel_map = {} # user_id -> channel_id
channel_last_active = {} # channel_id -> datetime (timezone aware)
user_next_model = {} # user_id -> 'prefix:model_name' (Bir sonraki sohbet için tercih)
warned_inactive_channels = set() # İnaktivite uyarısı gönderilen kanallar

# --- Veritabanı Yardımcı Fonksiyonları (PostgreSQL) ---

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
                if not model_name_db or (not model_name_db.startswith(GEMINI_PREFIX) and not model_name_db.startswith(DEEPSEEK_PREFIX)):
                    logger.warning(f"DB'de geçersiz model adı bulundu (channel_id: {row['channel_id']}), varsayılana dönülüyor: {DEFAULT_MODEL_NAME}")
                    model_name_db = DEFAULT_MODEL_NAME
                    update_channel_model_db(row['channel_id'], DEFAULT_MODEL_NAME)

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

# Gemini API'yi yapılandır (varsa)
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

# DeepSeek için OpenAI SDK durumu zaten yukarıda kontrol edildi.

# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Yardımcı Fonksiyonlar ---

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
    """Belirtilen kanalda seçili AI modeline (Gemini/DeepSeek) mesaj gönderir ve yanıtlar."""
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
            if not current_model_with_prefix.startswith(GEMINI_PREFIX) and not current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                 logger.warning(f"DB'den geçersiz prefix'li model adı okundu ({current_model_with_prefix}), varsayılana dönülüyor.")
                 current_model_with_prefix = DEFAULT_MODEL_NAME
                 update_channel_model_db(channel_id, DEFAULT_MODEL_NAME)

            logger.info(f"'{channel.name}' (ID: {channel_id}) için AI sohbet oturumu {current_model_with_prefix} ile başlatılıyor.")

            if current_model_with_prefix.startswith(GEMINI_PREFIX):
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
                    'history': None # Gemini için history kullanılmıyor, session içinde
                }
            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                # DeepSeek için OpenAI SDK kullanılacak, özel session yok, sadece history listesi
                if not DEEPSEEK_API_KEY: raise ValueError("DeepSeek API anahtarı ayarlı değil.")
                if not OPENAI_SDK_AVAILABLE: raise ImportError("'openai' kütüphanesi yüklenemedi.")
                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': None, # Deepseek için session yok
                    'history': [] # Boş geçmiş listesi (OpenAI formatına uygun olacak)
                }
            else:
                raise ValueError(f"Tanımsız model ön eki: {current_model_with_prefix}")

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
    response = None # API yanıtını saklamak için

    async with channel.typing():
        try:
            # --- API Çağrısı (Modele Göre) ---
            if current_model_with_prefix.startswith(GEMINI_PREFIX):
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

                if prompt_feedback_reason == "SAFETY":
                    user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı."
                    error_occurred = True
                    logger.warning(f"Gemini prompt safety block (Kanal: {channel_id}). Sebep: {response.prompt_feedback.block_reason}")
                elif finish_reason == "SAFETY":
                     user_error_msg = "Yanıt güvenlik filtrelerine takıldı."
                     error_occurred = True
                     logger.warning(f"Gemini response safety block (Kanal: {channel_id}). Sebep: {response.candidates[0].finish_reason}")
                     ai_response_text = None
                elif finish_reason == "RECITATION":
                     user_error_msg = "Yanıt, alıntı filtrelerine takıldı."
                     error_occurred = True
                     logger.warning(f"Gemini response recitation block (Kanal: {channel_id}).")
                     ai_response_text = None
                elif finish_reason == "OTHER":
                     user_error_msg = "Yanıt oluşturulamadı (bilinmeyen sebep)."
                     error_occurred = True
                     logger.warning(f"Gemini response 'OTHER' finish reason (Kanal: {channel_id}).")
                     ai_response_text = None
                elif not ai_response_text and not error_occurred:
                     logger.warning(f"Gemini'den boş yanıt alındı, finish_reason: {finish_reason} (Kanal: {channel_id})")


            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                # OpenAI SDK kullanılacak
                if not OPENAI_SDK_AVAILABLE: raise ImportError("'openai' kütüphanesi bulunamadı.")
                if not DEEPSEEK_API_KEY: raise ValueError("DeepSeek API anahtarı ayarlı değil.")

                history = chat_data.get('history')
                if history is None: raise ValueError("DeepSeek geçmişi bulunamadı.")

                actual_model_name = current_model_with_prefix[len(DEEPSEEK_PREFIX):]

                # Yeni mesajı geçmişe ekle (OpenAI formatı)
                history.append({"role": "user", "content": prompt_text})

                # DeepSeek API'sine bağlanmak için OpenAI istemcisini yapılandır
                try:
                    client = OpenAI(
                        api_key=DEEPSEEK_API_KEY,
                        base_url=DEEPSEEK_BASE_URL
                    )
                except Exception as client_err:
                    logger.error(f"OpenAI istemcisi (DeepSeek için) oluşturulurken hata: {client_err}")
                    raise ValueError("DeepSeek için OpenAI istemcisi oluşturulamadı.") from client_err

                # API çağrısını thread'de yap
                try:
                    response = await asyncio.to_thread(
                        client.chat.completions.create,
                        model=actual_model_name,
                        messages=history, # Sistem mesajı olmadan direkt geçmişi gönderiyoruz
                        # max_tokens=1024, # İsteğe bağlı
                        # temperature=0.7, # İsteğe bağlı
                        stream=False
                    )
                # OpenAI'nin özel hatalarını yakala
                except APIError as e: # Genel API hatası (4xx, 5xx)
                    logger.error(f"DeepSeek API Hatası (OpenAI SDK): {e.status_code} - {e.message}")
                    error_occurred = True
                    user_error_msg = f"DeepSeek API Hatası ({e.status_code}). Lütfen tekrar deneyin veya model adını kontrol edin."
                    if history: history.pop() # Başarısız isteği geçmişten çıkar
                except RateLimitError as e: # Rate limit hatası (429)
                    logger.warning(f"DeepSeek API Rate Limit Aşıldı (OpenAI SDK): {e.message}")
                    error_occurred = True
                    user_error_msg = "DeepSeek API kullanım limitine ulaştınız. Lütfen biraz bekleyin."
                    if history: history.pop()
                except Exception as e: # Diğer genel hatalar (örn: bağlantı)
                    logger.error(f"DeepSeek API çağrısı sırasında beklenmedik hata (OpenAI SDK): {type(e).__name__}: {e}")
                    logger.error(traceback.format_exc()) # Traceback'i logla
                    error_occurred = True
                    user_error_msg = "DeepSeek ile iletişimde beklenmedik bir sorun oluştu."
                    if history: history.pop()

                # Hata oluşmadıysa yanıtı işle
                if not error_occurred and response:
                    if response.choices:
                        choice = response.choices[0]
                        ai_response_text = choice.message.content.strip()
                        finish_reason = choice.finish_reason

                        if ai_response_text:
                            # Başarılı yanıtı geçmişe ekle
                            history.append({"role": "assistant", "content": ai_response_text})
                        else: # İçerik boşsa
                            logger.warning(f"DeepSeek'ten boş içerikli yanıt alındı. Finish Reason: {finish_reason}")
                            # Boş yanıtı geçmişe eklemeyelim, kullanıcı mesajını da silelim ki tekrar denenebilsin
                            if history and history[-1]["role"] == "user": history.pop()

                        if finish_reason == 'length':
                            logger.warning(f"DeepSeek yanıtı max_tokens sınırına ulaştı (model: {actual_model_name}, Kanal: {channel_id})")
                        elif finish_reason == 'content_filter':
                            user_error_msg = "DeepSeek yanıtı içerik filtrelerine takıldı."
                            error_occurred = True
                            logger.warning(f"DeepSeek content filter block (Kanal: {channel_id}).")
                            # Geçmişten kullanıcı mesajını ve potansiyel boş asistan mesajını çıkar
                            if history and history[-1]["role"] == "assistant": history.pop() # Eklenen boş yanıtı sil
                            if history and history[-1]["role"] == "user": history.pop() # Kullanıcı mesajını da çıkar
                            ai_response_text = None # Yanıt yok
                        elif finish_reason != 'stop' and not ai_response_text: # Durma sebebi 'stop' değilse ve yanıt yoksa
                            user_error_msg = f"DeepSeek yanıtı beklenmedik bir sebeple durdu ({finish_reason})."
                            error_occurred = True
                            logger.warning(f"DeepSeek unexpected finish reason: {finish_reason} (Kanal: {channel_id}).")
                            if history and history[-1]["role"] == "assistant": history.pop() # Eklenen boş yanıtı sil
                            if history and history[-1]["role"] == "user": history.pop() # Kullanıcı mesajını da çıkar
                            ai_response_text = None

                    else: # choices listesi boşsa
                        usage_info = response.usage if hasattr(response, 'usage') else 'Yok'
                        logger.warning(f"[AI CHAT/{current_model_with_prefix}] DeepSeek'ten boş 'choices' listesi alındı. Usage: {usage_info} (Kanal: {channel_id})")
                        user_error_msg = "DeepSeek'ten bir yanıt alınamadı (boş 'choices')."
                        error_occurred = True
                        if history: history.pop() # Kullanıcı mesajını çıkar

            else:
                logger.error(f"İşlenemeyen model türü: {current_model_with_prefix}")
                user_error_msg = "Bilinmeyen bir yapay zeka modeli yapılandırılmış."
                error_occurred = True

            # --- Yanıt İşleme ve Gönderme ---
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

        except ImportError as e: # OpenAI kütüphanesi yoksa
             logger.error(f"Gerekli kütüphane bulunamadı: {e}")
             error_occurred = True
             user_error_msg = "Gerekli yapay zeka kütüphanesi (openai) sunucuda bulunamadı."
             active_ai_chats.pop(channel_id, None)
             remove_temp_channel_db(channel_id)
        except genai.types.StopCandidateException as stop_e:
            logger.error(f"Gemini StopCandidateException (Kanal: {channel_id}): {stop_e}")
            error_occurred = True
            user_error_msg = "Gemini yanıtı beklenmedik bir şekilde durdu."
        except genai.types.BlockedPromptException as block_e:
            logger.warning(f"Gemini BlockedPromptException (Kanal: {channel_id}): {block_e}")
            error_occurred = True
            user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı (BlockedPromptException)."
        except Exception as e: # Diğer tüm genel hatalar
            logger.error(f"[AI CHAT/{current_model_with_prefix}] API/İşlem hatası (Kanal: {channel_id}): {type(e).__name__}: {e}")
            logger.error(traceback.format_exc()) # Tam traceback'i logla
            error_occurred = True
            # Hata mesajı zaten yukarıdaki DeepSeek/OpenAI try-except bloğunda ayarlanmış olabilir.
            # Ayarlanmadıysa genel bir mesaj kullan:
            if user_error_msg == "Yapay zeka ile konuşurken bir sorun oluştu.":
                error_str = str(e).lower()
                # Spesifik hata mesajları (OpenAI SDK için de genellenebilir)
                if "api key" in error_str or "authentication" in error_str or "permission_denied" in error_str or "401" in error_str or "403" in error_str:
                    user_error_msg = "API Anahtarı sorunu veya yetki reddi."
                elif "quota" in error_str or "limit" in error_str or "429" in error_str or "resource_exhausted" in error_str:
                    user_error_msg = "API kullanım limiti aşıldı veya çok fazla istek gönderildi."
                elif "invalid" in error_str or "400" in error_str or "bad request" in error_str or "not found" in error_str or "could not find model" in error_str:
                    user_error_msg = "Geçersiz istek (örn: model adı yanlış olabilir veya API yolu bulunamadı)."
                elif "500" in error_str or "internal error" in error_str or "unavailable" in error_str or "503" in error_str:
                    user_error_msg = "Yapay zeka sunucusunda geçici bir sorun oluştu. Lütfen sonra tekrar deneyin."
                elif isinstance(e, asyncio.TimeoutError):
                    user_error_msg = "Yapay zeka sunucusundan yanıt alınamadı (zaman aşımı)."
                elif isinstance(e, ValueError) and "OpenAI istemcisi oluşturulamadı" in str(e):
                    user_error_msg = "DeepSeek API istemcisi başlatılamadı."
                # DeepSeek geçmiş hatası kontrolü (geçerli olabilir)
                if DEEPSEEK_PREFIX in current_model_with_prefix and 'history' in locals() and isinstance(e, (TypeError, ValueError, AttributeError)) and "history" in error_str :
                    logger.warning(f"DeepSeek geçmişiyle ilgili potansiyel hata, geçmiş sıfırlanıyor: {channel_id}")
                    if channel_id in active_ai_chats and 'history' in active_ai_chats[channel_id]:
                        active_ai_chats[channel_id]['history'] = []
                    user_error_msg += " (Konuşma geçmişi olası bir hata nedeniyle sıfırlandı.)"


    # Hata oluştuysa kullanıcıya mesaj gönder
    if error_occurred:
        try:
            await channel.send(f"⚠️ {user_error_msg}", delete_after=20)
        except discord.errors.NotFound: pass
        except Exception as send_err: logger.warning(f"Hata mesajı gönderilemedi (Kanal: {channel_id}): {send_err}")
        return False

    return False # Normalde buraya gelinmemeli

# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'{bot.user} olarak giriş yapıldı (ID: {bot.user.id}).')
    logger.info(f"Mevcut Ayarlar - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")

    logger.info("Kalıcı veriler (geçici kanallar) yükleniyor...");
    temporary_chat_channels.clear()
    user_to_channel_map.clear()
    channel_last_active.clear()
    active_ai_chats.clear()
    warned_inactive_channels.clear()

    loaded_channels = load_all_temp_channels()
    valid_channel_count = 0
    invalid_channel_ids = []
    guild_ids = {g.id for g in bot.guilds}

    for ch_id, u_id, last_active_ts, ch_model_name_with_prefix in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel) and channel_obj.guild.id in guild_ids:
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            channel_last_active[ch_id] = last_active_ts
            valid_channel_count += 1
        else:
            reason = "Discord'da bulunamadı/geçersiz"
            if channel_obj and channel_obj.guild.id not in guild_ids: reason = f"Bot artık '{channel_obj.guild.name}' sunucusunda değil"
            elif not channel_obj: reason = "Discord'da bulunamadı"
            logger.warning(f"DB'deki geçici kanal {ch_id} yüklenemedi ({reason}). DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)

    for invalid_id in invalid_channel_ids:
        remove_temp_channel_db(invalid_id)

    logger.info(f"{valid_channel_count} geçerli geçici kanal DB'den yüklendi.")
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")

    entry_channel_name = "Ayarlanmadı"
    try:
        if entry_channel_id:
            entry_channel = await bot.fetch_channel(entry_channel_id)
            if entry_channel: entry_channel_name = f"#{entry_channel.name}"
            else: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı veya erişilemiyor.")
        elif not entry_channel_id: entry_channel_name = "Ayarlanmadı"

        activity_text = f"Sohbet için {entry_channel_name}"
        await bot.change_presence(activity=discord.Game(name=activity_text))
        logger.info(f"Bot aktivitesi ayarlandı: '{activity_text}'")
    except discord.errors.NotFound:
        logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı (aktivite ayarlanırken).")
        await bot.change_presence(activity=discord.Game(name="Sohbet için kanal?"))
    except Exception as e:
        logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    if not check_inactivity.is_running():
        check_inactivity.start()
        logger.info("İnaktivite kontrol görevi başlatıldı.")

    logger.info("Bot komutları ve mesajları dinliyor..."); print("-" * 20)

@bot.event
async def on_message(message: discord.Message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    if message.author == bot.user or message.author.bot: return
    if not message.guild: return
    if not isinstance(message.channel, discord.TextChannel): return

    author = message.author
    author_id = author.id
    channel = message.channel
    channel_id = channel.id
    guild = message.guild

    ctx = await bot.get_context(message)
    if ctx.command:
        await bot.process_commands(message)
        return

    # --- Otomatik Kanal Oluşturma (Giriş Kanalında) ---
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

        chosen_model_with_prefix = user_next_model.pop(author_id, DEFAULT_MODEL_NAME)
        logger.info(f"{author.name} giriş kanalına yazdı, {chosen_model_with_prefix} ile kanal oluşturuluyor...")

        try:
            processing_msg = await channel.send(f"{author.mention}, özel sohbet kanalın oluşturuluyor...", delete_after=15)
        except Exception as e:
            logger.warning(f"Giriş kanalına 'işleniyor' mesajı gönderilemedi: {e}")
            processing_msg = None

        new_channel = await create_private_chat_channel(guild, author)

        if new_channel:
            new_channel_id = new_channel.id
            temporary_chat_channels.add(new_channel_id)
            user_to_channel_map[author_id] = new_channel_id
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[new_channel_id] = now_utc
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_with_prefix)

            display_model_name = chosen_model_with_prefix.split(':')[-1]
            try:
                 embed = discord.Embed(title="👋 Özel Yapay Zeka Sohbeti Başlatıldı!",
                                    description=(f"Merhaba {author.mention}!\n\n"
                                                 f"Bu kanalda `{display_model_name}` modeli ile sohbet edeceksin."),
                                    color=discord.Color.og_blurple())
                 embed.set_thumbnail(url=bot.user.display_avatar.url)
                 timeout_hours_display = "Asla"
                 if inactivity_timeout:
                     timeout_hours_display = f"`{inactivity_timeout.total_seconds() / 3600:.1f}` saat"
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

            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            try: await channel.send(f"{author.mention}, özel sohbet kanalı {new_channel.mention} oluşturuldu! Oradan devam edebilirsin.", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            try:
                logger.info(f"-----> AI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_with_prefix})")
                success = await send_to_ai_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success: logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else: logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e: logger.error(f"İlk mesaj işlenirken/gönderilirken hata: {e}")

            try:
                await channel.delete_messages([discord.Object(id=original_message_id)])
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except discord.errors.NotFound: pass
            except discord.errors.Forbidden: logger.warning(f"Giriş kanalında ({channel_id}) mesaj silme izni yok.")
            except Exception as e: logger.warning(f"Giriş kanalındaki orijinal mesaj ({original_message_id}) silinirken hata: {e}")
        else:
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
@tasks.loop(minutes=5)
async def check_inactivity():
    """Aktif olmayan geçici kanalları kontrol eder, uyarır ve siler."""
    global warned_inactive_channels
    if inactivity_timeout is None: return

    now = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []
    channels_to_warn = []
    warning_delta = min(datetime.timedelta(minutes=10), inactivity_timeout * 0.1)
    warning_threshold = inactivity_timeout - warning_delta

    last_active_copy = channel_last_active.copy()

    for channel_id, last_active_time in last_active_copy.items():
        if channel_id not in temporary_chat_channels:
             logger.warning(f"İnaktivite kontrol: {channel_id} `channel_last_active` içinde ama `temporary_chat_channels` içinde değil. State tutarsızlığı, temizleniyor.")
             channel_last_active.pop(channel_id, None)
             warned_inactive_channels.discard(channel_id)
             active_ai_chats.pop(channel_id, None)
             user_id_to_remove = None
             for user_id, ch_id in list(user_to_channel_map.items()):
                 if ch_id == channel_id: user_id_to_remove = user_id; break
             if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
             remove_temp_channel_db(channel_id)
             continue

        if not isinstance(last_active_time, datetime.datetime):
            logger.error(f"İnaktivite kontrolü: Kanal {channel_id} için geçersiz last_active_time tipi ({type(last_active_time)}). Atlanıyor.")
            continue
        if last_active_time.tzinfo is None:
            logger.warning(f"İnaktivite kontrolü: Kanal {channel_id} için timezone bilgisi olmayan last_active_time ({last_active_time}). UTC varsayılıyor.")
            last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)

        try:
            time_inactive = now - last_active_time
        except TypeError as te:
             logger.error(f"İnaktivite süresi hesaplanırken hata (Kanal: {channel_id}, Now: {now}, LastActive: {last_active_time}): {te}")
             continue

        if time_inactive > inactivity_timeout:
            channels_to_delete.append(channel_id)
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels:
            channels_to_warn.append(channel_id)

    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                current_last_active = channel_last_active.get(channel_id)
                if not current_last_active: continue
                if current_last_active.tzinfo is None:
                    current_last_active = current_last_active.replace(tzinfo=datetime.timezone.utc)
                remaining_time = inactivity_timeout - (now - current_last_active)
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir. Devam etmek için mesaj yazın.", delete_after=300)
                warned_inactive_channels.add(channel_id)
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id} ({channel_obj.name})")
            except discord.errors.NotFound:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Kanal bulunamadı.")
                warned_inactive_channels.discard(channel_id)
                temporary_chat_channels.discard(channel_id)
                active_ai_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
                if user_id: user_to_channel_map.pop(user_id[0], None)
                remove_temp_channel_db(channel_id)
            except discord.errors.Forbidden:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Mesaj gönderme izni yok.")
                warned_inactive_channels.add(channel_id)
            except Exception as e:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else:
            logger.warning(f"İnaktivite uyarısı için kanal {channel_id} Discord'da bulunamadı.")
            warned_inactive_channels.discard(channel_id)
            temporary_chat_channels.discard(channel_id)
            active_ai_chats.pop(channel_id, None)
            channel_last_active.pop(channel_id, None)
            user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
            if user_id: user_to_channel_map.pop(user_id[0], None)
            remove_temp_channel_db(channel_id)

    if channels_to_delete:
        logger.info(f"İnaktivite: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id)
            reason = "İnaktivite nedeniyle otomatik silindi."
            if channel_to_delete:
                channel_name_log = channel_to_delete.name
                try:
                    await channel_to_delete.delete(reason=reason)
                    logger.info(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) başarıyla silindi.")
                    temporary_chat_channels.discard(channel_id)
                    active_ai_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id)
                except discord.errors.NotFound:
                    logger.warning(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken bulunamadı. State zaten temizlenmiş olabilir veya manuel temizleniyor.")
                    temporary_chat_channels.discard(channel_id)
                    active_ai_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id)
                except discord.errors.Forbidden:
                     logger.error(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinemedi: 'Kanalları Yönet' izni yok.")
                     temporary_chat_channels.discard(channel_id)
                     active_ai_chats.pop(channel_id, None)
                     channel_last_active.pop(channel_id, None)
                     warned_inactive_channels.discard(channel_id)
                     user_id_to_remove = None
                     for user_id, ch_id in list(user_to_channel_map.items()):
                         if ch_id == channel_id: user_id_to_remove = user_id; break
                     if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                     remove_temp_channel_db(channel_id)
                except Exception as e:
                    logger.error(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken hata: {e}\n{traceback.format_exc()}")
                    temporary_chat_channels.discard(channel_id)
                    active_ai_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id)
            else:
                logger.warning(f"İnaktif kanal (ID: {channel_id}) Discord'da bulunamadı. DB'den ve state'den siliniyor.")
                temporary_chat_channels.discard(channel_id)
                active_ai_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                warned_inactive_channels.discard(channel_id)
                user_id_to_remove = None
                for user_id, ch_id in list(user_to_channel_map.items()):
                    if ch_id == channel_id: user_id_to_remove = user_id; break
                if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                remove_temp_channel_db(channel_id)
            warned_inactive_channels.discard(channel_id)

@check_inactivity.before_loop
async def before_check_inactivity():
    await bot.wait_until_ready()
    logger.info("Bot hazır, inaktivite kontrol döngüsü başlıyor.")

@bot.event
async def on_guild_channel_delete(channel):
    """Bir kanal silindiğinde tetiklenir (state temizliği için)."""
    channel_id = channel.id
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) silindi (Discord Event), ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id)
        active_ai_chats.pop(channel_id, None)
        channel_last_active.pop(channel_id, None)
        warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id:
                user_id_to_remove = user_id
                break
        if user_id_to_remove:
            removed_channel_id = user_to_channel_map.pop(user_id_to_remove, None)
            if removed_channel_id:
                 logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi (Silinen Kanal ID: {removed_channel_id}).")
            else:
                 logger.warning(f"Silinen kanal {channel_id} için kullanıcı {user_id_to_remove} haritadan çıkarılamadı (zaten yok?).")
        else:
            logger.warning(f"Silinen geçici kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")
        remove_temp_channel_db(channel_id)

# --- Komutlar ---

@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx: commands.Context):
    """Mevcut geçici AI sohbet kanalını siler."""
    channel_id = ctx.channel.id
    author_id = ctx.author.id

    is_temp_channel = False
    expected_user_id = None

    if channel_id in temporary_chat_channels:
        is_temp_channel = True
        for user_id, ch_id in user_to_channel_map.items():
            if ch_id == channel_id:
                expected_user_id = user_id
                break

    if not is_temp_channel or expected_user_id is None:
        conn = None
        try:
            conn = db_connect()
            cursor = conn.cursor(cursor_factory=DictCursor)
            cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,))
            owner_row = cursor.fetchone()
            cursor.close()
            if owner_row:
                is_temp_channel = True
                db_user_id = owner_row['user_id']
                if expected_user_id is None:
                    expected_user_id = db_user_id
                elif expected_user_id != db_user_id:
                     logger.warning(f".endchat: Kanal {channel_id} için state sahibi ({expected_user_id}) ile DB sahibi ({db_user_id}) farklı! DB sahibine öncelik veriliyor.")
                     expected_user_id = db_user_id
                if channel_id not in temporary_chat_channels: temporary_chat_channels.add(channel_id)
                if expected_user_id not in user_to_channel_map or user_to_channel_map[expected_user_id] != channel_id:
                    user_to_channel_map[expected_user_id] = channel_id
            else:
                 if not is_temp_channel:
                    await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10)
                    try: await ctx.message.delete(delay=10)
                    except: pass
                    return
        except (Exception, psycopg2.DatabaseError) as e:
            logger.error(f".endchat DB kontrol hatası (channel_id: {channel_id}): {e}")
            await ctx.send("Kanal bilgisi kontrol edilirken bir hata oluştu.", delete_after=10)
            try: await ctx.message.delete(delay=10)
            except: pass
            return
        finally:
            if conn: conn.close()

    if expected_user_id and author_id != expected_user_id:
        owner = ctx.guild.get_member(expected_user_id)
        owner_name = f"<@{expected_user_id}>" if not owner else owner.mention
        await ctx.send(f"Bu kanalı sadece oluşturan kişi ({owner_name}) kapatabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return
    elif not expected_user_id:
        logger.error(f".endchat: Kanal {channel_id} sahibi (state veya DB'de) bulunamadı! Yine de silmeye çalışılıyor.")
        await ctx.send("Kanal sahibi bilgisi bulunamadı. Kapatma işlemi yapılamıyor.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send("Kanalları yönetme iznim yok, bu yüzden kanalı silemiyorum.", delete_after=10)
        return

    try:
        channel_name_log = ctx.channel.name
        logger.info(f"Kanal '{channel_name_log}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı. State zaten temizlenmiş olabilir veya manuel temizleniyor.")
        temporary_chat_channels.discard(channel_id)
        active_ai_chats.pop(channel_id, None)
        channel_last_active.pop(channel_id, None)
        warned_inactive_channels.discard(channel_id)
        if expected_user_id: user_to_channel_map.pop(expected_user_id, None)
        remove_temp_channel_db(channel_id)
    except discord.errors.Forbidden:
         logger.error(f"Kanal '{ctx.channel.name}' (ID: {channel_id}) manuel silinemedi: 'Kanalları Yönet' izni yok.")
         await ctx.send("Kanalları yönetme iznim yok, bu yüzden kanalı silemiyorum.", delete_after=10)
    except Exception as e:
        logger.error(f".endchat komutunda kanal silinirken hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Kanal silinirken bir hata oluştu.", delete_after=10)
        temporary_chat_channels.discard(channel_id)
        active_ai_chats.pop(channel_id, None)
        channel_last_active.pop(channel_id, None)
        warned_inactive_channels.discard(channel_id)
        if expected_user_id: user_to_channel_map.pop(expected_user_id, None)
        remove_temp_channel_db(channel_id)

@bot.command(name='resetchat', aliases=['sıfırla'])
@commands.guild_only()
async def reset_chat_session(ctx: commands.Context):
    """Mevcut geçici sohbet kanalının AI konuşma geçmişini/oturumunu sıfırlar."""
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece aktif geçici sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10);
        except: pass
        return

    if channel_id in active_ai_chats:
        # Oturumu veya geçmişi temizle/sıfırla
        # Sadece state'den kaldırmak yeterli, bir sonraki mesajda yeniden oluşacak
        active_ai_chats.pop(channel_id, None)
        logger.info(f"Sohbet geçmişi/oturumu {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi/oturumu sıfırlandı. Bir sonraki mesajınızla yeni bir oturum başlayacak.", delete_after=15)
    else:
        logger.info(f"Sıfırlanacak aktif oturum/geçmiş yok: Kanal {channel_id}")
        await ctx.send("✨ Şu anda sıfırlanacak aktif bir konuşma geçmişi/oturumu bulunmuyor. Zaten temiz.", delete_after=10)
    try: await ctx.message.delete(delay=15);
    except: pass

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: str = None):
    """Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç)."""
    if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
        await ctx.send("Mesajları silebilmem için bu kanalda 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10)
        return

    if amount is None:
        await ctx.send(f"Silinecek mesaj sayısı (`{ctx.prefix}clear 5`) veya tümü için `{ctx.prefix}clear all` yazın.", delete_after=10)
        try: await ctx.message.delete(delay=10);
        except: pass
        return

    deleted_count = 0
    skipped_pinned = 0 # Bu sayaç purge ile tam doğru çalışmayabilir
    original_command_message_id = ctx.message.id

    try:
        if amount.lower() == 'all':
            status_msg = await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)... Lütfen bekleyin.", delete_after=15)
            try: await ctx.message.delete() # Komutu hemen sil
            except: pass

            while True:
                try:
                    # check=lambda m: not m.pinned bazen bulk silmede yavaşlatabilir veya tam çalışmayabilir.
                    # Güvenlik için limitle gidelim.
                    deleted_messages = await ctx.channel.purge(limit=100, check=lambda m: not m.pinned, bulk=True)
                    if not deleted_messages: break
                    deleted_count += len(deleted_messages)
                    if len(deleted_messages) < 100: break
                    await asyncio.sleep(1)
                except discord.errors.NotFound: break
                except discord.errors.HTTPException as http_e:
                     if http_e.status == 429:
                          retry_after = float(http_e.response.headers.get('Retry-After', 1))
                          logger.warning(f".clear 'all' rate limited. Retrying after {retry_after}s")
                          await status_msg.edit(content=f"Rate limit! {retry_after:.1f} saniye bekleniyor...", delete_after=retry_after + 2)
                          await asyncio.sleep(retry_after)
                          continue
                     else: raise
            try:
                msg_content = f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi (sabitlenmişler hariç)."
                await status_msg.edit(content=msg_content, delete_after=10)
            except: pass

        else: # Belirli sayıda silme
            try:
                limit = int(amount)
                if limit <= 0: raise ValueError("Sayı pozitif olmalı")
                if limit > 500:
                    limit = 500
                    await ctx.send("Tek seferde en fazla 500 mesaj silebilirsiniz.", delete_after=5)

                # Komut mesajından öncekileri silmek daha mantıklı
                deleted_messages = await ctx.channel.purge(limit=limit, check=lambda m: not m.pinned, before=ctx.message, bulk=True)
                actual_deleted_count = len(deleted_messages)

                try: await ctx.message.delete() # Komut mesajını ayrıca sil
                except: pass

                msg = f"{actual_deleted_count} mesaj silindi (sabitlenmişler hariç)."
                await ctx.send(msg, delete_after=7)

            except ValueError:
                await ctx.send(f"Geçersiz sayı: '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10)
                try: await ctx.message.delete(delay=10);
                except: pass

    except discord.errors.Forbidden:
        logger.error(f"HATA: '{ctx.channel.name}' kanalında mesaj silme izni yok (Bot için)!")
        await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10)
        try: await ctx.message.delete(delay=10);
        except: pass
    except Exception as e:
        logger.error(f".clear hatası: {e}\n{traceback.format_exc()}")
        await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10)
        try: await ctx.message.delete(delay=10);
        except: pass

@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def ask_in_channel(ctx: commands.Context, *, question: str = None):
    """Sorulan soruyu varsayılan Gemini modeline iletir ve yanıtı geçici olarak bu kanalda gösterir."""
    if not gemini_default_model_instance:
        if not GEMINI_API_KEY: user_msg = "⚠️ Gemini API anahtarı ayarlanmadığı için bu komut kullanılamıyor."
        else: user_msg = "⚠️ Varsayılan Gemini modeli yüklenemedi. Bot loglarını kontrol edin."
        await ctx.reply(user_msg, delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    if question is None or not question.strip():
        error_msg = await ctx.reply(f"Lütfen bir soru sorun (örn: `{ctx.prefix}ask Evren nasıl oluştu?`).", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None

    try:
        async with ctx.typing():
            try:
                response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e:
                 logger.error(f".ask için Gemini API hatası: {type(gemini_e).__name__}: {gemini_e}")
                 error_str = str(gemini_e).lower()
                 user_msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."
                 if "api key" in error_str or "authentication" in error_str: user_msg = "API Anahtarı sorunu."
                 elif "quota" in error_str or "limit" in error_str or "429" in error_str: user_msg = "API kullanım limiti aşıldı."
                 elif "500" in error_str or "internal error" in error_str: user_msg = "Yapay zeka sunucusunda geçici sorun."
                 await ctx.reply(f"⚠️ {user_msg}", delete_after=15)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return

            gemini_response_text = ""
            try: gemini_response_text = response.text.strip()
            except ValueError as ve:
                 logger.warning(f".ask Gemini yanıtını okurken hata: {ve}. Prompt feedback kontrol ediliyor.")
                 gemini_response_text = ""
            except Exception as text_err:
                 logger.error(f".ask Gemini response.text okuma hatası: {text_err}")
                 gemini_response_text = ""

            finish_reason = None
            try: finish_reason = response.candidates[0].finish_reason.name
            except (IndexError, AttributeError): pass
            prompt_feedback_reason = None
            try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
            except AttributeError: pass

            if prompt_feedback_reason == "SAFETY":
                await ctx.reply("Girdiğiniz mesaj güvenlik filtrelerine takıldı.", delete_after=15)
                try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                except: pass
                return
            elif finish_reason == "SAFETY":
                 await ctx.reply("Yanıt güvenlik filtrelerine takıldı.", delete_after=15)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return
            elif finish_reason == "RECITATION":
                 await ctx.reply("Yanıt, alıntı filtrelerine takıldı.", delete_after=15)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return
            elif finish_reason == "OTHER":
                 await ctx.reply("Yanıt oluşturulamadı (bilinmeyen sebep).", delete_after=15)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return
            elif not gemini_response_text and finish_reason != "STOP":
                 logger.warning(f"Gemini'den .ask için boş yanıt alındı (Finish Reason: {finish_reason}).")
                 await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return
            elif not gemini_response_text:
                 logger.info(f"Gemini'den .ask için boş yanıt alındı (Normal bitiş).")
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return

        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)
        response_display = gemini_response_text if len(gemini_response_text) <= 1024 else gemini_response_text[:1021] + "..."
        embed.add_field(name="Yanıt", value=response_display, inline=False)
        footer_text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik silinecektir."
        if len(gemini_response_text) > 1024: footer_text += " (Yanıt kısaltıldı)"
        embed.set_footer(text=footer_text)

        bot_response_message = await ctx.reply(embed=embed, mention_author=False)
        logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")

        try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        except discord.errors.NotFound: pass
        except Exception as e: logger.warning(f".ask komut mesajı silinirken hata: {e}")

        if bot_response_message:
            try: await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY)
            except discord.errors.NotFound: pass
            except Exception as e: logger.warning(f".ask yanıt mesajı silinirken hata: {e}")

    except Exception as e:
        logger.error(f".ask genel hatası: {e}\n{traceback.format_exc()}")
        await ctx.reply("Sorunuz işlenirken beklenmedik bir hata oluştu.", delete_after=15)
        try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        except: pass

@ask_in_channel.error
async def ask_error(ctx, error):
    """ .ask komutuna özel cooldown hatasını yakalar """
    if isinstance(error, commands.CommandOnCooldown):
        delete_delay = max(5, int(error.retry_after) + 1)
        await ctx.send(f"⏳ `.ask` komutu için beklemedesiniz. Lütfen **{error.retry_after:.1f} saniye** sonra tekrar deneyin.", delete_after=delete_delay)
        try: await ctx.message.delete(delay=delete_delay)
        except: pass
    else: pass # Diğer hatalar genel işleyiciye

@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def list_available_models(ctx: commands.Context):
    """Sohbet için kullanılabilir Gemini ve DeepSeek modellerini listeler."""
    status_msg = await ctx.send("Kullanılabilir modeller kontrol ediliyor...", delete_after=5)

    async def fetch_gemini():
        if not GEMINI_API_KEY: return ["_(Gemini API anahtarı ayarlı değil)_"]
        try:
            gemini_models_list = []
            gemini_models = await asyncio.to_thread(genai.list_models)
            for m in gemini_models:
                if 'generateContent' in m.supported_generation_methods and m.name.startswith("models/"):
                    model_id = m.name.split('/')[-1]
                    prefix = ""
                    if "gemini-1.5-flash" in model_id: prefix = "⚡ "
                    elif "gemini-1.5-pro" in model_id: prefix = "✨ "
                    elif "gemini-pro" == model_id and "vision" not in model_id: prefix = "✅ "
                    elif "aqa" in model_id: prefix="❓ "
                    gemini_models_list.append(f"{GEMINI_PREFIX}{prefix}`{model_id}`")
            gemini_models_list.sort(key=lambda x: x.split('`')[1])
            return gemini_models_list if gemini_models_list else ["_(Kullanılabilir Gemini modeli bulunamadı)_"]
        except Exception as e:
            logger.error(f"Gemini modelleri listelenirken hata: {e}")
            return ["_(Gemini modelleri alınamadı - API Hatası)_"]

    async def fetch_deepseek():
        if not DEEPSEEK_API_KEY: return ["_(DeepSeek API anahtarı ayarlı değil)_"]
        # DeepSeek'in OpenAI SDK üzerinden model listeleme desteği yok, bilinenleri listele
        if not OPENAI_SDK_AVAILABLE: return ["_('openai' kütüphanesi bulunamadı)_"] # Kütüphane kontrolü
        try:
            deepseek_models_list = []
            # Bilinen DeepSeek modelleri (Dokümantasyona göre güncelleyin)
            known_deepseek_models = ["deepseek-chat", "deepseek-coder"]
            for model_id in known_deepseek_models:
                deepseek_models_list.append(f"{DEEPSEEK_PREFIX}🧭 `{model_id}`")

            deepseek_models_list.sort()
            return deepseek_models_list if deepseek_models_list else ["_(Bilinen DeepSeek modeli bulunamadı)_"]
        except Exception as e:
            logger.error(f"DeepSeek modelleri listelenirken hata: {e}")
            return ["_(DeepSeek modelleri listelenirken hata oluştu)_"]

    results = await asyncio.gather(fetch_gemini(), fetch_deepseek())
    all_models_list = []
    all_models_list.extend(results[0])
    all_models_list.extend(results[1])

    valid_models = [m for m in all_models_list if not m.startswith("_(")]
    error_models = [m for m in all_models_list if m.startswith("_(")]

    if not valid_models:
        error_text = "\n".join(error_models) if error_models else "API anahtarları ayarlanmamış veya bilinmeyen bir sorun var."
        await ctx.send(f"Kullanılabilir model bulunamadı.\n{error_text}")
        try: await status_msg.delete()
        except: pass
        return

    embed = discord.Embed(
        title="🤖 Kullanılabilir Yapay Zeka Modelleri",
        description=f"Bir sonraki özel sohbetiniz için `{ctx.prefix}setmodel <model_adi_on_ekli>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(valid_models),
        color=discord.Color.gold()
    )
    embed.add_field(name="Ön Ekler", value=f"`{GEMINI_PREFIX}` Gemini Modeli\n`{DEEPSEEK_PREFIX}` DeepSeek Modeli", inline=False)
    embed.set_footer(text="⚡ Flash, ✨ Pro (Gemini), ✅ Eski Pro (Gemini), 🧭 DeepSeek, ❓ AQA (Gemini)")

    if error_models:
         footer_text = embed.footer.text + "\nUyarılar: " + " ".join(error_models)
         embed.set_footer(text=footer_text[:1024])

    try: await status_msg.delete()
    except: pass
    await ctx.send(embed=embed)
    try: await ctx.message.delete()
    except: pass

@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx: commands.Context, *, model_id_with_or_without_prefix: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini veya DeepSeek modelini ayarlar."""
    global user_next_model
    if model_id_with_or_without_prefix is None:
        await ctx.send(f"Lütfen bir model adı belirtin (örn: `{GEMINI_PREFIX}gemini-1.5-flash-latest` veya `{DEEPSEEK_PREFIX}deepseek-chat`). Modeller için `{ctx.prefix}listmodels`.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    model_input = model_id_with_or_without_prefix.strip().replace('`', '')
    selected_model_full_name = None
    is_valid = False
    error_message = None

    if not model_input.startswith(GEMINI_PREFIX) and not model_input.startswith(DEEPSEEK_PREFIX):
         await ctx.send(f"❌ Lütfen model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_PREFIX}` ön ekini ekleyin. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=20)
         try: await ctx.message.delete(delay=20)
         except: pass
         return

    async with ctx.typing():
        if model_input.startswith(GEMINI_PREFIX):
            if not GEMINI_API_KEY: error_message = f"❌ Gemini API anahtarı ayarlı değil."; is_valid = False
            else:
                actual_model_name = model_input[len(GEMINI_PREFIX):]
                if not actual_model_name: error_message = "❌ Lütfen bir Gemini model adı belirtin."; is_valid = False
                else:
                    target_gemini_name = f"models/{actual_model_name}"
                    try:
                        await asyncio.to_thread(genai.get_model, target_gemini_name)
                        selected_model_full_name = model_input
                        is_valid = True
                        logger.info(f"{ctx.author.name} Gemini modelini doğruladı: {target_gemini_name}")
                    except Exception as e:
                        logger.warning(f"Geçersiz Gemini modeli denendi ({target_gemini_name}): {e}")
                        error_message = f"❌ `{actual_model_name}` geçerli veya erişilebilir bir Gemini modeli değil."
                        is_valid = False

        elif model_input.startswith(DEEPSEEK_PREFIX):
            if not DEEPSEEK_API_KEY: error_message = f"❌ DeepSeek API anahtarı ayarlı değil."; is_valid = False
            elif not OPENAI_SDK_AVAILABLE: error_message = f"❌ DeepSeek için gerekli 'openai' kütüphanesi bulunamadı."; is_valid = False
            else:
                actual_model_name = model_input[len(DEEPSEEK_PREFIX):]
                if not actual_model_name: error_message = "❌ Lütfen bir DeepSeek model adı belirtin."; is_valid = False
                else:
                    # DeepSeek model adını doğrudan API ile doğrulayamıyoruz (OpenAI SDK üzerinden).
                    # Sadece bilinen modelleri kontrol edebilir veya geçerli olduğunu varsayabiliriz.
                    known_deepseek = ["deepseek-chat", "deepseek-coder"] # Listeyi güncel tutun
                    if actual_model_name in known_deepseek:
                        selected_model_full_name = model_input
                        is_valid = True
                        logger.info(f"{ctx.author.name} DeepSeek modelini ayarladı: {actual_model_name}")
                    else:
                        # Bilinmeyen bir model adı girilirse uyarı verelim ama yine de ayarlayalım
                        logger.warning(f"{ctx.author.name}, bilinmeyen DeepSeek modeli ayarladı: {actual_model_name}. Yine de denenecek.")
                        selected_model_full_name = model_input
                        is_valid = True # Hata vermemesi için geçerli kabul edelim
                        error_message = f"⚠️ `{actual_model_name}` bilinen DeepSeek modelleri arasında yok, ancak denemek üzere ayarlandı." # Hata yerine uyarı
                        # VEYA: Sadece bilinenleri kabul etmek isterseniz:
                        # error_message = f"❌ `{actual_model_name}` bilinen DeepSeek modellerinden biri değil ({', '.join(known_deepseek)})."
                        # is_valid = False

    if is_valid and selected_model_full_name:
        user_next_model[ctx.author.id] = selected_model_full_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{selected_model_full_name}' olarak ayarlandı.")
        reply_msg = f"✅ Başarılı! Bir sonraki özel sohbetiniz `{selected_model_full_name}` modeli ile başlayacak."
        if error_message and error_message.startswith("⚠️"): # Uyarı varsa ekle
             reply_msg += f"\n{error_message}"
        await ctx.send(reply_msg, delete_after=20)
    else:
        final_error_msg = error_message if error_message else f"❌ `{model_input}` geçerli bir model adı değil veya bir sorun oluştu."
        await ctx.send(f"{final_error_msg} `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)

    try: await ctx.message.delete(delay=20)
    except: pass


@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_entry_channel(ctx: commands.Context, channel: discord.TextChannel = None):
    """Otomatik sohbet kanalı oluşturulacak giriş kanalını ayarlar (Admin)."""
    global entry_channel_id
    if channel is None:
        current_entry_channel_mention = "Ayarlanmamış"
        if entry_channel_id:
            try: current_ch = await bot.fetch_channel(entry_channel_id); current_entry_channel_mention = current_ch.mention
            except: current_entry_channel_mention = f"ID: {entry_channel_id} (Bulunamadı/Erişilemiyor)"
        await ctx.send(f"Lütfen bir metin kanalı etiketleyin veya ID'sini yazın (örn: `{ctx.prefix}setentrychannel #genel-sohbet`).\nMevcut giriş kanalı: {current_entry_channel_mention}")
        return

    perms = channel.permissions_for(ctx.guild.me)
    if not perms.view_channel or not perms.send_messages or not perms.manage_messages:
         await ctx.send(f"❌ Bu kanalda ({channel.mention}) gerekli izinlerim (Gör, Mesaj Gönder, Mesajları Yönet) yok.")
         return

    entry_channel_id = channel.id
    save_config('entry_channel_id', entry_channel_id)
    logger.info(f"Giriş kanalı yönetici {ctx.author.name} tarafından {channel.mention} (ID: {channel.id}) olarak ayarlandı.")
    await ctx.send(f"✅ Giriş kanalı başarıyla {channel.mention} olarak ayarlandı.")
    try:
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için #{channel.name}"))
    except Exception as e:
        logger.warning(f"Giriş kanalı ayarlandıktan sonra bot aktivitesi güncellenemedi: {e}")

@bot.command(name='settimeout', aliases=['zamanasimi'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_inactivity_timeout(ctx: commands.Context, hours: str = None):
    """Geçici kanalların aktif olmazsa silineceği süreyi saat cinsinden ayarlar (Admin). 0 = kapatır."""
    global inactivity_timeout
    current_timeout_hours = 'Kapalı'
    if inactivity_timeout:
        current_timeout_hours = f"{inactivity_timeout.total_seconds()/3600:.2f}"

    if hours is None:
        await ctx.send(f"Lütfen pozitif bir saat değeri girin (örn: `{ctx.prefix}settimeout 2.5`) veya kapatmak için `0` yazın.\nMevcut: `{current_timeout_hours}` saat")
        return

    try:
        hours_float = float(hours)
        if hours_float < 0:
            await ctx.send("Lütfen pozitif bir saat değeri veya `0` girin.")
            return
        if hours_float == 0:
             inactivity_timeout = None
             save_config('inactivity_timeout_hours', '0')
             logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından kapatıldı.")
             await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **kapatıldı**.")
        elif hours_float < 0.1:
             await ctx.send("Minimum zaman aşımı 0.1 saattir (6 dakika). Kapatmak için 0 girin.")
             return
        elif hours_float > 720:
             await ctx.send("Maksimum zaman aşımı 720 saattir (30 gün).")
             return
        else:
            inactivity_timeout = datetime.timedelta(hours=hours_float)
            save_config('inactivity_timeout_hours', str(hours_float))
            logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours_float} saat olarak ayarlandı.")
            await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **{hours_float:.2f} saat** olarak ayarlandı.")

    except ValueError:
         await ctx.send(f"Geçersiz saat değeri: '{hours}'. Lütfen sayısal bir değer girin (örn: 1, 0.5, 0).")

@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx: commands.Context):
    """Botun kullanılabilir komutlarını listeler."""
    entry_channel_mention = "Ayarlanmamış"
    if entry_channel_id:
        try:
            entry_channel = await bot.fetch_channel(entry_channel_id)
            entry_channel_mention = entry_channel.mention if entry_channel else f"ID: {entry_channel_id} (Bulunamadı/Erişilemiyor)"
        except discord.errors.NotFound: entry_channel_mention = f"ID: {entry_channel_id} (Bulunamadı)"
        except discord.errors.Forbidden: entry_channel_mention = f"ID: {entry_channel_id} (Erişim Yok)"
        except Exception as e: logger.warning(f"Yardım komutunda giriş kanalı alınırken hata: {e}"); entry_channel_mention = f"ID: {entry_channel_id} (Hata)"

    embed = discord.Embed(title=f"{bot.user.name} Komut Listesi", description=f"**Özel Sohbet Başlatma:**\n{entry_channel_mention} kanalına mesaj yazın.\n\n**Diğer Komutlar:**", color=discord.Color.dark_purple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)

    user_cmds, chat_cmds, admin_cmds = [], [], []
    for command in sorted(bot.commands, key=lambda cmd: cmd.name):
        if command.hidden: continue

        help_text = command.help or command.short_doc or "Açıklama yok."
        help_text = help_text.split('\n')[0]
        aliases = f" (Diğer: `{'`, `'.join(command.aliases)}`)" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""
        cmd_string = f"`{ctx.prefix}{command.name}{params}`{aliases}\n_{help_text}_"

        is_admin_cmd = any(isinstance(check, type(commands.has_permissions)) or isinstance(check, type(commands.is_owner)) for check in command.checks)
        is_super_admin_cmd = False
        if command.name in ['setentrychannel', 'settimeout']: is_super_admin_cmd = True

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
    try:
        await ctx.send(embed=embed)
        try: await ctx.message.delete()
        except: pass
    except discord.errors.HTTPException as e:
        logger.error(f"Yardım mesajı gönderilemedi (çok uzun olabilir): {e}")
        await ctx.send("Komut listesi çok uzun olduğu için gönderilemedi.")


# --- Genel Hata Yakalama ---
@bot.event
async def on_command_error(ctx: commands.Context, error):
    """Komutlarla ilgili hataları merkezi olarak yakalar."""
    original_error = getattr(error, 'original', error)

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CommandOnCooldown):
        if ctx.command and ctx.command.name == 'ask': return # Ask kendi hatasını yönetiyor
        delete_delay = max(5, int(error.retry_after) + 1)
        await ctx.send(f"⏳ `{ctx.command.qualified_name}` komutu için beklemedesiniz. Lütfen **{error.retry_after:.1f} saniye** sonra tekrar deneyin.", delete_after=delete_delay)
        try: await ctx.message.delete(delay=delete_delay)
        except Exception: pass
        return

    if isinstance(error, commands.UserInputError):
        delete_delay = 15
        command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with
        usage = f"`{ctx.prefix}{command_name} {ctx.command.signature if ctx.command else ''}`".replace('=None', '').replace('= Ellipsis', '...')
        error_message = "Hatalı komut kullanımı."
        if isinstance(error, commands.MissingRequiredArgument): error_message = f"Eksik argüman: `{error.param.name}`."
        elif isinstance(error, commands.BadArgument): error_message = f"Geçersiz argüman türü: {error}"
        elif isinstance(error, commands.TooManyArguments): error_message = "Çok fazla argüman girdiniz."
        await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay)
        try: await ctx.message.delete(delay=delete_delay)
        except Exception: pass
        return

    delete_user_msg = True
    delete_delay = 10

    if isinstance(error, commands.MissingPermissions):
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {original_error.missing_permissions}")
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        delete_delay = 15
        await ctx.send(f"⛔ Üzgünüm {ctx.author.mention}, bu komutu kullanmak için şu izinlere sahip olmalısın: **{perms}**", delete_after=delete_delay)
    elif isinstance(error, commands.BotMissingPermissions):
        logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {original_error.missing_permissions}")
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        delete_delay = 15
        delete_user_msg = False
        await ctx.send(f"🆘 Benim bu komutu çalıştırmak için şu izinlere sahip olmam gerekiyor: **{perms}**", delete_after=delete_delay)
    elif isinstance(error, commands.CheckFailure):
        logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {error}")
        user_msg = "🚫 Bu komutu burada veya bu şekilde kullanamazsınız."
        if isinstance(error, commands.NoPrivateMessage): user_msg = "🚫 Bu komut sadece sunucu kanallarında kullanılabilir."; delete_user_msg = False
        elif isinstance(error, commands.PrivateMessageOnly): user_msg = "🚫 Bu komut sadece özel mesajla (DM) kullanılabilir."
        elif isinstance(error, commands.NotOwner): user_msg = "🚫 Bu komutu sadece bot sahibi kullanabilir."
        try: await ctx.author.send(user_msg)
        except Exception: pass
        await ctx.send(user_msg, delete_after=delete_delay)
    else:
        logger.error(f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' işlenirken beklenmedik hata: {type(original_error).__name__}: {original_error}")
        traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__))
        logger.error(f"Traceback:\n{traceback_str}")
        delete_delay = 15
        await ctx.send("⚙️ Komut işlenirken beklenmedik bir hata oluştu. Sorun devam ederse lütfen geliştirici ile iletişime geçin.", delete_after=delete_delay)

    if delete_user_msg and ctx.guild:
        try: await ctx.message.delete(delay=delete_delay)
        except discord.errors.NotFound: pass
        except discord.errors.Forbidden: pass
        except Exception as e: logger.warning(f"Komut hatası sonrası mesaj silinemedi: {e}")

# === Render/Koyeb için Web Sunucusu ===
app = Flask(__name__)
@app.route('/')
def home():
    """Basit bir sağlık kontrolü endpoint'i."""
    if bot and bot.is_ready():
        try:
            guild_count = len(bot.guilds)
            active_chats = len(temporary_chat_channels)
            return f"Bot '{bot.user.name}' çalışıyor. {guild_count} sunucu. {active_chats} aktif sohbet (state).", 200
        except Exception as e:
             logger.error(f"Sağlık kontrolü sırasında hata: {e}")
             return "Bot çalışıyor ama durum alınırken hata oluştu.", 500
    elif bot and not bot.is_ready():
        return "Bot başlatılıyor, henüz hazır değil...", 503
    else:
        return "Bot durumu bilinmiyor veya başlatılamadı.", 500

def run_webserver():
    """Flask web sunucusunu ayrı bir thread'de çalıştırır."""
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        logger.info(f"Flask web sunucusu http://{host}:{port} adresinde başlatılıyor...")
        app.run(host=host, port=port, debug=False)
    except Exception as e:
        logger.critical(f"Web sunucusu başlatılırken KRİTİK HATA: {e}")
# ===================================

# --- Botu Çalıştır ---
if __name__ == "__main__":
    if not DISCORD_TOKEN: logger.critical("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı!"); exit()
    if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); exit()
    # API anahtarı kontrolleri zaten yukarıda yapıldı.

    logger.info("Bot başlatılıyor...")
    webserver_thread = None
    try:
        # Web sunucusunu ayrı thread'de başlat
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()

        # Botu çalıştır
        bot.run(DISCORD_TOKEN, log_handler=None)

    except discord.errors.LoginFailure:
        logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired:
        logger.critical("HATA: Gerekli Intent'ler (Members, Message Content) Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err:
        logger.critical(f"PostgreSQL bağlantı hatası (Başlangıçta): {db_err}")
    except ImportError as import_err:
        # OpenAI SDK import hatasını tekrar kontrol et, eğer başlatılamıyorsa kritik
        if "openai" in str(import_err) and not OPENAI_SDK_AVAILABLE:
             logger.critical(f"HATA: Gerekli 'openai' kütüphanesi bulunamadı ve başlatılamadı. {import_err}")
        else:
             logger.critical(f"Bot çalıştırılırken kritik import hatası: {import_err}")
             logger.critical(traceback.format_exc())
    except Exception as e:
        logger.critical(f"Bot çalıştırılırken kritik hata: {type(e).__name__}: {e}")
        logger.critical(traceback.format_exc())
    finally:
        logger.info("Bot kapatılıyor...")