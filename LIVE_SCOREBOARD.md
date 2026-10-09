# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1967 | -1.95 | -38.27 | 51% | -3.8% |
| edge > 0 after fees | 780 | -3.61 | -28.13 | 45% | -7.7% |
| edge >= 5c | 419 | -4.33 | -18.16 | 41% | -9.9% |
| edge >= 7c (trade gate) | 306 | -5.98 | -18.29 | 37% | -14.3% |
| edge >= 10c | 193 | -8.73 | -16.84 | 32% | -22.2% |
| edge >= 15c | 95 | -15.82 | -15.03 | 20% | -45.9% |

Brier score (lower is better) — model 0.2247 vs market 0.2017.

1 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
