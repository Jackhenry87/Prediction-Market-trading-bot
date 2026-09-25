# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1876 | -2.02 | -37.97 | 51% | -4.0% |
| edge > 0 after fees | 739 | -3.30 | -24.37 | 45% | -7.0% |
| edge >= 5c | 404 | -3.85 | -15.57 | 41% | -8.8% |
| edge >= 7c (trade gate) | 295 | -5.84 | -17.24 | 37% | -14.0% |
| edge >= 10c | 186 | -8.01 | -14.91 | 33% | -20.4% |
| edge >= 15c | 90 | -15.55 | -13.99 | 20% | -45.4% |

Brier score (lower is better) — model 0.2260 vs market 0.2039.

6 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
