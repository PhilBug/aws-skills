#!/usr/bin/env python3
"""CDK Custom Resource Lambda for AgentCore Gateway lifecycle management.

Handles create/update/delete of AgentCore Gateway resources via CloudFormation
Custom Resource events. Used in CDK stacks that deploy Gateway + Lambda targets.

This Lambda is invoked by CloudFormation during stack create/update/delete and
manages the AgentCore Gateway resource lifecycle through the boto3 API.

Environment Variables:
    GATEWAY_NAME: Name of the Gateway to manage
    TARGET_LAMBDA_ARN: ARN of the Lambda function to register as target
    OPENAPI_SCHEMA_S3_URI: S3 URI of the OpenAPI schema file
    GATEWAY_IAM_ROLE_ARN: IAM role ARN for Gateway to invoke Lambda
    CREDENTIAL_PROVIDER_ARN: (Optional) Credential provider ARN for OAuth targets
"""

import json
import logging
import os
import time

import boto3
import cfnresponse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

client = boto3.client("bedrock-agentcore-control")


def handler(event, context):
    """CloudFormation Custom Resource handler."""
    request_type = event["RequestType"]
    try:
        if request_type == "Create":
            result = handle_create(event, context)
        elif request_type == "Update":
            result = handle_update(event, context)
        elif request_type == "Delete":
            result = handle_delete(event, context)
        else:
            raise ValueError(f"Unknown RequestType: {request_type}")

        cfnresponse.send(event, context, cfnresponse.SUCCESS, result)
    except Exception as e:
        logger.error(f"Error: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(e)})


def handle_create(event, context):
    """Create Gateway and register Lambda target."""
    gateway_name = os.environ["GATEWAY_NAME"]
    target_lambda_arn = os.environ["TARGET_LAMBDA_ARN"]
    schema_s3_uri = os.environ["OPENAPI_SCHEMA_S3_URI"]
    gateway_role_arn = os.environ["GATEWAY_IAM_ROLE_ARN"]

    # Create Gateway
    response = client.create_gateway(
        name=gateway_name,
        protocolType="MCP",
        description=f"Gateway for {gateway_name}",
    )
    gateway_id = response["gatewayId"]
    logger.info(f"Created Gateway: {gateway_id}")

    # Wait for Gateway to be available
    wait_for_gateway_available(gateway_id)

    # Create Gateway Target (Lambda)
    target_config = {
        "lambdaTargetConfiguration": {
            "lambdaArn": target_lambda_arn,
        }
    }

    target_response = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=f"{gateway_name}-target",
        targetConfiguration=target_config,
        description="Lambda target",
    )

    return {
        "GatewayId": gateway_id,
        "TargetId": target_response.get("targetId", ""),
    }


def handle_update(event, context):
    """Update existing Gateway target configuration."""
    # For updates, delete and recreate (simplified approach)
    handle_delete(event, context)
    return handle_create(event, context)


def handle_delete(event, context):
    """Delete Gateway and all its targets."""
    physical_id = event.get("PhysicalResourceId", "")
    if not physical_id:
        return {"Status": "Nothing to delete"}

    try:
        # List and delete all targets
        targets = client.list_gateway_targets(gatewayIdentifier=physical_id)
        for target in targets.get("gatewayTargets", []):
            client.delete_gateway_target(
                gatewayIdentifier=physical_id,
                targetIdentifier=target["targetId"],
            )

        # Delete the gateway
        client.delete_gateway(gatewayIdentifier=physical_id)
        logger.info(f"Deleted Gateway: {physical_id}")
    except client.exceptions.ResourceNotFoundException:
        logger.info(f"Gateway {physical_id} already deleted")

    return {"Status": "Deleted"}


def wait_for_gateway_available(gateway_id, timeout=300, interval=10):
    """Poll until Gateway reaches AVAILABLE status."""
    elapsed = 0
    while elapsed < timeout:
        response = client.get_gateway(gatewayIdentifier=gateway_id)
        status = response.get("status", "")
        if status == "AVAILABLE":
            return
        if status in ("FAILED", "DELETED"):
            raise RuntimeError(f"Gateway {gateway_id} reached terminal status: {status}")
        logger.info(f"Gateway {gateway_id} status: {status}, waiting...")
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Gateway {gateway_id} not available after {timeout}s")
