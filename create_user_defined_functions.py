# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
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

import base64
import os
import json
from enum import Enum

from annotated_types import T
import requests
import urllib3
import argparse

from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete
from utils.core_hub_client import CoreHubClient
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, create_entity_schedules, create_pipeline_schedules, map_data_type, load_yaml_config, process_filter_clauses
from pathlib import Path, PosixPath
from pydantic import BaseModel, ConfigDict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
CHRONOS_URL = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')
ENTITY_START_TIMEOUT = float(os.getenv('ENTITY_START_TIMEOUT', '1'))
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'
UDF_PATH = os.getenv('UDF_PATH','')

UDF_CLASS_FILENAME = "UserDefinedFunctionTemplate"

# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

class UdfFunctionType(str, Enum):
    java = 'Java'
    kotlin = 'Kotlin'
    # python = 'python'
    # javascript = 'javascript'
    # ruby = 'ruby'

    def extension(self):
        if self == UdfFunctionType.java:
            return ".java"
        elif self == UdfFunctionType.kotlin:
            return ".kt"
        # elif self == UdfFunctionType.python:
        #     return ".py"
        # elif self == UdfFunctionType.javascript:
        #     return ".js"
        # elif self == UdfFunctionType.rust:
        #     return ".rs"
        # elif self == UdfFunctionType.go:
        #     return ".go"
        # elif self == UdfFunctionType.r:
        #     return ".r"
        # elif self == UdfFunctionType.ruby:
        #     return ".rb"
        # elif self == UdfFunctionType.scala:
        #     return ".scala"
        else:
            raise NotImplementedError("not implemented")

class UdfFunctionCompileRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    code: str
    type: UdfFunctionType
    udfName: str

def get_udf_function_for_table(table_name: str, udf: list[dict]) -> dict:
    return next((item for item in udf if item.get("name") == table_name), {})

def find_udf_definition_in_path(udf_name: str, udf_type: UdfFunctionType) -> PosixPath:
    filename = f"{udf_name}{udf_type.extension()}"
    path_location = Path(UDF_PATH)
    file_path = next((p for p in path_location.rglob(filename)), None)
    return file_path

def read_file(filepath):
    try:
        with open(filepath, "r", encoding="UTF-8") as f:
            content = f.read()
    except Exception as e:
        logger.exception(e)
        raise
    else:
        return content

def test_udf_function(pipeline_id: str, token: str, udf_compile_request: UdfFunctionCompileRequest) -> str:
    try:
        logger.info(f"Testing UDF function for entity {udf_compile_request.udfName} (type: {udf_compile_request.type})")
        logger.debug(f"Test UDF function request: {udf_compile_request.model_dump()}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/mapping-functions/test-mapping-function",
            method="POST",
            token=token,
            body=udf_compile_request.model_dump()
        )
        logger.info(f"Successfully tested UDF function for entity {udf_compile_request.udfName}")
        return response
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        logger.error(f"Failed to compile UDF function for entity {udf_compile_request.udfName}: {error_msg}")
        raise

def compile_udf_function(pipeline_id: str, token: str, udf_compile_request: UdfFunctionCompileRequest) -> str:
    try:
        logger.info(f"Compiling UDF function for entity {udf_compile_request.udfName} (type: {udf_compile_request.type})")
        logger.debug(f"Compile UDF function request: {udf_compile_request.model_dump()}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/mapping-functions/compile-mapping-function",
            method="POST",
            token=token,
            body=udf_compile_request.model_dump()
        )
        logger.info(f"Successfully compiled UDF function for entity {udf_compile_request.udfName}")
        return response
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        logger.error(f"Failed to compile UDF function for entity {udf_compile_request.udfName}: {error_msg}")
        raise

