# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
import os
import google.generativeai as genai
from dotenv import load_dotenv
import asyncio
import logging
import traceback
import datetime
# import sqlite3 # Veritabanı için - KALDIRILDI
import psycopg2 # PostgreSQL için EKLE
from psycopg2.extras import DictCursor # Satırlara sözlük gibi erişim için EKLE
from flask import Flask # Koyeb/Render için web sunucusu
import threading      # Web sunucusunu ayrı thread'de çalıştırmak için

# --- Logging Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('discord_gemini_bot')
# Flask'ın kendi loglarını biraz kısmak için
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


# .env dosyasındaki değişkenleri yükle (Lokal geliştirme için)
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") # Render PostgreSQL bağlantı URL'si

# Varsayılan değerler (ortam değişkeni veya DB'den okunamazsa kullanılır)
# Bu değerler veritabanına ilk kurulumda yazılır veya load_config ile çekilir
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = os.getenv("DEFAULT_INACTIVITY_TIMEOUT_HOURS", "1") # String olarak al, sonra float'a çevir
DEFAULT_MODEL_NAME = 'models/gemini-1.5-flash-latest'
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Veritabanı Ayarları (Artık PostgreSQL) ---
# DB_FILE = 'bot_data.db' # KALDIRILDI

# --- Global Değişkenler (Başlangıçta None, on_ready'de doldurulacak) ---
entry_channel_id = None
inactivity_timeout = None
model_name = DEFAULT_MODEL_NAME # Başlangıçta varsayılan model

active_gemini_chats = {} # channel_id -> {'session': ChatSession, 'model': model_adı}
temporary_chat_channels = set() # Geçici kanal ID'leri
user_to_channel_map = {} # user_id -> channel_id
channel_last_active = {} # channel_id -> datetime (timezone-aware)
user_next_model = {} # user_id -> model_adı (Bir sonraki sohbet için tercih)
warned_inactive_channels = set() # İnaktivite uyarısı gönderilen kanallar

# --- Veritabanı Yardımcı Fonksiyonları (PostgreSQL için GÜNCELLENDİ) ---

def db_connect():
    """PostgreSQL veritabanı bağlantısı oluşturur."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL ortam değişkeni ayarlanmamış!")
    try:
        # Render genellikle dış bağlantılarda SSL gerektirir
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
        # temp_channels tablosu (PostgreSQL syntax)
        default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_active TIMESTAMPTZ NOT NULL,
                model_name TEXT DEFAULT '{default_model_short}'
            )
        ''')
        conn.commit() # Değişiklikleri kaydet
        cursor.close()
        logger.info("PostgreSQL veritabanı tabloları kontrol edildi/oluşturuldu.")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.critical(f"PostgreSQL veritabanı kurulumu sırasında KRİTİK HATA: {e}")
        if conn:
            conn.rollback() # Hata durumunda işlemleri geri al
        # Başlangıçta veritabanı kurulamazsa botun başlamaması mantıklı olabilir
        exit(1)
    finally:
        if conn:
            conn.close() # Bağlantıyı her zaman kapat

def save_config(key, value):
    """Yapılandırma ayarını PostgreSQL'e kaydeder (varsa günceller)."""
    conn = None
    # INSERT ... ON CONFLICT (PostgreSQL'e özgü "upsert")
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
        logger.debug(f"Yapılandırma kaydedildi: {key} = {value}")
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
        # DictCursor kullanarak sütun adlarıyla erişim
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute(sql, (key,))
        result = cursor.fetchone()
        cursor.close()
        # Eğer sonuç varsa ve 'value' sütunu varsa değerini döndür
        return result['value'] if result else default
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Yapılandırma yüklenirken PostgreSQL hatası (Key: {key}): {e}")
        return default # Hata durumunda varsayılanı döndür
    finally:
        if conn: conn.close()

def load_all_temp_channels():
    """Tüm geçici kanal durumlarını PostgreSQL'den yükler."""
    conn = None
    sql = "SELECT channel_id, user_id, last_active, model_name FROM temp_channels;"
    loaded_data = []
    default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
    try:
        conn = db_connect()
        cursor = conn.cursor(cursor_factory=DictCursor) # Sözlük olarak al
        cursor.execute(sql)
        channels = cursor.fetchall()
        cursor.close()

        for row in channels:
            try:
                # psycopg2 TIMESTAMPTZ'yi zaten timezone bilgili datetime objesine çevirir
                last_active_dt = row['last_active']
                # Zaman dilimi bilgisi yoksa UTC varsayalım (gerçi TIMESTAMPTZ bunu çözmeli)
                if last_active_dt.tzinfo is None:
                    last_active_dt = last_active_dt.replace(tzinfo=datetime.timezone.utc)

                model_name_db = row['model_name'] or default_model_short
                loaded_data.append((row['channel_id'], row['user_id'], last_active_dt, model_name_db))
            except (ValueError, TypeError, KeyError) as row_error:
                logger.error(f"DB satırı işlenirken hata (channel_id: {row.get('channel_id', 'Bilinmiyor')}): {row_error} - Satır: {row}")
        return loaded_data
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanallar yüklenirken PostgreSQL DB hatası: {e}")
        return [] # Hata durumunda boş liste döndür
    finally:
        if conn: conn.close()

def add_temp_channel_db(channel_id, user_id, timestamp, model_used):
    """Yeni geçici kanalı PostgreSQL'e ekler veya günceller."""
    conn = None
    # ON CONFLICT ile ekleme/güncelleme (upsert)
    sql = """
        INSERT INTO temp_channels (channel_id, user_id, last_active, model_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            last_active = EXCLUDED.last_active,
            model_name = EXCLUDED.model_name;
    """
    model_short_name = model_used.split('/')[-1]
    try:
        conn = db_connect()
        cursor = conn.cursor()
        # Timestamp'ın timezone bilgisi olduğundan emin ol (UTC)
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        cursor.execute(sql, (channel_id, user_id, timestamp, model_short_name))
        conn.commit()
        cursor.close()
        logger.debug(f"Geçici kanal eklendi/güncellendi: {channel_id}")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal PostgreSQL'e eklenirken/güncellenirken hata (channel_id: {channel_id}): {e}")
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
        # Timestamp'ın timezone bilgisi olduğundan emin ol (UTC)
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        cursor.execute(sql, (timestamp, channel_id))
        conn.commit()
        cursor.close()
        logger.debug(f"Kanal aktivitesi güncellendi: {channel_id}")
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
        rowcount = cursor.rowcount # Kaç satırın silindiğini kontrol et
        cursor.close()
        if rowcount > 0:
            logger.info(f"Geçici kanal {channel_id} PostgreSQL veritabanından silindi.")
        else:
            # Bu bir hata değil, kanal zaten silinmiş olabilir
            logger.warning(f"Silinecek geçici kanal {channel_id} PostgreSQL'de bulunamadı (muhtemelen zaten silinmişti).")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal PostgreSQL'den silinirken hata (channel_id: {channel_id}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

# --- Yapılandırma Kontrolleri (Başlangıçta) ---
# Veritabanı ve API anahtarlarının varlığını kontrol et
if not DISCORD_TOKEN: logger.critical("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı!"); exit(1)
if not GEMINI_API_KEY: logger.critical("HATA: GEMINI_API_KEY ortam değişkeni bulunamadı!"); exit(1)
if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); exit(1)

# Veritabanı tablolarını kontrol et/oluştur
try:
    setup_database()
except Exception as db_setup_err:
     logger.critical(f"Veritabanı kurulumu başlangıçta başarısız: {db_setup_err}")
     exit(1) # Veritabanı olmadan devam edemeyiz


# --- Yapılandırma Değerlerini Yükle (DB veya Varsayılan) ---
# Bu değerler on_ready içinde tekrar yüklenecek ama başlangıç logları için burada da alalım
loaded_entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
loaded_inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', DEFAULT_INACTIVITY_TIMEOUT_HOURS)

temp_entry_channel_id = None
if loaded_entry_channel_id_str:
    try: temp_entry_channel_id = int(loaded_entry_channel_id_str)
    except (ValueError, TypeError): logger.error(f"DB'den yüklenen giriş kanalı ID'si geçersiz: '{loaded_entry_channel_id_str}'. Varsayılan denenecek.")
    if not temp_entry_channel_id and DEFAULT_ENTRY_CHANNEL_ID: # DB geçersizse env varsayılana bak
        try: temp_entry_channel_id = int(DEFAULT_ENTRY_CHANNEL_ID)
        except (ValueError, TypeError): logger.error(f"Varsayılan giriş kanalı ID'si de geçersiz: '{DEFAULT_ENTRY_CHANNEL_ID}'.")
else: # DB'de yoksa env varsayılana bak
    if DEFAULT_ENTRY_CHANNEL_ID:
        try: temp_entry_channel_id = int(DEFAULT_ENTRY_CHANNEL_ID)
        except (ValueError, TypeError): logger.error(f"Varsayılan giriş kanalı ID'si geçersiz: '{DEFAULT_ENTRY_CHANNEL_ID}'.")

if not temp_entry_channel_id: logger.warning("UYARI: Giriş Kanalı ID'si ne ortam değişkeni ne de veritabanında geçerli olarak bulunamadı! Otomatik kanal oluşturma çalışmayacak.")
else: logger.info(f"Başlangıç Giriş Kanalı ID'si: {temp_entry_channel_id}")

temp_inactivity_timeout = None
try:
    temp_inactivity_timeout = datetime.timedelta(hours=float(loaded_inactivity_timeout_hours_str))
    logger.info(f"Başlangıç İnaktivite Zaman Aşımı: {temp_inactivity_timeout}")
except (ValueError, TypeError):
    logger.error(f"DB/Varsayılan İnaktivite süresi ('{loaded_inactivity_timeout_hours_str}') geçersiz! Varsayılan 1 saat kullanılacak.")
    try: # Güvenlik için tekrar varsayılanı deneyelim
        temp_inactivity_timeout = datetime.timedelta(hours=float(DEFAULT_INACTIVITY_TIMEOUT_HOURS))
    except: # Bu da başarısız olursa 1 saat hardcode
        temp_inactivity_timeout = datetime.timedelta(hours=1)
    logger.info(f"Geçerli İnaktivite Zaman Aşımı: {temp_inactivity_timeout}")


