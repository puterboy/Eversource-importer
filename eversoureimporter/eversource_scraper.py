#!/usr/bin/env python3
#==============================================================================
# pylint: disable=too-many-locals, too-many-branches, too-many-statements, too-many-return-statements
# pylint: disable=line-too-long
#==============================================================================
"""Eversource 15-Minute Interval Smart Meter Data Extraction Engine.
#
# App: Eversource Downloader and Importer
# File: eversource_scraper.py
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#

Downloads interval usage data from the Eversource customer portal,
strips the proprietary header block, and writes/appends a clean CSV.

If no start date given, then start date is the last date in the output file if it exists or else 7 days ago
If no end date given, then end date is today

Algorithm / navigation sequence
-------------------------------
1. Open the Eversource login page and submit username + password.
2. Optionally dismiss the MFA "Ask Me Again Later" prompt.
3. Confirm we have left the login URL (auth succeeded).
4. Click "View energy usage data" to reach the usage-detail SPA route.
5. Locate the <opower-widget-usage-export> custom element and enter its
   open shadow DOM (all export controls live inside the shadow root).
6. Click "Download my data" if the export panel is not already open.
7. Select the "Export usage for a range of days" radio button.
8. Fill the From / To date fields (default: last day in output file through today if exists or last 7 days otherwise)
9. Click Export; the portal generates a .zip containing one CSV.
10. Wait for the .zip to finish downloading into TEMP_DIR.
11. Unzip, discard the proprietary address/account header block, keep only
    the TYPE,DATE,... data rows, and append rows that are strictly newer
    than the last timestamp already present in the output CSV.

Returns
-------
  0  new data rows were written
  1  Standard runtime errors
  2  No new data (everything already present)
  5  Unexpected exception errors

NOTE: To make this work in Alpine Linux (e.g., HA Advanced SSH shell) you may need to do:
    apk add --no-cache chromium chromium-chromedriver
    pip3 install --no-cache-dir --break-system-packages selenium
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import cast

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --------------------------------------------------------------------------
# Global variables
# --------------------------------------------------------------------------
TEMP_DIR: str = "/tmp/eversource.tmp"
CHROMIUM_TEMP_DIR: str = TEMP_DIR + "/chromium-user-data"

LOGIN_URL: str = (
    "https://www.eversource.com/security/account/login"
    "?ReturnUrl=/cg/customer/accountoverview"
)
USAGE_URL: str = "https://www.eversource.com/cg/customer/Account#/usage-detail/"

DEFAULT_START_DAYS = 7
LAST_DATE: str | None = None
LAST_START: str | None = None

START_DATE: str
END_DATE: str

OUTPUT_FILE: str

HEADLESS: bool

TEST: bool

args: argparse.Namespace

# --------------------------------------------------------------------------
# Tunable timeouts (seconds) -- adjust here without touching the logic below
# --------------------------------------------------------------------------
TIMEOUT_DEFAULT: int = 30          # Generic light-DOM element wait
TIMEOUT_LOGIN: int = 30            # Login timeout
TIMEOUT_2FA: int = 10              # 2FA timeout
TIMEOUT_INITIALIZATION: int = 45   # Initialization timeout
TIMEOUT_USAGE: int = 45            # Usage detail wait
TIMEOUT_HOST: int = 30             # opower-widget-usage-export host
TIMEOUT_SHADOW: int = 30           # Generic shadow-DOM element wait
TIMEOUT_DOWNLOAD_BTN: int = 30     # "Download my data" inside shadow root
TIMEOUT_ZIP: int = 90              # Wall-clock wait for .zip download

# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------
def parse_arguments()->argparse.Namespace:
    """Parse arguments and return argparse object"""
    def non_negative_int(value: str) -> int:
        """Test for argparse non-negative int"""
        intvalue = int(value)
        if intvalue < 0:
            raise argparse.ArgumentTypeError("must be >= 0")
        return intvalue
    def positive_int(value: str) -> int:
        """Test for argparse positive int"""
        intvalue = int(value)
        if intvalue <= 0:
            raise argparse.ArgumentTypeError("must be > 0")
        return intvalue

    parser = argparse.ArgumentParser(
        description="Eversource 15-min interval kWH and cost data downloader"
    )

    parser.add_argument(
        "-u", "--user",
        type=str,
        default=os.environ.get("USER_EVERSOURCE"),
        help="Username / email [default=USER_EVERSOURCE env variable]",
    )
    parser.add_argument(
        "-p", "--passwd",
        type=str,
        default=os.environ.get("PASSWD_EVERSOURCE"),
        help="Password [default=PASSWD_EVERSOURCE env variable]",
    )
    parser.add_argument(
        "-a", "--account",
        type=positive_int,
        default=None,
        help="Account number (optional)"
    )
    parser.add_argument(
        "-s", "--start",
        type=non_negative_int,
        help=f"Number of days back from today to START downloading (must be >=0 [default= last date in output file if exists or {DEFAULT_START_DAYS} days ago]"
    )
    parser.add_argument(
        "-e", "--end",
        type=non_negative_int,
        default=0,
        help="Number of days back from today END downloading (must be >=0 [default=0, today)"
    )

    parser.add_argument(
        "-t", "--test",
        action="store_true",
        help="Dry-run: download and parse usage data, but do not write the output CSV",
    )

    parser.add_argument(
        "-H", "--headed",
        action="store_true",
        help="Run as headed with browser window output (requires a working display!)",
    )

    parser.add_argument(
        "-v",
        action="count",
        default=0,
        dest="verbosity",
        help="Increase verbosity (-v, -vv, ...)",
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        required=True,
        help="Path to the output CSV file (created or appended to)",
    )

    return parser.parse_args()

# --------------------------------------------------------------------------
# Logging helper
# --------------------------------------------------------------------------
def log(level: int, message: str) -> None:
    """Emit a timestamped line when the requested verbosity is active."""
    if args.verbosity >= level:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {message}")


# --------------------------------------------------------------------------
# Path conversion for Cygwin/Windows implementations
# --------------------------------------------------------------------------

def unix_to_native(path: str) -> str:
    """Convert a Unix/Cygwin path to the native OS path."""
    if sys.platform != "cygwin":
        return path

    if not path.startswith("/"):
        return path

    # /cygdrive/c/foo -> C:\foo
    if path.startswith("/cygdrive/") and len(path) >= 11:
        drive = path[10]
        if len(path) == 11 or path[11] == "/":
            rest = path[12:] if len(path) > 11 else ""
            return f"{drive.upper()}:\\{rest.replace('/', '\\')}"

    # /tmp, /usr, /home, etc. require Cygwin's mount mapping.
    return subprocess.check_output(
        ["cygpath", "-w", path],
        text=True,
    ).strip()


def native_to_unix(path: str) -> str:
    """Convert a native OS path to a Unix/Cygwin path."""
    if sys.platform != "cygwin":
        return path

    p = PureWindowsPath(path)

    # C:\foo -> /cygdrive/c/foo
    if p.drive and len(p.drive) == 2 and p.drive[1] == ":":
        drive = p.drive[0].lower()
        rest = "/".join(p.parts[1:])
        return f"/cygdrive/{drive}/{rest}" if rest else f"/cygdrive/{drive}"

    # Let Cygwin resolve anything else.
    return subprocess.check_output(
        ["cygpath", "-u", path],
        text=True,
    ).strip()

# --------------------------------------------------------------------------
# Global variable initalization
# --------------------------------------------------------------------------
def initialize_globals() -> None:
    """Parse/validate arguments and prepare global configuration."""
    global TEMP_DIR, CHROMIUM_TEMP_DIR, OUTPUT_FILE, LAST_DATE, LAST_START, START_DATE, END_DATE, HEADLESS, TEST # pylint: disable=global-statement

    if not args.user or not args.passwd:
        raise RuntimeError("Missing login credentials: Provide -u|--user / -p|--passwd or set USER_EVERSOURCE / PASSWD_EVERSOURCE.")

    TEMP_DIR = unix_to_native(TEMP_DIR)
    CHROMIUM_TEMP_DIR = unix_to_native(CHROMIUM_TEMP_DIR)
    log(2, f"TEMP_DIR={TEMP_DIR}, CHROMIUM_TEMP_DIR={CHROMIUM_TEMP_DIR}")

    if os.path.exists(CHROMIUM_TEMP_DIR):  # Cleanup stale chromium remnants from prior run
        try:
            shutil.rmtree(CHROMIUM_TEMP_DIR)
        except OSError as exc:
            raise RuntimeError(f"Could not remove chromium temp directory, '{CHROMIUM_TEMP_DIR}: {exc}") from exc

    os.makedirs(TEMP_DIR, exist_ok=True)
    for name in os.listdir(TEMP_DIR):  # Cleanup stale zip downloads from prior run
        if name.endswith(".zip") or name.endswith(".crdownload"):
            try:
                os.remove(os.path.join(TEMP_DIR, name))
            except OSError:
                pass

    os.environ["TMPDIR"] = TEMP_DIR  # Requests that child processes use TEMP_DIR for temporary files

    log(3, f"Temp directory for chromium, zip downloads and debugging: {TEMP_DIR}")

    OUTPUT_FILE = args.file
    if os.path.exists(OUTPUT_FILE):
        LAST_DATE, LAST_START = inspect_existing_output_file(OUTPUT_FILE)
    else:
        parent = Path(OUTPUT_FILE).parent
        if not parent.exists() or not os.access(parent, os.W_OK):
            raise RuntimeError(f"Output file not writable: {OUTPUT_FILE}")

        log(2, f"Output file '{OUTPUT_FILE}' doesn't exist, will create new one...")

    today = datetime.today()
    if args.start is not None:  # Start date option takes precedence
        START_DATE = (today - timedelta(days=args.start)).strftime("%m/%d/%Y")
    elif LAST_DATE is not None:  # Then date from ouput file if it exists
        START_DATE = datetime.strptime(LAST_DATE, "%Y-%m-%d").strftime("%m/%d/%Y")
    else:  # Then default
        START_DATE = (today - timedelta(days=DEFAULT_START_DAYS)).strftime("%m/%d/%Y")

    END_DATE = (today - timedelta(days=args.end)).strftime("%m/%d/%Y")
    start_dt = datetime.strptime(START_DATE, "%m/%d/%Y")
    end_dt = datetime.strptime(END_DATE, "%m/%d/%Y")
    if end_dt < start_dt:
        END_DATE = START_DATE
    log(1, f"Download window: {START_DATE} --> {END_DATE}")

    HEADLESS = not args.headed
    TEST = args.test

# --------------------------------------------------------------------------
# Diagnostic dump on failure
# --------------------------------------------------------------------------
def dump_diagnostic_state(driver: WebDriver, label: str) -> None:
    """Write the current page source and print a clear failure banner."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = os.path.join(TEMP_DIR, f"debug_{label}_{ts}.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)

        png_path = os.path.join(TEMP_DIR, f"debug_{label}_{ts}.png")
        driver.save_screenshot(png_path)

        log_path = os.path.join(TEMP_DIR, f"debug_{label}_{ts}.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            for entry in driver.get_log("browser"):  # type: ignore[attr-defined]
                fh.write(f"{entry.get('level')}: {entry.get('message')}\n")

        print()
        print("=" * 70)
        print(f"[DIAGNOSTIC FAILURE: {label.upper()}]")
        print(f"Timestamp  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Current URL: {driver.current_url}")
        print(f"Title      : {driver.title}")
        print(f"DOM dump   : {html_path}")
        print(f"Browser console log: {log_path}")
        print("=" * 70)
        print()
    except (OSError, WebDriverException) as exc:
        print(f"[DIAGNOSTIC CRITICAL] Could not write HTML dump: {exc}")


# --------------------------------------------------------------------------
# Browser initialisation
# --------------------------------------------------------------------------
def init_driver() -> webdriver.Chrome:
    """Create a headless Chrome instance configured for silent downloads."""
    log(3, "Configuring Chrome options...")
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-data-dir={CHROMIUM_TEMP_DIR}")  # User data (as distinct from process data)
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})  # Enables console logging

    # Tell Chrome to drop downloads straight into TEMP_DIR with no prompt
    prefs = {
        "download.default_directory": TEMP_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    options.add_experimental_option("prefs", prefs)

    result = SeleniumManager().binary_paths(["--browser", "chrome"])  # Use this to create OS-indpendent paths for binary_location & service
    options.binary_location = result["browser_path"]

    service = Service(
    native_to_unix(result["driver_path"]),
    log_output=os.path.join(TEMP_DIR, "chromedriver.log"),
)
#    service = Service(native_to_unix(result["driver_path"]))

    driver = webdriver.Chrome(service=service, options=options)
    log(3, "Driver started.")
    return driver


# --------------------------------------------------------------------------
# Generic wait helper (light DOM)
# --------------------------------------------------------------------------
def wait_for(
    driver: WebDriver,
    by: str,
    value: str,
    timeout: int = TIMEOUT_DEFAULT,
    clickable: bool = False,
) -> WebElement:
    """Wait for an element in the light DOM and return it."""
    condition = (
        EC.element_to_be_clickable((by, value))
        if clickable
        else EC.presence_of_element_located((by, value))
    )
    log(3, f"Waiting up to {timeout}s for {by}={value!r} (clickable={clickable})")
    element = WebDriverWait(driver, timeout).until(condition)
    log(
        3,
        f" -> Found: tag={element.tag_name}, "
        f"id={element.get_attribute('id')!r}, "
        f"text={element.text[:60]!r}",
    )
    return element


# --------------------------------------------------------------------------
# Shadow-DOM helpers
# --------------------------------------------------------------------------
def get_shadow_root(driver: WebDriver, host: WebElement) -> object:
    """Return the open shadowRoot of a host element."""
    shadow = driver.execute_script("return arguments[0].shadowRoot", host)
    if shadow is None:
        raise RuntimeError("shadowRoot is null")
    return shadow


def shadow_find(
    driver: WebDriver,
    shadow: object,
    css: str,
    timeout: int = TIMEOUT_SHADOW,
) -> WebElement:
    """Poll inside a shadow root until the CSS selector matches."""
    end = time.time() + timeout
    while time.time() < end:
        el = cast(
            WebElement | None,
            driver.execute_script(
                "return arguments[0].querySelector(arguments[1])", shadow, css
            ),
        )
        if el is not None:
            return el
        time.sleep(0.25)
    raise TimeoutException(f"Shadow element not found: {css}")

# --------------------------------------------------------------------------
# CSV Output file and download processing
# --------------------------------------------------------------------------

def inspect_existing_output_file(path: str) -> tuple[str, str]:
    """Validate an existing output file and return its last DATE|START key."""
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"Output file not writable: {path}")

    last = None
    count = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if row:
                last = row
                count += 1

    if last is None:
        raise RuntimeError(
            f"Not a valid energy data file: {path}\n"
            "Expected at least one row with the following columns: TYPE,DATE,START TIME,END TIME,USAGE (kWh)"
        )
    if len(last) < 5:
        raise RuntimeError(
            f"Last row has only {len(last)} columns: {last!r}\n"
            "Expected at least the following data columns: TYPE,DATE,START TIME,END TIME,USAGE (kWh)"
        )
    try:
        datetime.strptime(last[1], "%Y-%m-%d")
        datetime.strptime(last[2], "%H:%M")
        datetime.strptime(last[3], "%H:%M")
        float(last[4])
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid last row: {last!r}\n"
            "Expected at least the following data columns: TYPE,DATE,START TIME,END TIME,USAGE (kWh)"
        ) from exc

    log(2, f"Output file '{path}' has {count} rows with last entry: {last[1]}: {last[2]} --> {last[3]}")
    return last[1], last[2]


