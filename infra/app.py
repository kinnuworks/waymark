#!/usr/bin/env python3
"""CDK entry point for the Waymark stack (account/region come from the AWS CLI profile)."""
import os

import aws_cdk as cdk

from waymark_stack import WaymarkStack

app = cdk.App()
WaymarkStack(
    app,
    "Waymark",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Waymark (OpenCV AI Competition 2026): Graviton4 API behind CloudFront",
)
cdk.Tags.of(app).add("project", "waymark")
app.synth()
