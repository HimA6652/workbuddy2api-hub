"""Shared model/endpoint queue Gate for upstream codes 6020 and 6021.

Queue failures stay separate from Account cooldowns and affinity. Every real
request carries a generation ticket: late responses cannot erase newer queue
evidence or reopen a Gate after a newer probe has confirmed recovery.
"""
import json
import threading
import time

QUEUE_WAITING = 6020
QUEUE_FULL = 6021
QUEUE_CODES = frozenset((QUEUE_WAITING, QUEUE_FULL))
DEFAULT_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0
MAX_BACKOFF_STREAK = 5
# Crash backstop, not a network timeout. Operators can configure longer header
# timeouts; a lease expiring never makes an old response current again.
PROBE_LEASE_SECONDS = 180.0
MAX_WAIT_HINT_SECONDS = 3600.0
MAX_SCOPES = 256
# Wake sleepers promptly on state changes, while retaining a bounded poll for
# client-disconnect callbacks and a crash/expiry backstop.
WAIT_POLL_SECONDS = 0.2
# Only JSON error bodies are decoded. This is larger than observed queue envelopes
# (including padded fixtures) while retaining a finite per-error memory bound.
QUEUE_ERROR_BODY_LIMIT = 8 * 1024 * 1024


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except (ValueError, OverflowError):
            return None
    return None


def parse_upstream_error(detail):
    """Return (6020|6021, data) from explicit JSON code locations only.

    retry_after is seconds; estimated_wait is milliseconds and is intentionally
    not added to or substituted for that hint. Prose alone never opens a Gate.
    """
    try:
        payload = json.loads(detail or "")
    except (TypeError, ValueError):
        return None, {}

    def visit(obj, depth=0):
        if not isinstance(obj, dict) or depth > 16:
            return None
        code = _integer(obj.get("code"))
        if code in QUEUE_CODES:
            data = obj.get("data")
            return code, data if isinstance(data, dict) else obj
        for key in ("error", "data"):
            found = visit(obj.get(key), depth + 1)
            if found is not None:
                return found
        return None

    return visit(payload) or (None, {})


def classify(detail):
    return parse_upstream_error(detail)


def queue_scope(account, model, chat_url=None, product=None):
    """Return the exact (realm, model, product, endpoint) Gate key.

    A transport attempt passes the same product snapshot that built chat_url.
    Account.product is mutable during an explicit/automatic product switch;
    reading it again here could attribute a response to a different endpoint.
    """
    if chat_url:
        # wb_proxy historically used /v2; wb_model_queue documents /v3.
        # Both identify the same chat product endpoint, never part of its key.
        for suffix in ("/v3/chat/completions", "/v2/chat/completions"):
            if chat_url.endswith(suffix):
                base = chat_url[:-len(suffix)]
                break
        else:
            base = chat_url
    else:
        base = account.chat_base_url()
    if product is None:
        product = getattr(account, "product", "") or ""
    return (str(account.realm), str(model), str(product), str(base))


def scope_key(scope):
    return tuple(scope)


class QueueWaitCancelled(Exception):
    """The client went away while the gateway waited for a shared queue."""


