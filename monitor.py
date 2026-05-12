#!/usr/bin/env python3
"""
Monitor de Pasarela de Pago - Ayuntamiento de Tijuana
Verifica que la pasarela de pago del predial no haya sido suplantada.

Ajustes incluidos:
- Búsqueda de botones alternativos por paso, sin brincar el orden del flujo.
- Clasificación de dominio: portal, pasarela esperada, dominio externo desconocido.
- Evita falsos críticos cuando el navegador sigue en pagos.tijuana.gob.mx.
- Corrige bug de step_fail(..., log).
- Lista botones visibles en logs cuando falla un paso.
"""

import os
import sys
import time
import logging
import smtplib
import argparse
import traceback
import subprocess
import uuid

from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from pathlib import Path

from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    StaleElementReferenceException,
)
from selenium.webdriver.chrome.service import Service


class C:
    """ANSI color code constants for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    MAGENTA = "\033[95m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"


def colorize(color: str, text: str) -> str:
    """Colorize the given text with the specified ANSI color code."""
    return f"{color}{text}{C.RESET}"


BASE_DIR = Path(__file__).parent.resolve()
LOCAL_STORAGE_DIR = BASE_DIR / "predial"
SCREENSHOTS_DIR = LOCAL_STORAGE_DIR / "screenshots"
LOGS_DIR = LOCAL_STORAGE_DIR / "logs"


def ensure_dirs():
    for path in (LOCAL_STORAGE_DIR, SCREENSHOTS_DIR, LOGS_DIR):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            print(colorize(C.YELLOW, f"  📁 Carpeta creada: {path}"))
        else:
            print(colorize(C.GRAY, f"  ✓  Carpeta OK:     {path}"))


try:
    from dotenv import load_dotenv  # pylint: disable=import-error
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

PORTAL_USER = os.getenv("PORTAL_USER", "")
PORTAL_PASS = os.getenv("PORTAL_PASS", "")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ALERT_TO = os.getenv("ALERT_TO", "")
ALERT_TO_CRITICAL = [x.strip() for x in os.getenv(
    "ALERT_TO_CRITICAL", "").split(",") if x.strip()]
ALERT_FROM = os.getenv("ALERT_FROM", SMTP_USER)

EXPECTED_GATEWAY_DOMAIN = os.getenv(
    "EXPECTED_GATEWAY_DOMAIN", "www.adquiramexico.com.mx").lower().strip()
URL_LOGIN = "https://pagos.tijuana.gob.mx/PagosEnLinea/index.aspx"
PORTAL_DOMAIN = urlparse(URL_LOGIN).netloc.lower()

PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "60"))
LOOP_INTERVAL_BUSINESS = int(os.getenv("LOOP_INTERVAL_BUSINESS", "600"))
LOOP_INTERVAL_OFF = int(os.getenv("LOOP_INTERVAL_OFF", "3600"))
BUSINESS_HOUR_START = int(os.getenv("BUSINESS_HOUR_START", "8"))
BUSINESS_HOUR_END = int(os.getenv("BUSINESS_HOUR_END", "17"))
CLAVES_TIMEOUT = int(os.getenv("CLAVES_TIMEOUT", "120"))

IS_DOCKER = os.path.exists("/.dockerenv")
IS_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None

_log_filename = os.getenv("LOG_FILE", "monitor.log")
if _log_filename.startswith("/"):
    _log_filename = Path(_log_filename).name
LOG_FILE = LOGS_DIR / _log_filename
TZ = ZoneInfo("America/Tijuana")


def now_local() -> datetime:
    return datetime.now(TZ)


def is_business_hours() -> bool:
    now = now_local()
    return now.weekday() < 5 and BUSINESS_HOUR_START <= now.hour < BUSINESS_HOUR_END


def current_interval() -> int:
    return LOOP_INTERVAL_BUSINESS if is_business_hours() else LOOP_INTERVAL_OFF


class TijuanaFileFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TZ)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: C.GRAY,
        logging.INFO: C.WHITE,
        logging.WARNING: C.YELLOW,
        logging.ERROR: C.RED,
        logging.CRITICAL: C.BG_RED + C.BOLD,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, C.RESET)
        ts = now_local().strftime("%H:%M:%S")
        level = f"{record.levelname:<8}"
        return f"{colorize(C.GRAY, ts)} {colorize(color, level)} {record.getMessage()}"


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("monitor_pasarela")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter())
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(TijuanaFileFormatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    return logger


def validate_config() -> list:
    errors = []
    if not PORTAL_USER:
        errors.append("PORTAL_USER no definido")
    if not PORTAL_PASS:
        errors.append("PORTAL_PASS no definido")
    if SMTP_USER and not SMTP_PASS:
        errors.append("SMTP_USER definido pero falta SMTP_PASS")
    if SMTP_USER and not ALERT_TO:
        errors.append("SMTP_USER definido pero falta ALERT_TO")
    return errors


def print_config_summary(log: logging.Logger):
    bh = f"Lun-Vie {BUSINESS_HOUR_START:02d}:00–{BUSINESS_HOUR_END:02d}:00"
    env_label = "Railway ☁️" if IS_RAILWAY else "Docker 🐳" if IS_DOCKER else "Local 💻"
    log.info(colorize(C.CYAN + C.BOLD,
             "─── Configuración ──────────────────────────────────────────"))
    log.info("  Portal user   : %s", colorize(
        C.WHITE, PORTAL_USER or "❌ NO DEFINIDO"))
    log.info("  Portal pass   : %s", colorize(
        C.WHITE, "●●●●●●" if PORTAL_PASS else "❌ NO DEFINIDO"))
    log.info("  Portal domain : %s", colorize(C.CYAN, PORTAL_DOMAIN))
    log.info("  Pasarela OK   : %s", colorize(
        C.GREEN, EXPECTED_GATEWAY_DOMAIN))
    log.info("  SMTP          : %s", colorize(
        C.WHITE, SMTP_USER or "⚠️  no configurado (sin alertas)"))
    log.info("  Alertas a     : %s", colorize(
        C.WHITE, ALERT_TO or "⚠️  no configurado"))
    if ALERT_TO_CRITICAL:
        log.info("  Críticas a    : %s", colorize(
            C.WHITE, ", ".join(ALERT_TO_CRITICAL)))
    log.info("  Zona horaria  : %s", colorize(C.CYAN, "America/Tijuana"))
    log.info("  Horario       : %s  → cada %ds / fuera: cada %ds",
             colorize(C.CYAN, bh), LOOP_INTERVAL_BUSINESS, LOOP_INTERVAL_OFF)
    log.info("  Entorno       : %s", colorize(C.CYAN, env_label))
    log.info("  Storage local : %s", colorize(C.GRAY, str(LOCAL_STORAGE_DIR)))
    log.info("  Screenshots   : %s", colorize(C.GRAY, str(SCREENSHOTS_DIR)))
    log.info("  Log           : %s", colorize(C.GRAY, str(LOG_FILE)))


def cleanup_chrome_processes(log: logging.Logger = None):
    try:
        subprocess.run(
            ["pkill", "-f", "chromium|chrome|chromedriver"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if log:
            log.info("  🧹 Limpieza de procesos Chrome/Chromedriver completada")
    except Exception as exc:  # pylint: disable=broad-except
        if log:
            log.warning("  ⚠️ No se pudo limpiar Chrome/Chromedriver: %s", exc)


def create_driver(visible: bool = False, log: logging.Logger = None) -> Chrome:
    cleanup_chrome_processes(log)
    opts = Options()
    is_docker = os.path.exists("/.dockerenv")

    if IS_RAILWAY or is_docker:
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--remote-debugging-port=0")
        opts.add_argument(
            f"--user-data-dir=/tmp/chrome-user-data-{uuid.uuid4()}")
        opts.add_argument(f"--data-path=/tmp/chrome-data-{uuid.uuid4()}")
        opts.add_argument(f"--disk-cache-dir=/tmp/chrome-cache-{uuid.uuid4()}")
    elif visible:
        opts.add_argument("--window-size=1920,1080")
    else:
        opts.add_argument("--window-position=-10000,-10000")
        opts.add_argument("--window-size=1920,1080")

    for arg in (
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--safebrowsing-disable-auto-update",
        "--ignore-certificate-errors",
    ):
        opts.add_argument(arg)

    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
    )

    chrome_binary = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    chromedriver_binary = os.getenv(
        "CHROMEDRIVER_BIN", "/usr/bin/chromedriver")
    if not os.path.exists(chrome_binary):
        raise WebDriverException(
            f"Chrome/Chromium no encontrado: {chrome_binary}")
    if not os.path.exists(chromedriver_binary):
        raise WebDriverException(
            f"chromedriver no encontrado: {chromedriver_binary}")

    opts.binary_location = chrome_binary
    service = Service(executable_path=chromedriver_binary)
    driver = Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.set_script_timeout(PAGE_TIMEOUT)
    return driver


def take_screenshot(driver: Chrome, name: str, log: logging.Logger) -> str:
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{ts}_{name}.png"
    try:
        driver.save_screenshot(str(path))
        log.info("  📸 Screenshot: %s", colorize(C.GRAY, str(path)))
        return str(path)
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("  ⚠️ No se pudo tomar screenshot: %s", exc)
        return ""


def safe_current_url(driver: Chrome, log: logging.Logger = None) -> str:
    try:
        return driver.current_url or ""
    except Exception as exc:  # pylint: disable=broad-except
        if log:
            log.warning("  ⚠️ No se pudo obtener URL actual: %s", exc)
        return ""


def safe_text(element) -> str:
    try:
        for attr in (None, "value", "aria-label", "title", "name", "id"):
            value = element.text if attr is None else element.get_attribute(
                attr)
            value = (value or "").strip()
            if value:
                return value
    except Exception:  # pylint: disable=broad-except
        return ""
    return ""


def is_same_or_subdomain(domain: str, expected: str) -> bool:
    domain = (domain or "").lower().strip()
    expected = (expected or "").lower().strip()
    return bool(domain and expected and (domain == expected or domain.endswith("." + expected)))


def classify_location(driver: Chrome, log: logging.Logger = None) -> tuple:
    current_url = safe_current_url(driver, log)
    domain = urlparse(current_url).netloc.lower()
    if not domain:
        return "unknown", current_url, domain
    if is_same_or_subdomain(domain, PORTAL_DOMAIN):
        return "portal", current_url, domain
    if is_same_or_subdomain(domain, EXPECTED_GATEWAY_DOMAIN):
        return "gateway_ok", current_url, domain
    return "gateway_unknown", current_url, domain


def scroll_and_click(driver: Chrome, element, log: logging.Logger, label: str = "elemento") -> bool:
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", element)
        log.info("  │  Click ejecutado: %s", colorize(C.GRAY, label))
        return True
    except Exception as js_exc:  # pylint: disable=broad-except
        try:
            element.click()
            log.info("  │  Click ejecutado: %s", colorize(C.GRAY, label))
            return True
        except Exception as click_exc:  # pylint: disable=broad-except
            log.warning(
                "  │  No se pudo hacer click en %s: JS=%s | normal=%s", label, js_exc, click_exc)
            return False


def send_alert_email(subject: str, body: str, log: logging.Logger, screenshot_path: str = None, severity: str = "warning") -> bool:
    if not all([SMTP_USER, SMTP_PASS, ALERT_TO]):
        log.warning("SMTP no configurado — alerta solo en consola/log")
        return False

    severity = (severity or "warning").lower()
    if severity == "critical":
        title = "🚨 Alerta crítica de Predial"
        title_color = "#c0392b"
        box_bg = "#fff5f5"
        border_color = "#c0392b"
        footer_color = "#666666"
    else:
        title = "⚠️ Aviso del Monitor de Predial"
        title_color = "#8a6d3b"
        box_bg = "#fffaf0"
        border_color = "#d6b656"
        footer_color = "#777777"

    normal_recipients = [email.strip()
                         for email in ALERT_TO.split(",") if email.strip()]
    recipients = normal_recipients
    if severity == "critical":
        recipients = list(dict.fromkeys(normal_recipients + ALERT_TO_CRITICAL))
    if not recipients:
        log.warning(
            "No hay destinatarios configurados para este tipo de alerta")
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM
    msg["To"] = ", ".join(recipients)

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(body, "plain", "utf-8"))
    alt_part.attach(MIMEText(f"""<html>
