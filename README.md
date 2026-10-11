# TAJ OPTICALS — storefront & owner dashboard

A responsive Taj Opticals storefront styled from the supplied screenshot, with a Python standard-library API, persistent SQLite catalogue, protected owner dashboard, inventory/media administration, CSV import/export, server-repriced cart quotes and WhatsApp order-message drafts.

This workspace began without an application, admin account, object store, WhatsApp verification or AI provider key. The local application is now running and an initial owner admin account has been created for the email supplied by the owner. External services remain opt-in and are not reported as connected when they are not configured.

## Run locally

Python 3.13+ is sufficient; this project has no third-party runtime dependencies.

```bash
python3 server.py
```

Open `http://localhost:8000`. The server binds to `0.0.0.0` for a hosted preview and uses:

- SQLite: `data/taj-opticals.sqlite3`
- Local media storage: `uploads/`

The database schema and initial store settings/categories/pages are created on startup. The catalogue starts empty intentionally: no prices, specifications, reviews or sample product records have been fabricated.

## First owner account

There is no public registration route or default password. The owner account for this workspace has been created using the email supplied by the owner. Its generated temporary password was provided separately; sign in at `/admin` and rotate it immediately using **Store settings → Change admin password**. Password changes require the current password and sign out other admin sessions.

For a fresh installation, create the first administrator from a trusted server shell:

```bash
python3 server.py create-admin
```

The command prompts for an email and a password (12–256 characters); it stores a PBKDF2-HMAC-SHA256 password hash, not the clear-text password. Alternatively set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in the process environment for a one-time non-interactive setup, then remove those setup values.

The dashboard uses an HttpOnly session cookie, a separate CSRF token, server-side authorization on every admin API route, a login rate limit and no administrator self-sign-up. Use HTTPS in production and set `COOKIE_SECURE=1`.

## Products and 500+ record imports

1. Sign in at `/admin` and use **Products → Add product**, or **Bulk import / export**.
2. Download the blank CSV template in the importer. Required columns are `sku`, `name`, `category` and `selling_price_inr`. Additional fields include brand, description, status, stock, frame attributes, contact-lens fields and JSON specifications.
3. Import accepts at least 500 rows; the CSV upload is capped at 10 MB. Click **Preview import** before committing. Duplicate SKUs and invalid rows are reported by row number; only valid rows are eligible for import. A blank status defaults to `draft` so unreviewed products are not published accidentally.
4. Upload multiple JPG, PNG or WebP images and MP4/WebM videos from a saved product's media manager. The owner can add HTTPS media URLs, preview media, reorder items, set a cover image and delete media. Product imports and edits persist in SQLite.
5. Export all catalogue entries from the same admin area. Imported media may be attached after the product records are created.

The public catalogue is database-backed and paginated (12 per page by default, up to 48); it supports text/SKU search, category and brand filters, price bounds, price sorting and newest-first sorting. Product detail pages include actual related products from the same category when available.

## WhatsApp order setup

The confirmed business phone `9597378385` is saved as a **call** number. It is deliberately **not** treated as a verified WhatsApp number. Customer WhatsApp order buttons remain inactive until the owner enters an international-format WhatsApp number in **Admin → Store settings** and checks the explicit confirmation box.

When enabled, the customer reviews a server-generated quote first. The WhatsApp draft includes product names, SKUs, quantities, unit prices, line subtotals, product subtotal, delivery, total and product URLs. Customers must press **Send** in WhatsApp; opening a draft is not a sent or confirmed order. No online payment gateway is included.

## Delivery calculation

Defaults are configured as requested:

- Product subtotal **below ₹1,700**: **₹50** delivery.
- Product subtotal **₹1,700 or more**: **FREE DELIVERY**.

The subtotal is computed in integer paise from current database prices; the browser cannot submit its own trusted total or delivery amount. Cart quantity changes and pre-WhatsApp order review call the backend quote endpoint. Admin can update the threshold and standard charge from **Store settings**. No taxes or additional charges are added.

## AI Video Studio

The owner dashboard includes **AI Video Studio**, product-photo selection and upload, owner-editable headline/description, 9:16 / 1:1 / 16:9 formats, duration and style choices, asynchronous job tracking, output preview/download and an option to save a completed output to the product gallery.

A real Runway Dev image-to-video adapter is present, but it is **not configured in this workspace**. To enable it:

