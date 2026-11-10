"""
DeskFlow Focus Mode - Math Challenge System
Phase 5 implementation:
- Single active challenge globally (120s TTL)
- Groq AI model `openai/gpt-oss-20b` for typed parameters only (non-blocking executor)
- Strict typed parameter validation (arith, linear, percent, quad; bounds <= 1000; integer answers)
- Trusted server-side English and Vietnamese localized templates
- Secure offline fallback using random.SystemRandom()
- HMAC-protected answer storage: HMAC(secret, challenge_id + ":" + canonical_int)
- Answers never logged or exposed in API
- Verification: constant-time HMAC check, turn Focus OFF on correct answer
- Consecutive failure tracking and 60-second cooldown after 5 failed attempts (HTTP 429)
- Concurrency serialization and compare-and-set challenge installation
"""

import re
import json
import hmac
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple

from .storage import FocusStorage
from .auth import FocusAuth, parse_iso_utc, get_current_utc_iso

logger = logging.getLogger("DeskFlow.FocusMode.Challenge")

CHALLENGE_TTL_SECONDS = 120
COOLDOWN_SECONDS = 60
MAX_FAILED_ATTEMPTS = 5
MAX_ANSWER_MAGNITUDE = 1000

# Server-side templates
TEMPLATES = {
    "en": {
        "arith_add": "What is {a} + {b}?",
        "arith_sub": "What is {a} - {b}?",
        "arith_mul": "What is {a} × {b}?",
        "linear": "Solve for integer x: {a}x {sign} {abs_b} = {c}",
        "percent": "What is {p}% of {n}?",
        "quad": "Find the larger root of: x² {b_sign} {abs_b}x {c_sign} {abs_c} = 0"
    },
    "vi": {
        "arith_add": "Tính: {a} + {b} = ?",
        "arith_sub": "Tính: {a} - {b} = ?",
        "arith_mul": "Tính: {a} × {b} = ?",
        "linear": "Tìm số nguyên x: {a}x {sign} {abs_b} = {c}",
        "percent": "Tính {p}% của {n}?",
        "quad": "Tìm nghiệm lớn hơn của phương trình: x² {b_sign} {abs_b}x {c_sign} {abs_c} = 0"
    }
}


