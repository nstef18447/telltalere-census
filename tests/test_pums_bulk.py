"""Unit tests for telltalere_census.pums_bulk."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from telltalere_census.pums_bulk import (
    PumsBulkResult,
    _pums_parquet_path,
    _pums_url,
    download_pums_bulk,
)


# ---------------------------------------------------------------------------
# URL + path helpers
# ---------------------------------------------------------------------------

def test_pums_url_5yr_format():
    url = _pums_url(end_year=2024, pums_type="5yr", state_abbrev="IL", record_type="h")
    assert url == "https://www2.census.gov/programs-surveys/acs/data/pums/2024/5-Year/csv_hil.zip"


def test_pums_url_1yr_format():
    url = _pums_url(end_year=2024, pums_type="1yr", state_abbrev="IL", record_type="p")
    assert url == "https://www2.census.gov/programs-surveys/acs/data/pums/2024/1-Year/csv_pil.zip"


def test_pums_url_handles_pr():
    """Puerto Rico — abbrev should lowercase to 'pr'."""
    url = _pums_url(end_year=2024, pums_type="5yr", state_abbrev="PR", record_type="h")
    assert url.endswith("csv_hpr.zip")


def test_pums_parquet_path_layout(tmp_path: Path):
    p = _pums_parquet_path(tmp_path, "2020-2024", "h", "17")
    assert p == tmp_path / "pums" / "2020-2024" / "h_17.parquet"


# ---------------------------------------------------------------------------
# Synthetic ZIP/CSV fixtures
# ---------------------------------------------------------------------------

def _build_synthetic_pums_zip(record_type: str, n_rows: int = 5) -> bytes:
    """Build an in-memory ZIP with a CSV that mimics PUMS schema."""
    csv = io.StringIO()
    if record_type == "h":
        csv.write("SERIALNO,PUMA,WGTP,NP,TEN,HINCP\n")
        for i in range(n_rows):
            csv.write(f"2024GQ{i:07d},00100,{50 + i},{2 + i},1,{50000 + i * 1000}\n")
    else:
        csv.write("SERIALNO,SPORDER,AGEP,SEX,PWGTP,WAGP\n")
        for i in range(n_rows):
            csv.write(f"2024GQ{i:07d},1,{30 + i},1,{20 + i},{40000 + i * 1000}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        member = "psam_h17.csv" if record_type == "h" else "psam_p17.csv"
        zf.writestr(member, csv.getvalue())
    return buf.getvalue()


def _fake_zip_response(record_type: str, status: int = 200, n_rows: int = 5):
    r = MagicMock()
    r.status_code = status
    r.content = _build_synthetic_pums_zip(record_type, n_rows) if status == 200 else b""
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# download_pums_bulk integration with mocked HTTP
# ---------------------------------------------------------------------------

def test_download_pums_bulk_writes_parquet(tmp_path: Path):
    def fake_get(url, timeout=None, stream=None):
        record_type = "h" if "csv_h" in url else "p"
        return _fake_zip_response(record_type, n_rows=4)

    with patch("telltalere_census.pums_bulk.requests") as mock_requests:
        mock_requests.get.side_effect = fake_get
        mock_requests.RequestException = Exception

        result = download_pums_bulk(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            pums_type=["5yr"],
            states=["17"],
            record_types=["h", "p"],
            max_concurrent_downloads=2,
            min_free_gb=0.0,
        )

    assert isinstance(result, PumsBulkResult)
    assert result.completed_count == 2
    assert result.failed_count == 0

    h_pq = tmp_path / "pums" / "2020-2024" / "h_17.parquet"
    p_pq = tmp_path / "pums" / "2020-2024" / "p_17.parquet"
    assert h_pq.exists()
    assert p_pq.exists()

    h_df = pd.read_parquet(h_pq)
    p_df = pd.read_parquet(p_pq)
    assert "HINCP" in h_df.columns
    assert "AGEP" in p_df.columns
    assert len(h_df) == 4
    assert len(p_df) == 4


def test_download_pums_bulk_is_resumable(tmp_path: Path):
    pre = tmp_path / "pums" / "2020-2024" / "h_17.parquet"
    pre.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"SERIALNO": ["2024GQ0000001"]}).to_parquet(pre, index=False)

    call_count = {"n": 0}

    def fake_get(url, timeout=None, stream=None):
        call_count["n"] += 1
        record_type = "h" if "csv_h" in url else "p"
        return _fake_zip_response(record_type)

    with patch("telltalere_census.pums_bulk.requests") as mock_requests:
        mock_requests.get.side_effect = fake_get
        mock_requests.RequestException = Exception

        result = download_pums_bulk(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            pums_type=["5yr"],
            states=["17"],
            record_types=["h", "p"],
            max_concurrent_downloads=1,
            min_free_gb=0.0,
        )

    assert result.skipped_count == 1
    assert result.completed_count == 1
    assert call_count["n"] == 1  # only p was fetched; h was already present


def test_download_pums_bulk_dry_run(tmp_path: Path):
    with patch("telltalere_census.pums_bulk.requests") as mock_requests:
        mock_requests.get.side_effect = AssertionError("dry-run should not call HTTP")
        mock_requests.RequestException = Exception

        result = download_pums_bulk(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            pums_type=["5yr"],
            states=["17"],
            record_types=["h"],
            min_free_gb=0.0,
            dry_run=True,
        )
    assert result.completed_count == 0


def test_download_pums_bulk_logs_http_failure(tmp_path: Path):
    def fake_get_404(url, timeout=None, stream=None):
        return _fake_zip_response("h", status=404)

    with patch("telltalere_census.pums_bulk.requests") as mock_requests:
        mock_requests.get.side_effect = fake_get_404
        mock_requests.RequestException = Exception

        result = download_pums_bulk(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            pums_type=["5yr"],
            states=["17"],
            record_types=["h"],
            min_free_gb=0.0,
        )
    assert result.completed_count == 0
    assert result.failed_count == 1
