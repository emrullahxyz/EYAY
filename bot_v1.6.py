# -*- coding: utf-8 -*-
# bot_v1.6.py - Discord Bot with AI Chat and Music


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
# DeepSeek için artık OpenRouter anahtarını kullanacağız
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# İsteğe bağlı OpenRouter başlıkları için ortam değişkenleri
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "") # Varsayılan boş
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "Discord AI Bot") # Varsayılan isim
# Müzik kanalı ID'si
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", 0))

# --- Müzik Oynatıcı Sınıfı ---
class MusicPlayer:
    """Müzik çalma, kuyruk yönetimi ve ses seviyesi kontrolü için optimize edilmiş sınıf."""
    
    def __init__(self):
        # Müzik çalma ayarları
        self.ytdl_format_options = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'noplaylist': False,  # Oynatma listelerini işlemeye izin ver
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'source_address': '0.0.0.0',
            'extract_flat': 'in_playlist',
        }
        
        self.ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn',
        }
        
        # Ses seviyesi ayarları
        self.volume = 0.5  # Varsayılan ses seviyesi (0.0 - 1.0)
        self.default_volume = 0.5  # Kalıcı varsayılan ses seviyesi
        
        # Sunucu başına müzik durumu
        self.voice_clients: Dict[int, discord.VoiceClient] = {}  # guild_id -> voice_client
        self.queues: Dict[int, deque] = {}  # guild_id -> şarkı kuyruğu
        self.now_playing: Dict[int, Dict[str, str]] = {}  # guild_id -> şimdi çalan şarkı bilgisi
        
        # Kilitleme mekanizması - eşzamanlı erişim için
        self.locks: Dict[int, asyncio.Lock] = {}  # guild_id -> lock
        self.is_seeking: Dict[int, bool] = {} # guild_id -> is_seeking flag
    
    def get_lock(self, guild_id: int) -> asyncio.Lock:
        """Belirli bir sunucu için kilit nesnesi al veya oluştur."""
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]
    
    async def join_voice_channel(self, ctx: commands.Context) -> bool:
        """Ses kanalına katıl. Başarılıysa True, başarısızsa False döndür."""
        if not ctx.author.voice:
            await ctx.send("❌ Önce bir ses kanalına katılmalısın.")
            return False
            
        channel = ctx.author.voice.channel
        
        # Zaten bağlı mı kontrol et
        if ctx.guild.id in self.voice_clients and self.voice_clients[ctx.guild.id].is_connected():
            # Farklı bir kanaldaysa taşı
            if self.voice_clients[ctx.guild.id].channel != channel:
                await self.voice_clients[ctx.guild.id].move_to(channel)
                await ctx.send(f"✅ {channel.mention} kanalına taşındım.")
        else:
            # Yeni bağlantı kur
            try:
                voice_client = await channel.connect(timeout=10.0, reconnect=True)
                self.voice_clients[ctx.guild.id] = voice_client
                await ctx.send(f"✅ {channel.mention} kanalına katıldım.")
                
                # Ses seviyesini yükle
                await self.load_volume_settings()
            except discord.ClientException as e:
                logger.error(f"Ses kanalına bağlanırken hata: {e}")
                await ctx.send("❌ Ses kanalına bağlanırken bir hata oluştu.")
                return False
                
        return True
    
    async def load_volume_settings(self):
        """Veritabanından ses seviyesi ayarlarını yükle"""
        try:
            volume_settings = get_volume_settings()
            if volume_settings:
                self.volume = volume_settings.get('current_volume', 0.5)
                self.default_volume = volume_settings.get('default_volume', 0.5)
                logger.info(f"Ses seviyesi ayarları yüklendi: {self.volume:.2f} (mevcut), {self.default_volume:.2f} (varsayılan)")
        except Exception as e:
            logger.error(f"Ses seviyesi ayarları yüklenirken hata: {e}")
    
    async def play_next(self, guild_id: int, ctx: Optional[commands.Context] = None, from_callback: bool = False) -> bool:
        """Kuyruktaki bir sonraki şarkıyı çal
        
        Args:
            guild_id: Sunucu ID'si
            ctx: Komut bağlamı (opsiyonel)
            from_callback: Eğer True ise, after_playing callback'inden çağrıldığını belirtir
        """
        try:
            # Guild objesini bul
            guild = bot.get_guild(guild_id)
            if not guild:
                logger.warning(f"play_next: Guild {guild_id} bulunamadı.")
                return False
            
            # Voice client'i bul
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                logger.warning(f"play_next: Sunucu {guild_id} için ses bağlantısı yok.")
                return False
            
            # Kuyruk kontrolü
            if not hasattr(self, 'queues'):
                self.queues = {}
                
            if guild_id not in self.queues or not self.queues[guild_id]:
                # Kuyruk boş, döngü ayarını kontrol et
                loop_mode = "off"
                if hasattr(self, 'loop_settings'):
                    loop_mode = self.loop_settings.get(guild_id, "off")
                    
                # Eğer kuyruk döngüsü açıksa ve daha önce çalan şarkılar varsa
                if loop_mode == "queue" and hasattr(self, 'played_history') and guild_id in self.played_history and self.played_history[guild_id]:
                    logger.info(f"play_next: Kuyruk döngüsü aktif, geçmiş şarkılar kuyruğa ekleniyor.")
                    # Geçmiş şarkıları kuyruğa ekle
                    self.queues[guild_id] = deque(self.played_history[guild_id])
                    # Geçmiş listesini temizle
                    self.played_history[guild_id] = []
                    
                    # Karıştırma kontrolü
                    if hasattr(self, 'shuffle_settings') and self.shuffle_settings.get(guild_id, False):
                        logger.info(f"play_next: Karıştırma aktif, kuyruk karıştırılıyor.")
                        queue_list = list(self.queues[guild_id])
                        random.shuffle(queue_list)
                        self.queues[guild_id] = deque(queue_list)
                else:
                    logger.info(f"play_next: Sunucu {guild_id} için kuyruk boş.")
                    if hasattr(self, 'now_playing'):
                        self.now_playing[guild_id] = None
                    if ctx and not from_callback:
                        await ctx.send("❌ Kuyrukta başka şarkı yok.")
                    return False
            
            # Bir sonraki şarkıyı al
            next_song = self.queues[guild_id].popleft()
            logger.info(f"play_next: Sıradaki şarkı alındı: {next_song.get('title', 'Bilinmeyen Şarkı')}")
            
            # Kuyruk döngüsü için çalınan şarkıları kaydet
            if hasattr(self, 'loop_settings') and self.loop_settings.get(guild_id) == "queue":
                if not hasattr(self, 'played_history'):
                    self.played_history = {}
                if guild_id not in self.played_history:
                    self.played_history[guild_id] = []
                
                # Şarkının kopyasını oluştur
                song_copy = dict(next_song)
                if 'start_time' in song_copy:
                    del song_copy['start_time']  # Başlangıç zamanını kopya için siliyoruz
                
                self.played_history[guild_id].append(song_copy)
                logger.info(f"play_next: Şarkı geçmiş listesine eklendi (kuyruk döngüsü için).")
            
            # Now playing sözlüğünü oluştur (yoksa)
            if not hasattr(self, 'now_playing'):
                self.now_playing = {}
            
            # Şarkı başlangıç zamanını kaydet
            next_song['start_time'] = datetime.datetime.now()
            self.now_playing[guild_id] = next_song
            logger.info(f"play_next: now_playing güncellendi, start_time: {next_song['start_time']}")
            
            # Ses kaynağını oluştur
            ffmpeg_options = dict(self.ffmpeg_options) if hasattr(self, 'ffmpeg_options') else {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'
            }
            
            try:
                audio_source = discord.FFmpegPCMAudio(next_song['url'], **ffmpeg_options)
                logger.info(f"play_next: Ses kaynağı oluşturuldu.")
            except Exception as e:
                logger.error(f"play_next: Ses kaynağı oluşturulurken hata: {e}\n{traceback.format_exc()}")
                if ctx:
                    await ctx.send(f"❌ Şarkı çalınırken bir hata oluştu: {str(e)[:500]}")
                return False
            
            # Ses seviyesini ayarla
            volume = 0.5  # Varsayılan ses seviyesi
            if hasattr(self, 'volume'):
                volume = self.volume
            
            volume_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
            
            # Şarkı bitince bir sonrakine geç fonksiyonu
            def after_playing(error):
                if error:
                    logger.error(f"play_next: Müzik çalınırken hata: {error}")
                else:
                    logger.info(f"play_next: Şarkı bitti, bir sonrakine geçiliyor.")
                
                # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
                coro = self.play_next(guild_id, ctx, True)  # from_callback=True ile çağır
                future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"play_next: Bir sonraki şarkıya geçerken hata: {e}\n{traceback.format_exc()}")
            
            # Döngü ayarını kontrol et
            loop_mode = "off"
            if hasattr(self, 'loop_settings'):
                loop_mode = self.loop_settings.get(guild_id, "off")
                
            # Eğer tek şarkı döngüsü açıksa, şarkıyı kuyruğa geri ekle
            if loop_mode == "song":
                # Şarkının kopyasını oluştur
                song_copy = dict(next_song)
                if 'start_time' in song_copy:
                    del song_copy['start_time']  # Başlangıç zamanını kopya için siliyoruz
                
                # Kuyruğun başına ekle
                if guild_id not in self.queues:
                    self.queues[guild_id] = deque()
                self.queues[guild_id].appendleft(song_copy)
                logger.info(f"play_next: Tek şarkı döngüsü aktif, şarkı kuyruğa geri eklendi.")
            
            # Şarkıyı çal
            voice_client.play(volume_source, after=after_playing)
            
            # Şarkı başladı mesajı
            if ctx:
                embed = discord.Embed(
                    title="▶️ Şimdi Çalınıyor",
                    description=f"**{next_song['title']}**",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Süre", value=next_song['duration'], inline=True)
                embed.add_field(name="Ekleyen", value=next_song.get('requester', 'Bilinmiyor'), inline=True)
                
                # Döngü ve karıştırma durumunu göster
                status_text = ""
                if loop_mode != "off":
                    status_text += f"Döngü: {loop_mode.capitalize()} | "
                
                if hasattr(self, 'shuffle_settings') and self.shuffle_settings.get(guild_id, False):
                    status_text += "Karıştırma: Açık | "
                
                if status_text:
                    embed.add_field(name="Ayarlar", value=status_text[:-3], inline=True)  # Son '| ' karakterlerini kaldır
                
                if next_song.get('thumbnail'):
                    embed.set_thumbnail(url=next_song['thumbnail'])
                await ctx.send(embed=embed)
            
            return True
            
        except Exception as e:
            logger.error(f"Şarkı çalınırken hata: {e}")
            traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"Şarkı çalınırken hata detayı:\n{traceback_str}")
            if ctx:
                await ctx.send(f"❌ Şarkı çalınırken bir hata oluştu: {str(e)[:1000]}")
            return False
    
    async def cleanup(self, guild_id: int):
        """Ses bağlantısını ve ilgili kaynakları temizle"""
        # Ses bağlantısını kapat
        if guild_id in self.voice_clients and self.voice_clients[guild_id].is_connected():
            await self.voice_clients[guild_id].disconnect()
            del self.voice_clients[guild_id]
            
        # Kuyruğu temizle
        if guild_id in self.queues:
            self.queues[guild_id].clear()
            del self.queues[guild_id]
            
        # Şimdi çalan bilgisini temizle
        if guild_id in self.now_playing:
            self.now_playing[guild_id] = None
            
        # Döngü ve karıştırma ayarlarını sıfırla
        if hasattr(self, 'loop_settings') and guild_id in self.loop_settings:
            del self.loop_settings[guild_id]
            
        if hasattr(self, 'shuffle_settings') and guild_id in self.shuffle_settings:
            del self.shuffle_settings[guild_id]
            
        logger.info(f"Sunucu {guild_id} için müzik kaynakları temizlendi.")
    
    async def seek(self, guild_id: int, position_seconds: int, ctx: Optional[commands.Context] = None) -> bool:
        """Mevcut çalan şarkıyı belirli bir konuma atla. Başarılıysa True, başarısızsa False döndür."""
        lock = self.get_lock(guild_id)
        async with lock:
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    logger.error(f"seek: Guild {guild_id} bulunamadı.")
                    if ctx: await ctx.send("❌ Sunucu bilgisi bulunamadı.")
                    return False
            
                voice_client = guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    logger.error(f"seek: Sunucu {guild_id} için ses bağlantısı yok.")
                    if ctx: await ctx.send("❌ Ses kanalına bağlı değilim.")
                    return False
            
                if not voice_client.is_playing() and not voice_client.is_paused():
                    logger.error(f"seek: Şu anda çalan veya duraklatılmış bir şarkı yok.")
                    if ctx: await ctx.send("❌ Şu anda çalan bir şarkı yok.")
                    return False
            
                current_song_info = self.now_playing.get(guild_id)
                if not current_song_info:
                    logger.error(f"seek: now_playing sözlüğünde şarkı bilgisi bulunamadı.")
                    if ctx: await ctx.send("❌ Şarkı bilgisi bulunamadı.")
                    return False
            
                current_url = current_song_info.get('url')
                if not current_url:
                    logger.error(f"seek: Şarkı URL'si bulunamadı: {current_song_info}")
                    if ctx: await ctx.send("❌ Şarkı URL'si bulunamadı.")
                    return False
                
                song_title = current_song_info.get('title', 'Bilinmeyen Şarkı')
                song_requester = current_song_info.get('requester')

                if position_seconds < 0:
                    position_seconds = 0
                    logger.warning(f"seek: Negatif pozisyon {position_seconds}s -> 0s olarak düzeltildi.")

                self.is_seeking[guild_id] = True
                logger.info(f"seek: Set is_seeking=True for guild {guild_id} before stopping playback.")

                try:
                    # Mevcut şarkıyı durdur. Bu, eğer varsa, normal şarkı bitiş callback'ini tetikleyebilir.
                    # Ancak o callback is_seeking bayrağını kontrol edip atlayacaktır.
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop()
                        logger.info(f"seek: Playback stopped for guild {guild_id} to seek to {position_seconds}s.")

                    ffmpeg_opts = self.ffmpeg_options.copy()
                    ffmpeg_opts['before_options'] = f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {position_seconds}"
                    
                    logger.info(f"seek: Attempting to stream from {current_url} at {position_seconds}s for guild {guild_id}.")
                    audio_source = discord.FFmpegPCMAudio(current_url, **ffmpeg_opts)
                    volume_source = discord.PCMVolumeTransformer(audio_source, volume=self.volume)

                    # Seek sonrası çalma bittiğinde çağrılacak fonksiyon
                    def after_playing_for_seek(error):
                        if error:
                            logger.error(f"seek: Error during playback after seek for guild {guild_id}: {error}")
                        else:
                            logger.info(f"seek: Playback successfully finished after seek for guild {guild_id}.")
                        
                        self.is_seeking[guild_id] = False
                        logger.info(f"seek: Set is_seeking=False for guild {guild_id} from seek's own callback.")
                        
                        # Kuyruktaki bir sonraki şarkıyı çal
                        # ctx orijinal komut bağlamını taşıyabilir, play_next'e iletiyoruz.
                        coro = self.play_next(guild_id, ctx, from_callback=True) 
                        future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                        try:
                            future.result()
                        except Exception as e_play_next:
                            logger.error(f"seek: Error in play_next called after seek for guild {guild_id}: {e_play_next}\n{traceback.format_exc()}")
            
                    # Şarkının başlangıç zamanını güncelle (seek pozisyonuna göre)
                    # Bu now_playing girdisini güncel tutar.
                    new_start_time = datetime.datetime.now() - datetime.timedelta(seconds=position_seconds)
                    self.now_playing[guild_id] = {
                        'url': current_url,
                        'title': song_title,
                        'requester': song_requester,
                        'start_time': new_start_time,
                        'channel_id': ctx.channel.id if ctx else current_song_info.get('channel_id'),
                        'message_id': ctx.message.id if ctx else current_song_info.get('message_id')
                    }
                    logger.info(f"seek: Updated now_playing for guild {guild_id} with new start_time: {new_start_time}")

                    voice_client.play(volume_source, after=after_playing_for_seek)
                    logger.info(f"seek: Playback started from {position_seconds}s for guild {guild_id} with title '{song_title}'.")
                    
                    if ctx:
                        minutes, seconds_display = divmod(position_seconds, 60)
                        await ctx.send(f"⏩ **{song_title}** şarkısı {minutes:02d}:{seconds_display:02d} konumuna atlandı.")
                    return True
                
                except Exception as e:
                    logger.error(f"seek: General error during seek operation for guild {guild_id}: {e}\n{traceback.format_exc()}")
                    self.is_seeking[guild_id] = False # Hata durumunda bayrağı sıfırla
                    logger.info(f"seek: Set is_seeking=False for guild {guild_id} due to an exception.")
                    if ctx:
                        await ctx.send(f"❌ Şarkı konumuna gidilirken bir hata oluştu: {str(e)[:100]}")
                    return False
            # Misplaced comment and return True removed. Successful return should be at the end of the preceding 'try' block.
            
            except Exception as e:
                logger.error(f"seek: General error during seek operation for guild {guild_id}: {e}\n{traceback.format_exc()}")
                self.is_seeking[guild_id] = False
                logger.info(f"seek: Set is_seeking=False for guild {guild_id} due to an exception in general handler.")
                if ctx:
                    await ctx.send(f"❌ Şarkı konumlandırılırken genel bir hata oluştu. Detaylar loglarda. ({str(e)[:150]})")
                return False
    
    async def forward(self, guild_id: int, seconds: int, ctx: Optional[commands.Context] = None) -> bool:
        """Mevcut çalan şarkıyı belirli bir süre ileri sar. Başarılıysa True, başarısızsa False döndür."""
        # Guild ve ses bağlantısı kontrolü
        guild = bot.get_guild(guild_id)
        if not guild:
            logger.error(f"forward: Guild {guild_id} bulunamadı.")
            if ctx:
                await ctx.send("❌ Sunucu bilgisi bulunamadı.")
            return False
        
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            logger.error(f"forward: Sunucu {guild_id} için ses bağlantısı yok.")
            if ctx:
                await ctx.send("❌ Ses kanalına bağlı değilim.")
            return False
        
        if not voice_client.is_playing():
            if ctx:
                await ctx.send("❌ Şu anda çalan bir şarkı yok.")
            return False
        
        # Mevcut pozisyonu tahmin et
        current_position = 0
        
        # Şimdi çalan şarkı bilgisi
        current_song = self.now_playing.get(guild_id, None)
        
        # Eğer şarkı başlangıç zamanı kaydedilmişse, mevcut pozisyonu hesapla
        if current_song and 'start_time' in current_song:
            current_position = int((datetime.datetime.now() - current_song['start_time']).total_seconds())
            logger.info(f"forward: Mevcut pozisyon: {current_position} saniye")
        else:
            # Eğer start_time yoksa, tahmini bir değer kullan
            logger.warning(f"forward: Şarkı için start_time bulunamadı, varsayılan pozisyon kullanılıyor.")
            if ctx:
                await ctx.send("⚠️ Şarkı başlangıç zamanı bilinmiyor. Varsayılan pozisyon kullanılıyor.")
        
        # Yeni pozisyon = mevcut pozisyon + ileri sarma süresi
        new_position = current_position + seconds
        logger.info(f"forward: Yeni pozisyon: {new_position} saniye (+{seconds} saniye)")
        
        # Bilgi mesajı
        if ctx:
            minutes, seconds_display = divmod(seconds, 60)
            time_str = f"{minutes:02d}:{seconds_display:02d}"
            await ctx.send(f"⏩ Şarkı {time_str} ileri sarılıyor...")
        
        # Seek metodunu kullanarak ileri sar - ctx parametresini iletiyoruz
        return await self.seek(guild_id, new_position, ctx)
        
    async def rewind(self, guild_id: int, seconds: int, ctx: Optional[commands.Context] = None) -> bool:
        """Mevcut çalan şarkıyı belirli bir süre geri sar. Başarılıysa True, başarısızsa False döndür."""
        # Guild ve ses bağlantısı kontrolü
        guild = bot.get_guild(guild_id)
        if not guild:
            logger.error(f"rewind: Guild {guild_id} bulunamadı.")
            if ctx:
                await ctx.send("❌ Sunucu bilgisi bulunamadı.")
            return False
        
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            logger.error(f"rewind: Sunucu {guild_id} için ses bağlantısı yok.")
            if ctx:
                await ctx.send("❌ Ses kanalına bağlı değilim.")
            return False
        
        if not voice_client.is_playing():
            if ctx:
                await ctx.send("❌ Şu anda çalan bir şarkı yok.")
            return False
        
        # Mevcut pozisyonu tahmin et
        current_position = 0
        
        # Şimdi çalan şarkı bilgisi
        current_song = self.now_playing.get(guild_id, None)
        
        # Eğer şarkı başlangıç zamanı kaydedilmişse, mevcut pozisyonu hesapla
        if current_song and 'start_time' in current_song:
            current_position = int((datetime.datetime.now() - current_song['start_time']).total_seconds())
            logger.info(f"rewind: Mevcut pozisyon: {current_position} saniye")
        else:
            # Eğer start_time yoksa, tahmini bir değer kullan
            logger.warning(f"rewind: Şarkı için start_time bulunamadı, varsayılan pozisyon kullanılıyor.")
            if ctx:
                await ctx.send("⚠️ Şarkı başlangıç zamanı bilinmiyor. Varsayılan pozisyon kullanılıyor.")
        
        # Yeni pozisyon = mevcut pozisyon - geri sarma süresi (en az 0)
        new_position = max(0, current_position - seconds)
        logger.info(f"rewind: Yeni pozisyon: {new_position} saniye (-{seconds} saniye)")
        
        # Bilgi mesajı
        if ctx:
            minutes, seconds_display = divmod(seconds, 60)
            time_str = f"{minutes:02d}:{seconds_display:02d}"
            await ctx.send(f"⏪ Şarkı {time_str} geri sarılıyor...")
        
        # Seek metodunu kullanarak geri sar - ctx parametresini iletiyoruz
        return await self.seek(guild_id, new_position, ctx)
    
    def toggle_loop(self, guild_id: int) -> str:
        """Belirli bir sunucu için döngü modunu değiştir. Döngü durumunu döndür."""
        # Döngü ayarları sözlüğü oluştur (yoksa)
        if not hasattr(self, 'loop_settings'):
            self.loop_settings = {}
        
        # Mevcut döngü ayarını al (varsayılan: kapalı)
        current_setting = self.loop_settings.get(guild_id, "off")
        
        # Döngü modunu değiştir (off -> song -> queue -> off)
        if current_setting == "off":
            new_setting = "song"  # Tek şarkı döngüsü
        elif current_setting == "song":
            new_setting = "queue"  # Kuyruk döngüsü
        else:  # queue
            new_setting = "off"  # Döngü kapalı
        
        # Yeni ayarı kaydet
        self.loop_settings[guild_id] = new_setting
        
        return new_setting
    
    def toggle_shuffle(self, guild_id: int) -> bool:
        """Belirli bir sunucu için karıştırma modunu değiştir. Karıştırma durumunu döndür."""
        # Karıştırma ayarları sözlüğü oluştur (yoksa)
        if not hasattr(self, 'shuffle_settings'):
            self.shuffle_settings = {}
        
        # Mevcut karıştırma ayarını al (varsayılan: False)
        current_setting = self.shuffle_settings.get(guild_id, False)
        
        # Karıştırma modunu değiştir
        new_setting = not current_setting
        
        # Yeni ayarı kaydet
        self.shuffle_settings[guild_id] = new_setting
        
        # Eğer karıştırma açıldıysa ve kuyruk varsa, kuyruğu karıştır
        if new_setting and guild_id in self.queues and self.queues[guild_id]:
            queue_list = list(self.queues[guild_id])
            random.shuffle(queue_list)
            self.queues[guild_id] = deque(queue_list)
        
        return new_setting
    
    def shuffle_queue(self, guild_id: int) -> bool:
        """Belirli bir sunucunun kuyruğunu karıştır. Başarılıysa True, başarısızsa False döndür."""
        if guild_id not in self.queues or not self.queues[guild_id]:
            return False
            
        queue_list = list(self.queues[guild_id])
        random.shuffle(queue_list)
        self.queues[guild_id] = deque(queue_list)
        
        return True
        
    def create_after_playing_callback(self, guild_id: int, current_song: Dict[str, Any] = None):
        """Bir şarkı bittiğinde çağrılacak callback fonksiyonu oluştur.
        
        Args:
            guild_id: Sunucu ID'si
            current_song: Şu anda çalan şarkı bilgisi (None ise now_playing'den alınır)
        """
        def after_playing(error):
            # Eğer seek işlemi devam ediyorsa, bu callback'i atla.
            # Seek'in kendi callback'i is_seeking'i yönetecek ve play_next'i çağıracak.
            if self.is_seeking.get(guild_id, False):
                logger.info(f"create_after_playing_callback: Normal playback after_playing for guild {guild_id} skipped because is_seeking is True.")
                return

            if error:
                logger.error(f"Müzik çalınırken hata (normal playback): {error}")
                
            # Eğer current_song verilmişse, şarkı bilgilerini koru (bu genellikle play_next'te zaten güncellenir)
            # Bu kontrol burada gereksiz olabilir, play_next zaten now_playing'i yönetiyor.
            # if current_song is not None:
            #     self.now_playing[guild_id] = current_song
            
            # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
            # ctx parametresi olarak None gönder, from_callback=True olarak işaretle
            coro = self.play_next(guild_id, None, from_callback=True)
            future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
            try:
                future.result()
            except Exception as e:
                logger.error(f"Bir sonraki şarkıya geçerken hata (normal playback): {e}\n{traceback.format_exc()}")
            
        return after_playing