class ChallengeError(Exception):
    def __init__(self, message: str, code: str = "challenge_error", status: int = 400, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


class CooldownActiveError(ChallengeError):
    def __init__(self, retry_after: int):
        super().__init__(f"Cooldown active. Retry after {retry_after}s.", code="cooldown", status=429, retry_after=retry_after)


class ChallengeNotActiveError(ChallengeError):
    def __init__(self, message: str = "Challenge is not active or has already been consumed."):
        super().__init__(message, code="challenge_not_active", status=409)


class MathChallengeSystem:
    def __init__(self, storage: FocusStorage, auth: FocusAuth, groq_client: Optional[Any] = None):
        self.storage = storage
        self.auth = auth
        self.groq_client = groq_client
        self._sys_random = random.SystemRandom()
        self._challenge_lock = asyncio.Lock()

    def _format_question(self, q_type: str, params: Dict[str, Any], lang: str) -> Tuple[str, int]:
        """
        Constructs trusted localized question text and computes the canonical integer answer.
        Returns (question_text, canonical_int_answer).
        """
        templates = TEMPLATES.get(lang, TEMPLATES["en"])

        if q_type == "arith":
            op = params["op"]
            a = int(params["a"])
            b = int(params["b"])
            if op == "+":
                ans = a + b
                q = templates["arith_add"].format(a=a, b=b)
            elif op == "-":
                ans = a - b
                q = templates["arith_sub"].format(a=a, b=b)
            elif op == "*":
                ans = a * b
                q = templates["arith_mul"].format(a=a, b=b)
            else:
                raise ValueError(f"Unsupported arithmetic op: {op}")
            return q, ans

        elif q_type == "linear":
            a = int(params["a"])
            b = int(params["b"])
            c = int(params["c"])
            if a == 0 or (c - b) % a != 0:
                raise ValueError("Linear equation does not have an integer solution.")
            ans = (c - b) // a
            sign = "+" if b >= 0 else "-"
            q = templates["linear"].format(a=a, sign=sign, abs_b=abs(b), c=c)
            return q, ans

        elif q_type == "percent":
            p = int(params["p"])
            n = int(params["n"])
            if (p * n) % 100 != 0:
                raise ValueError("Percentage calculation does not produce an integer.")
            ans = (p * n) // 100
            q = templates["percent"].format(p=p, n=n)
            return q, ans

        elif q_type == "quad":
            r1 = int(params["r1"])
            r2 = int(params["r2"])
            if r1 == r2:
                raise ValueError("Roots must be distinct.")
            # Equation: (x - r1)(x - r2) = x^2 - (r1+r2)x + (r1*r2) = 0
            B = -(r1 + r2)
            C = r1 * r2
            ans = max(r1, r2)
            b_sign = "+" if B >= 0 else "-"
            c_sign = "+" if C >= 0 else "-"
            q = templates["quad"].format(b_sign=b_sign, abs_b=abs(B), c_sign=c_sign, abs_c=abs(C))
            return q, ans

        raise ValueError(f"Unknown question type: {q_type}")

    def _generate_offline_params(self) -> Tuple[str, Dict[str, Any]]:
        """Generates cryptographically random challenge parameters offline."""
        q_types = ["arith", "linear", "percent", "quad"]
        choice = self._sys_random.choice(q_types)

        if choice == "arith":
            op = self._sys_random.choice(["+", "-", "*"])
            if op in ["+", "-"]:
                a = self._sys_random.randint(12, 99)
                b = self._sys_random.randint(11, 89)
            else:
                a = self._sys_random.randint(4, 25)
                b = self._sys_random.randint(4, 25)
            return "arith", {"op": op, "a": a, "b": b}

        elif choice == "linear":
            a = self._sys_random.randint(2, 9)
            x_ans = self._sys_random.randint(-20, 20)
            b = self._sys_random.randint(-30, 30)
            c = a * x_ans + b
            return "linear", {"a": a, "b": b, "c": c}

        elif choice == "percent":
            p = self._sys_random.choice([10, 15, 20, 25, 30, 40, 50, 60, 75])
            # Choose n such that (p * n) % 100 == 0
            multipliers = [20, 40, 50, 60, 80, 100, 120, 150, 200, 300, 400]
            valid_ns = [n for n in multipliers if (p * n) % 100 == 0]
            n = self._sys_random.choice(valid_ns)
            return "percent", {"p": p, "n": n}

        elif choice == "quad":
            r1 = self._sys_random.randint(-12, 12)
            r2 = self._sys_random.randint(-12, 12)
            while r2 == r1:
                r2 = self._sys_random.randint(-12, 12)
            return "quad", {"r1": r1, "r2": r2}

        return "arith", {"op": "+", "a": 15, "b": 27}

    def _fetch_groq_params_sync(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        if not self.groq_client:
            return None
        try:
            prompt = (
                "You are an arithmetic challenge generator for an anti-cheat focus study app. "
                "Output ONLY a single raw JSON object specifying typed math problem parameters. "
                "Choose one of the 4 question types: 'arith', 'linear', 'percent', 'quad'. "
                "Format examples:\n"
                "- arith: {\"type\": \"arith\", \"params\": {\"op\": \"+\", \"a\": 35, \"b\": 47}}\n"
                "- linear: {\"type\": \"linear\", \"params\": {\"a\": 4, \"b\": -8, \"c\": 24}}\n"
                "- percent: {\"type\": \"percent\", \"params\": {\"p\": 25, \"n\": 120}}\n"
                "- quad: {\"type\": \"quad\", \"params\": {\"r1\": 3, \"r2\": -5}}\n"
                "Requirements:\n"
                "1. All parameter numbers MUST be integers.\n"
                "2. The answer must be an integer with absolute value <= 1000.\n"
                "3. Linear solutions must be exact integers.\n"
                "4. Percent results must be exact integers.\n"
                "5. Quadratic roots must be distinct integers.\n"
                "Do NOT include any questions, answers, or conversational text."
            )
            completion = self.groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[
                    {"role": "system", "content": "You are a JSON-only generator."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_completion_tokens=256
            )
            raw = completion.choices[0].message.content.strip()
            # Clean markdown code blocks if present
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            data = json.loads(raw)
            q_type = data.get("type")
            params = data.get("params")
            if q_type in ["arith", "linear", "percent", "quad"] and isinstance(params, dict):
                return q_type, params
            return None
        except Exception as e:
            logger.debug(f"[Challenge] Groq generation fallback triggered: {e}")
            return None

    async def generate_challenge_data(self, lang: str = "en") -> Tuple[str, str, str, str]:
        """
        Generates challenge outside state lock.
        Returns (challenge_id, question_text, answer_hmac, expires_at_iso).
        """
        lang = lang if lang in ["en", "vi"] else "en"
        params_tuple = None

        # Try Groq outside state lock with timeout
        if self.groq_client:
            try:
                loop = asyncio.get_running_loop()
                params_tuple = await asyncio.wait_for(
                    loop.run_in_executor(None, self._fetch_groq_params_sync),
                    timeout=3.0
                )
            except Exception:
                params_tuple = None

        # Validate Groq params or fallback
        valid = False
        q_type, params = "", {}
        q_text, ans = "", 0

        if params_tuple:
            try:
                cand_type, cand_params = params_tuple
                cand_text, cand_ans = self._format_question(cand_type, cand_params, lang)
                if abs(cand_ans) <= MAX_ANSWER_MAGNITUDE:
                    q_type, params = cand_type, cand_params
                    q_text, ans = cand_text, cand_ans
                    valid = True
            except Exception:
                valid = False

        if not valid:
            # Fallback to random.SystemRandom
            for _ in range(5):
                try:
                    cand_type, cand_params = self._generate_offline_params()
                    cand_text, cand_ans = self._format_question(cand_type, cand_params, lang)
                    if abs(cand_ans) <= MAX_ANSWER_MAGNITUDE:
                        q_type, params = cand_type, cand_params
                        q_text, ans = cand_text, cand_ans
                        valid = True
                        break
                except Exception:
                    continue

        if not valid:
            # Absolute hard fallback
            q_text, ans = ("What is 12 + 15?" if lang == "en" else "Tính: 12 + 15 = ?"), 27

        challenge_id = f"ch_{self._sys_random.getrandbits(64):016x}"
        answer_payload = f"{challenge_id}:{ans}"
        answer_hmac = self.auth.compute_hmac(answer_payload)

        now_dt = datetime.now(timezone.utc)
        expires_at_dt = now_dt + timedelta(seconds=CHALLENGE_TTL_SECONDS)
        expires_at_iso = expires_at_dt.isoformat()

        return challenge_id, q_text, answer_hmac, expires_at_iso

    def check_cooldown(self, state: Dict[str, Any]) -> None:
        cooldown_until = state.get("cooldown_until")
        if cooldown_until:
            try:
                cd_dt = parse_iso_utc(cooldown_until)
                now_dt = datetime.now(timezone.utc)
                if now_dt < cd_dt:
                    retry_after = max(1, int((cd_dt - now_dt).total_seconds()))
                    raise CooldownActiveError(retry_after)
            except CooldownActiveError:
                raise
            except Exception:
                pass

    async def get_or_create_challenge(self, lang: str = "en", force_new: bool = False) -> Dict[str, Any]:
        """
        Retrieves active challenge or creates a new one under challenge_lock.
        Guarantees that concurrent requests cannot overwrite an active challenge.
        Returns public challenge dict (never exposing answer).
        """
        async with self._challenge_lock:
            state = self.storage.get_state()
            if not state.get("enabled", False):
                raise ChallengeError("Focus Mode is not active.", code="focus_not_active", status=409)

            self.check_cooldown(state)

            active = state.get("challenge")
            now_dt = datetime.now(timezone.utc)

            if active and not force_new:
                try:
                    exp_dt = parse_iso_utc(active.get("expires_at", ""))
                    if now_dt < exp_dt:
                        rem_sec = max(0, int((exp_dt - now_dt).total_seconds()))
                        return {
                            "challenge_id": active["challenge_id"],
                            "question": active["question"],
                            "language": active.get("language", lang),
                            "expires_at": active["expires_at"],
                            "expires_in": rem_sec,
                            "server_time": now_dt.isoformat()
                        }
                except Exception:
                    pass

            # Generate new challenge
            target_lang = lang or state.get("language", "en")
            ch_id, q_text, ans_hmac, exp_iso = await self.generate_challenge_data(target_lang)

            challenge_record = {
                "challenge_id": ch_id,
                "question": q_text,
                "answer_hmac": ans_hmac,
                "language": target_lang,
                "created_at": now_dt.isoformat(),
                "expires_at": exp_iso
            }

            def _mutator(st: Dict[str, Any]) -> None:
                st["challenge"] = challenge_record

            await self.storage.mutate(_mutator)

            return {
                "challenge_id": ch_id,
                "question": q_text,
                "language": target_lang,
                "expires_at": exp_iso,
                "expires_in": CHALLENGE_TTL_SECONDS,
                "server_time": now_dt.isoformat()
            }

    async def verify_challenge(self, challenge_id: str, submitted_answer: int) -> Dict[str, Any]:
        """
        Verifies answer against active challenge under challenge_lock.
        Enforces compare-and-set semantics to serialize concurrent answer submissions.
        """
        async with self._challenge_lock:
            state = self.storage.get_state()
            if not state.get("enabled", False):
                raise ChallengeError("Focus Mode is not active.", code="focus_not_active", status=409)

            self.check_cooldown(state)

            current_ch = state.get("challenge")
            if not current_ch or current_ch.get("challenge_id") != challenge_id:
                # Unknown, consumed, or replayed challenge
                raise ChallengeNotActiveError("Challenge is not active, unknown, or already consumed.")

            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            is_expired = False
            try:
                exp_dt = parse_iso_utc(current_ch.get("expires_at", ""))
                if now_dt >= exp_dt:
                    is_expired = True
            except Exception:
                is_expired = True

            # Constant-time comparison
            expected_payload = f"{challenge_id}:{int(submitted_answer)}"
            expected_hmac = self.auth.compute_hmac(expected_payload)
            stored_hmac = current_ch.get("answer_hmac", "")
            is_match = hmac.compare_digest(expected_hmac, stored_hmac)

            if is_match and not is_expired:
                # SUCCESS: Disable Focus Mode
                def _success_mutator(st: Dict[str, Any]) -> None:
                    # Compare-and-set: verify challenge wasn't consumed concurrently
                    curr = st.get("challenge")
                    if not curr or curr.get("challenge_id") != challenge_id:
                        raise ChallengeNotActiveError("Challenge has already been consumed or replaced.")

                    st["enabled"] = False
                    st["challenge"] = None
                    st["failed_attempts"] = 0
                    st["cooldown_until"] = None
                    st["version"] = st.get("version", 0) + 1
                    st["updated_at"] = now_iso

                    # Record completed session into history
                    curr_sess = st.get("session") or st.get("current_session")
                    if curr_sess:
                        curr_sess["end_time"] = now_iso
                        try:
                            start_str = curr_sess.get("started_at") or curr_sess.get("start_time", "")
                            start_dt = parse_iso_utc(start_str)
                            curr_sess["duration_seconds"] = max(0, int((now_dt - start_dt).total_seconds()))
                        except Exception:
                            curr_sess["duration_seconds"] = 0
                        curr_sess["status"] = "completed"
                        curr_sess["reason"] = "Challenge solved successfully"
                        st.setdefault("history", []).append(curr_sess)
                        st["session"] = None
                        st["current_session"] = None

                updated_state = await self.storage.mutate(_success_mutator)
                logger.info(f"[Challenge] Solved correctly. Focus Mode is now OFF (version {updated_state['version']}).")
                return {
                    "success": True,
                    "correct": True,
                    "enabled": False,
                    "focus_active": False,
                    "version": updated_state["version"],
                    "message": "Challenge solved successfully. Focus Mode disabled across all devices."
                }

            # INCORRECT or EXPIRED
            # Generate replacement challenge
            lang = current_ch.get("language", state.get("language", "en"))
            new_id, new_text, new_ans_hmac, new_exp_iso = await self.generate_challenge_data(lang)

            new_ch_record = {
                "challenge_id": new_id,
                "question": new_text,
                "answer_hmac": new_ans_hmac,
                "language": lang,
                "created_at": now_iso,
                "expires_at": new_exp_iso
            }

            def _fail_mutator(st: Dict[str, Any]) -> None:
                # Compare-and-set: verify challenge wasn't consumed concurrently
                curr = st.get("challenge")
                if not curr or curr.get("challenge_id") != challenge_id:
                    raise ChallengeNotActiveError("Challenge has already been consumed or replaced.")

                failed = st.get("failed_attempts", 0) + 1
                st["failed_attempts"] = failed
                st["challenge"] = new_ch_record
                if failed >= MAX_FAILED_ATTEMPTS:
                    cooldown_time = datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SECONDS)
                    st["cooldown_until"] = cooldown_time.isoformat()
                    st["failed_attempts"] = 0
                st["version"] = st.get("version", 0) + 1
                st["updated_at"] = now_iso

            updated_state = await self.storage.mutate(_fail_mutator)

            replacement_pub = {
                "challenge_id": new_id,
                "question": new_text,
                "language": lang,
                "expires_at": new_exp_iso,
                "server_time": now_iso
            }

            return {
                "success": False,
                "correct": False,
                "enabled": True,
                "focus_active": True,
                "version": updated_state["version"],
                "new_challenge": replacement_pub,
                "error": "challenge_expired" if is_expired else "wrong_answer",
                "message": "Math challenge expired. New challenge generated." if is_expired else "Incorrect answer. Please calculate carefully."
            }
