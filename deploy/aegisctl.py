#!/usr/bin/env python3
"""aegisctl - AegisOS 통합 배포 CLI.

AegisOS = Fedora Hummingbird(bootc image-mode OS) + Hermes Agent 통합 이미지.
"방패(Aegis)"는 AI 가 함부로 건드릴 수 없는 불변 OS 콘셉트를 뜻한다.

흩어진 배포 스크립트(기존 deploy/build_ami.py)를 단일 CLI 로 통합한다.
순수 Python 표준 라이브러리만 사용한다(외부 의존성 0).

컨테이너 런타임은 podman 만 사용한다(docker 미지원, 단순성). 자세한 이유는
아래 podman 전용 근거 주석 참조. 모든 서브커맨드는 진입 시 shutil.which 로
podman(필요 시 aws) 존재를 확인하고, 없으면 한국어 에러로 즉시 종료한다.

서브커맨드:
  build   Containerfile 로 컨테이너 이미지 빌드 (sudo podman build)
  push    컨테이너 이미지를 레지스트리(ECR/사설)에 push
  disk    디스크 이미지 생성 (ami|qcow2|raw, bootc-image-builder)
  all     build -> push -> disk 순차 실행(파이프라인)

전역 옵션:
  --dry-run   실제 실행 없이 podman/aws 커맨드만 출력한다.

사용 예:
  # 1) 컨테이너 이미지 빌드
  python3 deploy/aegisctl.py build --tag localhost/aegisos:latest

  # 2) ECR 로 push (repo 없으면 생성)
  python3 deploy/aegisctl.py push --registry ecr \\
      --account-id 123456789012 --region us-east-1 --repo aegisos --create-repo \\
      --image localhost/aegisos:latest

  # 3) 로컬 qcow2 + raw 디스크 생성
  python3 deploy/aegisctl.py disk --type qcow2,raw --image localhost/aegisos:latest

  # 4) AMI 생성 + AWS 업로드
  python3 deploy/aegisctl.py disk --type ami \\
      --aws-ami-name aegisos --aws-bucket my-bootc-bucket --aws-region us-east-1 \\
      --env-file ./aws.secrets --image localhost/aegisos:latest

  # 5) 전체 파이프라인
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

# aegisctl 은 podman 만 사용한다(docker 미지원). 이유:
#   1) bootc-image-builder 의 disk 빌드는 rootful podman 을 강제한다
#      ("bootc-image-builder requires rootful Podman to build images. Running it in
#        rootless mode causes the build to fail.")
#   2) 로컬 빌드 이미지를 쓸 때 podman 로컬 스토리지(/var/lib/containers/storage)를
#      직접 마운트해야 한다("If you are using a container image built locally, you must
#      include the -v /var/lib/containers/storage:/var/lib/containers/storage argument.").
#      docker 는 스토리지 백엔드가 달라 이 경로가 통하지 않는다.
#   docker 분기를 두지 않는 것은 단순성 때문이다.
#   근거: https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_image_mode_for_rhel_to_build_deploy_and_manage_operating_systems/creating-bootc-compatible-base-disk-images-by-using-bootc-image-builder

# 디스크 빌더 컨테이너 이미지(현재 표준). disk 서브커맨드에서 사용한다.
# 확인: https://github.com/osbuild/bootc-image-builder (README 의 podman run 예시)
#
# RHEL 11 에서 bootc-image-builder 가 image-builder 로 대체될 예정이다(공식 deprecation 공지).
#   - https://osbuild.org/docs/bootc/deprecation-notice/
# 그때는 --builder-image 로 image-builder 컨테이너를 지정하면 코드 변경 없이 전환 가능하다.
# 단, image-builder CLI 의 --type/--aws-* 인자 호환성은 아직 미검증이므로 전환 시점에
# 재확인이 필요하다(지금은 빌더 이미지 참조만 분리하고 image-builder 분기 로직은 두지 않는다).
DEFAULT_BUILDER_IMAGE = "quay.io/centos-bootc/bootc-image-builder:latest"

# 빌더 이미지 오버라이드 환경변수. 우선순위: --builder-image 플래그 > 환경변수 > 기본 상수.
BUILDER_IMAGE_ENV = "AEGISCTL_BUILDER_IMAGE"

# 기본 이미지 태그. 프로젝트명 AegisOS 를 따른다.
DEFAULT_TAG = "localhost/aegisos:latest"

# 스크립트는 deploy/ 안에 있다. 프로젝트 루트 = deploy 의 부모.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

VALID_DISK_TYPES = ("ami", "qcow2", "raw")


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
def run(cmd: list[str], dry_run: bool) -> None:
    """커맨드를 출력하고(항상) 실행한다(dry-run 이 아니면)."""
    print(f"[aegisctl] $ {' '.join(cmd)}")
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def require_tool(name: str, dry_run: bool = False) -> None:
    """필수 외부 도구 존재를 shutil.which 로 확인한다.

    dry-run 은 커맨드만 출력하므로(실제 실행 없음) 도구가 없어도 통과시킨다.
    """
    if dry_run:
        return
    if shutil.which(name) is None:
        print(f"[aegisctl] 오류: '{name}' 를 PATH 에서 찾을 수 없다. 설치 후 다시 시도하라.",
              file=sys.stderr)
        raise SystemExit(1)


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    """한국어 에러 메시지를 출력하고 종료한다."""
    print(f"[aegisctl] 오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def do_build(tag: str, file: str | None, context: str | None,
             install_browser: bool, dry_run: bool) -> str:
    """Containerfile 로 컨테이너 이미지를 빌드한다. 빌드한 태그를 반환한다.

    확인: bootc 이미지는 일반 OCI 이미지이므로 `podman build` 로 빌드한다.
      - https://bootc.dev/bootc/building/guidance.html
    """
    require_tool("podman", dry_run)

    # --file 기본값: 프로젝트 루트의 Containerfile.
    containerfile = Path(file).resolve() if file else (PROJECT_ROOT / "Containerfile")
    # --context 기본값: 프로젝트 루트.
    build_context = Path(context).resolve() if context else PROJECT_ROOT

    if not dry_run and not containerfile.is_file():
        fail(f"Containerfile 이 없다: {containerfile}")
    if not dry_run and not build_context.is_dir():
        fail(f"빌드 컨텍스트 디렉토리가 없다: {build_context}")

    cmd = ["sudo", "podman", "build",
           "--file", str(containerfile),
           "--tag", tag]
    # Containerfile 의 ARG INSTALL_BROWSER 토글.
    cmd += ["--build-arg", f"INSTALL_BROWSER={'true' if install_browser else 'false'}"]
    cmd.append(str(build_context))

    run(cmd, dry_run)
    print(f"[aegisctl] build 완료: {tag}")
    return tag


def cmd_build(args: argparse.Namespace) -> int:
    do_build(args.tag, args.file, args.context, args.install_browser, args.dry_run)
    return 0


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def resolve_ecr_host(registry: str, account_id: str | None,
                     ecr_host: str | None, region: str | None) -> tuple[str, str]:
    """ECR 호스트와 region 을 확정한다. (host, region) 반환.

    ECR 레지스트리 URI 형식: <account-id>.dkr.ecr.<region>.amazonaws.com
    확인: https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html
    """
    # registry 자체가 ECR 호스트인 경우(.dkr.ecr. 포함)를 자동감지.
    host = ecr_host
    if host is None and ".dkr.ecr." in registry:
        host = registry

    if host is None:
        # account-id + region 으로 조립.
        if not account_id or not region:
            fail("ECR push 에는 (--ecr-host) 또는 (--account-id 와 --region) 이 필요하다.")
        host = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    # region 이 명시되지 않았으면 호스트에서 추출한다.
    if not region and ".dkr.ecr." in host:
        # account.dkr.ecr.<region>.amazonaws.com
        region = host.split(".dkr.ecr.", 1)[1].split(".", 1)[0]
    if not region:
        fail("ECR region 을 확정할 수 없다. --region 으로 명시하라.")
    return host, region


def ecr_login(host: str, region: str, dry_run: bool) -> None:
    """`aws ecr get-login-password` 결과를 `podman login --password-stdin` 로 흘려보낸다.

    확인: https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html
      aws ecr get-login-password --region <region> \\
        | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
    podman login 은 docker login 과 동일한 --username/--password-stdin 인터페이스를 가진다.
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
    """repo 가 없으면 생성한다(describe 실패 시 create).

    확인:
      - https://docs.aws.amazon.com/cli/latest/reference/ecr/create-repository.html
        (--repository-name 필수, --region 글로벌 옵션 지원)
      - describe-repositories --repository-names <name> 는 존재 확인용 표준 구문.
    """
    describe = ["aws", "ecr", "describe-repositories",
                "--repository-names", repo, "--region", region]
    create = ["aws", "ecr", "create-repository",
              "--repository-name", repo, "--region", region]
    print(f"[aegisctl] $ {' '.join(describe)}  (없으면 create)")
    if dry_run:
        print(f"[aegisctl] $ {' '.join(create)}")
        return

    exists = subprocess.run(describe, capture_output=True, text=True).returncode == 0
    if exists:
        print(f"[aegisctl] ECR repo 가 이미 존재한다: {repo}")
        return
    subprocess.run(create, check=True)
    print(f"[aegisctl] ECR repo 생성: {repo}")