# Gemini API'yi yapılandır ve global modeli oluştur
model = None
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API anahtarı yapılandırıldı.")
    # Varsayılan modeli global değişkene ata
    model_name = DEFAULT_MODEL_NAME # Bu da config'den okunabilir ileride
    try:
        # Modeli oluşturmayı dene
        model = genai.GenerativeModel(model_name)
        logger.info(f"Global varsayılan Gemini modeli ('{model_name}') başarıyla oluşturuldu.")
    except Exception as model_error:
        logger.critical(f"HATA: Varsayılan Gemini modeli ('{model_name}') oluşturulamadı: {model_error}")
        model = None
        exit(1) # Model olmadan .ask komutu çalışmaz

except Exception as configure_error:
    logger.critical(f"HATA: Gemini API genel yapılandırma hatası: {configure_error}")
    model = None
    exit(1) # API yapılandırılamazsa bot başlamasın

# Model oluşturulamadıysa son kontrol (Gerçi yukarıda exit() var)
if model is None:
     logger.critical("HATA: Global Gemini modeli oluşturulamadı. Bot başlatılamıyor.")
     exit(1)


# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True # Mesaj içeriğini okumak için
intents.members = True         # Üye bilgilerini almak için (kanal izinleri vb.)
intents.messages = True        # Mesaj olayları için
intents.guilds = True          # Sunucu bilgilerini almak için
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Global Durum Yönetimi (Veritabanı sonrası) ---
# active_gemini_chats, temporary_chat_channels, user_to_channel_map, channel_last_active, user_next_model, warned_inactive_channels - yukarıda tanımlandı

# --- Yardımcı Fonksiyonlar ---

async def create_private_chat_channel(guild, author):
    """Verilen kullanıcı için özel sohbet kanalı oluşturur ve kanal nesnesini döndürür."""
    if not guild.me.guild_permissions.manage_channels:
        logger.warning(f"'{guild.name}' sunucusunda 'Kanalları Yönet' izni eksik. Kanal oluşturulamıyor.")
        return None

    # Kullanıcı adından güvenli bir kanal adı türet
    safe_username = "".join(c for c in author.display_name if c.isalnum() or c in [' ', '-']).strip().replace(' ', '-').lower()
    if not safe_username: safe_username = f"kullanici-{author.id}" # İsim tamamen geçersizse ID kullan
    safe_username = safe_username[:80] # Kanal adı uzunluk sınırına dikkat et

    base_channel_name = f"sohbet-{safe_username}"
    channel_name = base_channel_name
    counter = 1
    # Büyük/küçük harf duyarsız kontrol için mevcut kanal adlarını küçük harfe çevir
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}

    # Benzersiz ve geçerli uzunlukta bir kanal adı bul
    while channel_name.lower() in existing_channel_names:
        potential_name = f"{base_channel_name}-{counter}"
        # Kanal adı Discord'da max 100 karakter olabilir
        if len(potential_name) > 100:
             # Eğer sayaçla bile aşıyorsa, base ismi kırpmak gerekebilir (pek olası değil ama önlem)
             allowed_base_len = 100 - len(str(counter)) - 1 # -1 tire için
             potential_name = f"{base_channel_name[:allowed_base_len]}-{counter}"

        channel_name = potential_name
        counter += 1
        if counter > 1000: # Sonsuz döngü koruması
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı). Rastgele ID kullanılıyor.")
            channel_name = f"sohbet-{author.id}-{datetime.datetime.now().strftime('%M%S')}"[:100]
            if channel_name.lower() in existing_channel_names:
                logger.error(f"Alternatif rastgele kanal adı '{channel_name}' de mevcut. Oluşturma başarısız.")
                return None # Son çare de başarısız
            logger.warning(f"Alternatif rastgele kanal adı kullanılıyor: {channel_name}")
            break

    logger.info(f"'{guild.name}' sunucusunda '{author.name}' için oluşturulacak kanal adı: {channel_name}")

    # İzinleri ayarla: @everyone göremez, sadece yazar ve bot görür/yazar
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, attach_files=True # Botun kendi mesajlarını yönetebilmesi ve dosya ekleyebilmesi için
        )
    }

    try:
        new_channel = await guild.create_text_channel(
            channel_name,
            overwrites=overwrites,
            reason=f"{author.name} için otomatik Gemini sohbet kanalı."
            # Kategoriye eklemek isterseniz: category=guild.get_channel(kategori_id)
        )
        logger.info(f"Kullanıcı {author.name} ({author.id}) için '{channel_name}' (ID: {new_channel.id}) kanalı başarıyla oluşturuldu.")
        return new_channel
    except discord.errors.Forbidden:
         logger.error(f"'{guild.name}' sunucusunda kanal oluşturma izni reddedildi.")
         # Kullanıcıya DM ile bilgi verilebilir (izin varsa)
         # await author.send("Üzgünüm, bu sunucuda özel sohbet kanalı oluşturma iznim yok.")
         return None
    except Exception as e:
        logger.error(f"'{channel_name}' kanalı oluşturulurken beklenmedik hata: {e}\n{traceback.format_exc()}")
        return None

async def send_to_gemini_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    """Belirtilen kanalda Gemini'ye mesaj gönderir ve yanıtlar. Sohbet oturumunu yönetir."""
    global channel_last_active, active_gemini_chats
    if not prompt_text.strip():
        logger.warning(f"Boş prompt gönderilmeye çalışıldı (Kanal: {channel_id}). Yoksayılıyor.")
        return False # Boş mesaj gönderme

    # Aktif sohbet oturumu yoksa veya model değişmişse yeniden başlat
    current_chat_data = active_gemini_chats.get(channel_id)
    # Kanal için kayıtlı modeli DB'den al (her seferinde almak yerine on_ready'de state'e yüklenebilir?)
    # Şimdilik her ihtimale karşı DB'den alalım, belki model değişmiştir.
    db_model_name = DEFAULT_MODEL_NAME.split('/')[-1] # Varsayılan
    conn_local = None
    try:
        conn_local = db_connect()
        cursor = conn_local.cursor(cursor_factory=DictCursor)
        cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = %s", (channel_id,))
        result = cursor.fetchone()
        cursor.close()
        if result and result['model_name']:
            db_model_name = result['model_name']
    except Exception as db_err:
         logger.error(f"Sohbet için model adı DB'den okunamadı (Kanal: {channel_id}): {db_err}")
    finally:
         if conn_local: conn_local.close()

    model_full_name = f"models/{db_model_name}"

    # Oturumu başlat/yeniden başlat
    if not current_chat_data or current_chat_data.get('model') != db_model_name:
        try:
            logger.info(f"'{channel.name}' (ID: {channel_id}) için Gemini sohbet oturumu '{model_full_name}' ile başlatılıyor/yeniden başlatılıyor.")
            # Yeni model örneği oluştur
            chat_model_instance = genai.GenerativeModel(model_full_name)
            # Oturumu başlat (boş geçmişle)
            chat_session = chat_model_instance.start_chat(history=[])
            active_gemini_chats[channel_id] = {'session': chat_session, 'model': db_model_name}
            current_chat_data = active_gemini_chats[channel_id] # Yeni veriyi al
        except Exception as e:
            logger.error(f"'{channel.name}' için Gemini sohbet oturumu başlatılamadı ({model_full_name}): {e}")
            try:
                await channel.send(f"❌ Yapay zeka oturumu (`{db_model_name}`) başlatılamadı. Model geçerli olmayabilir veya bir API hatası oluştu.", delete_after=20)
            except discord.errors.NotFound: pass # Kanal bu arada silinmiş olabilir
            except Exception as send_err: logger.warning(f"Oturum başlatma hatası mesajı gönderilemedi: {send_err}")
            # Oturum yoksa, eski oturumu temizle (varsa)
            active_gemini_chats.pop(channel_id, None)
            return False

    # Sohbet verilerini al
    chat_session = current_chat_data['session']
    current_model_short_name = current_chat_data['model']

    logger.info(f"[GEMINI CHAT/{current_model_short_name}] [{author.name} @ {channel.name}] ->: {prompt_text[:150]}{'...' if len(prompt_text)>150 else ''}")

    # Mesajı göndermeden önce "typing" göstergesi gönder
    async with channel.typing():
        gemini_response_text = "" # Yanıtı saklamak için
        try:
            # Mesajı Gemini'ye gönder (async)
            response = await chat_session.send_message_async(prompt_text)

            # Yanıtı al (response.text)
            gemini_response_text = response.text.strip()

            # Son aktivite zamanını güncelle (hem state hem DB)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[channel_id] = now_utc
            update_channel_activity_db(channel_id, now_utc) # DB'yi de güncelle

            # Yanıtı işle ve gönder
            if not gemini_response_text:
                logger.warning(f"[GEMINI CHAT/{current_model_short_name}] <- Boş yanıt (Kanal: {channel_id}).")
                # Kullanıcıya bildirim gönderilebilir: await channel.send("Yapay zeka boş bir yanıt döndürdü.", delete_after=10)
            else:
                logger.info(f"[GEMINI CHAT/{current_model_short_name}] <- Yanıt ({len(gemini_response_text)} kr) (Kanal: {channel_id}): {gemini_response_text[:150]}{'...' if len(gemini_response_text)>150 else ''}")
                # Discord karakter limitini (2000) kontrol et ve gerekirse parçala
                if len(gemini_response_text) > 2000:
                    logger.info(f"Yanıt 2000 karakterden uzun (Kanal: {channel_id}), parçalanıyor...")
                    for i in range(0, len(gemini_response_text), 2000):
                        await channel.send(gemini_response_text[i:i+2000])
                else:
                    await channel.send(gemini_response_text)
            return True # Başarılı

        except Exception as e:
            # API veya diğer hataları yakala
            logger.error(f"[GEMINI CHAT/{current_model_short_name}] API/İşlem hatası (Kanal: {channel_id}): {e}")
            error_str = str(e).lower()
            user_msg = "Yapay zeka ile konuşurken bir sorun oluştu."

            # Daha spesifik hata mesajları (Gemini özelinde)
            # Güvenlik filtrelemesi kontrolü
            finish_reason = ""
            try:
                # response objesi varsa ve prompt_feedback varsa oradan kontrol et
                if 'response' in locals() and response.prompt_feedback:
                     finish_reason = str(response.prompt_feedback).upper()
                # Veya candidates listesindeki ilk adayın finish_reason'una bak
                elif 'response' in locals() and response.candidates:
                     finish_reason = str(response.candidates[0].finish_reason).upper()
            except Exception: pass # Hata olursa görmezden gel

            if "finish_reason: SAFETY" in finish_reason:
                 user_msg = "⚠️ Üzgünüm, yanıtım güvenlik filtrelerine takıldı. Farklı bir şekilde sormayı deneyebilir misin?"
            elif "api key not valid" in error_str or "permission_denied" in error_str or "403" in error_str:
                user_msg = "❌ API Anahtarı sorunu veya yetki reddi. Lütfen bot yöneticisi ile iletişime geçin."
            elif "quota" in error_str or "resource_exhausted" in error_str or "429" in error_str:
                user_msg = "⏳ API kullanım limiti aşıldı. Lütfen daha sonra tekrar deneyin."
            elif "400" in error_str or "invalid argument" in error_str:
                user_msg = "🤔 Geçersiz bir istek gönderildi. Sorunuzu veya komutunuzu kontrol edin."
            elif "500" in error_str or "internal error" in error_str:
                 user_msg = "⚙️ Yapay zeka sunucusunda geçici bir sorun oluştu. Lütfen biraz sonra tekrar deneyin."
            elif isinstance(e, asyncio.TimeoutError):
                 user_msg = "⏳ İstek zaman aşımına uğradı. Lütfen tekrar deneyin."

            try: await channel.send(user_msg, delete_after=20)
            except discord.errors.NotFound: pass # Kanal bu arada silinmiş olabilir
            except Exception as send_err: logger.warning(f"API hatası mesajı gönderilemedi: {send_err}")
            return False # Başarısız
        except discord.errors.HTTPException as discord_e:
            # Discord'a gönderirken hata (örn. rate limit, çok uzun mesaj - gerçi böldük ama)
            logger.error(f"Discord'a mesaj gönderilemedi (Kanal: {channel_id}): {discord_e}")
            # Bu durumda Gemini yanıtı gelmiş olabilir ama gönderilememiştir.
            return False # Gönderme başarısız

# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'----- {bot.user} olarak giriş yapıldı -----')
    logger.info(f'Bot ID: {bot.user.id}')
    logger.info(f'{len(bot.guilds)} sunucuda aktif.')

    # Ayarları DB'den tekrar yükle (en güncel halleri için)
    loaded_entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
    loaded_inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', DEFAULT_INACTIVITY_TIMEOUT_HOURS)

    # Entry Channel ID'yi ayarla
    entry_channel_id = None # Önce sıfırla
    if loaded_entry_channel_id_str:
        try: entry_channel_id = int(loaded_entry_channel_id_str)
        except (ValueError, TypeError): logger.error(f"DB'den yüklenen giriş kanalı ID'si geçersiz: '{loaded_entry_channel_id_str}'.")
    if not entry_channel_id: # DB'den yüklenemezse veya geçersizse env'e bak
         if DEFAULT_ENTRY_CHANNEL_ID:
              try: entry_channel_id = int(DEFAULT_ENTRY_CHANNEL_ID)
              except (ValueError, TypeError): logger.error(f"Varsayılan giriş kanalı ID'si de geçersiz: '{DEFAULT_ENTRY_CHANNEL_ID}'.")

    # Inactivity Timeout'u ayarla
    inactivity_timeout = None # Önce sıfırla
    try: inactivity_timeout = datetime.timedelta(hours=float(loaded_inactivity_timeout_hours_str))
    except (ValueError, TypeError): logger.error(f"DB/Varsayılan İnaktivite süresi ('{loaded_inactivity_timeout_hours_str}') geçersiz!")
    if not inactivity_timeout: # Yüklenemezse veya <= 0 ise varsayılan 1 saat
        inactivity_timeout = datetime.timedelta(hours=1)
        logger.warning(f"Geçerli İnaktivite süresi ayarlanamadı, varsayılan {inactivity_timeout} kullanılıyor.")

    logger.info(f"Ayarlar yüklendi - Giriş Kanalı ID: {entry_channel_id if entry_channel_id else 'Ayarlanmadı'}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")

    # Kalıcı verileri (aktif sohbetler) DB'den yükle ve state'i ayarla
    logger.info("Aktif geçici sohbet kanalları DB'den yükleniyor...")
    temporary_chat_channels.clear()
    user_to_channel_map.clear()
    channel_last_active.clear()
    active_gemini_chats.clear() # Başlangıçta sohbet oturumlarını temizle, ilk mesajda oluşturulacak
    warned_inactive_channels.clear()

    loaded_channels = load_all_temp_channels()
    valid_channel_count = 0
    invalid_channel_ids_in_db = []

    for ch_id, u_id, last_active_ts, ch_model_name in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel):
            # Kanal Discord'da hala varsa, state'e ekle
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            channel_last_active[ch_id] = last_active_ts
            # Model adını da saklayabiliriz ama send_to_gemini içinde zaten DB'den okunuyor.
            # active_gemini_chats[ch_id] = {'model': ch_model_name} # Sadece model adını saklamak yeterli olabilir
            valid_channel_count += 1
            logger.debug(f"Aktif kanal yüklendi: ID {ch_id}, Sahip {u_id}, Son Aktif {last_active_ts}, Model {ch_model_name}")
        else:
            # Kanal Discord'da yoksa veya türü yanlışsa, DB'den silinmeli
            logger.warning(f"DB'deki geçici kanal {ch_id} Discord'da bulunamadı/geçersiz. DB'den silinmek üzere işaretlendi.")
            invalid_channel_ids_in_db.append(ch_id)

    # Geçersiz kanalları DB'den temizle
    if invalid_channel_ids_in_db:
        logger.info(f"{len(invalid_channel_ids_in_db)} geçersiz kanal DB'den temizleniyor...")
        for invalid_id in invalid_channel_ids_in_db:
            remove_temp_channel_db(invalid_id)

    logger.info(f"{valid_channel_count} aktif geçici kanal DB'den başarıyla yüklendi ve state ayarlandı.")

    # Bot aktivitesini ayarla
    entry_channel_name = "Ayarlanmadı"
    if entry_channel_id:
        try:
            entry_channel = await bot.fetch_channel(entry_channel_id) # fetch_channel kullanmak daha garanti
            if entry_channel: entry_channel_name = f"#{entry_channel.name}"
            else: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı.")
        except discord.errors.NotFound:
            logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı.")
        except discord.errors.Forbidden:
            logger.warning(f"Giriş Kanalına (ID: {entry_channel_id}) erişim izni yok.")
        except Exception as e:
            logger.warning(f"Giriş kanalı alınırken hata: {e}")

    try:
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için {entry_channel_name}"))
        logger.info(f"Bot aktivitesi ayarlandı: 'Oynuyor: Sohbet için {entry_channel_name}'")
    except Exception as e:
        logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    # İnaktivite kontrol görevini başlat (eğer çalışmıyorsa)
    if not check_inactivity.is_running():
        try:
            check_inactivity.start()
            logger.info("İnaktivite kontrol görevi başarıyla başlatıldı.")
        except Exception as task_e:
             logger.error(f"İnaktivite kontrol görevi başlatılamadı: {task_e}")

    logger.info("----- Bot tamamen hazır ve komutları dinliyor -----")
    print("-" * 50) # Konsola ayırıcı çizgi

