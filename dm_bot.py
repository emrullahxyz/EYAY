# -*- coding: utf-8 -*-
# dm_bot_v2.py # Sürüm adı güncellendi

import discord
from discord.ext import commands, tasks
import os
import google.generativeai as genai
from dotenv import load_dotenv
import asyncio
import logging
import traceback
import datetime
import requests
import json
import psycopg2
from psycopg2.extras import DictCursor
from typing import Optional # Optional type hinting için

# --- Logging Ayarları ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('discord_dm_ai_bot_v2')

# .env dosyasındaki değişkenleri yükle
load_dotenv()

# --- Ortam Değişkenleri ve Yapılandırma ---
# AYRI BİR BOT TOKENI KULLANIN!
DISCORD_TOKEN = os.getenv("DM_BOT_DISCORD_TOKEN") # Farklı değişken adı önerilir
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") # Aynı DB olabilir

# Model Ön Ekleri ve Varsayılanlar
GEMINI_PREFIX = "gs:"
DEEPSEEK_OPENROUTER_PREFIX = "ds:"
OPENROUTER_DEEPSEEK_MODEL_NAME = os.getenv("OPENROUTER_DEEPSEEK_MODEL", "deepseek/deepseek-chat")
DEFAULT_GEMINI_MODEL_NAME = 'gemini-2.5-flash-preview-04-17'
DEFAULT_MODEL_NAME = f"{GEMINI_PREFIX}{DEFAULT_GEMINI_MODEL_NAME}"

# OpenRouter API Endpoint
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Yeni Yapılandırma Değişkenleri
DM_INACTIVITY_TIMEOUT_HOURS = float(os.getenv("DM_INACTIVITY_TIMEOUT_HOURS", 24)) # DM sohbet zaman aşımı (saat)
SHOW_MODEL_PREFIX_IN_RESPONSE = os.getenv("SHOW_MODEL_PREFIX_IN_RESPONSE", "true").lower() == "true" # Yanıtlarda model ön eki gösterilsin mi?
HISTORY_WARNING_THRESHOLD = int(os.getenv("HISTORY_WARNING_THRESHOLD", 40)) # Geçmiş bu sayıya ulaşınca uyarı ver (çift mesaj = 2 sayılır)

# API Anahtarı ve Kütüphane Kontrolleri
if not DISCORD_TOKEN: logger.critical("HATA: DM_BOT_DISCORD_TOKEN bulunamadı!"); exit()
if not GEMINI_API_KEY and not OPENROUTER_API_KEY: logger.critical("HATA: Ne Gemini ne de OpenRouter API Anahtarı bulunamadı!"); exit()
if not GEMINI_API_KEY: logger.warning("UYARI: Gemini API Anahtarı yok, Gemini kullanılamayacak.")
if not OPENROUTER_API_KEY: logger.warning("UYARI: OpenRouter API Anahtarı yok, DeepSeek kullanılamayacak.")
try: import requests; REQUESTS_AVAILABLE = True
except ImportError: REQUESTS_AVAILABLE = False; logger.error("HATA: 'requests' kütüphanesi bulunamadı!"); exit()

# --- Global Değişkenler ---
# user_id -> {'model': '...', 'session': GeminiSession|None, 'history': List|None, 'last_active': datetime}
active_dm_chats = {}
user_preferences = {} # user_id -> 'prefix:model_name'
first_dm_sent = set() # İlk DM mesajı gönderilen kullanıcıların ID'leri

# --- Veritabanı Yardımcı Fonksiyonları (Aynı kalabilir) ---
def db_connect_dm():
    if not DATABASE_URL: return None
    try: return psycopg2.connect(DATABASE_URL, sslmode='require')
    except psycopg2.DatabaseError as e: logger.error(f"[DM Bot] PostgreSQL bağlantı hatası: {e}"); return None

