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
import sqlite3 # Veritabanı için
from flask import Flask # Koyeb için web sunucusu
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


# .env dosyasındaki değişkenleri yükle
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Varsayılan değerler (veritabanından okunamazsa kullanılır)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
DEFAULT_MODEL_NAME = 'models/gemini-1.5-flash-latest'
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Veritabanı Ayarları ---
DB_FILE = 'bot_data.db' # Veritabanı dosyasının adı

# --- Global Değişkenler (Başlangıçta None, on_ready'de doldurulacak) ---
entry_channel_id = None
inactivity_timeout = None
model_name = DEFAULT_MODEL_NAME # Başlangıçta varsayılan model

active_gemini_chats = {} # channel_id -> {'session': ChatSession, 'model': model_adı}
temporary_chat_channels = set() # Geçici kanal ID'leri
user_to_channel_map = {} # user_id -> channel_id
channel_last_active = {} # channel_id -> datetime
user_next_model = {} # user_id -> model_adı (Bir sonraki sohbet için tercih)
warned_inactive_channels = set() # İnaktivite uyarısı gönderilen kanallar

# --- Veritabanı Yardımcı Fonksiyonları ---

def db_connect():
    """Veritabanı bağlantısı oluşturur."""
    return sqlite3.connect(DB_FILE)

def setup_database():
    """Veritabanı tablolarını oluşturur (varsa dokunmaz)."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Varsayılan modeli temp_channels tablosuna ekle (parameterized query daha güvenli olurdu ama f-string burada muhtemelen çalışır)
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                last_active TIMESTAMP NOT NULL,
                model_name TEXT DEFAULT '{DEFAULT_MODEL_NAME.split('/')[-1]}'
            )
        ''')
        conn.commit()
        conn.close()
        logger.info("Veritabanı tabloları kontrol edildi/oluşturuldu.")
    except Exception as e:
        logger.critical(f"Veritabanı kurulumu sırasında KRİTİK HATA: {e}")
        exit()

def save_config(key, value):
    """Yapılandırma ayarını veritabanına kaydeder."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"Yapılandırma kaydedilirken hata (Key: {key}): {e}")

def load_config(key, default=None):
    """Yapılandırma ayarını veritabanından yükler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else default
    except Exception as e: logger.error(f"Yapılandırma yüklenirken hata (Key: {key}): {e}"); return default

def load_all_temp_channels():
    """Tüm geçici kanal durumlarını veritabanından yükler."""
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row # Sütun adlarıyla erişim için
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, user_id, last_active, model_name FROM temp_channels")
        channels = cursor.fetchall()
        conn.close()
        loaded_data = []
        default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
        for row in channels:
            try:
                last_active_dt = datetime.datetime.fromisoformat(row['last_active'])
                model_name_db = row['model_name'] or default_model_short # DB'de NULL ise varsayılanı kullan
                loaded_data.append((row['channel_id'], row['user_id'], last_active_dt, model_name_db))
            except (ValueError, TypeError) as ts_error:
                logger.error(f"DB'de geçersiz zaman damgası (channel_id: {row['channel_id']}): {ts_error} - Değer: {row['last_active']}")
        return loaded_data
    except Exception as e: logger.error(f"Geçici kanallar yüklenirken DB hatası: {e}"); return []

def add_temp_channel_db(channel_id, user_id, timestamp, model_used):
    """Yeni geçici kanalı veritabanına ekler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        # Model adını kaydederken kısa adını (models/ olmadan) kaydet
        model_short_name = model_used.split('/')[-1]
        cursor.execute("REPLACE INTO temp_channels (channel_id, user_id, last_active, model_name) VALUES (?, ?, ?, ?)",
                       (channel_id, user_id, timestamp.isoformat(), model_short_name))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"Geçici kanal DB'ye eklenirken hata (channel_id: {channel_id}): {e}")

def update_channel_activity_db(channel_id, timestamp):
    """Kanalın son aktivite zamanını veritabanında günceller."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("UPDATE temp_channels SET last_active = ? WHERE channel_id = ?",
                       (timestamp.isoformat(), channel_id))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"Kanal aktivitesi DB'de güncellenirken hata (channel_id: {channel_id}): {e}")

def remove_temp_channel_db(channel_id):
    """Geçici kanalı veritabanından siler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM temp_channels WHERE channel_id = ?", (channel_id,))
        conn.commit()
        conn.close()
        logger.info(f"Geçici kanal {channel_id} veritabanından silindi.")
    except Exception as e: logger.error(f"Geçici kanal DB'den silinirken hata (channel_id: {channel_id}): {e}")

# --- Yapılandırma Kontrolleri (Veritabanı ile birlikte) ---
setup_database()

entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))

if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); exit()
if not GEMINI_API_KEY: logger.critical("HATA: Gemini API Anahtarı bulunamadı!"); exit()
if not entry_channel_id_str: logger.warning("UYARI: Giriş Kanalı ID'si (ENTRY_CHANNEL_ID) .env veya veritabanında bulunamadı! Otomatik kanal oluşturma çalışmayacak."); entry_channel_id = None
else:
    try: entry_channel_id = int(entry_channel_id_str); logger.info(f"Giriş Kanalı ID'si: {entry_channel_id}")
    except (ValueError, TypeError): logger.critical(f"HATA: Giriş Kanalı ID'si ('{entry_channel_id_str}') geçerli bir sayı değil!"); exit()

try: inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str)) ; logger.info(f"İnaktivite Zaman Aşımı: {inactivity_timeout}")
except (ValueError, TypeError): logger.error(f"HATA: İnaktivite süresi ('{inactivity_timeout_hours_str}') geçersiz! Varsayılan {DEFAULT_INACTIVITY_TIMEOUT_HOURS} saat kullanılıyor."); inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)

# Gemini API'yi yapılandır ve global modeli oluştur
model = None
try:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API anahtarı yapılandırıldı.")
    # Varsayılan modeli global değişkene ata (zaten yukarıda atanmıştı ama burada teyit ediliyor)
    model_name = DEFAULT_MODEL_NAME
    try:
        # Modeli oluşturmayı dene (varlık kontrolü ve oluşturma bir arada)
        model = genai.GenerativeModel(model_name)
        logger.info(f"Global varsayılan Gemini modeli ('{model_name}') başarıyla oluşturuldu.")
    except Exception as model_error:
        # Eğer model oluşturulamazsa
        logger.critical(f"HATA: Varsayılan Gemini modeli ('{model_name}') oluşturulamadı: {model_error}")
        model = None
        exit() # Programı durdur

except Exception as configure_error:
    # Configure hatasını yakala
    logger.critical(f"HATA: Gemini API genel yapılandırma hatası: {configure_error}")
    model = None
    exit() # Programı durdur

# Model oluşturulamadıysa kontrol et (Gerçi yukarıda exit() var ama yine de kontrol)
if model is None:
     logger.critical("HATA: Global Gemini modeli oluşturulamadı. Bot başlatılamıyor.")
     exit()


# --- Bot Kurulumu ---
intents = discord.Intents.default(); intents.message_content = True; intents.members = True; intents.messages = True; intents.guilds = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Global Durum Yönetimi (Veritabanı sonrası) ---
# active_gemini_chats, temporary_chat_channels, user_to_channel_map, channel_last_active, user_next_model, warned_inactive_channels

