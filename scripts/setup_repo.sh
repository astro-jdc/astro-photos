#!/usr/bin/env bash
#
# setup_repo.sh — configura el repositorio de GitHub de astro-photos.
#
# Aplica lo que dice `docs/branching.md`: crea `develop`, protege `main` y
# `develop`, crea los GitHub Environments `staging` y `production` (este último
# con revisor obligatorio) y las etiquetas estándar.
#
# Es **idempotente**: se puede ejecutar tantas veces como haga falta; lo que ya
# está bien no se toca. Y no hace nada sin confirmación explícita.
#
#   ./scripts/setup_repo.sh --dry-run          # enseña qué haría, no toca nada
#   ./scripts/setup_repo.sh                    # pide confirmación y aplica
#   ./scripts/setup_repo.sh --yes              # aplica sin preguntar (para CI)
#   ./scripts/setup_repo.sh --repo otro/repo   # sobre otro repositorio
#   ./scripts/setup_repo.sh --solo             # sin revisiones ni CODEOWNERS
#                                              # (proyecto de una sola persona)
#
# Requisitos: `gh` autenticado con permisos de administración del repositorio.

set -euo pipefail

REPO="${ASTRO_REPO:-astro-jdc/astro-photos}"
DRY_RUN=false
ASSUME_YES=false
# Con un solo desarrollador, exigir revisiones y CODEOWNERS bloquea el repo: no
# puedes aprobar tu propio PR, y una entrada de CODEOWNERS que apunte a un equipo
# inexistente hace que la regla no pueda satisfacerse nunca. --solo baja esas dos
# y deja intacto lo que de verdad protege: PR obligatorio, CI en verde, historial
# lineal, sin force-push ni borrado.
SOLO=false
REVIEWS_MAIN="${ASTRO_REVIEWS_MAIN:-1}"
REVIEWS_DEVELOP="${ASTRO_REVIEWS_DEVELOP:-1}"
CODEOWNERS_REVIEW="${ASTRO_CODEOWNERS_REVIEW:-true}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; DIM=$'\033[2m'; RESET=$'\033[0m'

usage() {
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --yes|-y)  ASSUME_YES=true; shift ;;
    --solo)    SOLO=true; shift ;;
    --repo)    REPO="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "${RED}Opción desconocida: $1${RESET}" >&2; usage 1 ;;
  esac
done

if $SOLO; then
  REVIEWS_MAIN=0
  REVIEWS_DEVELOP=0
  CODEOWNERS_REVIEW=false
fi

