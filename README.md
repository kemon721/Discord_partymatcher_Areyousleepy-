# Discord 파티 매칭 봇

Discord에서 파티 모집 및 관리를 도와주는 봇입니다.

## 기능

- 파티 모집 생성
- 파티 참여/탈퇴 관리
- 출발 시간 알림
- 파티 완료/취소 기능
- 파티장과 파티원 권한 구분

## Render를 사용한 배포 방법

### 1. GitHub 저장소 준비

1. GitHub 계정에 로그인
2. 새 저장소 생성 (New Repository)
3. 저장소 이름 입력 (예: discord-party-bot)
4. Public으로 설정
5. 코드를 GitHub에 업로드

### 2. Discord Bot 토큰 준비

1. [Discord Developer Portal](https://discord.com/developers/applications) 접속
2. 봇 애플리케이션 생성 및 토큰 복사
3. 토큰을 안전한 곳에 보관 (절대 GitHub에 올리지 말 것!)

### 3. Render 배포

1. [Render](https://render.com) 계정 생성
2. 'New +' 버튼 클릭 → 'Web Service' 선택
3. GitHub 저장소 연결
4. 다음 설정 입력:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
5. 환경변수 설정:
   - Key: `DISCORD_TOKEN`
   - Value: Discord에서 복사한 봇 토큰
6. 'Create Web Service' 클릭

### 4. Keep-Alive 설정

Render 무료 플랜은 30분 동안 요청이 없으면 서버가 잠들어집니다. 
이를 방지하기 위해 [UptimeRobot](https://uptimerobot.com)이나 [Cronitor](https://cronitor.io) 같은 서비스를 사용하여 
5분마다 `https://your-app-name.onrender.com/ping`에 요청을 보내도록 설정하세요.

## 로컬 개발 환경 설정

1. 저장소 클론
```bash
git clone https://github.com/your-username/discord-party-bot.git
cd discord-party-bot
```

2. 가상환경 생성 및 활성화
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate
```

3. 의존성 설치
```bash
pip install -r requirements.txt
```

4. `.env` 파일 생성
```
DISCORD_TOKEN=your_discord_bot_token_here
```

5. 봇 실행
```bash
python bot.py
```

## 사용법

### 슬래시 명령어

- `/파티매칭`: 새 파티 모집 시작
- `/파티완료`: 파티장이 파티 활동 완료 처리
- `/파티취소`: 파티장이 파티 모집 취소

### 버튼 기능

- **📥 참여하기**: 파티에 참여 (파티원용)
- **📤 나가기**: 파티에서 나가기 (파티원용)
- **✅ 파티완료**: 파티 활동 완료 (파티장용)
- **❌ 파티취소**: 파티 모집 취소 (파티장용)

## 보안 주의사항

- Discord 봇 토큰을 절대 GitHub나 공개 장소에 노출하지 마세요
- 환경변수를 통해서만 토큰을 관리하세요
- `.env` 파일은 `.gitignore`에 포함되어 Git에 업로드되지 않습니다

## 기술 스택

- Python 3.8+
- discord.py
- Flask (Keep-Alive용)
- python-dotenv 