# Müzik oynatıcı örneği oluştur
music_player = MusicPlayer()

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

# Komut izleme sistemi - aynı komutun birden fazla işlenmesini önlemek için
# Yapı: command_id (channel_id + message_id) -> timestamp
processed_commands = {}

# Komut izleme sisteminden muaf tutulacak komutlar
exempt_commands = [
    'play', 'skip', 'stop', 'pause', 'resume', 'queue', 'np', 'volume', 'rewind', 'forward', 'seek', 'loop', 'shuffle',
    'çal', 'duraklat', 'devam', 'dur', 'ses', 'ileri', 'geri'
]

# --- Veritabanı Yardımcı Fonksiyonları (PostgreSQL - BAĞLANTI HAVUZU İLE OPTİMİZASYON) ---
# Veritabanı bağlantı havuzu oluştur
from psycopg2 import pool

# Global bağlantı havuzu
db_pool = None

def init_db_pool():
    """Veritabanı bağlantı havuzunu başlat"""
    global db_pool
    try:
        if db_pool is None:
            # Minimum 1, maksimum 10 bağlantı ile bir havuz oluştur
            db_pool = pool.ThreadedConnectionPool(1, 10, DATABASE_URL, sslmode='require')
            logger.info("PostgreSQL bağlantı havuzu başarıyla oluşturuldu.")
        return True
    except Exception as e:
        logger.critical(f"PostgreSQL bağlantı havuzu oluşturulurken hata: {e}")
        return False

def get_db_connection():
    """Havuzdan bir bağlantı al"""
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
    """Bağlantıyı havuza geri ver"""
    global db_pool
    if db_pool is not None and conn is not None:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Bağlantı havuza geri verilirken hata: {e}")
            try:
                conn.close()
            except:
                pass