log()  { printf '%s\n' "$*"; }
info() { printf '%s→%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
skip() { printf '%s·%s %s %s(ya estaba)%s\n' "$DIM" "$RESET" "$*" "$DIM" "$RESET"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# `run` ejecuta o, en dry-run, solo enseña. Todo cambio pasa por aquí.
run() {
  if $DRY_RUN; then
    printf '%s  [dry-run]%s %s\n' "$DIM" "$RESET" "$*"
    return 0
  fi
  "$@"
}

api() { run gh api -H "Accept: application/vnd.github+json" "$@"; }

# --------------------------------------------------------------------------- #
# Comprobaciones previas
# --------------------------------------------------------------------------- #
command -v gh >/dev/null || die "hace falta el CLI 'gh' (https://cli.github.com)"
command -v jq >/dev/null || die "hace falta 'jq'"
gh auth status >/dev/null 2>&1 || die "'gh' no está autenticado: ejecuta 'gh auth login'"

gh repo view "$REPO" >/dev/null 2>&1 || die "no se ve el repositorio $REPO (¿nombre correcto? ¿permisos?)"

PERMISO=$(gh api "repos/$REPO" --jq '.permissions.admin // false')
if [[ "$PERMISO" != "true" ]]; then
  die "hacen falta permisos de administración sobre $REPO para aplicar protecciones"
fi

DEFAULT_BRANCH=$(gh api "repos/$REPO" --jq '.default_branch')

# --------------------------------------------------------------------------- #
# Resumen y confirmación — nada se ejecuta solo
# --------------------------------------------------------------------------- #
cat <<EOF

  ${BLUE}astro-photos — configuración del repositorio${RESET}

  repositorio      ${REPO}
  rama por defecto ${DEFAULT_BRANCH}
  modo             $($DRY_RUN && echo "${YELLOW}dry-run (no se cambia nada)${RESET}" || echo "${RED}APLICAR CAMBIOS${RESET}")

  Se va a:
    · crear la rama 'develop' si no existe
    · proteger 'main'     (PR obligatorio, $REVIEWS_MAIN review(s), checks ci/backend ci/frontend
                           ci/infra ci/models, historial lineal, sin force-push,
                           conversaciones resueltas)
    · proteger 'develop'  (PR obligatorio, mismos checks, sin force-push)
    · crear los environments 'staging' y 'production'
      ('production' con revisor obligatorio y limitado a la rama 'main')
    · crear las etiquetas estándar

EOF

if ! $DRY_RUN && ! $ASSUME_YES; then
  printf '  Escribe %sSI%s para continuar: ' "$GREEN" "$RESET"
  read -r respuesta
  [[ "$respuesta" == "SI" ]] || die "cancelado por el usuario"
  echo
fi

# --------------------------------------------------------------------------- #
# 1. Rama develop
# --------------------------------------------------------------------------- #
info "Ramas"
if gh api "repos/$REPO/branches/develop" >/dev/null 2>&1; then
  skip "la rama 'develop' existe"
else
  SHA=$(gh api "repos/$REPO/git/ref/heads/$DEFAULT_BRANCH" --jq '.object.sha')
  api --method POST "repos/$REPO/git/refs" -f ref='refs/heads/develop' -f sha="$SHA" >/dev/null
  ok "rama 'develop' creada desde '$DEFAULT_BRANCH' ($SHA)"
fi

# --------------------------------------------------------------------------- #
# 2. Protección de ramas
# --------------------------------------------------------------------------- #
# Los nombres de los checks son EXACTAMENTE los `name:` de los jobs de ci.yml.
CHECKS='["ci/backend","ci/frontend","ci/infra","ci/models"]'

proteger() {
  local rama="$1" revisiones="$2" lineal="$3" conversaciones="$4"
  local payload
  payload=$(jq -n \
    --argjson checks "$CHECKS" \
    --argjson revisiones "$revisiones" \
    --argjson lineal "$lineal" \
    --argjson conversaciones "$conversaciones" \
    --argjson codeowners "$CODEOWNERS_REVIEW" \
    '{
      required_status_checks: { strict: true, contexts: $checks },
      enforce_admins: false,
      required_pull_request_reviews: {
        required_approving_review_count: $revisiones,
        dismiss_stale_reviews: true,
        require_code_owner_reviews: $codeowners,
        require_last_push_approval: true
      },
      restrictions: null,
      required_linear_history: $lineal,
      allow_force_pushes: false,
      allow_deletions: false,
      block_creations: false,
      required_conversation_resolution: $conversaciones,
      lock_branch: false,
      allow_fork_syncing: false
    }')

  if $DRY_RUN; then
    printf '%s  [dry-run]%s PUT repos/%s/branches/%s/protection\n%s%s%s\n' \
      "$DIM" "$RESET" "$REPO" "$rama" "$DIM" "$(echo "$payload" | jq -c .)" "$RESET"
    return 0
  fi

  echo "$payload" | gh api --method PUT "repos/$REPO/branches/$rama/protection" --input - >/dev/null
  ok "protección aplicada a '$rama'"
}

info "Protección de ramas"
# main: PR obligatorio, 1 review, historial lineal (squash), conversaciones resueltas.
proteger "$DEFAULT_BRANCH" "$REVIEWS_MAIN" true true
# develop: PR obligatorio y checks, pero sin exigir historial lineal (es la rama
# de integración: los back-merges traen merges de verdad).
proteger develop "$REVIEWS_DEVELOP" false false

# Squash merge como única estrategia: `main` tiene que quedar lineal.
info "Estrategia de merge"
api --method PATCH "repos/$REPO" \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true \
  -F allow_auto_merge=true \
  -f squash_merge_commit_title=PR_TITLE \
  -f squash_merge_commit_message=PR_BODY >/dev/null
ok "squash merge, borrado de rama al mergear y auto-merge activados"

# --------------------------------------------------------------------------- #
# 3. Environments
# --------------------------------------------------------------------------- #
info "Environments"

crear_environment() {
  local nombre="$1" payload="$2"
  if $DRY_RUN; then
    printf '%s  [dry-run]%s PUT repos/%s/environments/%s\n%s%s%s\n' \
      "$DIM" "$RESET" "$REPO" "$nombre" "$DIM" "$(echo "$payload" | jq -c .)" "$RESET"
    return 0
  fi
  echo "$payload" | gh api --method PUT "repos/$REPO/environments/$nombre" --input - >/dev/null
  ok "environment '$nombre' configurado"
}

# staging: sin aprobación. Cada merge a develop despliega solo.
crear_environment staging "$(jq -n '{
  wait_timer: 0,
  prevent_self_review: false,
  reviewers: [],
  deployment_branch_policy: { protected_branches: false, custom_branch_policies: true }
}')"

if ! $DRY_RUN; then
  # Solo `develop` puede desplegar a staging.
  gh api --method POST "repos/$REPO/environments/staging/deployment-branch-policies" \
    -f name=develop -f type=branch >/dev/null 2>&1 || true
fi

# production: revisor obligatorio. Este es el punto donde una persona mira antes
# de que algo llegue a los usuarios (`docs/branching.md`).
REVISORES_JSON='[]'
REVISOR="${ASTRO_PROD_REVIEWER:-}"
if [[ -n "$REVISOR" ]]; then
  TIPO=$(gh api "users/$REVISOR" --jq '.type' 2>/dev/null || echo "")
  if [[ "$TIPO" == "User" ]]; then
    ID=$(gh api "users/$REVISOR" --jq '.id')
    REVISORES_JSON=$(jq -n --argjson id "$ID" '[{type:"User", id:$id}]')
  else
    warn "ASTRO_PROD_REVIEWER='$REVISOR' no es un usuario de GitHub válido; se deja sin revisor"
  fi
else
  # El dueño del repositorio es el revisor por defecto: un environment de
  # producción sin revisor no protege de nada.
  DUENIO=$(gh api "repos/$REPO" --jq '.owner.login')
  TIPO=$(gh api "repos/$REPO" --jq '.owner.type')
  if [[ "$TIPO" == "User" ]]; then
    ID=$(gh api "repos/$REPO" --jq '.owner.id')
    REVISORES_JSON=$(jq -n --argjson id "$ID" '[{type:"User", id:$id}]')
    log "  revisor de producción: @$DUENIO (por defecto; usa ASTRO_PROD_REVIEWER para cambiarlo)"
  else
    warn "el repositorio pertenece a una organización: añade el equipo revisor a mano en Settings → Environments → production"
  fi
fi

crear_environment production "$(jq -n --argjson reviewers "$REVISORES_JSON" '{
  wait_timer: 0,
  prevent_self_review: false,
  reviewers: $reviewers,
  deployment_branch_policy: { protected_branches: true, custom_branch_policies: false }
}')"

