# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1985 | -1.95 | -38.62 | 51% | -3.8% |
| edge > 0 after fees | 787 | -3.82 | -30.05 | 45% | -8.1% |
| edge >= 5c | 422 | -4.41 | -18.60 | 41% | -10.1% |
| edge >= 7c (trade gate) | 308 | -6.01 | -18.53 | 37% | -14.4% |
| edge >= 10c | 194 | -9.04 | -17.53 | 32% | -22.9% |
| edge >= 15c | 95 | -15.82 | -15.03 | 20% | -45.9% |

Brier score (lower is better) — model 0.2249 vs market 0.2018.

3 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
