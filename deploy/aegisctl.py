#!/usr/bin/env python3
"""aegisctl - unified deployment CLI for AegisOS.

AegisOS = Fedora Hummingbird (bootc image-mode OS) + Hermes Agent combined image.
"Aegis" refers to an immutable OS that an AI cannot tamper with.

Consolidates the scattered deployment scripts (the former deploy/build_ami.py) into a
single CLI. Uses only the Python standard library (zero external dependencies).

The only container runtime is podman (docker unsupported, for simplicity). See the
podman-only rationale comment below for details. Every subcommand checks for podman
(and aws when needed) via shutil.which on entry and exits immediately if missing.

Subcommands:
  build   Build a container image from the Containerfile (sudo podman build)
  push    Push a container image to a registry (ECR/private)
  disk    Create disk images (ami|qcow2|raw, bootc-image-builder)
  all     Run build -> push -> disk in sequence (pipeline)

Global options:
  --dry-run   Print the podman/aws commands without running them.

Examples:
  # 1) Build a container image
  python3 deploy/aegisctl.py build --tag localhost/aegisos:latest

  # 2) Push to ECR (create the repo if missing)
  python3 deploy/aegisctl.py push --registry ecr \\
      --account-id 123456789012 --region us-east-1 --repo aegisos --create-repo \\
      --image localhost/aegisos:latest

  # 3) Create local qcow2 + raw disks
  python3 deploy/aegisctl.py disk --type qcow2,raw --image localhost/aegisos:latest

  # 4) Build an AMI and upload to AWS
  python3 deploy/aegisctl.py disk --type ami \\
      --aws-ami-name aegisos --aws-bucket my-bootc-bucket --aws-region us-east-1 \\
      --env-file ./aws.secrets --image localhost/aegisos:latest

  # 5) Full pipeline
  python3 deploy/aegisctl.py all --tag localhost/aegisos:latest \\
      --registry ecr --account-id 123456789012 --region us-east-1 --repo aegisos \\
      --type qcow2
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# aegisctl uses only podman (docker unsupported). Reasons:
#   1) bootc-image-builder's disk build requires rootful podman
#      ("bootc-image-builder requires rootful Podman to build images. Running it in
#        rootless mode causes the build to fail.")
#   2) When using a locally built image, podman local storage
#      (/var/lib/containers/storage) must be mounted directly ("If you are using a
#      container image built locally, you must include the
#      -v /var/lib/containers/storage:/var/lib/containers/storage argument.").
#      docker uses a different storage backend, so this path does not work.
#   No docker branch is kept, for simplicity.
#   Reference: https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_image_mode_for_rhel_to_build_deploy_and_manage_operating_systems/creating-bootc-compatible-base-disk-images-by-using-bootc-image-builder

# Disk builder container image (current standard). Used by the disk subcommand.
# Reference: https://github.com/osbuild/bootc-image-builder (podman run example in the README)
#
# On RHEL 11, bootc-image-builder will be replaced by image-builder (official deprecation notice).
#   - https://osbuild.org/docs/bootc/deprecation-notice/
# At that point, pointing --builder-image at the image-builder container allows switching without code changes.
# However, image-builder CLI compatibility for --type/--aws-* is not yet verified, so recheck at switch time
# (for now we only factor out the builder image reference and keep no image-builder branching logic).
DEFAULT_BUILDER_IMAGE = "quay.io/centos-bootc/bootc-image-builder:latest"

# Env var to override the builder image. Priority: --builder-image flag > env var > default constant.
BUILDER_IMAGE_ENV = "AEGISCTL_BUILDER_IMAGE"

# Default image tag, following the project name AegisOS.
DEFAULT_TAG = "localhost/aegisos:latest"

# This script lives in deploy/. Project root = the parent of deploy.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

VALID_DISK_TYPES = ("ami", "qcow2", "raw")


# --------------------------------------------------------------------------- #
# Common utilities
# --------------------------------------------------------------------------- #
def run(cmd: list[str], dry_run: bool) -> None:
    """Print the command (always) and run it (unless dry-run)."""
    print(f"[aegisctl] $ {' '.join(cmd)}")
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def require_tool(name: str, dry_run: bool = False) -> None:
    """Check that a required external tool exists via shutil.which.

    A dry-run only prints commands (no real execution), so it passes even if the tool is missing.
    """
    if dry_run:
        return
    if shutil.which(name) is None:
        print(f"[aegisctl] error: '{name}' not found on PATH. Install it and try again.",
              file=sys.stderr)
        raise SystemExit(1)


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    """Print an error message and exit."""
    print(f"[aegisctl] error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def do_build(tag: str, file: str | None, context: str | None,
             install_browser: bool, dry_run: bool) -> str:
    """Build a container image from the Containerfile. Returns the built tag.

    Reference: a bootc image is an ordinary OCI image, so build it with `podman build`.
      - https://bootc.dev/bootc/building/guidance.html
    """
    require_tool("podman", dry_run)

    # --file default: the Containerfile at the project root.
    containerfile = Path(file).resolve() if file else (PROJECT_ROOT / "Containerfile")
    # --context default: the project root.
    build_context = Path(context).resolve() if context else PROJECT_ROOT

    if not dry_run and not containerfile.is_file():
        fail(f"Containerfile not found: {containerfile}")
    if not dry_run and not build_context.is_dir():
        fail(f"build context directory not found: {build_context}")

    cmd = ["sudo", "podman", "build",
           "--file", str(containerfile),
           "--tag", tag]
    # Toggle the Containerfile's ARG INSTALL_BROWSER.
    cmd += ["--build-arg", f"INSTALL_BROWSER={'true' if install_browser else 'false'}"]
    cmd.append(str(build_context))

    run(cmd, dry_run)
    print(f"[aegisctl] build done: {tag}")
    return tag


def cmd_build(args: argparse.Namespace) -> int:
    do_build(args.tag, args.file, args.context, args.install_browser, args.dry_run)
    return 0


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def resolve_ecr_host(registry: str, account_id: str | None,
                     ecr_host: str | None, region: str | None) -> tuple[str, str]:
    """Resolve the ECR host and region. Returns (host, region).

    ECR registry URI format: <account-id>.dkr.ecr.<region>.amazonaws.com
    Reference: https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html
    """
    # Auto-detect when registry is itself an ECR host (contains .dkr.ecr.).
    host = ecr_host
    if host is None and ".dkr.ecr." in registry:
        host = registry

    if host is None:
        # Assemble from account-id + region.
        if not account_id or not region:
            fail("ECR push requires either (--ecr-host) or (--account-id and --region).")
        host = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    # If region was not given, extract it from the host.
    if not region and ".dkr.ecr." in host:
        # account.dkr.ecr.<region>.amazonaws.com
        region = host.split(".dkr.ecr.", 1)[1].split(".", 1)[0]
    if not region:
        fail("Could not resolve the ECR region. Specify it with --region.")
    return host, region


def ecr_login(host: str, region: str, dry_run: bool) -> None:
    """Pipe `aws ecr get-login-password` into `podman login --password-stdin`.

    Reference: https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html
      aws ecr get-login-password --region <region> \\
        | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
    podman login shares the same --username/--password-stdin interface as docker login.
      - https://docs.podman.io/en/latest/markdown/podman-login.1.html
    """
    require_tool("aws", dry_run)
    require_tool("podman", dry_run)

    pw_cmd = ["aws", "ecr", "get-login-password", "--region", region]
    login_cmd = ["podman", "login", "--username", "AWS", "--password-stdin", host]
    print(f"[aegisctl] $ {' '.join(pw_cmd)} | {' '.join(login_cmd)}")
    if dry_run:
        return

    password = subprocess.run(pw_cmd, check=True, capture_output=True, text=True).stdout
    subprocess.run(login_cmd, input=password, check=True, text=True)


def ecr_ensure_repo(repo: str, region: str, dry_run: bool) -> None:
    """Create the repo if it does not exist (create when describe fails).

    Reference:
      - https://docs.aws.amazon.com/cli/latest/reference/ecr/create-repository.html
        (--repository-name required, --region supported as a global option)
      - describe-repositories --repository-names <name> is the standard existence check.
    """
    describe = ["aws", "ecr", "describe-repositories",
                "--repository-names", repo, "--region", region]
    create = ["aws", "ecr", "create-repository",
              "--repository-name", repo, "--region", region]
    print(f"[aegisctl] $ {' '.join(describe)}  (create if missing)")
    if dry_run:
        print(f"[aegisctl] $ {' '.join(create)}")
        return

    exists = subprocess.run(describe, capture_output=True, text=True).returncode == 0
    if exists:
        print(f"[aegisctl] ECR repo already exists: {repo}")
        return
    subprocess.run(create, check=True)
    print(f"[aegisctl] ECR repo created: {repo}")


def private_login(host: str, username: str | None, password_stdin: bool,
                  dry_run: bool) -> None:
    """Log in to a private registry. Use --username/--password-stdin or assume an existing login.

    Reference: https://docs.podman.io/en/latest/markdown/podman-login.1.html
      echo $pw | podman login -u <user> --password-stdin <host>
    """
    require_tool("podman", dry_run)
    if not password_stdin:
        # With no credential options, assume already logged in and skip the login.
        print(f"[aegisctl] assuming {host} is already logged in (--password-stdin not given).")
        return
    if not username:
        fail("when using --password-stdin, also specify --username.")

    login_cmd = ["podman", "login", "--username", username, "--password-stdin", host]
    print(f"[aegisctl] $ (stdin) | {' '.join(login_cmd)}")
    if dry_run:
        return
    # Read the password from the caller's stdin (pipe/terminal) and pass it through.
    password = sys.stdin.read()
    subprocess.run(login_cmd, input=password, check=True, text=True)


def do_push(image: str, registry: str, dest: str | None,
            region: str | None, account_id: str | None, ecr_host: str | None,
            repo: str | None, create_repo: bool,
            username: str | None, password_stdin: bool, dry_run: bool) -> str:
    """Push the image to a registry. Returns the final push target tag."""
    require_tool("podman", dry_run)

    is_ecr = registry == "ecr" or ".dkr.ecr." in registry or bool(ecr_host)

    if is_ecr:
        host, region = resolve_ecr_host(registry, account_id, ecr_host, region)
        if not repo and not dest:
            fail("ECR push requires --repo (or a full --dest tag).")
        target = dest or f"{host}/{repo}:latest"
        ecr_login(host, region, dry_run)
        if create_repo:
            if not repo:
                fail("when using --create-repo, specify --repo.")
            ecr_ensure_repo(repo, region, dry_run)
    else:
        # Private registry. registry = host.
        host = registry
        if not dest:
            # Assemble as host/<image-name> (tag included).
            name = image.split("/")[-1]
            target = f"{host}/{name}"
        else:
            target = dest
        private_login(host, username, password_stdin, dry_run)

    # podman tag <local> <target>, then podman push.
    # Reference: https://docs.podman.io/en/latest/markdown/podman-push.1.html
    run(["sudo", "podman", "tag", image, target], dry_run)
    run(["sudo", "podman", "push", target], dry_run)
    print(f"[aegisctl] push done: {target}")
    return target


def cmd_push(args: argparse.Namespace) -> int:
    do_push(args.image, args.registry, args.dest, args.region, args.account_id,
            args.ecr_host, args.repo, args.create_repo, args.username,
            args.password_stdin, args.dry_run)
    return 0


# --------------------------------------------------------------------------- #
# disk
# --------------------------------------------------------------------------- #
def parse_disk_types(raw: str) -> list[str]:
    """Convert a comma-separated --type into a validated list."""
    types = [t.strip() for t in raw.split(",") if t.strip()]
    if not types:
        fail("--type is empty. Specify at least one of ami|qcow2|raw.")
    for t in types:
        if t not in VALID_DISK_TYPES:
            fail(f"unsupported disk type: {t} (allowed: {', '.join(VALID_DISK_TYPES)})")
    return types


def resolve_builder_image(flag_value: str | None) -> str:
    """Resolve the builder image by priority: --builder-image flag > env var > default constant."""
    if flag_value:
        return flag_value
    env_value = os.environ.get(BUILDER_IMAGE_ENV)
    if env_value:
        return env_value
    return DEFAULT_BUILDER_IMAGE


def do_disk(image: str, types: list[str],
            aws_ami_name: str | None, aws_bucket: str | None, aws_region: str | None,
            env_file: str | None, config: str | None,
            no_pull_base: bool, builder_image: str, dry_run: bool) -> None:
    """Create disk images with bootc-image-builder.

    Reference: https://github.com/osbuild/bootc-image-builder
      - One-shot container:
          sudo podman run --rm -it --privileged --pull=newer \\
            --security-opt label=type:unconfined_t \\
            -v /var/lib/containers/storage:/var/lib/containers/storage ...
      - --type selects the output type: ami|qcow2|raw, etc.
      - AWS AMI auto-upload: --aws-ami-name/--aws-bucket/--aws-region must all be
        specified together ("These flags must all be specified together").
        The bucket must already exist (the builder does not create it), and the account
        must have a vmimport service role configured.
      - Pass AWS credentials via --env-file, not plaintext --env.
    """
    require_tool("podman", dry_run)

    want_ami = "ami" in types
    want_local = any(t in ("qcow2", "raw") for t in types)

    # The three AMI flags must be specified together.
    if want_ami:
        if not (aws_ami_name and aws_bucket and aws_region):
            fail("when --type includes ami, --aws-ami-name/--aws-bucket/--aws-region "
                 "must all be specified together.")
        if not env_file:
            fail("AMI upload requires --env-file (AWS credentials).")

    # Pre-pull the base (target) image. Skip locally built images that start with localhost/.
    if not no_pull_base and not image.startswith("localhost/"):
        run(["sudo", "podman", "pull", image], dry_run)

    # Refresh the builder image itself with newer.
    run(["sudo", "podman", "pull", builder_image], dry_run)

    cmd = ["sudo", "podman", "run",
           "--rm", "-it",
           "--privileged",
           "--pull=newer",
           "--security-opt", "label=type:unconfined_t",
           "-v", "/var/lib/containers/storage:/var/lib/containers/storage"]

    # AWS credentials env file (for AMI upload).
    if want_ami:
        env_path = Path(env_file).resolve()
        if not dry_run and not env_path.is_file():
            fail(f"AWS credentials file not found: {env_path}\n"
                 "  Create it as AWS_ACCESS_KEY_ID=...\n  AWS_SECRET_ACCESS_KEY=...")
        cmd += ["--env-file", str(env_path)]

    # Local output (qcow2/raw) is written to the ./output directory.
    if want_local:
        output_dir = Path.cwd() / "output"
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{output_dir}:/output"]

    # Mount config.toml (optional) as /config.toml:ro.
    if config:
        config_path = Path(config).resolve()
        if not dry_run and not config_path.is_file():
            fail(f"config file not found: {config_path}")
        cmd += ["-v", f"{config_path}:/config.toml:ro"]

    cmd.append(builder_image)
    # Support multiple --type values: repeat the --type flag for each type.
    for t in types:
        cmd += ["--type", t]
    if config:
        cmd += ["--config", "/config.toml"]
    if want_ami:
        cmd += ["--aws-ami-name", aws_ami_name,
                "--aws-bucket", aws_bucket,
                "--aws-region", aws_region]
    cmd.append(image)

    run(cmd, dry_run)
    if want_local:
        print(f"[aegisctl] disk done. Local output: {Path.cwd() / 'output'}")
    if want_ami:
        print("[aegisctl] disk(ami) done. Check the registered AMI in the AWS console under EC2 > AMIs.")


def cmd_disk(args: argparse.Namespace) -> int:
    types = parse_disk_types(args.type)
    builder_image = resolve_builder_image(args.builder_image)
    do_disk(args.image, types, args.aws_ami_name, args.aws_bucket, args.aws_region,
            args.env_file, args.config, args.no_pull_base, builder_image, args.dry_run)
    return 0


# --------------------------------------------------------------------------- #
# all (build -> push -> disk)
# --------------------------------------------------------------------------- #
def cmd_all(args: argparse.Namespace) -> int:
    """Run build -> push -> disk in sequence. Stops if any step fails."""
    # 1) build
    tag = do_build(args.tag, args.file, args.context, args.install_browser, args.dry_run)

    # 2) push (use the built tag as the source)
    do_push(tag, args.registry, args.dest, args.region, args.account_id,
            args.ecr_host, args.repo, args.create_repo, args.username,
            args.password_stdin, args.dry_run)

    # 3) disk (convert the locally built image)
    types = parse_disk_types(args.type)
    builder_image = resolve_builder_image(args.builder_image)
    do_disk(tag, types, args.aws_ami_name, args.aws_bucket, args.aws_region,
            args.env_file, args.config, args.no_pull_base, builder_image, args.dry_run)

    print("[aegisctl] all pipeline done (build -> push -> disk).")
    return 0


# --------------------------------------------------------------------------- #
# argparse setup
# --------------------------------------------------------------------------- #
def add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tag", default=DEFAULT_TAG,
                   help="Image tag to build.")
    p.add_argument("--file", default=None,
                   help="Containerfile path (default: project-root Containerfile).")
    p.add_argument("--context", default=None,
                   help="Build context directory (default: project root).")
    p.add_argument("--install-browser", action="store_true",
                   help="Build with playwright/chromium included (INSTALL_BROWSER=true).")


def add_push_args(p: argparse.ArgumentParser, image_default: str | None = None) -> None:
    p.add_argument("--registry", required=True,
                   help="Target registry. 'ecr', or a host (ECR auto-detected when it contains "
                        ".dkr.ecr.; otherwise a private registry host).")
    p.add_argument("--dest", default=None,
                   help="Full push target tag (if unset, ECR assembles host/repo:latest).")
    # ECR options
    p.add_argument("--region", default=None, help="(ECR) AWS region.")
    p.add_argument("--account-id", default=None, help="(ECR) AWS account ID.")
    p.add_argument("--ecr-host", default=None,
                   help="(ECR) Full ECR host (account.dkr.ecr.region.amazonaws.com).")
    p.add_argument("--repo", default=None, help="(ECR) Repository name.")
    p.add_argument("--create-repo", action="store_true",
                   help="(ECR) Create the repo if it does not exist.")
    # Private registry options
    p.add_argument("--username", default=None, help="(private) Login username.")
    p.add_argument("--password-stdin", action="store_true",
                   help="(private) Read the password from stdin for podman login.")
    # Source image
    if image_default is None:
        p.add_argument("--image", required=True, help="Local source image to push.")
    # In all mode, the built --tag is the source, so --image is not added.


def add_disk_args(p: argparse.ArgumentParser, image_default: str | None = None) -> None:
    p.add_argument("--type", default="qcow2",
                   help="Disk type. ami|qcow2|raw, comma-separated for multiple (e.g. qcow2,raw).")
    p.add_argument("--aws-ami-name", default=None,
                   help="(ami) AMI name to register. Required together with the other two for the ami type.")
    p.add_argument("--aws-bucket", default=None,
                   help="(ami) S3 bucket for intermediate storage (must already exist). Required as one of the three.")
    p.add_argument("--aws-region", default=None,
                   help="(ami) Upload target region. Required as one of the three.")
    p.add_argument("--env-file", default=None,
                   help="(ami) AWS credentials env file (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY).")
    p.add_argument("--config", default=None,
                   help="Path to a bootc-image-builder config.toml (optional, mounted as /config.toml:ro).")
    p.add_argument("--no-pull-base", action="store_true",
                   help="Skip pre-pulling the target image with podman.")
    p.add_argument("--builder-image", default=None,
                   help=f"Override the disk builder container image. "
                        f"If unset, uses the env var {BUILDER_IMAGE_ENV}, then "
                        f"the default ({DEFAULT_BUILDER_IMAGE}). Prepares for the RHEL 11 image-builder switch.")
    if image_default is None:
        p.add_argument("--image", required=True, help="bootc image to convert to a disk.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegisctl",
        description="Unified deployment CLI for AegisOS (build/push/disk/all).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the podman/aws commands without running them.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build a container image from the Containerfile.",
                             formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_build_args(p_build)
    p_build.set_defaults(func=cmd_build)

    p_push = sub.add_parser("push", help="Push a container image to a registry.",
                            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_push_args(p_push)
    p_push.set_defaults(func=cmd_push)

    p_disk = sub.add_parser("disk", help="Create disk images (ami|qcow2|raw).",
                            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_disk_args(p_disk)
    p_disk.set_defaults(func=cmd_disk)

    # all: build -> push -> disk. The build --tag becomes the source image for push/disk.
    p_all = sub.add_parser("all", help="Run build -> push -> disk in sequence (pipeline).",
                           formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_build_args(p_all)
    add_push_args(p_all, image_default="tag")
    add_disk_args(p_all, image_default="tag")
    p_all.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except subprocess.CalledProcessError as exc:
        print(f"[aegisctl] command failed (exit {exc.returncode}): {' '.join(exc.cmd)}",
              file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