if [[ "$REVISORES_JSON" == "[]" ]]; then
  warn "'production' se ha quedado SIN revisor obligatorio: añádelo a mano antes del primer despliegue"
fi

log "  ${DIM}Los secretos (AWS_DEPLOY_ROLE_ARN) y variables (AWS_REGION, HOSTED_ZONE_ID)"
log "  se cargan aparte con 'gh secret set' / 'gh variable set': este script no"
log "  toca secretos a propósito.${RESET}"

# --------------------------------------------------------------------------- #
# 4. Etiquetas
# --------------------------------------------------------------------------- #
info "Etiquetas"

# nombre|color|descripción
ETIQUETAS=(
  "bug|d73a4a|Algo no funciona"
  "enhancement|a2eeef|Funcionalidad nueva"
  "triage|ededed|Sin clasificar todavía"
  "documentation|0075ca|Documentación"
  "backend|1d76db|API FastAPI"
  "frontend|5319e7|Nuxt y visor"
  "models|0e8a16|Pipelines de reconstrucción"
  "infra|c2e0c6|AWS, CDK y CI/CD"
  "ci|bfd4f2|Integración continua"
  "dependencies|0366d6|Actualización de dependencias"
  "docker|2496ed|Imágenes de contenedor"
  "security|b60205|Seguridad"
  "cost|fbca04|Impacto en la factura de AWS"
  "breaking-change|b60205|Rompe el contrato de API o de datos"
  "migration|d4c5f9|Lleva migración de Alembic"
  "licensing|5319e7|Licencias CC y procedencia"
  "physics|e99695|Toca lo que es físicamente posible prometer"
  "model|0e8a16|Tirada de entrenamiento"
  "experiment|c5def5|Experimento, puede no cuajar"
  "backmerge|ededed|Back-merge automático de main a develop"
  "automated|ededed|Abierto por un workflow"
  "good first issue|7057ff|Buen punto de entrada"
  "help wanted|008672|Se agradece ayuda"
  "wontfix|ffffff|No se va a arreglar"
)