# --- Ses Ayarları için Veritabanı Fonksiyonları ---
def check_volume_table_structure():
    """Ses ayarları tablosunun yapısını kontrol eder ve sütun adlarını döndürür."""
    try:
        conn = db_connect()
        if not conn:
            return None
            
        with conn.cursor() as cur:
            # Tablo var mı kontrol et
            cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'volume_settings')")
            if not cur.fetchone()[0]:
                return None  # Tablo yok
                
            # Sütunları kontrol et
            cur.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'volume_settings'
            """)
            columns = [row[0] for row in cur.fetchall()]
            return columns
            
    except psycopg2.Error as e:
        logger.error(f"Ses ayarları tablosu yapısı kontrol edilirken hata: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)

def setup_volume_table():
    """Ses ayarları için PostgreSQL tablosunu oluşturur veya günceller."""
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları tablosu oluşturulamadı: Veritabanı bağlantısı kurulamadı.")
            return False
            
        # Tablo yapısını kontrol et
        columns = check_volume_table_structure()
        
        with conn.cursor() as cur:
            if columns is None:
                # Tablo yok, yeni oluştur
                cur.execute("""
                    CREATE TABLE volume_settings (
                        id SERIAL PRIMARY KEY,
                        setting_key VARCHAR(50) UNIQUE NOT NULL,
                        setting_value FLOAT NOT NULL
                    )
                """)
                
                # Varsayılan değerleri ekle
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', 0.5)")
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', 0.5)")
                
                conn.commit()
                logger.info("Ses ayarları tablosu oluşturuldu ve varsayılan değerler eklendi.")
            elif 'key' in columns and 'value' in columns:
                # Eski şema ile uyumlu
                logger.info("Ses ayarları tablosu eski şema ile mevcut, uyumlu şekilde kullanılacak.")
            elif 'setting_key' in columns and 'setting_value' in columns:
                # Yeni şema ile uyumlu
                logger.info("Ses ayarları tablosu yeni şema ile mevcut.")
            else:
                # Uyumsuz şema, tabloyu yeniden oluştur
                logger.warning("Ses ayarları tablosu uyumsuz şema ile mevcut, yeniden oluşturulacak.")
                cur.execute("DROP TABLE volume_settings")
                cur.execute("""
                    CREATE TABLE volume_settings (
                        id SERIAL PRIMARY KEY,
                        setting_key VARCHAR(50) UNIQUE NOT NULL,
                        setting_value FLOAT NOT NULL
                    )
                """)
                
                # Varsayılan değerleri ekle
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('current_volume', 0.5)")
                cur.execute("INSERT INTO volume_settings (setting_key, setting_value) VALUES ('default_volume', 0.5)")
                
                conn.commit()
                logger.info("Ses ayarları tablosu yeniden oluşturuldu ve varsayılan değerler eklendi.")
                
            return True
            
    except psycopg2.Error as e:
        logger.error(f"Ses ayarları tablosu oluşturulurken hata: {e}")
        return False
    finally:
        if conn:
            release_db_connection(conn)

def save_volume_settings(current_volume, default_volume):
    """Ses seviyesi ayarlarını PostgreSQL'e kaydeder."""
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları kaydedilemedi: Veritabanı bağlantısı kurulamadı.")
            return False
        
        # Tablo yapısını kontrol et
        columns = check_volume_table_structure()
        if columns is None:
            # Tablo yok, oluştur
            setup_volume_table()
            columns = check_volume_table_structure()
            if columns is None:
                logger.error("Ses ayarları tablosu oluşturulamadı.")
                return False
        
        with conn.cursor() as cur:
            if 'key' in columns and 'value' in columns:
                # Eski şema
                # Mevcut ses seviyesini güncelle
                cur.execute("UPDATE volume_settings SET value = %s WHERE key = 'current_volume'", (current_volume,))
                
                # Varsayılan ses seviyesini güncelle
                cur.execute("UPDATE volume_settings SET value = %s WHERE key = 'default_volume'", (default_volume,))
            elif 'setting_key' in columns and 'setting_value' in columns:
                # Yeni şema
                # Mevcut ses seviyesini güncelle
                cur.execute("UPDATE volume_settings SET setting_value = %s WHERE setting_key = 'current_volume'", (current_volume,))
                
                # Varsayılan ses seviyesini güncelle
                cur.execute("UPDATE volume_settings SET setting_value = %s WHERE setting_key = 'default_volume'", (default_volume,))
            else:
                logger.error("Ses ayarları tablosu uyumsuz şema ile mevcut.")
                return False
            
            conn.commit()
            logger.debug(f"Ses ayarları kaydedildi: current={current_volume}, default={default_volume}")
            return True
    except Exception as e:
        logger.error(f"Ses ayarları kaydedilirken hata: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: release_db_connection(conn)

def get_volume_settings():
    """Ses seviyesi ayarlarını PostgreSQL'den yükler."""
    try:
        conn = db_connect()
        if not conn:
            logger.error("Ses ayarları yüklenemedi: Veritabanı bağlantısı kurulamadı.")
            return (0.5, 0.5)  # Varsayılan değerler
        
        # Tablo yapısını kontrol et
        columns = check_volume_table_structure()
        if columns is None:
            # Tablo yok, oluştur
            setup_volume_table()
            columns = check_volume_table_structure()
            if columns is None:
                logger.error("Ses ayarları tablosu oluşturulamadı.")
                return (0.5, 0.5)  # Varsayılan değerler
            
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Tüm ses ayarlarını al
            if 'key' in columns and 'value' in columns:
                # Eski şema
                cur.execute("SELECT key, value FROM volume_settings WHERE key IN ('current_volume', 'default_volume')")
                rows = cur.fetchall()
                if not rows:
                    return (0.5, 0.5)  # Varsayılan değerler
                
                # Değerleri al
                current_volume = default_volume = 0.5
                for row in rows:
                    if row['key'] == 'current_volume':
                        current_volume = float(row['value'])
                    elif row['key'] == 'default_volume':
                        default_volume = float(row['value'])
                
                return (current_volume, default_volume)
            elif 'setting_key' in columns and 'setting_value' in columns:
                # Yeni şema
                cur.execute("SELECT setting_key, setting_value FROM volume_settings WHERE setting_key IN ('current_volume', 'default_volume')")
                rows = cur.fetchall()
                if not rows:
                    return (0.5, 0.5)  # Varsayılan değerler
                
                # Değerleri al
                current_volume = default_volume = 0.5
                for row in rows:
                    if row['setting_key'] == 'current_volume':
                        current_volume = float(row['setting_value'])
                    elif row['setting_key'] == 'default_volume':
                        default_volume = float(row['setting_value'])
                
                return (current_volume, default_volume)
            else:
                logger.error("Ses ayarları tablosu uyumsuz şema ile mevcut.")
                return (0.5, 0.5)  # Varsayılan değerler
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Ses ayarları yüklenirken hata: {e}")
        return (0.5, 0.5)  # Hata durumunda varsayılan değerler
    finally:
        if conn:
            release_db_connection(conn)

def db_connect():
    """PostgreSQL veritabanına bağlanır (bağlantı havuzunu kullanarak)."""
    return get_db_connection()

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
        if conn: release_db_connection(conn)

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
        logger.error(f"Yapılandırma ayarı kaydedilirken hata: {e}")
        if conn: conn.rollback()
    finally:
        if conn: release_db_connection(conn)

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
        if conn: release_db_connection(conn)

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
                # last_active'i datetime objesine çevir
                last_active_dt = row['last_active']
                if not last_active_dt.tzinfo:
                    last_active_dt = last_active_dt.replace(tzinfo=datetime.timezone.utc)
                
                # model_name'i kontrol et ve varsayılanı ayarla
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
        if conn: release_db_connection(conn)

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
        if conn: release_db_connection(conn)

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
            logger.debug(f"Geçici kanal {channel_id} PostgreSQL'de bulunamadı (silinirken).")
    except (Exception, psycopg2.DatabaseError) as e:
        logger.error(f"Geçici kanal silinirken PostgreSQL hatası (channel_id: {channel_id}): {e}")
        if conn: conn.rollback()
    finally:
        if conn: release_db_connection(conn)

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
          if conn: release_db_connection(conn)

def update_channel_activity_db(channel_id, timestamp):
     """DB'deki bir kanalın son aktivite zamanını günceller."""
     conn = None
     sql = "UPDATE temp_channels SET last_active = %s WHERE channel_id = %s;"
     try:
          conn = db_connect()
          cursor = conn.cursor()
          cursor.execute(sql, (timestamp, channel_id))
          conn.commit()
          cursor.close()
          logger.debug(f"DB'deki Kanal {channel_id} son aktivite zamanı güncellendi.")
     except (Exception, psycopg2.DatabaseError) as e:
          logger.error(f"DB Kanal aktivitesi güncellenirken hata (channel_id: {channel_id}): {e}")
          if conn: conn.rollback()
     finally:
          if conn: release_db_connection(conn)


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

# --- Müzik İçin Ses Ayarları ---
# Ses ayarları tablosu oluştur
if not setup_volume_table():
    logger.warning("Ses ayarları tablosu oluşturulamadı, varsayılan değerler kullanılacak")

# Ses ayarlarını yükle
volume_settings = get_volume_settings()

# --- Müzik Çalar Sınıfı ---
class MusicPlayer:
    def __init__(self):
        self.queues = {}  # guild_id: şarkı kuyruğu
        self.now_playing = {}  # guild_id: çalan şarkı bilgisi
        self.voice_clients = {}  # guild_id: ses istemcisi
        self.played_history = {}  # guild_id: çalınan şarkıların geçmişi
        self.loop_settings = {}  # guild_id: döngü ayarı ("off", "song", "queue")
        self.shuffle_settings = {}  # guild_id: karıştırma ayarı (True/False)
        
        # Ses ayarlarını veritabanından yükle
        volume_settings = get_volume_settings()
        self.volume = volume_settings[0]  # Mevcut ses seviyesi
        self.default_volume = volume_settings[1]  # Varsayılan ses seviyesi
        
        # yt-dlp ayarları
        self.ytdl_format_options = {
            'format': 'bestaudio/best',
            'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'source_address': '0.0.0.0'
        }
        
        self.ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn',
        }

    async def play_next(self, guild_id: int, ctx: commands.Context, from_callback: bool = False):
        """Kuyrukta sıradaki şarkıyı çal
        
        Args:
            guild_id: Sunucu ID'si
            ctx: Komut bağlamı
            from_callback: Eğer True ise, after_playing callback'inden çağrıldığını belirtir
        """
        if guild_id not in self.queues or not self.queues[guild_id]:
            self.now_playing[guild_id] = None
            return
            
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            if not from_callback:  # Sadece doğrudan komuttan çağrılırsa mesaj gönder
                await ctx.send("❌ Ses kanalına bağlı değilim!")
            return
        
        # Kuyruktan sonraki şarkıyı al
        next_song = self.queues[guild_id].popleft()
        self.now_playing[guild_id] = next_song
        
        def after_playing(error):
            if error:
                logger.error(f"Müzik çalma hatası: {error}")
            # Callback'den çağrıldığını belirt
            asyncio.run_coroutine_threadsafe(self.play_next(guild_id, ctx, True), bot.loop)
        
        # Şarkıyı çalmaya başla
        self.voice_clients[guild_id].play(
            discord.FFmpegPCMAudio(next_song['url'], **self.ffmpeg_options),
            after=after_playing)
        self.voice_clients[guild_id].source = discord.PCMVolumeTransformer(self.voice_clients[guild_id].source, volume=self.volume)
        
        # Sadece doğrudan komuttan çağrılırsa veya ilk çağrıda mesaj gönder
        if not from_callback:
            await ctx.send(f"▶️ Şimdi çalınıyor: **{next_song['title']}** - {next_song['duration']}")
        else:
            # Callback'den çağrıldığında sessizce geçiş yap
            logger.info(f"Sonraki şarkıya geçiliyor: {next_song['title']}")


    async def join_voice_channel(self, ctx: commands.Context):
        """Kullanıcının ses kanalına katıl"""
        if ctx.author.voice is None:
            await ctx.send("❌ Bir ses kanalında olmalısın!")
            return False
            
        channel = ctx.author.voice.channel
        if ctx.guild.id not in self.voice_clients:
            self.voice_clients[ctx.guild.id] = await channel.connect()
            return True
        else:
            await self.voice_clients[ctx.guild.id].move_to(channel)
            return True

# --- Bot Kurulumu ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.messages = True
intents.guilds = True
intents.voice_states = True  # Ses durumu izinleri eklendi

# Duplicate mesajları önlemek için geliştirilmiş çözüm
# Komut başına bir bayrak ve periyodik temizleme kullanacağız

# Özel bot sınıfı oluştur
class CustomBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_lock = asyncio.Lock()  # Komut işleme için kilit oluştur
    
    async def get_context(self, message, *, cls=commands.Context):
        # Normal context'i al
        ctx = await super().get_context(message, cls=cls)
        return ctx


# Bot oluştur
bot = CustomBot(command_prefix=['!', '.'], intents=intents, help_command=None)

# Müzik çaları başlat
music_player = MusicPlayer()

# Komut izleme sistemi - aynı komutun birden fazla işlenmesini önler
@bot.event
async def on_command(ctx):
    """Komut izleme sistemi - aynı komutun birden fazla işlenmesini önler."""
    global processed_commands
    
    # Eğer bu bir muaf komut ise, izleme sistemini atla
    if ctx.command and ctx.command.name in exempt_commands:
        logger.debug(f"Komut izleme sisteminden muaf tutuldu: {ctx.command.name}")
        return
    
    # Benzersiz bir komut kimliği oluştur (kanal + mesaj kimliği)
    command_id = f"{ctx.channel.id}-{ctx.message.id}"
    
    # Bu komut daha önce işlendi mi kontrol et
    if command_id in processed_commands:
        logger.warning(f"Komut zaten işlendi, tekrar işlenmeyecek: {ctx.command.name} (ID: {command_id})")
        ctx.command = None  # Komutu None olarak ayarlayarak işlenmesini engelle
        return
    
    # Komutu işlendi olarak işaretle
    processed_commands[command_id] = datetime.datetime.now()
    logger.debug(f"Komut işleme başladı: {ctx.command.name} (ID: {command_id})")

# Komut izleme temizleme görevi
@tasks.loop(minutes=5)
async def cleanup_command_tracking():
    """Komut izleme sözlüğünü temizler ve bellek sızıntılarını önler."""
    global processed_commands
    now = datetime.datetime.now()
    expired_commands = []
    
    # 60 saniyeden eski komutları temizle
    for command_id, timestamp in processed_commands.items():
        if (now - timestamp).total_seconds() > 60:
            expired_commands.append(command_id)
    
    # Eski komutları kaldır
    for command_id in expired_commands:
        processed_commands.pop(command_id, None)
    
    if expired_commands:
        logger.debug(f"{len(expired_commands)} eski komut izleme kaydı temizlendi.")

@cleanup_command_tracking.before_loop
async def before_cleanup_command_tracking():
    await bot.wait_until_ready()
    logger.info("Komut izleme temizleme görevi başlatıldı.")

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

    # Boş mesajları işleme
    if not prompt_text or not prompt_text.strip(): 
        return False
        
    # Komut olup olmadığını son bir kez daha kontrol et
    # Bu, on_message'da ve process_ai_message'da yapılan kontrollere ek bir güvenlik katmanıdır
    if prompt_text.startswith(tuple(bot.command_prefix)):
        potential_command = prompt_text.split()[0][1:] if prompt_text.split() else ""
        if potential_command and bot.get_command(potential_command):
            logger.debug(f"send_to_ai_and_respond: Komut algılandı, AI yanıtı engellendi - {potential_command}")
            return False

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

            # --- Yanıt İşleme ve Gönderme ---
            if not error_occurred and ai_response_text:
                # Başarılı yanıt durumunda hata mesajını temizle
                user_error_msg = ""
                
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
                try:
                    update_channel_activity_db(channel_id, now_utc)
                except Exception as e:
                    logger.error(f"Kanal aktivitesi güncellenirken hata: {e}")
                warned_inactive_channels.discard(channel_id)
                return True
            elif not error_occurred and not ai_response_text:
                 logger.info(f"AI'dan boş yanıt alındı, mesaj gönderilmiyor (Kanal: {channel_id}).")
                 now_utc = datetime.datetime.now(datetime.timezone.utc)
                 channel_last_active[channel_id] = now_utc
                 try:
                     update_channel_activity_db(channel_id, now_utc)
                 except Exception as e:
                     logger.error(f"Kanal aktivitesi güncellenirken hata: {e}")
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

# Komut takip sistemleri
command_tracking = {}  # on_command için
processed_commands = {}  # on_message için

# Komut takip sisteminden muaf tutulacak komutlar
exempt_commands = [
    # Müzik komutları
    'play', 'pause', 'resume', 'stop', 'volume', 'forward', 'rewind', 'seek', 'skip', 'queue', 'loop', 'shuffle',
    'çal', 'duraklat', 'devam', 'dur', 'ses', 'ileri', 'geri', 'geç', 'kuyruk', 'döngü', 'karıştır',
    # Diğer komutlar
    'clear', 'temizle'
]

# Komut izleme temizleme görevi
@tasks.loop(seconds=60)
async def cleanup_command_tracking():
    """Eski komut izleme kayıtlarını temizle (60 saniyeden eski)"""
    now = datetime.datetime.now()
    to_remove = []
    
    # processed_commands temizleme
    for cmd_id, timestamp in processed_commands.items():
        if (now - timestamp).total_seconds() > 60:
            to_remove.append(cmd_id)
    
    for cmd_id in to_remove:
        processed_commands.pop(cmd_id, None)
    
    if to_remove:
        logger.debug(f"{len(to_remove)} eski komut izleme kaydı temizlendi.")


@bot.event
async def on_command(ctx):
    """Komut işleme olayı"""
    # Komut izleme sistemini tamamen devre dışı bıraktık
    # Bu fonksiyon artık sadece loglama için kullanılıyor
    if ctx.command:
        logger.debug(f"on_command: Komut işleniyor: {ctx.command.name}")
    return


# on_ready fonksiyonu aynı kalır
@bot.event
async def on_ready():
    """Bot hazır olduğunda çalışacak fonksiyon."""
    global entry_channel_id, inactivity_timeout, temporary_chat_channels, user_to_channel_map, channel_last_active
    
    # Temel bilgileri logla
    logger.info(f"{bot.user.name} olarak giriş yapıldı (ID: {bot.user.id})")
    logger.info(f"Discord.py Sürümü: {discord.__version__}")
    
    # Veritabanı bağlantı havuzunu başlat
    init_db_pool()
    
    # Veritabanından ayarları yükle
    # Giriş kanalı ID'sini yükle
    entry_channel_id = int(load_config("entry_channel_id", DEFAULT_ENTRY_CHANNEL_ID) or 0)
    
    # İnaktivite zaman aşımını yükle
    timeout_hours = float(load_config("inactivity_timeout_hours", DEFAULT_INACTIVITY_TIMEOUT_HOURS))
    inactivity_timeout = datetime.timedelta(hours=timeout_hours) if timeout_hours > 0 else None
    
    # Ayarları logla
    logger.info(f"Mevcut Ayarlar - Giriş Kanalı: {entry_channel_id}, Zaman Aşımı: {inactivity_timeout}")
    if not entry_channel_id: 
        logger.warning("Giriş Kanalı ID'si ayarlanmamış! Otomatik kanal oluşturma devre dışı.")
    
    # Kalıcı verileri sıfırla ve yükle
    logger.info("Kalıcı veriler (geçici kanallar) yükleniyor...")
    temporary_chat_channels.clear()
    user_to_channel_map.clear()
    channel_last_active.clear()
    active_ai_chats.clear()
    warned_inactive_channels.clear()
    
    # Geçici kanalları yükle
    try:
        # Veritabanından geçici kanalları yükle
        conn = db_connect()
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute("SELECT channel_id, user_id, last_active, model_name FROM temp_channels")
        loaded_channels = cursor.fetchall()
        cursor.close()
        release_db_connection(conn)
        
        logger.info(f"{len(loaded_channels)} geçici kanal veritabanından yüklendi.")
    except Exception as e:
        logger.error(f"Geçici kanallar yüklenirken hata: {e}")
        loaded_channels = []
    
    # Arka plan görevlerini başlat
    check_inactivity.start()
    cleanup_command_tracking.start()  # Komut izleme temizleme görevini başlat
    
    # Bot durumunu ayarla
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/help | AI Chat"))
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


# on_message fonksiyonu - Hem AI chat hem de müzik özelliklerini destekler
@bot.event
async def on_message(message: discord.Message):
    # Bot veya diğer botlardan gelen mesajları yoksay
    if message.author == bot.user or message.author.bot: 
        return
    
    # Özel mesajları ve metin kanalı olmayan mesajları yoksay
    if not message.guild:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return
    
    # Mesajın kanalı ve içeriği
    channel = message.channel
    channel_id = channel.id
    author = message.author
    
    # Komut işleme için context oluştur
    ctx = await bot.get_context(message)
    
    # Komutları işle
    if ctx.command is not None:
        # Komutu doğrudan işle - hiçbir izleme yok
        logger.debug(f"Komut işleniyor: {ctx.command.name}")
        await bot.invoke(ctx)
        return # Eğer bu bir komutsa, AI işlemlerini yapma

    author = message.author; author_id = author.id
    channel = message.channel; channel_id = channel.id
    guild = message.guild

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
            except Exception as e:
                logger.error(f"-----> İLK İSTEK SIRASINDA HATA (Kanal: {new_channel_id}): {e}")
        return
    
    # --- Geçici Sohbet Kanallarındaki Mesajlar ---
    if channel_id in temporary_chat_channels and not message.author.bot:
        # Mesajın bir komut olup olmadığını kontrol et
        if ctx.command:
            # Bu bir komut, AI yanıtı verme
            logger.debug(f"Geçici kanalda komut algılandı, AI yanıtı verilmiyor: {ctx.command.name}")
            return
            
        # Mesajın bir komut olup olmadığını manuel olarak kontrol et (ekstra güvenlik)
        prompt_text = message.content
        if prompt_text.startswith(tuple(bot.command_prefix)):
            potential_command = prompt_text.split()[0][1:] if prompt_text.split() else ""
            if potential_command and bot.get_command(potential_command):
                logger.debug(f"Geçici kanalda komut algılandı (manuel kontrol), AI yanıtı verilmiyor: {potential_command}")
                return
        
        # Mesajı işlendi olarak işaretle
        processed_commands[message_id] = datetime.datetime.now()
        
        # AI yanıtı için mesajı gönder
        await process_ai_message(channel, author, prompt_text, channel_id)


