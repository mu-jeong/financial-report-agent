# 의사결정 검토안 0001: 로컬 권위와 원격 관측 경계

| 항목 | 값 |
| --- | --- |
| 상태 | 운영판 검토안 · 미승인 |
| 작성일 | 2026-08-08 |
| 검토 책임자 | 기획자 / 단일 운영자 |
| 기술 검토자 | 지정 필요 |
| 관련 요구사항 | PROD-001~006, DATA-001~004, OBS-001~005 |

> 현재 PoC/MVP 저장소의 구조 결정이 아니다. 별도로 진행할 운영판에서 채택할지 검토하기 위한 자료이며, 운영판 저장소에서 승인되기 전에는 ADR이나 구현 계약으로 간주하지 않는다.

## 배경

애플리케이션은 로컬 SQLite/FAISS와 로컬 문서 산출물에 기반해 검색·답변한다. 앞으로 설치본은 불특정 다수에게 배포되지만 production 운영, 이슈 triage, 평가와 릴리스 승격은 한 명의 운영자가 담당한다.

원격 관측과 이슈 수집을 위해 Supabase를 사용하려 한다. 공개 바이너리에 포함된 값은 추출·변조할 수 있으므로 사용자 설치본을 신뢰할 수 있는 backend나 operator로 취급할 수 없다. 동시에 단순 운영 이벤트 POST를 위해 모든 사용자의 계정 생성이나 device credential 발급을 강제하는 것은 초기 제품 범위에 비해 무겁다.

## 검토안

1. 로컬 SQLite/FAISS/artifact를 검색·답변 데이터의 권위 원본으로 유지한다.
2. Supabase는 application telemetry, issue, evaluation summary, release registry를 위한 보조 control plane으로 사용한다.
3. 공개 클라이언트의 원격 전송은 local save 이후 bounded outbox에서 비동기로 수행하며, 원격 장애가 핵심 사용자 흐름을 실패시키지 않는다.
4. 공개 설치본은 Supabase application table을 직접 읽거나 쓰지 않고 Edge Function만 호출한다.
5. 클라이언트에는 project URL과 publishable key만 둘 수 있다. publishable key는 공개 application/project API key이며 user/device/genuine installation 신원을 증명하지 않는다. secret/service-role/DB credential은 서버 secret store에만 둔다.
6. 초기 event/issue POST는 user login 또는 device credential 없이 허용한다. 권장 설정은 `verify_jwt=false`로 배포한 Function handler가 `auth: 'publishable:<name>'` 모드로 `apikey`를 검증하는 것이다. 완전 공개 `auth: 'none'`은 별도 보안 결정으로 남긴다. 두 방식 모두 모든 입력을 `trust_level=anonymous`로 처리하며 schema/size/rate/replay/software-release/redaction gate를 통과해야 한다.
7. `installation_id`는 resettable random UUID이며 인증이나 genuine-app 증명이 아니다.
8. 운영자 dashboard, release 등록, triage, retention 변경은 별도의 Supabase Auth/allowlist/RLS 경계를 사용한다.
9. 원격 데이터는 자동으로 evaluation expectation, prompt/model/config, active local revision을 변경하지 않는다.
10. attachment는 metadata-only ingest가 안정된 이후 private Storage와 검증된 upload capability를 갖춘 별도 단계로 도입한다. Supabase signed upload URL의 현재 2시간 bearer lifetime보다 짧은 보장이 필요하면 Function proxy 또는 별도 one-time ticket을 사용한다.
11. public endpoint의 evaluation observation은 non-qualifying telemetry다. canonical evaluation artifact와 promotion evidence는 authenticated operator/release-tool endpoint로만 등록한다.

## 예상 영향

### 기대 효과

- Supabase 장애와 로컬 핵심 기능의 failure domain이 분리된다.
- 기존 local-first retrieval architecture를 유지하면서 다수 설치본의 software/local-runtime 조합별 문제를 볼 수 있다.
- 공개 바이너리에 privileged credential을 배포하지 않는다.
- 사용자 계정 없이도 낮은 마찰로 운영 이벤트와 명시적 이슈를 받을 수 있다.

### 비용과 제약

- anonymous public endpoint 악용을 완전히 방지할 수 없다.
- rate limit, quota, idempotency, quarantine, kill switch와 비용 감시를 애플리케이션이 소유해야 한다.
- at-least-once 전송과 retention을 위한 local outbox/migration이 추가된다.
- remote row는 신뢰 가능한 사실이 아니라 검증 전 claim이므로 operator triage가 필요하다.
- issue 상태를 local/remote로 구분해 사용자에게 설명해야 한다.

## 보안상 필수 조건

- public role의 direct table CRUD denial test
- secret scan of packaged binary/artifact
- unique event ID와 replay window
- IP/installation/software-release/endpoint 복합 quota
- Supabase Auth rate limit과 별개인 application-owned inbound rate limiter
- known SoftwareReleaseManifest registry와 unknown-software quarantine
- allowlist serializer + server-side validation
- body/batch/string/depth/attachment limit
- operator-only read/triage/delete/promotion
- remote kill switch와 cost cap

위 항목이 검증되지 않으면 공개 rollout을 승인하지 않는다.

## 채택하지 않는 대안

### Supabase table에 클라이언트가 직접 insert

RLS로 제한할 수 있어도 public schema와 write policy가 공격 표면이 되고 validation, rate limit, redaction, quarantine을 여러 곳에 분산시킨다. 공개 write는 Edge Function 하나로 통합한다.

### Service-role/secret key를 앱에 포함

공개 실행 파일에서 비밀을 보호할 수 없고 RLS를 우회할 수 있으므로 거부한다.

### 모든 사용자에게 계정/JWT 요구

계정이 제품 기능에 필요하지 않은 현재 범위에서는 UX와 운영 복잡도가 과하다. operator 인증만 필수로 하고 public input은 untrusted로 제한한다.

### 설치 UUID를 device credential로 사용

복제·재생성·위조 가능하므로 인증이 아니다. rate-limit 보조 dimension으로만 사용한다.

### Supabase를 검색 데이터의 원본으로 전환

핵심 서비스 구조와 failure domain을 크게 바꾸며 이번 production 기획 범위를 벗어난다.

### Logs Explorer에 desktop log를 저장

Logs Explorer는 Supabase 인프라 로그 조회 기능이며 application event의 canonical store가 아니다. 구조화된 application table을 사용한다.

## 재검토 조건

다음 중 하나가 발생하면 운영판 착수 전에 검토안을 다시 논의한다. 운영판에서 이미 승인된 뒤라면 새 ADR로 이전 결정을 대체한다.

- anonymous endpoint 악용이 정상 사용을 방해하거나 비용 한도를 반복 초과
- 설치 단위 revocation/quota가 IP/behavior limit보다 효과적이라는 증거
- 사용자 계정이 실제 제품 기능으로 도입됨
- 중앙에서 로컬 데이터/구성을 관리해야 하는 명시적 제품 요구
- 다중 운영자, 조직, tenant 분리가 필요해짐

## 승인 전 확인 사항

- Supabase timeout/429/5xx에도 기존 model-provider 기반 chat 경로는 영향을 받지 않으며, bounded issue payload는 retry-only outbox에서 재시도된다.
- 공개 클라이언트 credential로 table read/write가 모두 거부된다.
- unknown software manifest/replay/oversize/malformed 요청이 정상 지표에 들어가지 않으며, valid unknown local runtime은 `unverified_claim`으로 분리된다.
- packaged application에서 privileged secret이 탐지되지 않는다.
- anonymous issue가 operator 승인 없이 evaluation candidate로 승격되지 않는다.
