#!/usr/bin/env python3
"""
Monitor de Pasarela de Pago - Ayuntamiento de Tijuana
=====================================================
Verifica que la pasarela de pago del predial no haya sido suplantada.

Además:
- Guarda screenshots localmente
- Guarda logs localmente

Estructura local:
  predial/
    screenshots/
    logs/

Uso:
  python3 monitor.py
  python3 monitor.py --loop
  python3 monitor.py --visible
  python3 monitor.py --visible --step-delay 5
"""

import os
import sys
import time
import logging
import smtplib
import argparse
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlparse
from pathlib import Path
from email.mime.base import MIMEBase
from email import encoders

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service

# ──────────────────────────────────────────────────────────────────────────────
# COLORES ANSI
# ──────────────────────────────────────────────────────────────────────────────


class C:  # pylint: disable=too-few-public-methods
    """Códigos de color ANSI para la terminal."""
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
    """Envuelve texto en un código de color ANSI."""
    return f"{color}{text}{C.RESET}"


# ──────────────────────────────────────────────────────────────────────────────
# RUTAS
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
LOCAL_STORAGE_DIR = BASE_DIR / "predial"
SCREENSHOTS_DIR = LOCAL_STORAGE_DIR / "screenshots"
LOGS_DIR = LOCAL_STORAGE_DIR / "logs"


def ensure_dirs():
    """Crea los directorios de trabajo si no existen."""
    for _name, path in {
        "root": LOCAL_STORAGE_DIR,
        "screenshots": SCREENSHOTS_DIR,
        "logs": LOGS_DIR,
    }.items():
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            print(colorize(C.YELLOW, f"  📁 Carpeta creada: {path}"))
        else:
            print(colorize(C.GRAY, f"  ✓  Carpeta OK:     {path}"))


# ──────────────────────────────────────────────────────────────────────────────
# VARIABLES DE ENTORNO
# ──────────────────────────────────────────────────────────────────────────────

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
ALERT_TO_CRITICAL = [
    "kperez@tijuana.gob.mx",
    "gehernandez@tijuana.gob.mx"
]
ALERT_FROM = os.getenv("ALERT_FROM", SMTP_USER)

EXPECTED_GATEWAY_DOMAIN = os.getenv(
    "EXPECTED_GATEWAY_DOMAIN",
    "www.adquiramexico.com.mx"
)

URL_LOGIN = "https://pagos.tijuana.gob.mx/PagosEnLinea/index.aspx"

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


# ──────────────────────────────────────────────────────────────────────────────
# HORARIO
# ──────────────────────────────────────────────────────────────────────────────
def now_local() -> datetime:
    """Fecha/hora actual en zona horaria de Tijuana."""
    return datetime.now(TZ)


def is_business_hours() -> bool:
    """Devuelve True si ahora es lunes-viernes entre las horas configuradas."""
    now = now_local()
    return now.weekday() < 5 and BUSINESS_HOUR_START <= now.hour < BUSINESS_HOUR_END


def current_interval() -> int:
    """Intervalo de loop según el horario actual."""
    return LOOP_INTERVAL_BUSINESS if is_business_hours() else LOOP_INTERVAL_OFF


