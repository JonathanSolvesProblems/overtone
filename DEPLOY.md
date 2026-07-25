# Deploying the Overtone demo

The app is a Python + ffmpeg service, so it needs a container host, not static
hosting. Fly.io is the recommended target: it builds the `Dockerfile` on a
remote builder (no local Docker needed) and runs it close to the B2
`us-east-005` bucket.

Your personal site stays where it is. To give the demo a nice URL like
`overtone.yourdomain.com`, add a DNS record pointing the subdomain at the Fly
app (step 5).

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
