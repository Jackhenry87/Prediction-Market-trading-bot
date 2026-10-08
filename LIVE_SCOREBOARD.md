# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1961 | -1.94 | -38.13 | 51% | -3.8% |
| edge > 0 after fees | 778 | -3.57 | -27.79 | 45% | -7.6% |
| edge >= 5c | 418 | -4.43 | -18.52 | 41% | -10.2% |
| edge >= 7c (trade gate) | 305 | -6.12 | -18.66 | 37% | -14.7% |
| edge >= 10c | 193 | -8.73 | -16.84 | 32% | -22.2% |
| edge >= 15c | 95 | -15.82 | -15.03 | 20% | -45.9% |

Brier score (lower is better) — model 0.2245 vs market 0.2014.

5 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
