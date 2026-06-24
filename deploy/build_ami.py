#!/usr/bin/env python3
# deprecated: superseded by `aegisctl disk --type ami` (deploy/aegisctl.py).
"""Build and upload the Hummingbird + Hermes image to an AWS AMI via bootc-image-builder.

Internally invokes quay.io/centos-bootc/bootc-image-builder through podman.

Verified official behavior (https://github.com/osbuild/bootc-image-builder):
  - `--type ami` produces AMI output.
  - AWS auto-upload requires `--aws-ami-name`, `--aws-bucket`, and `--aws-region`
    to all be specified together ("These flags must all be specified together").
  - The bucket must already exist (the builder does not create it).
  - The account must have a vmimport service role configured.
  - Pass AWS credentials via --env-file, not as plaintext --env.
  - The /output volume is not needed for AWS upload (the image is uploaded to AWS directly).

Note (verified against official docs/community):
  - The bootc-image-builder repo is archived and merged into image-builder.
    For current usage, also check https://github.com/osbuild/image-builder.
  - Recent bootc-image-builder may not auto-pull the base image, so it is safer to
    `podman pull` the target image beforehand.

Example:
  python3 deploy/build_ami.py \\
    --image localhost/aegisos:latest \\
    --ami-name aegisos \\
    --bucket my-bootc-bucket \\
    --region us-east-1 \\
    --aws-secrets ./aws.secrets \\
    --config deploy/config.example.toml
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

BIB_IMAGE = "quay.io/centos-bootc/bootc-image-builder:latest"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build and upload an AMI via bootc-image-builder.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--image",
        default="localhost/aegisos:latest",
        help="bootc container image to convert to an AMI (must already be podman built/pulled).",
    )
    p.add_argument("--ami-name", required=True, help="AMI name to register in AWS (--aws-ami-name).")
    p.add_argument("--bucket", required=True, help="S3 bucket for intermediate storage (--aws-bucket). Must already exist.")
    p.add_argument("--region", required=True, help="AWS region to upload to (--aws-region).")
    p.add_argument(
        "--aws-secrets",
        default="aws.secrets",
        help="AWS credentials env file holding AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to a bootc-image-builder config.toml (optional). When set, mounted as /config.toml.",
    )
    p.add_argument(
        "--no-pull-base",
        action="store_true",
        help="Skip pre-pulling the target image with podman.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the podman command without running it.",
    )
    return p.parse_args()


def run(cmd: list[str], dry_run: bool) -> None:
    printable = " ".join(cmd)
    print(f"[build_ami] $ {printable}")
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def main() -> int:
    args = parse_args()

    if shutil.which("podman") is None:
        print("[build_ami] error: podman not found.", file=sys.stderr)
        return 1

    secrets_path = Path(args.aws_secrets)
    if not args.dry_run and not secrets_path.is_file():
        print(
            f"[build_ami] error: AWS credentials file not found: {secrets_path}\n"
            "  AWS_ACCESS_KEY_ID=...\n  AWS_SECRET_ACCESS_KEY=...\n"
            "  Create it in this format.",
            file=sys.stderr,
        )
        return 1

    # Pre-pull the base (target) image. Ignore failures for locally built images, where pull may fail.
    if not args.no_pull_base and not args.image.startswith("localhost/"):
        run(["sudo", "podman", "pull", args.image], args.dry_run)

    # Refresh bootc-image-builder itself with newer.
    run(["sudo", "podman", "pull", BIB_IMAGE], args.dry_run)

    cmd = [
        "sudo", "podman", "run",
        "--rm",
        "-it",
        "--privileged",
        "--pull=newer",
        "--security-opt", "label=type:unconfined_t",
        "-v", "/var/lib/containers/storage:/var/lib/containers/storage",
        "--env-file", str(secrets_path),
    ]

    if args.config:
        config_path = Path(args.config).resolve()
        if not args.dry_run and not config_path.is_file():
            print(f"[build_ami] error: config file not found: {config_path}", file=sys.stderr)
            return 1
        cmd += ["-v", f"{config_path}:/config.toml:ro"]

    cmd += [
        BIB_IMAGE,
        "--type", "ami",
        "--aws-ami-name", args.ami_name,
        "--aws-bucket", args.bucket,
        "--aws-region", args.region,
        args.image,
    ]

    try:
        run(cmd, args.dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"[build_ami] build failed (exit {exc.returncode}).", file=sys.stderr)
        return exc.returncode

    print("[build_ami] done. Check the registered AMI in the AWS console under EC2 > AMIs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
