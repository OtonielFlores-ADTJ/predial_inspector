# =============================================================================

# Opción A: systemd timer (recomendado para servidores Linux)

# =============================================================================

# Copia estos archivos a /etc/systemd/system/ y ajusta las rutas.

#

# 1. Crear el archivo de servicio:

# sudo nano /etc/systemd/system/monitor-pasarela.service

#

# 2. Crear el archivo de timer:

# sudo nano /etc/systemd/system/monitor-pasarela.timer

#

# 3. Activar:

# sudo systemctl daemon-reload

# sudo systemctl enable monitor-pasarela.timer

# sudo systemctl start monitor-pasarela.timer

#

# 4. Verificar:

# sudo systemctl status monitor-pasarela.timer

# sudo journalctl -u monitor-pasarela.service -f

# ---- monitor-pasarela.service ----

# [Unit]

# Description=Monitor de Pasarela de Pago - Predial Tijuana

# After=network-online.target

# Wants=network-online.target

#

# [Service]

# Type=oneshot

# User=tu_usuario

# WorkingDirectory=/ruta/a/monitor_pasarela

# ExecStart=/usr/bin/python3 /ruta/a/monitor_pasarela/monitor.py

#

# # Cargar variables de entorno (.env)

# EnvironmentFile=/ruta/a/monitor_pasarela/.env

#

# # Logs recomendados

# StandardOutput=append:/ruta/a/monitor_pasarela/predial/logs/systemd.log

# StandardError=append:/ruta/a/monitor_pasarela/predial/logs/systemd-error.log

#

# [Install]

# WantedBy=multi-user.target

# ---- monitor-pasarela.timer ----

# [Unit]

# Description=Ejecutar monitor de pasarela cada 10 minutos

#

# [Timer]

# OnBootSec=2min

# OnUnitActiveSec=10min

# Persistent=true

#

# [Install]

# WantedBy=timers.target

# =============================================================================

# Opción B: crontab (más simple)

# =============================================================================

# Ejecuta:

# crontab -e

#

# Agrega:

#

# _/10 _ \* \* \* cd /ruta/a/monitor_pasarela && /usr/bin/python3 monitor.py >> predial/logs/cron.log 2>&1

# =============================================================================

# Opción C: modo loop integrado (recomendado)

# =============================================================================

# Ejecuta directamente:

#

# python3 monitor.py --loop

#

# O con intervalo personalizado:

#

# python3 monitor.py --loop --interval 600

# =============================================================================

# Opción D: Docker (recomendado para producción)

# =============================================================================

# Levantar contenedor:

#

# docker compose up -d

#

# Ver logs:

#

# docker compose logs -f

#

# Nota:

# El volumen ./predial está montado en /app/predial,

# por lo que screenshots y logs se guardan persistentes en el host.

# =============================================================================

# Notas importantes

# =============================================================================

# - No es necesario DISPLAY (usa Chrome headless automáticamente)

# - Asegúrate de que Chrome/Chromedriver estén instalados en sistema (si no usas Docker)

# - El script maneja automáticamente:

# - Horario laboral vs fuera de horario

# - Errores de Selenium

# - Alertas por correo

# - Logs principales:

# predial/logs/monitor.log

# - Evidencia:

# predial/screenshots/
