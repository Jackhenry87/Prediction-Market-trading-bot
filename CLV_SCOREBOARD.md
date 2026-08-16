# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 75 / 100
- **Mean CLV:** -2.6c per bet
- **Beat the close:** 71% of bets

### Settlement record (scored against Kalshi's official results)

- **42W – 9L** over 51 settled contracts
- **Net +145c** on 4055c staked (**+3.6%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 48
- filled and still open: 22
- expired unfilled (never hit — correctly NOT scored): 74
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ⏳ 75 of 100 scored samples — not enough to read yet. No real money until this reaches 100+ and the mean is positive.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
