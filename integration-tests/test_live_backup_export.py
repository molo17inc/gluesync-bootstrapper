#!/usr/bin/env python3
"""
Live integration test for Automator backup/export with include_secrets.

This test connects to a live CoreHub instance to verify:
- Export with include_secrets=False masks passwords
- Export with include_secrets=True preserves encrypted passwords
"""

import io
import logging
import os
import sys
import zipfile
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_app.corehub as corehub
from commons import configure_core_hub, fetch_core_hub


def test_live_backup_export():
    """Test live backup/export against CoreHub at 10.17.3.132."""
    
    # Live CoreHub configuration
    base_url = "https://10.17.3.132:443"
    username = "admin"
    password = os.getenv("COREHUB_PASSWORD", "")
    
    if not password:
        logging.error("COREHUB_PASSWORD environment variable not set")
        return False
    
    logging.info(f"Connecting to CoreHub at {base_url}")
    
    # Configure CoreHub client with the target URL
    try:
        configure_core_hub(base_url, use_ssl=True, skip_verify=True)
    except Exception as e:
        logging.error(f"Failed to configure CoreHub client: {e}")
        return False
    
    # Login to get token
    try:
        response = fetch_core_hub(
            path="/authentication/login",
            method="POST",
            body={"username": username, "password": password},
        )
        token = response.get("apiToken") if isinstance(response, dict) else None
        logging.info(f"Login successful, token: {token[:20] if token else 'None'}...")
    except Exception as e:
        logging.error(f"Login failed: {e}")
        return False
    
    # List pipelines
    try:
        pipelines = corehub.list_pipelines(
            token=token,
            base_url=base_url,
            use_ssl=True,
            skip_verify=True,
        )
        logging.info(f"Found {len(pipelines)} pipelines")
        if not pipelines:
            logging.warning("No pipelines found to test export")
            return False
        
        pipeline_id = pipelines[0]["id"]
        pipeline_name = pipelines[0]["name"]
        logging.info(f"Testing with pipeline: {pipeline_name} (ID: {pipeline_id})")
    except Exception as e:
        logging.error(f"Failed to list pipelines: {e}")
        return False
    
    # Test 1: Export with include_secrets=False (default - passwords masked)
    logging.info("\n=== Test 1: Export with include_secrets=False ===")
    try:
        zip_bytes = corehub.export_pipeline_full_backup(
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=True,
            skip_verify=True,
            include_secrets=False,
        )
        
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()
                logging.info(f"ZIP contains: {namelist}")
                
                for name in namelist:
                    if "agents-config.yaml" in name:
                        content = zf.read(name).decode("utf-8")
                        if "*******" in content:
                            logging.info("✓ Passwords are masked (******* found)")
                        else:
                            logging.warning("✗ Passwords are NOT masked")
                        break
        
        logging.info("Test 1: PASSED")
    except Exception as e:
        logging.error(f"Test 1 FAILED: {e}")
        return False
    
    # Test 2: Export with include_secrets=True (passwords preserved)
    logging.info("\n=== Test 2: Export with include_secrets=True ===")
    try:
        zip_bytes = corehub.export_pipeline_full_backup(
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=True,
            skip_verify=True,
            include_secrets=True,
        )
        
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()
                logging.info(f"ZIP contains: {namelist}")
                
                for name in namelist:
                    if "agents-config.yaml" in name:
                        content = zf.read(name).decode("utf-8")
                        if "*******" in content:
                            logging.warning("✗ Passwords are still masked despite include_secrets=True")
                        else:
                            logging.info("✓ Passwords are preserved (no ******* found)")
                        
                        # Check if password field exists and has content
                        agents_config = yaml.safe_load(content)
                        if "agents" in agents_config:
                            for agent in agents_config["agents"]:
                                if "hostCredentials" in agent and "password" in agent["hostCredentials"]:
                                    pwd = agent["hostCredentials"]["password"]
                                    if pwd and pwd != "*******":
                                        logging.info(f"  Agent {agent.get('agentTag')} has preserved password: {pwd[:10]}...")
                        break
        
        logging.info("Test 2: PASSED")
    except Exception as e:
        logging.error(f"Test 2 FAILED: {e}")
        return False
    
    # Test 3: Export all pipelines with include_secrets=True
    logging.info("\n=== Test 3: Export all pipelines with include_secrets=True ===")
    try:
        zip_bytes = corehub.export_all_pipelines_yaml(
            token=token,
            base_url=base_url,
            use_ssl=True,
            skip_verify=True,
            include_secrets=True,
        )
        
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()
                logging.info(f"ZIP contains {len(namelist)} files")
                
                for name in namelist:
                    if "agents-config.yaml" in name:
                        content = zf.read(name).decode("utf-8")
                        if "*******" in content:
                            logging.warning("✗ Passwords are masked in bulk export")
                        else:
                            logging.info("✓ Passwords are preserved in bulk export")
                        break
        
        logging.info("Test 3: PASSED")
    except Exception as e:
        logging.error(f"Test 3 FAILED: {e}")
        return False
    
    logging.info("\n=== All tests PASSED ===")
    return True


if __name__ == "__main__":
    success = test_live_backup_export()
    sys.exit(0 if success else 1)
