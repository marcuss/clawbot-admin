# ClawBot Admin

> Approval service con TOTP + Docker para ejecutar tareas privilegiadas solo con aprobación explícita de Marcus via WhatsApp.

## Arquitectura

```mermaid
sequenceDiagram
    participant CB as ClawBot (cliente)
    participant AS as Approval Service
    participant WA as WhatsApp (Marcus)
    participant DC as Docker Container
    participant AL as Audit Log

    CB->>AS: request_privileged_task(description, command) via Unix socket
    AS->>WA: "🔐 Tarea pendiente. Responde OK <TOTP> o NO"
    WA-->>AS: "OK 847291" (via /tmp/clawbot-admin-response.txt)
    AS->>AS: Validar TOTP contra seed local
    AS->>DC: docker compose run clawbot-admin (TASK_ID=xyz)
    DC->>DC: Ejecutar /tasks/xyz.sh con secrets
    DC-->>AS: stdout/stderr + exit code
    AS-->>CB: {output, exit_code, duration_seconds}
    AS->>AL: append-only JSON log + S3 backup
```

## Seguridad

- **TOTP seed**: generado localmente, NUNCA sale del Mac
- **Credenciales admin**: solo en `~/.clawbot-admin/` (Docker secrets)
- **Un solo intento**: TOTP incorrecto → tarea cancelada automáticamente
- **Timeout 5 min**: sin respuesta en 5 min → cancelación automática
- **Audit log append-only**: registro permanente de todo lo aprobado o rechazado
- **Container efímero**: `--rm` en cada ejecución, read-only filesystem

## Setup inicial (una sola vez)

### 1. Instalar dependencias

```bash
pip3 install -r requirements.txt
```

### 2. Generar el seed TOTP

```bash
chmod +x setup.sh
./setup.sh
```

Escanea el QR que aparece en terminal con **Microsoft Authenticator**:
- Toca `+` → "Otra cuenta (trabajo o escuela)"
- Apunta la cámara al QR

### 3. Crear las credenciales admin

Crea los archivos de secretos (NO los commits al repo):

```bash
mkdir -p ~/.clawbot-admin
chmod 700 ~/.clawbot-admin

# Token de Infisical con acceso admin
echo "your-infisical-admin-token" > ~/.clawbot-admin/infisical_admin_token

# AWS credentials con permisos admin
echo "AKIAIOSFODNN7EXAMPLE" > ~/.clawbot-admin/aws_admin_key
echo "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" > ~/.clawbot-admin/aws_admin_secret

# Permisos seguros
chmod 600 ~/.clawbot-admin/*
```

### 4. Construir el Docker container

```bash
docker compose -f docker-compose.admin.yml build
```

### 5. Instalar el launchd service

```bash
cp com.clawbot.admin.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.clawbot.admin.plist
```

Verificar que está corriendo:

```bash
launchctl list | grep clawbot
# Debe mostrar: com.clawbot.admin
```

Logs:
```bash
tail -f ~/.clawbot-admin/approval_service.log
```

---

## Cómo solicita ClawBot una tarea

```python
from clawbot_client import request_privileged_task

result = request_privileged_task(
    description="Revisar tabla profiles de producción",
    command="psql $PROD_DB_URL -c 'SELECT id, email FROM profiles LIMIT 20'",
    timeout_seconds=300
)
print(result.output)
print(f"Exit code: {result.exit_code}")
```

El servicio enviará un WhatsApp a Marcus con los detalles de la tarea.

---

## Cómo aprueba Marcus (WhatsApp)

Marcus recibe un mensaje como:

```
🔐 ClawBot Admin — Solicitud de tarea privilegiada

ID: abc12345
Descripción: Revisar tabla profiles de producción
Comando: psql $PROD_DB_URL -c 'SELECT id, email FROM profiles LIMIT 20'

Responde OK <código-TOTP> para aprobar (ej: OK 847291)
o NO para cancelar.

⏳ Tienes 5 minutos. Un solo intento.
```

**Para aprobar:**
```
OK 847291
```
*(usa el código de 6 dígitos de Microsoft Authenticator en ese momento)*

**Para rechazar:**
```
NO
```

### Importante:
- El código TOTP **expira cada 30 segundos** — úsalo inmediatamente
- Solo tienes **1 intento** — si el código es incorrecto, la tarea se cancela
- Si no respondes en **5 minutos**, la tarea se cancela automáticamente

---

## Cómo responde el agente (ClawBot principal)

