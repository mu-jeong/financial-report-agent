# OpenRouter API 키 발급 방법

Finance LLM을 실행하려면 OpenRouter API 키와 OpenRouter 크레딧이 필요합니다. API 키는 답변 생성, 임베딩 생성, 선택형 rerank 호출에 사용되고, 크레딧은 유료 모델 호출 비용을 결제하는 잔액입니다.

## 1. OpenRouter 접속

아래 주소로 이동합니다.

- OpenRouter: <https://openrouter.ai>
- Credits / Billing 직접 링크: <https://openrouter.ai/settings/credits>
- API Keys 직접 링크: <https://openrouter.ai/settings/keys>

## 2. 로그인

OpenRouter 계정으로 로그인합니다. 계정이 없다면 화면 안내에 따라 가입합니다.

## 3. 크레딧 충전

Finance LLM의 기본 모델은 무료 모델이 아니므로, API 키만 만들고 크레딧을 충전하지 않으면 실행 중 잔액 부족 오류가 날 수 있습니다.

1. 로그인 후 `Credits`, `Billing`, 또는 `Settings > Credits` 화면으로 이동합니다.
2. 직접 링크를 사용할 수도 있습니다.

   ```text
   https://openrouter.ai/settings/credits
   ```

3. `Add Credits` 버튼을 누릅니다.
4. 처음에는 작은 금액만 충전해 테스트하는 것을 권장합니다.(1달러만 있어도 충분히 기능 테스트가 가능하지만 수수료가 0.8달러로 고정이기 때문에, 참고하셔서 적당량 충전을 권장드립니다, 5월에 발간된 리포트를 정리하고 기능테스트를 해도 약 0.2달러 정도를 사용했습니다.)
5. 필요하면 Auto Top Up을 설정할 수 있지만, 예상치 못한 자동 결제를 막으려면 처음에는 수동 충전을 권장합니다.
6. 결제 후 Credits 화면에서 잔액이 반영되었는지 확인합니다.

OpenRouter는 달러(USD) 기준 크레딧 시스템을 사용합니다. OpenRouter FAQ에 따르면 사용자는 잔액을 수동으로 충전하거나, 잔액이 기준 이하로 내려갔을 때 자동 충전되도록 설정할 수 있습니다.

## 4. API Keys 화면 열기

크레딧 충전 후 `Settings` 또는 `API Keys` 메뉴로 이동합니다.

직접 링크를 사용할 수도 있습니다.

```text
https://openrouter.ai/settings/keys
```

## 5. 새 API 키 만들기

`Create Key`, `New Key`, 또는 유사한 새 키 생성 버튼을 누릅니다.

키 이름은 알아보기 쉽게 지정하는 것을 권장합니다.

예:

```text
finance_llm_quickstart
```

OpenRouter 화면에서 credit limit 설정을 제공하면, 처음에는 작은 한도를 설정해 비용을 제한하는 것을 권장합니다. API 키별 한도를 걸어두면 실수로 많은 요청이 발생했을 때 비용을 줄이는 데 도움이 됩니다.

## 6. API 키 복사

생성된 API 키를 복사합니다.

주의: API 키는 생성 직후 한 번만 전체가 표시될 수 있습니다. 바로 복사해 안전한 곳에 보관하세요.

## 7. Quick Start에 붙여넣기

`RUN_QUICKSTART.bat`을 실행하면 다음과 같은 메시지가 나옵니다.

```text
OpenRouter API 키를 붙여넣고 Enter를 누르세요:
```

복사한 API 키를 붙여넣고 Enter를 누르면 `.env` 파일에 자동 저장됩니다.

## 8. 잔액과 사용량 확인

OpenRouter 웹 화면에서 Credits 또는 Activity 화면을 확인하세요.

- Credits / Billing: <https://openrouter.ai/settings/credits>
- Activity: <https://openrouter.ai/activity>
- Models and pricing: <https://openrouter.ai/models>

개발자용으로는 OpenRouter의 credits API도 있습니다. OpenRouter 문서에 따르면 `/api/v1/credits`는 구매한 총 크레딧과 사용량을 반환합니다.

## 9. 비용 주의사항

- Finance LLM은 임베딩과 답변 생성에 OpenRouter API를 호출하므로 크레딧이 차감될 수 있습니다.
- Quick Start는 기본적으로 `USE_RERANKER=false`로 실행해 추가 rerank 비용을 줄입니다.
- 처음에는 작은 금액만 충전하고, 사용량을 확인한 뒤 필요할 때 추가 충전하세요.
- 모델별 가격은 바뀔 수 있으므로 OpenRouter Models 페이지에서 현재 가격을 확인하세요.

## 10. 보안 주의사항

- API 키를 다른 사람에게 공유하지 마세요.
- `.env` 파일에는 실제 API 키가 들어가므로 Git에 올리거나 타인에게 공유하지 마세요.
- API 키가 노출되었다고 생각되면 OpenRouter API Keys 화면에서 해당 키를 삭제하고 새 키를 만드세요.
- OpenRouter 공식 문서도 환경 변수 등 안전한 저장 방식을 권장합니다.