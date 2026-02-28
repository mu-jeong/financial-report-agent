# 🔑 API 연동 가이드 (API Setup Guide)

이 문서는 Finance LLM 프로젝트를 실행하기 위해 필요한 **Google Gemini API 키 발급** 및 **환경 변수(.env) 설정 방법**을 안내합니다.

## 1. Google Gemini API 키 발급 방법

이 프로젝트는 텍스트 임베딩(Vector 변환)과 질의응답(RAG) 텍스트 생성을 위해 모두 Google의 Gemini 모델을 사용합니다. 무료 구간 내에서 훌륭한 성능을 제공합니다.

1. **Google AI Studio 접속:**
   - 브라우저를 열고 [Google AI Studio (aistudio.google.com)](https://aistudio.google.com/) 로 이동합니다.
   - 구글 계정으로 로그인합니다.
2. **API 키 생성:**
   - 좌측 메뉴판에서 **"Get API key"** (또는 API 키 발급) 항목을 클릭합니다.
   - **"Create API key"** 버튼을 누릅니다.
   - 프로젝트를 선택하라는 창이 나오면 기존 프로젝트를 선택하거나, 새로운 프로젝트 생성을 선택합니다.
   - 생성된 영문자와 숫자로 이루어진 긴 문자열이 바로 API Key입니다. **이 키는 외부에 유출되지 않도록 주의하세요.**

---

## 2. 프로젝트에서 환경 변수 설정하기 (.env 파일)

소스 코드 내에 API 키를 직접 적는 것은 보안상 매우 위험합니다(깃허브 등에 올릴 경우 특히). 따라서 환경 변수를 외부 파일인 `.env`로 빼서 관리합니다.

1. **`.env` 파일 생성:**
   프로젝트의 최상위 루트 경로(README.md 파일이 있는 곳)에 `.env` 라는 이름의 새 파일을 만듭니다. (확장자 없음)
   
   이전에 이미 제공된 템플릿 파일이 있다면 복사해서 사용할 수 있습니다.
   ```bash
   # Linux/macOS
   cp .env.example .env
   
   # Windows (cmd)
   copy .env.example .env
   ```

2. **API 키 입력:**
   생성된 `.env` 파일을 메모장이나 VS Code로 엽니다. 아까 발급받은 API 키를 복사하여 아래 형식으로 붙여넣습니다. (따옴표는 쓰지 마세요)

   ```env
   GEMINI_API_KEY=AIzaSyBxxxxxxx_xxxxxxxxxxxxxxxxxxxxxx
   ```

3. **자동 인식:**
   파이썬 코드 내의 `src/configs/config.py` 에서 `load_dotenv()` 함수를 호출하므로, 프로그램 실행 시 자동으로 이 `.env` 파일을 읽어들여 안전하게 API 키를 사용합니다.

---