# --- Komut İzleme Sistemi ---
@bot.event
async def on_command(ctx):
    """Komut izleme sistemi - aynı komutun birden fazla işlenmesini önler."""
    global processed_commands
    
    # Komut muaf listesi - bunları izleme sisteminden muaf tutacağız
    if ctx.command and ctx.command.name in exempt_commands:
        logger.debug(f"Komut izleme sisteminden muaf tutuldu: {ctx.command.name}")
        return
    
    # Benzersiz bir komut kimliği oluştur (kanal + mesaj kimliği)
    command_id = f"{ctx.channel.id}-{ctx.message.id}"
    
    # Bu komut daha önce işlendi mi kontrol et
    if command_id in processed_commands:
        logger.warning(f"Komut zaten işlendi, tekrar işlenmeyecek: {ctx.command.name} (ID: {command_id})")
        ctx.command = None  # Komutu None olarak ayarlayarak işlenmesini engelle
        return
    
    # Komutu işlendi olarak işaretle
    processed_commands[command_id] = datetime.datetime.now()
    logger.debug(f"Komut işleme başladı: {ctx.command.name} (ID: {command_id})")

# --- Komut İzleme Temizleme Görevi ---


# --- AI Mesaj İşleme Fonksiyonu ---
async def process_ai_message(channel, author, prompt_text, channel_id):
    """AI yanıtlarını işlemek için geliştirilmiş fonksiyon.
    Bu fonksiyon, komutların geçici kanallarda yanlışlıkla AI yanıtı üretmesini önler."""
    # Boş mesajları kontrol et
    if not prompt_text or not prompt_text.strip():
        return False
        
    # Bir kez daha komut olup olmadığını kontrol et (ekstra güvenlik)
    if prompt_text.startswith(tuple(bot.command_prefix)):
        potential_command = prompt_text.split()[0][1:] if prompt_text.split() else ""
        if potential_command and bot.get_command(potential_command):
            logger.debug(f"AI yanıtı engellendi: Muhtemel komut algılandı - {potential_command}")
            return False
    
    # Mevcut send_to_ai_and_respond fonksiyonunu çağır
    return await send_to_ai_and_respond(channel, author, prompt_text, channel_id)

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
                actual_deleted_count = len(deleted_messages)
                
                # Komut mesajını silmeyi dene, hata olursa devam et
                try:
                    await ctx.message.delete()
                except discord.errors.NotFound:
                    logger.debug("Komut mesajı zaten silinmiş.")
                except Exception as e:
                    logger.warning(f"Komut mesajı silinirken hata: {e}")
                
                # Mesajı sadece bir kez gönder
                msg = f"{actual_deleted_count} mesaj silindi (sabitlenmişler hariç)."
                
                # Mesaj gönderildiğinde bir bayrak ayarla
                if not hasattr(ctx, 'clear_message_sent'):
                    await ctx.send(msg, delete_after=7)
                    ctx.clear_message_sent = True
            except ValueError: 
                await ctx.send(f"Geçersiz sayı: '{amount}'. Lütfen pozitif bir tam sayı veya 'all' girin.", delete_after=10)
                try:
                    await ctx.message.delete(delay=10)
                except discord.errors.NotFound:
                    logger.debug("Komut mesajı zaten silinmiş.")
                except Exception as e:
                    logger.warning(f"Komut mesajı silinirken hata: {e}")
    except discord.errors.Forbidden: 
        logger.error(f"HATA: '{ctx.channel.name}' kanalında mesaj silme izni yok (Bot için)!")
        await ctx.send("Bu kanalda mesajları silme iznim yok.", delete_after=10)
        try:
            await ctx.message.delete(delay=10)
        except discord.errors.NotFound:
            logger.debug("Komut mesajı zaten silinmiş.")
        except Exception as e:
            logger.warning(f"Komut mesajı silinirken hata: {e}")
    except Exception as e: 
        logger.error(f".clear hatası: {e}\n{traceback.format_exc()}")
        await ctx.send("Mesajlar silinirken bir hata oluştu.", delete_after=10)
        try:
            await ctx.message.delete(delay=10)
        except discord.errors.NotFound:
            logger.debug("Komut mesajı zaten silinmiş.")
        except Exception as e:
            logger.warning(f"Komut mesajı silinirken hata: {e}")

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

    await status_msg.delete(); await ctx.send(embed=embed)
    # Kullanıcının orjinal mesajını korumak için silme kodu kaldırıldı

# setmodel komutu güncellendi
@bot.command(name='setmodel')
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_next_chat_model(ctx: commands.Context, *, model_id_with_or_without_prefix: str = None):
    """Bir sonraki özel sohbetiniz için kullanılacak Gemini veya DeepSeek (OpenRouter) modelini ayarlar."""
    global user_next_model
    if model_id_with_or_without_prefix is None:
        await ctx.send(f"Lütfen bir model adı belirtin (örn: `{GEMINI_PREFIX}gemini-1.5-flash-latest` veya `{DEEPSEEK_OPENROUTER_PREFIX}{OPENROUTER_DEEPSEEK_MODEL_NAME}`). Modeller için `{ctx.prefix}listmodels`.", delete_after=15)
        return

    model_input = model_id_with_or_without_prefix.strip().replace('`', '')
    selected_model_full_name = None
    is_valid = False
    error_message = None

    if not model_input.startswith(GEMINI_PREFIX) and not model_input.startswith(DEEPSEEK_OPENROUTER_PREFIX):
         await ctx.send(f"❌ Lütfen model adının başına `{GEMINI_PREFIX}` veya `{DEEPSEEK_OPENROUTER_PREFIX}` ön ekini ekleyin. `{ctx.prefix}listmodels` ile kontrol edin.", delete_after=20)
         return

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

    # Orjinal mesajı silme kodunu kaldırdık
    # Kullanıcının mesajını korumak için

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

# commandlist komutu aynı kalır, sadece DeepSeek açıklamasını güncelleyebiliriz.
# Eski commandlist komutu kaldırıldı (help komutu ile birleştirildi)

# === YENİ KOMUTLAR: BELİRLİ MODELLERE SORU ===

