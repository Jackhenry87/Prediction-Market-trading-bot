# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 131 / 100
- **Mean CLV:** -2.7c per bet
- **Beat the close:** 69% of bets

### Settlement record (scored against Kalshi's official results)

- **69W – 15L** over 84 settled contracts
- **Net +122c** on 6778c staked (**+1.8%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 40
- filled and still open: 13
- expired unfilled (never hit — correctly NOT scored): 135
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -2.7c over 131 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
