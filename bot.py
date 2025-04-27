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

# --- Logging Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('discord_gemini_bot')

# .env dosyasındaki değişkenleri yükle
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Varsayılan değerler (veritabanından okunamazsa kullanılır)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID") # .env'den ilk okuma
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
DEFAULT_MODEL_NAME = 'models/gemini-1.5-flash-latest'
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye) (10 dakika)

# --- Veritabanı Ayarları ---
DB_FILE = 'bot_data.db'

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
        # Yapılandırma tablosu (Bu doğru, parametre yok)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # ---> TEMP_CHANNELS TABLOSU DÜZELTMESİ <---
        # Varsayılan değeri f-string ile doğrudan ekle ve parametreyi kaldır
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                last_active TIMESTAMP NOT NULL,
                model_name TEXT DEFAULT '{DEFAULT_MODEL_NAME}'
            )
        ''') # İkinci parametre olan tuple kaldırıldı!
        # ---> DÜZELTME SONU <---

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
    except Exception as e:
        logger.error(f"Yapılandırma kaydedilirken hata (Key: {key}): {e}")

def load_config(key, default=None):
    """Yapılandırma ayarını veritabanından yükler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else default
    except Exception as e:
        logger.error(f"Yapılandırma yüklenirken hata (Key: {key}): {e}")
        return default # Hata durumunda varsayılanı döndür

def load_all_temp_channels():
    """Tüm geçici kanal durumlarını veritabanından yükler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, user_id, last_active, model_name FROM temp_channels")
        channels = cursor.fetchall()
        conn.close()
        # Zaman damgalarını datetime nesnesine çevir (hata toleranslı)
        loaded_data = []
        for ch in channels:
            try:
                last_active_dt = datetime.datetime.fromisoformat(ch[2])
                loaded_data.append((ch[0], ch[1], last_active_dt, ch[3] or DEFAULT_MODEL_NAME))
            except (ValueError, TypeError) as ts_error:
                logger.error(f"Geçici kanal DB'sinde geçersiz zaman damgası (channel_id: {ch[0]}): {ts_error} - Değer: {ch[2]}")
        return loaded_data
    except Exception as e:
        logger.error(f"Geçici kanallar yüklenirken veritabanı hatası: {e}")
        return [] # Hata durumunda boş liste döndür

def add_temp_channel_db(channel_id, user_id, timestamp, model_used):
    """Yeni geçici kanalı veritabanına ekler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("REPLACE INTO temp_channels (channel_id, user_id, last_active, model_name) VALUES (?, ?, ?, ?)",
                       (channel_id, user_id, timestamp.isoformat(), model_used))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Geçici kanal DB'ye eklenirken hata (channel_id: {channel_id}): {e}")


def update_channel_activity_db(channel_id, timestamp):
    """Kanalın son aktivite zamanını veritabanında günceller."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("UPDATE temp_channels SET last_active = ? WHERE channel_id = ?",
                       (timestamp.isoformat(), channel_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Kanal aktivitesi DB'de güncellenirken hata (channel_id: {channel_id}): {e}")

def remove_temp_channel_db(channel_id):
    """Geçici kanalı veritabanından siler."""
    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM temp_channels WHERE channel_id = ?", (channel_id,))
        conn.commit()
        conn.close()
        logger.info(f"Geçici kanal {channel_id} veritabanından silindi.")
    except Exception as e:
        logger.error(f"Geçici kanal DB'den silinirken hata (channel_id: {channel_id}): {e}")


# --- Yapılandırma Kontrolleri (Veritabanı ile birlikte) ---
setup_database() # Bot başlarken veritabanını hazırla

# Ayarları veritabanından yükle, yoksa .env veya varsayılanı kullan
entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))

if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); exit()
if not GEMINI_API_KEY: logger.critical("HATA: Gemini API Anahtarı bulunamadı!"); exit()
if not entry_channel_id_str: logger.critical("HATA: Giriş Kanalı ID'si (ENTRY_CHANNEL_ID) .env veya veritabanında bulunamadı!"); exit()

try:
    entry_channel_id = int(entry_channel_id_str)
    logger.info(f"Giriş Kanalı ID'si: {entry_channel_id}")
except (ValueError, TypeError):
    logger.critical(f"HATA: Giriş Kanalı ID'si ('{entry_channel_id_str}') geçerli bir sayı değil!"); exit()

try:
    inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str))
    logger.info(f"İnaktivite Zaman Aşımı: {inactivity_timeout}")
except (ValueError, TypeError):
    logger.error(f"HATA: İnaktivite süresi ('{inactivity_timeout_hours_str}') geçerli bir sayı değil! Varsayılan {DEFAULT_INACTIVITY_TIMEOUT_HOURS} saat kullanılıyor.")
    inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)


# Gemini API'yi yapılandır ve global modeli (varsayılanı) oluştur
model = None
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model_name = DEFAULT_MODEL_NAME # Bu da config'den okunabilir ileride
    try:
        genai.get_model(model_name)
        logger.info(f"Gemini API başarıyla yapılandırıldı. Varsayılan model: {model_name}")
        model = genai.GenerativeModel(model_name)
        logger.info(f"Global varsayılan Gemini modeli ('{model_name}') başarıyla oluşturuldu.")
    except Exception as model_error:
         logger.critical(f"HATA: Varsayılan Gemini modeli ({model_name}) bulunamadı: {model_error}. Bot başlatılamıyor.")
         exit()
except Exception as e:
    logger.critical(f"HATA: Gemini API genel yapılandırma hatası: {e}. Bot başlatılamıyor.")
    exit()

if model is None: logger.critical("HATA: Global Gemini modeli oluşturulamadı."); exit()


# --- Bot Kurulumu ---
intents = discord.Intents.default(); intents.message_content = True; intents.members = True; intents.messages = True; intents.guilds = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Global Durum Yönetimi (Veritabanı sonrası) ---
# active_gemini_chats, temporary_chat_channels, user_to_channel_map, channel_last_active
# on_ready içinde veritabanından yüklenecek.
# user_next_model (bu geçici, DB'ye yazmaya gerek yok)
# warned_inactive_channels (bunu da şimdilik bellekte tutalım)


# --- Yardımcı Fonksiyonlar ---

async def create_private_chat_channel(guild, author):
    """Verilen kullanıcı için özel sohbet kanalı oluşturur ve kanal nesnesini döndürür."""
    if not guild.me.guild_permissions.manage_channels: logger.warning(f"'{guild.name}' sunucusunda 'Kanalları Yönet' izni eksik."); return None
    safe_username = "".join(c for c in author.display_name if c.isalnum() or c == ' ').strip().replace(' ', '-').lower()
    if not safe_username: safe_username = "kullanici"
    safe_username = safe_username[:80]

    base_channel_name = f"sohbet-{safe_username}"
    channel_name = base_channel_name
    counter = 1
    existing_channel_names = {ch.name.lower() for ch in guild.text_channels}

    while channel_name.lower() in existing_channel_names or len(channel_name) > 100:
        channel_name = f"{base_channel_name}-{counter}"
        counter += 1
        # --- DÜZELTİLMİŞ IF BLOĞU (Counter > 1000) ---
        if counter > 1000:
            logger.error(f"{author.name} için benzersiz kanal adı bulunamadı (1000 deneme aşıldı).")
            channel_name = f"{base_channel_name}-{datetime.datetime.now().strftime('%M%S')}"
            if channel_name.lower() in existing_channel_names:
                logger.error(f"Alternatif kanal adı '{channel_name}' de mevcut. Kanal oluşturulamıyor.")
                return None
            logger.warning(f"Alternatif kanal adı kullanılıyor: {channel_name}")
            break
        # --- DÜZELTME SONU ---

    logger.info(f"Oluşturulacak kanal adı: {channel_name}")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True)
    }
    # Yönetici rol engelleme (isteğe bağlı)
    # admin_role_ids_str = load_config("admin_role_ids", "")
    # if admin_role_ids_str: ... (ID'leri alıp overwrites'a ekle) ...

    try:
        new_channel = await guild.create_text_channel(channel_name, overwrites=overwrites, reason=f"{author.name} için otomatik Gemini sohbet kanalı.")
        logger.info(f"Kullanıcı {author.name} ({author.id}) için '{channel_name}' (ID: {new_channel.id}) kanalı otomatik oluşturuldu.")
        return new_channel
    except Exception as e: logger.error(f"Kanal oluşturmada hata: {e}\n{traceback.format_exc()}"); return None


async def send_to_gemini_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    """Belirtilen kanalda Gemini'ye mesaj gönderir ve yanıtlar. Sohbet oturumunu kullanır."""
    global channel_last_active

    if not prompt_text.strip(): return False

    if channel_id not in active_gemini_chats:
        try:
            # Kanalın modelini DB'den oku
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = ?", (channel_id,))
            result = cursor.fetchone()
            conn.close()
            current_model_name = result[0] if result and result[0] else DEFAULT_MODEL_NAME
            logger.info(f"'{channel.name}' (ID: {channel_id}) için Gemini sohbet oturumu {current_model_name} ile başlatılıyor.")
            chat_model_instance = genai.GenerativeModel(current_model_name)
            active_gemini_chats[channel_id] = {'session': chat_model_instance.start_chat(history=[]), 'model': current_model_name}
        except Exception as e:
            logger.error(f"'{channel.name}' için Gemini sohbet oturumu başlatılamadı: {e}")
            try: await channel.send("Üzgünüm, yapay zeka oturumunu başlatırken bir sorun oluştu.", delete_after=15)
            except: pass
            return False

    chat_data = active_gemini_chats[channel_id]
    chat_session = chat_data['session']
    current_model_name = chat_data['model']

    logger.info(f"[GEMINI CHAT/{current_model_name}] [{author.name} @ {channel.name}] gönderiyor: {prompt_text[:100]}{'...' if len(prompt_text)>100 else ''}")

    async with channel.typing():
        try:
            response = await chat_session.send_message_async(prompt_text)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[channel_id] = now_utc # Bellekteki zamanı güncelle
            update_channel_activity_db(channel_id, now_utc) # Veritabanını güncelle
            gemini_response = response.text.strip()

            if not gemini_response: logger.warning(f"[GEMINI CHAT/{current_model_name}] Boş yanıt alındı (Kanal: {channel_id}).")
            elif len(gemini_response) > 2000:
                logger.info(f"Yanıt 2000 karakterden uzun (Kanal: {channel_id}), parçalanıyor...")
                for i in range(0, len(gemini_response), 2000): await channel.send(gemini_response[i:i+2000])
            else: await channel.send(gemini_response)
            return True

        except Exception as e:
            logger.error(f"[GEMINI CHAT/{current_model_name}] API ile iletişimde sorun oluştu (Kanal: {channel_id}): {e}")
            error_str = str(e).lower(); user_msg = "Üzgünüm, yapay zeka ile konuşurken bir sorunla karşılaştım."
            if "api key not valid" in error_str or "permission_denied" in error_str or "403" in error_str: user_msg = "API Anahtarı ile ilgili bir sorun var."
            elif "quota exceeded" in error_str or "resource_exhausted" in error_str or "429" in error_str: user_msg = "API kullanım limiti aşıldı."
            elif "stopcandidateexception" in error_str or "finish_reason: SAFETY" in error_str.upper(): user_msg = "Yanıt, güvenlik filtreleri nedeniyle engellendi."
            elif "400" in error_str or "invalid argument" in error_str: user_msg = "Gönderilen istekte bir sorun var."
            try: await channel.send(user_msg, delete_after=15)
            except: pass
            return False
        except discord.errors.HTTPException as e:
            logger.error(f"Discord'a mesaj gönderilemedi (Kanal: {channel_id}): {e}")
            return False


# --- Bot Olayları ---

@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, model_name
    global temporary_chat_channels, user_to_channel_map, channel_last_active

    logger.info(f'{bot.user} olarak giriş yapıldı.')

    # Ayarları yükle
    entry_channel_id_str = load_config('entry_channel_id', DEFAULT_ENTRY_CHANNEL_ID)
    inactivity_timeout_hours_str = load_config('inactivity_timeout_hours', str(DEFAULT_INACTIVITY_TIMEOUT_HOURS))
    try: entry_channel_id = int(entry_channel_id_str) if entry_channel_id_str else None
    except (ValueError, TypeError): logger.error(f"DB'den yüklenen ENTRY_CHANNEL_ID geçersiz: {entry_channel_id_str}."); entry_channel_id = int(DEFAULT_ENTRY_CHANNEL_ID) if DEFAULT_ENTRY_CHANNEL_ID else None
    try: inactivity_timeout = datetime.timedelta(hours=float(inactivity_timeout_hours_str))
    except (ValueError, TypeError): logger.error(f"DB'den yüklenen inactivity_timeout_hours geçersiz: {inactivity_timeout_hours_str}. Varsayılan {DEFAULT_INACTIVITY_TIMEOUT_HOURS} saat kullanılıyor."); inactivity_timeout = datetime.timedelta(hours=DEFAULT_INACTIVITY_TIMEOUT_HOURS)

    logger.info(f"Ayarlar yüklendi - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id:
         logger.warning("Giriş Kanalı ID'si ayarlanmamış. Otomatik kanal oluşturma çalışmayacak!")

    # Kalıcı verileri yükle
    logger.info("Kalıcı veriler veritabanından yükleniyor...")
    # Önce mevcut bellek verilerini temizle (yeniden başlatma durumu için)
    temporary_chat_channels.clear(); user_to_channel_map.clear(); channel_last_active.clear(); active_gemini_chats.clear()
    loaded_channels = load_all_temp_channels()
    valid_channel_count = 0
    invalid_channel_ids = []
    for ch_id, u_id, last_active_ts, ch_model_name in loaded_channels:
        channel_obj = bot.get_channel(ch_id)
        if channel_obj and isinstance(channel_obj, discord.TextChannel):
            temporary_chat_channels.add(ch_id)
            user_to_channel_map[u_id] = ch_id
            channel_last_active[ch_id] = last_active_ts
            # active_gemini_chats'ı burada doldurmuyoruz, ilk mesajda oluşturulacak
            valid_channel_count += 1
        else:
            logger.warning(f"Veritabanında bulunan geçici kanal (ID: {ch_id}) Discord'da bulunamadı/geçersiz. DB'den siliniyor.")
            invalid_channel_ids.append(ch_id)
    for invalid_id in invalid_channel_ids: remove_temp_channel_db(invalid_id)
    logger.info(f"{valid_channel_count} adet aktif geçici kanal durumu veritabanından yüklendi.")

    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif.")
    entry_channel_name = "Bilinmeyen Kanal"
    try:
        entry_channel = bot.get_channel(entry_channel_id) if entry_channel_id else None
        if entry_channel: entry_channel_name = f"#{entry_channel.name}"
        elif entry_channel_id: logger.warning(f"Giriş Kanalı (ID: {entry_channel_id}) bulunamadı veya erişilemiyor.")
        else: entry_channel_name = "Ayarlanmadı"
        await bot.change_presence(activity=discord.Game(name=f"Sohbet için {entry_channel_name}"))
    except Exception as e: logger.warning(f"Bot aktivitesi ayarlanamadı: {e}")

    if not check_inactivity.is_running():
         check_inactivity.start()
         logger.info("İnaktivite kontrol görevi başlatıldı.")
    logger.info("Bot komutları ve mesajları dinliyor...")
    print("-" * 20)

@bot.event
async def on_message(message):
    """Bir mesaj alındığında çalışacak fonksiyon."""
    if message.author == bot.user or message.author.bot: return
    if isinstance(message.channel, discord.DMChannel): return
    if entry_channel_id is None: return # Giriş kanalı ayarlanmadıysa devam etme

    author = message.author; author_id = author.id
    channel = message.channel; channel_id = channel.id
    guild = message.guild

    await bot.process_commands(message)
    ctx = await bot.get_context(message)
    if ctx.valid: return

    # --- Otomatik Kanal Oluşturma ve İlk Mesajı Yönlendirme ---
    if channel_id == entry_channel_id:
        if author_id in user_to_channel_map:
            active_channel_id = user_to_channel_map[author_id]; active_channel = bot.get_channel(active_channel_id)
            mention = f"<#{active_channel_id}>" if active_channel else f"(ID: {active_channel_id})"
            logger.info(f"{author.name} giriş kanalına yazdı ama zaten aktif kanalı var: {mention}")
            try: info_msg = await channel.send(f"{author.mention}, zaten aktif bir özel sohbet kanalın var: {mention}", delete_after=15); await message.delete()
            except Exception as e: logger.warning(f"Giriş kanalına 'zaten kanal var' bildirimi/silme hatası: {e}")
            return

        initial_prompt = message.content; original_message_id = message.id
        if not initial_prompt.strip():
             logger.info(f"{author.name} giriş kanalına boş mesaj gönderdi, yoksayılıyor.")
             try: await message.delete()
             except: pass # Hata önemli değil
             return

        chosen_model_name = user_next_model.pop(author_id, DEFAULT_MODEL_NAME) # Seçilen modeli al, yoksa varsayılan
        logger.info(f"{author.name} giriş kanalına ({channel.name}) yazdı, {chosen_model_name} modeli ile özel kanal oluşturuluyor...")
        new_channel = await create_private_chat_channel(guild, author)

        if new_channel:
            new_channel_id = new_channel.id; temporary_chat_channels.add(new_channel_id); user_to_channel_map[author_id] = new_channel_id
            now_utc = datetime.datetime.now(datetime.timezone.utc); channel_last_active[new_channel_id] = now_utc
            add_temp_channel_db(new_channel_id, author_id, now_utc, chosen_model_name) # DB'ye ekle

            # Yeni kanala Embed olarak hoşgeldin mesajı
            try:
                embed = discord.Embed(title="👋 Özel Gemini Sohbeti Başlatıldı!", description=(f"Merhaba {author.mention}!\n\nBu kanalda `{chosen_model_name}` modeli ile sohbet edeceksin."), color=discord.Color.og_blurple())
                embed.set_thumbnail(url=bot.user.display_avatar.url)
                embed.add_field(name="⏳ Otomatik Kapanma", value=f"Bu kanal `{inactivity_timeout.total_seconds() / 3600:.1f}` saat boyunca işlem görmezse otomatik olarak silinecektir.", inline=False)
                embed.add_field(name="🛑 Manuel Kapatma", value=f"Sohbeti daha erken bitirmek için `{bot.command_prefix[0]}endchat` yaz.", inline=True)
                embed.add_field(name="🔄 Modeli Değiştir", value=f"`{bot.command_prefix[0]}setmodel <ad>` ile sonraki sohbet modelini seç.", inline=True)
                embed.add_field(name="💬 Geçmişi Sıfırla", value=f"`{bot.command_prefix[0]}resetchat` ile geçmişi temizle.", inline=True)
                await new_channel.send(embed=embed)

            except Exception as e:
                # --- DÜZELTİLMİŞ EXCEPT BLOĞU (Embed Gönderilemedi) ---
                logger.warning(f"Yeni kanala ({new_channel_id}) hoşgeldin embed'i gönderilemedi: {e}")
                # Fallback: Embed gönderilemezse basit metin gönder
                try:
                     await new_channel.send(f"Merhaba {author.mention}! Özel sohbet kanalın oluşturuldu. `{bot.command_prefix[0]}endchat` ile kapatabilirsin.") # Fallback mesajı güncellendi
                except Exception as fallback_e:
                     logger.error(f"Yeni kanala ({new_channel_id}) fallback hoşgeldin mesajı da gönderilemedi: {fallback_e}")
                # --- DÜZELTME SONU ---         
                
            # Kullanıcıyı giriş kanalında bilgilendir
            try: await channel.send(f"{author.mention}, senin için özel sohbet kanalı {new_channel.mention} oluşturuldu!", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına bildirim gönderilemedi: {e}")

            # Kullanıcının orijinal mesajını yeni kanala gönder
            try: await new_channel.send(f"**{author.display_name}:** {initial_prompt}"); logger.info(f"İlk mesaj ({original_message_id}) geçici kanala ({new_channel_id}) kopyalandı.")
            except Exception as e: logger.error(f"İlk mesaj geçici kanala kopyalanamadı: {e}")

            # İlk Mesajı Gemini'ye Gönder (Yanıt için)
            logger.info(f"-----> GEMINI'YE İLK İSTEK GÖNDERİLİYOR (Kanal: {new_channel_id}, Model: {chosen_model_name})")
            success = await send_to_gemini_and_respond(new_channel, author, initial_prompt, new_channel_id)
            if success: logger.info(f"-----> GEMINI'YE İLK İSTEK BAŞARIYLA GÖNDERİLDİ (Kanal: {new_channel_id})")
            else: logger.warning(f"-----> GEMINI'YE İLK İSTEK BAŞARISIZ OLDU (Kanal: {new_channel_id})")

            # Giriş kanalındaki orijinal mesajı sil
            try: msg_to_delete = await channel.fetch_message(original_message_id); await msg_to_delete.delete(); logger.info(f"{author.name}'in giriş kanalındaki mesajı ({original_message_id}) silindi.")
            except Exception as e: logger.warning(f"Giriş kanalındaki mesaj {original_message_id} silinemedi: {e}")
        else:
            try: await channel.send(f"{author.mention}, üzgünüm, özel sohbet kanalı oluştururken bir sorun oluştu.", delete_after=20)
            except Exception as e: logger.warning(f"Giriş kanalına hata mesajı gönderilemedi: {e}")
        return

    # --- Geçici Kanallarda Devam Eden Gemini Sohbeti ---
    if channel_id in temporary_chat_channels and not message.author.bot:
        prompt_text = message.content
        await send_to_gemini_and_respond(channel, author, prompt_text, channel_id)


# --- Arka Plan Görevi: İnaktivite Kontrolü ---
@tasks.loop(minutes=5)
async def check_inactivity():
    """Aktif olmayan geçici kanalları kontrol eder ve siler."""
    global warned_inactive_channels
    if inactivity_timeout is None: logger.debug("İnaktivite zaman aşımı ayarlanmadığı için kontrol atlandı."); return

    now = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []; channels_to_warn = []
    warning_threshold = inactivity_timeout - datetime.timedelta(minutes=5)

    for channel_id, last_active_time in list(channel_last_active.items()):
        time_inactive = now - last_active_time
        if time_inactive > inactivity_timeout: channels_to_delete.append(channel_id)
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels: channels_to_warn.append(channel_id)

    for channel_id in channels_to_warn:
        channel_obj = bot.get_channel(channel_id)
        if channel_obj:
            try:
                remaining_time = inactivity_timeout - (now - channel_last_active[channel_id])
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal yaklaşık **{remaining_minutes} dakika** içinde aktif olmazsa otomatik olarak silinecektir.", delete_after=300)
                warned_inactive_channels.add(channel_id)
                logger.info(f"İnaktivite uyarısı gönderildi: Kanal ID {channel_id}")
            except Exception as e: logger.warning(f"İnaktivite uyarısı gönderilemedi (Kanal: {channel_id}): {e}")
        else: warned_inactive_channels.discard(channel_id)

    if channels_to_delete: logger.info(f"İnaktivite zaman aşımı: {len(channels_to_delete)} kanal silinecek: {channels_to_delete}")
    for channel_id in channels_to_delete:
        channel_to_delete = bot.get_channel(channel_id)
        if channel_to_delete:
            try: await channel_to_delete.delete(reason="İnaktivite nedeniyle otomatik silindi.")
            except Exception as e: logger.error(f"İnaktif kanal (ID: {channel_id}) silinirken hata: {e}"); remove_temp_channel_db(channel_id)
        else: logger.warning(f"İnaktif kanal (ID: {channel_id}) bot cache'inde bulunamadı. DB'den siliniyor."); remove_temp_channel_db(channel_id)
        warned_inactive_channels.discard(channel_id)

@check_inactivity.before_loop
async def before_check_inactivity():
    await bot.wait_until_ready()
    logger.info("Bot hazır, inaktivite kontrol döngüsü başlıyor.")


# --- Komutlar ---

# === YENİ KOMUT: MEVCUT MODELİ GÖSTER ===
@bot.command(name='currentmodel', aliases=['modelinfo', 'aktifmodel'])
@commands.guild_only() # Sunucuda çalışsın
@commands.cooldown(1, 5, commands.BucketType.user) # Spam önleme
async def show_current_model(ctx):
    """
    Mevcut sohbet kanalında kullanılan veya bir sonraki sohbet için seçilen modeli gösterir.
    """
    author_id = ctx.author.id
    channel_id = ctx.channel.id

    embed = discord.Embed(title="🤖 Model Bilgisi", color=discord.Color.teal()) # Renk ayarı
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

    current_channel_model = None
    next_chat_model = user_next_model.get(author_id) # Kullanıcının sonraki seçimi

    # 1. Komut geçici bir sohbet kanalında mı kullanıldı?
    if channel_id in temporary_chat_channels:
        # Aktif sohbet oturumundan modeli almayı dene
        if channel_id in active_gemini_chats:
            # .get() kullanarak anahtar yoksa hata almayı önle, varsayılanı kullan
            current_channel_model = active_gemini_chats[channel_id].get('model', DEFAULT_MODEL_NAME)
        else:
            # Oturum henüz başlamamış olabilir (ilk mesaj atılmamış), DB'den oku
            try:
                conn = db_connect()
                cursor = conn.cursor()
                cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = ?", (channel_id,))
                result = cursor.fetchone()
                conn.close()
                if result and result[0]:
                    current_channel_model = result[0]
                else: # DB'de de yoksa (beklenmez ama olabilir)
                    logger.warning(f"Geçici kanal {channel_id} için DB'de model adı bulunamadı.")
                    current_channel_model = DEFAULT_MODEL_NAME
            except Exception as e:
                logger.error(f"Mevcut kanal modeli DB'den okunurken hata: {e}")
                current_channel_model = DEFAULT_MODEL_NAME # Hata durumunda varsayılan

        embed.add_field(name=f"Bu Kanalda Kullanılan Model ({ctx.channel.mention})", value=f"`{current_channel_model}`", inline=False)

        # Kullanıcının bir sonraki seçimi farklı mı?
        if next_chat_model and next_chat_model != current_channel_model:
            embed.add_field(name="Bir Sonraki Sohbet İçin Seçilen", value=f"`{next_chat_model}`\n*(Giriş kanalına tekrar yazdığınızda aktif olacak)*", inline=False)
        elif not next_chat_model: # Sonraki için seçim yapılmamış
             embed.add_field(name="Bir Sonraki Sohbet İçin Seçilen", value=f"*Özel bir model seçilmedi (Varsayılan: `{DEFAULT_MODEL_NAME}`).*\n*`.setmodel <ad>` ile seçebilirsiniz.*", inline=False)
        # Eğer sonraki seçim mevcutla aynıysa ek bir şey belirtmeye gerek yok

    # 2. Komut geçici olmayan bir kanalda kullanıldı
    else:
        embed.description = "Şu anda geçici bir Gemini sohbet kanalında değilsiniz."
        if next_chat_model:
            embed.add_field(name="Bir Sonraki Sohbet İçin Seçilen Model", value=f"`{next_chat_model}`\n*(Giriş kanalına yazdığınızda bu modelle yeni kanal açılacak)*", inline=False)
        else:
            embed.add_field(name="Bir Sonraki Sohbet İçin Seçilen Model", value=f"*Özel bir model seçilmedi (Varsayılan: `{DEFAULT_MODEL_NAME}`).*\n*`.setmodel <ad>` ile seçebilirsiniz.*", inline=False)

    await ctx.send(embed=embed)
# ========================================


@bot.command(name='endchat', aliases=['end', 'closechat'])
@commands.guild_only()
async def end_chat(ctx):
    """Mevcut geçici Gemini sohbet kanalını siler."""
    channel_id = ctx.channel.id; author_id = ctx.author.id
    if channel_id not in temporary_chat_channels: return # Sadece geçici kanallarda çalışsın

    expected_user_id = None
    for user_id, ch_id in user_to_channel_map.items():
        if ch_id == channel_id: expected_user_id = user_id; break

    # Kanal sahibi kontrolü
    if expected_user_id and author_id != expected_user_id:
         await ctx.send("Bu kanalı sadece oluşturan kişi kapatabilir.", delete_after=10)
         try: await ctx.message.delete(delay=10)
         except: pass # Hataları yoksay
         return
    elif expected_user_id is None:
         logger.warning(f".endchat: Kanal {channel_id} geçici listede ama sahibi DB'de bulunamadı.")
         # Sahibi bulunamasa da silmeye izin verelim mi? Şimdilik evet.

    if not ctx.guild.me.guild_permissions.manage_channels:
         logger.error(f"Botun '{ctx.channel.name}' kanalını silmek için izni yok.")
         # Kullanıcıya bilgi verilebilir ama kanal silinemeyecek.
         return

    try:
        logger.info(f"Kanal '{ctx.channel.name}' (ID: {channel_id}) kullanıcı {ctx.author.name} tarafından manuel siliniyor.")
        await ctx.channel.delete(reason=f"Gemini sohbeti {ctx.author.name} tarafından sonlandırıldı.")
        # State temizliği on_guild_channel_delete tarafından yapılacak.
    except discord.errors.NotFound:
        logger.warning(f"'{ctx.channel.name}' kanalı manuel silinirken bulunamadı. State manuel temizleniyor.")
        remove_temp_channel_db(channel_id) # DB'den manuel sil
        temporary_chat_channels.discard(channel_id); active_gemini_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id: user_id_to_remove = user_id; break
        if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None)
    except Exception as e: logger.error(f".endchat komutunda hata: {e}\n{traceback.format_exc()}")


@bot.event
async def on_guild_channel_delete(channel):
    """Bir kanal silindiğinde tetiklenir (state temizliği için)."""
    channel_id = channel.id
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) silindi, tüm ilgili state'ler temizleniyor.")
        temporary_chat_channels.discard(channel_id); active_gemini_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id)
        user_id_to_remove = None
        for user_id, ch_id in list(user_to_channel_map.items()):
            if ch_id == channel_id: user_id_to_remove = user_id; break
        if user_id_to_remove: user_to_channel_map.pop(user_id_to_remove, None); logger.info(f"Kullanıcı {user_id_to_remove} için kanal haritası temizlendi.")
        remove_temp_channel_db(channel_id) # Veritabanından da sil


@bot.command(name='clear')
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx, amount: str = None):
    """
    Mevcut kanalda belirtilen sayıda mesajı veya tüm mesajları siler (sabitlenmişler hariç).
    Kullanım: .clear <sayı> veya .clear all
    """
    if not ctx.guild.me.guild_permissions.manage_messages:
         logger.warning(f"Botun '{ctx.channel.name}' kanalında 'Mesajları Yönet' izni yok.")
         await ctx.send("Bu kanaldaki mesajları silebilmem için 'Mesajları Yönet' iznine ihtiyacım var.", delete_after=10)
         return

    if amount is None:
        await ctx.send(f"Lütfen silinecek mesaj sayısını (`{ctx.prefix}clear 5` gibi) veya `{ctx.prefix}clear all` yazın.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass # Hataları yoksay
        return

    deleted_count = 0; skipped_pinned = 0
    try:
        if amount.lower() == 'all':
            await ctx.send("Kanal temizleniyor...", delete_after=5)
            try: await ctx.message.delete()
            except: pass # Hataları yoksay

            def check_not_pinned(m):
                if m.pinned: nonlocal skipped_pinned; skipped_pinned += 1; return False
                return True

            while True:
                deleted = await ctx.channel.purge(limit=100, check=check_not_pinned, bulk=True)
                deleted_count += len(deleted)
                if len(deleted) < 100: break
                await asyncio.sleep(1)

            msg = f"Kanal temizlendi! Toplam {deleted_count} mesaj silindi."
            if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
            await ctx.send(msg, delete_after=10)

        else: # Sayı belirtilmişse
            try:
                limit = int(amount); assert limit > 0
                limit_with_command = limit + 1

                def check_not_pinned_limit(m):
                    if m.pinned: nonlocal skipped_pinned; skipped_pinned += 1; return False
                    return True

                deleted = await ctx.channel.purge(limit=limit_with_command, check=check_not_pinned_limit, bulk=True); actual_deleted_count = len(deleted) - (1 if ctx.message in deleted else 0)

                msg = f"{actual_deleted_count} mesaj silindi."
                if skipped_pinned > 0: msg += f" ({skipped_pinned} sabitlenmiş mesaj atlandı)."
                await ctx.send(msg, delete_after=5)

            except (ValueError, AssertionError):
                 await ctx.send(f"Geçersiz sayı. Lütfen pozitif bir sayı girin.", delete_after=10)
                 try: await ctx.message.delete(delay=10)
                 except: pass # Hataları yoksay

    except discord.errors.Forbidden:
        logger.error(f"HATA: '{ctx.channel.name}' kanalında mesajları silme izni yok (Bot: {ctx.guild.me.name})!")
        await ctx.send("Mesajları silme iznim yok gibi görünüyor.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
    except Exception as e:
        logger.error(f".clear komutunda beklenmedik hata: {e}\n{traceback.format_exc()}")
        await ctx.send("Komutu işlerken beklenmedik bir hata oluştu.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass


@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
async def ask_in_channel(ctx, *, question: str = None):
    """
    Sorulan soruyu Gemini'ye iletir ve yanıtı geçici olarak bu kanalda gösterir.
    Hem soru hem de cevap belirli bir süre sonra otomatik silinir.
    """
    global model # Global modeli kullan
    if question is None:
        error_msg = await ctx.reply(f"Lütfen komuttan sonra bir soru sorun (örn: `{ctx.prefix}ask Ankara'nın nüfusu kaçtır?`).", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] geçici soru sordu: {question[:100]}...")
    bot_response_message: discord.Message = None
    try:
        async with ctx.typing():
            try:
                if model is None:
                    logger.error(".ask için global model yok!")
                    await ctx.reply("Model yüklenemedi.", delete_after=10)
                    try:
                        await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                    except Exception:
                        pass
                return

                response = await asyncio.to_thread(model.generate_content, question) # Global modeli kullan
            except Exception as gemini_e:
                # --- DÜZELTİLMİŞ EXCEPT BLOĞU (ask - Gemini Hatası) ---
                logger.error(f".ask için Gemini API hatası: {gemini_e}")
                # Hata mesajını belirle (önceki yanıttaki gibi daha detaylı olabilir)
                msg = "Yapay zeka ile iletişim kurarken bir sorun oluştu."
                # Kullanıcıya yanıt gönder
                await ctx.reply(msg, delete_after=10)
                # Hata durumunda kullanıcının orijinal mesajını silmeyi dene
                try:
                    await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
                except discord.errors.NotFound: pass
                except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .ask komut mesajını ({ctx.message.id}) (hata sonrası) silme izni yok.")
                except Exception as delete_e: logger.error(f".ask komut mesajı ({ctx.message.id}) (hata sonrası) silinirken hata: {delete_e}")
                return # Hata oluştuğu için fonksiyondan çık
                # --- DÜZELTME SONU ---
            gemini_response = response.text.strip()

        if not gemini_response:
            # --- DÜZELTİLMİŞ IF BLOĞU (ask - Boş Yanıt) ---
            logger.warning(f"Gemini'den .ask için boş yanıt alındı (Soru: {question[:50]}...)") # Logla
            await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15) # Kullanıcıya bilgi ver
            # Boş yanıt durumunda da kullanıcının mesajını silmeyi dene
            try:
                await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
            except discord.errors.NotFound: pass
            except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .ask komut mesajını ({ctx.message.id}) (boş yanıt sonrası) silme izni yok.")
            except Exception as delete_e: logger.error(f".ask komut mesajı ({ctx.message.id}) (boş yanıt sonrası) silinirken hata: {delete_e}")
            return # Boş yanıt geldiği için fonksiyondan çık
            # --- DÜZELTME SONU ---

        # Yanıtı Embed ile gönder
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url)
        # Soru çok uzunsa kısalt
        question_display = question if len(question) <= 1024 else question[:1021] + "..."
        embed.add_field(name="Soru", value=question_display, inline=False)
         # Yanıt çok uzunsa kısalt (Embed field limiti 1024)
        response_display = gemini_response if len(gemini_response) <= 1024 else gemini_response[:1021] + "..."
        embed.add_field(name=" yanıt", value=response_display, inline=False)
        embed.set_footer(text=f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)} dakika sonra silinecektir.")
        bot_response_message = await ctx.reply(embed=embed)

        logger.info(f".ask komutuna yanıt gönderildi (Mesaj ID: {bot_response_message.id})")
        try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY); logger.info(f".ask komut mesajı ({ctx.message.id}) silinmek üzere zamanlandı.")
        except Exception as e: logger.warning(f".ask komut mesajı ({ctx.message.id}) silme işlemi zamanlanamadı: {e}")
        try: await bot_response_message.delete(delay=MESSAGE_DELETE_DELAY); logger.info(f".ask yanıt mesajı ({bot_response_message.id}) silinmek üzere zamanlandı.")
        except Exception as e: logger.warning(f".ask yanıt mesajı ({bot_response_message.id}) silme işlemi zamanlanamadı: {e}")

    except Exception as e:
        # --- DÜZELTİLMİŞ EXCEPT BLOĞU (ask - Genel Hata) ---
        logger.error(f".ask komutunda genel hata: {e}\n{traceback.format_exc()}") # Hatayı logla
        await ctx.reply("Sorunuzu işlerken beklenmedik bir hata oluştu.", delete_after=15) # Kullanıcıya bilgi ver
        # Genel hata durumunda da kullanıcının mesajını silmeyi dene
        try:
            await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        except discord.errors.NotFound: pass
        except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .ask komut mesajını ({ctx.message.id}) (genel hata sonrası) silme izni yok.")
        except Exception as delete_e: logger.error(f".ask komut mesajı ({ctx.message.id}) (genel hata sonrası) silinirken hata: {delete_e}")
        # Return etmeye gerek yok, fonksiyon zaten bitiyor.
        # --- DÜZELTME SONU ---


@bot.command(name='resetchat')
@commands.guild_only()
async def reset_chat_session(ctx):
    """Mevcut geçici sohbet kanalının Gemini konuşma geçmişini sıfırlar."""
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        # --- DÜZELTİLMİŞ IF BLOĞU (resetchat - Kanal Kontrolü) ---
        await ctx.send("Bu komut sadece geçici sohbet kanallarında kullanılabilir.", delete_after=10) # Kullanıcıya bilgi ver
        # Komut mesajını silmeyi dene
        try:
            await ctx.message.delete(delay=10)
        except discord.errors.NotFound: pass
        except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .resetchat komut mesajını (yanlış kanal) silme izni yok.")
        except Exception as delete_e: logger.error(f".resetchat komut mesajı (yanlış kanal) silinirken hata: {delete_e}")
        return # Geçici kanal değilse fonksiyondan çık
        # --- DÜZELTME SONU ---

    if channel_id in active_gemini_chats:
        active_gemini_chats.pop(channel_id, None) # Oturumu bellekten kaldır
        logger.info(f"Sohbet geçmişi {ctx.author.name} tarafından '{ctx.channel.name}' (ID: {channel_id}) için sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi sıfırlandı.", delete_after=15)
    else:
        logger.info(f"Sıfırlanacak aktif sohbet oturumu bulunamadı: Kanal {channel_id}")
        await ctx.send("Zaten aktif bir konuşma geçmişi bulunmuyor.", delete_after=10)
    try: await ctx.message.delete(delay=15)
    except: pass


@bot.command(name='listmodels', aliases=['models'])
@commands.cooldown(1, 10, commands.BucketType.user) # Kullanıcı başına 10 saniyede 1
async def list_available_models(ctx):
    """Kullanılabilir Gemini modellerini listeler."""
    try:
        models_list = []
        view_available = False # Vision modellerini gösterelim mi?
        async with ctx.typing():
             available_models = await asyncio.to_thread(genai.list_models)
             for m in available_models:
                 is_chat_model = 'generateContent' in m.supported_generation_methods
                 is_vision_model = 'vision' in m.name # Basit kontrol
                 if is_chat_model and (view_available or not is_vision_model): # Şimdilik sadece metin modelleri
                     model_id = m.name.split('/')[-1]
                     # Belki popüler olanları veya önerilenleri işaretleyebiliriz
                     prefix = "✨" if "1.5-flash" in model_id or "1.5-pro" in model_id else "-"
                     models_list.append(f"{prefix} `{model_id}`")

        if not models_list: await ctx.send("Kullanılabilir Gemini modeli bulunamadı."); return

        embed = discord.Embed(title="🤖 Kullanılabilir Gemini Modelleri", description="Aşağıdaki modelleri `.setmodel <model_adı>` ile seçebilirsiniz:\n" + "\n".join(sorted(models_list)), color=discord.Color.gold())
        embed.set_footer(text="✨: Önerilen güncel modeller.")
        await ctx.send(embed=embed)

    except Exception as e: logger.error(f"Gemini modelleri listelenirken hata: {e}"); await ctx.send("Modelleri listelerken bir sorun oluştu.")


@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user) # Kullanıcı başına 5 saniyede 1
async def set_next_chat_model(ctx, model_id: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini modelini ayarlar."""
    global user_next_model
    if model_id is None:
        # --- DÜZELTİLMİŞ IF BLOĞU (setmodel - Model Adı Eksik) ---
        await ctx.send(f"Lütfen bir model adı belirtin. Kullanılabilir modeller için `{ctx.prefix}listmodels` komutunu kullanın.", delete_after=15) # Kullanıcıya bilgi ver
        # Komut mesajını silmeyi dene
        try:
            await ctx.message.delete(delay=15)
        except discord.errors.NotFound: pass # Mesaj zaten silinmiş olabilir
        except discord.errors.Forbidden: logger.warning(f"'{ctx.channel.name}' kanalında .setmodel komut mesajını (model adı eksik) silme izni yok.")
        except Exception as delete_e: logger.error(f".setmodel komut mesajı (model adı eksik) silinirken hata: {delete_e}")
        return # Model adı belirtilmediyse fonksiyondan çık
        # --- DÜZELTME SONU ---

    # Kullanıcı sadece kısa adı yazmış olabilir (örn: gemini-1.5-pro-latest)
    # API için tam adı oluşturmamız lazım (models/...)
    if not model_id.startswith("models/"):
         target_model_name = f"models/{model_id}"
    else:
         target_model_name = model_id # Kullanıcı zaten tam adı yazmışsa

    try:
        logger.info(f"{ctx.author.name} bir sonraki sohbet için {target_model_name} modelini ayarlamaya çalışıyor...")
        async with ctx.typing():
             await asyncio.to_thread(genai.get_model, target_model_name) # Modelin varlığını kontrol et

        # Modeli saklarken sadece ID'sini saklamak daha iyi olabilir
        user_model_id = target_model_name.split('/')[-1]
        user_next_model[ctx.author.id] = user_model_id
        logger.info(f"{ctx.author.name} için bir sonraki model '{user_model_id}' olarak ayarlandı.")
        await ctx.send(f"✅ Bir sonraki özel sohbetiniz için model başarıyla `{user_model_id}` olarak ayarlandı.", delete_after=20)
        try: await ctx.message.delete(delay=20)
        except: pass

    except Exception as e:
        logger.warning(f"Geçersiz model adı denendi ({target_model_name}): {e}")
        await ctx.send(f"❌ `{model_id}` geçerli/erişilebilir bir model değil. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=15)
        try: await ctx.message.delete(delay=15)
        except: pass


@bot.command(name='setentrychannel')
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_entry_channel(ctx, channel: discord.TextChannel = None):
    """Otomatik sohbet kanalı oluşturulacak giriş kanalını ayarlar."""
    global entry_channel_id
    if channel is None: await ctx.send(f"Lütfen bir kanal etiketleyin veya ID'sini yazın."); return

    entry_channel_id = channel.id
    save_config('entry_channel_id', entry_channel_id)
    logger.info(f"Giriş kanalı yönetici {ctx.author.name} tarafından {channel.name} (ID: {channel.id}) olarak ayarlandı.")
    await ctx.send(f"✅ Giriş kanalı başarıyla {channel.mention} olarak ayarlandı.")
    try: await bot.change_presence(activity=discord.Game(name=f"Sohbet için #{channel.name}"))
    except: pass

@bot.command(name='settimeout')
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_inactivity_timeout(ctx, hours: float = None):
    """Geçici kanalların aktif olmazsa silineceği süreyi saat cinsinden ayarlar."""
    global inactivity_timeout
    if hours is None or hours <= 0: await ctx.send(f"Lütfen pozitif bir saat değeri girin (örn: `{ctx.prefix}settimeout 1.5`)."); return

    inactivity_timeout = datetime.timedelta(hours=hours)
    save_config('inactivity_timeout_hours', str(hours))
    logger.info(f"İnaktivite zaman aşımı yönetici {ctx.author.name} tarafından {hours} saat olarak ayarlandı.")
    await ctx.send(f"✅ İnaktivite zaman aşımı başarıyla {hours} saat olarak ayarlandı.")


@bot.command(name='commandlist', aliases=['commands', 'cmdlist', 'yardım', 'help', 'komutlar'])
async def show_commands(ctx):
    """Botun kullanılabilir komutlarını listeler."""
    entry_channel_mention = f"<#{entry_channel_id}>" if entry_channel_id and bot.get_channel(entry_channel_id) else "Ayarlanmamış Giriş Kanalı"
    embed = discord.Embed(title=f"{bot.user.name} Komut Listesi", description=f"Özel sohbet başlatmak için {entry_channel_mention} kanalına mesaj yazın.\nDiğer komutlar için `{ctx.prefix}` ön ekini kullanabilirsiniz.", color=discord.Color.dark_purple())
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    for command in sorted(bot.commands, key=lambda cmd: cmd.name):
        if command.hidden or command.name == 'startchat': continue
        try: can_run = await command.can_run(ctx); assert can_run
        except: continue
        help_text = command.short_doc or "Açıklama yok."; aliases = f" (Diğer adlar: {', '.join(command.aliases)})" if command.aliases else ""
        params = f" {command.signature}" if command.signature else ""
        embed.add_field(name=f"{ctx.prefix}{command.name}{params}{aliases}", value=help_text, inline=False)
    embed.set_footer(text=f"Bot Prefixleri: {', '.join(bot.command_prefix)}")
    await ctx.send(embed=embed)


# --- Genel Hata Yakalama ---
@bot.event
async def on_command_error(ctx, error):
    """Komutlarla ilgili hataları merkezi olarak yakalar."""
    error = getattr(error, 'original', error) # Orijinal hatayı al
    if isinstance(error, commands.CommandNotFound): return # Bilinmeyen komutları yoksay

    # Belirli hatalar için kullanıcı dostu mesajlar
    if isinstance(error, commands.MissingPermissions):
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {error.missing_permissions}")
        perms = "\n- ".join(error.missing_permissions).replace('_', ' ').title()
        await ctx.send(f"Üzgünüm {ctx.author.mention}, şu izinlere sahip olmalısın:\n- {perms}", delete_after=15)
    elif isinstance(error, commands.BotMissingPermissions):
        logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {error.missing_permissions}")
        perms = "\n- ".join(error.missing_permissions).replace('_', ' ').title()
        await ctx.send(f"Benim şu izinlere sahip olmam gerekiyor:\n- {perms}", delete_after=15)
        return # Botun izni yoksa komut mesajını silmeye çalışma
    elif isinstance(error, commands.NoPrivateMessage):
        # --- DÜZELTİLMİŞ ELIF BLOĞU (NoPrivateMessage) ---
        logger.warning(f"'{ctx.command.qualified_name}' komutu DM'de kullanılamaz ({ctx.author.name}).") # Logla
        # Kullanıcıya DM göndermeyi dene
        try:
            await ctx.author.send("Bu komut sadece sunucu kanallarında kullanılabilir.")
        except discord.errors.Forbidden:
            # Kullanıcının DM'leri kapalı olabilir, sorun değil.
            logger.debug(f"{ctx.author.name}'e DM gönderilemedi (DM'ler kapalı olabilir).")
        except Exception as dm_error:
            logger.error(f"{ctx.author.name}'e DM gönderilirken hata: {dm_error}")
        return # DM hatası durumunda da fonksiyondan çıkmak mantıklı olabilir
        # --- DÜZELTME SONU ---
    elif isinstance(error, commands.CheckFailure):
        logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - {error}") # Örn: @commands.guild_only() DM'de
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Beklemede. Lütfen {error.retry_after:.1f} saniye sonra tekrar dene.", delete_after=5)
    elif isinstance(error, commands.BadArgument) or isinstance(error, commands.MissingRequiredArgument):
         param = f" ({error.param.name})" if isinstance(error, commands.MissingRequiredArgument) else ""
         command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with
         usage = f"`{ctx.prefix}{command_name}{ctx.command.signature if ctx.command else ''}`"
         await ctx.send(f"Hatalı/eksik komut kullanımı{param}. Doğru kullanım: {usage}", delete_after=15)
    else: # Diğer tüm beklenmedik hatalar
        logger.error(f"'{ctx.command.qualified_name if ctx.command else '?'}' işlenirken hata oluştu: {error}")
        traceback_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        logger.error(f"Traceback:\n{traceback_str}")
        await ctx.send("Komut işlenirken beklenmedik bir hata oluştu.", delete_after=10)

    # Hata mesajı gönderdikten sonra kullanıcının komutunu silmeyi dene (eğer botun izni varsa)
    try:
        # Botun izni yoksa zaten yukarıda return ile çıkıldı (BotMissingPermissions)
        await ctx.message.delete(delay=15 if isinstance(error, (commands.MissingPermissions, commands.BadArgument, commands.MissingRequiredArgument)) else 10)
    except: pass # Silinemezse sorun değil

# --- Botu Çalıştır ---
if __name__ == "__main__":
    logger.info("Bot başlatılıyor...")
    try:
        bot.run(DISCORD_TOKEN, log_handler=None) # Kendi logger'ımızı kullanıyoruz
    except discord.errors.LoginFailure: logger.critical("HATA: Geçersiz Discord Token!")
    except Exception as e: logger.critical(f"Bot çalıştırılırken kritik hata: {e}"); logger.critical(traceback.format_exc())
    finally:
        logger.info("Bot kapatılıyor...")
        # Veritabanı bağlantısını kapatmaya gerek yok, SQLite otomatik halleder.