# --- Yardımcı Fonksiyonlar ---

async def create_private_chat_channel(guild, author):
    """Verilen kullanıcı için özel sohbet kanalı oluşturur ve kanal nesnesini döndürür."""
    if not guild.me.guild_permissions.manage_channels: logger.warning(f"'{guild.name}' sunucusunda 'Kanalları Yönet' izni eksik."); return None
    safe_username = "".join(c for c in author.display_name if c.isalnum() or c == ' ').strip().replace(' ', '-').lower()
    if not safe_username: safe_username = "kullanici"
    safe_username = safe_username[:80] # Kanal adı uzunluk sınırına dikkat
    base_channel_name = f"sohbet-{safe_username}"; channel_name = base_channel_name; counter = 1
    # Büyük/küçük harf duyarsız kontrol
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}
    while channel_name.lower() in existing_channel_names:
        # Aşırı uzunluğu da kontrol et
        potential_name = f"{base_channel_name}-{counter}"
        if len(potential_name) > 100:
             potential_name = f"{base_channel_name[:90]}-{counter}" # Kırp ve ekle
        channel_name = potential_name
        counter += 1
        if counter > 1000: # Sonsuz döngü koruması
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı).")
            # Daha rastgele bir isim dene
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
    # Yönetici rol engelleme (opsiyonel, DB'den okunabilir)
    # admin_role_ids_str = load_config("admin_role_ids", "")
    # if admin_role_ids_str: ... (admin rollerini overwrites'a ekle view_channel=False ile)
    try:
        new_channel = await guild.create_text_channel(channel_name, overwrites=overwrites, reason=f"{author.name} için otomatik Gemini sohbet kanalı.")
        logger.info(f"Kullanıcı {author.name} ({author.id}) için '{channel_name}' (ID: {new_channel.id}) kanalı oluşturuldu.")
        return new_channel
    except Exception as e: logger.error(f"Kanal oluşturmada hata: {e}\n{traceback.format_exc()}"); return None

async def send_to_gemini_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    """Belirtilen kanalda Gemini'ye mesaj gönderir ve yanıtlar. Sohbet oturumunu kullanır."""
    global channel_last_active, active_gemini_chats
    if not prompt_text.strip(): return False # Boş mesaj gönderme

    # Aktif sohbet yoksa oluştur
    if channel_id not in active_gemini_chats:
        try:
            # Kanal için kaydedilen modeli DB'den al
            conn = db_connect(); cursor = conn.cursor(); cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = ?", (channel_id,)); result = cursor.fetchone(); conn.close()
            current_model_short_name = result[0] if result and result[0] else DEFAULT_MODEL_NAME.split('/')[-1]
            current_model_full_name = f"models/{current_model_short_name}"

            logger.info(f"'{channel.name}' (ID: {channel_id}) için Gemini sohbet oturumu {current_model_full_name} ile başlatılıyor.")
            # Yeni model örneği oluştur
            chat_model_instance = genai.GenerativeModel(current_model_full_name)
            active_gemini_chats[channel_id] = {'session': chat_model_instance.start_chat(history=[]), 'model': current_model_short_name}
        except Exception as e:
            logger.error(f"'{channel.name}' için Gemini sohbet oturumu başlatılamadı ({current_model_full_name}): {e}")
            try: await channel.send("Yapay zeka oturumu başlatılamadı. Model geçerli olmayabilir veya API hatası.", delete_after=15)
            except discord.errors.NotFound: pass # Kanal bu arada silinmiş olabilir
            except Exception as send_err: logger.warning(f"Oturum hatası mesajı gönderilemedi: {send_err}")
            return False

    # Sohbet verilerini al
    chat_data = active_gemini_chats[channel_id]
    chat_session = chat_data['session']
    current_model_name = chat_data['model'] # Kullanılan modelin kısa adı

    logger.info(f"[GEMINI CHAT/{current_model_name}] [{author.name} @ {channel.name}] gönderiyor: {prompt_text[:100]}{'...' if len(prompt_text)>100 else ''}")

    async with channel.typing():
        try:
            # Mesajı Gemini'ye gönder
            response = await chat_session.send_message_async(prompt_text)

            # Son aktivite zamanını güncelle
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[channel_id] = now_utc
            update_channel_activity_db(channel_id, now_utc) # DB'yi de güncelle

            gemini_response = response.text.strip()

            # Yanıtı işle ve gönder
            if not gemini_response:
                logger.warning(f"[GEMINI CHAT/{current_model_name}] Boş yanıt (Kanal: {channel_id}).")
                # Kullanıcıya boş yanıt geldiğini bildirebiliriz (isteğe bağlı)
                # await channel.send("Yapay zeka boş bir yanıt döndürdü.", delete_after=10)
            elif len(gemini_response) > 2000:
                logger.info(f"Yanıt >2000kr (Kanal: {channel_id}), parçalanıyor...")
                for i in range(0, len(gemini_response), 2000):
                    await channel.send(gemini_response[i:i+2000])
            else:
                await channel.send(gemini_response)
            return True # Başarılı

        except Exception as e:
            logger.error(f"[GEMINI CHAT/{current_model_name}] API hatası (Kanal: {channel_id}): {e}")
            error_str = str(e).lower()
            user_msg = "Yapay zeka ile konuşurken bir sorun oluştu."
            # Daha spesifik hata mesajları
            if "api key not valid" in error_str or "permission_denied" in error_str or "403" in error_str:
                user_msg = "API Anahtarı sorunu veya yetki reddi."
            elif "quota exceeded" in error_str or "resource_exhausted" in error_str or "429" in error_str:
                user_msg = "API kullanım limiti aşıldı. Lütfen daha sonra tekrar deneyin."
            elif "finish_reason: SAFETY" in str(response.prompt_feedback).upper() or "finish_reason: SAFETY" in str(getattr(response, 'candidates', [{}])[0].get('finish_reason', '')).upper():
                 user_msg = "Yanıt güvenlik filtrelerine takıldı." # Daha güvenilir kontrol
            elif "400" in error_str or "invalid argument" in error_str:
                user_msg = "Geçersiz istek gönderildi (örn: model bu tür girdiyi desteklemiyor olabilir)."
            elif "500" in error_str or "internal error" in error_str:
                 user_msg = "Yapay zeka sunucusunda geçici bir sorun oluştu. Lütfen tekrar deneyin."

            try: await channel.send(user_msg, delete_after=15)
            except discord.errors.NotFound: pass # Kanal bu arada silinmiş olabilir
            except Exception as send_err: logger.warning(f"API hatası mesajı gönderilemedi: {send_err}")
            return False # Başarısız
        except discord.errors.HTTPException as e:
            # Discord'a gönderirken hata (örn. rate limit)
            logger.error(f"Discord'a mesaj gönderilemedi (Kanal: {channel_id}): {e}")
            return False # Başarısız (ama Gemini yanıt vermiş olabilir)

# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'{bot.user} olarak giriş yapıldı.')

    # Ayarları tekrar yükle (DB güncellenmiş olabilir)
    entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
    inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))

    try: entry_channel_id = int(entry_channel_id_str) if entry_channel_id_str else None
    except (ValueError, TypeError): logger.error(f"DB'den ENTRY_CHANNEL_ID yüklenemedi: {entry_channel_id_str}. Varsayılan kullanılıyor."); entry_channel_id = int(DEFAULT_ENTRY_CHANNEL_ID) if DEFAULT_ENTRY_CHANNEL_ID else None

    try: inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str))
    except (ValueError, TypeError): logger.error(f"DB'den inactivity_timeout_hours yüklenemedi: {inactivity_timeout_hours_str}. Varsayılan kullanılıyor."); inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)

    logger.info(f"Ayarlar yüklendi - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")

    # Kalıcı verileri temizle ve DB'den yükle
    logger.info("Kalıcı veriler yükleniyor...");
    temporary_chat_channels.clear(); user_to_channel_map.clear(); channel_last_active.clear(); active_gemini_chats.clear() # Aktif sohbetleri de temizle
    warned_inactive_channels.clear()

    loaded_channels = load_all_temp_channels(); valid_channel_count = 0; invalid_channel_ids = []
    default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
    for ch_id, u_id, last_active_ts, ch_model_name in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel):
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            channel_last_active[ch_id] = last_active_ts
            # Aktif sohbeti başlatma, ilk mesaja kadar bekleyelim. Sadece model adını saklayabiliriz veya DB'den okuruz.
            # Model adını `add_temp_channel_db` içinde kaydettiğimiz için sorun yok.
            valid_channel_count += 1
        else:
            logger.warning(f"DB'deki geçici kanal {ch_id} Discord'da bulunamadı/geçersiz. DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)

    # Geçersiz kanalları DB'den temizle
    for invalid_id in invalid_channel_ids:
        remove_temp_channel_db(invalid_id)

    logger.info(f"{valid_channel_count} aktif geçici kanal DB'den yüklendi (State bilgileri ayarlandı).")
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")

    # Bot aktivitesini ayarla
    entry_channel_name = "Ayarlanmadı"
    try:
        entry_channel = bot.get_channel(entry_channel_id) if entry_channel_id else None
        if entry_channel: entry_channel_name = f"#{entry_channel.name}"
        elif entry_channel_id: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı.")
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için {entry_channel_name}"))
    except Exception as e: logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    # İnaktivite kontrol görevini başlat (eğer çalışmıyorsa)
    if not check_inactivity.is_running():
        check_inactivity.start()
        logger.info("İnaktivite kontrol görevi başlatıldı.")

    logger.info("Bot komutları ve mesajları dinliyor..."); print("-" * 20)

@bot.event
async def on_message(message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    # Temel kontroller
    if message.author == bot.user or message.author.bot: return # Botun kendi mesajlarını veya diğer botları yoksay
    if isinstance(message.channel, discord.DMChannel): return # DM mesajlarını yoksay
    # Giriş kanalı ayarlı değilse otomatik kanal oluşturmayı atla
    if entry_channel_id is None and message.channel.id == entry_channel_id: return

    author = message.author
    author_id = author.id
    channel = message.channel
    channel_id = channel.id
    guild = message.guild

    # Mesajın bir komut olup olmadığını kontrol et
    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    # Eğer geçerli bir komutsa, on_message'dan çık (komut handler'ı devralır)
    if ctx.valid:
        return

    # --- Otomatik Kanal Oluşturma Mantığı (Sadece Giriş Kanalında) ---
    if channel_id == entry_channel_id:
        # Kullanıcının zaten aktif bir kanalı var mı?
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]
            active_channel = bot.get_channel(active_channel_id)
            mention = f"<#{active_channel_id}>" if active_channel else f"silinmiş kanal (ID: {active_channel_id})"
            logger.info(f"{author.name} giriş kanalına yazdı ama aktif kanalı var: {mention}")
            try:
                info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15)
                await message.delete(delay=15) # Kullanıcının mesajını da sil
            except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
            return

        # Yeni kanal oluşturma süreci
        initial_prompt = message.content
        original_message_id = message.id

        # Boş mesajları yoksay
        if not initial_prompt.strip():
             logger.info(f"{author.name} giriş kanalına boş mesaj gönderdi, yoksayılıyor.")
             try: await message.delete()
             except discord.errors.NotFound: pass
             except Exception as del_e: logger.warning(f"Giriş kanalındaki boş mesaj ({original_message_id}) silinemedi: {del_e}")
             return

        # Kullanıcının bir sonraki sohbet için seçtiği modeli al (varsa)
        default_model_short = DEFAULT_MODEL_NAME.split('/')[-1]
        chosen_model_name_short = user_next_model.pop(author_id, default_model_short)
        chosen_model_name_full = f"models/{chosen_model_name_short}"

        logger.info(f"{author.name} giriş kanalına yazdı, {chosen_model_name_full} ile kanal oluşturuluyor...")
        new_channel = await create_private_chat_channel(guild, author)

        if new_channel:
            new_channel_id = new_channel.id
            # State'i güncelle
            temporary_chat_channels.add(new_channel_id)
            user_to_channel_map[author_id] = new_channel_id
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[new_channel_id] = now_utc
            # DB'ye ekle
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_name_short) # Kısa adı kaydet

            # Hoşgeldin Mesajı (Embed ile)
            try:
                embed = discord.Embed(title="👋 Özel Gemini Sohbeti Başlatıldı!",
                                      description=(f"Merhaba {author.mention}!\n\n"
                                                   f"Bu kanalda `{chosen_model_name_short}` modeli ile sohbet edeceksin."),
                                      color=discord.Color.og_blurple())
                embed.set_thumbnail(url=bot.user.display_avatar.url)
                timeout_hours = inactivity_timeout.total_seconds() / 3600
                embed.add_field(name="⏳ Otomatik Kapanma", value=f"Kanal `{timeout_hours:.1f}` saat işlem görmezse otomatik olarak silinir.", inline=False)
                prefix = bot.command_prefix[0] # İlk prefix'i alalım
                embed.add_field(name="🛑 Kapat", value=f"`{prefix}endchat`", inline=True)
                embed.add_field(name="🔄 Model Seç (Sonraki)", value=f"`{prefix}setmodel <ad>`", inline=True)
                embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{prefix}resetchat`", inline=True)
                await new_channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                # Fallback düz metin mesajı
                try:
                     await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.")
                except Exception as fallback_e:
                     logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")

            # Kullanıcıya bilgi ver (giriş kanalında)
            try: await channel.send(f"{author.mention}, özel sohbet kanalı {new_channel.mention} oluşturuldu!", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")
            
            # ---> DEĞİŞİKLİK BURADA <---
            # Kullanıcının yazdığı ilk mesajı yeni kanala gönderelim
            try:
                # Mesajı "KullanıcıAdı: Mesaj" formatında gönder
                await new_channel.send(f"**{author.display_name}:** {initial_prompt}")
                logger.info(f"Kullanıcının ilk mesajı ({original_message_id}) yeni kanala ({new_channel_id}) kopyalandı.")
            except discord.errors.HTTPException as send_error:
                 logger.error(f"İlk mesaj yeni kanala gönderilemedi (Muhtemelen çok uzun): {send_error}")
            except Exception as e:
                logger.error(f"Kullanıcının ilk mesajı yeni kanala kopyalanamadı: {e}")
            # ---> DEĞİŞİKLİK SONU <---

            # İlk mesajı yeni kanala kopyala ve Gemini'ye gönder
            try:
                #await new_channel.send(f"**{author.display_name}:** {initial_prompt}") # Kopyalamak yerine doğrudan işleme gönderelim
                logger.info(f"İlk mesaj ({original_message_id}) işlenmek üzere kanala ({new_channel_id}) yönlendirildi.")
                logger.info(f"-----> GEMINI'YE İLK İSTEK (Kanal: {new_channel_id}, Model: {chosen_model_name_full})")
                success = await send_to_gemini_and_respond(new_channel, author, initial_prompt, new_channel_id)
                if success: logger.info(f"-----> İLK İSTEK BAŞARILI (Kanal: {new_channel_id})")
                else: logger.warning(f"-----> İLK İSTEK BAŞARISIZ (Kanal: {new_channel_id})")
            except Exception as e: logger.error(f"İlk mesaj işlenirken/gönderilirken hata: {e}")

            # Orijinal mesajı giriş kanalından sil
            try:
                msg_to_delete = await channel.fetch_message(original_message_id)
                await msg_to_delete.delete()
                logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except discord.errors.NotFound: logger.warning(f"Giriş kanalındaki mesaj {original_message_id} silinemeden önce kayboldu.")
            except Exception as e: logger.warning(f"Giriş kanalındaki mesaj {original_message_id} silinemedi: {e}")
        else:
            # Kanal oluşturulamadıysa
            try: await channel.send(f"{author.mention}, üzgünüm, senin için özel kanal oluşturulamadı. Lütfen tekrar dene veya bir yetkiliye bildir.", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına kanal oluşturma hata mesajı gönderilemedi: {e}")
            # Başarısız olan mesajı da silmeyi deneyebiliriz
            try: await message.delete(delay=20)
            except: pass
        return # Giriş kanalı işlemi bitti

    # --- Geçici Sohbet Kanallarındaki Mesajlar ---
    if channel_id in temporary_chat_channels and not message.author.bot:
        # Komut değilse ve geçici kanaldaysa, Gemini'ye gönder
        prompt_text = message.content
        await send_to_gemini_and_respond(channel, author, prompt_text, channel_id)

# --- Arka Plan Görevi: İnaktivite Kontrolü ---
@tasks.loop(minutes=5) # Kontrol sıklığı (5 dakika)
async def check_inactivity():
    """Aktif olmayan geçici kanalları kontrol eder, uyarır ve siler."""
    global warned_inactive_channels
    if inactivity_timeout is None: return # Zaman aşımı ayarlı değilse çık

    now = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []
    channels_to_warn = []
    warning_threshold = inactivity_timeout - datetime.timedelta(minutes=10) # Silmeden 10 dk önce uyar

    # --- DÜZELTME: Iterasyon sırasında dict boyutunun değişmemesi için kopya üzerinde çalış ---
    # for channel_id, last_active_time in channel_last_active.items(): # Bu satır riskli
    for channel_id, last_active_time in list(channel_last_active.items()): # Kopya üzerinde çalış
    # --- DÜZELTME SONU ---
        if channel_id not in temporary_chat_channels: # Eğer bir şekilde state tutarsızsa atla
             logger.warning(f"İnaktivite kontrol: {channel_id} `channel_last_active` içinde ama `temporary_chat_channels` içinde değil. Atlanıyor.")
             channel_last_active.pop(channel_id, None) # Tutarsız veriyi temizle
             warned_inactive_channels.discard(channel_id)
             continue

        time_inactive = now - last_active_time

        # Silme zamanı geldi mi?
        if time_inactive > inactivity_timeout:
            channels_to_delete.append(channel_id)
        # Uyarı zamanı geldi mi ve daha önce uyarılmadı mı?
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels:
            channels_to_warn.append(channel_id)

    # Uyarıları gönder
    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                # Kalan süreyi hesapla (yaklaşık)
                remaining_time = inactivity_timeout - (now - channel_last_active[channel_id])
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde otomatik olarak silinecektir.", delete_after=300) # Uyarı 5 dk görünsün
                warned_inactive_channels.add(channel_id)
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id}")
            except discord.errors.NotFound: warned_inactive_channels.discard(channel_id) # Kanal bu arada silinmiş olabilir
            except Exception as e: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else:
            # Kanal Discord'da yoksa, uyarı listesinden çıkar (silme listesine zaten girecek)
            warned_inactive_channels.discard(channel_id)

    # Silinecek kanalları işle
    if channels_to_delete:
        logger.info(f"İnaktivite: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
        for channel_id in channels_to_delete:
            channel_to_delete = bot.get_channel(channel_id)
            if channel_to_delete:
                try:
                    await channel_to_delete.delete(reason="İnaktivite nedeniyle otomatik silindi.")
                    logger.info(f"İnaktif kanal '{channel_to_delete.name}' (ID: {channel_id}) silindi.")
                    # on_guild_channel_delete olayı state'i temizleyecek
                except discord.errors.NotFound:
                    logger.warning(f"İnaktif kanal (ID: {channel_id}) silinirken bulunamadı. State manuel temizleniyor.")
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
                except Exception as e:
                    logger.error(f"İnaktif kanal (ID: {channel_id}) silinirken hata: {e}")
                    # Hata olsa bile DB'den silmeyi dene
                    remove_temp_channel_db(channel_id)
            else:
                logger.warning(f"İnaktif kanal (ID: {channel_id}) Discord'da bulunamadı. DB'den ve state'den siliniyor.")
                # Kanal Discord'da yoksa state'i temizle
                temporary_chat_channels.discard(channel_id)
                active_gemini_chats.pop(channel_id, None)
                channel_last_active.pop(channel_id, None)
                warned_inactive_channels.discard(channel_id)
                user_id_to_remove = None
                for user_id, ch_id in list(user_to_channel_map.items()):
                    if ch_id == channel_id: user_id_to_remove = user_id; break
                if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
                remove_temp_channel_db(channel_id) # DB'den de sil
            # Her durumda uyarı setinden çıkar
            warned_inactive_channels.discard(channel_id)

@check_inactivity.before_loop
async def before_check_inactivity():
    await bot.wait_until_ready()
    logger.info("Bot hazır, inaktivite kontrol döngüsü başlıyor.")

# --- Komutlar ---

@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx):
    """Mevcut geçici Gemini sohbet kanalını siler."""
    channel_id = ctx.channel.id
    author_id = ctx.author.id

    # Kanal geçici kanal listesinde mi?
    if channel_id not in temporary_chat_channels:
        # Belki sadece state tutarsızdır, DB'ye bakalım
        conn = db_connect(); cursor=conn.cursor(); cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = ?", (channel_id,)); owner = cursor.fetchone(); conn.close()
        if owner:
            logger.warning(f".endchat: Kanal {channel_id} state'de yok ama DB'de var (Sahip: {owner[0]}). State'e ekleniyor.")
            temporary_chat_channels.add(channel_id) # State'i düzelt
            # user_to_channel_map ve channel_last_active de güncellenebilir ama silineceği için çok kritik değil
        else:
            await ctx.send("Bu komut sadece otomatik oluşturulan özel sohbet kanallarında kullanılabilir.", delete_after=10)
            try: await ctx.message.delete(delay=10)
            except: pass
            return

    # Kanalın sahibini bul (state'den veya DB'den)
    expected_user_id = None
    for user_id, ch_id in user_to_channel_map.items():
        if ch_id == channel_id:
            expected_user_id = user_id
            break
    # State'de yoksa DB'ye tekrar bak (yukarıda eklenmiş olabilir)
    if expected_user_id is None:
        conn = db_connect(); cursor=conn.cursor(); cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = ?", (channel_id,)); owner = cursor.fetchone(); conn.close()
        if owner: expected_user_id = owner[0]; logger.warning(f".endchat: Kanal {channel_id} sahibi state'de yoktu, DB'den bulundu: {expected_user_id}")
        else: logger.error(f".endchat: Kanal {channel_id} geçici listede/DB'de ama sahibi bulunamadı!") # Bu durum olmamalı

    # Sahibi kontrol et (eğer bulabildiysek)
    if expected_user_id and author_id != expected_user_id:
        # --- DÜZELTİLMİŞ IF BLOĞU (Owner Check in endchat) ---
        # Bu blok içindeki kod zaten syntax olarak doğru görünüyor.
        await ctx.send("Bu kanalı sadece oluşturan kişi kapatabilir.", delete_after=10)
        try:
            # Komutu silmek için gecikme ekle ki kullanıcı mesajı görsün
            await ctx.message.delete(delay=10)
        except discord.errors.NotFound: pass # Mesaj zaten silinmiş olabilir
        except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .endchat komut mesajını (yetkisiz kullanım) silme izni yok.")
        except Exception as delete_e: logger.error(f".endchat komut mesajı (yetkisiz kullanım) silinirken hata: {delete_e}")
        return
        # --- DÜZELTME SONU ---

    # Botun kanalı silme izni var mı?
    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send("Kanalları yönetme iznim yok.", delete_after=10)
        return

    # Kanalı sil
    try:
        logger.info(f"Kanal '{ctx.channel.name}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
        # State temizliği on_guild_channel_delete tarafından yapılacak.
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' manuel silinirken bulunamadı. State temizleniyor.")
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
    except Exception as e:
        logger.error(f".endchat komutunda kanal silinirken hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Kanal silinirken bir hata oluştu.", delete_after=10)

@bot.event
async def on_guild_channel_delete(channel):
    """Bir kanal silindiğinde tetiklenir (state temizliği için)."""
    channel_id = channel.id
    # Sadece bizim yönettiğimiz geçici kanallarla ilgilen
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) silindi, tüm ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id)
        active_gemini_chats.pop(channel_id, None) # Aktif sohbeti sonlandır
        channel_last_active.pop(channel_id, None) # Aktivite takibini bırak
        warned_inactive_channels.discard(channel_id) # Uyarıldıysa listeden çıkar

        # Kullanıcı haritasını temizle
        user_id_to_remove = None
        # Kopya üzerinde iterasyon yapmaya gerek yok çünkü pop yapıyoruz, boyut değişebilir. list() kullanmak güvenli.
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id:
                user_id_to_remove = user_id
                break # Bir kanala sadece bir kullanıcı atanır
        if user_id_to_remove:
            user_to_channel_map.pop(user_id_to_remove, None)
            logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi.")
        else:
             logger.warning(f"Silinen kanal {channel_id} için kullanıcı haritasında eşleşme bulunamadı.")

        # Veritabanından sil
        remove_temp_channel_db(channel_id)

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx, amount: str = None):
    """Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç)."""
    if not ctx.guild.me.guild_permissions.manage_messages:
        await ctx.send("Mesajları silebilmem için 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10)
        return

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
        if amount.lower() == 'all':
            await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)...", delete_after=5)
            # Komut mesajını hemen silmeye çalış
            try: await ctx.message.delete()
            except: pass

            while True:
                # purge() 14 günden eski mesajları toplu silemez, ancak tek tek silebiliriz (çok yavaş olur).
                # Şimdilik bulk=True ile 14 gün sınırını kabul ediyoruz.
                deleted = await ctx.channel.purge(limit=100, check=check_not_pinned, bulk=True)
                deleted_count += len(deleted)
                if len(deleted) < 100: # 100'den az sildiyse muhtemelen son batch'ti
                    break
                await asyncio.sleep(1) # Rate limit'e takılmamak için küçük bir bekleme

            msg = f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi."
            if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
            await ctx.send(msg, delete_after=10)
        else:
            try:
                limit = int(amount)
                if limit <= 0: raise ValueError("Sayı pozitif olmalı.")
                # Kullanıcının komut mesajını da saydığımız için +1 ekliyoruz
                limit_with_command = limit + 1

                deleted = await ctx.channel.purge(limit=limit_with_command, check=check_not_pinned, bulk=True)
                # Silinenler listesinde komut mesajı varsa, onu sayma
                actual_deleted_count = len(deleted) - (1 if ctx.message in deleted else 0)

                msg = f"{actual_deleted_count} mesaj silindi."
                if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
                # Komut mesajı zaten purge ile silindiyse tekrar silmeye gerek yok.
                # Silinmediyse (örn. pinned ise), burada silmeye çalışalım.
                if ctx.message not in deleted:
                     try: await ctx.message.delete()
                     except: pass

                await ctx.send(msg, delete_after=5)

            except ValueError:
                 await ctx.send(f"Geçersiz sayı '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10)
                 try: await ctx.message.delete(delay=10)
                 except: pass
            except AssertionError: # Bu genellikle limit 0 veya negatifse çıkar ama ValueError ile yakalıyoruz
                 await ctx.send(f"Geçersiz sayı. Lütfen pozitif bir sayı girin.", delete_after=10)
                 try: await ctx.message.delete(delay=10)
                 except: pass
    except discord.errors.Forbidden:
        logger.error(f"HATA: '{ctx.channel.name}' kanalında silme izni yok!")
        await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10)
        # Komut mesajını silmeyi dene
        try: await ctx.message.delete(delay=10)
        except: pass
    except Exception as e:
        logger.error(f".clear hatası: {e}\n{traceback.format_exc()}")
        await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass


@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user) # Kullanıcı başına 5 saniyede 1 kullanım
async def ask_in_channel(ctx, *, question: str = None):
    """Sorulan soruyu Gemini'ye iletir ve yanıtı geçici olarak bu kanalda gösterir."""
    global model # Global varsayılan modeli kullanır
    if question is None:
        error_msg = await ctx.reply(f"Lütfen soru sorun (örn: `{ctx.prefix}ask Gemini nedir?`).", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru (.ask): {question[:100]}...")
    bot_response_message: discord.Message = None # Yanıt mesajını saklamak için

    try:
        async with ctx.typing():
            try:
                # Global modelin hala geçerli olduğunu varsayıyoruz (on_ready'de kontrol edildi)
                if model is None:
                    logger.error(".ask için global model yüklenmemiş/yok!")
                    await ctx.reply("Yapay zeka modeli şu anda kullanılamıyor.", delete_after=10)
                    try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                    except: pass
                    return
                # Blocking API çağrısını ayrı thread'de çalıştır
                response = await asyncio.to_thread(model.generate_content, question)

            except Exception as gemini_e:
                 logger.error(f".ask için Gemini API hatası: {gemini_e}")
                 error_str = str(gemini_e).lower()
                 user_msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."
                 if "api key not valid" in error_str or "permission_denied" in error_str: user_msg = "API Anahtarı sorunu."
                 elif "quota exceeded" in error_str or "429" in error_str: user_msg = "API kullanım limiti aşıldı."
                 elif "finish_reason: SAFETY" in str(response.prompt_feedback).upper() or "finish_reason: SAFETY" in str(getattr(response, 'candidates', [{}])[0].get('finish_reason', '')).upper(): user_msg = "Yanıt güvenlik filtrelerine takıldı."
                 await ctx.reply(user_msg, delete_after=10)
                 # Hata durumunda da kullanıcı mesajını silmeyi zamanla
                 try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                 except: pass
                 return

            # Yanıtı al
            gemini_response = response.text.strip()

        # Yanıt boş mu?
        if not gemini_response:
            logger.warning(f"Gemini'den .ask için boş yanıt alındı.")
            await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
            try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            except: pass
            return

        # Yanıtı Embed içinde göster
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)

        # Soru ve yanıtı embed alanlarına sığdır
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)

        response_display = gemini_response if len(gemini_response) <= 1024 else gemini_response[:1021] + "..."
        embed.add_field(name=" yanıt", value=response_display, inline=False) # " yanıt" başlığı biraz garip, "Yanıt" olabilir?

        embed.set_footer(text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra otomatik olarak silinecektir.")

        # Yanıtı gönder ve mesaj nesnesini sakla
        bot_response_message = await ctx.reply(embed=embed, mention_author=False) # Yanıt verirken ping atma
        logger.info(f".ask yanıtı gönderildi (Mesaj ID: {bot_response_message.id})")

        # Kullanıcının komut mesajını silmeyi zamanla
        try:
            await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            logger.info(f".ask komut mesajı ({ctx.message.id}) {MESSAGE_DELETE_DELAY} saniye sonra silinmek üzere zamanlandı.")
        except Exception as e:
            logger.warning(f".ask komut mesajı ({ctx.message.id}) silme zamanlanamadı: {e}")

        # Botun yanıt mesajını silmeyi zamanla
        try:
            await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY)
            logger.info(f".ask yanıt mesajı ({bot_response_message.id}) {MESSAGE_DELETE_DELAY} saniye sonra silinmek üzere zamanlandı.")
        except Exception as e:
            logger.warning(f".ask yanıt mesajı ({bot_response_message.id}) silme zamanlanamadı: {e}")

    except Exception as e:
        logger.error(f".ask genel hatası: {e}\n{traceback.format_exc()}")
        try:
             await ctx.reply("Sorunuz işlenirken beklenmedik bir hata oluştu.", delete_after=15)
             # Hata durumunda da kullanıcı mesajını silmeyi dene
             await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
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
        active_gemini_chats.pop(channel_id, None) # Oturumu sözlükten kaldır
        logger.info(f"Sohbet geçmişi {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi sıfırlandı. Bir sonraki mesajınızla yeni bir oturum başlayacak.", delete_after=15)
    else:
        # Zaten aktif oturum yoksa (örn. bot yeniden başlatıldıysa veya hiç konuşulmadıysa)
        logger.info(f"Sıfırlanacak aktif oturum yok: Kanal {channel_id}")
        await ctx.send("Aktif bir konuşma geçmişi bulunmuyor (zaten sıfır).", delete_after=10)

    # Komut mesajını sil
    try: await ctx.message.delete(delay=15)
    except: pass


@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user) # Sık kullanımı engelle
async def list_available_models(ctx):
    """Sohbet için kullanılabilir (metin tabanlı) Gemini modellerini listeler."""
    try:
         models_list = []
         await ctx.send("Kullanılabilir modeller kontrol ediliyor...", delete_after=5) # Geri bildirim
         async with ctx.typing():
             # API çağrısını thread'de yap
             available_models = await asyncio.to_thread(genai.list_models)
             for m in available_models:
                 # Sadece 'generateContent' destekleyen (sohbet/metin) ve vision olmayan modelleri alalım
                 is_chat_model = 'generateContent' in m.supported_generation_methods
                 # is_vision_model = 'vision' in m.name # Şimdilik vision modelleri filtrelemeyelim, belki ileride lazım olur?
                 # is_embedding_model = 'embedContent' in m.supported_generation_methods # Embedding modellerini filtrele

                 # Sadece sohbet edebilenleri alalım
                 if is_chat_model: # and not is_embedding_model: # Eğer embedding modelleri listelenmesin istenirse
                     model_id = m.name.split('/')[-1] # Sadece kısa adı göster:örn. gemini-1.5-flash-latest
                     # Önerilen modelleri işaretle
                     prefix = ""
                     if "gemini-1.5-flash" in model_id: prefix = "⚡ " # Flash için
                     elif "gemini-1.5-pro" in model_id: prefix = "✨ " # Pro için
                     elif "gemini-pro" == model_id: prefix = "✅ " # Eski stabil pro

                     models_list.append(f"{prefix}`{model_id}`")

         if not models_list:
             await ctx.send("Google API'den kullanılabilir sohbet modeli alınamadı veya bulunamadı.")
             return

         # Modelleri sırala (önce işaretliler, sonra diğerleri alfabetik)
         models_list.sort(key=lambda x: (not x.startswith(("\⚡", "✨", "✅")), x.lower()))

         embed = discord.Embed(
             title="🤖 Kullanılabilir Gemini Modelleri",
             description=f"Bir sonraki özel sohbetiniz için `.setmodel <ad>` komutu ile model seçebilirsiniz:\n\n" + "\n".join(models_list),
             color=discord.Color.gold()
         )
         embed.set_footer(text="⚡ Flash (Hızlı), ✨ Pro (Gelişmiş), ✅ Eski Pro")
         await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Modeller listelenirken hata: {e}")
        await ctx.send("Modeller listelenirken bir hata oluştu. API anahtarınızı veya bağlantınızı kontrol edin.")


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
    target_model_name = model_id if model_id.startswith("models/") else f"models/{model_id}"
    target_model_short_name = target_model_name.split('/')[-1] # Kısa adı al

    try:
        logger.info(f"{ctx.author.name} ({ctx.author.id}) bir sonraki sohbet için '{target_model_short_name}' modelini ayarlıyor...")
        # Modelin geçerli olup olmadığını kontrol et (API'ye sorarak)
        async with ctx.typing():
            # genai.get_model senkron bir çağrı, thread'e taşı
            await asyncio.to_thread(genai.get_model, target_model_name)
            # Başarılı olursa, sadece desteklenen modelleri kabul edebiliriz (listmodels gibi)
            # model_info = await asyncio.to_thread(genai.get_model, target_model_name)
            # if 'generateContent' not in model_info.supported_generation_methods:
            #     raise ValueError(f"Model '{target_model_short_name}' sohbet ('generateContent') desteklemiyor.")

        # Modeli kullanıcı için sakla (kısa adını saklamak yeterli)
        user_next_model[ctx.author.id] = target_model_short_name
        logger.info(f"{ctx.author.name} ({ctx.author.id}) için bir sonraki sohbet modeli '{target_model_short_name}' olarak ayarlandı.")
        await ctx.send(f"✅ Başarılı! Bir sonraki özel sohbetiniz `{target_model_short_name}` modeli ile başlayacak.", delete_after=20)
        try: await ctx.message.delete(delay=20)
        except: pass
    except Exception as e:
        logger.warning(f"{ctx.author.name} geçersiz/erişilemeyen model denedi ({target_model_name}): {e}")
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
        await ctx.send(f"Lütfen bir metin kanalı etiketleyin veya ID'sini yazın. Örn: `{ctx.prefix}setentrychannel #sohbet-baslat`")
        return

    # Kanalın gerçekten bir metin kanalı olduğunu teyit et
    if not isinstance(channel, discord.TextChannel):
         await ctx.send("Lütfen geçerli bir metin kanalı belirtin.")
         return

    entry_channel_id = channel.id
    save_config('entry_channel_id', entry_channel_id) # Ayarı DB'ye kaydet
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
    if hours is None or hours <= 0:
        await ctx.send(f"Lütfen pozitif bir saat değeri girin (örn: `{ctx.prefix}settimeout 2.5` -> 2.5 saat). Mevcut: `{inactivity_timeout.total_seconds()/3600 if inactivity_timeout else 'Ayarlanmamış'}` saat")
        return

    # Çok kısa veya çok uzun süreleri engellemek isteyebiliriz
    if hours < 0.1:
         await ctx.send("Minimum zaman aşımı 0.1 saattir (6 dakika).")
         return
    if hours > 168: # 1 hafta
         await ctx.send("Maksimum zaman aşımı 168 saattir (1 hafta).")
         return


    inactivity_timeout = datetime.timedelta(hours=hours)
    save_config('inactivity_timeout_hours', str(hours)) # Ayarı DB'ye kaydet
    logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours} saat olarak ayarlandı.")
    await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla **{hours} saat** olarak ayarlandı.")