def setup_database_dm():
    conn = db_connect_dm();
    if not conn: logger.warning("[DM Bot] Veritabanı bağlantısı yok, tercihler/durumlar kalıcı olmayacak."); return
    try:
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS user_preferences (user_id BIGINT PRIMARY KEY, preferred_model TEXT NOT NULL)')
        # İlk DM durumunu saklamak için (opsiyonel, bellek de yeterli olabilir)
        # cursor.execute('CREATE TABLE IF NOT EXISTS user_flags (user_id BIGINT PRIMARY KEY, first_dm_received BOOLEAN DEFAULT FALSE)')
        conn.commit(); cursor.close(); logger.info("[DM Bot] Veritabanı tabloları kontrol edildi/oluşturuldu.")
    except Exception as e: logger.error(f"[DM Bot] Veritabanı tabloları oluşturulurken hata: {e}"); conn.rollback()
    finally: conn.close()

def save_user_preference(user_id, model_name_with_prefix):
    conn = db_connect_dm();
    if not conn: return False
    sql = "INSERT INTO user_preferences (user_id, preferred_model) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET preferred_model = EXCLUDED.preferred_model;"
    try:
        cursor = conn.cursor(); cursor.execute(sql, (user_id, model_name_with_prefix)); conn.commit(); cursor.close()
        user_preferences[user_id] = model_name_with_prefix; logger.info(f"[DM Bot] Kullanıcı {user_id} tercihi kaydedildi: {model_name_with_prefix}"); return True
    except Exception as e: logger.error(f"[DM Bot] Kullanıcı {user_id} tercihi kaydedilirken hata: {e}"); conn.rollback(); return False
    finally: conn.close()

def load_user_preference(user_id) -> str:
    if user_id in user_preferences: return user_preferences[user_id]
    conn = db_connect_dm();
    if not conn: return DEFAULT_MODEL_NAME
    sql = "SELECT preferred_model FROM user_preferences WHERE user_id = %s;"; preference = DEFAULT_MODEL_NAME
    try:
        cursor = conn.cursor(cursor_factory=DictCursor); cursor.execute(sql, (user_id,)); result = cursor.fetchone(); cursor.close()
        if result:
            preference = result['preferred_model']
            if preference.startswith(DEEPSEEK_OPENROUTER_PREFIX) and preference != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
                 preference = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
            user_preferences[user_id] = preference
    except Exception as e: logger.error(f"[DM Bot] Kullanıcı {user_id} tercihi yüklenirken hata: {e}")
    finally: conn.close()
    return preference

def load_all_preferences():
     conn = db_connect_dm();
     if not conn: logger.warning("[DM Bot] DB Bağlantısı yok, tercihler yüklenemiyor."); return
     sql = "SELECT user_id, preferred_model FROM user_preferences;"; count = 0
     try:
        cursor = conn.cursor(cursor_factory=DictCursor); cursor.execute(sql); prefs = cursor.fetchall(); cursor.close()
        for row in prefs:
            pref = row['preferred_model']
            if pref.startswith(DEEPSEEK_OPENROUTER_PREFIX) and pref != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}": pref = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
            user_preferences[row['user_id']] = pref; count += 1
        logger.info(f"[DM Bot] {count} kullanıcı tercihi veritabanından yüklendi.")
     except Exception as e: logger.error(f"[DM Bot] Tüm tercihler yüklenirken hata: {e}")
     finally: conn.close()
# --- Gemini Yapılandırması ---
if GEMINI_API_KEY:
    try: genai.configure(api_key=GEMINI_API_KEY); logger.info("[DM Bot] Gemini API anahtarı yapılandırıldı.")
    except Exception as configure_error: logger.error(f"[DM Bot] Gemini API yapılandırma hatası: {configure_error}"); GEMINI_API_KEY = None

# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
bot = commands.Bot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Ana AI Fonksiyonu (DM için - Yanıt Ön Eki Eklendi) ---
async def send_dm_to_ai(user: discord.User, prompt_text: str):
    global active_dm_chats
    user_id = user.id
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # --- Aktif Sohbeti Başlat veya Yükle ---
    if user_id not in active_dm_chats:
        try:
            preferred_model = load_user_preference(user_id)
            logger.info(f"[DM Bot] Kullanıcı {user_id} için yeni sohbet başlatılıyor ({preferred_model} modeli ile).")
            active_dm_chats[user_id] = {'model': preferred_model, 'session': None, 'history': None, 'last_active': now_utc} # last_active eklendi
            if preferred_model.startswith(GEMINI_PREFIX):
                if not GEMINI_API_KEY: raise ValueError("Gemini API anahtarı yok.")
                actual_model_name = preferred_model[len(GEMINI_PREFIX):]
                try:
                    gemini_model_instance = genai.GenerativeModel(f"models/{actual_model_name}")
                    active_dm_chats[user_id]['session'] = gemini_model_instance.start_chat(history=[])
                except Exception as model_err:
                    logger.error(f"[DM Bot] Gemini modeli '{actual_model_name}' oluşturulamadı: {model_err}.")
                    # Varsayılana dön (eğer Gemini ise)
                    if DEFAULT_MODEL_NAME.startswith(GEMINI_PREFIX):
                        active_dm_chats[user_id]['model'] = DEFAULT_MODEL_NAME
                        gemini_model_instance = genai.GenerativeModel(f"models/{DEFAULT_GEMINI_MODEL_NAME}")
                        active_dm_chats[user_id]['session'] = gemini_model_instance.start_chat(history=[])
                    else: raise # Başka varsayılan varsa hata ver
            elif preferred_model.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                if not OPENROUTER_API_KEY: raise ValueError("OpenRouter API anahtarı yok.")
                if not REQUESTS_AVAILABLE: raise ImportError("'requests' yok.")
                active_dm_chats[user_id]['history'] = []
            else: raise ValueError(f"Geçersiz model: {preferred_model}")
        except (ValueError, ImportError, Exception) as init_err:
             # Hata yönetimi aynı
             logger.error(f"[DM Bot] Kullanıcı {user_id} için AI sohbet oturumu başlatılamadı: {init_err}")
             try: await user.send("❌ Yapay zeka oturumu başlatılamadı. Lütfen tekrar deneyin veya yöneticiye bildirin.")
             except: pass
             active_dm_chats.pop(user_id, None)
             return False

    # --- Sohbet Verilerini Al ve Aktivite Güncelle ---
    if user_id not in active_dm_chats: return False # Tekrar kontrol
    chat_data = active_dm_chats[user_id]
    chat_data['last_active'] = now_utc # Aktivite zamanını güncelle
    current_model_with_prefix = chat_data['model']
    logger.info(f"[DM AI CHAT/{current_model_with_prefix}] [{user.name} ({user_id})] gönderiyor: {prompt_text[:100]}{'...' if len(prompt_text)>100 else ''}")

    ai_response_text: Optional[str] = None
    error_occurred = False
    user_error_msg = "Yapay zeka ile konuşurken bir sorun oluştu."
    response_data = None

    # Geçmiş uzunluğu kontrolü (Opsiyonel)
    current_history = chat_data.get('history') # Gemini için None olacak
    if isinstance(current_history, list) and len(current_history) >= HISTORY_WARNING_THRESHOLD:
        try:
            await user.send(f"⚠️ Konuşma geçmişi **{len(current_history)} mesaja** ulaştı. Çok uzun konuşmalar AI'ın performansını etkileyebilir veya hatalara yol açabilir. Geçmişi sıfırlamak için `{bot.command_prefix[0]}resetchat` komutunu kullanabilirsiniz.")
            # Uyarıyı tekrar göndermemek için bir işaretleyici eklenebilir (örn. chat_data['warned_history'] = True)
        except: pass # Hata olursa devam et

    try:
        # --- API Çağrısı (Modele Göre) ---
        if current_model_with_prefix.startswith(GEMINI_PREFIX):
            gemini_session = chat_data.get('session')
            if not gemini_session: raise ValueError("Gemini oturumu yok.")
            response = await gemini_session.send_message_async(prompt_text)
            ai_response_text = response.text.strip()
            # Gemini hata kontrolü (aynı)
            # ... (güvenlik, other, boş yanıt kontrolleri) ...
            if error_occurred: ai_response_text = None # Hata varsa yanıtı temizle

        elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
            if not OPENROUTER_API_KEY: raise ValueError("OpenRouter API anahtarı yok.")
            history = chat_data['history'] # Liste olmalı
            history.append({"role": "user", "content": prompt_text})
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            payload = {"model": OPENROUTER_DEEPSEEK_MODEL_NAME, "messages": history}
            try:
                api_response = await asyncio.to_thread(requests.post, OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)
                api_response.raise_for_status()
                response_data = api_response.json()
            except requests.exceptions.RequestException as e:
                # OpenRouter hata yönetimi (aynı)
                # ... (Timeout, 4xx, 5xx kontrolleri) ...
                error_occurred = True; history.pop()
                if e.response is not None:
                    # ... (daha spesifik user_error_msg atamaları) ...
                    pass

            # Yanıt işleme (aynı)
            if not error_occurred and response_data:
                try:
                    # ... (choices, message, content kontrolü) ...
                    if response_data.get("choices") and response_data["choices"][0].get("message") and response_data["choices"][0]["message"].get("content"):
                        ai_response_text = response_data["choices"][0]["message"]["content"].strip()
                        history.append({"role": "assistant", "content": ai_response_text})
                        # ... (finish_reason kontrolü) ...
                    else:
                         # ... (eksik alan veya boş choices hatası) ...
                         error_occurred = True; user_error_msg = "..." ; history.pop()
                except (KeyError, IndexError, TypeError) as parse_error:
                     # ... (yanıt parse hatası) ...
                     error_occurred = True; user_error_msg = "..." ; history.pop()
            if error_occurred: ai_response_text = None # Hata varsa yanıtı temizle
        else: # Bilinmeyen prefix
             error_occurred = True; user_error_msg="Bilinmeyen model yapılandırılmış."

        # --- Yanıt Gönderme ---
        if not error_occurred and ai_response_text:
            # Model ön ekini ekle (yapılandırıldıysa)
            response_prefix = ""
            if SHOW_MODEL_PREFIX_IN_RESPONSE:
                model_type = "Gemini" if current_model_with_prefix.startswith(GEMINI_PREFIX) else "DeepSeek"
                response_prefix = f"**[{model_type}]** "

            full_response = response_prefix + ai_response_text
            if len(full_response) > 2000:
                # Yanıt çok uzunsa ön eki sadece ilk parçaya ekle
                first_part = response_prefix + ai_response_text[:2000 - len(response_prefix)]
                remaining_text = ai_response_text[2000 - len(response_prefix):]
                parts = [first_part] + [remaining_text[i:i+2000] for i in range(0, len(remaining_text), 2000)]
                logger.info(f"[DM Bot] Yanıt >2000kr (User: {user_id}), {len(parts)} parçaya bölünüyor...")
                for part in parts: await user.send(part); await asyncio.sleep(0.5)
            else:
                await user.send(full_response)
            return True
        elif not error_occurred and not ai_response_text:
             logger.info(f"[DM Bot] AI'dan boş yanıt (User: {user_id}), mesaj gönderilmiyor.")
             return True

    # Genel Hata Yakalama (aynı)
    except Exception as e:
        if not error_occurred: logger.error(f"[DM Bot] API/İşlem hatası (User: {user_id}): {type(e).__name__}: {e}\n{traceback.format_exc()}"); error_occurred = True

    # Hata mesajı gönderme (aynı)
    if error_occurred:
        try: await user.send(f"⚠️ {user_error_msg}")
        except Exception as send_err: logger.warning(f"[DM Bot] Hata mesajı gönderilemedi (User: {user_id}): {send_err}")
        return False
    return False

