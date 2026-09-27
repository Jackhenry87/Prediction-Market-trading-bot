# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1909 | -2.01 | -38.35 | 51% | -3.9% |
| edge > 0 after fees | 755 | -3.69 | -27.87 | 45% | -7.9% |
| edge >= 5c | 411 | -4.23 | -17.40 | 41% | -9.7% |
| edge >= 7c (trade gate) | 300 | -6.16 | -18.49 | 37% | -14.8% |
| edge >= 10c | 190 | -8.27 | -15.72 | 33% | -21.0% |
| edge >= 15c | 93 | -15.33 | -14.26 | 20% | -44.5% |

Brier score (lower is better) — model 0.2258 vs market 0.2029.

27 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
