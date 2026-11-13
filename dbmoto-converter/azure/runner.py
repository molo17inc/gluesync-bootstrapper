"""Host the unmodified DbMoto converter Lambda handler (lambda_function.lambda_handler) in a container.

MODE=http  Plain HTTP server that builds API Gateway REST (payload v1) proxy events, like the AWS REST API
           dbmoto-xml-converter-api: only GET and POST /convert are routed; anything else answers 403
           {"message":"Missing Authentication Token"} as API Gateway does. multipart/form-data is a binary
           media type there, so those bodies reach the handler base64-encoded.
MODE=sqs   Long-polls the jobs queue (QUEUE_URL, default SQS_QUEUE_URL) and invokes the handler with one SQS
           record at a time (the Lambda event source mapping has BatchSize 1). The message is deleted only when
           the handler returns; if the process dies mid-job the message becomes visible again after the queue's
           visibility timeout (900 s) and is processed again, so no job is lost.
GET /__runner/health answers 200 in http mode.
The queue and the job table can live on AWS (SQS, DynamoDB) or on Azure-local stand-ins (ElasticMQ, DynamoDB Local)
selected with the AWS SDK's AWS_ENDPOINT_URL_SQS / AWS_ENDPOINT_URL_DYNAMODB; see ensure_state().
"""
import base64
import importlib
import json
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, os.environ.get('APP_DIR', '/app'))
MODE = os.environ.get('MODE', 'http')
HANDLER_SPEC = os.environ.get('HANDLER', 'lambda_function.lambda_handler')
FUNCTION_NAME = os.environ.get('FUNCTION_NAME', 'dbmoto-xml-converter')
STAGE = os.environ.get('STAGE', 'prod')
TIMEOUT_S = int(os.environ.get('HANDLER_TIMEOUT_S', '900'))
ROUTES = {('POST', '/convert'), ('GET', '/convert')}
BINARY_TYPES = ('multipart/form-data',)


def load_handler():
    mod, fn = HANDLER_SPEC.rsplit('.', 1)
    return getattr(importlib.import_module(mod), fn)


class Context:
    def __init__(self, timeout_s):
        self.aws_request_id = str(uuid.uuid4())
        self.function_name = FUNCTION_NAME
        self.function_version = '$LATEST'
        self.memory_limit_in_mb = int(os.environ.get('MEMORY_LIMIT_MB', '1024'))
        self.invoked_function_arn = f'arn:azure:containerapp:{FUNCTION_NAME}'
        self.log_group_name = f'/azure/containerapp/{FUNCTION_NAME}'
        self.log_stream_name = os.environ.get('HOSTNAME', 'local')
        self._deadline = time.time() + timeout_s

    def get_remaining_time_in_millis(self):
        return max(0, int((self._deadline - time.time()) * 1000))


def log(**kw):
    print(json.dumps(kw), flush=True)


