# OpenRouter API 키 발급 방법

Finance LLM을 실행하려면 OpenRouter API 키와 OpenRouter 크레딧이 필요합니다. API 키는 답변 생성, 질문 재작성, SQL 생성, 임베딩 생성, 선택형 rerank 호출에 사용되고, 크레딧은 유료 모델 호출 비용을 결제하는 잔액입니다.

## 1. OpenRouter 접속

- OpenRouter: <https://openrouter.ai>
- API Keys: <https://openrouter.ai/settings/keys>
- Credits / Billing: <https://openrouter.ai/settings/credits>
- Activity: <https://openrouter.ai/activity>
- Models: <https://openrouter.ai/models>

## 2. 로그인 또는 가입

OpenRouter 계정으로 로그인합니다. 계정이 없다면 화면 안내에 따라 가입합니다.

## 3. 크레딧 충전

Finance LLM의 기본 모델은 무료 모델이 아닐 수 있으므로, API 키만 만들고 크레딧을 충전하지 않으면 실행 중 잔액 부족 오류가 날 수 있습니다.

권장 순서:

1. `Credits`, `Billing`, 또는 `Settings > Credits` 화면으로 이동합니다.
2. `Add Credits` 버튼으로 소액을 먼저 충전합니다.
3. 결제 후 Credits 화면에서 잔액이 반영되었는지 확인합니다.
4. 예상치 못한 비용을 막기 위해 처음에는 API key credit limit을 작게 설정하는 것을 권장합니다.

Quick Start는 기본적으로 rerank를 끄지만, 임베딩과 답변 생성에는 OpenRouter 호출이 필요합니다.

## 4. API 키 만들기

API Keys 화면에서 새 키를 생성합니다. 이름은 나중에 알아보기 쉽게 지정하세요.

예:

```text
finance_llm_local
```

OpenRouter 화면에서 키별 credit limit 설정을 제공하면 처음에는 작은 한도를 지정하는 것을 권장합니다. 실수로 많은 요청이 발생했을 때 비용을 줄이는 데 도움이 됩니다.

## 5. API 키 복사

생성된 API 키를 즉시 복사합니다.

주의: API 키는 생성 직후 한 번만 전체가 표시될 수 있습니다. 바로 복사해 안전한 곳에 보관하세요.

## 6. Quick Start에 입력

`RUN_QUICKSTART.bat`을 실행하면 다음과 같은 API 키 입력 안내가 나옵니다.

```text
OpenRouter API 키를 붙여넣고 Enter를 누르세요:
```

복사한 API 키를 붙여넣고 Enter를 누르면 `.env` 파일의 `OPENROUTER_API_KEY`에 자동 저장됩니다.

## 7. 직접 `.env`에 입력

수동으로 설정할 때는 프로젝트 루트의 `.env`에 아래 값을 추가하거나 수정합니다.

```env
OPENROUTER_API_KEY=sk-or-...
```

다른 모델/검색 설정은 [`API_SETUP.md`](API_SETUP.md)를 참고하세요.

## 8. 잔액과 사용량 확인

OpenRouter Activity 또는 Credits 화면에서 사용량과 잔액을 확인할 수 있습니다.

- Credits / Billing: <https://openrouter.ai/settings/credits>
- Activity: <https://openrouter.ai/activity>
- Models and pricing: <https://openrouter.ai/models>

개발자용 조회 API도 있습니다. 일반 API 키는 `GET /api/v1/key`로 해당 키의 사용량과 한도를 조회할 수 있습니다. `GET /api/v1/credits`는 Management API key가 필요하며 계정의 총 구매 크레딧과 사용량을 반환합니다.

## 9. 비용 주의사항

- Finance LLM은 임베딩과 답변 생성에 OpenRouter API를 호출하므로 크레딧이 차감될 수 있습니다.
- Quick Start는 기본적으로 `USE_RERANKER=false`로 실행해 추가 rerank 비용을 줄입니다.
- 처음에는 작은 금액만 충전하고, 사용량을 확인한 뒤 필요할 때 추가 충전하세요.
- 모델별 가격과 제공 여부는 바뀔 수 있으므로 OpenRouter Models 페이지에서 현재 정보를 확인하세요.

## 10. 보안 주의사항

- API 키를 다른 사람에게 공유하지 마세요.
- `.env` 파일에는 실제 API 키가 들어가므로 Git에 올리거나 타인에게 공유하지 마세요.
- API 키가 노출되었다고 생각되면 OpenRouter API Keys 화면에서 해당 키를 삭제하고 새 키를 만드세요.
- 공개 저장소나 이슈, 채팅, 스크린샷에 API 키가 포함되지 않도록 주의하세요.