1. Create a Runway Dev account/project, create an API secret and explicitly add credits in that provider account.
2. Set `RUNWAYML_API_SECRET` on the server. Optionally set `RUNWAY_MODEL` (default `gen4.5`). Never put the key in frontend code or a public repository.
3. Reload/restart the app. The admin studio will show the provider as configured. Each video generation still requires an administrator to confirm the potentially paid generation.

The integration submits one selected lead image, polls the asynchronous task endpoint, persists job metadata and retains the returned provider URL. If permitted by the provider output URL, the owner can download and save the generated MP4 to local media storage. The current adapter does not add music or voice-over and does not render text overlays. No provider request was made during development and no credits were purchased. Runway says API usage is credit-metered at $0.01 per credit, with a minimum $10 purchase to get started; model rates vary, so check current model pricing before use: [Runway API pricing](https://docs.dev.runwayml.com/guides/pricing/) and [account / credit setup](https://docs.dev.runwayml.com/guides/setup/).

## Editable pages, promotions and store details

The dashboard can update store name, tagline, address, Google Maps URL, business phone, verified WhatsApp number, logo and delivery configuration. It can create, edit, schedule, publish or delete image promotions. No unapproved Diwali offer is preloaded. FAQ, return/exchange, privacy and terms text starts as clearly labelled draft copy for owner review; it is not represented as confirmed legal policy. Delivery values on the customer site are derived from saved settings.

The provided reference screenshot supplies the hero photograph and category artwork crops. The logo is a crisp built-in multicolour glasses wordmark unless the owner uploads the original logo.

## Configuration

See `.env.example`. The app reads normal process environment variables; it does not automatically parse a `.env` file.

- `PORT`: listen port, default `8000`.
- `TAJ_DB_PATH`: SQLite path, default `data/taj-opticals.sqlite3`.
- `TAJ_UPLOAD_DIR`: local uploads directory, default `uploads/`.
- `COOKIE_SECURE=1`: mark admin cookies Secure behind HTTPS.
- `RUNWAYML_API_SECRET`: optional, required for paid Runway video requests.
- `RUNWAY_MODEL`: optional model id, default `gen4.5`.

No payment credentials, email-delivery service, store hours or verified WhatsApp number are configured. The owner email is used for admin sign-in only; this app does not send email.

## Tests and checks

Run:

```bash
python3 -m py_compile server.py
node --check static/app.js
python3 -m unittest discover -s tests -v
```

The API tests use isolated temporary SQLite databases and test authorization/CSRF, product persistence, image upload, CSV duplicate reporting, WhatsApp verification, server-side quote calculation at ₹1,699/₹1,700, settings changes, product deletion and admin password rotation. External Runway generation is not exercised because no provider credentials are configured.

## Deployment and remaining production setup

The app is prepared to run behind an HTTPS reverse proxy using `python3 server.py`; set `PORT`, `COOKIE_SECURE=1`, `TAJ_DB_PATH` and `TAJ_UPLOAD_DIR` in the host. Use a persistent volume and a **single application instance** with SQLite/local uploads. SQLite and local filesystem media are real persistent storage on that volume, but are not suitable for horizontal multi-instance scaling or ephemeral serverless filesystems. For a multi-instance deployment, replace the SQLite adapter with managed PostgreSQL and move media to private S3-compatible object storage/CDN; set access controls, backups and lifecycle rules before launch.

Before production launch:

- create the admin account on the production host;
- attach persistent database and media storage; enable HTTPS and secure cookies;
- verify the WhatsApp phone number in WhatsApp, then configure it in admin;
- review draft policies and promotional wording;
- load actual products and confirm stock/prices/specifications;
- configure and cost-review Runway only if wanted;
- set up off-host encrypted backups for the database and uploads.

Use `python3 server.py init-db` for an idempotent schema initialization. For a safe online SQLite backup while the app is running:

```bash
python3 scripts/backup_db.py
```

A minimal `Dockerfile` is included; mount a persistent volume at `/data` (SQLite database at `/data/taj-opticals.sqlite3`, uploaded media at `/data/uploads`), set `COOKIE_SECURE=1`, and terminate HTTPS at a trusted reverse proxy. A managed Python host with a persistent disk and single worker is also suitable. See `RAILWAY_DEPLOY.md` for Railway setup notes. This project has not been deployed to a public domain. No production hosting account, external database, object storage, confirmed WhatsApp registration or AI API key has been connected.