@bot.event
async def on_message(message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    # Temel kontroller
    if message.author == bot.user or message.author.bot: return # Botun kendi veya diğer botların mesajlarını yoksay
    if not message.guild: return # Sadece sunucu mesajlarını işle (DM değil)

    author = message.author
    author_id = author.id
    channel = message.channel
    channel_id = channel.id
    guild = message.guild

    # Mesajın bir komut olup olmadığını kontrol et ve işle
    # process_commands() çağrısı komutları tetikler ve eğer komut bulunursa ilgili fonksiyona yönlendirir.
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    # Eğer mesaj geçerli bir komutsa, bu fonksiyondan çık (komut handler'ı devralır)
    if ctx.valid:
        return

    # --- Otomatik Kanal Oluşturma Mantığı (Sadece Ayarlanan Giriş Kanalında) ---
    if entry_channel_id and channel_id == entry_channel_id:
        # Kullanıcının zaten aktif bir sohbet kanalı var mı?
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]
            active_channel = bot.get_channel(active_channel_id)
            mention = f"<#{active_channel_id}>" if active_channel else f"silinmiş kanal (ID: {active_channel_id})"
            logger.info(f"{author.name} giriş kanalına yazdı ama aktif kanalı var: {mention}")
            try:
                # Kullanıcıyı bilgilendir ve mesajını sil
                info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15)
                await asyncio.sleep(0.5) # Mesajın görünmesi için kısa bekleme
                await message.delete()
            except discord.errors.NotFound: pass # Mesajlar zaten silinmiş olabilir
            except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
            return

        # Yeni kanal oluşturma süreci
        initial_prompt = message.content
        original_message_id = message.id

        # Boş mesajları yoksay
        if not initial_prompt.strip():
             logger.info(f"{author.name} ({author_id}) giriş kanalına boş mesaj gönderdi, yoksayılıyor.")
             try: await message.delete()
             except discord.errors.NotFound: pass
             except Exception as del_e: logger.warning(f"Giriş kanalındaki boş mesaj ({original_message_id}) silinemedi: {del_e}")
             return

        # Kullanıcının bir sonraki sohbet için seçtiği modeli al (varsa), yoksa varsayılanı kullan
        default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
        chosen_model_name_short = user_next_model.pop(author_id, default_model_short)
        chosen_model_name_full = f"models/{chosen_model_name_short}"

        logger.info(f"{author.name} giriş kanalına yazdı. '{chosen_model_name_full}' modeli ile özel kanal oluşturuluyor...")
        # Kanalı oluşturmayı dene
        new_channel = await create_private_chat_channel(guild, author)

        if new_channel:
            new_channel_id = new_channel.id
            # Kanal başarıyla oluşturulduysa: State'i ve DB'yi güncelle
            temporary_chat_channels.add(new_channel_id)
            user_to_channel_map[author_id] = new_channel_id
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[new_channel_id] = now_utc
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_name_short) # DB'ye ekle

            # Hoşgeldin Mesajı (Embed ile)
            try:
                embed = discord.Embed(title="👋 Özel Gemini Sohbetin Başlatıldı!",
                                      description=(f"Merhaba {author.mention}!\n\n"
                                                   f"Bu kanalda **`{chosen_model_name_short}`** modeli ile sohbet edeceksin."),
                                      color=discord.Color.og_blurple()) # Discord'un mavi rengi
                embed.set_thumbnail(url=bot.user.display_avatar.url)
                timeout_hours_display = f"{inactivity_timeout.total_seconds() / 3600:.1f}".rstrip('0').rstrip('.') # 1.0 -> 1, 1.5 -> 1.5
                embed.add_field(name="⏳ Otomatik Kapanma", value=f"Kanal `{timeout_hours_display}` saat boyunca işlem görmezse otomatik olarak silinir.", inline=False)
                prefix = bot.command_prefix[0] # İlk prefix'i alalım
                embed.add_field(name="🛑 Kapat", value=f"`{prefix}endchat`", inline=True)
                embed.add_field(name="🔄 Model Seç (Sonraki)", value=f"`{prefix}setmodel <ad>`", inline=True)
                embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{prefix}resetchat`", inline=True)
                embed.set_footer(text="Sorularını doğrudan bu kanala yazabilirsin.")
                await new_channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                # Fallback düz metin mesajı
                try:
                     await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.")
                except Exception as fallback_e:
                     logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")

            # Kullanıcıya giriş kanalında bilgi ver
            try:
                await channel.send(f"{author.mention}, özel sohbet kanalın {new_channel.mention} oluşturuldu! Seni oraya yönlendiriyorum...", delete_after=20)
            except Exception as e:
                logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            # Kullanıcının yazdığı ilk mesajı yeni kanala kopyala (Yeni eklenen kısım)
            try:
                # Mesajı "KullanıcıAdı: Mesaj" formatında gönder
                await new_channel.send(f"**{author.display_name}:** {initial_prompt}")
                logger.info(f"Kullanıcının ilk mesajı ({original_message_id}) yeni kanala ({new_channel_id}) kopyalandı.")
            except discord.errors.HTTPException as send_error:
                 logger.error(f"İlk mesaj yeni kanala gönderilemedi (Muhtemelen çok uzun >2000kr): {send_error}")
                 # Çok uzunsa belki sadece bir kısmını gönderebiliriz veya hata mesajı verebiliriz
                 try: await new_channel.send(f"*{author.mention}, ilk mesajın çok uzun olduğu için tam olarak kopyalanamadı.*")
                 except: pass
            except Exception as e:
                logger.error(f"Kullanıcının ilk mesajı yeni kanala kopyalanamadı: {e}")

            # Şimdi ilk mesajı Gemini'ye gönderip yanıtını alalım
            try:
                logger.info(f"-----> GEMINI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_name_full})")
                # Hata durumunu kontrol etmek için success değişkenini kullan
                success = await send_to_gemini_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success:
                    logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else:
                    # send_to_gemini_and_respond içinde hata mesajı gönderildiği için burada tekrar göndermeye gerek yok.
                    logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e:
                # Bu genellikle send_to_gemini_and_respond çağrısında beklenmedik bir hata olursa tetiklenir
                logger.error(f"İlk Gemini isteği gönderilirken/işlenirken genel hata: {e}")
                try: await new_channel.send("İlk sorunuz işlenirken bir hata oluştu.")
                except: pass

            # Orijinal mesajı giriş kanalından sil
            try:
                # fetch_message daha güvenilir olabilir
                msg_to_delete = await channel.fetch_message(original_message_id)
                await msg_to_delete.delete()
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except discord.errors.NotFound:
                logger.warning(f"Giriş kanalındaki mesaj {original_message_id} silinemeden önce kayboldu.")
            except discord.errors.Forbidden:
                 logger.warning(f"Giriş kanalındaki mesajı ({original_message_id}) silme izni yok.")
            except Exception as e:
                logger.warning(f"Giriş kanalındaki mesaj {original_message_id} silinemedi: {e}")
        else:
            # Kanal oluşturulamadıysa (izin yoksa veya başka bir hata olduysa)
            try:
                await channel.send(f"{author.mention}, üzgünüm, senin için özel kanal oluşturulamadı. Sunucu yöneticisi izinleri kontrol etmeli veya bir sorun olabilir.", delete_after=20)
                await asyncio.sleep(0.5)
                await message.delete() # Başarısız olan mesajı da silmeyi dene
            except discord.errors.NotFound: pass
            except Exception as e:
                logger.warning(f"Giriş kanalına kanal oluşturma hata mesajı gönderilemedi/mesaj silinemedi: {e}")
        return # Giriş kanalı işlemi bitti

    # --- Geçici Sohbet Kanallarındaki Normal Mesajlar ---
    # Mesaj bir komut değilse VE geçici sohbet kanallarından birindeyse
    if channel_id in temporary_chat_channels and not ctx.valid:
        # Bu kanaldaki mesajları Gemini'ye gönder
        prompt_text = message.content
        await send_to_gemini_and_respond(channel, author, prompt_text, channel_id)

# --- Arka Plan Görevi: İnaktivite Kontrolü ---
@tasks.loop(minutes=5) # Kontrol sıklığı (5 dakika)
async def check_inactivity():
    """Aktif olmayan geçici kanalları kontrol eder, uyarır ve siler."""
    global warned_inactive_channels
    if inactivity_timeout is None:
        logger.debug("İnaktivite kontrolü: Zaman aşımı ayarlı değil, görev atlanıyor.")
        return # Zaman aşımı ayarlı değilse çık

    now = datetime.datetime.now(datetime.timezone.utc) # Mevcut zaman (UTC)
    channels_to_delete = []
    channels_to_warn = []
    # Silmeden ne kadar süre önce uyarılacak (örn: 10 dakika)
    warning_delta = datetime.timedelta(minutes=10)
    # Uyarının geçerli olması için zaman aşımının uyarı süresinden uzun olması gerekir
    if inactivity_timeout > warning_delta:
        warning_threshold = inactivity_timeout - warning_delta
    else:
        warning_threshold = None # Çok kısa zaman aşımlarında uyarı vermeyelim
        logger.debug("Zaman aşımı süresi uyarı eşiğinden kısa, inaktivite uyarısı devre dışı.")

    logger.debug(f"İnaktivite kontrolü çalışıyor... Zaman: {now.isoformat()}")

    # Kopya üzerinde iterasyon yap (sözlük iterasyon sırasında değişebilir)
    active_channels_copy = list(channel_last_active.items())

    for channel_id, last_active_time in active_channels_copy:
        # last_active_time'ın timezone bilgisi olduğundan emin ol (DB'den doğru gelmeli)
        if last_active_time.tzinfo is None:
            logger.warning(f"Kanal {channel_id} için zaman damgasında TZ bilgisi eksik! UTC varsayılıyor.")
            last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)

        time_inactive = now - last_active_time
        logger.debug(f"Kanal {channel_id}: İnaktif süre {time_inactive}, Son aktif {last_active_time.isoformat()}")

        # Silme zamanı geldi mi?
        if time_inactive > inactivity_timeout:
            channels_to_delete.append(channel_id)
            logger.debug(f"Kanal {channel_id} silinmek üzere işaretlendi (İnaktif: {time_inactive} > {inactivity_timeout})")
        # Uyarı zamanı geldi mi, uyarı eşiği tanımlı mı ve daha önce uyarılmadı mı?
        elif warning_threshold and time_inactive > warning_threshold and channel_id not in warned_inactive_channels:
            channels_to_warn.append(channel_id)
            logger.debug(f"Kanal {channel_id} uyarılmak üzere işaretlendi (İnaktif: {time_inactive} > {warning_threshold})")

    # Uyarıları gönder
    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                # Kalan süreyi hesapla (yaklaşık)
                remaining_time = inactivity_timeout - time_inactive # time_inactive zaten hesaplanmıştı
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir. Konuşmaya devam ederek silinmesini engelleyebilirsiniz.", delete_after=300) # Uyarı 5 dk görünsün
                warned_inactive_channels.add(channel_id) # Uyarıldı olarak işaretle
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id} (Kalan süre: ~{remaining_minutes} dk)")
            except discord.errors.NotFound:
                logger.warning(f"Uyarı gönderilecek kanal {channel_id} bulunamadı.")
                warned_inactive_channels.discard(channel_id) # Bulunamadıysa uyarı setinden çıkar
            except discord.errors.Forbidden:
                 logger.warning(f"Kanal {channel_id}'a uyarı mesajı gönderme izni yok.")
                 # İzin yoksa uyarıldı olarak işaretleyemeyiz, tekrar denemesin diye discard edebiliriz?
                 # Şimdilik sadece loglayalım, belki izinler düzelir.
            except Exception as e:
                logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else:
            # Kanal Discord'da yoksa, uyarı listesinden çıkar (silme listesine zaten girecek veya girdi)
            logger.warning(f"Uyarı gönderilecek kanal objesi {channel_id} alınamadı.")
            warned_inactive_channels.discard(channel_id)

    # Silinecek kanalları işle
    if channels_to_delete:
        logger.info(f"İnaktivite nedeniyle {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id)
            if channel_to_delete:
                logger.info(f"'{channel_to_delete.name}' (ID: {channel_id}) kanalı siliniyor...")
                try:
                    await channel_to_delete.delete(reason="İnaktivite nedeniyle otomatik silindi.")
                    logger.info(f"İnaktif kanal '{channel_to_delete.name}' (ID: {channel_id}) başarıyla silindi.")
                    # State temizliği on_guild_channel_delete olayı tarafından yapılacak.
                except discord.errors.NotFound:
                    logger.warning(f"İnaktif kanal (ID: {channel_id}) silinirken bulunamadı (muhtemelen başka bir işlemle silindi). State manuel temizleniyor.")
                    # Kanal zaten yoksa state'i manuel temizle (on_guild_channel_delete tetiklenmeyebilir)
                    temporary_chat_channels.discard(channel_id)
                    active_gemini_chats.pop(channel_id, None)
                    channel_last_active.pop(channel_id, None)
                    warned_inactive_channels.discard(channel_id)
                    user_id_to_remove = None
                    for user_id, ch_id in list(user_to_channel_map.items()):
                        if ch_id == channel_id: user_id_to_remove = user_id; break
                    if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                    remove_temp_channel_db(channel_id) # DB'den de sil
                except discord.errors.Forbidden:
                     logger.error(f"İnaktif kanal (ID: {channel_id}) silinirken 'Forbidden' hatası alındı. İzinleri kontrol edin.")
                     # İzin yoksa state'i ve DB'yi temizleyelim ki tekrar denemesin
                     remove_temp_channel_db(channel_id)
                     temporary_chat_channels.discard(channel_id)
                     active_gemini_chats.pop(channel_id, None)
                     channel_last_active.pop(channel_id, None)
                     warned_inactive_channels.discard(channel_id)
                     # user_map temizlemesi burada da yapılabilir
                except Exception as e:
                    logger.error(f"İnaktif kanal (ID: {channel_id}) silinirken beklenmedik hata: {e}")
                    # Hata olsa bile DB'den silmeyi dene (sorun tekrarlamasın)
                    remove_temp_channel_db(channel_id)
            else:
                logger.warning(f"İnaktif kanal (ID: {channel_id}) Discord'da bulunamadı. DB'den ve state'den siliniyor.")
                # Kanal Discord'da yoksa state'i ve DB'yi temizle
                remove_temp_channel_db(channel_id) # DB'den sil
                temporary_chat_channels.discard(channel_id)
                active_gemini_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                warned_inactive_channels.discard(channel_id)
                user_id_to_remove = None
                for user_id, ch_id in list(user_to_channel_map.items()):
                    if ch_id == channel_id: user_id_to_remove = user_id; break
                if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)

            # Başarılı veya başarısız, her durumda uyarı setinden çıkar (artık relevant değil)
            warned_inactive_channels.discard(channel_id)
    else:
         logger.debug("Silinecek inaktif kanal bulunmadı.")


@check_inactivity.before_loop
async def before_check_inactivity():
    logger.info("İnaktivite kontrol döngüsü başlamadan önce botun hazır olması bekleniyor...")
    await bot.wait_until_ready()
    logger.info("Bot hazır, inaktivite kontrol döngüsü başlıyor.")

# --- Komutlar ---

@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx):
    """Mevcut geçici Gemini sohbet kanalını manuel olarak siler."""
    channel_id = ctx.channel.id
    author_id = ctx.author.id

    # Kanal geçici kanal listesinde mi?
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    # Kanalın sahibini bul (state'den)
    expected_user_id = None
    for user_id, ch_id in user_to_channel_map.items():
        if ch_id == channel_id:
            expected_user_id = user_id
            break

    # Sahibi kontrol et
    # expected_user_id None ise bir tutarsızlık var ama yine de silmeye izin verebiliriz?
    # Şimdilik sahibi olmayan kanalı da silelim (belki DB'de var state'de yoktu)
    if expected_user_id and author_id != expected_user_id:
        await ctx.send("❌ Bu kanalı sadece oluşturan kişi (`<@"+str(expected_user_id)+">`) kapatabilir.", delete_after=10)
        try:
            await ctx.message.delete(delay=10)
        except discord.errors.NotFound: pass
        except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .endchat komut mesajını (yetkisiz kullanım) silme izni yok.")
        except Exception as delete_e: logger.error(f".endchat komut mesajı (yetkisiz kullanım) silinirken hata: {delete_e}")
        return
    elif not expected_user_id:
         logger.warning(f".endchat: Kanal {channel_id} geçici listesinde ama sahibi user_map'te bulunamadı. Yine de silme deneniyor.")

    # Botun kanalı silme izni var mı?
    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send("Kanalları yönetme iznim yok.", delete_after=10)
        return

    # Kanalı sil
    try:
        channel_name = ctx.channel.name # Log için ismi al
        logger.info(f"Kanal '{channel_name}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
        # State temizliği on_guild_channel_delete tarafından yapılacak.
        # Kullanıcıya DM ile bildirim gönderilebilir (isteğe bağlı)
        # try: await ctx.author.send(f"'{channel_name}' adlı sohbet kanalınız kapatıldı.")
        # except: pass
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı. State manuel temizleniyor.")
        # Kanal zaten yoksa state'i manuel temizle
        temporary_chat_channels.discard(channel_id)
        active_gemini_chats.pop(channel_id, None)
        channel_last_active.pop(channel_id, None)
        warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id: user_id_to_remove = user_id; break
        if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
        remove_temp_channel_db(channel_id) # DB'den de sil
    except discord.errors.Forbidden:
         logger.error(f"Kanal {channel_id} manuel silinirken 'Forbidden' hatası. İzinleri kontrol edin.")
         await ctx.send("❌ Kanalı silme iznim yok.", delete_after=10)
    except Exception as e:
        logger.error(f".endchat komutunda kanal silinirken hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Kanal silinirken bir hata oluştu.", delete_after=10)

@bot.event
async def on_guild_channel_delete(channel):
    """Bir kanal silindiğinde tetiklenir (state temizliği için)."""
    channel_id = channel.id
    # Sadece bizim yönettiğimiz geçici kanallarla ilgilen
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici sohbet kanalı '{getattr(channel, 'name', 'Bilinmiyor')}' (ID: {channel_id}) silindi (veya bot erişemiyor), ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id)
        active_gemini_chats.pop(channel_id, None) # Aktif sohbeti sonlandır
        channel_last_active.pop(channel_id, None) # Aktivite takibini bırak
        warned_inactive_channels.discard(channel_id) # Uyarıldıysa listeden çıkar

        # Kullanıcı haritasını temizle
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id:
                user_id_to_remove = user_id
                break # Bir kanala sadece bir kullanıcı atanır
        if user_id_to_remove:
            user_to_channel_map.pop(user_id_to_remove, None)
            logger.info(f"Silinen kanal ({channel_id}) için kullanıcı {user_id_to_remove} haritası temizlendi.")
        else:
             logger.warning(f"Silinen kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")

        # Veritabanından sil (bu kanalın tekrar yüklenmemesi için)
        remove_temp_channel_db(channel_id)

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True) # Sadece mesajları yönetebilenler
@commands.bot_has_permissions(manage_messages=True) # Botun da izni olmalı
async def clear_messages(ctx, amount: str = None):
    """Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç)."""
    # İzinler decorator'lar ile kontrol ediliyor.

    if amount is None:
        await ctx.send(f"Silinecek mesaj sayısı (`{ctx.prefix}clear 5`) veya tümü için `{ctx.prefix}clear all` yazın.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    deleted_count = 0
    skipped_pinned = 0

    # Sabitlenmemiş mesajları kontrol eden fonksiyon
    def check_not_pinned(m):
        nonlocal skipped_pinned
        if m.pinned:
            skipped_pinned += 1
            return False
        return True

    try:
        # Komut mesajını öncelikle silmeyi dene (başarısız olursa önemli değil)
        try: await ctx.message.delete()
        except: pass

        if amount.lower() == 'all':
            await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)...", delete_after=5)
            # purge() 14 günden eski mesajları toplu silemez.
            # Sadece son 14 gündeki sabitlenmemişleri siler.
            # Tüm geçmişi silmek için kanalı kopyalayıp eskisini silmek gerekir (daha karmaşık).
            while True:
                # bulk=True ile 14 gün sınırı vardır.
                deleted = await ctx.channel.purge(limit=100, check=check_not_pinned, bulk=True)
                deleted_count += len(deleted)
                if len(deleted) < 100: # 100'den az sildiyse muhtemelen son batch'ti
                    break
                await asyncio.sleep(1) # Rate limit'e takılmamak için küçük bir bekleme

            msg = f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi (son 14 gün)."
            if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
            await ctx.send(msg, delete_after=10)
        else:
            try:
                limit = int(amount)
                if limit <= 0: raise ValueError("Sayı pozitif olmalı.")
                # Kullanıcının komutu zaten silindiği için +1'e gerek yok.
                deleted = await ctx.channel.purge(limit=limit, check=check_not_pinned, bulk=True)
                actual_deleted_count = len(deleted)

                msg = f"{actual_deleted_count} mesaj silindi."
                if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
                await ctx.send(msg, delete_after=5)

            except ValueError:
                 await ctx.send(f"Geçersiz sayı '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10)
            except Exception as num_err: # Diğer sayısal hatalar
                 await ctx.send(f"Sayı işlenirken hata: {num_err}", delete_after=10)

    # İzin hataları decorator'lar tarafından yakalanıp on_command_error'a gönderilir.
    # except discord.errors.Forbidden: ... (Gerek yok)
    except Exception as e:
        logger.error(f".clear komutunda hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10)


@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user) # Kullanıcı başına 5 saniyede 1 kullanım
async def ask_in_channel(ctx, *, question: str = None):
    """Sorulan soruyu Gemini'ye iletir ve yanıtı geçici olarak bu kanalda gösterir."""
    global model # Global varsayılan modeli kullanır
    if question is None:
        error_msg = await ctx.reply(f"Lütfen soru sorun (örn: `{ctx.prefix}ask Gemini nedir?`).", delete_after=15, mention_author=False)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None # Yanıt mesajını saklamak için

    try:
        # Global modelin varlığını tekrar kontrol et (programın başında oluşturulmuş olmalı)
        if model is None:
            logger.error(".ask için global model yüklenmemiş/yok!")
            await ctx.reply("❌ Yapay zeka modeli şu anda kullanılamıyor.", delete_after=10, mention_author=False)
            try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            except: pass
            return

        async with ctx.typing():
            try:
                # Blocking API çağrısını ayrı thread'de çalıştır (Discord botunu bloklamamak için)
                response = await asyncio.to_thread(model.generate_content, question)
                gemini_response_text = response.text.strip()

            except Exception as gemini_e:
                 logger.error(f".ask için Gemini API hatası: {gemini_e}")
                 error_str = str(gemini_e).lower()
                 user_msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."
                 # Hata mesajlarını burada da özelleştirebiliriz (send_to_gemini_and_respond'daki gibi)
                 finish_reason = ""
                 try:
                     if 'response' in locals() and response.prompt_feedback: finish_reason = str(response.prompt_feedback).upper()
                     elif 'response' in locals() and response.candidates: finish_reason = str(response.candidates[0].finish_reason).upper()
                 except: pass
                 if "finish_reason: SAFETY" in finish_reason: user_msg = "⚠️ Yanıt güvenlik filtrelerine takıldı."
                 elif "quota" in error_str or "429" in error_str: user_msg = "⏳ API kullanım limiti aşıldı."
                 elif "api key" in error_str or "403" in error_str: user_msg = "❌ API Anahtarı sorunu."

                 await ctx.reply(user_msg, delete_after=15, mention_author=False)
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY) # Hata durumunda da kullanıcı mesajını sil
                 except: pass
                 return

        # Yanıt boş mu?
        if not gemini_response_text:
            logger.warning(f"Gemini'den .ask için boş yanıt alındı (Soru: {question[:50]}...).")
            await ctx.reply("🤔 Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15, mention_author=False)
            try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            except: pass
            return

        # Yanıtı Embed içinde göster
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None)

        # Soru ve yanıtı embed alanlarına sığdır (1024 karakter sınırı)
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)

        response_display = gemini_response_text if len(gemini_response_text) <= 1024 else gemini_response_text[:1021] + "..."
        embed.add_field(name="Yanıt", value=response_display, inline=False) # Başlık " yanıt" yerine "Yanıt"

        footer_text = f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik olarak silinecektir."
        embed.set_footer(text=footer_text)

        # Yanıtı gönder ve mesaj nesnesini sakla
        bot_response_message = await ctx.reply(embed=embed, mention_author=False) # Yanıt verirken ping atma
        logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")

        # Kullanıcının komut mesajını silmeyi zamanla
        try:
            await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            logger.info(f".ask komut mesajı ({ctx.message.id}) {MESSAGE_DELETE_DELAY}s sonra silinmek üzere zamanlandı.")
        except Exception as e:
            # Zaten silinmiş olabilir veya izin yoktur, çok önemli değil.
            logger.warning(f".ask komut mesajı ({ctx.message.id}) silme zamanlanamadı/başarısız: {e}")

        # Botun yanıt mesajını silmeyi zamanla
        try:
            await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY)
            logger.info(f".ask yanıt mesajı ({bot_response_message.id}) {MESSAGE_DELETE_DELAY}s sonra silinmek üzere zamanlandı.")
        except Exception as e:
            logger.warning(f".ask yanıt mesajı ({bot_response_message.id}) silme zamanlanamadı/başarısız: {e}")

    # Cooldown hatası on_command_error tarafından yakalanır.
    except Exception as e:
        logger.error(f".ask komutunda genel hata: {e}\n{traceback.format_exc()}")
        try:
             await ctx.reply("⚙️ Sorunuz işlenirken beklenmedik bir hata oluştu.", delete_after=15, mention_author=False)
             # Hata durumunda da kullanıcı mesajını silmeyi dene (zamanlanmamışsa)
             if not (ctx.message.flags.ephemeral or ctx.message.flags.is_crossposted): # Geçici mesaj değilse
                 await ctx.message.delete(delay=15) # Daha kısa gecikme
        except: pass


@bot.command(name='resetchat', aliases=['sıfırla'])
@commands.guild_only()
async def reset_chat_session(ctx):
    """Mevcut geçici sohbet kanalının Gemini konuşma geçmişini sıfırlar."""
    channel_id = ctx.channel.id

    # Sadece geçici kanallarda çalışır
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    # Aktif sohbet oturumunu sonlandır (varsa)
    if channel_id in active_gemini_chats:
        # Oturumu sözlükten kaldırarak geçmişi temizle
        removed_session = active_gemini_chats.pop(channel_id, None)
        if removed_session:
             logger.info(f"Sohbet geçmişi {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı. Kullanılan model: {removed_session.get('model', 'Bilinmiyor')}")
             await ctx.send("✅ Konuşma geçmişi sıfırlandı. Bir sonraki mesajınızla yeni bir oturum başlayacak.", delete_after=15)
        else: # Pop başarısız olduysa (çok nadir)
             logger.warning(f"Aktif oturum {channel_id} için pop işlemi başarısız oldu.")
             await ctx.send("Oturum sıfırlanırken bir sorun oluştu.", delete_after=10)
    else:
        # Zaten aktif oturum yoksa (örn. bot yeniden başlatıldı veya hiç konuşulmadı)
        logger.info(f"Sıfırlanacak aktif oturum yok (zaten temiz): Kanal {channel_id}")
        await ctx.send("ℹ️ Aktif bir konuşma geçmişi bulunmuyor (zaten sıfır).", delete_after=10)

    # Komut mesajını sil
    try: await ctx.message.delete(delay=15)
    except: pass


@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user) # Sık kullanımı engelle
async def list_available_models(ctx):
    """Sohbet için kullanılabilir (metin tabanlı) Gemini modellerini listeler."""
    try:
         models_list = []
         # Geri bildirim mesajı
         processing_msg = await ctx.send("🤖 Kullanılabilir modeller kontrol ediliyor...", delete_after=10)

         async with ctx.typing():
             # API çağrısını thread'de yap (blocking olabilir)
             available_models = await asyncio.to_thread(genai.list_models)

             for m in available_models:
                 # Sadece 'generateContent' destekleyen (sohbet/metin) modelleri alalım
                 # Vision, Embedding gibi diğer modelleri filtreleyelim
                 if 'generateContent' in m.supported_generation_methods:
                     model_id = m.name.split('/')[-1] # Sadece kısa adı göster: örn. gemini-1.5-flash-latest

                     # Desteklenmeyen veya eski modelleri filtreleyebiliriz (isteğe bağlı)
                     # if 'vision' in model_id or 'embed' in model_id or 'aqa' in model_id:
                     #      continue

                     # Önerilen modelleri işaretle
                     prefix = "▪️" # Varsayılan işaret
                     if "1.5-flash" in model_id: prefix = "⚡" # Flash için
                     elif "1.5-pro" in model_id: prefix = "✨" # Pro için (daha yeni)
                     elif "gemini-pro" == model_id and "1.5" not in model_id : prefix = "✅" # Eski stabil pro

                     models_list.append(f"{prefix} `{model_id}`")

         # İşlem mesajını sil (eğer hala varsa)
         try: await processing_msg.delete()
         except: pass

         if not models_list:
             await ctx.send("Google API'den kullanılabilir sohbet modeli alınamadı veya bulunamadı.")
             return

         # Modelleri sırala (önce işaretliler, sonra diğerleri alfabetik)
         models_list.sort(key=lambda x: (not x.startswith(("⚡", "✨", "✅")), x.lower()))

         embed = discord.Embed(
             title="🤖 Kullanılabilir Gemini Sohbet Modelleri",
             description=f"Bir sonraki özel sohbetiniz için `.setmodel <ad>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(models_list),
             color=discord.Color.gold()
         )
         embed.set_footer(text="⚡ Flash (Hızlı), ✨ Pro 1.5 (Gelişmiş), ✅ Pro (Stabil)")
         await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Modeller listelenirken hata: {e}")
        await ctx.send("Modeller listelenirken bir hata oluştu. API anahtarınızı veya bağlantınızı kontrol edin.")
        # İşlem mesajını silmeyi dene (hata durumunda)
        try: await processing_msg.delete()
        except: pass

