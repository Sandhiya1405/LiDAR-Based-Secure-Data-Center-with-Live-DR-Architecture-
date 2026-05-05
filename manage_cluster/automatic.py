"""
PostgreSQL HA Cluster Manager
==============================
Run this script on BOTH machines simultaneously.

Normal state   : LIVE (10.110.173.246) = PRIMARY  |  DR (10.110.173.162) = STANDBY
After failover : LIVE = offline / standby         |  DR = PRIMARY
After failback : LIVE = STANDBY                   |  DR = PRIMARY  (LIVE rejoins cleanly)

KEY DESIGN:
  - Script detects the CURRENT actual PostgreSQL role on startup every time.
  - It does NOT assume roles from IP address.
  - Stopping and restarting app.py (or this script) will resume from the
    real current cluster state — it never resets back to "original" roles.

MANUAL INTERVENTION REQUIRED:
  - When a node's PostgreSQL service is stopped (for any reason), it stays DOWN.
  - The script will NOT automatically start the service or trigger pg_rewind.
  - You must manually run: net start postgresql-x64-18
  - Once the service is up, the script detects the role and rejoins as STANDBY
    automatically (running pg_rewind if needed).
"""

import subprocess
import time
import os
import socket
import logging

# ─── Configuration ────────────────────────────────────────────────────────────

PRIMARY_IP         = "10.110.173.246"   # Original / intended primary (LIVE)
DR_IP              = "10.110.173.162"   # DR node

PG_PORT            = 5432
PG_VERSION         = "18"
PG_SERVICE         = f"postgresql-x64-{PG_VERSION}"
PG_BIN             = rf"C:\Program Files\PostgreSQL\{PG_VERSION}\bin"
PG_DATA = r"D:\pgdata"
PG_SUPERUSER       = "postgres"
PG_SUPER_PASS      = "newpassword123"

CHECK_INTERVAL     = 3    # seconds between health checks
FAILOVER_THRESHOLD = 2    # consecutive failures before promoting

PSQL               = os.path.join(PG_BIN, "psql.exe")
PG_REWIND          = os.path.join(PG_BIN, "pg_rewind.exe")
AUTO_CONF          = os.path.join(PG_DATA, "postgresql.auto.conf")
STANDBY_SIGNAL     = os.path.join(PG_DATA, "standby.signal")

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger()

# ─── Low-level helpers ────────────────────────────────────────────────────────

def run(cmd, env=None):
    env_vars = os.environ.copy()
    if env:
        env_vars.update(env)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env_vars)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def pg_env():
    return {"PGPASSWORD": PG_SUPER_PASS}

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((PRIMARY_IP, 80))
    ip = s.getsockname()[0]
    s.close()
    return ip

def can_connect(host):
    """TCP-level check — is port 5432 accepting connections on host?"""
    try:
        with socket.create_connection((host, PG_PORT), timeout=2):
            return True
    except Exception:
        return False

def is_in_recovery(host):
    """
    Returns:
        True  → node is a STANDBY  (pg_is_in_recovery = t)
        False → node is a PRIMARY  (pg_is_in_recovery = f)
        None  → could not connect / query failed
    """
    cmd = (
        f'"{PSQL}" -h {host} -U {PG_SUPERUSER} -p {PG_PORT} '
        f'-t -c "SELECT pg_is_in_recovery();"'
    )
    rc, out, _ = run(cmd, pg_env())
    if rc != 0:
        return None
    out = out.strip().lower()
    if "t" in out:
        return True
    if "f" in out:
        return False
    return None

def stop_postgres():
    log.info("Stopping PostgreSQL service...")
    run(f'net stop "{PG_SERVICE}"')
    for _ in range(20):
        if not can_connect("127.0.0.1"):
            log.info("PostgreSQL stopped.")
            return True
        time.sleep(1)
    log.warning("PostgreSQL may still be running after stop command.")
    return True   # proceed anyway

