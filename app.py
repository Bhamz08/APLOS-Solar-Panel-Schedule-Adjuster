"""Dashboard entry point: `streamlit run app.py`."""

import streamlit as st

from aplos import APP_NAME
from aplos.ui import dashboard, history_page, schedule_page, settings_page, weather_page
from aplos.ui.common import setup_page

st.set_page_config(page_title=APP_NAME, page_icon="☀️", layout="wide")
setup_page()

pages = st.navigation([
    st.Page(dashboard.render, title="Dashboard", icon="📊", default=True),
    st.Page(schedule_page.render, title="Schedule", icon="🗓️", url_path="schedule"),
    st.Page(weather_page.render, title="Weather", icon="🌦️", url_path="weather"),
    st.Page(history_page.render, title="Decision history", icon="🧾", url_path="history"),
    st.Page(settings_page.render, title="Settings", icon="⚙️", url_path="settings"),
])
st.sidebar.caption(APP_NAME)
pages.run()
