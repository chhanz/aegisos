#!/usr/bin/env python3
# deprecated: aegisctl disk --type ami 로 대체됨 (deploy/aegisctl.py).
"""bootc-image-builder 로 Hummingbird + Hermes 이미지를 AWS AMI 로 빌드/업로드한다.

내부적으로 podman 으로 quay.io/centos-bootc/bootc-image-builder 를 호출한다.

확인한 공식 동작 (https://github.com/osbuild/bootc-image-builder):
  - `--type ami` 로 AMI 출력.
  - AWS 자동 업로드는 `--aws-ami-name`, `--aws-bucket`, `--aws-region` 세 플래그를
    반드시 함께 지정해야 한다("These flags must all be specified together").
  - 버킷은 미리 존재해야 한다(빌더가 만들지 않는다).
  - 계정에 vmimport service role 이 구성되어 있어야 한다.
  - AWS 자격증명은 --env 로 평문 전달하지 말고 --env-file 로 전달한다.
  - AWS 업로드 시 /output 볼륨은 필요 없다(이미지가 AWS 로 직접 업로드됨).

주의(공식 문서/커뮤니티 확인):
  - bootc-image-builder 저장소는 archived 되어 image-builder 로 병합되었다.
    최신 사용 시 https://github.com/osbuild/image-builder 도 확인하라.
  - 최신 bootc-image-builder 는 베이스 이미지를 자동 pull 하지 않을 수 있으므로,
    빌드 대상 이미지를 미리 `podman pull` 해 두는 것이 안전하다.

사용 예:
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
        description="bootc-image-builder 로 AMI 를 빌드/업로드한다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--image",
        default="localhost/aegisos:latest",
        help="AMI 로 변환할 bootc 컨테이너 이미지(미리 podman build/pull 되어 있어야 함).",
    )
    p.add_argument("--ami-name", required=True, help="AWS 에 등록할 AMI 이름(--aws-ami-name).")
    p.add_argument("--bucket", required=True, help="중간 저장용 S3 버킷 이름(--aws-bucket). 미리 존재해야 함.")
    p.add_argument("--region", required=True, help="AWS 업로드 대상 리전(--aws-region).")
    p.add_argument(
        "--aws-secrets",
        default="aws.secrets",
        help="AWS 자격증명 env 파일. AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY 를 담는다.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="bootc-image-builder config.toml 경로(선택). 지정하면 /config.toml 로 마운트.",
    )
    p.add_argument(
        "--no-pull-base",
        action="store_true",
        help="대상 이미지를 미리 podman pull 하는 단계를 건너뛴다.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 실행하지 않고 podman 커맨드만 출력한다.",
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
        print("[build_ami] 오류: podman 을 찾을 수 없다.", file=sys.stderr)
        return 1

    secrets_path = Path(args.aws_secrets)
    if not args.dry_run and not secrets_path.is_file():
        print(
            f"[build_ami] 오류: AWS 자격증명 파일이 없다: {secrets_path}\n"
            "  AWS_ACCESS_KEY_ID=...\n  AWS_SECRET_ACCESS_KEY=...\n"
            "  형식으로 만들어라.",
            file=sys.stderr,
        )
        return 1

    # 베이스(대상) 이미지를 미리 pull. 로컬 빌드 이미지면 pull 이 실패할 수 있어 무시한다.
    if not args.no_pull_base and not args.image.startswith("localhost/"):
        run(["sudo", "podman", "pull", args.image], args.dry_run)

    # bootc-image-builder 자체는 newer 로 갱신.
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
            print(f"[build_ami] 오류: config 파일이 없다: {config_path}", file=sys.stderr)
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
        print(f"[build_ami] 빌드 실패(exit {exc.returncode}).", file=sys.stderr)
        return exc.returncode

    print("[build_ami] 완료. AWS 콘솔의 EC2 > AMIs 에서 등록된 AMI 를 확인하라.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