@bot.command(name='gemini', aliases=['g'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def gemini_direct(ctx: commands.Context, *, question: str = None):
    """
    (Sadece Sohbet Kanalında) Geçerli kanaldaki varsayılan modeli DEĞİŞTİRMEDEN,
    doğrudan varsayılan Gemini modeline soru sorar.
    """
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        # await ctx.send("Bu komut sadece özel sohbet kanallarında kullanılabilir.", delete_after=10)
        # Komut yanlış yerde kullanıldıysa mesajı silmek iyi olabilir yine de? Karar sizin.
        # try: await ctx.message.delete(delay=10)
        # except: pass
        return # Sadece geçici kanallarda çalışırsa sessizce çıkalım

    global gemini_default_model_instance
    if not GEMINI_API_KEY or not gemini_default_model_instance:
        await ctx.reply("⚠️ Gemini API anahtarı ayarlanmamış veya varsayılan model yüklenememiş.", delete_after=10)
        # try: await ctx.message.delete(delay=10) # Hata durumunda silinebilir
        # except: pass
        return

    if question is None or not question.strip():
        await ctx.reply(f"Lütfen komuttan sonra bir soru sorun (örn: `{ctx.prefix}gemini Türkiye'nin başkenti neresidir?`).", delete_after=15)
        # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
        # except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] doğrudan Gemini'ye sordu: {question[:100]}...")
    response_message: discord.Message = None

    try:
        async with ctx.typing():
            try:
                response = await asyncio.to_thread(gemini_default_model_instance.generate_content, question)
            except Exception as gemini_e:
                 logger.error(f".gemini komutu için API hatası: {gemini_e}")
                 await ctx.reply("Gemini API ile iletişim kurarken bir sorun oluştu.", delete_after=10)
                 # try: await ctx.message.delete(delay=10) # Hata durumunda silinebilir
                 # except: pass
                 return

            gemini_response_text = ""; finish_reason = None; prompt_feedback_reason = None
            try: gemini_response_text = response.text.strip()
            except: pass # Hata olsa bile devam et, aşağıda kontrol edilecek
            try: finish_reason = response.candidates[0].finish_reason.name
            except: pass
            try: prompt_feedback_reason = response.prompt_feedback.block_reason.name
            except: pass

            user_error_msg = None
            if prompt_feedback_reason == "SAFETY": user_error_msg = "Girdiğiniz mesaj güvenlik filtrelerine takıldı."
            elif finish_reason == "SAFETY": user_error_msg = "Yanıt güvenlik filtrelerine takıldı."; gemini_response_text = None
            elif finish_reason == "RECITATION": user_error_msg = "Yanıt alıntı filtrelerine takıldı."; gemini_response_text = None
            elif finish_reason == "OTHER": user_error_msg = "Yanıt oluşturulamadı (bilinmeyen sebep)."; gemini_response_text = None
            elif not gemini_response_text and finish_reason != "STOP": user_error_msg = f"Yanıt beklenmedik bir sebeple durdu ({finish_reason})."

            if user_error_msg:
                 await ctx.reply(f"⚠️ {user_error_msg}", delete_after=15)
                 # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
                 # except: pass
                 return

            if not gemini_response_text:
                logger.warning(f"Gemini'den .gemini için boş yanıt alındı.")
                await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
                # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
                # except: pass
                return

        # --- YANITI EMBED İLE GÖNDER (Model Adı Dahil) ---
        try:
            embed = discord.Embed(
                description=gemini_response_text[:4096], # Embed açıklaması daha uzun olabilir
                color=discord.Color.blue()
            )
            # Kullanılan varsayılan Gemini modelinin adını al (prefixsiz)
            used_model_name = DEFAULT_GEMINI_MODEL_NAME
            embed.set_author(
                name=f"Gemini Yanıtı ({used_model_name})",
                icon_url="https://i.imgur.com/tGd6A4F.png" # Örnek Gemini ikonu (değiştirilebilir)
            )
            # Eğer yanıt çok uzunsa footer'da belirt
            if len(gemini_response_text) > 4096:
                 embed.set_footer(text="Yanıtın tamamı gösterilemiyor (Discord limiti aşıldı).")

            response_message = await ctx.reply(embed=embed)
            logger.info(f".gemini komutuna yanıt gönderildi.")

        except discord.errors.HTTPException as e:
             # Embed çok büyükse veya başka bir sorun varsa düz metin gönder
             logger.warning(f".gemini yanıtı embed olarak gönderilemedi: {e}. Düz metin deneniyor.")
             try:
                  response_message = await ctx.reply(f"**Gemini Yanıtı ({DEFAULT_GEMINI_MODEL_NAME}):**\n{gemini_response_text[:1900]}") # Düz metin limiti daha düşük
             except Exception as send_e:
                  logger.error(f".gemini yanıtı düz metin olarak da gönderilemedi: {send_e}")

        # --- KOMUT MESAJINI SİLME KISMI KALDIRILDI ---
        # try:
        #     await ctx.message.delete()
        # except:
        #     pass
        # --- ------------------------------------- ---

    except Exception as e:
        logger.error(f".gemini komutunda genel hata: {e}\n{traceback.format_exc()}")
        await ctx.reply("Komutu işlerken beklenmedik bir hata oluştu.", delete_after=15)
        # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
        # except: pass

@bot.command(name='deepseek', aliases=['ds'])
@commands.guild_only()
@commands.cooldown(1, 5, commands.BucketType.user)
async def deepseek_direct(ctx: commands.Context, *, question: str = None):
    """
    (Sadece Sohbet Kanalında) Geçerli kanaldaki varsayılan modeli DEĞİŞTİRMEDEN,
    doğrudan yapılandırılmış DeepSeek (OpenRouter) modeline soru sorar.
    """
    channel_id = ctx.channel.id
    if channel_id not in temporary_chat_channels:
        # await ctx.send("Bu komut sadece özel sohbet kanallarında kullanılabilir.", delete_after=10)
        return # Sessizce çık

    if not OPENROUTER_API_KEY:
        await ctx.reply("⚠️ DeepSeek için OpenRouter API anahtarı ayarlanmamış.", delete_after=10)
        # try: await ctx.message.delete(delay=10)
        # except: pass
        return
    if not REQUESTS_AVAILABLE:
        await ctx.reply("⚠️ DeepSeek (OpenRouter) için 'requests' kütüphanesi bulunamadı.", delete_after=10)
        # try: await ctx.message.delete(delay=10)
        # except: pass
        return

    if question is None or not question.strip():
        await ctx.reply(f"Lütfen komuttan sonra bir soru sorun (örn: `{ctx.prefix}deepseek Python kod örneği yaz`).", delete_after=15)
        # try: await ctx.message.delete(delay=15)
        # except: pass
        return

    logger.info(f"[{ctx.author.name} @ {ctx.channel.name}] doğrudan DeepSeek'e sordu: {question[:100]}...")
    response_message: discord.Message = None
    ai_response_text = None
    error_occurred = False
    user_error_msg = "DeepSeek (OpenRouter) ile konuşurken bir sorun oluştu."

    try:
        async with ctx.typing():
            headers = { "Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json" }
            if OPENROUTER_SITE_URL: headers["HTTP-Referer"] = OPENROUTER_SITE_URL
            if OPENROUTER_SITE_NAME: headers["X-Title"] = OPENROUTER_SITE_NAME
            payload = { "model": OPENROUTER_DEEPSEEK_MODEL_NAME, "messages": [{"role": "user", "content": question}] }
            response_data = None
            try:
                api_response = await asyncio.to_thread(requests.post, OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)
                api_response.raise_for_status()
                response_data = api_response.json()
            except requests.exceptions.Timeout: logger.error("OpenRouter API isteği zaman aşımına uğradı."); error_occurred = True; user_error_msg = "Yapay zeka sunucusundan yanıt alınamadı (zaman aşımı)."
            except requests.exceptions.RequestException as e:
                logger.error(f"OpenRouter API isteği sırasında hata: {e}"); error_occurred = True
                if e.response is not None:
                     logger.error(f"OR Hata Kodu: {e.response.status_code}, İçerik: {e.response.text[:200]}")
                     if e.response.status_code == 401: user_error_msg = "OpenRouter API Anahtarı geçersiz."
                     elif e.response.status_code == 402: user_error_msg = "OpenRouter krediniz yetersiz."
                     elif e.response.status_code == 429: user_error_msg = "OpenRouter API limiti aşıldı."
                     elif 400 <= e.response.status_code < 500: user_error_msg = f"OpenRouter API Hatası ({e.response.status_code}): Geçersiz istek."
                     elif 500 <= e.response.status_code < 600: user_error_msg = f"OpenRouter API Sunucu Hatası ({e.response.status_code})."
                else: user_error_msg = "OpenRouter API'sine bağlanılamadı."
            except Exception as thread_e: logger.error(f"OpenRouter API isteği gönderilirken thread hatası: {thread_e}"); error_occurred = True; user_error_msg = "Yapay zeka isteği gönderilirken hata oluştu."

            if not error_occurred and response_data:
                try:
                    ai_response_text = response_data["choices"][0]["message"]["content"].strip()
                    finish_reason = response_data["choices"][0].get("finish_reason")
                    if finish_reason == 'length': logger.warning(f"OpenRouter/DeepSeek yanıtı max_tokens sınırına ulaştı (.ds)")
                    elif finish_reason == 'content_filter': user_error_msg = "Yanıt içerik filtrelerine takıldı."; error_occurred = True; logger.warning(f"OpenRouter/DeepSeek content filter block (.ds)"); ai_response_text = None
                    elif finish_reason != 'stop' and not ai_response_text: user_error_msg = f"Yanıt beklenmedik sebeple durdu ({finish_reason})."; error_occurred = True; logger.warning(f"OpenRouter/DeepSeek unexpected finish: {finish_reason} (.ds)"); ai_response_text = None
                except (KeyError, IndexError, TypeError) as parse_error: logger.error(f"OpenRouter yanıtı işlenirken hata (.ds): {parse_error}. Yanıt: {response_data}"); error_occurred = True; user_error_msg = "Yapay zeka yanıtı işlenirken sorun oluştu."
            elif not error_occurred: error_occurred=True; user_error_msg="Yapay zekadan geçerli yanıt alınamadı."

        if error_occurred:
            await ctx.reply(f"⚠️ {user_error_msg}", delete_after=15)
            # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
            # except: pass
            return

        if not ai_response_text:
            logger.warning(f"DeepSeek'ten .ds için boş yanıt alındı.")
            await ctx.reply("Üzgünüm, bu soruya bir yanıt alamadım.", delete_after=15)
            # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
            # except: pass
            return

        # --- YANITI EMBED İLE GÖNDER (Model Adı Dahil) ---
        try:
            embed = discord.Embed(
                description=ai_response_text[:4096], # Açıklama limiti daha yüksek
                color=discord.Color.dark_green() # Farklı bir renk
            )
            # Kullanılan DeepSeek model adını al (sabit)
            used_model_name = OPENROUTER_DEEPSEEK_MODEL_NAME
            embed.set_author(
                name=f"DeepSeek Yanıtı ({used_model_name})",
                icon_url="https://avatars.githubusercontent.com/u/14733136?s=280&v=4" # Örnek OpenRouter ikonu (değiştirilebilir)
            )
            if len(ai_response_text) > 4096:
                embed.set_footer(text="Yanıtın tamamı gösterilemiyor (Discord limiti aşıldı).")

            response_message = await ctx.reply(embed=embed)
            logger.info(f".ds komutuna yanıt gönderildi.")

        except discord.errors.HTTPException as e:
             logger.warning(f".ds yanıtı embed olarak gönderilemedi: {e}. Düz metin deneniyor.")
             try:
                  response_message = await ctx.reply(f"**DeepSeek Yanıtı ({OPENROUTER_DEEPSEEK_MODEL_NAME}):**\n{ai_response_text[:1900]}")
             except Exception as send_e:
                  logger.error(f".ds yanıtı düz metin olarak da gönderilemedi: {send_e}")
        # --- ------------------------------------- ---


        # --- KOMUT MESAJINI SİLME KISMI KALDIRILDI ---
        # try:
        #     await ctx.message.delete()
        # except:
        #     pass
        # --- ------------------------------------- ---

    except Exception as e:
        logger.error(f".ds komutunda genel hata: {e}\n{traceback.format_exc()}")
        await ctx.reply("Komutu işlerken beklenmedik bir hata oluştu.", delete_after=15)
        # try: await ctx.message.delete(delay=15) # Hata durumunda silinebilir
        # except: pass

# --- Duplicate Mesaj Sorunu ---
# Bu sorunu çözmek için her komut için özel çözüm uygulayacağız

# --- Özel Yardım Komutu ---
# Tüm yardım komutlarını kaldır
bot.remove_command('help')
bot.remove_command('commandlist')

# Sadece tek bir yardım komutu oluştur
@bot.command(name='help', aliases=['yardım', 'komutlar'])
async def custom_help(ctx: commands.Context):
    # Kanal türünü belirle
    channel_id = ctx.channel.id
    is_music_channel = (channel_id == MUSIC_CHANNEL_ID)
    is_ai_channel = (channel_id in temporary_chat_channels or channel_id == entry_channel_id)
    
    embed = discord.Embed(
        title="📜 Kullanılabilir Komutlar",
        color=discord.Color.blue()
    )
    
    # Müzik kanalında ise müzik komutlarını göster
    if is_music_channel:
        embed.description = "Müzik kanalında kullanılabilir komutlar:"
        embed.add_field(
            name="🎧 Müzik Komutları",
            value=(
                f"`{ctx.prefix}play <URL/arama>` - Bir şarkı çal veya kuyruğa ekle\n"
                f"`{ctx.prefix}skip` - Şu an çalan şarkıyı atla\n"
                f"`{ctx.prefix}pause` - Müziği duraklat\n"
                f"`{ctx.prefix}resume` - Müziği devam ettir\n"
                f"`{ctx.prefix}stop` - Müziği durdur ve kuyruğu temizle\n"
                f"`{ctx.prefix}queue` - Şarkı kuyruğunu göster\n"
                f"`{ctx.prefix}nowplaying` - Şu an çalan şarkıyı göster\n"
                f"`{ctx.prefix}volume` veya `{ctx.prefix}vol` - Ses seviyesini göster/ayarla\n"
                f"`{ctx.prefix}setdefaultvolume` veya `{ctx.prefix}ses` - Varsayılan ses seviyesini ayarla\n"
                f"`{ctx.prefix}leave` - Ses kanalından ayrıl"
            ),
            inline=False
        )
    
    # AI kanalında ise AI komutlarını göster
    elif is_ai_channel:
        embed.description = "Yapay zeka kanalında kullanılabilir komutlar:"
        
        # Geçici kanallarda kullanılabilir komutlar
        if channel_id in temporary_chat_channels:
            embed.add_field(
                name="🤖 Sohbet Komutları",
                value=(
                    f"`{ctx.prefix}resetchat` - Sohbet geçmişini sıfırla\n"
                    f"`{ctx.prefix}endchat` - Sohbeti sonlandır ve kanalı sil\n"
                    f"`{ctx.prefix}deepseek` veya `{ctx.prefix}ds` - DeepSeek modeline doğrudan soru sor\n"
                    f"`{ctx.prefix}gemini` - Gemini modeline doğrudan soru sor"
                ),
                inline=False
            )
        
        # Giriş kanalında kullanılabilir komutlar
        if channel_id == entry_channel_id:
            embed.add_field(
                name="🌐 Model Komutları",
                value=(
                    f"`{ctx.prefix}listmodels` - Kullanılabilir modelleri listele\n"
                    f"`{ctx.prefix}setmodel <model>` - Bir sonraki sohbet için model seç\n"
                ),
                inline=False
            )
        
        # Genel AI komutları
        embed.add_field(
            name="💬 Genel Komutlar",
            value=(
                f"`{ctx.prefix}ask <soru>` - Gemini modeline doğrudan soru sor\n"
                f"`{ctx.prefix}clear <sayı>` - Belirtilen sayıda mesajı sil\n"
            ),
            inline=False
        )
    
    # Diğer kanallarda genel komutları göster
    else:
        embed.description = "Genel komutlar:"
        embed.add_field(
            name="💬 Genel Komutlar",
            value=(
                f"`{ctx.prefix}help` - Bu yardım mesajını göster\n"
                f"`{ctx.prefix}ask <soru>` - Gemini modeline doğrudan soru sor\n"
                f"`{ctx.prefix}clear <sayı>` - Belirtilen sayıda mesajı sil\n"
            ),
            inline=False
        )
        
        # Yönetici komutlarını göster (sadece yöneticiler için)
        if ctx.author.guild_permissions.administrator:
            embed.add_field(
                name="🛡️ Yönetici Komutları",
                value=(
                    f"`{ctx.prefix}setentrychannel <kanal>` - Giriş kanalını ayarla\n"
                    f"`{ctx.prefix}settimeout <saat>` - Geçici kanal zaman aşımını ayarla\n"
                ),
                inline=False
            )
    
    # Bot bilgilerini footer olarak ekle
    embed.set_footer(text=f"B0T v1.5 | Prefix: {ctx.prefix} | Kanal: {ctx.channel.name}")
    
    # Mesajı sadece bir kez gönder
    if not hasattr(ctx, 'help_message_sent'):
        await ctx.send(embed=embed)
        ctx.help_message_sent = True

# --- Genel Hata Yakalama ---
# on_command_error fonksiyonu aynı kalır
@bot.event
async def on_command_error(ctx, error):
    # Komut bulunamadı hatalarını yoksay
    if isinstance(error, commands.CommandNotFound): 
        return
        
    # Cooldown hatalarını işle
    if isinstance(error, commands.CommandOnCooldown):
        # ask komutu için cooldown mesajlarını gösterme
        if ctx.command and ctx.command.name == 'ask': 
            return
            
        # Cooldown süresi kadar bekle
        delete_delay = max(5, int(error.retry_after) + 1)
        
        # Kullanıcıya cooldown hakkında bilgi ver
        try:
            await ctx.send(f"⌛ `{ctx.command.qualified_name}` ... **{error.retry_after:.1f} saniye** ...", delete_after=delete_delay)
        except discord.errors.Forbidden:
            logger.warning(f"Cooldown mesajı gönderilirken izin hatası (Kanal: {ctx.channel.id})")
        except Exception as e:
            logger.warning(f"Cooldown mesajı gönderilirken hata: {e}")
        
        # Belirli komutlar için mesaj silme işlemini engelle
        if ctx.command and ctx.command.name not in ['deepseek', 'ds']:
            try:
                await ctx.message.delete(delay=delete_delay)
            except discord.errors.NotFound:
                pass  # Mesaj zaten silinmiş
            except discord.errors.Forbidden:
                logger.warning(f"Mesaj silinirken izin hatası (Kanal: {ctx.channel.id})")
            except Exception as e:
                logger.warning(f"Mesaj silinirken hata: {e}")
        return
    if isinstance(error, commands.UserInputError):
        delete_delay = 15
        command_name = ctx.command.qualified_name if ctx.command else ctx.invoked_with
        usage = f"`{ctx.prefix}{command_name} {ctx.command.signature if ctx.command else ''}`".replace('=None', '').replace('= Ellipsis', '...')
        error_message = "Hatalı komut kullanımı."
        
        # Spesifik kullanıcı giriş hatalarını işle
        if isinstance(error, commands.MissingRequiredArgument):
            error_message = f"Eksik argüman: `{error.param.name}`."
        elif isinstance(error, commands.BadArgument):
            error_message = f"Geçersiz argüman türü: {error}"
        elif isinstance(error, commands.TooManyArguments):
            error_message = "Çok fazla argüman girdiniz."
            
        # Hata mesajını gönder
        try:
            await ctx.send(f"⚠️ {error_message}\nDoğru kullanım: {usage}", delete_after=delete_delay)
        except discord.errors.Forbidden:
            logger.warning(f"Hata mesajı gönderilirken izin hatası (Kanal: {ctx.channel.id})")
        except Exception as e:
            logger.warning(f"Hata mesajı gönderilirken hata: {e}")
        
        # Belirli komutlar için mesaj silme işlemini engelle
        if ctx.command and ctx.command.name not in ['deepseek', 'ds']:
            try:
                await ctx.message.delete(delay=delete_delay)
            except discord.errors.NotFound:
                pass  # Mesaj zaten silinmiş
            except discord.errors.Forbidden:
                logger.warning(f"Mesaj silinirken izin hatası (Kanal: {ctx.channel.id})")
            except Exception as e:
                logger.warning(f"Mesaj silinirken hata: {e}")
        return
    # Diğer hata türleri için varsayılan ayarlar
    delete_user_msg = True
    delete_delay = 10
    original_error = getattr(error, 'original', error)
    
    # İzin hatalarını işle
    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        logger.warning(f"{ctx.author.name}, '{ctx.command.qualified_name}' izni yok: {original_error.missing_permissions}")
        delete_delay = 15
        try:
            await ctx.send(f"⛔ Üzügünüm {ctx.author.mention}, bu komutu kullanmak için gerekli izinlere sahip değilsiniz: **{perms}**", delete_after=delete_delay)
        except Exception as e:
            logger.warning(f"Hata mesajı gönderilirken hata: {e}")
    
    # Bot izin hatalarını işle
    elif isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in original_error.missing_permissions)
        logger.error(f"Botun '{ctx.command.qualified_name}' izni eksik: {original_error.missing_permissions}")
        delete_delay = 15
        delete_user_msg = False  # Kullanıcı mesajını silme
        try:
            await ctx.send(f"🆘 Bu komutu gerçekleştirmek için gerekli izinlere sahip değilim: **{perms}**", delete_after=delete_delay)
        except Exception as e:
            logger.warning(f"Hata mesajı gönderilirken hata: {e}")
    
    # Kontrol hatalarını işle (yetkisiz komut kullanımı, yanlış kanal, vb.)
    elif isinstance(error, commands.CheckFailure):
        logger.warning(f"Komut kontrolü başarısız: {ctx.command.qualified_name} - Kullanıcı: {ctx.author.name} - Hata: {error}")
        user_msg = "🚫 Bu komutu burada veya bu şekilde kullanamazsınız."
        try:
            await ctx.send(user_msg, delete_after=delete_delay)
        except Exception as e:
            logger.warning(f"Hata mesajı gönderilirken hata: {e}")
    
    # Diğer tüm beklenmedik hataları işle
    else:
        logger.error(f"'{ctx.command.qualified_name if ctx.command else ctx.invoked_with}' işlenirken beklenmedik hata: {type(original_error).__name__}: {original_error}")
        traceback_str = "".join(traceback.format_exception(type(original_error), original_error, original_error.__traceback__))
        logger.error(f"Traceback:\n{traceback_str}")
        delete_delay = 15
        try:
            await ctx.send("⚙️ Komut işlenirken beklenmedik bir hata oluştu. Lütfen daha sonra tekrar deneyin.", delete_after=delete_delay)
        except Exception as e:
            logger.warning(f"Hata mesajı gönderilirken hata: {e}")
    
    # Belirli komutlar için mesaj silme işlemini engelle
    if delete_user_msg and ctx.guild and ctx.command and ctx.command.name not in ['deepseek', 'ds']: 
        try: 
            await ctx.message.delete(delay=delete_delay)
        except discord.errors.NotFound:
            pass  # Mesaj zaten silinmiş
        except discord.errors.Forbidden:
            logger.warning(f"Mesaj silinirken izin hatası (Kanal: {ctx.channel.id})")
        except Exception as e: 
            logger.warning(f"Mesaj silinirken hata: {e}")

@bot.command(name='play', aliases=['p', 'çal'])
async def play_music(ctx: commands.Context, *, query: str = None):
    """YouTube'dan müzik çal"""
    # Müzik kanalı kontrolü
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
        
    # Sorgu boşsa ve mesaj ekli ise, eki kullan
    if query is None:
        if ctx.message.attachments and ctx.message.attachments[0].filename.endswith(('.mp3', '.wav', '.ogg', '.m4a')):
            query = ctx.message.attachments[0].url
        else:
            await ctx.send("❌ Lütfen bir şarkı adı veya URL belirtin.")
            return
    
    # Ses kanalına katıl
    if not ctx.author.voice:
        await ctx.send("❌ Önce bir ses kanalına katılmalısın.")
        return
    
    voice_channel = ctx.author.voice.channel
    
    # Ses kanalına bağlan veya taşın
    if ctx.voice_client is None:
        voice_client = await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)
        voice_client = ctx.voice_client
    else:
        voice_client = ctx.voice_client
    
    # MusicPlayer'a voice client'i kaydet
    if hasattr(music_player, 'voice_clients'):
        music_player.voice_clients[ctx.guild.id] = voice_client
    
    # Yükleniyor mesajı
    loading_msg = await ctx.send(f"⌛ **{query}** aranıyor...")
    
    try:
        # yt-dlp ile video bilgilerini al
        with yt_dlp.YoutubeDL(music_player.ytdl_format_options) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            
            # Sonuç bir oynatma listesiyse veya birden fazla video varsa ilkini al
            if '_type' in info and info['_type'] == 'playlist':
                entries = info.get('entries', [])
                if not entries:
                    await loading_msg.edit(content=f"❌ Oynatma listesinde video bulunamadı.")
                    return
                info = entries[0]
            elif 'entries' in info:
                entries = info.get('entries', [])
                if not entries:
                    await loading_msg.edit(content=f"❌ Sonuçlarda video bulunamadı.")
                    return
                info = entries[0]
                
            # Video bilgileri
            title = info.get('title', 'Bilinmeyen Başlık')
            url = info.get('url', None)
            duration_sec = info.get('duration', 0)
            duration = str(datetime.timedelta(seconds=duration_sec)) if duration_sec else 'Bilinmeyen Süre'
            thumbnail = info.get('thumbnail', None)
            
            # Kuyruk oluştur (yoksa)
            if not hasattr(music_player, 'queues'):
                music_player.queues = {}
                
            if ctx.guild.id not in music_player.queues:
                music_player.queues[ctx.guild.id] = deque()
            
            # Kuyruğa ekle
            song_info = {
                'title': title, 
                'url': url, 
                'duration': duration,
                'thumbnail': thumbnail,
                'requester': ctx.author.name,
                'start_time': datetime.datetime.now()
            }
            music_player.queues[ctx.guild.id].append(song_info)
            
            # Çalma durumunu kontrol et
            is_playing = False
            if ctx.voice_client and ctx.voice_client.is_playing():
                is_playing = True
            
            # Now playing sözlüğünü oluştur (yoksa)
            if not hasattr(music_player, 'now_playing'):
                music_player.now_playing = {}
            
            if not is_playing:
                # Kuyruktaki ilk şarkıyı çal
                if music_player.queues[ctx.guild.id]:
                    next_song = music_player.queues[ctx.guild.id].popleft()
                    music_player.now_playing[ctx.guild.id] = next_song
                    
                    # Ses kaynağını oluştur
                    ffmpeg_options = {
                        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                        'options': '-vn'
                    }
                    
                    audio_source = discord.FFmpegPCMAudio(next_song['url'], **ffmpeg_options)
                    
                    # Ses seviyesini ayarla
                    volume = 0.5  # Varsayılan ses seviyesi
                    if hasattr(music_player, 'volume'):
                        volume = music_player.volume
                    
                    volume_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
                    
                    # Şarkı bitince bir sonrakine geç fonksiyonu
                    def after_playing(error):
                        if error:
                            logger.error(f"Müzik çalınırken hata: {error}")
                        
                        # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
                        if hasattr(music_player, 'play_next'):
                            coro = music_player.play_next(ctx.guild.id, None)
                            future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                            try:
                                future.result()
                            except Exception as e:
                                logger.error(f"Bir sonraki şarkıya geçerken hata: {e}")
                    
                    # Şarkıyı çal
                    ctx.voice_client.play(volume_source, after=after_playing)
                    
                    # Şarkı başladı mesajı
                    await loading_msg.delete()
                    embed = discord.Embed(
                        title="▶️ Şimdi Çalınıyor",
                        description=f"**{next_song['title']}**",
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="Süre", value=next_song['duration'], inline=True)
                    embed.add_field(name="Ekleyen", value=next_song.get('requester', 'Bilinmiyor'), inline=True)
                    
                    if next_song.get('thumbnail'):
                        embed.set_thumbnail(url=next_song['thumbnail'])
                    await ctx.send(embed=embed)
            else:
                # Daha güzel bir embed mesajı ile kuyruğa eklendi bilgisi
                await loading_msg.delete()
                embed = discord.Embed(
                    title="✅ Şarkı Kuyruğa Eklendi",
                    description=f"**{title}**\nSüre: {duration}",
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"Ekleyen: {ctx.author.name}")
                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)
                await ctx.send(embed=embed)
                
    except Exception as e:
        logger.error(f"Müzik yüklenirken hata: {e}")
        await loading_msg.edit(content=f"❌ Video yüklenirken bir hata oluştu: {str(e)[:1000]}")
        traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        logger.error(f"Müzik yüklenirken hata detayı:\n{traceback_str}")

