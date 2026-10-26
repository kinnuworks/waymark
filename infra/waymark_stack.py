"""Waymark infrastructure: one Graviton4 instance behind CloudFront, plus S3, DynamoDB,
logs, a dashboard and a cost budget. Nothing here depends on the app code at synth time."""
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

UBUNTU_ARM64_AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
CLOUDFRONT_ORIGIN_FACING_PL = "com.amazonaws.global.cloudfront.origin-facing"
UV_VERSION = "0.12.9"
LOG_GROUP_NAME = "/waymark/app"


class WaymarkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo_url = self.node.try_get_context("waymark:repoUrl")
        instance_type = self.node.try_get_context("waymark:instanceType") or "c8g.large"
        az = self.node.try_get_context("waymark:availabilityZone") or "us-east-1a"
        budget_usd = float(self.node.try_get_context("waymark:monthlyBudgetUsd") or 40)
        # CloudFront needs an account that AWS has "verified" for CloudFront. Until then deploy
        # with this off: everything else is created and port 80 stays CloudFront-only.
        enable_cf = str(self.node.try_get_context("waymark:enableCloudFront")).lower() not in ("false", "0", "no")

        # ------------------------------------------------------------------ storage
        bucket = s3.Bucket(
            self,
            "Archive",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-frames", prefix="frames/", expiration=Duration.days(30)),
                s3.LifecycleRule(id="abort-mpu", abort_incomplete_multipart_upload_after=Duration.days(7)),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        table = dynamodb.Table(
            self,
            "Traces",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        log_group = logs.LogGroup(
            self,
            "AppLogs",
            log_group_name=LOG_GROUP_NAME,
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------ network
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        cf_prefix_list = ec2.PrefixList.from_lookup(
            self, "CloudFrontOriginFacing", prefix_list_name=CLOUDFRONT_ORIGIN_FACING_PL
        )

        sg = ec2.SecurityGroup(
            self,
            "OriginSg",
            vpc=vpc,
            description="Waymark origin: HTTP 80 from CloudFront origin-facing ranges only",
            allow_all_outbound=True,
        )
        sg.add_ingress_rule(
            ec2.Peer.prefix_list(cf_prefix_list.prefix_list_id),
            ec2.Port.tcp(80),
            "CloudFront origin-facing",
        )

        # Used by scripts/launch_bench_instance.sh: no inbound rules at all (SSM only).
        bench_sg = ec2.SecurityGroup(
            self,
            "BenchSg",
            vpc=vpc,
            description="Waymark temporary benchmark instances: no inbound",
            allow_all_outbound=True,
        )

        # ------------------------------------------------------------------ IAM (least privilege)
        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Waymark instance: its bucket, its table, its log group, SSM core",
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        bucket.grant_read_write(role)
        table.grant_read_write_data(role)
        log_group.grant_write(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchAgentLogs",
                actions=["logs:DescribeLogStreams", "logs:DescribeLogGroups"],
                resources=[log_group.log_group_arn, f"{log_group.log_group_arn}:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchAgentMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "CWAgent"}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchAgentDescribe",
                actions=["ec2:DescribeTags", "ec2:DescribeVolumes"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockNovaOnly",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*.amazon.nova-*",
                ],
            )
        )

        # ------------------------------------------------------------------ instance
        user_data_script = (Path(__file__).parent / "userdata" / "bootstrap.sh").read_text()
        for key, value in {
            "@@BUCKET@@": bucket.bucket_name,
            "@@TABLE@@": table.table_name,
            "@@INSTANCE_TYPE@@": instance_type,
            "@@REPO_URL@@": repo_url,
            "@@REGION@@": self.region,
            "@@LOG_GROUP@@": LOG_GROUP_NAME,
            "@@UV_VERSION@@": UV_VERSION,
        }.items():
            user_data_script = user_data_script.replace(key, value)

        subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC, availability_zones=[az])
        instance = ec2.Instance(
            self,
            "Api",
            vpc=vpc,
            vpc_subnets=subnets,
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.from_ssm_parameter(
                UBUNTU_ARM64_AMI_PARAM, os=ec2.OperatingSystemType.LINUX, cached_in_context=True
            ),
            security_group=sg,
            role=role,
            require_imdsv2=True,
            user_data=ec2.UserData.custom(user_data_script),
            user_data_causes_replacement=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        30, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True, delete_on_termination=True
                    ),
                )
            ],
        )
        instance.node.add_dependency(log_group)

        eip = ec2.CfnEIP(self, "ApiEip", domain="vpc", instance_id=instance.instance_id)
        # Stable public DNS for the EIP (us-east-1 naming): ec2-1-2-3-4.compute-1.amazonaws.com
        origin_dns = Fn.join(
            "",
            ["ec2-", Fn.join("-", Fn.split(".", eip.attr_public_ip)), ".compute-1.amazonaws.com"],
        )

        # ------------------------------------------------------------------ CloudFront
        distribution = None
        if enable_cf:
            origin = origins.HttpOrigin(
                origin_dns,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                http_port=80,
                read_timeout=Duration.seconds(60),
                keepalive_timeout=Duration.seconds(5),
            )
            distribution = cloudfront.Distribution(
                self,
                "Cdn",
                comment="Waymark",
                price_class=cloudfront.PriceClass.PRICE_CLASS_100,
                http_version=cloudfront.HttpVersion.HTTP2_AND_3,
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    compress=True,
                ),
                additional_behaviors={
                    "/assets/*": cloudfront.BehaviorOptions(
                        origin=origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                        cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                        compress=True,
                    )
                },
            )

        # ------------------------------------------------------------------ dashboard
        def ec2_metric(name: str, stat: str = "Average") -> cw.Metric:
            return cw.Metric(
                namespace="AWS/EC2",
                metric_name=name,
                dimensions_map={"InstanceId": instance.instance_id},
                statistic=stat,
                period=Duration.minutes(5),
            )

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="Waymark")
        dashboard.add_widgets(
            cw.GraphWidget(
                title="CPU %",
                left=[ec2_metric("CPUUtilization"), ec2_metric("CPUUtilization", "Maximum")],
                left_y_axis=cw.YAxisProps(min=0, max=100),
                width=8,
            ),
            cw.GraphWidget(
                title="Network bytes",
                left=[ec2_metric("NetworkIn", "Sum"), ec2_metric("NetworkOut", "Sum")],
                width=8,
            ),
            cw.GraphWidget(
                title="Memory / disk % (CW agent)",
                left=[
                    cw.Metric(
                        namespace="CWAgent",
                        metric_name="mem_used_percent",
                        dimensions_map={"InstanceId": instance.instance_id},
                        period=Duration.minutes(5),
                    ),
                    cw.MathExpression(
                        expression=f"SEARCH('{{CWAgent,InstanceId,fstype,path}} MetricName=\"disk_used_percent\" InstanceId=\"{instance.instance_id}\"', 'Average', 300)",
                        label="disk",
                        period=Duration.minutes(5),
                    ),
                ],
                left_y_axis=cw.YAxisProps(min=0, max=100),
                width=8,
            ),
        )
        if distribution is not None:
            dashboard.add_widgets(
                cw.GraphWidget(
                    title="CloudFront 5xx / 4xx error rate %",
                    left=[
                        distribution.metric5xx_error_rate(period=Duration.minutes(5)),
                        distribution.metric4xx_error_rate(period=Duration.minutes(5)),
                    ],
                    width=12,
                ),
                cw.GraphWidget(
                    title="CloudFront requests",
                    left=[distribution.metric_requests(period=Duration.minutes(5), statistic="Sum")],
                    width=12,
                ),
            )

        dashboard.add_widgets(
            cw.LogQueryWidget(
                title="App state= lines",
                log_group_names=[log_group.log_group_name],
                query_lines=[
                    "fields @timestamp, @logStream, @message",
                    "filter @message like /state=/",
                    "sort @timestamp desc",
                    "limit 100",
                ],
                width=24,
                height=8,
            )
        )

        # ------------------------------------------------------------------ budget (SNS only, no email)
        topic = sns.Topic(self, "BudgetAlerts", display_name="Waymark budget alerts")
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["SNS:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        def notification(kind: str, pct: float) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN",
                    notification_type=kind,
                    threshold=pct,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[budgets.CfnBudget.SubscriberProperty(address=topic.topic_arn, subscription_type="SNS")],
            )

        budget = budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="waymark-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=budget_usd, unit="USD"),
            ),
            notifications_with_subscribers=[notification("ACTUAL", 80), notification("FORECASTED", 100)],
        )
        budget.node.add_dependency(topic)

        # ------------------------------------------------------------------ outputs
        if distribution is not None:
            CfnOutput(self, "CloudFrontUrl", value=f"https://{distribution.distribution_domain_name}")
            CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        else:
            CfnOutput(self, "CloudFrontUrl", value="disabled (set context waymark:enableCloudFront=true once the account is verified for CloudFront)")
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "OriginDns", value=origin_dns)
        CfnOutput(self, "ElasticIp", value=eip.attr_public_ip)
        CfnOutput(self, "BudgetTopicArn", value=topic.topic_arn)
        CfnOutput(self, "InstanceProfileName", value=instance.instance.iam_instance_profile)
        CfnOutput(self, "BenchSecurityGroupId", value=bench_sg.security_group_id)
        bench_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC, availability_zones=[az]).subnet_ids
        # Empty only on the first synth pass, before the VPC context lookup has been cached.
        CfnOutput(self, "BenchSubnetId", value=bench_subnets[0] if bench_subnets else "pending-lookup")