class TijuanaFileFormatter(logging.Formatter):
    """Formatter para archivo usando zona horaria de Tijuana."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()

# ──────────────────────────────────────────────────────────────────────────────
# LOGGER
# ──────────────────────────────────────────────────────────────────────────────


class ColorFormatter(logging.Formatter):
    """Formatter con colores ANSI para la salida en consola."""

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
    """Configura logger con handler de consola y archivo."""
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


# ──────────────────────────────────────────────────────────────────────────────
# VALIDACIÓN DE CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────

def validate_config() -> list:
    """Valida las variables de entorno obligatorias. Retorna lista de errores."""
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
    """Muestra un resumen visual de la configuración activa."""
    bh = f"Lun-Vie {BUSINESS_HOUR_START:02d}:00–{BUSINESS_HOUR_END:02d}:00"
    log.info(colorize(C.CYAN + C.BOLD,
                      "─── Configuración ──────────────────────────────────────────"))
    log.info("  Portal user   : %s", colorize(
        C.WHITE, PORTAL_USER or "❌ NO DEFINIDO"))
    log.info("  Portal pass   : %s", colorize(
        C.WHITE, "●●●●●●" if PORTAL_PASS else "❌ NO DEFINIDO"))
    log.info("  Pasarela OK   : %s", colorize(
        C.GREEN, EXPECTED_GATEWAY_DOMAIN))
    log.info("  SMTP          : %s", colorize(
        C.WHITE, SMTP_USER or "⚠️  no configurado (sin alertas)"))
    log.info("  Alertas a     : %s", colorize(
        C.WHITE, ALERT_TO or "⚠️  no configurado"))
    log.info("  Zona horaria  : %s", colorize(C.CYAN, "America/Tijuana"))
    log.info("  Horario       : %s  → cada %ds / fuera: cada %ds",
             colorize(C.CYAN, bh), LOOP_INTERVAL_BUSINESS, LOOP_INTERVAL_OFF)
    if IS_RAILWAY:
        env_label = "Railway ☁️"
    elif IS_DOCKER:
        env_label = "Docker 🐳"
    else:
        env_label = "Local 💻"
    log.info("  Entorno       : %s", colorize(C.CYAN, env_label))
    log.info("  Storage local : %s", colorize(C.GRAY, str(LOCAL_STORAGE_DIR)))
    log.info("  Screenshots   : %s", colorize(C.GRAY, str(SCREENSHOTS_DIR)))
    log.info("  Log           : %s", colorize(C.GRAY, str(LOG_FILE)))

# ──────────────────────────────────────────────────────────────────────────────
# SELENIUM — driver adaptativo local/Railway
# ──────────────────────────────────────────────────────────────────────────────


def create_driver(visible: bool = False) -> webdriver.Chrome:
    """
    Crea ChromeDriver adaptado al entorno.
    """
    opts = Options()
    is_docker = os.path.exists("/.dockerenv")

    if IS_RAILWAY or is_docker:
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")

    elif visible:

        opts.add_argument("--window-size=1920,1080")

    else:

        opts.add_argument("--window-position=-10000,-10000")
        opts.add_argument("--window-size=1920,1080")

    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-first-run")
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
    )
    chrome_bin_candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]

    chromedriver_candidates = [
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]

    chrome_binary = next(
        (p for p in chrome_bin_candidates if os.path.exists(p)), None)
    chromedriver_binary = next(
        (p for p in chromedriver_candidates if os.path.exists(p)), None)

    if chrome_binary:
        opts.binary_location = chrome_binary

    if not chromedriver_binary:

        raise WebDriverException(
            "chromedriver no encontrado. Revisar instalación en contenedor."
        )

    service = Service(executable_path=chromedriver_binary)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.set_script_timeout(PAGE_TIMEOUT)
    return driver

# ──────────────────────────────────────────────────────────────────────────────
# UTILIDADES
# ──────────────────────────────────────────────────────────────────────────────


def take_screenshot(driver: webdriver.Chrome, name: str, log: logging.Logger) -> str:
    """Guarda una captura de pantalla y retorna la ruta del archivo."""
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{ts}_{name}.png"
    driver.save_screenshot(str(path))
    log.info("  📸 Screenshot: %s", colorize(C.GRAY, str(path)))
    return str(path)


def send_alert_email(
    subject: str,
    body: str,
    log: logging.Logger,
    screenshot_path: str = None,
    severity: str = "warning",
) -> bool:
    """Envía un correo de alerta vía SMTP y adjunta screenshot si existe."""

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
    critical_recipients = [email.strip()
                           for email in ALERT_TO_CRITICAL if email.strip()]

    if severity == "critical":
        recipients = list(dict.fromkeys(
            normal_recipients + critical_recipients))
    else:
        recipients = normal_recipients

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
    alt_part.attach(MIMEText(
        f"""<html>
        <body style="font-family:Arial,sans-serif;padding:20px;background:#ffffff;">
            <h2 style="color:{title_color};margin-bottom:16px;">{title}</h2>
            <div style="
                background:{box_bg};
                padding:15px;
                border-radius:8px;
                border-left:4px solid {border_color};
                white-space:pre-wrap;
                font-family:Consolas,Monaco,monospace;
                line-height:1.45;
                color:#222;
            ">{body}</div>
            <p style="color:{footer_color};font-size:12px;margin-top:18px;">
                Monitor automático — {now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}
            </p>
        </body>
        </html>""",
        "html", "utf-8"
    ))
    msg.attach(alt_part)

    if screenshot_path and Path(screenshot_path).exists():
        try:
            with open(screenshot_path, "rb") as file:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{Path(screenshot_path).name}"'
            )
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
        log.info("  ✉️  Alerta enviada a %s", colorize(C.GREEN, ALERT_TO))
        return True
    except smtplib.SMTPException as exc:
        log.error("  Error SMTP: %s", exc)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS VISUALES DE PASOS
# ──────────────────────────────────────────────────────────────────────────────


def step_header(num, desc: str, log: logging.Logger):
    """Imprime el encabezado de un paso del flujo."""
    log.info(colorize(C.CYAN + C.BOLD, f"\n  ┌─ Paso {num}: {desc}"))


def step_ok(msg: str, log: logging.Logger):
    """Marca un paso como completado exitosamente."""
    log.info(colorize(C.GREEN, f"  └─ ✅ {msg}"))


def step_skip(msg: str, log: logging.Logger):
    """Marca un paso como omitido intencionalmente."""
    log.info(colorize(C.GRAY, f"  └─ ⏭  OMITIDO: {msg}"))


def step_warn(msg: str, log: logging.Logger):
    """Marca un paso con advertencia."""
    log.warning(colorize(C.BG_YELLOW + C.BOLD, f"  └─ ⚠️  INCIDENCIA: {msg}"))


def step_fail(msg: str, log: logging.Logger):
    """Marca un paso como fallido."""
    log.error(colorize(C.RED, f"  └─ ❌ {msg}"))


# ──────────────────────────────────────────────────────────────────────────────
# DETECCIÓN DE MANTENIMIENTO
# ──────────────────────────────────────────────────────────────────────────────

def check_maintenance(driver: webdriver.Chrome, log: logging.Logger) -> bool:
    """
    Detecta mantenimiento del portal.

    NOTA:
    El panel ContentPlaceHolder1_pnlMantenimientoLogin es el formulario normal
    de login, así que NO sirve por sí solo para detectar mantenimiento.
    """
    maintenance_markers = [
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

        # Si no hay ninguna frase típica de mantenimiento, no marcar incidencia
        if not any(marker in body_text for marker in maintenance_markers):
            return False

        # Si sí hay texto de mantenimiento, registrar incidencia
        hora = now_local().strftime("%H:%M")

        matched = next(
            (marker for marker in maintenance_markers if marker in body_text),
            "mantenimiento"
        )

        if is_business_hours():
            step_warn(
                f"Portal en MANTENIMIENTO durante horario laboral ({hora})", log)
        else:
            log.info(colorize(
                C.GRAY,
                f"  │  Portal en mantenimiento fuera de horario ({hora}) — esperado"
            ))

        log.info("  │  Indicador detectado: %s", colorize(C.GRAY, matched))
        return True

    except NoSuchElementException:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# HELPER DE ESPERA DE CARGA DE PREDIAL
# ──────────────────────────────────────────────────────────────────────────────


def wait_predial_loaded(driver: webdriver.Chrome, timeout: int) -> None:
    """Espera a que la vista de Predial cargue usando señales del contenido."""
    wait = WebDriverWait(driver, timeout)
    wait.until(
        EC.any_of(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//*[contains(normalize-space(.), 'Claves Catastrales Registradas')]")
            ),
            EC.presence_of_element_located(
                (By.XPATH,
                 "//*[contains(normalize-space(.), 'Clave Catastral')]")
            ),
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(normalize-space(.), 'YY000004')]")
            ),
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(normalize-space(.), 'Detalle')]")
            ),
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# VERIFICACIÓN PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────


def run_check(
    visible: bool = False,
    log: logging.Logger = None,
    step_delay: int = 2,
) -> dict:
    """
    Ejecuta el flujo completo de verificación.
    """
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
        # Paso 1: Login
        step_header(1, "Login en el portal", log)
        driver = create_driver(visible=visible)
        wait = WebDriverWait(driver, PAGE_TIMEOUT)

        driver.get(URL_LOGIN)
        result["step"] = "login_page_loaded"
        log.info("  │  URL: %s", colorize(C.GRAY, URL_LOGIN))

        in_maintenance = check_maintenance(driver, log)
        result["maintenance"] = in_maintenance
        if in_maintenance:
            result["incidence"] = is_business_hours()
            if result["incidence"]:
                result["error"] = (
                    f"Portal en mantenimiento en horario laboral "
                    f"({now_local().strftime('%H:%M')})"
                )

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

        # Paso 2: Clic en trámite predial
        step_header(2, "Entrar a Predial desde el botón real del portal", log)
        try:
            predial_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.ID, "ContentPlaceHolder1_predial"))
            )
        except TimeoutException:
            step_fail("Botón de Predial no encontrado", log)
            result["error"] = "Botón de Predial no encontrado"
            result["screenshot"] = take_screenshot(
                driver, "predial_btn_not_found", log)
            return result
        except NoSuchElementException:
            step_fail("Elemento de Predial no encontrado", log)
            result["error"] = "Elemento de Predial no encontrado"
            result["screenshot"] = take_screenshot(
                driver, "predial_not_found", log)
            return result
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", predial_btn)
            time.sleep(1)
            driver.execute_script("arguments[0].click();", predial_btn)
            log.info("  │  Click en botón Predial: %s",
                     colorize(C.GRAY, "ContentPlaceHolder1_predial"))
            pause("Cargando módulo Predial")
            wait_predial_loaded(driver, CLAVES_TIMEOUT)
            log.info("  │  URL actual: %s", colorize(
                C.GRAY, driver.current_url))
            log.info("  │  Vista Predial detectada por contenido")
            step_ok("Predial cargado desde postback del portal", log)
            result["step"] = "predial_page"
        except TimeoutException:
            step_fail("La vista de Predial no terminó de cargar", log)
            result["error"] = "La vista de Predial no terminó de cargar"
            result["screenshot"] = take_screenshot(
                driver, "predial_view_timeout", log)
            return result
        except WebDriverException as exc:
            step_fail(f"Error al hacer click o cargar Predial: {exc}", log)
            result["error"] = f"Error al hacer click o cargar Predial: {exc}"
            result["screenshot"] = take_screenshot(
                driver, "predial_click_error", log)
            return result

        # Paso 3: Clave catastral
        step_header(3, "Seleccionar clave catastral YY000004", log)
        try:
            log.info("  │  Esperando tabla de claves catastrales (hasta %ss)...",
                     colorize(C.GRAY, str(CLAVES_TIMEOUT)))
            WebDriverWait(driver, CLAVES_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.PARTIAL_LINK_TEXT, "Detalle"))
            )
            rows = driver.find_elements(By.TAG_NAME, "tr")
            detalle_link = None

            for row in rows:
                if "YY000004" in row.text:
                    try:
                        detalle_link = row.find_element(
                            By.PARTIAL_LINK_TEXT, "Detalle")
                        break
                    except NoSuchElementException:
                        continue

            if detalle_link is None:
                step_fail(
                    "Clave catastral YY000004 no encontrada en la tabla", log)
                result["error"] = "Clave catastral YY000004 no encontrada"
                result["screenshot"] = take_screenshot(
                    driver, "clave_not_found", log)
                return result

            detalle_link.click()

            log.info("  │  Click en Detalle de YY000004")

        except TimeoutException:

            step_fail(
                f"Timeout esperando tabla de claves catastrales ({CLAVES_TIMEOUT}s)", log)
            result["error"] = f"Timeout en tabla de claves ({CLAVES_TIMEOUT}s)"
            result["screenshot"] = take_screenshot(
                driver, "claves_timeout", log)
            return result

        pause("Cargando detalle de clave catastral")
        step_ok("Detalle de clave cargado", log)
        result["step"] = "detalle_clave"

        # Paso 4: Pago en Línea
        step_header(4, "Click en 'Pago en Línea'", log)
        try:
            pago_btn = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.ID, "MainContent_btnPagarEnLinea")))
            driver.execute_script("arguments[0].click();", pago_btn)
            log.info(colorize(C.GREEN, "  │  ✓ Botón encontrado y presionado"))
            step_ok("Avanzando a página de confirmación", log)
            pause("Cargando confirmación de pago")
            result["step"] = "pago_confirmacion"
        except (TimeoutException, NoSuchElementException):
            step_skip(
                "Botón 'Pago en Línea' no existe en esta vista — pasando directo al paso 5 ('Realizar Pago')",
                log
            )

        # Paso 5: Realizar Pago
        step_header(5, "Click en 'Realizar Pago'", log)
        try:
            realizar_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.ID, "MainContent_btnRealizarPago")))
            realizar_btn.click()
            log.info("  │  Botón presionado")
        except (TimeoutException, NoSuchElementException):
            step_fail(
                "Botón 'Realizar Pago' no encontrado (ni después del paso 4 ni directamente)", log)
            result["error"] = "Botón 'Realizar Pago' no encontrado en ninguna ruta"
            result["screenshot"] = take_screenshot(
                driver, "pago_btn_not_found", log)
            return result

        log.info("  │  Esperando redirección a pasarela...")
        pause("Redirigiendo a pasarela de pago")
        result["step"] = "gateway_redirect"

        # Paso 6: Verificar dominio
        step_header(6, "Verificar dominio de la pasarela", log)
        current_url = driver.current_url
        parsed = urlparse(current_url)
        gateway_domain = parsed.netloc.lower()
        result["gateway_url"] = current_url
        result["gateway_domain"] = gateway_domain

        log.info("  │  URL detectada    : %s", colorize(C.WHITE, current_url))
        log.info("  │  Dominio detectado: %s",
                 colorize(C.WHITE, gateway_domain))
        log.info("  │  Dominio esperado : %s", colorize(
            C.GREEN, EXPECTED_GATEWAY_DOMAIN))

        result["screenshot"] = take_screenshot(
            driver, "gateway_evidencia", log)

        if EXPECTED_GATEWAY_DOMAIN.lower() in gateway_domain:
            result["domain_match"] = True
            result["ok"] = True
            step_ok("PASARELA LEGÍTIMA — %s" % colorize(
                C.GREEN + C.BOLD, gateway_domain), log)
        else:
            result["redirect_mismatch"] = True
            result["error"] = (
                f"DOMINIO SUPLANTADO | "
                f"esperado: {EXPECTED_GATEWAY_DOMAIN} | "
                f"detectado: {gateway_domain} | "
                f"url: {current_url}"
            )
            result["screenshot"] = take_screenshot(
                driver, "ALERTA_SUPLANTACION", log)
            step_fail(result["error"], log)

        # Paso 7: Logout

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

# ──────────────────────────────────────────────────────────────────────────────
# PROCESAR RESULTADO
# ──────────────────────────────────────────────────────────────────────────────


def process_result(result: dict, log: logging.Logger):
    """Procesa el resultado: éxito, warning o alerta crítica."""
    ts = result.get("timestamp", now_local().isoformat())
    print()

    # 1. Warning por mantenimiento en horario laboral
    if result.get("incidence"):
        log.warning(colorize(
            C.BG_YELLOW + C.BOLD,
            "  ⚠️   INCIDENCIA: Portal en mantenimiento en horario laboral  "
        ))
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

    # 2. Éxito
    if result["ok"]:
        log.info(colorize(C.BG_GREEN + C.BOLD, "  ✅  VERIFICACIÓN EXITOSA  "))
        log.info("  Pasarela : %s", colorize(
            C.GREEN, result["gateway_domain"]))
        if result.get("maintenance"):
            log.info("  Nota     : %s",
                     colorize(C.GRAY, "Portal en mantenimiento detectado durante la ejecución"))
        log.info("  Hora     : %s", colorize(C.GRAY, ts))
        return

    # 3. CRÍTICO solo si hay redirección a dominio distinto
    if result.get("redirect_mismatch"):
        log.critical(colorize(C.BG_RED + C.BOLD,
                              "  🚨  ALERTA CRÍTICA: REDIRECCIÓN A DOMINIO NO ESPERADO  "))
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

    # 4. Todo lo demás = WARNING operativo
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


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────


def main():
    """Punto de entrada del monitor."""
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
    parser.add_argument("--step-delay", dest="step_delay", type=int, default=2,
                        help="Pausa entre pasos en segundos (default: 2)")
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
        log.info(colorize(C.YELLOW,
                          "  🖥️  Modo visible — pausa entre pasos: %ss"),
                 colorize(C.WHITE, str(args.step_delay)))

    print(colorize(C.CYAN + C.BOLD,
                   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"))

    if args.loop:
        run_count = 0
        while True:
            run_count += 1
            interval = args.interval if args.interval > 0 else current_interval()
            bh_label = (
                colorize(C.GREEN, "horario laboral")
                if is_business_hours()
                else colorize(C.GRAY, "fuera de horario")
            )

            log.info(colorize(
                C.MAGENTA + C.BOLD,
                f"\n  ┌────────── Ejecución #{run_count} "
                f"[{bh_label}{C.MAGENTA + C.BOLD}] ──────────────────────"
            ))

            try:
                result = run_check(
                    visible=show_browser,
                    log=log,
                    step_delay=args.step_delay
                )
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
            log.info("\n  Próxima ejecución a las %s (%ds — %s)",
                     colorize(C.CYAN, next_ts), interval,
                     "horario laboral" if is_business_hours() else "fuera de horario")
            time.sleep(interval)
    else:
        result = run_check(
            visible=show_browser,
            log=log,
            step_delay=args.step_delay
        )
        process_result(result, log)
        sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