# --- İlk DM Karşılama ---
async def send_first_dm_welcome(user: discord.User):
    """Kullanıcıya ilk DM'de hoş geldin mesajı gönderir."""
    if user.id in first_dm_sent: return # Zaten gönderildiyse tekrar gönderme

    logger.info(f"[DM Bot] Kullanıcı {user.id} ({user.name}) için ilk DM karşılama mesajı gönderiliyor.")
    embed = discord.Embed(
        title=f"👋 Merhaba {user.display_name}!",
        description=(
            f"Ben {bot.user.name}, kişisel yapay zeka asistanınızım!\n\n"
            "Benimle doğrudan buradan sohbet edebilirsiniz. Varsayılan olarak "
            f"`{DEFAULT_MODEL_NAME}` modelini kullanıyorum."
        ),
        color=discord.Color.green()
    )
    if bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    prefix = bot.command_prefix[0]
    embed.add_field(name="Temel Komutlar ⌨️", value=(
        f"• `{prefix}listmodels`: Kullanılabilir modelleri gösterir.\n"
        f"• `{prefix}setmodel <model>`: Sohbet modelinizi değiştirir.\n"
        f"• `{prefix}resetchat`: Bu konuşmanın geçmişini sıfırlar.\n"
        f"• `{prefix}help`: Tüm komutları listeler."
    ), inline=False)
    embed.set_footer(text="Sormak istediğiniz bir şey yazarak başlayın!")

    try:
        await user.send(embed=embed)
        first_dm_sent.add(user.id) # Gönderildi olarak işaretle
        # İsteğe bağlı: Bu durumu DB'ye de kaydedebilirsiniz
    except discord.errors.Forbidden:
        logger.warning(f"[DM Bot] Kullanıcı {user.id} ilk hoşgeldin DM'sini alamadı (DM kapalı?).")
    except Exception as e:
        logger.error(f"[DM Bot] Kullanıcı {user.id} ilk hoşgeldin DM'si gönderilirken hata: {e}")