@bot.command(name='skip', aliases=['s', 'geç'])
async def skip_song(ctx: commands.Context):
    """Mevcut şarkıyı geç"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
    
    # Çalma durumu kontrolü
    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        # Şimdi çalan şarkı bilgisini al
        current_song = None
        try:
            if hasattr(music_player, 'now_playing') and ctx.guild.id in music_player.now_playing:
                current_song = music_player.now_playing[ctx.guild.id]
        except Exception as e:
            logger.error(f"Now playing bilgisi alınırken hata: {e}")
        
        song_title = current_song['title'] if current_song and isinstance(current_song, dict) and 'title' in current_song else "Bilinmeyen şarkı"
        
        # Şarkıyı durdur (bir sonrakine geçecek)
        ctx.voice_client.stop()
        
        # Embed ile bilgi ver
        embed = discord.Embed(
            title="⏭️ Şarkı Geçildi",
            description=f"**{song_title}** geçildi.",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Geçen: {ctx.author.name}")
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Şu anda çalan bir şarkı yok")

@bot.command(name='queue', aliases=['q', 'kuyruk', 'list', 'liste'])
async def show_queue(ctx: commands.Context):
    """Müzik kuyruğunu göster"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Embed ile kuyruk bilgisini göster
    embed = discord.Embed(
        title="🎶 Müzik Kuyruğu",
        color=discord.Color.purple()
    )
    
    # Şimdi çalan şarkı
    if ctx.guild.id in music_player.now_playing and music_player.now_playing[ctx.guild.id]:
        current = music_player.now_playing[ctx.guild.id]
        embed.add_field(
            name="▶️ Şimdi Çalınıyor",
            value=f"**{current['title']}**\nSüre: {current['duration']}\nEkleyen: {current.get('requester', 'Bilinmiyor')}",
            inline=False
        )
        
        # Küçük resim ekle
        if current.get('thumbnail'):
            embed.set_thumbnail(url=current['thumbnail'])
    else:
        embed.add_field(
            name="▶️ Şimdi Çalınıyor",
            value="*Hiçbir şey çalmıyor*",
            inline=False
        )
    
    # Kuyruktaki şarkılar
    if ctx.guild.id in music_player.queues and music_player.queues[ctx.guild.id]:
        queue_text = ""
        for i, song in enumerate(music_player.queues[ctx.guild.id], 1):
            queue_text += f"`{i}.` **{song['title']}** ({song['duration']}) - {song.get('requester', 'Bilinmiyor')}\n"
            if i >= 10:  # En fazla 10 şarkı göster
                remaining = len(music_player.queues[ctx.guild.id]) - 10
                if remaining > 0:
                    queue_text += f"\n*...ve {remaining} şarkı daha*"
                break
        
        embed.add_field(
            name="📜 Sıradaki Şarkılar",
            value=queue_text or "*Kuyrukta başka şarkı yok*",
            inline=False
        )
    else:
        embed.add_field(
            name="📜 Sıradaki Şarkılar",
            value="*Kuyrukta şarkı yok*",
            inline=False
        )
    
    # Toplam şarkı sayısı ve tahmini çalma süresi
    total_songs = len(music_player.queues.get(ctx.guild.id, []))
    embed.set_footer(text=f"Toplam {total_songs} şarkı kuyrukta | Ses seviyesi: %{int(music_player.volume * 100)}")
    
    await ctx.send(embed=embed)

@bot.command(name='pause', aliases=['duraklat'])
async def pause_music(ctx: commands.Context):
    """Müziği duraklat"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
    
    # Çalma durumu kontrolü
    if ctx.voice_client.is_playing():
        try:
            # Şimdi çalan şarkı bilgisini al
            current_song = None
            try:
                if hasattr(music_player, 'now_playing') and ctx.guild.id in music_player.now_playing:
                    current_song = music_player.now_playing[ctx.guild.id]
            except Exception as e:
                logger.error(f"Now playing bilgisi alınırken hata: {e}")
            
            song_title = "Bilinmeyen şarkı"
            if current_song and isinstance(current_song, dict) and 'title' in current_song:
                song_title = current_song['title']
            
            # Şarkıyı duraklat
            ctx.voice_client.pause()
            
            # Embed ile bilgi ver
            embed = discord.Embed(
                title="⏸️ Müzik Duraklatıldı",
                description=f"**{song_title}** duraklatıldı.",
                color=discord.Color.gold()
            )
            embed.set_footer(text=f"Durduran: {ctx.author.name}")
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Müzik duraklatılırken hata: {e}")
            traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"Müzik duraklatılırken hata detayı:\n{traceback_str}")
            await ctx.send(f"❌ Müzik duraklatılırken bir hata oluştu: {str(e)[:1000]}")
    elif ctx.voice_client.is_paused():
        await ctx.send("❌ Müzik zaten duraklatılmış durumda.")
    else:
        await ctx.send("❌ Şu anda çalan bir şarkı yok.")
@bot.command(name='resume', aliases=['devam'])
async def resume_music(ctx: commands.Context):
    """Duraklatılmış müziği devam ettir"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
    
    # Duraklatılmış durumu kontrolü
    if ctx.voice_client.is_paused():
        try:
            # Şimdi çalan şarkı bilgisini al
            current_song = None
            try:
                if hasattr(music_player, 'now_playing') and ctx.guild.id in music_player.now_playing:
                    current_song = music_player.now_playing[ctx.guild.id]
            except Exception as e:
                logger.error(f"Now playing bilgisi alınırken hata: {e}")
            
            song_title = "Bilinmeyen şarkı"
            if current_song and isinstance(current_song, dict) and 'title' in current_song:
                song_title = current_song['title']
            
            # Şarkıyı devam ettir
            ctx.voice_client.resume()
            
            # Embed ile bilgi ver
            embed = discord.Embed(
                title="▶️ Müzik Devam Ediyor",
                description=f"**{song_title}** çalmaya devam ediyor.",
                color=discord.Color.green()
            )
            embed.set_footer(text=f"Devam ettiren: {ctx.author.name}")
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Müzik devam ettirilirken hata: {e}")
            traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"Müzik devam ettirilirken hata detayı:\n{traceback_str}")
            await ctx.send(f"❌ Müzik devam ettirilirken bir hata oluştu: {str(e)[:1000]}")
    elif ctx.voice_client.is_playing():
        await ctx.send("❌ Müzik zaten çalıyor.")
    else:
        await ctx.send("❌ Şu anda duraklatılmış bir müzik yok.")

@bot.command(name='stop', aliases=['dur'])
async def stop_music(ctx: commands.Context):
    """Müziği durdur ve kuyruğu temizle"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
    
    # Çalma durumu kontrolü
    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        # Şimdi çalan şarkı bilgisini al (temizlemeden önce)
        current_song = None
        try:
            if hasattr(music_player, 'now_playing') and ctx.guild.id in music_player.now_playing:
                current_song = music_player.now_playing[ctx.guild.id]
        except Exception as e:
            logger.error(f"Now playing bilgisi alınırken hata: {e}")
        
        song_title = "Bilinmeyen şarkı"
        if current_song and isinstance(current_song, dict) and 'title' in current_song:
            song_title = current_song['title']
        
        try:
            # Şarkıyı durdur
            ctx.voice_client.stop()
            
            # Kuyruğu temizle
            if hasattr(music_player, 'queues') and ctx.guild.id in music_player.queues:
                music_player.queues[ctx.guild.id].clear()
            
            # Şimdi çalan bilgisini temizle
            if hasattr(music_player, 'now_playing') and ctx.guild.id in music_player.now_playing:
                music_player.now_playing[ctx.guild.id] = None
            
            # Ses kanalından ayrıl
            await ctx.voice_client.disconnect()
            
            # Voice client referansını temizle
            if hasattr(music_player, 'voice_clients') and ctx.guild.id in music_player.voice_clients:
                del music_player.voice_clients[ctx.guild.id]
            
            # Embed ile bilgi ver
            embed = discord.Embed(
                title="⏹️ Müzik Durduruldu",
                description=f"**{song_title}** ve tüm kuyruk durduruldu.",
                color=discord.Color.red()
            )
            embed.set_footer(text=f"Durduran: {ctx.author.name}")
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Müzik durdurulurken hata: {e}")
            traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            logger.error(f"Müzik durdurulurken hata detayı:\n{traceback_str}")
            await ctx.send(f"❌ Müzik durdurulurken bir hata oluştu: {str(e)[:1000]}")
    else:
        await ctx.send("❌ Şu anda çalan bir şarkı yok.")

@bot.command(name='volume', aliases=['vol', 'ses'])
async def set_volume(ctx: commands.Context, volume: int = None):
    """Ses seviyesini ayarla (0-100)"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Ses seviyesi ayarlarını yap
    if volume is None:
        # Mevcut ses seviyesini göster
        current_volume = int(music_player.volume * 100)
        
        # Embed ile bilgi ver
        embed = discord.Embed(
            title="🔊 Ses Seviyesi Bilgisi",
            description=f"Mevcut ses seviyesi: **%{current_volume}**",
            color=discord.Color.blue()
        )
        
        # Ses seviyesi görseli
        volume_bar = "▬" * int(current_volume / 5)  # Her 5 birim için bir blok
        empty_bar = "▫" * (20 - int(current_volume / 5))  # Kalan boş bloklar
        embed.add_field(name="Ses Seviyesi", value=f"`{volume_bar}{empty_bar}` %{current_volume}", inline=False)
        
        await ctx.send(embed=embed)
        return
    # Ses seviyesi sınırlarını kontrol et
    if volume < 0 or volume > 100:
        await ctx.send("❌ Ses seviyesi 0 ile 100 arasında olmalıdır.")
        return
        
    # Önceki ses seviyesini kaydet
    old_volume = int(music_player.volume * 100)
    
    # Ses seviyesini ayarla
    music_player.volume = volume / 100.0
    
    # Eğer çalan bir şarkı varsa, anlık olarak ses seviyesini değiştir
    if ctx.guild.id in music_player.voice_clients and music_player.voice_clients[ctx.guild.id].is_playing():
        if hasattr(music_player.voice_clients[ctx.guild.id].source, 'volume'):
            music_player.voice_clients[ctx.guild.id].source.volume = music_player.volume
    
    # Ses seviyesi ayarlarını veritabanına kaydet
    try:
        save_volume_settings(music_player.volume, music_player.default_volume)
        
        # Embed ile bilgi ver
        embed = discord.Embed(
            title="🔊 Ses Seviyesi Değiştirildi",
            description=f"Ses seviyesi **%{old_volume}** → **%{volume}** olarak değiştirildi.",
            color=discord.Color.green()
        )
        
        # Ses seviyesi görseli
        volume_bar = "▬" * int(volume / 5)  # Her 5 birim için bir blok
        empty_bar = "▫" * (20 - int(volume / 5))  # Kalan boş bloklar
        embed.add_field(name="Yeni Ses Seviyesi", value=f"`{volume_bar}{empty_bar}` %{volume}", inline=False)
        
        embed.set_footer(text=f"Ayarlayan: {ctx.author.name}")
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Ses seviyesi kaydedilirken hata: {e}")
        await ctx.send(f"✅ Ses seviyesi **%{volume}** olarak ayarlandı, ancak kalıcı olarak kaydedilemedi.")

