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

import contextlib
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


# PR 8.3: module-level cache of full-attribute cookies from the most
# recent acquire_session() call. Read via ``get_last_session_full_cookies``.
_LAST_FULL_COOKIES: list[dict] = []


def get_last_session_full_cookies() -> list[dict]:
    """Return the full Playwright cookie list from the most recent
    successful ``acquire_session()`` call. Empty list if no session has
    been acquired this process or the last attempt failed.

    Used by ``scraper.probate_case_detail.CaseDetailFetcher`` to inject
    cookies with their full attributes into a fresh Playwright context
    (the SPA's bootstrap requires secure/sameSite/etc. preserved).
    """
    return list(_LAST_FULL_COOKIES)


def acquire_session() -> Optional[dict[str, str]]:
    """Log in to re:SearchTX via Playwright and return session cookies.

    Returns:
        Dict ``{cookie_name: cookie_value}`` for the research.txcourts.gov
        domain on success.

        ``None`` on any failure (credentials missing, Playwright crash,
        login rejected, redirect didn't land on dashboard, etc.).

    Never raises. Caller should check for ``None`` and soft-fail.
    """
    email_raw = os.environ.get(config.PROBATE_EMAIL_ENV, "")
    password  = os.environ.get(config.PROBATE_PASSWORD_ENV, "").strip()

    # Defensive sanitization (PR 12.23). The Tyler IdP rejects the
    # username with "commas not allowed" if the value contains a comma,
    # but commas are invalid in any RFC-compliant email anyway — they
    # only get here if the GHA secret was typed with extra junk
    # (trailing comma, wrapping quotes, leading whitespace, etc.).
    # Strip the obvious offenders rather than fail the run.
    email = email_raw.strip()
    for junk in (",", '"', "'", ";", " ", "\t"):
        email = email.replace(junk, "")
    if email != email_raw.strip():
        logger.warning(
            "Probate auth: %s contained %d junk char(s) "
            "(commas/quotes/semicolons/whitespace) that were stripped. "
            "Re-set the GHA secret to remove them.",
            config.PROBATE_EMAIL_ENV,
            len(email_raw.strip()) - len(email),
        )

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

                # PR 12.23 diagnostic: read the username input back from
                # the DOM post-fill and log its length + structural
                # fingerprint (no value leakage). If Tyler's form is
                # mangling input client-side, the readback won't match
                # what we passed in.
                try:
                    filled_email = page.input_value(SELECTOR_EMAIL) or ""
                    logger.info(
                        "Probate auth: username field readback "
                        "len=%d has_at=%s has_comma=%s has_space=%s "
                        "matches_input=%s",
                        len(filled_email),
                        "@" in filled_email,
                        "," in filled_email,
                        " " in filled_email,
                        filled_email == email,
                    )
                except Exception as e:
                    logger.warning("Probate auth: readback failed: %s", e)

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
                    _capture_failure_diagnostics(page, "signin_timeout")
                    return None
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    pass

                # Step 4: extract cookies for research.txcourts.gov.
                # PR 8.3: capture FULL cookie attributes too so the case-
                # detail fetcher can re-inject them into its own browser
                # context (the SPA's bootstrap auth check requires
                # secure/sameSite/etc. attributes preserved).
                all_cookies = context.cookies()
                cookies = _filter_research_cookies(all_cookies)
                full_cookies = filter_research_cookies_full(all_cookies)
                if not cookies:
                    logger.error(
                        "Probate auth: logged in but no research.txcourts "
                        "cookies present in browser context"
                    )
                    return None

                # Stash the full cookie list at module-level so callers
                # who need full attributes (Playwright re-injection) can
                # grab it via ``get_last_session_full_cookies()``. Keeps
                # the existing ``acquire_session()`` -> dict return shape
                # backward-compatible for the search-API caller.
                global _LAST_FULL_COOKIES
                _LAST_FULL_COOKIES = full_cookies

                elapsed = time.time() - started
                logger.info(
                    "Probate auth: success in %.1fs (%d cookies captured, "
                    "%d with full attributes)",
                    elapsed, len(cookies), len(full_cookies),
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


@contextlib.contextmanager
def live_authenticated_session():
    """PR 8.4 — same login flow as :func:`acquire_session`, but keeps the
    Playwright browser alive and yields ``(cookies, page)`` for callers
    that need to make ADDITIONAL navigations while authenticated.

    Used by the case-detail API discovery probe
    (``scripts/probe_tyler_case_api_discovery.py``) — re-injecting cookies
    into a fresh Playwright context proved insufficient for Tyler's SPA
    (the SPA's ``/api/auth/claims`` bootstrap call needs more than cookies
    alone). Using the SAME session that just authenticated has all the
    state the SPA expects.

    Yields:
        ``(cookies: dict[str, str], page: playwright Page)`` on success.
        ``(None, None)`` on auth failure.

    Browser closes on context exit regardless of yield value.

    Usage::

        from scraper.probate_auth import live_authenticated_session

        with live_authenticated_session() as (cookies, page):
            if cookies is None:
                return
            page.on("response", my_handler)
            page.goto("https://research.txcourts.gov/CourtRecordsSearch/ui/...")
            # do work with the live session

    For server-side API calls that only need cookies (e.g. the search API
    via probate.fetch_dallas_probate), use the cheaper
    :func:`acquire_session` which closes the browser before returning.
    """
    email_raw = os.environ.get(config.PROBATE_EMAIL_ENV, "")
    password  = os.environ.get(config.PROBATE_PASSWORD_ENV, "").strip()
    email = email_raw.strip()
    for junk in (",", '"', "'", ";", " ", "\t"):
        email = email.replace(junk, "")

    if not email or not password:
        logger.warning(
            "Probate auth: %s / %s env vars not set; cannot log in",
            config.PROBATE_EMAIL_ENV, config.PROBATE_PASSWORD_ENV,
        )
        yield (None, None)
        return

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError as exc:
        logger.error("Probate auth: playwright not available: %s", exc)
        yield (None, None)
        return

    started = time.time()
    logger.info("Probate auth (live): launching Playwright Chromium")

    pw_ctx = sync_playwright()
    p = pw_ctx.__enter__()
    browser = None
    try:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(NAV_TIMEOUT_MS)

        # Same flow as acquire_session, condensed (selectors unchanged)
        logger.info("Probate auth (live): navigating to entry URL")
        page.goto(ENTRY_URL)
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass

        signin_clicked = False
        for candidate in SELECTORS_HOME_SIGNIN:
            try:
                page.wait_for_selector(candidate, state="visible", timeout=5_000)
                page.click(candidate)
                signin_clicked = True
                break
            except PWTimeout:
                continue
        if not signin_clicked:
            logger.error("Probate auth (live): no Sign In selector matched")
            yield (None, None)
            return

        try:
            page.wait_for_url(f"**{LOGIN_URL_FRAGMENT}**", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            logger.error("Probate auth (live): redirect to IdP didn't happen")
            yield (None, None)
            return

        page.wait_for_selector(SELECTOR_EMAIL, state="visible", timeout=NAV_TIMEOUT_MS)
        page.fill(SELECTOR_EMAIL, email)
        page.fill(SELECTOR_PASS, password)
        page.click(SELECTOR_SUBMIT)

        try:
            page.wait_for_url(
                lambda url: (
                    "/CourtRecordsSearch/ui/dashboard" in url
                    or url.rstrip("/").endswith("/CourtRecordsSearch/ui/Home")
                ),
                timeout=LOGIN_TIMEOUT_MS,
            )
        except PWTimeout:
            logger.error(
                "Probate auth (live): post-login URL not reached: %s",
                page.url,
            )
            yield (None, None)
            return

        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        all_cookies = context.cookies()
        cookies = _filter_research_cookies(all_cookies)
        if not cookies:
            logger.error("Probate auth (live): no research.txcourts cookies captured")
            yield (None, None)
            return

        elapsed = time.time() - started
        logger.info(
            "Probate auth (live): success in %.1fs (%d cookies). "
            "Yielding live page; close via context exit.",
            elapsed, len(cookies),
        )
        yield (cookies, page)

    except Exception as exc:
        logger.exception("Probate auth (live): Playwright crashed: %s", exc)
        yield (None, None)
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            pw_ctx.__exit__(None, None, None)
        except Exception:
            pass


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


def filter_research_cookies_full(all_cookies: list[dict]) -> list[dict]:
    """PR 8.3: same domain filter as ``_filter_research_cookies`` but
    preserves the FULL Playwright cookie attributes (secure, httpOnly,
    sameSite, expires, path, domain).

    Required for re-injection into a fresh Playwright BrowserContext —
    the SPA's bootstrap auth check rejects cookies missing ``secure`` /
    ``sameSite`` attributes, even though the same cookies work fine
    sent as a flat ``Cookie:`` header via Python requests (which is
    how ``probate.fetch_dallas_probate`` uses them for the search API).

    Returns a list of Playwright-format cookie dicts. Empty list when
    no matching cookies were captured.
    """
    out: list[dict] = []
    for c in all_cookies:
        domain = c.get("domain") or ""
        if "research.txcourts.gov" not in domain:
            continue
        if not c.get("name") or c.get("value") is None:
            continue
        # Preserve only the attributes Playwright accepts in add_cookies.
        # ``expires`` is a unix timestamp; ``sameSite`` is "Strict"/"Lax"/"None".
        cookie = {
            "name":   c["name"],
            "value":  c["value"],
            "domain": c["domain"],
            "path":   c.get("path", "/"),
        }
        if "secure" in c:
            cookie["secure"] = bool(c["secure"])
        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if c.get("sameSite") in ("Strict", "Lax", "None"):
            cookie["sameSite"] = c["sameSite"]
        if "expires" in c and isinstance(c["expires"], (int, float)) and c["expires"] > 0:
            cookie["expires"] = c["expires"]
        out.append(cookie)
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


# Selectors scanned for any visible failure message — not just the canonical
# #validation-summary-alert. Tyler may surface errors in any of these.
_FAILURE_TEXT_SELECTORS: tuple[str, ...] = (
    "#validation-summary-alert",
    ".validation-summary",
    ".validation-summary-errors",
    ".alert",
    ".alert-danger",
    ".alert-error",
    ".text-danger",
    '[role="alert"]',
    ".error-message",
    ".field-validation-error",
)


def _capture_failure_diagnostics(page, label: str) -> None:
    """Best-effort dump of what the IdP page looked like when login failed.

    Writes a screenshot + HTML to ``data/probes/probate_<label>_<ts>.{png,html}``
    and logs:
      - Final URL and page title
      - Any visible text from common error/alert selectors
      - Whether navigator.webdriver is exposed (primary bot-detection signal)
      - List of forms / input IDs actually present on the page (so we can
        tell if selectors drifted)

    Never raises. Caller still soft-fails the auth.
    """
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    probe_dir = config.DATA_DIR / "probes"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Probate diagnostics: could not create %s: %s", probe_dir, e)
        return

    png_path  = probe_dir / f"probate_{label}_{ts}.png"
    html_path = probe_dir / f"probate_{label}_{ts}.html"

    # URL + title (cheap)
    try:
        logger.info("Probate diagnostics: final_url=%s", page.url)
    except Exception:
        pass
    try:
        logger.info("Probate diagnostics: page_title=%r", page.title())
    except Exception:
        pass

    # navigator.webdriver — if True, Tyler can trivially detect Playwright.
    try:
        is_webdriver = page.evaluate("() => navigator.webdriver")
        logger.info("Probate diagnostics: navigator.webdriver=%s", is_webdriver)
    except Exception as e:
        logger.info("Probate diagnostics: navigator.webdriver probe failed: %s", e)

    # Scan every common error/alert selector for visible text.
    found_any = False
    for sel in _FAILURE_TEXT_SELECTORS:
        try:
            elements = page.query_selector_all(sel)
            for el in elements:
                txt = (el.inner_text() or "").strip()
                if txt:
                    logger.info("Probate diagnostics: %s -> %r", sel, txt[:200])
                    found_any = True
        except Exception:
            continue
    if not found_any:
        logger.info("Probate diagnostics: no visible text in any known error selector")

    # Inventory of input field IDs — catches selector drift (e.g. Tyler
    # renames UserName to Email). We just list what's present; comparing
    # against SELECTOR_EMAIL/SELECTOR_PASS is left to log review.
    try:
        ids = page.evaluate(
            "() => Array.from(document.querySelectorAll('input, button'))"
            "  .map(e => ({tag: e.tagName.toLowerCase(), id: e.id, "
            "             type: e.getAttribute('type'), "
            "             name: e.getAttribute('name')}))"
            "  .filter(e => e.id || e.name)"
        )
        logger.info("Probate diagnostics: form inputs/buttons: %s", ids)
    except Exception as e:
        logger.info("Probate diagnostics: input inventory failed: %s", e)

    # Screenshot + HTML (the authoritative artifacts).
    try:
        page.screenshot(path=str(png_path), full_page=True)
        logger.info("Probate diagnostics: screenshot saved %s", png_path)
    except Exception as e:
        logger.info("Probate diagnostics: screenshot failed: %s", e)
    try:
        html = page.content() or ""
        html_path.write_text(html, encoding="utf-8")
        logger.info(
            "Probate diagnostics: HTML saved %s (%d chars)",
            html_path, len(html),
        )
    except Exception as e:
        logger.info("Probate diagnostics: HTML dump failed: %s", e)
