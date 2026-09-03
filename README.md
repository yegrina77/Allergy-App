# 알러지 매니저 (Allergy Manager)

레스토랑 재료 성분표 사진을 AI로 분석해 메뉴별 알러지원을 자동 관리하는 웹앱.

## 로컬에서 실행하기

```bash
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export SECRET_KEY=아무-랜덤-문자열
python app.py
```

브라우저에서 http://localhost:5000 접속.

## 구조

```
backend/app.py       Flask 서버 + SQLite DB + Claude API 연동
backend/requirements.txt
frontend/index.html  오너/관리자용 대시보드 (로그인, 재료/메뉴 관리, 감사 로그)
frontend/staff.html  직원용 공개 조회 화면 (로그인 불필요)
```

## 배포 (Render)

README 대신 대화에서 안내받은 단계를 따르세요. 필요한 환경변수:
- `ANTHROPIC_API_KEY`
- `SECRET_KEY`
- `FLASK_ENV=production`
