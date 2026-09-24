# Medline Industries - Private Label Portfolio

A private tracker of Medline's private-label SKUs:
- new item numbers over the last 30, 60 and 90 days
- items that left distribution
- Medline categories and subcategories
- likely country of origin
- an Excel/CSV portfolio download for any category

**Live site:** GitHub Pages serves this repo, so the site works in any browser. Enter the team passcode to unlock it.

## How the data stays private
The site's data (`data.enc.json`) and the pipeline's working data (`state/state.enc.json`) are encrypted with AES-256-GCM. The key comes from the passcode through PBKDF2-SHA256 (250,000 rounds). The repo and the site hold only ciphertext, and the browser decrypts it locally once the passcode is entered. The Actions logs print counts only.

## Monthly refresh
`.github/workflows/refresh.yml` runs on the 1st of each month. You can also run it by hand from **Actions → Monthly data refresh → Run workflow**. It pulls three sources:
1. **FDA AccessGUDID release files:** Medline Industries device records. A new publish counts as a new SKU; "Not in commercial distribution" counts as an exit.
2. **medline.com catalog search:** Medline-brand families by Medline category, and a category for each new item number.
3. **openFDA establishment registration:** the manufacturing sites and countries listed for each FDA product code.

If a source can't be reached, the run keeps that source's data from the previous month and says so in the run summary.

### One-time setup
- **Settings → Secrets and variables → Actions → New repository secret:** name it `LABEL_WATCH_PASSCODE` and use the site passcode as the value.
- **Settings → Pages:** Source "Deploy from a branch", Branch `main`, folder `/ (root)`.

### Changing the passcode
Run the pipeline locally with the old passcode to decrypt `state/state.enc.json`. Then re-encrypt it and `data.enc.json` with the new passcode, and update the secret. Ask Claude to do it.
