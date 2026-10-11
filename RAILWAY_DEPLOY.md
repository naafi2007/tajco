# Railway deployment notes

This project is prepared for Railway's Dockerfile builder. **No Railway service has been created or deployed from this workspace.** Railway account authorization and a repository/service connection are still required.

## Free-plan caveat

As checked on 11 October 2026, Railway's Free plan includes **$1 of resource usage per month** and allows one replica with up to **0.5 GB volume storage**. A new account's trial is a one-time **$5 credit for up to 30 days**; afterward it reverts to Free. The $1 credit does not roll over. Railway says volumes created by Trial accounts are deleted 30 days after the trial credits expire, and its pricing page lists volume-data deletion 30 days after a Free/Trial plan expires. Therefore, do **not** rely on a new-account Free plan as durable hosting for this site's SQLite database and customer uploads unless Railway confirms the volume will be retained. Hobby is **$5/month**, including $5 of monthly resource usage; usage above that is billed separately. It is not a free plan. Check Railway's current terms and usage controls before deploying.

Official references:
- https://docs.railway.com/pricing/plans
- https://docs.railway.com/pricing/free-trial
- https://docs.railway.com/volumes

## Deployment steps

1. Put the project in a private GitHub repository. Do **not** commit `.env`, `data/`, `uploads/`, or any database/password files; `.gitignore` already excludes them.
2. In Railway, create a project and deploy the repository. Railway should use the included `Dockerfile`.
3. Attach a Railway Volume to the app service with mount path **`/data`**. The app writes the SQLite database to `/data/taj-opticals.sqlite3` and uploaded media to `/data/uploads` so both live on that volume.
4. Add these service variables:
   - `COOKIE_SECURE=1`
   - `TAJ_DB_PATH=/data/taj-opticals.sqlite3`
   - `TAJ_UPLOAD_DIR=/data/uploads`
   - `RAILWAY_RUN_UID=0` — Railway mounts volumes as root; the included entrypoint takes ownership of `/data`, then drops privileges to the unprivileged `app` user before starting the web server.
5. Generate a Railway public domain and wait for `GET /api/health` to return 200. Terminate HTTPS using Railway's domain/TLS.
6. Create the first **production** admin from a private service shell with `python3 server.py create-admin`. Do not reuse a password posted in chat. Add real products only after the persistent volume is attached.

## Important limits

- This app uses SQLite plus local image/video uploads. Run one app instance only; don't scale horizontally without migrating to PostgreSQL and object storage.
- The Free plan's small volume/usage allowance is suitable only for a small demo. Product photos/videos count against volume space. Back up the database and uploads separately.
- Railway's $1 monthly free usage credit is not an unlimited, guaranteed always-on hosting plan. Check the billing dashboard and set usage limits before giving the URL to a client.
- The existing workspace preview is not a permanent public deployment. The public Railway domain does not exist until an authorized Railway account creates the service and domain.
