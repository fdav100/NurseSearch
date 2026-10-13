# AHPRA registration lookup

Files: `app.py` (interface), `ahpra.py` (lookup logic), `requirements.txt`.

## Deploy on streamlit.app
1. Put these files in a GitHub repo.
2. At share.streamlit.io choose "Create app", pick the repo, main file `app.py`.
3. Under Advanced settings > Secrets add:
       APP_PASSWORD = "choose-a-password"
4. Deploy, open the app, paste ONE number and click "Test first number (diagnostics)".
   All three steps should show HTTP 200 and a parsed profile. If step 1-3 show a
   bot-protection block, run locally instead.

## Run locally (fallback)
    pip install -r requirements.txt
    APP_PASSWORD=yourpassword streamlit run app.py
(Windows cmd: `set APP_PASSWORD=yourpassword` then `streamlit run app.py`.
 For local use without a password: set ALLOW_NO_PASSWORD=1.)

## Notes
- Delay slider defaults to 3-6 s between numbers (plus 1-2 s between the two requests per number).
- Clicking any button (e.g. "Stop run") interrupts a run; results so far stay and can be downloaded.
- Results: Found / Not found / Error. "Retry errors" re-runs only the errors.
- Please check AHPRA's website terms before bulk use.
