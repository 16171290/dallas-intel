"""Standalone probate-auth probe.

Runs only scraper.probate_auth.acquire_session() — does NOT run the full
pipeline. Use to isolate authentication failures from foreclosure /
publicsearch / DCAD concerns.

Usage (PowerShell, Windows)
---------------------------
    cd <repo>
    .\.venv\Scripts\Activate.ps1          # if you have a venv; else skip
    $env:RESEARCH_TX_EMAIL    = "you@example.com"
    $env:RESEARCH_TX_PASSWORD = "your-password"
    python probe_probate_auth.py

On failure, see data/probes/probate_signin_timeout_<ts>.{png,html} plus
the 'Probate diagnostics:' log lines printed to the terminal.
"""

import logging
import os
import sys

from scraper import probate_auth, config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger = logging.getLogger("probe_probate_auth")

    email    = os.environ.get(config.PROBATE_EMAIL_ENV, "").strip()
    password = os.environ.get(config.PROBATE_PASSWORD_ENV, "").strip()
    if not email:
        logger.error(
            "%s is not set in the environment. Set it before running this probe.",
            config.PROBATE_EMAIL_ENV,
        )
        return 2
    if not password:
        logger.error(
            "%s is not set in the environment. Set it before running this probe.",
            config.PROBATE_PASSWORD_ENV,
        )
        return 2

    logger.info("Email length: %d  Password length: %d (just confirming they're set)",
                len(email), len(password))

    cookies = probate_auth.acquire_session()

    if cookies is None:
        logger.error("RESULT: acquire_session() returned None (login failed).")
        logger.error("Check the 'Probate diagnostics:' lines above and the")
        logger.error("data/probes/probate_signin_timeout_*.{png,html} artifacts.")
        return 1

    logger.info("RESULT: success. Captured %d cookies: %s",
                len(cookies), sorted(cookies.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