@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx):
    """Botun kullanılabilir komutlarını listeler."""
    entry_channel_mention = "Ayarlanmamış"
    if entry_channel_id:
        entry_channel = bot.get_channel(entry_channel_id)
        entry_channel_mention = f"<#{entry_channel_id}>" if entry_channel else f"ID: {entry_channel_id} (Bulunamadı)"

    embed = discord.Embed(
        title=f"{bot.user.name} Komut Listesi",
        description=f"**Özel Sohbet Başlatma:**\n"
                    f"{entry_channel_mention} kanalına herhangi bir mesaj yazarak yapay zeka ile özel sohbet başlatabilirsiniz.\n\n"
                    f"**Diğer Komutlar:**\n"
                    f"Aşağıdaki komutları `{ctx.prefix}` ön eki ile kullanabilirsiniz.",
        color=discord.Color.dark_purple()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)

    # Komutları gruplandırabiliriz (Genel, Sohbet, Yönetim vb.)
    user_commands = []
    chat_commands = []
    admin_commands = []

    # Komutları al ve sırala
    all_commands = sorted(bot.commands, key=lambda cmd: cmd.name)

    for command in all_commands:
        # Gizli komutları atla
        if command.hidden: continue

        # Kullanıcının komutu çalıştırıp çalıştıramayacağını kontrol et
        try:
            # Asenkron check'leri çalıştırmak için context gerekli
            can_run = await command.can_run(ctx)
            if not can_run: continue
        except commands.CheckFailure: # Check başarısız olursa atla
            continue
        except Exception as e: # Diğer hatalarda logla ama yine de atla
             logger.warning(f"Komut '{command.name}' için can_run kontrolünde hata: {e}")
             continue


        # Komut bilgilerini formatla
        help_text = command.help or command.short_doc or "Açıklama yok."
        aliases = f" (Diğer adlar: `{'`, `'.join(command.aliases)}`)" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""
        cmd_string = f"`{ctx.prefix}{command.name}{params}`{aliases}\n{help_text}"

        # Kategoriye ayır (izinlere göre basitçe)
        if any(check.__qualname__.startswith('has_permissions') or check.__qualname__.startswith('is_owner') for check in command.checks):
             # Check decorator'larında 'administrator=True' veya 'manage_guild=True' gibi daha spesifik kontroller yapılabilir
             # Şimdilik has_permissions içerenleri admin sayalım
             if 'administrator=True' in str(command.checks): # Daha spesifik kontrol
                 admin_commands.append(cmd_string)
             else: # Diğer izin gerektirenler (örn. manage_messages)
                 # Bunu da admin'e ekleyebiliriz veya ayrı bir 'Moderatör' grubu? Şimdilik Admin.
                 admin_commands.append(cmd_string)

        elif command.name in ['endchat', 'resetchat', 'clear']: # Geçici sohbet kanalı komutları
            chat_commands.append(cmd_string)
        else: # Genel kullanıcı komutları
            user_commands.append(cmd_string)


    if user_commands:
        embed.add_field(name="👤 Genel Komutlar", value="\n\n".join(user_commands), inline=False)
    if chat_commands:
        embed.add_field(name="💬 Sohbet Kanalı Komutları", value="\n\n".join(chat_commands), inline=False)
    if admin_commands:
         embed.add_field(name="🛠️ Yönetici Komutları", value="\n\n".join(admin_commands), inline=False)


    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)}")
    try:
        await ctx.send(embed=embed)
    except discord.errors.HTTPException as e:
         logger.error(f"Yardım mesajı gönderilemedi (çok uzun olabilir): {e}")
         await ctx.send("Komut listesi çok uzun olduğu için gönderilemedi.")