def private_login(host: str, username: str | None, password_stdin: bool,
                  dry_run: bool) -> None:
    """사설 레지스트리 로그인. --username/--password-stdin 또는 기존 로그인 가정.

    확인: https://docs.podman.io/en/latest/markdown/podman-login.1.html
      echo $pw | podman login -u <user> --password-stdin <host>
    """
    require_tool("podman", dry_run)
    if not password_stdin:
        # 자격증명 옵션이 없으면 이미 로그인된 상태로 가정하고 로그인을 건너뛴다.
        print(f"[aegisctl] {host} 는 기존 로그인 상태로 가정한다(--password-stdin 미지정).")
        return
    if not username:
        fail("--password-stdin 사용 시 --username 도 지정하라.")

    login_cmd = ["podman", "login", "--username", username, "--password-stdin", host]
    print(f"[aegisctl] $ (stdin) | {' '.join(login_cmd)}")
    if dry_run:
        return
    # 호출자(파이프/터미널)의 stdin 에서 패스워드를 읽어 그대로 전달한다.
    password = sys.stdin.read()
    subprocess.run(login_cmd, input=password, check=True, text=True)


def do_push(image: str, registry: str, dest: str | None,
            region: str | None, account_id: str | None, ecr_host: str | None,
            repo: str | None, create_repo: bool,
            username: str | None, password_stdin: bool, dry_run: bool) -> str:
    """이미지를 레지스트리에 push 한다. 최종 push 대상 태그를 반환한다."""
    require_tool("podman", dry_run)

    is_ecr = registry == "ecr" or ".dkr.ecr." in registry or bool(ecr_host)

    if is_ecr:
        host, region = resolve_ecr_host(registry, account_id, ecr_host, region)
        if not repo and not dest:
            fail("ECR push 에는 --repo(또는 --dest 전체 태그)가 필요하다.")
        target = dest or f"{host}/{repo}:latest"
        ecr_login(host, region, dry_run)
        if create_repo:
            if not repo:
                fail("--create-repo 사용 시 --repo 를 지정하라.")
            ecr_ensure_repo(repo, region, dry_run)
    else:
        # 사설 레지스트리. registry = host.
        host = registry
        if not dest:
            # host/<이미지이름> 형태로 조립(태그 포함).
            name = image.split("/")[-1]
            target = f"{host}/{name}"
        else:
            target = dest
        private_login(host, username, password_stdin, dry_run)

    # podman tag <local> <target> 후 podman push.
    # 확인: https://docs.podman.io/en/latest/markdown/podman-push.1.html
    run(["sudo", "podman", "tag", image, target], dry_run)
    run(["sudo", "podman", "push", target], dry_run)
    print(f"[aegisctl] push 완료: {target}")
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
    """쉼표 구분 --type 을 검증된 리스트로 변환한다."""
    types = [t.strip() for t in raw.split(",") if t.strip()]
    if not types:
        fail("--type 이 비었다. ami|qcow2|raw 중 하나 이상 지정하라.")
    for t in types:
        if t not in VALID_DISK_TYPES:
            fail(f"지원하지 않는 디스크 타입: {t} (가능: {', '.join(VALID_DISK_TYPES)})")
    return types


