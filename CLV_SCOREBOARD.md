# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 106 / 100
- **Mean CLV:** -1.2c per bet
- **Beat the close:** 72% of bets

### Settlement record (scored against Kalshi's official results)

- **52W – 12L** over 64 settled contracts
- **Net +181c** on 5019c staked (**+3.6%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 17
- filled and still open: 8
- expired unfilled (never hit — correctly NOT scored): 108
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -1.2c over 106 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
