# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1898 | -1.99 | -37.71 | 51% | -3.9% |
| edge > 0 after fees | 749 | -3.65 | -27.36 | 45% | -7.8% |
| edge >= 5c | 408 | -4.15 | -16.95 | 41% | -9.5% |
| edge >= 7c (trade gate) | 297 | -6.07 | -18.04 | 37% | -14.6% |
| edge >= 10c | 188 | -8.35 | -15.71 | 32% | -21.2% |
| edge >= 15c | 91 | -15.65 | -14.24 | 20% | -45.9% |

Brier score (lower is better) — model 0.2261 vs market 0.2032.

12 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
