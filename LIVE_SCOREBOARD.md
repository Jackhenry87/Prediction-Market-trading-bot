# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1857 | -2.02 | -37.57 | 51% | -4.0% |
| edge > 0 after fees | 733 | -3.31 | -24.27 | 45% | -7.1% |
| edge >= 5c | 399 | -4.04 | -16.10 | 41% | -9.2% |
| edge >= 7c (trade gate) | 293 | -5.45 | -15.97 | 38% | -13.1% |
| edge >= 10c | 186 | -8.01 | -14.91 | 33% | -20.4% |
| edge >= 15c | 90 | -15.55 | -13.99 | 20% | -45.4% |

Brier score (lower is better) — model 0.2252 vs market 0.2030.

2 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
