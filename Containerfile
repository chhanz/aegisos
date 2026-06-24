# AegisOS - Fedora Hummingbird + Hermes Agent 통합 bootc 이미지
#
# 빌드 (단순 컨테이너 빌드):
#   sudo podman build -t localhost/aegisos:latest .
#   (또는: python3 deploy/aegisctl.py build)
#
# browser(playwright/chromium) 포함 빌드:
#   sudo podman build --build-arg INSTALL_BROWSER=true -t localhost/aegisos:latest .
#
# 설계 근거는 DESIGN-PLAN.md 참조.

# --- 베이스 이미지 ---
# Fedora Hummingbird 공식 bootc 이미지.
# 확인: quay.io API 로 hummingbird-community/bootc-os 저장소 및 latest 태그(multi-arch) 실재 확인.
#   - https://quay.io/api/v1/repository/hummingbird-community/bootc-os/tag/
#   - https://fedoramagazine.org/fedora-hummingbird-linux-taking-the-hummingbird-model-to-the-full-os/
#   - https://discussion.fedoraproject.org/t/fedora-hummingbird-taking-the-hummingbird-model-to-the-full-operating-system/191184
#     (메인테이너가 hummingbird-ci -> hummingbird-community 로 정정함)
# Hummingbird 는 롤링 릴리스 모델이므로 :latest 를 사용한다.
FROM quay.io/hummingbird-community/bootc-os:latest

# --- 빌드 인자 ---
# browser(playwright/chromium) 설치 토글. 기본은 이미지 비대 방지를 위해 끔.
ARG INSTALL_BROWSER=false

# Hermes 데이터 디렉토리. bootc 의 영구 쓰기 영역인 /var 아래에 둔다.
ARG HERMES_HOME=/var/lib/hermes

# --- 시스템 의존성 설치 ---
# bootc(image-mode)에서 dnf install 후 동일 RUN 레이어에서 dnf clean all 로 캐시 정리.
# 확인: bootc 공식 가이드 "RUN $pkgsystem install somepackage && $pkgsystem clean all"
#   - https://bootc.dev/bootc/building/guidance.html
#
# nodejs 를 빌드타임에 선설치하는 이유: install.sh 가 시스템 node 가 충분하지 않으면
# HERMES_HOME(=/var) 아래에 Hermes-managed Node 를 설치한다. 그러면 런타임 의존성이
# 데이터 영역에 깔리는 함정이 생긴다. 시스템 node 를 미리 깔아 회피한다.
# Hermes 의 node 요구사항: ^20.19 || >=22.12 (install.sh node_satisfies_build()).
# Fedora 의 nodejs 패키지는 22 이상을 제공한다.
RUN dnf install -y \
        nodejs \
        npm \
        git \
        ripgrep \
        ffmpeg-free \
        python3 \
    && node --version \
    && dnf clean all

# --- Hermes Agent 설치 ---
# install.sh 를 비대화형으로 실행한다.
#   - HERMES_HOME=/var/lib/hermes  -> 데이터(config.yaml, .env, sessions, logs ...)는 /var 로
#   - 코드는 root+Linux FHS 레이아웃에 따라 /usr/local/lib/hermes-agent (불변 /usr) 로 감
#   - 명령은 /usr/local/bin/hermes 심링크
#   - uv-managed Python 은 /usr/local/share/uv 로
#   - --skip-setup 으로 config/.env 를 굽지 않음(비밀은 부팅 후 주입)
#   - --skip-browser 로 playwright/chromium 건너뜀(INSTALL_BROWSER=true 면 설치)
# 확인: install.sh 실독 - 플래그(--non-interactive/--skip-setup/--skip-browser/--hermes-home) 및
#       gateway 서브커맨드 존재 확인.
#   - https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh
#
# 주의: 빌드타임에 /var/lib/hermes 에 쓴 내용은 bootc 에서 "첫 부팅 시드"로만 취급되고
#       이후 런타임 /var 와는 분리 보존된다(VOLUME /var 시맨틱, "unpacked only from the
#       initial image"). 그래서 코드를 /var 가 아닌 /usr/local 에 두는 것이 중요하다.
#   - https://bootc.dev/bootc/filesystem.html
ENV HERMES_HOME=${HERMES_HOME}
RUN set -eux; \
    EXTRA_FLAGS="--skip-browser"; \
    if [ "${INSTALL_BROWSER}" = "true" ]; then EXTRA_FLAGS=""; fi; \
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
        -o /tmp/hermes-install.sh; \
    HERMES_HOME="${HERMES_HOME}" bash /tmp/hermes-install.sh \
        --non-interactive \
        --skip-setup \
        ${EXTRA_FLAGS} \
        --hermes-home "${HERMES_HOME}"; \
    rm -f /tmp/hermes-install.sh; \
    /usr/local/bin/hermes --version || true

# --- systemd 유닛 / tmpfiles.d 배치 ---
# tmpfiles.d 로 /var/lib/hermes 를 첫 부팅 시 생성 보장.
# 확인: bootc 가이드가 /var 하위 디렉토리 사전 생성에 tmpfiles.d 사용을 권장
#   ("recommended to use systemd tmpfiles.d for this").
#   - https://bootc.dev/bootc/filesystem.html
COPY tmpfiles.d/hermes.conf /usr/lib/tmpfiles.d/hermes.conf
COPY systemd/hermes-gateway.service /usr/lib/systemd/system/hermes-gateway.service
COPY systemd/hermes-gateway.path /usr/lib/systemd/system/hermes-gateway.path

# .path 유닛을 enable 하여 부팅 시 config.yaml 생성을 감시하게 한다.
# config.yaml 이 생기는 순간 hermes-gateway.service 가 자동 기동된다.
RUN systemctl enable hermes-gateway.path

# bootc 컨테이너 린트(있으면 tmpfiles.d 누락 등 검사). 실패해도 빌드는 막지 않음.
RUN bootc container lint || true
