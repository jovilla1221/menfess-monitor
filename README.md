# Menfess Monitor Bot

Bot Telegram untuk monitoring 12+ channel menfess, notifikasi keyword ke user via DM.

## Fitur
- Monitor 12+ channel menfess (auto-detect post baru)
- Keyword filter per user
- Notifikasi real-time via DM
- Admin panel: tambah/hapus channel, interval, broadcast
- SQLite storage ringan

## Setup
```bash
git clone https://github.com/aonagiworks/menfess-monitor.git
cd menfess-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: BOT_TOKEN + ADMIN_ID
python bot.py
```

## Perintah
| Command | Fungsi |
|---------|--------|
| `/start` | Daftar / lihat status |
| `/keyword kata` | Tambah keyword filter |
| `/unkeyword kata` | Hapus keyword |
| `/list` | Lihat keyword aktif |
| `/channel` | List channel yang dimonitor |
| `/interval 30` | Set interval check (admin) |
| `/addchannel @username` | Tambah channel (admin) |
| `/delchannel @username` | Hapus channel (admin) |
| `/broadcast pesan` | Broadcast ke user (admin) |

## Deploy VPS
```bash
# systemd service
sudo cp menfess-monitor.service /etc/systemd/system/
sudo systemctl enable --now menfess-monitor
```

## License
MIT