"""
Tests for scraper/probate_auth.py.

All Playwright interactions are mocked at the playwright.sync_api source
level. No real browser launches. No real HTTP requests. No credentials
needed.

Tests cover:
  - happy path: full login flow with synthetic Playwright mocks
  - missing credentials: returns None without launching Playwright
  - import failure: returns None if playwright package unavailable
  - login rejection: detects validation summary, returns None
  - unexpected redirect: detects wrong final URL, returns None
  - timeout during navigation: returns None
  - generic exception: caught, logged, returns None
  - cookie filtering: only research.txcourts.gov cookies included
  - cookies_to_header formatting

Patching note:
    scraper.probate_auth.acquire_session does a lazy import of
    ``from playwright.sync_api import sync_playwright, TimeoutError``
    inside the function body. This means ``sync_playwright`` is NOT an
    attribute of the scraper.probate_auth module at patch time, so we
    cannot patch ``scraper.probate_auth.sync_playwright`` directly.

    Instead, we patch the symbols on the playwright.sync_api module
    itself, which is where the lazy import resolves them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import config, probate_auth
from scraper.probate_auth import (
    acquire_session,
    cookies_to_header,
    _filter_research_cookies,
)


# ============================================================================
# Helpers - synthetic Playwright mock chain
# ============================================================================


def _make_playwright_mock(*, final_url: str, cookies: list[dict],
                          error_text: str = "",
                          wait_raises=False) -> MagicMock:
    """Build a mock callable that, when called as ``sync_playwright()``,
    returns a context manager yielding a fake Playwright root object.

    Returns the callable mock. Pass this as the ``new`` arg to
    ``patch("playwright.sync_api.sync_playwright", new=...)``.
    """
    pw = MagicMock()
    browser = MagicMock()
    context = MagicMock()
    page = MagicMock()

    page.url = final_url
    context.cookies.return_value = cookies
    context.new_page.return_value = page
    browser.new_context.return_value = context
    pw.chromium.launch.return_value = browser

    if wait_raises:
        # Simulate a timeout while waiting for dashboard URL
        from playwright.sync_api import TimeoutError as PWTimeout
        page.wait_for_url.side_effect = PWTimeout("timeout")
    else:
        page.wait_for_url.return_value = None

    if error_text:
        error_el = MagicMock()
        error_el.inner_text.return_value = error_text
        page.query_selector.return_value = error_el
    else:
        page.query_selector.return_value = None

    # The sync_playwright() context manager
    cm = MagicMock()
    cm.__enter__.return_value = pw
    cm.__exit__.return_value = None

    # sync_playwright is a callable that RETURNS the cm
    sync_pw_callable = MagicMock(return_value=cm)
    return sync_pw_callable


@pytest.fixture
def creds_set(monkeypatch):
    """Set both credential env vars to non-empty values."""
    monkeypatch.setenv(config.PROBATE_EMAIL_ENV, "test@example.com")
    monkeypatch.setenv(config.PROBATE_PASSWORD_ENV, "TestPassword123")


# ============================================================================
# Missing credentials - short-circuit (no Playwright involvement)
# ============================================================================


class TestMissingCredentials:
    def test_missing_email_returns_none(self, monkeypatch):
        monkeypatch.delenv(config.PROBATE_EMAIL_ENV, raising=False)
        monkeypatch.setenv(config.PROBATE_PASSWORD_ENV, "x")
        # No Playwright patch needed; should short-circuit on env-var check
        result = acquire_session()
        assert result is None

    def test_missing_password_returns_none(self, monkeypatch):
        monkeypatch.setenv(config.PROBATE_EMAIL_ENV, "x")
        monkeypatch.delenv(config.PROBATE_PASSWORD_ENV, raising=False)
        result = acquire_session()
        assert result is None

    def test_empty_email_returns_none(self, monkeypatch):
        monkeypatch.setenv(config.PROBATE_EMAIL_ENV, "   ")
        monkeypatch.setenv(config.PROBATE_PASSWORD_ENV, "x")
        result = acquire_session()
        assert result is None

    def test_empty_password_returns_none(self, monkeypatch):
        monkeypatch.setenv(config.PROBATE_EMAIL_ENV, "x")
        monkeypatch.setenv(config.PROBATE_PASSWORD_ENV, "")
        result = acquire_session()
        assert result is None


# ============================================================================
# Happy path - successful login
# ============================================================================


class TestHappyPath:
    def test_successful_login_returns_cookies(self, creds_set):
        synthetic_cookies = [
            {"domain": ".research.txcourts.gov", "name": "FedAuth",  "value": "AAAA"},
            {"domain": ".research.txcourts.gov", "name": "FedAuth1", "value": "BBBB"},
            {"domain": ".research.txcourts.gov", "name": "RSCH_JWT", "value": "eyJ..."},
            {"domain": "texas.tylertech.cloud", "name": "OtherCookie", "value": "xxx"},
        ]
        sync_pw = _make_playwright_mock(
            final_url="https://research.txcourts.gov/CourtRecordsSearch/ui/dashboard",
            cookies=synthetic_cookies,
        )
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            result = acquire_session()
        assert result is not None
        assert "FedAuth" in result
        assert "FedAuth1" in result
        assert "RSCH_JWT" in result
        # tylertech.cloud cookie filtered out
        assert "OtherCookie" not in result

    def test_successful_login_calls_form_fields_in_order(self, creds_set):
        sync_pw = _make_playwright_mock(
            final_url="https://research.txcourts.gov/CourtRecordsSearch/ui/dashboard",
            cookies=[{"domain": ".research.txcourts.gov",
                      "name": "FedAuth", "value": "x"}],
        )
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            acquire_session()

        # Reach into the mock chain to inspect what got called
        cm = sync_pw.return_value
        pw_root = cm.__enter__.return_value
        browser = pw_root.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value

        page.fill.assert_any_call(probate_auth.SELECTOR_EMAIL, "test@example.com")
        page.fill.assert_any_call(probate_auth.SELECTOR_PASS, "TestPassword123")
        page.click.assert_called_with(probate_auth.SELECTOR_SUBMIT)


# ============================================================================
# Login rejection paths
# ============================================================================


class TestLoginRejection:
    def test_credentials_rejected_returns_none(self, creds_set):
        sync_pw = _make_playwright_mock(
            final_url=(
                "https://texas.tylertech.cloud/idp/account/signin"
                "?ReturnUrl=..."
            ),
            cookies=[],
            error_text="Sign in was unsuccessful. Invalid Email or password.",
            wait_raises=True,
        )
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            result = acquire_session()
        assert result is None

    def test_unexpected_redirect_destination_returns_none(self, creds_set):
        sync_pw = _make_playwright_mock(
            final_url="https://research.txcourts.gov/maintenance",
            cookies=[],
            wait_raises=True,
        )
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            result = acquire_session()
        assert result is None

    def test_logged_in_but_no_research_cookies_returns_none(self, creds_set):
        sync_pw = _make_playwright_mock(
            final_url="https://research.txcourts.gov/CourtRecordsSearch/ui/dashboard",
            cookies=[
                {"domain": "tylertech.cloud", "name": "X", "value": "y"},
            ],
        )
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            result = acquire_session()
        assert result is None


# ============================================================================
# Exception handling
# ============================================================================


class TestExceptionHandling:
    def test_playwright_exception_returns_none(self, creds_set):
        # The context manager itself raises when entered
        cm = MagicMock()
        cm.__enter__.side_effect = RuntimeError("Chromium crashed")
        sync_pw = MagicMock(return_value=cm)
        with patch("playwright.sync_api.sync_playwright", new=sync_pw):
            result = acquire_session()
        assert result is None

    def test_playwright_import_failure_returns_none(self, creds_set, monkeypatch):
        # Simulate playwright not installed by making the import fail
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright.sync_api":
                raise ImportError("playwright not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = acquire_session()
        assert result is None


# ============================================================================
# Cookie filtering
# ============================================================================


class TestCookieFiltering:
    def test_filter_includes_exact_domain(self):
        cookies = [
            {"domain": "research.txcourts.gov", "name": "A", "value": "1"},
            {"domain": ".research.txcourts.gov", "name": "B", "value": "2"},
        ]
        result = _filter_research_cookies(cookies)
        assert result == {"A": "1", "B": "2"}

    def test_filter_excludes_other_domains(self):
        cookies = [
            {"domain": "tylertech.cloud", "name": "A", "value": "1"},
            {"domain": "texas.tylertech.cloud", "name": "B", "value": "2"},
            {"domain": "google.com", "name": "C", "value": "3"},
        ]
        result = _filter_research_cookies(cookies)
        assert result == {}

    def test_filter_skips_cookies_with_missing_fields(self):
        cookies = [
            {"domain": ".research.txcourts.gov", "name": None, "value": "x"},
            {"domain": ".research.txcourts.gov", "name": "A"},  # no value
            {"domain": ".research.txcourts.gov", "name": "B", "value": "good"},
        ]
        result = _filter_research_cookies(cookies)
        assert result == {"B": "good"}


# ============================================================================
# Cookie header formatting
# ============================================================================


class TestCookieHeader:
    def test_basic_formatting(self):
        cookies = {"FedAuth": "AAA", "FedAuth1": "BBB"}
        result = cookies_to_header(cookies)
        assert result == "FedAuth=AAA; FedAuth1=BBB"

    def test_empty_dict_returns_empty_string(self):
        assert cookies_to_header({}) == ""

    def test_single_cookie(self):
        assert cookies_to_header({"X": "Y"}) == "X=Y"


# ═══════════════════════════════════════════════════════════════════════════
# PR 8.3 — filter_research_cookies_full preserves attributes for
# Playwright re-injection (case-detail SPA bootstrap needs full attrs).
# ═══════════════════════════════════════════════════════════════════════════


from scraper.probate_auth import (
    filter_research_cookies_full,
    get_last_session_full_cookies,
)


class TestFilterResearchCookiesFull:
    def test_preserves_secure_and_samesite(self):
        all_cookies = [
            {"name": "sess", "value": "abc",
             "domain": ".research.txcourts.gov", "path": "/",
             "secure": True, "httpOnly": True, "sameSite": "Lax"},
        ]
        result = filter_research_cookies_full(all_cookies)
        assert len(result) == 1
        assert result[0]["name"] == "sess"
        assert result[0]["value"] == "abc"
        assert result[0]["secure"] is True
        assert result[0]["httpOnly"] is True
        assert result[0]["sameSite"] == "Lax"

    def test_excludes_unrelated_domains(self):
        all_cookies = [
            {"name": "tracking", "value": "v", "domain": ".google.com", "path": "/"},
            {"name": "sess", "value": "abc",
             "domain": ".research.txcourts.gov", "path": "/"},
        ]
        result = filter_research_cookies_full(all_cookies)
        names = [c["name"] for c in result]
        assert "tracking" not in names
        assert "sess" in names

    def test_skips_invalid_samesite(self):
        """Playwright add_cookies only accepts 'Strict'/'Lax'/'None'.
        Any other value (e.g. legacy 'No Restriction') must be omitted."""
        all_cookies = [
            {"name": "sess", "value": "abc",
             "domain": ".research.txcourts.gov", "path": "/",
             "sameSite": "No Restriction"},
        ]
        result = filter_research_cookies_full(all_cookies)
        assert "sameSite" not in result[0]

    def test_skips_negative_expires(self):
        """Negative expiry (-1 = session cookie in Playwright) shouldn't
        be passed as an explicit expires value."""
        all_cookies = [
            {"name": "sess", "value": "abc",
             "domain": ".research.txcourts.gov", "path": "/",
             "expires": -1},
        ]
        result = filter_research_cookies_full(all_cookies)
        assert "expires" not in result[0]


class TestGetLastSessionFullCookies:
    def test_returns_empty_when_no_session(self):
        # Reset the module-level cache for test isolation
        import scraper.probate_auth as pa
        pa._LAST_FULL_COOKIES = []
        assert get_last_session_full_cookies() == []

    def test_returns_cached_list(self):
        import scraper.probate_auth as pa
        pa._LAST_FULL_COOKIES = [
            {"name": "sess", "value": "abc",
             "domain": ".research.txcourts.gov", "path": "/"},
        ]
        result = get_last_session_full_cookies()
        assert len(result) == 1
        assert result[0]["name"] == "sess"
        # Returned list must be a copy — mutating it shouldn't poison the cache
        result.append({"name": "evil", "value": "no"})
        assert len(get_last_session_full_cookies()) == 1