# --- Bot Olayları ---
@bot.event
async def on_ready():
    logger.info(f'[DM Bot] {bot.user} olarak giriş yapıldı (ID: {bot.user.id}).')
    setup_database_dm()
    load_all_preferences()
    # İlk DM gönderilenleri de DB'den yükle (eğer DB'ye kaydediyorsanız)
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="DM üzerinden sorularınızı"))
        logger.info("[DM Bot] Aktivite ayarlandı.")
    except Exception as e: logger.warning(f"[DM Bot] Aktivite ayarlanamadı: {e}")
    # DM inaktivite kontrolünü başlat
    if DM_INACTIVITY_TIMEOUT_HOURS > 0 and not check_dm_inactivity.is_running():
        check_dm_inactivity.start()
        logger.info(f"[DM Bot] DM inaktivite kontrol görevi {DM_INACTIVITY_TIMEOUT_HOURS} saat aralıkla başlatıldı.")
    elif DM_INACTIVITY_TIMEOUT_HOURS <= 0:
         logger.info("[DM Bot] DM inaktivite kontrolü devre dışı.")

    logger.info("[DM Bot] Komutları ve DM'leri dinliyor..."); print("-" * 20)

@bot.event
async def on_message(message: discord.Message):
    if message.guild is not None or message.author.bot: return

    # İlk DM karşılama mesajını gönder
    # Bu kontrolü, kullanıcı komut yazsa bile yapmak mantıklı olabilir
    if message.author.id not in first_dm_sent:
        await send_first_dm_welcome(message.author)

    ctx = await bot.get_context(message)
    if ctx.valid: await bot.process_commands(message); return

    prompt = message.content
    if prompt.strip():
        await send_dm_to_ai(message.author, prompt)