def start_postgres():
    log.info("Starting PostgreSQL service...")
    run(f'net start "{PG_SERVICE}"')
    for _ in range(30):
        if can_connect("127.0.0.1"):
            log.info("PostgreSQL is up and accepting connections.")
            return True
        time.sleep(2)
    log.error("PostgreSQL did not come up in time.")
    return False

# ─── Role detection ───────────────────────────────────────────────────────────

def detect_local_role():
    """
    Returns: "primary" | "standby" | "offline"
    """
    if not can_connect("127.0.0.1"):
        return "offline"
    result = is_in_recovery("127.0.0.1")
    if result is True:
        return "standby"
    if result is False:
        return "primary"
    return "offline"

# ─── Cluster operations ───────────────────────────────────────────────────────

def promote_to_primary():
    """Promote local standby → primary using pg_promote()."""
    log.info("Promoting this node to PRIMARY via pg_promote()...")
    cmd = f'"{PSQL}" -h 127.0.0.1 -U {PG_SUPERUSER} -t -c "SELECT pg_promote();"'
    rc, out, err = run(cmd, pg_env())
    if rc != 0:
        log.error(f"pg_promote() failed: {err}")
        return False
    for _ in range(20):
        if is_in_recovery("127.0.0.1") is False:
            log.info("Promotion confirmed — this node is now PRIMARY.")
            return True
        time.sleep(1)
    log.error("Promotion timed out — node still in recovery.")
    return False


def write_standby_config(primary_host):
    """Write primary_conninfo to postgresql.auto.conf and create standby.signal."""
    conninfo_line = (
        f"primary_conninfo = 'host={primary_host} port={PG_PORT} "
        f"user={PG_SUPERUSER} password={PG_SUPER_PASS}'\n"
    )
    try:
        if os.path.exists(AUTO_CONF):
            with open(AUTO_CONF, "r") as f:
                lines = [l for l in f if not l.strip().startswith("primary_conninfo")]
            with open(AUTO_CONF, "w") as f:
                f.writelines(lines)
                f.write(conninfo_line)
        else:
            with open(AUTO_CONF, "w") as f:
                f.write(conninfo_line)
        log.info(f"postgresql.auto.conf updated → primary = {primary_host}")
    except Exception as e:
        log.error(f"Failed to write postgresql.auto.conf: {e}")
        return False

    try:
        open(STANDBY_SIGNAL, "w").close()
        log.info("standby.signal created.")
    except Exception as e:
        log.error(f"Failed to create standby.signal: {e}")
        return False

    return True


def rejoin_as_standby(new_primary_host):
    """
    Full failback / rejoin sequence:
      1. Stop local PostgreSQL  (required by pg_rewind)
      2. Wait for new primary to be reachable
      3. Run pg_rewind to sync data directories
      4. Write standby config (postgresql.auto.conf + standby.signal)
      5. Start PostgreSQL — it will come up as a standby
    """
    log.info(f"REJOIN: configuring this node as STANDBY from {new_primary_host}...")

    # 1. Stop local PG
    stop_postgres()

    # 2. Wait for the new primary to be reachable
    log.info(f"Waiting for primary {new_primary_host} to be reachable...")
    for _ in range(40):
        if can_connect(new_primary_host):
            log.info(f"{new_primary_host} is reachable.")
            break
        time.sleep(2)
    else:
        log.error("Primary never became reachable. Aborting rejoin.")
        start_postgres()
        return False

    # 3. pg_rewind
    log.info("Running pg_rewind...")
    conn_str = (
        f"host={new_primary_host} port={PG_PORT} "
        f"user={PG_SUPERUSER} password={PG_SUPER_PASS}"
    )
    cmd = f'"{PG_REWIND}" -D "{PG_DATA}" --source-server="{conn_str}"'
    rc, out, err = run(cmd, pg_env())
    if rc != 0:
        log.error(f"pg_rewind failed:\n{err}")
        start_postgres()
        return False
    log.info("pg_rewind succeeded.")

    # 4. Write standby config
    if not write_standby_config(new_primary_host):
        log.error("Failed to configure standby. Aborting.")
        start_postgres()
        return False

    # 5. Start as standby
    if start_postgres():
        log.info(f"REJOIN COMPLETE — this node is now STANDBY, replicating from {new_primary_host}.")
        return True
    else:
        log.error("PostgreSQL failed to start after rejoin.")
        return False

