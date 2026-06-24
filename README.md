# AegisOS - Fedora Hummingbird + Hermes Agent bootc 이미지

AegisOS 는 Fedora Hummingbird(컨테이너 네이티브 image-mode OS) 위에 [Hermes Agent](https://github.com/NousResearch/hermes-agent)를 통합한 bootc 이미지입니다. "Aegis(방패)"라는 이름은 OS 본체가 읽기 전용으로 고정되어 AI 가 함부로 건드릴 수 없는 불변(immutable) 구조를 뜻합니다. 이 이미지로 빌드한 시스템은 부팅 후 간단한 설정만 하면 Hermes Agent 게이트웨이가 바로 동작합니다.

## 어떻게 동작하나요

- OS 본체와 Hermes 코드는 읽기 전용 영역(`/usr`)에 고정됩니다. OS 업데이트는 이미지를 통째로 교체하는 방식이라 안정적입니다.
- API 키, 설정, 세션 기록 같은 사용자 데이터는 영구 쓰기 영역인 `/var/lib/hermes`에 저장됩니다. OS를 업데이트하거나 롤백해도 이 데이터는 보존됩니다.
- API 키 같은 비밀 정보는 이미지에 굽지 않습니다. 첫 부팅 후 직접 설정합니다.

## 준비물

- `podman` (이미지 빌드 및 디스크 이미지 생성에 사용)
- AWS ECR 에 push 하거나 AMI 를 만들려면: AWS 계정, `aws` CLI, (AMI 의 경우) S3 버킷과 vmimport 서비스 역할 설정

## aegisctl 한눈에 보기

배포 작업은 단일 CLI `deploy/aegisctl.py` 로 통합되어 있습니다. 네 가지 명령이 있습니다.

| 명령 | 하는 일 |
| --- | --- |
| `aegisctl build` | Containerfile 로 컨테이너 이미지 빌드 |
| `aegisctl push`  | 이미지를 레지스트리(AWS ECR 또는 사설)에 push |
| `aegisctl disk`  | 디스크 이미지 생성(AMI / qcow2 / raw) |
| `aegisctl all`   | build -> push -> disk 를 한 번에 |

어떤 명령이든 `--dry-run` 을 붙이면 실제로 실행하지 않고 어떤 명령이 돌아가는지만 보여줍니다. 처음 쓸 때 먼저 확인해 보길 권합니다. 자세한 옵션은 `python3 deploy/aegisctl.py <명령> --help` 로 볼 수 있습니다.

## 1. 이미지 빌드

```bash
python3 deploy/aegisctl.py build --tag localhost/aegisos:latest
```

브라우저 자동화(playwright/chromium)가 필요하면 `--install-browser` 를 붙입니다. 이미지 용량이 커지므로 기본은 꺼져 있습니다.

```bash
python3 deploy/aegisctl.py build --tag localhost/aegisos:latest --install-browser
```

`aegisctl` 없이 직접 빌드해도 결과는 같습니다.

```bash
sudo podman build -t localhost/aegisos:latest .
```

## 2. 레지스트리에 push (선택)

여러 머신에서 같은 이미지를 받아 쓰고 싶을 때 레지스트리에 올립니다.

AWS ECR 로 push 하는 경우(미리 `aws` CLI 로그인이 되어 있어야 합니다). `--create-repo` 를 주면 리포지토리가 없을 때 자동으로 만듭니다.

```bash
python3 deploy/aegisctl.py push \
  --registry ecr \
  --account-id 123456789012 \
  --region us-east-1 \
  --repo aegisos \
  --create-repo \
  --image localhost/aegisos:latest
```

사내 사설 레지스트리로 push 하는 경우:

```bash
echo "$REGISTRY_PASSWORD" | python3 deploy/aegisctl.py push \
  --registry registry.example.com:5000 \
  --username myuser --password-stdin \
  --image localhost/aegisos:latest
```

## 3. 디스크 이미지 만들기 (선택)

로컬에서 가상머신용 디스크(qcow2, raw)를 만들려면 `--type` 에 원하는 형식을 지정합니다. 쉼표로 여러 개를 한 번에 만들 수 있고, 결과물은 현재 디렉토리의 `./output` 에 생성됩니다.

```bash
python3 deploy/aegisctl.py disk --type qcow2,raw --image localhost/aegisos:latest
```

AWS AMI 를 만들려면 먼저 AWS 자격증명 파일을 준비합니다. 이 파일은 절대 git 에 커밋하지 마세요.

```bash
cat > aws.secrets <<'EOF'
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
EOF
```

그다음 AMI 빌드를 실행합니다. `--aws-ami-name`, `--aws-bucket`, `--aws-region` 은 반드시 함께 지정해야 하며, 지정한 S3 버킷은 해당 리전에 미리 존재해야 합니다(빌더가 만들어 주지 않습니다). 또한 계정에 vmimport 서비스 역할이 설정되어 있어야 합니다.

```bash
python3 deploy/aegisctl.py disk \
  --type ami \
  --aws-ami-name aegisos \
  --aws-bucket my-bootc-bucket \
  --aws-region us-east-1 \
  --env-file ./aws.secrets \
  --config deploy/config.example.toml \
  --image localhost/aegisos:latest
```

`--config` 로 넘기는 `config.example.toml` 에는 첫 부팅 시 만들 사용자 계정과 SSH 키를 적습니다. 실제 사용 전에 본인 SSH 공개키로 바꾸세요.

완료되면 AWS 콘솔의 EC2 > AMIs 메뉴에서 등록된 AMI 를 확인할 수 있습니다. 이 AMI 로 EC2 인스턴스를 띄우면 됩니다.

### 빌더 이미지 바꾸기 (`--builder-image`)와 RHEL 11 전환 안내

디스크 빌드에는 기본적으로 `quay.io/centos-bootc/bootc-image-builder:latest` 컨테이너를 사용합니다. 다른 빌더 이미지를 쓰고 싶으면 `--builder-image` 로 지정하거나, 환경변수 `AEGISCTL_BUILDER_IMAGE` 로 지정할 수 있습니다. 우선순위는 `--builder-image` 플래그 > 환경변수 > 기본값 순입니다.

```bash
# 플래그로 지정
python3 deploy/aegisctl.py disk --type qcow2 --image localhost/aegisos:latest \
  --builder-image quay.io/centos-bootc/bootc-image-builder:latest

# 환경변수로 지정
export AEGISCTL_BUILDER_IMAGE=quay.io/centos-bootc/bootc-image-builder:latest
python3 deploy/aegisctl.py disk --type qcow2 --image localhost/aegisos:latest
```

RHEL 11 부터는 `bootc-image-builder` 가 더 이상 제공되지 않고 `image-builder` 로 통합/대체될 예정입니다([공식 deprecation 공지](https://osbuild.org/docs/bootc/deprecation-notice/)). 그 시점이 오면 `--builder-image` 로 `image-builder` 컨테이너를 지정해 코드 수정 없이 전환할 수 있습니다. 다만 `image-builder` 의 `--type`/`--aws-*` 인자 호환성은 아직 검증되지 않았으므로, 전환할 때 해당 인자가 그대로 통하는지 다시 확인하세요.

> 참고: aegisctl 은 컨테이너 런타임으로 `podman` 만 사용합니다(docker 미지원). 디스크 빌드 도구가 rootful podman 을 요구하고, 로컬에서 빌드한 이미지를 변환할 때 podman 로컬 스토리지(`/var/lib/containers/storage`)를 직접 마운트해야 하기 때문입니다([Red Hat image-mode 문서](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_image_mode_for_rhel_to_build_deploy_and_manage_operating_systems/creating-bootc-compatible-base-disk-images-by-using-bootc-image-builder)). docker 는 스토리지 백엔드가 달라 이 방식이 통하지 않습니다.

## 4. 한 번에 (build -> push -> disk)

빌드부터 push, 디스크 생성까지 한 번에 돌리려면 `all` 을 씁니다. 각 단계 옵션을 그대로 받습니다. 한 단계라도 실패하면 거기서 멈춥니다.

```bash
python3 deploy/aegisctl.py all \
  --tag localhost/aegisos:latest \
  --registry ecr --account-id 123456789012 --region us-east-1 --repo aegisos --create-repo \
  --type qcow2
```

> 참고: 예전 `deploy/build_ami.py` 는 `aegisctl disk --type ami` 로 대체되었습니다. 호환을 위해 파일은 남겨 두었지만 새 작업에는 `aegisctl` 사용을 권장합니다.

## 5. 첫 부팅 후 설정

부팅 직후에는 아직 설정 파일이 없어서 Hermes 게이트웨이가 자동으로 뜨지 않습니다(설정 없이 무한 재시작하는 것을 막는 안전장치입니다). SSH 로 접속한 뒤 설정을 진행합니다.

```bash
# Hermes 데이터 위치를 알려줍니다.
export HERMES_HOME=/var/lib/hermes

# 설정 마법사를 실행해 config.yaml 과 API 키(.env)를 만듭니다.
hermes setup
```

`hermes setup` 으로 `/var/lib/hermes/config.yaml` 이 만들어지는 순간, 미리 켜 둔 감시 유닛(`hermes-gateway.path`)이 이를 감지해 게이트웨이(`hermes-gateway.service`)를 자동으로 기동합니다. 별도로 서비스를 시작할 필요가 없습니다.

상태 확인:

```bash
systemctl status hermes-gateway.service
journalctl -u hermes-gateway.service -f
```

## 데이터 보존

`/var/lib/hermes` 아래의 모든 데이터(설정, API 키, 세션, 로그, 스킬, 메모리 등)는 OS 업데이트와 롤백에도 보존됩니다. 백업이 필요하면 이 디렉토리를 백업하세요.

## 파일 구조

```
aegisos/
  Containerfile                  # 베이스 이미지 + 의존성 + Hermes 설치 + 유닛 배치
  systemd/
    hermes-gateway.service       # 게이트웨이 서비스(config 있을 때만 기동)
    hermes-gateway.path          # config.yaml 생성 감지 -> 서비스 자동 기동
  tmpfiles.d/
    hermes.conf                  # /var/lib/hermes 디렉토리 생성 보장
  deploy/
    aegisctl.py                  # 통합 배포 CLI(build/push/disk/all)
    build_ami.py                 # (deprecated) aegisctl disk --type ami 로 대체됨
    config.example.toml          # 디스크 이미지 빌드 커스터마이징 예시
  README.md
```

## 참고 링크

- Fedora Hummingbird: https://fedoramagazine.org/fedora-hummingbird-linux-taking-the-hummingbird-model-to-the-full-os/
- bootc 문서: https://bootc.dev/
- bootc-image-builder: https://github.com/osbuild/bootc-image-builder
- Hermes Agent: https://github.com/NousResearch/hermes-agent