# --- Genel Hata Yakalama ---
@bot.event
async def on_command_error(ctx, error):
    """Komutlarla ilgili hataları merkezi olarak yakalar ve kullanıcıya bilgi verir."""
    # Orijinal hatayı al (eğer başka bir hataya sarılmışsa)
    original_error = getattr(error, 'original', error)
    delete_user_msg = True  # Varsayılan olarak kullanıcının komut mesajını sil
    delete_delay = 10       # Mesajların silinme gecikmesi (saniye)

    # Bilinen ve görmezden gelinebilecek hatalar
    if isinstance(original_error, commands.CommandNotFound):
        logger.debug(f"Bilinmeyen komut denendi: {ctx.message.content}")
        return # Bilinmeyen komutları sessizce geç

    # İzin Hataları
    if isinstance(original_error, commands.MissingPermissions):
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' komutu için gerekli izinlere sahip değil: {original_error.missing_permissions}")
        perms = ", ".join(p.replace('_', ' ').title() for p in original_error.missing_permissions)
        delete_delay = 15
        await ctx.send(f"⛔ Üzgünüm {ctx.author.mention}, bu komutu kullanmak için şu izin(ler)e sahip olmalısın: **{perms}**", delete_after=delete_delay)
    elif isinstance(original_error, commands.BotMissingPermissions):
        logger.error(f"Bot, '{ctx.command.qualified_name}' komutunu çalıştırmak için gerekli izinlere sahip değil: {original_error.missing_permissions}")
        perms = ", ".join(p.replace('_', ' ').title() for p in original_error.missing_permissions)
        delete_delay = 15
        delete_user_msg = False # Botun hatası, kullanıcının mesajı kalsın
        await ctx.send(f"🆘 Bu komutu çalıştırabilmem için benim şu izin(ler)e sahip olmam gerekiyor: **{perms}**", delete_after=delete_delay)

    # Kullanım Yeri Hataları
    elif isinstance(original_error, commands.NoPrivateMessage):
        logger.warning(f"'{ctx.command.qualified_name}' komutu DM'de kullanılmaya çalışıldı.")
        delete_user_msg = False # DM'de mesajı silemeyiz zaten
        try: await ctx.author.send("Bu komut sadece sunucu kanallarında kullanılabilir.")
        except discord.errors.Forbidden: pass # DM kapalıysa yapacak bir şey yok
    elif isinstance(original_error, commands.PrivateMessageOnly):
        logger.warning(f"'{ctx.command.qualified_name}' komutu sunucuda kullanılmaya çalışıldı.")
        delete_delay = 10
        await ctx.send("Bu komut sadece özel mesajla (DM) kullanılabilir.", delete_after=delete_delay)
    elif isinstance(original_error, commands.CheckFailure):
        # Genel kontrol hatası (örn. @commands.is_owner() veya özel check decorator'ları)
        logger.warning(f"Komut kontrolü başarısız oldu: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {original_error}")
        delete_delay = 10
        # Kullanıcıya genel bir mesaj verilebilir veya sessiz kalınabilir
        await ctx.send("🚫 Bu komutu kullanma yetkiniz yok veya koşullar sağlanmıyor.", delete_after=delete_delay)

    # Bekleme Süresi Hatası
    elif isinstance(original_error, commands.CommandOnCooldown):
        delete_delay = max(5, int(original_error.retry_after) + 1) # En az 5sn veya cooldown + 1sn göster
        await ctx.send(f"⏳ Bu komut beklemede. Lütfen **{original_error.retry_after:.1f} saniye** sonra tekrar deneyin.", delete_after=delete_delay)

    # Argüman/Kullanım Hataları
    elif isinstance(original_error, commands.UserInputError): # BadArgument, MissingRequiredArgument vb. bunun alt sınıfı
         delete_delay = 15
         command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with
         usage = f"`{ctx.prefix}{command_name}{ctx.command.signature if ctx.command else ''}`"
         error_message = "Hatalı komut kullanımı."
         if isinstance(original_error, commands.MissingRequiredArgument):
              error_message = f"Eksik argüman: `{original_error.param.name}`."
         elif isinstance(original_error, commands.BadArgument):
              error_message = f"Geçersiz argüman türü: {original_error}"
         elif isinstance(original_error, commands.TooManyArguments):
              error_message = "Çok fazla argüman girdiniz."
         # Diğer UserInputError türleri eklenebilir

         await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay)

    # Diğer Beklenmedik Hatalar
    else:
        logger.error(f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' komutu işlenirken beklenmedik bir hata oluştu: {original_error}")
        traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__))
        logger.error(f"Traceback:\n{traceback_str}")
        delete_delay = 15
        # Kullanıcıya sadece genel bir hata mesajı göster
        await ctx.send("⚙️ Komut işlenirken beklenmedik bir hata oluştu. Sorun devam ederse lütfen geliştiriciye bildirin.", delete_after=delete_delay)

    # Kullanıcının komut mesajını sil (eğer ayarlandıysa)
    if delete_user_msg and ctx.guild: # Sadece sunucudaysa silmeyi dene
        try:
            await ctx.message.delete(delay=delete_delay)
        except discord.errors.NotFound: pass # Mesaj zaten silinmiş olabilir
        except discord.errors.Forbidden: pass # Silme izni yoksa yapacak bir şey yok
        except Exception as e: logger.warning(f"Hata sonrası komut mesajı ({ctx.message.id}) silinirken ek hata: {e}")