Cuando Marcus responde via WhatsApp, ClawBot escribe la respuesta al archivo de polling:

```python
from clawbot_client import write_approval_response
write_approval_response("abc12345", "OK 847291")
```

O via CLI:
```bash
python3 clawbot_client.py respond abc12345 "OK 847291"
```

---

## Estructura del Audit Log

Archivo: `~/.clawbot-admin/audit.log` (JSONL, una entrada por línea)
Backup: `s3://couplesapp-e2e-reports/admin-audit/audit.log`

```json
{
  "timestamp": "2026-03-15T19:00:00+00:00",
  "request_id": "abc12345",
  "requester": "ClawBot",
  "task_description": "Revisar tabla profiles de producción",
  "task_command": "psql $PROD_DB_URL -c 'SELECT id, email FROM profiles LIMIT 20'",
  "approved_by": "+573013275073",
  "approval_channel": "WhatsApp",
  "approval_method": "TOTP",
  "status": "executed",
  "execution_output": " id | email\n----+-------\n ...",
  "exit_code": 0,
  "duration_seconds": 8.2
}
```

### Status values:
| Status | Descripción |
|--------|-------------|
| `executed` | Tarea completada exitosamente |
| `rejected` | Marcus respondió NO |
| `timeout` | Sin respuesta en 5 minutos |
| `totp_failed` | Código TOTP incorrecto |
| `notification_failed` | No se pudo enviar el WhatsApp |

Ver log en tiempo real:
```bash
tail -f ~/.clawbot-admin/audit.log | python3 -m json.tool
```

---

## Agregar credenciales admin

Todas las credenciales viven en `~/.clawbot-admin/` y se montan como Docker secrets.

Para agregar una nueva credencial:

1. Crear el archivo:
   ```bash
   echo "valor-secreto" > ~/.clawbot-admin/mi_nueva_credencial
   chmod 600 ~/.clawbot-admin/mi_nueva_credencial
   ```

2. Agregar el secret en `docker-compose.admin.yml`:
   ```yaml
   secrets:
     mi_nueva_credencial:
       file: ${HOME}/.clawbot-admin/mi_nueva_credencial
   ```

3. Agregar el secret al servicio:
   ```yaml
   services:
     clawbot-admin:
       secrets:
         - mi_nueva_credencial
   ```

4. Usar en `entrypoint.sh`:
   ```bash
   export MI_CREDENCIAL
   MI_CREDENCIAL=$(cat /run/secrets/mi_nueva_credencial)
   ```

---

## Gestionar el servicio

```bash
# Iniciar
launchctl load ~/Library/LaunchAgents/com.clawbot.admin.plist

# Detener
launchctl unload ~/Library/LaunchAgents/com.clawbot.admin.plist

# Reiniciar
launchctl unload ~/Library/LaunchAgents/com.clawbot.admin.plist
launchctl load ~/Library/LaunchAgents/com.clawbot.admin.plist

# Ver logs
tail -f ~/.clawbot-admin/approval_service.log
tail -f ~/.clawbot-admin/approval_service.error.log

# Estado
launchctl list | grep clawbot
```

---

## Estructura del repositorio

```
clawbot-admin/
├── setup.sh                    # Setup inicial — generar TOTP seed (correr UNA vez)
├── approval_service.py         # Daemon que escucha, valida TOTP y ejecuta
├── audit_logger.py             # Audit log append-only + backup S3
├── clawbot_client.py           # Cliente para solicitar tareas desde ClawBot
├── docker-compose.admin.yml    # Docker Compose para el admin container
├── com.clawbot.admin.plist     # launchd plist para autostart en macOS
├── requirements.txt            # Dependencias Python
├── README.md                   # Este archivo
├── .gitignore                  # Nunca commits secrets/seeds
├── admin-container/
│   ├── Dockerfile              # python:3.12-slim + awscli + infisical + psql
│   └── entrypoint.sh           # Ejecuta /tasks/${TASK_ID}.sh
└── tasks/                      # Generado en runtime, ignorado por git
    └── .gitkeep
```

---

## Seguridad — Reglas críticas

1. **`~/.clawbot-admin/totp.secret`** — NUNCA en el repo, NUNCA en logs, NUNCA en red
2. **Credenciales admin** — SOLO en `~/.clawbot-admin/` como Docker secrets
3. **Audit log** — append-only, nunca borrar ni modificar entradas existentes
4. **Un solo intento TOTP** — si falla, la tarea se cancela sin excepción
5. **Timeout 5 minutos** — sin respuesta → cancelación automática
