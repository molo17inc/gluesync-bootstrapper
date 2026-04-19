# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

import logging

logger = logging.getLogger(__name__)

class TransactionsAuditClient:
    """Client for managing Transactions Auditing via CoreHub API."""

    def __init__(self, core_hub_client):
        self.client = core_hub_client

    def get_configuration(self, pipeline_id, token):
        """Get the transactions audit configuration for a pipeline."""
        return self.client.request(
            f"/pipelines/{pipeline_id}/transactions-audit",
            method='GET',
            token=token
        )

    def upsert_configuration(self, pipeline_id, token, enabled, target_agent_id=None, deep_trace=False, confirm_table_creation=True):
        """Update the transactions audit configuration for a pipeline."""
        body = {
            "enabled": enabled,
            "targetAgentId": target_agent_id,
            "deepTraceAudit": deep_trace,
            "confirmTableCreation": confirm_table_creation
        }
        return self.client.request(
            f"/pipelines/{pipeline_id}/transactions-audit",
            method='PUT',
            token=token,
            body=body
        )

    def configure_from_yaml(self, pipeline_id, token, yaml_config):
        """Configure transactions auditing based on YAML configuration."""
        if not yaml_config or not isinstance(yaml_config, dict):
            return

        audit_config = yaml_config.get('transactionsAudit')
        if not audit_config:
            return

        enabled = audit_config.get('enabled', False)
        target_agent_id = audit_config.get('targetAgentId')
        deep_trace = audit_config.get('deepTrace', False)

        if enabled and not target_agent_id:
            logger.warning(f"Transactions auditing enabled for pipeline {pipeline_id} but no targetAgentId provided. Skipping.")
            return

        logger.info(f"Configuring transactions auditing for pipeline {pipeline_id}: enabled={enabled}, target={target_agent_id}, deepTrace={deep_trace}")
        try:
            self.upsert_configuration(
                pipeline_id=pipeline_id,
                token=token,
                enabled=enabled,
                target_agent_id=target_agent_id,
                deep_trace=deep_trace,
                confirm_table_creation=True
            )
            logger.info(f"Successfully configured transactions auditing for pipeline {pipeline_id}")
        except Exception as e:
            logger.error(f"Failed to configure transactions auditing for pipeline {pipeline_id}: {e}")
            raise
