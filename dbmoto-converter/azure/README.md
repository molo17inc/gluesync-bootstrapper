# DbMoto converter on Azure (mirror of the `dbmoto-xml-converter` Lambda)

The converter API (`https://dbmoto-converter.labs.molo17.com/convert`, used by the molo17.com page
`/syniti-replicate-metadata-converter/`) also runs on Azure Container Apps, so it keeps working when AWS Lambda is
unavailable. The handler code is the same `lambda_function.py` + `parse_dbmoto_metadata_xml.py` the `deploy_lambda`
job zips; `runner.py` hosts it unchanged.

## What runs where

Resource group `rg-molo17-dr-weu`, environment `cae-molo17-dr-neu` (North Europe), images in `molo17dracr`.

| App | Role | Image |
| --- | --- | --- |
| `dbmoto-xml-converter-api` | `MODE=http`: API Gateway REST (v1) events for `POST /convert` (submit) and `GET /convert?job_id=` (status); any other route answers 403 like API Gateway. External ingress. | `dbmoto-xml-converter:<sha>` |
| `dbmoto-xml-converter-worker` | `MODE=sqs`: takes one job at a time from the queue and runs the conversion (Lambda timeout 900 s). One replica: the converter keeps per-job state in process globals. | `dbmoto-xml-converter:<sha>` |
| `dbmoto-xml-converter-sqs` | ElasticMQ 1.7.1 (SQS API), queue `dbmoto-xml-converter-jobs`, messages persisted (H2) on the Azure Files share `dbmoto-sqs`, so queued jobs survive restarts. Config: `elasticmq.conf` in this folder, stored on the share. Internal ingress. | `dbmoto-xml-converter/elasticmq:1.7.1` |
| `dbmoto-xml-converter-ddb` | DynamoDB Local 2.5.4, table `dbmoto-xml-converter-jobs` (job status), data on the Azure Files share `dbmoto-ddb`. Internal ingress. | `dbmoto-xml-converter/dynamodb-local:2.5.4` |

The API and the worker reach the queue and the table through the AWS SDK endpoint overrides
`AWS_ENDPOINT_URL_SQS=http://dbmoto-xml-converter-sqs` and `AWS_ENDPOINT_URL_DYNAMODB=http://dbmoto-xml-converter-ddb`;
`runner.py` creates the queue and table if they are missing (`ENSURE_QUEUE`, `ENSURE_TABLE`).
Why not the AWS queue and table: the CI deploy key may use the S3 downloads bucket but not SQS or DynamoDB, and the
Lambda execution role can only be assumed by Lambda. Giving the Azure apps an IAM user with the Lambda role's policy
would let them share the AWS queue and table instead: drop the two endpoint overrides, set `SQS_QUEUE_URL` to the AWS
queue URL and swap the key secrets.

Data that stays on AWS: inputs are staged and result ZIPs published in `s3://molo17-website-downloads-162172359273/gs-content/dbmoto-conversion/`
(served as `https://molo17.com/gs-content/dbmoto-conversion/...`), exactly as the Lambda does.

## Deploy

`deploy:azure` in `.gitlab-ci.yml` runs on the same `dbmoto-*` tags as `deploy_lambda`, independently of it (it only
needs the converter unit tests), so a release reaches Azure even when the AWS deploy cannot run. It builds the image
with `az acr build`, rolls the API and the worker to it, waits for both revisions, then runs a real conversion of
`tests/data/rrtest.xml` against the Azure FQDN.

The rollout changes only the image. Configuration and secrets stay as set on the apps:

| App secret (api + worker) | Environment variable | Source |
| --- | --- | --- |
| `aws-access-key-id` / `aws-secret-access-key` | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | CI variables of the same names (S3 downloads bucket access) |
| `ftp-host`, `ftp-user`, `ftp-password` | `FTP_HOST`, `FTP_USER`, `FTP_PASSWORD` | CI variables `FTP_SITE`, `FTP_USER`, `FTP_PASSWORD` (used only when `DUAL_WRITE_SG=1`) |

Plain settings on both apps mirror the Lambda environment in `cloudformation-template.yaml`: `DOWNLOADS_S3_BUCKET`,
`DOWNLOADS_S3_PREFIX`, `DUAL_WRITE_SG=0`, `FTP_BASE_PATH`, `SQS_QUEUE_URL`, `DDB_TABLE_NAME`.

Azure credentials: the protected CI variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_CLIENT_SECRET` (service principal `gitlab-ci-azure-deploy`, Contributor on `rg-molo17-dr-weu`, AcrPush on
`molo17dracr`). Every tag is protected in this project, so `dbmoto-*` tag pipelines see them.

## Custom domain

`dbmoto-converter.labs.molo17.com` points at API Gateway on AWS. To serve it from Azure, the API app gets the
hostname with the Let's Encrypt certificate `dbmoto-converter-labs-molo17-le` uploaded to the environment (needs the
TXT record `asuid.dbmoto-converter.labs.molo17.com`), then the CNAME moves to
`dbmoto-xml-converter-api.delightfulmushroom-76d2e0ca.northeurope.azurecontainerapps.io`.
