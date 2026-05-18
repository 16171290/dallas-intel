"""
re:SearchTX session acquisition via Playwright.

Phase 8 PR 4.1. The sole responsibility of this module is to log in to
Tyler's Odyssey Identity Provider and return a usable set of session
cookies for ``research.txcourts.gov``. Probate-data concerns are
elsewhere (scraper/probate.py); this module knows nothing about probate.

Why Playwright instead of a raw HTTP login client:
    The Tyler login flow is a Microsoft WS-Federation redirect chain
    (research.txcourts.gov -> texas.tylertech.cloud/idp -> back to
    research.txcourts.gov/auth/ofs -> dashboard). Each hop sets cookies,
    requires the previous hop's anti-CSRF token, and depends on browser
    JavaScript executing. Recreating that chain with ``requests`` is
    fragile; driving it with a real browser is straightforward and
    self-healing if Tyler tweaks the flow.

Architecture:
    Strategy B from Phase 4.A recon: Playwright runs in GHA, logs in
    fresh on each cron run, hands cookies to the rest of the pipeline.
    Login is fast (~10-15 seconds end-to-end) so per-run cost is low.

Soft-fail discipline:
    Every failure path returns ``None`` from ``acquire_session``. The
    caller (probate.py) treats ``None`` the same as "probate disabled":
    skip the stage, log a warning, foreclosure pipeline continues.

Authentication contract verified Phase 4.A:
    Login URL:    https://research.txcourts.gov/CourtRecordsSearch/
                  (auto-redirects to Tyler IdP)
    IdP host:     texas.tylertech.cloud
    Form fields:  input#UserName / input#Password
    Submit:       button#sign-in-btn
    CSRF:         __RequestVerificationToken (hidden field, auto-handled
                  by browser form-submit)
    Success URL:  research.txcourts.gov/CourtRecordsSearch/ui/dashboard
    Failure:      same login URL re-renders with validation-summary text
    MFA:          none required for this account
    CAPTCHA:      none observed
    Bot defense:  no Cloudflare bot-management headers observed

Cookies produced by successful login (any of these may carry value;
all returned so downstream Cookie header is complete):
    FedAuth, FedAuth1      - WS-Federation security context token (split)
    RSCH_JWT               - Tyler-issued JWT with subscription claims
    sess_loggedIn          - bool flag
    sess_lastActivity      - ISO timestamp
    IdSvr.WsFedTracking    - identity-server tracking
    _ga, _ga_*             - analytics (kept for fingerprint consistency)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


# ============================================================================
# Public constants - selectors and URLs verified Phase 4.A
# ============================================================================

ENTRY_URL = "https://research.txcourts.gov/CourtRecordsSearch/"
DASHBOARD_URL_GLOB = "**/CourtRecordsSearch/ui/dashboard**"
LOGIN_URL_FRAGMENT = "/idp/account/signin"

# Sign-in trigger on the public landing page (research.txcourts.gov/CourtRecordsSearch/ui/Home).
# Clicking this redirects through WS-Fed to the Tyler IdP login form.
# Landing-page Sign In triggers (Phase 4.A.2 recon).
# The landing page is Angular 21 + Tyler Forge Web Components. Sign In
# buttons are <forge-button> custom elements, not <a> or <button>, and
# clicks are JS-driven (no href). Three triggers exist with stable IDs:
#   #signInLink         - header (desktop variant; duplicate mobile copy)
#   #topSignInButton    - hero "Sign in with eFileTexas Account"
#   #bottomSignInButton - footer "Sign In"
# Primary uses signInLink (above-the-fold, no scroll); fallbacks for
# resilience if Tyler renames it.
SELECTORS_HOME_SIGNIN = (
    "forge-button#signInLink",
    "forge-button#topSignInButton",
    "forge-button#bottomSignInButton",
    'forge-button[id*="ignIn"]',
)

# Substring of the auth-claims XHR URL. Waiting for this response is our
# hydration barrier: it fires after Angular has booted and Forge components
# have upgraded, which is when click handlers become bound.
AUTH_CLAIMS_URL_FRAGMENT = "/api/auth/claims"

# IdP form selectors (plain HTML at texas.tylertech.cloud/idp/account/signin)
SELECTOR_EMAIL  = "input#UserName"
SELECTOR_PASS   = "input#Password"
SELECTOR_SUBMIT = "button#sign-in-btn"
SELECTOR_ERROR  = "#validation-summary-alert"

# Browser tuning
NAV_TIMEOUT_MS    = 30_000   # navigation steps
LOGIN_TIMEOUT_MS  = 30_000   # form-submit -> dashboard redirect
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


# ============================================================================
# Public API
# ============================================================================


def acquire_session() -> Optional[dict[str, str]]:
    """Log in to re:SearchTX via Playwright and return session cookies.

    Returns:
        Dict ``{cookie_name: cookie_value}`` for the research.txcourts.gov
        domain on success.

        ``None`` on any failure (credentials missing, Playwright crash,
        login rejected, redirect didn't land on dashboard, etc.).

    Never raises. Caller should check for ``None`` and soft-fail.
    """
    email    = os.environ.get(config.PROBATE_EMAIL_ENV, "").strip()
    password = os.environ.get(config.PROBATE_PASSWORD_ENV, "").strip()

    if not email or not password:
        logger.warning(
            "Probate auth: %s / %s env vars not set; cannot log in",
            config.PROBATE_EMAIL_ENV, config.PROBATE_PASSWORD_ENV,
        )
        return None

    # Lazy-import so unit tests can patch playwright without needing it
    # installed for the import itself
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as exc:
        logger.error("Probate auth: playwright not available: %s", exc)
        return None

    started = time.time()
    logger.info("Probate auth: launching Playwright Chromium")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                page = context.new_page()
                page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                page.set_default_timeout(NAV_TIMEOUT_MS)

                # Step 1: navigate to entry URL (302 chain -> /ui/Home)
                logger.info("Probate auth: navigating to entry URL")
                page.goto(ENTRY_URL)

                # Step 2: hydration barrier - wait for network to idle.
                # Forge Web Components upgrade after their JS chunks load.
                # Waiting for networkidle ensures: chunks loaded, components
                # upgraded, click handlers bound, page is interactive.
                # Long-poll widgets (Zendesk, analytics) may prevent true
                # idle, so we use a generous timeout but treat timeout as
                # non-fatal - the page is likely interactive enough to click.
                logger.info("Probate auth: waiting for page to reach networkidle")
                try:
                    page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
                    logger.info("Probate auth: networkidle reached")
                except PWTimeout:
                    logger.warning(
                        "Probate auth: networkidle not reached within %ds; "
                        "proceeding anyway (long-poll widgets common)",
                        NAV_TIMEOUT_MS // 1000,
                    )

                # Step 3: click Sign In with fallback chain
                signin_selector_used = None
                for candidate in SELECTORS_HOME_SIGNIN:
                    try:
                        page.wait_for_selector(candidate, state="visible", timeout=5_000)
                        page.click(candidate)
                        signin_selector_used = candidate
                        break
                    except PWTimeout:
                        continue
                if signin_selector_used is None:
                    logger.error(
                        "Probate auth: no Sign In selector matched. Tried: %s",
                        list(SELECTORS_HOME_SIGNIN),
                    )
                    return None
                logger.info("Probate auth: clicked %s", signin_selector_used)

                # Step 4: wait for WS-Fed redirect to IdP form
                logger.info("Probate auth: waiting for redirect to IdP")
                try:
                    page.wait_for_url(
                        f"**{LOGIN_URL_FRAGMENT}**",
                        timeout=NAV_TIMEOUT_MS,
                    )
                except PWTimeout:
                    logger.error(
                        "Probate auth: click did not trigger redirect. URL=%s",
                        page.url,
                    )
                    return None
                page.wait_for_selector(SELECTOR_EMAIL, state="visible", timeout=NAV_TIMEOUT_MS)

                # Step 5: fill credentials and submit
                logger.info("Probate auth: submitting credentials")
                page.fill(SELECTOR_EMAIL, email)
                page.fill(SELECTOR_PASS, password)
                page.click(SELECTOR_SUBMIT)

                # Step 6: wait for post-auth landing (dashboard OR /ui/Home)
                try:
                    page.wait_for_url(
                        lambda url: (
                            "/CourtRecordsSearch/ui/dashboard" in url
                            or url.rstrip("/").endswith("/CourtRecordsSearch/ui/Home")
                        ),
                        timeout=LOGIN_TIMEOUT_MS,
                    )
                except PWTimeout:
                    final_url = page.url
                    if LOGIN_URL_FRAGMENT in final_url:
                        err_text = _extract_error_text(page)
                        logger.error(
                            "Probate auth: login rejected by IdP. error=%r",
                            err_text or "(no validation message visible)",
                        )
                    else:
                        logger.error(
                            "Probate auth: unexpected post-login URL: %s",
                            final_url,
                        )
                    return None
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    pass

                # Step 4: extract cookies for research.txcourts.gov
                all_cookies = context.cookies()
                cookies = _filter_research_cookies(all_cookies)
                if not cookies:
                    logger.error(
                        "Probate auth: logged in but no research.txcourts "
                        "cookies present in browser context"
                    )
                    return None

                elapsed = time.time() - started
                logger.info(
                    "Probate auth: success in %.1fs (%d cookies captured)",
                    elapsed, len(cookies),
                )
                return cookies

            finally:
                browser.close()

    except Exception as exc:
        logger.exception("Probate auth: Playwright crashed: %s", exc)
        return None


def cookies_to_header(cookies: dict[str, str]) -> str:
    """Format a cookie dict as a single Cookie header value.

    Output format: ``name1=value1; name2=value2; ...``
    Suitable for direct use as the ``Cookie`` HTTP request header.
    """
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ============================================================================
# Internal helpers
# ============================================================================


def _filter_research_cookies(all_cookies: list[dict]) -> dict[str, str]:
    """Pick the cookies that re:SearchTX needs from a full Playwright dump.

    Playwright's ``context.cookies()`` returns every cookie set during the
    session across every domain visited. We want the ones whose domain is
    ``research.txcourts.gov`` (or a subdomain match), since those are
    what Tyler's API server will see.

    We don't enumerate cookie names because Tyler's set can drift and
    sending extra cookies is harmless; we just need everything for the
    research.txcourts.gov domain.
    """
    out: dict[str, str] = {}
    for c in all_cookies:
        domain = c.get("domain") or ""
        # ".research.txcourts.gov" matches; "research.txcourts.gov" matches;
        # other domains (tylertech.cloud, etc) skipped
        if "research.txcourts.gov" in domain:
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                out[name] = value
    return out


def _extract_error_text(page) -> str:
    """Return the validation-summary text if present, else empty string.

    Used only when login appears to have failed (still on /idp/account/signin
    after timeout). Best-effort; never raises.
    """
    try:
        element = page.query_selector(SELECTOR_ERROR)
        if element:
            text = element.inner_text().strip()
            return text
    except Exception:
        pass
    return ""
