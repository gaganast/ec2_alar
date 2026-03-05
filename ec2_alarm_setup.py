import boto3
from datetime import datetime, timedelta, timezone
import time
import argparse
import logging

# To use the Script pass: "python import_boto3.py instance_id

REGION = "ap-south-1"
LAMBDA_NAME = "ec2-alarm-logger-RPA4"
SSM_TIMEOUT = 180

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

ec2 = boto3.client("ec2", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

LAMBDA_ARN = None


# ---------------- USER CHOICE ----------------

def ask_alarm_type():
    print("\nWhich alarm do you want to create?")
    print("1. Memory alarm")
    print("2. Status check alarm")
    print("3. All alarms")
    print("4. CPU alarm")
    return input("Enter choice (1/2/3/4): ").strip()


# ---------------- Lambda lookup ----------------

def ensure_lambda():
    global LAMBDA_ARN
    LAMBDA_ARN = lambda_client.get_function(
        FunctionName=LAMBDA_NAME
    )["Configuration"]["FunctionArn"]
    logger.info(f"Using Lambda: {LAMBDA_NAME}")


# ---------------- SSM helpers ----------------

def wait_for_ssm(command_id, instance_id):
    start = time.time()
    while time.time() - start < SSM_TIMEOUT:
        try:
            resp = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            if resp["Status"] == "Success":
                return True, resp.get("StandardOutputContent", "")
            if resp["Status"] in ["Failed", "TimedOut", "Cancelled"]:
                return False, resp.get("StandardErrorContent", "")
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return False, "Timeout"


def run_ssm(instance_id, commands):
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
    )
    return wait_for_ssm(resp["Command"]["CommandId"], instance_id)


# ---------------- CWAgent ----------------

def cwagent_metrics_visible(instance_id):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=10)

    resp = cw.get_metric_statistics(
        Namespace="CWAgent",
        MetricName="mem_used_percent",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=300,
        Statistics=["Average"],
    )
    return len(resp["Datapoints"]) > 0


def install_agent(instance_id):
    logger.info(f"Installing CloudWatch Agent on {instance_id}")
    cmds = [
        "wget -q https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb",
        "sudo dpkg -i -E ./amazon-cloudwatch-agent.deb",
    ]
    ok, out = run_ssm(instance_id, cmds)

    if not ok:
        logger.error(f"Agent installation failed for {instance_id}: {out}")
        return False

    return True


def ensure_agent_config(instance_id):
    logger.info(f"Configuring CloudWatch Agent on {instance_id}")

    cmds = [
        "sudo mkdir -p /opt/aws/amazon-cloudwatch-agent/etc",
        """cat <<'EOF' | sudo tee /opt/aws/amazon-cloudwatch-agent/etc/config.json
{
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": {
      "InstanceId": "${aws:InstanceId}"
    },
    "metrics_collected": {
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      }
    }
  }
}
EOF""",
        "sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl "
        "-a fetch-config -m ec2 -s "
        "-c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json",
        "systemctl is-active amazon-cloudwatch-agent"
    ]

    ok, out = run_ssm(instance_id, cmds)

    if not ok or "active" not in out:
        logger.error(f"Agent config failed for {instance_id}: {out}")
        return False

    logger.info("CloudWatch Agent running")
    return True


# ---------------- Alarm creation ----------------

def create_memory_alarm(instance_id, instance_name):
    safe = instance_name.replace(" ", "-")
    try:
        cw.put_metric_alarm(
            AlarmName=f"EC2-Memory-High-{instance_id}-{safe}",
            AlarmDescription=f"Memory usage above 90% on {instance_name}",
            Namespace="CWAgent",
            MetricName="mem_used_percent",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=90,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            AlarmActions=[LAMBDA_ARN],
            TreatMissingData="notBreaching"
        )
        logger.info(f"Memory alarm created for {instance_name}")
    except Exception as e:
        logger.error(f"Failed to create memory alarm: {e}")


def create_status_check_alarm(instance_id, instance_name):
    safe = instance_name.replace(" ", "-")
    try:
        cw.put_metric_alarm(
            AlarmName=f"EC2-StatusCheck-{instance_id}-{safe}",
            AlarmDescription=f"EC2 status check failed on {instance_name}",
            Namespace="AWS/EC2",
            MetricName="StatusCheckFailed",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            Statistic="Maximum",
            Period=60,
            EvaluationPeriods=2,
            Threshold=1,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            AlarmActions=[LAMBDA_ARN],
            TreatMissingData="notBreaching"
        )
        logger.info(f"Status check alarm created for {instance_name}")
    except Exception as e:
        logger.error(f"Failed to create status alarm: {e}")


def create_cpu_alarm(instance_id, instance_name):
    safe = instance_name.replace(" ", "-")
    try:
        cw.put_metric_alarm(
            AlarmName=f"EC2-CPU-High-{instance_id}-{safe}",
            AlarmDescription=f"CPU utilization above 90% for 5 minutes on {instance_name}",
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=90,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            AlarmActions=[LAMBDA_ARN],
            TreatMissingData="notBreaching"
        )
        logger.info(f"CPU alarm created for {instance_name}")
    except Exception as e:
        logger.error(f"Failed to create CPU alarm: {e}")


# ---------------- EC2 ----------------

def get_instances(ids):
    resp = ec2.describe_instances(InstanceIds=ids)
    result = []
    for r in resp["Reservations"]:
        for i in r["Instances"]:
            name = "Unnamed"
            for t in i.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            result.append({"id": i["InstanceId"], "name": name})
    return result


# ---------------- MAIN ----------------

def main():
    ensure_lambda()
    alarm_choice = ask_alarm_type()

    parser = argparse.ArgumentParser()
    parser.add_argument("instances", nargs="+")
    args = parser.parse_args()

    try:
        instances = get_instances(args.instances)
        if not instances:
            logger.error("No instances found.")
            return
    except Exception as e:
        logger.error(f"Failed to retrieve instances: {e}")
        return

    for inst in instances:
        iid = inst["id"]
        name = inst["name"]

        logger.info(f"Processing {name} ({iid})")

        if alarm_choice in ["2", "3"]:
            create_status_check_alarm(iid, name)

        if alarm_choice in ["4", "3"]:
            create_cpu_alarm(iid, name)

        if alarm_choice in ["1", "3"]:
            if cwagent_metrics_visible(iid):
                create_memory_alarm(iid, name)
            else:
                if install_agent(iid) and ensure_agent_config(iid):
                    metrics_appeared = False
                    for _ in range(10):
                        if cwagent_metrics_visible(iid):
                            create_memory_alarm(iid, name)
                            metrics_appeared = True
                            break
                        time.sleep(30)

                    if not metrics_appeared:
                        logger.warning(f"Metrics not visible for {iid}, skipping memory alarm.")
                else:
                    logger.warning(f"Agent setup failed for {iid}, skipping memory alarm.")


if __name__ == "__main__":
    main()