class ModelQueueGate(object):
    """Lock-protected Gate with generation-checked real-request admissions.

    begin() returns (proceed, retry_after, ticket). A ticket is
    (generation, probe_token); probe_token is False for an ordinary request.
    False as the whole ticket means that admission has not happened yet.
    """

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self._changed = threading.Condition(self.lock)
        self.states = {}
        self.probes = {}
        self.waiters = {}

    @staticmethod
    def _delay(streak, retry_after=None):
        hint = _integer(retry_after)
        if hint is not None:
            return float(min(MAX_WAIT_HINT_SECONDS, max(1, hint)))
        step = min(MAX_BACKOFF_STREAK, max(0, int(streak) - 1))
        return min(MAX_RETRY_SECONDS, DEFAULT_RETRY_SECONDS * (2 ** step))

    @staticmethod
    def _fresh(row, now):
        return bool(row and now < row["until"])

    @staticmethod
    def _generation(row):
        return int(row.get("generation", 0)) if row else 0

    @staticmethod
    def _probe_token(ticket):
        return ticket[1] if isinstance(ticket, tuple) and len(ticket) == 2 else False

    def _retain(self, protected=None):
        """Evict stale history, never the scope with fresh queue evidence.

        Every stored state remains meaningful to its in-flight generation. When
        all older scopes have live probes, dropping the newest short-wait Gate
        would let its next request bypass the very 6020/6021 just observed.
        Bound the extra live-probe history instead of losing that evidence.
        """
        now = self.clock()
        for key, _row in sorted(self.states.items(),
                                key=lambda item: (
                                    self._fresh(item[1], now), item[1]["until"])):
            if len(self.states) <= MAX_SCOPES:
                break
            if key != protected and key not in self.probes:
                del self.states[key]

    def _note_locked(self, key, retry_after):
        now = self.clock()
        row = self.states.get(key)
        # note() is used only by direct tests/administrative priming. Real
        # request failures use fail(), which invalidates only the matching lease.
        generation = self._generation(row) + 1
        streak = min(MAX_BACKOFF_STREAK + 1,
                     int(row["streak"]) + 1 if row else 1)
        delay = self._delay(streak, retry_after)
        until = min(max(now + delay, row["until"] if row else 0.0),
                    now + MAX_WAIT_HINT_SECONDS)
        self.states[key] = {"until": until, "streak": streak,
                            "generation": generation}
        # Fresh evidence invalidates every outstanding probe in this generation.
        # The next begin() will issue exactly one new lease after the new delay.
        self.probes.pop(key, None)
        self._retain(protected=key)
        self._changed.notify_all()
        return max(1, int(round(until - now)))

    def note(self, scope, retry_after=None):
        """Record explicit fresh evidence; request paths should use fail()."""
        with self.lock:
            return self._note_locked(scope_key(scope), retry_after)

    def begin(self, scope):
        """Atomically admit a request and possibly issue one probe lease."""
        now = self.clock()
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            generation = self._generation(row)
            if row is None or (row["streak"] == 0 and now >= row["until"]):
                return True, 0, (generation, False)
            if now < row["until"]:
                return False, max(1, int(round(row["until"] - now))), False
            probe = self.probes.get(key)
            if probe is not None:
                if now < probe["deadline"]:
                    # The probe is in flight, not a claim that congestion lasts
                    # until its crash lease expires. Clients get a short hint;
                    # configured local waiters are woken by its actual result.
                    return False, 1, False
                del self.probes[key]
            token = object()
            self.probes[key] = {"token": token, "generation": generation,
                                "deadline": now + PROBE_LEASE_SECONDS}
            return True, 0, (generation, token)

    def wait(self, scope, timeout, cancelled=None):
        """Wait for admission without creating a probe or polling upstream.

        The budget uses the real monotonic clock even when tests give the Gate
        a synthetic state clock. Condition wakeups follow every generation
        change, successful recovery and abandoned probe. The caller retries
        begin() after True; False means its finite request budget expired.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        key = scope_key(scope)
        with self._changed:
            self.waiters[key] = self.waiters.get(key, 0) + 1
            try:
                while True:
                    if cancelled is not None and cancelled():
                        raise QueueWaitCancelled()
                    now = self.clock()
                    row = self.states.get(key)
                    probe = self.probes.get(key)
                    half_open = bool(probe and now < probe["deadline"])
                    if not self._fresh(row, now) and not half_open:
                        return True
                    wake_at = max(now, row["until"] if row else now)
                    if half_open:
                        wake_at = max(wake_at, probe["deadline"])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    # Re-evaluate fresh 6020/6021 evidence immediately instead
                    # of sleeping through an extended upstream retry_after.
                    # An active probe has no short ETA: wait for its result,
                    # not for the full 180-second crash lease.
                    delay = (WAIT_POLL_SECONDS if half_open
                             else max(0.01, wake_at - now))
                    self._changed.wait(min(WAIT_POLL_SECONDS, delay, remaining))
            finally:
                count = self.waiters.get(key, 1) - 1
                if count > 0:
                    self.waiters[key] = count
                else:
                    self.waiters.pop(key, None)

    def _owns(self, key, ticket):
        probe_token = self._probe_token(ticket)
        row = self.probes.get(key)
        return bool(probe_token is not False and probe_token is not None and row
                    and row["token"] is probe_token
                    and row["generation"] == ticket[0]
                    and self._generation(self.states.get(key)) == ticket[0])

    def _current(self, key, ticket):
        return (isinstance(ticket, tuple) and len(ticket) == 2
                and self._generation(self.states.get(key)) == ticket[0])

    def _stale_wait(self, row, retry_after):
        now = self.clock()
        if self._fresh(row, now):
            return max(1, int(round(row["until"] - now)))
        # A closed tombstone is the opposite of evidence of congestion. A late
        # ignored failure gets the minimal client retry hint, never a new Gate.
        if row is not None and not row["streak"]:
            return 1
        return max(1, int(self._delay(1, retry_after)))

    def success(self, scope, ticket=False):
        """Close only the generation that this exact real request admitted."""
        with self.lock:
            key = scope_key(scope)
            if not self._current(key, ticket):
                return False
            if self._probe_token(ticket) is False:
                # A closed tombstone still rejects older queue errors. Ordinary
                # success does not infer recovery for another generation.
                return not self.probes.get(key) and not self._fresh(
                    self.states.get(key), self.clock())
            if not self._owns(key, ticket):
                return False
            row = self.states.get(key)
            self.states[key] = {"until": 0.0, "streak": 0,
                                "generation": self._generation(row) + 1}
            self.probes.pop(key, None)
            self._changed.notify_all()
            return True

    def fail(self, scope, ticket=False, retry_after=None):
        """Open only for a current admission; ignore late pre-Gate responses."""
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            probe_token = self._probe_token(ticket)
            if probe_token is not False:
                if not self._owns(key, ticket):
                    return self._stale_wait(row, retry_after)
            elif not self._current(key, ticket):
                # Once a Gate exists, the current half-open real request owns
                # recovery. Every older ordinary response is stale: letting one
                # advance the generation would invalidate that newer probe and
                # reopen the endpoint even when the probe subsequently succeeds.
                return self._stale_wait(row, retry_after)
            return self._note_locked(key, retry_after)

    def heartbeat(self, scope, ticket, lease_seconds=None):
        """Extend the existing real request, never create a second probe.

        An upstream response-header timeout may exceed the crash lease. The
        thread blocked in urlopen is still the sole probe; refreshing its
        lease keeps recovery single-flight for the whole actual network wait.
        """
        seconds = max(1.0, float(lease_seconds or PROBE_LEASE_SECONDS))
        with self.lock:
            key = scope_key(scope)
            if not self._owns(key, ticket):
                return False
            self.probes[key]["deadline"] = self.clock() + seconds
            return True

    def abandon(self, scope, ticket):
        """Release only this probe; do not invent recovery or extra delay."""
        if self._probe_token(ticket) is False:
            return
        with self.lock:
            key = scope_key(scope)
            if self._owns(key, ticket):
                self.probes.pop(key, None)
                self._changed.notify_all()

    def wake_all(self):
        """Unblock sleepers during draining; a finite retry must still finish.

        This is deliberately not clear(): closing the shared Gate on hotswap
        would falsify congestion evidence and could duplicate upstream work.
        """
        with self.lock:
            self._changed.notify_all()

    def check(self, scope):
        """Read-only fallback check, including an active half-open lease."""
        now = self.clock()
        with self.lock:
            key = scope_key(scope)
            row = self.states.get(key)
            if self._fresh(row, now):
                return False, max(1, int(round(row["until"] - now)))
            probe = self.probes.get(key)
            if probe and now < probe["deadline"]:
                return False, 1
            return True, 0

    def snapshot(self):
        now = self.clock()
        out = []
        with self.lock:
            for scope, row in list(self.states.items()):
                probe = self.probes.get(scope)
                half_open = bool(probe and now < probe["deadline"])
                if not self._fresh(row, now) and not half_open:
                    continue
                # Public Retry-After is the queue delay, never the unrelated
                # 180-second crash backstop for an in-flight real request.
                wait = row["until"] - now
                out.append({"realm": scope[0], "model": scope[1],
                            "product": scope[2], "endpoint": scope[3],
                            "phase": "half_open" if half_open else "open",
                            "retry_after": max(1, int(round(wait))),
                            "backoff_streak": row["streak"]})
        out.sort(key=lambda row: (row["realm"], row["model"],
                                  row["product"], row["endpoint"]))
        return out

    def clear(self):
        with self.lock:
            self.states.clear()
            self.probes.clear()
            # Administrative clearing must release existing waiters too, not
            # leave them asleep until their original retry_after expires.
            self._changed.notify_all()
