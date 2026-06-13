# PolarisFolio Web — VPS Deployment

A self-hosted web app that pulls from Microsoft 365 and ICS calendars,
generates a hyperlinked planner PDF, and pushes it to your reMarkable Paper Pro.

---

## VPS setup (Ubuntu 22.04 / 24.04)

### 1. Copy files to server

```bash
scp -r polarisfolio_web/ ubuntu@your-server-ip:~/polarisfolio_web
```

### 2. Install dependencies

```bash
ssh ubuntu@your-server-ip

cd ~/polarisfolio_web
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure nginx

```bash
sudo apt install nginx -y
sudo cp nginx.conf /etc/nginx/sites-available/polarisfolio
sudo ln -s /etc/nginx/sites-available/polarisfolio /etc/nginx/sites-enabled/
# Edit the server_name in /etc/nginx/sites-available/polarisfolio
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Add SSL (optional but recommended)

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
# Then uncomment the HTTPS block in nginx.conf
```

### 5. Run as a service

```bash
# Edit polarisfolio.service - update User and paths if needed
sudo cp polarisfolio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polarisfolio
sudo systemctl start polarisfolio
sudo systemctl status polarisfolio
```

### 6. Open in browser

Visit `http://your-server-ip` or your domain.

Go to Settings first and add your Azure app client ID, then connect Microsoft 365.

---

## Running locally (dev)

```bash
cd polarisfolio_web
pip install -r requirements.txt
uvicorn app:app --reload --port 8001
# Visit http://localhost:8001
```

---

## Microsoft OAuth redirect URI

When registering your Azure app, add this redirect URI:

```
http://your-domain.com/auth/callback
```

For local dev:
```
http://localhost:8001/auth/callback
```

In Azure portal: App registrations → your app → Authentication →
Add a platform → Web → paste the redirect URI.

---

## Updating

```bash
ssh ubuntu@your-server-ip
cd ~/polarisfolio_web
# copy new files
sudo systemctl restart polarisfolio
```

---

## File locations

| Path | Purpose |
|------|---------|
| `~/.polarisfolio_web.db` | SQLite database (settings, feeds, history) |
| `~/polarisfolio_pdfs/` | Generated PDF files |
| `~/.polarisfolio_msal_cache.json` | Microsoft OAuth token cache |
| `~/.polarisfolio_rm_token` | reMarkable device token |
