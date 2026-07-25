# Deploying the Overtone demo

The app is a Python + ffmpeg service, so it needs a container host, not static
hosting.

## Live deployment: OVH box (Docker + Traefik)

Overtone runs on the OVH sandbox at `~/experimental-projects/overtone/`, built
from the public repo and routed by the box's existing Traefik (entrypoint
`websecure`, certresolver `le`, HTTP-01), the same pattern as the other web
stacks on that box.

```
~/experimental-projects/overtone/
├── app/                  git clone of the public repo
├── docker-compose.yml    Traefik-labelled web service, capped CPU/memory
└── .env                  secrets, chmod 600
```

**Deploy / update to the latest commit:**

```bash
ssh jonathan@51.161.82.166
cd ~/experimental-projects/overtone
git -C app pull
docker compose up -d --build
docker logs -f overtone      # expect: Uvicorn running on 0.0.0.0:8080
```

**The one manual step — DNS.** Traefik cannot issue the TLS certificate until
`overtone.jonathanandrei.com` resolves to the box. Add an `A` record in the DNS
panel for `jonathanandrei.com`:

```
Type: A   Name: overtone   Value: 51.161.82.166   TTL: default
```

(If that domain's DNS is proxied through Cloudflare, set the record to
"DNS only" / grey cloud so the Let's Encrypt HTTP-01 challenge reaches the box.)
Once it resolves, Traefik obtains the certificate automatically on the next
retry and the demo is live at `https://overtone.jonathanandrei.com`.

## Alternative: Fly.io

The repo also ships a `fly.toml` and `deploy-secrets.ps1` for hosting on Fly
instead. Fly builds the `Dockerfile` on a remote builder (no local Docker
needed).

## One-time setup

1. **Install flyctl** and sign in:
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   fly auth login
   ```

2. **Create the app** (uses the committed `fly.toml`; don't let it overwrite):
   ```powershell
   fly apps create overtone      # or pick another name and update fly.toml
   ```

## Deploy

3. **Push the secrets** from your local `.env` (this script reads `.env` and
   sets them as Fly secrets — nothing secret is ever committed):
   ```powershell
   ./deploy-secrets.ps1
   ```

4. **Deploy**:
   ```powershell
   fly deploy
   ```
   The first build takes a few minutes (it installs ffmpeg and the Python deps).
   When it finishes, `fly open` opens the live URL.

## Custom subdomain (optional)

5. Point a subdomain on your existing domain at the app:
   ```powershell
   fly certs add overtone.yourdomain.com
   ```
   Fly prints the exact DNS records to add. In your Hostinger DNS panel, add the
   `CNAME` (and the validation record Fly shows). Once it validates, the demo is
   live at your subdomain over HTTPS.

## Notes

- **Keeping it warm for judges.** `fly.toml` sets `min_machines_running = 0`, so
  the machine suspends when idle and cold-starts (~2–5s) on the next visit. To
  avoid any wait during judging, set it to `1` (a shared-cpu-1x/1GB machine is a
  few dollars a month).
- **Spend safety.** The app enforces a daily spend cap, a per-client rate limit,
  and a demo-video allowlist (see `src/overtone/guard.py`), so the live-describe
  button cannot run up the provider bill or be pointed at arbitrary videos.
- **Redeploys.** After changing code, just `fly deploy` again. Secrets persist;
  only re-run `deploy-secrets.ps1` if a key changed.