for entrada in "${ETIQUETAS[@]}"; do
  IFS='|' read -r nombre color descripcion <<< "$entrada"
  if gh label list --repo "$REPO" --limit 200 --json name --jq '.[].name' 2>/dev/null | grep -qxF "$nombre"; then
    if $DRY_RUN; then
      printf '%s  [dry-run]%s gh label edit %s (asegurar color/descripción)\n' "$DIM" "$RESET" "$nombre"
    else
      gh label edit "$nombre" --repo "$REPO" --color "$color" --description "$descripcion" >/dev/null
      skip "etiqueta '$nombre'"
    fi
  else
    run gh label create "$nombre" --repo "$REPO" --color "$color" --description "$descripcion" >/dev/null \
      && ok "etiqueta '$nombre' creada"
  fi
done

# --------------------------------------------------------------------------- #
# 5. Avisos finales
# --------------------------------------------------------------------------- #
info "Comprobaciones que no se pueden automatizar"

for equipo in maintainers backend frontend astro-ml platform; do
  org="${REPO%%/*}"
  if gh api "orgs/$org/teams/$equipo" >/dev/null 2>&1; then
    ok "equipo @$org/$equipo existe (CODEOWNERS)"
  else
    warn "el equipo @$org/$equipo de .github/CODEOWNERS no existe todavía"
  fi
done

cat <<EOF

  ${GREEN}Listo.${RESET} Queda por hacer a mano:

    1. Secretos y variables por environment:
         gh secret   set AWS_DEPLOY_ROLE_ARN --repo $REPO --env staging    --body 'arn:aws:iam::<cuenta>:role/gha-astro-photos-staging'
         gh secret   set AWS_DEPLOY_ROLE_ARN --repo $REPO --env production --body 'arn:aws:iam::<cuenta>:role/gha-astro-photos-prod'
         gh variable set AWS_REGION          --repo $REPO --env staging    --body 'eu-west-1'
         gh variable set AWS_REGION          --repo $REPO --env production --body 'eu-west-1'
         gh variable set HOSTED_ZONE_ID      --repo $REPO --env staging    --body 'Z...'
         gh variable set HOSTED_ZONE_ID      --repo $REPO --env production --body 'Z...'
    2. El proveedor OIDC de GitHub y los roles en AWS (infra/README.md).
    3. Activar Dependabot alerts, secret scanning y push protection en
       Settings → Code security.

EOF

$DRY_RUN && log "${YELLOW}(dry-run: no se ha cambiado nada)${RESET}"
exit 0
