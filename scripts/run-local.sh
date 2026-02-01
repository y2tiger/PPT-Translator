#!/bin/bash

# PPT Translator 로컬 실행 스크립트

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== PPT Translator 로컬 개발 환경 ===${NC}"

# .env 파일 확인
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo -e "${YELLOW}⚠️  .env 파일이 없습니다. .env.example을 복사합니다.${NC}"
        cp .env.example .env
        echo -e "${RED}❗ .env 파일에서 OPENAI_API_KEY를 설정해주세요!${NC}"
        exit 1
    fi
fi

# OPENAI_API_KEY 확인
source .env 2>/dev/null || true
if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "sk-your-api-key-here" ]; then
    echo -e "${RED}❗ OPENAI_API_KEY가 설정되지 않았습니다.${NC}"
    echo "   .env 파일을 편집해서 API 키를 설정하세요."
    exit 1
fi

# 실행 방식 선택
echo ""
echo "실행 방식을 선택하세요:"
echo "  1) Docker (권장 - Render 환경과 동일)"
echo "  2) Python venv (빠른 개발, LibreOffice 필요)"
echo ""
read -p "선택 (1/2): " choice

case $choice in
    1)
        echo -e "${GREEN}🐳 Docker로 실행합니다...${NC}"
        docker-compose up --build
        ;;
    2)
        echo -e "${GREEN}🐍 Python venv로 실행합니다...${NC}"

        # venv 확인/생성
        if [ ! -d "venv" ]; then
            echo "venv 생성 중..."
            python3 -m venv venv
        fi

        source venv/bin/activate
        pip install -q -r requirements.txt

        # uploads 디렉토리 생성
        mkdir -p uploads

        # LibreOffice 확인
        if ! command -v libreoffice &> /dev/null; then
            echo -e "${YELLOW}⚠️  LibreOffice가 설치되지 않았습니다.${NC}"
            echo "   Visual QA 기능이 작동하지 않습니다."
            echo "   설치: brew install --cask libreoffice (macOS)"
            echo "         sudo apt install libreoffice (Ubuntu)"
        fi

        echo ""
        echo -e "${GREEN}🚀 서버 시작: http://localhost:8000${NC}"
        uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
        ;;
    *)
        echo "잘못된 선택입니다."
        exit 1
        ;;
esac
