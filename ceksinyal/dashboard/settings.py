"""Pengaturan dashboard: dari variabel lingkungan, lalu st.secrets (Streamlit Cloud), lalu config.yaml (lokal).

Tidak mengimpor streamlit: `secrets` diberikan sebagai mapping biasa supaya bisa dites.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .. import config as config_mod
from ..config import Config, ConfigError
from . import data

log = logging.getLogger(__name__)

ENV_DB = "CEKSINYAL_DASHBOARD_DB"          # path DB -> mode sqlite
ENV_MODE = "CEKSINYAL_DASHBOARD_MODE"      # sqlite / sheets
ENV_PUBLIC = "CEKSINYAL_DASHBOARD_PUBLIC"  # 1 / 0
SECRET_SERVICE_ACCOUNT = "gcp_service_account"
READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
MODE_SQLITE, MODE_SHEETS = "sqlite", "sheets"


@dataclass
class DashboardSettings:
    config: Config
    mode: str
    public: bool
    aliases: dict[str, str]
    db_path: Path | None = None
    spreadsheet_id: str = ""
    credentials_file: Path | None = None
    credentials_info: dict[str, Any] | None = None  # isi JSON service account dari st.secrets

    @property
    def cache_key(self) -> tuple:
        """Kunci cache data (tanpa rahasia). mtime ikut supaya DB lokal yang bertambah terbaca ulang."""
        if self.mode == MODE_SQLITE:
            mtime = self.db_path.stat().st_mtime if self.db_path and self.db_path.is_file() else 0
            return (MODE_SQLITE, str(self.db_path), mtime)
        return (MODE_SHEETS, self.spreadsheet_id)

    def problem(self) -> str | None:
        """Alasan data tidak bisa dimuat (untuk ditampilkan), atau None kalau pengaturan lengkap."""
        if self.mode == MODE_SQLITE:
            if not self.db_path or not self.db_path.is_file():
                return f"File database tidak ditemukan: {self.db_path}"
            return None
        if not self.spreadsheet_id:
            return "ID spreadsheet belum diisi (dashboard.spreadsheet_id di secrets, atau sheets.spreadsheet_id di config)."
        if not self.credentials_info and not (self.credentials_file and self.credentials_file.is_file()):
            return ("Kredensial service account belum ada ([gcp_service_account] di secrets, atau "
                    "GOOGLE_CREDENTIALS_FILE di .env).")
        return None


def _plain(value: Any) -> Any:
    """st.secrets (AttrDict bersarang) -> dict/list biasa."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _truthy(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "ya", "yes", "on")


def resolve(secrets: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None,
            config_path: str | Path | None = None) -> DashboardSettings:
    secrets = _plain(secrets or {})
    env = os.environ if env is None else env
    try:
        cfg = config_mod.load_config(config_path)
    except ConfigError as exc:
        log.warning("config.yaml tidak bisa dibaca (%s); memakai pengaturan bawaan", exc)
        cfg = Config()

    # Bagian [diagnosis], [dashboard], [kos_aliases] di secrets menimpa config (di Cloud tidak ada config.yaml).
    dash_secret = dict(secrets.get("dashboard") or {})
    spreadsheet_id = str(dash_secret.pop("spreadsheet_id", "") or "")
    overlay: dict[str, Any] = {}
    if secrets.get("diagnosis"):
        overlay["diagnosis"] = secrets["diagnosis"]
    if "timezone_offset_hours" in dash_secret:
        overlay["timezone_offset_hours"] = dash_secret.pop("timezone_offset_hours")
    aliases = secrets.get("kos_aliases") or dash_secret.pop("kos_aliases", None)
    if dash_secret or aliases:
        overlay["dashboard"] = {**dash_secret, **({"kos_aliases": aliases} if aliases else {})}
    if overlay:
        merged = config_mod._build(overlay, Path.cwd(), None)
        if "diagnosis" in overlay:
            cfg.diagnosis = merged.diagnosis
        if "dashboard" in overlay:
            cfg.dashboard = merged.dashboard
        if "timezone_offset_hours" in overlay:
            cfg.timezone_offset_hours = merged.timezone_offset_hours

    info = secrets.get(SECRET_SERVICE_ACCOUNT)
    settings = DashboardSettings(
        config=cfg, mode=cfg.dashboard.mode, public=cfg.dashboard.public, aliases=dict(cfg.dashboard.kos_aliases),
        db_path=cfg.database, spreadsheet_id=spreadsheet_id or cfg.sheets.spreadsheet_id,
        credentials_file=cfg.sheets.credentials_file, credentials_info=dict(info) if info else None,
    )
    if env.get(ENV_PUBLIC):
        settings.public = _truthy(env[ENV_PUBLIC])
    if env.get(ENV_MODE, "").lower() in (MODE_SQLITE, MODE_SHEETS):
        settings.mode = env[ENV_MODE].lower()
    if env.get(ENV_DB):
        settings.mode, settings.db_path = MODE_SQLITE, Path(env[ENV_DB]).expanduser()
    if settings.mode == "auto":
        has_credentials = bool(settings.credentials_info or settings.credentials_file)
        settings.mode = MODE_SHEETS if settings.spreadsheet_id and has_credentials else MODE_SQLITE
    return settings


def load_dataset(settings: DashboardSettings) -> data.Dataset:
    """Muat data sesuai mode. Melempar exception dengan pesan yang layak ditampilkan kalau gagal."""
    if settings.mode == MODE_SQLITE:
        return data.load_sqlite(settings.db_path)
    import gspread

    if settings.credentials_info:
        client = gspread.service_account_from_dict(settings.credentials_info, scopes=READONLY_SCOPES)
    else:
        client = gspread.service_account(filename=str(settings.credentials_file), scopes=READONLY_SCOPES)
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    return data.load_sheets(data.gspread_reader(spreadsheet), source="Google Sheets (spreadsheet bersama)")
