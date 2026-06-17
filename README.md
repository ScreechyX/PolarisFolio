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
git pull
sudo systemctl restart polarisfolio
```

---

## Auto-deploy from GitHub (webhook)

The container can update itself on every push using a GitHub webhook that hits
the built-in `POST /webhook/github` endpoint. It verifies an HMAC secret, then
runs `update.sh` (git pull → reinstall deps if `requirements.txt` changed →
restart the service) only when something actually changed.

**1. Let the app user restart the service without a password.** As root:

```bash
# adjust the username to match polarisfolio.service's User=
echo 'ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart polarisfolio' \
  > /etc/sudoers.d/polarisfolio-update
chmod 440 /etc/sudoers.d/polarisfolio-update
visudo -c   # validate
```

**2. Set the webhook secret** (pick a long random value, e.g. `openssl rand -hex 32`).
Add it to the service environment and reload:

```bash
sudo systemctl edit polarisfolio
# In the editor, add:
#   [Service]
#   Environment=GITHUB_WEBHOOK_SECRET=<your-secret>
#   Environment=GITHUB_DEPLOY_BRANCH=main
sudo systemctl daemon-reload && sudo systemctl restart polarisfolio
chmod +x update.sh
```

**3. Add the webhook on GitHub** — repo *Settings → Webhooks → Add webhook*:

| Field | Value |
|-------|-------|
| Payload URL | `https://your-host/webhook/github` |
| Content type | `application/json` |
| Secret | the same secret as above |
| Events | *Just the push event* |

GitHub must be able to reach your host (public URL or a tunnel such as
Cloudflare Tunnel). The “ping” GitHub sends on creation should return `200`.

**4. (Optional) Daily safety-net timer** — re-pulls once a day in case a push
webhook was ever missed:

```bash
sudo cp polarisfolio-update.service polarisfolio-update.timer /etc/systemd/system/
# edit User=/WorkingDirectory=/ExecStart= paths in the .service if yours differ
sudo systemctl daemon-reload
sudo systemctl enable --now polarisfolio-update.timer
systemctl list-timers polarisfolio-update.timer
```

Deploy activity is logged to `~/.polarisfolio_update.log`. You can trigger a
manual deploy any time with `./update.sh`.

---

## File locations

| Path | Purpose |
|------|---------|
| `~/.polarisfolio_web.db` | SQLite database (settings, feeds, history) |
| `~/polarisfolio_pdfs/` | Generated PDF files |
| `~/.polarisfolio_msal_cache.json` | Microsoft OAuth token cache |
| `~/.polarisfolio_rm_token` | reMarkable device token |
