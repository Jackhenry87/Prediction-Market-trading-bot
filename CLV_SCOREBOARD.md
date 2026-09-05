# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 147 / 100
- **Mean CLV:** -2.6c per bet
- **Beat the close:** 69% of bets

### Settlement record (scored against Kalshi's official results)

- **76W – 17L** over 93 settled contracts
- **Net +39c** on 7561c staked (**+0.5%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 17
- filled and still open: 2
- expired unfilled (never hit — correctly NOT scored): 183
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -2.6c over 147 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
