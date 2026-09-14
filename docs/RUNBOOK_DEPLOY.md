# Runbook — M-Deploy: VPS Hostinger

> **VPS:** `2.24.101.201` · Ubuntu 24.04 · Hostinger KVM 2  
> **Dominio API:** `api.mirkocalzadilla.com` → A record en Porkbun  
> **Registro de imágenes:** `ghcr.io/funnelops-marketing-services/server`

---

## 1. Setup inicial del VPS (una sola vez)

Conectarse como root:
```bash
ssh -i ~/.ssh/marketing_services root@2.24.101.201
```

### 1.1 Instalar Docker + compose plugin
```bash
apt-get update && apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 1.2 Crear usuario deploy
```bash
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
# Agregar clave SSH pública del CI (GitHub Actions) y del equipo
mkdir -p /home/deploy/.ssh
echo "<PUBLIC_KEY_CI>" >> /home/deploy/.ssh/authorized_keys
echo "<PUBLIC_KEY_CHRIS>" >> /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys
```

### 1.3 UFW — solo puertos necesarios
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (Caddy → redirect a HTTPS)
ufw allow 443/tcp   # HTTPS
ufw enable
```

### 1.4 Autenticación GitHub para git pull (PAT + HTTPS)

La organización tiene **Deploy keys deshabilitadas por policy**. Se usa HTTPS con PAT en lugar de SSH.

**Token actual: fine-grained PAT** (creado 2026-06-10 por `chrisCs99` — ver §3.1). Receta para crearlo/rotarlo:
`github.com → perfil → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token`
- Token name: `VPS git pull`
- Expiration: 1 año (anotar la fecha — al vencer se rompe el `git pull` del VPS)
- Resource owner: `funnelops-marketing-services` (la org, NO el usuario personal)
- Repository access: Only select repositories → `server`
- Permissions → Repository permissions → **Contents: Read-only** (Metadata: Read-only se marca solo)

**Configurar en el VPS como usuario `deploy`:**
```bash
su - deploy
git config --global credential.helper store
echo "https://TOKEN_AQUI@github.com" > ~/.git-credentials
chmod 600 ~/.git-credentials
```

Verificar acceso:
```bash
git ls-remote https://github.com/funnelops-marketing-services/server.git HEAD
# → debe mostrar un hash sin pedir contraseña
```

### 1.5 Clonar repo y crear .env prod
```bash
# Como usuario deploy — HTTPS (no SSH, la org tiene deploy keys deshabilitadas)
git clone https://github.com/funnelops-marketing-services/server.git /home/deploy/marketing-services
cd /home/deploy/marketing-services
cp .env.example .env
nano .env   # completar con valores reales (ver sección 2)
```

### 1.6 Primer deploy manual
```bash
# Como usuario deploy, desde /home/deploy/marketing-services
# -u = usuario GitHub dueño del PAT (no el nombre de la org)
echo "<GHCR_PAT>" | docker login ghcr.io -u chrisCs99 --password-stdin

# Construir imagen localmente la primera vez (o esperar que CI la pushee)
docker compose -f docker-compose.prod.yml pull

docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate

docker compose -f docker-compose.prod.yml up -d
```

Verificar:
```bash
curl https://api.mirkocalzadilla.com/health          # liveness → {"status":"ok"}
curl https://api.mirkocalzadilla.com/health/deep     # readiness → checks DB/Redis/worker (§5.1)
```

---

## 2. Variables de entorno para producción (.env en el VPS)

Basarse en `.env.example`. Cambios respecto al dev:

| Variable | Valor prod |
|---|---|
| `APP_ENV` | `production` |
| `APP_DEBUG` | `false` |
| `DATABASE_URL` | `postgresql+asyncpg://marketing:<PASSWORD>@postgres:5432/marketing_platform` ← **postgres**, no localhost |
| `REDIS_URL` | `redis://redis:6379/0` ← **redis**, no localhost |
| `POSTGRES_USER` | `marketing` |
| `POSTGRES_PASSWORD` | password fuerte (generar con `openssl rand -hex 32`) |
| `POSTGRES_DB` | `marketing_platform` |
| `JWT_SECRET_KEY` | valor fuerte (generar con `openssl rand -hex 32`) |
| `CORS_ALLOWED_ORIGINS` | `https://crm.mirkocalzadilla.com,https://mirkocalzadilla.com,https://www.mirkocalzadilla.com` ← el CRM vive en el subdominio `crm.`; debe ir en la lista o el navegador bloquea el login por CORS |
| `LLM_PROVIDER` | `anthropic` (o `openai`) |
| `HEALTH_TOKEN` | valor fuerte (`openssl rand -hex 32`) — habilita ver `sha`/`version` en `/health/deep` (§5.1, #246) |
| Claves Meta | `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, etc. |

---

## 3. Secrets de GitHub Actions (configurar una sola vez)

> **Nota 2026-06-25 (deploy pull-based):** con el deploy ya **no** por SSH, `VPS_HOST` y
> `VPS_SSH_KEY` quedaron **sin uso** (se pueden borrar). `GHCR_PAT` tampoco lo usa ya el CI;
> el VPS pullea con su `docker login` persistido. Lo único que CI necesita para publicar la
> imagen es el `GITHUB_TOKEN` automático (`packages: write`).

En `github.com/funnelops-marketing-services/server → Settings → Secrets → Actions`:

| Secret | Valor |
|---|---|
| `VPS_HOST` | `2.24.101.201` |
| `VPS_SSH_KEY` | Clave SSH **privada** del usuario `deploy` (Ed25519 recomendado) |
| `GHCR_PAT` | GitHub PAT **classic** con scope `read:packages` (para que el VPS haga `docker pull`) — ver §3.1 |

El `GITHUB_TOKEN` (para que el CI *pushee* la imagen) es automático — no requiere configuración.

### 3.1 Ownership y rotación de los PATs

Ambos tokens fueron creados el **2026-06-10 por `chrisCs99`** (member de la org, admin del repo `server`) y quedan atados a su cuenta personal: si esa cuenta sale de la org, revoca un token o el token expira, se rompen `git pull` (VPS) y/o `docker pull` (GHCR) en producción.

| Token | Tipo | Alcance | Dónde vive |
|---|---|---|---|
| `VPS git pull` | Fine-grained | Contents: Read-only, solo repo `server` | `~/.git-credentials` del user `deploy` en el VPS (§1.4) |
| `GHCR pull VPS` | Classic | `read:packages` | Secret `GHCR_PAT` en Actions (§3) + `docker login` en el VPS (§1.6) |

**Si el deploy falla con 401/403 contra github.com o ghcr.io:** revisar primero estos tokens (expiración, revocación, membresía de `chrisCs99` en la org). Cualquier member con acceso de lectura al repo puede regenerarlos — no requiere ser owner de la org; las recetas están en §1.4 (fine-grained) y arriba (classic, `read:packages`). Tras rotar: actualizar `~/.git-credentials` en el VPS, el secret `GHCR_PAT` y rehacer el `docker login`.

---

## 4. DNS — Porkbun

Agregar un registro A en `mirkocalzadilla.com`:

| Tipo | Host | Valor | TTL |
|---|---|---|---|
| A | `api` | `2.24.101.201` | 600 |

El apex (`@`) y `www` siguen apuntando a Vercel.

---

## 5. Flujo de deploy automático (día a día) — PULL-BASED

> **Cambio 2026-06-25:** el deploy dejó de ser por SSH (el firewall del VPS bloquea el puerto 22
> para los runners de GitHub → `i/o timeout`). Ahora es **pull-based**: el VPS chequea `origin/main`
> cada ~2 min y se actualiza solo. Diseño del deploy pull-based: implementado en PR #58 (ver BITACORA).

```
merge PR → main
    └─► GitHub Actions (deploy.yml): lint + test + build-and-push
            → ghcr.io/.../server:latest + :<sha>      (solo push a GHCR; CI NO entra al VPS)

VPS · mks-deploy.timer (cada ~2 min) → scripts/vps-pull-deploy.sh:
    git fetch origin main → ¿avanzó y ya está la imagen :<sha> en GHCR?
        ├─ no  → no-op / reintenta el próximo tick
        └─ sí  → git pull → compose pull → migrate (alembic upgrade head)
                 → up -d → health-check (marketing-app healthy)
                     ├─ OK    → registra SHA + prune
                     └─ falla → rollback de imagen al SHA previo + marca failed
```

### Instalación / actualización del timer (una vez, como root en el VPS)
```bash
cd /home/deploy/marketing-services && git pull --ff-only origin main   # como user deploy
sudo /home/deploy/marketing-services/deploy/systemd/install.sh
```

### Operación
```bash
systemctl status mks-deploy.timer            # ¿activo? próximo disparo
systemctl list-timers mks-deploy.timer
journalctl -u mks-deploy.service -f          # logs de cada deploy
systemctl start mks-deploy.service           # forzar un chequeo/deploy ya
cat /home/deploy/.mks-deploy/deployed_sha    # SHA en prod
# Si un commit quedó marcado como fallido y querés reintentar tras arreglarlo:
rm -f /home/deploy/.mks-deploy/failed_sha
```

### Deploy/rollback manual puntual (si hace falta saltarse el timer)
```bash
# como user deploy, en /home/deploy/marketing-services
IMAGE_TAG=<sha> docker compose -f docker-compose.prod.yml pull app worker migrate
IMAGE_TAG=<sha> docker compose -f docker-compose.prod.yml --profile migrate run --rm -T migrate
IMAGE_TAG=<sha> docker compose -f docker-compose.prod.yml up -d --remove-orphans
```

**`IMAGE_TAG` es obligatorio** (#233): `docker-compose.prod.yml` ya no tiene fallback a
`:latest` — cualquier comando de compose sin la variable falla con un mensaje explícito, en
vez de deployar en silencio una imagen vieja (incidente 2026-07-22). Para operar sobre lo que
ya está en prod: `IMAGE_TAG=$(cat /home/deploy/.mks-deploy/deployed_sha) docker compose ...`.
Además el timer se auto-cura: si alguien recrea app/worker con otra imagen a mano, el próximo
tick detecta el drift (compara la imagen corriendo contra `deployed_sha`) y redeploya solo.

### 5.1 Validar qué versión corre en prod

`__version__` es estático ("0.1.0"): el commit real va **embebido en la imagen** (#217) y se
expone como `sha` en `/health/deep` **solo con el `HEALTH_TOKEN`** (#246 — el sha público era
recon: con el repo en mano de un tercero, decía exactamente qué corre en prod). El token vive
en el `.env` del VPS (§2); guardalo también en tu máquina.

**Sin SSH (rápido):**
```bash
curl -s -H "Authorization: Bearer $HEALTH_TOKEN" https://api.mirkocalzadilla.com/health/deep
# {"status":"ok","checks":{"database":true,"redis":true,"worker":true},"sha":"<commit>","version":"0.1.0"}
```
Sin token (o token errado) responde lo mismo **sin** `sha`/`version` — sanitizado, no 401: es
lo que consume el uptime monitor de Sentry. `status:"degraded"` + HTTP 503 ⇒ alguna dependencia
caída (el check culpable va en `false`; `worker:false` = heartbeat vencido, worker caído o
colgado >60s).

Comparar el `sha` con el **último merge real** de `main`. OJO: `main` HEAD suele ser un commit
`[skip ci]` del bot de bitácora **sin imagen propia**; comparar contra el `Merge pull request`
de abajo, no contra HEAD:
```bash
git fetch -q origin main && git log --oneline -6 origin/main
# el SHA desplegado es el del "Merge pull request #NNN" más reciente
```

**Con SSH (fuente de verdad):**
```bash
ssh root@2.24.101.201                                  # o deploy@
cat /home/deploy/.mks-deploy/deployed_sha              # imagen desplegada (post-#219: la REAL)
cd /home/deploy/marketing-services
docker compose -f docker-compose.prod.yml images app   # TAG = SHA de la imagen que corre
journalctl -u mks-deploy.service --since "1 hour ago" | tail -20   # log del último deploy
systemctl list-timers mks-deploy.timer                 # próximo tick (~2 min)
cat /home/deploy/.mks-deploy/failed_sha 2>/dev/null    # si existe: un deploy falló e hizo rollback
```

**Interpretación:**
- `deployed_sha` == imagen de `docker compose images app` == último merge real de `main` → al día.
- `/health/deep` `sha` != último merge real → todavía no pulleó (esperar el próximo tick) o falló.
- `failed_sha` presente → el deploy de ese SHA falló y quedó en rollback; ver logs, arreglar y
  `rm -f /home/deploy/.mks-deploy/failed_sha` para reintentar.

**Forzar un deploy ya:** `systemctl start mks-deploy.service` (como root). Desde #219 el script
registra la **imagen real** desplegada (no HEAD) y espera el build del commit real en vez de caer
a una imagen más vieja, así que el loop converge solo.

---

## 6. Operaciones frecuentes

### Ver logs
```bash
docker compose -f docker-compose.prod.yml logs -f app
docker compose -f docker-compose.prod.yml logs -f worker
```

### Reiniciar un servicio
```bash
docker compose -f docker-compose.prod.yml restart app
```

### Conectar DBeaver a Postgres via SSH tunnel
En DBeaver → Nueva conexión → PostgreSQL → SSH tunnel:
- **SSH host:** `2.24.101.201` · **user:** `deploy` · **key:** tu clave privada
- **Local port:** cualquiera, ej. `15432`
- **Remote host:** `localhost` · **Remote port:** `5432`

Luego en la conexión: `host=localhost port=15432 db=marketing_platform user=marketing`.

### Rollback manual
```bash
# Reemplazar <SHA> por el tag del commit anterior
docker compose -f docker-compose.prod.yml stop app worker
docker pull ghcr.io/funnelops-marketing-services/server:<SHA>
# Editar docker-compose.prod.yml para usar :<SHA> en vez de :latest, luego:
docker compose -f docker-compose.prod.yml up -d app worker
```

### Backup manual de Postgres
```bash
docker exec marketing-postgres pg_dump -U marketing marketing_platform | gzip > backup_$(date +%Y%m%d).sql.gz
```