def resolve_builder_image(flag_value: str | None) -> str:
    """빌더 이미지를 우선순위에 따라 확정한다: --builder-image 플래그 > 환경변수 > 기본 상수."""
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
    """bootc-image-builder 로 디스크 이미지를 생성한다.

    확인: https://github.com/osbuild/bootc-image-builder
      - 일회성 컨테이너:
          sudo podman run --rm -it --privileged --pull=newer \\
            --security-opt label=type:unconfined_t \\
            -v /var/lib/containers/storage:/var/lib/containers/storage ...
      - --type 으로 ami|qcow2|raw 등 출력 타입 지정.
      - AWS AMI 자동 업로드: --aws-ami-name/--aws-bucket/--aws-region 세 플래그를
        반드시 함께 지정("These flags must all be specified together").
        버킷은 미리 존재해야 하고(빌더가 만들지 않음), 계정에 vmimport service role 이
        구성되어 있어야 한다.
      - AWS 자격증명은 평문(--env) 대신 --env-file 로 전달한다.
    """
    require_tool("podman", dry_run)

    want_ami = "ami" in types
    want_local = any(t in ("qcow2", "raw") for t in types)

    # AMI 3종 플래그는 함께 지정되어야 한다.
    if want_ami:
        if not (aws_ami_name and aws_bucket and aws_region):
            fail("--type 에 ami 가 포함되면 --aws-ami-name/--aws-bucket/--aws-region 을 "
                 "모두 함께 지정해야 한다.")
        if not env_file:
            fail("AMI 업로드에는 --env-file(AWS 자격증명)이 필요하다.")

    # 베이스(대상) 이미지를 미리 pull. localhost/ 로 시작하는 로컬 빌드 이미지는 skip.
    if not no_pull_base and not image.startswith("localhost/"):
        run(["sudo", "podman", "pull", image], dry_run)

    # 빌더 이미지 자체는 newer 로 갱신.
    run(["sudo", "podman", "pull", builder_image], dry_run)

    cmd = ["sudo", "podman", "run",
           "--rm", "-it",
           "--privileged",
           "--pull=newer",
           "--security-opt", "label=type:unconfined_t",
           "-v", "/var/lib/containers/storage:/var/lib/containers/storage"]

    # AWS 자격증명 env 파일(AMI 업로드 시).
    if want_ami:
        env_path = Path(env_file).resolve()
        if not dry_run and not env_path.is_file():
            fail(f"AWS 자격증명 파일이 없다: {env_path}\n"
                 "  AWS_ACCESS_KEY_ID=...\n  AWS_SECRET_ACCESS_KEY=... 형식으로 만들어라.")
        cmd += ["--env-file", str(env_path)]

    # 로컬 출력(qcow2/raw)은 ./output 디렉토리로 받는다.
    if want_local:
        output_dir = Path.cwd() / "output"
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{output_dir}:/output"]

    # config.toml(선택) 을 /config.toml:ro 로 마운트.
    if config:
        config_path = Path(config).resolve()
        if not dry_run and not config_path.is_file():
            fail(f"config 파일이 없다: {config_path}")
        cmd += ["-v", f"{config_path}:/config.toml:ro"]

    cmd.append(builder_image)
    # 다중 --type 지원: 각 타입마다 --type 플래그를 반복한다.
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
        print(f"[aegisctl] disk 완료. 로컬 출력: {Path.cwd() / 'output'}")
    if want_ami:
        print("[aegisctl] disk(ami) 완료. AWS 콘솔 EC2 > AMIs 에서 등록된 AMI 를 확인하라.")


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
    """build -> push -> disk 순차 실행. 한 단계라도 실패하면 중단된다."""
    # 1) build
    tag = do_build(args.tag, args.file, args.context, args.install_browser, args.dry_run)

    # 2) push (build 한 태그를 소스로 사용)
    do_push(tag, args.registry, args.dest, args.region, args.account_id,
            args.ecr_host, args.repo, args.create_repo, args.username,
            args.password_stdin, args.dry_run)

    # 3) disk (로컬 빌드 이미지를 변환)
    types = parse_disk_types(args.type)
    builder_image = resolve_builder_image(args.builder_image)
    do_disk(tag, types, args.aws_ami_name, args.aws_bucket, args.aws_region,
            args.env_file, args.config, args.no_pull_base, builder_image, args.dry_run)

    print("[aegisctl] all 파이프라인 완료(build -> push -> disk).")
    return 0