def process_downloaded_zip(zip_path: str, output_file: str) -> int:
    """Unzip the Eversource package, strip the proprietary header block,
    and append only rows that are newer than the last existing data.

    Returns the number of new rows that were written.
    """
    log(2, f"Processing zip: {zip_path}")

    # ----- extract the single CSV that lives inside the zip -----
    with zipfile.ZipFile(zip_path, "r") as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("No CSV file found inside the downloaded zip")
        log(3, f"CSV inside zip: {csv_names[0]}")
        raw_text = zf.read(csv_names[0]).decode("utf-8", errors="replace")

    # ----- locate the real header row (TYPE,DATE,...) -----
    # Everything above that line is Eversource metadata (address, account, ...)
    lines = raw_text.splitlines()
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("TYPE,DATE,START TIME,END TIME"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("Could not locate the TYPE,DATE,... header row")

    header_line = lines[header_idx]
    data_text = "\n".join(lines[header_idx + 1 :])

    # Parse new data
    new_reader = csv.reader(data_text.splitlines())
    new_rows: list[list[str]] = [row for row in new_reader if len(row) >= 4]
    log(1, f"Parsed {len(new_rows)} data rows from download")

    # ----- keep only rows strictly newer than: (LAST_DATE, LAST_START) -----
    rows_to_write: list[list[str]] = []
    for row in new_rows:
        if LAST_DATE is None or (row[1], row[2]) > (LAST_DATE, LAST_START):
            rows_to_write.append(row)

    log(1, f"{len(rows_to_write)} new rows will be written "
        f"(out of {len(new_rows)} downloaded)")

    # ----- write with Unix line endings only -----
    if TEST:
        log(1, f"Test mode: Skipped writing to {output_file}")
        return len(rows_to_write)
    mode = "a" if LAST_DATE else "w"  # Append if file exists, otherwise create new file
    with open(output_file, mode, encoding="utf-8", newline="\n") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        if not LAST_DATE:
            fh.write(header_line + "\n")
            log(2, "Wrote column header")
        writer.writerows(rows_to_write)

    log(1, f"Finished writing to {output_file}")
    return len(rows_to_write)


# --------------------------------------------------------------------------
# Main navigation + download sequence
# --------------------------------------------------------------------------
def run_eversource_download(driver: WebDriver, username: str, password: str, account: int | None) -> int:
    """Drive the browser through login, navigation, export and file processing.

    Returns the number of new CSV rows written, or -1 on failure.
    """
    # ------------------------------------------------------------------
    # STEP 1: Login page
    # ------------------------------------------------------------------
    log(2, f"Navigating to login page: {LOGIN_URL}")
    driver.get(LOGIN_URL)
    log(2, f"Current page: {driver.current_url}")
    log(2, f"Page title  : {driver.title}")

    try:
        username_field = wait_for(driver, By.ID, "Email/Username", clickable=True)
    except TimeoutException:
        dump_diagnostic_state(driver, "step1_username_missing")
        print("[PATH FAILURE] Username field never appeared.")
        return -1

    log(2, "Typing username...")
    username_field.clear()
    username_field.send_keys(username)
    log(2, f" -> Username entered: {username}")

    try:
        password_field = wait_for(driver, By.ID, "password", clickable=True)
    except TimeoutException:
        dump_diagnostic_state(driver, "step1_password_missing")
        print("[PATH FAILURE] Password field missing.")
        return -1

    log(2, "Typing password...")
    password_field.clear()
    password_field.send_keys(password)
    log(2, " -> Password entered: XXXXX")

    try:
        sign_in_btn = wait_for(driver, By.ID, "signIn", clickable=True)
    except TimeoutException:
        dump_diagnostic_state(driver, "step1_signin_missing")
        print("[PATH FAILURE] Sign-In button missing.")
        return -1

    log(2, "Clicking Sign-In...")
    sign_in_btn.click()
    log(2, " -> Sign-In clicked.")

    # ------------------------------------------------------------------
    # STEP 2: Optional 2FA, then confirm we left the login page
    # ------------------------------------------------------------------
    log(2, "Verifying authentication (and declining 2FA if offered)...")
    try:
        def _post_login_ready(d: WebDriver) -> WebElement | bool:
            # Success path: already past the login URL
            if "security/account/login" not in d.current_url:
                return True
            # Optional path: MFA "Ask Me Again Later" is present
            els = d.find_elements(By.ID, "dt-mfa-askmeagainlater")
            if els and els[0].is_displayed():
                return els[0]
            return False

        result = WebDriverWait(driver, TIMEOUT_LOGIN).until(_post_login_ready)
        if result is not True:
            log(2, "2FA overlay found - clicking 'Ask Me Again Later'")
            cast(WebElement, result).click()
            WebDriverWait(driver, TIMEOUT_2FA).until(
                lambda d: "security/account/login" not in d.current_url
            )
        log(2, f"Authenticated. Current URL: {driver.current_url}")
    except TimeoutException:
        dump_diagnostic_state(driver, "step2_still_on_login")
        print("[PATH FAILURE] Still on login page - credentials rejected?")
        return -1

    # ------------------------------------------------------------------
    # STEP 3: Wait for initialization to complete
    # ------------------------------------------------------------------

    log(2, "Waiting for Eversource customer application to initialize...")
    try:
        WebDriverWait(driver, TIMEOUT_INITIALIZATION).until(
            lambda d: (
                "/cg/customer/" in d.current_url
                and d.get_cookie(".REGION") is not None
            )
        )
        log(2, f"Eversource customer initialization complete: {driver.current_url}")

    except TimeoutException:
        log(2, f"Customer initialization timeout: {driver.current_url}")
        dump_diagnostic_state(driver, "step3_customer_initialization_timeout")
        print("[PATH FAILURE] Eversource customer application did not initialize.")
        return -1

    # ------------------------------------------------------------------
    # STEP 4: Navigate to Usage Detail page
    # ------------------------------------------------------------------
    usage_url = USAGE_URL + f"{account if account is not None else ''}"
    log(2, f"Navigating to usage data-detail page: {usage_url}")


    for tries in range(1, 6):
        driver.get(usage_url)
        time.sleep(2)
        if driver.current_url == usage_url:
            break
    else:
        raise RuntimeError(
            f"[PATHFAILURE] Failed to navigate to usage-detail page (tries=5): {driver.current_url}"
        )

    log(2, f"Successfully navigated to usage-detail page (tries={tries}): {usage_url}")

    # ------------------------------------------------------------------
    # STEP 5: Wait for Usage page to render and locate Opower custom element.
    # ------------------------------------------------------------------
    log(2, "Waiting for usage export widget to render...")

    try:
        host = WebDriverWait(driver, TIMEOUT_HOST).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "opower-widget-usage-export")
            )
        )
        log(2, f"Usage widget present. URL: {driver.current_url}")

    except TimeoutException:
        dump_diagnostic_state(driver, "step5_host_missing")
        print("[PATH FAILURE] Usage usage export widget never appeared.")
        return -1


    # ------------------------------------------------------------------
    # STEP 6: Open the export form (if it is not already expanded)
    # All export UI (radio, dates, Export button) lives inside the shadow DOM.
    # ------------------------------------------------------------------

    try:
        shadow = get_shadow_root(driver, host)
    except RuntimeError:
        dump_diagnostic_state(driver, "step5_no_shadow")
        print("[PATH FAILURE] shadowRoot is null.")
        return -1
    log(2, "Shadow root obtained.")

    try:
        download_btn = shadow_find(
            driver,
            shadow,
            "button.download-button, button.secondary.download-button",
            timeout=TIMEOUT_DOWNLOAD_BTN,
        )
        log(2, "Clicking 'Download my data' button inside shadow...")
        driver.execute_script("arguments[0].click()", download_btn)
        time.sleep(1.0)  # allow the panel animation to finish
    except TimeoutException:
        log(3, "Download button not found - form may already be open.")

    # ------------------------------------------------------------------
    # STEP 7: Select "Export usage for a range of days"
    # ------------------------------------------------------------------
    log(2, "Selecting date-range radio...")
    try:
        label = shadow_find(
            driver,
            shadow,
            "label[for='period-date'], label.period-date-radio-label",
        )
        driver.execute_script("arguments[0].click()", label)
        log(2, " -> Radio selected.")
        time.sleep(0.5)  # let the date inputs enable
    except TimeoutException:
        dump_diagnostic_state(driver, "step7_radio_missing")
        print("[PATH FAILURE] period-date radio not found inside shadow.")
        return -1

    # ------------------------------------------------------------------
    # STEP 8: Populate the From / To date fields
    # ------------------------------------------------------------------

    def fill_date(el: WebElement, value: str, label: str) -> None:
        """
        Date-picker inputs do not reliably update application state via Selenium typing
        or direct value assignment if not trusted keystrokes.
        Use the native value and dispatch input/change/focus events to simulate a committed edit.
        """

        driver.execute_script(
            """
            const input = arguments[0];
            const value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                'value'
            ).set;

            setter.call(input, value);

            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
            input.dispatchEvent(new Event('focusout', { bubbles: true }));
            """, el, value)

        shown = el.get_attribute("value")
        log(2, f" -> {label} date entered: {value} (readback: {shown!r})")

    log(2, f"Setting date range: {START_DATE} --> {END_DATE}")

    try:
        from_input = shadow_find(driver, shadow, "#date-selector--select-date-from")
        fill_date(from_input, START_DATE, "From")
    except TimeoutException:
        dump_diagnostic_state(driver, "step8_from_missing")
        print("[PATH FAILURE] From-date input missing.")
        return -1

    try:
        to_input = shadow_find(driver, shadow, "#date-selector--select-date-to")
        fill_date(to_input, END_DATE, "To")
    except TimeoutException:
        dump_diagnostic_state(driver, "step8_to_missing")
        print("[PATH FAILURE] To-date input missing.")
        return -1

    # ------------------------------------------------------------------
    # STEP 9: Click the Export button
    # ------------------------------------------------------------------
    log(2, "Clicking Export button...")
    try:
        export_btn = shadow_find(
            driver, shadow, "button.button.primary.button-spinner"
        )
        driver.execute_script("arguments[0].click()", export_btn)
        log(2, " -> Export clicked.")
    except TimeoutException:
        dump_diagnostic_state(driver, "step9_export_missing")
        print("[PATH FAILURE] Export button missing inside shadow.")
        return -1

    # ------------------------------------------------------------------
    # STEP 10: Wait for the .zip to finish downloading
    # Chrome writes a .crdownload partial while the transfer is in progress;
    # we wait until a .zip exists and no .crdownload remains.
    # ------------------------------------------------------------------
    log(2, f"Monitoring {TEMP_DIR} for completed .zip (max {TIMEOUT_ZIP} s)...")
    deadline = time.time() + TIMEOUT_ZIP
    zip_path: str | None = None

    while time.time() < deadline:
        files = os.listdir(TEMP_DIR)
        zips = [f for f in files if f.endswith(".zip")]
        partials = [f for f in files if f.endswith(".crdownload")]
        log(3, f" Snapshot: zips={zips}, partials={partials}")
        if zips and not partials:
            zips.sort(key=lambda n: os.path.getmtime(os.path.join(TEMP_DIR, n)), reverse=True)
            zip_path = os.path.join(TEMP_DIR, zips[0])  # Select newest zip
            break
        time.sleep(2)

    if zip_path is None:
        print("\n[DOWNLOAD FAILURE] Timed out waiting for .zip.")
        dump_diagnostic_state(driver, "step10_download_timeout")
        return -1

    log(1, f"Download complete: {zip_path}")

    # ------------------------------------------------------------------
    # STEP 11: Unzip, clean, and append newer rows to the target CSV
    # ------------------------------------------------------------------
    try:
        new_count = process_downloaded_zip(zip_path, OUTPUT_FILE)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] Failed to process downloaded zip: {exc}")
        return -1

    # Clean up the temporary zip
    try:
        os.remove(zip_path)
        log(3, f"Removed temporary zip: {zip_path}")
    except OSError:
        pass

    log(0, f"[SUCCESS] {'[TEST only] ' if TEST else ''}{new_count} new rows written to {OUTPUT_FILE}")
    return new_count


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    """Create download directory, launch browser, run the scraper, clean up."""

    driver: WebDriver | None = None
    new_rows: int = -1
    exit_code = 5

    try:
        global args   # pylint: disable=global-statement
        args = parse_arguments()
        initialize_globals()

        log(2, f'Starting {"headless Chrome" if HEADLESS else "Chrome with visible browser window"}...')
        driver = init_driver()
        new_rows = run_eversource_download(driver, args.user, args.passwd, args.account)  # This is the main called routine
        if new_rows < 0:  # Error during navigation / download
            raise RuntimeError("Download/navigation failed")
        exit_code = 0 if new_rows > 0 else 2  # 0 = new data; 2 = no new data

    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1

    except WebDriverException as exc:
        print(f"UNEXPECTED WEB DRIVER ERROR: {exc}", file=sys.stderr)

        log_path = os.path.join(TEMP_DIR, "chromedriver.log")
        if os.path.exists(log_path):
            print(f"Check ChromeDriver log for details: {log_path}", file=sys.stderr)

            try:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    log_text = fh.read()

                relevant = [
                    line for line in log_text.splitlines()
                    if "ERROR" in line or "WARNING" in line
                ]

                if relevant:
                    for line in relevant[-10:]:
                        print(f"  {line}", file=sys.stderr)
            except OSError:
                pass

        exit_code = 5

    except Exception as exc:  # pylint: disable=broad-except
        print(f"UNEXPECTED ERROR {exc}", file=sys.stderr)
        if driver is not None:
            try:
                print(f"Last URL: {driver.current_url}")
                dump_diagnostic_state(driver, "unexpected_crash")
            except Exception:  # pylint: disable=broad-except
                pass
        exit_code = 5

    finally:
        if driver is not None:
            try:
                driver.quit()
                log(2, "Browser closed.")
            except Exception as exc:  # pylint: disable=broad-except
                print(f"Error closing browser: {exc}", file=sys.stderr)

        if exit_code in (0, 2): # Successful completion so cleanup and exit with proper success code
            try:
                shutil.rmtree(CHROMIUM_TEMP_DIR)
            except OSError as exc:
                print(f"Could not remove temp directory: {exc}", file=sys.stderr)

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
