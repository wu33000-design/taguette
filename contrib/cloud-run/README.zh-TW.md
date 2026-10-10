# 單人使用：Cloud Run + Cloud SQL PostgreSQL

此 fork 的部署目標是：單一使用者、保留密碼登入、文件匯入時才轉檔，
不配置 VM、Redis 或多人協作服務。Cloud Run 關閉瀏覽器且無請求後可縮到零；
Cloud SQL、儲存與備份仍會計費。瀏覽器開啟專案時，每 30 秒以短請求
讀取 SQL 事件；新增、修改或刪除完成後立即刷新，也能處理不同執行個體。

網頁介面預設為繁體中文（台灣），包含登入、專案、文件、標籤、錯誤訊息
與匯出內容。瀏覽器語言不會覆蓋此預設；可在「帳號 → 設定 → 語言」
選擇其他語言，或選「預設語言」恢復繁體中文。
`TAGUETTE_LANGUAGE` 可覆蓋部署預設值 `zh_TW`。

## 預設架構

| 元件 | 設定 |
| --- | --- |
| Cloud Run | gen2、1 vCPU、2 GiB、min 0、max 1、concurrency 4 |
| 文件轉換 | 同一容器的 Calibre/wv；一次最多 1 個轉檔程序 |
| 資料庫 | PostgreSQL 16、Enterprise、db-f1-micro、單區 |
| 磁碟 | 10 GB SSD、自動擴充上限 50 GB |
| 身分 | 固定 `admin` 帳號，關閉註冊與協作者管理 |
| 備份 | 每日 Cloud SQL 備份保留 14 份；PITR 日誌保留 7 天 |
| 邏輯備份 | 每週 Cloud Run Job → 私人 Cloud Storage，保留 56 天 |
| 原始文件 | 獨立私人 bucket，需自行上傳；不會自動典藏 |

這是低成本起始規格，不包含 HA；共享核心 Cloud SQL 不涵蓋 SLA。
Cloud Run 的 max 1 用於控費，**事件正確性不依賴只有一個執行個體**。
2 GiB 是否足以處理你的 PDF 要經實際匯入測試，必要時提高至 4 GiB。
掃描 PDF 需事先 OCR。文件轉檔尚未改為獨立非同步服務；若請求被中止，
匯入可能失敗，請先確認是否已建立文件再重試，避免重複文件。

## 1. 建立基礎設施

需要已啟用計費的新 GCP 專案、gcloud、Terraform 1.5+，以及 repo 的乾淨版本。
請使用專用專案，勿套用到現有其他專案。

```bash
export PROJECT_ID='你的專案ID'
export REGION='asia-east1'
export SQL_INSTANCE='taguette-db'
terraform -chdir=contrib/cloud-run init
terraform -chdir=contrib/cloud-run plan \
  -var="project_id=$PROJECT_ID" -var="region=$REGION" \
  -var="sql_instance=$SQL_INSTANCE" -out=plan.tfplan
terraform -chdir=contrib/cloud-run apply plan.tfplan
```

`apply` 會建立付費 Cloud SQL、buckets、服務帳號、Artifact Registry 與
**空的** Secret Manager secret。這些檔案未執行任何雲端部署。
Terraform state 需保存在受控、持久位置；不要提交 state、plan 或密碼。
repo 已保留 `.terraform.lock.hcl`，固定實際解析的 provider 版本。
在 Billing 建立預算告警；預算告警不會自動停止計費。

## 2. 資料庫角色與 Secret Manager

在 Cloud SQL Console 建立以下三個內建登入角色，使用不同的強密碼：

| SQL 角色 | 用途 | 對應 secret |
| --- | --- | --- |
| `taguette_owner` | 初始化與 migration，擁有 Taguette 資料表 | `taguette-owner-password` |
| `taguette_app` | 網頁讀寫資料，不建立或修改資料表 | `taguette-app-password` |
| `taguette_backup` | 唯讀備份 | `taguette-backup-password` |

在 `taguette` 資料庫，以 Cloud SQL 管理者執行 `roles.sql`，**在 init 之前執行**。
Cloud SQL Console 新建角色可能自動取得 `cloudsqlsuperuser`，此檔案會撤銷
該權限並設定有限的 schema、資料表與 sequence 權限。不要讓 app 使用 owner。
如果已初始化，檢查資料表均由 `taguette_owner` 擁有，再執行權限設定。

將三個資料庫密碼加入對應 secret 的版本。另加入：

- `taguette-admin-password`：你的 Taguette 登入密碼，至少 12 字元。
- `taguette-cookie-secret`：獨立、隨機、至少 32 字元的簽章密鑰。

可以使用 Secret Manager Console 輸入 secret，避免出現在 shell history。
部署腳本預設使用 secret 版本 `1`；更換版本時，設定腳本中對應的
`APP_PASSWORD_VERSION`、`OWNER_PASSWORD_VERSION`、`BACKUP_PASSWORD_VERSION`、
`ADMIN_PASSWORD_VERSION` 或 `COOKIE_SECRET_VERSION`。

web 服務無權讀取 owner、backup 或管理者登入密碼。
資料庫密碼放在 `connect_args` 而非 URL；應用程式不會將 URL 密碼寫入日誌。
所有 secret 都以檔案掛載，禁止提交到 repo。

## 3. 建置兩個映像

從 repo 根目錄執行：

```bash
export IMAGE_TAG="$(git rev-parse HEAD)"
gcloud builds submit --project "$PROJECT_ID" \
  --config contrib/cloud-run/cloudbuild.yaml \
  --substitutions="_REGION=$REGION,_TAG=$IMAGE_TAG" .
```

