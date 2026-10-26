# Benchmarking Waymark on the COOL AMI (and x86 for comparison)

COOL ("Cloud Optimized OpenCV Library") is an AWS Marketplace AMI with an OpenCV 5 build
tuned for Graviton (KleidiCV). Marketplace listing: `prodview-fdvbfiewzuehs`
("Cloud Optimized OpenCV For AWS Graviton4"), product code `aajkmdd4qo3r7yhqg61a7aah9`.
It is Ubuntu-based (login user `ubuntu`), supports c8g / m8g / r8g, and installs to `/opt/cool`:

- Python venvs with COOL's `cv2`: `/opt/cool/venvs/python_3.10|3.11|3.12`
- Or with system Python: `LD_LIBRARY_PATH=/opt/cool/cpp_sdk/lib` and
  `PYTHONPATH=/opt/cool/python_3.12/site-packages/`
- C++ SDK: `/opt/cool/cpp_sdk` (`OpenCV_DIR=/opt/cool/cpp_sdk/lib/cmake/opencv4`)

The listing charges a small software fee per instance-hour on top of EC2
(about $0.02-0.04/h on the large sizes). The temporary instance is terminated when the run ends.

## 1. Subscribe (one time, in the console)

Accept the subscription yourself at
https://aws.amazon.com/marketplace/pp/prodview-fdvbfiewzuehs → **Continue to Subscribe** → **Accept terms**.
Until you do, `run-instances` fails with `OptInRequired`.

Find the AMI id (the script does this for you when you pass `cool`):

```bash
aws ec2 describe-images --region us-east-1 --owners aws-marketplace \
  --filters Name=product-code,Values=aajkmdd4qo3r7yhqg61a7aah9 \
  --query 'sort_by(Images,&CreationDate)[].[ImageId,Name,CreationDate]' --output table
```

When this was written (2026-10-27) the newest was `ami-033e481a24f94c8cb`
(`Graviton5-COOL-v3-...`, arm64), with `ami-01db31139bc5615d8` (`COOL-Graviton4-v2-...`) before it.

## 2. Run the same benchmark in three places

The Waymark stack must be deployed first: the script reuses its instance profile, its
no-inbound `BenchSg` security group, and its subnet. Every run launches a fresh SSM-managed
instance with no SSH key and no open ports, waits for SSM, clones the repo, runs
`scripts/fetch_models.sh`, runs your command from the repo root as root, prints the output,
and terminates the instance. A `trap` on EXIT/INT/TERM does the terminate, and a
`shutdown -h +120` on the box (with shutdown behaviour = terminate) backs it up.

```bash
BENCH='uv run --project server python -m waymark.bench'      # whatever the bench entry point is

# a) the always-on Waymark box (Ubuntu 24.04 + pip opencv, c8g.large)
scripts/bench_remote.sh "$BENCH"

# b) COOL on the same instance type. PYTHONPATH puts COOL's cv2 ahead of the pip wheel.
scripts/launch_bench_instance.sh cool c8g.large \
  'export LD_LIBRARY_PATH=/opt/cool/cpp_sdk/lib PYTHONPATH=/opt/cool/python_3.12/site-packages/;
   uv run --python 3.12 --project server python -c "import cv2; print(cv2.__version__, cv2.__file__)" &&
   uv run --python 3.12 --project server python -m waymark.bench'

# c) x86 comparison: stock Ubuntu 24.04 amd64 on c7i.large
scripts/launch_bench_instance.sh ubuntu-amd64 c7i.large "$BENCH"

# (optional) stock Ubuntu arm64 on a fresh c8g.large, a clean baseline for (b)
scripts/launch_bench_instance.sh ubuntu-arm64 c8g.large "$BENCH"
```

Check that the COOL run prints a `cv2.__file__` under `/opt/cool`. If it shows a
`site-packages/cv2` from the wheel, the pip OpenCV is still shadowing COOL and the comparison
is meaningless. If COOL's cv2 fails to import inside the uv venv (for example a NumPy ABI
mismatch), use COOL's own venv instead:
`source /opt/cool/venvs/python_3.12/bin/activate && pip install -e server && pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python; python -m waymark.bench`
and check `cv2.__file__` again.

Inside the command, `WAYMARK_INSTANCE`, `AWS_REGION`, `WAYMARK_BUCKET`, `WAYMARK_TABLE` and
`IS_COOL` (0/1) are exported. Use them to label results, or copy result files to
`s3://$WAYMARK_BUCKET/bench/` (the instance profile can write to that bucket).

Knobs: `WAYMARK_REF=<branch|sha>`, `WAYMARK_BENCH_TIMEOUT=<s>` (default 3600),
`WAYMARK_BENCH_MAX_MIN=<min>` (self-destruct, default 120), `WAYMARK_ROOT_GB`.
SSM returns at most ~24 KB of stdout, so print a summary and put raw data in S3.

## 3. Make sure nothing is left running

```bash
aws ec2 describe-instances --filters Name=tag:purpose,Values=bench Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime]' --output table
```
