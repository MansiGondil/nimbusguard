"""Cloud API collectors.

AWS CloudWatch is implemented (requires ``pip install boto3`` and standard
AWS credentials). Azure Monitor and VMware vCenter follow the same
``CloudCollector`` interface and are on the roadmap.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.collectors.base import Sample

log = logging.getLogger(__name__)


class CloudCollector:
    async def collect(self, device_id: str, resource: dict) -> Sample | None:  # pragma: no cover
        raise NotImplementedError


class CloudWatchCollector(CloudCollector):
    """Maps EC2 CloudWatch metrics onto NimbusGuard's metric catalogue."""

    METRICS = {
        "cpu": ("CPUUtilization", "Average", 1.0),
        "traffic_in": ("NetworkIn", "Sum", 8 / 1e6 / 300),  # bytes per 5 min -> Mbps
        "traffic_out": ("NetworkOut", "Sum", 8 / 1e6 / 300),
    }

    def __init__(self, region: str = "us-east-1", client=None) -> None:
        if client is None:
            import boto3  # optional dependency

            client = boto3.client("cloudwatch", region_name=region)
        self.client = client

    async def collect(self, device_id: str, resource: dict) -> Sample | None:
        import asyncio

        instance_id = resource["instance_id"]
        end = datetime.now(UTC)
        queries = [
            {
                "Id": key,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": name,
                        "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                    },
                    "Period": 300,
                    "Stat": stat,
                },
            }
            for key, (name, stat, _) in self.METRICS.items()
        ]
        try:
            resp = await asyncio.to_thread(
                self.client.get_metric_data,
                MetricDataQueries=queries,
                StartTime=end - timedelta(minutes=15),
                EndTime=end,
            )
        except Exception as exc:
            log.warning("CloudWatch collection for %s failed: %s", instance_id, exc)
            return None
        values = {}
        for result in resp.get("MetricDataResults", []):
            if result.get("Values"):
                values[result["Id"]] = round(result["Values"][0] * self.METRICS[result["Id"]][2], 3)
        return Sample(device_id, end, values) if values else None
