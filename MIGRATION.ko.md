# MR2 글로벌 업데이트 마이그레이션 작업 공간

[English](MIGRATION.md)

이 로컬 저장소는 기존 이력을 보존하면서 Orin NX 로버의 Ubuntu 24.04 / ROS 2 Jazzy / Gazebo Harmonic 전환을 준비합니다. Jazzy 포팅 완료본이나 실제 배포 릴리스는 아닙니다.

- 잠정 기준: upstream 기본 브랜치 `2026`의 조사 커밋 `74422f36c55140519f111cf6709f1725711b536a`.
- 기본 브랜치: `main`; 로컬 기준 태그: `audit/2026-11-14`.
- `upstream`의 fetch 주소는 `https://github.com/URC-kaist/mr2-stack.git`이며 로컬 push URL은 비활성화했습니다. `origin`은 `https://github.com/URC-kaist/mr2-stack-jazzy.git`입니다.
- GitHub fork와 [공유 Project](https://github.com/orgs/URC-kaist/projects/2)를 생성했습니다. Project는 조직의 비공개 기본 설정을 유지하며 [착수 이슈](https://github.com/URC-kaist/mr2-stack-jazzy/issues)의 담당자는 아직 배정하지 않았습니다.
- 소스·서브모듈 이력을 보존합니다. 이번 준비에서는 애플리케이션 포팅, 의존성 설치, 장치 업데이트를 수행하지 않았습니다.

워크스테이션에서는 병렬 개발하고, 사용 가능함이 확인된 재설정 가능한 Orin Nano에서는 후보 환경을 네이티브로 실험합니다. 예정된 검증 시간 외에는 NX 로버의 기존 운용 환경을 유지합니다. Nano 결과가 NX 인코더, 캐리어, 실제 운용 부하, 필드 인수를 입증하지는 않습니다.

영문 원문과 한국어 문서는 `~/docs/content/docs/global-update-project/`에 준비되어 있습니다. Project 구성 초안, 양언어 이슈 초안, 저장소 계획은 `~/docs/project-drafts/global-update/`에 있습니다. 실제 공유 시에는 로컬 경로를 게시된 링크로 바꿉니다.

SW 팀장 `@yongwhan-chung`이 인력과 투입 여력을 정합니다. 요청자는 Yongwhan에게 Nano 인수를 요청했으며, 이 문서는 알림이나 담당자 배정을 보내지 않습니다. 실제 전환은 HW/SW/운용 공동 검증과 팀의 릴리스 결정에 따릅니다.

분야별 포팅을 합치기 전에 실제 NX 운용 기준, 필수 릴리스 프로파일, rover-direct 작업 반영을 기록합니다. API 비호환성 때문에 구현·설정을 분리할 수 있지만, 이 저장소가 전면 재작성을 뜻하지는 않습니다.

기존 빌드·배포 안내는 마이그레이션 과정에서 명시적으로 고치기 전까지 과거 플랫폼을 설명합니다. 후보 의존성은 격리된 개발 환경에서 구성하며, 운용 NX에서 실험하지 않습니다.