@bot.command(name='rewind', aliases=['gerisar', 'rw'])
async def rewind(ctx: commands.Context, seconds: int = 10, _timestamp: str = None):
    """Mevcut çalan şarkıyı belirli bir süre geri sar"""
    # Kanal kontrolü
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return

    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Saniye sınırlarını kontrol et
    if seconds <= 0:
        await ctx.send("❌ Geri sarma süresi pozitif bir sayı olmalıdır.")
        return
    
    # Guild ID'yi al
    guild_id = ctx.guild.id
        
    # Şarkı çalma durumu kontrolü
    if not ctx.voice_client.is_playing():
        await ctx.send("❌ Şu anda çalan bir şarkı yok.")
        return
    
    # Aktif çalan şarkının URL'sini bulmak için
    # 1. Mevcut çalan ses kaynağını kontrol et
    current_audio_source = None
    current_url = None
    song_backup = None
    
    try:
        # Discord.py'nin ses kaynağından URL'yi almaya çalış
        if hasattr(ctx.voice_client, 'source') and ctx.voice_client.source:
            if isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer):
                if hasattr(ctx.voice_client.source, 'original'):
                    current_audio_source = ctx.voice_client.source.original
                    
                    # FFmpegPCMAudio'dan URL'yi çıkar
                    if isinstance(current_audio_source, discord.FFmpegPCMAudio):
                        if hasattr(current_audio_source, 'filename'):
                            current_url = current_audio_source.filename
    except Exception as e:
        logger.error(f"Ses kaynağından URL alınırken hata: {e}")
    
    # 2. Eğer ses kaynağından alamadıysak, now_playing'den almayı dene
    if not current_url:
        try:
            if hasattr(music_player, 'now_playing') and guild_id in music_player.now_playing and music_player.now_playing[guild_id]:
                current_song = music_player.now_playing[guild_id]
                if isinstance(current_song, dict) and 'url' in current_song:
                    current_url = current_song['url']
                    
                    # Şarkı bilgilerini yedekle
                    song_backup = current_song.copy()
        except Exception as e:
            logger.error(f"Now playing bilgisi alınırken hata: {e}")
    
    # 3. Eğer hala URL bulamadıysak, kuyruktaki ilk şarkıyı kontrol et
    if not current_url:
        try:
            if hasattr(music_player, 'queues') and guild_id in music_player.queues and music_player.queues[guild_id]:
                current_song = music_player.queues[guild_id][0]
                if isinstance(current_song, dict) and 'url' in current_song:
                    current_url = current_song['url']
                    
                    # Şarkı bilgilerini yedekle
                    song_backup = current_song.copy()
        except Exception as e:
            logger.error(f"Kuyruk bilgisi alınırken hata: {e}")
    
    # Hala URL bulamadıysak hata mesajı göster
    if not current_url:
        await ctx.send("❌ Şarkı bilgisine erişilemedi. Lütfen şarkıyı durdurup yeniden başlatın.")
        return
    
    # Mevcut pozisyonu hesapla
    current_position = 0
    try:
        if song_backup and 'start_time' in song_backup:
            current_position = int((datetime.datetime.now() - song_backup['start_time']).total_seconds())
    except Exception as e:
        logger.error(f"Mevcut pozisyon hesaplanırken hata: {e}")
        # Hata durumunda pozisyonu sıfırla, işleme devam et
        current_position = 0
    
    # Yeni pozisyon = mevcut pozisyon - geri sarma süresi (en az 0)
    new_position = max(0, current_position - seconds)
    
    # Seek işlemi
    try:
        # Şarkıyı durdur
        ctx.voice_client.stop()
        
        # Yeni ses kaynağı oluştur ve belirtilen konumdan başlat
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': f'-vn -ss {new_position}'
        }
        
        # Ses kaynağını oluştur
        audio_source = discord.FFmpegPCMAudio(current_url, **ffmpeg_options)
        volume = 0.5  # Varsayılan ses seviyesi
        
        # Ses seviyesini ayarla
        if hasattr(music_player, 'volume'):
            volume = music_player.volume
        
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
        
        # Şarkı bilgilerini güncelle
        if song_backup:
            song_backup['start_time'] = datetime.datetime.now()
            
            # Şarkı bilgilerini now_playing'e kaydet
            if hasattr(music_player, 'now_playing'):
                music_player.now_playing[guild_id] = song_backup
        
        # Şarkı bitince bir sonrakine geç fonksiyonu
        def after_playing(error):
            if error:
                logger.error(f"Müzik çalınırken hata: {error}")
            
            # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
            try:
                if hasattr(music_player, 'play_next'):
                    coro = music_player.play_next(guild_id, None)
                    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                    future.result()
            except Exception as e:
                logger.error(f"Bir sonraki şarkıya geçerken hata: {e}")
        
        # Şarkıyı çal
        ctx.voice_client.play(volume_source, after=after_playing)
        
        # Bilgi mesajı
        minutes, seconds = divmod(new_position, 60)
        position_str = f"{minutes:02d}:{seconds:02d}"
        
        # Şarkı başlığını al
        song_title = "Bilinmeyen şarkı"
        if song_backup and 'title' in song_backup:
            song_title = song_backup['title']
            
        await ctx.send(f"⏪ **{song_title}** şarkısı {position_str} konumuna geri sarıldı.")
        
    except Exception as e:
        logger.error(f"Geri sarma işlemi sırasında hata: {e}")
        await ctx.send(f"❌ Şarkı geri sarılırken bir hata oluştu: {str(e)[:1000]}")
        
    # Komut izleme için temizleme işlemi
    try:
        command_id = f"{ctx.channel.id}-{ctx.message.id}"
        if command_id in processed_commands:
            del processed_commands[command_id]
            logger.debug(f"Rewind komutu için izleme temizlendi: {command_id}")
    except Exception as e:
        logger.error(f"Rewind komutu için izleme temizlenirken hata: {e}")



@bot.command(name='forward', aliases=['ilerisar', 'ff'])
async def forward(ctx: commands.Context, seconds: int = 10, _timestamp: str = None):
    """Mevcut çalan şarkıyı belirli bir süre ileri sar"""
    # Kanal kontrolü
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
        
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Saniye sınırlarını kontrol et
    if seconds <= 0:
        await ctx.send("❌ İleri sarma süresi pozitif bir sayı olmalıdır.")
        return
    
    # Guild ID'yi al
    guild_id = ctx.guild.id
        
    # Şarkı çalma durumu kontrolü
    if not ctx.voice_client.is_playing():
        await ctx.send("❌ Şu anda çalan bir şarkı yok.")
        return
    
    # Aktif çalan şarkının URL'sini bulmak için
    # 1. Mevcut çalan ses kaynağını kontrol et
    current_audio_source = None
    current_url = None
    
    try:
        # Discord.py'nin ses kaynağından URL'yi almaya çalış
        if hasattr(ctx.voice_client, 'source') and ctx.voice_client.source:
            if isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer):
                if hasattr(ctx.voice_client.source, 'original'):
                    current_audio_source = ctx.voice_client.source.original
                    
                    # FFmpegPCMAudio'dan URL'yi çıkar
                    if isinstance(current_audio_source, discord.FFmpegPCMAudio):
                        if hasattr(current_audio_source, 'filename'):
                            current_url = current_audio_source.filename
    except Exception as e:
        logger.error(f"Ses kaynağından URL alınırken hata: {e}")
    
    # 2. Eğer ses kaynağından alamadıysak, now_playing'den almayı dene
    if not current_url:
        try:
            if hasattr(music_player, 'now_playing') and guild_id in music_player.now_playing and music_player.now_playing[guild_id]:
                current_song = music_player.now_playing[guild_id]
                if isinstance(current_song, dict) and 'url' in current_song:
                    current_url = current_song['url']
                    
                    # Şarkı bilgilerini yedekle
                    song_backup = current_song.copy()
        except Exception as e:
            logger.error(f"Now playing bilgisi alınırken hata: {e}")
    
    # 3. Eğer hala URL bulamadıysak, kuyruktaki ilk şarkıyı kontrol et
    if not current_url:
        try:
            if hasattr(music_player, 'queues') and guild_id in music_player.queues and music_player.queues[guild_id]:
                current_song = music_player.queues[guild_id][0]
                if isinstance(current_song, dict) and 'url' in current_song:
                    current_url = current_song['url']
                    
                    # Şarkı bilgilerini yedekle
                    song_backup = current_song.copy()
        except Exception as e:
            logger.error(f"Kuyruk bilgisi alınırken hata: {e}")
    
    # Hala URL bulamadıysak hata mesajı göster
    if not current_url:
        await ctx.send("❌ Şarkı bilgisine erişilemedi. Lütfen şarkıyı durdurup yeniden başlatın.")
        return
    
    # Mevcut pozisyonu hesapla
    current_position = 0
    try:
        if 'start_time' in song_backup:
            current_position = int((datetime.datetime.now() - song_backup['start_time']).total_seconds())
    except Exception as e:
        logger.error(f"Mevcut pozisyon hesaplanırken hata: {e}")
        # Hata durumunda pozisyonu sıfırla, işleme devam et
        current_position = 0
    
    # Yeni pozisyon = mevcut pozisyon + ileri sarma süresi
    new_position = current_position + seconds
    
    # Seek işlemi
    try:
        # Şarkıyı durdur
        ctx.voice_client.stop()
        
        # Yeni ses kaynağı oluştur ve belirtilen konumdan başlat
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': f'-vn -ss {new_position}'
        }
        
        # Ses kaynağını oluştur
        audio_source = discord.FFmpegPCMAudio(current_url, **ffmpeg_options)
        volume = 0.5  # Varsayılan ses seviyesi
        
        # Ses seviyesini ayarla
        if hasattr(music_player, 'volume'):
            volume = music_player.volume
        
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
        
        # Şarkı bilgilerini güncelle
        if 'start_time' in song_backup:
            song_backup['start_time'] = datetime.datetime.now()
        
        # Şarkı bilgilerini now_playing'e kaydet
        if hasattr(music_player, 'now_playing'):
            music_player.now_playing[guild_id] = song_backup
        
        # Şarkı bitince bir sonrakine geç fonksiyonu
        def after_playing(error):
            if error:
                logger.error(f"Müzik çalınırken hata: {error}")
            
            # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
            try:
                if hasattr(music_player, 'play_next'):
                    coro = music_player.play_next(guild_id, None)
                    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                    future.result()
            except Exception as e:
                logger.error(f"Bir sonraki şarkıya geçerken hata: {e}")
        
        # Şarkıyı çal
        ctx.voice_client.play(volume_source, after=after_playing)
        
        # Bilgi mesajı
        minutes, seconds = divmod(new_position, 60)
        position_str = f"{minutes:02d}:{seconds:02d}"
        
        # Şarkı başlığını al
        song_title = "Bilinmeyen şarkı"
        if 'title' in song_backup:
            song_title = song_backup['title']
            
        await ctx.send(f"⏩ **{song_title}** şarkısı {position_str} konumuna ileri sarıldı.")
        
    except Exception as e:
        logger.error(f"İleri sarma işlemi sırasında hata: {e}")
        await ctx.send(f"❌ Şarkı ileri sarılırken bir hata oluştu: {str(e)[:1000]}")
        
    # Komut izleme için temizleme işlemi
    try:
        command_id = f"{ctx.channel.id}-{ctx.message.id}"
        if command_id in processed_commands:
            del processed_commands[command_id]
            logger.debug(f"Forward komutu için izleme temizlendi: {command_id}")
    except Exception as e:
        logger.error(f"Forward komutu için izleme temizlenirken hata: {e}")


@bot.command(name='seek', aliases=['atla', 'git'])
async def seek(ctx: commands.Context, position: str = None, _timestamp: str = None):
    """Mevcut çalan şarkıyı belirli bir konuma atla"""
    # Kanal kontrolü
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
        
    if position is None:
        await ctx.send("❌ Lütfen bir konum belirtin (dakika:saniye formatında, örn: 1:30).")
        return
        
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Guild ID'yi al
    guild_id = ctx.guild.id
        
    # Konum formatını kontrol et ve saniyeye çevir
    position_seconds = 0
    try:
        if ':' in position:
            try:
                minutes, seconds = map(int, position.split(':'))
                position_seconds = minutes * 60 + seconds
            except ValueError:
                await ctx.send("❌ Geçersiz konum formatı. Lütfen dakika:saniye formatında girin (1:30 gibi).")
                return
        else:
            try:
                position_seconds = int(position)
            except ValueError:
                await ctx.send("❌ Geçersiz konum. Lütfen bir sayı veya dakika:saniye formatında girin.")
                return
                
        if position_seconds < 0:
            await ctx.send("❌ Konum negatif olamaz.")
            return
                
        # Şarkı çalma durumu kontrolü
        if not ctx.voice_client.is_playing():
            await ctx.send("❌ Şu anda çalan bir şarkı yok.")
            return
        
        # Aktif çalan şarkının URL'sini bulmak için
        # 1. Mevcut çalan ses kaynağını kontrol et
        current_audio_source = None
        current_url = None
        song_backup = None
        
        try:
            # Discord.py'nin ses kaynağından URL'yi almaya çalış
            if hasattr(ctx.voice_client, 'source') and ctx.voice_client.source:
                if isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer):
                    if hasattr(ctx.voice_client.source, 'original'):
                        current_audio_source = ctx.voice_client.source.original
                        
                        # FFmpegPCMAudio'dan URL'yi çıkar
                        if isinstance(current_audio_source, discord.FFmpegPCMAudio):
                            if hasattr(current_audio_source, 'filename'):
                                current_url = current_audio_source.filename
        except Exception as e:
            logger.error(f"Ses kaynağından URL alınırken hata: {e}")
        
        # 2. Eğer ses kaynağından alamadıysak, now_playing'den almayı dene
        if not current_url:
            try:
                if hasattr(music_player, 'now_playing') and guild_id in music_player.now_playing and music_player.now_playing[guild_id]:
                    current_song = music_player.now_playing[guild_id]
                    if isinstance(current_song, dict) and 'url' in current_song:
                        current_url = current_song['url']
                        
                        # Şarkı bilgilerini yedekle
                        song_backup = current_song.copy()
            except Exception as e:
                logger.error(f"Now playing bilgisi alınırken hata: {e}")
        
        # 3. Eğer hala URL bulamadıysak, kuyruktaki ilk şarkıyı kontrol et
        if not current_url:
            try:
                if hasattr(music_player, 'queues') and guild_id in music_player.queues and music_player.queues[guild_id]:
                    current_song = music_player.queues[guild_id][0]
                    if isinstance(current_song, dict) and 'url' in current_song:
                        current_url = current_song['url']
                        
                        # Şarkı bilgilerini yedekle
                        song_backup = current_song.copy()
            except Exception as e:
                logger.error(f"Kuyruk bilgisi alınırken hata: {e}")
        
        # Hala URL bulamadıysak hata mesajı göster
        if not current_url:
            await ctx.send("❌ Şarkı bilgisine erişilemedi. Lütfen şarkıyı durdurup yeniden başlatın.")
            return
                
        # Şarkıyı durdur
        ctx.voice_client.stop()
        
        # Yeni ses kaynağı oluştur ve belirtilen konumdan başlat
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': f'-vn -ss {position_seconds}'
        }
        
        # Ses kaynağını oluştur
        audio_source = discord.FFmpegPCMAudio(current_url, **ffmpeg_options)
        volume = 0.5  # Varsayılan ses seviyesi
        
        # Ses seviyesini ayarla
        if hasattr(music_player, 'volume'):
            volume = music_player.volume
        
        volume_source = discord.PCMVolumeTransformer(audio_source, volume=volume)
        
        # Şarkı bilgilerini güncelle
        if song_backup:
            song_backup['start_time'] = datetime.datetime.now()
            
            # Şarkı bilgilerini now_playing'e kaydet
            if hasattr(music_player, 'now_playing'):
                music_player.now_playing[guild_id] = song_backup
        
        # Şarkı bitince bir sonrakine geç fonksiyonu
        def after_playing(error):
            if error:
                logger.error(f"Müzik çalınırken hata: {error}")
            
            # Bir sonraki şarkıyı çalmak için asyncio.run_coroutine_threadsafe kullan
            try:
                if hasattr(music_player, 'play_next'):
                    coro = music_player.play_next(guild_id, None)
                    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                    future.result()
            except Exception as e:
                logger.error(f"Bir sonraki şarkıya geçerken hata: {e}")
        
        # Şarkıyı çal
        ctx.voice_client.play(volume_source, after=after_playing)
        
        # Bilgi mesajı
        minutes, seconds = divmod(position_seconds, 60)
        position_str = f"{minutes:02d}:{seconds:02d}"
        
        # Şarkı başlığını al
        song_title = "Bilinmeyen şarkı"
        if song_backup and 'title' in song_backup:
            song_title = song_backup['title']
            
        await ctx.send(f"⏬ **{song_title}** şarkısı {position_str} konumuna atlandı.")
        
    except Exception as e:
        logger.error(f"Seek işlemi sırasında hata: {e}")
        await ctx.send(f"❌ Şarkı konumlandırılırken bir hata oluştu: {str(e)[:1000]}")
        
    # Komut izleme için temizleme işlemi
    try:
        command_id = f"{ctx.channel.id}-{ctx.message.id}"
        if command_id in processed_commands:
            del processed_commands[command_id]
            logger.debug(f"Seek komutu için izleme temizlendi: {command_id}")
    except Exception as e:
        logger.error(f"Seek komutu için izleme temizlenirken hata: {e}")


