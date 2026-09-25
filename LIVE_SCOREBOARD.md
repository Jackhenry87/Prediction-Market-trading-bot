# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1867 | -2.02 | -37.76 | 51% | -4.0% |
| edge > 0 after fees | 735 | -3.28 | -24.14 | 45% | -7.0% |
| edge >= 5c | 401 | -3.98 | -15.98 | 41% | -9.1% |
| edge >= 7c (trade gate) | 294 | -5.67 | -16.67 | 37% | -13.6% |
| edge >= 10c | 186 | -8.01 | -14.91 | 33% | -20.4% |
| edge >= 15c | 90 | -15.55 | -13.99 | 20% | -45.4% |

Brier score (lower is better) — model 0.2258 vs market 0.2037.

7 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
