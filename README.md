# 369bot - Solana Wallet & KOL Tracker Discord Bot

A high-performance Discord bot that tracks Solana wallets and KOL (Key Opinion Leader) activity in real-time, queries token details via contract address (`/ca`), manages premium subscriptions via Supabase, and processes secure Solana payment splits.

## 🚀 VPS Deployment Guide (Linux / Ubuntu)

Follow these steps to deploy and run the bot 24/7 in the background on your VPS:

### 1. System Update and Installation of Dependencies
Update your packages and install Python 3.10+, pip, virtual environment, and Git:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv git -y
```

### 2. Clone the Repository
Clone your repository to the VPS:
```bash
git clone https://github.com/johnlick120012-boop/369bot.git
cd 369bot
```

### 3. Setup Virtual Environment
Create and activate a Python virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Requirements
Install all required dependencies:
```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables
Copy the template configuration file and fill in your tokens, Supabase keys, RPC endpoints, and channel/category restrictions:
```bash
cp .env.example .env
nano .env
```
*(Press `Ctrl + O` then `Enter` to save, and `Ctrl + X` to exit nano)*

### 6. Run 24/7 using Systemd (Recommended)
To ensure the bot automatically starts on system boot and restarts if it crashes, create a systemd service file:

1. Create the service file:
   ```bash
   sudo nano /etc/systemd/system/369bot.service
   ```
2. Paste the following configuration (replace `/path/to/369bot` with your actual directory path, e.g. `/home/ubuntu/369bot`):
   ```ini
   [Unit]
   Description=369bot Discord Bot
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/path/to/369bot
   ExecStart=/path/to/369bot/venv/bin/python bot.py
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
3. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable 369bot.service
   sudo systemctl start 369bot.service
   ```
4. View status or logs:
   ```bash
   sudo systemctl status 369bot.service
   sudo journalctl -u 369bot.service -f
   ```

---

## ⚙️ Configuration Parameters (.env)
* `KOL_TRACKER_CHANNEL_ID`: Comma-separated list of Discord channels to broadcast KOL purchase alerts to.
* `ALLOWED_CATEGORY_IDS`: Comma-separated list of category IDs where general bot commands (like `/ca` and `/wallet`) are accepted.
* `CA_CHANNEL_ID`: Channel ID restricted for `/ca` command usage.
* `SUPABASE_URL` / `SUPABASE_KEY`: Supabase connection details for premium user state and custom wallet persistence.
