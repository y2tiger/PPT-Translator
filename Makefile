.PHONY: help dev docker clean logs test

help:
	@echo "PPT Translator - 사용 가능한 명령어"
	@echo ""
	@echo "  make dev       - Docker로 로컬 개발 서버 실행 (hot reload)"
	@echo "  make docker    - Docker 이미지 빌드 후 실행"
	@echo "  make logs      - Docker 로그 확인"
	@echo "  make stop      - Docker 컨테이너 중지"
	@echo "  make clean     - 임시 파일 및 캐시 정리"
	@echo "  make venv      - Python venv로 실행 (LibreOffice 필요)"
	@echo ""

# Docker 개발 모드 (코드 변경 시 자동 리로드)
dev:
	@if [ ! -f .env ]; then cp .env.example .env; echo "⚠️  .env 파일 생성됨. OPENAI_API_KEY를 설정하세요!"; exit 1; fi
	docker-compose up --build

# Docker 프로덕션 빌드
docker:
	docker build -t ppt-translator .
	docker run -p 8000:8000 --env-file .env ppt-translator

# 로그 확인
logs:
	docker-compose logs -f

# 컨테이너 중지
stop:
	docker-compose down

# Python venv 실행
venv:
	@if [ ! -d "venv" ]; then python3 -m venv venv; fi
	@. venv/bin/activate && pip install -q -r requirements.txt
	@mkdir -p uploads
	@. venv/bin/activate && LOG_LEVEL=DEBUG uvicorn app.main:app --reload

# 정리
clean:
	rm -rf uploads/*
	rm -rf __pycache__ app/__pycache__ app/**/__pycache__
	rm -rf .pytest_cache
	docker-compose down -v --remove-orphans 2>/dev/null || true
