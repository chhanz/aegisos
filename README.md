# AegisOS

AegisOS is a [bootc](https://bootc.dev/) image that bundles the [Hermes Agent](https://github.com/NousResearch/hermes-agent) on top of Fedora Hummingbird, a container-native, image-mode operating system. The name "Aegis" (shield) reflects the design: the OS itself is locked read-only and immutable, so an AI agent running on it cannot tamper with the system underneath. Build a system from this image, do a small amount of first-boot setup, and the Hermes Agent gateway comes up ready to use.

## Overview

- The OS and the Hermes code live in a read-only area (`/usr`). Updates replace the whole image at once, which keeps the system consistent and easy to roll back.
- User data, such as API keys, configuration, and session history, is stored in the persistent writable area `/var/lib/hermes`. This data survives OS updates and rollbacks.
- Secrets such as API keys are never baked into the image. You set them up yourself after the first boot.

## Features

- Immutable, image-mode base built on Fedora Hummingbird (rolling release).
- Hermes Agent preinstalled, with the gateway started automatically once configured.
- A single deployment CLI (`aegisctl`) for building, pushing, and producing disk images.
- Multiple output formats: container image, AWS AMI, qcow2, and raw disk.
- Optional browser automation (playwright/chromium) via a build flag.

## Prerequisites

- `podman` (used for building the image and producing disk images).
- To push to AWS ECR or build an AMI: an AWS account, the `aws` CLI, and (for AMIs) an S3 bucket plus a configured vmimport service role.

All deployment tasks run through a single CLI. Run it from the repo root with the `./aegisctl` wrapper, which has four commands:

| Command | What it does |
| --- | --- |
| `aegisctl build` | Build a container image from the Containerfile |
| `aegisctl push`  | Push the image to a registry (AWS ECR or private) |
| `aegisctl disk`  | Create a disk image (AMI / qcow2 / raw) |
| `aegisctl all`   | Run build -> push -> disk in one go |

The `./aegisctl` wrapper resolves its own location, so it works from any directory and via an absolute path. If your environment has no `bash`, call the script directly with `python3 deploy/aegisctl.py <command>` instead; the arguments are identical.

Add `--dry-run` to any command to print the underlying commands without running them; it is a good way to preview what will happen. The `--dry-run` flag is global, so place it before the command (`./aegisctl --dry-run build ...`). See `./aegisctl <command> --help` for the full set of options.

## Build

```bash
./aegisctl build --tag localhost/aegisos:latest
```

Add `--install-browser` if you need browser automation (playwright/chromium). It is off by default because it increases the image size.

```bash
./aegisctl build --tag localhost/aegisos:latest --install-browser
```

Building directly with podman produces the same result:

```bash
sudo podman build -t localhost/aegisos:latest .
```

## Push

Push the image to a registry when you want to pull the same image on multiple machines.

To push to AWS ECR (you must already be logged in via the `aws` CLI). Pass `--create-repo` to create the repository automatically if it does not exist.

```bash
./aegisctl push \
  --registry ecr \
  --account-id 123456789012 \
  --region us-east-1 \
  --repo aegisos \
  --create-repo \
  --image localhost/aegisos:latest
```

To push to a private registry:

```bash
echo "$REGISTRY_PASSWORD" | ./aegisctl push \
  --registry registry.example.com:5000 \
  --username myuser --password-stdin \
  --image localhost/aegisos:latest
```

## Disk image

To build local VM disks (qcow2, raw), pass the formats you want to `--type`. You can request several at once with commas, and the output lands in `./output` under the current directory.

```bash
./aegisctl disk --type qcow2,raw --image localhost/aegisos:latest
```

To build an AWS AMI, first prepare an AWS credentials file. Never commit this file to git.

```bash
cat > aws.secrets <<'EOF'
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
EOF
```

Then run the AMI build. `--aws-ami-name`, `--aws-bucket`, and `--aws-region` must all be specified together, the S3 bucket must already exist in that region (the builder will not create it), and the account must have a vmimport service role configured.

```bash
./aegisctl disk \
  --type ami \
  --aws-ami-name aegisos \
  --aws-bucket my-bootc-bucket \
  --aws-region us-east-1 \
  --env-file ./aws.secrets \
  --config deploy/config.example.toml \
  --image localhost/aegisos:latest
```

The `config.example.toml` passed via `--config` defines the user account and SSH key created on first boot. Replace it with your own SSH public key before real use.

When the build finishes, you will find the registered AMI in the AWS console under EC2 > AMIs, ready to launch an EC2 instance.

### Choosing the builder image and the RHEL 11 transition

Disk builds use the `quay.io/centos-bootc/bootc-image-builder:latest` container by default. To use a different builder image, set `--builder-image`, or the `AEGISCTL_BUILDER_IMAGE` environment variable. The priority is `--builder-image` flag > environment variable > default.

```bash
# Via flag
./aegisctl disk --type qcow2 --image localhost/aegisos:latest \
  --builder-image quay.io/centos-bootc/bootc-image-builder:latest

# Via environment variable
export AEGISCTL_BUILDER_IMAGE=quay.io/centos-bootc/bootc-image-builder:latest
./aegisctl disk --type qcow2 --image localhost/aegisos:latest
```

Starting with RHEL 11, `bootc-image-builder` is being replaced by `image-builder` ([official deprecation notice](https://osbuild.org/docs/bootc/deprecation-notice/)). When that happens, you can point `--builder-image` at the `image-builder` container to switch without code changes. Note that `image-builder` CLI compatibility for `--type`/`--aws-*` is not yet verified, so recheck those arguments when you make the switch.

> Note: aegisctl uses `podman` only as its container runtime (docker is unsupported). The disk build tool requires rootful podman, and converting a locally built image needs podman local storage (`/var/lib/containers/storage`) mounted directly ([Red Hat image-mode docs](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_image_mode_for_rhel_to_build_deploy_and_manage_operating_systems/creating-bootc-compatible-base-disk-images-by-using-bootc-image-builder)). docker uses a different storage backend, so this approach does not work there.

### All at once (build -> push -> disk)

To run build, push, and disk creation in a single pass, use `all`. It accepts the options of each step and stops at the first failure.

```bash
./aegisctl all \
  --tag localhost/aegisos:latest \
  --registry ecr --account-id 123456789012 --region us-east-1 --repo aegisos --create-repo \
  --type qcow2
```

> Note: the older `deploy/build_ami.py` has been superseded by `aegisctl disk --type ami`. The file is kept for compatibility, but use `aegisctl` for new work.

## First-boot setup

Right after boot there is no configuration yet, so the Hermes gateway does not start automatically (a safeguard against an endless restart loop with no config). SSH in and run the setup.

```bash
# Tell Hermes where its data lives.
export HERMES_HOME=/var/lib/hermes

# Run the setup wizard to create config.yaml and the API keys (.env).
hermes setup
```

The moment `hermes setup` creates `/var/lib/hermes/config.yaml`, a watcher unit (`hermes-gateway.path`) that is enabled ahead of time detects it and starts the gateway (`hermes-gateway.service`) automatically. There is no separate service to start.

Check the status:

```bash
systemctl status hermes-gateway.service
journalctl -u hermes-gateway.service -f
```

All data under `/var/lib/hermes` (configuration, API keys, sessions, logs, skills, memory, and so on) survives OS updates and rollbacks. To back it up, back up this directory.

## Project structure

```
aegisos/
  aegisctl                       # Wrapper: run ./aegisctl from the repo root
  Containerfile                  # Base image + dependencies + Hermes install + unit placement
  systemd/
    hermes-gateway.service       # Gateway service (starts only when config exists)
    hermes-gateway.path          # Watches for config.yaml -> starts the service automatically
  tmpfiles.d/
    hermes.conf                  # Ensures the /var/lib/hermes directory exists
  deploy/
    aegisctl.py                  # Unified deployment CLI (build/push/disk/all)
    build_ami.py                 # (deprecated) superseded by aegisctl disk --type ami
    config.example.toml          # Example customizations for disk image builds
  README.md
```

## References

- Fedora Hummingbird: https://fedoramagazine.org/fedora-hummingbird-linux-taking-the-hummingbird-model-to-the-full-os/
- bootc documentation: https://bootc.dev/
- bootc-image-builder: https://github.com/osbuild/bootc-image-builder
- Hermes Agent: https://github.com/NousResearch/hermes-agent

## License

Licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
