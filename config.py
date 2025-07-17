# 디스코드 봇 설정 파일
import os
from dotenv import load_dotenv

# .env 파일에서 환경변수 로드 (로컬 개발용)
load_dotenv()

# 환경변수에서 토큰 가져오기 (Render에서는 환경변수로 설정)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다!")

# 파티 설정 기본값
DEFAULT_PARTY_SIZE = 8
MIN_PARTY_SIZE = 2
MAX_PARTY_SIZE = 16

# 알림 설정
NOTIFICATION_MINUTES_BEFORE = 10 