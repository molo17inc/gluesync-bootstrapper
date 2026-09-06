"""AWS Lambda handler for DbMoto XML to Gluesync YAML conversion API (asynchronous).

This module implements three invocation modes:

- API Gateway POST /convert:
    * Parses multipart/form-data with the uploaded XML (and optional template).
    * Stages the raw payload on the S3 downloads bucket (optional FTP dual-write).
    * Enqueues a job on SQS and creates/updates a DynamoDB job record.
    * Returns a fast HTTP 200 with {status: "queued", job_id: "..."}.

- API Gateway GET /convert?job_id=...:
    * Returns the current job status from DynamoDB.

- SQS-triggered worker:
    * Downloads staged XML/template from S3.
    * Runs the existing conversion pipeline (parse_xml/export_as_yaml/write_conversion_report).
    * Creates the ZIP, uploads it to S3, and updates the DynamoDB job record.
"""

# This program is part of Gluesync.
#
# DbMoto XML Converter is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

import json
import os
import tempfile
import base64
import gzip
import ftplib
import os.path
from urllib.parse import urljoin
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from parse_dbmoto_metadata_xml import parse_xml, export_as_yaml, write_conversion_report


sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")


def _json_default(obj):
    """JSON serializer for objects not serializable by default json code.

    DynamoDB uses Decimal for all numeric types; convert these to int/float
    so they can be emitted in JSON responses.
    """
    if isinstance(obj, Decimal):
        # Prefer int when the value is integral to avoid surprises
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _response(status_code, body_dict):
    """Helper to build HTTP-style responses for API Gateway."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body_dict, default=_json_default),
    }


def _get_jobs_table():
    table_name = os.environ.get("DDB_TABLE_NAME")
    if not table_name:
        return None
    return dynamodb.Table(table_name)


def _init_job(job_id, message):
    """Create an initial job record in DynamoDB (if configured)."""
    table = _get_jobs_table()
    if not table:
        return
    now = datetime.utcnow().isoformat() + "Z"
    item = {
        "job_id": job_id,
        "job_status": "QUEUED",
        "status": "pending",
        "message": message,
        "created_at": now,
        "updated_at": now,
    }
    table.put_item(Item=item)


def _update_job_status(job_id, status, message=None, download_url=None, stats=None):
    """Update job status and metadata in DynamoDB (if configured).

    status: internal job_status (QUEUED, PROCESSING, COMPLETED, FAILED, ...)
    For compatibility with existing clients, we also maintain a "status" field:
      * COMPLETED -> "success"
      * FAILED    -> "error"
      * otherwise -> "pending"
    """
    table = _get_jobs_table()
    if not table:
        return

    now = datetime.utcnow().isoformat() + "Z"
    external_status = "pending"
    if status == "COMPLETED":
        external_status = "success"
    elif status == "FAILED":
        external_status = "error"

    # Use ExpressionAttributeNames to avoid reserved keyword conflicts (e.g. "status").
    update_expr = "SET job_status = :s, #status = :es, updated_at = :u"
    expr_values = {":s": status, ":es": external_status, ":u": now}
    expr_names = {"#status": "status"}

    if message is not None:
        update_expr += ", message = :m"
        expr_values[":m"] = message
    if download_url is not None:
        update_expr += ", download_url = :d"
        expr_values[":d"] = download_url
    if stats is not None:
        update_expr += ", stats = :st"
        expr_values[":st"] = stats

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def parse_multipart_data(body, boundary):
    """Parse multipart/form-data content.

    This is adapted from the original synchronous implementation.
    """
    files = {}
    params = {}
    filenames = {}

    boundary_bytes = b"--" + boundary.encode()
    parts = body.split(boundary_bytes)

    print(f"DEBUG: Found {len(parts)} parts after splitting on boundary")
    for i, part in enumerate(parts):
        print(f"DEBUG: Part {i} length: {len(part)}")
        if i < 3:
            print(f"DEBUG: Part {i} content: {part[:100]}...")

    for part in parts:
        part = part.strip()
        if not part or part == b"--":
            continue

        if b"Content-Disposition" in part:
            print(f"DEBUG: Processing part with Content-Disposition, length: {len(part)}")
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                header_end = part.find(b"\n\n")
                if header_end == -1:
                    continue

            headers = part[:header_end]
            content = part[header_end + 4 :]

            print(f"DEBUG: Headers: {headers[:100]}")
            print(f"DEBUG: Content length: {len(content)}")
            print(f"DEBUG: Content first 20 bytes: {content[:20]}")

            disposition = headers.decode("utf-8", errors="ignore")
            if "filename=" in disposition:
                field_name = None
                filename = None

                for line in disposition.split("\n"):
                    line = line.strip()
                    if line.startswith("Content-Disposition"):
                        parts_disp = line.split(";")
                        for part_disp in parts_disp:
                            part_disp = part_disp.strip()
                            if part_disp.startswith("name="):
                                field_name = part_disp.split("=", 1)[1].strip().strip("\"")
                            elif part_disp.startswith("filename="):
                                filename = part_disp.split("=", 1)[1].strip().strip("\"")

                if field_name:
                    files[field_name] = content
                    filenames[field_name] = filename or "unknown"
                    print(
                        f"DEBUG: Added file {field_name}, filename: {filenames[field_name]}, content length: {len(content)}"
                    )
            else:
                field_name = None
                for line in disposition.split("\n"):
                    line = line.strip()
                    if line.startswith("Content-Disposition"):
                        parts_disp = line.split(";")
                        for part_disp in parts_disp:
                            part_disp = part_disp.strip()
                            if part_disp.startswith("name="):
                                field_name = part_disp.split("=", 1)[1].strip().strip("\"")
                                break

                if field_name:
                    params[field_name] = content.decode("utf-8", errors="ignore").strip()
                    print(f"DEBUG: Added param {field_name}: {params[field_name]}")

    return files, params, filenames


def create_output_zip(output_dir, temp_dir):
    """Create a zip file containing all YAML and report files."""
    import zipfile

    zip_path = os.path.join(temp_dir, "conversion_outputs.zip")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for filename in os.listdir(output_dir):
            if filename.endswith(".yaml") or filename.endswith(".txt"):
                file_path = os.path.join(output_dir, filename)
                zip_file.write(file_path, filename)

    return zip_path


def _s3_client():
    return boto3.client("s3")


def _s3_bucket_and_key(remote_filename):
    bucket = os.environ.get("DOWNLOADS_S3_BUCKET", "molo17-website-downloads-162172359273")
    prefix = os.environ.get("DOWNLOADS_S3_PREFIX", "gs-content/dbmoto-conversion").strip("/")
    key = f"{prefix}/{remote_filename}" if prefix else remote_filename
    return bucket, key


def _upload_to_ftp_legacy(local_path, remote_filename):
    """Optional SiteGround FTP dual-write (DUAL_WRITE_SG=1)."""
    ftp_host = os.environ.get("FTP_HOST")
    ftp_user = os.environ.get("FTP_USER")
    ftp_password = os.environ.get("FTP_PASSWORD")
    ftp_base_path = os.environ.get(
        "FTP_BASE_PATH", "/molo17.com/public_html/gs-content/dbmoto-conversion/"
    )

    if not all([ftp_host, ftp_user, ftp_password]):
        raise ValueError(
            "FTP credentials not configured. Please set FTP_HOST, FTP_USER, and FTP_PASSWORD environment variables."
        )

    ftp = ftplib.FTP(ftp_host, ftp_user, ftp_password)
    ftp.voidcmd("TYPE I")

    try:
        ftp.cwd(ftp_base_path)
    except ftplib.error_perm:
        parts = ftp_base_path.strip("/").split("/")
        current_path = ""
        for part in parts:
            current_path += "/" + part
            try:
                ftp.cwd(current_path)
            except ftplib.error_perm:
                ftp.mkd(current_path)
                ftp.cwd(current_path)

    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {remote_filename}", f)

    ftp.quit()
    return f"ftp://{ftp_host}{ftp_base_path}{remote_filename}"


def upload_to_ftp(local_path, remote_filename):
    """Upload artifact to S3 downloads bucket (primary). Optional FTP dual-write.

    Kept name upload_to_ftp for call-site compatibility; returns public HTTPS URL.
    """
    bucket, key = _s3_bucket_and_key(remote_filename)
    try:
        _s3_client().upload_file(local_path, bucket, key)
        print(f"Uploaded s3://{bucket}/{key}")
    except Exception as e:
        error_msg = f"S3 upload failed: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)

    ftp_url = None
    if os.environ.get("DUAL_WRITE_SG", "0") == "1":
        try:
            ftp_url = _upload_to_ftp_legacy(local_path, remote_filename)
            print(f"Dual-wrote to FTP: {ftp_url}")
        except Exception as e:
            # S3 succeeded; surface FTP failure but do not fail the job
            print(f"WARNING: DUAL_WRITE_SG FTP upload failed: {e}")

    public_url = f"https://molo17.com/{key}"
    return public_url if ftp_url is None else public_url


def download_from_ftp(remote_filename, local_path):
    """Download artifact from S3 downloads bucket (primary). FTP name kept for compatibility."""
    bucket, key = _s3_bucket_and_key(remote_filename)
    try:
        _s3_client().download_file(bucket, key, local_path)
        print(f"Downloaded s3://{bucket}/{key} -> {local_path}")
    except Exception as e:
        error_msg = f"S3 download failed: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)


def _run_conversion_job(xml_content, filename, template_content, include_targets, force_schemas, job_id, context):
    """Core conversion pipeline used by the async SQS worker.

    This reuses the original synchronous logic but operates on in-memory XML bytes.
    """
    gzip_magic = b"\x1f\x8b"

    print(
        f"DEBUG: xml_content type: {type(xml_content)}, length: {len(xml_content) if xml_content else 'None'}"
    )
    print(f"DEBUG: filename: {filename}")
    print(
        f"DEBUG: first 10 bytes: {xml_content[:10] if xml_content and len(xml_content) >= 10 else 'N/A'}"
    )
    print(
        f"DEBUG: starts with gzip magic: {xml_content[:2] == gzip_magic if xml_content and len(xml_content) >= 2 else False}"
    )

    if xml_content is None:
        raise ValueError("XML file content is empty or malformed")

    if xml_content[:2] == gzip_magic:
        print("DEBUG: Decompressing gzipped XML content")
        xml_content = gzip.decompress(xml_content)
        print(f"DEBUG: After decompression, length: {len(xml_content)}")
        print(
            f"DEBUG: First 50 chars after decompression: {xml_content[:50].decode('utf-8', errors='ignore')}"
        )
    elif filename.endswith(".gz"):
        print("DEBUG: Filename ends with .gz, trying base64 decode then gzip decompress")
        try:
            decoded = base64.b64decode(xml_content)
            print(f"DEBUG: Base64 decoded, length: {len(decoded)}")
            print(f"DEBUG: First 20 bytes of decoded: {decoded[:20]}")
            print(f"DEBUG: Decoded starts with gzip magic: {decoded[:2] == gzip_magic}")

            if decoded[:2] == gzip_magic:
                xml_content = gzip.decompress(decoded)
                print(
                    f"DEBUG: Successfully decompressed base64+gzip content, length: {len(xml_content)}"
                )
                print(
                    f"DEBUG: First 50 chars after decompression: {xml_content[:50].decode('utf-8', errors='ignore')}"
                )
            else:
                print("DEBUG: Base64 decoded content is not gzipped, keeping as-is")
                xml_content = decoded
        except Exception as e:
            print(f"DEBUG: Base64 decode failed: {e}, keeping original content")
    else:
        print("DEBUG: XML content is not gzipped")
        print(f"DEBUG: First 50 chars: {xml_content[:50].decode('utf-8', errors='ignore')}")

    include_targets_flag = bool(include_targets)
    force_schemas_value = force_schemas

    with tempfile.TemporaryDirectory() as temp_dir:
        xml_path = os.path.join(temp_dir, "input.xml")
        with open(xml_path, "wb") as f:
            f.write(xml_content)

        template_path = None
        if template_content:
            template_path = os.path.join(temp_dir, "template.yaml")
            with open(template_path, "wb") as f:
                f.write(template_content)

        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        os.environ["XML_PATH"] = xml_path
        os.environ["OUTPUT_DIR"] = output_dir
        if template_path:
            os.environ["TEMPLATE_PATH"] = template_path
        os.environ["INCLUDE_TARGETS"] = str(include_targets_flag)
        if force_schemas_value:
            os.environ["FORCE_SCHEMAS"] = force_schemas_value

        connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings, journal_checkpoints, refresh_filters = parse_xml()
        exported_count = export_as_yaml(
            connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name,
            record_id_mappings=record_id_mappings, journal_checkpoints=journal_checkpoints, refresh_filters=refresh_filters
        )

        report_path = write_conversion_report()

        zip_path = create_output_zip(output_dir, temp_dir)

        request_id = job_id or getattr(context, "aws_request_id", "unknown")
        prefix = os.environ.get("DOWNLOADS_S3_PREFIX", "gs-content/dbmoto-conversion").strip("/")
        base_url = f"https://molo17.com/{prefix}/"

        zip_filename = f"conversion_{request_id}.zip"
        # Primary publish is S3; function name retained for compatibility
        storage_url = upload_to_ftp(zip_path, zip_filename)
        public_url = urljoin(base_url, zip_filename)

        stats = {
            "yaml_files_generated": exported_count,
            "tables_processed": sum(
                len(schema.get("tables", {}))
                for conn in connections.values()
                for schema in conn.get("schemas", {}).values()
            ),
            "zip_file_url": public_url,
            "zip_file_size": os.path.getsize(zip_path),
        }

        return {
            "status": "success",
            "message": f"Successfully processed {exported_count} YAML files",
            "request_id": request_id,
            "download_url": public_url,
            "ftp_path": storage_url,  # public HTTPS (S3-backed); legacy field name
            "stats": stats,
        }


def _handle_submit_request(event, context):
    """Handle API Gateway POST /convert: stage XML to S3, enqueue SQS job, create DDB record."""
    headers = event.get("headers", {}) or {}
    content_type = headers.get("content-type") or headers.get("Content-Type", "")

    if "multipart/form-data" not in content_type:
        return _response(400, {"error": "Content-Type must be multipart/form-data"})

    boundary = content_type.split("boundary=")[-1]

    if event.get("isBase64Encoded"):
        print("DEBUG: Body is base64 encoded (binary data) [submit]")
        body = base64.b64decode(event["body"])
    else:
        print("DEBUG: Body is not base64 encoded [submit]")
        body_data = event["body"]
        if isinstance(body_data, str):
            print("DEBUG: Treating string body as latin-1 to preserve bytes [submit]")
            body = body_data.encode("latin-1", errors="replace")
        else:
            body = body_data

    files, params, filenames = parse_multipart_data(body, boundary)
    print(f"DEBUG[submit]: files keys: {list(files.keys())}")
    print(f"DEBUG[submit]: params keys: {list(params.keys())}")
    print(f"DEBUG[submit]: filenames: {filenames}")

    if "xml_file" not in files:
        return _response(400, {"error": "xml_file is required"})

    xml_content = files["xml_file"]
    template_content = files.get("template_file")
    filename = filenames.get("xml_file", "metadata.xml")

    include_targets = params.get("include_targets", "true").lower() == "true"
    force_schemas = params.get("force_schemas")
    trial_kit_id = params.get("trial_kit_id")

    job_id = context.aws_request_id
    print(f"DEBUG[submit]: job_id={job_id}")

    staging_xml_name = f"staging_{job_id}.bin"
    staging_template_name = None

    with tempfile.TemporaryDirectory() as temp_dir:
        xml_stage_path = os.path.join(temp_dir, staging_xml_name)
        with open(xml_stage_path, "wb") as f:
            f.write(xml_content)
        upload_to_ftp(xml_stage_path, staging_xml_name)

        if template_content:
            staging_template_name = f"staging_template_{job_id}.yaml"
            template_stage_path = os.path.join(temp_dir, staging_template_name)
            with open(template_stage_path, "wb") as f:
                f.write(template_content)
            upload_to_ftp(template_stage_path, staging_template_name)

    _init_job(job_id, "Job queued")

    queue_url = os.environ.get("SQS_QUEUE_URL")
    if not queue_url:
        _update_job_status(job_id, "FAILED", "SQS_QUEUE_URL not configured")
        return _response(500, {"error": "Queue not configured"})

    message = {
        "job_id": job_id,
        "xml_remote_filename": staging_xml_name,
        "template_remote_filename": staging_template_name,
        "filename": filename,
        "include_targets": include_targets,
        "force_schemas": force_schemas,
        "trial_kit_id": trial_kit_id,
    }

    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
    _update_job_status(job_id, "QUEUED", "Job queued")

    return _response(
        200,
        {
            "status": "queued",
            "job_id": job_id,
            "message": "Your conversion job has been queued.",
        },
    )


def _handle_status_request(event, context):
    """Handle API Gateway GET /convert?job_id=...: return job status from DynamoDB."""
    params = event.get("queryStringParameters") or {}
    job_id = params.get("job_id") if params else None

    if not job_id:
        return _response(400, {"error": "job_id is required"})

    table = _get_jobs_table()
    if not table:
        return _response(500, {"error": "DDB_TABLE_NAME not configured"})

    try:
        res = table.get_item(Key={"job_id": job_id})
    except ClientError as e:
        print(f"Error reading job status from DynamoDB: {e}")
        return _response(500, {"error": "Error reading job status"})

    item = res.get("Item")
    if not item:
        return _response(
            200,
            {
                "job_id": job_id,
                "job_status": "UNKNOWN",
                "status": "pending",
                "message": "Job not found",
            },
        )

    body = {
        "job_id": job_id,
        "job_status": item.get("job_status", "UNKNOWN"),
        "status": item.get("status", "pending"),
        "message": item.get("message"),
        "download_url": item.get("download_url"),
        "stats": item.get("stats"),
    }

    return _response(200, body)


def _handle_sqs_event(event, context):
    """Handle SQS events: process queued conversion jobs."""
    records = event.get("Records", [])
    for record in records:
        try:
            body = json.loads(record.get("body", "{}"))
            _process_job(body, context)
        except Exception as e:
            print(f"Error processing SQS record: {e}")

    return {"statusCode": 200, "body": json.dumps({"message": "SQS batch processed"})}


def _process_job(job, context):
    job_id = job.get("job_id") or getattr(context, "aws_request_id", "unknown")
    xml_remote_filename = job.get("xml_remote_filename")
    template_remote_filename = job.get("template_remote_filename")
    filename = job.get("filename", "metadata.xml")
    include_targets = job.get("include_targets", True)
    force_schemas = job.get("force_schemas")

    print(
        f"DEBUG[worker]: Processing job_id={job_id}, xml_remote_filename={xml_remote_filename}, template_remote_filename={template_remote_filename}"
    )

    if not xml_remote_filename:
        _update_job_status(job_id, "FAILED", "Missing xml_remote_filename in job message")
        return

    _update_job_status(job_id, "PROCESSING", "Conversion in progress")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            xml_stage_path = os.path.join(temp_dir, "staging.bin")
            download_from_ftp(xml_remote_filename, xml_stage_path)
            with open(xml_stage_path, "rb") as f:
                xml_content = f.read()

            template_content = None
            if template_remote_filename:
                template_stage_path = os.path.join(temp_dir, "staging_template.yaml")
                download_from_ftp(template_remote_filename, template_stage_path)
                with open(template_stage_path, "rb") as f:
                    template_content = f.read()

            result = _run_conversion_job(
                xml_content=xml_content,
                filename=filename,
                template_content=template_content,
                include_targets=include_targets,
                force_schemas=force_schemas,
                job_id=job_id,
                context=context,
            )

        _update_job_status(
            job_id,
            "COMPLETED",
            result.get("message"),
            result.get("download_url"),
            result.get("stats"),
        )

    except Exception as e:
        print(f"Error processing job {job_id}: {e}")
        _update_job_status(job_id, "FAILED", str(e))
        raise


def lambda_handler(event, context):  # type: ignore[override]
    """Entry point for all invocations (API Gateway + SQS).

    - SQS event: process queued jobs.
    - API Gateway POST /convert: submit job.
    - API Gateway GET /convert?job_id=...: return job status.
    """
    try:
        if "Records" in event and event.get("Records"):
            first = event["Records"][0]
            if first.get("eventSource") == "aws:sqs":
                return _handle_sqs_event(event, context)

        http_method = event.get("httpMethod")
        if http_method == "GET":
            return _handle_status_request(event, context)
        if http_method == "POST":
            return _handle_submit_request(event, context)

        return _response(400, {"error": "Unsupported request"})

    except Exception as e:
        print(f"Error in lambda_handler: {str(e)}")
        import traceback

        traceback.print_exc()

        return _response(
            500,
            {
                "status": "error",
                "message": str(e),
                "request_id": getattr(context, "aws_request_id", "unknown"),
            },
        )

