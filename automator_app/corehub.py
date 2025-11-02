# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
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

"""CoreHub authentication and entity execution helpers for the automator UI."""

from __future__ import annotations

import io
import logging
import os
from contextlib import redirect_stdout
from typing import Any, Callable, Dict, Optional

from commons import configure_core_hub, set_scheduling_enabled, fetch_core_hub
from create_all_entities import (
    CREATE_TABLE_IF_NOT_EXISTS,
    main as create_entities_main,
    set_create_table_if_not_exists,
)

logger = logging.getLogger(__name__)


def authenticate(
    *,
    base_url: str,
    username: str,
    password: str,
    use_ssl: Optional[bool] = None,
    skip_verify: Optional[bool] = None,
) -> str:
    """Authenticate against CoreHub and return an API token."""

    client = configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)
    logger.info("Configured CoreHub client for authentication")

    response = client.request(
        "/authentication/login",
        method="POST",
        body={"username": username, "password": password},
    )

    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise RuntimeError("Authentication failed: no token returned")

    logger.info("Authentication successful for user %s", username)
    return token


def run_create_entities(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    source_schema: str,
    target_schema: str,
    source_type: str,
    target_type: str,
    yaml_file: str,
    skip_errors: bool,
    chunk_size: int,
    enable_scheduling: bool,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Invoke create_all_entities.main and capture output."""

    set_scheduling_enabled(enable_scheduling)
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    prev_env_create_tables = os.environ.get('CREATE_TABLE_IF_NOT_EXISTS')
    prev_flag_create_tables = CREATE_TABLE_IF_NOT_EXISTS
    set_create_table_if_not_exists(create_tables)
    os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = 'true' if create_tables else 'false'

    handler = None
    root_logger = logging.getLogger()
    if log_callback is not None:
        class CallbackHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
                message = record.getMessage()
                if message:
                    log_callback(message)

        handler = CallbackHandler(level=logging.INFO)
        root_logger.addHandler(handler)

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            create_entities_main(
                pipeline_id,
                source_schema,
                target_schema,
                source_type,
                target_type,
                yaml_file,
                token,
                skip_errors,
                chunk_size,
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("create_all_entities execution failed")
        if handler:
            root_logger.removeHandler(handler)
        return {
            "success": False,
            "logs": buffer.getvalue().splitlines(),
            "error": str(exc),
        }
    finally:
        if handler:
            root_logger.removeHandler(handler)
        if prev_env_create_tables is not None:
            os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = prev_env_create_tables
        else:
            os.environ.pop('CREATE_TABLE_IF_NOT_EXISTS', None)
        set_create_table_if_not_exists(prev_flag_create_tables)

    if log_callback is not None:
        for line in buffer.getvalue().splitlines():
            log_callback(line)
    return {
        "success": True,
        "logs": buffer.getvalue().splitlines(),
    }


def validate_token(token: str) -> bool:
    try:
        response = fetch_core_hub("/pipelines", token=token)
        return isinstance(response, list)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Token validation failed")
        return False