@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx, model_id: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini modelini ayarlar."""
    global user_next_model
    if model_id is None:
        await ctx.send(f"Lütfen bir model adı belirtin. Kullanılabilir modeller için `{ctx.prefix}listmodels` komutunu kullanın.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    # Kullanıcı sadece kısa adı girdiyse başına "models/" ekle
    # Kullanıcı 'models/...' girdiyse dokunma
    target_model_name = model_id if model_id.startswith("models/") else f"models/{model_id}"
    target_model_short_name = target_model_name.split('/')[-1] # Kısa adı al (kayıt için)

    # Çok temel bir ön kontrol (en azından 'gemini' içersin)
    if not target_model_short_name or 'gemini' not in target_model_short_name:
         await ctx.send(f"❌ Geçersiz model adı formatı gibi görünüyor: `{model_id}`. Lütfen `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)
         try: await ctx.message.delete(delay=15)
         except: pass
         return

    # Modeli test etmek için işlem mesajı
    processing_msg = await ctx.send(f"⚙️ `{target_model_short_name}` modeli kontrol ediliyor...", delete_after=10)

    try:
        logger.info(f"{ctx.author.name} ({ctx.author.id}) bir sonraki sohbet için '{target_model_short_name}' modelini ayarlıyor...")
        # Modelin geçerli olup olmadığını kontrol et (API'ye sorarak)
        async with ctx.typing():
            # genai.get_model senkron bir çağrı, thread'e taşı
            model_info = await asyncio.to_thread(genai.get_model, target_model_name)
            # Modelin sohbeti destekleyip desteklemediğini kontrol et
            if 'generateContent' not in model_info.supported_generation_methods:
                raise ValueError(f"Model '{target_model_short_name}' sohbet ('generateContent') desteklemiyor.")

        # İşlem mesajını sil
        try: await processing_msg.delete()
        except: pass

        # Modeli kullanıcı için sakla (kısa adını saklamak yeterli)
        user_next_model[ctx.author.id] = target_model_short_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{target_model_short_name}' olarak ayarlandı.")
        await ctx.send(f"✅ Başarılı! Bir sonraki özel sohbetiniz **`{target_model_short_name}`** modeli ile başlayacak.", delete_after=20)
        try: await ctx.message.delete(delay=20)
        except: pass
    except ValueError as ve: # Modelin sohbet desteklemediği durum
        logger.warning(f"{ctx.author.name} sohbet desteklemeyen model denedi ({target_model_name}): {ve}")
        try: await processing_msg.delete()
        except: pass
        await ctx.send(f"❌ `{target_model_short_name}` modeli sohbet için kullanılamaz. Lütfen `{ctx.prefix}listmodels` ile sohbet modellerini kontrol edin.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
    except Exception as e: # Diğer hatalar (model bulunamadı, API hatası vb.)
        logger.warning(f"{ctx.author.name} geçersiz/erişilemeyen model denedi ({target_model_name}): {e}")
        try: await processing_msg.delete()
        except: pass
        await ctx.send(f"❌ `{target_model_short_name}` geçerli veya erişilebilir bir model gibi görünmüyor.\nLütfen `{ctx.prefix}listmodels` komutu ile listeyi kontrol edin veya model adını doğru yazdığınızdan emin olun.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass


@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True) # Sadece adminler
@commands.guild_only()
async def set_entry_channel(ctx, channel: discord.TextChannel = None):
    """Otomatik sohbet kanalı oluşturulacak giriş kanalını ayarlar (Admin)."""
    global entry_channel_id
    if channel is None:
        # Mevcut ayarı göster
        current_channel_id = load_config('entry_channel_id')
        current_channel_mention = "Ayarlanmamış"
        if current_channel_id:
             try:
                 fetched_channel = await bot.fetch_channel(int(current_channel_id))
                 current_channel_mention = fetched_channel.mention
             except: current_channel_mention = f"ID: {current_channel_id} (Erişilemiyor)"
        await ctx.send(f"ℹ️ Mevcut giriş kanalı: {current_channel_mention}\nAyarlamak için: `{ctx.prefix}setentrychannel #kanal-adı` veya `{ctx.prefix}setentrychannel <kanal_id>`")
        return

    # Kanalın gerçekten bu sunucuda bir metin kanalı olduğunu teyit et
    if not isinstance(channel, discord.TextChannel) or channel.guild != ctx.guild:
         await ctx.send("❌ Lütfen bu sunucudaki geçerli bir metin kanalını etiketleyin veya ID'sini yazın.")
         return

    new_entry_channel_id = channel.id
    save_config('entry_channel_id', new_entry_channel_id) # Ayarı DB'ye kaydet
    entry_channel_id = new_entry_channel_id # Global değişkeni de güncelle
    logger.info(f"Giriş kanalı yönetici {ctx.author.name} tarafından {channel.mention} (ID: {channel.id}) olarak ayarlandı.")
    await ctx.send(f"✅ Giriş kanalı başarıyla {channel.mention} olarak ayarlandı.")

    # Botun aktivitesini güncelle
    try:
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için #{channel.name}"))
    except Exception as e:
        logger.warning(f"Giriş kanalı ayarlandıktan sonra bot aktivitesi güncellenemedi: {e}")

@bot.command(name='settimeout', aliases=['zamanasimi'])
@commands.has_permissions(administrator=True) # Sadece adminler
@commands.guild_only()
async def set_inactivity_timeout(ctx, hours: float = None):
    """Geçici kanalların aktif olmazsa silineceği süreyi saat cinsinden ayarlar (Admin)."""
    global inactivity_timeout
    if hours is None:
        current_timeout_hours = "Ayarlanmamış"
        current_db_val = load_config('inactivity_timeout_hours')
        if current_db_val:
            try: current_timeout_hours = f"{float(current_db_val):.1f}".rstrip('0').rstrip('.')
            except: pass # Geçersiz değer varsa Ayarlanmamış kalsın
        await ctx.send(f"ℹ️ Mevcut inaktivite zaman aşımı: **{current_timeout_hours} saat**.\nAyarlamak için: `{ctx.prefix}settimeout <saat>` (örn: `{ctx.prefix}settimeout 2.5`)")
        return

    # Çok kısa veya çok uzun süreleri engelle
    min_hours = 0.1 # 6 dakika
    max_hours = 168 # 1 hafta (7 gün)
    if not (min_hours <= hours <= max_hours):
         await ctx.send(f"❌ Geçersiz saat değeri. Süre {min_hours} ile {max_hours} saat arasında olmalıdır.")
         return

    new_inactivity_timeout = datetime.timedelta(hours=hours)
    save_config('inactivity_timeout_hours', str(hours)) # Ayarı DB'ye kaydet (string olarak)
    inactivity_timeout = new_inactivity_timeout # Global değişkeni de güncelle
    logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours} saat olarak ayarlandı.")
    # Kullanıcıya gösterirken güzel formatla
    hours_display = f"{hours:.1f}".rstrip('0').rstrip('.')
    await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **{hours_display} saat** olarak ayarlandı.")


@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx):
    """Botun kullanılabilir komutlarını listeler."""
    entry_channel_mention = "Ayarlanmamış"
    # entry_channel_id global değişkeninden okuyalım
    if entry_channel_id:
        entry_channel = bot.get_channel(entry_channel_id) # get_channel cache'den okur, daha hızlı
        if entry_channel:
             entry_channel_mention = f"<#{entry_channel_id}>"
        else:
             # Cache'de yoksa fetch etmeyi deneyelim (başlangıçta bulunamadıysa diye)
             try:
                 fetched_channel = await bot.fetch_channel(entry_channel_id)
                 entry_channel_mention = fetched_channel.mention
             except:
                 entry_channel_mention = f"ID: {entry_channel_id} (Erişilemiyor)"
    elif load_config('entry_channel_id'): # Globalde yoksa DB'ye bakalım
          db_id = load_config('entry_channel_id')
          entry_channel_mention = f"ID: {db_id} (Ayarlı ama erişilemiyor?)"


    embed = discord.Embed(
        title=f"🤖 {bot.user.name} Komut Listesi",
        description=f"**Özel Sohbet Başlatma:**\n"
                    f"{entry_channel_mention} kanalına herhangi bir mesaj yazarak yapay zeka ile özel sohbet başlatabilirsiniz.\n\n"
                    f"**Diğer Komutlar:**\n"
                    f"Aşağıdaki komutları `{ctx.prefix}` ön eki ile kullanabilirsiniz.",
        color=discord.Color.dark_purple()
    )
    # Botun avatarını ekle
    if bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    # Komutları gruplandır (Genel, Sohbet, Yönetim)
    user_commands = []
    chat_commands = []
    admin_commands = []

    # Komutları al ve sırala
    all_commands = sorted(bot.commands, key=lambda cmd: cmd.name)

    for command in all_commands:
        # Gizli komutları veya çalıştırılamayanları atla
        if command.hidden: continue
        try:
            can_run = await command.can_run(ctx)
            if not can_run: continue
        except commands.CheckFailure: continue
        except Exception as e:
             logger.warning(f"Komut '{command.name}' için can_run kontrolünde hata: {e}")
             continue # Hata olursa gösterme

        # Komut bilgilerini formatla
        help_text = command.help or command.short_doc or "Açıklama yok."
        aliases = f"\n*Diğer adlar:* `{'`, `'.join(command.aliases)}`" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""
        # Komut adını ve parametrelerini kalın yap
        cmd_string = f"**`{ctx.prefix}{command.name}{params}`**\n{help_text}{aliases}"

        # Kategoriye ayır (izinlere göre)
        is_admin = False
        if command.checks:
            for check in command.checks:
                 # has_permissions içinde administrator=True var mı diye bakmak daha doğru olur
                 # Ama şimdilik sadece decorator'ın varlığına bakalım
                 if hasattr(check, '__qualname__') and ('has_permissions' in check.__qualname__ or 'is_owner' in check.__qualname__):
                      # Daha spesifik: Admin yetkisi gerektiriyor mu?
                      required_perms = getattr(check, 'permissions', {})
                      if required_perms.get('administrator', False):
                           is_admin = True
                           break # Admin ise diğer kontrollere gerek yok
            # Sadece admin yetkisi gerektirenleri Admin grubuna alalım
            if is_admin:
                admin_commands.append(cmd_string)
            # Diğer izin gerektirenler (moderatör gibi) veya özel check'ler
            # Şimdilik bunları da admin grubuna ekleyelim veya ayrı grup açılabilir
            elif any(hasattr(c, '__qualname__') and ('has_permissions' in c.__qualname__ or 'is_owner' in c.__qualname__) for c in command.checks):
                 admin_commands.append(cmd_string)

        # Sohbet kanalı komutları (isimden tahmin)
        if command.name in ['endchat', 'resetchat']:
            chat_commands.append(cmd_string)
        # Admin veya sohbet değilse genel komut
        elif not is_admin and command.name not in ['endchat', 'resetchat']:
            user_commands.append(cmd_string)

    # Embed'e alanları ekle
    if user_commands:
        embed.add_field(name="👤 Genel Komutlar", value="\n\n".join(user_commands), inline=False)
    if chat_commands:
        embed.add_field(name="💬 Sohbet Kanalı Komutları", value="\n\n".join(chat_commands), inline=False)
    if admin_commands:
         embed.add_field(name="🛠️ Yönetici Komutları", value="\n\n".join(admin_commands), inline=False)

    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)} | <> içindekiler gerekli, [] içindekiler isteğe bağlı argümanlardır.")
    try:
        await ctx.send(embed=embed)
    except discord.errors.HTTPException as e:
         # Genellikle embed çok uzunsa bu hata alınır
         logger.error(f"Yardım mesajı gönderilemedi (çok uzun olabilir): {e}")
         await ctx.send("Komut listesi çok uzun olduğu için gönderilemedi.")
    except Exception as e:
         logger.error(f"Yardım mesajı gönderilirken hata: {e}")
         await ctx.send("Komut listesi gösterilirken bir hata oluştu.")

# --- Genel Hata Yakalama ---
@bot.event
async def on_command_error(ctx, error):
    """Komutlarla ilgili hataları merkezi olarak yakalar ve kullanıcıya bilgi verir."""
    # Orijinal hatayı al (eğer başka bir hataya sarılmışsa)
    original_error = getattr(error, 'original', error)
    delete_user_msg = True  # Varsayılan olarak kullanıcının komut mesajını sil
    delete_delay = 10       # Mesajların silinme gecikmesi (saniye)

    # Görmezden gelinecek hatalar
    if isinstance(original_error, commands.CommandNotFound):
        logger.debug(f"Bilinmeyen komut denendi: {ctx.message.content}")
        return # Bilinmeyen komutları sessizce geç
    if isinstance(original_error, commands.NotOwner): # Sahibi değilse sessiz kal
        logger.warning(f"Sahibi olmayan kullanıcı ({ctx.author}) sahip komutu denedi: {ctx.command.qualified_name}")
        return

    # Hata mesajını hazırlamak için bir string
    error_message = None
    log_level = logging.WARNING # Varsayılan log seviyesi

    # Bilinen Hata Türlerini İşle
    if isinstance(original_error, commands.MissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        error_message = f"⛔ Bu komutu kullanmak için şu izin(ler)e sahip olmalısın: {perms}"
        delete_delay = 15
    elif isinstance(original_error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        error_message = f"🆘 Bu komutu çalıştırabilmem için benim şu izin(ler)e sahip olmam gerekiyor: {perms}"
        delete_user_msg = False # Botun hatası, kullanıcının mesajı kalsın
        delete_delay = 15
        log_level = logging.ERROR # Botun hatası daha kritik
    elif isinstance(original_error, commands.NoPrivateMessage):
        error_message = "ℹ️ Bu komut sadece sunucu kanallarında kullanılabilir."
        delete_user_msg = False # DM'de mesajı silemeyiz
        try: await ctx.author.send(error_message) # Kullanıcıya DM at
        except: pass # DM kapalıysa yapacak bir şey yok
        return # Kanalda mesaj gönderme
    elif isinstance(original_error, commands.PrivateMessageOnly):
        error_message = "ℹ️ Bu komut sadece özel mesajla (DM) kullanılabilir."
        delete_delay = 10
    elif isinstance(original_error, commands.CheckFailure):
        # Özel check decorator'ları veya guild_only gibi genel checkler
        error_message = "🚫 Bu komutu burada veya şu anda kullanamazsınız."
        # Daha spesifik mesajlar için check'in türüne bakılabilir ama şimdilik genel tutalım
        delete_delay = 10
    elif isinstance(original_error, commands.CommandOnCooldown):
        error_message = f"⏳ Komut beklemede. Lütfen **{original_error.retry_after:.1f} saniye** sonra tekrar deneyin."
        delete_delay = max(5, int(original_error.retry_after) + 1) # En az 5sn veya cooldown + 1sn göster
    elif isinstance(original_error, commands.UserInputError): # BadArgument, MissingRequiredArgument vb.
         command_name = ctx.command.qualified_name
         usage = f"`{ctx.prefix}{command_name} {ctx.command.signature}`"
         if isinstance(original_error, commands.MissingRequiredArgument):
              error_message = f"⚠️ Eksik argüman: `{original_error.param.name}`.\nKullanım: {usage}"
         elif isinstance(original_error, commands.BadArgument):
              # Hatanın kendisi genellikle neyin yanlış gittiğini söyler
              error_message = f"⚠️ Geçersiz argüman: {original_error}\nKullanım: {usage}"
         elif isinstance(original_error, commands.TooManyArguments):
              error_message = f"⚠️ Çok fazla argüman girdiniz.\nKullanım: {usage}"
         else: # Diğer UserInputError'lar
              error_message = f"⚠️ Hatalı komut kullanımı.\nKullanım: {usage}"
         delete_delay = 15
    # Bot ile ilgili olmayan, kod içindeki genel hatalar
    elif isinstance(original_error, discord.errors.Forbidden):
         error_message = f"❌ Discord İzin Hatası: Belirtilen işlemi yapma iznim yok. ({original_error.text})"
         log_level = logging.ERROR
         delete_user_msg = False
    elif isinstance(original_error, discord.errors.HTTPException):
         error_message = f"🌐 Discord API Hatası: {original_error.status} - {original_error.text}"
         log_level = logging.ERROR
    else:
        # Bilinmeyen/Beklenmedik Hatalar
        error_message = "⚙️ Komut işlenirken beklenmedik bir hata oluştu."
        log_level = logging.ERROR # Beklenmedik hatalar önemlidir
        delete_delay = 15
        # Traceback'i logla
        tb_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__))
        logger.log(log_level, f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' komutunda işlenmeyen hata:\n{tb_str}")

    # Hata mesajını gönder (eğer oluşturulduysa ve DM değilse)
    if error_message and ctx.guild:
        try:
            await ctx.send(error_message, delete_after=delete_delay)
        except discord.errors.Forbidden:
             logger.warning(f"'{ctx.channel.name}' kanalına hata mesajı gönderme izni yok.")
        except Exception as send_err:
             logger.error(f"Hata mesajı gönderilirken ek hata: {send_err}")

    # Kullanıcının komut mesajını sil (eğer ayarlandıysa ve sunucudaysa)
    if delete_user_msg and ctx.guild and ctx.message:
        # Botun mesajları yönetme izni varsa silmeyi dene
        if ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            try:
                await ctx.message.delete(delay=delete_delay)
            except discord.errors.NotFound: pass # Mesaj zaten silinmiş olabilir
            except discord.errors.Forbidden: pass # İzin yoksa (az önce kontrol ettik ama yine de)
            except Exception as e:
                logger.warning(f"Hata sonrası komut mesajı ({ctx.message.id}) silinirken ek hata: {e}")
        else:
             logger.debug(f"'{ctx.channel.name}' kanalında mesaj silme izni yok, komut mesajı ({ctx.message.id}) silinemedi.")


# === Web Sunucusu (Render/Koyeb için) ===
app = Flask(__name__)
@app.route('/')
def home():
    """Basit bir sağlık kontrolü endpoint'i. Uptime monitor için."""
    if bot and bot.is_ready():
        try:
            guild_count = len(bot.guilds)
            # Aktif sohbet sayısını state'den alalım
            active_chats = len(temporary_chat_channels)
            latency_ms = round(bot.latency * 1000)
            return f"OK: Bot '{bot.user.name}' çalışıyor. {guild_count} sunucu, {active_chats} aktif sohbet. Gecikme: {latency_ms}ms", 200
        except Exception as e:
             logger.error(f"Sağlık kontrolü endpoint'inde hata: {e}")
             return "ERROR: Bot durumu alınırken hata.", 500
    elif bot and not bot.is_ready():
        return "PENDING: Bot başlatılıyor, henüz hazır değil...", 503 # Service Unavailable
    else:
        return "ERROR: Bot durumu bilinmiyor veya başlatılamadı.", 500 # Internal Server Error