# --------------------------------------------------------------------------- #
# argparse 구성
# --------------------------------------------------------------------------- #
def add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tag", default=DEFAULT_TAG,
                   help="빌드할 이미지 태그.")
    p.add_argument("--file", default=None,
                   help="Containerfile 경로(기본: 프로젝트 루트 Containerfile).")
    p.add_argument("--context", default=None,
                   help="빌드 컨텍스트 디렉토리(기본: 프로젝트 루트).")
    p.add_argument("--install-browser", action="store_true",
                   help="playwright/chromium 을 포함해 빌드한다(INSTALL_BROWSER=true).")


def add_push_args(p: argparse.ArgumentParser, image_default: str | None = None) -> None:
    p.add_argument("--registry", required=True,
                   help="대상 레지스트리. 'ecr' 또는 호스트(.dkr.ecr. 포함 시 ECR 자동감지, "
                        "그 외는 사설 레지스트리 호스트).")
    p.add_argument("--dest", default=None,
                   help="push 대상 전체 태그(미지정 시 ECR=호스트/repo:latest 로 조립).")
    # ECR 옵션
    p.add_argument("--region", default=None, help="(ECR) AWS region.")
    p.add_argument("--account-id", default=None, help="(ECR) AWS 계정 ID.")
    p.add_argument("--ecr-host", default=None,
                   help="(ECR) 전체 ECR 호스트(account.dkr.ecr.region.amazonaws.com).")
    p.add_argument("--repo", default=None, help="(ECR) 리포지토리 이름.")
    p.add_argument("--create-repo", action="store_true",
                   help="(ECR) repo 가 없으면 생성한다.")
    # 사설 레지스트리 옵션
    p.add_argument("--username", default=None, help="(사설) 로그인 사용자명.")
    p.add_argument("--password-stdin", action="store_true",
                   help="(사설) 패스워드를 stdin 에서 읽어 podman login 한다.")
    # 소스 이미지
    if image_default is None:
        p.add_argument("--image", required=True, help="push 할 로컬 소스 이미지.")
    # all 모드에서는 build 한 --tag 를 소스로 쓰므로 --image 를 추가하지 않는다.


