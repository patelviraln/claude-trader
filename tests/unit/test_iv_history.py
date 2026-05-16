"""Unit tests for src/iv_history.py."""
import json
from pathlib import Path

import pytest

from src.iv_history import append_iv_sample, load_iv_series


class TestLoadIvSeries:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        result = load_iv_series("AAPL", history_dir=tmp_path)
        assert result == []

    def test_returns_single_entry(self, tmp_path):
        (tmp_path / "AAPL.jsonl").write_text('{"date": "2026-01-01", "iv": 0.25}\n')
        result = load_iv_series("AAPL", history_dir=tmp_path)
        assert result == [0.25]

    def test_returns_multiple_entries_in_order(self, tmp_path):
        lines = '\n'.join([
            '{"date": "2026-01-01", "iv": 0.20}',
            '{"date": "2026-01-02", "iv": 0.30}',
            '{"date": "2026-01-03", "iv": 0.25}',
        ]) + '\n'
        (tmp_path / "NVDA.jsonl").write_text(lines)
        result = load_iv_series("NVDA", history_dir=tmp_path)
        assert result == [0.20, 0.30, 0.25]

    def test_skips_blank_lines(self, tmp_path):
        (tmp_path / "MSFT.jsonl").write_text(
            '{"date": "2026-01-01", "iv": 0.18}\n\n{"date": "2026-01-02", "iv": 0.22}\n'
        )
        result = load_iv_series("MSFT", history_dir=tmp_path)
        assert result == [0.18, 0.22]

    def test_skips_malformed_lines(self, tmp_path):
        (tmp_path / "TSLA.jsonl").write_text(
            '{"date": "2026-01-01", "iv": 0.40}\nnot-json\n{"date": "2026-01-03", "iv": 0.35}\n'
        )
        result = load_iv_series("TSLA", history_dir=tmp_path)
        assert result == [0.40, 0.35]

    def test_skips_entries_missing_iv_key(self, tmp_path):
        (tmp_path / "SPY.jsonl").write_text(
            '{"date": "2026-01-01"}\n{"date": "2026-01-02", "iv": 0.15}\n'
        )
        result = load_iv_series("SPY", history_dir=tmp_path)
        assert result == [0.15]

    def test_isolates_by_ticker(self, tmp_path):
        (tmp_path / "AAPL.jsonl").write_text('{"date": "2026-01-01", "iv": 0.20}\n')
        (tmp_path / "NVDA.jsonl").write_text('{"date": "2026-01-01", "iv": 0.45}\n')
        assert load_iv_series("AAPL", history_dir=tmp_path) == [0.20]
        assert load_iv_series("NVDA", history_dir=tmp_path) == [0.45]


class TestAppendIvSample:
    def test_creates_file_if_not_exists(self, tmp_path):
        append_iv_sample("AAPL", 0.25, history_dir=tmp_path)
        assert (tmp_path / "AAPL.jsonl").exists()

    def test_creates_history_dir_if_missing(self, tmp_path):
        subdir = tmp_path / "nested" / "iv_history"
        append_iv_sample("AAPL", 0.25, history_dir=subdir)
        assert subdir.exists()
        assert (subdir / "AAPL.jsonl").exists()

    def test_written_entry_is_valid_json(self, tmp_path):
        append_iv_sample("NVDA", 0.44, history_dir=tmp_path)
        line = (tmp_path / "NVDA.jsonl").read_text().strip()
        entry = json.loads(line)
        assert "date" in entry
        assert "iv" in entry

    def test_iv_is_rounded_to_4_decimal_places(self, tmp_path):
        append_iv_sample("MSFT", 0.123456789, history_dir=tmp_path)
        line = (tmp_path / "MSFT.jsonl").read_text().strip()
        assert json.loads(line)["iv"] == 0.1235

    def test_appends_multiple_entries(self, tmp_path):
        append_iv_sample("AAPL", 0.20, history_dir=tmp_path)
        append_iv_sample("AAPL", 0.25, history_dir=tmp_path)
        append_iv_sample("AAPL", 0.30, history_dir=tmp_path)
        result = load_iv_series("AAPL", history_dir=tmp_path)
        assert result == [0.20, 0.25, 0.30]

    def test_roundtrip_preserves_value(self, tmp_path):
        append_iv_sample("TSLA", 0.6123, history_dir=tmp_path)
        result = load_iv_series("TSLA", history_dir=tmp_path)
        assert result == [0.6123]
