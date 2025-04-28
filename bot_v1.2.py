
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
# DeepSeek kütüphanesini import etmeyi dene

logger = logging.getLogger('discord_ai_bot') # Mevcut logger'ı kullan
DEEPSEEK_AVAILABLE = False # Başlangıçta False yapalım
DeepSeekClient = None      # None olarak başlatalım

try:
    from deepseek import DeepSeekClient
    DEEPSEEK_AVAILABLE = True
    logger.info(">>> DEBUG: DeepSeek kütüphanesi başarıyla import edildi.") # INFO seviyesinde logla
except ImportError:
    # ImportError özelinde loglama (Bu normalde beklenen hata)
    logger.warning(">>> DEBUG: DeepSeek kütüphanesi import edilemedi (ImportError).")
except Exception as e:
    # Diğer TÜM hataları yakala ve logla
    logger.error(f">>> DEBUG: DeepSeek import sırasında beklenmedik bir HATA oluştu: {type(e).__name__}: {e}", exc_info=True)
    # exc_info=True traceback'i de loglar
    
import sys
import subprocess
print("Python Path:", sys.path)
try:
    result = subprocess.run([sys.executable, '-m', 'pip', 'show', 'deepseek'], capture_output=True, text=True, check=True)
    print("pip show deepseek output:\n", result.stdout)
except Exception as e:
    print(f"'pip show deepseek' çalıştırılamadı: {e}")

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
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") # YENİ

# API Anahtarı Kontrolleri
if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); exit()
if not GEMINI_API_KEY and not DEEPSEEK_API_KEY:
    logger.critical("HATA: Ne Gemini ne de DeepSeek API Anahtarı bulunamadı! En az biri gerekli.")
    exit()
if not GEMINI_API_KEY:
    logger.warning("UYARI: Gemini API Anahtarı bulunamadı! Gemini modelleri kullanılamayacak.")
if not DEEPSEEK_API_KEY:
    logger.warning("UYARI: DeepSeek API Anahtarı bulunamadı! DeepSeek modelleri kullanılamayacak.")
elif not DEEPSEEK_AVAILABLE: # elif kullanmak önemli
    # Hata logunu BURAYA taşıyalım, çünkü API anahtarı VARSA ama kütüphane YOKSA bu hatayı vermeliyiz.
    logger.error("HATA: DeepSeek API anahtarı bulundu ancak 'deepseek' kütüphanesi yüklenemedi/import edilemedi. Lütfen kurulumu ve önceki logları kontrol edin.")
    DEEPSEEK_API_KEY = None # Kütüphane yoksa anahtarı yok say

# Render PostgreSQL bağlantısı için
DATABASE_URL = os.getenv("DATABASE_URL")

# Model Ön Ekleri ve Varsayılanlar
GEMINI_PREFIX = "gs:"
DEEPSEEK_PREFIX = "ds:"
DEFAULT_GEMINI_MODEL_NAME = 'gemini-1.5-flash-latest' # Prefixsiz temel ad
DEFAULT_MODEL_NAME = f"{GEMINI_PREFIX}{DEFAULT_GEMINI_MODEL_NAME}" # Kullanılacak varsayılan (ön ekli)

# Varsayılan değerler (DB'den veya ortamdan okunamzsa)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Global Değişkenler ---
entry_channel_id = None
inactivity_timeout = None

# Aktif sohbet oturumları ve geçmişleri
# Yapı: channel_id -> {'model': 'prefix:model_name', 'session': GeminiSession or None, 'history': DeepSeekHistoryList or None}
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
        # Render'daki DB'ler genellikle SSL gerektirir
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except psycopg2.DatabaseError as e:
        logger.error(f"PostgreSQL bağlantı hatası: {e}")
        raise # Hatanın yukarıya bildirilmesini sağla

def setup_database():
    """PostgreSQL tablolarını oluşturur (varsa dokunmaz)."""
    conn = None
    try:
        conn = db_connect()
        cursor = conn.cursor()
        # config tablosu
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # temp_channels tablosu (varsayılan model ön ekli)
        default_model_with_prefix_for_db = DEFAULT_MODEL_NAME # Zaten prefix içeriyor
        # DİKKAT: F-string'i doğrudan SQL'de kullanmak SQL enjeksiyonu riski taşır.
        # Güvenli bir ortamda veya değerin kontrol edildiği varsayılarak kullanılmıştır.
        # Daha güvenli yöntem, parametreleri kullanmaktır, ancak CREATE TABLE DEFAULT için bu biraz daha karmaşıktır.
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_active TIMESTAMPTZ NOT NULL,
                model_name TEXT DEFAULT %s
            )
        ''', (default_model_with_prefix_for_db,)) # Parametre kullanarak daha güvenli hale getirme
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
                last_active_dt = row['last_active'] # TIMESTAMPTZ zaten timezone aware olmalı
                # DB'deki model adını al, boşsa veya prefix içermiyorsa varsayılana dön (güvenlik için)
                model_name_db = row['model_name']
                if not model_name_db or (not model_name_db.startswith(GEMINI_PREFIX) and not model_name_db.startswith(DEEPSEEK_PREFIX)):
                    logger.warning(f"DB'de geçersiz model adı bulundu (channel_id: {row['channel_id']}), varsayılana dönülüyor: {DEFAULT_MODEL_NAME}")
                    model_name_db = DEFAULT_MODEL_NAME
                    # DB'yi de düzeltmek iyi olur
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
        # psycopg2'nin timestamp'leri UTC'ye dönüştürmesi beklenir, ancak emin olmak için kontrol edelim
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
# DB URL kontrolü
if not DATABASE_URL:
    logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı! Render PostgreSQL eklendi mi?")
    exit()
# DB Kurulumu
setup_database() # setup_database içinde hata olursa exit() çağrılır

# Diğer ayarların yüklenmesi
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
        # .ask komutu için varsayılan modeli oluşturmayı dene
        try:
             # Prefixsiz ismi kullan
             gemini_default_model_instance = genai.GenerativeModel(f"models/{DEFAULT_GEMINI_MODEL_NAME}")
             logger.info(f".ask komutu için varsayılan Gemini modeli ('{DEFAULT_GEMINI_MODEL_NAME}') yüklendi.")
        except Exception as model_error:
             logger.error(f"HATA: Varsayılan Gemini modeli ('{DEFAULT_GEMINI_MODEL_NAME}') oluşturulamadı: {model_error}")
             gemini_default_model_instance = None # Başarısız oldu
    except Exception as configure_error:
        logger.error(f"HATA: Gemini API genel yapılandırma hatası: {configure_error}")
        GEMINI_API_KEY = None # Yapılandırma hatası varsa API anahtarını yok say
else:
    logger.warning("Gemini API anahtarı ayarlanmadığı için Gemini özellikleri devre dışı.")

# DeepSeek API'si zaten kontrol edildi.

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
    safe_username = safe_username[:80] # İlk 80 karakteri al
    base_channel_name = f"sohbet-{safe_username}"
    channel_name = base_channel_name
    counter = 1
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}

    # Kanal adı 100 karakteri geçmemeli
    while len(channel_name) > 100 or channel_name.lower() in existing_channel_names:
        potential_name = f"{base_channel_name[:90]}-{counter}" # Uzunsa base'i kısalt
        if len(potential_name) > 100: potential_name = f"{base_channel_name[:80]}-long-{counter}" # Daha da kısalt
        channel_name = potential_name[:100] # Son kez 100'e kırp
        counter += 1
        if counter > 1000:
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı).")
            # Son çare olarak ID ve zaman damgası kullan
            timestamp_str = datetime.datetime.now().strftime('%M%S%f')[:-3] # Milisaniye hassasiyeti
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
            conn.close() # Bağlantıyı hemen kapat

            current_model_with_prefix = result['model_name'] if result and result['model_name'] else DEFAULT_MODEL_NAME
            # Güvenlik: DB'den gelen model adının geçerli bir prefix'e sahip olduğunu kontrol et
            if not current_model_with_prefix.startswith(GEMINI_PREFIX) and not current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                 logger.warning(f"DB'den geçersiz prefix'li model adı okundu ({current_model_with_prefix}), varsayılana dönülüyor.")
                 current_model_with_prefix = DEFAULT_MODEL_NAME
                 update_channel_model_db(channel_id, DEFAULT_MODEL_NAME) # DB'yi düzelt

            logger.info(f"'{channel.name}' (ID: {channel_id}) için AI sohbet oturumu {current_model_with_prefix} ile başlatılıyor.")

            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                if not GEMINI_API_KEY: raise ValueError("Gemini API anahtarı ayarlı değil.")
                actual_model_name = current_model_with_prefix[len(GEMINI_PREFIX):]
                target_gemini_name = f"models/{actual_model_name}"
                try:
                    # Modelin varlığını kontrol et
                    await asyncio.to_thread(genai.get_model, target_gemini_name)
                    gemini_model_instance = genai.GenerativeModel(target_gemini_name)
                except Exception as model_err:
                    logger.error(f"Gemini modeli '{target_gemini_name}' yüklenemedi/bulunamadı: {model_err}. Varsayılana dönülüyor.")
                    current_model_with_prefix = DEFAULT_MODEL_NAME # Varsayılana dön
                    update_channel_model_db(channel_id, DEFAULT_MODEL_NAME) # DB'yi düzelt
                    if not GEMINI_API_KEY: raise ValueError("Varsayılan Gemini için de API anahtarı yok.") # Hata fırlat devam etmesin
                    actual_model_name = DEFAULT_MODEL_NAME[len(GEMINI_PREFIX):]
                    gemini_model_instance = genai.GenerativeModel(f"models/{actual_model_name}")

                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': gemini_model_instance.start_chat(history=[]),
                    'history': None
                }
            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                if not DEEPSEEK_API_KEY: raise ValueError("DeepSeek API anahtarı ayarlı değil.")
                if not DEEPSEEK_AVAILABLE: raise ImportError("DeepSeek kütüphanesi yüklenemedi.")
                # DeepSeek için model varlığı kontrolü API ile yapılabilirse eklenebilir.
                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': None,
                    'history': [] # Boş geçmiş listesi başlat
                }
            else: # Bu duruma yukarıdaki kontrolle gelinmemeli
                raise ValueError(f"Tanımsız model ön eki: {current_model_with_prefix}")

        except (psycopg2.DatabaseError, ValueError, ImportError) as init_err:
             logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı (DB/Config/Import): {init_err}")
             try: await channel.send("Yapay zeka oturumu başlatılamadı. Veritabanı, yapılandırma veya kütüphane sorunu.", delete_after=15)
             except discord.errors.NotFound: pass
             except Exception as send_err: logger.warning(f"Oturum başlatma hata mesajı gönderilemedi: {send_err}")
             # Hata durumunda kanalı state'den temizle, DB'den sil
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

    async with channel.typing():
        try:
            # --- API Çağrısı (Modele Göre) ---
            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                gemini_session = chat_data.get('session') # Use .get for safety
                if not gemini_session: raise ValueError("Gemini oturumu bulunamadı.")
                # Gemini API çağrısı (asenkron)
                response = await gemini_session.send_message_async(prompt_text)
                ai_response_text = response.text.strip()

                # Gemini güvenlik/hata kontrolü
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
                     ai_response_text = None # Güvenlik nedeniyle yanıt yok
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
                elif not ai_response_text and not error_occurred: # Yanıt yok ama hata da yoksa?
                     logger.warning(f"Gemini'den boş yanıt alındı, finish_reason: {finish_reason} (Kanal: {channel_id})")
                     # Bunu hata olarak işaretlemeyebiliriz, model bazen boş dönebilir.


            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                if not DEEPSEEK_AVAILABLE: raise ImportError("DeepSeek kütüphanesi kullanılamıyor.")
                history = chat_data.get('history')
                if history is None: raise ValueError("DeepSeek geçmişi bulunamadı.") # History listesi olmalı

                actual_model_name = current_model_with_prefix[len(DEEPSEEK_PREFIX):]
                history.append({"role": "user", "content": prompt_text})

                # DeepSeek istemcisini oluştur (API anahtarı başta kontrol edildi)
                client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)

                # API çağrısını thread'de yap
                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=actual_model_name,
                    messages=history,
                    # max_tokens=1024, # Gerekirse eklenebilir
                    # temperature=0.7 # Gerekirse eklenebilir
                )

                if response.choices:
                    choice = response.choices[0]
                    ai_response_text = choice.message.content.strip()
                    finish_reason = choice.finish_reason

                    if ai_response_text: # Sadece geçerli yanıt varsa geçmişe ekle
                        history.append({"role": "assistant", "content": ai_response_text})
                    else: # İçerik boşsa
                         logger.warning(f"DeepSeek'ten boş içerikli yanıt alındı. Finish Reason: {finish_reason}")
                         # Kullanıcıya bilgi verilebilir ama şimdilik sessiz kalalım.

                    if finish_reason == 'length':
                        logger.warning(f"DeepSeek yanıtı max_tokens sınırına ulaştı (model: {actual_model_name}, Kanal: {channel_id})")
                        # Hata olarak işaretlemeye gerek yok, yanıtın bir kısmı geldi.
                    elif finish_reason == 'content_filter': # DeepSeek'in güvenlik filtresi finish_reason'ı bu ise
                        user_error_msg = "DeepSeek yanıtı içerik filtrelerine takıldı."
                        error_occurred = True
                        logger.warning(f"DeepSeek content filter block (Kanal: {channel_id}).")
                        history.pop() # Başarısız isteği geçmişten çıkar (hem user hem assistant)
                        if history and history[-1]["role"] == "user": history.pop() # Kullanıcı mesajını da çıkar
                        ai_response_text = None # Yanıt yok
                    elif finish_reason != 'stop' and not ai_response_text: # Durma sebebi 'stop' değilse ve yanıt yoksa
                        user_error_msg = f"DeepSeek yanıtı beklenmedik bir sebeple durdu ({finish_reason})."
                        error_occurred = True
                        logger.warning(f"DeepSeek unexpected finish reason: {finish_reason} (Kanal: {channel_id}).")
                        history.pop() # Başarısız isteği geçmişten çıkar (hem user hem assistant)
                        if history and history[-1]["role"] == "user": history.pop() # Kullanıcı mesajını da çıkar
                        ai_response_text = None

                else: # choices listesi boşsa
                    usage_info = response.usage if hasattr(response, 'usage') else 'Yok'
                    logger.warning(f"[AI CHAT/{current_model_with_prefix}] DeepSeek'ten boş 'choices' listesi alındı. Usage: {usage_info} (Kanal: {channel_id})")
                    user_error_msg = "DeepSeek'ten bir yanıt alınamadı (boş 'choices')."
                    error_occurred = True
                    history.pop() # Kullanıcı mesajını çıkar

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
                        await asyncio.sleep(0.5) # Rate limit'e takılmamak için küçük bekleme
                else:
                    await channel.send(ai_response_text)

                # Başarılı ise aktivite zamanını güncelle
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                channel_last_active[channel_id] = now_utc
                update_channel_activity_db(channel_id, now_utc)
                warned_inactive_channels.discard(channel_id) # Aktivite oldu, uyarıyı kaldır
                return True # Başarılı dönüş
            elif not error_occurred and not ai_response_text:
                 # Hata yok ama yanıt da yok (örn. Gemini boş döndü), sessiz kalabiliriz.
                 logger.info(f"AI'dan boş yanıt alındı, mesaj gönderilmiyor (Kanal: {channel_id}).")
                 # Aktivite zamanını yine de güncelleyebiliriz, çünkü işlem yapıldı.
                 now_utc = datetime.datetime.now(datetime.timezone.utc)
                 channel_last_active[channel_id] = now_utc
                 update_channel_activity_db(channel_id, now_utc)
                 warned_inactive_channels.discard(channel_id) # Aktivite oldu, uyarıyı kaldır
                 return True # İşlem başarılı sayılır

        except ImportError as e: # Deepseek kütüphanesi yoksa
             logger.error(f"Gerekli kütüphane bulunamadı: {e}")
             error_occurred = True
             user_error_msg = "Gerekli yapay zeka kütüphanesi sunucuda bulunamadı."
             # Oturumu/geçmişi temizleyip DB'den kaldırabiliriz çünkü bu model kullanılamaz
             active_ai_chats.pop(channel_id, None)
             remove_temp_channel_db(channel_id)
        except genai.types.StopCandidateException as stop_e: # Gemini özel hatası
            logger.error(f"Gemini StopCandidateException (Kanal: {channel_id}): {stop_e}")
            error_occurred = True
            user_error_msg = "Gemini yanıtı beklenmedik bir şekilde durdu."
        except genai.types.BlockedPromptException as block_e: # Gemini özel hatası
            logger.warning(f"Gemini BlockedPromptException (Kanal: {channel_id}): {block_e}")
            error_occurred = True
            user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı (BlockedPromptException)."
        except Exception as e:
            logger.error(f"[AI CHAT/{current_model_with_prefix}] API/İşlem hatası (Kanal: {channel_id}): {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            error_occurred = True
            error_str = str(e).lower()
            # Spesifik hata mesajları
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
            # Güvenlik hataları yukarıda ele alındı

            # Eğer hata DeepSeek geçmişiyle ilgiliyse, geçmişi sıfırlamayı dene
            if DEEPSEEK_PREFIX in current_model_with_prefix and 'history' in locals() and isinstance(e, (TypeError, ValueError, AttributeError)) and "history" in error_str : # Örnek kontrol
                 logger.warning(f"DeepSeek geçmişiyle ilgili potansiyel hata, geçmiş sıfırlanıyor: {channel_id}")
                 if channel_id in active_ai_chats and 'history' in active_ai_chats[channel_id]:
                      active_ai_chats[channel_id]['history'] = [] # Geçmişi sıfırla
                 user_error_msg += " (Konuşma geçmişi olası bir hata nedeniyle sıfırlandı.)"


    # Hata oluştuysa kullanıcıya mesaj gönder
    if error_occurred:
        try:
            await channel.send(f"⚠️ {user_error_msg}", delete_after=20)
        except discord.errors.NotFound: pass # Kanal silinmiş olabilir
        except Exception as send_err: logger.warning(f"Hata mesajı gönderilemedi (Kanal: {channel_id}): {send_err}")
        return False # Başarısız dönüş

    # Normalde buraya gelinmemeli ama her ihtimale karşı
    return False

# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'{bot.user} olarak giriş yapıldı (ID: {bot.user.id}).')

    # Ayarları yükle (DB'den) - Tekrar yüklemeye gerek yok, başlangıçta yüklendi.
    # Sadece loglayalım
    logger.info(f"Mevcut Ayarlar - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")

    # Kalıcı verileri temizle ve DB'den yükle
    logger.info("Kalıcı veriler (geçici kanallar) yükleniyor...");
    temporary_chat_channels.clear()
    user_to_channel_map.clear()
    channel_last_active.clear()
    active_ai_chats.clear() # Başlangıçta sohbet oturumlarını temizle
    warned_inactive_channels.clear()

    loaded_channels = load_all_temp_channels()
    valid_channel_count = 0
    invalid_channel_ids = []
    guild_ids = {g.id for g in bot.guilds} # Botun bulunduğu sunucuların ID'leri

    for ch_id, u_id, last_active_ts, ch_model_name_with_prefix in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel) and channel_obj.guild.id in guild_ids:
            # Kanalın bulunduğu sunucu botun erişiminde mi kontrol et
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            # last_active_ts DB'den geldiği için zaten timezone aware olmalı (TIMESTAMPTZ)
            channel_last_active[ch_id] = last_active_ts
            # Başlangıçta aktif sohbet oturumu oluşturmuyoruz, ilk mesaja kadar bekliyoruz.
            valid_channel_count += 1
            # logger.debug(f"Kanal yüklendi: {ch_id} (User: {u_id}, LastActive: {last_active_ts}, Model: {ch_model_name_with_prefix})")
        else:
            reason = "Discord'da bulunamadı/geçersiz"
            if channel_obj and channel_obj.guild.id not in guild_ids:
                 reason = f"Bot artık '{channel_obj.guild.name}' sunucusunda değil"
            elif not channel_obj:
                 reason = "Discord'da bulunamadı"

            logger.warning(f"DB'deki geçici kanal {ch_id} yüklenemedi ({reason}). DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)

    for invalid_id in invalid_channel_ids:
        remove_temp_channel_db(invalid_id)

    logger.info(f"{valid_channel_count} geçerli geçici kanal DB'den yüklendi.")
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")

    # Bot aktivitesini ayarla
    entry_channel_name = "Ayarlanmadı"
    try:
        if entry_channel_id:
            entry_channel = await bot.fetch_channel(entry_channel_id) # fetch_channel kullanmak daha garanti
            if entry_channel: entry_channel_name = f"#{entry_channel.name}"
            else: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı veya erişilemiyor.")
        elif not entry_channel_id:
             entry_channel_name = "Ayarlanmadı"

        activity_text = f"Sohbet için {entry_channel_name}"
        await bot.change_presence(activity=discord.Game(name=activity_text))
        logger.info(f"Bot aktivitesi ayarlandı: '{activity_text}'")
    except discord.errors.NotFound:
        logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı (aktivite ayarlanırken).")
        await bot.change_presence(activity=discord.Game(name="Sohbet için kanal?"))
    except Exception as e:
        logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    # İnaktivite kontrol görevini başlat
    if not check_inactivity.is_running():
        check_inactivity.start()
        logger.info("İnaktivite kontrol görevi başlatıldı.")

    logger.info("Bot komutları ve mesajları dinliyor..."); print("-" * 20)

@bot.event
async def on_message(message: discord.Message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    if message.author == bot.user or message.author.bot: return
    if not message.guild: return # DM mesajlarını veya group DM'leri yoksay
    if not isinstance(message.channel, discord.TextChannel): return # Sadece text kanalları

    # Giriş kanalı ID'sini on_ready'de global olarak ayarladığımız için tekrar kontrol etmeye gerek yok.
    # if entry_channel_id is None: return # Giriş kanalı ayarlı değilse işlem yapma (artık gereksiz)

    author = message.author
    author_id = author.id
    channel = message.channel
    channel_id = channel.id
    guild = message.guild

    # Komut kontrolü - Önce komutları işle
    ctx = await bot.get_context(message)
    if ctx.command: # Eğer mesaj bir komut çağrısı ise
        await bot.process_commands(message)
        return # Komut işlendi, AI işlemesini atla

    # --- Otomatik Kanal Oluşturma (Giriş Kanalında) ---
    if entry_channel_id and channel_id == entry_channel_id:
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]
            active_channel = bot.get_channel(active_channel_id)
            if active_channel: # Kanal hala varsa
                 mention = active_channel.mention
                 logger.info(f"{author.name} giriş kanalına yazdı ama aktif kanalı var: {mention}")
                 try:
                     info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15)
                     await message.delete(delay=15)
                 except discord.errors.NotFound: pass # Mesajlar zaten silinmiş olabilir
                 except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
                 return
            else: # Kanal state'de var ama Discord'da yoksa (nadiren olabilir)
                 logger.warning(f"{author.name} için map'te olan kanal ({active_channel_id}) bulunamadı. Map temizleniyor.")
                 user_to_channel_map.pop(author_id, None)
                 # DB'den de silinmeli (on_guild_channel_delete tetiklenmemiş olabilir)
                 remove_temp_channel_db(active_channel_id)
                 # Kullanıcı tekrar yazabilir, işleme devam et

        initial_prompt = message.content
        original_message_id = message.id
        if not initial_prompt.strip():
             logger.info(f"{author.name} giriş kanalına boş mesaj gönderdi, yoksayılıyor.")
             try: await message.delete()
             except: pass
             return

        # Seçilen modeli al (ön ekli)
        chosen_model_with_prefix = user_next_model.pop(author_id, DEFAULT_MODEL_NAME)
        logger.info(f"{author.name} giriş kanalına yazdı, {chosen_model_with_prefix} ile kanal oluşturuluyor...")

        # Kanal oluşturmadan önce bilgilendirme mesajı
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
            # DB'ye ön ekli modeli kaydet
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_with_prefix)

            # Hoşgeldin mesajı (Prefixsiz model adını göster)
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

                 prefix = bot.command_prefix[0] # İlk prefix'i kullan
                 embed.add_field(name="🛑 Kapat", value=f"`{prefix}endchat`", inline=True)
                 embed.add_field(name="🔄 Model Seç (Sonraki)", value=f"`{prefix}setmodel <model>`", inline=True)
                 embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{prefix}resetchat`", inline=True)
                 await new_channel.send(embed=embed)
            except Exception as e:
                 logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                 try: await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.")
                 except Exception as fallback_e: logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")

            # Kullanıcıya bilgi ver (Giriş kanalında - önceki mesajı silip yenisini gönder)
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            try: await channel.send(f"{author.mention}, özel sohbet kanalı {new_channel.mention} oluşturuldu! Oradan devam edebilirsin.", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            # Kullanıcının ilk mesajını yeni kanala göndermeye gerek yok, AI'ye direkt prompt olarak veriyoruz.
            # await new_channel.send(f"**{author.display_name}:** {initial_prompt}") # Bu satıra gerek yok

            # AI'ye ilk isteği gönder
            try:
                logger.info(f"-----> AI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_with_prefix})")
                success = await send_to_ai_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success: logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else: logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e: logger.error(f"İlk mesaj işlenirken/gönderilirken hata: {e}")

            # Orijinal mesajı sil
            try:
                # Orijinal mesaj nesnesini tekrar almak yerine ID ile silmeyi dene
                await channel.delete_messages([discord.Object(id=original_message_id)])
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except discord.errors.NotFound: pass # Zaten silinmiş olabilir
            except discord.errors.Forbidden: logger.warning(f"Giriş kanalında ({channel_id}) mesaj silme izni yok.")
            except Exception as e: logger.warning(f"Giriş kanalındaki orijinal mesaj ({original_message_id}) silinirken hata: {e}")
        else: # new_channel None ise (oluşturulamadı)
            if processing_msg:
                try: await processing_msg.delete()
                except: pass
            try: await channel.send(f"{author.mention}, üzgünüm, özel kanal oluşturulamadı. İzinleri veya Discord API durumunu kontrol edin.", delete_after=20)
            except: pass
            try: await message.delete(delay=20)
            except: pass
        return # Giriş kanalı işlemleri bitti

    # --- Geçici Sohbet Kanallarındaki Mesajlar ---
    if channel_id in temporary_chat_channels and not message.author.bot:
        # Komut değilse (yukarıda kontrol edildi), AI'ye gönder
        prompt_text = message.content
        await send_to_ai_and_respond(channel, author, prompt_text, channel_id)

# --- Arka Plan Görevi: İnaktivite Kontrolü ---
@tasks.loop(minutes=5)
async def check_inactivity():
    """Aktif olmayan geçici kanalları kontrol eder, uyarır ve siler."""
    global warned_inactive_channels
    if inactivity_timeout is None:
        # logger.debug("İnaktivite kontrolü: Zaman aşımı ayarlanmadığı için atlanıyor.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []
    channels_to_warn = []
    # Uyarı eşiği (timeout'dan 10 dk önce veya timeout'un %10'u, hangisi daha kısaysa)
    warning_delta = min(datetime.timedelta(minutes=10), inactivity_timeout * 0.1)
    warning_threshold = inactivity_timeout - warning_delta
    # logger.debug(f"İnaktivite kontrolü başlıyor. Zaman Aşımı: {inactivity_timeout}, Uyarı Eşiği: {warning_threshold}")

    # channel_last_active'in kopyası üzerinde işlem yap
    last_active_copy = channel_last_active.copy()

    for channel_id, last_active_time in last_active_copy.items():
        if channel_id not in temporary_chat_channels:
             # Bu durum normalde on_guild_channel_delete ile temizlenmeli
             logger.warning(f"İnaktivite kontrol: {channel_id} `channel_last_active` içinde ama `temporary_chat_channels` içinde değil. State tutarsızlığı, temizleniyor.")
             channel_last_active.pop(channel_id, None)
             warned_inactive_channels.discard(channel_id)
             active_ai_chats.pop(channel_id, None) # AI sohbetini de temizle
             # Kullanıcı map'ini de temizle
             user_id_to_remove = None
             for user_id, ch_id in list(user_to_channel_map.items()):
                 if ch_id == channel_id: user_id_to_remove = user_id; break
             if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
             remove_temp_channel_db(channel_id) # DB'den de sil
             continue

        if not isinstance(last_active_time, datetime.datetime):
            logger.error(f"İnaktivite kontrolü: Kanal {channel_id} için geçersiz last_active_time tipi ({type(last_active_time)}). Atlanıyor.")
            continue

        # last_active_time'ın timezone bilgisi olduğundan emin ol (DB'den gelmeli)
        if last_active_time.tzinfo is None:
            logger.warning(f"İnaktivite kontrolü: Kanal {channel_id} için timezone bilgisi olmayan last_active_time ({last_active_time}). UTC varsayılıyor.")
            last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)
            # Bu durumu düzeltmek için DB'yi güncellemek iyi olabilir ama döngü içinde yapmayalım.
            # update_channel_activity_db(channel_id, last_active_time)

        try:
            time_inactive = now - last_active_time
        except TypeError as te:
             logger.error(f"İnaktivite süresi hesaplanırken hata (Kanal: {channel_id}, Now: {now}, LastActive: {last_active_time}): {te}")
             continue

        # logger.debug(f"Kanal {channel_id}: İnaktif Süre: {time_inactive}")

        if time_inactive > inactivity_timeout:
            channels_to_delete.append(channel_id)
            # logger.debug(f"Kanal {channel_id} silinmek üzere işaretlendi.")
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels:
            channels_to_warn.append(channel_id)
            # logger.debug(f"Kanal {channel_id} uyarılmak üzere işaretlendi.")

    # Uyarıları gönder
    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                # Kalan süreyi tekrar hesapla (döngü biraz zaman almış olabilir)
                current_last_active = channel_last_active.get(channel_id)
                if not current_last_active: continue # Nadir durum, kanal bu arada silinmiş olabilir
                if current_last_active.tzinfo is None: # Tekrar TZ kontrolü
                    current_last_active = current_last_active.replace(tzinfo=datetime.timezone.utc)

                remaining_time = inactivity_timeout - (now - current_last_active)
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir. Devam etmek için mesaj yazın.", delete_after=300)
                warned_inactive_channels.add(channel_id)
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id} ({channel_obj.name})")
            except discord.errors.NotFound:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Kanal bulunamadı.")
                warned_inactive_channels.discard(channel_id) # Bulunamıyorsa uyarı setinden çıkar
                # Kanal bulunamadığı için state'i temizle (on_guild_channel_delete gelmemiş olabilir)
                temporary_chat_channels.discard(channel_id)
                active_ai_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
                if user_id: user_to_channel_map.pop(user_id[0], None)
                remove_temp_channel_db(channel_id)
            except discord.errors.Forbidden:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal {channel_id}): Mesaj gönderme izni yok.")
                warned_inactive_channels.add(channel_id) # Tekrar denememek için ekle
            except Exception as e:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else:
            logger.warning(f"İnaktivite uyarısı için kanal {channel_id} Discord'da bulunamadı.")
            warned_inactive_channels.discard(channel_id)
            # Kanal bulunamadığı için state'i temizle
            temporary_chat_channels.discard(channel_id)
            active_ai_chats.pop(channel_id, None)
            channel_last_active.pop(channel_id, None)
            user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
            if user_id: user_to_channel_map.pop(user_id[0], None)
            remove_temp_channel_db(channel_id)

    # Silinecek kanalları işle
    if channels_to_delete:
        logger.info(f"İnaktivite: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id)
            reason = "İnaktivite nedeniyle otomatik silindi."
            if channel_to_delete:
                channel_name_log = channel_to_delete.name # İsim loglamak için al
                try:
                    await channel_to_delete.delete(reason=reason)
                    logger.info(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) başarıyla silindi.")
                    # State temizliği normalde on_guild_channel_delete tarafından yapılır,
                    # ama biz yine de burada yapalım ki race condition olmasın veya event gelmezse diye.
                    temporary_chat_channels.discard(channel_id)
                    active_ai_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id) # DB'den silmeyi unutma
                except discord.errors.NotFound:
                    logger.warning(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken bulunamadı. State zaten temizlenmiş olabilir veya manuel temizleniyor.")
                    # Manuel temizlik (on_guild_channel_delete tetiklenmeyebilir)
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
                     # İzin yoksa state'i temizle, tekrar denemesin
                     temporary_chat_channels.discard(channel_id)
                     active_ai_chats.pop(channel_id, None)
                     channel_last_active.pop(channel_id, None)
                     warned_inactive_channels.discard(channel_id)
                     user_id_to_remove = None
                     for user_id, ch_id in list(user_to_channel_map.items()):
                         if ch_id == channel_id: user_id_to_remove = user_id; break
                     if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                     remove_temp_channel_db(channel_id) # DB'den de sil
                except Exception as e:
                    logger.error(f"İnaktif kanal '{channel_name_log}' (ID: {channel_id}) silinirken hata: {e}\n{traceback.format_exc()}")
                    # Hata olsa bile DB'den silmeyi dene ve state'i temizle
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
                # Manuel temizlik
                temporary_chat_channels.discard(channel_id)
                active_ai_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                warned_inactive_channels.discard(channel_id)
                user_id_to_remove = None
                for user_id, ch_id in list(user_to_channel_map.items()):
                    if ch_id == channel_id: user_id_to_remove = user_id; break
                if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                remove_temp_channel_db(channel_id)
            # Silinen veya bulunamayan kanalı uyarı listesinden çıkar (her durumda)
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
        # user_to_channel_map'in kopyası üzerinde dön
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id:
                user_id_to_remove = user_id
                break # Eşleşme bulundu, döngüden çık
        if user_id_to_remove:
            removed_channel_id = user_to_channel_map.pop(user_id_to_remove, None)
            if removed_channel_id:
                 logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi (Silinen Kanal ID: {removed_channel_id}).")
            else:
                 logger.warning(f"Silinen kanal {channel_id} için kullanıcı {user_id_to_remove} haritadan çıkarılamadı (zaten yok?).")
        else:
            logger.warning(f"Silinen geçici kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")
        # DB'den silmeyi unutma (inaktivite veya endchat zaten silmiş olabilir ama garanti olsun)
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

    # Önce state'i kontrol et
    if channel_id in temporary_chat_channels:
        is_temp_channel = True
        # Sahibi state'den bulmaya çalış
        for user_id, ch_id in user_to_channel_map.items():
            if ch_id == channel_id:
                expected_user_id = user_id
                break

    # State'de yoksa veya sahip bulunamadıysa DB'ye bak
    if not is_temp_channel or expected_user_id is None:
        conn = None
        try:
            conn = db_connect()
            cursor = conn.cursor(cursor_factory=DictCursor)
            cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,))
            owner_row = cursor.fetchone()
            cursor.close()
            if owner_row:
                is_temp_channel = True # DB'de bulundu, geçici kanal kabul et
                db_user_id = owner_row['user_id']
                if expected_user_id is None:
                    expected_user_id = db_user_id # State'de yoksa DB'dekini al
                elif expected_user_id != db_user_id:
                     logger.warning(f".endchat: Kanal {channel_id} için state sahibi ({expected_user_id}) ile DB sahibi ({db_user_id}) farklı! DB sahibine öncelik veriliyor.")
                     expected_user_id = db_user_id
                # Eksik state'i doldur
                if channel_id not in temporary_chat_channels: temporary_chat_channels.add(channel_id)
                if expected_user_id not in user_to_channel_map or user_to_channel_map[expected_user_id] != channel_id:
                    user_to_channel_map[expected_user_id] = channel_id
            else: # DB'de de yoksa
                 if not is_temp_channel: # State'de de yoktu
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

    # Sahip kontrolü
    if expected_user_id and author_id != expected_user_id:
        owner = ctx.guild.get_member(expected_user_id)
        owner_name = f"<@{expected_user_id}>" if not owner else owner.mention
        await ctx.send(f"Bu kanalı sadece oluşturan kişi ({owner_name}) kapatabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return
    elif not expected_user_id:
        logger.error(f".endchat: Kanal {channel_id} sahibi (state veya DB'de) bulunamadı! Yine de silmeye çalışılıyor.")
        # Sahibi bulamasa da admin veya kanal yöneticisi ise silebilmeli mi? Şimdilik hayır.
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
        # State temizliği on_guild_channel_delete tarafından yapılacak.
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı. State zaten temizlenmiş olabilir veya manuel temizleniyor.")
        # Manuel temizlik (on_guild_channel_delete tetiklenmeyebilir)
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
        # Hata olsa bile state ve db'yi temizlemeyi dene
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
        # DB'ye de bakmaya gerek yok, sadece aktif sohbetler için mantıklı
        await ctx.send("Bu komut sadece aktif geçici sohbet kanallarında kullanılabilir.", delete_after=10)
        try: 
            await ctx.message.delete(delay=10); 
        except: 
            pass
        return

    if channel_id in active_ai_chats:
        # Oturum veya geçmişi temizle/sıfırla
        model_type = active_ai_chats[channel_id].get('model', '').split(':')[0]
        active_ai_chats.pop(channel_id, None) # Komple kaldır, bir sonraki mesajda yeniden oluşsun
        # VEYA:
        # if model_type == GEMINI_PREFIX.strip(':'):
        #     # Gemini için yeni session başlatmak daha iyi olabilir
        #     active_ai_chats.pop(channel_id, None)
        # elif model_type == DEEPSEEK_PREFIX.strip(':'):
        #     if 'history' in active_ai_chats[channel_id]:
        #         active_ai_chats[channel_id]['history'] = [] # Sadece geçmişi sıfırla
        #     else:
        #         active_ai_chats.pop(channel_id, None) # History yoksa komple kaldır
        # else:
        #      active_ai_chats.pop(channel_id, None) # Bilinmiyorsa kaldır

        logger.info(f"Sohbet geçmişi/oturumu {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi/oturumu sıfırlandı. Bir sonraki mesajınızla yeni bir oturum başlayacak.", delete_after=15)
    else:
        logger.info(f"Sıfırlanacak aktif oturum/geçmiş yok: Kanal {channel_id}")
        await ctx.send("✨ Şu anda sıfırlanacak aktif bir konuşma geçmişi/oturumu bulunmuyor. Zaten temiz.", delete_after=10)
    try: 
        await ctx.message.delete(delay=15); 
    except: 
        pass

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True) # Sadece mesaj yönetebilenler
async def clear_messages(ctx: commands.Context, amount: str = None):
    """Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç)."""
    if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
        await ctx.send("Mesajları silebilmem için bu kanalda 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10)
        return

    # Kanalın geçici sohbet kanalı olup olmadığını kontrol et (sadece bu kanallarda yetki verelim?)
    # if ctx.channel.id not in temporary_chat_channels:
    #      await ctx.send("Bu komut sadece geçici sohbet kanallarında kullanılabilir.", delete_after=10)
    #      try: await ctx.message.delete(delay=10); except: pass
    #      return
    # Yukarıdaki kontrolü kaldırdım, yönetici herhangi bir kanalda kullanabilsin.

    if amount is None:
        await ctx.send(f"Silinecek mesaj sayısı (`{ctx.prefix}clear 5`) veya tümü için `{ctx.prefix}clear all` yazın.", delete_after=10)
        try: 
            await ctx.message.delete(delay=10);
        except: 
            pass
        return

    deleted_count = 0
    skipped_pinned = 0
    original_command_message_id = ctx.message.id # Komut mesajının ID'si

    # Sabitlenmemiş mesajları kontrol eden fonksiyon
    def check_not_pinned_and_not_command(m):
        nonlocal skipped_pinned
        # Komut mesajını silme (purge sonrasında ayrıca silinecek)
        if m.id == original_command_message_id:
            return True # Silme kontrolünden geçsin ama sayaca eklenmesin diye true dönebiliriz? Veya direkt False
        if m.pinned:
            skipped_pinned += 1
            return False
        return True

    try:
        if amount.lower() == 'all':
            status_msg = await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)... Lütfen bekleyin.", delete_after=15)
            try: await ctx.message.delete() # Komut mesajını hemen sil
            except: pass

            # purge limitini 1000 gibi bir değere ayarlayarak daha hızlı silebiliriz ama rate limit riski artar.
            # 100'lük gruplar halinde silmek daha güvenli.
            while True:
                # `before` parametresi çok eski mesajları silmek için optimize edebilir.
                try:
                    # check fonksiyonu bulk silmede çağrılmaz, filtrelemeyi sonradan yaparız.
                    # Bu yüzden check olmadan silelim, sonra sabitlenmişleri sayalım.
                    deleted_messages = await ctx.channel.purge(limit=100, check=lambda m: not m.pinned, bulk=True)
                    # bulk=True olmasına rağmen check çalışmayabilir, API limitleri vs.
                    # Alternatif: Önce fetch, sonra delete
                    # messages_to_delete = [msg async for msg in ctx.channel.history(limit=101) if not msg.pinned and msg.id != original_command_message_id]
                    # if not messages_to_delete: break
                    # await ctx.channel.delete_messages(messages_to_delete)
                    # deleted_count += len(messages_to_delete)
                    # if len(messages_to_delete) < 100: break # 100'den az geldiyse son grup

                    if not deleted_messages: # Hiç mesaj silinmediyse döngüden çık
                        break
                    deleted_count += len(deleted_messages)
                    if len(deleted_messages) < 100: # Son grup silindi
                         break
                    await asyncio.sleep(1) # Rate limit için bekleme
                except discord.errors.NotFound: break # Kanal bu arada silindiyse
                except discord.errors.HTTPException as http_e:
                     if http_e.status == 429: # Rate limit
                          retry_after = float(http_e.response.headers.get('Retry-After', 1))
                          logger.warning(f".clear 'all' rate limited. Retrying after {retry_after}s")
                          await status_msg.edit(content=f"Rate limit! {retry_after:.1f} saniye bekleniyor...", delete_after=retry_after + 2)
                          await asyncio.sleep(retry_after)
                          continue
                     else: raise # Diğer HTTP hatalarını tekrar fırlat
            try:
                msg_content = f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi."
                # Sabitlenmişleri saymak için tekrar history çekmek gerekir, şimdilik atlayalım.
                # pinned_count = len([msg async for msg in ctx.channel.history(limit=None) if msg.pinned])
                # if pinned_count > 0: msg_content += f" ({pinned_count} sabitlenmiş mesaj atlandı)."
                await status_msg.edit(content=msg_content, delete_after=10)
            except: pass # status_msg silinmiş olabilir

        else: # Belirli sayıda silme
            try:
                limit = int(amount)
                if limit <= 0: raise ValueError("Sayı pozitif olmalı")
                if limit > 500: # Makul bir üst sınır koyalım
                    limit = 500
                    await ctx.send("Tek seferde en fazla 500 mesaj silebilirsiniz.", delete_after=5)

                # Komut mesajını da sayarak limit+1 alalım
                limit_with_command = limit + 1
                deleted_messages = await ctx.channel.purge(limit=limit_with_command, check=check_not_pinned_and_not_command, bulk=True, before=ctx.message) # Komut mesajından öncekileri sil
                actual_deleted_count = len(deleted_messages) # purge zaten check'e uymayanları saymaz

                try: await ctx.message.delete() # Komut mesajını ayrıca sil
                except: pass

                msg = f"{actual_deleted_count} mesaj silindi."
                if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
                await ctx.send(msg, delete_after=7)

            except ValueError:
                await ctx.send(f"Geçersiz sayı: '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10)
                try:
                    # Bu satır bir üstteki try'a göre girintili olmalı
                    await ctx.message.delete(delay=10)
                except:
                    # Bu except, içteki try ile aynı hizada olmalı
                    pass
            except AssertionError: # Assert kullanmıyoruz artık
                 pass

    except discord.errors.Forbidden:
        logger.error(f"HATA: '{ctx.channel.name}' kanalında mesaj silme izni yok (Bot için)!")
        await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10)
        try: 
            await ctx.message.delete(delay=10);
        except: 
            pass
    except Exception as e:
        logger.error(f".clear hatası: {e}\n{traceback.format_exc()}")
        await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10)
        try: 
            await ctx.message.delete(delay=10)
        except:
            pass

@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user) # Kullanıcı başına 5 saniyede 1 istek
async def ask_in_channel(ctx: commands.Context, *, question: str = None):
    """Sorulan soruyu varsayılan Gemini modeline iletir ve yanıtı geçici olarak bu kanalda gösterir."""
    if not gemini_default_model_instance: # Gemini başlatılamadıysa veya API anahtarı yoksa
        if not GEMINI_API_KEY:
            user_msg = "⚠️ Gemini API anahtarı ayarlanmadığı için bu komut kullanılamıyor."
        else:
            user_msg = "⚠️ Varsayılan Gemini modeli yüklenemedi. Bot loglarını kontrol edin."
        await ctx.reply(user_msg, delete_after=15)
        try: 
            await ctx.message.delete(delay=15)
        except: 
            pass
        return

    if question is None or not question.strip():
        error_msg = await ctx.reply(f"Lütfen bir soru sorun (örn: `{ctx.prefix}ask Evren nasıl oluştu?`).", delete_after=15)
        try: 
            await ctx.message.delete(delay=15)
        except: 
            pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None

    try:
        async with ctx.typing():
            try:
                # Blocking API çağrısını ayrı thread'de çalıştır
                # generate_content asenkron olmadığı için to_thread gerekli
                response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e:
                 logger.error(f".ask için Gemini API hatası: {type(gemini_e).__name__}: {gemini_e}")
                 error_str = str(gemini_e).lower()
                 user_msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."
                 if "api key" in error_str or "authentication" in error_str: user_msg = "API Anahtarı sorunu."
                 elif "quota" in error_str or "limit" in error_str or "429" in error_str: user_msg = "API kullanım limiti aşıldı."
                 elif "500" in error_str or "internal error" in error_str: user_msg = "Yapay zeka sunucusunda geçici sorun."
                 await ctx.reply(f"⚠️ {user_msg}", delete_after=15)
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     pass
                 return

            gemini_response_text = ""
            try:
                 gemini_response_text = response.text.strip()
            except ValueError as ve: # Bazen response.text yerine hata dönebilir (örn. prompt block)
                 logger.warning(f".ask Gemini yanıtını okurken hata: {ve}. Prompt feedback kontrol ediliyor.")
                 gemini_response_text = "" # Yanıt yok kabul et
            except Exception as text_err:
                 logger.error(f".ask Gemini response.text okuma hatası: {text_err}")
                 gemini_response_text = ""

            # Güvenlik/hata kontrolü (send_to_ai_and_respond'daki gibi)
            finish_reason = None
            try: finish_reason = response.candidates[0].finish_reason.name
            except (IndexError, AttributeError): pass
            prompt_feedback_reason = None
            try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
            except AttributeError: pass

            if prompt_feedback_reason == "SAFETY":
                await ctx.reply("Girdiğiniz mesaj güvenlik filtrelerine takıldı.", delete_after=15)
                try: 
                    await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                except: 
                    pass
                return
            elif finish_reason == "SAFETY":
                 await ctx.reply("Yanıt güvenlik filtrelerine takıldı.", delete_after=15)
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     passs
                 return
            elif finish_reason == "RECITATION":
                 await ctx.reply("Yanıt, alıntı filtrelerine takıldı.", delete_after=15)
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     pass
                 return
            elif finish_reason == "OTHER":
                 await ctx.reply("Yanıt oluşturulamadı (bilinmeyen sebep).", delete_after=15)
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     pass
                 return
            elif not gemini_response_text and finish_reason != "STOP": # Yanıt yok ve normal bitmediyse
                 logger.warning(f"Gemini'den .ask için boş yanıt alındı (Finish Reason: {finish_reason}).")
                 await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     pass
                 return
            elif not gemini_response_text: # Yanıt yok ama normal bitti (STOP)
                 logger.info(f"Gemini'den .ask için boş yanıt alındı (Normal bitiş).")
                 # Kullanıcıya bir şey demeye gerek yok, model bazen boş dönebilir.
                 try: 
                     await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: 
                     pass
                 return # Yanıt mesajı gönderme

        # Embed oluştur ve gönder
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)

        # Discord embed field limitleri (1024 karakter)
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)

        response_display = gemini_response_text if len(gemini_response_text) <= 1024 else gemini_response_text[:1021] + "..."
        embed.add_field(name="Yanıt", value=response_display, inline=False)

        footer_text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik silinecektir."
        # Eğer yanıt kesildiyse bunu belirtelim
        if len(gemini_response_text) > 1024:
             footer_text += " (Yanıt kısaltıldı)"
        embed.set_footer(text=footer_text)

        # mention_author=False ile ping'lemeyi engelle
        bot_response_message = await ctx.reply(embed=embed, mention_author=False)
        logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")

        # Mesajları silme zamanlaması
        try:
            await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            # logger.debug(f".ask komut mesajı ({ctx.message.id}) silinmek üzere zamanlandı.")
        except discord.errors.NotFound: pass
        except Exception as e: logger.warning(f".ask komut mesajı silinirken hata: {e}")

        if bot_response_message:
            try:
                await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY)
                # logger.debug(f".ask yanıt mesajı ({bot_response_message.id}) silinmek üzere zamanlandı.")
            except discord.errors.NotFound: pass
            except Exception as e: logger.warning(f".ask yanıt mesajı silinirken hata: {e}")

    except Exception as e:
        logger.error(f".ask genel hatası: {e}\n{traceback.format_exc()}")
        await ctx.reply("Sorunuz işlenirken beklenmedik bir hata oluştu.", delete_after=15)
        try: 
            await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        except: 
            pass

@ask_in_channel.error
async def ask_error(ctx, error):
    """ .ask komutuna özel cooldown hatasını yakalar """
    if isinstance(error, commands.CommandOnCooldown):
        delete_delay = max(5, int(error.retry_after) + 1)
        await ctx.send(f"⏳ `.ask` komutu için beklemedesiniz. Lütfen **{error.retry_after:.1f} saniye** sonra tekrar deneyin.", delete_after=delete_delay)
        try: 
            await ctx.message.delete(delay=delete_delay)
        except: 
            pass
    else:
        # Diğer hatalar on_command_error'a gitsin
        # logger.error(f".ask error handler'da beklenmedik hata: {error}")
        pass # Let the global handler take care of it


@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def list_available_models(ctx: commands.Context):
    """Sohbet için kullanılabilir Gemini ve DeepSeek modellerini listeler."""
    status_msg = await ctx.send("Kullanılabilir modeller kontrol ediliyor...", delete_after=5)

    async def fetch_gemini():
        if not GEMINI_API_KEY: return ["_(Gemini API anahtarı ayarlı değil)_"]
        try:
            gemini_models_list = []
            # API çağrısını thread'de yap
            gemini_models = await asyncio.to_thread(genai.list_models)
            for m in gemini_models:
                # Sadece 'generateContent' destekleyen ve 'models/' ile başlayanları alalım
                if 'generateContent' in m.supported_generation_methods and m.name.startswith("models/"):
                    model_id = m.name.split('/')[-1]
                    prefix = ""
                    # Daha spesifik model isimlerine göre ikon atama
                    if "gemini-1.5-flash" in model_id: prefix = "⚡ "
                    elif "gemini-1.5-pro" in model_id: prefix = "✨ "
                    elif "gemini-pro" == model_id and "vision" not in model_id: prefix = "✅ " # Sadece text modeli için
                    elif "aqa" in model_id: prefix="❓ " # Attributed Question Answering
                    # Diğer modeller için genel prefix veya boş bırakılabilir
                    gemini_models_list.append(f"{GEMINI_PREFIX}{prefix}`{model_id}`")
            # İsimlerine göre sırala
            gemini_models_list.sort(key=lambda x: x.split('`')[1])
            return gemini_models_list if gemini_models_list else ["_(Kullanılabilir Gemini modeli bulunamadı)_"]
        except Exception as e:
            logger.error(f"Gemini modelleri listelenirken hata: {e}")
            return ["_(Gemini modelleri alınamadı - API Hatası)_"]

    async def fetch_deepseek():
        if not DEEPSEEK_API_KEY: return ["_(DeepSeek API anahtarı ayarlı değil)_"]
        if not DEEPSEEK_AVAILABLE: return ["_(DeepSeek kütüphanesi bulunamadı)_"]
        try:
            deepseek_models_list = []
            # DeepSeek modellerini listeleme API'si (varsa)
            # client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)
            # response = await asyncio.to_thread(client.models.list)
            # if response and hasattr(response, 'data'):
            #     for model in response.data:
            #          # Belki sadece chat modellerini filtrelemek gerekir
            #          # if "chat" in model.id or "instruct" in model.id:
            #          deepseek_models_list.append(f"{DEEPSEEK_PREFIX}🧭 `{model.id}`")

            # API yoksa veya çalışmazsa bilinen modelleri manuel ekle
            if not deepseek_models_list:
                 known_deepseek_models = ["deepseek-chat", "deepseek-coder"]
                 for model_id in known_deepseek_models:
                      deepseek_models_list.append(f"{DEEPSEEK_PREFIX}🧭 `{model_id}`")

            deepseek_models_list.sort()
            return deepseek_models_list if deepseek_models_list else ["_(Kullanılabilir DeepSeek modeli bulunamadı/listelenemedi)_"]
        except Exception as e:
            logger.error(f"DeepSeek modelleri listelenirken hata: {e}")
            return ["_(DeepSeek modelleri alınamadı - API/Kod Hatası)_"]

    # Modelleri paralel olarak çek
    results = await asyncio.gather(fetch_gemini(), fetch_deepseek())
    all_models_list = []
    all_models_list.extend(results[0]) # Gemini sonuçları
    all_models_list.extend(results[1]) # DeepSeek sonuçları

    # Hata mesajlarını filtrele, sadece geçerli modeller kalsın (veya hepsi hataysa mesaj ver)
    valid_models = [m for m in all_models_list if not m.startswith("_(")]
    error_models = [m for m in all_models_list if m.startswith("_(")]

    if not valid_models:
        error_text = "\n".join(error_models) if error_models else "API anahtarları ayarlanmamış veya bilinmeyen bir sorun var."
        await ctx.send(f"Kullanılabilir model bulunamadı.\n{error_text}")
        try: await status_msg.delete()
        except: pass
        return

    # Geçerli modeller varsa Embed oluştur
    embed = discord.Embed(
        title="🤖 Kullanılabilir Yapay Zeka Modelleri",
        description=f"Bir sonraki özel sohbetiniz için `{ctx.prefix}setmodel <model_adi_on_ekli>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(valid_models),
        color=discord.Color.gold()
    )
    embed.add_field(name="Ön Ekler", value=f"`{GEMINI_PREFIX}` Gemini Modeli\n`{DEEPSEEK_PREFIX}` DeepSeek Modeli", inline=False)
    embed.set_footer(text="⚡ Flash, ✨ Pro (Gemini), ✅ Eski Pro (Gemini), 🧭 DeepSeek, ❓ AQA (Gemini)")

    if error_models: # Hata mesajları varsa footer'a ekle
         footer_text = embed.footer.text + "\nUyarılar: " + " ".join(error_models)
         embed.set_footer(text=footer_text[:1024]) # Footer limiti

    try: await status_msg.delete() # Önceki mesajı sil
    except: pass
    await ctx.send(embed=embed)
    try: await ctx.message.delete() # Komut mesajını sil
    except: pass

@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx: commands.Context, *, model_id_with_or_without_prefix: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini veya DeepSeek modelini ayarlar."""
    global user_next_model
    if model_id_with_or_without_prefix is None:
        await ctx.send(f"Lütfen bir model adı belirtin (örn: `{GEMINI_PREFIX}gemini-1.5-flash-latest` veya `{DEEPSEEK_PREFIX}deepseek-chat`). Modeller için `{ctx.prefix}listmodels`.", delete_after=15)
        try: 
            await ctx.message.delete(delay=15)
        except: 
            pass
        return

    model_input = model_id_with_or_without_prefix.strip().replace('`', '') # Backtickleri temizle
    selected_model_full_name = None
    is_valid = False
    error_message = None

    # Model adı geçerli bir prefix ile başlıyor mu kontrol et
    if not model_input.startswith(GEMINI_PREFIX) and not model_input.startswith(DEEPSEEK_PREFIX):
         await ctx.send(f"❌ Lütfen model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_PREFIX}` ön ekini ekleyin. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=20)
         try: 
             await ctx.message.delete(delay=20)
         except: 
             pass
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
                        # Modeli doğrulamak için API'yi çağır (thread içinde)
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
            elif not DEEPSEEK_AVAILABLE: error_message = f"❌ DeepSeek kütüphanesi sunucuda bulunamadı."; is_valid = False
            else:
                actual_model_name = model_input[len(DEEPSEEK_PREFIX):]
                if not actual_model_name: error_message = "❌ Lütfen bir DeepSeek model adı belirtin."; is_valid = False
                else:
                    try:
                        # DeepSeek model varlığını API ile doğrula (destekliyorsa)
                        # client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)
                        # await asyncio.to_thread(client.models.retrieve, actual_model_name)

                        # Şimdilik bilinen modelleri kabul edelim veya sadece var olduğunu varsayalım
                        # Daha sağlam bir kontrol için Deepseek API'sinin model listeleme/getirme yeteneği gerekir.
                        # Bilinen modeller listesiyle karşılaştırma yapılabilir:
                        # known_deepseek = ["deepseek-chat", "deepseek-coder"]
                        # if actual_model_name not in known_deepseek:
                        #    raise ValueError(f"Model '{actual_model_name}' bilinen DeepSeek modelleri arasında değil.")

                        selected_model_full_name = model_input
                        is_valid = True # Şimdilik geçerli kabul et
                        logger.info(f"{ctx.author.name} DeepSeek modelini ayarladı: {actual_model_name}")
                    except Exception as e:
                        logger.warning(f"DeepSeek modeli doğrulanırken hata ({actual_model_name}): {e}")
                        error_message = f"❌ `{actual_model_name}` modeli kontrol edilirken hata oluştu veya geçerli değil."
                        is_valid = False

    if is_valid and selected_model_full_name:
        user_next_model[ctx.author.id] = selected_model_full_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{selected_model_full_name}' olarak ayarlandı.")
        await ctx.send(f"✅ Başarılı! Bir sonraki özel sohbetiniz `{selected_model_full_name}` modeli ile başlayacak.", delete_after=20)
    else:
        # Hata mesajı zaten ayarlanmış olmalı
        final_error_msg = error_message if error_message else f"❌ `{model_input}` geçerli bir model adı değil veya bir sorun oluştu."
        await ctx.send(f"{final_error_msg} `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)

    try: 
        await ctx.message.delete(delay=20)
    except: 
        pass


@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True) # Sadece adminler
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
    # channel argümanı discord.TextChannel türünde otomatik kontrol edilir.

    # Botun kanalı görebildiğinden ve yazabildiğinden emin ol
    perms = channel.permissions_for(ctx.guild.me)
    if not perms.view_channel or not perms.send_messages or not perms.manage_messages: # manage_messages da ekleyelim (eski mesajları silmek için)
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
@commands.has_permissions(administrator=True) # Sadece adminler
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
             save_config('inactivity_timeout_hours', '0') # DB'ye 0 kaydet
             logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından kapatıldı.")
             await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **kapatıldı**.")
        elif hours_float < 0.1:
             await ctx.send("Minimum zaman aşımı 0.1 saattir (6 dakika). Kapatmak için 0 girin.")
             return
        elif hours_float > 720: # 1 aydan fazla olmasın?
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
    # Komutları gruplara ayır
    for command in sorted(bot.commands, key=lambda cmd: cmd.name):
        if command.hidden: continue # Gizli komutları atla

        # Komutun çalıştırılıp çalıştırılamayacağını kontrol et (kullanıcı yetkisi vs.)
        # try:
        #      can_run = await command.can_run(ctx)
        #      if not can_run and not await bot.is_owner(ctx.author): continue # Sahibi her zaman çalıştırabilir varsayımı
        # except commands.CheckFailure: continue # Check hatası varsa gösterme
        # except Exception as e: logger.warning(f"Komut {command.name} için can_run kontrolü başarısız: {e}"); continue
        # can_run kontrolü olmadan tüm komutları listeleyelim, çalıştırmayı deneyince hata alır.

        help_text = command.help or command.short_doc or "Açıklama yok."
        # Help text'i kısaltabiliriz gerekirse
        help_text = help_text.split('\n')[0] # Sadece ilk satırı al

        aliases = f" (Diğer: `{'`, `'.join(command.aliases)}`)" if command.aliases else ""
        # Parametreleri gösterirken < > zorunlu, [ ] opsiyonel gösterimi
        params = f" {command.signature}" if command.signature else ""
        # Parametrelerdeki =None gibi varsayılan değerleri temizleyebiliriz
        # params = params.replace('=None', '').replace('= Ellipsis', '...') # Daha temiz görünüm

        cmd_string = f"`{ctx.prefix}{command.name}{params}`{aliases}\n_{help_text}_"

        # Komutun admin komutu olup olmadığını belirle (daha sağlam yol)
        is_admin_cmd = any(isinstance(check, type(commands.has_permissions)) or isinstance(check, type(commands.is_owner)) for check in command.checks)
        # Özel olarak administrator=True kontrolü
        is_super_admin_cmd = False
        for check in command.checks:
             if isinstance(check, type(commands.has_permissions)):
                  # Check fonksiyonunun __closure__ veya __code__ içinden izinleri okumak zor.
                  # Şimdilik setentrychannel ve settimeout'u admin kabul edelim.
                  if command.name in ['setentrychannel', 'settimeout']:
                       is_super_admin_cmd = True; break

        if is_super_admin_cmd:
             admin_cmds.append(cmd_string)
        elif command.name in ['endchat', 'resetchat', 'clear'] and command.cog is None: # clear'ın admin yetkisi var ama sohbetle ilgili
             # clear komutunun has_permissions kontrolü var, onu admin'e alalım
             if command.name == 'clear' and is_admin_cmd:
                  admin_cmds.append(cmd_string)
             else: # endchat, resetchat (yetki kontrolü yok)
                  chat_cmds.append(cmd_string)
        elif is_admin_cmd: # Diğer yetkili komutlar
             admin_cmds.append(cmd_string)
        else: # Yetki gerektirmeyen genel komutlar
            user_cmds.append(cmd_string)

    # Embed'e alanları ekle (25 alan sınırı var!)
    # Daha fazla komut olursa sayfalandırma gerekebilir.
    if user_cmds: embed.add_field(name="👤 Genel Komutlar", value="\n\n".join(user_cmds)[:1024], inline=False) # 1024 karakter sınırı
    if chat_cmds: embed.add_field(name="💬 Sohbet Kanalı Komutları", value="\n\n".join(chat_cmds)[:1024], inline=False)
    if admin_cmds: embed.add_field(name="🛠️ Yönetici Komutları", value="\n\n".join(admin_cmds)[:1024], inline=False)

    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)}")
    try:
        await ctx.send(embed=embed)
        try: await ctx.message.delete() # Komutu sil
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
        if ctx.command and ctx.command.name == 'ask':
            return
        delete_delay = max(5, int(error.retry_after) + 1)
        await ctx.send(
            f"⏳ `{ctx.command.qualified_name}` komutu için beklemedesiniz. Lütfen **{error.retry_after:.1f} saniye** sonra tekrar deneyin.",
            delete_after=delete_delay
        )
        try:
            await ctx.message.delete(delay=delete_delay)
        except Exception:
            pass
        return

    if isinstance(error, commands.UserInputError):
        delete_delay = 15
        command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with
        usage = f"`{ctx.prefix}{command_name} {ctx.command.signature if ctx.command else ''}`".replace('=None', '').replace('= Ellipsis', '...')
        error_message = "Hatalı komut kullanımı."
        if isinstance(error, commands.MissingRequiredArgument):
            error_message = f"Eksik argüman: `{error.param.name}`."
        elif isinstance(error, commands.BadArgument):
            error_message = f"Geçersiz argüman türü: {error}"
            if isinstance(error, commands.ChannelNotFound):
                error_message = f"Kanal bulunamadı: `{error.argument}`."
            elif isinstance(error, commands.MemberNotFound):
                error_message = f"Kullanıcı bulunamadı: `{error.argument}`."
            elif isinstance(error, commands.UserNotFound):
                error_message = f"Kullanıcı bulunamadı: `{error.argument}`."
            elif isinstance(error, commands.RoleNotFound):
                error_message = f"Rol bulunamadı: `{error.argument}`."
            elif isinstance(error, commands.BadLiteralArgument):
                error_message = f"Geçersiz seçenek: `{error.argument}`. Şunlardan biri olmalı: {', '.join(f'`{lit}`' for lit in error.literals)}"
            elif isinstance(error, commands.BadUnionArgument):
                error_message = f"Geçersiz argüman türü: `{error.param.name}` için uygun bir değer girin."
        elif isinstance(error, commands.TooManyArguments):
            error_message = "Çok fazla argüman girdiniz."

        await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay)
        try:
            await ctx.message.delete(delay=delete_delay)
        except Exception:
            pass
        return

    delete_user_msg = True
    delete_delay = 10

    if isinstance(error, commands.MissingPermissions):
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {original_error.missing_permissions}")
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        delete_delay = 15
        await ctx.send(
            f"⛔ Üzgünüm {ctx.author.mention}, bu komutu kullanmak için şu izinlere sahip olmalısın: **{perms}**",
            delete_after=delete_delay
        )

    elif isinstance(error, commands.BotMissingPermissions):
        logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {original_error.missing_permissions}")
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        delete_delay = 15
        delete_user_msg = False
        await ctx.send(
            f"🆘 Benim bu komutu çalıştırmak için şu izinlere sahip olmam gerekiyor: **{perms}**",
            delete_after=delete_delay
        )

    elif isinstance(error, commands.CheckFailure):
        logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {error}")
        user_msg = "🚫 Bu komutu burada veya bu şekilde kullanamazsınız."

        if isinstance(error, commands.NoPrivateMessage):
            user_msg = "🚫 Bu komut sadece sunucu kanallarında kullanılabilir."
            delete_user_msg = False
        elif isinstance(error, commands.PrivateMessageOnly):
            user_msg = "🚫 Bu komut sadece özel mesajla (DM) kullanılabilir."
        elif isinstance(error, commands.NotOwner):
            user_msg = "🚫 Bu komutu sadece bot sahibi kullanabilir."

        try:
            await ctx.author.send(user_msg)
        except Exception:
            pass

        await ctx.send(user_msg, delete_after=delete_delay)

    else:
        logger.error(
            f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' işlenirken beklenmedik hata: {type(original_error).__name__}: {original_error}"
        )
        traceback_str = "".join(
            traceback.format_exception(type(original_error), original_error, original_error.__traceback__)
        )
        logger.error(f"Traceback:\n{traceback_str}")
        delete_delay = 15
        await ctx.send(
            "⚙️ Komut işlenirken beklenmedik bir hata oluştu. Sorun devam ederse lütfen geliştirici ile iletişime geçin.",
            delete_after=delete_delay
        )

    if delete_user_msg and ctx.guild:
        try:
            await ctx.message.delete(delay=delete_delay)
        except discord.errors.NotFound:
            pass
        except discord.errors.Forbidden:
            pass
        except Exception as e:
            logger.warning(f"Komut hatası sonrası mesaj silinemedi: {e}")

# === Render/Koyeb için Web Sunucusu ===
app = Flask(__name__)
@app.route('/')
def home():
    """Basit bir sağlık kontrolü endpoint'i."""
    if bot and bot.is_ready():
        try:
            guild_count = len(bot.guilds)
            active_chats = len(temporary_chat_channels) # Aktif state'deki kanallar
            # DB'deki kanal sayısı ile karşılaştırılabilir
            # conn = db_connect(); cursor = conn.cursor(); cursor.execute("SELECT COUNT(*) FROM temp_channels"); db_count = cursor.fetchone()[0]; conn.close()
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
    # Ortam değişkeninden portu al, yoksa 8080 kullan
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0") # Render/Koyeb genellikle 0.0.0.0 ister
    try:
        logger.info(f"Flask web sunucusu http://{host}:{port} adresinde başlatılıyor...")
        # Werkzeug'un loglarını kısmıştık, Flask'ın kendi logları için debug=False önemli
        app.run(host=host, port=port, debug=False)
    except Exception as e:
        logger.critical(f"Web sunucusu başlatılırken KRİTİK HATA: {e}")
        # Web sunucusu çökse bile bot çalışmaya devam etmeli (daemon=True sayesinde)
# ===================================

# --- Botu Çalıştır ---
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.critical("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı!")
        exit()
    if not DATABASE_URL:
        logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!")
        exit()

    logger.info("Bot başlatılıyor...")
    webserver_thread = None
    try:
        # setup_database() zaten başlangıçta çağrıldı.

        # Web sunucusunu ayrı thread'de başlat
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()

        # Botu çalıştır (Discord.py'nin kendi logger'ını kullanmamak için log_handler=None)
        bot.run(DISCORD_TOKEN, log_handler=None)

    except discord.errors.LoginFailure:
        logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired:
        logger.critical("HATA: Gerekli Intent'ler (Members, Message Content) Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err:
        logger.critical(f"PostgreSQL bağlantı hatası (Başlangıçta): {db_err}")
    except Exception as e:
        logger.critical(f"Bot çalıştırılırken kritik hata: {type(e).__name__}: {e}")
        logger.critical(traceback.format_exc())
    finally:
        logger.info("Bot kapatılıyor...")
        # Bot kapatılırken web sunucusu thread'i otomatik olarak duracaktır (daemon=True olduğu için)
        # Gerekirse diğer temizleme işlemleri buraya eklenebilir.