def add_disk_args(p: argparse.ArgumentParser, image_default: str | None = None) -> None:
    p.add_argument("--type", default="qcow2",
                   help="디스크 타입. ami|qcow2|raw, 쉼표 구분 다중 가능(예: qcow2,raw).")
    p.add_argument("--aws-ami-name", default=None,
                   help="(ami) 등록할 AMI 이름. ami 타입에서 3종 함께 필수.")
    p.add_argument("--aws-bucket", default=None,
                   help="(ami) 중간 저장용 S3 버킷(미리 존재해야 함). 3종 함께 필수.")
    p.add_argument("--aws-region", default=None,
                   help="(ami) 업로드 대상 region. 3종 함께 필수.")
    p.add_argument("--env-file", default=None,
                   help="(ami) AWS 자격증명 env 파일(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY).")
    p.add_argument("--config", default=None,
                   help="bootc-image-builder config.toml 경로(선택, /config.toml:ro 마운트).")
    p.add_argument("--no-pull-base", action="store_true",
                   help="대상 이미지 사전 podman pull 단계를 건너뛴다.")
    p.add_argument("--builder-image", default=None,
                   help=f"디스크 빌더 컨테이너 이미지 오버라이드. "
                        f"미지정 시 환경변수 {BUILDER_IMAGE_ENV}, 그것도 없으면 "
                        f"기본값({DEFAULT_BUILDER_IMAGE}). RHEL 11 의 image-builder 전환 대비.")
    if image_default is None:
        p.add_argument("--image", required=True, help="디스크로 변환할 bootc 이미지.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegisctl",
        description="AegisOS 통합 배포 CLI (build/push/disk/all).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="실제 실행 없이 podman/aws 커맨드만 출력한다.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Containerfile 로 컨테이너 이미지 빌드.",
                             formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_build_args(p_build)
    p_build.set_defaults(func=cmd_build)

    p_push = sub.add_parser("push", help="컨테이너 이미지를 레지스트리에 push.",
                            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_push_args(p_push)
    p_push.set_defaults(func=cmd_push)

    p_disk = sub.add_parser("disk", help="디스크 이미지 생성(ami|qcow2|raw).",
                            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_disk_args(p_disk)
    p_disk.set_defaults(func=cmd_disk)

    # all: build -> push -> disk. build 의 --tag 가 push/disk 의 소스 이미지가 된다.
    p_all = sub.add_parser("all", help="build -> push -> disk 순차 실행(파이프라인).",
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
        print(f"[aegisctl] 커맨드 실패(exit {exc.returncode}): {' '.join(exc.cmd)}",
              file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
