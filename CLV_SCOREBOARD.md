# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 17 / 100
- **Mean CLV:** +3.1c per bet
- **Beat the close:** 65% of bets

### Settlement record (scored against Kalshi's official results)

- **10W – 3L** over 13 settled contracts
- **Net +91c** on 909c staked (**+10.0%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 72
- filled and still open: 53
- expired unfilled (never hit — correctly NOT scored): 22
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ⏳ 17 of 100 scored samples — not enough to read yet. No real money until this reaches 100+ and the mean is positive.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