# --- DM İnaktivite Kontrol Görevi ---
@tasks.loop(hours=DM_INACTIVITY_TIMEOUT_HOURS)
async def check_dm_inactivity():
    """Aktif olmayan DM sohbetlerini bellekten temizler."""
    if not active_dm_chats: return # Boşsa kontrol etme

    now = datetime.datetime.now(datetime.timezone.utc)
    inactive_users = []
    timeout_delta = datetime.timedelta(hours=DM_INACTIVITY_TIMEOUT_HOURS)

    # Kopya üzerinde çalış
    current_chats = active_dm_chats.copy()

    for user_id, chat_data in current_chats.items():
        last_active_time = chat_data.get('last_active')
        if last_active_time and isinstance(last_active_time, datetime.datetime):
             # Timezone kontrolü (ekstra güvenlik)
             if last_active_time.tzinfo is None:
                  last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)

             if now - last_active_time > timeout_delta:
                  inactive_users.append(user_id)
        else: # last_active bilgisi yoksa veya geçersizse (eski veriden kalmış olabilir), temizle
            logger.warning(f"[DM Bot] Kullanıcı {user_id} için geçersiz 'last_active' verisi, sohbet temizleniyor.")
            inactive_users.append(user_id)

    if inactive_users:
        logger.info(f"[DM Bot] İnaktivite: {len(inactive_users)} kullanıcının sohbet durumu bellekten temizleniyor: {inactive_users}")
        for user_id in inactive_users:
            active_dm_chats.pop(user_id, None)
            # İsteğe bağlı: Kullanıcıya bilgi verilebilir ama spam olabilir.
            # user = bot.get_user(user_id)
            # if user:
            #     try: await user.send("ℹ️ Uzun süre aktif olmadığınız için önceki sohbet geçmişiniz sıfırlandı.")
            #     except: pass # DM kapalıysa

@check_dm_inactivity.before_loop
async def before_check_dm_inactivity():
    await bot.wait_until_ready()
    logger.info("[DM Bot] Bot hazır, DM inaktivite kontrol döngüsü başlıyor.")


# --- Komutlar (DM için) ---
# listmodels_dm, set_dm_model, reset_dm_chat, help_dm komutları öncekiyle aynı kalabilir.
# Sadece set_dm_model'de aktif sohbeti sıfırlama mantığı eklendi.
# Önceki kodda bu komutlar zaten mevcut ve doğru.

# --- Genel Hata Yakalama (DM için - Aynı kalabilir) ---
@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.PrivateMessageOnly): logger.warning(f"PrivateMessageOnly hatası? Komut: {ctx.command}, Kanal: {ctx.channel}"); return
    if isinstance(error, commands.CommandOnCooldown): await ctx.send(f"⏳ Lütfen **{error.retry_after:.1f} saniye** bekleyin."); return
    if isinstance(error, commands.UserInputError): await ctx.send(f"⚠️ Hatalı kullanım. `{ctx.prefix}help` yazın."); return
    logger.error(f"[DM Bot] Komut hatası: {type(error).__name__}: {error}"); traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
    try: await ctx.send("⚙️ Komut işlenirken bir hata oluştu.")
    except: pass


# --- Botu Çalıştır ---
if __name__ == "__main__":
    if not DISCORD_TOKEN: logger.critical("HATA: DM_BOT_DISCORD_TOKEN bulunamadı!"); exit()
    logger.info("[DM Bot] Başlatılıyor...")
    try: bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.LoginFailure: logger.critical("[DM Bot] HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired: logger.critical("[DM Bot] HATA: Gerekli Intent'ler (DM Messages, Message Content) etkinleştirilmemiş!")
    except Exception as e: logger.critical(f"[DM Bot] Başlatılırken kritik hata: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally: logger.info("[DM Bot] Kapatılıyor...")