Cloud Build 執行帳號需要 Artifact Registry Writer。若專案未配置建置帳號，
先依 Cloud Build 的專案設定建立並授權，勿為 web 帳號增加建置權限。

在 Artifact Registry 取得兩個映像的 digest，設定完整映像名稱：

```bash
export WEB_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/taguette/web@sha256:替換為digest"
export BACKUP_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/taguette/backup@sha256:替換為digest"
```

腳本拒絕以 mutable tag 部署。web 依賴由 `poetry.lock` 匯出；備份映像使用
PostgreSQL 16 的 pg_dump 與固定版本的 Storage SDK。基底映像使用版本 tag，
最終產物固定 digest；之後要重建以取得系統安全更新。

## 4. 初始化，再部署網頁

```bash
bash contrib/cloud-run/deploy.sh init
export TAGUETTE_DOMAIN='taguette.invalid'
bash contrib/cloud-run/deploy.sh web
export TAGUETTE_DOMAIN="$(gcloud run services describe taguette \
  --project "$PROJECT_ID" --region "$REGION" \
  --format='value(status.url)' | sed 's#https://##')"
bash contrib/cloud-run/deploy.sh web
```

`taguette.invalid` 只供第一次取得 Cloud Run URL；第二次設定實際 hostname。
若你已決定自訂網域，直接使用該 hostname，另外完成 DNS 與網域映射。

init Job 建立資料表與 `admin`，重複執行不會覆寫既有密碼。
日常 web 啟動要求初始化已完成，不會自行建立資料表、執行 PostgreSQL
migration 或印出臨時密碼。以 `admin` 和你設定的密碼登入。

網頁 endpoint 允許網際網路連線，但研究頁面和 API 仍須 Taguette 密碼登入。
保留 XSRF 保護，登入 cookie 使用 Secure、HttpOnly、SameSite=Lax。
不提供郵件重設密碼；登入後可在帳號設定修改密碼，忘記密碼則使用管理 Job。

## 5. 備份與排程

```bash
bash contrib/cloud-run/deploy.sh backup
bash contrib/cloud-run/deploy.sh schedule
```

第一次 backup 會立刻執行並確認上傳。scheduler 每週日台灣時間 03:00
以 OAuth 呼叫備份 Job；scheduler 帳號只取得該 Job 的 invoker。
備份帳號只有讀取資料庫與新增備份物件權限；dump 失敗或為空不會上傳。
排程請求成功只代表 Job 被啟動，**仍需針對 Job 執行失敗設定告警**。
同時監控 Cloud SQL 儲存使用量、備份狀態與記憶體。
備份 Job 的暫存 dump 會占用記憶體；預設 1 GiB 適合小型資料庫。
資料增長時提高 Job 記憶體，或改採 Cloud SQL 匯出，不能假設 1 GiB
可備份任意大小的資料庫。

原始文件不會自動保存到 bucket；你可自行上傳至
`gs://專案ID-taguette-originals`。文件及資料庫可能包含研究個資，兩個
bucket 都禁止公開存取；持有權限的人仍能讀取備份。

## 6. 還原演練

1. 建立隔離的 PostgreSQL 16 測試資料庫，勿覆寫正式資料。
2. 由有權限的管理者下載一份 `.dump`。
3. 建立 `taguette_owner`，以該角色執行
   `pg_restore --no-owner --no-acl --exit-on-error --dbname=測試連線 research.dump`。
4. 在測試資料庫執行 `roles.sql`，使 app 與 backup 取得權限。
5. 啟動同一版 Taguette，驗證帳號、文件、標籤、中文標註位置與匯出。
6. 完成後移除演練環境，保留驗證紀錄。

不要把密碼放進連線 CLI 參數；使用受控的 `.pgpass` 或 secret 環境。
還原初期目標：每日備份最多遺失 24 小時資料，4 小時內恢復，
以實際演練結果確認。PITR 可縮小資料遺失窗口，但仍需驗證流程。

## 7. 升級與密碼重設

升級先執行 backup，在隔離資料庫測試新映像的 migration。
正式升級時關閉瀏覽器、暫時撤銷 endpoint 的公開 invoker 並確認在途請求結束，
避免 web 與 schema migration 同時寫入。將 `WEB_IMAGE` 改為新 digest：

```bash
bash contrib/cloud-run/deploy.sh migrate
bash contrib/cloud-run/deploy.sh web
```

web 部署會恢復公開 endpoint（仍需登入）。若 migration 失敗，先修復或還原
資料庫，勿只切回舊映像。`migrate` 和 `init` 使用 owner，web 使用 app。
資料庫升級 Job 的 engine 保留檔案掛載密碼與連線參數。

忘記密碼：先新增 admin-password secret 版本，再明確執行：

```bash
export ADMIN_PASSWORD_VERSION='新的版本號'
bash contrib/cloud-run/deploy.sh reset-password
```

重設密碼不會撤銷既有登入 cookie。如需強制登出，輪替 cookie-secret 並重部署 web。

## 驗證

GitHub Actions 會測試 SQLite 與 PostgreSQL 16、驗證事件排程，
並建置兩個容器，不會部署或建立付費資源。

```bash
poetry install --with validation -E postgres
poetry run scripts/update_translations.sh
poetry run python -m unittest tests tests_cloud -v
node contrib/cloud-run/test-events.cjs
bash -n contrib/cloud-run/deploy.sh
```

正式啟用前需額外驗證：真實 PDF/DOCX 轉檔、中文標註與匯出、
Cloud Run 重啟資料持久性、Cloud SQL Auth Proxy 連線，以及備份還原。
