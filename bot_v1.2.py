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
try:
    from deepseek import DeepSeekClient
    DEEPSEEK_AVAILABLE = True
except ImportError:
    DEEPSEEK_AVAILABLE = False
    DeepSeekClient = None # Kütüphane yoksa None ata

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
elif not DEEPSEEK_AVAILABLE:
    logger.error("HATA: DeepSeek API anahtarı bulundu ancak 'deepseek' kütüphanesi yüklenemedi. Lütfen `pip install deepseek` komutunu çalıştırın.")
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
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_active TIMESTAMPTZ NOT NULL,
                model_name TEXT DEFAULT '{default_model_with_prefix_for_db}'
            )
        ''')
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
setup_database()

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
else:
    logger.warning("Gemini API anahtarı ayarlanmadığı için Gemini özellikleri devre dışı.")

# DeepSeek API'si zaten kontrol edildi.

# --- Bot Kurulumu ---
intents = discord.Intents.default(); intents.message_content = True; intents.members = True; intents.messages = True; intents.guilds = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Yardımcı Fonksiyonlar ---

async def create_private_chat_channel(guild, author):
    """Verilen kullanıcı için özel sohbet kanalı oluşturur ve kanal nesnesini döndürür."""
    if not guild.me.guild_permissions.manage_channels: logger.warning(f"'{guild.name}' sunucusunda 'Kanalları Yönet' izni eksik."); return None
    safe_username = "".join(c for c in author.display_name if c.isalnum() or c == ' ').strip().replace(' ', '-').lower()
    if not safe_username: safe_username = "kullanici"
    safe_username = safe_username[:80]
    base_channel_name = f"sohbet-{safe_username}"; channel_name = base_channel_name; counter = 1
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}
    while channel_name.lower() in existing_channel_names:
        potential_name = f"{base_channel_name}-{counter}"
        if len(potential_name) > 100: potential_name = f"{base_channel_name[:90]}-{counter}"
        channel_name = potential_name
        counter += 1
        if counter > 1000:
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı).")
            channel_name = f"sohbet-{author.id}-{datetime.datetime.now().strftime('%M%S')}"[:100]
            if channel_name.lower() in existing_channel_names: logger.error(f"Alternatif rastgele kanal adı '{channel_name}' de mevcut."); return None
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
    except Exception as e: logger.error(f"Kanal oluşturmada hata: {e}\n{traceback.format_exc()}"); return None

async def send_to_ai_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    """Belirtilen kanalda seçili AI modeline (Gemini/DeepSeek) mesaj gönderir ve yanıtlar."""
    global channel_last_active, active_ai_chats

    if not prompt_text.strip(): return False

    # --- Aktif Sohbeti Başlat veya Yükle ---
    if channel_id not in active_ai_chats:
        try:
            conn = db_connect(); cursor = conn.cursor(cursor_factory=DictCursor); cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = %s", (channel_id,)); result = cursor.fetchone(); conn.close()
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
                gemini_model_instance = genai.GenerativeModel(f"models/{actual_model_name}")
                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': gemini_model_instance.start_chat(history=[]),
                    'history': None
                }
            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                if not DEEPSEEK_API_KEY: raise ValueError("DeepSeek API anahtarı ayarlı değil.")
                if not DEEPSEEK_AVAILABLE: raise ImportError("DeepSeek kütüphanesi yüklenemedi.")
                active_ai_chats[channel_id] = {
                    'model': current_model_with_prefix,
                    'session': None,
                    'history': []
                }
            else: # Bu duruma yukarıdaki kontrolle gelinmemeli
                raise ValueError(f"Tanımsız model ön eki: {current_model_with_prefix}")

        except Exception as e:
            logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı: {e}")
            try: await channel.send("Yapay zeka oturumu başlatılamadı. Model/API anahtarı sorunu olabilir.", delete_after=15)
            except: pass
            # Hata durumunda kanalı DB'den silmek veya varsayılana döndürmek düşünülebilir
            # remove_temp_channel_db(channel_id) # Veya
            # add_temp_channel_db(channel_id, author.id, datetime.datetime.now(datetime.timezone.utc), DEFAULT_MODEL_NAME)
            return False

    # --- Sohbet Verilerini Al ---
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
                gemini_session = chat_data['session']
                if not gemini_session: raise ValueError("Gemini oturumu bulunamadı.")
                response = await gemini_session.send_message_async(prompt_text)
                ai_response_text = response.text.strip()
                # Gemini güvenlik kontrolü
                # Not: prompt_feedback bazen None olabilir, kontrol ekleyelim
                prompt_feedback_str = str(getattr(response, 'prompt_feedback', ''))
                candidates_str = str(getattr(response, 'candidates', []))
                if "finish_reason: SAFETY" in prompt_feedback_str.upper() or \
                   "finish_reason=SAFETY" in candidates_str.upper() or \
                   "finish_reason: OTHER" in prompt_feedback_str.upper(): # Bazen OTHER da güvenlik olabiliyor
                     user_error_msg = "Yanıt güvenlik filtrelerine takıldı veya oluşturulamadı."
                     error_occurred = True

            elif current_model_with_prefix.startswith(DEEPSEEK_PREFIX):
                if not DEEPSEEK_AVAILABLE: raise ImportError("DeepSeek kütüphanesi kullanılamıyor.")
                history = chat_data['history']
                actual_model_name = current_model_with_prefix[len(DEEPSEEK_PREFIX):]
                history.append({"role": "user", "content": prompt_text})

                # DeepSeek istemcisini oluştur (API anahtarı başta kontrol edildi)
                client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)

                # API çağrısını thread'de yap
                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=actual_model_name,
                    messages=history,
                    # max_tokens=1024, # Örnek parametre
                    # temperature=0.7 # Örnek parametre
                )

                if response.choices:
                    ai_response_text = response.choices[0].message.content.strip()
                    # Yanıtı geçmişe ekle
                    history.append({"role": "assistant", "content": ai_response_text})
                    # DeepSeek güvenlik kontrolü (finish_reason'a bakılabilir)
                    finish_reason = response.choices[0].finish_reason
                    if finish_reason == 'length':
                        logger.warning(f"DeepSeek yanıtı max_tokens sınırına ulaştı (model: {actual_model_name})")
                    # DeepSeek'in güvenlik için özel bir finish_reason'ı varsa burada kontrol et
                    # elif finish_reason == 'content_filter':
                    #     user_error_msg = "DeepSeek yanıtı içerik filtrelerine takıldı."
                    #     error_occurred = True
                    #     history.pop() # Başarısız yanıtı geçmişten çıkar
                else:
                    logger.warning(f"[AI CHAT/{current_model_with_prefix}] DeepSeek'ten boş yanıt alındı. Finish Reason: {response.choices[0].finish_reason if response.choices else 'Yok'}")
                    ai_response_text = None
                    # Kullanıcıya mesaj verilebilir
                    user_error_msg = "DeepSeek'ten bir yanıt alınamadı."
                    error_occurred = True
                    history.pop() # Başarısız isteği geçmişten çıkar

            else:
                logger.error(f"İşlenemeyen model türü: {current_model_with_prefix}")
                error_occurred = True

            # --- Yanıt İşleme ve Gönderme ---
            if error_occurred:
                 pass # Hata mesajı zaten ayarlandı
            elif not ai_response_text:
                logger.warning(f"[AI CHAT/{current_model_with_prefix}] Boş yanıt (Kanal: {channel_id}).")
                # Sessiz kalabiliriz
            elif len(ai_response_text) > 2000:
                logger.info(f"Yanıt >2000kr (Kanal: {channel_id}), parçalanıyor...")
                for i in range(0, len(ai_response_text), 2000):
                    await channel.send(ai_response_text[i:i+2000])
            else:
                await channel.send(ai_response_text)

            # Başarılı ise aktivite zamanını güncelle
            if not error_occurred:
                 now_utc = datetime.datetime.now(datetime.timezone.utc)
                 channel_last_active[channel_id] = now_utc
                 update_channel_activity_db(channel_id, now_utc)
                 return True

        except ImportError as e: # Deepseek kütüphanesi yoksa
             logger.error(f"Gerekli kütüphane bulunamadı: {e}")
             error_occurred = True
             user_error_msg = "Gerekli yapay zeka kütüphanesi sunucuda bulunamadı."
        except Exception as e:
            logger.error(f"[AI CHAT/{current_model_with_prefix}] API/İşlem hatası (Kanal: {channel_id}): {e}")
            logger.error(traceback.format_exc())
            error_occurred = True
            error_str = str(e).lower()
            # Spesifik hata mesajları
            if "api key" in error_str or "authentication" in error_str or "permission_denied" in error_str:
                user_error_msg = "API Anahtarı sorunu veya yetki reddi."
            elif "quota" in error_str or "limit" in error_str or "429" in error_str or "resource_exhausted" in error_str:
                user_error_msg = "API kullanım limiti aşıldı."
            elif "invalid" in error_str or "400" in error_str or "bad request" in error_str or "not found" in error_str:
                 user_error_msg = "Geçersiz istek (örn: model adı yanlış olabilir veya API yolu bulunamadı)."
            elif "500" in error_str or "internal error" in error_str or "unavailable" in error_str:
                 user_error_msg = "Yapay zeka sunucusunda geçici bir sorun oluştu."
            # Güvenlik hataları yukarıda ele alındı

            # Eğer hata DeepSeek geçmişiyle ilgiliyse, geçmişi sıfırlamayı dene
            if DEEPSEEK_PREFIX in current_model_with_prefix and 'history' in locals() and isinstance(e, (TypeError, ValueError)): # Örnek kontrol
                 logger.warning(f"DeepSeek geçmişiyle ilgili potansiyel hata, geçmiş sıfırlanıyor: {channel_id}")
                 if channel_id in active_ai_chats:
                      active_ai_chats[channel_id]['history'] = [] # Geçmişi sıfırla
                 user_error_msg += " (Konuşma geçmişi olası bir hata nedeniyle sıfırlandı.)"


    # Hata oluştuysa kullanıcıya mesaj gönder
    if error_occurred:
        try: await channel.send(f"⚠️ {user_error_msg}", delete_after=15)
        except: pass
        return False

    return False # Beklenmedik bir durum

# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'{bot.user} olarak giriş yapıldı.')

    # Ayarları yükle (DB'den)
    entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
    inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))
    try: entry_channel_id = int(entry_channel_id_str) if entry_channel_id_str else None
    except (ValueError, TypeError): logger.error(f"DB/Env'den ENTRY_CHANNEL_ID yüklenemedi: {entry_channel_id_str}."); entry_channel_id = None
    try: inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str))
    except (ValueError, TypeError): logger.error(f"DB/Env'den inactivity_timeout_hours yüklenemedi: {inactivity_timeout_hours_str}."); inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)

    logger.info(f"Ayarlar yüklendi - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")

    # Kalıcı verileri temizle ve DB'den yükle
    logger.info("Kalıcı veriler yükleniyor...");
    temporary_chat_channels.clear(); user_to_channel_map.clear(); channel_last_active.clear(); active_ai_chats.clear()
    warned_inactive_channels.clear()

    loaded_channels = load_all_temp_channels(); valid_channel_count = 0; invalid_channel_ids = []
    for ch_id, u_id, last_active_ts, ch_model_name_with_prefix in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel):
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            channel_last_active[ch_id] = last_active_ts
            # Başlangıçta aktif sohbet oturumu oluşturmuyoruz, ilk mesaja kadar bekliyoruz.
            valid_channel_count += 1
        else:
            logger.warning(f"DB'deki geçici kanal {ch_id} Discord'da bulunamadı/geçersiz. DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)
    for invalid_id in invalid_channel_ids: remove_temp_channel_db(invalid_id)

    logger.info(f"{valid_channel_count} aktif geçici kanal DB'den yüklendi.")
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")

    # Bot aktivitesini ayarla
    entry_channel_name = "Ayarlanmadı"
    try:
        entry_channel = bot.get_channel(entry_channel_id) if entry_channel_id else None
        if entry_channel: entry_channel_name = f"#{entry_channel.name}"
        elif entry_channel_id: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı.")
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için {entry_channel_name}"))
    except Exception as e: logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    # İnaktivite kontrol görevini başlat
    if not check_inactivity.is_running():
        check_inactivity.start()
        logger.info("İnaktivite kontrol görevi başlatıldı.")

    logger.info("Bot komutları ve mesajları dinliyor..."); print("-" * 20)

@bot.event
async def on_message(message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    if message.author == bot.user or message.author.bot: return
    if isinstance(message.channel, discord.DMChannel): return
    if entry_channel_id is None: return # Giriş kanalı ayarlı değilse işlem yapma

    author = message.author; author_id = author.id
    channel = message.channel; channel_id = channel.id
    guild = message.guild

    # Komut kontrolü
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    if ctx.valid: return # Mesaj bir komutsa burada dur

    # Otomatik Kanal Oluşturma (Giriş Kanalında)
    if channel_id == entry_channel_id:
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]
            active_channel = bot.get_channel(active_channel_id)
            mention = f"<#{active_channel_id}>" if active_channel else f"silinmiş kanal (ID: {active_channel_id})"
            logger.info(f"{author.name} giriş kanalına yazdı ama aktif kanalı var: {mention}")
            try:
                info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15)
                await message.delete(delay=15)
            except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
            return

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
                 timeout_hours = inactivity_timeout.total_seconds() / 3600 if inactivity_timeout else DEFAULT_INACTIVITY_TIMEOUT_HOURS
                 embed.add_field(name="⏳ Otomatik Kapanma", value=f"Kanal `{timeout_hours:.1f}` saat işlem görmezse otomatik olarak silinir.", inline=False)
                 prefix = bot.command_prefix[0]
                 embed.add_field(name="🛑 Kapat", value=f"`{prefix}endchat`", inline=True)
                 embed.add_field(name="🔄 Model Seç (Sonraki)", value=f"`{prefix}setmodel <model>`", inline=True)
                 embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{prefix}resetchat`", inline=True)
                 await new_channel.send(embed=embed)
            except Exception as e:
                 logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                 try: await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.")
                 except Exception as fallback_e: logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")

            # Kullanıcıya bilgi ver (Giriş kanalında)
            try: await channel.send(f"{author.mention}, özel sohbet kanalı {new_channel.mention} oluşturuldu!", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            # Kullanıcının ilk mesajını yeni kanala gönder
            try:
                await new_channel.send(f"**{author.display_name}:** {initial_prompt}")
                logger.info(f"Kullanıcının ilk mesajı ({original_message_id}) yeni kanala ({new_channel_id}) kopyalandı.")
            except Exception as e: logger.error(f"Kullanıcının ilk mesajı yeni kanala kopyalanamadı: {e}")

            # AI'ye ilk isteği gönder
            try:
                logger.info(f"-----> AI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_with_prefix})")
                success = await send_to_ai_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success: logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else: logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e: logger.error(f"İlk mesaj işlenirken/gönderilirken hata: {e}")

            # Orijinal mesajı sil
            try:
                msg_to_delete = await channel.fetch_message(original_message_id)
                await msg_to_delete.delete()
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except: pass # Hataları görmezden gel
        else:
            try: await channel.send(f"{author.mention}, üzgünüm, özel kanal oluşturulamadı.", delete_after=20)
            except: pass
            try: await message.delete(delay=20)
            except: pass
        return

    # Geçici Sohbet Kanallarındaki Mesajlar
    if channel_id in temporary_chat_channels and not message.author.bot:
        # Komut değilse (yukarıda kontrol edildi), AI'ye gönder
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
    # Uyarı eşiği (timeout'dan 10 dk önce veya timeout'un %90'ı, hangisi daha kısaysa)
    warning_delta = min(datetime.timedelta(minutes=10), inactivity_timeout * 0.1)
    warning_threshold = inactivity_timeout - warning_delta

    for channel_id, last_active_time in list(channel_last_active.items()):
        if channel_id not in temporary_chat_channels:
             logger.warning(f"İnaktivite kontrol: {channel_id} `channel_last_active` içinde ama `temporary_chat_channels` içinde değil. Atlanıyor.")
             channel_last_active.pop(channel_id, None)
             warned_inactive_channels.discard(channel_id)
             continue

        time_inactive = now - last_active_time

        if time_inactive > inactivity_timeout:
            channels_to_delete.append(channel_id)
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels:
            channels_to_warn.append(channel_id)

    # Uyarıları gönder
    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                remaining_time = inactivity_timeout - (now - channel_last_active[channel_id])
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir.", delete_after=300)
                warned_inactive_channels.add(channel_id)
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id}")
            except discord.errors.NotFound: warned_inactive_channels.discard(channel_id)
            except Exception as e: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else:
            warned_inactive_channels.discard(channel_id)

    # Silinecek kanalları işle
    if channels_to_delete:
        logger.info(f"İnaktivite: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id)
            reason = "İnaktivite nedeniyle otomatik silindi."
            if channel_to_delete:
                try:
                    await channel_to_delete.delete(reason=reason)
                    logger.info(f"İnaktif kanal '{channel_to_delete.name}' (ID: {channel_id}) silindi.")
                    # State temizliği on_guild_channel_delete tarafından yapılacak.
                except discord.errors.NotFound:
                    logger.warning(f"İnaktif kanal (ID: {channel_id}) silinirken bulunamadı. State manuel temizleniyor.")
                    # Manuel temizlik (on_guild_channel_delete tetiklenmeyebilir)
                    temporary_chat_channels.discard(channel_id)
                    active_ai_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
                    if user_id: user_to_channel_map.pop(user_id[0], None)
                    remove_temp_channel_db(channel_id)
                except Exception as e:
                    logger.error(f"İnaktif kanal (ID: {channel_id}) silinirken hata: {e}")
                    remove_temp_channel_db(channel_id) # Hata olsa bile DB'den silmeyi dene
            else:
                logger.warning(f"İnaktif kanal (ID: {channel_id}) Discord'da bulunamadı. DB'den ve state'den siliniyor.")
                # Manuel temizlik
                temporary_chat_channels.discard(channel_id)
                active_ai_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                warned_inactive_channels.discard(channel_id)
                user_id = [uid for uid, cid in user_to_channel_map.items() if cid == channel_id]
                if user_id: user_to_channel_map.pop(user_id[0], None)
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
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) silindi, tüm ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id)
        active_ai_chats.pop(channel_id, None)
        channel_last_active.pop(channel_id, None)
        warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id: user_id_to_remove = user_id; break
        if user_id_to_remove:
            user_to_channel_map.pop(user_id_to_remove, None)
            logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi.")
        else: logger.warning(f"Silinen kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")
        remove_temp_channel_db(channel_id)

# --- Komutlar ---

@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx):
    """Mevcut geçici AI sohbet kanalını siler."""
    channel_id = ctx.channel.id
    author_id = ctx.author.id

    if channel_id not in temporary_chat_channels:
         # State'de yoksa DB'ye bak (nadiren olabilir)
         conn = db_connect(); cursor=conn.cursor(cursor_factory=DictCursor); cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,)); owner_row = cursor.fetchone(); conn.close()
         if owner_row:
             logger.warning(f".endchat: Kanal {channel_id} state'de yok ama DB'de var (Sahip: {owner_row['user_id']}). State'e ekleniyor.")
             temporary_chat_channels.add(channel_id)
         else:
            await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass; return

    # Sahibi bul
    expected_user_id = None
    for user_id, ch_id in user_to_channel_map.items():
        if ch_id == channel_id: expected_user_id = user_id; break
    if expected_user_id is None: # State'de yoksa DB'ye bak
        conn = db_connect(); cursor=conn.cursor(cursor_factory=DictCursor); cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,)); owner_row = cursor.fetchone(); conn.close()
        if owner_row: expected_user_id = owner_row['user_id']
        else: logger.error(f".endchat: Kanal {channel_id} sahibi bulunamadı!")

    if expected_user_id and author_id != expected_user_id:
        await ctx.send("Bu kanalı sadece oluşturan kişi kapatabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send("Kanalları yönetme iznim yok.", delete_after=10)
        return

    try:
        logger.info(f"Kanal '{ctx.channel.name}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı. State temizleniyor.")
        # Manuel temizlik (on_guild_channel_delete tetiklenmeyebilir)
        temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id)
        if expected_user_id: user_to_channel_map.pop(expected_user_id, None)
        remove_temp_channel_db(channel_id)
    except Exception as e:
        logger.error(f".endchat komutunda kanal silinirken hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Kanal silinirken bir hata oluştu.", delete_after=10)

@bot.command(name='resetchat', aliases=['sıfırla'])
@commands.guild_only()
async def reset_chat_session(ctx):
    """Mevcut geçici sohbet kanalının AI konuşma geçmişini/oturumunu sıfırlar."""
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece geçici sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10); except: pass
        return

    if channel_id in active_ai_chats:
        active_ai_chats.pop(channel_id, None) # Oturumu veya geçmişi temizle
        logger.info(f"Sohbet geçmişi/oturumu {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi/oturumu sıfırlandı.", delete_after=15)
    else:
        logger.info(f"Sıfırlanacak aktif oturum/geçmiş yok: Kanal {channel_id}")
        await ctx.send("Aktif bir konuşma geçmişi/oturumu bulunmuyor.", delete_after=10)
    try: await ctx.message.delete(delay=15); except: pass

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx, amount: str = None):
    """Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç)."""
    if not ctx.guild.me.guild_permissions.manage_messages:
        await ctx.send("Mesajları silebilmem için 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10); return

    if amount is None:
        await ctx.send(f"Silinecek mesaj sayısı (`{ctx.prefix}clear 5`) veya tümü için `{ctx.prefix}clear all` yazın.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass; return

    deleted_count = 0; skipped_pinned = 0
    def check_not_pinned(m): nonlocal skipped_pinned; if m.pinned: skipped_pinned += 1; return False; return True

    try:
        if amount.lower() == 'all':
            await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)...", delete_after=5); try: await ctx.message.delete(); except: pass
            while True:
                deleted = await ctx.channel.purge(limit=100, check=check_not_pinned, bulk=True)
                deleted_count += len(deleted)
                if len(deleted) < 100: break
                await asyncio.sleep(1)
            msg = f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi."
            if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
            await ctx.send(msg, delete_after=10)
        else:
            try:
                limit = int(amount); assert limit > 0; limit_with_command = limit + 1
                deleted = await ctx.channel.purge(limit=limit_with_command, check=check_not_pinned, bulk=True)
                actual_deleted_count = len(deleted) - (1 if ctx.message in deleted else 0)
                msg = f"{actual_deleted_count} mesaj silindi."
                if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
                if ctx.message not in deleted: try: await ctx.message.delete(); except: pass
                await ctx.send(msg, delete_after=5)
            except (ValueError, AssertionError):
                 await ctx.send(f"Geçersiz sayı '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass
    except discord.errors.Forbidden:
        logger.error(f"HATA: '{ctx.channel.name}' kanalında silme izni yok!"); await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass
    except Exception as e:
        logger.error(f".clear hatası: {e}\n{traceback.format_exc()}"); await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass

@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def ask_in_channel(ctx, *, question: str = None):
    """Sorulan soruyu varsayılan Gemini modeline iletir ve yanıtı geçici olarak bu kanalda gösterir."""
    if not gemini_default_model_instance: # Gemini başlatılamadıysa
        await ctx.reply("⚠️ Varsayılan Gemini modeli kullanılamıyor. API anahtarını kontrol edin.", delete_after=10); try: await ctx.message.delete(delay=10); except: pass; return
    if question is None:
        error_msg = await ctx.reply(f"Lütfen soru sorun (örn: `{ctx.prefix}ask ...`).", delete_after=15); try: await ctx.message.delete(delay=15); except: pass; return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None

    try:
        async with ctx.typing():
            try:
                # Blocking API çağrısını ayrı thread'de çalıştır
                response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e:
                 logger.error(f".ask için Gemini API hatası: {gemini_e}"); user_msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."; await ctx.reply(user_msg, delete_after=10); try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); except: pass; return

            gemini_response = response.text.strip()
            # Güvenlik kontrolü
            prompt_feedback_str = str(getattr(response, 'prompt_feedback', ''))
            candidates_str = str(getattr(response, 'candidates', []))
            if "finish_reason: SAFETY" in prompt_feedback_str.upper() or \
               "finish_reason=SAFETY" in candidates_str.upper() or \
               "finish_reason: OTHER" in prompt_feedback_str.upper():
                 await ctx.reply("Yanıt güvenlik filtrelerine takıldı veya oluşturulamadı.", delete_after=15); try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); except: pass; return

        if not gemini_response:
            logger.warning(f"Gemini'den .ask için boş yanıt alındı."); await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15); try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); except: pass; return

        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)
        response_display = gemini_response if len(gemini_response) <= 1024 else gemini_response[:1021] + "..."
        embed.add_field(name="Yanıt", value=response_display, inline=False) # Başlık " yanıt" yerine "Yanıt"
        embed.set_footer(text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik olarak silinecektir.")
        bot_response_message = await ctx.reply(embed=embed, mention_author=False)
        logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")

        try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); logger.info(f".ask komut mesajı ({ctx.message.id}) silinmek üzere zamanlandı.")
        except: pass
        try: await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY); logger.info(f".ask yanıt mesajı ({bot_response_message.id}) silinmek üzere zamanlandı.")
        except: pass
    except Exception as e:
        logger.error(f".ask genel hatası: {e}\n{traceback.format_exc()}"); await ctx.reply("Sorunuz işlenirken hata.", delete_after=15); try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); except: pass

@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def list_available_models(ctx):
    """Sohbet için kullanılabilir Gemini ve DeepSeek modellerini listeler."""
    all_models_list = []
    await ctx.send("Kullanılabilir modeller kontrol ediliyor...", delete_after=5)

    async def fetch_gemini():
        if not GEMINI_API_KEY: return ["_(Gemini API anahtarı ayarlı değil)_"]
        try:
            gemini_models_list = []
            gemini_models = await asyncio.to_thread(genai.list_models)
            for m in gemini_models:
                if 'generateContent' in m.supported_generation_methods:
                    model_id = m.name.split('/')[-1]
                    prefix = ""
                    if "gemini-1.5-flash" in model_id: prefix = "⚡ "
                    elif "gemini-1.5-pro" in model_id: prefix = "✨ "
                    elif "gemini-pro" == model_id: prefix = "✅ "
                    gemini_models_list.append(f"{GEMINI_PREFIX}{prefix}`{model_id}`")
            return gemini_models_list
        except Exception as e:
            logger.error(f"Gemini modelleri listelenirken hata: {e}")
            return ["_(Gemini modelleri alınamadı)_"]

    async def fetch_deepseek():
        if not DEEPSEEK_API_KEY: return ["_(DeepSeek API anahtarı ayarlı değil)_"]
        if not DEEPSEEK_AVAILABLE: return ["_(DeepSeek kütüphanesi bulunamadı)_"]
        try:
            # DeepSeek modellerini listele (API destekliyorsa)
            deepseek_models_list = []
            # Varsayım: OpenAI benzeri listeleme
            # client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)
            # response = await asyncio.to_thread(client.models.list)
            # for model in response.data:
            #     deepseek_models_list.append(f"{DEEPSEEK_PREFIX}🧭 `{model.id}`")

            # Şimdilik bilinen popüler modelleri ekleyelim (API'den dinamik almak daha iyi)
            known_deepseek_models = ["deepseek-chat", "deepseek-coder"]
            for model_id in known_deepseek_models:
                 deepseek_models_list.append(f"{DEEPSEEK_PREFIX}🧭 `{model_id}`")

            return deepseek_models_list
        except Exception as e:
            logger.error(f"DeepSeek modelleri listelenirken hata: {e}")
            return ["_(DeepSeek modelleri alınamadı)_"]

    # Modelleri paralel olarak çek
    results = await asyncio.gather(fetch_gemini(), fetch_deepseek())
    all_models_list.extend(results[0]) # Gemini sonuçları
    all_models_list.extend(results[1]) # DeepSeek sonuçları

    if not all_models_list or all(s.startswith("_(") for s in all_models_list):
        await ctx.send("Kullanılabilir model bulunamadı veya API anahtarları ayarlanmamış.")
        return

    all_models_list.sort() # Ön eke göre sırala

    embed = discord.Embed(
        title="🤖 Kullanılabilir Yapay Zeka Modelleri",
        description=f"Bir sonraki özel sohbetiniz için `{ctx.prefix}setmodel <model_adi_on_ekli>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(all_models_list),
        color=discord.Color.gold()
    )
    embed.add_field(name="Ön Ekler", value=f"`{GEMINI_PREFIX}` Gemini Modeli\n`{DEEPSEEK_PREFIX}` DeepSeek Modeli", inline=False)
    embed.set_footer(text="⚡ Flash, ✨ Pro (Gemini), ✅ Eski Pro (Gemini), 🧭 DeepSeek")
    await ctx.send(embed=embed)

@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx, model_id_with_or_without_prefix: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini veya DeepSeek modelini ayarlar."""
    global user_next_model
    if model_id_with_or_without_prefix is None:
        await ctx.send(f"Lütfen bir model adı belirtin (örn: `{GEMINI_PREFIX}gemini-1.5-flash-latest` veya `{DEEPSEEK_PREFIX}deepseek-chat`). Modeller için `{ctx.prefix}listmodels`.", delete_after=15); try: await ctx.message.delete(delay=15); except: pass; return

    model_input = model_id_with_or_without_prefix.strip()
    selected_model_full_name = None
    is_valid = False

    async with ctx.typing():
        if model_input.startswith(GEMINI_PREFIX):
            if not GEMINI_API_KEY: await ctx.send(f"❌ Gemini API anahtarı ayarlı değil.", delete_after=15); return
            actual_model_name = model_input[len(GEMINI_PREFIX):]
            target_gemini_name = f"models/{actual_model_name}"
            try:
                await asyncio.to_thread(genai.get_model, target_gemini_name)
                selected_model_full_name = model_input
                is_valid = True
                logger.info(f"{ctx.author.name} Gemini modelini doğruladı: {target_gemini_name}")
            except Exception as e: logger.warning(f"Geçersiz Gemini modeli denendi ({target_gemini_name}): {e}")

        elif model_input.startswith(DEEPSEEK_PREFIX):
            if not DEEPSEEK_API_KEY: await ctx.send(f"❌ DeepSeek API anahtarı ayarlı değil.", delete_after=15); return
            if not DEEPSEEK_AVAILABLE: await ctx.send(f"❌ DeepSeek kütüphanesi sunucuda bulunamadı.", delete_after=15); return
            actual_model_name = model_input[len(DEEPSEEK_PREFIX):]
            try:
                # DeepSeek model varlığını API ile doğrula (eğer destekliyorsa)
                # client = DeepSeekClient(api_key=DEEPSEEK_API_KEY)
                # await asyncio.to_thread(client.models.retrieve, actual_model_name)
                # Şimdilik basitçe kabul edelim
                selected_model_full_name = model_input
                is_valid = True
                logger.info(f"{ctx.author.name} DeepSeek modelini ayarladı: {actual_model_name}")
            except Exception as e: logger.warning(f"DeepSeek modeli doğrulanırken hata ({actual_model_name}): {e}")

        else:
            await ctx.send(f"❌ Lütfen model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_PREFIX}` ön ekini ekleyin.", delete_after=20); try: await ctx.message.delete(delay=20); except: pass; return

    if is_valid and selected_model_full_name:
        user_next_model[ctx.author.id] = selected_model_full_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{selected_model_full_name}' olarak ayarlandı.")
        await ctx.send(f"✅ Başarılı! Bir sonraki özel sohbetiniz `{selected_model_full_name}` modeli ile başlayacak.", delete_after=20)
    else:
        await ctx.send(f"❌ `{model_input}` geçerli veya erişilebilir bir model değil. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)

    try: await ctx.message.delete(delay=20); except: pass


@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_entry_channel(ctx, channel: discord.TextChannel = None):
    """Otomatik sohbet kanalı oluşturulacak giriş kanalını ayarlar (Admin)."""
    global entry_channel_id
    if channel is None: await ctx.send(f"Lütfen bir metin kanalı etiketleyin veya ID'sini yazın."); return
    if not isinstance(channel, discord.TextChannel): await ctx.send("Lütfen geçerli bir metin kanalı belirtin."); return

    entry_channel_id = channel.id
    save_config('entry_channel_id', entry_channel_id)
    logger.info(f"Giriş kanalı yönetici {ctx.author.name} tarafından {channel.mention} (ID: {channel.id}) olarak ayarlandı.")
    await ctx.send(f"✅ Giriş kanalı başarıyla {channel.mention} olarak ayarlandı.")
    try: await bot.change_presence(activity=discord.Game(name=f"Sohbet için #{channel.name}"))
    except Exception as e: logger.warning(f"Giriş kanalı ayarlandıktan sonra bot aktivitesi güncellenemedi: {e}")

@bot.command(name='settimeout', aliases=['zamanasimi'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_inactivity_timeout(ctx, hours: float = None):
    """Geçici kanalların aktif olmazsa silineceği süreyi saat cinsinden ayarlar (Admin)."""
    global inactivity_timeout
    current_timeout_hours = inactivity_timeout.total_seconds()/3600 if inactivity_timeout else 'Ayarlanmamış'
    if hours is None: await ctx.send(f"Lütfen pozitif bir saat değeri girin (örn: `{ctx.prefix}settimeout 2.5`). Mevcut: `{current_timeout_hours}` saat"); return
    if hours <= 0: await ctx.send("Lütfen pozitif bir saat değeri girin."); return
    if hours < 0.1: await ctx.send("Minimum zaman aşımı 0.1 saattir (6 dakika)."); return
    if hours > 168: await ctx.send("Maksimum zaman aşımı 168 saattir (1 hafta)."); return

    inactivity_timeout = datetime.timedelta(hours=hours)
    save_config('inactivity_timeout_hours', str(hours))
    logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours} saat olarak ayarlandı.")
    await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **{hours} saat** olarak ayarlandı.")

@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx):
    """Botun kullanılabilir komutlarını listeler."""
    entry_channel_mention = "Ayarlanmamış"
    if entry_channel_id: entry_channel = bot.get_channel(entry_channel_id); entry_channel_mention = f"<#{entry_channel_id}>" if entry_channel else f"ID: {entry_channel_id} (Bulunamadı)"

    embed = discord.Embed(title=f"{bot.user.name} Komut Listesi", description=f"**Özel Sohbet Başlatma:**\n{entry_channel_mention} kanalına mesaj yazın.\n\n**Diğer Komutlar:**", color=discord.Color.dark_purple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)

    user_cmds, chat_cmds, admin_cmds = [], [], []
    for command in sorted(bot.commands, key=lambda cmd: cmd.name):
        if command.hidden: continue
        try:
             can_run = await command.can_run(ctx)
             if not can_run: continue
        except: continue # Check hatası veya başka bir sorun

        help_text = command.help or command.short_doc or "Açıklama yok."
        aliases = f" (Diğer: `{'`, `'.join(command.aliases)}`)" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""
        cmd_string = f"`{ctx.prefix}{command.name}{params}`{aliases}\n{help_text}"

        is_admin = any(check.__qualname__.startswith('has_permissions') or check.__qualname__.startswith('is_owner') for check in command.checks)
        if is_admin and 'administrator=True' in str(command.checks): admin_cmds.append(cmd_string)
        elif command.name in ['endchat', 'resetchat', 'clear']: chat_cmds.append(cmd_string)
        elif is_admin: admin_cmds.append(cmd_string) # Diğer izinli komutlar da admin'e
        else: user_cmds.append(cmd_string)

    if user_cmds: embed.add_field(name="👤 Genel Komutlar", value="\n\n".join(user_cmds), inline=False)
    if chat_cmds: embed.add_field(name="💬 Sohbet Kanalı Komutları", value="\n\n".join(chat_cmds), inline=False)
    if admin_cmds: embed.add_field(name="🛠️ Yönetici Komutları", value="\n\n".join(admin_cmds), inline=False)

    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)}")
    try: await ctx.send(embed=embed)
    except discord.errors.HTTPException: await ctx.send("Komut listesi çok uzun olduğu için gönderilemedi.")


# --- Genel Hata Yakalama ---
@bot.event
async def on_command_error(ctx, error):
    """Komutlarla ilgili hataları merkezi olarak yakalar."""
    original_error = getattr(error, 'original', error)
    delete_user_msg = True; delete_delay = 10

    if isinstance(original_error, commands.CommandNotFound): return

    if isinstance(original_error, commands.MissingPermissions):
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {original_error.missing_permissions}"); perms = ", ".join(p.replace('_', ' ').title() for p in original_error.missing_permissions); delete_delay = 15
        await ctx.send(f"⛔ Üzgünüm {ctx.author.mention}, şu izinlere sahip olmalısın: **{perms}**", delete_after=delete_delay)
    elif isinstance(original_error, commands.BotMissingPermissions):
        logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {original_error.missing_permissions}"); perms = ", ".join(p.replace('_', ' ').title() for p in original_error.missing_permissions); delete_delay = 15; delete_user_msg = False
        await ctx.send(f"🆘 Benim şu izinlere sahip olmam gerekiyor: **{perms}**", delete_after=delete_delay)
    elif isinstance(original_error, commands.NoPrivateMessage):
        logger.warning(f"'{ctx.command.qualified_name}' DM'de kullanılamaz."); delete_user_msg = False; try: await ctx.author.send("Bu komut sadece sunucu kanallarında kullanılabilir.") except: pass
    elif isinstance(original_error, commands.PrivateMessageOnly):
        logger.warning(f"'{ctx.command.qualified_name}' sunucuda kullanıldı."); await ctx.send("Bu komut sadece özel mesajla (DM) kullanılabilir.", delete_after=10)
    elif isinstance(original_error, commands.CheckFailure):
        logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {original_error}"); await ctx.send("🚫 Bu komutu kullanma yetkiniz yok veya koşullar sağlanmıyor.", delete_after=10)
    elif isinstance(original_error, commands.CommandOnCooldown):
        delete_delay = max(5, int(original_error.retry_after) + 1); await ctx.send(f"⏳ Beklemede. Lütfen **{original_error.retry_after:.1f} saniye** sonra tekrar dene.", delete_after=delete_delay)
    elif isinstance(original_error, commands.UserInputError):
         delete_delay = 15; command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with; usage = f"`{ctx.prefix}{command_name}{ctx.command.signature if ctx.command else ''}`"; error_message = "Hatalı komut kullanımı."
         if isinstance(original_error, commands.MissingRequiredArgument): error_message = f"Eksik argüman: `{original_error.param.name}`."
         elif isinstance(original_error, commands.BadArgument): error_message = f"Geçersiz argüman türü: {original_error}"
         elif isinstance(original_error, commands.TooManyArguments): error_message = "Çok fazla argüman girdiniz."
         await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay)
    else:
        logger.error(f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' işlenirken beklenmedik hata: {original_error}"); traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__)); logger.error(f"Traceback:\n{traceback_str}"); delete_delay = 15
        await ctx.send("⚙️ Komut işlenirken beklenmedik bir hata oluştu.", delete_after=delete_delay)

    if delete_user_msg and ctx.guild:
        try: await ctx.message.delete(delay=delete_delay)
        except: pass

# === Render için Web Sunucusu ===
app = Flask(__name__)
@app.route('/')
def home():
    """Basit bir sağlık kontrolü endpoint'i."""
    if bot and bot.is_ready():
        guild_count = len(bot.guilds); active_chats = len(temporary_chat_channels)
        return f"Bot '{bot.user.name}' çalışıyor. {guild_count} sunucu. {active_chats} aktif sohbet.", 200
    elif bot and not bot.is_ready(): return "Bot başlatılıyor, henüz hazır değil...", 503
    else: return "Bot durumu bilinmiyor veya başlatılamadı.", 500

def run_webserver():
    """Flask web sunucusunu ayrı bir thread'de çalıştırır."""
    port = int(os.environ.get("PORT", 8080))
    try:
        logger.info(f"Flask web sunucusu http://0.0.0.0:{port} adresinde başlatılıyor...")
        app.run(host='0.0.0.0', port=port)
    except Exception as e: logger.error(f"Web sunucusu başlatılırken kritik hata: {e}")
# ===================================

# --- Botu Çalıştır ---
if __name__ == "__main__":
    if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); exit()
    logger.info("Bot başlatılıyor...")
    try:
        setup_database()
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.LoginFailure: logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired: logger.critical("HATA: Gerekli Intent'ler Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err: logger.critical(f"PostgreSQL bağlantı hatası (Başlangıçta): {db_err}")
    except Exception as e: logger.critical(f"Bot çalıştırılırken kritik hata: {e}"); logger.critical(traceback.format_exc())
    finally: logger.info("Bot kapatılıyor...")