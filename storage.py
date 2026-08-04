"""토큰 보유량 저장소.

서버(길드)별로 사용자의 토큰 보유량을 JSON 파일에 저장한다.
디스코드에서는 조회만 가능하고, 값을 바꾸는 경로는 이 모듈뿐이다.
"""

import asyncio
import json
import os
import tempfile
from typing import Dict, Iterable, List, Optional, Tuple

import config


class TokenStore:
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or config.DATA_DIR
        self.path = os.path.join(self.data_dir, 'tokens.json')
        self._lock = asyncio.Lock()
        # {guild_id(str): {user_id(str): balance(int)}}
        self._balances: Dict[str, Dict[str, int]] = {}
        # {guild_id(str): "YYYY-MM-DD"} - 마지막으로 일일 보정을 한 날짜
        self._last_topup: Dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 파일 입출력
    # ------------------------------------------------------------------
    def load(self) -> None:
        """파일에서 보유량을 읽어온다. 파일이 없으면 빈 상태로 시작한다."""
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            balances = raw.get('balances', {})
            self._balances = {
                str(gid): {str(uid): int(amount) for uid, amount in members.items()}
                for gid, members in balances.items()
            }
            self._last_topup = {str(gid): str(day) for gid, day in raw.get('last_topup', {}).items()}
            print(f"[storage] {self.path} 에서 {sum(len(m) for m in self._balances.values())}건을 불러왔습니다.")
        except FileNotFoundError:
            self._balances = {}
            self._last_topup = {}
            print(f"[storage] {self.path} 이(가) 없어 새로 시작합니다.")
        except (json.JSONDecodeError, ValueError) as e:
            # 파일이 깨진 경우 백업만 남기고 빈 상태로 시작한다.
            backup = self.path + '.broken'
            try:
                os.replace(self.path, backup)
                print(f"[storage] 파일을 읽을 수 없어 {backup} 으로 옮겼습니다: {e}")
            except OSError:
                pass
            self._balances = {}
            self._last_topup = {}
        self._loaded = True

    def _write(self) -> None:
        """임시 파일에 쓴 뒤 교체해서 중간에 끊겨도 파일이 깨지지 않게 한다."""
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {'version': 1, 'balances': self._balances, 'last_topup': self._last_topup}
        fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, prefix='tokens-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def save(self) -> None:
        await asyncio.to_thread(self._write)

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    def _guild(self, guild_id: int) -> Dict[str, int]:
        return self._balances.setdefault(str(guild_id), {})

    def has_account(self, guild_id: int, user_id: int) -> bool:
        return str(user_id) in self._guild(guild_id)

    def get_balance(self, guild_id: int, user_id: int) -> int:
        return self._guild(guild_id).get(str(user_id), 0)

    def get_last_topup(self, guild_id: int) -> Optional[str]:
        """마지막으로 일일 보정을 한 날짜(YYYY-MM-DD). 기록이 없으면 None."""
        return self._last_topup.get(str(guild_id))

    def top(self, guild_id: int, count: int = 5) -> List[Tuple[int, int]]:
        """보유량 상위 인원을 (user_id, balance) 목록으로 돌려준다."""
        members = self._guild(guild_id)
        ordered = sorted(members.items(), key=lambda kv: (-kv[1], int(kv[0])))
        return [(int(uid), amount) for uid, amount in ordered[:count]]

    # ------------------------------------------------------------------
    # 변경
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp(amount: int) -> int:
        return max(0, min(config.MAX_TOKENS, amount))

    async def grant_initial(self, guild_id: int, user_ids: Iterable[int]) -> int:
        """계정이 없는 인원에게만 최초 지급을 한다. 지급한 인원 수를 돌려준다."""
        async with self._lock:
            members = self._guild(guild_id)
            granted = 0
            for user_id in user_ids:
                key = str(user_id)
                if key not in members:
                    members[key] = config.INITIAL_TOKENS
                    granted += 1
            if granted:
                await asyncio.to_thread(self._write)
            return granted

    async def daily_topup(self, guild_id: int, user_ids: Iterable[int], day: str) -> int:
        """보유량이 기준선 미만인 인원을 기준선으로 맞춘다. 보정된 인원 수를 돌려준다.

        보정한 날짜(day)를 함께 기록해서, 봇이 재시작해도 그날 보정을 이미 했는지
        판단할 수 있게 한다. 바뀐 인원이 없어도 날짜는 기록한다.
        """
        async with self._lock:
            members = self._guild(guild_id)
            changed = 0
            for user_id in user_ids:
                key = str(user_id)
                if members.get(key, 0) < config.DAILY_FLOOR:
                    members[key] = config.DAILY_FLOOR
                    changed += 1
            self._last_topup[str(guild_id)] = day
            await asyncio.to_thread(self._write)
            return changed

    async def adjust(self, guild_id: int, user_id: int, delta: int) -> int:
        """한 명의 보유량을 증감시키고 결과 보유량을 돌려준다."""
        async with self._lock:
            members = self._guild(guild_id)
            key = str(user_id)
            members[key] = self._clamp(members.get(key, 0) + delta)
            await asyncio.to_thread(self._write)
            return members[key]

    async def transfer(self, guild_id: int, winner_id: int, loser_id: int, amount: int) -> Tuple[int, int]:
        """패자에게서 승자로 토큰을 옮기고 (승자 보유량, 패자 보유량)을 돌려준다."""
        async with self._lock:
            members = self._guild(guild_id)
            wkey, lkey = str(winner_id), str(loser_id)
            members[wkey] = self._clamp(members.get(wkey, 0) + amount)
            members[lkey] = self._clamp(members.get(lkey, 0) - amount)
            await asyncio.to_thread(self._write)
            return members[wkey], members[lkey]


store = TokenStore()
