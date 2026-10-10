#!/usr/bin/env bash
# Explicit actions only: this script never provisions a VM or a database.
set -euo pipefail
action=${1:?Usage: deploy.sh init|migrate|reset-password|web|backup|schedule}
: "${PROJECT_ID:?Set PROJECT_ID explicitly}"
REGION=${REGION:-asia-east1}
SQL_INSTANCE=${SQL_INSTANCE:-taguette-db}
SERVICE=${SERVICE:-taguette}
INSTANCE_CONNECTION_NAME="$PROJECT_ID:$REGION:$SQL_INSTANCE"
APP_PASSWORD_VERSION=${APP_PASSWORD_VERSION:-1}
OWNER_PASSWORD_VERSION=${OWNER_PASSWORD_VERSION:-1}
BACKUP_PASSWORD_VERSION=${BACKUP_PASSWORD_VERSION:-1}
ADMIN_PASSWORD_VERSION=${ADMIN_PASSWORD_VERSION:-1}
COOKIE_SECRET_VERSION=${COOKIE_SECRET_VERSION:-1}

require_digest() {
  if [[ "$1" != *@sha256:* ]]; then
    echo 'Use an immutable image digest, not a mutable tag.' >&2
    exit 2
  fi
}

case "$action" in
  init|migrate|reset-password)
    : "${WEB_IMAGE:?Set WEB_IMAGE to the web image digest}"
    require_digest "$WEB_IMAGE"
    job="taguette-$action"
    secrets="/secrets/db-password=taguette-owner-password:$OWNER_PASSWORD_VERSION"
    envs="INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,TAGUETTE_DB_USER=taguette_owner,TAGUETTE_DB_PASSWORD_FILE=/secrets/db-password"
    if [[ "$action" != migrate ]]; then
      secrets+=",/secrets/admin-password=taguette-admin-password:$ADMIN_PASSWORD_VERSION"
      envs+=",TAGUETTE_ADMIN_PASSWORD_FILE=/secrets/admin-password"
    fi
    gcloud run jobs deploy "$job" --project "$PROJECT_ID" --region "$REGION" \
      --image "$WEB_IMAGE" --service-account "taguette-manage@$PROJECT_ID.iam.gserviceaccount.com" \
      --set-cloudsql-instances "$INSTANCE_CONNECTION_NAME" \
      --set-env-vars "$envs" --set-secrets "$secrets" \
      --command python --args "-m,taguette.cloud,$action" \
      --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 900s \
      --cpu 1 --memory 512Mi
    gcloud run jobs execute "$job" --project "$PROJECT_ID" --region "$REGION" --wait
    ;;
  web)
    : "${WEB_IMAGE:?Set WEB_IMAGE to the web image digest}"
    : "${TAGUETTE_DOMAIN:?Set TAGUETTE_DOMAIN to a hostname without https://}"
    require_digest "$WEB_IMAGE"
    gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --image "$WEB_IMAGE" --execution-environment gen2 \
      --service-account "taguette-web@$PROJECT_ID.iam.gserviceaccount.com" \
      --set-cloudsql-instances "$INSTANCE_CONNECTION_NAME" \
      --set-env-vars "INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,TAGUETTE_DOMAIN=$TAGUETTE_DOMAIN,TAGUETTE_DB_PASSWORD_FILE=/secrets/db-password,TAGUETTE_SECRET_KEY_FILE=/secrets/cookie-secret,TORNADO_SHUTDOWN_TIME=8" \
      --set-secrets "/secrets/db-password=taguette-app-password:$APP_PASSWORD_VERSION,/secrets/cookie-secret=taguette-cookie-secret:$COOKIE_SECRET_VERSION" \
      --cpu 1 --memory 2Gi --concurrency 4 --timeout 900 \
      --min 0 --max 1 --max-instances 1 --port 8080 \
      --startup-probe 'httpGet.path=/health,httpGet.port=8080,periodSeconds=10,timeoutSeconds=5,failureThreshold=24' \
      --allow-unauthenticated
    # The endpoint is reachable; Taguette still requires the admin password.
    ;;
  backup)
    : "${BACKUP_IMAGE:?Set BACKUP_IMAGE to the backup image digest}"
    require_digest "$BACKUP_IMAGE"
    bucket=${TAGUETTE_BACKUP_BUCKET:-$PROJECT_ID-taguette-backups}
    gcloud run jobs deploy taguette-backup --project "$PROJECT_ID" --region "$REGION" \
      --image "$BACKUP_IMAGE" --service-account "taguette-backup@$PROJECT_ID.iam.gserviceaccount.com" \
      --set-cloudsql-instances "$INSTANCE_CONNECTION_NAME" \
      --set-env-vars "INSTANCE_CONNECTION_NAME=$INSTANCE_CONNECTION_NAME,TAGUETTE_DB_PASSWORD_FILE=/secrets/db-password,TAGUETTE_BACKUP_BUCKET=$bucket" \
      --set-secrets "/secrets/db-password=taguette-backup-password:$BACKUP_PASSWORD_VERSION" \
      --tasks 1 --parallelism 1 --max-retries 1 --task-timeout 900s \
      --cpu 1 --memory 1Gi
    gcloud run jobs execute taguette-backup --project "$PROJECT_ID" --region "$REGION" --wait
    ;;
  schedule)
    scheduler="taguette-scheduler@$PROJECT_ID.iam.gserviceaccount.com"
    gcloud run jobs add-iam-policy-binding taguette-backup --project "$PROJECT_ID" \
      --region "$REGION" --member "serviceAccount:$scheduler" --role roles/run.invoker
    operation=create
    if gcloud scheduler jobs describe taguette-weekly-backup --project "$PROJECT_ID" \
        --location "$REGION" >/dev/null 2>&1; then
      operation=update
    fi
    gcloud scheduler jobs "$operation" http taguette-weekly-backup \
      --project "$PROJECT_ID" --location "$REGION" \
      --schedule '0 3 * * 0' --time-zone Asia/Taipei \
      --uri "https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/taguette-backup:run" \
      --http-method POST --oauth-service-account-email "$scheduler"
    ;;
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac
