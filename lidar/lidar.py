"""
LiDAR Intrusion Monitor — LIVE Node
=====================================
1. Builds a distance baseline from the first N scans (keep area clear).
2. Continuously monitors for sudden drops in mean distance.
3. On confirmed intrusion → stops PostgreSQL service ONCE.
4. Keeps monitoring forever so the next intrusion is also caught
   (after you manually resolve + restart Postgres whenever ready).

TUNING (top of file):
  BASELINE_SCANS      — scans averaged for the baseline (keep area clear)
  INTRUSION_THRESHOLD — mm drop that counts as a hit
  TRIGGER_COUNT       — consecutive hits required before action fires
"""

from rplidar import RPLidar
import numpy as np
import subprocess
import logging
import time

# ─── Configuration ────────────────────────────────────────────────────────────

PORT                = 'COM3'
BAUDRATE            = 115200

BASELINE_SCANS      = 10     # number of clean scans to average for baseline
INTRUSION_THRESHOLD = 300    # mm — mean distance must drop by this much
TRIGGER_COUNT       = 3      # consecutive anomalous scans before action fires

PG_SERVICE          = "postgresql-x64-18"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger()

# ─── PostgreSQL stop ──────────────────────────────────────────────────────────

def stop_postgres():
    log.critical("🚨 INTRUSION DETECTED — stopping PostgreSQL service...")
    result = subprocess.run(
        f'net stop "{PG_SERVICE}"',
        shell=True, capture_output=True, text=True
    )
    if result.returncode == 0:
        log.critical("✅ PostgreSQL stopped. Resolve the intrusion, then start the service manually.")
    else:
        log.error(f"❌ Could not stop PostgreSQL: {result.stderr.strip()}")
        log.error("Make sure you are running this script as Administrator.")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def mean_distance(scan):
    distances = [d for (_, _, d) in scan if d > 0]
    return float(np.mean(distances)) if distances else 0.0

def safe_scan_loop(lidar):
    """
    Wrapper around iter_scans() that silently restarts on buffer-overflow
    warnings instead of crashing. Yields scans as normal.
    """
    while True:
        try:
            for scan in lidar.iter_scans():
                yield scan
        except Exception as e:
            msg = str(e).lower()
            if "buffer" in msg or "too many bytes" in msg:
                log.debug("Buffer overflow — restarting scan loop.")
                try:
                    lidar.stop()
                except Exception:
                    pass
                time.sleep(0.2)
                # restart scanning
                try:
                    lidar.start_motor()
                except Exception:
                    pass
                # brief pause to let buffer drain
                time.sleep(0.5)
            else:
                raise   # unexpected error — let it bubble up

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Connecting to LiDAR on %s ...", PORT)
    lidar = RPLidar(PORT, baudrate=BAUDRATE)

    baseline         = None
    baseline_buf     = []
    consecutive_hits = 0
    postgres_stopped = False   # only stop the service once per intrusion event

    try:
        for scan in safe_scan_loop(lidar):

            dist = mean_distance(scan)
            if dist == 0:
                continue

            # ── Phase 1: build baseline ───────────────────────────────────────
            if baseline is None:
                baseline_buf.append(dist)
                log.info(
                    "Baseline scan %d/%d — mean: %.0f mm",
                    len(baseline_buf), BASELINE_SCANS, dist
                )
                if len(baseline_buf) >= BASELINE_SCANS:
                    baseline = float(np.mean(baseline_buf))
                    log.info("=" * 60)
                    log.info("✅ Baseline: %.0f mm", baseline)
                    log.info(
                        "Monitoring — threshold: drop > %d mm over %d consecutive scans",
                        INTRUSION_THRESHOLD, TRIGGER_COUNT
                    )
                    log.info("=" * 60)
                continue

            # ── Phase 2: monitor ──────────────────────────────────────────────
            drop = baseline - dist   # positive → objects closer than baseline

            if drop > INTRUSION_THRESHOLD:
                consecutive_hits += 1
                log.warning(
                    "⚠️  Anomalous scan %d/%d — mean: %.0f mm  drop: %.0f mm",
                    consecutive_hits, TRIGGER_COUNT, dist, drop
                )
            else:
                if consecutive_hits > 0:
                    log.info("Environment back to normal. Counter reset.")
                consecutive_hits = 0

                # Once the intrusion clears, allow the next intrusion event
                # to stop Postgres again (in case it was manually restarted).
                if postgres_stopped:
                    log.info(
                        "Intrusion cleared. Will trigger again if Postgres "
                        "is restarted and another intrusion occurs."
                    )
                    postgres_stopped = False

            # ── Fire once when threshold crossed ─────────────────────────────
            if consecutive_hits >= TRIGGER_COUNT and not postgres_stopped:
                postgres_stopped = True
                stop_postgres()
                consecutive_hits = 0   # reset so monitor stays clean

    except KeyboardInterrupt:
        log.info("Stopped by user.")

    finally:
        try:
            lidar.stop()
            lidar.disconnect()
        except Exception:
            pass
        log.info("LiDAR disconnected.")


if __name__ == "__main__":
    main()