# ─── Monitoring loops ─────────────────────────────────────────────────────────

def loop_as_primary(local_ip, other_ip):
    """
    Run while this node is PRIMARY.
    Exits if this node's role changes (e.g. PG stopped externally).
    """
    log.info(f"[PRIMARY] Running. Other node = {other_ip}")

    while True:
        role = detect_local_role()

        if role != "primary":
            log.warning(f"[PRIMARY] Local role changed to '{role}'. Exiting primary loop.")
            return role   # tell caller what we are now

        # Check other node for split-brain
        if can_connect(other_ip):
            other_recovery = is_in_recovery(other_ip)
            if other_recovery is False:
                log.critical(
                    f"SPLIT-BRAIN: both {local_ip} and {other_ip} report PRIMARY. "
                    f"This node will step down and do pg_rewind."
                )
                # This node steps down to resolve split-brain
                rejoin_as_standby(other_ip)
                return "standby"

        time.sleep(CHECK_INTERVAL)


def loop_as_standby(local_ip, primary_ip):
    """
    Run while this node is STANDBY, monitoring primary_ip.
    Promotes if primary goes unreachable for FAILOVER_THRESHOLD consecutive checks.
    Exits and returns new role when promotion completes.
    """
    log.info(f"[STANDBY] Running. Monitoring primary = {primary_ip}")
    failures = 0

    while True:
        # First confirm we're still standby
        role = detect_local_role()
        if role == "primary":
            log.info("[STANDBY] Local node is already PRIMARY (detected). Exiting standby loop.")
            return "primary"
        if role == "offline":
            log.warning("[STANDBY] Local PostgreSQL went offline. Waiting for manual start...")
            return "offline"

        # Monitor primary
        if can_connect(primary_ip):
            if failures > 0:
                log.info(f"Primary {primary_ip} reachable again (was unreachable {failures}x).")
            failures = 0
        else:
            failures += 1
            log.warning(
                f"[STANDBY] Primary {primary_ip} unreachable "
                f"({failures}/{FAILOVER_THRESHOLD})"
            )

            if failures >= FAILOVER_THRESHOLD:
                log.critical(
                    f"Primary {primary_ip} DOWN after {FAILOVER_THRESHOLD} checks. "
                    f"Initiating FAILOVER..."
                )
                if promote_to_primary():
                    log.info(f"[{local_ip}] FAILOVER COMPLETE — this node is now PRIMARY.")
                    return "primary"
                else:
                    log.error("Promotion failed. Resetting failure count and retrying...")
                    failures = 0

        time.sleep(CHECK_INTERVAL)


def loop_as_promoted_primary(local_ip, original_primary_ip):
    """
    This node was promoted after failover and is acting as primary.
    Monitors the original primary. When it comes back as STANDBY, logs success.
    Exits if this node's role changes unexpectedly.
    """
    log.info(
        f"[PRIMARY after failover] Watching for {original_primary_ip} to rejoin as STANDBY..."
    )
    rejoined_logged = False

    while True:
        role = detect_local_role()
        if role != "primary":
            log.warning(f"[POST-FAILOVER PRIMARY] Role changed to '{role}'. Exiting.")
            return role

        if can_connect(original_primary_ip):
            orig = is_in_recovery(original_primary_ip)
            if orig is True:
                if not rejoined_logged:
                    log.info(
                        f"Original primary {original_primary_ip} has rejoined as STANDBY. "
                        f"Cluster is fully healthy!"
                    )
                    rejoined_logged = True
            elif orig is False:
                log.warning(
                    f"{original_primary_ip} is online but reporting PRIMARY. "
                    f"pg_rewind may still be running, or split-brain risk."
                )
                rejoined_logged = False
            else:
                rejoined_logged = False
        else:
            if rejoined_logged:
                log.warning(f"Original primary {original_primary_ip} went offline again.")
                rejoined_logged = False

        time.sleep(CHECK_INTERVAL)

