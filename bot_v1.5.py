# -*- coding: utf-8 -*-
# bot_v1.6_corrected_v2.py - Discord Bot with AI Chat and Music


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
from psycopg2 import pool # Bağlantı havuzu için
from flask import Flask # Koyeb/Render için web sunucusu
import threading      # Web sunucusunu ayrı thread'de çalıştırmak için
import sys
import requests # OpenRouter için requests kütüphanesi
import json     # JSON verileri için
import yt_dlp  # YouTube video indirme için
import random  # Çeşitli işlemler için rastgele sayı üreteci
from collections import deque  # Müzik kuyruğu için
from typing import Optional, Dict, Any, Union
import socket  # Tek instance kontrolü için
import atexit  # Program sonlandığında temizlik için

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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "Discord AI Bot")
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", 0))
DATABASE_URL = os.getenv("DATABASE_URL")

# Model Ön Ekleri ve Varsayılanlar
GEMINI_PREFIX = "gs:"
DEEPSEEK_OPENROUTER_PREFIX = "ds:"
OPENROUTER_DEEPSEEK_MODEL_NAME = "deepseek/deepseek-chat" # API dokümanınızdaki tam model adını yazın
DEFAULT_GEMINI_MODEL_NAME = 'gemini-1.5-flash-latest' # 'models/' prefixi olmadan temel ad
DEFAULT_MODEL_NAME = f"{GEMINI_PREFIX}{DEFAULT_GEMINI_MODEL_NAME}"

# OpenRouter API Endpoint
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Varsayılan değerler (DB'den veya ortamdan okunamzsa)
DEFAULT_ENTRY_CHANNEL_ID = os.getenv("ENTRY_CHANNEL_ID")
DEFAULT_INACTIVITY_TIMEOUT_HOURS = 1
MESSAGE_DELETE_DELAY = 600 # .ask mesajları için silme gecikmesi (saniye)

# --- Global Değişkenler ---
entry_channel_id = None
inactivity_timeout = None
active_ai_chats = {} # channel_id -> {'model': ..., 'session': ..., 'history': ...}
temporary_chat_channels = set()
user_to_channel_map = {}
channel_last_active = {}
user_next_model = {}
warned_inactive_channels = set()
initial_ready_complete = False

# Komut izleme sistemi - aynı komutun birden fazla işlenmesini önlemek için
processed_commands = {} # command_id (channel_id + message_id) -> timestamp

# Komut izleme sisteminden muaf tutulacak komutlar
exempt_commands = [
    'play', 'p', 'çal',
    'skip', 's', 'geç',
    'stop', 'dur',
    'pause', 'duraklat',
    'resume', 'devam',
    'queue', 'q', 'kuyruk', 'list', 'liste',
    'nowplaying', 'np', 'şimdiçalıyor', 'şimdi',
    'volume', 'vol', 'ses',
    'setdefaultvolume', 'setvol', 'defaultvol', 'defaultvolume', 'varsayılanses', 'varsayılansesseviyesi',
    'rewind', 'gerisar', 'rw',
    'forward', 'ilerisar', 'ff',
    'seek', 'atla', 'git',
    'loop', 'döngü', 'tekrarla',
    'shuffle', 'karıştır',
    'toggleshuffle', 'karıştırma', 'karıştırmaç', 'shufflemode', # Bu komut kaldırılmıştı ama listede kalabilir
    'playlist', 'pl', 'oynatmalistesi', # Bu komut play ile birleştirilmişti, ama listede kalabilir
    'leave',
    'clear', 'temizle', 'purge',
    'help', 'yardım', 'komutlar'
]

# --- Veritabanı Bağlantı Havuzu ---
db_pool = None

def init_db_pool():
    global db_pool
    try:
        if db_pool is None:
            db_pool = pool.ThreadedConnectionPool(1, 10, DATABASE_URL, sslmode='require')
            logger.info("PostgreSQL bağlantı havuzu başarıyla oluşturuldu.")
        return True
    except Exception as e:
        logger.critical(f"PostgreSQL bağlantı havuzu oluşturulurken hata: {e}")
        return False

def get_db_connection():
    global db_pool
    if db_pool is None:
        if not init_db_pool():
            return None
    try:
        return db_pool.getconn()
    except Exception as e:
        logger.error(f"Havuzdan bağlantı alınırken hata: {e}")
        return None

def release_db_connection(conn):
    global db_pool
    if db_pool is not None and conn is not None:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Bağlantı havuza geri verilirken hata: {e}")
            try: conn.close()
            except: pass

def db_connect():
    return get_db_connection()

# --- MusicPlayer Sınıfı ---
class MusicPlayer:
    def __init__(self):
        self.ytdl_format_options = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'noplaylist': False, # Playlistleri işlemek için False olmalı
            'nocheckcertificate': True,
            'ignoreerrors': False, # Hataları yakalamak için False daha iyi olabilir
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'source_address': '0.0.0.0', # Bağlantı sorunlarını çözmeye yardımcı olabilir
            'extract_flat': 'in_playlist', # Playlist'leri verimli işlemek için
            'cookiefile': None, # Başlangıçta None, on_ready'de ayarlanacak
            'geo_bypass': True, # Coğrafi kısıtlamaları atlamaya yardımcı olabilir

        }
        self.ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn',
        }
        self.volume = 0.5 # Varsayılan ses seviyesi
        self.default_volume = 0.5 # DB'den yüklenecek varsayılan
        self.voice_clients: Dict[int, discord.VoiceClient] = {}
        self.queues: Dict[int, deque] = {}
        self.now_playing: Dict[int, Dict[str, Any]] = {}
        self.locks: Dict[int, asyncio.Lock] = {} # Sunucu başına kilit
        self.is_seeking: Dict[int, bool] = {} # Seek işlemi sırasında bayrak
        self.played_history: Dict[int, list] = {} # Kuyruk döngüsü için
        self.loop_settings: Dict[int, str] = {} # "off", "song", "queue"
        self.shuffle_settings: Dict[int, bool] = {} # Karıştırma açık/kapalı

        # load_volume_settings çağrısı __init__'ten on_ready'e taşındı.

    def set_cookie_file_for_ytdlp(self, path: Optional[str]):
        """yt-dlp için kullanılacak çerez dosyasının yolunu ayarlar."""
        self.cookie_file_to_use = path
        if self.ytdl_format_options: # Güvenlik kontrolü
            self.ytdl_format_options['cookiefile'] = path
        if path:
            logger.info(f"yt-dlp için çerez dosyası yolu ayarlandı: {path}")
        else:
            logger.warning("yt-dlp için çerez dosyası yolu kaldırılamadı veya ayarlanamadı (path is None).")

    def get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def join_voice_channel(self, ctx: commands.Context) -> bool:
        if not ctx.author.voice:
            await ctx.send("❌ Önce bir ses kanalına katılmalısın.")
            return False
        channel = ctx.author.voice.channel
        guild_id = ctx.guild.id

        if guild_id in self.voice_clients and self.voice_clients[guild_id].is_connected():
            if self.voice_clients[guild_id].channel != channel:
                try:
                    await self.voice_clients[guild_id].move_to(channel)
                    await ctx.send(f"✅ {channel.mention} kanalına taşındım.")
                except asyncio.TimeoutError:
                    await ctx.send(f"❌ {channel.mention} kanalına taşınırken zaman aşımı.")
                    return False
                except Exception as e:
                    await ctx.send(f"❌ {channel.mention} kanalına taşınırken hata: {e}")
                    return False
        else:
            try:
                voice_client = await channel.connect(timeout=10.0, reconnect=True)
                self.voice_clients[guild_id] = voice_client
                await ctx.send(f"✅ {channel.mention} kanalına katıldım.")
                if voice_client.source and hasattr(voice_client.source, 'volume'):
                     voice_client.source.volume = self.volume # Yeni bağlantıda sesi ayarla
            except asyncio.TimeoutError:
                await ctx.send("❌ Ses kanalına bağlanırken zaman aşımı.")
                return False
            except discord.ClientException as e:
                logger.error(f"Ses kanalına bağlanırken hata: {e}")
                await ctx.send("❌ Ses kanalına bağlanırken bir hata oluştu.")
                return False
        return True

    async def load_volume_settings(self):
        try:
            # bot.wait_until_ready() burada gereksiz çünkü on_ready içinden çağrılıyor.
            loaded_current, loaded_default = get_volume_settings_db()
            self.volume = loaded_current
            self.default_volume = loaded_default
            logger.info(f"Ses seviyesi ayarları yüklendi: {self.volume:.2f} (mevcut), {self.default_volume:.2f} (varsayılan)")
            for vc in self.voice_clients.values(): # Mevcut bağlantıların sesini güncelle
                if vc and vc.is_connected() and vc.source and hasattr(vc.source, 'volume'):
                    vc.source.volume = self.volume
        except Exception as e:
            logger.error(f"Ses seviyesi ayarları yüklenirken hata: {e}")

    def create_after_playing_callback(self, guild_id: int, ctx_channel_id: Optional[int] = None):
        def after_playing(error):
            # Seek işlemi devam ediyorsa bu callback'i atla.
            if self.is_seeking.get(guild_id, False):
                logger.info(f"create_after_playing_callback: Normal playback after_playing for guild {guild_id} skipped (is_seeking).")
                return

            if error:
                logger.error(f"Müzik çalınırken hata (normal playback callback): {error}")

            # Bir sonraki şarkıyı çal (ctx olmadan, mesaj göndermeyecek)
            coro = self.play_next(guild_id, ctx=None, from_callback=True)
            future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
            try:
                future.result()
            except Exception as e_play_next:
                logger.error(f"Bir sonraki şarkıya geçerken hata (normal callback): {e_play_next}\n{traceback.format_exc()}")
        return after_playing

    async def play_next(self, guild_id: int, ctx: Optional[commands.Context] = None, from_callback: bool = False) -> bool:
        guild = bot.get_guild(guild_id)
        if not guild: logger.warning(f"play_next: Guild {guild_id} bulunamadı."); return False

        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            logger.warning(f"play_next: Sunucu {guild_id} için ses bağlantısı yok veya kesilmiş.")
            if guild_id in self.queues: self.queues[guild_id].clear()
            self.now_playing.pop(guild_id, None)
            return False

        if self.is_seeking.get(guild_id, False):
            logger.info(f"play_next for guild {guild_id} skipped (is_seeking).")
            return False # Seek işlemi öncelikli

        # Eğer zaten bir şey çalıyorsa (ve bu çağrı callback'ten değilse) yeni şarkı eklenmiştir.
        if (voice_client.is_playing() or voice_client.is_paused()) and not from_callback:
            logger.info(f"play_next for guild {guild_id} skipped (already playing/paused, not from callback).")
            return True # Kuyruğa eklendi mesajı .play içinde gönderildi, burada True dönelim.

        loop_mode = self.loop_settings.get(guild_id, "off")

        # Tek şarkı döngüsü: Eğer şarkı bittiyse (from_callback=True) ve tek şarkı döngüsü aktifse,
        # mevcut çalan şarkıyı (now_playing'den) alıp kuyruğun başına ekle.
        if loop_mode == "song" and self.now_playing.get(guild_id) and from_callback:
            song_to_replay = self.now_playing[guild_id].copy() # Kopyasını al
            if 'start_time' in song_to_replay: del song_to_replay['start_time'] # start_time'ı sıfırla
            if guild_id not in self.queues: self.queues[guild_id] = deque()
            self.queues[guild_id].appendleft(song_to_replay)
            logger.info(f"play_next: Tek şarkı döngüsü aktif, '{song_to_replay.get('title')}' kuyruğun başına eklendi.")

        # Kuyruk boş mu kontrol et
        if not self.queues.get(guild_id):
            # Kuyruk döngüsü aktif mi ve geçmiş var mı kontrol et
            if loop_mode == "queue" and self.played_history.get(guild_id):
                logger.info(f"play_next: Kuyruk döngüsü aktif, geçmiş şarkılar ({len(self.played_history[guild_id])}) kuyruğa ekleniyor.")
                self.queues[guild_id] = deque(self.played_history[guild_id])
                self.played_history[guild_id] = [] # Geçmişi temizle
                # Eğer karıştırma da aktifse, yeni oluşturulan kuyruğu karıştır
                if self.shuffle_settings.get(guild_id, False):
                    queue_list = list(self.queues[guild_id])
                    random.shuffle(queue_list)
                    self.queues[guild_id] = deque(queue_list)
                    logger.info(f"play_next: Kuyruk döngüsü için kuyruk karıştırıldı.")
            else: # Kuyruk gerçekten boş
                logger.info(f"play_next: Sunucu {guild_id} için kuyruk boş.")
                self.now_playing[guild_id] = None
                if ctx and not from_callback: # Sadece kullanıcı komutuyla çağrıldıysa mesaj gönder
                    await ctx.send("✅ Kuyruk tamamlandı. Yeni şarkılar ekleyebilirsiniz!")
                return False # Kuyruk boş, çalacak bir şey yok

        # Kuyruktan bir sonraki şarkıyı al
        next_song = self.queues[guild_id].popleft()
        logger.info(f"play_next: Sıradaki şarkı: {next_song.get('title', 'Bilinmeyen Şarkı')}")

        # Kuyruk döngüsü için çalınan şarkıyı geçmişe ekle
        if loop_mode == "queue":
            if guild_id not in self.played_history: self.played_history[guild_id] = []
            song_copy_for_history = next_song.copy()
            if 'start_time' in song_copy_for_history: del song_copy_for_history['start_time']
            self.played_history[guild_id].append(song_copy_for_history)

        # Şarkı başlangıç zamanını kaydet ve now_playing'i güncelle
        next_song['start_time'] = datetime.datetime.now()
        self.now_playing[guild_id] = next_song

        try:
            # FFmpeg ile ses kaynağını oluştur
            audio_source = discord.FFmpegPCMAudio(next_song['url'], **self.ffmpeg_options)
        except Exception as e_audio:
            logger.error(f"play_next: Ses kaynağı oluşturulurken hata (Şarkı: {next_song.get('title')}): {e_audio}\n{traceback.format_exc()}")
            if ctx: await ctx.send(f"❌ **{next_song.get('title', 'Bu şarkı')}** çalınırken bir kaynak hatası oluştu. Atlanıyor.")
            # Sorunlu şarkıyı atlayıp bir sonrakini denemek için play_next'i tekrar çağır
            return await self.play_next(guild_id, ctx, from_callback=True) # from_callback=True önemli

        # Ses seviyesini ayarla
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=self.volume)

        # Şarkı bittiğinde çağrılacak callback fonksiyonu
        # ctx.channel.id'yi sakla, böylece ctx objesi olmadan da loglarda veya mesajlarda kullanılabilir
        current_ctx_channel_id = ctx.channel.id if ctx else None
        after_callback_fn = self.create_after_playing_callback(guild_id, current_ctx_channel_id)

        voice_client.play(volume_source, after=after_callback_fn)

        # Şarkı başladı mesajı (sadece kullanıcı komutuyla çağrıldıysa ve callback'ten değilse)
        if ctx and not from_callback:
            embed = discord.Embed(title="▶️ Şimdi Çalınıyor", description=f"**{next_song['title']}**", color=discord.Color.blue())
            embed.add_field(name="Süre", value=next_song.get('duration', 'Bilinmiyor'), inline=True)
            embed.add_field(name="Ekleyen", value=next_song.get('requester', 'Bilinmiyor'), inline=True)
            status_text = []
            current_loop_disp = self.loop_settings.get(guild_id, "off")
            if current_loop_disp != "off": status_text.append(f"Döngü: {current_loop_disp.capitalize()}")
            if self.shuffle_settings.get(guild_id, False): status_text.append("Karıştırma: Açık")
            if status_text: embed.add_field(name="Ayarlar", value=" | ".join(status_text), inline=False) # Tek satırda
            if next_song.get('thumbnail'): embed.set_thumbnail(url=next_song['thumbnail'])
            await ctx.send(embed=embed)
        return True

    async def seek(self, guild_id: int, position_seconds: int, ctx: Optional[commands.Context] = None) -> bool:
        lock = self.get_lock(guild_id)
        async with lock:
            guild = bot.get_guild(guild_id)
            if not guild:
                if ctx: await ctx.send("❌ Sunucu bilgisi bulunamadı."); return False
                return False # ctx yoksa logla ve çık (zaten loglandı)
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                if ctx: await ctx.send("❌ Ses kanalına bağlı değilim."); return False
                return False
            if not (voice_client.is_playing() or voice_client.is_paused()):
                if ctx: await ctx.send("❌ Şu anda çalan veya duraklatılmış bir şarkı yok."); return False
                return False

            current_song_info = self.now_playing.get(guild_id)
            if not current_song_info or not current_song_info.get('url'):
                if ctx: await ctx.send("❌ Şarkı bilgisi veya URL'si bulunamadı (seek)."); return False
                return False

            current_url = current_song_info['url']
            # Orijinal şarkı bilgilerini koru
            original_song_data = current_song_info.copy() # Kopyasını al, start_time hariç

            if position_seconds < 0: position_seconds = 0

            self.is_seeking[guild_id] = True
            logger.info(f"seek: is_seeking=True. Durduruluyor (Guild {guild_id}) seek: {position_seconds}s.")

            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop() # Bu, normal after_playing'i tetikleyebilir, is_seeking kontrolü orada önemli

            # FFmpeg options for seeking
            # -ss input öncesi (before_options) daha hızlıdır.
            ffmpeg_opts_for_seek = self.ffmpeg_options.copy()
            # Varolan before_options'a ekle
            current_before_options = ffmpeg_opts_for_seek.get('before_options', '')
            ffmpeg_opts_for_seek['before_options'] = f"-ss {position_seconds} {current_before_options}".strip()
            # options'daki -ss'i kaldır (eğer varsa)
            ffmpeg_opts_for_seek['options'] = ffmpeg_opts_for_seek['options'].replace(f"-ss {position_seconds}", "").strip()


            logger.info(f"seek: Streaming from {current_url} at {position_seconds}s. FFmpeg options: {ffmpeg_opts_for_seek}")

            try:
                audio_source = discord.FFmpegPCMAudio(current_url, **ffmpeg_opts_for_seek)
                volume_source = discord.PCMVolumeTransformer(audio_source, volume=self.volume)

                def after_playing_for_seek(error):
                    if error: logger.error(f"seek: Hata (playback after seek, Guild {guild_id}): {error}")
                    else: logger.info(f"seek: Bitti (playback after seek, Guild {guild_id}).")

                    self.is_seeking[guild_id] = False # ÖNEMLİ: Seek bittiğinde bayrağı sıfırla
                    logger.info(f"seek: is_seeking=False (seek's own callback, Guild {guild_id}).")

                    # play_next'i çağır (ctx olmadan, mesajsız)
                    coro = self.play_next(guild_id, ctx=None, from_callback=True)
                    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                    try: future.result()
                    except Exception as e_play_next:
                        logger.error(f"seek: Hata (play_next after seek, {guild_id}): {e_play_next}\n{traceback.format_exc()}")

                # now_playing'i güncelle, start_time'ı seek pozisyonuna göre ayarla
                # Orijinal şarkı verilerini kullan, sadece start_time değişecek
                original_song_data['start_time'] = datetime.datetime.now() - datetime.timedelta(seconds=position_seconds)
                self.now_playing[guild_id] = original_song_data
                logger.info(f"seek: now_playing güncellendi (Guild {guild_id}), yeni start_time seek'e göre.")

                voice_client.play(volume_source, after=after_playing_for_seek)
                logger.info(f"seek: Başladı ({position_seconds}s) '{original_song_data.get('title')}'.")

                if ctx:
                    minutes, secs_display = divmod(position_seconds, 60)
                    await ctx.send(f"⏩ **{original_song_data.get('title', 'Şarkı')}** {minutes:02d}:{secs_display:02d} konumuna atlandı.")
                return True

            except Exception as e:
                logger.error(f"seek: Hata (seek operation, {guild_id}): {e}\n{traceback.format_exc()}")
                self.is_seeking[guild_id] = False # Hata durumunda bayrağı sıfırla
                logger.info(f"seek: is_seeking=False (exception, Guild {guild_id}).")
                if ctx: await ctx.send(f"❌ Şarkı konumuna gidilirken bir hata oluştu: {str(e)[:100]}")
                return False

    async def forward(self, guild_id: int, seconds_to_forward: int, ctx: Optional[commands.Context] = None) -> bool:
        current_song = self.now_playing.get(guild_id)
        if not current_song or not current_song.get('start_time'):
            if ctx: await ctx.send("⚠️ Şarkı başlangıç zamanı bilinmiyor, süre hesaplanamıyor (forward)."); return False
            return False

        elapsed_time = (datetime.datetime.now(datetime.timezone.utc) - current_song['start_time'].replace(tzinfo=datetime.timezone.utc)).total_seconds()
        new_position = int(elapsed_time + seconds_to_forward)

        if ctx:
            minutes, secs_display = divmod(seconds_to_forward, 60)
            await ctx.send(f"⏩ Şarkı {minutes:02d}:{secs_display:02d} ileri sarılıyor...")
        return await self.seek(guild_id, new_position, ctx)

    async def rewind(self, guild_id: int, seconds_to_rewind: int, ctx: Optional[commands.Context] = None) -> bool:
        current_song = self.now_playing.get(guild_id)
        if not current_song or not current_song.get('start_time'):
            if ctx: await ctx.send("⚠️ Şarkı başlangıç zamanı bilinmiyor, süre hesaplanamıyor (rewind)."); return False
            return False
        elapsed_time = (datetime.datetime.now(datetime.timezone.utc) - current_song['start_time'].replace(tzinfo=datetime.timezone.utc)).total_seconds()
        new_position = int(max(0, elapsed_time - seconds_to_rewind))

        if ctx:
            minutes, secs_display = divmod(seconds_to_rewind, 60)
            await ctx.send(f"⏪ Şarkı {minutes:02d}:{secs_display:02d} geri sarılıyor...")
        return await self.seek(guild_id, new_position, ctx)

    def toggle_loop(self, guild_id: int) -> str:
        current_setting = self.loop_settings.get(guild_id, "off")
        if current_setting == "off": new_setting = "song"
        elif current_setting == "song": new_setting = "queue"
        else: new_setting = "off" # queue -> off
        self.loop_settings[guild_id] = new_setting
        if new_setting == "off" and guild_id in self.played_history:
            self.played_history[guild_id] = [] # Döngü kapanınca geçmişi temizle
        logger.info(f"Döngü modu {guild_id} için {new_setting} olarak ayarlandı.")
        return new_setting

    def toggle_shuffle(self, guild_id: int) -> bool: # Bu komut kaldırılmıştı, gerekirse aktif edilebilir
        current_setting = self.shuffle_settings.get(guild_id, False)
        new_setting = not current_setting
        self.shuffle_settings[guild_id] = new_setting
        if new_setting and self.queues.get(guild_id):
            self.shuffle_queue(guild_id) # Karıştırma açılırsa ve kuyruk varsa hemen karıştır
        logger.info(f"Karıştırma modu {guild_id} için {'açık' if new_setting else 'kapalı'} olarak ayarlandı.")
        return new_setting

    def shuffle_queue(self, guild_id: int) -> bool:
        if guild_id not in self.queues or not self.queues[guild_id]:
            return False
        queue_list = list(self.queues[guild_id])
        random.shuffle(queue_list)
        self.queues[guild_id] = deque(queue_list)
        logger.info(f"Kuyruk {guild_id} için karıştırıldı.")
        return True

    async def cleanup(self, guild_id: int):
        logger.info(f"Sunucu {guild_id} için müzik kaynakları temizleniyor/sıfırlanıyor...")
        vc = self.voice_clients.get(guild_id)
        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused(): vc.stop() # Önce çalmayı durdur
            await vc.disconnect(force=False) # force=False daha nazik ayrılır
        # voice_clients'tan silmek yerine, yeni bağlantıda üzerine yazılır.
        # self.voice_clients.pop(guild_id, None)

        if guild_id in self.queues: self.queues[guild_id].clear()
        self.now_playing.pop(guild_id, None)
        self.loop_settings.pop(guild_id, None)
        self.shuffle_settings.pop(guild_id, None)
        self.played_history.pop(guild_id, None)
        self.is_seeking.pop(guild_id, None)
        # self.locks.pop(guild_id, None) # Kilitleri silmek yerine yeniden kullanmak daha iyi
        logger.info(f"Sunucu {guild_id} için müzik kaynakları temizlendi.")

music_player = MusicPlayer() # MusicPlayer örneği burada oluşturuluyor

# --- API Anahtarı Kontrolleri ---
if not DISCORD_TOKEN: logger.critical("HATA: Discord Token bulunamadı!"); sys.exit(1)
if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); sys.exit(1)

if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
    logger.critical("HATA: Ne Gemini ne de OpenRouter API Anahtarı bulunamadı! En az biri gerekli.")
    sys.exit(1)
if not GEMINI_API_KEY:
    logger.warning("UYARI: Gemini API Anahtarı bulunamadı! Gemini modelleri kullanılamayacak.")
if not OPENROUTER_API_KEY:
    logger.warning("UYARI: OpenRouter API Anahtarı bulunamadı! DeepSeek (OpenRouter üzerinden) kullanılamayacak.")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.error(">>> HATA: 'requests' kütüphanesi bulunamadı. Lütfen 'pip install requests' ile kurun.")
    if OPENROUTER_API_KEY:
        logger.error("HATA: OpenRouter API anahtarı bulundu ancak 'requests' kütüphanesi yüklenemedi. DeepSeek kullanılamayacak.")
        OPENROUTER_API_KEY = None # Kullanılamaz hale getir