@bot.command(name='loop', aliases=['döngü', 'tekrarla'])
async def loop(ctx: commands.Context, _timestamp: str = None):
    # Zaman damgası parametresi komut izleme sistemini atlatmak için kullanılır
    # Her çağrıda farklı bir değer olacağı için, komut her zaman yeni bir komut olarak işlenir
    """Döngü modunu değiştir (kapalı -> şarkı -> kuyruk -> kapalı)"""
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Döngü modunu değiştir
    new_mode = music_player.toggle_loop(ctx.guild.id)
    
    # Emoji ve açıklama belirle
    if new_mode == "song":
        emoji = "🔂"  # Repeat single
        description = "Tek şarkı döngüsü açıldı. Şu anki şarkı sürekli tekrarlanacak."
    elif new_mode == "queue":
        emoji = "🔁"  # Repeat all
        description = "Kuyruk döngüsü açıldı. Tüm kuyruk bitince baştan başlayacak."
    else:  # off
        emoji = "❌"  # Cross mark
        description = "Döngü modu kapatıldı."
    
    # Embed ile bilgi ver
    embed = discord.Embed(
        title=f"{emoji} Döngü Modu: {new_mode.capitalize()}",
        description=description,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Ayarlayan: {ctx.author.name}")
    await ctx.send(embed=embed)

@bot.command(name='shuffle', aliases=['karıştır'])
async def shuffle(ctx: commands.Context, _timestamp: str = None):
    # Zaman damgası parametresi komut izleme sistemini atlatmak için kullanılır
    # Her çağrıda farklı bir değer olacağı için, komut her zaman yeni bir komut olarak işlenir
    """Kuyruktaki şarkıları karıştır"""
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Kuyruk kontrolü
    if ctx.guild.id not in music_player.queues or not music_player.queues[ctx.guild.id]:
        await ctx.send("❌ Kuyrukta şarkı yok.")
        return
        
    # Kuyruğu karıştır
    music_player.shuffle_queue(ctx.guild.id)
    
    # Embed ile bilgi ver
    embed = discord.Embed(
        title="🔀 Kuyruk Karıştırıldı",
        description=f"Kuyruktaki {len(music_player.queues[ctx.guild.id])} şarkı karıştırıldı.",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Karıştıran: {ctx.author.name} | !queue ile kuyruğu görüntüleyebilirsiniz")
    await ctx.send(embed=embed)

@bot.command(name='toggleshuffle', aliases=['karıştırma', 'karıştırmaç', 'shufflemode'])
async def toggle_shuffle(ctx: commands.Context):
    """Karıştırma modunu aç/kapat"""
    # Ses kanalı kontrolü
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.send("❌ Şu anda bir ses kanalına bağlı değilim.")
        return
        
    # Ses kanalı kontrolü
    if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send("❌ Bu komutu kullanmak için benimle aynı ses kanalında olmalısın.")
        return
        
    # Karıştırma modunu değiştir
    is_shuffle_on = music_player.toggle_shuffle(ctx.guild.id)
    
    # Embed ile bilgi ver
    if is_shuffle_on:
        embed = discord.Embed(
            title="🔀 Karıştırma Modu: Açık",
            description="Karıştırma modu açıldı. Yeni şarkılar kuyruğa eklendiğinde otomatik olarak karıştırılacak.",
            color=discord.Color.blue()
        )
    else:
        embed = discord.Embed(
            title="❌ Karıştırma Modu: Kapalı",
            description="Karıştırma modu kapatıldı. Şarkılar eklendiği sırayla çalınacak.",
            color=discord.Color.red()
        )
    
    embed.set_footer(text=f"Ayarlayan: {ctx.author.name}")
    await ctx.send(embed=embed)

@bot.command(name='setdefaultvolume', aliases=['setvol', 'defaultvol', 'defaultvolume', 'varsayılanses', 'varsayılansesseviyesi'])
async def set_default_volume(ctx: commands.Context, volume: int = None):
    """Varsayılan ses seviyesini ayarla (0-100)"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Benzersiz komut kimliği oluştur (komut izleme için)
    command_id = f"{ctx.channel.id}-{ctx.message.id}-setdefaultvolume"
    
    # Bu komut daha önce işlendi mi kontrol et
    if command_id in processed_commands:
        logger.debug(f"SetDefaultVolume komutu zaten işlendi, tekrar işlenmeyecek: {command_id}")
        return
        
    # Komutu işlendi olarak işaretle
    processed_commands[command_id] = datetime.datetime.now()
        
    if volume is None:
        # Mevcut varsayılan ses seviyesini göster
        default_volume = int(music_player.default_volume * 100)
        
        # Embed ile bilgi ver
        embed = discord.Embed(
            title="🔊 Varsayılan Ses Seviyesi",
            description=f"Mevcut varsayılan ses seviyesi: **%{default_volume}**",
            color=discord.Color.blue()
        )
        
        # Ses seviyesi görseli
        volume_bar = "▬" * int(default_volume / 5)  # Her 5 birim için bir blok
        empty_bar = "▫" * (20 - int(default_volume / 5))  # Kalan boş bloklar
        embed.add_field(name="Varsayılan Ses Seviyesi", value=f"`{volume_bar}{empty_bar}` %{default_volume}", inline=False)
        
        await ctx.send(embed=embed)
        return
        
    # Ses seviyesi sınırlarını kontrol et
    if volume < 0 or volume > 100:
        await ctx.send("❌ Ses seviyesi 0 ile 100 arasında olmalıdır.")
        return
        
    # Önceki varsayılan ses seviyesini kaydet
    old_volume = int(music_player.default_volume * 100)
    
    # Varsayılan ses seviyesini ayarla
    music_player.default_volume = volume / 100.0
        
    # Ses seviyesi ayarlarını veritabanına kaydet
    try:
        save_volume_settings(music_player.volume, music_player.default_volume)
        
        # Embed ile bilgi ver
        embed = discord.Embed(
            title="🔊 Varsayılan Ses Seviyesi Değiştirildi",
            description=f"Varsayılan ses seviyesi **%{old_volume}** → **%{volume}** olarak değiştirildi.",
            color=discord.Color.green()
        )
        
        # Ses seviyesi görseli
        volume_bar = "▬" * int(volume / 5)  # Her 5 birim için bir blok
        empty_bar = "▫" * (20 - int(volume / 5))  # Kalan boş bloklar
        embed.add_field(name="Yeni Varsayılan Ses Seviyesi", value=f"`{volume_bar}{empty_bar}` %{volume}", inline=False)
        
        embed.set_footer(text=f"Ayarlayan: {ctx.author.name}")
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Varsayılan ses seviyesi kaydedilirken hata: {e}")
        await ctx.send(f"✅ Varsayılan ses seviyesi **%{volume}** olarak ayarlandı, ancak kalıcı olarak kaydedilemedi.")

@bot.command(name='nowplaying', aliases=['np', 'şimdiçalıyor', 'şimdi'])
async def now_playing(ctx: commands.Context):
    """Mevcut çalan şarkı hakkında detaylı bilgi göster"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    # Benzersiz komut kimliği oluştur (komut izleme için)
    command_id = f"{ctx.channel.id}-{ctx.message.id}-nowplaying"
    
    # Bu komut daha önce işlendi mi kontrol et
    if command_id in processed_commands:
        logger.debug(f"NowPlaying komutu zaten işlendi, tekrar işlenmeyecek: {command_id}")
        return
        
    # Komutu işlendi olarak işaretle
    processed_commands[command_id] = datetime.datetime.now()
    
    # Şimdi çalan şarkı kontrolü
    if (ctx.guild.id not in music_player.now_playing or 
        music_player.now_playing[ctx.guild.id] is None or 
        ctx.guild.id not in music_player.voice_clients or 
        not music_player.voice_clients[ctx.guild.id].is_playing()):
        await ctx.send("❌ Şu anda çalan bir şarkı yok.")
        return
    
    # Şarkı bilgilerini al
    current_song = music_player.now_playing[ctx.guild.id]
    
    # Embed oluştur
    embed = discord.Embed(
        title="▶️ Şimdi Çalınıyor",
        description=f"**{current_song['title']}**",
        color=discord.Color.purple()
    )
    
    # Küçük resim ekle
    if current_song.get('thumbnail'):
        embed.set_thumbnail(url=current_song['thumbnail'])
    
    # Süre bilgisi
    embed.add_field(name="Süre", value=current_song.get('duration', 'Bilinmiyor'), inline=True)
    
    # Ekleyen bilgisi
    embed.add_field(name="Ekleyen", value=current_song.get('requester', 'Bilinmiyor'), inline=True)
    
    # Ses seviyesi
    embed.add_field(name="Ses Seviyesi", value=f"%{int(music_player.volume * 100)}", inline=True)
    
    # Kuyruk bilgisi
    queue_length = len(music_player.queues.get(ctx.guild.id, []))
    embed.set_footer(text=f"Kuyrukta {queue_length} şarkı daha var | !queue ile kuyruğu görüntüleyebilirsiniz")
    
    await ctx.send(embed=embed)

@bot.command(name='playlist', aliases=['pl', 'oynatmalistesi'])
async def play_playlist(ctx: commands.Context, *, query: str = None):
    """YouTube oynatma listesini kuyruğa ekle"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
    
    if query is None:
        await ctx.send("❌ Lütfen bir oynatma listesi URL'si veya adı belirtin.")
        return
    
    # Benzersiz komut kimliği oluştur (komut izleme için)
    command_id = f"{ctx.channel.id}-{ctx.message.id}-playlist"
    
    # Bu komut daha önce işlendi mi kontrol et
    if command_id in processed_commands:
        logger.debug(f"Playlist komutu zaten işlendi, tekrar işlenmeyecek: {command_id}")
        return
        
    # Komutu işlendi olarak işaretle
    processed_commands[command_id] = datetime.datetime.now()
    
    # Ses kanalına katıl
    if not await music_player.join_voice_channel(ctx):
        return
    
    # Yükleniyor mesajı
    loading_msg = await ctx.send(f"⌛ **{query}** oynatma listesi aranıyor...")
    
    try:
        # yt-dlp ile oynatma listesi bilgilerini al
        ydl_opts = dict(music_player.ytdl_format_options)
        ydl_opts['extract_flat'] = True  # Oynatma listesi için düz bilgi al
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            
            # Sonuç bir oynatma listesi mi kontrol et
            if '_type' not in info or info['_type'] != 'playlist':
                if 'entries' not in info:
                    await loading_msg.edit(content=f"❌ **{query}** bir oynatma listesi değil veya içinde video yok.")
                    return
            
            # Oynatma listesi bilgileri
            playlist_title = info.get('title', 'Bilinmeyen Oynatma Listesi')
            entries = info.get('entries', [])
            
            if not entries:
                await loading_msg.edit(content=f"❌ **{playlist_title}** oynatma listesinde video bulunamadı.")
                return
            
            # Kuyruk oluştur (yoksa)
            if ctx.guild.id not in music_player.queues:
                music_player.queues[ctx.guild.id] = deque()
            
            # En fazla 50 video ekle
            max_videos = min(50, len(entries))
            added_videos = 0
            
            # İlerleme mesajı
            progress_msg = await ctx.send(f"Oynatma listesinden videolar ekleniyor: 0/{max_videos}")
            
            # Her video için detaylı bilgi al ve kuyruğa ekle
            for i, entry in enumerate(entries[:max_videos]):
                try:
                    # Video URL'sini al
                    video_url = entry.get('url', None)
                    if not video_url and 'id' in entry:
                        video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                    
                    if not video_url:
                        continue
                    
                    # Detaylı video bilgilerini al
                    with yt_dlp.YoutubeDL(music_player.ytdl_format_options) as video_ydl:
                        video_info = await asyncio.to_thread(video_ydl.extract_info, video_url, download=False)
                        
                        # Video bilgileri
                        title = video_info.get('title', f'Video {i+1}')
                        url = video_info.get('url', None)
                        duration_sec = video_info.get('duration', 0)
                        duration = str(datetime.timedelta(seconds=duration_sec)) if duration_sec else 'Bilinmeyen Süre'
                        thumbnail = video_info.get('thumbnail', None)
                        
                        # Kuyruğa ekle
                        song_info = {
                            'title': title, 
                            'url': url, 
                            'duration': duration,
                            'thumbnail': thumbnail,
                            'requester': ctx.author.name
                        }
                        music_player.queues[ctx.guild.id].append(song_info)
                        added_videos += 1
                        
                        # Her 5 videoda bir ilerleme mesajını güncelle
                        if i % 5 == 0 or i == max_videos - 1:
                            await progress_msg.edit(content=f"Oynatma listesinden videolar ekleniyor: {i+1}/{max_videos}")
                
                except Exception as e:
                    logger.error(f"Oynatma listesinden video eklenirken hata: {e}")
                    continue
            
            # İlerleme mesajını sil
            await progress_msg.delete()
            
            # Şarkı çalma durumunu kontrol et
            if (ctx.guild.id not in music_player.now_playing or 
                music_player.now_playing[ctx.guild.id] is None or 
                not music_player.voice_clients[ctx.guild.id].is_playing()):
                await loading_msg.delete()
                await music_player.play_next(ctx.guild.id, ctx)
            else:
                # Oynatma listesi eklendi mesajı
                embed = discord.Embed(
                    title="✅ Oynatma Listesi Eklendi",
                    description=f"**{playlist_title}** oynatma listesinden **{added_videos}** video kuyruğa eklendi.",
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"Ekleyen: {ctx.author.name} | !queue ile kuyruğu görüntüleyebilirsiniz")
                await loading_msg.edit(content=None, embed=embed)
    
    except Exception as e:
        logger.error(f"Oynatma listesi yüklenirken hata: {e}")
        await loading_msg.edit(content=f"❌ Oynatma listesi yüklenirken bir hata oluştu: {str(e)[:1000]}")
        traceback_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        logger.error(f"Oynatma listesi yüklenirken hata detayı:\n{traceback_str}")

@bot.command(name='leave')
async def leave_voice(ctx: commands.Context):
    """Botun ses kanalından ayrılmasını sağla"""
    if ctx.channel.id != MUSIC_CHANNEL_ID:
        return
        
    if ctx.guild.id in music_player.voice_clients:
        if ctx.guild.id in music_player.queues:
            music_player.queues[ctx.guild.id].clear()
        if ctx.guild.id in music_player.now_playing:
            music_player.now_playing[ctx.guild.id] = None
        await music_player.voice_clients[ctx.guild.id].disconnect()
        del music_player.voice_clients[ctx.guild.id]
        await ctx.send("👋 Ses kanalından ayrıldım")
    else:
        await ctx.send("❌ Bir ses kanalında değilim")

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

# --- Tek İnstans Kontrolü ---
import socket
import sys
import atexit

# Global soket nesnesi
single_instance_socket = None

def cleanup_socket():
    """Program sonlandığında soket bağlantısını temizler"""
    global single_instance_socket
    if single_instance_socket:
        try:
            single_instance_socket.close()
            logger.info("Soket bağlantısı temizlendi.")
        except Exception as e:
            logger.error(f"Soket temizlenirken hata: {e}")

def ensure_single_instance():
    """Botun sadece tek bir instance'da çalışmasını sağlar."""
    global single_instance_socket
    try:
        # 12345 portunda bir soket açmaya çalış
        single_instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        single_instance_socket.bind(('localhost', 12345))
        # Program sonlandığında soketi temizle
        atexit.register(cleanup_socket)
        return single_instance_socket
    except socket.error:
        logger.critical("HATA: Bot zaten çalışıyor! Lütfen önce diğer instance'ı kapatın.")
        sys.exit(1)

# --- Komut İzleme Temizleme Görevi ---
# Bu görev on_ready içinde başlatılıyor

@tasks.loop(minutes=5)
async def cleanup_command_tracking():
    """Komut izleme sözlüğünü temizler ve bellek sızıntılarını önler."""
    global processed_commands
    now = datetime.datetime.now()
    expired_commands = []
    
    # 60 saniyeden eski komutları temizle
    for command_id, timestamp in processed_commands.items():
        if (now - timestamp).total_seconds() > 60:
            expired_commands.append(command_id)
    
    # Eski komutları kaldır
    for command_id in expired_commands:
        processed_commands.pop(command_id, None)
    
    if expired_commands:
        logger.debug(f"{len(expired_commands)} eski komut izleme kaydı temizlendi.")

@cleanup_command_tracking.before_loop
async def before_cleanup_command_tracking():
    await bot.wait_until_ready()
    logger.info("Komut izleme temizleme görevi başlatıldı.")

# --- Botu Çalıştır ---
if __name__ == "__main__":
    # Tek instance kontrolü
    instance_lock = ensure_single_instance()
    
    if not DISCORD_TOKEN: logger.critical("HATA: DISCORD_TOKEN ortam değişkeni bulunamadı!"); exit()
    if not DATABASE_URL: logger.critical("HATA: DATABASE_URL ortam değişkeni bulunamadı!"); exit()
    # API anahtarı kontrolleri yukarıda yapıldı

    # Veritabanı havuzunu başlat
    init_db_pool()

    logger.info("Bot başlatılıyor...")
    webserver_thread = None
    try:
        webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="FlaskWebserverThread")
        webserver_thread.start()
        
        # Bot'u çalıştır (cleanup_resources on_ready içinde başlatılacak)
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.errors.LoginFailure: logger.critical("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired: logger.critical("HATA: Gerekli Intent'ler (Members, Message Content) Discord Developer Portal'da etkinleştirilmemiş!")
    except psycopg2.OperationalError as db_err: logger.critical(f"PostgreSQL bağlantı hatası (Başlangıçta): {db_err}")
    except ImportError as import_err: logger.critical(f"Bot çalıştırılırken kritik import hatası: {import_err}\n{traceback.format_exc()}")
    except Exception as e: logger.critical(f"Bot çalıştırılırken kritik hata: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally: 
        logger.info("Bot kapatılıyor...")
        # Soket bağlantısını temizle
        cleanup_socket()