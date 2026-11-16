"""Titik masuk dashboard CekSinyal untuk Streamlit.

Lokal : python -m ceksinyal dashboard [--db <path>]
Cloud : pilih file ini (dashboard/streamlit_app.py) sebagai "Main file path".
"""

import sys
from pathlib import Path

# Folder repo harus ada di sys.path supaya paket `ceksinyal` bisa diimpor (Streamlit hanya menambahkan folder file ini).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ceksinyal.dashboard.app import main  # noqa: E402

main()