def check_and_compile_udf_function(table_name: str, udf_definition: dict, pipeline_id: str, token: str):
    udf_name = udf_definition.get("name")
    udf_type = UdfFunctionType(udf_definition.get("type"))
    logger.info(f"Processing UDF '{udf_name}' for table {table_name} (type: {udf_type})...")
    
    file_path = find_udf_definition_in_path(udf_name, udf_type)
    if file_path:
        logger.info(f"Found UDF file at: {file_path}")
        try:
            file_data = read_file(file_path)
            logger.debug(f"Read {len(file_data)} characters from UDF file")
            b64_file_data = base64.b64encode(file_data.encode())
            udf_compile_request = UdfFunctionCompileRequest(code=b64_file_data, type=udf_type, udfName=udf_name)
            compile_udf_function(pipeline_id=pipeline_id, token=token, udf_compile_request=udf_compile_request)
            logger.info(f"Successfully processed UDF '{udf_name}' for table {table_name}")

            # TODO: enable UDF testing by following this body mock
            #             {
            #    "data":{
            #       "ID":721979669,
            #       "STREET":"ZEAnKAL3Dl",
            #       "CITY":"C9OLgoQaaj",
            #       "STREET_NUMBER":269871002,
            #       "STREET_SUFFIX":"yca0cSk5Or",
            #       "COUNTRY":"XzAugDTWjJ",
            #       "POSTAL_CODE":"NTLE3AUDvD",
            #       "NOTES":"OVwYNmxi2t"
            #    },
            #    "operation":"Insert",
            #    "type":"Java",
            #    "udfName":"UDF_fd9cdaf2f1784ef7bb59438578275acc",
            #    "targetColumns":[
            #       {
            #          "name":"ID",
            #          "type":"INT",
            #          "isPrimaryKey":true
            #       },
            #       {
            #          "name":"STREET",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"CITY",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"STREET_NUMBER",
            #          "type":"INT",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"STREET_SUFFIX",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"COUNTRY",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"POSTAL_CODE",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       },
            #       {
            #          "name":"NOTES",
            #          "type":"STRING",
            #          "isPrimaryKey":false
            #       }
            #    ]
            # }
            # logger.info(f"Testing UDF '{udf_name}' for table {table_name}...")
            # test_udf_function(pipeline_id=pipeline_id, token=token, udf_compile_request=udf_compile_request)
            # logger.info(f"Successfully tested UDF '{udf_name}' for table {table_name}")

        except Exception as e:
            logger.error(f"Failed to process UDF '{udf_name}' for table {table_name}: {str(e)}")
            raise
    else:
        expected_filename = f"{udf_name}{udf_type.extension()}"
        error_msg = f"Missing UDF file for '{udf_name}' (table: {table_name}, type: {udf_type}). Expected filename: {expected_filename} in directory: {UDF_PATH or 'current directory'}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

def handle_udf_function_definition(table_name, pipeline_id, udf, token):
    logger.info(f"Checking UDF definitions for table {table_name}")
    logger.debug(f"Available UDF list: {udf}")
    
    # Process each UDF definition in the list
    for udf_definition in udf:
        if udf_definition:
            logger.info(f"Processing UDF definition: {udf_definition}")
            try:
                check_and_compile_udf_function(table_name, udf_definition, pipeline_id, token)
            except Exception as e:
                logger.error(f"Failed to process UDF definition {udf_definition} for table {table_name}: {str(e)}")
                raise
        else:
            logger.warning(f"Empty UDF definition found for table {table_name}")
    
    if not udf:
        logger.debug(f"No UDF definitions found for table {table_name}")

