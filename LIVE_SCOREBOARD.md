# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1880 | -2.03 | -38.07 | 51% | -4.0% |
| edge > 0 after fees | 741 | -3.43 | -25.41 | 45% | -7.3% |
| edge >= 5c | 405 | -3.98 | -16.11 | 41% | -9.1% |
| edge >= 7c (trade gate) | 296 | -6.01 | -17.79 | 37% | -14.4% |
| edge >= 10c | 187 | -8.26 | -15.45 | 33% | -21.0% |
| edge >= 15c | 90 | -15.55 | -13.99 | 20% | -45.4% |

Brier score (lower is better) — model 0.2262 vs market 0.2040.

14 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
