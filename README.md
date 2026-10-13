# AHPRA registration lookup (cloud, browser-based)

Files: `app.py` (interface), `ahpra.py` (lookup logic), `requirements.txt`,
`packages.txt` (system libraries Chromium needs on Streamlit Cloud).

A headless Chromium (Playwright) loads the AHPRA register page so the site's own
JavaScript runs like a normal visit. No stealth plugins or fingerprint tricks. If a
CAPTCHA or hard block appears, the run stops and says so.

## Deploy on streamlit.app
1. Put all five files in a GitHub repo (`packages.txt` must be in the repo root).
2. At share.streamlit.io choose "Create app", pick the repo, main file `app.py`.
3. Advanced settings > Secrets:
       APP_PASSWORD = "choose-a-password"
4. Deploy. The first start installs the system libraries (a few minutes).
5. Open the app, enter the password, paste ONE number and click
   "Test first number (diagnostics)". The first browser start downloads Chromium
   (1-2 minutes). Three green steps = the browser got through.
   A red step shows what the browser saw (screenshot + start of the HTML).

## Notes
- One browser is shared by the whole app; only one run at a time (others are told to wait).
- Delay slider defaults to 3-6 s between numbers (plus 1-2 s between the two steps per number).
- Clicking any button (e.g. "Stop run") interrupts a run after the current lookup;
  results so far stay and can be downloaded.
- "Retry errors" re-runs only the errors. "Restart browser" resets a stuck browser.
- Streamlit's free tier has limited memory. If the app crashes/reboots mid-run, send me the log.
- If AHPRA still blocks the shared cloud IP: use a Docker-based host (Hugging Face Spaces,
  Render, Fly.io, Cloud Run) or AHPRA's paid API.
- Please check AHPRA's website terms on automated access before bulk use.