# === KOYEB İÇİN BASİT WEB SUNUCUSU ===
app = Flask(__name__)
@app.route('/')
def home():
    """Basit bir sağlık kontrolü endpoint'i."""
    # Botun hazır olup olmadığını kontrol et
    if bot and bot.is_ready():
        # Belki daha fazla bilgi döndürebiliriz (sunucu sayısı vb.)
        guild_count = len(bot.guilds)
        return f"EYAY sorunsuz çalışıyor. {guild_count} sunucuda aktif.", 200
    elif bot and not bot.is_ready():
        # Bot çalışıyor ama henüz tam hazır değil (login oldu ama on_ready bekleniyor)
        return "Bot başlatılıyor, henüz hazır değil...", 503 # Service Unavailable
    else:
        # Bot nesnesi hiç yoksa veya beklenmedik bir durumdaysa
        return "Bot durumu bilinmiyor veya başlatılamadı.", 500 # Internal Server Error

def run_webserver():
    """Flask web sunucusunu ayrı bir thread'de çalıştırır."""
    # Koyeb genellikle PORT ortam değişkenini ayarlar
    port = int(os.environ.get("PORT", 8080)) # Varsayılan 8080
    # Host 0.0.0.0 olarak ayarlanmalı ki container dışından erişilebilsin
    try:
        logger.info(f"Flask web sunucusu http://0.0.0.0:{port} adresinde başlatılıyor...")
        # Flask'ın geliştirme sunucusunu kullan (production için Gunicorn vb. daha uygun olabilir ama bu basitlik için yeterli)
        # log seviyesini ayarlayarak gereksiz logları azalt
        # logging.getLogger('werkzeug').setLevel(logging.ERROR) # Zaten yukarıda yapıldı
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        logger.error(f"Web sunucusu başlatılırken kritik hata: {e}")
# ===================================


