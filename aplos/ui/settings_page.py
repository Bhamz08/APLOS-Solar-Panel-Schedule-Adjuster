from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path
from zoneinfo import available_timezones

import streamlit as st
from pydantic import ValidationError

from aplos.config import env
from aplos.config.settings import AppSettings
from aplos.database import SettingsRepository
from aplos.llm.advisor import build_advisor, resolve_model_path
from aplos.llm.backends import LlamaCppBackend
from aplos.ui.common import conn, settings

TIMEZONES = sorted(available_timezones())


def _save(s: AppSettings) -> None:
    try:
        validated = AppSettings.model_validate(s.model_dump())
    except ValidationError as exc:
        for err in exc.errors():
            st.error(f"{'.'.join(map(str, err['loc']))}: {err['msg']}")
        return
    SettingsRepository(conn()).save(validated)
    st.success("Settings saved. The scheduler picks up changes on its next check (within a minute).")


def render() -> None:
    st.title("Settings")
    s = settings()
    site_tab, rules_tab, weather_tab, llm_tab, auto_tab = st.tabs(
        ["Site & schedule", "Decision rules", "Weather API", "Local model", "Automation"]
    )

    with site_tab:
        with st.form("site"):
            s.site.name = st.text_input("Site name", s.site.name)
            c1, c2 = st.columns(2)
            s.site.latitude = c1.number_input("Latitude", -90.0, 90.0, s.site.latitude, format="%.4f")
            s.site.longitude = c2.number_input("Longitude", -180.0, 180.0, s.site.longitude, format="%.4f")
            s.site.timezone = st.selectbox("Time zone", TIMEZONES, TIMEZONES.index(s.site.timezone)
                                           if s.site.timezone in TIMEZONES else 0)
            st.markdown("**Daily execution**")
            c1, c2 = st.columns(2)
            run_t = c1.time_input("Daily run time (site local)", time.fromisoformat(s.schedule.daily_run_time),
                                  step=timedelta(minutes=5))
            s.schedule.evaluation_horizon_hours = c2.number_input(
                "Evaluate cleanings within (hours)", 6, 240, s.schedule.evaluation_horizon_hours)
            st.markdown("**Cleaning operations**")
            c1, c2, c3 = st.columns(3)
            s.schedule.cleaning_window_start_hour = c1.number_input("Earliest start hour", 0, 23,
                                                                    s.schedule.cleaning_window_start_hour)
            s.schedule.cleaning_window_end_hour = c2.number_input("Latest start hour", 0, 23,
                                                                  s.schedule.cleaning_window_end_hour)
            s.schedule.cleaning_duration_hours = c3.number_input("Cleaning duration (h)", 0.5, 24.0,
                                                                 float(s.schedule.cleaning_duration_hours), 0.5)
            c1, c2 = st.columns(2)
            s.schedule.default_interval_days = c1.number_input("Default interval (days)", 1, 120,
                                                               s.schedule.default_interval_days)
            clean_t = c2.time_input("Default cleaning time", time.fromisoformat(s.schedule.default_cleaning_time),
                                    step=timedelta(minutes=15))
            c1, c2 = st.columns(2)
            s.schedule.auto_extend_plan = c1.toggle("Keep the recurring plan filled automatically",
                                                    s.schedule.auto_extend_plan)
            s.schedule.plan_horizon_days = c2.number_input("Plan ahead (days)", 14, 730, s.schedule.plan_horizon_days)
            st.caption("New cleanings continue the cycle from the last originally planned date at the default "
                       "interval; postponements do not shift the cycle and cancelled cleanings are not recreated.")
            if st.form_submit_button("Save", type="primary"):
                s.schedule.daily_run_time = run_t.strftime("%H:%M")
                s.schedule.default_cleaning_time = clean_t.strftime("%H:%M")
                _save(s)

    with rules_tab:
        r = s.rules
        with st.form("rules"):
            st.markdown("**Evaluation window**")
            c1, c2 = st.columns(2)
            r.pre_window_hours = c1.number_input("Hours checked before cleaning", 0.0, 72.0, float(r.pre_window_hours))
            r.post_window_hours = c2.number_input("Hours checked after cleaning", 0.0, 168.0, float(r.post_window_hours))
            st.markdown("**Rain**")
            c1, c2, c3 = st.columns(3)
            r.rain_amount_threshold_mm = c1.number_input("Significant rain (mm in window)", 0.1, 200.0,
                                                         float(r.rain_amount_threshold_mm), 0.5)
            r.rain_probability_threshold_pct = c2.number_input("Required probability (%)", 0.0, 100.0,
                                                               float(r.rain_probability_threshold_pct), 5.0)
            r.skip_rain_amount_mm = c3.number_input("Heavy rain → skip (mm)", 0.1, 500.0, float(r.skip_rain_amount_mm), 1.0)
            c1, c2, c3 = st.columns(3)
            r.marginal_probability_pct = c1.number_input("Marginal probability (%)", 0.0, 100.0,
                                                         float(r.marginal_probability_pct), 5.0)
            r.dry_buffer_hours = c2.number_input("Drying buffer after rain (h)", 0.0, 96.0, float(r.dry_buffer_hours))
            r.wet_hour_mm = c3.number_input("Wet-hour threshold (mm/h)", 0.05, 10.0, float(r.wet_hour_mm), 0.05)
            st.markdown("**Sand & dust**")
            c1, c2, c3 = st.columns(3)
            r.dust_threshold_ugm3 = c1.number_input("Dust event (µg/m³)", 1.0, 20000.0, float(r.dust_threshold_ugm3), 10.0)
            r.sand_wind_gust_threshold_kmh = c2.number_input("Sand-lifting gusts (km/h)", 1.0, 250.0,
                                                             float(r.sand_wind_gust_threshold_kmh))
            r.dust_settle_hours = c3.number_input("Settle time after event (h)", 0.0, 96.0, float(r.dust_settle_hours))
            r.use_wind_as_sand_proxy = st.checkbox("Treat strong gusts as sand risk when no dust data is available",
                                                   r.use_wind_as_sand_proxy)
            st.markdown("**Postponement limits & data quality**")
            c1, c2, c3 = st.columns(3)
            r.max_postpone_hours = c1.number_input("Max delay from original (h)", 1.0, 720.0, float(r.max_postpone_hours))
            r.max_postponements = c2.number_input("Max postponements per event", 0, 20, r.max_postponements)
            r.min_model_confidence = c3.number_input("Min model confidence", 0.0, 1.0, float(r.min_model_confidence), 0.05)
            c1, c2, c3 = st.columns(3)
            r.stale_after_hours = c1.number_input("Forecast stale after (h)", 1.0, 168.0, float(r.stale_after_hours))
            r.max_forecast_age_hours = c2.number_input("Forecast unusable after (h)", 1.0, 336.0,
                                                       float(r.max_forecast_age_hours))
            r.min_window_coverage_pct = c3.number_input("Required window coverage (%)", 0.0, 100.0,
                                                        float(r.min_window_coverage_pct), 5.0)
            if st.form_submit_button("Save rules", type="primary"):
                _save(s)
        if st.button("Reset rules to defaults"):
            s.rules = AppSettings().rules
            _save(s)

    with weather_tab:
        w = s.weather
        with st.form("weather"):
            labels = {"open_meteo": "Open-Meteo (no key required)", "openweathermap": "OpenWeatherMap (API key)",
                      "mock": "MOCK — synthetic development data"}
            keys = list(labels)
            w.provider = st.selectbox("Provider", keys, keys.index(w.provider), format_func=labels.get)
            c1, c2, c3 = st.columns(3)
            w.request_timeout_s = c1.number_input("Request timeout (s)", 1.0, 120.0, float(w.request_timeout_s))
            w.retries = c2.number_input("Retries", 0, 5, w.retries)
            w.forecast_days = c3.number_input("Forecast days", 1, 16, w.forecast_days)
            w.include_dust = st.checkbox("Fetch dust concentration (Open-Meteo air quality)", w.include_dust)
            w.mock_scenario = st.selectbox("Mock scenario (mock provider only)",
                                           ["clear", "rain", "heavy_rain", "sand", "mixed"],
                                           ["clear", "rain", "heavy_rain", "sand", "mixed"].index(w.mock_scenario))
            if st.form_submit_button("Save weather settings", type="primary"):
                _save(s)
        st.markdown("**OpenWeatherMap API key**")
        current = env.get_secret(env.ENV_OPENWEATHER_KEY)
        st.caption(f"Current: {env.mask(current)}. Stored only in the local `.env` file (excluded from version "
                   "control), never in the database.")
        with st.form("key"):
            new_key = st.text_input("New API key", type="password")
            if st.form_submit_button("Save key") and new_key.strip():
                env.save_secret(env.ENV_OPENWEATHER_KEY, new_key.strip())
                st.success("API key saved to .env")

    with llm_tab:
        m = s.llm
        override = env.model_path_override()
        with st.form("llm"):
            modes = {"local": "Local Gemma 2B (llama.cpp)", "mock": "MOCK backend (development, no model)",
                     "disabled": "Disabled (deterministic rules only)"}
            m.mode = st.selectbox("Mode", list(modes), list(modes).index(m.mode), format_func=modes.get)
            m.model_path = st.text_input("Model file (GGUF), absolute or relative to the project", m.model_path,
                                         disabled=bool(override))
            if override:
                st.caption(f"Overridden by APLOS_MODEL_PATH={override}")
            c1, c2, c3 = st.columns(3)
            m.n_ctx = c1.number_input("Context tokens", 512, 8192, m.n_ctx, 256)
            m.n_threads = c2.number_input("CPU threads (0 = auto)", 0, 128, m.n_threads)
            m.n_gpu_layers = c3.number_input("GPU layers (-1 = all)", -1, 200, m.n_gpu_layers)
            c1, c2, c3 = st.columns(3)
            m.temperature = c1.number_input("Temperature", 0.0, 2.0, float(m.temperature), 0.05)
            m.max_tokens = c2.number_input("Max output tokens", 64, 2048, m.max_tokens, 32)
            m.timeout_s = c3.number_input("Inference timeout (s)", 5.0, 900.0, float(m.timeout_s), 5.0)
            if st.form_submit_button("Save model settings", type="primary"):
                _save(s)
        path = Path(resolve_model_path(m))
        st.markdown("**Installation check**")
        st.write(f"- Model file: `{env.display_path(path)}` — {'found' if path.is_file() else '**not found**'}")
        st.write(f"- llama-cpp-python: {'installed' if LlamaCppBackend.library_available() else '**not installed**'}")
        if st.button("Load model and test"):
            with st.spinner("Loading model…"):
                status = build_advisor(m, preload=True).status()
            (st.success if status.available else st.error)(status.detail)
        st.caption("See README → 'Installing the local Gemma 2B model' for download instructions.")

    with auto_tab:
        a = s.automation
        with st.form("auto"):
            a.auto_apply = st.toggle("Apply recommendations automatically", a.auto_apply)
            a.auto_apply_on_stale = st.toggle("Also apply automatically when the forecast is stale", a.auto_apply_on_stale)
            st.caption("When off, POSTPONE/SKIP recommendations wait for operator approval on the Schedule page. "
                       "Mock weather data is never applied automatically; with no trustworthy data no change is made.")
            if st.form_submit_button("Save automation settings", type="primary"):
                _save(s)
        st.markdown(f"**Storage** — database `{env.display_path(env.db_path())}`, logs `{env.display_path(env.log_dir())}`")
