# Supabase keepalive 운영 절차

## 목적과 보안 경계

이 자동화는 Supabase Free 프로젝트의 장기 비활성에 따른 일시 정지 가능성을 낮추기 위한 best-effort 운영 보조 수단이다. GitHub Actions가 12시간마다 전용 `supabase-health-check` Edge Function을 호출하고, 함수가 읽기 전용 `project_healthcheck_v1()` RPC를 한 번 실행해 실제 PostgreSQL 연결을 확인한다. uptime이나 일시 정지 방지를 보장하는 장치는 아니다.

GitHub에는 Supabase secret/service-role 키를 저장하지 않는다. GitHub가 보관하는 `SUPABASE_HEALTHCHECK_TOKEN`은 이 전용 함수만 호출할 수 있는 목적 제한 토큰이다. 토큰은 URL, 저장소 파일, 커밋, 로그 또는 명령 출력에 넣지 않는다. 기존 `issue-report-ingest`는 신고 데이터와 rate-limit 상태를 변경하므로 keepalive에 사용하지 않는다.

## 최초 설정과 배포

전제 조건은 hosted Supabase 프로젝트, 해당 프로젝트에 연결된 Supabase CLI, 저장소 설정을 변경할 권한이다. 환경마다 서로 다른 토큰을 사용한다.

1. 암호학적으로 안전한 난수 생성기로 최소 32바이트 토큰을 만든다. 생성 명령의 결과를 터미널 기록, 채팅 또는 이 문서에 붙여 넣지 않는다.
2. 같은 값을 Supabase Edge Function secret `HEALTHCHECK_TOKEN`과 GitHub Actions repository secret `SUPABASE_HEALTHCHECK_TOKEN`에 각각 등록한다.
3. migration을 먼저 적용한 뒤 Edge Function을 배포한다.

```powershell
supabase login
supabase link --project-ref <project-ref>
supabase db push
supabase functions deploy supabase-health-check --use-api
```

Supabase Dashboard의 **Edge Functions → Secrets**에서 `HEALTHCHECK_TOKEN`을 등록하고, GitHub 웹 UI의 **Settings → Secrets and variables → Actions → Secrets**에서 같은 값을 `SUPABASE_HEALTHCHECK_TOKEN`으로 등록한다. 실제 값을 `supabase secrets set NAME=VALUE` 형태의 CLI 인자에 넣으면 shell history에 남을 수 있으므로 이 문서에서는 사용하지 않는다. 조직에서 승인한 secret manager 절차가 있다면 그 절차를 우선한다.

프로젝트 URL `https://rjjnhvoontxpimhiabou.supabase.co`는 워크플로에 고정되어 있으므로 URL variable을 만들지 않는다. 이 고정값은 토큰이 다른 Supabase tenant나 임의 host로 전송되는 것을 막는다. migration 적용과 Function 배포가 성공하기 전에 워크플로 성공을 기대하면 안 된다.

## 예약 실행과 수동 검증

워크플로 `.github/workflows/supabase-health-check.yml`은 checkout이나 패키지 설치 없이 `curl`만 사용한다. 예약식 `17 */12 * * *`은 UTC 기준 매일 00:17와 12:17, 한국 표준시 기준 09:17와 21:17 실행을 요청한다. GitHub 부하에 따라 실제 시작은 늦어질 수 있다.

배포 직후 GitHub의 **Actions → Supabase health check → Run workflow**에서 `main` 브랜치를 선택해 수동 실행한다. 다음을 확인한다.

- job이 5분 안에 성공하고 로그에는 일반 성공 메시지만 남는다.
- 잘못된 토큰, 비-2xx 응답, DNS/네트워크 오류 또는 요청 timeout은 job을 실패시킨다.
- Function invocation 로그에서 RPC가 성공했는지 확인하되 요청 헤더, 토큰 또는 원문 DB 오류가 기록되지 않았는지 점검한다.
- 배포 후 24시간 동안 수동 실행 1회와 예약 실행 2회의 성공 기록을 확인한다.
- 이슈 신고 테이블과 rate-limit 카운터에 keepalive가 만든 행 또는 변경이 없는지 확인한다.