# --- Veritabanı Yardımcı Fonksiyonları (Devamı) ---
def check_volume_table_structure_db():
    conn = None
    try:
        conn = db_connect()
        if not conn: return None
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'volume_settings')")
            if not cur.fetchone()[0]: return None # Tablo yok
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'volume_settings'")
            return [row[0] for row in cur.fetchall()]
    except psycopg2.Error as e:
        logger.error(f"Ses ayarları tablosu yapısı kontrol edilirken hata: {e}")
        return None
    finally:
        if conn: release_db_connection(conn)

def setup_volume_table_db():
    conn = None
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları tablosu oluşturulamadı: Veritabanı bağlantısı kurulamadı.")
            return False
        columns = check_volume_table_structure_db()
        with conn.cursor() as cur:
            if columns is None: # Tablo yok, yeni oluştur
                cur.execute("""
                    CREATE TABLE volume_settings (
                        id SERIAL PRIMARY KEY,
                        setting_key VARCHAR(50) UNIQUE NOT NULL,
                        setting_value FLOAT NOT NULL
                    )""")
                # Varsayılan değerleri ekle (ON CONFLICT ile)
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', 0.5) ON CONFLICT (setting_key) DO NOTHING")
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', 0.5) ON CONFLICT (setting_key) DO NOTHING")
                conn.commit()
                logger.info("Ses ayarları tablosu oluşturuldu ve varsayılan değerler eklendi.")
            elif not ('setting_key' in columns and 'setting_value' in columns): # Uyumsuz şema
                logger.warning("Ses ayarları tablosu uyumsuz şema ile mevcut, yeniden oluşturulacak.")
                cur.execute("DROP TABLE IF EXISTS volume_settings") # Önce sil
                cur.execute("""
                    CREATE TABLE volume_settings (
                        id SERIAL PRIMARY KEY,
                        setting_key VARCHAR(50) UNIQUE NOT NULL,
                        setting_value FLOAT NOT NULL
                    )""")
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', 0.5) ON CONFLICT (setting_key) DO NOTHING")
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', 0.5) ON CONFLICT (setting_key) DO NOTHING")
                conn.commit()
                logger.info("Ses ayarları tablosu yeniden oluşturuldu ve varsayılan değerler eklendi.")
            else: # Zaten var ve uyumlu
                logger.debug("Ses ayarları tablosu zaten mevcut ve uyumlu.")
            return True
    except psycopg2.Error as e:
        logger.error(f"Ses ayarları tablosu oluşturulurken/güncellenirken hata: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: release_db_connection(conn)

def save_volume_settings_db(current_volume: float, default_volume: float):
    conn = None
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları kaydedilemedi: Veritabanı bağlantısı kurulamadı."); return False
        if not setup_volume_table_db(): # Tablo yoksa veya uyumsuzsa oluşturur/düzeltir
             logger.error("Ses ayarları tablosu hazırlanamadı (kayıt sırasında)."); return False

        with conn.cursor() as cur:
            # UPSERT (Update or Insert)
            cur.execute("""
                INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """, (current_volume,))
            cur.execute("""
                INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """, (default_volume,))
            conn.commit()
            logger.debug(f"Ses ayarları kaydedildi: current={current_volume}, default={default_volume}")
            return True
    except Exception as e:
        logger.error(f"Ses ayarları kaydedilirken hata: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: release_db_connection(conn)

def get_volume_settings_db() -> tuple[float, float]:
    conn = None
    default_vals_tuple = (0.5, 0.5) # Değişken adını değiştirdim
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları yüklenemedi: Veritabanı bağlantısı kurulamadı."); return default_vals_tuple
        if not setup_volume_table_db(): # Tablo yoksa oluşturur/düzeltir ve varsayılanları ekler
            logger.error("Ses ayarları tablosu hazırlanamadı (getirme sırasında).")
            # setup_volume_table_db varsayılanları eklediği için, bu çağrıdan sonra okuma başarılı olmalı.
            # Eğer setup_volume_table_db False dönerse, varsayılanı döndürmek mantıklı.
            return default_vals_tuple

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT setting_key, setting_value FROM volume_settings WHERE setting_key IN ('current_volume', 'default_volume')")
            rows = cur.fetchall()
            if not rows: # Bu durum setup_volume_table_db doğru çalışıyorsa olmamalı.
                logger.warning("Veritabanından ses ayarı okunamadı (tablo boş olabilir). Varsayılanlar kullanılıyor.")
                # Varsayılanları tekrar eklemeyi dene (güvenlik için, setup_volume_table_db bunu yapmalıydı)
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', %s) ON CONFLICT (setting_key) DO NOTHING", (default_vals_tuple[0],))
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', %s) ON CONFLICT (setting_key) DO NOTHING", (default_vals_tuple[1],))
                conn.commit()
                return default_vals_tuple

            current_v = default_vals_tuple[0] # Değişken adlarını değiştirdim
            default_v = default_vals_tuple[1]
            for row in rows:
                if row['setting_key'] == 'current_volume': current_v = float(row['setting_value'])
                elif row['setting_key'] == 'default_volume': default_v = float(row['setting_value'])
            return current_v, default_v
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Ses ayarları yüklenirken hata: {e}")
        return default_vals_tuple
    finally:
        if conn: release_db_connection(conn)

def setup_database(): # Genel DB kurulumu
    conn = None
    try:
        conn = db_connect()
        if not conn:
            logger.critical("Veritabanı bağlantısı kurulamadı (setup_database). Çıkılıyor.")
            sys.exit(1)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        default_model_for_db = DEFAULT_MODEL_NAME # Değişken adını değiştirdim
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS temp_channels (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_active TIMESTAMPTZ NOT NULL,
                model_name TEXT DEFAULT %s
            )
        ''', (default_model_for_db,))
        conn.commit()
        logger.info("PostgreSQL 'config' ve 'temp_channels' tabloları kontrol edildi/oluşturuldu.")
        # Ses ayarları tablosunu da burada kontrol et/oluştur
        if not setup_volume_table_db():
            logger.error("Başlangıçta ses ayarları tablosu ('volume_settings') oluşturulamadı.")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.critical(f"PostgreSQL veritabanı kurulumu sırasında KRİTİK HATA: {e}")
        if conn: conn.rollback()
        sys.exit(1)
    finally:
        if conn: release_db_connection(conn)

def save_config_db(key, value):
    conn = None
    sql = "INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    try:
        conn = db_connect()
        if not conn: logger.error(f"Yapılandırma kaydedilemedi (Key: {key}): Veritabanı bağlantısı yok."); return
        cursor = conn.cursor()
        cursor.execute(sql, (key, str(value)))
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Yapılandırma ayarı kaydedilirken hata (Key: {key}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: release_db_connection(conn)

def load_config_db(key, default=None):
    conn = None
    sql = "SELECT value FROM config WHERE key = %s;"
    try:
        conn = db_connect()
        if not conn: logger.error(f"Yapılandırma yüklenemedi (Key: {key}): Veritabanı bağlantısı yok."); return default
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute(sql, (key,))
        result = cursor.fetchone()
        return result['value'] if result else default
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Yapılandırma yüklenirken PostgreSQL hatası (Key: {key}): {e}")
        return default
    finally:
        if conn: release_db_connection(conn)

# Diğer DB fonksiyonları (add_temp_channel_db, remove_temp_channel_db vb.) önceki versiyondaki gibi kalabilir.
# Sadece db_connect() ve release_db_connection() kullandıklarından emin olun.
def update_channel_model_db(channel_id, model_with_prefix):
     conn = None
     sql = "UPDATE temp_channels SET model_name = %s WHERE channel_id = %s;"
     try:
          conn = db_connect()
          if not conn: return
          cursor = conn.cursor()
          if model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and model_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
               model_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
          cursor.execute(sql, (model_with_prefix, channel_id))
          conn.commit()
          logger.info(f"DB'deki Kanal {channel_id} modeli {model_with_prefix} olarak güncellendi.")
     except (Exception, psycopg2.DatabaseError) as e:
          logger.error(f"DB Kanal modeli güncellenirken hata (channel_id: {channel_id}): {e}")
          if conn: conn.rollback()
     finally:
          if conn: release_db_connection(conn)

def add_temp_channel_db(channel_id, user_id, timestamp, model_used_with_prefix):
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
        if not conn: return
        cursor = conn.cursor()
        if timestamp.tzinfo is None:
             timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        if model_used_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and model_used_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
             model_used_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
        cursor.execute(sql, (channel_id, user_id, timestamp, model_used_with_prefix))
        conn.commit()
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal PostgreSQL'e eklenirken/güncellenirken hata (channel_id: {channel_id}, model: {model_used_with_prefix}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: release_db_connection(conn)

def remove_temp_channel_db(channel_id):
    conn = None
    sql = "DELETE FROM temp_channels WHERE channel_id = %s;"
    try:
        conn = db_connect()
        if not conn: return
        cursor = conn.cursor()
        cursor.execute(sql, (channel_id,))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Geçici kanal {channel_id} PostgreSQL'den silindi.")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal silinirken PostgreSQL hatası (channel_id: {channel_id}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: release_db_connection(conn)

def update_channel_activity_db(channel_id, timestamp):
     conn = None
     sql = "UPDATE temp_channels SET last_active = %s WHERE channel_id = %s;"
     try:
          conn = db_connect()
          if not conn: return
          cursor = conn.cursor()
          if timestamp.tzinfo is None: # Emin olmak için
               timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
          cursor.execute(sql, (timestamp, channel_id))
          conn.commit()
     except (Exception, psycopg2.DatabaseError) as e:
          logger.error(f"DB Kanal aktivitesi güncellenirken hata (channel_id: {channel_id}): {e}")
          if conn: conn.rollback()
     finally:
          if conn: release_db_connection(conn)


# --- Yapılandırma Yükleme (Başlangıçta on_ready içinde yapılacak) ---
# entry_channel_id ve inactivity_timeout on_ready içinde yüklenecek.

# Gemini API Yapılandırması
gemini_default_model_instance = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("Gemini API anahtarı yapılandırıldı.")
        # DEFAULT_GEMINI_MODEL_NAME 'models/' prefixi içermemeli
        gemini_default_model_instance = genai.GenerativeModel(f"models/{DEFAULT_GEMINI_MODEL_NAME}")
        logger.info(f".ask komutu için varsayılan Gemini modeli ('{DEFAULT_GEMINI_MODEL_NAME}') yüklendi.")
    except Exception as configure_error:
        logger.error(f"HATA: Gemini API yapılandırma/model yükleme hatası: {configure_error}")
        GEMINI_API_KEY = None # Hata varsa anahtarı yok say
        gemini_default_model_instance = None
else:
    logger.warning("Gemini API anahtarı ayarlanmadığı için Gemini özellikleri devre dışı.")


# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

class CustomBot(commands.Bot): # CustomBot sınıfı korunuyor
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def get_context(self, message, *, cls=commands.Context):
        return await super().get_context(message, cls=cls)

bot = CustomBot(command_prefix=['!', '.'], intents=intents, help_command=None)

# --- Komut İzleme Sistemi ---
@bot.event
async def on_command(ctx: commands.Context):
    if ctx.command and ctx.command.name in exempt_commands:
        logger.debug(f"Komut izleme sisteminden muaf tutuldu: {ctx.command.name}")
        return
    command_id = f"{ctx.channel.id}-{ctx.message.id}"
    if command_id in processed_commands:
        logger.warning(f"Komut zaten işlendi (on_command), tekrar işlenmeyecek: {ctx.command.name} (ID: {command_id})")
        # Komutu iptal etmenin bir yolu olarak hata fırlatabiliriz.
        # Bu, on_command_error tarafından yakalanır.
        raise commands.CommandError("Duplicate command invocation prevented by on_command.")
    processed_commands[command_id] = datetime.datetime.now()
    logger.debug(f"Komut işleme başladı (on_command): {ctx.command.name} (ID: {command_id})")

@tasks.loop(minutes=1)
async def cleanup_command_tracking():
    global processed_commands
    now = datetime.datetime.now()
    timeout_seconds = 60 # 60 saniyeden eski kayıtları sil
    expired_commands = [cmd_id for cmd_id, ts in processed_commands.items() if (now - ts).total_seconds() > timeout_seconds]
    for cmd_id in expired_commands:
        processed_commands.pop(cmd_id, None)
    if expired_commands:
        logger.debug(f"{len(expired_commands)} eski komut izleme kaydı (timeout: {timeout_seconds}s) temizlendi.")

@cleanup_command_tracking.before_loop
async def before_cleanup_command_tracking():
    await bot.wait_until_ready()
    logger.info("Komut izleme temizleme görevi başlatıldı.")

# create_private_chat_channel ve send_to_ai_and_respond fonksiyonları önceki mesajdaki gibi kalabilir.
# Onları buraya tekrar eklemiyorum, çok uzayacak. Önceki mesajdaki halleriyle uyumlular.
async def create_private_chat_channel(guild: discord.Guild, author: discord.Member):
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

    while True:
        if len(channel_name) > 95:
            channel_name = f"{base_channel_name[:95-len(str(counter))-1]}-{counter}"
        if channel_name.lower() not in existing_channel_names:
            break
        counter += 1
        channel_name = f"{base_channel_name}-{counter}"
        if counter > 100:
            timestamp_str = datetime.datetime.now().strftime('%M%S%f')[:-3]
            channel_name = f"sohbet-{author.id}-{timestamp_str}"[:100]
            if channel_name.lower() in existing_channel_names:
                logger.error(f"{author.name} için alternatif rastgele kanal adı da mevcut: {channel_name}")
                return None
            logger.warning(f"Benzersiz kanal adı bulunamadı, rastgele kullanılıyor: {channel_name}")
            break
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
        logger.error(f"Kanal oluşturulamadı '{channel_name}': Botun 'Kanalları Yönet' izni yok.")
        return None
    except discord.errors.HTTPException as http_e:
        logger.error(f"Kanal oluşturulamadı '{channel_name}': Discord API hatası: {http_e.status} {http_e.code} - {http_e.text}")
        return None
    except Exception as e:
        logger.error(f"Kanal '{channel_name}' oluşturmada beklenmedik hata: {e}\n{traceback.format_exc()}")
        return None

async def send_to_ai_and_respond(channel: discord.TextChannel, author: discord.Member, prompt_text: str, channel_id: int):
    global channel_last_active, active_ai_chats, warned_inactive_channels
    if not prompt_text or not prompt_text.strip(): return False
    if prompt_text.startswith(tuple(bot.command_prefix)):
        potential_command = prompt_text.split()[0][1:] if prompt_text.split() else ""
        if potential_command and bot.get_command(potential_command):
            return False
    if channel_id not in active_ai_chats:
        try:
            conn = db_connect()
            if not conn: raise ConnectionError("Veritabanı bağlantısı kurulamadı (AI chat init).")
            cursor = conn.cursor(cursor_factory=DictCursor)
            cursor.execute("SELECT model_name FROM temp_channels WHERE channel_id = %s", (channel_id,))
            result = cursor.fetchone()
            release_db_connection(conn)
            current_model_with_prefix = (result['model_name'] if result and result['model_name'] else DEFAULT_MODEL_NAME)
            if not current_model_with_prefix.startswith(GEMINI_PREFIX) and not current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                current_model_with_prefix = DEFAULT_MODEL_NAME
                update_channel_model_db(channel_id, DEFAULT_MODEL_NAME)
            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX) and current_model_with_prefix != f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}":
                current_model_with_prefix = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
                update_channel_model_db(channel_id, current_model_with_prefix)
            logger.info(f"'{channel.name}' (ID: {channel_id}) için AI sohbet oturumu {current_model_with_prefix} ile başlatılıyor.")
            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                if not GEMINI_API_KEY: raise ValueError("Gemini API anahtarı ayarlı değil.")
                actual_model_name = current_model_with_prefix[len(GEMINI_PREFIX):]
                target_gemini_name = f"models/{actual_model_name}"
                try:
                    gemini_model_instance = genai.GenerativeModel(target_gemini_name)
                except Exception as model_err:
                    if not DEFAULT_MODEL_NAME.startswith(GEMINI_PREFIX) or not GEMINI_API_KEY:
                        raise ValueError("Varsayılan model Gemini değil veya Gemini API anahtarı yok.")
                    current_model_with_prefix = DEFAULT_MODEL_NAME
                    update_channel_model_db(channel_id, DEFAULT_MODEL_NAME)
                    actual_model_name = DEFAULT_MODEL_NAME[len(GEMINI_PREFIX):]
                    gemini_model_instance = genai.GenerativeModel(f"models/{actual_model_name}")
                active_ai_chats[channel_id] = {'model': current_model_with_prefix, 'session': gemini_model_instance.start_chat(history=[]), 'history': None}
            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                if not OPENROUTER_API_KEY: raise ValueError("OpenRouter API anahtarı ayarlı değil.")
                if not REQUESTS_AVAILABLE: raise ImportError("'requests' kütüphanesi bulunamadı.")
                active_ai_chats[channel_id] = {'model': current_model_with_prefix, 'session': None, 'history': []}
            else: raise ValueError(f"Tanımsız model ön eki: {current_model_with_prefix}")
        except (psycopg2.Error, ConnectionError, ValueError, ImportError) as init_err:
            logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı: {init_err}")
            try: await channel.send("Yapay zeka oturumu başlatılamadı.", delete_after=15)
            except: pass
            active_ai_chats.pop(channel_id, None)
            return False
        except Exception as e:
            logger.error(f"'{channel.name}' için AI sohbet oturumu başlatılamadı (Genel Hata): {e}\n{traceback.format_exc()}")
            try: await channel.send("Yapay zeka oturumu başlatılamadı (beklenmedik hata).", delete_after=15)
            except: pass
            active_ai_chats.pop(channel_id, None)
            return False
    if channel_id not in active_ai_chats:
        logger.error(f"Kritik Hata: Kanal {channel_id} için aktif sohbet verisi bulunamadı.")
        try: await channel.send("Sohbet durumu bulunamadı.", delete_after=15)
        except: pass
        return False

    chat_data = active_ai_chats[channel_id]
    current_model_with_prefix = chat_data['model']
    logger.info(f"[AI CHAT/{current_model_with_prefix}] [{author.name} @ {channel.name}] prompt: {prompt_text[:100]}{'...' if len(prompt_text)>100 else ''}")
    ai_response_text = None; error_occurred = False; user_error_msg = "Yapay zeka ile iletişimde bir sorun oluştu."
    async with channel.typing():
        try:
            if current_model_with_prefix.startswith(GEMINI_PREFIX):
                gemini_session = chat_data.get('session')
                if not gemini_session: raise ValueError("Gemini oturumu bulunamadı.")
                response = await asyncio.to_thread(gemini_session.send_message, prompt_text)
                ai_response_text = response.text.strip()
                finish_reason = getattr(response.candidates[0].finish_reason, 'name', None) if response.candidates else None
                prompt_feedback_reason = getattr(response.prompt_feedback.block_reason, 'name', None) if hasattr(response, 'prompt_feedback') and hasattr(response.prompt_feedback, 'block_reason') else None
                if prompt_feedback_reason == "SAFETY": user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı."; error_occurred = True;
                elif finish_reason == "SAFETY": user_error_msg = "Yanıt güvenlik filtrelerine takıldı."; error_occurred = True; ai_response_text = None;
                elif finish_reason == "RECITATION": user_error_msg = "Yanıt, alıntı filtrelerine takıldı."; error_occurred = True; ai_response_text = None;
                elif finish_reason == "OTHER": user_error_msg = "Yanıt oluşturulamadı (bilinmeyen sebep)."; error_occurred = True; ai_response_text = None;
            elif current_model_with_prefix.startswith(DEEPSEEK_OPENROUTER_PREFIX):
                history = chat_data.get('history'); target_model_name = OPENROUTER_DEEPSEEK_MODEL_NAME
                history.append({"role": "user", "content": prompt_text})
                headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
                if OPENROUTER_SITE_URL: headers["HTTP-Referer"] = OPENROUTER_SITE_URL
                if OPENROUTER_SITE_NAME: headers["X-Title"] = OPENROUTER_SITE_NAME
                payload = {"model": target_model_name, "messages": history, "max_tokens": 2048}
                api_response = await asyncio.to_thread(requests.post, OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)
                api_response.raise_for_status()
                response_data = api_response.json()
                if response_data and response_data.get("choices"):
                    choice = response_data["choices"][0]
                    if choice.get("message") and choice["message"].get("content"):
                        ai_response_text = choice["message"]["content"].strip()
                        history.append({"role": "assistant", "content": ai_response_text})
                        finish_reason = choice.get("finish_reason")
                        if finish_reason == 'length': logger.warning("OpenRouter yanıtı max_tokens sınırına ulaştı.")
                        elif finish_reason == 'content_filter': user_error_msg = "Yanıt içerik filtrelerine takıldı."; error_occurred = True; ai_response_text = None;
                    else: error_occurred = True; user_error_msg = "Yapay zekadan geçerli yanıt formatı alınamadı.";
                else: error_occurred = True; user_error_msg = "Yapay zekadan yanıt alınamadı.";
                if error_occurred and history and history[-1]["role"] == "user": history.pop()
            else: error_occurred = True; user_error_msg = "Bilinmeyen model."

            if not error_occurred and ai_response_text:
                for i in range(0, len(ai_response_text), 2000):
                    await channel.send(ai_response_text[i:i+2000])
                    if i + 2000 < len(ai_response_text): await asyncio.sleep(0.5)
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                channel_last_active[channel_id] = now_utc
                update_channel_activity_db(channel_id, now_utc)
                warned_inactive_channels.discard(channel_id)
                return True
            elif not error_occurred and not ai_response_text:
                logger.info(f"AI'dan boş yanıt (Kanal: {channel_id}).")
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                channel_last_active[channel_id] = now_utc
                update_channel_activity_db(channel_id, now_utc)
                warned_inactive_channels.discard(channel_id)
                return True
        except requests.exceptions.Timeout: error_occurred = True; user_error_msg = "Yapay zeka sunucusu zaman aşımına uğradı."
        except requests.exceptions.RequestException as req_e:
            error_occurred = True; user_error_msg = f"API Hatası ({str(req_e)[:50]})" # Daha kısa
            if req_e.response is not None:
                 if req_e.response.status_code == 401: user_error_msg = "OpenRouter API Anahtarı geçersiz."
                 elif req_e.response.status_code == 402: user_error_msg = "OpenRouter krediniz yetersiz."
                 elif req_e.response.status_code == 429: user_error_msg = "OpenRouter API limiti aşıldı."
            if chat_data and chat_data.get('model','').startswith(DEEPSEEK_OPENROUTER_PREFIX) and chat_data.get('history') and chat_data['history'][-1]["role"] == "user": chat_data['history'].pop()
        except (genai.types.StopCandidateException, genai.types.BlockedPromptException) as gemini_safety_e:
            error_occurred = True; user_error_msg = "Mesajınız/yanıt güvenlik filtrelerine takıldı."; ai_response_text = None;
        except Exception as e:
            if not error_occurred: logger.error(f"[AI CHAT] Hata: {e}\n{traceback.format_exc()}"); error_occurred = True;
    if error_occurred:
        try: await channel.send(f"⚠️ {user_error_msg}", delete_after=20)
        except: pass
        return False
    return False

# --- Bot Olayları (on_ready, on_message) ---
@bot.event
async def on_ready():
    global entry_channel_id, inactivity_timeout, initial_ready_complete # initial_ready_complete'i global yap

    # Bu blok sadece ilk on_ready çağrısında çalışsın
    if not initial_ready_complete:
        logger.info(f"{bot.user.name} olarak giriş yapıldı (ID: {bot.user.id})")
        logger.info(f"Discord.py Sürümü: {discord.__version__}")

        if not init_db_pool():
            logger.critical("DB Havuzu başlatılamadı, bot düzgün çalışmayabilir.")
        setup_database()

        entry_channel_id_str = load_config_db('entry_channel_id', str(DEFAULT_ENTRY_CHANNEL_ID))
        # ... (diğer config yüklemeleri) ...

        logger.info("Render'daki olası çerez dosyası yolları kontrol ediliyor...")
        possible_cookie_paths = [
            "/etc/secrets/cookies.txt",            # Render Secret Files için en standart ve öncelikli yol
            "/var/run/secrets/cookies.txt",        # Başka bir olası secret yolu
            "cookies.txt",                         # Çalışma dizini (son çare)
            "/opt/render/project/src/cookies.txt"  # Tipik Render çalışma dizini
        ]
        found_cookie_path = None
        for path_to_check in possible_cookie_paths:
            if os.path.exists(path_to_check):
                logger.info(f"BULUNDU: Çerez dosyası şu yolda mevcut: {path_to_check}")
                found_cookie_path = path_to_check
                break
            else:
                logger.info(f"Bulunamadı (denenen yol): {path_to_check}")

        if found_cookie_path:
            music_player.set_cookie_file_for_ytdlp(found_cookie_path)
        else:
            logger.error("KRİTİK: Render'da 'cookies.txt' dosyası bulunamadı! YouTube indirmeleri başarısız olabilir.")
            music_player.set_cookie_file_for_ytdlp(None)

        await music_player.load_volume_settings()

        if not check_inactivity.is_running(): check_inactivity.start()
        if not cleanup_command_tracking.is_running(): cleanup_command_tracking.start()

        initial_ready_complete = True # Bayrağı set et
    else:
        logger.info(f"Bot yeniden bağlandı (on_ready tekrar tetiklendi), başlangıç ayarları atlanıyor.")


    # Aktivite ayarlama her on_ready'de yapılabilir
    activity_name = "!help | AI & Music"
    if entry_channel_id: # entry_channel_id'nin globalden okunması lazım
        try:
            entry_ch_obj = await bot.fetch_channel(entry_channel_id)
            if entry_ch_obj: activity_name = f"#{entry_ch_obj.name} | AI Chat"
        except: pass
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=activity_name))
    logger.info(f"Bot {len(bot.guilds)} sunucuda aktif. Aktivite: '{activity_name}'")
    logger.info("Bot komutları ve mesajları dinliyor (veya yeniden bağlandı)...");



@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or message.author.bot: return
    if not message.guild or not isinstance(message.channel, discord.TextChannel): return

    prefixes = await bot.get_prefix(message)
    if isinstance(prefixes, str): prefixes = [prefixes]
    is_potential_command = False; cleaned_content = message.content.strip()
    for pfx_check in prefixes:
        if cleaned_content.startswith(pfx_check):
            cmd_name_match = cleaned_content[len(pfx_check):].split(" ")[0]
            if cmd_name_match and bot.get_command(cmd_name_match):
                is_potential_command = True; break
    
    if is_potential_command:
        command_id_msg = f"{message.channel.id}-{message.id}"
        cmd_name_log_msg = cleaned_content.split(" ")[0]
        if cmd_name_log_msg not in exempt_commands and command_id_msg in processed_commands:
            logger.warning(f"on_message: Komut zaten işlendi (ID: {command_id_msg}), tekrar işlenmeyecek.")
            return
        elif cmd_name_log_msg not in exempt_commands:
            processed_commands[command_id_msg] = datetime.datetime.now()
        await bot.process_commands(message)
        return

    # AI Mesaj İşleme (Eğer Komut Değilse)
    channel_id_ai = message.channel.id; author_ai = message.author; guild_ai = message.guild
    if entry_channel_id and channel_id_ai == entry_channel_id:
        if author_ai.id in user_to_channel_map:
            active_ch_id = user_to_channel_map[author_ai.id]
            active_ch_obj = bot.get_channel(active_ch_id)
            if active_ch_obj:
                try:
                    await message.channel.send(f"{author_ai.mention}, zaten aktif bir özel sohbet kanalın var: {active_ch_obj.mention}", delete_after=15)
                    await message.delete(delay=15)
                except: pass
                return
            else: user_to_channel_map.pop(author_ai.id, None); remove_temp_channel_db(active_ch_id)
        initial_prompt_ai = message.content
        if not initial_prompt_ai.strip():
            try: await message.delete()
            except: pass
            return
        chosen_model = user_next_model.pop(author_ai.id, DEFAULT_MODEL_NAME)
        if chosen_model.startswith(DEEPSEEK_OPENROUTER_PREFIX):
             chosen_model = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
        processing_msg_ai = None
        try: processing_msg_ai = await message.channel.send(f"{author_ai.mention}, özel sohbet kanalın oluşturuluyor...", delete_after=10)
        except: pass
        new_ch_ai = await create_private_chat_channel(guild_ai, author_ai)
        if new_ch_ai:
            temporary_chat_channels.add(new_ch_ai.id); user_to_channel_map[author_ai.id] = new_ch_ai.id
            now_utc_ai = datetime.datetime.now(datetime.timezone.utc)
            channel_last_active[new_ch_ai.id] = now_utc_ai
            add_temp_channel_db(new_ch_ai.id, author_ai.id, now_utc_ai, chosen_model)
            display_model_ai = chosen_model.split(':')[-1]
            embed_ai = discord.Embed(title="👋 Özel Yapay Zeka Sohbeti Başlatıldı!",
                                  description=f"Merhaba {author_ai.mention}!\nBu kanalda `{display_model_ai}` modeli ile sohbet edeceksin.",
                                  color=discord.Color.og_blurple())
            if bot.user and bot.user.display_avatar: embed_ai.set_thumbnail(url=bot.user.display_avatar.url)
            timeout_disp_ai = f"`{inactivity_timeout.total_seconds()/3600:.1f}` saat" if inactivity_timeout else "Asla"
            embed_ai.add_field(name="⏳ Otomatik Kapanma", value=f"Kanal {timeout_disp_ai} işlem görmezse silinir.", inline=False)
            pfx_ai = prefixes[0]
            embed_ai.add_field(name="🛑 Kapat", value=f"`{pfx_ai}endchat`", inline=True)
            embed_ai.add_field(name="🔄 Model Seç", value=f"`{pfx_ai}setmodel <model>`", inline=True)
            embed_ai.add_field(name="💬 Sıfırla", value=f"`{pfx_ai}resetchat`", inline=True)
            try: await new_ch_ai.send(embed=embed_ai)
            except: pass
            if processing_msg_ai:
                try: await processing_msg_ai.delete()
                except: pass
            try: await message.channel.send(f"{author_ai.mention}, özel sohbet kanalı {new_ch_ai.mention} oluşturuldu!", delete_after=20)
            except: pass
            try: await message.delete(delay=20)
            except: pass
            await send_to_ai_and_respond(new_ch_ai, author_ai, initial_prompt_ai, new_ch_ai.id)
        else:
            if processing_msg_ai:
                try: await processing_msg_ai.edit(content=f"{author_ai.mention}, özel sohbet kanalın oluşturulamadı.", delete_after=15)
                except: pass
            try: await message.delete(delay=15)
            except: pass
        return
    if channel_id_ai in temporary_chat_channels:
        await send_to_ai_and_respond(message.channel, author_ai, message.content, channel_id_ai)

# check_inactivity, on_guild_channel_delete, AI komutları (endchat, resetchat vb.),
# Müzik komutları (play, skip vb.) ve diğerleri önceki mesajdaki gibi kalabilir.
# Onları buraya tekrar eklemiyorum. Ana yapı ve düzeltmeler burada.

# --- Arka Plan Görevi: İnaktivite Kontrolü (Önceki gibi) ---
@tasks.loop(minutes=5)
async def check_inactivity():
    global warned_inactive_channels, temporary_chat_channels, user_to_channel_map, channel_last_active, active_ai_chats
    if inactivity_timeout is None: return
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    channels_to_delete = []; channels_to_warn = []
    active_channel_ids = list(channel_last_active.keys())
    for channel_id in active_channel_ids:
        if channel_id not in temporary_chat_channels:
            channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id); active_ai_chats.pop(channel_id, None)
            uid_to_remove = next((uid for uid, cid_mapped in user_to_channel_map.items() if cid_mapped == channel_id), None)
            if uid_to_remove: user_to_channel_map.pop(uid_to_remove, None)
            remove_temp_channel_db(channel_id)
            continue
        last_active_time = channel_last_active.get(channel_id)
        if not isinstance(last_active_time, datetime.datetime): continue
        if last_active_time.tzinfo is None: last_active_time = last_active_time.replace(tzinfo=datetime.timezone.utc)
        time_inactive = now_utc - last_active_time
        warning_delta = min(datetime.timedelta(minutes=10), inactivity_timeout * 0.1) if inactivity_timeout > datetime.timedelta(minutes=10) else datetime.timedelta(minutes=5)
        warning_threshold = inactivity_timeout - warning_delta
        if time_inactive > inactivity_timeout: channels_to_delete.append(channel_id)
        elif time_inactive > warning_threshold and channel_id not in warned_inactive_channels: channels_to_warn.append(channel_id)
    for ch_id_warn in channels_to_warn:
        channel_obj = bot.get_channel(ch_id_warn)
        if channel_obj:
            try:
                current_last_active = channel_last_active.get(ch_id_warn)
                if not current_last_active: continue
                if current_last_active.tzinfo is None: current_last_active = current_last_active.replace(tzinfo=datetime.timezone.utc)
                remaining_time = inactivity_timeout - (now_utc - current_last_active)
                remaining_minutes = max(1, int(remaining_time.total_seconds() / 60))
                await channel_obj.send(f"⚠️ Bu kanal, inaktivite nedeniyle yaklaşık **{remaining_minutes} dakika** içinde silinecektir.", delete_after=max(300, remaining_minutes * 60 - 60))
                warned_inactive_channels.add(ch_id_warn)
            except: pass # Hataları yoksay (kanal bulunamadı vs.)
        else: # Discord'da kanal yok, state'i temizle
            temporary_chat_channels.discard(ch_id_warn); active_ai_chats.pop(ch_id_warn, None); channel_last_active.pop(ch_id_warn, None); warned_inactive_channels.discard(ch_id_warn)
            uid = next((u for u,c in user_to_channel_map.items() if c == ch_id_warn), None); user_to_channel_map.pop(uid,None) if uid else None
            remove_temp_channel_db(ch_id_warn)
    if channels_to_delete:
        for ch_id_del in channels_to_delete:
            channel_to_delete = bot.get_channel(ch_id_del)
            try:
                if channel_to_delete: await channel_to_delete.delete(reason="İnaktivite")
            except: pass # Hataları yoksay
            finally: # Her durumda state'i temizle
                temporary_chat_channels.discard(ch_id_del); active_ai_chats.pop(ch_id_del, None); channel_last_active.pop(ch_id_del, None); warned_inactive_channels.discard(ch_id_del)
                uid_to_remove_del = next((uid for uid, cid_mapped_del in user_to_channel_map.items() if cid_mapped_del == ch_id_del), None)
                if uid_to_remove_del: user_to_channel_map.pop(uid_to_remove_del, None)
                remove_temp_channel_db(ch_id_del)

@check_inactivity.before_loop
async def before_check_inactivity():
    await bot.wait_until_ready(); logger.info("İnaktivite kontrol döngüsü başlıyor.")

@bot.event
async def on_guild_channel_delete(channel):
    channel_id = channel.id
    if channel_id in temporary_chat_channels:
        logger.info(f"Geçici kanal '{channel.name}' (ID: {channel_id}) Discord'dan silindi, state temizleniyor.")
        temporary_chat_channels.discard(channel_id); active_ai_chats.pop(channel_id, None); channel_last_active.pop(channel_id, None); warned_inactive_channels.discard(channel_id)
        uid_to_remove = next((uid for uid, cid_mapped in user_to_channel_map.items() if cid_mapped == channel_id), None)
        if uid_to_remove: user_to_channel_map.pop(uid_to_remove, None)
        remove_temp_channel_db(channel_id)

# --- Diğer Komutlar (endchat, resetchat, clear, ask, listmodels, setmodel, setentrychannel, settimeout, .gemini, .deepseek, help) ---
# Bu komutlar önceki düzeltilmiş versiyondaki gibi kalabilir.
# Tekrar eklenmeleri kodu çok uzatacaktır. Önceki versiyondaki hallerini referans alabilirsiniz.
# ÖNEMLİ: Komutların içindeki `ctx.prefix` kullanımlarını `await bot.get_prefix(ctx.message)` veya `bot.command_prefix[0]`
#          ile değiştirmek daha dinamik prefix kullanımını destekler. Şimdilik `ctx.prefix` olarak bırakıldı.

# --- Komutlar (AI ve Genel) ---
@bot.command(name='endchat', aliases=['end', 'closechat', 'kapat'])
@commands.guild_only()
async def end_chat(ctx: commands.Context):
    channel_id = ctx.channel.id; author_id = ctx.author.id
    owner_user_id = next((uid for uid, cid_map in user_to_channel_map.items() if cid_map == channel_id), None)
    if not owner_user_id and channel_id in temporary_chat_channels: # DB'ye bak (state'de yoksa)
        conn = db_connect()
        if conn:
            try:
                cursor = conn.cursor(cursor_factory=DictCursor)
                cursor.execute("SELECT user_id FROM temp_channels WHERE channel_id = %s", (channel_id,))
                owner_row = cursor.fetchone()
                if owner_row: owner_user_id = owner_row['user_id']
            finally: release_db_connection(conn)
    if not (channel_id in temporary_chat_channels or owner_user_id): # Ne state'de ne DB'de (veya DB'den alınamadı)
        await ctx.send("Bu komut sadece özel AI sohbet kanallarında kullanılabilir.", delete_after=10); return
    if owner_user_id and author_id != owner_user_id:
        owner_mention = f"<@{owner_user_id}>"
        await ctx.send(f"Bu kanalı sadece oluşturan kişi ({owner_mention}) kapatabilir.", delete_after=10); return
    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send("Kanalı silmek için 'Kanalları Yönet' iznim yok.", delete_after=10); return
    try:
        await ctx.channel.delete(reason=f"Sohbet {ctx.author.name} tarafından sonlandırıldı.")
    except: pass # Hatalar on_guild_channel_delete veya genel hata yakalayıcı tarafından ele alınır

@bot.command(name='resetchat', aliases=['sıfırla'])
@commands.guild_only()
async def reset_chat_session(ctx: commands.Context):
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        await ctx.send("Bu komut sadece özel AI sohbet kanallarında kullanılabilir.", delete_after=10); return
    if channel_id in active_ai_chats:
        active_ai_chats.pop(channel_id, None) # Oturumu tamamen sil, send_to_ai yeniden başlatır
        logger.info(f"Sohbet '{ctx.channel.name}' ({channel_id}) {ctx.author.name} tarafından sıfırlandı.")
        await ctx.send("✅ Konuşma geçmişi sıfırlandı. Yeni oturum başlayacak.", delete_after=15)
    else: await ctx.send("✨ Sıfırlanacak aktif konuşma yok.", delete_after=10)
    try: await ctx.message.delete(delay=15)
    except: pass

@bot.command(name='clear', aliases=['temizle', 'purge'])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: Union[int, str]):
    deleted_count = 0; is_all = isinstance(amount, str) and amount.lower() == 'all'; limit_num = 0
    if not is_all:
        try:
            limit_num = int(amount)
            if not (0 < limit_num <= 500):
                await ctx.send("Lütfen 1-500 arası bir sayı girin veya 'all'.", delete_after=10); return
        except ValueError: await ctx.send("Geçersiz sayı.", delete_after=10); return
    try: await ctx.message.delete()
    except: pass
    status_msg = None
    try:
        if is_all:
            status_msg = await ctx.send("Kanal temizleniyor (sabitlenmişler hariç)...", delete_after=30)
            while True:
                deleted_batch = await ctx.channel.purge(limit=100, check=lambda m: not m.pinned, bulk=True)
                deleted_count += len(deleted_batch)
                if len(deleted_batch) < 100: break
                await asyncio.sleep(1)
            if status_msg: await status_msg.edit(content=f"Kanal temizlendi! Yaklaşık {deleted_count} mesaj silindi.", delete_after=10)
        else:
            deleted_batch = await ctx.channel.purge(limit=limit_num, check=lambda m: not m.pinned, bulk=True)
            await ctx.send(f"{len(deleted_batch)} mesaj silindi.", delete_after=7)
    except Exception as e:
        if status_msg: await status_msg.delete()
        await ctx.send("Mesajlar silinirken hata oluştu.", delete_after=10)

@bot.command(name='ask', aliases=['sor'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def ask_in_channel(ctx: commands.Context, *, question: str = None):
    if not gemini_default_model_instance:
        await ctx.reply("⚠️ Gemini varsayılan modeli kullanılamıyor.", delete_after=15); return
    if not question:
        await ctx.reply(f"Lütfen bir soru sorun.", delete_after=15); return
    bot_response_msg = None
    try:
        async with ctx.typing():
            response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            text_response = response.text.strip() if hasattr(response, 'text') else ""
            if not text_response: await ctx.reply("Yanıt alınamadı/filtrelendi.", delete_after=15); return
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=f"{ctx.author.display_name} Sordu:", icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else "")
        embed.add_field(name="Soru", value=question[:1020]+"..." if len(question)>1024 else question, inline=False)
        embed.add_field(name="Yanıt", value=text_response[:1020]+"..." if len(text_response)>1024 else text_response, inline=False)
        footer = f"Bu mesajlar {int(MESSAGE_DELETE_DELAY/60)}dk sonra silinecektir."
        if len(text_response) > 1024: footer += " (Yanıt kısaltıldı)"
        embed.set_footer(text=footer)
        bot_response_msg = await ctx.reply(embed=embed, mention_author=False)
    except Exception as e: await ctx.reply("Soru işlenirken hata.", delete_after=15)
    finally:
        try: await ctx.message.delete(delay=MESSAGE_DELETE_DELAY)
        except: pass
        if bot_response_msg:
            try: await bot_response_msg.delete(delay=MESSAGE_DELETE_DELAY)
            except: pass
@ask_in_channel.error
async def ask_error_handler(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        delay = max(5, int(error.retry_after) + 1)
        await ctx.send(f"⏳ `.ask` için beklemedesiniz. **{error.retry_after:.1f}sn** sonra deneyin.", delete_after=delay)
        try: await ctx.message.delete(delay=delay)
        except: pass

@bot.command(name='listmodels', aliases=['models', 'modeller'])
@commands.cooldown(1, 10, commands.BucketType.user)
async def list_available_models(ctx: commands.Context):
    status_msg = await ctx.send("Modeller kontrol ediliyor...", delete_after=5)
    all_models = []
    if GEMINI_API_KEY:
        try:
            models_fetched = await asyncio.to_thread(genai.list_models)
            for m in models_fetched:
                if 'generateContent' in m.supported_generation_methods and m.name.startswith("models/"):
                    model_id = m.name.split('/')[-1]
                    pfx = "❓ ";
                    if "1.5-flash" in model_id: pfx = "⚡ "
                    elif "1.5-pro" in model_id: pfx = "✨ "
                    elif "gemini-pro" == model_id: pfx = "✅ "
                    all_models.append(f"{GEMINI_PREFIX}{pfx}`{model_id}`")
        except Exception as e: all_models.append("_(Gemini modelleri alınamadı)_")
    else: all_models.append("_(Gemini API anahtarı yok)_")
    if OPENROUTER_API_KEY and REQUESTS_AVAILABLE:
        all_models.append(f"{DEEPSEEK_OPENROUTER_PREFIX}🧭 `{OPENROUTER_DEEPSEEK_MODEL_NAME}`")
    elif not OPENROUTER_API_KEY: all_models.append("_(OpenRouter API anahtarı yok)_")
    valid = [m for m in all_models if not m.startswith("_(")]; errors = [m for m in all_models if m.startswith("_(")]
    if not valid: await ctx.send(f"Model bulunamadı.\n" + ("\n".join(errors) if errors else "")); return
    embed = discord.Embed(title="🤖 Kullanılabilir AI Modelleri", description=f"`{ctx.prefix}setmodel <ad>` ile seçin:\n\n" + "\n".join(sorted(valid)), color=discord.Color.gold())
    embed.add_field(name="Ön Ekler", value=f"`{GEMINI_PREFIX}` Gemini\n`{DEEPSEEK_OPENROUTER_PREFIX}` DeepSeek (OpenRouter)", inline=False)
    footer = "⚡Flash ✨Pro ✅EskiPro 🧭DeepSeek ❓AQA"
    if errors: footer += "\nUyarılar: " + " ".join(errors)
    embed.set_footer(text=footer[:1024])
    if status_msg: await status_msg.delete()
    await ctx.send(embed=embed)

@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx: commands.Context, *, model_id_input: str = None):
    global user_next_model
    if not model_id_input:
        await ctx.send(f"Model adı belirtin. `{ctx.prefix}listmodels`", delete_after=15); return
    cleaned_input = model_id_input.strip().replace('`', '')
    selected = None; is_valid = False; err_msg = None
    if not cleaned_input.startswith(GEMINI_PREFIX) and not cleaned_input.startswith(DEEPSEEK_OPENROUTER_PREFIX):
         await ctx.send(f"❌ Model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_OPENROUTER_PREFIX}` ekleyin.", delete_after=20); return
    async with ctx.typing():
        if cleaned_input.startswith(GEMINI_PREFIX):
            if not GEMINI_API_KEY: err_msg = "❌ Gemini API anahtarı yok."
            else:
                name = cleaned_input[len(GEMINI_PREFIX):]
                if not name: err_msg = "❌ Gemini model adı belirtin."
                else:
                    try: await asyncio.to_thread(genai.get_model, f"models/{name}"); selected = cleaned_input; is_valid = True
                    except: err_msg = f"❌ `{name}` geçerli Gemini modeli değil."
        elif cleaned_input.startswith(DEEPSEEK_OPENROUTER_PREFIX):
            if not OPENROUTER_API_KEY: err_msg = "❌ OpenRouter API anahtarı yok."
            elif not REQUESTS_AVAILABLE: err_msg = "❌ 'requests' kütüphanesi yok."
            else:
                expected = f"{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}"
                if cleaned_input == expected: selected = cleaned_input; is_valid = True
                else: err_msg = f"❌ Sadece `{expected}` modeli kullanılabilir."
    if is_valid and selected:
        user_next_model[ctx.author.id] = selected
        await ctx.send(f"✅ Sonraki sohbetiniz `{selected}` ile başlayacak.", delete_after=20)
    else: await ctx.send(f"{err_msg if err_msg else 'Geçersiz model.'} `{ctx.prefix}listmodels`", delete_after=15)

@bot.command(name='setentrychannel', aliases=['giriskanali'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_entry_channel(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    global entry_channel_id
    if not channel:
        current = "Ayarlanmamış";
        if entry_channel_id:
            try: current = (await bot.fetch_channel(entry_channel_id)).mention
            except: current = f"ID: {entry_channel_id} (Bulunamadı)"
        await ctx.send(f"Kullanım: `{ctx.prefix}setentrychannel #kanal`. Mevcut: {current}", delete_after=20); return
    perms = channel.permissions_for(ctx.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.manage_messages):
        await ctx.send(f"❌ {channel.mention} kanalında izinlerim eksik.", delete_after=15); return
    entry_channel_id = channel.id; save_config_db('entry_channel_id', entry_channel_id)
    await ctx.send(f"✅ Giriş kanalı {channel.mention} olarak ayarlandı.")
    try: await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="!help | AI & Music"))
    except: pass

@bot.command(name='settimeout', aliases=['zamanasimi'])
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def set_inactivity_timeout(ctx: commands.Context, hours_str: Optional[str] = None):
    global inactivity_timeout
    current_disp = "Kapalı"
    if inactivity_timeout: current_disp = f"{inactivity_timeout.total_seconds()/3600:.2f} saat"
    if not hours_str:
        await ctx.send(f"Kullanım: `{ctx.prefix}settimeout <saat>`. 0 = kapalı. Mevcut: `{current_disp}`", delete_after=20); return
    try:
        val = float(hours_str)
        if val < 0: await ctx.send("Pozitif saat veya 0 girin."); return
        if val == 0: inactivity_timeout = None; save_config_db('inactivity_timeout_hours', '0'); await ctx.send(f"✅ İnaktivite zaman aşımı kapatıldı.")
        elif val < 0.1: await ctx.send("Min 0.1 saat (6dk)."); return
        elif val > 720: await ctx.send("Max 720 saat (30 gün)."); return
        else: inactivity_timeout = datetime.timedelta(hours=val); save_config_db('inactivity_timeout_hours', str(val)); await ctx.send(f"✅ İnaktivite zaman aşımı **{val:.2f} saat** olarak ayarlandı.")
    except ValueError: await ctx.send(f"Geçersiz saat: '{hours_str}'.")

@bot.command(name='gemini', aliases=['g'])
@commands.guild_only()
@commands.cooldown(1, 3, commands.BucketType.user)
async def gemini_direct(ctx: commands.Context, *, question: str = None):
    if ctx.channel.id not in temporary_chat_channels: return
    if not gemini_default_model_instance: await ctx.reply("⚠️ Gemini varsayılan modeli yok.", delete_after=10); return
    if not question: await ctx.reply(f"Soru sorun.", delete_after=15); return
    try:
        async with ctx.typing():
            response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            text = response.text.strip() if hasattr(response, 'text') else ""
            if not text: await ctx.reply("Yanıt alınamadı/filtrelendi.", delete_after=10); return
        embed = discord.Embed(description=text[:4090] + "..." if len(text) > 4096 else text, color=discord.Color.blue())
        embed.set_author(name=f"Gemini Yanıtı ({DEFAULT_GEMINI_MODEL_NAME})", icon_url="https://i.imgur.com/tGd6A4F.png")
        if len(text) > 4096: embed.set_footer(text="Yanıt kısaltıldı.")
        await ctx.reply(embed=embed)
    except Exception as e: await ctx.reply("Hata oluştu.", delete_after=15)

@bot.command(name='deepseek', aliases=['ds'])
@commands.guild_only()
@commands.cooldown(1, 3, commands.BucketType.user)
async def deepseek_direct(ctx: commands.Context, *, question: str = None):
    if ctx.channel.id not in temporary_chat_channels: return
    if not OPENROUTER_API_KEY or not REQUESTS_AVAILABLE: await ctx.reply("⚠️ DeepSeek (OpenRouter) ayarlı değil.", delete_after=10); return
    if not question: await ctx.reply(f"Soru sorun.", delete_after=15); return
    text_resp = None; err_msg = "DeepSeek (OpenRouter) hatası."
    try:
        async with ctx.typing():
            headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
            if OPENROUTER_SITE_URL: headers["HTTP-Referer"] = OPENROUTER_SITE_URL
            if OPENROUTER_SITE_NAME: headers["X-Title"] = OPENROUTER_SITE_NAME
            payload = {"model": OPENROUTER_DEEPSEEK_MODEL_NAME, "messages": [{"role": "user", "content": question}], "max_tokens": 2048}
            api_resp = await asyncio.to_thread(requests.post, OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)
            api_resp.raise_for_status()
            data = api_resp.json()
            if data and data.get("choices"): text_resp = data["choices"][0]["message"]["content"].strip()
            else: raise ValueError("OpenRouter'dan yanıt alınamadı.")
        embed = discord.Embed(description=text_resp[:4090] + "..." if len(text_resp) > 4096 else text_resp, color=discord.Color.dark_green())
        embed.set_author(name=f"DeepSeek Yanıtı ({OPENROUTER_DEEPSEEK_MODEL_NAME})", icon_url="https://avatars.githubusercontent.com/u/14733136?s=280&v=4")
        if len(text_resp) > 4096: embed.set_footer(text="Yanıt kısaltıldı.")
        await ctx.reply(embed=embed)
    except Exception as e: await ctx.reply(f"⚠️ {err_msg} ({str(e)[:50]})", delete_after=15)

@bot.command(name='help', aliases=['yardım', 'komutlar'])
@commands.cooldown(1, 5, commands.BucketType.user)
async def custom_help(ctx: commands.Context):
    embed = discord.Embed(title="📜 Komutlar", color=discord.Color.blurple()); pfx = ctx.prefix
    general = (f"`{pfx}help` - Bu mesaj.\n"
               f"`{pfx}ask <soru>` - Gemini'ye soru sor.\n"
               f"`{pfx}listmodels` - AI modellerini listele.\n"
               f"`{pfx}setmodel <ad>` - Sonraki sohbet için AI modeli ayarla.")
    embed.add_field(name="🌐 Genel AI", value=general, inline=False)
    if ctx.channel.id in temporary_chat_channels:
        ai_chat = (f"`{pfx}resetchat` - Sohbeti sıfırla.\n"
                   f"`{pfx}endchat` - Sohbeti kapat.\n"
                   f"`{pfx}gemini <soru>` - Gemini'ye sor.\n"
                   f"`{pfx}deepseek <soru>` - DeepSeek'e sor.")
        embed.add_field(name="🤖 Özel AI Sohbet", value=ai_chat, inline=False)
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id == MUSIC_CHANNEL_ID:
        music = (f"`{pfx}play <ad/URL>` - Müzik çal/ekle.\n"
                 f"`{pfx}skip` - Şarkıyı atla.\n"
                 f"`{pfx}pause`/`{pfx}resume` - Duraklat/Devam et.\n"
                 f"`{pfx}stop` - Durdur ve ayrıl.\n"
                 f"`{pfx}queue` - Kuyruğu göster.\n"
                 f"`{pfx}nowplaying` - Çalan şarkı.\n"
                 f"`{pfx}volume [0-100]` - Ses ayarla/göster.\n"
                 f"`{pfx}setdefaultvolume [0-100]` - Varsayılan ses.\n"
                 f"`{pfx}loop` - Döngü modu.\n"
                 f"`{pfx}shuffle` - Kuyruğu karıştır.\n"
                 f"`{pfx}seek <süre>` - Şarkıda atla.\n"
                 f"`{pfx}forward [sn]` / `{pfx}rewind [sn]` - İleri/Geri sar.\n"
                 f"`{pfx}leave` - Kanaldan ayrıl.")
        embed.add_field(name="🎧 Müzik", value=music, inline=False)
    if ctx.author.guild_permissions.administrator:
        admin = (f"`{pfx}setentrychannel #kanal` - Giriş kanalı ayarla.\n"
                 f"`{pfx}settimeout <saat>` - İnaktivite zaman aşımı (0=kapalı).\n"
                 f"`{pfx}clear <sayı/all>` - Mesajları sil.")
        embed.add_field(name="🛡️ Yönetici", value=admin, inline=False)
    embed.set_footer(text=f"Bot v1.6_corrected_v2 | Prefix: {pfx}")
    await ctx.send(embed=embed)

# --- Genel Hata Yakalama (on_command_error) ---
# Bu fonksiyon önceki mesajdaki gibi kalabilir. Tekrar eklenmiyor.
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound): return
    if str(error) == "Duplicate command invocation prevented by on_command.": # on_command'dan gelen özel hata
        logger.warning(f"Engellenen çift komut: '{ctx.invoked_with}' (Yazar: {ctx.author.name})")
        return
    if isinstance(error, commands.CommandOnCooldown):
        if ctx.command and ctx.command.qualified_name == 'ask': return # ask kendi cooldown mesajını verir
        delay = max(5, int(error.retry_after) + 1)
        await ctx.send(f"⌛ `{ctx.command.qualified_name}` için beklemedesiniz. **{error.retry_after:.1f}sn** sonra deneyin.", delete_after=delay)
        try: await ctx.message.delete(delay=delay)
        except: pass
        return
    if isinstance(error, commands.UserInputError):
        delay = 15; usage = f"`{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature if ctx.command else ''}`".replace('=None','').replace('= Ellipsis','...')
        msg = "Hatalı kullanım."
        if isinstance(error, commands.MissingRequiredArgument): msg = f"Eksik argüman: `{error.param.name}`."
        elif isinstance(error, commands.BadArgument): msg = f"Geçersiz argüman: {error}"
        await ctx.send(f"⚠️ {msg}\nDoğru kullanım: {usage}", delete_after=delay)
        try: await ctx.message.delete(delay=delay)
        except: pass
        return
    if isinstance(error, (commands.MissingPermissions, commands.BotMissingPermissions)):
        perms = getattr(error, 'missing_permissions', getattr(error, 'missing_perms', []))
        perms_str = ", ".join(f"`{p.replace('_',' ').title()}`" for p in perms)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(f"⛔ Bu komut için izniniz yok: **{perms_str}**", delete_after=15)
        else: await ctx.send(f"🆘 Bu komut için iznim yok: **{perms_str}**", delete_after=15)
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 Bu komutu burada/bu şekilde kullanamazsınız.", delete_after=10)
        try: await ctx.message.delete(delay=10)
        except: pass
        return

    original = getattr(error, 'original', error)
    logger.error(f"'{ctx.invoked_with}' işlenirken BEKLENMEDİK HATA: {type(original).__name__}: {original}")
    logger.error(f"Traceback:\n{''.join(traceback.format_exception(type(original), original, original.__traceback__))}")
    await ctx.send("⚙️ Beklenmedik bir hata oluştu.", delete_after=15)


# --- Müzik Komutları (play_music_cmd, skip_song_cmd vb.) ---
# Bu komutlar da önceki düzeltilmiş versiyondaki gibi kalabilir.
# Sadece fonksiyon adları (örn: play_music_cmd) ve MUSIC_CHANNEL_ID kontrolü eklendi.
# Tekrar eklenmeleri kodu çok uzatacaktır. Önceki versiyondaki hallerini referans alabilirsiniz.
@bot.command(name='play', aliases=['p', 'çal'])
async def play_music_cmd(ctx: commands.Context, *, query: str = None):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID:
        await ctx.send(f"Müzik komutları <#{MUSIC_CHANNEL_ID}> kanalında.", delete_after=10); return
    if query is None and ctx.message.attachments:
        if ctx.message.attachments[0].filename.lower().endswith(('.mp3','.wav','.ogg','.m4a','.flac')): query = ctx.message.attachments[0].url
        else: await ctx.send("Desteklenmeyen dosya veya şarkı adı/URL belirtin."); return
    elif query is None: await ctx.send("Şarkı adı/URL belirtin."); return
    if not await music_player.join_voice_channel(ctx): return
    loading_msg = await ctx.send(f"⌛ **{query[:70]}{'...' if len(query)>70 else ''}** aranıyor...")
    try:
        ytdl_opts = music_player.ytdl_format_options.copy()
        with yt_dlp.YoutubeDL(ytdl_opts) as ydl: info = await asyncio.to_thread(ydl.extract_info, query, download=False)
        songs_to_add = []; playlist_title = None
        if '_type' in info and info['_type'] == 'playlist':
            playlist_title = info.get('title', 'Oynatma Listesi'); max_items = 50; count = 0
            for entry in info.get('entries', []):
                if count >= max_items: break
                if entry:
                    url_detail = entry.get('url') or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get('id') else None)
                    if not url_detail: continue
                    try:
                        with yt_dlp.YoutubeDL(music_player.ytdl_format_options) as ydl_detail:
                            v_info = await asyncio.to_thread(ydl_detail.extract_info, url_detail, download=False)
                        if v_info.get('entries'): v_info = v_info['entries'][0] # Hala playlist ise ilkini al
                        title = v_info.get('title','Bilinmeyen'); stream = v_info.get('url'); dur_s = v_info.get('duration',0)
                        dur_str = str(datetime.timedelta(seconds=dur_s)) if dur_s else 'Bilinmiyor'; thumb = v_info.get('thumbnail')
                        if stream: songs_to_add.append({'title':title,'url':stream,'duration':dur_str,'thumbnail':thumb,'requester':ctx.author.display_name})
                        count += 1
                    except: pass # Hatalı playlist öğesini atla
        else:
            if 'entries' in info and info.get('entries'): info = info['entries'][0]
            title=info.get('title','Bilinmeyen');stream=info.get('url');dur_s=info.get('duration',0)
            dur_str=str(datetime.timedelta(seconds=dur_s)) if dur_s else 'Bilinmiyor';thumb=info.get('thumbnail')
            if stream: songs_to_add.append({'title':title,'url':stream,'duration':dur_str,'thumbnail':thumb,'requester':ctx.author.display_name})
        if not songs_to_add: await loading_msg.edit(content=f"❌ Çalınabilir şarkı bulunamadı."); return
        gid = ctx.guild.id
        if gid not in music_player.queues: music_player.queues[gid] = deque()
        for song in songs_to_add: music_player.queues[gid].append(song)
        vc = ctx.guild.voice_client
        if not (vc and (vc.is_playing() or vc.is_paused())):
            await music_player.play_next(gid, ctx)
            try: await loading_msg.delete()
            except: pass
        else:
            desc = f"**{playlist_title if playlist_title else songs_to_add[0]['title']}** ({len(songs_to_add)} şarkı) kuyruğa eklendi." if playlist_title else f"**{songs_to_add[0]['title']}** kuyruğa eklendi."
            embed = discord.Embed(title="✅ Kuyruğa Eklendi", description=desc, color=discord.Color.green())
            if songs_to_add[0].get('thumbnail') and not playlist_title : embed.set_thumbnail(url=songs_to_add[0]['thumbnail'])
            await loading_msg.edit(content=None, embed=embed)
    except yt_dlp.utils.DownloadError as e: await loading_msg.edit(content=f"❌ İndirme/bilgi alma hatası: {str(e)[:150]}")
    except Exception as e: await loading_msg.edit(content=f"❌ Müzik yüklenirken hata: {str(e)[:150]}")

@bot.command(name='skip', aliases=['s', 'geç'])
async def skip_song_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    vc = ctx.guild.voice_client
    if not (vc and vc.is_connected() and (vc.is_playing() or vc.is_paused())):
        await ctx.send("❌ Atlanacak şarkı yok."); return
    title = music_player.now_playing.get(ctx.guild.id, {}).get('title', 'Şarkı')
    vc.stop()
    await ctx.send(embed=discord.Embed(title="⏭️ Şarkı Geçildi", description=f"**{title}** geçildi.", color=discord.Color.blue()).set_footer(text=f"Geçen: {ctx.author.display_name}"))

@bot.command(name='pause', aliases=['duraklat'])
async def pause_music_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    vc = ctx.guild.voice_client
    if not (vc and vc.is_connected()): await ctx.send("Bağlı değilim."); return
    if vc.is_paused(): await ctx.send("Zaten duraklatılmış."); return
    if not vc.is_playing(): await ctx.send("Çalan şarkı yok."); return
    vc.pause(); title = music_player.now_playing.get(ctx.guild.id,{}).get('title','Şarkı')
    await ctx.send(embed=discord.Embed(title="⏸️ Müzik Duraklatıldı", description=f"**{title}**.", color=discord.Color.gold()))

@bot.command(name='resume', aliases=['devam'])
async def resume_music_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    vc = ctx.guild.voice_client
    if not (vc and vc.is_connected()): await ctx.send("Bağlı değilim."); return
    if vc.is_playing() and not vc.is_paused(): await ctx.send("Zaten çalıyor."); return
    if not vc.is_paused(): await ctx.send("Duraklatılmış şarkı yok."); return
    vc.resume(); title = music_player.now_playing.get(ctx.guild.id,{}).get('title','Şarkı')
    await ctx.send(embed=discord.Embed(title="▶️ Müzik Devam Ediyor", description=f"**{title}**.", color=discord.Color.green()))

@bot.command(name='stop', aliases=['dur'])
async def stop_music_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    vc = ctx.guild.voice_client
    if not (vc and vc.is_connected() and (vc.is_playing() or vc.is_paused())):
        await ctx.send("Durdurulacak şarkı yok."); return
    title = music_player.now_playing.get(ctx.guild.id,{}).get('title','Şarkı')
    vc.stop(); await music_player.cleanup(ctx.guild.id)
    music_player.voice_clients.pop(ctx.guild.id, None)
    await ctx.send(embed=discord.Embed(title="⏹️ Müzik Durduruldu", description=f"**{title}** ve kuyruk durduruldu. Kanaldan ayrıldım.", color=discord.Color.red()))

@bot.command(name='queue', aliases=['q', 'kuyruk', 'list', 'liste'])
async def show_queue_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    gid = ctx.guild.id; embed = discord.Embed(title="🎶 Müzik Kuyruğu", color=discord.Color.purple())
    song = music_player.now_playing.get(gid); vc = ctx.guild.voice_client
    if song and vc and (vc.is_playing() or vc.is_paused()):
        elapsed = "";
        if song.get('start_time'):
            e_s = (datetime.datetime.now(datetime.timezone.utc) - song['start_time'].replace(tzinfo=datetime.timezone.utc)).total_seconds()
            elapsed = f"{int(e_s//60):02d}:{int(e_s%60):02d} / "
        embed.add_field(name="▶️ Çalıyor", value=f"**{song['title']}**\n[{elapsed}{song.get('duration','Bilinmiyor')}] - {song.get('requester','Bilinmiyor')}", inline=False)
        if song.get('thumbnail'): embed.set_thumbnail(url=song['thumbnail'])
    else: embed.add_field(name="▶️ Çalıyor", value="*Hiçbir şey çalmıyor*", inline=False)
    queue_items = music_player.queues.get(gid)
    if queue_items:
        text = ""; total_dur_s = 0
        for i, s_q in enumerate(list(queue_items)[:10], 1):
            text += f"`{i}.` **{s_q['title']}** ({s_q.get('duration','Bilinmiyor')}) - {s_q.get('requester','Bilinmiyor')}\n"
            try: parts=list(map(int,s_q.get('duration','0:0').split(':'))); total_dur_s += (parts[0]*60+parts[1]) if len(parts)==2 else (parts[0]*3600+parts[1]*60+parts[2] if len(parts)==3 else 0)
            except: pass
        if len(queue_items) > 10: text += f"\n*...ve {len(queue_items)-10} şarkı daha*"
        embed.add_field(name=f"📜 Sırada ({len(queue_items)})", value=text or "*Kuyruk boş*", inline=False)
        if total_dur_s > 0: embed.description = f"Toplam kuyruk süresi: **{str(datetime.timedelta(seconds=total_dur_s))}**"
    else: embed.add_field(name="📜 Sırada", value="*Kuyruk boş*", inline=False)
    loop=music_player.loop_settings.get(gid,"off"); shuffle="Açık" if music_player.shuffle_settings.get(gid,False) else "Kapalı"
    embed.set_footer(text=f"Ses: %{int(music_player.volume*100)} | Döngü: {loop.capitalize()} | Karıştırma: {shuffle}")
    await ctx.send(embed=embed)

@bot.command(name='volume', aliases=['vol', 'ses'])
async def set_volume_cmd(ctx: commands.Context, level: Optional[int] = None):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if level is None:
        vol = int(music_player.volume*100); bar="▬"*int(vol/5)+"▫"*(20-int(vol/5))
        await ctx.send(embed=discord.Embed(title=f"🔊 Ses Seviyesi: %{vol}", description=f"`{bar}`", color=discord.Color.blue())); return
    if not (0<=level<=100): await ctx.send("❌ Ses 0-100 arası olmalı."); return
    old_vol=int(music_player.volume*100); music_player.volume=level/100.0
    if ctx.guild.voice_client and ctx.guild.voice_client.source: ctx.guild.voice_client.source.volume=music_player.volume
    save_volume_settings_db(music_player.volume, music_player.default_volume)
    bar_n="▬"*int(level/5)+"▫"*(20-int(level/5))
    await ctx.send(embed=discord.Embed(title=f"🔊 Ses Değiştirildi: %{old_vol} → %{level}", description=f"`{bar_n}`", color=discord.Color.green()))

@bot.command(name='setdefaultvolume', aliases=['setdvol']) # Kısaltıldı
async def set_default_volume_cmd(ctx: commands.Context, level: Optional[int] = None):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if level is None:
        vol=int(music_player.default_volume*100); bar="▬"*int(vol/5)+"▫"*(20-int(vol/5))
        await ctx.send(embed=discord.Embed(title=f"🔊 Varsayılan Ses: %{vol}", description=f"`{bar}`",color=discord.Color.blue())); return
    if not (0<=level<=100): await ctx.send("❌ Ses 0-100 arası olmalı."); return
    old_vol=int(music_player.default_volume*100); music_player.default_volume=level/100.0
    save_volume_settings_db(music_player.volume, music_player.default_volume)
    bar_n="▬"*int(level/5)+"▫"*(20-int(level/5))
    await ctx.send(embed=discord.Embed(title=f"🔊 Varsayılan Ses Değiştirildi: %{old_vol} → %{level}", description=f"`{bar_n}`",color=discord.Color.green()))

@bot.command(name='rewind', aliases=['gerisar', 'rw'])
async def rewind_cmd(ctx: commands.Context, seconds: int = 10):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if not (ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused())):
        await ctx.send("❌ Geri sarılacak şarkı yok."); return
    if seconds <=0: await ctx.send("❌ Süre pozitif olmalı."); return
    await music_player.rewind(ctx.guild.id, seconds, ctx)

@bot.command(name='forward', aliases=['ilerisar', 'ff'])
async def forward_cmd(ctx: commands.Context, seconds: int = 10):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if not (ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused())):
        await ctx.send("❌ İleri sarılacak şarkı yok."); return
    if seconds <=0: await ctx.send("❌ Süre pozitif olmalı."); return
    await music_player.forward(ctx.guild.id, seconds, ctx)

@bot.command(name='seek', aliases=['atla', 'git'])
async def seek_cmd(ctx: commands.Context, *, position: str):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if not (ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused())):
        await ctx.send("❌ Konumuna gidilecek şarkı yok."); return
    try:
        parts=list(map(int,position.split(':'))); secs = parts[0] if len(parts)==1 else (parts[0]*60+parts[1] if len(parts)==2 else (parts[0]*3600+parts[1]*60+parts[2] if len(parts)==3 else -1))
        if secs < 0: raise ValueError()
    except: await ctx.send("❌ Geçersiz süre formatı (örn: 1:30 veya 90)."); return
    await music_player.seek(ctx.guild.id, secs, ctx)

@bot.command(name='loop', aliases=['döngü', 'tekrarla'])
async def loop_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    if not (ctx.guild.voice_client and ctx.guild.voice_client.is_connected()):
        await ctx.send("❌ Ses kanalına bağlı değilim."); return
    mode=music_player.toggle_loop(ctx.guild.id); emoji={"off":"❌","song":"🔂","queue":"🔁"}.get(mode,"")
    desc={"off":"Döngü kapalı.","song":"Tek şarkı döngüsü.","queue":"Kuyruk döngüsü."}.get(mode)
    await ctx.send(embed=discord.Embed(title=f"{emoji} Döngü: {mode.capitalize()}", description=desc, color=discord.Color.blue()))

@bot.command(name='shuffle', aliases=['karıştır'])
async def shuffle_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    gid=ctx.guild.id
    if not music_player.queues.get(gid): await ctx.send("❌ Karıştırılacak şarkı yok."); return
    if music_player.shuffle_queue(gid):
        await ctx.send(embed=discord.Embed(title="🔀 Kuyruk Karıştırıldı", description=f"{len(music_player.queues[gid])} şarkı.", color=discord.Color.blue()))

@bot.command(name='nowplaying', aliases=['np', 'şimdiçalıyor', 'şimdi'])
async def now_playing_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    gid = ctx.guild.id; song = music_player.now_playing.get(gid); vc = ctx.guild.voice_client
    if song and vc and (vc.is_playing() or vc.is_paused()):
        embed = discord.Embed(title=f"▶️ Çalıyor: {song['title']}", color=discord.Color.purple())
        if song.get('thumbnail'): embed.set_thumbnail(url=song['thumbnail'])
        elapsed = "";
        if song.get('start_time'):
            e_s=(datetime.datetime.now(datetime.timezone.utc)-song['start_time'].replace(tzinfo=datetime.timezone.utc)).total_seconds()
            elapsed=f"{int(e_s//60):02d}:{int(e_s%60):02d} / "
        embed.add_field(name="Süre",value=f"[{elapsed}{song.get('duration','Bilinmiyor')}]",inline=True)
        embed.add_field(name="Ekleyen",value=song.get('requester','Bilinmiyor'),inline=True)
        embed.add_field(name="Ses",value=f"%{int(music_player.volume*100)}",inline=True)
        q_len=len(music_player.queues.get(gid,[])); loop_m=music_player.loop_settings.get(gid,"off").capitalize()
        embed.set_footer(text=f"Kuyruk: {q_len} şarkı | Döngü: {loop_m}")
        await ctx.send(embed=embed)
    else: await ctx.send(embed=discord.Embed(title="ℹ️ Bilgi", description="*Bir şey çalmıyor.*", color=discord.Color.light_grey()))

@bot.command(name='leave')
async def leave_voice_cmd(ctx: commands.Context):
    if MUSIC_CHANNEL_ID != 0 and ctx.channel.id != MUSIC_CHANNEL_ID: return
    vc = ctx.guild.voice_client
    if vc and vc.is_connected():
        await music_player.cleanup(ctx.guild.id)
        music_player.voice_clients.pop(ctx.guild.id, None)
        await ctx.send("👋 Ses kanalından ayrıldım.")
    else: await ctx.send("❌ Zaten ses kanalında değilim.")


# === Render/Koyeb için Web Sunucusu ===
app = Flask(__name__)
@app.route('/')
def home_route():
    if bot and bot.is_ready():
        return f"Bot '{bot.user.name}' çalışıyor. Sunucular: {len(bot.guilds)}. Aktif AI sohbetleri: {len(temporary_chat_channels)}.", 200
    return "Bot durumu bilinmiyor/başlatılıyor.", 503

def run_webserver_thread():
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        logger.info(f"Flask web sunucusu http://{host}:{port} adresinde başlatılıyor...")
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.critical(f"Web sunucusu başlatılırken KRİTİK HATA: {e}")

# --- Tek İnstans Kontrolü ---
single_instance_socket_obj = None
def cleanup_instance_socket():
    global single_instance_socket_obj
    if single_instance_socket_obj:
        try: single_instance_socket_obj.close(); logger.info("Tek instance soketi temizlendi.")
        except: pass
def ensure_single_instance_lock():
    global single_instance_socket_obj
    try:
        single_instance_socket_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_port = int(os.getenv("INSTANCE_LOCK_PORT", 12345)) # Portu özelleştirilebilir yap
        single_instance_socket_obj.bind(('localhost', lock_port))
        atexit.register(cleanup_instance_socket)
        logger.info(f"Tek instance kilidi port {lock_port} üzerinde alındı.")
        return True
    except socket.error:
        logger.critical(f"HATA: Bot zaten çalışıyor (port meşgul)! Çıkılıyor.")
        return False

# --- Botu Çalıştır ---
if __name__ == "__main__":
    if not ensure_single_instance_lock():
        sys.exit(1)

    logger.info("Bot başlatılıyor...")
    web_thread = None
    try:
        if not init_db_pool(): # DB havuzunu burada başlat
             logger.critical("DB Havuzu ana thread'de başlatılamadı. Çıkılıyor."); sys.exit(1)

        web_thread = threading.Thread(target=run_webserver_thread, daemon=True, name="FlaskWebserverThread")
        web_thread.start()
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.LoginFailure: logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired: logger.critical("HATA: Gerekli Intent'ler Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_main_err: logger.critical(f"PostgreSQL bağlantı hatası (Bot başlatılırken): {db_main_err}")
    except Exception as main_e: logger.critical(f"Bot çalıştırılırken kritik genel hata: {type(main_e).__name__}: {main_e}\n{traceback.format_exc()}")
    finally:
        logger.info("Bot kapatılıyor...")
        if db_pool:
            try: db_pool.closeall(); logger.info("PostgreSQL bağlantı havuzu kapatıldı.")
            except: pass
        # cleanup_instance_socket atexit tarafından çağrılacak