# ── HTTP mode ─────────────────────────────────────────────────────────────
class HttpHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    handler = None

    def log_message(self, *a):
        pass

    def _send(self, status, body=b'', headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for k, v in (headers or {}).items():
            if isinstance(v, list):
                for one in v:
                    self.send_header(k, str(one))
            else:
                self.send_header(k, str(v))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _event(self, u, raw):
        headers, mv_headers = {}, {}
        for k, v in self.headers.items():
            headers[k] = v
            mv_headers.setdefault(k, []).append(v)
        qs_pairs = parse_qsl(u.query, keep_blank_values=True)
        qs, mv_qs = {}, {}
        for k, v in qs_pairs:
            qs[k] = v
            mv_qs.setdefault(k, []).append(v)
        ct = (self.headers.get('Content-Type') or '').lower()
        binary = bool(raw) and any(t in ct for t in BINARY_TYPES)
        ip = (self.headers.get('X-Forwarded-For') or self.client_address[0]).split(',')[0].strip()
        now = datetime.now(timezone.utc)
        if not raw:
            body = None
        elif binary:
            body = base64.b64encode(raw).decode()
        else:
            body = raw.decode('utf-8', errors='replace')
        return {
            'resource': u.path, 'path': u.path, 'httpMethod': self.command,
            'headers': headers, 'multiValueHeaders': mv_headers,
            'queryStringParameters': qs or None, 'multiValueQueryStringParameters': mv_qs or None,
            'pathParameters': None, 'stageVariables': None,
            'requestContext': {
                'resourcePath': u.path, 'httpMethod': self.command, 'path': f'/{STAGE}{u.path}', 'stage': STAGE,
                'requestId': str(uuid.uuid4()), 'requestTime': now.strftime('%d/%b/%Y:%H:%M:%S +0000'),
                'requestTimeEpoch': int(now.timestamp() * 1000), 'protocol': self.request_version,
                'domainName': self.headers.get('Host', ''), 'apiId': 'azure', 'accountId': 'azure',
                'identity': {'sourceIp': ip, 'userAgent': self.headers.get('User-Agent')},
            },
            'body': body, 'isBase64Encoded': binary,
        }

    def _handle(self):
        started = time.time()
        u = urlsplit(self.path)
        if u.path == '/__runner/health':
            return self._send(200, 'ok', {'Content-Type': 'text/plain'})
        path = u.path.rstrip('/') or '/'
        if (self.command, path) not in ROUTES:
            return self._send(403, '{"message":"Missing Authentication Token"}', {'Content-Type': 'application/json'})
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            out = self.handler(self._event(u._replace(path=path), raw), Context(TIMEOUT_S)) or {}
        except Exception:
            log(msg='handler error', method=self.command, path=path, error=traceback.format_exc())
            return self._send(502, '{"message": "Internal server error"}', {'Content-Type': 'application/json'})
        status = int(out.get('statusCode') or 200)
        hdrs = dict(out.get('headers') or {})
        hdrs.update(out.get('multiValueHeaders') or {})
        body = out.get('body') or ''
        body = base64.b64decode(body) if out.get('isBase64Encoded') else str(body)
        self._send(status, body, hdrs)
        log(msg='request', method=self.command, path=path, status=status, ms=int((time.time() - started) * 1000))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


def run_http():
    HttpHandler.handler = staticmethod(load_handler())
    port = int(os.environ.get('PORT', '8080'))
    log(msg=f'runner http on :{port}', function=FUNCTION_NAME, routes=sorted(f'{m} {p}' for m, p in ROUTES))
    ThreadingHTTPServer(('0.0.0.0', port), HttpHandler).serve_forever()


# ── SQS mode ──────────────────────────────────────────────────────────────
STOP = False


def _stop(*_):
    # Finish the job in hand, then exit (Container Apps sends SIGTERM on scale-in / new revision).
    global STOP
    STOP = True


def run_sqs():
    import boto3
    handler = load_handler()
    queue_url = os.environ.get('QUEUE_URL') or os.environ['SQS_QUEUE_URL']
    region = os.environ.get('AWS_REGION', 'eu-central-1')
    queue_arn = 'arn:aws:sqs:{}:{}:{}'.format(region, *queue_url.rstrip('/').split('/')[-2:])
    sqs = boto3.client('sqs', region_name=region)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log(msg='runner sqs polling', queue=queue_url)
    while not STOP:
        try:
            msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20,
                                       AttributeNames=['All'], MessageAttributeNames=['All']).get('Messages', [])
        except Exception as e:
            log(msg='receive failed', error=str(e))
            time.sleep(5)
            continue
        for m in msgs:
            record = {
                'messageId': m['MessageId'], 'receiptHandle': m['ReceiptHandle'], 'body': m['Body'],
                'attributes': m.get('Attributes', {}),
                'messageAttributes': {k: {'stringValue': v.get('StringValue'), 'dataType': v.get('DataType')}
                                      for k, v in (m.get('MessageAttributes') or {}).items()},
                'md5OfBody': m.get('MD5OfBody'), 'eventSource': 'aws:sqs', 'eventSourceARN': queue_arn,
                'awsRegion': region,
            }
            started = time.time()
            try:
                handler({'Records': [record]}, Context(TIMEOUT_S))
            except Exception:
                # Lambda semantics: a failed invocation leaves the message on the queue for a retry.
                log(msg='handler failed, message left for retry', messageId=m['MessageId'], error=traceback.format_exc())
                continue
            try:
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m['ReceiptHandle'])
            except Exception as e:
                log(msg='delete failed', messageId=m['MessageId'], error=str(e))
            log(msg='job done', messageId=m['MessageId'], ms=int((time.time() - started) * 1000))
    log(msg='runner sqs stopped')


def ensure_state():
    """Azure-local queue/table (ElasticMQ, DynamoDB Local) start empty: create what the handler expects.
    ENSURE_TABLE=<name>:<hash key>  ENSURE_QUEUE=<queue url>  (both idempotent, no-ops when unset)."""
    if not (os.environ.get('ENSURE_TABLE') or os.environ.get('ENSURE_QUEUE')):
        return
    import boto3
    region = os.environ.get('AWS_REGION', 'eu-central-1')
    for attempt in range(30):
        try:
            if os.environ.get('ENSURE_TABLE'):
                name, key = os.environ['ENSURE_TABLE'].split(':')
                ddb = boto3.client('dynamodb', region_name=region)
                if name not in ddb.list_tables().get('TableNames', []):
                    ddb.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                                     AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                                     KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}])
                    log(msg='created table', table=name)
            if os.environ.get('ENSURE_QUEUE'):
                boto3.client('sqs', region_name=region).create_queue(
                    QueueName=os.environ['ENSURE_QUEUE'].rstrip('/').split('/')[-1],
                    Attributes={'VisibilityTimeout': '900'})
            return
        except Exception as e:
            if 'ResourceInUse' in str(e):
                return
            log(msg='ensure state retry', attempt=attempt, error=str(e))
            time.sleep(2)
    raise SystemExit('queue/table not reachable')


if __name__ == '__main__':
    ensure_state()
    {'http': run_http, 'sqs': run_sqs}[MODE]()
