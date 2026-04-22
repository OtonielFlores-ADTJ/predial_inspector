# 🏛️ Monitor de Pasarela de Pago — Predial Tijuana

Herramienta automatizada para verificar que la pasarela de pago del impuesto predial de Tijuana (`pagos.tijuana.gob.mx`) **no haya sido suplantada**.

Ejecuta el flujo completo de pago y valida que la redirección final apunte al dominio legítimo:

```
www.adquiramexico.com.mx
```

---

## 🚨 Objetivo

Detectar en tiempo real:

- Phishing
- Redirecciones maliciosas
- Alteraciones en el flujo de pago
- Caídas del sistema o mantenimiento en horario laboral

---

## ⚙️ ¿Qué hace?

1. 🔐 Inicia sesión en el portal de pagos
2. 🧾 Navega al módulo de Predial (click real del sistema)
3. 🏠 Selecciona la clave catastral `YY000004`
4. 💳 Ejecuta el flujo de pago completo
5. 🌐 Detecta la URL final de la pasarela
6. ✅ Valida el dominio esperado
7. 📸 Guarda evidencia (screenshot)
8. 📄 Guarda logs de ejecución
9. 📧 Envía alerta por correo si hay anomalía

---

## 🧠 Flujo

```
Login → Predial → Clave → Pago → Redirección → Validación dominio
```

---

## 📁 Estructura del proyecto

```
.
├── monitor.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── railway.toml
└── predial/
    ├── screenshots/
    └── logs/
```

---

## 🔐 Configuración

Crea tu archivo `.env`:

```
cp .env.example .env
```

Ejemplo:

```
PORTAL_USER=usuario@correo.com
PORTAL_PASS=TU_PASSWORD

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=tu_correo@gmail.com
SMTP_PASS=app_password

ALERT_TO=destinatario@ejemplo.com
ALERT_FROM=tu_correo@gmail.com

EXPECTED_GATEWAY_DOMAIN=www.adquiramexico.com.mx

PAGE_TIMEOUT=60

LOOP_INTERVAL_BUSINESS=600
LOOP_INTERVAL_OFF=3600

BUSINESS_HOUR_START=8
BUSINESS_HOUR_END=17

LOG_FILE=monitor.log
```

---

## 📧 Configuración de correo (Gmail)

Debes usar **App Password**:

https://myaccount.google.com/apppasswords

---

## 🧪 Ejecución local

### 1. Crear entorno virtual

```
python3 -m venv venv
source venv/bin/activate
```

### 2. Instalar dependencias

```
pip install -r requirements.txt
```

### 3. Ejecutar

```
python3 monitor.py
```

---

## 🖥️ Modo visual (debug)

```
python3 monitor.py --visible --step-delay 5
```

---

## 🔁 Modo automático (loop)

```
python3 monitor.py --loop
```

Intervalo personalizado:

```
python3 monitor.py --loop --interval 300
```

---

## 🐳 Docker

### Ejecutar

```
docker compose up -d
```

### Ver logs

```
docker compose logs -f
```

---

## 🚂 Railway (Deploy)

1. Subir repo a GitHub
2. Crear proyecto en Railway
3. Deploy desde repo
4. Configurar variables de entorno
5. Railway detecta Docker automáticamente

⚠️ Railway no persiste archivos locales.

---

## 📸 Evidencia

Se guarda en:

```
predial/screenshots/
predial/logs/
```

---

## 🚨 Alertas

Se dispara alerta cuando:

- Dominio distinto al esperado
- Fallo en el flujo
- Mantenimiento en horario laboral

---

## 📦 Dependencias

```
selenium>=4.20.0
python-dotenv>=1.0.0
```
