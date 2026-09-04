# CLV scoreboard — favourite-bias maker model

Closing-Line Value = (market price of our side at close) − (price we paid). Positive = we beat the close = edge. The honest test.

- **Scored samples:** 143 / 100
- **Mean CLV:** -2.1c per bet
- **Beat the close:** 71% of bets

### Settlement record (scored against Kalshi's official results)

- **73W – 16L** over 89 settled contracts
- **Net +83c** on 7217c staked (**+1.2%**)

_Real outcomes from public settlement data — no order required. What this does NOT prove is that a resting maker bid would have been filled at these prices; only live orders establish queue position._

### Sample accounting

- resting (maker bid not yet hit): 28
- filled and still open: 6
- expired unfilled (never hit — correctly NOT scored): 172
- unscored (closed before any open snapshot — capture gap): 4

**Verdict:** ❌ Mean CLV -2.1c over 143 bets — no edge vs the closing line. Do NOT put real money here.

> ⚠️ 4 row(s) unscored. That is a measurement failure, not a result — the tracker saw them only after their market had closed.