<body style="font-family:Arial,sans-serif;padding:20px;background:#ffffff;">
<h2 style="color:{title_color};margin-bottom:16px;">{title}</h2>
<div style="background:{box_bg};padding:15px;border-radius:8px;border-left:4px solid {border_color};white-space:pre-wrap;font-family:Consolas,Monaco,monospace;line-height:1.45;color:#222;">{body}</div>
<p style="color:{footer_color};font-size:12px;margin-top:18px;">Monitor automático — {now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}</p>
</body></html>""", "html", "utf-8"))
    msg.attach(alt_part)

    if screenshot_path and Path(screenshot_path).exists():
        try:
            with open(screenshot_path, "rb") as file:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", f'attachment; filename="{Path(screenshot_path).name}"')
            msg.attach(part)
        except OSError as exc:
            log.warning("No se pudo adjuntar screenshot al correo: %s", exc)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(ALERT_FROM, recipients, msg.as_string())
        log.info("  ✉️  Alerta enviada a %s", colorize(
            C.GREEN, ", ".join(recipients)))
        return True
    except smtplib.SMTPException as exc:
        log.error("  Error SMTP: %s", exc)
        return False


def step_header(num, desc: str, log: logging.Logger):
    log.info(colorize(C.CYAN + C.BOLD, f"\n  ┌─ Paso {num}: {desc}"))


def step_ok(msg: str, log: logging.Logger):
    log.info(colorize(C.GREEN, f"  └─ ✅ {msg}"))


def step_skip(msg: str, log: logging.Logger):
    log.info(colorize(C.GRAY, f"  └─ ⏭  OMITIDO: {msg}"))


def step_warn(msg: str, log: logging.Logger):
    log.warning(colorize(C.BG_YELLOW + C.BOLD, f"  └─ ⚠️  INCIDENCIA: {msg}"))


def step_fail(msg: str, log: logging.Logger):
    log.error(colorize(C.RED, f"  └─ ❌ {msg}"))


def check_maintenance(driver: Chrome, log: logging.Logger) -> bool:
    markers = [
        "mantenimiento",
        "temporalmente fuera de servicio",
        "sitio en mantenimiento",
        "servicio no disponible",
        "portal en mantenimiento",
        "cierre temporal",
        "fuera de servicio",
    ]
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if not any(marker in body_text for marker in markers):
            return False
        hora = now_local().strftime("%H:%M")
        matched = next(
            (marker for marker in markers if marker in body_text), "mantenimiento")
        if is_business_hours():
            step_warn(
                f"Portal en MANTENIMIENTO durante horario laboral ({hora})", log)
        else:
            log.info(colorize(
                C.GRAY, f"  │  Portal en mantenimiento fuera de horario ({hora}) — esperado"))
        log.info("  │  Indicador detectado: %s", colorize(C.GRAY, matched))
        return True
    except NoSuchElementException:
        return False


BUTTON_XPATH_COMMON = (
    "//*[self::a or self::button or self::input[@type='submit'] "
    "or self::input[@type='button'] or self::input[@type='image']]"
)


def xpath_text_contains(*words: str) -> str:
    haystack = (
        "translate(concat(' ', normalize-space(.), ' ', normalize-space(@value), ' ', "
        "normalize-space(@title), ' ', normalize-space(@aria-label), ' '), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÜÑ', "
        "'abcdefghijklmnopqrstuvwxyzáéíóúüñ')"
    )
    conditions = []
    for word in words:
        safe_word = word.lower().replace("'", "")
        conditions.append(f"contains({haystack}, '{safe_word}')")
    return f"{BUTTON_XPATH_COMMON}[{' and '.join(conditions)}]"


DETALLE_CANDIDATES = [
    ("Detalle dentro de fila YY000004", By.XPATH,
     "//tr[contains(normalize-space(.), 'YY000004')]//*[self::a or self::button or self::input][contains(translate(concat(normalize-space(.), ' ', normalize-space(@value)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'detalle')]"),
    ("Detalle link parcial", By.PARTIAL_LINK_TEXT, "Detalle"),
    ("Detalle por texto", By.XPATH, xpath_text_contains("detalle")),
    ("Ver detalle por texto", By.XPATH, xpath_text_contains("ver", "detalle")),
]

PAGO_LINEA_CANDIDATES = [
    ("Pago en Línea ID", By.ID, "MainContent_btnPagarEnLinea"),
    ("Pagar en Línea ID alterno", By.ID, "btnPagarEnLinea"),
    ("Pago en Línea texto", By.XPATH, xpath_text_contains("pago", "línea")),
    ("Pago en Linea texto sin acento", By.XPATH,
     xpath_text_contains("pago", "linea")),
    ("Pagar en Línea texto", By.XPATH, xpath_text_contains("pagar", "línea")),
    ("Pagar en Linea texto sin acento", By.XPATH,
     xpath_text_contains("pagar", "linea")),
]

REALIZAR_PAGO_CANDIDATES = [
    ("Realizar Pago ID", By.ID, "MainContent_btnRealizarPago"),
    ("Realizar Pago ID alterno", By.ID, "btnRealizarPago"),
    ("Realizar Pago texto", By.XPATH, xpath_text_contains("realizar", "pago")),
    ("Pagar texto", By.XPATH, xpath_text_contains("pagar")),
]


def page_text(driver: Chrome) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:  # pylint: disable=broad-except
        return ""


def detect_portal_state(driver: Chrome, log: logging.Logger = None) -> str:
    location, _current_url, _domain = classify_location(driver, log)
    txt = page_text(driver)
    if location == "gateway_ok":
        return "gateway_ok"
    if location == "gateway_unknown":
        return "gateway_unknown"
    if "usuario" in txt and ("contraseña" in txt or "contrasenia" in txt):
        return "login"
    if "claves catastrales registradas" in txt and "yy000004" in txt:
        if "impuesto predial" in txt or "donativo cruz roja" in txt or "total:" in txt:
            return "detalle"
        return "predial_list"
    if "realizar pago" in txt or "confirm" in txt or "pagar" in txt:
        return "confirmation"
    if location == "portal":
        return "portal_unknown"
    return "unknown"


def dump_clickables(driver: Chrome, log: logging.Logger, limit: int = 60):
    try:
        elements = driver.find_elements(By.XPATH, BUTTON_XPATH_COMMON)
        visible = []
        for el in elements:
            try:
                if not el.is_displayed():
                    continue
                visible.append((
                    el.tag_name or "",
                    el.get_attribute("id") or "",
                    el.get_attribute("name") or "",
                    (el.get_attribute("class") or "")[:80],
                    safe_text(el)[:160],
                ))
            except StaleElementReferenceException:
                continue
        log.info("  │  Elementos clickeables visibles detectados: %s", len(visible))
        for idx, (tag, el_id, name, cls, text) in enumerate(visible[:limit], start=1):
            log.info(
                "  │    %02d) tag=%s id=%s name=%s class=%s text/value=%s",
                idx,
                colorize(C.GRAY, tag),
                colorize(C.GRAY, el_id or "-"),
                colorize(C.GRAY, name or "-"),
                colorize(C.GRAY, cls or "-"),
                colorize(C.WHITE, text or "-"),
            )
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("  │  No se pudo listar elementos clickeables: %s", exc)


def find_first_clickable(driver: Chrome, log: logging.Logger, candidates: list, timeout_per_candidate: int = 4):
    for label, by, selector in candidates:
        try:
            el = WebDriverWait(driver, timeout_per_candidate).until(
                EC.element_to_be_clickable((by, selector)))
            log.info("  │  Candidato encontrado: %s", colorize(C.GREEN, label))
            log.info("  │  Selector: %s = %s", colorize(
                C.GRAY, by), colorize(C.GRAY, selector))
            return el, label
        except TimeoutException:
            log.info("  │  No encontrado: %s", colorize(C.GRAY, label))
        except StaleElementReferenceException:
            log.info("  │  Elemento stale, se intenta siguiente candidato: %s", colorize(
                C.GRAY, label))
        except Exception as exc:  # pylint: disable=broad-except
            log.info("  │  Candidato falló: %s → %s",
                     colorize(C.GRAY, label), exc)
    return None, None


def wait_predial_loaded(driver: Chrome, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(EC.any_of(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(normalize-space(.), 'Claves Catastrales Registradas')]")),
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(normalize-space(.), 'Clave Catastral')]")),
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(normalize-space(.), 'YY000004')]")),
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(normalize-space(.), 'Detalle')]")),
    ))


def wait_after_click(driver: Chrome, log: logging.Logger, seconds: int = 2):
    time.sleep(seconds)
    state = detect_portal_state(driver, log)
    location, current_url, domain = classify_location(driver, log)
    log.info("  │  Estado detectado: %s", colorize(C.CYAN, state))
    log.info("  │  Clasificación URL: %s", colorize(C.CYAN, location))
    log.info("  │  URL actual: %s", colorize(C.GRAY, current_url or "N/A"))
    log.info("  │  Dominio actual: %s", colorize(C.GRAY, domain or "N/A"))
    return state


def run_check(visible: bool = False, log: logging.Logger = None, step_delay: int = 2) -> dict:
    result = {
        "ok": False,
        "step": "init",
        "gateway_url": "",
        "gateway_domain": "",
        "domain_match": False,
        "redirect_mismatch": False,
        "maintenance": False,
        "incidence": False,
        "error": None,
        "screenshot": None,
        "timestamp": now_local().isoformat(),
    }

    def pause(label: str = ""):
        if label:
            log.info(colorize(C.GRAY, f"  │  ⏸  {label} ({step_delay}s)"))
        time.sleep(step_delay)

    driver = None
    try:
        step_header(1, "Login en el portal", log)
        driver = create_driver(visible=visible, log=log)
        wait = WebDriverWait(driver, PAGE_TIMEOUT)
        driver.get(URL_LOGIN)
        result["step"] = "login_page_loaded"
        log.info("  │  URL: %s", colorize(C.GRAY, URL_LOGIN))

        in_maintenance = check_maintenance(driver, log)
        result["maintenance"] = in_maintenance
        if in_maintenance:
            result["incidence"] = is_business_hours()
            if result["incidence"]:
                result["error"] = f"Portal en mantenimiento en horario laboral ({now_local().strftime('%H:%M')})"

        try:
            user_input = wait.until(EC.presence_of_element_located(
                (By.ID, "ContentPlaceHolder1_txtUsuario")))
            pass_input = driver.find_element(
                By.ID, "ContentPlaceHolder1_txtContrasenia")
            login_btn = driver.find_element(
                By.ID, "ContentPlaceHolder1_btnLogin")
        except (TimeoutException, NoSuchElementException):
            step_fail(
                "Formulario de login no encontrado — sitio caído o modificado", log)
            result["error"] = "Formulario de login no encontrado"
            result["screenshot"] = take_screenshot(
                driver, "login_not_found", log)
            dump_clickables(driver, log)
            return result

        user_input.clear()
        user_input.send_keys(PORTAL_USER)
        pass_input.clear()
        pass_input.send_keys(PORTAL_PASS)
        login_btn.click()
        result["step"] = "login_submitted"
        log.info("  │  Credenciales enviadas: %s",
                 colorize(C.WHITE, PORTAL_USER))
        pause("Esperando respuesta de login")

        try:
            wait.until(EC.presence_of_element_located(
                (By.LINK_TEXT, "Cerrar Sesión")))
        except TimeoutException:
            try:
                driver.find_element(By.PARTIAL_LINK_TEXT, "Cerrar Sesi")
            except NoSuchElementException:
                step_fail(
                    "Login fallido — credenciales incorrectas o sitio modificado", log)
                result["error"] = "Login fallido"
                result["screenshot"] = take_screenshot(
                    driver, "login_failed", log)
                return result

        step_ok("Login exitoso", log)
        result["step"] = "logged_in"

        step_header(2, "Entrar a Predial desde el botón real del portal", log)
        try:
            predial_btn = wait.until(EC.element_to_be_clickable(
                (By.ID, "ContentPlaceHolder1_predial")))
        except (TimeoutException, NoSuchElementException):
            step_fail("Botón de Predial no encontrado", log)
            result["error"] = "Botón de Predial no encontrado"
            result["screenshot"] = take_screenshot(
                driver, "predial_btn_not_found", log)
            dump_clickables(driver, log)
            return result

        try:
            if not scroll_and_click(driver, predial_btn, log, "ContentPlaceHolder1_predial"):
                raise WebDriverException("No se pudo hacer click en Predial")
            pause("Cargando módulo Predial")
            wait_predial_loaded(driver, CLAVES_TIMEOUT)
            log.info("  │  URL actual: %s", colorize(
                C.GRAY, driver.current_url))
            step_ok("Predial cargado desde postback del portal", log)
            result["step"] = "predial_page"
        except TimeoutException:
            step_fail("La vista de Predial no terminó de cargar", log)
            result["error"] = "La vista de Predial no terminó de cargar"
            result["screenshot"] = take_screenshot(
                driver, "predial_view_timeout", log)
            dump_clickables(driver, log)
            return result
        except WebDriverException as exc:
            step_fail(f"Error al hacer click o cargar Predial: {exc}", log)
            result["error"] = f"Error al hacer click o cargar Predial: {exc}"
            result["screenshot"] = take_screenshot(
                driver, "predial_click_error", log)
            return result

        step_header(3, "Seleccionar clave catastral YY000004", log)
        try:
            log.info("  │  Esperando tabla de claves catastrales (hasta %ss)...", colorize(
                C.GRAY, str(CLAVES_TIMEOUT)))
            WebDriverWait(driver, CLAVES_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(normalize-space(.), 'YY000004')]"))
            )
            state = detect_portal_state(driver, log)
            log.info("  │  Estado antes de detalle: %s",
                     colorize(C.CYAN, state))

            detalle_link = None
            found_label = None
            rows = driver.find_elements(By.TAG_NAME, "tr")
            for row in rows:
                try:
                    if "YY000004" in row.text:
                        try:
                            detalle_link = row.find_element(
                                By.PARTIAL_LINK_TEXT, "Detalle")
                            found_label = "Detalle dentro de fila YY000004"
                            break
                        except NoSuchElementException:
                            continue
                except StaleElementReferenceException:
                    continue

            if detalle_link is None:
                detalle_link, found_label = find_first_clickable(
                    driver, log, DETALLE_CANDIDATES, timeout_per_candidate=4)

            if detalle_link is None:
                state = detect_portal_state(driver, log)
                if state == "detalle":
                    step_ok("Detalle de clave ya estaba cargado", log)
                    result["step"] = "detalle_clave"
                else:
                    step_fail(
                        "Clave YY000004 encontrada, pero no hay botón/enlace Detalle", log)
                    result["error"] = "Botón/enlace Detalle no encontrado para YY000004"
                    result["screenshot"] = take_screenshot(
                        driver, "detalle_not_found", log)
                    dump_clickables(driver, log)
                    return result
            else:
                if not scroll_and_click(driver, detalle_link, log, found_label or "Detalle"):
                    raise WebDriverException(
                        "No se pudo hacer click en Detalle")
                pause("Cargando detalle de clave catastral")
                wait_after_click(driver, log, seconds=1)
                step_ok("Detalle de clave cargado", log)
                result["step"] = "detalle_clave"
        except TimeoutException:
            step_fail(
                f"Timeout esperando tabla de claves catastrales ({CLAVES_TIMEOUT}s)", log)
            result["error"] = f"Timeout en tabla de claves ({CLAVES_TIMEOUT}s)"
            result["screenshot"] = take_screenshot(
                driver, "claves_timeout", log)
            dump_clickables(driver, log)
            return result
        except WebDriverException as exc:
            step_fail(f"Error en selección de Detalle: {exc}", log)
            result["error"] = f"Error en selección de Detalle: {exc}"
            result["screenshot"] = take_screenshot(
                driver, "detalle_click_error", log)
            return result

        step_header(
            4, "Click en 'Pago en Línea' si existe en esta pantalla", log)
        state = detect_portal_state(driver, log)
        log.info("  │  Estado antes de Pago en Línea: %s",
                 colorize(C.CYAN, state))
        pago_btn, pago_label = find_first_clickable(
            driver, log, PAGO_LINEA_CANDIDATES, timeout_per_candidate=3)
        if pago_btn:
            if scroll_and_click(driver, pago_btn, log, pago_label):
                step_ok("Avanzando desde Pago en Línea", log)
                result["step"] = "pago_en_linea_clicked"
                pause("Cargando confirmación de pago")
                wait_after_click(driver, log, seconds=1)
            else:
                step_warn(
                    "Se encontró 'Pago en Línea' pero no se pudo hacer click", log)
        else:
            step_skip(
                "Botón 'Pago en Línea' no existe en esta vista — se revisará el siguiente paso en orden", log)

        step_header(5, "Click en 'Realizar Pago' / botón final de pago", log)
        state = detect_portal_state(driver, log)
        log.info("  │  Estado antes de Realizar Pago: %s",
                 colorize(C.CYAN, state))
        realizar_btn, realizar_label = find_first_clickable(
            driver, log, REALIZAR_PAGO_CANDIDATES, timeout_per_candidate=4)

        if not realizar_btn:
            location, current_url, domain = classify_location(driver, log)
            result["gateway_url"] = current_url
            result["gateway_domain"] = domain
            if location == "gateway_ok":
                result["domain_match"] = True
                result["ok"] = True
                step_ok(
                    "Ya se llegó a la pasarela legítima antes del botón final", log)
            elif location == "gateway_unknown":
                result["redirect_mismatch"] = True
                result["error"] = f"DOMINIO NO VALIDADO | esperado: {EXPECTED_GATEWAY_DOMAIN} | detectado: {domain or 'N/A'} | url: {current_url or 'N/A'}"
                result["screenshot"] = take_screenshot(
                    driver, "ALERTA_SUPLANTACION", log)
                step_fail(result["error"], log)
                return result
            else:
                step_fail(
                    "No se encontró ningún botón válido para continuar al pago", log)
                result["error"] = "Botón 'Realizar Pago' no encontrado en ninguna ruta"
                result["redirect_mismatch"] = False
                result["screenshot"] = take_screenshot(
                    driver, "pago_btn_not_found", log)
                dump_clickables(driver, log)
                return result
        else:
            if not scroll_and_click(driver, realizar_btn, log, realizar_label):
                step_fail(
                    "Se encontró botón de pago, pero no se pudo hacer click", log)
                result["error"] = "No se pudo hacer click en botón de pago"
                result["screenshot"] = take_screenshot(
                    driver, "pago_click_failed", log)
                dump_clickables(driver, log)
                return result
            result["step"] = "gateway_redirect"
            log.info("  │  Botón final presionado")
            log.info("  │  Esperando redirección a pasarela...")
            pause("Redirigiendo a pasarela de pago")

        step_header(6, "Verificar dominio de la pasarela", log)
        try:
            WebDriverWait(driver, 35).until(lambda d: classify_location(d)[
                0] in ("gateway_ok", "gateway_unknown"))
        except TimeoutException:
            log.warning(
                "  ⚠️ Timeout esperando salida del portal; se usará la URL disponible")

        location, current_url, gateway_domain = classify_location(driver, log)
        result["gateway_url"] = current_url
        result["gateway_domain"] = gateway_domain

        log.info("  │  Clasificación    : %s", colorize(C.CYAN, location))
        log.info("  │  URL detectada    : %s",
                 colorize(C.WHITE, current_url or "N/A"))
        log.info("  │  Dominio detectado: %s", colorize(
            C.WHITE, gateway_domain or "N/A"))
        log.info("  │  Dominio esperado : %s", colorize(
            C.GREEN, EXPECTED_GATEWAY_DOMAIN))
        result["screenshot"] = take_screenshot(
            driver, "gateway_evidencia", log)

        if location == "gateway_ok":
            result["domain_match"] = True
            result["ok"] = True
            step_ok("PASARELA LEGÍTIMA — %s" % colorize(
                C.GREEN + C.BOLD, gateway_domain), log)
        elif location == "portal":
            result["redirect_mismatch"] = False
            result["error"] = f"No se llegó a la pasarela de pago; el navegador sigue en el portal ({gateway_domain or 'N/A'}). URL: {current_url or 'N/A'}"
            step_warn(result["error"], log)
            return result
        elif location == "gateway_unknown":
            result["redirect_mismatch"] = True
            result["error"] = f"DOMINIO NO VALIDADO | esperado: {EXPECTED_GATEWAY_DOMAIN} | detectado: {gateway_domain or 'N/A'} | url: {current_url or 'N/A'}"
            result["screenshot"] = take_screenshot(
                driver, "ALERTA_SUPLANTACION", log)
            step_fail(result["error"], log)
            return result
        else:
            result["redirect_mismatch"] = False
            result["error"] = f"No se pudo determinar la URL de pasarela. URL actual: {current_url or 'N/A'}"
            step_warn(result["error"], log)
            return result

        step_header(7, "Cerrar sesión", log)
        try:
            try:
                driver.find_element(By.ID, "regresar").click()
                pause("Regresando")
            except NoSuchElementException:
                try:
                    driver.back()
                    time.sleep(1)
                    driver.back()
                except WebDriverException:
                    pass
            try:
                driver.find_element(By.PARTIAL_LINK_TEXT,
                                    "Cerrar Sesi").click()
                step_ok("Sesión cerrada", log)
            except NoSuchElementException:
                step_skip(
                    "Enlace de cierre de sesión no encontrado — no crítico", log)
            except WebDriverException as exc:
                step_skip(f"Logout no completado — no crítico: {exc}", log)
        except WebDriverException as exc:
            step_skip(
                f"Error de WebDriver durante logout — no crítico: {exc}", log)

        result["step"] = "completed"

    except WebDriverException as exc:
        if result.get("ok"):
            log.warning(
                "WebDriverException posterior a validación exitosa: %s", exc)
            result["step"] = "completed_with_noncritical_webdriver_issue"
        else:
            step_fail(f"WebDriverException: {exc}", log)
            result["error"] = f"WebDriverException: {exc}"
            if driver:
                result["screenshot"] = take_screenshot(
                    driver, "webdriver_error", log)
    except Exception as exc:  # pylint: disable=broad-except
        step_fail(f"Error inesperado: {exc}", log)
        result["error"] = f"{exc}\n{traceback.format_exc()}"
        if driver:
            result["screenshot"] = take_screenshot(
                driver, "error_inesperado", log)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:  # pylint: disable=broad-except
                pass
    return result


def process_result(result: dict, log: logging.Logger):
    ts = result.get("timestamp", now_local().isoformat())
    print()

    if result.get("incidence"):
        log.warning(colorize(C.BG_YELLOW + C.BOLD,
                    "  ⚠️   INCIDENCIA: Portal en mantenimiento en horario laboral  "))
        send_alert_email(
            "⚠️ WARNING: Portal Predial Tijuana en mantenimiento (horario laboral)",
            (
                f"Nivel      : WARNING\n"
                f"Fecha/Hora : {ts}\n"
                f"Horario    : Lun-Vie {BUSINESS_HOUR_START:02d}:00–{BUSINESS_HOUR_END:02d}:00\n"
                f"Detalle    : {result.get('error', 'Portal en mantenimiento')}\n"
            ),
            log=log,
            screenshot_path=result.get("screenshot"),
            severity="warning",
        )

    if result["ok"]:
        log.info(colorize(C.BG_GREEN + C.BOLD, "  ✅  VERIFICACIÓN EXITOSA  "))
        log.info("  Pasarela : %s", colorize(
            C.GREEN, result["gateway_domain"]))
        if result.get("maintenance"):
            log.info("  Nota     : %s", colorize(
                C.GRAY, "Portal en mantenimiento detectado durante la ejecución"))
        log.info("  Hora     : %s", colorize(C.GRAY, ts))
        return

    if result.get("redirect_mismatch"):
        log.critical(colorize(
            C.BG_RED + C.BOLD, "  🚨  ALERTA CRÍTICA: REDIRECCIÓN A DOMINIO NO ESPERADO  "))
        body = (
            f"Nivel                : CRITICAL\n"
            f"Fecha/Hora           : {ts}\n"
            f"Último paso          : {result['step']}\n"
            f"Dominio esperado     : {EXPECTED_GATEWAY_DOMAIN}\n"
            f"Dominio detectado    : {result.get('gateway_domain', 'N/A')}\n"
            f"URL completa         : {result.get('gateway_url', 'N/A')}\n"
            f"Coincidencia dominio : {result.get('domain_match', False)}\n"
            f"\nDetalle del error:\n{result.get('error', 'Sin detalle')}\n"
            f"\nScreenshot           : {result.get('screenshot', 'N/A')}\n"
        )
        log.critical(body)
        send_alert_email(
            "🚨 CRITICAL: Pasarela de Pago Tijuana — Dominio no esperado detectado",
            body,
            log=log,
            screenshot_path=result.get("screenshot"),
            severity="critical",
        )
        return

    log.warning(colorize(C.BG_YELLOW + C.BOLD,
                "  ⚠️  WARNING: INCIDENCIA OPERATIVA DEL MONITOR  "))
    body = (
        f"Nivel                : WARNING\n"
        f"Fecha/Hora           : {ts}\n"
        f"Último paso          : {result['step']}\n"
        f"Dominio esperado     : {EXPECTED_GATEWAY_DOMAIN}\n"
        f"Dominio detectado    : {result.get('gateway_domain', 'N/A')}\n"
        f"URL completa         : {result.get('gateway_url', 'N/A')}\n"
        f"Coincidencia dominio : {result.get('domain_match', False)}\n"
        f"En mantenimiento     : {result.get('maintenance', False)}\n"
        f"\nDetalle del error:\n{result.get('error', 'Sin detalle')}\n"
        f"\nScreenshot           : {result.get('screenshot', 'N/A')}\n"
    )
    log.warning(body)
    send_alert_email(
        "⚠️ WARNING: Monitor Predial Tijuana — incidencia operativa",
        body,
        log=log,
        screenshot_path=result.get("screenshot"),
        severity="warning",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Monitor de pasarela de pago — Predial Tijuana",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python3 monitor.py\n"
            "  python3 monitor.py --visible --step-delay 5\n"
            "  python3 monitor.py --loop\n"
            "  python3 monitor.py --loop --interval 300\n"
        )
    )
    parser.add_argument("--loop", action="store_true",
                        help="Ejecutar en loop continuo")
    parser.add_argument("--interval", type=int, default=0,
                        help="Forzar intervalo fijo en segundos (ignora horario)")
    parser.add_argument("--visible", action="store_true",
                        help="Mostrar ventana del navegador (solo local, no Docker/Railway)")
    parser.add_argument("--step-delay", dest="step_delay", type=int,
                        default=2, help="Pausa entre pasos en segundos (default: 2)")
    args = parser.parse_args()

    show_browser = args.visible and not IS_RAILWAY and not IS_DOCKER

    print(colorize(C.CYAN + C.BOLD,
          "\n━━━ Monitor de Pasarela de Pago — Predial Tijuana ━━━━━━━━━━━━━"))
    print(colorize(C.CYAN, "  Verificando directorios..."))
    ensure_dirs()
    log = setup_logger()

    log.info(colorize(C.CYAN, "\n  Validando configuración..."))
    config_errors = validate_config()
    if config_errors:
        for err in config_errors:
            log.error("  ❌ %s", err)
        log.error(colorize(C.RED, "\n  Corrige los errores antes de continuar."))
        sys.exit(1)

    log.info(colorize(C.GREEN, "  ✅ Configuración válida"))
    print_config_summary(log)

    if show_browser:
        log.info("  🖥️  Modo visible — pausa entre pasos: %ss",
                 colorize(C.WHITE, str(args.step_delay)))

    print(colorize(C.CYAN + C.BOLD,
          "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"))

    if args.loop:
        run_count = 0
        while True:
            run_count += 1
            interval = args.interval if args.interval > 0 else current_interval()
            bh_label = colorize(C.GREEN, "horario laboral") if is_business_hours(
            ) else colorize(C.GRAY, "fuera de horario")
            log.info(colorize(C.MAGENTA + C.BOLD,
                     f"\n  ┌────────── Ejecución #{run_count} [{bh_label}{C.MAGENTA + C.BOLD}] ──────────────────────"))
            try:
                result = run_check(visible=show_browser,
                                   log=log, step_delay=args.step_delay)
                process_result(result, log)
            except Exception as exc:  # pylint: disable=broad-except
                log.error("Error inesperado en ciclo: %s", exc)
                send_alert_email(
                    "⚠️ WARNING: Error operativo en monitor de pasarela",
                    traceback.format_exc(),
                    log=log,
                    severity="warning",
                )
            next_ts = datetime.fromtimestamp(
                time.time() + interval, TZ).strftime("%H:%M:%S")
            log.info("\n  Próxima ejecución a las %s (%ds — %s)", colorize(
                C.CYAN, next_ts), interval, "horario laboral" if is_business_hours() else "fuera de horario")
            time.sleep(interval)
    else:
        result = run_check(visible=show_browser, log=log,
                           step_delay=args.step_delay)
        process_result(result, log)
        sys.exit(0 if result["ok"] else 1)


# Comentario ajuste
if __name__ == "__main__":
    main()
