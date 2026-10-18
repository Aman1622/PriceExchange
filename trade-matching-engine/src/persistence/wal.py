"""
wal.py
------
Append-only binary Write-Ahead Log (WAL) with CRC32 checksums, configurable
fsync modes, and torn-write detection.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import zlib
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Union

logger = logging.getLogger(__name__)

# ── Binary Framing Constants ──────────────────────────────────────────────────
# Header layout:
#   magic:      4 bytes (ASCII "WAL1")
#   length:     4 bytes unsigned int (byte length of body)
#   crc32:      4 bytes unsigned int (CRC32 checksum of body)
# Total header size: 12 bytes
WAL_MAGIC = b"WAL1"
HEADER_STRUCT = struct.Struct(">4sII")
HEADER_SIZE = HEADER_STRUCT.size  # 12 bytes

# Body layout:
#   seq:        8 bytes unsigned int (monotonic sequence number)
#   event_type: 2 bytes unsigned int (WalEventType code)
#   payload:    N bytes (UTF-8 JSON string)
BODY_HEADER_STRUCT = struct.Struct(">QH")
BODY_HEADER_SIZE = BODY_HEADER_STRUCT.size  # 10 bytes


class WalEventType(IntEnum):
    """Enumeration of event types written to the WAL."""
    NEW_ORDER = 1
    CANCEL = 2
    MODIFY = 3
    SNAPSHOT = 4


class WALRecord(NamedTuple):
    """Parsed in-memory representation of a WAL entry."""
    seq: int
    event_type: WalEventType
    payload: Dict[str, Any]


class WALWriter:
    """Append-only binary Write-Ahead Log writer.

    Writes events to disk BEFORE the matching engine mutates in-memory state.

    Attributes:
        filepath: Path to the .wal file.
        fsync_mode: 'always' (fsync per record) or 'batch' (fsync every N records).
        batch_size: Number of records between fsync calls when fsync_mode='batch'.

    Time Complexity:
        write_record ('batch'):  O(1) memory copy to OS page cache.
        write_record ('always'): O(Disk_I/O), blocking until disk platter/SSD commits.

    Why it is designed this way:
        1. Write-Ahead principle: Writing and syncing before state mutation guarantees
           crash-recoverability without relying on volatile RAM.
        2. CRC32 + Length Framing: Detects incomplete disk writes (torn writes) caused
           by sudden power loss or kernel panic.
        3. Configurable fsync: Exposes the fundamental durability vs throughput trade-off.
           'always' offers maximum durability (~1-5k ops/sec); 'batch' amortizes disk sync
           latency (~100k+ ops/sec) with a bounded crash loss window.
    """

    def __init__(
        self,
        filepath: Union[str, Path],
        fsync_mode: str = "always",
        batch_size: int = 100,
    ) -> None:
        if fsync_mode not in ("always", "batch"):
            raise ValueError(f"Invalid fsync_mode: {fsync_mode!r}. Expected 'always' or 'batch'.")
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.fsync_mode = fsync_mode
        self.batch_size = batch_size
        self._file = open(self.filepath, "ab")
        self._unflushed_count = 0

    def write_record(
        self,
        seq: int,
        event_type: Union[int, WalEventType],
        payload: Dict[str, Any],
    ) -> None:
        """Serialise and append a record to the WAL file.

        Time Complexity:
            'batch': O(M) serialisation time (M = payload size).
            'always': O(M) + O(fsync latency) (~0.5ms - 5ms).
        """
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        event_type_int = int(event_type)

        # Pack body: seq (8B) + event_type (2B) + payload_bytes
        body = BODY_HEADER_STRUCT.pack(seq, event_type_int) + payload_bytes

        # Calculate CRC32 over the entire body (detects torn writes / bit flips)
        crc = zlib.crc32(body) & 0xFFFFFFFF
        header = HEADER_STRUCT.pack(WAL_MAGIC, len(body), crc)

        # Atomic append to user space buffer
        self._file.write(header + body)

        if self.fsync_mode == "always":
            # Force OS to flush buffer cache directly to physical storage
            self._file.flush()
            os.fsync(self._file.fileno())
        else:
            self._unflushed_count += 1
            if self._unflushed_count >= self.batch_size:
                self.flush()

    def flush(self) -> None:
        """Force pending buffered records to disk platter / SSD via fsync."""
        self._file.flush()
        os.fsync(self._file.fileno())
        self._unflushed_count = 0

    def close(self) -> None:
        """Flush pending writes and close the file descriptor."""
        if not self._file.closed:
            self.flush()
            self._file.close()

    def __enter__(self) -> WALWriter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class WALReader:
    """Sequential binary WAL reader with torn-write and corruption detection.

    Time Complexity:
        O(N) sequential streaming read where N is total WAL byte size.

    Why it is designed this way:
        In real crashes, power fails mid-write. The operating system may flush
        the header but not the full body (torn write), or write garbled bytes.
        The reader validates magic bytes and CRC32 at each step. At the first
        sign of truncation or corruption, it halts replay cleanly and reports
        the failure instead of deserialising garbage state into the engine.
    """

    def __init__(self, filepath: Union[str, Path]) -> None:
        self.filepath = Path(filepath)
        self.torn_write_detected: bool = False
        self.error_message: Optional[str] = None

    def __iter__(self) -> Iterator[WALRecord]:
        """Stream valid records until EOF or first torn write."""
        if not self.filepath.exists():
            return

        with open(self.filepath, "rb") as f:
            offset = 0
            while True:
                header_bytes = f.read(HEADER_SIZE)
                if not header_bytes:
                    break  # Clean EOF

                if len(header_bytes) < HEADER_SIZE:
                    self.torn_write_detected = True
                    self.error_message = (
                        f"Torn write at offset {offset}: truncated header "
                        f"({len(header_bytes)}/{HEADER_SIZE} bytes)"
                    )
                    logger.warning(self.error_message)
                    break

                magic, length, expected_crc = HEADER_STRUCT.unpack(header_bytes)
                if magic != WAL_MAGIC:
                    self.torn_write_detected = True
                    self.error_message = (
                        f"Corrupt record at offset {offset}: invalid magic {magic!r}"
                    )
                    logger.warning(self.error_message)
                    break

                body = f.read(length)
                if len(body) < length:
                    self.torn_write_detected = True
                    self.error_message = (
                        f"Torn write at offset {offset}: truncated body "
                        f"({len(body)}/{length} bytes)"
                    )
                    logger.warning(self.error_message)
                    break

                # Verify checksum over the retrieved body
                actual_crc = zlib.crc32(body) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    self.torn_write_detected = True
                    self.error_message = (
                        f"Corrupt record at offset {offset}: CRC mismatch "
                        f"(expected {expected_crc}, got {actual_crc})"
                    )
                    logger.warning(self.error_message)
                    break

                # Unpack sequence number, event type, and JSON payload
                seq, event_type_int = BODY_HEADER_STRUCT.unpack(body[:BODY_HEADER_SIZE])
                payload_bytes = body[BODY_HEADER_SIZE:]
                try:
                    payload = json.loads(payload_bytes.decode("utf-8"))
                except Exception as err:
                    self.torn_write_detected = True
                    self.error_message = f"Corrupt payload at offset {offset}: {err}"
                    break

                offset += HEADER_SIZE + length
                yield WALRecord(
                    seq=seq,
                    event_type=WalEventType(event_type_int),
                    payload=payload,
                )

    def read_all(self) -> List[WALRecord]:
        """Convenience method to collect all valid records into a list."""
        return list(self)

