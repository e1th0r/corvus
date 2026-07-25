#!/usr/bin/env python3
"""
Email Validation Milter

Author: Hector Colina <hcolina@gmail.com>
Web:    https://team360.cl

Validates recipient addresses via a check-if-email-exists API,
using Valkey/Redis as a cache layer to avoid redundant probes.

Flow:
    RCPT TO -> check cache
                |-- HIT  -> use cached result (ACCEPT / REJECT)
                +-- MISS -> call validator API -> cache result -> ACCEPT / REJECT

Configuration is entirely via environment variables (see below).
Sensible defaults are provided for local/dev use.
"""

import os
import sys
import logging
import requests
import redis
import Milter

# =============================================================================
# Configuration (all overridable via environment variables)
# =============================================================================

MILTER_NAME    = os.environ.get("MILTER_NAME", "email-validator-milter")
MILTER_SOCKET  = os.environ.get("MILTER_SOCKET", "inet:7357@0.0.0.0")
MILTER_TIMEOUT = int(os.environ.get("MILTER_TIMEOUT", "600"))  # seconds

VALIDATOR_URL     = os.environ.get("VALIDATOR_URL", "http://127.0.0.1:8080")
VALIDATOR_TIMEOUT = int(os.environ.get("VALIDATOR_TIMEOUT", "30"))  # seconds per probe

VALKEY_HOST    = os.environ.get("VALKEY_HOST", "127.0.0.1")
VALKEY_PORT    = int(os.environ.get("VALKEY_PORT", "6379"))
VALKEY_TTL     = int(os.environ.get("VALKEY_TTL", "86400"))   # 24h — cache valid results
VALKEY_TTL_NEG = int(os.environ.get("VALKEY_TTL_NEG", "3600"))  # 1h — cache negative/risky results

# Domains that bypass validation entirely (internal systems, trusted senders, etc.)
# Comma-separated list, e.g. "internal.example.com,mail.example.com"
BYPASS_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get("BYPASS_DOMAINS", "").split(",")
    if d.strip()
}

# Reachability verdicts considered deliverable
VALID_VERDICTS = {"safe", "risky"}

# Reachability verdicts that trigger rejection
REJECT_VERDICTS = {"invalid"}

LOG_FILE = os.environ.get("MILTER_LOG_FILE", "/var/log/email-validator-milter.log")
SOCKET_DIR = os.environ.get("MILTER_SOCKET_DIR", "/var/run/email-validator-milter")

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger(MILTER_NAME)

# =============================================================================
# Valkey/Redis connection (shared, thread-safe)
# =============================================================================

try:
    cache = redis.Redis(host=VALKEY_HOST, port=VALKEY_PORT, decode_responses=True)
    cache.ping()
    log.info("Cache connection OK")
except Exception as e:
    log.error(f"Cache connection failed: {e}")
    cache = None

# =============================================================================
# Validator API
# =============================================================================

def validate_email(email: str) -> dict:
    """
    Call check-if-email-exists API.
    Returns the result dict, or raises on connection/HTTP error.
    """
    response = requests.post(
        VALIDATOR_URL,
        json={"to_emails": [email]},
        timeout=VALIDATOR_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json()
    return results[0] if results else {}


def check_recipient(email: str) -> tuple[str, str]:
    """
    Validate a recipient email address.
    Returns (verdict, source) where:
        verdict : "accept" | "reject" | "tempfail"
        source  : "bypass" | "cache" | "api"
    """
    email  = email.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""

    # --- Bypass check ---
    if domain in BYPASS_DOMAINS:
        log.info(f"BYPASS {email} - internal domain")
        return "accept", "bypass"

    # --- Cache check ---
    if cache:
        try:
            cached = cache.get(f"val:{email}")
            if cached:
                log.info(f"CACHE HIT {email} -> {cached}")
                return cached, "cache"
        except Exception as e:
            log.warning(f"Cache read error for {email}: {e}")

    # --- API check ---
    try:
        result       = validate_email(email)
        is_reachable = result.get("is_reachable", "unknown")
        log.info(f"API result {email} -> is_reachable={is_reachable}")

        if is_reachable in VALID_VERDICTS:
            verdict = "accept"
            ttl     = VALKEY_TTL
        elif is_reachable in REJECT_VERDICTS:
            verdict = "reject"
            ttl     = VALKEY_TTL_NEG
        else:
            # Unknown verdict - accept cautiously, cache briefly
            log.warning(f"Unknown verdict for {email}: {is_reachable} - accepting")
            verdict = "accept"
            ttl     = VALKEY_TTL_NEG

        # Store result in cache
        if cache:
            try:
                cache.set(f"val:{email}", verdict, ex=ttl)
            except Exception as e:
                log.warning(f"Cache write error for {email}: {e}")

        return verdict, "api"

    except requests.RequestException as e:
        log.error(f"Validator API error for {email}: {e}")
        return "tempfail", "api"


# =============================================================================
# Milter class
# =============================================================================

class ValidatorMilter(Milter.Base):
    """
    Per-connection milter instance.
    Validates each RCPT TO against the validator API + cache.
    REJECT on envrcpt affects only the current recipient, not the message.
    """

    def __init__(self):
        self.id = Milter.uniqueID()

    @Milter.noreply
    def connect(self, hostname, family, hostaddr):
        return Milter.CONTINUE

    @Milter.noreply
    def envfrom(self, mailfrom, *args):
        return Milter.CONTINUE

    def envrcpt(self, recipient, *args):
        """
        Called for each RCPT TO.
        Returning REJECT here rejects only this recipient.
        """
        email = recipient.strip("<>")
        verdict, source = check_recipient(email)

        if verdict == "reject":
            log.warning(f"REJECT recipient={email} source={source}")
            self.setreply("550", "5.1.1", f"Recipient address rejected: {email}")
            return Milter.REJECT

        if verdict == "tempfail":
            log.warning(f"TEMPFAIL recipient={email} source={source}")
            self.setreply("451", "4.1.1", "Temporary validation failure, please retry")
            return Milter.TEMPFAIL

        log.info(f"ACCEPT recipient={email} source={source}")
        return Milter.CONTINUE

    @Milter.noreply
    def header(self, name, value):
        return Milter.CONTINUE

    @Milter.noreply
    def eoh(self):
        return Milter.CONTINUE

    @Milter.noreply
    def body(self, chunk):
        return Milter.CONTINUE

    def eom(self):
        return Milter.ACCEPT

    def abort(self):
        return Milter.CONTINUE

    def close(self):
        return Milter.CONTINUE


# =============================================================================
# Entry point
# =============================================================================

def main():
    # Ensure socket directory exists (only relevant if using a unix socket)
    os.makedirs(SOCKET_DIR, exist_ok=True)
    socket_path = f"{SOCKET_DIR}/validator.sock"

    # Remove stale socket if present
    if os.path.exists(socket_path):
        os.remove(socket_path)

    log.info(f"Starting {MILTER_NAME} on {MILTER_SOCKET}")

    Milter.factory = ValidatorMilter
    Milter.set_flags(Milter.ADDHDRS)
    Milter.runmilter(MILTER_NAME, MILTER_SOCKET, MILTER_TIMEOUT)

    log.info(f"{MILTER_NAME} stopped")


if __name__ == "__main__":
    main()