워크플로는 고정된 프로젝트 endpoint만 호출하고 `curl --proto '=https'`로 HTTPS만 허용한 뒤 최초 호출 뒤 최대 2회 재시도한다. `curl --max-time 30`은 전체 실행이 아니라 각 시도의 최대 전송 시간이고, 재시도 시작 허용 창은 `--retry-max-time 90`으로 제한한다. GitHub job 자체의 최종 상한은 5분이다. HTTP 응답 본문은 Actions 로그에 출력하지 않으며, 응답 헤더도 출력하지 않는다. 실패 원인 조사는 GitHub의 일반 curl 오류와 민감정보를 제외한 Supabase Function 로그를 함께 사용한다.

## 토큰 회전

노출이 의심되거나 정기 회전할 때는 두 시스템을 짧은 유지보수 구간에서 함께 갱신한다.

1. 새 최소 32바이트 토큰을 생성한다.
2. Supabase의 `HEALTHCHECK_TOKEN`을 새 값으로 교체한다.
3. 즉시 GitHub의 `SUPABASE_HEALTHCHECK_TOKEN`을 같은 새 값으로 교체한다.
4. 수동 dispatch가 성공하는지 확인한다.
5. 이전 토큰이 더 이상 성공하지 않는지 별도의 안전한 호출 환경에서 확인하고 이전 값을 폐기한다.

두 값이 다른 동안 예약 실행은 `401`로 실패한다. 토큰이 Actions 로그, URL, 커밋 또는 다른 저장소 파일에 노출되었다면 먼저 즉시 교체하고 노출된 실행 로그와 복제본을 조직의 보안 절차에 따라 처리한다. Git 기록을 파괴적으로 다시 쓰는 작업은 별도 승인 없이 수행하지 않는다.

## 중지와 장애 대응

자동 호출을 중지하려면 GitHub Actions에서 워크플로를 비활성화한다. DB 또는 함수 장애가 해결된 뒤 수동 dispatch로 정상화를 확인하고 다시 활성화한다. 프로젝트를 교체하려면 migration, Function, Supabase secret을 새 프로젝트에 먼저 배포하고 코드 리뷰를 거쳐 워크플로의 고정 endpoint를 변경한다. endpoint와 token secret의 대상 프로젝트가 일치하는지 수동 dispatch로 검증한 뒤 예약 실행을 다시 활성화한다.

공개 저장소에서는 60일 동안 저장소 활동이 없으면 GitHub가 예약 워크플로를 자동으로 비활성화할 수 있다. Actions 화면에서 예약 워크플로 활성 상태를 정기적으로 점검하고, 비활성화되었다면 원인을 확인한 뒤 다시 활성화한다. 장기 무관리 운영이나 일시 정지 방지 보장이 필요하면 이 GitHub Actions 방식만 사용하지 말고 Supabase 유료 요금제 또는 별도 관리형 모니터링을 선택한다.

## 완료 기준

운영 설정은 다음 조건을 모두 만족할 때 완료로 본다.

- GitHub에는 목적 제한 토큰 외 Supabase secret/service-role 키가 없다.
- 수동 dispatch가 HTTP `200`과 실제 DB RPC 성공으로 끝난다.
- 잘못된 토큰과 DB 장애가 Actions 실패로 나타난다.
- 배포 후 예약 실행 2회가 성공한다.
- 신고 및 rate-limit 데이터에 keepalive로 인한 쓰기가 없다.

로컬 파일 추가만으로는 hosted 설정과 배포가 완료된 것이 아니다. 위 원격 단계는 인증된 운영자가 수행하고 증거를 확인해야 한다.

## 참고

- [Supabase Free 프로젝트 일시 정지](https://supabase.com/docs/guides/platform/free-project-pausing)
- [Supabase Edge Function secrets](https://supabase.com/docs/guides/functions/secrets)
- [GitHub 예약 워크플로 제약](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
