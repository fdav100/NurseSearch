import hmac
import io
import os
import random
import time

import pandas as pd
import streamlit as st

import ahpra

st.set_page_config(page_title="AHPRA registration lookup", layout="wide")


# --------------------------------------------------------------------------
# Password gate
# --------------------------------------------------------------------------
def get_expected_password():
    try:
        pw = st.secrets.get("APP_PASSWORD")
    except Exception:
        pw = None
    return pw or os.environ.get("APP_PASSWORD")


def check_password():
    if st.session_state.get("authed"):
        return
    expected = get_expected_password()
    if not expected:
        if os.environ.get("ALLOW_NO_PASSWORD") == "1":
            return
        st.error(
            "No password is configured. Add `APP_PASSWORD` to the app's Secrets "
            "(or set the environment variable ALLOW_NO_PASSWORD=1 for local use)."
        )
        st.stop()

    st.title("AHPRA registration lookup")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


check_password()

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
st.session_state.setdefault("results", {})  # number -> row dict
st.session_state.setdefault("order", [])    # input order


def results_df():
    rows = [st.session_state.results[n] for n in st.session_state.order
            if n in st.session_state.results]
    return pd.DataFrame(rows, columns=ahpra.COLUMNS)


def error_numbers():
    return [n for n in st.session_state.order
            if st.session_state.results.get(n, {}).get("Result") == "Error"]


def to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Results")
        ws = writer.sheets["Results"]
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col) + 2
            ws.column_dimensions[col[0].column_letter].width = min(width, 45)
    return buf.getvalue()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.title("AHPRA registration lookup")
st.caption(
    "Paste registration numbers (one per line, or separated by commas/spaces). "
    "Looks up name, date of first registration, suburb, state and whether any "
    "conditions are listed."
)

text = st.text_area("Registration numbers", height=180, placeholder="NMW0002122705")
delay = st.slider("Delay between lookups (seconds, randomised)", 2.0, 15.0, (3.0, 6.0), 0.5)

numbers = ahpra.parse_numbers(text)
odd = [n for n in numbers if not ahpra.looks_like_reg_number(n)]
if numbers:
    msg = f"{len(numbers)} unique number(s)."
    if odd:
        msg += f" Unusual format (will still be searched): {', '.join(odd[:10])}"
    st.caption(msg)

c1, c2, c3 = st.columns(3)
start = c1.button("Start lookup", type="primary", disabled=not numbers)
retry = c2.button("Retry errors", disabled=not error_numbers())
test = c3.button("Test first number (diagnostics)", disabled=not numbers)

table_area = st.empty()


def render_table():
    df = results_df()
    if df.empty:
        table_area.empty()
    else:
        table_area.dataframe(df, hide_index=True)


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
def snippet(r, n=500):
    return (r.text or "")[:n]


def run_diagnostics(number):
    st.subheader("Diagnostics")
    s = ahpra.new_session()
    steps = [
        ("1. Load register page", lambda: ahpra.warm_up(s)),
        ("2. GetPredictions", lambda: ahpra.get_predictions(s, number)),
        ("3. Profile POST", lambda: ahpra.get_profile(s, number)),
    ]
    for label, fn in steps:
        try:
            r = fn()
        except ahpra.LookupFailed as e:
            st.error(f"{label}: {e}")
            if e.blocked:
                st.info("Looks blocked. Try running the app locally instead.")
            return
        blocked = ahpra.looks_blocked(r)
        (st.error if blocked else st.success)(
            f"{label}: HTTP {r.status_code}"
            f"{' - looks like a bot-protection page' if blocked else ''}"
        )
        with st.expander(f"{label}: response start"):
            st.code(snippet(r))
        if blocked:
            return
        time.sleep(random.uniform(1.0, 2.0))
        if label.startswith("3"):
            parsed = ahpra.parse_profile(r.text, number)
            st.write("Parsed profile:", parsed if parsed else "Could not parse expected fields")


# --------------------------------------------------------------------------
# Run loop
# --------------------------------------------------------------------------
def run(batch):
    st.button("Stop run", help="Results so far stay on screen and can be downloaded.")
    progress = st.progress(0.0)
    status = st.empty()
    s = ahpra.new_session()

    try:
        ahpra.warm_up(s)
    except ahpra.LookupFailed as e:
        status.error(f"Couldn't load the AHPRA register page: {e}")
        return

    consecutive_blocked = 0
    for i, number in enumerate(batch):
        status.info(f"Looking up {number} ({i + 1} of {len(batch)})...")
        row, blocked = ahpra.lookup(s, number)
        st.session_state.results[number] = row
        render_table()
        progress.progress((i + 1) / len(batch))

        consecutive_blocked = consecutive_blocked + 1 if blocked else 0
        if consecutive_blocked >= 3:
            status.error(
                "Stopped: 3 lookups in a row were blocked by bot protection. "
                "Wait a while, increase the delay, or run the app locally."
            )
            return
        if i < len(batch) - 1:
            time.sleep(random.uniform(*delay))
    status.success("Done.")


if start:
    st.session_state.order = numbers
    st.session_state.results = {}
    run(numbers)
elif retry:
    run(error_numbers())
elif test:
    run_diagnostics(numbers[0])

render_table()

# --------------------------------------------------------------------------
# Summary + downloads
# --------------------------------------------------------------------------
df = results_df()
if not df.empty:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Found", int((df["Result"] == "Found").sum()))
    m2.metric("Not found", int((df["Result"] == "Not found").sum()))
    m3.metric("Errors", int((df["Result"] == "Error").sum()))
    m4.metric("Conditions flagged", int((df["Has conditions"] == "Yes").sum()))

    d1, d2 = st.columns(2)
    d1.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name="ahpra_results.csv",
        mime="text/csv",
    )
    d2.download_button(
        "Download Excel",
        to_excel_bytes(df),
        file_name="ahpra_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.caption(
        "'Has conditions' is Yes if any Conditions entry on the profile is not 'None'. "
        "'Check manually' means no Conditions entry could be read."
    )
