"""
test_wal.py
-----------
Unit tests for the binary Write-Ahead Log (WALWriter and WALReader),
including torn-write detection, CRC32 verification, and fsync modes.
"""

from __future__ import annotations

import os
import pytest
from src.persistence.wal import (
    HEADER_SIZE,
    WALReader,
    WALRecord,
    WALWriter,
    WalEventType,
)


def test_wal_roundtrip_single_record(tmp_path):
    """Write a single record and verify exact deserialization."""
    wal_file = tmp_path / "test.wal"
    payload = {"order_id": 1, "symbol": "AAPL", "side": "BUY", "price": 15000, "qty": 100}

    with WALWriter(wal_file, fsync_mode="always") as writer:
        writer.write_record(seq=1, event_type=WalEventType.NEW_ORDER, payload=payload)

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 1
    assert records[0].seq == 1
    assert records[0].event_type == WalEventType.NEW_ORDER
    assert records[0].payload == payload
    assert not reader.torn_write_detected


def test_wal_roundtrip_multiple_records(tmp_path):
    """Write multiple records of different event types and verify sequence ordering."""
    wal_file = tmp_path / "test.wal"

    with WALWriter(wal_file, fsync_mode="batch", batch_size=10) as writer:
        for i in range(1, 101):
            writer.write_record(
                seq=i,
                event_type=WalEventType.NEW_ORDER if i % 2 == 1 else WalEventType.CANCEL,
                payload={"order_id": i, "price": 10000 + i},
            )

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 100
    for idx, rec in enumerate(records, start=1):
        assert rec.seq == idx
        assert rec.payload["order_id"] == idx
    assert not reader.torn_write_detected


def test_wal_truncated_header_reports_torn_write(tmp_path):
    """A file cut off in the middle of a header is flagged as a torn write."""
    wal_file = tmp_path / "torn_header.wal"

    with WALWriter(wal_file, fsync_mode="always") as writer:
        writer.write_record(seq=1, event_type=WalEventType.NEW_ORDER, payload={"a": 1})

    # Append 5 bytes of an incomplete header (less than 12 bytes)
    with open(wal_file, "ab") as f:
        f.write(b"WAL1\x00")

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 1  # 1st record valid
    assert reader.torn_write_detected
    assert "truncated header" in reader.error_message


def test_wal_truncated_body_reports_torn_write(tmp_path):
    """A file with a valid header but truncated body bytes is flagged as a torn write."""
    wal_file = tmp_path / "torn_body.wal"

    with WALWriter(wal_file, fsync_mode="always") as writer:
        writer.write_record(seq=1, event_type=WalEventType.NEW_ORDER, payload={"test": "ok"})

    # Truncate the file by 5 bytes
    size = os.path.getsize(wal_file)
    with open(wal_file, "r+b") as f:
        f.truncate(size - 5)

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 0
    assert reader.torn_write_detected
    assert "truncated body" in reader.error_message


def test_wal_crc_mismatch_reports_corruption(tmp_path):
    """Corrupting a byte in the payload is caught by the CRC32 check."""
    wal_file = tmp_path / "corrupt_crc.wal"

    with WALWriter(wal_file, fsync_mode="always") as writer:
        writer.write_record(seq=1, event_type=WalEventType.NEW_ORDER, payload={"price": 10000})

    # Flip a byte in the body (after 12-byte header)
    with open(wal_file, "r+b") as f:
        f.seek(HEADER_SIZE + 4)
        corrupted_byte = bytes([f.read(1)[0] ^ 0xFF])
        f.seek(HEADER_SIZE + 4)
        f.write(corrupted_byte)

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 0
    assert reader.torn_write_detected
    assert "CRC mismatch" in reader.error_message


def test_wal_invalid_magic_bytes_reports_corruption(tmp_path):
    """Invalid magic bytes halt reading and report corruption."""
    wal_file = tmp_path / "bad_magic.wal"
    with open(wal_file, "wb") as f:
        f.write(b"BAD!\x00\x00\x00\x05\x00\x00\x00\x0012345")

    reader = WALReader(wal_file)
    records = reader.read_all()

    assert len(records) == 0
    assert reader.torn_write_detected
    assert "invalid magic" in reader.error_message


def test_wal_fsync_modes_validation():
    """Invalid fsync mode raises ValueError."""
    with pytest.raises(ValueError, match="Invalid fsync_mode"):
        WALWriter("dummy.wal", fsync_mode="async")

