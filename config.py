# 디스코드 봇 설정 파일
import os
from dotenv import load_dotenv

# .env 파일에서 환경변수 로드 (로컬 개발용)
load_dotenv()

# 환경변수에서 토큰 가져오기 (Render에서는 환경변수로 설정)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다!")

# ============================================
# 데이터 저장 위치
# ============================================
# Render 퍼시스턴트 디스크의 기본 마운트 경로.
RENDER_DISK_PATH = '/var/data'

# 마운트된 디스크가 있으면 그쪽에, 없으면 프로젝트 폴더의 data/ 에 저장한다.
# DATA_DIR 환경변수를 지정하면 그 값이 항상 우선한다.
DATA_DIR = os.getenv('DATA_DIR') or (
    RENDER_DISK_PATH if os.path.isdir(RENDER_DISK_PATH) else 'data'
)

# 디스크가 아닌 곳에 저장 중이면 재배포·재시작 시 데이터가 사라진다.
DATA_IS_PERSISTENT = os.path.isdir(RENDER_DISK_PATH) and os.path.abspath(DATA_DIR).startswith(
    os.path.abspath(RENDER_DISK_PATH)
)

# ============================================
# 채널 추천
# ============================================
CHANNEL_MIN = 1
CHANNEL_MAX = 38

# ============================================
# 토큰 지급 규칙
# ============================================
INITIAL_TOKENS = 1000       # 최초 1회 지급량
DAILY_FLOOR = 1000          # 매일 이 값 미만이면 이 값으로 보정
MAX_TOKENS = 1_000_000      # 보유 상한
DAILY_RESET_HOUR = 7        # 보정 시각 (시)
TIMEZONE = 'Asia/Seoul'     # 보정 시각의 기준 시간대

# ============================================
# 놀이 규칙
# ============================================
SOLO_BET = 100              # 혼자놀기 참가비 (오답 시 회수량)
ODD_EVEN_REWARD = 50        # 홀짝 맞추기 정답 시 지급량
NUMBER_REWARD = 400         # 숫자 맞추기 정답 시 지급량
DICE_MIN = 1                # 게임에 쓰이는 숫자 범위
DICE_MAX = 10

DUO_UNIT = 100              # 같이놀기 베팅 단위 (입력값 × DUO_UNIT)
DUO_MIN_BET = 100           # 같이놀기 최소 베팅량

# ============================================
# 시작 동작
# ============================================
# 명령어 목록이 바뀌었을 때만 디스코드에 동기화한다.
# 글로벌 동기화는 제한이 강해서 재배포마다 호출하면 차단될 수 있다.
# 강제로 다시 동기화해야 하면 FORCE_SYNC=1 환경변수를 준다.
FORCE_SYNC = os.getenv('FORCE_SYNC', '').strip() in ('1', 'true', 'True')

# 시작에 실패했을 때 종료 전 대기 시간(초).
# 곧바로 종료하면 Render가 즉시 재시작해 디스코드 속도 제한이 길어진다.
RESTART_BACKOFF = int(os.getenv('RESTART_BACKOFF', '120'))

# 속도 제한(429 / Cloudflare 1015)으로 실패했을 때의 대기 시간(초).
# 이 차단은 접속을 계속 시도하면 만료 시각이 갱신되므로, 훨씬 길게 쉬어야 풀린다.
RATE_LIMIT_BACKOFF = int(os.getenv('RATE_LIMIT_BACKOFF', '3600'))

# ============================================
# 시간 제한 (초)
# ============================================
MODAL_TIME_LIMIT = 10       # 모달 제출 제한 시간
INVITE_TIME_LIMIT = 10      # 같이놀기 수락/거절 제한 시간
BUTTON_TIME_LIMIT = 30      # 중간 단계 버튼 유효 시간
PLAY_LOCK_TIMEOUT = 90      # 놀이 잠금 자동 해제 시간