def run_webserver():
    """Flask web sunucusunu ayrı bir thread'de çalıştırır."""
    # Render/Koyeb PORT ortam değişkenini otomatik olarak sağlar
    port = int(os.environ.get("PORT", 8080)) # Varsayılan 8080
    try:
        logger.info(f"Flask web sunucusu http://0.0.0.0:{port} adresinde başlatılıyor...")
        # app.run() development server'dır, production için gunicorn gibi bir WSGI server daha iyi olabilir
        # Ancak basitlik ve çoğu PaaS platformu için bu genellikle yeterlidir.
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        # Genellikle port zaten kullanımda hatası burada yakalanır
        logger.critical(f"Web sunucusu başlatılırken kritik hata (Port: {port}): {e}")
        # Web sunucusu başlamazsa botun çalışmaya devam etmesi istenebilir veya durdurulabilir.
        # Şimdilik sadece loglayalım.
# ===================================


# --- Botu Çalıştır ---
if __name__ == "__main__":
    logger.info("===== Bot Başlatılıyor =====")
    try:
        # Web sunucusunu ayrı bir thread'de başlat
        # daemon=True: Ana thread (bot) bittiğinde webserver thread'inin de otomatik kapanmasını sağlar
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()
        logger.info("Web sunucusu thread'i başlatıldı.")

        # Discord botunu ana thread'de çalıştır
        # Kendi logger'ımızı kullandığımız için discord.py'nin varsayılan log handler'ını devre dışı bırakıyoruz
        bot.run(DISCORD_TOKEN, log_handler=None)

    except discord.errors.LoginFailure:
        logger.critical("HATA: Geçersiz Discord Token! Bot başlatılamadı.")
    except discord.errors.PrivilegedIntentsRequired:
         logger.critical("HATA: Gerekli Intent'ler (örn. Server Members, Message Content) Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err:
         # Veritabanı bağlantı hatası (örn. yanlış DATABASE_URL, DB kapalı)
         logger.critical(f"Başlangıçta PostgreSQL bağlantı hatası: {db_err}")
         logger.critical("DATABASE_URL doğru ayarlandı mı ve veritabanı erişilebilir mi kontrol edin.")
    except Exception as e:
        # Diğer tüm beklenmedik başlangıç hataları
        logger.critical(f"Bot çalıştırılırken kritik bir hata oluştu: {e}")
        logger.critical(traceback.format_exc()) # Tam traceback'i logla
    finally:
        # Bot kapatıldığında (Ctrl+C vb.) veya bir hata ile durduğunda çalışır
        logger.info("===== Bot Kapatılıyor =====")
        # Burada açık kaynakları kapatma işlemleri yapılabilir (DB bağlantısı vb. ama zaten her işlemde aç/kapa yapıyoruz)
        # asyncio event loop'u kapatma vb. (genellikle bot.run() bunu halleder)