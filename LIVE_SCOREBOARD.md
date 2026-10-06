# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1953 | -1.95 | -38.00 | 51% | -3.8% |
| edge > 0 after fees | 774 | -3.53 | -27.31 | 45% | -7.5% |
| edge >= 5c | 417 | -4.32 | -18.00 | 41% | -9.9% |
| edge >= 7c (trade gate) | 305 | -6.12 | -18.66 | 37% | -14.7% |
| edge >= 10c | 193 | -8.73 | -16.84 | 32% | -22.2% |
| edge >= 15c | 95 | -15.82 | -15.03 | 20% | -45.9% |

Brier score (lower is better) — model 0.2250 vs market 0.2018.

3 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
