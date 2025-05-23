# Discord AI Sohbet Botu (Gemini & OpenRouter/DeepSeek)

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![discord.py](https://img.shields.io/badge/discord.py-2.5.2-7289DA.svg)](https://github.com/Rapptz/discord.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) <!-- veya projenizin lisansı -->

Google Gemini ve DeepSeek (OpenRouter üzerinden) modellerini destekleyen, özel sohbet kanalları oluşturan gelişmiş bir Discord botu.

<img src="https://i.imgur.com/3ZgSW98.png" alt="Bot Tanıtım Resmi" width="500" height="500"/> <!-- İsteğe bağlı: Botun ekran görüntüsü veya GIF'i -->

## 🚀 Özellikler

*   **Çoklu AI Modeli Desteği:**
    *   Google Gemini ( `gs:` ön eki ile, örn: `gs:gemini-1.5-flash-latest`)
    *   DeepSeek (OpenRouter üzerinden, `ds:` ön eki ile, örn: `ds:deepseek/deepseek-chat`)
*   **Otomatik Özel Sohbet Kanalları:** Belirlenen bir giriş kanalına yazılan ilk mesajla kullanıcıya özel, geçici sohbet kanalları oluşturur.
*   **Model Seçimi:** Kullanıcılar bir sonraki sohbetleri için istedikleri modeli (`!setmodel` komutuyla) seçebilirler.
*   **Konuşma Geçmişi Yönetimi:** Her özel kanalda ayrı konuşma geçmişi tutar (Gemini için session, DeepSeek için liste).
*   **Otomatik Kanal Temizleme:** Belirli bir süre (ayarlanabilir) aktif olmayan özel kanalları otomatik olarak siler.
*   **Veritabanı Entegrasyonu:** PostgreSQL veritabanı kullanarak yapılandırmayı ve aktif kanal bilgilerini kalıcı olarak saklar (Render/Koyeb gibi platformlar için ideal).
*   **Komutlar:** Kanal kapatma, geçmiş sıfırlama, model listeleme, mesaj temizleme gibi kullanışlı komutlar içerir.
*   **Hata Yönetimi:** API hataları, izin sorunları ve kullanıcı hataları için detaylı hata yakalama ve bilgilendirme.
*   **Deployment Odaklı:** Render/Koyeb gibi platformlarda kolayca deploy edilebilmesi için Flask ile basit bir web sunucusu içerir.

## 🛠️ Kurulum ve Çalıştırma

### Gereksinimler

*   Python 3.11 veya üzeri
*   Discord Bot Token'ı
*   Google Gemini API Anahtarı (Gemini kullanmak için)
*   OpenRouter API Anahtarı (DeepSeek kullanmak için)
*   PostgreSQL Veritabanı URL'si (örn: Render veya Koyeb tarafından sağlanan)

### Adımlar

1.  **Repository'yi Klonlayın:**
    ```bash
    git clone https://github.com/KULLANICI_ADINIZ/REPOSITORY_ADINIZ.git
    cd REPOSITORY_ADINIZ
    ```

2.  **Sanal Ortam Oluşturun (Önerilir):**
    ```bash
    python -m venv .venv
    source .venv/bin/activate  # Linux/macOS
    # .venv\Scripts\activate  # Windows
    ```

3.  **Bağımlılıkları Yükleyin:**
    ```bash
    pip install -r requirements.txt
    ```
    *(requirements.txt dosyasının güncel olduğundan emin olun: `discord.py`, `google-generativeai`, `python-dotenv`, `psycopg2-binary`, `Flask`, `requests` içermelidir)*

4.  **Ortam Değişkenlerini Ayarlayın:**
    *   `.env.example` dosyasının adını `.env` olarak değiştirin.
    *   `.env` dosyasını açın ve aşağıdaki değişkenleri kendi bilgilerinizle doldurun:
        ```dotenv
        DISCORD_TOKEN=BURAYA_DISCORD_BOT_TOKENINIZI_YAZIN
        GEMINI_API_KEY=BURAYA_GEMINI_API_ANAHTARINIZI_YAZIN (isteğe bağlı)
        OPENROUTER_API_KEY=BURAYA_OPENROUTER_API_ANAHTARINIZI_YAZIN (isteğe bağlı)
        DATABASE_URL=postgresql://kullanici:sifre@host:port/veritabani (PostgreSQL URL'niz)

        # Opsiyonel - OpenRouter Sıralama Başlıkları
        # OPENROUTER_SITE_URL=https://siteniz.com
        # OPENROUTER_SITE_NAME=Sitenizin Adı

        # Opsiyonel - Bot varsayılanları (DB'de yoksa kullanılır)
        # ENTRY_CHANNEL_ID=GIRIS_KANALI_IDSINI_YAZIN
        # DEFAULT_INACTIVITY_TIMEOUT_HOURS=1
        ```
    *   **Önemli:** En az bir API anahtarı (`GEMINI_API_KEY` veya `OPENROUTER_API_KEY`) ve `DATABASE_URL` gereklidir.

5.  **Veritabanını Hazırlayın:**
    *   Bot ilk çalıştığında gerekli tabloları otomatik olarak oluşturacaktır. PostgreSQL sunucunuzun erişilebilir olduğundan emin olun.

6.  **Botu Çalıştırın:**
    ```bash
    python bot_v1.2.py
    ```
    
## 🤖 Bot Kullanımı

*   **Sohbet Başlatma:** `.env` veya `!setentrychannel` ile ayarladığınız giriş kanalına bir mesaj yazın. Bot size özel bir kanal oluşturacaktır.
*   **Komutlar:** Botun komutlarını görmek için `!help` veya `!komutlar` yazın. Başlıca komutlar:
    *   `!listmodels`: Kullanılabilir AI modellerini listeler.
    *   `!setmodel <model_adı>`: Bir sonraki sohbet için model seçer (örn: `!setmodel gs:gemini-1.5-pro-latest`).
    *   `!endchat`: Mevcut özel sohbet kanalını kapatır.
    *   `!resetchat`: Mevcut özel sohbet kanalındaki AI hafızasını sıfırlar.
    *   `!ask <soru>`: Herhangi bir kanalda hızlıca varsayılan Gemini modeline soru sorar (cevap geçicidir).
    *   `!clear <sayı|all>`: (Yetkili) Mesajları temizler.
    *   `!setentrychannel <#kanal>`: (Admin) Otomatik kanal oluşturma kanalını ayarlar.
    *   `!settimeout <saat>`: (Admin) Otomatik kanal silme süresini ayarlar (0 = kapalı).

## 🤝 Katkıda Bulunma

Katkılarınız memnuniyetle karşılanır! Lütfen bir Pull Request açmadan önce bir Issue oluşturarak yapmak istediğiniz değişikliği tartışın.

1.  Repository'yi Forklayın.
2.  Yeni bir Branch oluşturun (`git checkout -b özellik/yeni-ozellik`).
3.  Değişikliklerinizi yapın ve Commit edin (`git commit -m 'Yeni özellik eklendi'`).
4.  Branch'inizi Pushlayın (`git push origin özellik/yeni-ozellik`).
5.  Bir Pull Request açın.

## 📜 Lisans

Bu proje [MIT Lisansı](LICENSE) <!-- veya projenizin lisans dosyasına link --> altındadır.

---
