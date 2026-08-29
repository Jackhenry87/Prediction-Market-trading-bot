# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 128 / 100
- **Mean CLV:** -2.1c per bet
- **Beat the close:** 71% of bets

### Settlement record (scored against Kalshi's official results)

- **66W – 15L** over 81 settled contracts
- **Net +83c** on 6517c staked (**+1.3%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 42
- filled and still open: 14
- expired unfilled (never hit — correctly NOT scored): 134
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -2.1c over 128 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