# --- Botu Çalıştır ---
if __name__ == "__main__":
    logger.info("Bot başlatılıyor...")
    try:
        # Web sunucusunu ayrı bir thread'de başlat (daemon=True ile ana program bitince otomatik kapanır)
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()
        # Başladığını logla (port bilgisi run_webserver içinde loglanıyor)

        # Discord botunu ana thread'de çalıştır
        # Kendi logger'ımızı kullandığımız için discord.py'nin varsayılan log handler'ını devre dışı bırakıyoruz (log_handler=None)
        bot.run(DISCORD_TOKEN, log_handler=None)

    except discord.errors.LoginFailure:
        logger.critical("HATA: Geçersiz Discord Token! Bot başlatılamadı.")
    except discord.errors.PrivilegedIntentsRequired:
         logger.critical("HATA: Gerekli Intent'ler (örn. Server Members) Discord Developer Portal'da etkinleştirilmemiş!")
    except Exception as e:
        logger.critical(f"Bot çalıştırılırken kritik bir hata oluştu: {e}")
        logger.critical(traceback.format_exc())
    finally:
        # Bot kapatıldığında (Ctrl+C vb.) veya bir hata ile durduğunda çalışır
        logger.info("Bot kapatılıyor...")
        # Gerekirse burada temizleme işlemleri yapılabilir (örn. DB bağlantısını kapatmak - ama zaten her işlemde aç/kapa yapılıyor)