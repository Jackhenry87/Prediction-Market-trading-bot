# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 127 / 100
- **Mean CLV:** -2.1c per bet
- **Beat the close:** 72% of bets

### Settlement record (scored against Kalshi's official results)

- **65W – 15L** over 80 settled contracts
- **Net +68c** on 6432c staked (**+1.1%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 43
- filled and still open: 14
- expired unfilled (never hit — correctly NOT scored): 134
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -2.1c over 127 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