# ─── Main state machine ───────────────────────────────────────────────────────

def main():
    local_ip = get_local_ip()
    log.info(f"Local IP: {local_ip}")

    if local_ip not in (PRIMARY_IP, DR_IP):
        log.error(f"Unrecognised IP {local_ip}. Expected {PRIMARY_IP} or {DR_IP}.")
        return

    other_ip = DR_IP if local_ip == PRIMARY_IP else PRIMARY_IP

    log.info("=" * 60)
    log.info("Starting HA Cluster Manager")
    log.info("=" * 60)

    while True:
        # ── Always detect the REAL current role — never assume ─────────────────
        role = detect_local_role()
        log.info(f"Detected local role: {role.upper()}")

        # ──────────────────────────────────────────────────────────────────────
        # OFFLINE: local PostgreSQL is not running.
        # Do NOT auto-start or auto-rejoin. Wait for the operator to manually
        # run: net start postgresql-x64-18
        # Once the service comes up, the loop will detect the role and act.
        # ──────────────────────────────────────────────────────────────────────
        if role == "offline":
            log.info(
                "Local PostgreSQL is OFFLINE. "
                "Start the service manually (net start postgresql-x64-18) "
                "to rejoin the cluster as STANDBY."
            )
            time.sleep(CHECK_INTERVAL)

        # ──────────────────────────────────────────────────────────────────────
        # PRIMARY: this node is currently primary
        # ──────────────────────────────────────────────────────────────────────
        elif role == "primary":
            # Before entering the primary loop, check for split-brain conflict
            other_recovery = is_in_recovery(other_ip) if can_connect(other_ip) else None

            if other_recovery is False:
                # Both nodes are primary — step down to resolve
                log.critical(
                    f"CONFLICT on startup: both {local_ip} AND {other_ip} are PRIMARY. "
                    f"This node will pg_rewind and rejoin as STANDBY."
                )
                rejoin_as_standby(other_ip)
            else:
                # We are the sole primary — enter primary monitoring loop
                new_role = loop_as_primary(local_ip, other_ip)
                log.info(f"Primary loop exited. New role = {new_role}")
                # Falls through to top of while loop — will re-detect

        # ──────────────────────────────────────────────────────────────────────
        # STANDBY: this node is currently standby.
        # If we just came up manually after being a former primary,
        # pg_rewind + standby config is needed before replication can work.
        # Detect this by checking whether standby.signal is already present.
        # ──────────────────────────────────────────────────────────────────────
        elif role == "standby":
            if can_connect(other_ip) and is_in_recovery(other_ip) is False:
                current_primary_ip = other_ip

                # If standby.signal is missing, this node was a former primary
                # that was just manually started. Run pg_rewind to sync properly.
                if not os.path.exists(STANDBY_SIGNAL):
                    log.info(
                        f"No standby.signal found — this node was previously PRIMARY. "
                        f"Running pg_rewind to sync with current primary {current_primary_ip}..."
                    )
                    rejoin_as_standby(current_primary_ip)
                    time.sleep(1)
                    continue  # re-detect role after rejoin
            else:
                # Primary unreachable — monitor anyway (will trigger failover if needed)
                current_primary_ip = other_ip

            new_role = loop_as_standby(local_ip, primary_ip=current_primary_ip)
            log.info(f"Standby loop exited. New role = {new_role}")

            # If we just promoted, enter the post-failover primary loop
            if new_role == "primary":
                loop_as_promoted_primary(local_ip, original_primary_ip=other_ip)
            # Falls through to top of while loop — will re-detect

        time.sleep(1)


if __name__ == "__main__":
    main()
