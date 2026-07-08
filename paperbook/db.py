"""SQLite storage for the paper sportsbook: users, games, bets."""

import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "paperbook.db"
START_BALANCE_CENTS = 100_000  # $1,000 play money on signup


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                pw_hash TEXT NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                balance_cents INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                sport TEXT, home TEXT, away TEXT,
                commence_time TEXT,
                home_odds REAL, away_odds REAL,
                result TEXT,            -- 'home' | 'away' | NULL
                settled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                game_id TEXT NOT NULL REFERENCES games(id),
                side TEXT NOT NULL,     -- 'home' | 'away'
                stake_cents INTEGER NOT NULL,
                odds REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',  -- open|won|lost|void
                payout_cents INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prop_markets (
                id TEXT PRIMARY KEY,    -- deterministic: event_stat_player_line
                sport TEXT, event_id TEXT,
                player TEXT, stat TEXT,          -- e.g. 'pitcher_strikeouts'
                line REAL,                       -- the point, e.g. 6.5
                over_odds REAL, under_odds REAL, -- decimal, as posted
                commence_time TEXT,
                result TEXT,            -- 'over' | 'under' | 'push' | NULL
                settled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS prop_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                market_id TEXT NOT NULL REFERENCES prop_markets(id),
                side TEXT NOT NULL,     -- 'over' | 'under'
                stake_cents INTEGER NOT NULL,
                odds REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',  -- open|won|lost|void
                payout_cents INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """
        )


def new_api_key() -> str:
    return "pk_" + secrets.token_hex(20)


def create_user(username: str, email: str, pw_hash: str) -> dict:
    key = new_api_key()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, pw_hash, api_key, "
            "balance_cents, created_at) VALUES (?,?,?,?,?,?)",
            (username, email, pw_hash, key, START_BALANCE_CENTS, int(time.time())),
        )
        # read back on the SAME connection (row isn't committed to others yet)
        return c.execute("SELECT * FROM users WHERE id=?",
                         (cur.lastrowid,)).fetchone()


def get_user(user_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_name(username: str):
    with connect() as c:
        return c.execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_key(api_key: str):
    with connect() as c:
        return c.execute(
            "SELECT * FROM users WHERE api_key=?", (api_key,)).fetchone()


def upsert_game(g: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO games (id, sport, home, away, commence_time, "
            "home_odds, away_odds) VALUES (:id,:sport,:home,:away,"
            ":commence_time,:home_odds,:away_odds) "
            "ON CONFLICT(id) DO UPDATE SET home_odds=:home_odds, "
            "away_odds=:away_odds, commence_time=:commence_time",
            g,
        )


def open_games() -> list:
    with connect() as c:
        return c.execute(
            "SELECT * FROM games WHERE settled=0 ORDER BY commence_time"
        ).fetchall()


def get_game(game_id: str):
    with connect() as c:
        return c.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()


def place_bet(user_id: int, game_id: str, side: str, stake_cents: int) -> dict:
    """Deduct stake and record the bet at the game's current odds.
    Raises ValueError on any invalid input (fails closed, no money moves)."""
    if side not in ("home", "away"):
        raise ValueError("side must be 'home' or 'away'")
    if stake_cents <= 0:
        raise ValueError("stake must be positive")
    with connect() as c:
        game = c.execute(
            "SELECT * FROM games WHERE id=? AND settled=0", (game_id,)
        ).fetchone()
        if not game:
            raise ValueError("game not found or already settled")
        odds = game["home_odds"] if side == "home" else game["away_odds"]
        if not odds or odds <= 1:
            raise ValueError("no odds available for that side")
        user = c.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user["balance_cents"] < stake_cents:
            raise ValueError("insufficient balance")
        c.execute("UPDATE users SET balance_cents=balance_cents-? WHERE id=?",
                  (stake_cents, user_id))
        cur = c.execute(
            "INSERT INTO bets (user_id, game_id, side, stake_cents, odds, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (user_id, game_id, side, stake_cents, odds, int(time.time())),
        )
        return dict(id=cur.lastrowid, game_id=game_id, side=side,
                    stake_cents=stake_cents, odds=odds)


def user_bets(user_id: int) -> list:
    with connect() as c:
        return c.execute(
            "SELECT b.*, g.home, g.away, g.result FROM bets b "
            "JOIN games g ON g.id=b.game_id WHERE b.user_id=? "
            "ORDER BY b.created_at DESC", (user_id,)
        ).fetchall()


def leaderboard(limit: int = 20) -> list:
    with connect() as c:
        return c.execute(
            "SELECT username, balance_cents FROM users "
            "ORDER BY balance_cents DESC LIMIT ?", (limit,)
        ).fetchall()


def settle_game(game_id: str, result: str) -> int:
    """Mark a game's result and settle every open bet on it. Returns the
    number of bets settled."""
    if result not in ("home", "away"):
        raise ValueError("result must be 'home' or 'away'")
    settled = 0
    with connect() as c:
        c.execute("UPDATE games SET result=?, settled=1 WHERE id=?",
                  (result, game_id))
        for b in c.execute("SELECT * FROM bets WHERE game_id=? AND status='open'",
                           (game_id,)).fetchall():
            if b["side"] == result:
                payout = int(round(b["stake_cents"] * b["odds"]))
                c.execute("UPDATE bets SET status='won', payout_cents=? "
                          "WHERE id=?", (payout, b["id"]))
                c.execute("UPDATE users SET balance_cents=balance_cents+? "
                          "WHERE id=?", (payout, b["user_id"]))
            else:
                c.execute("UPDATE bets SET status='lost' WHERE id=?", (b["id"],))
            settled += 1
    return settled


# ---------- player props ----------
def upsert_prop(m: dict) -> None:
    """Post (or refresh the odds of) a player-prop market. Never disturbs an
    already-settled market's result."""
    with connect() as c:
        c.execute(
            "INSERT INTO prop_markets (id, sport, event_id, player, stat, "
            "line, over_odds, under_odds, commence_time) VALUES (:id,:sport,"
            ":event_id,:player,:stat,:line,:over_odds,:under_odds,"
            ":commence_time) ON CONFLICT(id) DO UPDATE SET "
            "over_odds=:over_odds, under_odds=:under_odds, "
            "commence_time=:commence_time WHERE settled=0",
            m,
        )


def open_props() -> list:
    with connect() as c:
        return c.execute(
            "SELECT * FROM prop_markets WHERE settled=0 ORDER BY commence_time"
        ).fetchall()


def get_prop(market_id: str):
    with connect() as c:
        return c.execute(
            "SELECT * FROM prop_markets WHERE id=?", (market_id,)).fetchone()


def place_prop_bet(user_id: int, market_id: str, side: str,
                   stake_cents: int) -> dict:
    """Deduct stake and record a paper prop bet at the market's posted odds.
    Fails closed (ValueError, no money moves) on any invalid input."""
    if side not in ("over", "under"):
        raise ValueError("side must be 'over' or 'under'")
    if stake_cents <= 0:
        raise ValueError("stake must be positive")
    with connect() as c:
        m = c.execute(
            "SELECT * FROM prop_markets WHERE id=? AND settled=0", (market_id,)
        ).fetchone()
        if not m:
            raise ValueError("prop market not found or already settled")
        odds = m["over_odds"] if side == "over" else m["under_odds"]
        if not odds or odds <= 1:
            raise ValueError("no odds available for that side")
        user = c.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user["balance_cents"] < stake_cents:
            raise ValueError("insufficient balance")
        c.execute("UPDATE users SET balance_cents=balance_cents-? WHERE id=?",
                  (stake_cents, user_id))
        cur = c.execute(
            "INSERT INTO prop_bets (user_id, market_id, side, stake_cents, "
            "odds, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, market_id, side, stake_cents, odds, int(time.time())),
        )
        return dict(id=cur.lastrowid, market_id=market_id, side=side,
                    stake_cents=stake_cents, odds=odds)


def user_prop_bets(user_id: int) -> list:
    with connect() as c:
        return c.execute(
            "SELECT b.*, m.player, m.stat, m.line, m.result FROM prop_bets b "
            "JOIN prop_markets m ON m.id=b.market_id WHERE b.user_id=? "
            "ORDER BY b.created_at DESC", (user_id,)
        ).fetchall()


def settle_prop(market_id: str, result: str) -> int:
    """Grade a prop market and settle every open bet on it. 'push' voids
    (stake returned). Returns the number of bets settled."""
    if result not in ("over", "under", "push"):
        raise ValueError("result must be 'over', 'under' or 'push'")
    settled = 0
    with connect() as c:
        c.execute("UPDATE prop_markets SET result=?, settled=1 WHERE id=?",
                  (result, market_id))
        for b in c.execute(
                "SELECT * FROM prop_bets WHERE market_id=? AND status='open'",
                (market_id,)).fetchall():
            if result == "push":
                c.execute("UPDATE prop_bets SET status='void', payout_cents=? "
                          "WHERE id=?", (b["stake_cents"], b["id"]))
                c.execute("UPDATE users SET balance_cents=balance_cents+? "
                          "WHERE id=?", (b["stake_cents"], b["user_id"]))
            elif b["side"] == result:
                payout = int(round(b["stake_cents"] * b["odds"]))
                c.execute("UPDATE prop_bets SET status='won', payout_cents=? "
                          "WHERE id=?", (payout, b["id"]))
                c.execute("UPDATE users SET balance_cents=balance_cents+? "
                          "WHERE id=?", (payout, b["user_id"]))
            else:
                c.execute("UPDATE prop_bets SET status='lost' WHERE id=?",
                          (b["id"],))
            settled += 1
    return settled
