"""
main.py
-------
Entry point for the trade matching engine daemon.

Wires together:
  - Matching engine
  - Write-Ahead Log (WAL) and snapshot persistence
  - Background WebSocket fan-out server
  - Graceful signal handling and shutdown checkpoints
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.engine.events import Trade
from src.engine.matching_engine import MatchingEngine
from src.network.websocket_server import WebSocketServer
from src.persistence.snapshot import SnapshotManager, recover
from src.persistence.wal import WALWriter

logger = logging.getLogger("main")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for engine configuration."""
    parser = argparse.ArgumentParser(
        prog="trade-matching-engine",
        description="High-performance in-memory matching engine with WAL persistence.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="WebSocket server bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="WebSocket server port (default: 8765)",
    )
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory for WAL files and snapshots (default: ./data)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["AAPL", "MSFT", "GOOG"],
        help="Trading symbols to initialize at startup",
    )
    parser.add_argument(
        "--fsync-mode",
        choices=["always", "batch"],
        default="batch",
        help="WAL durability mode: 'always' (strict fsync) or 'batch' (high throughput)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser


def configure_logging(level: str) -> None:
    """Configure structured logging output format."""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


class _ShutdownHandler:
    """Catches OS signals (SIGINT, SIGTERM) to trigger graceful engine shutdown."""

    def __init__(self) -> None:
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        logger.info("Shutdown signal %d received, commencing teardown...", signum)
        self.shutdown_requested = True


def initialize_engine(
    data_dir: Path,
    symbols: List[str],
    fsync_mode: str,
) -> Tuple[MatchingEngine, WALWriter, SnapshotManager]:
    """Recover state or bootstrap fresh engine with active WAL writer."""
    data_dir.mkdir(parents=True, exist_ok=True)
    wal_path = data_dir / "engine.wal"

    # Attempt crash recovery if previous snapshot or WAL exists
    if (data_dir / "snapshot.json").exists() or wal_path.exists():
        logger.info("Existing persistence data found in %s; recovering state...", data_dir)
        engine, replayed_count, _ = recover(data_dir)
        logger.info(
            "Recovery complete: replayed %d events, current seq=%d",
            replayed_count,
            engine.current_seq,
        )
    else:
        logger.info("No prior persistence files found; starting fresh engine.")
        engine = MatchingEngine(wal_writer=None)

    for symbol in symbols:
        engine.create_book(symbol)

    # Attach active append-only WAL writer for new live traffic
    wal_writer = WALWriter(wal_path, fsync_mode=fsync_mode, batch_size=100)
    engine.wal_writer = wal_writer
    snap_mgr = SnapshotManager(data_dir)

    return engine, wal_writer, snap_mgr


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    configure_logging(args.log_level)
    logger.info("Initializing Trade Matching Engine daemon (symbols=%s)", args.symbols)

    data_dir = Path(args.data_dir)
    engine, wal_writer, snap_mgr = initialize_engine(data_dir, args.symbols, args.fsync_mode)

    # Queue bridging matching engine thread and WebSocket fan-out server
    event_queue: queue.Queue = queue.Queue(maxsize=0)
    ws_server = WebSocketServer(event_queue=event_queue, host=args.host, port=args.port)
    ws_server.start()
    logger.info("WebSocket server listening on ws://%s:%d", args.host, args.port)

    shutdown = _ShutdownHandler()
    logger.info("Engine running. Press Ctrl+C to terminate cleanly.")

    try:
        last_snapshot_time = time.time()
        while not shutdown.shutdown_requested:
            # Heartbeat / maintenance loop (periodic snapshot every 60s)
            now = time.time()
            if now - last_snapshot_time >= 60.0:
                snap_mgr.save_snapshot(engine)
                last_snapshot_time = now

            time.sleep(0.2)
    finally:
        logger.info("Initiating graceful shutdown...")
        ws_server.stop()

        # Flush pending WAL buffers to disk
        wal_writer.flush()
        wal_writer.close()

        # Persist final checkpoint snapshot of resting order book state
        snapshot_path = snap_mgr.save_snapshot(engine)
        logger.info("Final state saved to %s (seq=%d). Exit complete.", snapshot_path, engine.current_seq)

    return 0


if __name__ == "__main__":
    sys.exit(main())