def create_user_defined_functions(token, pipeline_id, source_schema, target_schema, tables, source_agent_id,
                                  target_agent_id,
                                  source_type, target_type, yaml_config, skip_errors=True, chunk_size=50):
    """Create entities for the pipeline."""
    if not yaml_config:
        yaml_config = {}

    entities = []

    # Get node info for data type mapping
    source_node_info = get_node_info(token, pipeline_id, source_agent_id)
    target_node_info = get_node_info(token, pipeline_id, target_agent_id)

    print("Source Node Info:")
    print(json.dumps(source_node_info, indent=2))
    print("Target Node Info:")
    print(json.dumps(target_node_info, indent=2))

    print(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")

    schema_config = yaml_config.get(source_schema, {})
    print(f"Schema config for {source_schema}: {json.dumps(schema_config, indent=2)}")

    yaml_target_schema = schema_config.get('target', target_schema)
    whitelist = schema_config.get('tables', {}).get('whitelist', [])
    blacklist = schema_config.get('tables', {}).get('blacklist', [])
    # Handle empty custom tables attribute - convert None to empty dict
    tables_config = schema_config.get('tables', {})
    custom_tables = tables_config.get('custom', {})
    if custom_tables is None:
        custom_tables = {}
        print("Warning: 'custom' attribute is present but empty in YAML. Converting to empty dict.")

    # Get schema-level custom properties
    schema_custom_properties = schema_config.get('customProperties', {})
    global_source_custom_properties = schema_custom_properties.get('source', {})
    global_target_custom_properties = schema_custom_properties.get('target', {})

    print(f"Target schema: {yaml_target_schema}")
    print(f"Whitelist: {whitelist}")
    print(f"Blacklist: {blacklist}")
    print(f"Custom tables: {custom_tables}")
    print(f"Global source custom properties: {global_source_custom_properties}")
    print(f"Global target custom properties: {global_target_custom_properties}")

    for table in tables:
        if isinstance(table, str):
            table_name = table
        else:
            table_name = table.get("name")

        if not table_name:
            print(f"Warning: Table without name encountered. Skipping.")
            continue

        if (table_name.startswith("sys") or
                (blacklist and table_name in blacklist) or
                (whitelist and table_name not in whitelist)):
            print(f"Skipping table: {table_name}")
            continue

        # Extract UDFs from table-level custom properties
        table_config = custom_tables.get(table_name, {})
        table_custom_properties = table_config.get('customProperties', {})
        target_custom_properties = table_custom_properties.get('target', {})
        table_udfs = target_custom_properties.get('udf', [])
        
        if table_udfs:
            print(f"Found UDFs for table {table_name}: {table_udfs}")
            handle_udf_function_definition(table_name, pipeline_id, table_udfs, token)
        else:
            print(f"No UDFs defined for table {table_name}")

def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=True,
         chunk_size=50):
    """
    Main function to create entities for a pipeline
    """
    logger.info(f"Starting entity creation for pipeline {pipeline_id}")
    try:
        yaml_config = load_yaml_config(yaml_file) if yaml_file else None

        # Get pipeline configuration
        pipeline_config = get_pipeline_config(token, pipeline_id)

        # Get agents information
        agents = get_pipeline_agents(token, pipeline_id)

        if not agents or len(agents) != 2:
            error_msg = f"Expected 2 agents, found {len(agents) if agents else 0}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Identify source and target agents based on type
        source_agent = next((agent for agent in agents if agent['agentType'] == 'SOURCE'), None)
        target_agent = next((agent for agent in agents if agent['agentType'] == 'TARGET'), None)

        if not source_agent or not target_agent:
            error_msg = f"Could not find required agents. Source ({source_type}): {source_agent}, Target ({target_type}): {target_agent}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Get tables from source agent
        tables = get_agent_tables(token, pipeline_id, source_agent['agentId'], source_schema)

        if not tables:
            error_msg = f"No tables found in schema {source_schema}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Create entities
        create_user_defined_functions(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent['agentId'], target_agent['agentId'],
            source_type, target_type, yaml_config, skip_errors, chunk_size
        )

    except Exception as e:
        log_failure(logger, f"Error: {str(e)}")
        lockfile_failure()
        if not skip_errors:
            raise
        logger.warning("Skipping error due to skip_errors=True")
        return

    # Log successful completion
    log_success(logger, f"Entity creation completed successfully for pipeline {pipeline_id}")
    lockfile_complete()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gluesync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
    parser.add_argument('--chunk-size', type=int, default=50,
                        help="Number of entities to process in each chunk (default: 50)")
    parser.add_argument('--skip-errors', action='store_true', help="Continue execution even if errors occur")
    parser.add_argument('--target-schema', required=True, help="Target schema name")
    parser.add_argument('--source-type', required=True, help="Source agent type")
    parser.add_argument('--target-type', required=True, help="Target agent type")
    parser.add_argument('--yaml-file', help="YAML configuration file path")
    parser.add_argument('--token', required=True, help="Authentication token")
    parser.add_argument('--enable-scheduling', action='store_true',
                        help="Enable creation of schedules from YAML config")

    args = parser.parse_args()

    main(
        args.pipeline,
        args.source_schema,
        args.target_schema,
        args.source_type,
        args.target_type,
        args.yaml_file,
        args.token,
        args.skip_errors,
        args.chunk_size
    )
